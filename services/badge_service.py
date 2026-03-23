"""
Badge Service - Evaluates conditions and manages granting/revocation of badges + Discord Roles.
"""

import discord
import json
import logging
from services.database import db
from services.settings_service import settings_service

logger = logging.getLogger("mlbb_bot.badge_service")

class BadgeService:
    async def get_badges(self, user_id: int) -> list:
        row = await db.fetch_one("SELECT badges FROM users WHERE user_id = %s", (user_id,))
        if row and row.get("badges"):
            try:
                parsed = json.loads(row["badges"])
                if isinstance(parsed, list):
                    return parsed
            except Exception:
                pass
        return []

    async def add_badge(self, member: discord.Member, badge_name: str, role_setting_key: str = None):
        """Grants a badge and optionally its mapped Discord Role."""
        badges = await self.get_badges(member.id)
        if badge_name not in badges:
            badges.append(badge_name)
            
            # Ensure the user row exists to prevent update failure on new profiles
            await db.execute(
                "INSERT INTO users (user_id, badges) VALUES (%s, %s) ON DUPLICATE KEY UPDATE badges = %s",
                (member.id, json.dumps(badges), json.dumps(badges))
            )
            logger.info(f"[BadgeService] Granted '{badge_name}' to {member.id}")

        if role_setting_key:
            role_id = await settings_service.get_int(role_setting_key)
            if role_id:
                role = member.guild.get_role(role_id)
                if role and role not in member.roles:
                    try:
                        await member.add_roles(role, reason=f"Earned Badge: {badge_name}")
                    except Exception as e:
                        logger.error(f"Failed to grant role for {badge_name}: {e}")

    async def remove_badge(self, member: discord.Member, badge_name: str, role_setting_key: str = None):
        """Revokes a badge and optionally its mapped Discord Role."""
        badges = await self.get_badges(member.id)
        if badge_name in badges:
            badges.remove(badge_name)
            await db.execute("UPDATE users SET badges = %s WHERE user_id = %s", (json.dumps(badges), member.id))
            logger.info(f"[BadgeService] Revoked '{badge_name}' from {member.id}")

        if role_setting_key:
            role_id = await settings_service.get_int(role_setting_key)
            if role_id:
                role = member.guild.get_role(role_id)
                if role and role in member.roles:
                    try:
                        await member.remove_roles(role, reason=f"Lost Badge: {badge_name}")
                    except Exception as e:
                        logger.error(f"Failed to revoke role for {badge_name}: {e}")

    # --- Evaluators ---

    async def eval_twilight(self, member: discord.Member):
        row = await db.fetch_one("SELECT consecutive_active_days FROM users WHERE user_id = %s", (member.id,))
        if row and row.get('consecutive_active_days', 0) >= 3:
            await self.add_badge(member, "Twilight Pilgrim", "badge_role_twilight")

    async def eval_first_people(self, member: discord.Member):
        if member.joined_at and member.guild.created_at:
            delta = member.joined_at - member.guild.created_at
            if delta.days <= 30:
                await self.add_badge(member, "The First People", "badge_role_first_people")

    async def eval_sage(self, member: discord.Member):
        row = await db.fetch_one("SELECT thanks_received FROM users WHERE user_id = %s", (member.id,))
        if row and row.get('thanks_received', 0) >= 25:
            await self.add_badge(member, "Moniyan Sage", "badge_role_sage")

    async def eval_battlefield(self, member: discord.Member):
        row = await db.fetch_one("SELECT COUNT(DISTINCT event_id) as total FROM guild_event_rewards WHERE user_id = %s", (member.id,))
        if row and row.get('total', 0) >= 50:
            await self.add_badge(member, "Battlefield God", "badge_role_battlefield")

    async def eval_mogul(self, member: discord.Member):
        row = await db.fetch_one("SELECT lifetime_tokens FROM users WHERE user_id = %s", (member.id,))
        if row and row.get('lifetime_tokens', 0) >= 50000:
            await self.add_badge(member, "Mogul of the Land", "badge_role_mogul")

    async def eval_convivialist(self, member: discord.Member, force_revocation: bool = False):
        row = await db.fetch_one("SELECT consecutive_events_attended FROM users WHERE user_id = %s", (member.id,))
        streak = row.get('consecutive_events_attended', 0) if row else 0
        if streak >= 10:
            await self.add_badge(member, "Convivialist", "badge_role_convivialist")
        elif force_revocation and streak < 10:
            await self.remove_badge(member, "Convivialist", "badge_role_convivialist")
            
    async def eval_all(self, member: discord.Member):
        """Force-evaluates all metric checking at once."""
        await self.eval_twilight(member)
        await self.eval_first_people(member)
        await self.eval_sage(member)
        await self.eval_battlefield(member)
        await self.eval_mogul(member)
        await self.eval_convivialist(member)

badge_service = BadgeService()
