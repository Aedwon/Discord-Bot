"""
Universal Centralized Leaderboard Cog.
Manages all active server ranking systems in one polished dashboard channel.
Updates dynamically every 5 minutes to bypass rate-limits and heavily optimize performance.
"""

import discord
from discord.ext import commands, tasks
import logging
import time

from services.database import db
from services.settings_service import settings_service
from services.xp_service import xp_service
from services.ep_service import ep_service

logger = logging.getLogger("mlbb_bot.leaderboard")

class LeaderboardCog(commands.Cog, name="leaderboards"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def cog_unload(self):
        self.update_leaderboards.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.update_leaderboards.is_running():
            self.update_leaderboards.start()

    @tasks.loop(minutes=5)
    async def update_leaderboards(self):
        """Standard industry 5-minute background loop to continually overwrite the static leaderboard UX."""
        
        try:
            for guild in self.bot.guilds:
                # 1. Check if the Dashboard Channel is configured
                channel_id_str = await settings_service.get("leaderboard_channel_id")
                if not channel_id_str: continue
                    
                channel = guild.get_channel(int(channel_id_str))
                if not channel: continue
                
                # 2. Generate the dynamic UX embeds
                exp_embed = await self.generate_exp_leaderboard()
                ep_embed = await self.generate_event_leaderboard()
                quiz_embed = await self.generate_quiz_leaderboard()
                embeds = [exp_embed, ep_embed, quiz_embed]
                
                # 3. Pull the physical Message ID
                msg_id_str = await settings_service.get(f"leaderboard_msg_{guild.id}")
                
                if msg_id_str:
                    try:
                        msg = await channel.fetch_message(int(msg_id_str))
                        await msg.edit(embeds=embeds)
                        continue # Successfully Updated!
                    except discord.NotFound:
                        pass # Message was accidentally deleted by an admin, so we will generate a new one cleanly.
                    except discord.HTTPException as e:
                        logger.error(f"Leaderboard Edit Fast-Fail: {e}")
                        continue
                        
                # 4. If no message exists, inject a new one.
                new_msg = await channel.send(embeds=embeds)
                await settings_service.set(f"leaderboard_msg_{guild.id}", str(new_msg.id))
                
        except Exception as e:
            logger.error(f"Fatal error in 5-minute Leaderboard Loop: {e}")
            
    async def generate_exp_leaderboard(self) -> discord.Embed:
        top_xp = await db.fetch_all("SELECT user_id, xp FROM users WHERE xp > 0 ORDER BY xp DESC LIMIT 10")
        embed = discord.Embed(title="🌟 Hall of Fame: Experience", color=discord.Color.blue(), timestamp=discord.utils.utcnow())
        
        next_update = int(time.time() + 300)
        footer_text = f"\n\n*Next update:* <t:{next_update}:R>"
        
        if not top_xp:
            embed.description = "The server is quiet... no one has earned any XP yet." + footer_text
            return embed
            
        lines = []
        for i, u in enumerate(top_xp, 1):
            emoji = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else "🏅"
            level = xp_service.get_level(u['xp'])
            tier = xp_service.get_tier_name(level)
            lines.append(f"**{i}.** {emoji} <@{u['user_id']}> — **{u['xp']} XP** | Lv. {level} ({tier})")
            
        embed.description = "\n".join(lines) + footer_text
        return embed

    async def generate_event_leaderboard(self) -> discord.Embed:
        query = '''
            SELECT u.user_id, u.event_points, 
                   (SELECT COUNT(*) FROM event_redemptions e WHERE e.user_id = u.user_id) as total_events
            FROM users u 
            WHERE u.event_points > 0 
            ORDER BY u.event_points DESC 
            LIMIT 10
        '''
        top_ep = await db.fetch_all(query)
        embed = discord.Embed(title="🏆 Hall of Fame: Event Points", color=discord.Color.gold(), timestamp=discord.utils.utcnow())
        
        next_update = int(time.time() + 300)
        footer_text = f"\n\n*Next update:* <t:{next_update}:R>"
        
        if not top_ep:
            embed.description = "The event stands are empty... no Event Points have been formally distributed." + footer_text
            return embed
            
        lines = []
        for i, u in enumerate(top_ep, 1):
            emoji = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else "🏅"
            events = u['total_events'] or 0
            ep = u['event_points']
            role_name = ep_service.get_sub_tier(ep)
            lines.append(f"**{i}.** {emoji} <@{u['user_id']}> — **{ep} EP** | {events} Events ({role_name})")
            
        embed.description = "\n".join(lines) + footer_text
        return embed

    async def generate_quiz_leaderboard(self) -> discord.Embed:
        query = '''
            SELECT user_id, SUM(score) as total_score
            FROM quiz_history
            WHERE earned_at >= DATE_SUB(NOW(), INTERVAL 7 DAY)
            GROUP BY user_id
            ORDER BY total_score DESC
            LIMIT 10
        '''
        top_quiz = await db.fetch_all(query)
        embed = discord.Embed(title="🧠 Weekly Quiz Champions", color=discord.Color.purple(), timestamp=discord.utils.utcnow())
        
        next_update = int(time.time() + 300)
        footer_text = f"\n\n_Past 7 Days Highlights_ • *Next update:* <t:{next_update}:R>"
        
        if not top_quiz:
            embed.description = "No quiz scores recorded in the last 7 days. Be the first to answer correctly!" + footer_text
            return embed
            
        lines = []
        for i, u in enumerate(top_quiz, 1):
            emoji = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else "🏅"
            lines.append(f"**{i}.** {emoji} <@{u['user_id']}> — **{u['total_score']} pts**")
            
        embed.description = "\n".join(lines) + footer_text
        return embed

async def setup(bot: commands.Bot):
    await bot.add_cog(LeaderboardCog(bot))
