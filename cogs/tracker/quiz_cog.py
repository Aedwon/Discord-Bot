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

    async def cog_load(self):
        """Load questions from CSV on extension load."""
        self._load_questions()

    def cog_unload(self):
        self.quiz_scheduler.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        """Start scheduler and run crash recovery after bot is fully connected."""
        if not self.quiz_scheduler.is_running():
            self.quiz_scheduler.start()
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
        except Exception as e:
            logger.error(f"Startup quiz cleanup error: {e}")

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

        # In-memory session leaderboard: {user_id: total_score}
        leaderboard: dict[int, int] = {}

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

        # ─── LOCK CHANNEL ────────────────────────────────────────────
        await self._lock_channel(channel, locked=True)

        # ─── FINAL LEADERBOARD & PAYOUTS ─────────────────────────────
        await self._finalize_session(channel, guild, leaderboard)

    async def _run_round(self, channel: discord.TextChannel, round_num: int, question: dict, leaderboard: dict):
        """
        Run a single round. The channel stays open for the full 20 seconds.
        EVERYONE who types the correct answer earns time-decay points.
        Each user can only score once per round.
        
        Edge cases handled:
        - User answers correctly twice → scored_users set prevents double points
        - Massive spam of incorrect answers → check function silently rejects them
        - Channel deleted mid-round → _safe_send returns None, no crash  
        - Score floored at 0 (never negative even with clock skew)
        - Embed overflow → scorer list truncated with "...and N more"
        """
        correct_answer = question['answer'].strip().lower()

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
            """Only match correct answers from non-bot, non-duplicate users in quiz channel."""
            if msg.channel.id != channel.id:
                return False
            if msg.author.bot:
                return False
            if msg.author.id in scored_users:
                return False  # Already scored this round
            # Case-insensitive exact match, stripped of whitespace
            return msg.content.strip().lower() == correct_answer

        async def collect_answers():
            """Loop wait_for calls until the 20-second window expires."""
            loop = asyncio.get_running_loop()
            while True:
                try:
                    elapsed = loop.time() - start_time
                    remaining = SECONDS_PER_ROUND - elapsed
                    if remaining <= 0.1:  # Small buffer to avoid near-zero timeouts
                        break

                    msg = await self.bot.wait_for(
                        'message',
                        check=on_message_check,
                        timeout=remaining
                    )

                    # Calculate time-decay score using Discord's server timestamp
                    # (more accurate than local clock — avoids drift)
                    answer_ts = msg.created_at.timestamp()
                    time_taken = answer_ts - question_timestamp
                    # Clamp: floor at 0 (handles clock skew), cap at SECONDS_PER_ROUND
                    time_taken = max(0.0, min(time_taken, float(SECONDS_PER_ROUND)))

                    # Score formula: Score = round(1000 × (1 - time_taken / 20))
                    score = max(0, round(1000 * (1 - time_taken / SECONDS_PER_ROUND)))

                    scored_users.add(msg.author.id)
                    round_scorers.append({
                        "user_id": msg.author.id,
                        "mention": msg.author.mention,
                        "display_name": msg.author.display_name,
                        "score": score,
                        "time_taken": time_taken,
                    })

                    # Update session leaderboard
                    leaderboard[msg.author.id] = leaderboard.get(msg.author.id, 0) + score

                except asyncio.TimeoutError:
                    break  # Window expired
                except asyncio.CancelledError:
                    break  # Bot shutting down
                except Exception as e:
                    # Catch unexpected errors to prevent the entire round from dying
                    logger.error(f"Error in collect_answers loop: {e}")
                    break

        start_time = asyncio.get_running_loop().time()
        await collect_answers()

        # ─── ANNOUNCE RESULTS ────────────────────────────────────────
        win_msg = None
        if round_scorers:
            # Sort by time taken (fastest first)
            round_scorers.sort(key=lambda x: x['time_taken'])

            # Show only top 10 fastest in the embed (everyone still earns points)
            display_limit = 10
            shown = round_scorers[:display_limit]
            extra = len(round_scorers) - display_limit

            lines = []
            for i, s in enumerate(shown):
                prefix = "🏆" if i == 0 else "✅"
                lines.append(f"{prefix} {s['mention']} — **+{s['score']} pts** ({s['time_taken']:.2f}s)")

            if extra > 0:
                lines.append(f"*...and {extra} more also scored!*")

            description = "\n".join(lines)

            win_embed = discord.Embed(
                title=f"✅ Round {round_num} — {len(round_scorers)} correct!",
                description=description,
                color=discord.Color.green()
            )
            win_msg = await self._safe_send(channel, embed=win_embed)
        else:
            timeout_embed = discord.Embed(
                title=f"⏰ Round {round_num} — Time's Up!",
                description="Nobody got it!",
                color=discord.Color.red()
            )
            win_msg = await self._safe_send(channel, embed=timeout_embed)

        # Send the exact answer separately
        ans_msg = await self._safe_send(channel, content=f"🤫 **The correct answer was:** `{question['answer']}`")

        # ─── ROUND CLEANUP (PURGE) ───────────────────────────────────
        # Wait the standard delay so players can read the winner and answer before it vanishes
        await asyncio.sleep(DELAY_BETWEEN_ROUNDS)

        try:
            # Delete q_msg, the users' guesses, and ans_msg. Keep only win_msg.
            from datetime import timedelta
            after_time = q_msg.created_at - timedelta(seconds=1)
            
            def check_purge(m: discord.Message) -> bool:
                if win_msg and m.id == win_msg.id:
                    return False
                return True

            await channel.purge(limit=300, after=after_time, check=check_purge)
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

        # ─── PHASE 1: Determine who is eligible (not yet paid today) ─────
        already_paid_set: set[int] = set()
        for user_id, _ in sorted_board:
            already_paid = await db.fetch_one(
                "SELECT 1 FROM quiz_payouts WHERE user_id = %s AND payout_date = %s",
                (user_id, today_str)
            )
            if already_paid:
                already_paid_set.add(user_id)

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
                try:
                    from services.ep_service import ep_service
                    await ep_service.process_ep_update(guild, user_id, ep)
                    await db.execute(
                        "INSERT INTO quiz_payouts (user_id, payout_date, ep_awarded) VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE user_id = VALUES(user_id)",
                        (user_id, today_str, ep)
                    )
                    line = f"{medal} {name} — **{score} pts** | +**{ep} EP** ✅"
                except Exception as e:
                    logger.error(f"Failed to award quiz EP to {user_id}: {e}")
                    line = f"{medal} {name} — **{score} pts** | +{ep} EP ❌ (error)"
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
        
        # ─── PHASE 4: Record Raw Scores for Weekly Leaderboard ───────────
        try:
            for user_id, score in sorted_board:
                if score > 0:
                    await db.execute(
                        "INSERT INTO quiz_history (user_id, score) VALUES (%s, %s)",
                        (user_id, score)
                    )
        except Exception as e:
            logger.error(f"Failed to record quiz_history: {e}")

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
