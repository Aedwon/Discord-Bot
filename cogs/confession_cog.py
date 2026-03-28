"""
Confessions Cog - Anonymous confessions via persistent button.
A "Confess" button is attached to the initial panel and to every
posted confession, so users can always click to submit a new one.
Only verified users may confess.
Confession identity is logged via the global command log (main.py on_interaction).
"""

import discord
from discord.ext import commands
from discord import app_commands
import datetime
import logging

from services.settings_service import settings_service
from services.verification_service import verification_service
from utils.anon_log import log_anonymous_action

logger = logging.getLogger("mlbb_bot.confessions")

# In-memory confession counter per guild (resets on bot restart, purely cosmetic)
_confession_counters: dict[int, int] = {}


# ─── Persistent View & Modal ────────────────────────────────────────


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
        embed.set_footer(text="Click the button below to submit your own")

        try:
            # Post the confession WITH the confess button attached
            await self.target_channel.send(embed=embed, view=ConfessButtonView())
        except discord.Forbidden:
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

        # Log to admin channel
        await log_anonymous_action(
            interaction.client,
            user=interaction.user,
            action_type="Confession",
            content=text,
            channel=self.target_channel,
            reference_label=f"Confession #{confession_number}",
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


class ConfessButtonView(discord.ui.View):
    """Persistent view with a single 'Confess' button. Survives bot restarts."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Confess",
        style=discord.ButtonStyle.secondary,
        emoji="✉️",
        custom_id="confessions:confess_button",
    )
    async def confess_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handle confess button click — validate, then open modal."""

        # Guild-only guard
        if not interaction.guild:
            return await interaction.response.send_message(
                "❌ This can only be used in a server.", ephemeral=True
            )

        # Verification gate
        if not verification_service.is_verified(interaction.user.id):
            return await interaction.response.send_message(
                "❌ You must be verified to use confessions. "
                "Please verify your MLBB account first.",
                ephemeral=True,
            )

        # Get the confessions channel
        channel_id = await settings_service.get_int("confessions_channel_id")
        if not channel_id:
            return await interaction.response.send_message(
                "❌ Confessions channel has not been configured. Ask an admin.",
                ephemeral=True,
            )

        channel = interaction.guild.get_channel(channel_id)
        if not channel:
            return await interaction.response.send_message(
                "❌ The configured confessions channel no longer exists. Ask an admin.",
                ephemeral=True,
            )

        # Permission pre-check
        bot_member = interaction.guild.me
        permissions = channel.permissions_for(bot_member)
        if not permissions.send_messages or not permissions.embed_links:
            return await interaction.response.send_message(
                "❌ I don't have permission to send embeds in the confessions channel. "
                "Please notify an admin.",
                ephemeral=True,
            )

        await interaction.response.send_modal(ConfessionModal(channel))


# ─── Cog ─────────────────────────────────────────────────────────────


class ConfessionsCog(commands.Cog, name="Confessions"):
    """Anonymous confessions system via persistent button."""

    confess_group = app_commands.Group(
        name="confessions",
        description="Anonymous confessions system",
        default_permissions=discord.Permissions(administrator=True),
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        """Register the persistent view so buttons survive restarts."""
        self.bot.add_view(ConfessButtonView())
        logger.info("Confessions: Persistent view registered.")

    @confess_group.command(name="deploy", description="Post the confessions panel in the configured channel")
    @app_commands.default_permissions(administrator=True)
    async def deploy_panel(self, interaction: discord.Interaction):
        """Post the initial confessions panel with the Confess button."""
        await interaction.response.defer(ephemeral=True)

        channel_id = await settings_service.get_int("confessions_channel_id")
        if not channel_id:
            return await interaction.followup.send(
                "❌ Confessions channel is not configured. "
                "Use `/setup channel confessions <#channel>` first.",
                ephemeral=True,
            )

        channel = interaction.guild.get_channel(channel_id)
        if not channel:
            return await interaction.followup.send(
                "❌ The configured confessions channel no longer exists. Please reconfigure it.",
                ephemeral=True,
            )

        # Permission pre-check
        bot_member = interaction.guild.me
        permissions = channel.permissions_for(bot_member)
        if not permissions.send_messages or not permissions.embed_links:
            return await interaction.followup.send(
                "❌ I don't have permission to send embeds in that channel.",
                ephemeral=True,
            )

        embed = discord.Embed(
            title="🤫 Anonymous Confessions",
            description=(
                "Have something on your mind? Share it anonymously!\n\n"
                "Click the button below to submit your confession. "
                "Your identity will **never** be shown publicly.\n\n"
                "**Rules:**\n"
                "• Be respectful — no harassment or threats\n"
                "• Must be verified to confess\n"
                "• Minimum 10 characters"
            ),
            color=discord.Color.dark_grey(),
        )
        guild_icon = interaction.guild.icon.url if interaction.guild.icon else None
        if guild_icon:
            embed.set_thumbnail(url=guild_icon)
        embed.set_footer(text="All confessions are anonymous")

        try:
            await channel.send(embed=embed, view=ConfessButtonView())
            await interaction.followup.send(
                f"✅ Confessions panel deployed in {channel.mention}.", ephemeral=True
            )
        except discord.HTTPException as e:
            logger.error(f"Failed to deploy confessions panel: {e}")
            await interaction.followup.send(
                f"❌ Failed to send the panel: `{e}`", ephemeral=True
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(ConfessionsCog(bot))
