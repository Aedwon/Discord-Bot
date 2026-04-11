"""
Referral Service — Code generation, linking, stats, and weekly reset.
Deterministic codes derived from Discord user IDs (base-36).
"""

import logging
from datetime import datetime, timezone

from services.database import db

logger = logging.getLogger("mlbb_bot.referral_service")

CODE_PREFIX = "MSL"


class ReferralService:
    """Singleton service managing the referral system."""

    # ─── CODE GENERATION ────────────────────────────────────────────

    @staticmethod
    def _base36_encode(number: int) -> str:
        """Encode an integer to a base-36 string (0-9, A-Z)."""
        chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        if number == 0:
            return "0"
        result = []
        while number:
            result.append(chars[number % 36])
            number //= 36
        return "".join(reversed(result))

    def generate_code(self, user_id: int) -> str:
        """Generate a deterministic referral code from a Discord user ID."""
        encoded = self._base36_encode(user_id)
        return f"{CODE_PREFIX}-{encoded}"

    # ─── CRUD ───────────────────────────────────────────────────────

    async def get_or_create(self, user_id: int) -> dict:
        """Get existing referral row or create one with a generated code."""
        row = await db.fetch_one(
            "SELECT * FROM referrals WHERE user_id = %s", (user_id,)
        )
        if row:
            return dict(row)

        code = self.generate_code(user_id)
        await db.execute(
            """INSERT INTO referrals (user_id, own_code)
               VALUES (%s, %s)
               ON DUPLICATE KEY UPDATE user_id = user_id""",
            (user_id, code),
        )
        row = await db.fetch_one(
            "SELECT * FROM referrals WHERE user_id = %s", (user_id,)
        )
        return dict(row)

    async def get_by_code(self, code: str) -> dict | None:
        """Look up a referral row by its code (case-insensitive)."""
        row = await db.fetch_one(
            "SELECT * FROM referrals WHERE own_code = %s",
            (code.upper().strip(),),
        )
        return dict(row) if row else None

    # ─── LINKING ────────────────────────────────────────────────────

    async def link_referral(
        self, user_id: int, code: str, joined_at: datetime | None
    ) -> str | None:
        """
        Attempt to link a referral code to a user.

        Returns None on success, or an error string:
        - "self_referral"   — user tried to use their own code
        - "already_used"    — user already redeemed a referral code
        - "invalid_code"    — code doesn't exist in DB
        - "not_new"         — user joined the server > 30 days ago
        """
        code = code.upper().strip()

        # 1. Ensure the user has a row
        user_row = await self.get_or_create(user_id)

        # 2. Already used a code?
        if user_row.get("used_code"):
            return "already_used"

        # 3. Code exists?
        referrer = await self.get_by_code(code)
        if not referrer:
            return "invalid_code"

        # 4. Self-referral?
        if referrer["user_id"] == user_id:
            return "self_referral"

        # 5. New member check (joined < 30 days ago)
        if joined_at:
            now = datetime.now(timezone.utc)
            if joined_at.tzinfo is None:
                joined_at = joined_at.replace(tzinfo=timezone.utc)
            days_since_join = (now - joined_at).days
            if days_since_join > 30:
                return "not_new"

        # 6. All checks passed — link the referral atomically
        referrer_id = referrer["user_id"]

        # Update the new user's row — set used_code and referred_by
        await db.execute(
            """UPDATE referrals
               SET used_code = %s, referred_by = %s
               WHERE user_id = %s AND used_code IS NULL""",
            (code, referrer_id, user_id),
        )

        # Increment the referrer's counts
        await db.execute(
            """UPDATE referrals
               SET total_referrals = total_referrals + 1,
                   curr_week_referrals = curr_week_referrals + 1
               WHERE user_id = %s""",
            (referrer_id,),
        )

        logger.info(
            f"Referral linked: {user_id} used code {code} (referrer: {referrer_id})"
        )
        return None

    # ─── STATS ──────────────────────────────────────────────────────

    async def get_stats(self, user_id: int) -> dict:
        """Return referral stats for embed display."""
        row = await self.get_or_create(user_id)
        return {
            "own_code": row["own_code"],
            "used_code": row.get("used_code"),
            "referred_by": row.get("referred_by"),
            "total": row.get("total_referrals", 0),
            "this_week": row.get("curr_week_referrals", 0),
            "last_week": row.get("prev_week_referrals", 0),
        }

    # ─── LEADERBOARDS ───────────────────────────────────────────────

    async def get_leaderboard_alltime(self, limit: int = 10) -> list[dict]:
        """Top referrers by all-time total."""
        rows = await db.fetch_all(
            """SELECT user_id, total_referrals, curr_week_referrals, prev_week_referrals
               FROM referrals
               WHERE total_referrals > 0
               ORDER BY total_referrals DESC
               LIMIT %s""",
            (limit,),
        )
        return [dict(r) for r in rows]

    async def get_leaderboard_week(self, limit: int = 10) -> list[dict]:
        """Top referrers by current week."""
        rows = await db.fetch_all(
            """SELECT user_id, total_referrals, curr_week_referrals, prev_week_referrals
               FROM referrals
               WHERE curr_week_referrals > 0
               ORDER BY curr_week_referrals DESC
               LIMIT %s""",
            (limit,),
        )
        return [dict(r) for r in rows]

    async def get_previous_week_stats(self) -> list[dict]:
        """All users who had referrals last week, sorted descending."""
        rows = await db.fetch_all(
            """SELECT user_id, prev_week_referrals, total_referrals
               FROM referrals
               WHERE prev_week_referrals > 0
               ORDER BY prev_week_referrals DESC"""
        )
        return [dict(r) for r in rows]

    # ─── WEEKLY RESET ───────────────────────────────────────────────

    async def weekly_reset(self):
        """Shift curr_week → prev_week, zero curr_week for all users."""
        await db.execute(
            """UPDATE referrals
               SET prev_week_referrals = curr_week_referrals,
                   curr_week_referrals = 0"""
        )
        logger.info("Referral weekly stats reset complete.")


# Singleton
referral_service = ReferralService()
