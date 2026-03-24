import discord
from discord.ext import commands
from discord import app_commands
import time
from utils.checks import ACTIVE_ADMIN_SESSIONS, ADMIN_PASSWORD, SESSION_DURATION_MINUTES

class AuthModal(discord.ui.Modal, title="Admin Authentication"):
    password_input = discord.ui.TextInput(
        label="Master Password",
        style=discord.TextStyle.short,
        placeholder="Enter the master password...",
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        if self.password_input.value == ADMIN_PASSWORD:
            # Grant session
            expire_time = time.time() + (SESSION_DURATION_MINUTES * 60)
            ACTIVE_ADMIN_SESSIONS[interaction.user.id] = expire_time
            
            embed = discord.Embed(
                title="✅ Authentication Successful",
                description=f"Your admin session is now active for **{SESSION_DURATION_MINUTES} minutes**.",
                color=discord.Color.green()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            embed = discord.Embed(
                title="❌ Authentication Failed",
                description="Incorrect password.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)


class AuthCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    admin_group = app_commands.Group(
        name="admin", 
        description="Global Admin Security", 
        default_permissions=discord.Permissions(administrator=True)
    )

    @admin_group.command(name="auth", description="Authenticate to unlock heavy admin commands")
    async def admin_auth(self, inter: discord.Interaction):
        """Open the password modal to grant an admin session."""
        await inter.response.send_modal(AuthModal())
        
    @admin_group.command(name="logout", description="End your active admin session immediately")
    async def admin_logout(self, inter: discord.Interaction):
        """Revoke the current admin session."""
        if inter.user.id in ACTIVE_ADMIN_SESSIONS:
            del ACTIVE_ADMIN_SESSIONS[inter.user.id]
            await inter.response.send_message("🔒 Your admin session has been securely closed.", ephemeral=True)
        else:
            await inter.response.send_message("ℹ️ You do not have an active session.", ephemeral=True)

async def setup(bot):
    await bot.add_cog(AuthCog(bot))
