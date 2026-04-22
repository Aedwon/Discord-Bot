"""
Promotion Cog — Real-time tracking of custom status changes.

Listens for presence updates to detect when users add/remove the server's
invite URL from their Discord custom status. Updates promo flags and EP
multiplier in real time.

Also includes a background sync task as a safety net.
"""

import discord
import asyncio
import logging
from discord.ext import commands, tasks

from services.promo_service import promo_service
from services.settings_service import settings_service

logger = logging.getLogger("mlbb_bot.promo_cog")


class PromoCog(commands.Cog):
    """Tracks custom status promotions for EP multiplier."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Pre-fetched invite URL (refreshed on each sync cycle)
        self._invite_url: str | None = None

    async def cog_load(self):
        """Start background tasks when cog loads."""
        if not self._sync_task.is_running():
            self._sync_task.start()

    async def cog_unload(self):
        """Cancel background tasks when cog unloads."""
        self._sync_task.cancel()

    async def _ensure_invite_url(self) -> str:
        """Lazy-load and cache the invite URL from settings."""
        if self._invite_url is None:
            self._invite_url = await settings_service.get("promo_invite_url")
        return self._invite_url

    # ─── PRESENCE LISTENER ────────────────────────────────────────

    @commands.Cog.listener()
    async def on_presence_update(self, before: discord.Member, after: discord.Member):
        """
        Detect custom status changes in real time.

        This event fires very frequently (every activity/status change for
        every member). We minimize DB load by:
        1. Early-exit if custom activities haven't changed.
        2. RAM cache in promo_service prevents redundant DB writes.
        """
        if after.bot:
            return

        # ── Quick bail: compare custom activity objects ──
        # Extract CustomActivity from before and after
        before_custom = None
        after_custom = None

        for act in (before.activities or []):
            if isinstance(act, discord.CustomActivity):
                before_custom = act
                break

        for act in (after.activities or []):
            if isinstance(act, discord.CustomActivity):
                after_custom = act
                break

        # If neither had/has a custom activity, nothing to do
        if before_custom is None and after_custom is None:
            return

        # If the custom activity text is identical, skip
        before_text = (before_custom.state or "") if before_custom else ""
        after_text = (after_custom.state or "") if after_custom else ""

        if before_text == after_text:
            # Also check .name (some clients use that instead)
            before_name = (before_custom.name or "") if before_custom else ""
            after_name = (after_custom.name or "") if after_custom else ""
            if before_name == after_name:
                return

        # ── Status text changed — check for promo URL ──
        invite_url = await self._ensure_invite_url()
        has_promo = promo_service.check_status(after, invite_url)

        await promo_service.update_promo(after.id, has_promo)

    # ─── BACKGROUND SYNC ─────────────────────────────────────────

    @tasks.loop(hours=6)
    async def _sync_task(self):
        """
        Periodic full guild sync as a safety net.
        Catches any missed presence updates (e.g., during bot downtime).
        """
        await self.bot.wait_until_ready()

        # Refresh the cached invite URL every cycle
        self._invite_url = await settings_service.get("promo_invite_url")

        for guild in self.bot.guilds:
            try:
                stats = await promo_service.sync_guild(guild)
                logger.info(
                    f"Promo sync for {guild.name}: "
                    f"+{stats['activated']} / -{stats['deactivated']} / "
                    f"={stats['unchanged']} | Total: {stats['total_promoters']}"
                )
            except Exception as e:
                logger.error(f"Promo sync failed for {guild.name}: {e}")

            # Short pause between guilds to avoid API pressure
            await asyncio.sleep(2)

    @_sync_task.before_loop
    async def _before_sync(self):
        """Wait for the bot to be ready before starting sync."""
        await self.bot.wait_until_ready()
        # Give other cogs time to initialize
        await asyncio.sleep(30)


async def setup(bot: commands.Bot):
    await bot.add_cog(PromoCog(bot))
