"""
Embeds Cog - Discohook Embed Manager with Scheduling
Allows admins to send, edit, schedule, and extract embeds from Discohook links.
"""

import discord
from discord.ext import commands, tasks
from discord import app_commands
import json
import base64
import random
import string
import datetime
import logging
import asyncio
import copy
import os
import traceback
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote
from io import BytesIO
import pytz

from services.database import db
from utils.constants import TZ_MANILA
from utils.views import ManageScheduledEmbedView

logger = logging.getLogger('mlbb_bot')

# Resolve storage path relative to this file's directory so it always
# lands next to the bot code, regardless of the process's working directory.
STORAGE_DIR = Path(__file__).resolve().parent.parent
STORAGE_FILE = STORAGE_DIR / "scheduled_embeds.json"


def generate_identifier(length=6):
    """Generate a random identifier for scheduled embeds."""
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))


def discohook_to_view(components_data):
    """Convert Discohook component data to a discord.ui.View."""
    if not components_data:
        return None
    
    view = discord.ui.View(timeout=None)
    
    for row in components_data:
        for comp in row.get("components", []):
            comp_type = comp.get("type")
            
            if comp_type == 2:  # Button
                style = comp.get("style", 1)
                label = comp.get("label")
                url = comp.get("url")
                disabled = comp.get("disabled", False)
                emoji = comp.get("emoji", {}).get("name") if comp.get("emoji") else None
                
                if style == 5 and url:  # Link button
                    view.add_item(discord.ui.Button(
                        style=discord.ButtonStyle.link,
                        label=label,
                        url=url,
                        emoji=emoji,
                        disabled=disabled
                    ))
                else:
                    view.add_item(discord.ui.Button(
                        style=discord.ButtonStyle(style),
                        label=label,
                        custom_id=comp.get("custom_id"),
                        emoji=emoji,
                        disabled=disabled
                    ))
            
            elif comp_type == 3:  # Select Menu
                options = [
                    discord.SelectOption(
                        label=o.get("label", ""),
                        value=o.get("value", ""),
                        description=o.get("description"),
                        emoji=o.get("emoji", {}).get("name") if o.get("emoji") else None,
                        default=o.get("default", False)
                    )
                    for o in comp.get("options", [])
                ]
                view.add_item(discord.ui.Select(
                    custom_id=comp.get("custom_id"),
                    placeholder=comp.get("placeholder"),
                    min_values=comp.get("min_values", 1),
                    max_values=comp.get("max_values", 1),
                    options=options,
                    disabled=comp.get("disabled", False)
                ))
    
    return view if len(view.children) > 0 else None


class EmbedsCog(commands.Cog, name="Embeds"):
    """Discohook embed manager with scheduling capabilities."""
    
    embed_group = app_commands.Group(name="embed", description="Discohook embed manager", default_permissions=discord.Permissions(administrator=True))
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.storage_file = str(STORAGE_FILE)
        self.scheduled_data: dict = {}
        
    def _load_data(self):
        """Load scheduled embeds from the JSON file on disk."""
        if os.path.exists(self.storage_file):
            try:
                with open(self.storage_file, 'r') as f:
                    raw = f.read().strip()
                    if raw:
                        self.scheduled_data = json.loads(raw)
                    else:
                        self.scheduled_data = {}
            except (json.JSONDecodeError, OSError) as e:
                logger.error(f"Failed to load scheduled embeds from {self.storage_file}: {e}")
                self.scheduled_data = {}
        else:
            self.scheduled_data = {}
            logger.info(f"No scheduled embeds file found at {self.storage_file}, starting fresh.")
            
    def _save_data(self):
        """Atomically persist the scheduled data to disk."""
        try:
            temp_file = self.storage_file + ".tmp"
            with open(temp_file, 'w') as f:
                json.dump(self.scheduled_data, f, indent=2)
            os.replace(temp_file, self.storage_file)
        except OSError as e:
            logger.error(f"Failed to save scheduled embeds to {self.storage_file}: {e}")
    
    async def cog_load(self):
        """Start background task when cog is loaded."""
        self._load_data()
        
        # Reset any embeds that got stuck in 'processing' from a previous crash
        changed = False
        for identifier, data in self.scheduled_data.items():
            if data.get('status') == 'processing':
                data['status'] = 'pending'
                changed = True
        if changed:
            self._save_data()
        
        logger.info(f"EmbedsCog loaded. {len(self.scheduled_data)} scheduled embed(s) in queue. Storage: {self.storage_file}")
    
    @commands.Cog.listener()
    async def on_ready(self):
        """Start the schedule loop once the bot is fully connected and ready."""
        if not self.schedule_loop.is_running():
            self.schedule_loop.start()
            logger.info("Schedule loop started from on_ready.")
    
    def cog_unload(self):
        self.schedule_loop.cancel()
    
    # ─────────────────────────────────────────────────────────────────────
    # Background Task: Send Scheduled Embeds
    # ─────────────────────────────────────────────────────────────────────
    
    @tasks.loop(minutes=1)
    async def schedule_loop(self):
        """Check and send pending scheduled embeds."""
        try:
            # Always reload from disk in case the file was updated externally
            self._load_data()
            
            now_unix = int(datetime.datetime.now(pytz.UTC).timestamp())
            
            # Find due embeds
            due_keys = []
            for identifier, data in self.scheduled_data.items():
                if data.get('status') == 'pending' and data.get('schedule_for_utc', 0) <= now_unix:
                    due_keys.append(identifier)
                    
            if not due_keys:
                return

            for identifier in due_keys:
                # Re-check — the entry might have been removed between iterations
                if identifier not in self.scheduled_data:
                    continue
                    
                row = self.scheduled_data[identifier]
                row['status'] = 'processing'
                self._save_data()
                
                target_channel = None
                log_embed_color = discord.Color.red()
                log_title = "❌ Scheduled Embed Failed"
                message_link = None
                user_id = row.get('user_id')
                
                try:
                    channel = self.bot.get_channel(row['channel_id'])
                    if not channel:
                        channel = await self.bot.fetch_channel(row['channel_id'])
                    
                    target_channel = channel
                    
                    payload = row['embed_json']
                    if isinstance(payload, str):
                        payload = json.loads(payload)
                        
                    embeds = [discord.Embed.from_dict(copy.deepcopy(e)) for e in payload.get("embeds", [])]
                    view = discohook_to_view(payload.get("components", []))
                    
                    # Fix empty string content throwing 400 Bad Request
                    content = row.get('content')
                    safe_content = content if content else None
                    
                    # Wrap send in wait_for to prevent infinite hangs on rate limits 
                    sent_msg = await asyncio.wait_for(
                        channel.send(content=safe_content, embeds=embeds, view=view),
                        timeout=30.0
                    )
                    message_link = f"https://discord.com/channels/{channel.guild.id}/{channel.id}/{sent_msg.id}"
                    
                    # Remove from JSON store completely
                    self.scheduled_data.pop(identifier, None)
                    self._save_data()
                    
                    log_title = "✅ Scheduled Embed Sent"
                    log_embed_color = 0x00FF00
                    logger.info(f"Successfully sent scheduled embed {identifier} to #{channel.name}")
                
                except asyncio.TimeoutError:
                    logger.error(f"Timeout sending scheduled embed {identifier}")
                    if identifier in self.scheduled_data:
                        self.scheduled_data[identifier]['status'] = 'pending'
                        self._save_data()
                    continue  # Retry next tick
                except discord.HTTPException as e:
                    logger.error(f"HTTPException sending scheduled embed {identifier}: {e.status} - {e.text}")
                    if e.status >= 500 or e.status == 429:
                        if identifier in self.scheduled_data:
                            self.scheduled_data[identifier]['status'] = 'pending'
                            self._save_data()
                        continue  # Transient error, retry next tick
                    else:
                        self.scheduled_data.pop(identifier, None)
                        self._save_data()
                        log_title += f" (HTTP {e.status})"
                except Exception as e:
                    logger.error(f"Failed to send scheduled embed {identifier}: {e}")
                    logger.error(traceback.format_exc())
                    self.scheduled_data.pop(identifier, None)
                    self._save_data()
                    log_title += " (Internal Error)"
                
                # Try to send a log notification
                try:
                    guild_id = target_channel.guild.id if target_channel and hasattr(target_channel, 'guild') else None
                    if guild_id:
                        log_row = await db.fetch_one(
                            "SELECT embed_log_channel_id FROM guild_settings WHERE guild_id = %s",
                            (guild_id,)
                        )
                        if log_row and log_row.get('embed_log_channel_id'):
                            log_channel = self.bot.get_channel(log_row['embed_log_channel_id'])
                            if log_channel:
                                embed = discord.Embed(
                                    title=log_title,
                                    color=log_embed_color,
                                    timestamp=datetime.datetime.now(TZ_MANILA)
                                )
                                embed.add_field(name="Identifier", value=f"`{identifier}`")
                                embed.add_field(name="Channel", value=target_channel.mention)
                                embed.add_field(name="User", value=f"<@{user_id}>")
                                if message_link:
                                    embed.add_field(name="Link", value=f"[Jump to Message]({message_link})", inline=False)
                                
                                await log_channel.send(content=f"<@{user_id}>", embed=embed)
                except Exception as log_e:
                    logger.error(f"Failed to send log for scheduled embed {identifier}: {log_e}")
                
                # Sleep between dispatches to organically circumvent spam filters
                await asyncio.sleep(1)
                
        except Exception as e:
            logger.error(f"Critical error in schedule_loop: {e}")
            logger.error(traceback.format_exc())
    
    
    # ─────────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────────
    
    async def _process_link(self, link: str, interaction: discord.Interaction):
        """Parse a Discohook share link and extract embed data."""
        if not (link.startswith("https://discohook.org/?data=") or link.startswith("https://discohook.app/?data=")):
            await interaction.followup.send("❌ Invalid Discohook link! Ensure it starts with `https://discohook.app/?data=`.", ephemeral=True)
            return None

        try:
            parsed = urlparse(link)
            qs = parse_qs(parsed.query)
            encoded = qs.get("data", [None])[0]
            
            if not encoded:
                await interaction.followup.send("❌ No valid data found in the URL query.", ephemeral=True)
                return None
            
            # Add padding if needed
            missing = len(encoded) % 4
            if missing:
                encoded += "=" * (4 - missing)
            
            decoded = base64.urlsafe_b64decode(encoded).decode("utf-8")
            data = json.loads(decoded)
            
            # Discohook format: messages[0].data
            msg_data = data["messages"][0]["data"]
            return msg_data
        
        except Exception as e:
            logger.error(f"Link parse error: {e}")
            await interaction.followup.send(f"❌ Failed to parse Discohook link: `{e}`", ephemeral=True)
            return None

    # ─────────────────────────────────────────────────────────────────────
    # Slash Commands
    # ─────────────────────────────────────────────────────────────────────
    
    @embed_group.command(name="send", description="Send an embed from a Discohook link or Backup File")
    @app_commands.describe(
        channel="Channel to send the embed to",
        link="Discohook share link (if under 512 chars)",
        long_link="Alternative: Paste the full Discohook link here if it's too long",
        data_file="If link is too long for Discord (>6000 chars), upload a .txt with the link or the Discohook .json backup",
        schedule_for="(Optional) Date and time to send (DD/MM/YYYY HH:MM, UTC+8)"
    )
    async def send_embed(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        link: str | None = None,
        long_link: str | None = None,
        data_file: discord.Attachment | None = None,
        schedule_for: str | None = None
    ):
        """Send or schedule an embed from a Discohook link."""
        await interaction.response.defer(ephemeral=True)
        
        data = None
        if data_file:
            try:
                file_bytes = await data_file.read()
                file_text = file_bytes.decode('utf-8').strip()
                if file_text.startswith("http"):
                    # Process as text link
                    data = await self._process_link(file_text, interaction)
                else:
                    # Process directly as JSON backup
                    json_payload = json.loads(file_text)
                    if "messages" in json_payload and len(json_payload["messages"]) > 0:
                        data = json_payload["messages"][0]["data"]
                    else:
                        await interaction.followup.send("❌ Could not extract message data from the provided JSON file.", ephemeral=True)
                        return
            except Exception as e:
                logger.error(f"Failed to process data_file: {e}")
                await interaction.followup.send("❌ Error parsing the uploaded file. Ensure it's a valid link in a .txt or a valid discohook JSON backup.", ephemeral=True)
                return
        else:
            final_link = long_link or link
            if not final_link:
                await interaction.followup.send("❌ Please provide a Discohook link or a data_file.", ephemeral=True)
                return
            data = await self._process_link(final_link, interaction)

        if not data:
            return
        
        content = data.get("content", "")
        embeds_list = data.get("embeds", [])
        components_list = data.get("components", [])

        embeds = [discord.Embed.from_dict(copy.deepcopy(e)) for e in embeds_list]
        view = discohook_to_view(components_list)
        
        if schedule_for:
            # Parse DD/MM/YYYY HH:MM
            try:
                dt = datetime.datetime.strptime(schedule_for, "%d/%m/%Y %H:%M")
                dt = TZ_MANILA.localize(dt)
                now = datetime.datetime.now(TZ_MANILA)
                if (dt - now).total_seconds() <= 0:
                    await interaction.followup.send("❌ The scheduled time must be in the future.", ephemeral=True)
                    return
            except Exception:
                await interaction.followup.send(
                    "❌ Invalid date format. Use **DD/MM/YYYY HH:MM** (24-hour, UTC+8).\nExample: `23/04/2026 18:30`",
                    ephemeral=True
                )
                return

            identifier = generate_identifier()
            dt_utc = dt.astimezone(pytz.UTC)
            unix_utc = int(dt_utc.timestamp())

            self.scheduled_data[identifier] = {
                "channel_id": channel.id,
                "user_id": interaction.user.id,
                "content": content,
                "embed_json": {"embeds": embeds_list, "components": components_list},
                "schedule_for_utc": unix_utc,
                "status": "pending"
            }
            self._save_data()
            
            await interaction.followup.send(
                f"⏰ Embed scheduled for {dt.strftime('%d/%m/%Y %H:%M')} in {channel.mention}.\n**ID:** `{identifier}`",
                ephemeral=True
            )

            # Log preview
            log_row = await db.fetch_one(
                "SELECT embed_log_channel_id FROM guild_settings WHERE guild_id = %s",
                (interaction.guild.id,)
            )
            if log_row and log_row.get('embed_log_channel_id'):
                log_channel = self.bot.get_channel(log_row['embed_log_channel_id'])
                if log_channel:
                    preview_text = f"📝 **Scheduled embed PREVIEW**\n**ID:** `{identifier}`\n**User:** {interaction.user.mention}\n**Channel:** {channel.mention}\n**Scheduled for:** {dt.strftime('%d/%m/%Y %H:%M')} UTC+8\n\n{content or ''}"
                    await log_channel.send(content=preview_text, embeds=embeds, view=view)

        else:
            # Send immediately
            safe_content = content if content else None
            sent_msg = await channel.send(content=safe_content, embeds=embeds, view=view)
            message_link = f"https://discord.com/channels/{interaction.guild_id}/{channel.id}/{sent_msg.id}"
            await interaction.followup.send(f"✅ Embed sent to {channel.mention}: [Jump to Message]({message_link})", ephemeral=True)

            # Log immediately
            log_row = await db.fetch_one(
                "SELECT embed_log_channel_id FROM guild_settings WHERE guild_id = %s",
                (interaction.guild.id,)
            )
            if log_row and log_row.get('embed_log_channel_id'):
                log_channel = self.bot.get_channel(log_row['embed_log_channel_id'])
                if log_channel:
                    log_embed = discord.Embed(title="📢 Embed Sent", color=discord.Color.gold())
                    log_embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
                    log_embed.add_field(name="User", value=interaction.user.mention, inline=True)
                    log_embed.add_field(name="Channel", value=channel.mention, inline=True)
                    log_embed.add_field(name="Link", value=f"[Jump to Message]({message_link})", inline=False)
                    await log_channel.send(embed=log_embed)
    
    @embed_group.command(name="edit", description="Edit an existing message using a Discohook link or JSON File.")
    @app_commands.describe(
        message_link="The message link to edit",
        link="Short Discohook link (if under 512 characters)",
        long_link="Alternative: Paste the full Discohook link here if it's too long",
        data_file="If link is too long (>6000 chars), upload a .txt with the link or the Discohook .json backup"
    )
    async def edit_embed(
        self, interaction: discord.Interaction,
        message_link: str,
        link: str | None = None,
        long_link: str | None = None,
        data_file: discord.Attachment | None = None,
    ):
        await interaction.response.defer(ephemeral=True)
        
        data = None
        if data_file:
            try:
                file_bytes = await data_file.read()
                file_text = file_bytes.decode('utf-8').strip()
                if file_text.startswith("http"):
                    data = await self._process_link(file_text, interaction)
                else:
                    json_payload = json.loads(file_text)
                    if "messages" in json_payload and len(json_payload["messages"]) > 0:
                        data = json_payload["messages"][0]["data"]
                    else:
                        await interaction.followup.send("❌ Could not extract message data from the provided JSON file.", ephemeral=True)
                        return
            except Exception as e:
                logger.error(f"Failed to process data_file on edit: {e}")
                await interaction.followup.send("❌ Error parsing the uploaded file. Ensure it's a valid link in a .txt or a valid discohook JSON backup.", ephemeral=True)
                return
        else:
            final_link = long_link or link
            if not final_link:
                await interaction.followup.send("❌ No Discohook link or file provided.", ephemeral=True)
                return
            data = await self._process_link(final_link, interaction)

        if not data:
            return
        
        content = data.get("content", "")
        embeds_list = data.get("embeds", [])
        components_list = data.get("components", [])

        try:
            parts = message_link.strip().split("/")
            if len(parts) < 7:
                await interaction.followup.send("❌ Invalid message link format.", ephemeral=True)
                return

            guild_id, channel_id, msg_id = int(parts[-3]), int(parts[-2]), int(parts[-1])
            channel = interaction.guild.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
            target_message = await channel.fetch_message(msg_id)

            new_embeds = [discord.Embed.from_dict(copy.deepcopy(e)) for e in embeds_list]
            new_view = discohook_to_view(components_list)

            if target_message.author.id == self.bot.user.id:
                safe_content = content if content else None
                await target_message.edit(content=safe_content, embeds=new_embeds, view=new_view)
                await interaction.followup.send(
                    f"✅ Edited: [Jump to Message]({message_link})", ephemeral=True,
                )
                return

            if target_message.webhook_id:
                webhooks = await channel.webhooks()
                webhook = next((w for w in webhooks if w.id == target_message.webhook_id), None)
                if webhook and webhook.token:
                    safe_content = content if content else None
                    await webhook.edit_message(
                        message_id=target_message.id, content=safe_content, embeds=new_embeds,
                    )
                    await interaction.followup.send(
                        f"✅ Edited webhook message (components may not be supported on webhooks depending on discord config): [Jump]({message_link})",
                        ephemeral=True,
                    )
                    return
                else:
                    await interaction.followup.send("❌ Could not find webhook to edit.", ephemeral=True)
                    return

            await interaction.followup.send("❌ I can only edit my own messages or webhook messages.", ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: `{e}`", ephemeral=True)

    @embed_group.command(name="download", description="Generate a Discohook link from a Discord message.")
    @app_commands.describe(message_link="Link to the Discord message containing the embed.")
    async def dl_embed(self, interaction: discord.Interaction, message_link: str):
        try:
            parts = message_link.strip().split("/")
            if len(parts) < 7:
                await interaction.response.send_message("❌ Invalid message link format.", ephemeral=True)
                return

            guild_id, channel_id, msg_id = int(parts[-3]), int(parts[-2]), int(parts[-1])
            channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
            message = await channel.fetch_message(msg_id)

            if not message.embeds and not message.content and not message.components:
                await interaction.response.send_message("❌ Message has no embeds, content, or components.", ephemeral=True)
                return

            payload = {
                "messages": [{
                    "data": {
                        "content": message.content or "",
                        "embeds": [embed.to_dict() for embed in message.embeds],
                        "components": [c.to_dict() for c in message.components] if message.components else [],
                    },
                    "type": "message",
                }]
            }

            json_string = json.dumps(payload)
            encoded = base64.urlsafe_b64encode(json_string.encode()).decode().rstrip("=")
            discohook_link = f"https://discohook.app/?data={quote(encoded)}"

            if len(discohook_link) > 2000:
                buffer = BytesIO(discohook_link.encode("utf-8"))
                buffer.seek(0)
                await interaction.response.send_message(
                    content="📄 The link is too long for a single message. Here's a text file:",
                    ephemeral=True,
                    file=discord.File(fp=buffer, filename="discohook_link.txt"),
                )
            else:
                await interaction.response.send_message(
                    f"✅ [Open in Discohook]({discohook_link})", ephemeral=True,
                )

        except Exception as e:
            await interaction.response.send_message(f"❌ Error: `{e}`", ephemeral=True)

    @embed_group.command(name="manage", description="View, preview, and manage your scheduled embeds")
    @app_commands.default_permissions(administrator=True)
    async def manage_embeds(self, interaction: discord.Interaction):
        """Manage pending scheduled embeds."""
        # Refresh from disk to catch any external changes
        self._load_data()
        
        user_id = interaction.user.id
        formatted_rows = []
        
        for identifier, data in self.scheduled_data.items():
            if data.get('user_id') == user_id and data.get('status') == 'pending':
                sf_utc = datetime.datetime.fromtimestamp(data['schedule_for_utc'], tz=pytz.UTC)
                sf_manila = sf_utc.astimezone(TZ_MANILA)
                formatted_rows.append({
                    'identifier': identifier,
                    'schedule_for': sf_manila.strftime("%d/%m/%Y %H:%M")
                })
        
        if not formatted_rows:
            await interaction.response.send_message("❌ You have no pending scheduled embeds.", ephemeral=True)
            return

        view = ManageScheduledEmbedView(formatted_rows, self, interaction.user)
        await interaction.response.send_message("Select an embed to preview and manage:", view=view, ephemeral=True)
        
    async def preview_scheduled_embed_action(self, interaction: discord.Interaction, identifier: str):
        # Refresh from disk
        self._load_data()
        
        row = self.scheduled_data.get(identifier)
        if not row:
            await interaction.response.edit_message(content="❌ Could not find that embed.", embeds=[], view=None)
            return
            
        try:
            payload = row['embed_json']
            if isinstance(payload, str):
                payload = json.loads(payload)
            content = row.get('content', '')
            
            unix_time = row['schedule_for_utc']
            time_str = f"<t:{unix_time}:F> (<t:{unix_time}:R>)"
            
            embeds = [discord.Embed.from_dict(copy.deepcopy(e)) for e in payload.get("embeds", [])]
            
            # Prepend preview warning
            if content:
                content = f"**[PREVIEW FOR `{identifier}`]**\n**Scheduled for:** {time_str}\n\n{content}"
            else:
                content = f"**[PREVIEW FOR `{identifier}`]**\n**Scheduled for:** {time_str}"
                
            from utils.views import ManageEmbedActionView
            action_view = ManageEmbedActionView(identifier, self, interaction.user)
            
            await interaction.response.edit_message(content=content, embeds=embeds, view=action_view)
        except Exception as e:
            await interaction.response.edit_message(content=f"❌ Error previewing embed: `{e}`", embeds=[], view=None)
            
    async def cancel_scheduled_embed_action(self, interaction: discord.Interaction, identifier: str):
        """Actually cancel the embed (called from the view)."""
        self._load_data()
        if identifier in self.scheduled_data:
            self.scheduled_data.pop(identifier, None)
            self._save_data()
            await interaction.response.edit_message(content=f"🗑️ Cancelled and deleted scheduled embed `{identifier}`.", embeds=[], view=None)
        else:
            await interaction.response.edit_message(content="❌ Could not find that embed.", embeds=[], view=None)

    async def post_scheduled_embed_action(self, interaction: discord.Interaction, identifier: str):
        """Instantly post a scheduled embed right now."""
        self._load_data()
        row = self.scheduled_data.get(identifier)
        if not row or row.get('status') != 'pending':
            await interaction.response.edit_message(content="❌ Could not find that pending embed.", embeds=[], view=None)
            return
            
        try:
            channel = self.bot.get_channel(row['channel_id'])
            if not channel:
                channel = await self.bot.fetch_channel(row['channel_id'])
                
            payload = row['embed_json']
            if isinstance(payload, str):
                payload = json.loads(payload)
            
            embeds = [discord.Embed.from_dict(copy.deepcopy(e)) for e in payload.get("embeds", [])]
            view = discohook_to_view(payload.get("components", []))
            content = row.get('content')
            safe_content = content if content else None
            
            sent_msg = await channel.send(content=safe_content, embeds=embeds, view=view)
            
            self.scheduled_data.pop(identifier, None)
            self._save_data()
            
            await interaction.response.edit_message(
                content=f"✅ **Sent instantly!** Scheduled embed `{identifier}` has been published to {channel.mention}.",
                embeds=[],
                view=None
            )
        except Exception as e:
            logger.error(f"Failed to force post embed {identifier}: {e}")
            await interaction.response.edit_message(content=f"❌ Error posting embed: `{e}`", embeds=[], view=None)
    
    # ─────────────────────────────────────────────────────────────────────
    # Diagnostics
    # ─────────────────────────────────────────────────────────────────────
    
    @embed_group.command(name="diagnose", description="Run a full diagnostic on the scheduled embed pipeline")
    @app_commands.default_permissions(administrator=True)
    async def diagnose_embeds(self, interaction: discord.Interaction):
        """Diagnose the entire scheduled embed pipeline step by step."""
        await interaction.response.defer(ephemeral=True)
        
        lines = []
        lines.append("# 🔍 Embed Pipeline Diagnostic Report\n")
        
        # ── Step 1: Storage File ──
        lines.append("## 1️⃣ Storage File")
        lines.append(f"**Path:** `{self.storage_file}`")
        
        file_exists = os.path.exists(self.storage_file)
        lines.append(f"**Exists on disk:** {'✅ Yes' if file_exists else '❌ No'}")
        
        if file_exists:
            try:
                stat = os.stat(self.storage_file)
                lines.append(f"**File size:** {stat.st_size} bytes")
                mod_time = datetime.datetime.fromtimestamp(stat.st_mtime, tz=pytz.UTC).astimezone(TZ_MANILA)
                lines.append(f"**Last modified:** {mod_time.strftime('%d/%m/%Y %H:%M:%S')} (UTC+8)")
            except OSError as e:
                lines.append(f"**Stat error:** `{e}`")
            
            try:
                with open(self.storage_file, 'r') as f:
                    raw = f.read()
                disk_data = json.loads(raw) if raw.strip() else {}
                lines.append(f"**Parseable JSON:** ✅ Yes")
                lines.append(f"**Entries on disk:** {len(disk_data)}")
            except Exception as e:
                lines.append(f"**Parseable JSON:** ❌ No — `{e}`")
                disk_data = {}
        else:
            lines.append("⚠️ The file does not exist yet. It will be created when you schedule an embed.")
            disk_data = {}
        
        # Check write permissions
        try:
            test_path = self.storage_file + ".diag_test"
            with open(test_path, 'w') as f:
                f.write("test")
            os.remove(test_path)
            lines.append(f"**Write permissions:** ✅ Writable")
        except OSError as e:
            lines.append(f"**Write permissions:** ❌ Cannot write — `{e}`")
        
        # ── Step 2: In-Memory State ──
        lines.append("\n## 2️⃣ In-Memory State")
        lines.append(f"**Entries in memory:** {len(self.scheduled_data)}")
        
        pending = [k for k, v in self.scheduled_data.items() if v.get('status') == 'pending']
        processing = [k for k, v in self.scheduled_data.items() if v.get('status') == 'processing']
        other = [k for k, v in self.scheduled_data.items() if v.get('status') not in ('pending', 'processing')]
        
        lines.append(f"**Pending:** {len(pending)}")
        lines.append(f"**Processing (stuck?):** {len(processing)}")
        if other:
            lines.append(f"**Other statuses:** {len(other)}")
        
        # Memory vs disk sync check
        if file_exists:
            mem_keys = set(self.scheduled_data.keys())
            disk_keys = set(disk_data.keys())
            if mem_keys == disk_keys:
                lines.append("**Memory ↔ Disk sync:** ✅ In sync")
            else:
                only_mem = mem_keys - disk_keys
                only_disk = disk_keys - mem_keys
                lines.append("**Memory ↔ Disk sync:** ⚠️ Out of sync")
                if only_mem:
                    lines.append(f"  - Only in memory: `{', '.join(only_mem)}`")
                if only_disk:
                    lines.append(f"  - Only on disk: `{', '.join(only_disk)}`")
        
        # ── Step 3: Schedule Loop ──
        lines.append("\n## 3️⃣ Schedule Loop")
        loop = self.schedule_loop
        lines.append(f"**Running:** {'✅ Yes' if loop.is_running() else '❌ No'}")
        lines.append(f"**Failed:** {'⚠️ Yes' if loop.failed() else '✅ No'}")
        lines.append(f"**Current loop count:** {loop.current_loop}")
        
        if loop.next_iteration:
            next_iter_utc = loop.next_iteration
            next_iter_manila = next_iter_utc.astimezone(TZ_MANILA)
            lines.append(f"**Next iteration:** {next_iter_manila.strftime('%d/%m/%Y %H:%M:%S')} (UTC+8)")
        else:
            lines.append("**Next iteration:** ❌ Not scheduled")
        
        exc = loop.get_task().exception() if loop.get_task() and loop.get_task().done() else None
        if exc:
            lines.append(f"**Last exception:** `{exc}`")
        
        # ── Step 4: Current Time Comparison ──
        lines.append("\n## 4️⃣ Time Check")
        now_utc = datetime.datetime.now(pytz.UTC)
        now_manila = now_utc.astimezone(TZ_MANILA)
        now_unix = int(now_utc.timestamp())
        lines.append(f"**Current UTC:** {now_utc.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"**Current Manila:** {now_manila.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"**Current Unix:** `{now_unix}`")
        
        # ── Step 5: Per-entry Detail ──
        lines.append("\n## 5️⃣ Scheduled Entries Detail")
        
        if not self.scheduled_data:
            lines.append("*No entries.*")
        else:
            for ident, entry in self.scheduled_data.items():
                sched_unix = entry.get('schedule_for_utc', 0)
                status = entry.get('status', 'unknown')
                channel_id = entry.get('channel_id')
                user_id = entry.get('user_id')
                has_embeds = bool(entry.get('embed_json', {}).get('embeds'))
                
                sched_dt = datetime.datetime.fromtimestamp(sched_unix, tz=pytz.UTC).astimezone(TZ_MANILA)
                delta = sched_unix - now_unix
                
                if delta > 0:
                    mins_left = delta // 60
                    time_status = f"⏳ in {mins_left}m"
                else:
                    mins_overdue = abs(delta) // 60
                    time_status = f"🔴 OVERDUE by {mins_overdue}m"
                
                lines.append(f"\n**`{ident}`** — Status: `{status}` — {time_status}")
                lines.append(f"  Scheduled: {sched_dt.strftime('%d/%m/%Y %H:%M')} (Unix: `{sched_unix}`)")
                lines.append(f"  Channel: <#{channel_id}> — User: <@{user_id}>")
                lines.append(f"  Has embeds: {'✅' if has_embeds else '❌'}")
                
                # Verify channel is accessible
                try:
                    ch = self.bot.get_channel(channel_id)
                    if ch:
                        lines.append(f"  Channel accessible: ✅ #{ch.name}")
                    else:
                        ch = await self.bot.fetch_channel(channel_id)
                        lines.append(f"  Channel accessible: ✅ #{ch.name} (fetched)")
                except Exception as e:
                    lines.append(f"  Channel accessible: ❌ `{e}`")
        
        # ── Step 6: Log Channel ──
        lines.append("\n## 6️⃣ Log Channel")
        try:
            log_row = await db.fetch_one(
                "SELECT embed_log_channel_id FROM guild_settings WHERE guild_id = %s",
                (interaction.guild.id,)
            )
            if log_row and log_row.get('embed_log_channel_id'):
                log_ch_id = log_row['embed_log_channel_id']
                log_ch = self.bot.get_channel(log_ch_id)
                if log_ch:
                    lines.append(f"**Configured:** ✅ {log_ch.mention}")
                else:
                    lines.append(f"**Configured:** ⚠️ ID `{log_ch_id}` but channel not found in cache")
            else:
                lines.append("**Configured:** ❌ No log channel set (use `/embed logs`)")
        except Exception as e:
            lines.append(f"**DB query error:** `{e}`")
        
        # ── Step 7: Working Directory ──
        lines.append("\n## 7️⃣ Environment")
        lines.append(f"**CWD:** `{os.getcwd()}`")
        lines.append(f"**Bot code dir:** `{STORAGE_DIR}`")
        lines.append(f"**__file__:** `{__file__}`")
        
        # Send the report
        report = "\n".join(lines)
        
        # Discord has a 2000 char limit — send as file if too long
        if len(report) > 1900:
            buffer = BytesIO(report.encode("utf-8"))
            buffer.seek(0)
            await interaction.followup.send(
                content="📋 Diagnostic report attached below:",
                file=discord.File(fp=buffer, filename="embed_diagnostic.md"),
                ephemeral=True
            )
        else:
            await interaction.followup.send(report, ephemeral=True)
    
    @embed_group.command(name="logs", description="Set the channel for scheduled embed logs")
    @app_commands.describe(channel="Channel for embed logs")
    async def set_embed_log(self, interaction: discord.Interaction, channel: discord.TextChannel):
        """Configure where scheduled embed logs are sent."""
        await db.execute(
            """INSERT INTO guild_settings (guild_id, embed_log_channel_id) 
               VALUES (%s, %s) 
               ON DUPLICATE KEY UPDATE embed_log_channel_id = VALUES(embed_log_channel_id)""",
            (interaction.guild.id, channel.id)
        )
        await interaction.response.send_message(
            f"✅ Scheduled embed logs will be sent to {channel.mention}.",
            ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(EmbedsCog(bot))
