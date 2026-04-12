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

    async def get_weekly_quiz(self, limit: int = 10, exclude_ids: set[int] | None = None) -> list[dict]:
        """Top quiz scorers since the start of the current week."""
        week_start = self.get_week_start()
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

    async def get_weekly_voice(self, limit: int = 10, exclude_ids: set[int] | None = None) -> list[dict]:
        """Top users by voice minutes this week."""
        week_start = self.get_week_start()
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

    async def get_weekly_messages(self, limit: int = 10, exclude_ids: set[int] | None = None) -> list[dict]:
        """Top users by message count this week (3+ words, not deleted)."""
        week_start = self.get_week_start()
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


# Singleton
leaderboard_service = LeaderboardService()
