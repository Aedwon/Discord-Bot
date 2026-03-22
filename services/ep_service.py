"""
Event Points (EP) Service.
Handles MLBB-style tier calculations, dynamic Top-50 assignments, and Database interactions.
Strictly relies on the `event_points` and `last_ep_update` tie-breaker columns natively.
"""

from services.database import db
from services.settings_service import settings_service
import discord
import logging

logger = logging.getLogger("mlbb_bot.ep_service")

class EPService:
    def get_base_tier(self, ep: int) -> str:
        """Calculate the standard MLBB rank precisely based on EP progression limits."""
        if ep < 500: return "Warrior"
        if ep < 1500: return "Elite"
        if ep < 3000: return "Master"
        if ep < 5000: return "Grandmaster"
        if ep < 7500: return "Epic"
        if ep < 10000: return "Legend"
        return "Mythic" # The 10,000 EP Hard Gate
        
    async def process_ep_update(self, guild: discord.Guild, user_id: int, ep_change: int) -> int:
        """
        Calculates EP transactions explicitly, ensuring database integrity and tie-breaker timestamps.
        Runs purely synchronously, delegating UI assignments safely.
        """
        if not guild: return 0
        
        # 1. Update EP securely and force a timestamp reset for accurate Mythic Tie-Breakers.
        # This handles negative ep_changes intrinsically mathematically via GREATEST(0, ...).
        await db.execute('''
            INSERT INTO users (user_id, event_points) 
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE 
                event_points = GREATEST(0, event_points + VALUES(event_points)),
                last_ep_update = CURRENT_TIMESTAMP
        ''', (user_id, ep_change))
        
        row = await db.fetch_one("SELECT event_points FROM users WHERE user_id = %s", (user_id,))
        new_ep = row['event_points'] if row else 0
        
        # 2. Derive Standard Limit Threshold Tier
        new_tier = self.get_base_tier(new_ep)
        
        # 3. Retrieve their outdated / mapped physical roles
        member = guild.get_member(user_id)
        current_held_tier = None
        
        if member:
            all_ep_roles = []
            ranks = ["Warrior", "Elite", "Master", "Grandmaster", "Epic", "Legend", "Mythic", "Mythical Glory", "Mythical Immortal"]
            
            for r in ranks:
                role_id = await settings_service.get_int(f"ep_role_{r.replace(' ', '_')}")
                if role_id:
                    role_obj = guild.get_role(role_id)
                    if role_obj:
                        all_ep_roles.append(role_obj)
                        if role_obj in member.roles:
                            current_held_tier = r
            
            # Sub-10,000 Calculation:
            # If they dropped down securely or climbed conventionally out of Mythic boundaries
            if new_ep < 10000 and current_held_tier != new_tier:
                new_role_id = await settings_service.get_int(f"ep_role_{new_tier.replace(' ', '_')}")
                if new_role_id:
                    new_role_obj = guild.get_role(new_role_id)
                    if new_role_obj:
                        try:
                            await member.remove_roles(*all_ep_roles, reason="MLBB EP Core Shift")
                            await member.add_roles(new_role_obj, reason="MLBB EP Core Shift")
                        except discord.Forbidden:
                            pass
                            
        # 4. Critical Edge Case Verification: Top 50 Shift
        # A massive paradigm change is mathematically enforced here. If a player crossed 10K,
        # OR dropped below 10K losing their Mythic status, the ENTIRE Top-50 ladder shifts natively!
        if new_ep >= 10000 or (new_ep < 10000 and current_held_tier in ["Mythical Glory", "Mythical Immortal", "Mythic"]):
            await self.recalculate_mythic_roles(guild)
            
        return new_ep
        
    async def recalculate_mythic_roles(self, guild: discord.Guild):
        """
        Actively re-evaluates the Top 50 EP leaders safely.
        By querying via `<EP> DESC, <TIME> ASC`, we seamlessly settle literal mathematical EP ties logically.
        Forces the Immortal (1-10) and Glory (11-50) assignments locally.
        """
        top_players = await db.fetch_all('''
            SELECT user_id, event_points 
            FROM users 
            WHERE event_points >= 10000 
            ORDER BY event_points DESC, last_ep_update ASC
        ''')
        
        if not top_players: return # None exist beyond the gate securely
        
        imm_id = await settings_service.get_int("ep_role_Mythical_Immortal")
        glo_id = await settings_service.get_int("ep_role_Mythical_Glory")
        myt_id = await settings_service.get_int("ep_role_Mythic")
        
        imm_role = guild.get_role(imm_id) if imm_id else None
        glo_role = guild.get_role(glo_id) if glo_id else None
        myt_role = guild.get_role(myt_id) if myt_id else None
        
        for index, row in enumerate(top_players, 1):
            user_id = row['user_id']
            member = guild.get_member(user_id)
            if not member: continue
                
            # Distribute strictly to constraints natively
            if index <= 10:
                correct_role = imm_role
            elif index <= 50:
                correct_role = glo_role
            else:
                correct_role = myt_role # Booted entirely out of Top 50 globally.
                
            if correct_role and correct_role not in member.roles:
                try:
                    roles_to_strip = [r for r in [imm_role, glo_role, myt_role] if r and r in member.roles and r != correct_role]
                    if roles_to_strip:
                        await member.remove_roles(*roles_to_strip, reason="Top 50 Ladder Tie-Break Shift (Lost Ranking)")
                    await member.add_roles(correct_role, reason="Top 50 Ladder Allocation (Secured Board Placement)")
                except discord.Forbidden:
                    logger.error(f"Cannot secure Mythic Top-50 roles for mathematically qualified {user_id}")

# Core API Export 
ep_service = EPService()
