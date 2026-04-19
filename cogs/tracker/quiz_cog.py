"""
Quiz Session Cog — Automated MLBB lore quiz with double-header scheduling.
Features: 10 rounds per session, exact-match answer detection, time-decay scoring,
daily payout caps, EP/Token rewards, channel lock/unlock, and session leaderboard.
Runs twice daily at Noon and 8:00 PM (Asia/Manila).
"""

import discord
from discord.ext import commands, tasks
from discord import app_commands
import logging
import csv
import random
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone, time

from services.database import db
from services.settings_service import settings_service

logger = logging.getLogger("mlbb_bot.quiz_cog")

PHT = timezone(timedelta(hours=8))

# Schedule: Noon PHT = 04:00 UTC, 8PM PHT = 12:00 UTC
QUIZ_TIMES_UTC = [
    time(hour=4, minute=0, tzinfo=timezone.utc),   # Noon PHT
    time(hour=12, minute=0, tzinfo=timezone.utc),   # 8:00 PM PHT
]

# Reminder: 5 minutes before each quiz time
QUIZ_REMINDER_TIMES_UTC = [
    time(hour=3, minute=55, tzinfo=timezone.utc),   # 11:55 AM PHT
    time(hour=11, minute=55, tzinfo=timezone.utc),   # 7:55 PM PHT
]

ROUNDS_PER_SESSION = 10
SECONDS_PER_ROUND = 20
DELAY_BETWEEN_ROUNDS = 5

# Payout structure (EP)
PAYOUT_1ST = 150
PAYOUT_2ND = 100
PAYOUT_3RD = 75
PAYOUT_PARTICIPATION = 50

# Discord embed description limit
EMBED_DESC_LIMIT = 4096

CSV_PATH = "MLBB Quiz Questions.csv"
JOURNAL_PATH = "quiz_session_journal.json"


class QuizCog(commands.Cog, name="quiz"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.questions: list[dict] = []
        self.session_active = False
        self.session_lock = asyncio.Lock()
        self._quiz_channel_id: int | None = None  # Cached for startup cleanup
        # Track which question IDs were used today (noon + evening don't repeat)
        self._used_question_ids: set[str] = set()
        self._used_questions_date: str = ""  # YYYY-MM-DD in PHT

        # In-memory buffering & crash recovery journal
        self._session_data: dict = {}
        self._session_streaks: dict = {}

    async def cog_load(self):
        """Load questions from CSV on extension load."""
        self._load_questions()

    def cog_unload(self):
        self.quiz_scheduler.cancel()
        self.quiz_reminder.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        """Start scheduler and run crash recovery after bot is fully connected."""
        if not self.quiz_scheduler.is_running():
            self.quiz_scheduler.start()
        if not self.quiz_reminder.is_running():
            self.quiz_reminder.start()
        asyncio.create_task(self._startup_cleanup())

    def _load_questions(self):
        """Parse the MLBB Quiz CSV into memory."""
        self.questions = []
        try:
            import os
            # Resolve path: cogs/tracker/ -> cogs/ -> project root
            project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            csv_path = os.path.join(project_root, CSV_PATH)
            with open(csv_path, 'r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    q = row.get('QUESTION', '').strip()
                    a = row.get('ANSWER', '').strip()
                    if q and a:
                        self.questions.append({
                            "id": row.get('ID', ''),
                            "question": q,
                            "answer": a,
                        })
            logger.info(f"Loaded {len(self.questions)} quiz questions from CSV")
        except FileNotFoundError:
            logger.error("Quiz CSV not found at expected path. Questions will be empty.")
        except Exception as e:
            logger.error(f"Error loading quiz CSV: {e}")

    async def _startup_cleanup(self):
        """Re-lock the quiz channel on bot startup (crash recovery)."""
        try:
            channel_id = await settings_service.get_int("quiz_channel_id")
            if channel_id:
                self._quiz_channel_id = channel_id
                guild = self.bot.guilds[0] if self.bot.guilds else None
                if guild:
                    channel = guild.get_channel(channel_id)
                    if channel:
                        await self._lock_channel(channel, locked=True)
                        logger.info("Quiz channel re-locked on startup (crash recovery)")
                        # Sync any unfinished sessions to DB
                        await self._sync_journal_to_db(guild)
        except Exception as e:
            logger.error(f"Startup quiz cleanup error: {e}")

    async def _fetch_active_streaks(self) -> dict[int, dict]:
        """Fetch all users who currently have an active streak (>0)."""
        rows = await db.fetch_all("SELECT user_id, current_streak, max_streak FROM quiz_user_streaks WHERE current_streak > 0")
        return {int(r['user_id']): {"curr": r['current_streak'], "max": r['max_streak']} for r in rows}

    def _write_journal(self):
        """Write current session data to a local JSON file for crash recovery."""
        try:
            # We must convert int keys to strings for JSON
            payload = {
                "answer_logs": self._session_data.get("answer_logs", []),
                "question_stats": self._session_data.get("question_stats", {}),
                "user_streaks": {str(uid): s for uid, s in self._session_streaks.items()},
                "leaderboard": {str(uid): sc for uid, sc in self._session_data.get("leaderboard", {}).items()},
                "pht_date": datetime.now(PHT).strftime('%Y-%m-%d %H:%M:%S')
            }
            with open(JOURNAL_PATH, 'w') as f:
                json.dump(payload, f)
        except Exception as e:
            logger.error(f"Failed to write quiz journal: {e}")

    def _clear_journal(self):
        """Delete the local journal file after successful DB sync."""
        if os.path.exists(JOURNAL_PATH):
            try:
                os.remove(JOURNAL_PATH)
            except Exception as e:
                logger.error(f"Failed to delete journal: {e}")

    async def _sync_journal_to_db(self, guild: discord.Guild):
        """Replay a crashed session from JSON and commit to DB."""
        if not os.path.exists(JOURNAL_PATH):
            return

        try:
            with open(JOURNAL_PATH, 'r') as f:
                data = json.load(f)
            
            logger.info("Found unfinished quiz journal — syncing to database...")
            
            # 1. Sync Answer Logs
            if data.get("answer_logs"):
                await db.executemany('''
                    INSERT INTO quiz_answer_logs (user_id, question_id, question_text, time_taken, is_first)
                    VALUES (%s, %s, %s, %s, %s)
                ''', [tuple(x) for x in data["answer_logs"]])

            # 2. Sync Question Stats
            if data.get("question_stats"):
                stat_params = []
                for qid, s in data["question_stats"].items():
                    stat_params.append((qid, s['text'], s['asked'], s['correct'], s['time']))
                
                await db.executemany('''
                    INSERT INTO quiz_question_stats (question_id, question_text, times_asked, times_correct, total_time_taken)
                    VALUES (%s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE 
                        times_asked = times_asked + VALUES(times_asked),
                        times_correct = times_correct + VALUES(times_correct),
                        total_time_taken = total_time_taken + VALUES(total_time_taken),
                        question_text = VALUES(question_text)
                ''', stat_params)

            # 3. Sync User Streaks
            if data.get("user_streaks"):
                streak_params = []
                for uid_str, s in data["user_streaks"].items():
                    streak_params.append((int(uid_str), s['curr']))
                
                await db.executemany('''
                    INSERT INTO quiz_user_streaks (user_id, current_streak, max_streak, last_correct_at)
                    VALUES (%s, %s, 0, CURRENT_TIMESTAMP)
                    ON DUPLICATE KEY UPDATE 
                        current_streak = VALUES(current_streak),
                        max_streak = GREATEST(max_streak, VALUES(current_streak)),
                        last_correct_at = IF(VALUES(current_streak) > 0, CURRENT_TIMESTAMP, last_correct_at)
                ''', streak_params)

            # 4. Sync Leaderboard & History
            if data.get("leaderboard"):
                history_params = [(int(uid), score) for uid, score in data["leaderboard"].items() if score > 0]
                if history_params:
                    await db.executemany(
                        "INSERT INTO quiz_history (user_id, score) VALUES (%s, %s)",
                        history_params
                    )

            logger.info("Quiz journal sync complete.")
            self._clear_journal()

        except Exception as e:
            logger.error(f"Error syncing quiz journal: {e}", exc_info=True)

    # ─── SAFE CHANNEL SEND ──────────────────────────────────────────

    async def _safe_send(self, channel: discord.TextChannel, **kwargs) -> discord.Message | None:
        """Send a message with error handling for deleted/missing channels."""
        try:
            return await channel.send(**kwargs)
        except discord.NotFound:
            logger.error("Quiz channel was deleted mid-session.")
            return None
        except discord.Forbidden:
            logger.error("Bot lost send permission in quiz channel.")
            return None
        except discord.HTTPException as e:
            logger.error(f"HTTP error sending to quiz channel: {e}")
            return None

    # ─── SCHEDULER ──────────────────────────────────────────────────

    @tasks.loop(time=QUIZ_TIMES_UTC)
    async def quiz_scheduler(self):
        """Fires at Noon PHT and 8:00 PM PHT. Starts a quiz session."""
        await self.bot.wait_until_ready()
        logger.info("Quiz scheduler triggered")

        channel_id = await settings_service.get_int("quiz_channel_id")
        if not channel_id:
            logger.warning("No quiz channel configured. Skipping session.")
            return

        guild = self.bot.guilds[0] if self.bot.guilds else None
        if not guild:
            return

        channel = guild.get_channel(channel_id)
        if not channel:
            logger.error(f"Quiz channel {channel_id} not found")
            return

        await self.run_session(channel, guild)

    @quiz_scheduler.before_loop
    async def before_scheduler(self):
        await self.bot.wait_until_ready()

    # ─── PRE-QUIZ REMINDER ───────────────────────────────────────────

    @tasks.loop(time=QUIZ_REMINDER_TIMES_UTC)
    async def quiz_reminder(self):
        """Fires 5 minutes before each quiz. Pings the Quiz Notification role."""
        await self.bot.wait_until_ready()

        channel_id = await settings_service.get_int("quiz_channel_id")
        if not channel_id:
            return

        guild = self.bot.guilds[0] if self.bot.guilds else None
        if not guild:
            return

        channel = guild.get_channel(channel_id)
        if not channel:
            return

        # Find the Quiz Notification role by exact name
        role = discord.utils.get(guild.roles, name="Quiz Notification")
        if not role:
            logger.warning("Quiz Notification role not found — skipping reminder.")
            return

        now_pht = datetime.now(PHT).strftime("%I:%M %p")
        try:
            embed = discord.Embed(
                title="🧠 Quiz Starting Soon!",
                description=(
                    f"A quiz session begins in **5 minutes**!\n\n"
                    f"📍 Get ready right here in {channel.mention}\n"
                    f"⏱️ **{ROUNDS_PER_SESSION} rounds** • **{SECONDS_PER_ROUND}s** per question\n"
                    f"🏆 Top 3 earn EP rewards!"
                ),
                color=discord.Color.gold(),
            )
            embed.set_footer(text=f"Reminder sent at {now_pht} PHT")
            await channel.send(content=role.mention, embed=embed)
            logger.info("Quiz reminder sent successfully.")
        except discord.Forbidden:
            logger.error("Bot lacks permission to send quiz reminder.")
        except discord.HTTPException as e:
            logger.error(f"Failed to send quiz reminder: {e}")

    @quiz_reminder.before_loop
    async def before_reminder(self):
        await self.bot.wait_until_ready()

    # ─── SESSION ENGINE ─────────────────────────────────────────────

    async def run_session(self, channel: discord.TextChannel, guild: discord.Guild):
        """Execute a full 10-round quiz session with locking."""
        async with self.session_lock:
            if self.session_active:
                logger.warning("Session already active — skipping duplicate trigger.")
                return
            self.session_active = True

        try:
            await self._execute_session(channel, guild)
        except Exception as e:
            logger.error(f"Quiz session crashed: {e}", exc_info=True)
            await self._safe_send(
                channel,
                content="❌ **Quiz Error:** The session encountered an unexpected error and has ended."
            )
        finally:
            # ALWAYS re-lock the channel and clear state, even on crash
            try:
                await self._lock_channel(channel, locked=True)
            except Exception:
                pass
            self.session_active = False

    async def _execute_session(self, channel: discord.TextChannel, guild: discord.Guild):
        """Core session loop: unlock → 10 rounds → payout → lock."""
        loop = asyncio.get_running_loop()
        session_start = loop.time()

        if len(self.questions) < ROUNDS_PER_SESSION:
            await self._safe_send(channel, content="❌ Not enough questions loaded. Quiz cancelled.")
            return

        # Reset used-question tracker at midnight PHT (new day = fresh pool)
        today_pht = datetime.now(PHT).strftime('%Y-%m-%d')
        if self._used_questions_date != today_pht:
            self._used_question_ids = set()
            self._used_questions_date = today_pht

        # Select 10 questions that weren't used in today's earlier session
        available = [q for q in self.questions if q['id'] not in self._used_question_ids]
        if len(available) < ROUNDS_PER_SESSION:
            # Fallback: if not enough unused questions, use the full pool
            available = self.questions
            logger.warning("Not enough unused questions for dedup — using full pool.")

        session_questions = random.sample(available, ROUNDS_PER_SESSION)

        # Mark these as used for today
        for q in session_questions:
            self._used_question_ids.add(q['id'])

        # Buffer session data for bulk DB write at the end
        self._session_data = {
            "answer_logs": [],
            "question_stats": {}, # qid -> {asked, correct, time, text}
            "leaderboard": {}      # uid -> total_score
        }
        self._session_streaks = await self._fetch_active_streaks()

        # In-memory session leaderboard: {user_id: total_score}
        # (We use self._session_data['leaderboard'] instead of local 'leaderboard')
        leaderboard = self._session_data['leaderboard']

        # ─── UNLOCK CHANNEL ─────────────────────────────────────────
        await self._lock_channel(channel, locked=False)

        now_pht = datetime.now(PHT).strftime("%I:%M %p")
        start_embed = discord.Embed(
            title="🧠 MLBB Quiz Session Starting!",
            description=(
                f"**{ROUNDS_PER_SESSION} rounds** of MLBB lore trivia!\n\n"
                f"⏱️ **{SECONDS_PER_ROUND} seconds** per question\n"
                f"📊 **Scoring:** Faster answers = more points (max 1000)\n"
                f"🏆 **Prizes:** 1st: {PAYOUT_1ST} EP | 2nd: {PAYOUT_2ND} EP | 3rd: {PAYOUT_3RD} EP\n"
                f"🎖️ **Participation:** {PAYOUT_PARTICIPATION} EP (1+ correct answer)\n\n"
                f"*Type the exact answer in chat. Everyone who answers correctly earns points!*"
            ),
            color=discord.Color.gold(),
            timestamp=discord.utils.utcnow()
        )
        start_embed.set_footer(text=f"Session started at {now_pht} PHT")
        msg = await self._safe_send(channel, embed=start_embed)
        if not msg:
            return  # Channel is gone or bot lost perms — abort cleanly
        await asyncio.sleep(5)

        # ─── ROUNDS ─────────────────────────────────────────────────
        rounds_start = loop.time()
        for round_num, q in enumerate(session_questions, 1):
            try:
                await self._run_round(channel, round_num, q, leaderboard)
            except Exception as e:
                logger.error(f"Round {round_num} error: {e}", exc_info=True)
                await self._safe_send(
                    channel,
                    content=f"⚠️ Round {round_num} encountered an error. Skipping to next round."
                )

            # Delay and cleanup for each round is now handled internally by _run_round.

        rounds_elapsed = loop.time() - rounds_start

        # ─── LOCK CHANNEL ────────────────────────────────────────────
        await self._lock_channel(channel, locked=True)

        finalize_start = loop.time()
        # ─── FINAL LEADERBOARD & PAYOUTS ─────────────────────────────
        await self._finalize_session(channel, guild, leaderboard)
        finalize_elapsed = loop.time() - finalize_start

        session_elapsed = loop.time() - session_start
        logger.info(
            f"Quiz session completed: total={session_elapsed:.1f}s, "
            f"rounds={rounds_elapsed:.1f}s, finalize_embed={finalize_elapsed:.1f}s"
        )

    async def _run_round(self, channel: discord.TextChannel, round_num: int, question: dict, leaderboard: dict):
        """
        Run a single round. The channel stays open for the full 20 seconds.
        EVERYONE who types the correct answer earns time-decay points.
        Each user can only score once per round.
        """
        correct_answer = question['answer'].strip().lower()

        # Buffer global question stats (times asked)
        qid = question['id']
        if qid not in self._session_data['question_stats']:
            self._session_data['question_stats'][qid] = {
                "text": question['question'],
                "asked": 1,
                "correct": 0,
                "time": 0.0
            }
        else:
            self._session_data['question_stats'][qid]['asked'] += 1

        # Post the question
        q_embed = discord.Embed(
            title=f"📋 Round {round_num}/{ROUNDS_PER_SESSION}",
            description=f"**{question['question']}**",
            color=discord.Color.blue()
        )
        q_embed.set_footer(text=f"⏱️ {SECONDS_PER_ROUND}s | Type the exact answer!")
        q_msg = await self._safe_send(channel, embed=q_embed)
        if not q_msg:
            return  # Channel gone — skip this round

        question_timestamp = q_msg.created_at.timestamp()

        # Collect all correct answers during the window
        round_scorers: list[dict] = []
        scored_users: set[int] = set()

        def on_message_check(msg: discord.Message) -> bool:
            if msg.channel.id != channel.id or msg.author.bot or msg.author.id in scored_users:
                return False
            return msg.content.strip().lower() == correct_answer

        async def collect_answers():
            loop = asyncio.get_running_loop()
            while True:
                try:
                    elapsed = loop.time() - start_time
                    remaining = SECONDS_PER_ROUND - elapsed
                    if remaining <= 0.1:
                        break

                    msg = await self.bot.wait_for('message', check=on_message_check, timeout=remaining)

                    answer_ts = msg.created_at.timestamp()
                    time_taken = max(0.0, min(answer_ts - question_timestamp, float(SECONDS_PER_ROUND)))
                    score = max(0, round(1000 * (1 - time_taken / SECONDS_PER_ROUND)))

                    scored_users.add(msg.author.id)
                    round_scorers.append({
                        "user_id": msg.author.id,
                        "mention": msg.author.mention,
                        "display_name": msg.author.display_name,
                        "score": score,
                        "time_taken": time_taken,
                    })
                    leaderboard[msg.author.id] = leaderboard.get(msg.author.id, 0) + score

                except asyncio.TimeoutError:
                    break
                except Exception as e:
                    logger.error(f"Error in collect_answers loop: {e}")
                    break

        start_time = asyncio.get_running_loop().time()
        await collect_answers()

        # ─── PROCESS RESULTS ─────────────────────────────────────────
        win_msg = None
        if round_scorers:
            round_scorers.sort(key=lambda x: x['time_taken'])

            # Buffer logs, stats, and streaks
            for i, s in enumerate(round_scorers):
                is_first = (i == 0)
                # 1. Answer Logs
                self._session_data['answer_logs'].append((s['user_id'], qid, question['question'], s['time_taken'], is_first))
                
                # 2. Question Stats
                self._session_data['question_stats'][qid]['correct'] += 1
                self._session_data['question_stats'][qid]['time'] += s['time_taken']

                # 3. User Streaks (Increment)
                uid = s['user_id']
                if uid not in self._session_streaks:
                    self._session_streaks[uid] = {"curr": 1}
                else:
                    self._session_streaks[uid]['curr'] += 1

            # 4. Reset Streaks for anyone who missed
            for uid, s_info in self._session_streaks.items():
                if uid not in scored_users:
                    s_info['curr'] = 0

            # Write journal to disk for crash recovery
            self._write_journal()

            # Display round results
            display_limit = 10
            shown = round_scorers[:display_limit]
            extra = len(round_scorers) - display_limit

            lines = [f"{'🏆' if i == 0 else '✅'} {s['mention']} — **+{s['score']} pts** ({s['time_taken']:.2f}s)" 
                    for i, s in enumerate(shown)]
            if extra > 0:
                lines.append(f"*...and {extra} more also scored!*")

            win_embed = discord.Embed(
                title=f"✅ Round {round_num} — {len(round_scorers)} correct!",
                description="\n".join(lines),
                color=discord.Color.green()
            )
            win_msg = await self._safe_send(channel, embed=win_embed)
        else:
            # Everyone missed — reset ALL active streaks
            for s_info in self._session_streaks.values():
                s_info['curr'] = 0
            
            self._write_journal()

            timeout_embed = discord.Embed(
                title=f"⏰ Round {round_num} — Time's Up!",
                description="Nobody got it!",
                color=discord.Color.red()
            )
            win_msg = await self._safe_send(channel, embed=timeout_embed)

        await self._safe_send(channel, content=f"🤫 **The correct answer was:** `{question['answer']}`")

        # ─── ROUND CLEANUP (PURGE) ───────────────────────────────────
        await asyncio.sleep(DELAY_BETWEEN_ROUNDS)
        try:
            after_time = q_msg.created_at - timedelta(seconds=1)
            def check_purge(m: discord.Message) -> bool:
                return not (win_msg and m.id == win_msg.id)
            await channel.purge(limit=50, after=after_time, check=check_purge)
        except Exception as e:
            logger.error(f"Error purging round {round_num} messages: {e}")

    # ─── FINALIZE & PAYOUT ──────────────────────────────────────────

    async def _finalize_session(self, channel: discord.TextChannel, guild: discord.Guild, leaderboard: dict):
        """
        Build final leaderboard with CASCADING EP payouts.
        
        Users who already claimed EP today are skipped when assigning tiers.
        The highest-scoring ELIGIBLE user gets 1st-place EP, next eligible gets 2nd, etc.
        Everyone still appears on the visual leaderboard ranked by score.
        """
        if not leaderboard:
            await self._safe_send(channel, embed=discord.Embed(
                title="📊 Quiz Session Complete!",
                description="No one scored any points this session! Better luck next time. 🎲",
                color=discord.Color.greyple()
            ))
            return

        # Sort by score descending, then user_id ascending (deterministic tiebreak)
        sorted_board = sorted(leaderboard.items(), key=lambda x: (-x[1], x[0]))

        today_str = datetime.now(PHT).strftime('%Y-%m-%d')

        # ─── FAST PATH: Determine who is eligible (not yet paid today) ─────
        user_ids = [uid for uid, _ in sorted_board]
        placeholders = ', '.join(['%s'] * len(user_ids))
        already_paid_rows = await db.fetch_all(
            f"SELECT user_id FROM quiz_payouts WHERE user_id IN ({placeholders}) AND payout_date = %s",
            (*user_ids, today_str)
        )
        already_paid_set = {row['user_id'] for row in already_paid_rows}

        # ─── PHASE 2: Assign EP tiers via cascading ─────────────────────
        # Walk the sorted leaderboard; skip already-paid users for tier assignment
        payout_tiers = [PAYOUT_1ST, PAYOUT_2ND, PAYOUT_3RD]  # Top 3 tiers
        tier_index = 0  # Which tier to assign next
        user_payouts: dict[int, int] = {}  # user_id → EP to award

        for user_id, score in sorted_board:
            if user_id in already_paid_set:
                continue  # Skip — already claimed today
            if tier_index < len(payout_tiers):
                user_payouts[user_id] = payout_tiers[tier_index]
                tier_index += 1
            else:
                user_payouts[user_id] = PAYOUT_PARTICIPATION

        # ─── PHASE 3: Process payouts and build visual leaderboard ───────
        lines = []
        display_limit = 10

        for visual_rank, (user_id, score) in enumerate(sorted_board):
            member = guild.get_member(user_id)
            name = member.mention if member else f"<@{user_id}>"
            # Visual medal based on SCORE rank (not payout rank)
            if visual_rank == 0:
                medal = "🥇"
            elif visual_rank == 1:
                medal = "🥈"
            elif visual_rank == 2:
                medal = "🥉"
            else:
                medal = "🏅"

            if user_id in already_paid_set:
                # Already claimed — show on leaderboard but no EP
                line = f"{medal} {name} — **{score} pts** | *Already claimed today*"
            elif user_id in user_payouts:
                ep = user_payouts[user_id]
                line = f"{medal} {name} — **{score} pts** | +**{ep} EP** ✅"
            else:
                line = f"{medal} {name} — **{score} pts**"

            if visual_rank < display_limit:
                lines.append(line)

        extra = len(sorted_board) - display_limit
        if extra > 0:
            lines.append(f"*...and {extra} more participants also earned EP!*")

        final_embed = discord.Embed(
            title="🏆 Quiz Session Complete — Final Standings!",
            description="\n".join(lines),
            color=discord.Color.gold(),
            timestamp=discord.utils.utcnow()
        )
        final_embed.set_footer(text="Next session runs automatically. Daily EP cap: 1 payout per user per day.")
        await self._safe_send(channel, embed=final_embed)
        
        # ─── SLOW PATH: EP updates + history in background ──────────────
        asyncio.create_task(
            self._process_payouts_background(guild, user_payouts, sorted_board, today_str)
        )

    async def _process_payouts_background(self, guild: discord.Guild, user_payouts: dict, sorted_board: list, today_str: str):
        """
        Background task: commit session data, process EP updates, and record history.
        """
        loop = asyncio.get_running_loop()
        payout_start = loop.time()
        
        # ─── PHASE 1: Bulk Commit Session Data ─────────────────────
        try:
            # 1. Answer Logs
            if self._session_data.get("answer_logs"):
                await db.executemany('''
                    INSERT INTO quiz_answer_logs (user_id, question_id, question_text, time_taken, is_first)
                    VALUES (%s, %s, %s, %s, %s)
                ''', self._session_data["answer_logs"])

            # 2. Question Stats
            if self._session_data.get("question_stats"):
                stat_params = []
                for qid, s in self._session_data["question_stats"].items():
                    stat_params.append((qid, s['text'], s['asked'], s['correct'], s['time']))
                
                await db.executemany('''
                    INSERT INTO quiz_question_stats (question_id, question_text, times_asked, times_correct, total_time_taken)
                    VALUES (%s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE 
                        times_asked = times_asked + VALUES(times_asked),
                        times_correct = times_correct + VALUES(times_correct),
                        total_time_taken = total_time_taken + VALUES(total_time_taken),
                        question_text = VALUES(question_text)
                ''', stat_params)

            # 3. User Streaks
            if self._session_streaks:
                streak_params = []
                for uid, s in self._session_streaks.items():
                    streak_params.append((uid, s['curr']))
                
                await db.executemany('''
                    INSERT INTO quiz_user_streaks (user_id, current_streak, max_streak, last_correct_at)
                    VALUES (%s, %s, 0, CURRENT_TIMESTAMP)
                    ON DUPLICATE KEY UPDATE 
                        current_streak = VALUES(current_streak),
                        max_streak = GREATEST(max_streak, VALUES(current_streak)),
                        last_correct_at = IF(VALUES(current_streak) > 0, CURRENT_TIMESTAMP, last_correct_at)
                ''', streak_params)

            # 4. Success! Clear the journal
            self._clear_journal()
            logger.info("Quiz session data successfully committed to database.")

        except Exception as e:
            logger.error(f"Failed to commit quiz session data: {e}", exc_info=True)

        # ─── PHASE 2: EP Updates ───────────────────────────────────
        success_count = 0
        error_count = 0

        try:
            from services.ep_service import ep_service

            for user_id, ep in user_payouts.items():
                try:
                    await ep_service.process_ep_update(guild, user_id, ep)
                    await db.execute(
                        "INSERT INTO quiz_payouts (user_id, payout_date, ep_awarded) "
                        "VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE user_id = VALUES(user_id)",
                        (user_id, today_str, ep)
                    )
                    success_count += 1
                except Exception as e:
                    error_count += 1
                    logger.error(f"Background EP payout failed for {user_id}: {e}")
                # Rate limit protection
                await asyncio.sleep(0.5)

            # Batch record quiz_history
            history_params = [(uid, score) for uid, score in sorted_board if score > 0]
            if history_params:
                await db.executemany(
                    "INSERT INTO quiz_history (user_id, score) VALUES (%s, %s)",
                    history_params
                )

        except Exception as e:
            logger.error(f"Background payout task crashed: {e}", exc_info=True)

        elapsed = loop.time() - payout_start
        logger.info(
            f"Quiz background processing completed: {success_count} EP ok, "
            f"{error_count} EP errors, {elapsed:.1f}s total elapsed"
        )

    # ─── CHANNEL LOCK/UNLOCK ────────────────────────────────────────

    async def _lock_channel(self, channel: discord.TextChannel, locked: bool):
        """
        Toggle send_messages permission for @everyone in the quiz channel.
        
        Edge cases:
        - Bot lacks Manage Channels → logged, doesn't crash
        - Channel deleted → discord.NotFound caught
        - Rate limited → discord.HTTPException caught
        """
        try:
            overwrite = channel.overwrites_for(channel.guild.default_role)
            overwrite.send_messages = not locked
            await channel.set_permissions(
                channel.guild.default_role,
                overwrite=overwrite,
                reason=f"Quiz session {'ended' if locked else 'started'}"
            )
        except discord.NotFound:
            logger.error("Quiz channel no longer exists. Cannot modify permissions.")
        except discord.Forbidden:
            logger.error("Bot lacks Manage Channels permission for quiz channel.")
        except discord.HTTPException as e:
            logger.error(f"HTTP error modifying quiz channel permissions: {e}")
        except Exception as e:
            logger.error(f"Unexpected channel lock/unlock error: {e}")

    # ─── MANUAL COMMANDS ────────────────────────────────────────────

    quiz_group = app_commands.Group(
        name="quiz",
        description="MLBB Quiz management commands.",
        default_permissions=discord.Permissions(administrator=True)
    )

    @quiz_group.command(name="start", description="Manually start a quiz session now.")
    async def quiz_start(self, interaction: discord.Interaction):
        if self.session_active:
            return await interaction.response.send_message("❌ A quiz session is already running!", ephemeral=True)

        channel_id = await settings_service.get_int("quiz_channel_id")
        if not channel_id:
            return await interaction.response.send_message("❌ No quiz channel configured. Use `/setup quiz_channel`.", ephemeral=True)

        channel = interaction.guild.get_channel(channel_id)
        if not channel:
            return await interaction.response.send_message("❌ Quiz channel not found.", ephemeral=True)

        await interaction.response.send_message("✅ Quiz session starting now!", ephemeral=True)
        asyncio.create_task(self.run_session(channel, interaction.guild))

    @quiz_group.command(name="stop", description="Force-stop a running quiz session.")
    async def quiz_stop(self, interaction: discord.Interaction):
        if not self.session_active:
            return await interaction.response.send_message("❌ No quiz session is currently running.", ephemeral=True)

        # Set flag to false — the session loop checks this and will exit
        self.session_active = False

        channel_id = await settings_service.get_int("quiz_channel_id")
        if channel_id:
            channel = interaction.guild.get_channel(channel_id)
            if channel:
                await self._lock_channel(channel, locked=True)

        await interaction.response.send_message("✅ Quiz session force-stopped and channel re-locked.", ephemeral=True)

    @quiz_group.command(name="reload", description="Reload quiz questions from CSV.")
    async def quiz_reload(self, interaction: discord.Interaction):
        if self.session_active:
            return await interaction.response.send_message("❌ Can't reload while a session is running.", ephemeral=True)
        self._load_questions()
        await interaction.response.send_message(f"✅ Reloaded **{len(self.questions)}** questions from CSV.", ephemeral=True)

    @quiz_group.command(name="status", description="Check quiz system status.")
    async def quiz_status(self, interaction: discord.Interaction):
        channel_id = await settings_service.get_int("quiz_channel_id")
        embed = discord.Embed(title="🧠 Quiz System Status", color=discord.Color.blue())
        embed.add_field(name="Questions Loaded", value=f"**{len(self.questions)}**", inline=True)
        embed.add_field(name="Session Active", value="🟢 Yes" if self.session_active else "🔴 No", inline=True)
        embed.add_field(name="Channel", value=f"<#{channel_id}>" if channel_id else "Not configured", inline=True)
        embed.add_field(name="Schedule", value="Noon & 8:00 PM PHT daily", inline=True)
        embed.add_field(name="Rounds/Session", value=f"**{ROUNDS_PER_SESSION}**", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="quiz-leaderboard", description="View all-time quiz EP earnings.")
    async def quiz_leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        top = await db.fetch_all('''
            SELECT user_id, SUM(ep_awarded) as total_ep, COUNT(*) as sessions
            FROM quiz_payouts
            GROUP BY user_id ORDER BY total_ep DESC LIMIT 10
        ''')
        if not top:
            return await interaction.followup.send("No quiz data yet.")

        lines = []
        for i, row in enumerate(top, 1):
            medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else "🏅"
            lines.append(f"{medal} <@{row['user_id']}> — **{row['total_ep']} EP** ({row['sessions']} payouts)")

        embed = discord.Embed(title="🏆 All-Time Quiz Champions", description="\n".join(lines), color=discord.Color.gold())
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(QuizCog(bot))
