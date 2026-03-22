"""
Event Points End-of-Season Background Engine.
Handles Peak Rank legacy role upgrades, seasonal EP resets,
and automatic season counter tracking.
"""
import discord
import asyncio
from discord.ext import commands, tasks
import logging

from services.database import db
from services.settings_service import settings_service
from services.ep_service import ep_service, ALL_EP_ROLE_NAMES, MAIN_TIER_NAMES, MYTHIC_FLOOR

logger = logging.getLogger("mlbb_bot.ep_core")

# Peak role hierarchy (index = rank, higher = better)
PEAK_TIER_RANK = {name: i for i, name in enumerate(MAIN_TIER_NAMES)}


class EPCog(commands.Cog, name="event_points_core"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def cog_unload(self):
        self.eos_loop.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.eos_loop.is_running():
            self.eos_loop.start()

    @tasks.loop(hours=24)
    async def eos_loop(self):
        """End Of Season background checker. Executes if the EOS flag is set."""
        eos_flag = await settings_service.get_int("eos_reset_triggered")
        if eos_flag == 1:
            await self.execute_eos_wipe()

    # ─── END OF SEASON ─────────────────────────────────────────────────

    async def execute_eos_wipe(self):
        """
        Full End-of-Season process:
        1. Determine each user's main tier (including Mythic ladder position)
        2. Compare with their current Peak role → upgrade if higher
        3. Strip all 34 seasonal EP roles from every user
        4. Reset EP to 0
        5. Increment season counter
        """
        logger.info("═══ EOS WIPE INITIATED ═══")
        await settings_service.set("eos_reset_triggered", "0")

        guild = self.bot.guilds[0] if self.bot.guilds else None
        if not guild:
            logger.error("EOS: No guild found. Aborting.")
            return

        # ── Step 1: Resolve all Peak roles (10 fixed roles) ──
        peak_roles = {}  # {"Warrior": Role, "Elite": Role, ...}
        for tier_name in MAIN_TIER_NAMES:
            key = f"peak_role_{tier_name.replace(' ', '_')}"
            role_id = await settings_service.get_int(key)
            if role_id:
                role_obj = guild.get_role(role_id)
                if role_obj:
                    peak_roles[tier_name] = role_obj

        if not peak_roles:
            logger.warning("EOS: No Peak roles configured. Skipping Peak assignment.")

        # ── Step 2: Resolve all 34 seasonal EP roles ──
        seasonal_roles = []
        for role_name in ALL_EP_ROLE_NAMES:
            key = f"ep_role_{role_name.replace(' ', '_')}"
            role_id = await settings_service.get_int(key)
            if role_id:
                role_obj = guild.get_role(role_id)
                if role_obj:
                    seasonal_roles.append(role_obj)

        # ── Step 3: Get all players with EP, ordered for Mythic ladder ──
        all_players = await db.fetch_all(
            "SELECT user_id, event_points FROM users "
            "WHERE event_points > 0 "
            "ORDER BY event_points DESC, last_ep_update ASC"
        )

        if not all_players:
            logger.info("EOS: No players with EP. Incrementing season and resetting.")
            await self._finalize_eos()
            return

        logger.info(f"EOS: Processing {len(all_players)} players...")
        processed = 0
        errors = 0

        for position, row in enumerate(all_players, 1):
            user_id = row['user_id']
            ep = row['event_points']
            member = guild.get_member(user_id)

            if not member:
                continue

            try:
                # Determine their EOS tier (Mythic users get ladder position)
                eos_tier = ep_service.resolve_eos_tier(ep, position)

                # ── Peak Role upgrade logic ──
                if peak_roles:
                    eos_rank = PEAK_TIER_RANK.get(eos_tier, 0)

                    # Find their current Peak role (if any)
                    current_peak_name = None
                    current_peak_rank = -1
                    for tier_name, role_obj in peak_roles.items():
                        if role_obj in member.roles:
                            tier_rank = PEAK_TIER_RANK.get(tier_name, 0)
                            if tier_rank > current_peak_rank:
                                current_peak_name = tier_name
                                current_peak_rank = tier_rank

                    # Upgrade Peak if this season's tier is higher
                    if eos_rank > current_peak_rank:
                        new_peak = peak_roles.get(eos_tier)
                        if new_peak:
                            # Remove old peak role if they had one
                            if current_peak_name and current_peak_name in peak_roles:
                                old_peak = peak_roles[current_peak_name]
                                if old_peak in member.roles:
                                    await member.remove_roles(
                                        old_peak,
                                        reason=f"EOS Peak Upgrade: {current_peak_name} → {eos_tier}"
                                    )
                            await member.add_roles(
                                new_peak,
                                reason=f"EOS Peak Rank: {eos_tier} (Season Peak)"
                            )
                            logger.info(
                                f"  Peak upgraded: {member.display_name} "
                                f"{current_peak_name or 'None'} → {eos_tier}"
                            )

                # ── Strip seasonal EP roles ──
                roles_to_remove = [r for r in seasonal_roles if r in member.roles]
                if roles_to_remove:
                    await member.remove_roles(
                        *roles_to_remove, reason="EOS Seasonal Reset"
                    )

                processed += 1

            except discord.Forbidden:
                errors += 1
                logger.error(f"  Permission denied for {user_id}")
            except discord.HTTPException as e:
                errors += 1
                logger.error(f"  HTTP error for {user_id}: {e}")

            # Rate limit protection: 2 API calls per user, yield every iteration
            await asyncio.sleep(0.5)

        logger.info(
            f"EOS: Processed {processed} users, {errors} errors. "
            f"Finalizing..."
        )
        await self._finalize_eos()

    async def _finalize_eos(self):
        """Reset EP to 0 and increment the season counter."""
        # Reset all EP
        await db.execute("UPDATE users SET event_points = 0")

        # Increment season counter
        current_season = await settings_service.get_int("current_season")
        if current_season == 0:
            current_season = 1  # First season
        next_season = current_season + 1
        await settings_service.set("current_season", str(next_season))

        logger.info(
            f"═══ EOS COMPLETE ═══ "
            f"Season {current_season} ended. Now Season {next_season}."
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(EPCog(bot))
