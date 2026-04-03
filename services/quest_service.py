"""
Quest Service - CRUD operations for quest definitions + user quest generation & progress.
Manages the admin quest catalog and per-user daily quest assignments.
"""

import logging
import random
from datetime import date, datetime, timezone

from services.database import db

logger = logging.getLogger("mlbb_bot.quest_service")

# ─── Constants ──────────────────────────────────────────────────────────

QUEST_TIERS = ["common", "uncommon", "rare"]
QUEST_TASK_TYPES = ["message_count", "vc_minutes", "reaction_count"]

TIER_DISPLAY = {
    "common": {"emoji": "⭐", "label": "Common"},
    "uncommon": {"emoji": "💎", "label": "Uncommon"},
    "rare": {"emoji": "🌟", "label": "Rare"},
}

TASK_TYPE_DISPLAY = {
    "message_count": {"label": "Messages", "unit": "msgs"},
    "vc_minutes": {"label": "VC Minutes", "unit": "min"},
    "reaction_count": {"label": "Reactions", "unit": "reacts"},
}

TIER_REWARDS = {
    "common": 50,
    "uncommon": 150,
    "rare": 500,
}

# Weighted odds for the 3rd quest slot
THIRD_SLOT_WEIGHTS = {"common": 70, "uncommon": 25, "rare": 5}


class QuestService:
    """Handles quest definition CRUD + user quest generation & progress."""

    # ─── ADMIN CRUD ─────────────────────────────────────────────────

    async def create_quest(
        self,
        name: str,
        description: str,
        tier: str,
        task_type: str,
        target_goal: int,
        created_by: int,
    ) -> int:
        """
        Create a new quest definition.
        Returns the auto-generated quest ID.
        """
        if tier not in QUEST_TIERS:
            raise ValueError(f"Invalid tier '{tier}'. Must be one of: {QUEST_TIERS}")
        if task_type not in QUEST_TASK_TYPES:
            raise ValueError(f"Invalid task_type '{task_type}'. Must be one of: {QUEST_TASK_TYPES}")
        if target_goal <= 0:
            raise ValueError("target_goal must be a positive integer.")

        quest_id = await db.insert_get_id('''
            INSERT INTO quests (name, description, tier, task_type, target_goal, created_by)
            VALUES (%s, %s, %s, %s, %s, %s)
        ''', (name, description, tier, task_type, target_goal, created_by))

        logger.info(f"Quest created: #{quest_id} '{name}' ({tier}/{task_type}, goal={target_goal}) by {created_by}")
        return quest_id

    async def get_quest(self, quest_id: int) -> dict | None:
        """Get a single quest by ID."""
        return await db.fetch_one("SELECT * FROM quests WHERE id = %s", (quest_id,))

    async def get_active_quests(self) -> list[dict]:
        """Get all active quest definitions, ordered by tier then name."""
        rows = await db.fetch_all('''
            SELECT * FROM quests 
            WHERE is_active = TRUE 
            ORDER BY FIELD(tier, 'common', 'uncommon', 'rare'), name
        ''')
        return list(rows) if rows else []

    async def get_all_quests(self) -> list[dict]:
        """Get all quest definitions (including inactive), ordered by tier."""
        rows = await db.fetch_all('''
            SELECT * FROM quests 
            ORDER BY FIELD(tier, 'common', 'uncommon', 'rare'), name
        ''')
        return list(rows) if rows else []

    async def update_quest(self, quest_id: int, **fields) -> bool:
        """Update specific fields on a quest."""
        allowed = {"name", "description", "tier", "task_type", "target_goal", "is_active"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}

        if not updates:
            return False

        if "tier" in updates and updates["tier"] not in QUEST_TIERS:
            raise ValueError(f"Invalid tier '{updates['tier']}'.")
        if "task_type" in updates and updates["task_type"] not in QUEST_TASK_TYPES:
            raise ValueError(f"Invalid task_type '{updates['task_type']}'.")
        if "target_goal" in updates and updates["target_goal"] <= 0:
            raise ValueError("target_goal must be a positive integer.")

        set_clauses = ", ".join(f"{k} = %s" for k in updates)
        params = list(updates.values()) + [quest_id]

        await db.execute(f"UPDATE quests SET {set_clauses} WHERE id = %s", tuple(params))
        logger.info(f"Quest #{quest_id} updated: {updates}")
        return True

    async def delete_quest(self, quest_id: int) -> bool:
        """Permanently delete a quest definition."""
        existing = await self.get_quest(quest_id)
        if not existing:
            return False

        await db.execute("DELETE FROM quests WHERE id = %s", (quest_id,))
        logger.info(f"Quest #{quest_id} '{existing['name']}' deleted.")
        return True

    # ─── QUEST GENERATION ───────────────────────────────────────────

    async def _get_quests_by_tier(self) -> dict[str, list[dict]]:
        """Fetch all active quests grouped by tier."""
        all_quests = await self.get_active_quests()
        grouped = {"common": [], "uncommon": [], "rare": []}
        for q in all_quests:
            if q["tier"] in grouped:
                grouped[q["tier"]].append(q)
        return grouped

    def _pick_weighted_tier(self, available_tiers: list[str]) -> str | None:
        """Pick a tier for slot 3 using weighted odds, restricted to available tiers."""
        if not available_tiers:
            return None

        tiers = [t for t in available_tiers if t in THIRD_SLOT_WEIGHTS]
        if not tiers:
            return None

        weights = [THIRD_SLOT_WEIGHTS[t] for t in tiers]
        return random.choices(tiers, weights=weights, k=1)[0]

    async def generate_quests_for_user(self, user_id: int) -> list[dict]:
        """
        Generate 3 daily quests for a user.
        
        Slot 1: Random common quest
        Slot 2: Random uncommon quest
        Slot 3: Weighted random (70% common / 25% uncommon / 5% rare)
        
        Falls back to available tiers if a tier has no quests.
        Avoids duplicate quest picks across slots where possible.
        Returns the list of assigned quest dicts (with slot info).
        """
        today = date.today()
        grouped = await self._get_quests_by_tier()

        # Require at least 1 quest in every tier before generating
        missing_tiers = [t for t in QUEST_TIERS if not grouped.get(t)]
        if missing_tiers:
            logger.warning(f"Quest generation skipped: missing tiers {missing_tiers}")
            return []

        # Delete any existing quests for this user (covers stale days + rerolls)
        await db.execute("DELETE FROM quest_progress WHERE user_id = %s", (user_id,))

        picked_ids = set()
        assignments = []

        def pick_from_tier(tier: str) -> dict | None:
            """Pick a random quest from a tier, avoiding already-picked quests."""
            pool = [q for q in grouped.get(tier, []) if q["id"] not in picked_ids]
            if not pool:
                # Fallback: allow duplicates from the same tier
                pool = grouped.get(tier, [])
            return random.choice(pool) if pool else None

        def pick_with_fallback(preferred: str) -> dict | None:
            """Try preferred tier first, then fall back to any available tier."""
            quest = pick_from_tier(preferred)
            if quest:
                return quest
            # Fallback chain: try other tiers
            for fallback in QUEST_TIERS:
                if fallback != preferred:
                    quest = pick_from_tier(fallback)
                    if quest:
                        return quest
            return None

        # Slot 1: Common
        q1 = pick_with_fallback("common")
        if q1:
            picked_ids.add(q1["id"])
            assignments.append((1, q1))

        # Slot 2: Uncommon
        q2 = pick_with_fallback("uncommon")
        if q2:
            picked_ids.add(q2["id"])
            assignments.append((2, q2))

        # Slot 3: Weighted roll
        third_tier = self._pick_weighted_tier(QUEST_TIERS)
        if third_tier:
            q3 = pick_with_fallback(third_tier)
            if q3:
                picked_ids.add(q3["id"])
                assignments.append((3, q3))

        # Insert into quest_progress
        for slot, quest in assignments:
            await db.execute('''
                INSERT INTO quest_progress (user_id, quest_id, slot, progress, completed, assigned_date)
                VALUES (%s, %s, %s, 0, FALSE, %s)
            ''', (user_id, quest["id"], slot, today))

        logger.info(
            f"Generated {len(assignments)} quests for user {user_id}: "
            f"{[(s, q['name'], q['tier']) for s, q in assignments]}"
        )

        return [{"slot": s, **q} for s, q in assignments]

    # ─── USER QUEST RETRIEVAL ───────────────────────────────────────

    async def get_user_quests(self, user_id: int) -> list[dict]:
        """
        Get user's current quest assignments with full quest details.
        
        If assigned_date is stale (not today), returns empty list
        to signal that a regeneration is needed.
        """
        today = date.today()
        rows = await db.fetch_all('''
            SELECT qp.id AS progress_id, qp.slot, qp.progress, qp.completed, 
                   qp.completed_at, qp.assigned_date,
                   q.id AS quest_id, q.name, q.description, q.tier, 
                   q.task_type, q.target_goal
            FROM quest_progress qp
            JOIN quests q ON qp.quest_id = q.id
            WHERE qp.user_id = %s AND qp.assigned_date = %s
            ORDER BY qp.slot
        ''', (user_id, today))

        return list(rows) if rows else []

    # ─── PROGRESS TRACKING ──────────────────────────────────────────

    async def increment_progress(self, user_id: int, task_type: str, amount: int = 1) -> list[dict]:
        """
        Increment progress for all of the user's active (incomplete) quests
        that match the given task_type.
        
        Returns a list of quests that were JUST completed by this increment
        (for XP reward granting). Empty list if nothing completed.
        """
        today = date.today()

        # Fetch the user's active incomplete quests matching task_type
        rows = await db.fetch_all('''
            SELECT qp.id AS progress_id, qp.progress, qp.quest_id,
                   q.name, q.tier, q.task_type, q.target_goal
            FROM quest_progress qp
            JOIN quests q ON qp.quest_id = q.id
            WHERE qp.user_id = %s 
              AND qp.assigned_date = %s
              AND qp.completed = FALSE
              AND q.task_type = %s
        ''', (user_id, today, task_type))

        if not rows:
            return []

        newly_completed = []

        for row in rows:
            new_progress = min(row["progress"] + amount, row["target_goal"])
            just_completed = new_progress >= row["target_goal"]

            if just_completed:
                await db.execute('''
                    UPDATE quest_progress 
                    SET progress = %s, completed = TRUE, completed_at = %s
                    WHERE id = %s AND completed = FALSE
                ''', (new_progress, datetime.now(timezone.utc), row["progress_id"]))

                newly_completed.append({
                    "quest_id": row["quest_id"],
                    "name": row["name"],
                    "tier": row["tier"],
                    "task_type": row["task_type"],
                    "target_goal": row["target_goal"],
                    "reward_xp": TIER_REWARDS.get(row["tier"], 0),
                })
                logger.info(
                    f"Quest completed: user={user_id}, quest='{row['name']}' "
                    f"(+{TIER_REWARDS.get(row['tier'], 0)} XP)"
                )
            else:
                await db.execute(
                    "UPDATE quest_progress SET progress = %s WHERE id = %s",
                    (new_progress, row["progress_id"])
                )

        return newly_completed

    async def all_quests_completed_today(self, user_id: int) -> bool:
        """Check if the user has quests assigned today and ALL are completed."""
        today = date.today()
        row = await db.fetch_one('''
            SELECT 
                COUNT(*) AS total,
                SUM(completed) AS done
            FROM quest_progress
            WHERE user_id = %s AND assigned_date = %s
        ''', (user_id, today))

        if not row or row["total"] == 0:
            return False
        return row["done"] == row["total"]

    # ─── HELPERS ────────────────────────────────────────────────────

    def format_quest_line(self, quest: dict) -> str:
        """Format a quest into the display line: tier  target_goal  task_type."""
        tier_info = TIER_DISPLAY.get(quest["tier"], {"emoji": "❓", "label": quest["tier"]})
        task_info = TASK_TYPE_DISPLAY.get(quest["task_type"], {"unit": "?"})

        return (
            f"{tier_info['emoji']} **{tier_info['label']}** │ "
            f"`{quest['target_goal']} {task_info['unit']}` │ "
            f"{quest['task_type']}"
        )


# Singleton
quest_service = QuestService()
