import discord
from discord.ext import commands, tasks
from discord import app_commands
import logging
from datetime import datetime, timedelta
import pytz

from services.database import db
from services.settings_service import settings_service
from services.xp_service import xp_service
from services.badge_service import badge_service

logger = logging.getLogger("mlbb_bot.social_cog")
TZ_MANILA = pytz.timezone("Asia/Manila")

class SocialCog(commands.GroupCog, name="social"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.weekly_contributor_loop.start()

    def cog_unload(self):
        self.weekly_contributor_loop.cancel()
        
    @app_commands.command(name="thank", description="Thank someone for their help and award them 10 XP!")
    async def thank_user(self, interaction: discord.Interaction, user: discord.Member, reason: str):
        # Prevent self
        if user.id == interaction.user.id:
            return await interaction.response.send_message("❌ You cannot thank yourself!", ephemeral=True)
            
        # Check 24H cooldown for sender
        recent_sends = await db.fetch_all("SELECT created_at FROM thanks_history WHERE sender_id = %s ORDER BY created_at DESC LIMIT 1", (interaction.user.id,))
        if recent_sends:
            last_sent = recent_sends[0]['created_at']
            if (datetime.now() - last_sent).total_seconds() < 86400:
                return await interaction.response.send_message("❌ You can only use the `/social thank` command once every 24 hours.", ephemeral=True)
                
        # Check 7 Day cooldown for target
        target_sends = await db.fetch_all("SELECT created_at FROM thanks_history WHERE sender_id = %s AND receiver_id = %s ORDER BY created_at DESC LIMIT 1", (interaction.user.id, user.id))
        if target_sends:
            last_target = target_sends[0]['created_at']
            if (datetime.now() - last_target).total_seconds() < 604800:
                return await interaction.response.send_message(f"❌ You cannot thank {user.mention} again so soon! You must wait 7 days between thanking the same person.", ephemeral=True)

        # Log to thanks_history
        await db.execute("INSERT INTO thanks_history (sender_id, receiver_id, reason) VALUES (%s, %s, %s)", (interaction.user.id, user.id, reason))
        
        # Award 10 XP and increment overall count natively
        await db.execute('''
            INSERT INTO users (user_id, xp, thanks_received) VALUES (%s, %s, 1)
            ON DUPLICATE KEY UPDATE xp = xp + 10, thanks_received = IFNULL(thanks_received, 0) + 1
        ''', (user.id, 10))
        
        # Badge Evaluation (Moniyan Sage requires 25 thanks)
        await badge_service.eval_sage(user)
        
        embed = discord.Embed(
            title="💖 Appreciation Sent!",
            description=f"{interaction.user.mention} thanked {user.mention} for:\n> *\"{reason}\"*",
            color=discord.Color.from_rgb(255, 105, 180)
        )
        embed.set_footer(text="The receiver has been mathematically awarded 10 XP!")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="bind-badges", description="[Admin] Bind Discord Roles to dynamic Badges")
    @app_commands.default_permissions(administrator=True)
    async def bind_badges(
        self, interaction: discord.Interaction, 
        twilight_pilgrim: discord.Role = None,
        first_people: discord.Role = None,
        sage: discord.Role = None,
        battlefield: discord.Role = None,
        mogul: discord.Role = None,
        convivialist: discord.Role = None,
        mentor: discord.Role = None
    ):
        settings = {
            "badge_role_twilight": str(twilight_pilgrim.id) if twilight_pilgrim else "0",
            "badge_role_first_people": str(first_people.id) if first_people else "0",
            "badge_role_sage": str(sage.id) if sage else "0",
            "badge_role_battlefield": str(battlefield.id) if battlefield else "0",
            "badge_role_mogul": str(mogul.id) if mogul else "0",
            "badge_role_convivialist": str(convivialist.id) if convivialist else "0",
            "role_id_mentor": str(mentor.id) if mentor else "0"
        }
        for k, v in settings.items():
            await db.execute("INSERT INTO server_settings (`key`, value) VALUES (%s, %s) ON DUPLICATE KEY UPDATE value = VALUES(value)", (k, v))
        await interaction.response.send_message("✅ Dynamic Badge Roles and Mentor configurations have been mapped locally to the backend system.", ephemeral=True)

    @tasks.loop(minutes=5)
    async def weekly_contributor_loop(self):
        """Runs every Sunday at 8 AM Manila Time to determine the Contributor of the Week."""
        now = datetime.now(TZ_MANILA)
        if now.weekday() == 6 and now.hour == 8 and 0 <= now.minute < 5:
            await self._run_weekly_contributor()
            
    @weekly_contributor_loop.before_loop
    async def before_weekly_contributor(self):
        await self.bot.wait_until_ready()

    async def _run_weekly_contributor(self):
        # 1. Fetch top receiver from the past 7 days
        target_span = datetime.now() - timedelta(days=7)
        rows = await db.fetch_all('''
            SELECT receiver_id, COUNT(*) as count 
            FROM thanks_history 
            WHERE created_at >= %s
            GROUP BY receiver_id 
            ORDER BY count DESC
        ''', (target_span,))
        
        if not rows: return
        
        max_count = rows[0]['count']
        winners = [r['receiver_id'] for r in rows if r['count'] == max_count]
        
        # 2. Assign Moniyan Mentor Role
        mentor_role_id = await settings_service.get_int("role_id_mentor")
        mentor_role = None
        
        guild = self.bot.guilds[0] if self.bot.guilds else None
        if not guild: return
        
        if mentor_role_id:
            mentor_role = guild.get_role(mentor_role_id)
            if mentor_role:
                # 3. Strip from previous week
                for member in mentor_role.members:
                    if member.id not in winners:
                        try: await member.remove_roles(mentor_role, reason="Weekly Contributor rotation")
                        except: pass
                
                # 4. Award to new winners
                for wid in winners:
                    try:
                        mem = guild.get_member(wid)
                        if mem: await mem.add_roles(mentor_role, reason="Contributor of the Week")
                    except: pass
                    
        # 5. Output announcement in public event log 
        out_channel_id = await settings_service.get_int("boost_public_channel_id")
        if out_channel_id:
            channel = guild.get_channel(out_channel_id)
            if channel:
                mentions = ", ".join([f"<@{w}>" for w in winners])
                embed = discord.Embed(
                    title="🏆 Contributors of the Week",
                    description=f"Congratulations to {mentions} for answering the most questions and helping the community this week! You have gained **{max_count} Thanks** and earned the **Moniyan Mentor** role for 7 days!",
                    color=discord.Color.gold()
                )
                await channel.send(embed=embed)

async def setup(bot: commands.Bot):
    await bot.add_cog(SocialCog(bot))
