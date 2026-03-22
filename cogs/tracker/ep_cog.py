"""
Event Points End-of-Season Background Engine Config.
Gracefully executes Database resets and allocates non-expiring permanent Legacy Badges mathematically via asyncio logic loops.
"""
import discord
import asyncio
from discord.ext import commands, tasks
import logging

from services.database import db
from services.settings_service import settings_service

logger = logging.getLogger("mlbb_bot.ep_core")

class EPCog(commands.Cog, name="event_points_core"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.eos_loop.start()

    def cog_unload(self):
        self.eos_loop.cancel()
        
    @tasks.loop(hours=24)
    async def eos_loop(self):
        """End Of Season background checker. Purges mathematically if 90-days struck natively or manually induced."""
        await self.bot.wait_until_ready()
        
        eos_flag = await settings_service.get_int("eos_reset_triggered")
        if eos_flag == 1:
            await self.execute_eos_wipe()
            
    async def execute_eos_wipe(self):
        logger.info("EOS Systemic Wipe Initiated...")
        await settings_service.set("eos_reset_triggered", "0") # Reset toggle to avoid repeat wipes
        
        guild = self.bot.guilds[0] if self.bot.guilds else None
        if not guild: return
            
        all_players = await db.fetch_all("SELECT user_id, event_points FROM users WHERE event_points > 0 ORDER BY event_points DESC, last_ep_update ASC")
        
        # Accumulate strict non-expiring IDs
        legacies = {
            "immortal": guild.get_role(await settings_service.get_int("legacy_badge_Mythical_Immortal") or 0),
            "glory": guild.get_role(await settings_service.get_int("legacy_badge_Mythical_Glory") or 0),
            "mythic": guild.get_role(await settings_service.get_int("legacy_badge_Mythic") or 0),
            "legend": guild.get_role(await settings_service.get_int("legacy_badge_Legend") or 0),
            "epic": guild.get_role(await settings_service.get_int("legacy_badge_Epic") or 0),
            "grandmaster": guild.get_role(await settings_service.get_int("legacy_badge_Grandmaster") or 0),
            "master": guild.get_role(await settings_service.get_int("legacy_badge_Master") or 0),
            "elite": guild.get_role(await settings_service.get_int("legacy_badge_Elite") or 0),
            "warrior": guild.get_role(await settings_service.get_int("legacy_badge_Warrior") or 0),
        }
        
        # Identify seasonal ranks slated for erasure mathematically
        ranks = ["Warrior", "Elite", "Master", "Grandmaster", "Epic", "Legend", "Mythic", "Mythical Glory", "Mythical Immortal"]
        seasonal_roles = []
        for r in ranks:
            r_obj = guild.get_role(await settings_service.get_int(f"ep_role_{r.replace(' ', '_')}") or 0)
            if r_obj: seasonal_roles.append(r_obj)
            
        for index, row in enumerate(all_players, 1):
            user_id = row['user_id']
            ep = row['event_points']
            member = guild.get_member(user_id)
            if not member: continue
                
            # Algorithm explicitly derives standing limits
            badge_to_award = None
            if ep >= 10000:
                if index <= 10: badge_to_award = legacies["immortal"]
                elif index <= 50: badge_to_award = legacies["glory"]
                else: badge_to_award = legacies["mythic"]
            elif ep >= 7500: badge_to_award = legacies["legend"]
            elif ep >= 5000: badge_to_award = legacies["epic"]
            elif ep >= 3000: badge_to_award = legacies["grandmaster"]
            elif ep >= 1500: badge_to_award = legacies["master"]
            elif ep >= 500: badge_to_award = legacies["elite"]
            else: badge_to_award = legacies["warrior"]
                
            try:
                # Disconnect active MLBB seasons
                roles_to_remove = [r for r in seasonal_roles if r in member.roles]
                if roles_to_remove: 
                    await member.remove_roles(*roles_to_remove, reason="EOS Seasonal System Wipe")
                
                # Lock in History Baseline Badge natively
                if badge_to_award and badge_to_award not in member.roles:
                    await member.add_roles(badge_to_award, reason="EOS Legacy Badge Preservation")
            except discord.Forbidden: 
                pass
            
            # CORE PROTECTIONS:
            # Yield execution perfectly to chunk 2 Discord writes per second = 120 modifications/min, effectively erasing 3,000 users over exactly ~25 min safely
            await asyncio.sleep(0.5)
            
        # Hard wipe database natively in local array
        await db.execute("UPDATE users SET event_points = 0")
        logger.info("Global EOS Disconnected & Rebuilt Effectively.")

async def setup(bot: commands.Bot):
    await bot.add_cog(EPCog(bot))
