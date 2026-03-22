"""
Event Tracker Cog - Manages event points, interactive claiming kiosks, and event leaderboards.
Features persistent UI components to survive bot restarts.
"""

import discord
from discord.ext import commands
from discord import app_commands
import logging

from services.database import db

logger = logging.getLogger("mlbb_bot.event_cog")

class PersistentEventView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        
    @discord.ui.button(label="Claim Rewards", style=discord.ButtonStyle.success, custom_id="persistent_event_claim_btn", emoji="🎉")
    async def claim_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id
        message_id = interaction.message.id
        
        # 1. Fetch Kiosk Data
        kiosk = await db.fetch_one("SELECT ep_amount FROM event_kiosks WHERE message_id = %s", (message_id,))
        if not kiosk:
            return await interaction.response.send_message("❌ This event kiosk is inactive or expired.", ephemeral=True)
            
        ep_amount = kiosk['ep_amount']
        
        # 2. Check Event Claims
        claimed = await db.fetch_one("SELECT * FROM event_claims WHERE message_id = %s AND user_id = %s", (message_id, user_id))
        if claimed:
            return await interaction.response.send_message("❌ You have already claimed these Event Points!", ephemeral=True)
            
        # 3. Grant EP!
        try:
            await db.execute("INSERT INTO event_claims (message_id, user_id) VALUES (%s, %s)", (message_id, user_id))
            await db.execute("""
                INSERT INTO users (user_id, xp, tokens, event_points) 
                VALUES (%s, 0, 0, %s)
                ON DUPLICATE KEY UPDATE event_points = event_points + VALUES(event_points)
            """, (user_id, ep_amount))
            await interaction.response.send_message(f"✅ Successfully claimed **{ep_amount} EP**! Thank you for participating!", ephemeral=True)
        except Exception as e:
            logger.error(f"Failed to process EP claim: {e}")
            await interaction.response.send_message("❌ An error occurred processing your claim. Please contact an admin.", ephemeral=True)

class EventCog(commands.GroupCog, name="event"):
    """Event Points ecosystem."""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        
    async def cog_load(self):
        # Register the persistent view to survive bot restarts
        self.bot.add_view(PersistentEventView())
        
    @app_commands.command(name="post", description="Post an interactive Event Claim button.")
    @app_commands.default_permissions(administrator=True)
    async def event_post(self, interaction: discord.Interaction, title: str, ep: int, description: str = "Click the button below to claim your Event Points!"):
        """Create a new event rewards kiosk."""
        embed = discord.Embed(
            title=f"🎉 Event: {title}",
            description=f"{description}\n\n**Reward:** 🏆 `{ep} EP`",
            color=discord.Color.brand_green(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="Community Events System")
        
        view = PersistentEventView()
        await interaction.response.send_message(embed=embed, view=view)
        
        # Grab the message that was just sent by pulling original response
        msg = await interaction.original_response()
        
        # Save kiosk config to DB
        await db.execute("INSERT INTO event_kiosks (message_id, ep_amount) VALUES (%s, %s)", (msg.id, ep))

    @app_commands.command(name="leaderboard", description="Show the Top 10 most active Event attendees!")
    async def event_leaderboard(self, interaction: discord.Interaction):
        """Displays the top 10 ranked users by event points."""
        top_users = await db.fetch_all("""
            SELECT user_id, event_points 
            FROM users 
            WHERE event_points > 0 
            ORDER BY event_points DESC 
            LIMIT 10
        """)
        
        if not top_users:
            return await interaction.response.send_message("No one has earned any Event Points yet! Events haven't started.", ephemeral=True)
            
        embed = discord.Embed(
            title="🏆 Event Participation Leaderboard",
            description="The most dedicated community event attendees in the server!",
            color=discord.Color.gold(),
            timestamp=discord.utils.utcnow()
        )
        
        lines = []
        for i, u in enumerate(top_users, 1):
            emoji = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else "🏅"
            lines.append(f"**{i}.** {emoji} <@{u['user_id']}> — **{u['event_points']} EP**")
            
        embed.add_field(name="Top 10 Attendees", value="\n".join(lines), inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="profile", description="Check how many Event Points you have.")
    async def event_profile(self, interaction: discord.Interaction, user: discord.Member = None):
        """View your or someone else's current EP and Leaderboard Rank."""
        target = user or interaction.user
        
        user_data = await db.fetch_one("SELECT event_points FROM users WHERE user_id = %s", (target.id,))
        ep = user_data['event_points'] if user_data else 0
        
        # Calculate their rank position
        rank_data = await db.fetch_one("""
            SELECT COUNT(*) as pos 
            FROM users 
            WHERE event_points > (SELECT event_points FROM users WHERE user_id = %s)
        """, (target.id,))
        rank = (rank_data['pos'] + 1) if rank_data and ep > 0 else "Unranked"
        
        embed = discord.Embed(
            title=f"🎟️ {target.display_name}'s Event Profile",
            color=discord.Color.blurple()
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="Total Event Points", value=f"**{ep} EP**", inline=True)
        embed.add_field(name="Server Ranking", value=f"**#{rank}**", inline=True)
        
        await interaction.response.send_message(embed=embed)

async def setup(bot: commands.Bot):
    await bot.add_cog(EventCog(bot))
