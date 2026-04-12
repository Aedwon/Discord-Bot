"""
Dual Leaderboard Cog — Weekly & All-Time.

Manages two independent leaderboard channels with premium embed UX.
Updates every 5 minutes with MSL exclusion. Weekly board resets
every Monday 00:00 UTC+8 (Sunday 16:00 UTC).

Leaderboard categories:
  All-Time: XP, EP, Quiz, Counting, Referral, Boosting, Voice, Messages
  Weekly:   XP, EP, Quiz, Counting, Referral, Voice, Messages
"""

import discord
from discord.ext import commands, tasks
import logging
import time as time_module
from datetime import datetime, timedelta, timezone, time

from services.database import db
from services.settings_service import settings_service
from services.leaderboard_service import leaderboard_service, TZ_PHT
from services.xp_service import xp_service
from services.ep_service import ep_service

logger = logging.getLogger("mlbb_bot.leaderboard")

# ─── VISUAL CONSTANTS ──────────────────────────────────────────────────

# Category colour palette
CLR_XP       = 0x5865F2   # Discord Blurple
CLR_EP       = 0xF5A623   # Amber
CLR_QUIZ     = 0x9B59B6   # Purple
CLR_COUNTING = 0x2ECC71   # Emerald
CLR_REFERRAL = 0xE91E63   # Pink
CLR_BOOST    = 0xF47FFF   # Nitro pink
CLR_VOICE    = 0x3498DB   # Sky blue
CLR_MESSAGE  = 0x1ABC9C   # Teal

# Header embeds
CLR_WEEKLY_HEADER  = 0x5865F2
CLR_ALLTIME_HEADER = 0xF2C21A  # Gold

# Podium medals
MEDALS = ["👑", "🥈", "🥉"]

# Weekly reset: Monday 00:00 PHT = Sunday 16:00 UTC
RESET_TIME_UTC = time(hour=16, minute=0, second=0, tzinfo=timezone.utc)


class LeaderboardCog(commands.Cog, name="leaderboards"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        """Ensure required tables exist even on hot-reload."""
        try:
            await db.execute('''
                CREATE TABLE IF NOT EXISTS weekly_leaderboard_snapshots (
                    user_id BIGINT PRIMARY KEY,
                    xp_snapshot INT DEFAULT 0,
                    ep_snapshot INT DEFAULT 0,
                    snapshot_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            await db.execute('''
                CREATE TABLE IF NOT EXISTS counting_weekly_contributors (
                    guild_id BIGINT,
                    user_id BIGINT,
                    count INT DEFAULT 0,
                    PRIMARY KEY (guild_id, user_id)
                )
            ''')
            logger.info("Leaderboard tables verified/created.")
        except Exception as e:
            logger.error(f"Failed to ensure leaderboard tables: {e}")

    def cog_unload(self):
        self.update_leaderboards.cancel()
        self.weekly_reset_task.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.update_leaderboards.is_running():
            self.update_leaderboards.start()
            logger.info("Leaderboard 5-min update loop started.")
        if not self.weekly_reset_task.is_running():
            self.weekly_reset_task.start()
            logger.info("Leaderboard weekly reset task started.")

    # ═══════════════════════════════════════════════════════════════════
    #  5-MINUTE UPDATE LOOP
    # ═══════════════════════════════════════════════════════════════════

    @tasks.loop(minutes=5)
    async def update_leaderboards(self):
        """Master loop: refresh both leaderboard channels every 5 minutes."""
        try:
            for guild in self.bot.guilds:
                # Fetch MSL exclusion set once per cycle
                exclude_ids = await leaderboard_service.get_msl_user_ids()

                # Update each channel independently — one failing shouldn't
                # prevent the other from updating
                try:
                    await self._update_alltime_channel(guild, exclude_ids)
                except Exception as e:
                    logger.error(f"All-time leaderboard update failed: {e}", exc_info=True)

                try:
                    await self._update_weekly_channel(guild, exclude_ids)
                except Exception as e:
                    logger.error(f"Weekly leaderboard update failed: {e}", exc_info=True)

        except Exception as e:
            logger.error(f"Fatal error in leaderboard loop: {e}", exc_info=True)

    @update_leaderboards.before_loop
    async def before_update(self):
        await self.bot.wait_until_ready()

    # ═══════════════════════════════════════════════════════════════════
    #  WEEKLY RESET TASK
    # ═══════════════════════════════════════════════════════════════════

    @tasks.loop(time=RESET_TIME_UTC)
    async def weekly_reset_task(self):
        """
        Fires daily at Sunday 16:00 UTC (Monday 00:00 PHT).
        Only executes if today is actually Monday in PHT and
        hasn't already run this week.
        """
        now_pht = datetime.now(TZ_PHT)

        # Must be Monday (weekday 0)
        if now_pht.weekday() != 0:
            return

        # Double-execution guard
        iso_week = now_pht.strftime("%Y-W%W")
        last_reset = await settings_service.get("leaderboard_last_reset_week")
        if last_reset == iso_week:
            return

        count = await leaderboard_service.run_weekly_reset()
        await settings_service.set("leaderboard_last_reset_week", iso_week)
        logger.info(f"Weekly leaderboard reset: {count} users snapshotted (week {iso_week})")

    @weekly_reset_task.before_loop
    async def before_weekly_reset(self):
        await self.bot.wait_until_ready()

    # ═══════════════════════════════════════════════════════════════════
    #  SAFE QUERY WRAPPER
    # ═══════════════════════════════════════════════════════════════════

    async def _safe_query(self, coro, label: str, default=None):
        """Run a query coroutine with error handling. Returns default on failure."""
        try:
            return await coro
        except Exception as e:
            logger.error(f"Leaderboard query failed [{label}]: {e}")
            return default if default is not None else []

    # ═══════════════════════════════════════════════════════════════════
    #  ALL-TIME CHANNEL
    # ═══════════════════════════════════════════════════════════════════

    async def _update_alltime_channel(self, guild: discord.Guild, exclude_ids: set[int]):
        channel_id_str = await settings_service.get("leaderboard_alltime_channel_id")
        if not channel_id_str or channel_id_str == "0":
            return

        channel = guild.get_channel(int(channel_id_str))
        if not channel:
            logger.warning(f"All-time leaderboard channel {channel_id_str} not found in guild.")
            return

        logger.debug("Generating all-time leaderboard embeds...")
        next_update = int(time_module.time() + 300)

        # ── Header embed ──
        header = discord.Embed(
            title="🏛️ All-Time Hall of Fame",
            description=(
                "Cumulative lifetime records since server inception.\n"
                f"*Next update:* <t:{next_update}:R>"
            ),
            color=CLR_ALLTIME_HEADER,
            timestamp=discord.utils.utcnow(),
        )
        header.set_footer(text="🕐 Updates every 5 minutes")

        # ── Generate all 8 category embeds (each query is individually guarded) ──
        xp_data = await self._safe_query(
            leaderboard_service.get_alltime_xp(10, exclude_ids), "alltime_xp"
        )
        ep_data = await self._safe_query(
            leaderboard_service.get_alltime_ep(10, exclude_ids), "alltime_ep"
        )
        quiz_data = await self._safe_query(
            leaderboard_service.get_alltime_quiz(10, exclude_ids), "alltime_quiz"
        )
        counting_data = await self._safe_query(
            leaderboard_service.get_alltime_counting(), "alltime_counting", default={}
        )
        referral_data = await self._safe_query(
            leaderboard_service.get_alltime_referrals(10, exclude_ids), "alltime_referrals"
        )
        boost_data = await self._safe_query(
            leaderboard_service.get_alltime_boosting(10, exclude_ids), "alltime_boosting"
        )
        voice_data = await self._safe_query(
            leaderboard_service.get_alltime_voice(10, exclude_ids), "alltime_voice"
        )
        msg_data = await self._safe_query(
            leaderboard_service.get_alltime_messages(10, exclude_ids), "alltime_messages"
        )

        xp_embed = self._build_xp_embed(xp_data, is_weekly=False)
        ep_embed = self._build_ep_embed(ep_data, is_weekly=False)
        quiz_embed = self._build_quiz_embed(quiz_data, is_weekly=False)
        counting_embed = self._build_counting_embed_alltime(counting_data)
        referral_embed = self._build_referral_embed(referral_data, is_weekly=False)
        boost_embed = self._build_boosting_embed(boost_data)
        voice_embed = self._build_voice_embed(voice_data, is_weekly=False)
        msg_embed = self._build_message_embed(msg_data, is_weekly=False)

        # Split into message groups (max ~4 embeds per message for readability)
        groups = [
            [header, xp_embed, ep_embed, quiz_embed],
            [counting_embed, referral_embed, boost_embed],
            [voice_embed, msg_embed],
        ]

        await self._send_or_edit_groups(
            channel, guild, groups, key_prefix="leaderboard_alltime"
        )
        logger.debug("All-time leaderboard updated successfully.")

    # ═══════════════════════════════════════════════════════════════════
    #  WEEKLY CHANNEL
    # ═══════════════════════════════════════════════════════════════════

    async def _update_weekly_channel(self, guild: discord.Guild, exclude_ids: set[int]):
        channel_id_str = await settings_service.get("leaderboard_weekly_channel_id")
        if not channel_id_str or channel_id_str == "0":
            return

        channel = guild.get_channel(int(channel_id_str))
        if not channel:
            logger.warning(f"Weekly leaderboard channel {channel_id_str} not found in guild.")
            return

        logger.debug("Generating weekly leaderboard embeds...")
        next_update = int(time_module.time() + 300)
        week_start = leaderboard_service.get_week_start()
        next_reset = leaderboard_service.get_next_week_start()
        next_reset_ts = int(next_reset.timestamp())

        # Format period string
        week_end = week_start + timedelta(days=6)
        period_start_pht = week_start.astimezone(TZ_PHT)
        period_end_pht = week_end.astimezone(TZ_PHT)
        period_str = f"{period_start_pht.strftime('%b %d')} – {period_end_pht.strftime('%b %d, %Y')}"

        # ── Header embed ──
        header = discord.Embed(
            title="📅 Weekly Leaderboards",
            description=(
                f"**{period_str}**\n\n"
                f"⏳ Resets <t:{next_reset_ts}:R> · <t:{next_reset_ts}:F>\n"
                f"*Next update:* <t:{next_update}:R>"
            ),
            color=CLR_WEEKLY_HEADER,
            timestamp=discord.utils.utcnow(),
        )
        header.set_footer(text="🕐 Updates every 5 minutes • Resets Monday 12:00 AM PHT")

        # ── Generate 7 category embeds (no boosting on weekly) ──
        xp_data = await self._safe_query(
            leaderboard_service.get_weekly_xp(10, exclude_ids), "weekly_xp"
        )
        ep_data = await self._safe_query(
            leaderboard_service.get_weekly_ep(10, exclude_ids), "weekly_ep"
        )
        quiz_data = await self._safe_query(
            leaderboard_service.get_weekly_quiz(10, exclude_ids), "weekly_quiz"
        )
        counting_data = await self._safe_query(
            leaderboard_service.get_weekly_counting(), "weekly_counting", default={}
        )
        referral_data = await self._safe_query(
            leaderboard_service.get_weekly_referrals(10, exclude_ids), "weekly_referrals"
        )
        voice_data = await self._safe_query(
            leaderboard_service.get_weekly_voice(10, exclude_ids), "weekly_voice"
        )
        msg_data = await self._safe_query(
            leaderboard_service.get_weekly_messages(10, exclude_ids), "weekly_messages"
        )

        xp_embed = self._build_xp_embed(xp_data, is_weekly=True)
        ep_embed = self._build_ep_embed(ep_data, is_weekly=True)
        quiz_embed = self._build_quiz_embed(quiz_data, is_weekly=True)
        counting_embed = self._build_counting_embed_weekly(counting_data)
        referral_embed = self._build_referral_embed(referral_data, is_weekly=True)
        voice_embed = self._build_voice_embed(voice_data, is_weekly=True)
        msg_embed = self._build_message_embed(msg_data, is_weekly=True)

        groups = [
            [header, xp_embed, ep_embed, quiz_embed],
            [counting_embed, referral_embed],
            [voice_embed, msg_embed],
        ]

        await self._send_or_edit_groups(
            channel, guild, groups, key_prefix="leaderboard_weekly"
        )
        logger.debug("Weekly leaderboard updated successfully.")

    # ═══════════════════════════════════════════════════════════════════
    #  MESSAGE MANAGEMENT (edit-or-create)
    # ═══════════════════════════════════════════════════════════════════

    async def _send_or_edit_groups(
        self,
        channel: discord.TextChannel,
        guild: discord.Guild,
        embed_groups: list[list[discord.Embed]],
        key_prefix: str,
    ):
        """
        For each group of embeds, try to edit an existing message.
        If the message doesn't exist (first run or deleted), send a new one.
        Stores message IDs in settings for persistence.
        """
        for idx, embeds in enumerate(embed_groups):
            msg_key = f"{key_prefix}_msg_{idx}_{guild.id}"
            msg_id_str = await settings_service.get(msg_key)

            if msg_id_str and msg_id_str != "0":
                try:
                    msg = await channel.fetch_message(int(msg_id_str))
                    await msg.edit(embeds=embeds)
                    continue  # Edited successfully
                except discord.NotFound:
                    logger.info(f"Leaderboard message {msg_id_str} was deleted. Re-creating...")
                except discord.HTTPException as e:
                    logger.error(f"Leaderboard edit error ({key_prefix} #{idx}): {e}")
                    # Fall through to recreate

            # Create new message
            try:
                new_msg = await channel.send(embeds=embeds)
                await settings_service.set(msg_key, str(new_msg.id))
                logger.info(f"Created leaderboard message #{idx} for {key_prefix} (msg_id={new_msg.id})")
            except discord.HTTPException as e:
                logger.error(f"Leaderboard send error ({key_prefix} #{idx}): {e}")

        # Clean up excess messages if we now have fewer groups than before
        cleanup_idx = len(embed_groups)
        while True:
            extra_key = f"{key_prefix}_msg_{cleanup_idx}_{guild.id}"
            extra_id = await settings_service.get(extra_key)
            if not extra_id or extra_id == "0":
                break
            try:
                msg = await channel.fetch_message(int(extra_id))
                await msg.delete()
            except (discord.NotFound, discord.HTTPException):
                pass
            await settings_service.set(extra_key, "0")
            cleanup_idx += 1

    # ═══════════════════════════════════════════════════════════════════
    #  EMBED BUILDERS
    # ═══════════════════════════════════════════════════════════════════

    # ── XP ──────────────────────────────────────────────────────────

    def _build_xp_embed(self, data: list[dict], is_weekly: bool) -> discord.Embed:
        label = "This Week" if is_weekly else "All-Time"
        xp_key = "weekly_xp" if is_weekly else "xp"

        embed = discord.Embed(
            title=f"🌟 XP Leaderboard — {label}",
            color=CLR_XP,
            timestamp=discord.utils.utcnow(),
        )

        if not data:
            embed.description = (
                "> *No XP earned yet this week. Start chatting to climb the ranks!*"
                if is_weekly else
                "> *The server is quiet… no one has earned any XP yet.*"
            )
            return embed

        # Top 3 podium
        podium = []
        for i, row in enumerate(data[:3]):
            xp = row.get(xp_key, row.get("xp", 0)) or 0
            if is_weekly:
                podium.append(
                    f"{MEDALS[i]} <@{row['user_id']}>\n"
                    f"╰ **+{xp:,} XP** earned this week"
                )
            else:
                level = xp_service.get_level(xp)
                tier = xp_service.get_tier_name(level)
                podium.append(
                    f"{MEDALS[i]} <@{row['user_id']}>\n"
                    f"╰ **{xp:,} XP** · Lv. {level} ({tier})"
                )
        embed.description = "\n\n".join(podium)

        # Runners up (4-10)
        if len(data) > 3:
            runners = []
            for i, row in enumerate(data[3:], 4):
                xp = row.get(xp_key, row.get("xp", 0)) or 0
                if is_weekly:
                    runners.append(f"`{i}.` <@{row['user_id']}> — **+{xp:,} XP**")
                else:
                    level = xp_service.get_level(xp)
                    runners.append(f"`{i}.` <@{row['user_id']}> — **{xp:,} XP** · Lv. {level}")
            embed.add_field(name="── Runners Up ──", value="\n".join(runners), inline=False)

        embed.set_footer(text="Updates every 5 min")
        return embed

    # ── EP ──────────────────────────────────────────────────────────

    def _build_ep_embed(self, data: list[dict], is_weekly: bool) -> discord.Embed:
        label = "This Week" if is_weekly else "All-Time"
        ep_key = "weekly_ep" if is_weekly else "event_points"

        embed = discord.Embed(
            title=f"🏆 EP Leaderboard — {label}",
            color=CLR_EP,
            timestamp=discord.utils.utcnow(),
        )

        if not data:
            embed.description = (
                "> *No EP earned this week yet. Attend events to climb!*"
                if is_weekly else
                "> *No Event Points distributed yet.*"
            )
            return embed

        podium = []
        for i, row in enumerate(data[:3]):
            ep = row.get(ep_key, 0) or 0
            if is_weekly:
                podium.append(
                    f"{MEDALS[i]} <@{row['user_id']}>\n"
                    f"╰ **+{ep:,} EP** earned this week"
                )
            else:
                events = row.get("total_events", 0) or 0
                try:
                    role_name = ep_service.get_sub_tier(ep)
                except Exception:
                    role_name = "Unknown"
                podium.append(
                    f"{MEDALS[i]} <@{row['user_id']}>\n"
                    f"╰ **{ep:,} EP** · {events} Events ({role_name})"
                )
        embed.description = "\n\n".join(podium)

        if len(data) > 3:
            runners = []
            for i, row in enumerate(data[3:], 4):
                ep = row.get(ep_key, 0) or 0
                if is_weekly:
                    runners.append(f"`{i}.` <@{row['user_id']}> — **+{ep:,} EP**")
                else:
                    try:
                        role_name = ep_service.get_sub_tier(ep)
                    except Exception:
                        role_name = "Unknown"
                    runners.append(f"`{i}.` <@{row['user_id']}> — **{ep:,} EP** ({role_name})")
            embed.add_field(name="── Runners Up ──", value="\n".join(runners), inline=False)

        embed.set_footer(text="Updates every 5 min")
        return embed

    # ── Quiz ────────────────────────────────────────────────────────

    def _build_quiz_embed(self, data: list[dict], is_weekly: bool) -> discord.Embed:
        label = "This Week" if is_weekly else "All-Time"

        embed = discord.Embed(
            title=f"🧠 Quiz Leaderboard — {label}",
            color=CLR_QUIZ,
            timestamp=discord.utils.utcnow(),
        )

        if not data:
            embed.description = (
                "> *No quiz scores this week. Be the first to answer correctly!*"
                if is_weekly else
                "> *No quiz scores recorded yet.*"
            )
            return embed

        podium = []
        for i, row in enumerate(data[:3]):
            score = row.get("total_score", 0) or 0
            sessions = row.get("sessions", 0) or 0
            podium.append(
                f"{MEDALS[i]} <@{row['user_id']}>\n"
                f"╰ **{score:,} pts** · {sessions} session{'s' if sessions != 1 else ''}"
            )
        embed.description = "\n\n".join(podium)

        if len(data) > 3:
            runners = []
            for i, row in enumerate(data[3:], 4):
                score = row.get("total_score", 0) or 0
                runners.append(f"`{i}.` <@{row['user_id']}> — **{score:,} pts**")
            embed.add_field(name="── Runners Up ──", value="\n".join(runners), inline=False)

        embed.set_footer(text="Updates every 5 min")
        return embed

    # ── Counting (All-Time) ─────────────────────────────────────────

    def _build_counting_embed_alltime(self, data: dict) -> discord.Embed:
        embed = discord.Embed(
            title="🔢 Counting Challenge — All-Time",
            color=CLR_COUNTING,
            timestamp=discord.utils.utcnow(),
        )

        state = data.get("state") if data else None
        if not state:
            embed.description = "> *The counting game hasn't started yet!*"
            return embed

        current = state.get("current_count", 0) or 0
        high = state.get("high_score", 0) or 0
        broken_by = state.get("high_score_broken_by")

        lines = [f"**Current Streak:** `{current}`"]

        curr_contrib = data.get("current_contributors", [])
        if curr_contrib and current > 0:
            lines.append("*Top Contributors (Current):*")
            for i, c in enumerate(curr_contrib[:3], 1):
                lines.append(f"  `{i}.` <@{c['user_id']}> ({c['count']})")

        if high > 0:
            lines.append(f"\n🏆 **All-Time Record:** `{high}`")
            if broken_by:
                lines.append(f"╰ Broken by <@{broken_by}>")

            hs_contrib = data.get("highscore_contributors", [])
            if hs_contrib:
                lines.append("*Record Contributors:*")
                for i, c in enumerate(hs_contrib[:3], 1):
                    lines.append(f"  `{i}.` <@{c['user_id']}> ({c['count']})")
        else:
            lines.append("\n*No record set yet — start counting!*")

        embed.description = "\n".join(lines)
        embed.set_footer(text="Updates every 5 min")
        return embed

    # ── Counting (Weekly) ───────────────────────────────────────────

    def _build_counting_embed_weekly(self, data: dict) -> discord.Embed:
        embed = discord.Embed(
            title="🔢 Counting Challenge — This Week",
            color=CLR_COUNTING,
            timestamp=discord.utils.utcnow(),
        )

        state = data.get("state") if data else None
        weekly_contrib = data.get("weekly_contributors", []) if data else []

        if not weekly_contrib:
            embed.description = "> *No counting contributions this week yet. Head to the counting channel!*"
            if state:
                current = state.get("current_count", 0) or 0
                embed.description += f"\n\n**Current Streak:** `{current}`"
            return embed

        lines = []
        if state:
            current = state.get("current_count", 0) or 0
            lines.append(f"**Current Streak:** `{current}`\n")

        lines.append("**Top Weekly Contributors:**")
        for i, c in enumerate(weekly_contrib[:10], 1):
            medal = MEDALS[i - 1] if i <= 3 else f"`{i}.`"
            lines.append(f"{medal} <@{c['user_id']}> — **{c['count']}** counts")

        embed.description = "\n".join(lines)
        embed.set_footer(text="Updates every 5 min")
        return embed

    # ── Referral ────────────────────────────────────────────────────

    def _build_referral_embed(self, data: list[dict], is_weekly: bool) -> discord.Embed:
        label = "This Week" if is_weekly else "All-Time"
        count_key = "curr_week_referrals" if is_weekly else "total_referrals"

        embed = discord.Embed(
            title=f"🔗 Referral Leaderboard — {label}",
            color=CLR_REFERRAL,
            timestamp=discord.utils.utcnow(),
        )

        if not data:
            embed.description = (
                "> *No referrals this week yet. Share your code!*"
                if is_weekly else
                "> *No referrals recorded yet.*"
            )
            return embed

        podium = []
        for i, row in enumerate(data[:3]):
            count = row.get(count_key, 0) or 0
            suffix = "referral" if count == 1 else "referrals"
            podium.append(
                f"{MEDALS[i]} <@{row['user_id']}>\n"
                f"╰ **{count}** {suffix}"
            )
        embed.description = "\n\n".join(podium)

        if len(data) > 3:
            runners = []
            for i, row in enumerate(data[3:], 4):
                count = row.get(count_key, 0) or 0
                runners.append(f"`{i}.` <@{row['user_id']}> — **{count}** referrals")
            embed.add_field(name="── Runners Up ──", value="\n".join(runners), inline=False)

        embed.set_footer(text="Updates every 5 min")
        return embed

    # ── Boosting (All-Time only) ────────────────────────────────────

    def _build_boosting_embed(self, data: list[dict]) -> discord.Embed:
        embed = discord.Embed(
            title="💎 Boosting Streak — All-Time",
            color=CLR_BOOST,
            timestamp=discord.utils.utcnow(),
        )

        if not data:
            embed.description = "> *No active boosters yet. Boost the server to appear here!*"
            return embed

        podium = []
        for i, row in enumerate(data[:3]):
            days = row.get("days_boosting", 0) or 0
            months = days // 30
            remaining_days = days % 30
            duration = f"{months}mo {remaining_days}d" if months > 0 else f"{days}d"
            podium.append(
                f"{MEDALS[i]} <@{row['user_id']}>\n"
                f"╰ **{duration}** boost streak ({days:,} days)"
            )
        embed.description = "\n\n".join(podium)

        if len(data) > 3:
            runners = []
            for i, row in enumerate(data[3:], 4):
                days = row.get("days_boosting", 0) or 0
                runners.append(f"`{i}.` <@{row['user_id']}> — **{days:,}** days")
            embed.add_field(name="── Runners Up ──", value="\n".join(runners), inline=False)

        embed.set_footer(text="Updates every 5 min")
        return embed

    # ── Voice Activity ──────────────────────────────────────────────

    def _build_voice_embed(self, data: list[dict], is_weekly: bool) -> discord.Embed:
        label = "This Week" if is_weekly else "All-Time"

        embed = discord.Embed(
            title=f"🎤 Voice Activity — {label}",
            color=CLR_VOICE,
            timestamp=discord.utils.utcnow(),
        )

        if not data:
            embed.description = (
                "> *No voice activity this week. Join a voice channel to get started!*"
                if is_weekly else
                "> *No voice sessions recorded yet.*"
            )
            return embed

        podium = []
        for i, row in enumerate(data[:3]):
            minutes = int(row.get("total_minutes", 0) or 0)
            hours = minutes // 60
            mins = minutes % 60
            time_str = f"{hours}h {mins}m" if hours > 0 else f"{mins}m"
            podium.append(
                f"{MEDALS[i]} <@{row['user_id']}>\n"
                f"╰ **{time_str}** in voice"
            )
        embed.description = "\n\n".join(podium)

        if len(data) > 3:
            runners = []
            for i, row in enumerate(data[3:], 4):
                minutes = int(row.get("total_minutes", 0) or 0)
                hours = minutes // 60
                mins = minutes % 60
                time_str = f"{hours}h {mins}m" if hours > 0 else f"{mins}m"
                runners.append(f"`{i}.` <@{row['user_id']}> — **{time_str}**")
            embed.add_field(name="── Runners Up ──", value="\n".join(runners), inline=False)

        embed.set_footer(text="Updates every 5 min")
        return embed

    # ── Message Activity ────────────────────────────────────────────

    def _build_message_embed(self, data: list[dict], is_weekly: bool) -> discord.Embed:
        label = "This Week" if is_weekly else "All-Time"

        embed = discord.Embed(
            title=f"💬 Message Activity — {label}",
            color=CLR_MESSAGE,
            timestamp=discord.utils.utcnow(),
        )

        if not data:
            embed.description = (
                "> *No qualifying messages this week. Keep chatting!*"
                if is_weekly else
                "> *No messages recorded yet.*"
            )
            return embed

        podium = []
        for i, row in enumerate(data[:3]):
            msgs = row.get("total_messages", 0) or 0
            podium.append(
                f"{MEDALS[i]} <@{row['user_id']}>\n"
                f"╰ **{msgs:,}** messages"
            )
        embed.description = "\n\n".join(podium)

        if len(data) > 3:
            runners = []
            for i, row in enumerate(data[3:], 4):
                msgs = row.get("total_messages", 0) or 0
                runners.append(f"`{i}.` <@{row['user_id']}> — **{msgs:,}** messages")
            embed.add_field(name="── Runners Up ──", value="\n".join(runners), inline=False)

        embed.set_footer(text="Updates every 5 min · Counts messages with 3+ words")
        return embed


async def setup(bot: commands.Bot):
    await bot.add_cog(LeaderboardCog(bot))
