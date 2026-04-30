"""
Service to automatically evaluate and assign Discord roles based on
the number of giveaways (raffles) a user has hosted.
Uses a replacement strategy where only the highest milestone role is kept.
"""

import discord
import logging
import asyncio
from services.database import db
from services.settings_service import settings_service

logger = logging.getLogger("mlbb_bot.giveaway_milestones")

class GiveawayMilestoneService:
    MILESTONES = [5, 10, 20, 50, 100]

    async def evaluate_milestones(self, guild: discord.Guild, user_id: int) -> bool:
        """
        Evaluate and update a user's giveaway milestone role.
        Assigns the highest applicable role and strips any lower ones.
        Returns True if any role changes were made, False otherwise.
        """
        if not guild:
            return False

        member = guild.get_member(user_id)
        if not member:
            return False

        # 1. Count hosted raffles (excluding cancelled)
        # COALESCE prioritizes hosted_by (community host), fallback to host_id (creator)
        row = await db.fetch_one('''
            SELECT COUNT(*) as c 
            FROM event_raffles
            WHERE COALESCE(hosted_by, host_id) = %s AND status != 'cancelled'
        ''', (user_id,))
        
        count = row['c'] if row else 0

        # 2. Determine highest qualifying milestone
        highest_milestone = None
        for milestone in reversed(self.MILESTONES):
            if count >= milestone:
                highest_milestone = milestone
                break

        # 3. Resolve milestone roles from settings
        role_map = {}
        for m in self.MILESTONES:
            role_id = await settings_service.get_int(f"giveaway_milestone_{m}")
            if role_id:
                role = guild.get_role(role_id)
                if role:
                    role_map[m] = role

        if not role_map:
            # Roles aren't configured yet, silently skip
            return False

        # 4. Compute roles to add and remove
        correct_role = role_map.get(highest_milestone) if highest_milestone else None
        
        # All milestone roles the member currently has
        current_milestone_roles = {r for m, r in role_map.items() if r in member.roles}
        
        # Target state: only the correct_role (or empty set if no milestone)
        target_roles = {correct_role} if correct_role else set()
        
        roles_to_remove = current_milestone_roles - target_roles
        roles_to_add = target_roles - current_milestone_roles

        # Short-circuit: nothing to change
        if not roles_to_remove and not roles_to_add:
            return False

        # 5. Execute role changes
        try:
            if roles_to_remove:
                await member.remove_roles(*roles_to_remove, reason=f"Giveaway Milestone Eval: {count} hosted")
            
            if roles_to_add:
                await member.add_roles(*roles_to_add, reason=f"Giveaway Milestone: {highest_milestone}+ hosted")
            
            return True
                
        except discord.Forbidden:
            logger.warning(f"Missing permissions to manage giveaway milestone roles for {user_id}")
        except discord.HTTPException as e:
            logger.error(f"HTTP Error updating giveaway milestone roles for {user_id}: {e}")
        
        return False

    async def backfill_all(self, guild: discord.Guild) -> dict:
        """
        Retroactively scan all historical raffles and assign milestone roles.
        Respects Discord rate limits by sleeping between member updates.
        Returns a dict with execution stats.
        """
        stats = {"updated": 0, "skipped": 0, "errors": 0}
        if not guild:
            return stats

        # Fetch all qualifying hosts (>= 5 hosted raffles)
        rows = await db.fetch_all('''
            SELECT COALESCE(hosted_by, host_id) as effective_host, COUNT(*) as hosted_count
            FROM event_raffles
            WHERE status != 'cancelled'
            GROUP BY effective_host
            HAVING hosted_count >= 5
        ''')

        if not rows:
            return stats

        for row in rows:
            user_id = row['effective_host']
            if not user_id:
                continue

            member = guild.get_member(user_id)
            if not member:
                stats["skipped"] += 1
                continue

            try:
                changed = await self.evaluate_milestones(guild, user_id)
                if changed:
                    stats["updated"] += 1
                else:
                    stats["skipped"] += 1
            except Exception as e:
                logger.error(f"Error backfilling milestone for {user_id}: {e}")
                stats["errors"] += 1

            # Sleep to respect Discord's rate limits (10 role updates / 10s / guild)
            await asyncio.sleep(1)

        return stats

giveaway_milestone_service = GiveawayMilestoneService()
