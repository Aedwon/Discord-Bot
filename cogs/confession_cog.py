"""
Confessions Cog - Anonymous confessions system.
Users submit confessions via /confess, which are posted anonymously
to a configured confessions channel. Only verified users may confess.
Confession identity is logged in the command log channel (via main.py on_interaction).
"""

import discord
from discord.ext import commands
from discord import app_commands
import datetime
import logging

from services.settings_service import settings_service
from services.verification_service import verification_service

logger = logging.getLogger("mlbb_bot.confessions")

# In-memory confession counter per guild (resets on bot restart, purely cosmetic)
_confession_counters: dict[int, int] = {}


class ConfessionModal(discord.ui.Modal, title="📝 Submit a Confession"):
    """Modal for submitting an anonymous confession."""

    confession = discord.ui.TextInput(
        label="Your Confession",
        style=discord.TextStyle.paragraph,
        placeholder="Write your anonymous confession here...",
        required=True,
        min_length=10,
        max_length=2000,
    )

    def __init__(self, channel: discord.TextChannel):
        super().__init__()
        self.target_channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        text = self.confession.value.strip()

        # Guard: whitespace-only or too short after stripping
        if len(text) < 10:
            return await interaction.response.send_message(
                "❌ Your confession is too short (minimum 10 characters after trimming whitespace).",
                ephemeral=True,
            )

        # Increment confession counter for this guild
        guild_id = interaction.guild_id
        _confession_counters[guild_id] = _confession_counters.get(guild_id, 0) + 1
        confession_number = _confession_counters[guild_id]

        # Build the anonymous embed
        embed = discord.Embed(
            title=f"Confession #{confession_number}",
            description=text,
            color=discord.Color.dark_grey(),
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        guild_icon = interaction.guild.icon.url if interaction.guild.icon else None
        embed.set_author(name="Anonymous Confession", icon_url=guild_icon)
        embed.set_footer(text="Use /confess to submit your own")

        try:
            await self.target_channel.send(embed=embed)
        except discord.Forbidden:
            # Roll back counter on failure
            _confession_counters[guild_id] -= 1
            return await interaction.response.send_message(
                "❌ I don't have permission to send messages in the confessions channel. "
                "Please notify an admin.",
                ephemeral=True,
            )
        except discord.HTTPException as e:
            _confession_counters[guild_id] -= 1
            logger.error(f"Failed to post confession #{confession_number}: {e}")
            return await interaction.response.send_message(
                "❌ Something went wrong while posting your confession. Please try again later.",
                ephemeral=True,
            )

        await interaction.response.send_message(
            f"✅ Your confession (#{confession_number}) has been posted anonymously!",
            ephemeral=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        logger.error(f"Confession modal error for user {interaction.user.id}: {error}")
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ An unexpected error occurred. Please try again later.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    "❌ An unexpected error occurred. Please try again later.",
                    ephemeral=True,
                )
        except discord.HTTPException:
            pass


class ConfessionsCog(commands.Cog, name="Confessions"):
    """Anonymous confessions system."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="confess", description="Submit an anonymous confession")
    @app_commands.guild_only()
    async def confess(self, interaction: discord.Interaction):
        """Open the confession modal. Requires verification."""

        # Verification gate
        if not verification_service.is_verified(interaction.user.id):
            return await interaction.response.send_message(
                "❌ You must be verified to use confessions. "
                "Please verify your MLBB account first.",
                ephemeral=True,
            )

        # Check that the confessions channel is configured
        channel_id = await settings_service.get_int("confessions_channel_id")
        if not channel_id:
            return await interaction.response.send_message(
                "❌ Confessions channel has not been set up yet. "
                "Ask an admin to configure it with `/setup channel confessions`.",
                ephemeral=True,
            )

        channel = interaction.guild.get_channel(channel_id)
        if not channel:
            return await interaction.response.send_message(
                "❌ The configured confessions channel no longer exists. "
                "Ask an admin to reconfigure it.",
                ephemeral=True,
            )

        # Verify bot can send in that channel
        bot_member = interaction.guild.me
        permissions = channel.permissions_for(bot_member)
        if not permissions.send_messages or not permissions.embed_links:
            return await interaction.response.send_message(
                "❌ I don't have permission to send embeds in the confessions channel. "
                "Please notify an admin.",
                ephemeral=True,
            )

        await interaction.response.send_modal(ConfessionModal(channel))


async def setup(bot: commands.Bot):
    await bot.add_cog(ConfessionsCog(bot))
