"""
Leaderboard Service — Centralised data queries for the Dual Leaderboard engine.

Provides all-time and weekly ranking queries for every tracked category,
MSL exclusion helpers, and the Monday-midnight weekly reset logic.
"""

import logging
from datetime import datetime, timedelta, timezone

from services.database import db
from services.verification_service import verification_service

logger = logging.getLogger("mlbb_bot.leaderboard_service")

# UTC+8 (Philippine Standard Time / Manila)
TZ_PHT = timezone(timedelta(hours=8))


class LeaderboardService:
    """Singleton service for all leaderboard data access."""

    # ─── WEEK BOUNDARIES ────────────────────────────────────────────

    @staticmethod
    def get_week_start() -> datetime:
        """Return the most recent Monday 00:00 UTC+8 as a UTC datetime."""
        now_pht = datetime.now(TZ_PHT)
        days_since_monday = now_pht.weekday()  # Monday = 0
        monday_pht = now_pht.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_since_monday)
        return monday_pht.astimezone(timezone.utc)

    @staticmethod
    def get_next_week_start() -> datetime:
        """Return the *next* Monday 00:00 UTC+8 as a UTC datetime."""
        now_pht = datetime.now(TZ_PHT)
        days_until_monday = (7 - now_pht.weekday()) % 7
        if days_until_monday == 0 and (now_pht.hour > 0 or now_pht.minute > 0):
            days_until_monday = 7
        next_monday_pht = (now_pht + timedelta(days=days_until_monday)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return next_monday_pht.astimezone(timezone.utc)

    # ─── MSL EXCLUSION ──────────────────────────────────────────────

    async def get_msl_user_ids(self) -> set[int]:
        """
        Build a set of Discord user IDs that belong to verified MSL members.
        Cross-references verified_users with the in-memory MSL cache.
        """
        try:
            rows = await db.fetch_all(
                "SELECT user_id, mlbb_uid, mlbb_server FROM verified_users"
            )
            if not rows:
                return set()

            msl_ids: set[int] = set()
            for row in rows:
                uid = row["mlbb_uid"]
                server = row["mlbb_server"]
                if verification_service.is_msl(uid, server):
                    msl_ids.add(row["user_id"])

            return msl_ids
        except Exception as e:
            logger.warning(f"Failed to build MSL exclusion set: {e}")
            return set()

    @staticmethod
    def _build_exclusion_clause(exclude_ids: set[int], param_list: list) -> str:
        """
        Build a SQL `AND user_id NOT IN (...)` clause if there are IDs to exclude.
        Appends the IDs to *param_list* for parameterised queries.
        """
        if not exclude_ids:
            return ""
        placeholders = ", ".join(["%s"] * len(exclude_ids))
        param_list.extend(exclude_ids)
        return f" AND user_id NOT IN ({placeholders})"

    @staticmethod
    def _build_exclusion_clause_alias(
        alias: str, exclude_ids: set[int], param_list: list,
        column: str = "user_id"
    ) -> str:
        """Same as above but with a table alias prefix, e.g. 'u.user_id' or 'm.author_id'."""
        if not exclude_ids:
            return ""
        placeholders = ", ".join(["%s"] * len(exclude_ids))
        param_list.extend(exclude_ids)
        return f" AND {alias}.{column} NOT IN ({placeholders})"

    # ═══════════════════════════════════════════════════════════════════
    #  ALL-TIME QUERIES
    # ═══════════════════════════════════════════════════════════════════

    async def get_alltime_xp(self, limit: int = 10, exclude_ids: set[int] | None = None) -> list[dict]:
        params: list = []
        excl = self._build_exclusion_clause(exclude_ids or set(), params)
        params.append(limit)
        return await db.fetch_all(
            f"SELECT user_id, xp FROM users WHERE xp > 0{excl} ORDER BY xp DESC LIMIT %s",
            tuple(params),
        )

    async def get_alltime_ep(self, limit: int = 10, exclude_ids: set[int] | None = None) -> list[dict]:
        params: list = []
        excl = self._build_exclusion_clause_alias("u", exclude_ids or set(), params)
        params.append(limit)
        return await db.fetch_all(
            f"""SELECT u.user_id, u.event_points,
                       (SELECT COUNT(*) FROM event_redemptions e WHERE e.user_id = u.user_id) as total_events
                FROM users u
                WHERE u.event_points > 0{excl}
                ORDER BY u.event_points DESC LIMIT %s""",
            tuple(params),
        )

    async def get_alltime_quiz(self, limit: int = 10, exclude_ids: set[int] | None = None) -> list[dict]:
        params: list = []
        excl = self._build_exclusion_clause(exclude_ids or set(), params)
        params.append(limit)
        return await db.fetch_all(
            f"""SELECT user_id, SUM(score) as total_score, COUNT(*) as sessions
                FROM quiz_history
                WHERE score > 0{excl}
                GROUP BY user_id
                ORDER BY total_score DESC LIMIT %s""",
            tuple(params),
        )

    async def get_alltime_referrals(self, limit: int = 10, exclude_ids: set[int] | None = None) -> list[dict]:
        params: list = []
        excl = self._build_exclusion_clause(exclude_ids or set(), params)
        params.append(limit)
        return await db.fetch_all(
            f"""SELECT user_id, total_referrals
                FROM referrals
                WHERE total_referrals > 0{excl}
                ORDER BY total_referrals DESC LIMIT %s""",
            tuple(params),
        )

    async def get_alltime_boosting(self, limit: int = 10, exclude_ids: set[int] | None = None) -> list[dict]:
        """Top boosters by streak duration (days since boost_start_date)."""
        params: list = []
        excl = self._build_exclusion_clause(exclude_ids or set(), params)
        params.append(limit)
        return await db.fetch_all(
            f"""SELECT user_id, boost_start_date,
                       DATEDIFF(NOW(), boost_start_date) as days_boosting
                FROM users
                WHERE boost_start_date IS NOT NULL{excl}
                ORDER BY boost_start_date ASC LIMIT %s""",
            tuple(params),
        )

    async def get_alltime_counting(self) -> dict:
        """High score + top contributors for the counting game."""
        state = await db.fetch_one(
            "SELECT current_count, high_score, high_score_broken_by FROM counting_state LIMIT 1"
        )
        curr_contrib = await db.fetch_all(
            "SELECT user_id, count FROM counting_current_contributors ORDER BY count DESC LIMIT 5"
        )
        hs_contrib = await db.fetch_all(
            "SELECT user_id, count FROM counting_highscore_contributors ORDER BY count DESC LIMIT 5"
        )
        return {
            "state": state,
            "current_contributors": curr_contrib or [],
            "highscore_contributors": hs_contrib or [],
        }

    async def get_alltime_voice(self, limit: int = 10, exclude_ids: set[int] | None = None) -> list[dict]:
        """Top users by total voice session minutes (all-time)."""
        params: list = []
        excl = self._build_exclusion_clause(exclude_ids or set(), params)
        params.append(limit)
        return await db.fetch_all(
            f"""SELECT user_id,
                       ROUND(SUM(TIMESTAMPDIFF(SECOND, joined_at, COALESCE(left_at, NOW()))) / 60) as total_minutes
                FROM analytics_voice_sessions
                WHERE (left_at IS NOT NULL OR joined_at IS NOT NULL){excl}
                GROUP BY user_id
                HAVING total_minutes > 0
                ORDER BY total_minutes DESC LIMIT %s""",
            tuple(params),
        )

    async def get_alltime_messages(self, limit: int = 10, exclude_ids: set[int] | None = None) -> list[dict]:
        """Top users by message count (all-time, 3+ word messages only)."""
        params: list = []
        excl = self._build_exclusion_clause_alias("m", exclude_ids or set(), params, column="author_id")
        params.append(limit)
        return await db.fetch_all(
            f"""SELECT m.author_id as user_id, COUNT(*) as total_messages
                FROM analytics_messages m
                WHERE m.word_count >= 3 AND m.is_deleted = FALSE{excl}
                GROUP BY m.author_id
                ORDER BY total_messages DESC LIMIT %s""",
            tuple(params),
        )

    # ═══════════════════════════════════════════════════════════════════
    #  WEEKLY QUERIES
    # ═══════════════════════════════════════════════════════════════════

    async def get_weekly_xp(self, limit: int = 10, exclude_ids: set[int] | None = None) -> list[dict]:
        """Top users by XP gained this week (current - snapshot)."""
        params: list = []
        excl = self._build_exclusion_clause_alias("u", exclude_ids or set(), params)
        params.append(limit)
        return await db.fetch_all(
            f"""SELECT u.user_id,
                       (u.xp - COALESCE(s.xp_snapshot, 0)) as weekly_xp
                FROM users u
                LEFT JOIN weekly_leaderboard_snapshots s ON u.user_id = s.user_id
                WHERE (u.xp - COALESCE(s.xp_snapshot, 0)) > 0{excl}
                ORDER BY weekly_xp DESC LIMIT %s""",
            tuple(params),
        )

    async def get_weekly_ep(self, limit: int = 10, exclude_ids: set[int] | None = None) -> list[dict]:
        """Top users by EP gained this week (current - snapshot)."""
        params: list = []
        excl = self._build_exclusion_clause_alias("u", exclude_ids or set(), params)
        params.append(limit)
        return await db.fetch_all(
            f"""SELECT u.user_id,
                       (u.event_points - COALESCE(s.ep_snapshot, 0)) as weekly_ep
                FROM users u
                LEFT JOIN weekly_leaderboard_snapshots s ON u.user_id = s.user_id
                WHERE (u.event_points - COALESCE(s.ep_snapshot, 0)) > 0{excl}
                ORDER BY weekly_ep DESC LIMIT %s""",
            tuple(params),
        )

    async def get_weekly_quiz(self, limit: int = 10, exclude_ids: set[int] | None = None, *, week_start: datetime | None = None) -> list[dict]:
        """Top quiz scorers since the start of the current week."""
        week_start = week_start or self.get_week_start()
        params: list = [week_start]
        excl = self._build_exclusion_clause(exclude_ids or set(), params)
        params.append(limit)
        return await db.fetch_all(
            f"""SELECT user_id, SUM(score) as total_score, COUNT(*) as sessions
                FROM quiz_history
                WHERE earned_at >= %s{excl}
                GROUP BY user_id
                ORDER BY total_score DESC LIMIT %s""",
            tuple(params),
        )

    async def get_weekly_referrals(self, limit: int = 10, exclude_ids: set[int] | None = None) -> list[dict]:
        """Top referrers by current week count."""
        params: list = []
        excl = self._build_exclusion_clause(exclude_ids or set(), params)
        params.append(limit)
        return await db.fetch_all(
            f"""SELECT user_id, curr_week_referrals
                FROM referrals
                WHERE curr_week_referrals > 0{excl}
                ORDER BY curr_week_referrals DESC LIMIT %s""",
            tuple(params),
        )

    async def get_weekly_counting(self) -> dict:
        """Current streak + weekly contributors."""
        state = await db.fetch_one(
            "SELECT current_count, high_score, high_score_broken_by FROM counting_state LIMIT 1"
        )
        weekly_contrib = await db.fetch_all(
            "SELECT user_id, count FROM counting_weekly_contributors ORDER BY count DESC LIMIT 10"
        )
        return {
            "state": state,
            "weekly_contributors": weekly_contrib or [],
        }

    async def get_weekly_voice(self, limit: int = 10, exclude_ids: set[int] | None = None, *, week_start: datetime | None = None) -> list[dict]:
        """Top users by voice minutes this week."""
        week_start = week_start or self.get_week_start()
        params: list = [week_start]
        excl = self._build_exclusion_clause(exclude_ids or set(), params)
        params.append(limit)
        return await db.fetch_all(
            f"""SELECT user_id,
                       ROUND(SUM(TIMESTAMPDIFF(SECOND, joined_at, COALESCE(left_at, NOW()))) / 60) as total_minutes
                FROM analytics_voice_sessions
                WHERE joined_at >= %s{excl}
                GROUP BY user_id
                HAVING total_minutes > 0
                ORDER BY total_minutes DESC LIMIT %s""",
            tuple(params),
        )

    async def get_weekly_messages(self, limit: int = 10, exclude_ids: set[int] | None = None, *, week_start: datetime | None = None) -> list[dict]:
        """Top users by message count this week (3+ words, not deleted)."""
        week_start = week_start or self.get_week_start()
        params: list = [week_start]
        excl = self._build_exclusion_clause_alias("m", exclude_ids or set(), params, column="author_id")
        params.append(limit)
        return await db.fetch_all(
            f"""SELECT m.author_id as user_id, COUNT(*) as total_messages
                FROM analytics_messages m
                WHERE m.created_at >= %s AND m.word_count >= 3 AND m.is_deleted = FALSE{excl}
                GROUP BY m.author_id
                ORDER BY total_messages DESC LIMIT %s""",
            tuple(params),
        )

    # ═══════════════════════════════════════════════════════════════════
    #  WEEKLY RESET
    # ═══════════════════════════════════════════════════════════════════

    async def run_weekly_reset(self) -> int:
        """
        Snapshot current XP/EP into weekly_leaderboard_snapshots and
        reset counting_weekly_contributors.

        Called at Monday 00:00 UTC+8 by the leaderboard cog.
        Returns the number of users snapshotted.
        """
        try:
            # 1. Snapshot all users with positive XP or EP
            await db.execute("""
                INSERT INTO weekly_leaderboard_snapshots (user_id, xp_snapshot, ep_snapshot, snapshot_at)
                SELECT user_id, xp, event_points, NOW()
                FROM users
                WHERE xp > 0 OR event_points > 0
                ON DUPLICATE KEY UPDATE
                    xp_snapshot = VALUES(xp_snapshot),
                    ep_snapshot = VALUES(ep_snapshot),
                    snapshot_at = NOW()
            """)

            # 2. Get count
            count_row = await db.fetch_one(
                "SELECT COUNT(*) as cnt FROM weekly_leaderboard_snapshots"
            )
            count = count_row["cnt"] if count_row else 0

            # 3. Reset counting weekly contributors
            await db.execute("DELETE FROM counting_weekly_contributors")

            logger.info(f"Weekly leaderboard reset complete: {count} users snapshotted, counting weekly contributors cleared.")
            return count

        except Exception as e:
            logger.error(f"Weekly leaderboard reset failed: {e}")
            return 0

    # ═══════════════════════════════════════════════════════════════════
    #  WEEKLY ARCHIVE (history logging)
    # ═══════════════════════════════════════════════════════════════════

    # Category definitions: (key, query_method_name, value_field_key, uses_week_start)
    WEEKLY_CATEGORIES = [
        ("xp", "get_weekly_xp", "weekly_xp", False),
        ("ep", "get_weekly_ep", "weekly_ep", False),
        ("quiz", "get_weekly_quiz", "total_score", True),
        ("referral", "get_weekly_referrals", "curr_week_referrals", False),
        ("voice", "get_weekly_voice", "total_minutes", True),
        ("messages", "get_weekly_messages", "total_messages", True),
    ]

    async def archive_weekly_standings(self, exclude_ids: set[int]) -> tuple[str, int]:
        """
        Capture the final weekly standings (top 10 per category) into
        weekly_leaderboard_history. Called BEFORE run_weekly_reset().

        Returns (week_id, total_rows_archived). Idempotent — skips if already exists.
        """
        now_pht = datetime.now(TZ_PHT)
        week_id = now_pht.strftime("%Y-W%W")

        # Previous week's Monday 00:00 PHT -> UTC (for timestamp-based queries)
        prev_week_start = (now_pht - timedelta(days=7)).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).astimezone(timezone.utc)

        # Idempotency check
        existing = await db.fetch_one(
            "SELECT COUNT(*) as cnt FROM weekly_leaderboard_history WHERE week_id = %s",
            (week_id,)
        )
        if existing and existing["cnt"] > 0:
            logger.info(f"Weekly archive for {week_id} already exists ({existing['cnt']} rows). Skipping.")
            return week_id, 0

        total_archived = 0
        snapshot_time = now_pht.strftime("%Y-%m-%d %H:%M:%S")

        # Archive standard categories (XP, EP, Quiz, Referral, Voice, Messages)
        for cat_key, method_name, value_key, uses_week_start in self.WEEKLY_CATEGORIES:
            try:
                method = getattr(self, method_name)
                if uses_week_start:
                    rows = await method(10, exclude_ids, week_start=prev_week_start)
                else:
                    rows = await method(10, exclude_ids)
                if not rows:
                    continue

                for rank, row in enumerate(rows, 1):
                    value = row.get(value_key, 0) or 0
                    await db.execute(
                        """INSERT INTO weekly_leaderboard_history
                           (week_id, category, rank_position, user_id, value, extra_info, snapshot_at)
                           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                        (week_id, cat_key, rank, row["user_id"], int(value), None, snapshot_time)
                    )
                    total_archived += 1
            except Exception as e:
                logger.error(f"Failed to archive category '{cat_key}': {e}")
                continue

        # Archive counting (special structure — uses weekly_contributors list)
        try:
            counting_data = await self.get_weekly_counting()
            weekly_contrib = counting_data.get("weekly_contributors", [])
            for rank, row in enumerate(weekly_contrib[:10], 1):
                await db.execute(
                    """INSERT INTO weekly_leaderboard_history
                       (week_id, category, rank_position, user_id, value, extra_info, snapshot_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (week_id, "counting", rank, row["user_id"], row["count"], None, snapshot_time)
                )
                total_archived += 1
        except Exception as e:
            logger.error(f"Failed to archive counting: {e}")

        logger.info(f"Weekly archive complete for {week_id}: {total_archived} rows archived.")
        return week_id, total_archived

    async def get_archived_weeks(self, limit: int = 12) -> list[dict]:
        """Return list of distinct archived week_ids, most recent first."""
        return await db.fetch_all(
            """SELECT DISTINCT week_id, MIN(snapshot_at) as archived_at, COUNT(*) as total_entries
               FROM weekly_leaderboard_history
               GROUP BY week_id
               ORDER BY week_id DESC LIMIT %s""",
            (limit,)
        )

    async def get_archived_week_data(
        self, week_id: str, category: str | None = None
    ) -> list[dict]:
        """Return archived rows for a specific week, optionally filtered by category."""
        if category:
            return await db.fetch_all(
                """SELECT category, rank_position, user_id, value, extra_info
                   FROM weekly_leaderboard_history
                   WHERE week_id = %s AND category = %s
                   ORDER BY category, rank_position""",
                (week_id, category)
            )
        return await db.fetch_all(
            """SELECT category, rank_position, user_id, value, extra_info
               FROM weekly_leaderboard_history
               WHERE week_id = %s
               ORDER BY category, rank_position""",
            (week_id,)
        )

    # ═══════════════════════════════════════════════════════════════════
    #  WEEKLY PRIZE CALCULATION
    # ═══════════════════════════════════════════════════════════════════

    # EP prizes by rank position (EP category excluded — Diamonds only)
    PRIZE_EP = {1: 150, 2: 100, 3: 75}
    PRIZE_EP_DEFAULT = 50   # Ranks 4–10
    PRIZE_CATEGORIES = {"xp", "quiz", "counting", "referral"}

    async def calculate_weekly_prizes(self, week_id: str) -> list[dict]:
        """
        Calculate EP prizes from the archived weekly standings.

        Reads `weekly_leaderboard_history`, filters to prize-eligible categories,
        maps rank → EP amount, and aggregates across categories per user.

        Returns a list sorted by total_ep descending:
        [
            {
                "user_id": int,
                "total_ep": int,
                "breakdown": {"xp": 150, "quiz": 50, ...}
            },
            ...
        ]
        """
        rows = await db.fetch_all(
            """SELECT category, rank_position, user_id, value
               FROM weekly_leaderboard_history
               WHERE week_id = %s AND rank_position <= 10
               ORDER BY category, rank_position""",
            (week_id,),
        )

        if not rows:
            return []

        # Aggregate prizes per user across categories
        user_prizes: dict[int, dict] = {}  # user_id → {"total_ep": int, "breakdown": {}}

        for row in rows:
            cat = row["category"]
            if cat not in self.PRIZE_CATEGORIES:
                continue

            rank = row["rank_position"]
            user_id = row["user_id"]
            ep_award = self.PRIZE_EP.get(rank, self.PRIZE_EP_DEFAULT)

            if user_id not in user_prizes:
                user_prizes[user_id] = {"user_id": user_id, "total_ep": 0, "breakdown": {}}

            user_prizes[user_id]["total_ep"] += ep_award
            user_prizes[user_id]["breakdown"][cat] = ep_award

        # Sort by total EP descending for display
        result = sorted(user_prizes.values(), key=lambda x: x["total_ep"], reverse=True)
        return result

    async def backfill_archived_week(
        self,
        week_id: str,
        week_start_utc: datetime,
        week_end_utc: datetime,
        exclude_ids: set[int]
    ) -> dict[str, int]:
        """
        Backfill missing categories into an existing archived week.
        Returns dict mapping category -> number of rows inserted.
        """
        existing = await self.get_archived_week_data(week_id)
        existing_cats = {row["category"] for row in existing}
        
        inserted: dict[str, int] = {}
        now_str = datetime.now(TZ_PHT).strftime("%Y-%m-%d %H:%M:%S")

        async def _insert_rows(cat, rows, val_key):
            count = 0
            for rank, r in enumerate(rows, 1):
                val = r.get(val_key, 0) or 0
                await db.execute(
                    """INSERT INTO weekly_leaderboard_history
                       (week_id, category, rank_position, user_id, value, extra_info, snapshot_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (week_id, cat, rank, r["user_id"], int(val), None, now_str)
                )
                count += 1
            if count > 0:
                inserted[cat] = count
                
        # Quiz
        if "quiz" not in existing_cats:
            params = [week_start_utc, week_end_utc]
            excl = self._build_exclusion_clause(exclude_ids, params)
            params.append(10)
            rows = await db.fetch_all(
                f"""SELECT user_id, SUM(score) as total_score
                    FROM quiz_history
                    WHERE earned_at >= %s AND earned_at < %s{excl}
                    GROUP BY user_id
                    ORDER BY total_score DESC LIMIT %s""",
                tuple(params)
            )
            await _insert_rows("quiz", rows, "total_score")
            
        # Referral
        if "referral" not in existing_cats:
            now_pht = datetime.now(TZ_PHT)
            expected_prev_week_id = (now_pht - timedelta(days=7)).strftime("%Y-W%W")
            
            if week_id == expected_prev_week_id:
                params = []
                excl = self._build_exclusion_clause(exclude_ids, params)
                params.append(10)
                rows = await db.fetch_all(
                    f"""SELECT user_id, prev_week_referrals as curr_week_referrals
                        FROM referrals
                        WHERE prev_week_referrals > 0{excl}
                        ORDER BY prev_week_referrals DESC LIMIT %s""",
                    tuple(params)
                )
                await _insert_rows("referral", rows, "curr_week_referrals")
            else:
                logger.warning(f"Skipping referral backfill for {week_id}: prev_week_referrals data is stale.")

        # Voice
        if "voice" not in existing_cats:
            params = [week_start_utc, week_end_utc]
            excl = self._build_exclusion_clause(exclude_ids, params)
            params.append(10)
            rows = await db.fetch_all(
                f"""SELECT user_id,
                           ROUND(SUM(TIMESTAMPDIFF(SECOND, joined_at, COALESCE(left_at, NOW()))) / 60) as total_minutes
                    FROM analytics_voice_sessions
                    WHERE joined_at >= %s AND joined_at < %s{excl}
                    GROUP BY user_id
                    HAVING total_minutes > 0
                    ORDER BY total_minutes DESC LIMIT %s""",
                tuple(params)
            )
            await _insert_rows("voice", rows, "total_minutes")

        # Messages
        if "messages" not in existing_cats:
            params = [week_start_utc, week_end_utc]
            excl = self._build_exclusion_clause_alias("m", exclude_ids, params, column="author_id")
            params.append(10)
            rows = await db.fetch_all(
                f"""SELECT m.author_id as user_id, COUNT(*) as total_messages
                    FROM analytics_messages m
                    WHERE m.created_at >= %s AND m.created_at < %s AND m.word_count >= 3 AND m.is_deleted = FALSE{excl}
                    GROUP BY m.author_id
                    ORDER BY total_messages DESC LIMIT %s""",
                tuple(params)
            )
            await _insert_rows("messages", rows, "total_messages")
            
        return inserted

    async def calculate_prize_delta(
        self,
        week_id: str,
        already_awarded_categories: set[str],
    ) -> list[dict]:
        """Calculate EP owed for newly-backfilled prize-eligible categories."""
        rows = await db.fetch_all(
            """SELECT category, rank_position, user_id, value
               FROM weekly_leaderboard_history
               WHERE week_id = %s AND rank_position <= 10
               ORDER BY category, rank_position""",
            (week_id,),
        )

        if not rows:
            return []

        user_prizes: dict[int, dict] = {}
        for row in rows:
            cat = row["category"]
            if cat not in self.PRIZE_CATEGORIES:
                continue
            if cat in already_awarded_categories:
                continue

            rank = row["rank_position"]
            user_id = row["user_id"]
            ep_award = self.PRIZE_EP.get(rank, self.PRIZE_EP_DEFAULT)

            if user_id not in user_prizes:
                user_prizes[user_id] = {"user_id": user_id, "total_ep": 0, "breakdown": {}}

            user_prizes[user_id]["total_ep"] += ep_award
            user_prizes[user_id]["breakdown"][cat] = ep_award

        result = sorted(user_prizes.values(), key=lambda x: x["total_ep"], reverse=True)
        return result


# Singleton
leaderboard_service = LeaderboardService()

