"""
Referral Cog — /referral subcommands and weekly reset cron.
"""

import discord
import datetime
from discord.ext import commands, tasks
from discord import app_commands
import logging

from services.referral_service import referral_service
from services.settings_service import settings_service

logger = logging.getLogger("mlbb_bot.referral_cog")

# Manila timezone (UTC+8) — reset happens Sunday midnight PHT
TZ_MANILA = datetime.timezone(datetime.timedelta(hours=8))
# Sunday midnight PHT = Saturday 16:00 UTC
SUNDAY_MIDNIGHT_PHT_UTC = datetime.time(hour=16, minute=0, tzinfo=datetime.timezone.utc)

# Error messages for link_referral results
LINK_ERRORS = {
    "self_referral": "❌ You can't use your own referral code.",
    "already_used": "❌ You've already used a referral code.",
    "invalid_code": "❌ That referral code doesn't exist. Double-check and try again.",
    "not_new": "❌ Referral codes can only be used by members who joined within the last 30 days.",
}


class ReferralCog(commands.Cog, name="Referrals"):
    """Referral code system — generate, link, and track referrals."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.weekly_reset_loop.is_running():
            self.weekly_reset_loop.start()
        logger.info("Referral system ready")

    def cog_unload(self):
        self.weekly_reset_loop.cancel()

    # ─── WEEKLY RESET CRON ──────────────────────────────────────────

    @tasks.loop(time=SUNDAY_MIDNIGHT_PHT_UTC)
    async def weekly_reset_loop(self):
        """
        Runs every day at Sunday midnight PHT (Saturday 16:00 UTC).
        Only executes the reset if today is actually Sunday in PHT.
        Uses a settings flag to prevent double-runs.
        """
        now_pht = datetime.datetime.now(TZ_MANILA)

        # Only run on Sunday (weekday 6)
        if now_pht.weekday() != 6:
            return

        # Check if we already ran this week
        iso_week = now_pht.strftime("%Y-W%W")
        last_reset = await settings_service.get("referral_last_reset_week")
        if last_reset == iso_week:
            return

        await referral_service.weekly_reset()
        await settings_service.set("referral_last_reset_week", iso_week)
        logger.info(f"Referral weekly reset executed for week {iso_week}")

    @weekly_reset_loop.before_loop
    async def before_weekly_reset(self):
        await self.bot.wait_until_ready()

    # ─── COMMAND GROUP ──────────────────────────────────────────────

    referral_group = app_commands.Group(
        name="referral",
        description="Referral code system",
    )

    # ─── /referral view — View your code and stats ──────────────────

    @referral_group.command(name="view", description="View your referral code and stats")
    async def referral_view(self, interaction: discord.Interaction):
        """Display the user's referral code and stats."""
        stats = await referral_service.get_stats(interaction.user.id)

        embed = discord.Embed(
            title="🔗 Your Referral Code",
            color=discord.Color.gold(),
        )

        embed.add_field(
            name="Your Code",
            value=f"```{stats['own_code']}```",
            inline=False,
        )

        stats_lines = [
            f"**Total Referrals:** {stats['total']}",
            f"**This Week:** {stats['this_week']}",
            f"**Last Week:** {stats['last_week']}",
        ]

        if stats["referred_by"]:
            stats_lines.append(f"\n*You were referred by* <@{stats['referred_by']}>")

        embed.add_field(
            name="📊 Stats",
            value="\n".join(stats_lines),
            inline=False,
        )

        embed.set_footer(text="Share your code with new members who are verifying!")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ─── /referral link — Fallback for missed verification code ─────

    @referral_group.command(
        name="link",
        description="Link a referral code if you missed it during verification",
    )
    @app_commands.describe(code="The referral code to link (e.g. MSL-21I3V9)")
    async def referral_link(self, interaction: discord.Interaction, code: str):
        """Fallback command for users who missed the referral field in verification."""
        member = interaction.user

        # Use joined_at from the guild member
        joined_at = member.joined_at if isinstance(member, discord.Member) else None

        result = await referral_service.link_referral(
            member.id, code, joined_at
        )

        if result is None:
            # Success
            referrer = await referral_service.get_by_code(code.upper().strip())
            referrer_mention = f"<@{referrer['user_id']}>" if referrer else "someone"

            embed = discord.Embed(
                title="✅ Referral Linked!",
                description=(
                    f"You've been referred by {referrer_mention}.\n\n"
                    f"Code used: `{code.upper().strip()}`"
                ),
                color=discord.Color.green(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            # Error
            error_msg = LINK_ERRORS.get(result, f"❌ Unknown error: {result}")
            await interaction.response.send_message(error_msg, ephemeral=True)

    # ─── /referral leaderboard — Top referrers ──────────────────────

    @referral_group.command(
        name="leaderboard",
        description="View top referrers — all-time and this week",
    )
    async def referral_leaderboard(self, interaction: discord.Interaction):
        """Leaderboard showing top 10 referrers by all-time and current week."""
        alltime = await referral_service.get_leaderboard_alltime(10)
        weekly = await referral_service.get_leaderboard_week(10)

        embed = discord.Embed(
            title="🏆 Referral Leaderboard",
            color=discord.Color.gold(),
        )

        medals = ["🥇", "🥈", "🥉"]

        # All-time field
        if alltime:
            lines = []
            for i, row in enumerate(alltime):
                prefix = medals[i] if i < 3 else f"`{i+1}.`"
                lines.append(f"{prefix} <@{row['user_id']}> — **{row['total_referrals']}**")
            embed.add_field(name="🌟 All-Time", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="🌟 All-Time", value="*No referrals yet.*", inline=False)

        # Current week field
        if weekly:
            lines = []
            for i, row in enumerate(weekly):
                prefix = medals[i] if i < 3 else f"`{i+1}.`"
                lines.append(f"{prefix} <@{row['user_id']}> — **{row['curr_week_referrals']}**")
            embed.add_field(name="📅 This Week", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="📅 This Week", value="*No referrals this week.*", inline=False)

        embed.set_footer(text="Refer new members to climb the leaderboard!")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ─── /referral previous — Admin: last week's stats ──────────────

    @referral_group.command(
        name="previous",
        description="View last week's referral stats for all members",
    )
    @app_commands.default_permissions(administrator=True)
    async def referral_previous(self, interaction: discord.Interaction):
        """Admin command showing previous week referral counts."""
        stats = await referral_service.get_previous_week_stats()

        embed = discord.Embed(
            title="📋 Previous Week Referrals",
            color=discord.Color.orange(),
        )

        if not stats:
            embed.description = "*No referrals were recorded last week.*"
        else:
            lines = []
            total_prev = 0
            for i, row in enumerate(stats[:25], 1):  # Cap at 25 for embed limits
                count = row["prev_week_referrals"]
                total_prev += count
                lines.append(f"`{i}.` <@{row['user_id']}> — **{count}** referral{'s' if count != 1 else ''}")

            embed.description = "\n".join(lines)
            embed.set_footer(text=f"Total referrals last week: {total_prev}")

        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ReferralCog(bot))
