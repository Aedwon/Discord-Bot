"""
Promotion Service — Tracks users who promote the server
by setting their Discord custom status to include the server invite URL.

Users with an active promo status receive a 1.25x EP multiplier on participation EP.
"""

import discord
import logging
from services.database import db
from services.settings_service import settings_service

logger = logging.getLogger("mlbb_bot.promo_service")

# Default multiplier for active promoters
PROMO_EP_MULTIPLIER = 1.25


class PromoService:

    def __init__(self):
        # RAM cache: { user_id: bool } — avoids redundant DB writes
        # True = user currently has promo status, False = they don't
        self._cache: dict[int, bool] = {}

    # ─── STATUS DETECTION ─────────────────────────────────────────

    async def _get_invite_url(self) -> str:
        """Get the configured invite URL to search for in statuses."""
        return await settings_service.get("promo_invite_url")

    def check_status(self, member: discord.Member, invite_url: str) -> bool:
        """
        Check if a member's custom status text contains the promo invite URL.
        Checks both the 'state' (text) and 'name' of CustomActivity.
        Case-insensitive comparison for robustness.
        """
        if not member.activities:
            return False

        url_lower = invite_url.lower()

        for activity in member.activities:
            if isinstance(activity, discord.CustomActivity):
                # CustomActivity has .state (the text) and .name
                if activity.state and url_lower in activity.state.lower():
                    return True
                if activity.name and url_lower in activity.name.lower():
                    return True

        return False

    # ─── DATABASE UPDATES ─────────────────────────────────────────

    async def update_promo(self, user_id: int, has_status: bool) -> bool:
        """
        Update a user's promo status and EP multiplier.
        Returns True if the status *actually changed* (for logging purposes).
        """
        # Check RAM cache first — skip DB write if nothing changed
        cached = self._cache.get(user_id)
        if cached is not None and cached == has_status:
            return False  # No change

        new_multiplier = PROMO_EP_MULTIPLIER if has_status else 1.0

        await db.execute('''
            INSERT INTO users (user_id, has_promo_status, ep_multiplier)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE
                has_promo_status = %s,
                ep_multiplier = %s
        ''', (user_id, has_status, new_multiplier, has_status, new_multiplier))

        # Update cache
        self._cache[user_id] = has_status

        action = "activated" if has_status else "deactivated"
        logger.info(f"Promo status {action} for user {user_id} → EP multiplier: {new_multiplier}x")
        return True

    async def get_ep_multiplier(self, user_id: int) -> float:
        """Get a user's current EP multiplier."""
        result = await db.fetch_one(
            'SELECT ep_multiplier FROM users WHERE user_id = %s',
            (user_id,)
        )
        return result['ep_multiplier'] if result and result['ep_multiplier'] else 1.0

    # ─── GUILD SYNC ──────────────────────────────────────────────

    async def sync_guild(self, guild: discord.Guild) -> dict:
        """
        Full server scan — reconcile all members' promo status.
        Returns stats dict with counts.
        """
        invite_url = await self._get_invite_url()
        activated = 0
        deactivated = 0
        unchanged = 0

        for member in guild.members:
            if member.bot:
                continue

            has_status = self.check_status(member, invite_url)
            changed = await self.update_promo(member.id, has_status)

            if changed:
                if has_status:
                    activated += 1
                else:
                    deactivated += 1
            else:
                unchanged += 1

        total_promoters = await db.fetch_one(
            "SELECT COUNT(*) as c FROM users WHERE has_promo_status = TRUE"
        )

        logger.info(
            f"Promo sync complete: +{activated} / -{deactivated} / "
            f"={unchanged} | Total promoters: {total_promoters['c'] if total_promoters else 0}"
        )

        return {
            "activated": activated,
            "deactivated": deactivated,
            "unchanged": unchanged,
            "total_promoters": total_promoters['c'] if total_promoters else 0,
        }

    async def get_promo_stats(self) -> dict:
        """Get current promo adoption stats."""
        total = await db.fetch_one(
            "SELECT COUNT(*) as c FROM users WHERE has_promo_status = TRUE"
        )
        return {
            "promoters": total['c'] if total else 0,
        }


# Singleton export
promo_service = PromoService()
