import discord
from discord import app_commands
from discord.ext import commands
from services.database import db
from services.xp_service import xp_service
from services.badge_service import badge_service

class TestCog(commands.Cog, name="Test Commands"):
    """Admin commands exclusively for testing economy variables."""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    test_group = app_commands.Group(name="test", description="[Admin] Force update progression/variables", default_permissions=discord.Permissions(administrator=True))

    @test_group.command(name="add-xp", description="Force add XP to a user")
    async def add_xp(self, interaction: discord.Interaction, member: discord.Member, amount: int):
        await xp_service.add_xp(member.id, amount)
        await interaction.response.send_message(f"✅ Added {amount} XP to {member.mention}.", ephemeral=True)

    @test_group.command(name="add-ep", description="Force add Event Points")
    async def add_ep(self, interaction: discord.Interaction, member: discord.Member, amount: int):
        from services.ep_service import ep_service
        await ep_service.process_ep_update(interaction.guild, member.id, amount)
        await interaction.response.send_message(f"✅ Added {amount} EP to {member.mention}.", ephemeral=True)

    @test_group.command(name="add-tokens", description="Force add Economy Tokens (Triggers Mogul evaluation)")
    async def add_tokens(self, interaction: discord.Interaction, member: discord.Member, amount: int):
        await xp_service.award_currency(member.id, tokens=amount)
        await badge_service.eval_mogul(member)
        await interaction.response.send_message(f"✅ Added {amount} Tokens to {member.mention}.", ephemeral=True)

    @test_group.command(name="set-streak", description="Force set Daily Activity Streak (Triggers Twilight Pilgrim)")
    async def set_streak(self, interaction: discord.Interaction, member: discord.Member, days: int):
        await db.execute("INSERT INTO users (user_id) VALUES (%s) ON DUPLICATE KEY UPDATE user_id = VALUES(user_id)", (member.id,))
        await db.execute("UPDATE users SET consecutive_active_days = %s WHERE user_id = %s", (days, member.id))
        await badge_service.eval_twilight(member)
        await interaction.response.send_message(f"✅ Set Activity Streak for {member.mention} to {days} days.", ephemeral=True)

    @test_group.command(name="set-events", description="Force set Consecutive Events Attended (Triggers Convivialist)")
    async def set_events(self, interaction: discord.Interaction, member: discord.Member, events: int):
        await db.execute("INSERT INTO users (user_id) VALUES (%s) ON DUPLICATE KEY UPDATE user_id = VALUES(user_id)", (member.id,))
        await db.execute("UPDATE users SET consecutive_events_attended = %s WHERE user_id = %s", (events, member.id))
        await badge_service.eval_convivialist(member)
        await interaction.response.send_message(f"✅ Set consecutive events for {member.mention} to {events}.", ephemeral=True)

    @test_group.command(name="reset-user", description="Reset ALL economy/tracking stats for a user")
    async def reset_user(self, interaction: discord.Interaction, member: discord.Member):
        await db.execute("DELETE FROM thanks_history WHERE sender_id = %s OR receiver_id = %s", (member.id, member.id))
        await db.execute("DELETE FROM users WHERE user_id = %s", (member.id,))
        
        # Remove Discord Roles mathematically evaluated
        roles_to_strip = []
        for setting in ["badge_role_twilight", "badge_role_first_people", "badge_role_sage", "badge_role_battlefield", "badge_role_mogul", "badge_role_convivialist", "role_id_mentor"]:
            from services.settings_service import settings_service
            rid = await settings_service.get_int(setting)
            role = member.guild.get_role(rid) if rid else None
            if role and role in member.roles: roles_to_strip.append(role)
            
        if roles_to_strip:
            await member.remove_roles(*roles_to_strip, reason="Test Reset Profile")
            
        await interaction.response.send_message(f"✅ Completely wiped all economy and badge progression data (and roles) for {member.mention}.", ephemeral=True)

    @test_group.command(name="xp-debug", description="Checks internal XP state variables")
    async def xp_debug(self, interaction: discord.Interaction, member: discord.Member):
        from services.verification_service import verification_service
        from services.settings_service import settings_service
        
        xp_cog = interaction.client.get_cog("Leveling")
        
        is_verified = verification_service.is_verified(member.id)
        xp_enabled = await settings_service.get_int("xp_system_enabled") == 1
        bot_chan = await settings_service.get_int("bot_channel_id")
        
        if xp_cog:
            loop_running = xp_cog.batch_update_db.is_running()
            loop_failed = xp_cog.batch_update_db.failed()
            pending = xp_cog.pending_xp.get(member.id, "No pending XP")
            total_pending = len(xp_cog.pending_xp)
        else:
            loop_running = False
            loop_failed = False
            pending = "Cog Not Found"
            total_pending = 0
            
        msg = (
            f"**XP Diagnostics for {member.display_name}**\n"
            f"XP Enabled: `{xp_enabled}`\n"
            f"Verified Cache: `{is_verified}`\n"
            f"Loop Running: `{loop_running}`\n"
            f"Loop Failed: `{loop_failed}`\n"
            f"User Pending XP: `{pending}`\n"
            f"Total Queued Users: `{total_pending}`\n"
            f"Bot Channel ID: `{bot_chan}`"
        )
        await interaction.response.send_message(msg, ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(TestCog(bot))
