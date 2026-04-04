"""
Anonymous Messages Cog - Channel-based anonymous messaging with sticky panel.
Features:
  - Sticky panel re-posted every 10 minutes at the bottom of the channel
  - "Send Message" button on the panel → opens modal → posts anonymous embed
  - "Reply Anonymously" button on every posted message → opens modal → posts anonymous reply
  - Verification gate on all interactions
  - Channel also allows normal (visible) messages from users
"""

import discord
from discord.ext import commands, tasks
from discord import app_commands
import datetime
import logging

from services.settings_service import settings_service
from services.verification_service import verification_service
from utils.anon_log import log_anonymous_action

logger = logging.getLogger("mlbb_bot.anon_messages")

import asyncio
from discord.ext import tasks

# Track which channels need full-channel anon message number syncs
_channels_needing_sync_anon: set[int] = set()


# ─── Modals ──────────────────────────────────────────────────────────


class AnonMessageModal(discord.ui.Modal, title="📨 Send Anonymous Message"):
    """Modal for posting a new anonymous message."""

    message_input = discord.ui.TextInput(
        label="Your Message",
        style=discord.TextStyle.paragraph,
        placeholder="Write your anonymous message here...",
        required=True,
        min_length=10,
        max_length=2000,
    )

    def __init__(self, channel: discord.TextChannel):
        super().__init__()
        self.target_channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        text = self.message_input.value.strip()

        if len(text) < 10:
            return await interaction.response.send_message(
                "❌ Your message is too short (minimum 10 characters).",
                ephemeral=True,
            )

        msg_number = 1
        async for msg in self.target_channel.history(limit=50):
            if msg.author == interaction.client.user and msg.embeds:
                title = msg.embeds[0].title
                if title and title.startswith("Anonymous Message #"):
                    import re
                    match = re.search(r'#(\d+)', title)
                    if match:
                        msg_number = int(match.group(1)) + 1
                        break

        embed = discord.Embed(
            title=f"Anonymous Message #{msg_number}",
            description=text,
            color=discord.Color.blurple(),
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        guild_icon = interaction.guild.icon.url if interaction.guild.icon else None
        embed.set_author(name="Anonymous", icon_url=guild_icon)
        embed.set_footer(text="Click below to reply anonymously")

        try:
            await self.target_channel.send(embed=embed, view=AnonReplyView())
        except discord.Forbidden:
            return await interaction.response.send_message(
                "❌ I don't have permission to send messages in that channel.",
                ephemeral=True,
            )
        except discord.HTTPException as e:
            logger.error(f"Failed to post anon message #{msg_number}: {e}")
            return await interaction.response.send_message(
                "❌ Something went wrong. Please try again later.",
                ephemeral=True,
            )

        await interaction.response.send_message(
            f"✅ Your anonymous message (#{msg_number}) has been posted!",
            ephemeral=True,
        )

        # Log to admin channel
        await log_anonymous_action(
            interaction.client,
            user=interaction.user,
            action_type="Anon Message",
            content=text,
            channel=self.target_channel,
            reference_label=f"Message #{msg_number}",
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        logger.error(f"AnonMessageModal error for user {interaction.user.id}: {error}")
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ An unexpected error occurred.", ephemeral=True
                )
            else:
                await interaction.followup.send(
                    "❌ An unexpected error occurred.", ephemeral=True
                )
        except discord.HTTPException:
            pass


class AnonReplyModal(discord.ui.Modal, title="💬 Anonymous Reply"):
    """Modal for replying anonymously to an existing message."""

    reply_input = discord.ui.TextInput(
        label="Your Reply",
        style=discord.TextStyle.paragraph,
        placeholder="Write your anonymous reply here...",
        required=True,
        min_length=5,
        max_length=2000,
    )

    def __init__(self, original_message: discord.Message, channel: discord.TextChannel):
        super().__init__()
        self.original_message = original_message
        self.target_channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        text = self.reply_input.value.strip()

        if len(text) < 5:
            return await interaction.response.send_message(
                "❌ Your reply is too short (minimum 5 characters).",
                ephemeral=True,
            )

        embed = discord.Embed(
            description=text,
            color=discord.Color.greyple(),
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        guild_icon = interaction.guild.icon.url if interaction.guild.icon else None
        embed.set_author(name="Anonymous Reply", icon_url=guild_icon)

        try:
            await self.target_channel.send(
                embed=embed,
                view=AnonReplyView(),
                reference=self.original_message,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            return await interaction.response.send_message(
                "❌ I don't have permission to send messages in that channel.",
                ephemeral=True,
            )
        except discord.HTTPException as e:
            logger.error(f"Failed to post anon reply: {e}")
            return await interaction.response.send_message(
                "❌ Something went wrong. Please try again later.",
                ephemeral=True,
            )

        await interaction.response.send_message(
            "✅ Your anonymous reply has been posted!", ephemeral=True
        )

        # Log to admin channel
        await log_anonymous_action(
            interaction.client,
            user=interaction.user,
            action_type="Anon Reply",
            content=text,
            channel=self.target_channel,
            reference_label=f"Reply to message in #{self.target_channel.name}",
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        logger.error(f"AnonReplyModal error for user {interaction.user.id}: {error}")
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ An unexpected error occurred.", ephemeral=True
                )
            else:
                await interaction.followup.send(
                    "❌ An unexpected error occurred.", ephemeral=True
                )
        except discord.HTTPException:
            pass


# ─── Persistent Views ────────────────────────────────────────────────


class AnonPanelView(discord.ui.View):
    """Persistent view on the sticky panel. 'Send Message' button."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Send Message",
        style=discord.ButtonStyle.primary,
        emoji="✉️",
        custom_id="anon_messages:send_button",
    )
    async def send_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            return await interaction.response.send_message(
                "❌ This can only be used in a server.", ephemeral=True
            )

        if not verification_service.is_verified(interaction.user.id):
            return await interaction.response.send_message(
                "❌ You must be verified to send anonymous messages.",
                ephemeral=True,
            )

        channel_id = await settings_service.get_int("anon_messages_channel_id")
        if not channel_id:
            return await interaction.response.send_message(
                "❌ Anonymous messages channel is not configured.", ephemeral=True
            )

        channel = interaction.guild.get_channel(channel_id)
        if not channel:
            return await interaction.response.send_message(
                "❌ The configured channel no longer exists.", ephemeral=True
            )

        permissions = channel.permissions_for(interaction.guild.me)
        if not permissions.send_messages or not permissions.embed_links:
            return await interaction.response.send_message(
                "❌ I don't have permission to send in that channel.", ephemeral=True
            )

        await interaction.response.send_modal(AnonMessageModal(channel))


class AnonReplyView(discord.ui.View):
    """Persistent view attached to every anonymous message. 'Reply Anonymously' button."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Reply Anonymously",
        style=discord.ButtonStyle.secondary,
        emoji="💬",
        custom_id="anon_messages:reply_button",
    )
    async def reply_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            return await interaction.response.send_message(
                "❌ This can only be used in a server.", ephemeral=True
            )

        if not verification_service.is_verified(interaction.user.id):
            return await interaction.response.send_message(
                "❌ You must be verified to reply anonymously.",
                ephemeral=True,
            )

        # The message this button is on IS the message we're replying to
        original_message = interaction.message
        channel = interaction.channel

        permissions = channel.permissions_for(interaction.guild.me)
        if not permissions.send_messages or not permissions.embed_links:
            return await interaction.response.send_message(
                "❌ I don't have permission to send in this channel.", ephemeral=True
            )

        await interaction.response.send_modal(AnonReplyModal(original_message, channel))


# ─── Cog ─────────────────────────────────────────────────────────────


class AnonMessageCog(commands.Cog, name="AnonMessages"):
    """Anonymous messages system with sticky panel."""

    anon_group = app_commands.Group(
        name="anon",
        description="Anonymous messages system",
        default_permissions=discord.Permissions(administrator=True),
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._panel_message_id: int | None = None  # Cached panel message ID

    async def cog_load(self):
        """Register persistent views and start the sticky task."""
        self.bot.add_view(AnonPanelView())
        self.bot.add_view(AnonReplyView())

        # Load cached panel message ID
        stored = await settings_service.get_int("anon_panel_message_id")
        self._panel_message_id = stored if stored else None

        self.sticky_repost.start()
        self.sync_anon_queue_worker.start()
        logger.info("AnonMessages: Persistent views registered, tasks started.")

    def cog_unload(self):
        self.sticky_repost.cancel()
        self.sync_anon_queue_worker.cancel()

    @tasks.loop(seconds=5)
    async def sync_anon_queue_worker(self):
        """Process any pending channel resyncs one by one for anonymous messages."""
        if not _channels_needing_sync_anon:
            return
        
        channel_id = _channels_needing_sync_anon.pop()
        channel = self.bot.get_channel(channel_id)
        if not channel:
            return
            
        try:
            await self._run_full_channel_sync(channel)
        except Exception as e:
            logger.error(f"Error syncing anon messages for {channel_id}: {e}")
            
    async def _run_full_channel_sync(self, channel: discord.TextChannel):
        """Scans the entire channel and corrects all anonymous message embed titles end-to-end to be perfectly sequential."""
        expected_number = 1
        async for msg in channel.history(limit=None, oldest_first=True):
            if msg.author == self.bot.user and msg.embeds:
                embed = msg.embeds[0]
                if embed.title and embed.title.startswith("Anonymous Message #"):
                    correct_title = f"Anonymous Message #{expected_number}"
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
        """Trigger a sync if a message is deleted in the anon messages channel."""
        channel_id = await settings_service.get_int("anon_messages_channel_id")
        if payload.channel_id == channel_id:
            _channels_needing_sync_anon.add(channel_id)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        """Same logic for message purges."""
        channel_id = await settings_service.get_int("anon_messages_channel_id")
        if payload.channel_id == channel_id:
            _channels_needing_sync_anon.add(channel_id)

    # ─── Sticky Panel Background Task ────────────────────────────────

    @tasks.loop(minutes=10)
    async def sticky_repost(self):
        """Delete the old panel and re-post it at the bottom of the channel."""
        try:
            channel_id = await settings_service.get_int("anon_messages_channel_id")
            if not channel_id:
                return

            guild = self.bot.guilds[0] if self.bot.guilds else None
            if not guild:
                return

            channel = guild.get_channel(channel_id)
            if not channel:
                logger.warning("AnonMessages: Configured channel not found, skipping sticky repost.")
                return

            permissions = channel.permissions_for(guild.me)
            if not permissions.send_messages or not permissions.embed_links or not permissions.manage_messages:
                logger.warning("AnonMessages: Missing permissions in anon channel, skipping sticky repost.")
                return

            # Check if the panel is already at the bottom — skip repost if so
            if self._panel_message_id:
                try:
                    # Fetch the actual latest message from the API (not the cached gateway value)
                    # channel.last_message_id can be stale after restarts or missed events
                    last_messages = [msg async for msg in channel.history(limit=1)]
                    if last_messages and last_messages[0].id == self._panel_message_id:
                        # Panel is already the latest message, no need to repost
                        return
                except discord.HTTPException:
                    pass  # If history fetch fails, proceed with repost as fallback

                # Delete old panel since it's no longer at the bottom
                try:
                    old_msg = await channel.fetch_message(self._panel_message_id)
                    await old_msg.delete()
                except discord.NotFound:
                    pass  # Already deleted
                except discord.HTTPException as e:
                    logger.warning(f"AnonMessages: Failed to delete old panel: {e}")

            # Post new panel
            embed = discord.Embed(
                title="📨 Anonymous Messages",
                description=(
                    "Have something to say? Share it anonymously!\n\n"
                    "Click the button below to post an anonymous message. "
                    "Your identity will **never** be shown.\n\n"
                    "You can also reply anonymously to any message using its reply button."
                ),
                color=discord.Color.blurple(),
            )
            guild_icon = guild.icon.url if guild.icon else None
            if guild_icon:
                embed.set_thumbnail(url=guild_icon)
            embed.set_footer(text="This panel refreshes every 10 minutes")

            new_msg = await channel.send(embed=embed, view=AnonPanelView())
            self._panel_message_id = new_msg.id
            await settings_service.set("anon_panel_message_id", str(new_msg.id))

        except Exception as e:
            logger.error(f"AnonMessages: Sticky repost error: {e}")

    @sticky_repost.before_loop
    async def before_sticky(self):
        await self.bot.wait_until_ready()

    # ─── Admin Commands ──────────────────────────────────────────────

    @anon_group.command(name="sync", description="Force re-number all anonymous messages sequentially (fixes gaps from deletions)")
    @app_commands.default_permissions(administrator=True)
    async def sync_anon_messages(self, interaction: discord.Interaction):
        """Manually trigger a full end-to-end sequential renumber of all anonymous messages."""
        await interaction.response.defer(ephemeral=True)

        channel_id = await settings_service.get_int("anon_messages_channel_id")
        if not channel_id:
            return await interaction.followup.send(
                "❌ Anonymous messages channel is not configured. "
                "Use `/setup channel anon_messages <#channel>` first.",
                ephemeral=True,
            )

        channel = interaction.guild.get_channel(channel_id)
        if not channel:
            return await interaction.followup.send(
                "❌ The configured channel no longer exists.",
                ephemeral=True,
            )

        # Count how many anonymous messages exist before starting
        total = 0
        async for msg in channel.history(limit=None, oldest_first=True):
            if msg.author == self.bot.user and msg.embeds:
                if msg.embeds[0].title and msg.embeds[0].title.startswith("Anonymous Message #"):
                    total += 1

        if total == 0:
            return await interaction.followup.send(
                "ℹ️ No anonymous messages found in the channel. Nothing to sync.",
                ephemeral=True,
            )

        await interaction.followup.send(
            f"🔄 Syncing **{total}** anonymous messages... This may take a while "
            f"(~{total * 2}s max). I'll notify you when done.",
            ephemeral=True,
        )

        try:
            corrected = 0
            expected_number = 1
            async for msg in channel.history(limit=None, oldest_first=True):
                if msg.author == self.bot.user and msg.embeds:
                    embed = msg.embeds[0]
                    if embed.title and embed.title.startswith("Anonymous Message #"):
                        correct_title = f"Anonymous Message #{expected_number}"
                        if embed.title != correct_title:
                            embed.title = correct_title
                            try:
                                await msg.edit(embed=embed)
                                corrected += 1
                                await asyncio.sleep(2)
                            except discord.HTTPException as e:
                                logger.warning(f"Failed to edit anon message during sync: {e}")
                        expected_number += 1

            await interaction.followup.send(
                f"✅ Sync complete! **{corrected}** message(s) renumbered out of **{total}** total.",
                ephemeral=True,
            )
        except Exception as e:
            logger.error(f"Anon message sync error: {e}")
            await interaction.followup.send(
                f"❌ Sync encountered an error: `{e}`",
                ephemeral=True,
            )

    @anon_group.command(name="deploy", description="Deploy the anonymous messages panel now")
    @app_commands.default_permissions(administrator=True)
    async def deploy_panel(self, interaction: discord.Interaction):
        """Manually deploy/redeploy the sticky panel immediately."""
        await interaction.response.defer(ephemeral=True)

        channel_id = await settings_service.get_int("anon_messages_channel_id")
        if not channel_id:
            return await interaction.followup.send(
                "❌ Anonymous messages channel is not configured. "
                "Use `/setup channel anon_messages <#channel>` first.",
                ephemeral=True,
            )

        channel = interaction.guild.get_channel(channel_id)
        if not channel:
            return await interaction.followup.send(
                "❌ The configured channel no longer exists. Please reconfigure.",
                ephemeral=True,
            )

        permissions = channel.permissions_for(interaction.guild.me)
        if not permissions.send_messages or not permissions.embed_links:
            return await interaction.followup.send(
                "❌ I don't have permission to send embeds in that channel.",
                ephemeral=True,
            )

        # Delete old panel if exists
        if self._panel_message_id:
            try:
                old_msg = await channel.fetch_message(self._panel_message_id)
                await old_msg.delete()
            except (discord.NotFound, discord.HTTPException):
                pass

        # Post new panel
        embed = discord.Embed(
            title="📨 Anonymous Messages",
            description=(
                "Have something to say? Share it anonymously!\n\n"
                "Click the button below to post an anonymous message. "
                "Your identity will **never** be shown.\n\n"
                "You can also reply anonymously to any message using its reply button."
            ),
            color=discord.Color.blurple(),
        )
        guild_icon = interaction.guild.icon.url if interaction.guild.icon else None
        if guild_icon:
            embed.set_thumbnail(url=guild_icon)
        embed.set_footer(text="This panel refreshes every 10 minutes")

        try:
            new_msg = await channel.send(embed=embed, view=AnonPanelView())
            self._panel_message_id = new_msg.id
            await settings_service.set("anon_panel_message_id", str(new_msg.id))

            await interaction.followup.send(
                f"✅ Anonymous messages panel deployed in {channel.mention}.", ephemeral=True
            )
        except discord.HTTPException as e:
            logger.error(f"Failed to deploy anon panel: {e}")
            await interaction.followup.send(
                f"❌ Failed to send the panel: `{e}`", ephemeral=True
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(AnonMessageCog(bot))
