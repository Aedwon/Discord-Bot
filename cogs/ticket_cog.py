"""
Ticket System Cog.
Persistent ticket panel, category-based routing, claim/close flow,
HTML transcripts, 1-5 star ratings, 24h/48h escalation.
"""

import discord
from discord.ext import commands, tasks
from discord import app_commands
import datetime
import io
import html as html_mod
import re
import json
import logging
import pytz

from services.database import db
from services.settings_service import settings_service

logger = logging.getLogger("mlbb_bot.tickets")

TZ_MANILA = pytz.timezone("Asia/Manila")

# ────────────────────────────────────────────────────────────────
# Categories
# ────────────────────────────────────────────────────────────────

TICKET_CATEGORIES = {
    "CS": {
        "label": "Community Support",
        "tag": "cs",
        "emoji": "💬",
        "desc": "Role issues, server questions, verification help",
        "role_key": "ticket_role_cs",
    },
    "TC": {
        "label": "Bot & Technical",
        "tag": "tc",
        "emoji": "🔧",
        "desc": "Bot bugs, XP/EP issues, command problems",
        "role_key": "ticket_role_tc",
    },
    "EV": {
        "label": "Events & Activities",
        "tag": "ev",
        "emoji": "🎮",
        "desc": "Event inquiries, quiz issues, EP disputes",
        "role_key": "ticket_role_ev",
    },
    "RF": {
        "label": "Reports & Feedback",
        "tag": "rf",
        "emoji": "📝",
        "desc": "User reports, rule violations, suggestions",
        "role_key": "ticket_role_rf",
    },
}


# ────────────────────────────────────────────────────────────────
# HTML Transcript Generator
# ────────────────────────────────────────────────────────────────

def generate_html_transcript(messages: list[discord.Message], channel_name: str) -> str:
    style = """
    <style>
        body { font-family: 'gg sans', 'Helvetica Neue', Helvetica, Arial, sans-serif; background-color: #313338; color: #dbdee1; margin: 0; padding: 20px; }
        .header { border-bottom: 1px solid #3f4147; padding-bottom: 10px; margin-bottom: 20px; }
        .header h1 { color: #f2f3f5; margin: 0; font-size: 20px; }
        .chat-container { display: block; width: 100%; }
        .message-group { display: flex; margin-bottom: 16px; align-items: flex-start; width: 100%; }
        .avatar { width: 40px; height: 40px; border-radius: 50%; margin-right: 16px; flex-shrink: 0; background-color: #2b2d31; }
        .content { flex: 1; }
        .meta { display: flex; align-items: baseline; margin-bottom: 4px; }
        .username { font-weight: 500; color: #f2f3f5; margin-right: 8px; font-size: 16px; }
        .bot-tag { background-color: #5865f2; color: #fff; font-size: 10px; padding: 1px 4px; border-radius: 3px; vertical-align: middle; margin-left: 4px; }
        .timestamp { font-size: 12px; color: #949ba4; }
        .text { font-size: 16px; line-height: 1.375rem; white-space: pre-wrap; word-wrap: break-word; color: #dbdee1; }
        .text strong { font-weight: 700; color: #f2f3f5; }
        .text em { font-style: italic; }
        .text .mention { background-color: #3c4270; color: #c9cdfb; padding: 0 2px; border-radius: 3px; font-weight: 500; }
        .attachment { margin-top: 8px; }
        .attachment img { max-width: 400px; max-height: 300px; border-radius: 4px; }
        a { color: #00a8fc; text-decoration: none; }
        .embed { display: flex; max-width: 520px; background-color: #2b2d31; border-radius: 4px; border-left: 4px solid #202225; margin-top: 8px; font-size: 14px; }
        .embed-content { padding: 12px 16px; width: 100%; }
        .embed-title { color: #f2f3f5; margin: 0 0 8px; font-weight: 600; font-size: 16px; }
        .embed-desc { color: #dbdee1; line-height: 1.375rem; white-space: pre-wrap; margin-bottom: 8px; }
        .embed-fields { display: grid; grid-template-columns: auto auto; grid-gap: 8px; margin-top: 8px; }
        .embed-field-name { color: #f2f3f5; font-weight: 600; margin-bottom: 2px; }
        .embed-field-value { color: #dbdee1; white-space: pre-wrap; }
        .embed-footer { margin-top: 8px; font-size: 12px; color: #949ba4; }
    </style>
    """

    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>Transcript - {channel_name}</title>
        {style}
    </head>
    <body>
        <div class="header">
            <h1>#{channel_name}</h1>
            <p>Transcript generated on {datetime.datetime.now(TZ_MANILA).strftime('%Y-%m-%d %H:%M:%S')} (PHT)</p>
        </div>
        <div class="chat-container">
    """

    guild = messages[0].guild if messages else None

    for msg in messages:
        try:
            avatar_url = msg.author.display_avatar.url if msg.author.display_avatar else "https://cdn.discordapp.com/embed/avatars/0.png"
            username = html_mod.escape(msg.author.display_name)
            try:
                created_at_pht = msg.created_at.astimezone(TZ_MANILA)
            except Exception:
                created_at_pht = msg.created_at
            timestamp = created_at_pht.strftime('%m/%d/%Y %I:%M %p')

            content = html_mod.escape(msg.content or "")

            # Replace mentions
            def replace_user(match):
                uid = int(match.group(1))
                name = f"@{uid}"
                if guild:
                    member = guild.get_member(uid)
                    if member:
                        name = f"@{member.display_name}"
                return f'<span class="mention">{html_mod.escape(name)}</span>'
            content = re.sub(r'&lt;@!?(\d+)&gt;', replace_user, content)

            def replace_role(match):
                rid = int(match.group(1))
                name = f"@{rid}"
                if guild:
                    role = guild.get_role(rid)
                    if role:
                        name = f"@{role.name}"
                return f'<span class="mention">{html_mod.escape(name)}</span>'
            content = re.sub(r'&lt;@&amp;(\d+)&gt;', replace_role, content)

            def replace_channel(match):
                cid = int(match.group(1))
                name = f"#{cid}"
                if guild:
                    chan = guild.get_channel(cid)
                    if chan:
                        name = f"#{chan.name}"
                return f'<span class="mention">{html_mod.escape(name)}</span>'
            content = re.sub(r'&lt;#(\d+)&gt;', replace_channel, content)

            # Markdown
            content = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', content)
            content = re.sub(r'\*(.*?)\*', r'<em>\1</em>', content)
            content = content.replace("@everyone", '<span class="mention">@everyone</span>')
            content = content.replace("@here", '<span class="mention">@here</span>')

            bot_tag = '<span class="bot-tag">BOT</span>' if msg.author.bot else ''

            attachments_html = ""
            if msg.attachments:
                for att in msg.attachments:
                    if att.content_type and att.content_type.startswith('image/'):
                        attachments_html += f'<div class="attachment"><a href="{att.url}" target="_blank"><img src="{att.url}" alt="Attachment"></a></div>'
                    else:
                        attachments_html += f'<div class="attachment"><a href="{att.url}" target="_blank">📄 {html_mod.escape(att.filename)}</a></div>'

            embeds_html = ""
            if msg.embeds:
                for embed in msg.embeds:
                    color = f"#{embed.color.value:06x}" if embed.color else "#202225"
                    title_html = f'<div class="embed-title">{html_mod.escape(embed.title)}</div>' if embed.title else ""
                    desc_html = f'<div class="embed-desc">{html_mod.escape(embed.description)}</div>' if embed.description else ""
                    fields_html = ""
                    if embed.fields:
                        fields_html = '<div class="embed-fields">'
                        for field in embed.fields:
                            fields_html += f'<div class="embed-field"><div class="embed-field-name">{html_mod.escape(field.name)}</div><div class="embed-field-value">{html_mod.escape(field.value)}</div></div>'
                        fields_html += '</div>'
                    embeds_html += f'<div class="embed" style="border-left-color: {color};"><div class="embed-content">{title_html}{desc_html}{fields_html}</div></div>'

            html_content += f"""
            <div class="message-group">
                <img class="avatar" src="{avatar_url}" alt="{username}">
                <div class="content">
                    <div class="meta">
                        <span class="username">{username}</span>
                        {bot_tag}
                        <span class="timestamp">{timestamp}</span>
                    </div>
                    <div class="text">{content}</div>
                    {attachments_html}
                    {embeds_html}
                </div>
            </div>
            """
        except Exception as e:
            logger.error(f"Error processing message {msg.id}: {e}")
            continue

    html_content += """
        </div>
    </body>
    </html>
    """
    return html_content


# ────────────────────────────────────────────────────────────────
# UI Components
# ────────────────────────────────────────────────────────────────

class TicketTopicSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(
                label=data["label"], description=data["desc"],
                emoji=data["emoji"], value=key,
            )
            for key, data in TICKET_CATEGORIES.items()
        ]
        super().__init__(
            placeholder="Select the category of your concern...",
            min_values=1, max_values=1,
            custom_id="ticket_category_select", options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        selected_key = self.values[0]
        category_data = TICKET_CATEGORIES[selected_key]
        await interaction.response.send_modal(TicketModal(selected_key, category_data))


class TicketTopicView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(TicketTopicSelect())


class TicketCreateView(discord.ui.View):
    """Persistent "Create Ticket" button — survives bot restarts."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📩 Create Ticket", style=discord.ButtonStyle.primary, custom_id="create_ticket_base")
    async def create_start(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Please select the category below:", view=TicketTopicView(), ephemeral=True
        )


class TicketModal(discord.ui.Modal):
    def __init__(self, category_key: str, category_data: dict):
        super().__init__(title=f"New {category_data['label']} Ticket")
        self.category_key = category_key
        self.category_data = category_data

        self.ticket_subject = discord.ui.TextInput(
            label="Subject", placeholder="Briefly state your concern...", max_length=100,
        )
        self.ticket_desc = discord.ui.TextInput(
            label="Description", style=discord.TextStyle.paragraph,
            placeholder="Please provide more details...", max_length=1000,
        )
        self.add_item(self.ticket_subject)
        self.add_item(self.ticket_desc)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        user = interaction.user

        # Resolve ticket category folder
        cat_id = await settings_service.get_int("ticket_category_id")
        category_channel = guild.get_channel(cat_id) if cat_id else None
        if not category_channel:
            category_channel = discord.utils.get(guild.categories, name="🎟⎮tickets")
        if not category_channel:
            await interaction.followup.send(
                "❌ No ticket category configured. Ask an admin to run `/setup ticket_category`.",
                ephemeral=True,
            )
            return

        tag = self.category_data["tag"]
        channel_name = f"[{tag}]-{user.name}"

        # Prevent duplicate tickets
        existing = discord.utils.get(guild.text_channels, name=channel_name)
        if existing:
            await interaction.followup.send(
                f"❌ You already have a ticket of this type open: {existing.mention}", ephemeral=True
            )
            return

        # Build permission overwrites
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, manage_channels=True),
        }

        # Category-specific role
        cat_role_id = await settings_service.get_int(self.category_data["role_key"])
        cat_role = guild.get_role(cat_role_id) if cat_role_id else None
        if cat_role:
            overwrites[cat_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

        # Global support role
        support_role_id = await settings_service.get_int("support_role_id")
        support_role = guild.get_role(support_role_id) if support_role_id else None
        if support_role and support_role != cat_role:
            overwrites[support_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

        try:
            ticket_channel = await guild.create_text_channel(
                channel_name, category=category_channel, overwrites=overwrites
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I don't have permission to create channels in the ticket category.", ephemeral=True
            )
            return
        except discord.HTTPException as e:
            await interaction.followup.send(f"❌ Discord API error: {e}", ephemeral=True)
            return

        # Save to DB
        await db.execute(
            "INSERT INTO active_tickets (channel_id, creator_id, category_key, subject) "
            "VALUES (%s, %s, %s, %s)",
            (ticket_channel.id, user.id, self.category_key, self.ticket_subject.value),
        )

        embed = discord.Embed(
            title=f"{self.category_data['emoji']} {self.category_data['label']}",
            description=f"**Subject:** {self.ticket_subject.value}\n\n{self.ticket_desc.value}",
            color=0xF2C21A,
        )
        embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)

        mentions = []
        if cat_role:
            mentions.append(cat_role.mention)
        if support_role and support_role != cat_role:
            mentions.append(support_role.mention)
        mention_text = " ".join(mentions)
        
        try:
            await ticket_channel.send(
                content=f"{user.mention} {mention_text}",
                embed=embed, view=TicketActionsView(),
            )
        except Exception:
            pass

        await interaction.followup.send(f"✅ Ticket created: {ticket_channel.mention}", ephemeral=True)


# ── Ticket Action Buttons ──────────────────────────────────────

class TicketActionsView(discord.ui.View):
    """Persistent action buttons inside a ticket channel."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🛠 Claim", style=discord.ButtonStyle.success, custom_id="ticket:claim")
    async def claim_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cid = interaction.channel_id
        ticket = await db.fetch_one("SELECT * FROM active_tickets WHERE channel_id = %s", (cid,))
        if not ticket:
            return await interaction.followup.send("❌ Ticket data not found.", ephemeral=True)
        if ticket["claimed"]:
            button.disabled = True
            button.label = "Already Claimed"
            await interaction.message.edit(view=self)
            return await interaction.followup.send("❌ Already claimed.", ephemeral=True)

        user = interaction.user

        # Block creator from claiming
        if user.id == ticket["creator_id"]:
            return await interaction.followup.send("❌ You cannot claim your own ticket.", ephemeral=True)

        # Block added users
        added = json.loads(ticket["added_users"]) if ticket.get("added_users") else []
        if user.id in added:
            return await interaction.followup.send("❌ Added users cannot claim tickets.", ephemeral=True)

        # Role validation
        allowed = user.guild_permissions.administrator
        if not allowed:
            cat_data = TICKET_CATEGORIES.get(ticket["category_key"])
            if cat_data:
                role_id = await settings_service.get_int(cat_data["role_key"])
                role = interaction.guild.get_role(role_id) if role_id else None
                if role and role in user.roles:
                    allowed = True
            # Support role can always claim
            support_id = await settings_service.get_int("support_role_id")
            support = interaction.guild.get_role(support_id) if support_id else None
            if support and support in user.roles:
                allowed = True

        if not allowed:
            return await interaction.followup.send("❌ You don't have permission to claim this ticket.", ephemeral=True)

        await db.execute(
            "UPDATE active_tickets SET claimed = TRUE, claimed_by = %s WHERE channel_id = %s",
            (user.id, cid),
        )
        button.disabled = True
        button.label = f"Claimed by {user.display_name}"
        await interaction.message.edit(view=self)
        embed = discord.Embed(description=f"✅ **Ticket claimed by {user.mention}**", color=0x00FF00)
        await interaction.channel.send(content=user.mention, embed=embed)

    @discord.ui.button(label="🔄 Move", style=discord.ButtonStyle.secondary, custom_id="ticket:move")
    async def move_category(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Select the new category:", view=MoveCategoryView(), ephemeral=True)

    @discord.ui.button(label="👥 Add User", style=discord.ButtonStyle.secondary, custom_id="ticket:add_user")
    async def add_user(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Select users to add:", view=AddUserView(), ephemeral=True)

    @discord.ui.button(label="🚫 Remove User", style=discord.ButtonStyle.secondary, custom_id="ticket:remove_user")
    async def remove_user(self, interaction: discord.Interaction, button: discord.ui.Button):
        ticket = await db.fetch_one(
            "SELECT added_users FROM active_tickets WHERE channel_id = %s", (interaction.channel_id,)
        )
        added = json.loads(ticket["added_users"]) if ticket and ticket.get("added_users") else []
        if not added:
            return await interaction.response.send_message("❌ No users have been added.", ephemeral=True)
        await interaction.response.send_message("Select users to remove:", view=RemoveUserView(added), ephemeral=True)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, custom_id="ticket:close")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Please select a reason for closing:", view=CloseReasonView(), ephemeral=True
        )


# ── Add / Remove User Views ───────────────────────────────────

class AddUserView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Select users to add...", min_values=1, max_values=5)
    async def select_users(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        await interaction.response.defer()
        cid = interaction.channel_id
        ticket = await db.fetch_one("SELECT added_users FROM active_tickets WHERE channel_id = %s", (cid,))
        current = json.loads(ticket["added_users"]) if ticket and ticket.get("added_users") else []

        added_mentions = []
        for user in select.values:
            if user.bot:
                continue
            if user.id not in current:
                current.append(user.id)
            try:
                await interaction.channel.set_permissions(user, view_channel=True, send_messages=True)
                added_mentions.append(user.mention)
            except Exception:
                pass

        await db.execute("UPDATE active_tickets SET added_users = %s WHERE channel_id = %s", (json.dumps(current), cid))

        if added_mentions:
            await interaction.followup.send("✅ Users added.")
            await interaction.channel.send(f"👥 **{', '.join(added_mentions)}** have been added to the ticket.")
        else:
            await interaction.followup.send("No valid users selected.", ephemeral=True)
        self.stop()


class RemoveUserView(discord.ui.View):
    def __init__(self, allowed_ids: list):
        super().__init__(timeout=60)
        self.allowed_ids = allowed_ids

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Select users to remove...", min_values=1, max_values=5)
    async def select_remove(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        await interaction.response.defer()
        cid = interaction.channel_id
        ticket = await db.fetch_one("SELECT added_users FROM active_tickets WHERE channel_id = %s", (cid,))
        current = json.loads(ticket["added_users"]) if ticket and ticket.get("added_users") else []

        removed = []
        for user in select.values:
            if user.id in self.allowed_ids and user.id in current:
                try:
                    await interaction.channel.set_permissions(user, overwrite=None)
                    current.remove(user.id)
                    removed.append(user.display_name)
                except Exception:
                    pass

        await db.execute("UPDATE active_tickets SET added_users = %s WHERE channel_id = %s", (json.dumps(current), cid))

        if removed:
            await interaction.followup.send(f"🚫 Removed: {', '.join(removed)}")
        else:
            await interaction.followup.send("❌ Selected user wasn't in the added list.", ephemeral=True)
        self.stop()


# ── Move Category ──────────────────────────────────────────────

class MoveCategorySelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=data["label"], emoji=data["emoji"], value=key)
            for key, data in TICKET_CATEGORIES.items()
        ]
        super().__init__(placeholder="Select new category...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        new_key = self.values[0]
        new_cat = TICKET_CATEGORIES[new_key]
        cid = interaction.channel_id

        ticket = await db.fetch_one("SELECT * FROM active_tickets WHERE channel_id = %s", (cid,))
        old_key = ticket["category_key"] if ticket else None

        # Determine creator name for channel rename
        creator_name = "unknown"
        if ticket and ticket.get("creator_id"):
            mem = interaction.guild.get_member(ticket["creator_id"])
            creator_name = mem.name if mem else "unknown"
        if creator_name == "unknown":
            parts = interaction.channel.name.split("-", 1)
            if len(parts) > 1:
                creator_name = parts[1]

        overwrites = interaction.channel.overwrites.copy()
        guild = interaction.guild

        # Add new category role
        new_role_id = await settings_service.get_int(new_cat["role_key"])
        new_role = guild.get_role(new_role_id) if new_role_id else None
        if new_role:
            overwrites[new_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

        # Remove old category role (if different)
        if old_key and old_key != new_key:
            old_cat = TICKET_CATEGORIES.get(old_key)
            if old_cat:
                old_role_id = await settings_service.get_int(old_cat["role_key"])
                if old_role_id and old_role_id != new_role_id:
                    old_role = guild.get_role(old_role_id)
                    if old_role:
                        overwrites.pop(old_role, None)

        new_name = f"[{new_cat['tag']}]-{creator_name}"
        msg = f"✅ Ticket moved to **{new_cat['label']}**.\n"

        try:
            await interaction.channel.edit(name=new_name, overwrites=overwrites)
        except Exception as e:
            if "429" in str(e):
                msg += "⚠️ Channel rename skipped (rate limit). Try again later."
            else:
                msg += f"⚠️ Failed to update channel: {e}"

        if ticket:
            await db.execute(
                "UPDATE active_tickets SET category_key = %s WHERE channel_id = %s", (new_key, cid)
            )

        msg += f"Pinged: {new_role.mention if new_role else 'None'}"
        await interaction.followup.send(msg)


class MoveCategoryView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        self.add_item(MoveCategorySelect())


# ── Close Reason Flow ──────────────────────────────────────────

class CloseReasonSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Solved / Addressed", emoji="✅", value="Solved"),
            discord.SelectOption(label="Assistance Provided", emoji="🤝", value="Assistance Provided"),
            discord.SelectOption(label="Duplicate Ticket", emoji="📄", value="Duplicate"),
            discord.SelectOption(label="Invalid / Spam", emoji="🚫", value="Invalid"),
            discord.SelectOption(label="Inactivity", emoji="💤", value="Inactivity"),
            discord.SelectOption(label="Other", emoji="🔌", value="Other"),
        ]
        super().__init__(placeholder="Select a reason...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(CloseReasonModal(self.values[0]))


class CloseReasonModal(discord.ui.Modal):
    def __init__(self, reason: str):
        super().__init__(title=f"Closing: {reason}")
        self.reason = reason
        self.remarks = discord.ui.TextInput(
            label="Additional Remarks (Optional)",
            style=discord.TextStyle.paragraph,
            placeholder="Any details? Leave blank if none.",
            required=False, max_length=500,
        )
        self.add_item(self.remarks)

    async def on_submit(self, interaction: discord.Interaction):
        await _finish_closure(interaction, self.reason, self.remarks.value)


class CloseReasonView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        self.add_item(CloseReasonSelect())


# ── Rating System ──────────────────────────────────────────────

class FeedbackModal(discord.ui.Modal):
    def __init__(self, stars: int, pending_id: int):
        super().__init__(title=f"You rated {stars} Stars!")
        self.stars = stars
        self.pending_id = pending_id
        self.remarks = discord.ui.TextInput(
            label="Any comments? (Optional)",
            style=discord.TextStyle.paragraph,
            placeholder="Let us know how we can improve...",
            required=False, max_length=1000,
        )
        self.add_item(self.remarks)

    async def on_submit(self, interaction: discord.Interaction):
        pending = await db.fetch_one("SELECT * FROM pending_ratings WHERE id = %s", (self.pending_id,))
        if not pending:
            return await interaction.response.edit_message(
                content="This rating has already been submitted or expired.", view=None, embed=None,
            )

        await interaction.response.edit_message(
            content=f"✅ Thank you! You rated us **{self.stars}/5** ⭐", view=None, embed=None,
        )

        await db.execute("DELETE FROM pending_ratings WHERE id = %s", (self.pending_id,))

        if bool(pending.get("is_test")):
            return

        # Log
        log_channel_id = await settings_service.get_int("ticket_log_channel_id")
        if log_channel_id:
            log_ch = interaction.client.get_channel(log_channel_id)
            if log_ch:
                embed = discord.Embed(title="🌟 New Feedback", color=0xFFD700, timestamp=datetime.datetime.now(TZ_MANILA))
                embed.add_field(name="User", value=interaction.user.mention, inline=True)
                embed.add_field(name="Ticket", value=pending["ticket_name"], inline=True)
                embed.add_field(name="Handler", value=pending.get("handler_mention", "Staff"), inline=True)
                embed.add_field(name="Rating", value=f"{'⭐' * self.stars} ({self.stars}/5)", inline=False)
                if self.remarks.value:
                    embed.add_field(name="Remarks", value=self.remarks.value, inline=False)
                try:
                    await log_ch.send(embed=embed)
                except Exception:
                    pass

        await db.execute(
            "INSERT INTO ticket_ratings (ticket_name, user_id, handler_id, stars, remarks) "
            "VALUES (%s, %s, %s, %s, %s)",
            (pending["ticket_name"], interaction.user.id, pending.get("handler_id"), self.stars, self.remarks.value),
        )


def _make_rating_view(pending_id: int) -> discord.ui.View:
    """Create a rating view with custom_ids encoded with the pending_id."""
    view = discord.ui.View(timeout=None)
    for stars in range(1, 6):
        style = discord.ButtonStyle.success if stars == 5 else discord.ButtonStyle.secondary
        button = discord.ui.Button(
            label=str(stars), emoji="⭐", style=style,
            custom_id=f"rate:{pending_id}:{stars}",
        )
        view.add_item(button)
    return view


# ── Closure Logic ──────────────────────────────────────────────

async def _finish_closure(interaction: discord.Interaction, reason: str, remarks: str):
    await interaction.response.defer()
    cid = interaction.channel_id

    ticket = await db.fetch_one("SELECT * FROM active_tickets WHERE channel_id = %s", (cid,))
    if not ticket:
        return await interaction.followup.send("❌ Ticket already closed.", ephemeral=True)

    creator_id = ticket.get("creator_id")
    added_ids = json.loads(ticket["added_users"]) if ticket.get("added_users") else []

    # Archive into ticket_history BEFORE purging from active_tickets
    try:
        await db.execute(
            "INSERT INTO ticket_history (channel_name, creator_id, category_key, subject, handler_id, close_reason, is_test, created_at, closed_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                interaction.channel.name,
                creator_id,
                ticket.get("category_key", ""),
                ticket.get("subject"),
                ticket.get("claimed_by"),
                reason,
                bool(ticket.get("is_test")),
                ticket.get("created_at"),
                interaction.user.id,
            )
        )
    except Exception as e:
        logger.error(f"Failed to archive ticket to history: {e}")

    # Delete from active tickets
    await db.execute("DELETE FROM active_tickets WHERE channel_id = %s", (cid,))

    # Generate transcript
    messages = [msg async for msg in interaction.channel.history(limit=500, oldest_first=True)]
    html_content = generate_html_transcript(messages, interaction.channel.name)

    # Log embed
    embed = discord.Embed(title="Ticket Closed", color=0xFF0000, timestamp=datetime.datetime.now(TZ_MANILA))
    embed.add_field(name="Ticket", value=interaction.channel.name, inline=True)
    embed.add_field(name="Closed By", value=interaction.user.mention, inline=True)
    embed.add_field(name="Reason", value=reason, inline=True)
    if remarks:
        embed.add_field(name="Remarks", value=remarks, inline=False)

    creator = None
    if creator_id:
        try:
            creator = interaction.guild.get_member(creator_id) or await interaction.client.fetch_user(creator_id)
        except Exception:
            pass

    if creator:
        embed.add_field(name="Creator", value=creator.mention, inline=True)

    log_channel_id = await settings_service.get_int("ticket_log_channel_id")
    if log_channel_id:
        log_ch = interaction.client.get_channel(log_channel_id)
        if log_ch:
            try:
                f = discord.File(io.StringIO(html_content), filename=f"transcript-{interaction.channel.name}.html")
                await log_ch.send(embed=embed, file=f)
            except Exception:
                pass

    # DM creator with transcript + rating
    if creator:
        try:
            dm_embed = discord.Embed(
                title="Ticket Closed",
                description=f"Your ticket `{interaction.channel.name}` has been closed.",
                color=0xF2C21A, timestamp=datetime.datetime.now(TZ_MANILA),
            )
            dm_embed.add_field(name="Reason", value=reason)
            if remarks:
                dm_embed.add_field(name="Remarks", value=remarks)

            f_creator = discord.File(io.StringIO(html_content), filename=f"transcript-{interaction.channel.name}.html")
            await creator.send(embed=dm_embed, file=f_creator)

            # Rating
            handler_id = ticket.get("claimed_by")
            handler_mention = "Staff"
            if handler_id:
                h = interaction.guild.get_member(handler_id)
                handler_mention = h.mention if h else f"<@{handler_id}>"

            is_test = bool(ticket.get("is_test"))
            pending_id = await db.insert_get_id(
                "INSERT INTO pending_ratings (ticket_name, handler_id, handler_mention, is_test) "
                "VALUES (%s, %s, %s, %s)",
                (interaction.channel.name, handler_id, handler_mention, is_test),
            )

            rate_embed = discord.Embed(
                title="How was our service?",
                description=f"Please rate your experience with {handler_mention}.",
                color=0x5865F2,
            )
            if is_test:
                rate_embed.set_footer(text="🧪 Test Mode: Ratings will NOT be recorded.")

            await creator.send(embed=rate_embed, view=_make_rating_view(pending_id))
        except discord.Forbidden:
            pass
        except Exception as e:
            logger.error(f"Error DM'ing ticket creator: {e}")

    # DM added users transcript only
    for uid in added_ids:
        try:
            u = await interaction.client.fetch_user(uid)
            dm_embed = discord.Embed(
                title="Ticket Closed",
                description=f"Ticket `{interaction.channel.name}` has been closed.",
                color=0xF2C21A,
            )
            dm_embed.add_field(name="Reason", value=reason)
            f_added = discord.File(io.StringIO(html_content), filename=f"transcript-{interaction.channel.name}.html")
            await u.send(embed=dm_embed, file=f_added)
        except Exception:
            pass

    try:
        await interaction.channel.delete()
    except discord.NotFound:
        pass
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to delete channel: {e}", ephemeral=True)


# ────────────────────────────────────────────────────────────────
# Cog
# ────────────────────────────────────────────────────────────────

class TicketCog(commands.Cog, name="Tickets"):
    
    ticket_group = app_commands.Group(name="ticket", description="Support ticket management", default_permissions=discord.Permissions(administrator=True))
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.check_reminders.start()

    def cog_unload(self):
        self.check_reminders.cancel()

    async def cog_load(self):
        self.bot.add_view(TicketCreateView())
        self.bot.add_view(TicketActionsView())

    # ── Dynamic rating button listener ─────────────────────────

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        """Catch rating buttons with custom_id pattern: rate:{pending_id}:{stars}"""
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = interaction.data.get("custom_id", "")
        if not custom_id.startswith("rate:"):
            return

        parts = custom_id.split(":")
        if len(parts) != 3:
            return

        try:
            pending_id = int(parts[1])
            stars = int(parts[2])
        except ValueError:
            return

        pending = await db.fetch_one("SELECT * FROM pending_ratings WHERE id = %s", (pending_id,))
        if not pending:
            return await interaction.response.edit_message(
                content="This rating has already been submitted or expired.", view=None, embed=None,
            )

        await interaction.response.send_modal(FeedbackModal(stars, pending_id))

    # ── Admin Commands ─────────────────────────────────────────

    @ticket_group.command(name="deploy", description="Post the ticket panel in a channel.")
    @app_commands.default_permissions(administrator=True)
    async def setup_tickets(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        target = channel or interaction.channel
        embed = discord.Embed(
            title="🎟️ Support Tickets",
            description=(
                "**Need help?**\n\n"
                "Click the button below and select the category that best matches your concern.\n"
                "A private channel will be created for you and our team.\n\n"
                "💬 **Community Support** — roles, verification, server questions\n"
                "🔧 **Bot & Technical** — bot bugs, XP/EP issues\n"
                "🎮 **Events & Activities** — events, quizzes, EP disputes\n"
                "📝 **Reports & Feedback** — user reports, suggestions"
            ),
            color=0xF2C21A,
        )
        embed.set_footer(text="Please don't create duplicate tickets.")
        await target.send(embed=embed, view=TicketCreateView())
        await interaction.response.send_message(f"✅ Ticket panel posted in {target.mention}.", ephemeral=True)

    @ticket_group.command(name="config-category", description="Set the channel category for new tickets.")
    @app_commands.default_permissions(administrator=True)
    async def setup_ticket_category(self, interaction: discord.Interaction, category: discord.CategoryChannel):
        await settings_service.set("ticket_category_id", str(category.id))
        await interaction.response.send_message(
            f"✅ Ticket category set to **{category.name}**.", ephemeral=True
        )

    @ticket_group.command(name="config-roles", description="Map roles to ticket categories.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        category="Which ticket category",
        role="The role that handles this category",
    )
    async def setup_ticket_roles(
        self,
        interaction: discord.Interaction,
        category: str,
        role: discord.Role,
    ):
        cat_data = TICKET_CATEGORIES.get(category)
        if not cat_data:
            return await interaction.response.send_message("❌ Invalid category.", ephemeral=True)
        await settings_service.set(cat_data["role_key"], str(role.id))
        await interaction.response.send_message(
            f"✅ **{cat_data['label']}** tickets will ping {role.mention}.", ephemeral=True
        )

    @setup_ticket_roles.autocomplete("category")
    async def category_autocomplete(self, interaction: discord.Interaction, current: str):
        return [
            app_commands.Choice(name=data["label"], value=key)
            for key, data in TICKET_CATEGORIES.items()
            if current.lower() in data["label"].lower()
        ]

    @ticket_group.command(name="test", description="Toggle test mode for this ticket.")
    @app_commands.default_permissions(administrator=True)
    async def ticket_test(self, interaction: discord.Interaction, enabled: bool):
        await interaction.response.defer(ephemeral=True)
        cid = interaction.channel_id
        ticket = await db.fetch_one("SELECT * FROM active_tickets WHERE channel_id = %s", (cid,))
        if not ticket:
            return await interaction.followup.send("❌ This is not an active ticket channel.", ephemeral=True)

        await db.execute("UPDATE active_tickets SET is_test = %s WHERE channel_id = %s", (enabled, cid))

        if enabled:
            await interaction.followup.send("🧪 **Test Mode ENABLED** — ratings will NOT be recorded.", ephemeral=True)
        else:
            await interaction.followup.send("✅ **Test Mode DISABLED** — ratings WILL be recorded.", ephemeral=True)

    @ticket_group.command(name="stats", description="View ticket rating statistics.")
    @app_commands.default_permissions(administrator=True)
    async def ticket_stats(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        overall = await db.fetch_one(
            "SELECT COUNT(*) AS total, AVG(stars) AS avg_stars FROM ticket_ratings"
        )
        total = overall["total"] if overall else 0
        avg = overall["avg_stars"] if overall and overall["avg_stars"] else 0

        if total == 0:
            return await interaction.followup.send("No ticket ratings recorded yet.", ephemeral=True)

        breakdown = await db.fetch_all(
            "SELECT stars, COUNT(*) AS count FROM ticket_ratings GROUP BY stars ORDER BY stars"
        )
        star_counts = {row["stars"]: row["count"] for row in breakdown}
        breakdown_lines = [f"{'⭐' * s} — {star_counts.get(s, 0)} rating(s)" for s in range(5, 0, -1)]

        handlers = await db.fetch_all(
            "SELECT handler_id, COUNT(*) AS count, AVG(stars) AS avg "
            "FROM ticket_ratings WHERE handler_id IS NOT NULL "
            "GROUP BY handler_id ORDER BY avg DESC LIMIT 5"
        )
        handler_lines = [f"<@{h['handler_id']}> — {h['count']} tickets, {h['avg']:.1f}⭐ avg" for h in handlers]

        embed = discord.Embed(title="📊 Ticket Rating Statistics", color=0xF2C21A)
        embed.add_field(name="Overview", value=f"**{total}** total ratings\n**{avg:.1f}** ⭐ average", inline=False)
        embed.add_field(name="Breakdown", value="\n".join(breakdown_lines), inline=False)
        if handler_lines:
            embed.add_field(name="Top Handlers", value="\n".join(handler_lines), inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── Background: 24h/48h Escalation ─────────────────────────

    @tasks.loop(minutes=10)
    async def check_reminders(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        tickets = await db.fetch_all("SELECT * FROM active_tickets WHERE claimed = FALSE")

        for data in tickets:
            created_at = data["created_at"]
            if created_at.tzinfo is None:
                created_at = pytz.utc.localize(created_at)
            elapsed = now - created_at

            channel = self.bot.get_channel(data["channel_id"])
            if not channel:
                try:
                    channel = await self.bot.fetch_channel(data["channel_id"])
                except (discord.NotFound, discord.Forbidden):
                    await db.execute("DELETE FROM active_tickets WHERE channel_id = %s", (data["channel_id"],))
                    continue
                except Exception:
                    continue

            cat_data = TICKET_CATEGORIES.get(data.get("category_key", "GN"))
            role_key = cat_data["role_key"] if cat_data else None
            role_id = await settings_service.get_int(role_key) if role_key else 0

            try:
                # 48h escalation
                if elapsed > datetime.timedelta(hours=48) and not data.get("escalated_48h"):
                    support_id = await settings_service.get_int("support_role_id")
                    msg = (
                        f"🚨 **UNCLAIMED TICKET ESCALATION (48h)**\n"
                        f"{'<@&' + str(role_id) + '> ' if role_id else ''}{'<@&' + str(support_id) + '>' if support_id else ''}\n"
                        "This ticket has been unattended for 2 days. Please resolve immediately."
                    )
                    await channel.send(msg)
                    await db.execute(
                        "UPDATE active_tickets SET escalated_48h = TRUE WHERE channel_id = %s",
                        (data["channel_id"],),
                    )
                    continue

                # 24h reminder
                if elapsed > datetime.timedelta(hours=24) and not data.get("reminded_24h"):
                    msg = f"⏳ **Reminder:** This ticket has been unclaimed for 24 hours.\n{'<@&' + str(role_id) + '>' if role_id else 'Support team'} please review."
                    await channel.send(msg)
                    await db.execute(
                        "UPDATE active_tickets SET reminded_24h = TRUE WHERE channel_id = %s",
                        (data["channel_id"],),
                    )
            except discord.Forbidden:
                pass
            except Exception as e:
                logger.error(f"Reminder error: {e}")

    @check_reminders.before_loop
    async def before_reminders(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(TicketCog(bot))
