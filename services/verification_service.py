"""
Verification Service.
Handles MLBB account verification with in-memory cache for fast lookups.
All XP/EP gating checks use the cache (O(1)), not the database.
Includes MSL (Moonton Student Leader) cross-reference via Google Sheets.
"""

import csv
import io
import re
import aiohttp
from services.database import db
from services.settings_service import settings_service
import logging

logger = logging.getLogger("mlbb_bot.verification")


class VerificationService:
    def __init__(self):
        self._verified_cache: set[int] = set()
        self._msl_cache: dict[tuple[int, int], str] = {}  # {(uid, server): nickname}
        self._loaded = False

    # ─── VERIFICATION CACHE ─────────────────────────────────────────────

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
        if self.is_verified(user_id):
            return "already_verified"

        existing = await db.fetch_one(
            "SELECT user_id FROM verified_users WHERE mlbb_uid = %s",
            (mlbb_uid,)
        )
        if existing:
            return f"uid_taken:{existing['user_id']}"

        await db.execute(
            "INSERT INTO verified_users (user_id, full_name, mlbb_uid, mlbb_server) "
            "VALUES (%s, %s, %s, %s)",
            (user_id, full_name, mlbb_uid, mlbb_server)
        )

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

    # ─── MSL CACHE ──────────────────────────────────────────────────────

    @staticmethod
    def _extract_sheet_id(url: str) -> str | None:
        """Extract the Google Sheets ID from any valid sheets URL."""
        match = re.search(r'/spreadsheets/d/([a-zA-Z0-9_-]+)', url)
        return match.group(1) if match else None

    async def load_msl_cache(self) -> int:
        """
        Fetch the public Google Sheet CSV and build the MSL UID lookup cache.
        Returns the number of MSL entries loaded.
        """
        sheet_url = await settings_service.get("msl_sheet_url")
        if not sheet_url:
            logger.info("MSL sheet URL not configured, skipping")
            return 0

        sheet_id = self._extract_sheet_id(sheet_url)
        if not sheet_id:
            logger.warning(f"Invalid MSL sheet URL: {sheet_url}")
            return 0

        csv_url = (
            f"https://docs.google.com/spreadsheets/d/{sheet_id}"
            f"/gviz/tq?tqx=out:csv&sheet=FINAL"
        )

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(csv_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        logger.warning(f"MSL sheet fetch failed: HTTP {resp.status}")
                        return 0
                    text = await resp.text()

            new_cache: dict[tuple[int, int], str] = {}
            reader = csv.reader(io.StringIO(text))
            rows = list(reader)

            # Data starts at row 4 (index 3), columns: A=Nickname, B=Server, C=UID
            for row in rows[3:]:
                if len(row) < 3:
                    continue
                nickname = row[0].strip()
                server_str = row[1].strip()
                uid_str = row[2].strip()
                if not uid_str or not server_str:
                    continue
                # Handle UIDs and Servers that might have non-numeric chars
                uid_clean = re.sub(r'\D', '', uid_str)
                server_clean = re.sub(r'\D', '', server_str)
                if uid_clean and server_clean:
                    new_cache[(int(uid_clean), int(server_clean))] = nickname

            self._msl_cache = new_cache
            logger.info(f"MSL cache loaded: {len(new_cache)} entries")
            return len(new_cache)

        except Exception as e:
            logger.error(f"Failed to load MSL cache: {e}")
            return 0

    def is_msl(self, mlbb_uid: int, mlbb_server: int) -> bool:
        """O(1) check if a UID and Server belong to an MSL member."""
        return (mlbb_uid, mlbb_server) in self._msl_cache

    def get_msl_nickname(self, mlbb_uid: int, mlbb_server: int) -> str | None:
        """Get the MSL nickname for a UID and Server, or None if not MSL."""
        return self._msl_cache.get((mlbb_uid, mlbb_server))

    @property
    def msl_count(self) -> int:
        return len(self._msl_cache)


# Core API Export
verification_service = VerificationService()
