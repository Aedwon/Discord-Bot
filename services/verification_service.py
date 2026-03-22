"""
Verification Service.
Handles MLBB account verification with in-memory cache for fast lookups.
All XP/EP gating checks use the cache (O(1)), not the database.
"""

from services.database import db
import logging

logger = logging.getLogger("mlbb_bot.verification")


class VerificationService:
    def __init__(self):
        self._verified_cache: set[int] = set()
        self._loaded = False

    async def load_cache(self):
        """Load all verified user IDs into memory. Call once on bot startup."""
        rows = await db.fetch_all("SELECT user_id FROM verified_users")
        self._verified_cache = {row['user_id'] for row in rows} if rows else set()
        self._loaded = True
        logger.info(f"Verification cache loaded: {len(self._verified_cache)} users")

    def is_verified(self, user_id: int) -> bool:
        """O(1) check if a user is verified. Uses in-memory cache."""
        return user_id in self._verified_cache

    async def verify_user(self, user_id: int, full_name: str, mlbb_uid: int, mlbb_server: int) -> str | None:
        """
        Register a user's MLBB account.
        Returns None on success, or an error message string on failure.
        """
        # Check if already verified
        if self.is_verified(user_id):
            return "already_verified"

        # Check UID uniqueness
        existing = await db.fetch_one(
            "SELECT user_id FROM verified_users WHERE mlbb_uid = %s",
            (mlbb_uid,)
        )
        if existing:
            return f"uid_taken:{existing['user_id']}"

        # Insert into DB
        await db.execute(
            "INSERT INTO verified_users (user_id, full_name, mlbb_uid, mlbb_server) "
            "VALUES (%s, %s, %s, %s)",
            (user_id, full_name, mlbb_uid, mlbb_server)
        )

        # Update cache
        self._verified_cache.add(user_id)
        logger.info(f"User {user_id} verified (MLBB UID: {mlbb_uid})")
        return None

    async def unverify_user(self, user_id: int) -> bool:
        """Remove a user's verification. Returns True if they were verified."""
        if not self.is_verified(user_id):
            return False

        await db.execute("DELETE FROM verified_users WHERE user_id = %s", (user_id,))
        self._verified_cache.discard(user_id)
        logger.info(f"User {user_id} unverified")
        return True

    async def get_user_info(self, user_id: int) -> dict | None:
        """Get a user's full verification record."""
        return await db.fetch_one(
            "SELECT * FROM verified_users WHERE user_id = %s", (user_id,)
        )

    async def lookup_by_uid(self, mlbb_uid: int) -> dict | None:
        """Reverse lookup: find a Discord user by their MLBB UID."""
        return await db.fetch_one(
            "SELECT * FROM verified_users WHERE mlbb_uid = %s", (mlbb_uid,)
        )

    async def update_user_info(self, user_id: int, full_name: str, mlbb_uid: int, mlbb_server: int) -> str | None:
        """
        Admin-only: update a user's verification info.
        Returns None on success, or an error message on failure.
        """
        # Check UID uniqueness (exclude current user)
        existing = await db.fetch_one(
            "SELECT user_id FROM verified_users WHERE mlbb_uid = %s AND user_id != %s",
            (mlbb_uid, user_id)
        )
        if existing:
            return f"uid_taken:{existing['user_id']}"

        await db.execute(
            "UPDATE verified_users SET full_name = %s, mlbb_uid = %s, mlbb_server = %s "
            "WHERE user_id = %s",
            (full_name, mlbb_uid, mlbb_server, user_id)
        )
        logger.info(f"User {user_id} info updated by admin (MLBB UID: {mlbb_uid})")
        return None


# Core API Export
verification_service = VerificationService()
