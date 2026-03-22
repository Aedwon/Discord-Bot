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
from urllib.parse import urlparse, parse_qs, quote
from io import BytesIO
import pytz

from services.database import db
from utils.constants import TZ_MANILA
from utils.views import CancelScheduledEmbedView

logger = logging.getLogger('mlbb_bot')


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
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
    
    async def cog_load(self):
        """Start background task when cog is loaded."""
        self.schedule_loop.start()
    
    def cog_unload(self):
        self.schedule_loop.cancel()
    
    # ─────────────────────────────────────────────────────────────────────
    # Background Task: Send Scheduled Embeds
    # ─────────────────────────────────────────────────────────────────────
    
    @tasks.loop(minutes=1)
    async def schedule_loop(self):
        """Check and send pending scheduled embeds."""
        query = """
            SELECT identifier, channel_id, user_id, content, embed_json 
            FROM scheduled_embeds 
            WHERE status = 'pending' AND schedule_for <= NOW()
        """
        rows = await db.fetch_all(query)
        
        for row in rows:
            try:
                channel = self.bot.get_channel(row['channel_id'])
                if not channel:
                    channel = await self.bot.fetch_channel(row['channel_id'])
                
                data = json.loads(row['embed_json'])
                embeds = [discord.Embed.from_dict(e) for e in data.get("embeds", [])]
                view = discohook_to_view(data.get("components", []))
                content = row['content']
                
                sent_msg = await channel.send(content=content, embeds=embeds, view=view)
                message_link = f"https://discord.com/channels/{channel.guild.id}/{channel.id}/{sent_msg.id}"
                
                # Mark as sent
                await db.execute(
                    "UPDATE scheduled_embeds SET status = 'sent' WHERE identifier = %s",
                    (row['identifier'],)
                )
                
                # Log success
                log_row = await db.fetch_one(
                    "SELECT embed_log_channel_id FROM guild_settings WHERE guild_id = %s",
                    (channel.guild.id,)
                )
                if log_row and log_row.get('embed_log_channel_id'):
                    log_channel = self.bot.get_channel(log_row['embed_log_channel_id'])
                    if log_channel:
                        embed = discord.Embed(
                            title="✅ Scheduled Embed Sent",
                            color=0x00FF00,
                            timestamp=datetime.datetime.now(TZ_MANILA)
                        )
                        embed.add_field(name="Identifier", value=f"`{row['identifier']}`")
                        embed.add_field(name="Channel", value=channel.mention)
                        embed.add_field(name="User", value=f"<@{row['user_id']}>")
                        embed.add_field(name="Link", value=f"[Jump to Message]({message_link})", inline=False)
                        await log_channel.send(content=f"<@{row['user_id']}>", embed=embed)
            
            except Exception as e:
                logger.error(f"Failed to send scheduled embed {row['identifier']}: {e}")
                await db.execute(
                    "UPDATE scheduled_embeds SET status = 'failed' WHERE identifier = %s",
                    (row['identifier'],)
                )
    
    @schedule_loop.before_loop
    async def before_loop(self):
        await self.bot.wait_until_ready()
    
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
    
    @app_commands.command(name="send_embed", description="Send an embed from a Discohook link")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        channel="Channel to send the embed to",
        link="Discohook share link (if under 512 chars)",
        long_link="Alternative: Paste the full Discohook link here if it's too long",
        schedule_for="(Optional) Date and time to send (DD/MM/YYYY HH:MM, UTC+8)"
    )
    async def send_embed(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        link: str | None = None,
        long_link: str | None = None,
        schedule_for: str | None = None
    ):
        """Send or schedule an embed from a Discohook link."""
        await interaction.response.defer(ephemeral=True)
        final_link = long_link or link
        if not final_link:
            await interaction.followup.send("❌ Please provide a Discohook link.", ephemeral=True)
            return
            
        data = await self._process_link(final_link, interaction)
        if not data:
            return
        
        content = data.get("content", "")
        embeds_list = data.get("embeds", [])
        components_list = data.get("components", [])

        embeds = [discord.Embed.from_dict(e) for e in embeds_list]
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
            full_json = json.dumps({"embeds": embeds_list, "components": components_list})
            
            # Convert TZ aware datetime to UTC format or string equivalent for DB
            # Depending on DB timezone string is easiest:
            dt_str = dt.strftime("%Y-%m-%d %H:%M:%S")

            await db.execute(
                """INSERT INTO scheduled_embeds 
                   (identifier, channel_id, user_id, content, embed_json, schedule_for, status) 
                   VALUES (%s, %s, %s, %s, %s, %s, 'pending')""",
                (identifier, channel.id, interaction.user.id, content, full_json, dt_str)
            )
            
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
            sent_msg = await channel.send(content=content, embeds=embeds, view=view)
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
    
    @app_commands.command(name="edit_embed", description="Edit an existing message using a Discohook link.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        message_link="The message link to edit",
        link="Short Discohook link (if under 512 characters)",
        long_link="Alternative: Paste the full Discohook link here if it's too long",
    )
    async def edit_embed(
        self, interaction: discord.Interaction,
        message_link: str,
        link: str | None = None,
        long_link: str | None = None,
    ):
        await interaction.response.defer(ephemeral=True)
        final_link = long_link or link
        if not final_link:
            await interaction.followup.send("❌ No Discohook link provided.", ephemeral=True)
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

            new_embeds = [discord.Embed.from_dict(e) for e in embeds_list]
            new_view = discohook_to_view(components_list)

            if target_message.author.id == self.bot.user.id:
                await target_message.edit(content=content, embeds=new_embeds, view=new_view)
                await interaction.followup.send(
                    f"✅ Edited: [Jump to Message]({message_link})", ephemeral=True,
                )
                return

            if target_message.webhook_id:
                webhooks = await channel.webhooks()
                webhook = next((w for w in webhooks if w.id == target_message.webhook_id), None)
                if webhook and webhook.token:
                    await webhook.edit_message(
                        message_id=target_message.id, content=content, embeds=new_embeds,
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

    @app_commands.command(name="dl_embed", description="Generate a Discohook link from a Discord message.")
    @app_commands.default_permissions(administrator=True)
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

    @app_commands.command(name="cancel_embed", description="Cancel a scheduled embed")
    @app_commands.default_permissions(administrator=True)
    async def cancel_embed(self, interaction: discord.Interaction):
        """Cancel a pending scheduled embed."""
        rows = await db.fetch_all(
            "SELECT identifier, schedule_for FROM scheduled_embeds WHERE user_id = %s AND status = 'pending'",
            (interaction.user.id,)
        )
        
        if not rows:
            await interaction.response.send_message("❌ You have no pending scheduled embeds.", ephemeral=True)
            return
        
        view = CancelScheduledEmbedView(rows, self, interaction.user)
        await interaction.response.send_message("Select an embed to cancel:", view=view, ephemeral=True)
    
    async def cancel_scheduled_embed_action(self, interaction: discord.Interaction, identifier: str):
        """Actually cancel the embed (called from the view)."""
        await db.execute("DELETE FROM scheduled_embeds WHERE identifier = %s", (identifier,))
        await interaction.response.send_message(f"✅ Cancelled scheduled embed `{identifier}`.", ephemeral=True)
    
    @app_commands.command(name="set_embed_log", description="Set the channel for scheduled embed logs")
    @app_commands.default_permissions(administrator=True)
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
