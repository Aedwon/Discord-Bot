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

import asyncio
from discord.ext import tasks

# Track which channels need a full-channel end-to-end resynchronization of numbers
_channels_needing_sync: set[int] = set()


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

        # Dynamically determine the next confession number
        confession_number = 1
        async for msg in self.target_channel.history(limit=50):
            if msg.author == interaction.client.user and msg.embeds:
                title = msg.embeds[0].title
                if title and title.startswith("Confession #"):
                    import re
                    match = re.search(r'#(\d+)', title)
                    if match:
                        confession_number = int(match.group(1)) + 1
                        break

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
            return await interaction.response.send_message(
                "❌ I don't have permission to send messages in the confessions channel. "
                "Please notify an admin.",
                ephemeral=True,
            )
        except discord.HTTPException as e:
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
        if not self.sync_queue_worker.is_running():
            self.sync_queue_worker.start()
        logger.info("Confessions: Persistent view registered and sync worker started.")

    def cog_unload(self):
        self.sync_queue_worker.cancel()

    @tasks.loop(seconds=5)
    async def sync_queue_worker(self):
        """Process any pending channel resyncs one by one."""
        if not _channels_needing_sync:
            return
        
        channel_id = _channels_needing_sync.pop()
        channel = self.bot.get_channel(channel_id)
        if not channel:
            return
            
        try:
            await self._run_full_channel_sync(channel)
        except Exception as e:
            logger.error(f"Error syncing confessions for {channel_id}: {e}")
            
    async def _run_full_channel_sync(self, channel: discord.TextChannel):
        """Scans the entire channel and corrects all embed titles end-to-end to be perfectly sequential."""
        expected_number = 1
        async for msg in channel.history(limit=None, oldest_first=True):
            if msg.author == self.bot.user and msg.embeds:
                embed = msg.embeds[0]
                if embed.title and embed.title.startswith("Confession #"):
                    correct_title = f"Confession #{expected_number}"
                    if embed.title != correct_title:
                        embed.title = correct_title
                        try:
                            await msg.edit(embed=embed)
                            # Gently respect Discord rate limits
                            await asyncio.sleep(2)
                        except discord.HTTPException:
                            pass
                    expected_number += 1

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        """Trigger a sync if a message is deleted in the confessions channel."""
        channel_id = await settings_service.get_int("confessions_channel_id")
        if payload.channel_id == channel_id:
            # We add it to the set so it gets picked up by `sync_queue_worker`
            _channels_needing_sync.add(channel_id)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        """Same logic for message purges."""
        channel_id = await settings_service.get_int("confessions_channel_id")
        if payload.channel_id == channel_id:
            _channels_needing_sync.add(channel_id)

    @confess_group.command(name="sync", description="Force re-number all confessions sequentially (fixes gaps from deletions)")
    @app_commands.default_permissions(administrator=True)
    async def sync_confessions(self, interaction: discord.Interaction):
        """Manually trigger a full end-to-end sequential renumber of all confessions."""
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
                "❌ The configured confessions channel no longer exists.",
                ephemeral=True,
            )

        # Count how many confessions exist before starting
        total = 0
        async for msg in channel.history(limit=None, oldest_first=True):
            if msg.author == self.bot.user and msg.embeds:
                if msg.embeds[0].title and msg.embeds[0].title.startswith("Confession #"):
                    total += 1

        if total == 0:
            return await interaction.followup.send(
                "ℹ️ No confessions found in the channel. Nothing to sync.",
                ephemeral=True,
            )

        await interaction.followup.send(
            f"🔄 Syncing **{total}** confessions... This may take a while "
            f"(~{total * 2}s max). I'll notify you when done.",
            ephemeral=True,
        )

        try:
            corrected = 0
            expected_number = 1
            async for msg in channel.history(limit=None, oldest_first=True):
                if msg.author == self.bot.user and msg.embeds:
                    embed = msg.embeds[0]
                    if embed.title and embed.title.startswith("Confession #"):
                        correct_title = f"Confession #{expected_number}"
                        if embed.title != correct_title:
                            embed.title = correct_title
                            try:
                                await msg.edit(embed=embed)
                                corrected += 1
                                await asyncio.sleep(2)
                            except discord.HTTPException as e:
                                logger.warning(f"Failed to edit confession during sync: {e}")
                        expected_number += 1

            await interaction.followup.send(
                f"✅ Sync complete! **{corrected}** confession(s) renumbered out of **{total}** total.",
                ephemeral=True,
            )
        except Exception as e:
            logger.error(f"Confession sync error: {e}")
            await interaction.followup.send(
                f"❌ Sync encountered an error: `{e}`",
                ephemeral=True,
            )

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
