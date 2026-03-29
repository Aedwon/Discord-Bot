"""
Quest Service - CRUD operations for the quest definition catalog.
Manages the master list of quests that admins can create, edit, and delete.
The quest progress/reward system will be built on top of this in the future.
"""

import logging
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


class QuestService:
    """Handles quest definition CRUD operations."""

    # ─── CREATE ─────────────────────────────────────────────────────

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
        Raises ValueError for invalid tier/task_type.
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

    # ─── READ ───────────────────────────────────────────────────────

    async def get_quest(self, quest_id: int) -> dict | None:
        """Get a single quest by ID."""
        return await db.fetch_one(
            "SELECT * FROM quests WHERE id = %s", (quest_id,)
        )

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

    # ─── UPDATE ─────────────────────────────────────────────────────

    async def update_quest(self, quest_id: int, **fields) -> bool:
        """
        Update specific fields on a quest.
        
        Accepted fields: name, description, tier, task_type, target_goal, is_active.
        Returns True if the quest was found and updated.
        """
        allowed = {"name", "description", "tier", "task_type", "target_goal", "is_active"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}

        if not updates:
            return False

        # Validate tier/task_type if provided
        if "tier" in updates and updates["tier"] not in QUEST_TIERS:
            raise ValueError(f"Invalid tier '{updates['tier']}'.")
        if "task_type" in updates and updates["task_type"] not in QUEST_TASK_TYPES:
            raise ValueError(f"Invalid task_type '{updates['task_type']}'.")
        if "target_goal" in updates and updates["target_goal"] <= 0:
            raise ValueError("target_goal must be a positive integer.")

        set_clauses = ", ".join(f"{k} = %s" for k in updates)
        params = list(updates.values()) + [quest_id]

        await db.execute(
            f"UPDATE quests SET {set_clauses} WHERE id = %s",
            tuple(params)
        )

        logger.info(f"Quest #{quest_id} updated: {updates}")
        return True

    # ─── DELETE ─────────────────────────────────────────────────────

    async def delete_quest(self, quest_id: int) -> bool:
        """
        Permanently delete a quest definition.
        Returns True if a row was deleted (quest existed).
        """
        # Check existence first for accurate return value
        existing = await self.get_quest(quest_id)
        if not existing:
            return False

        await db.execute("DELETE FROM quests WHERE id = %s", (quest_id,))
        logger.info(f"Quest #{quest_id} '{existing['name']}' deleted.")
        return True

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
