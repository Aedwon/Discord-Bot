"""
Analytics Cog — Passive data collectors, RAM buffer + flush, background jobs, and slash commands.
Captures message metadata, voice sessions, member joins, reactions, RSVPs, and tracked link clicks.
All time operations aligned to Asia/Manila (UTC+8).
"""

import discord
from discord.ext import commands, tasks
from discord import app_commands
import logging
import re
from datetime import datetime, timedelta, timezone, time

from services.database import db
from services.settings_service import settings_service
from services.analytics_service import analytics_service

logger = logging.getLogger("mlbb_bot.analytics_cog")

PHT = timezone(timedelta(hours=8))
# Midnight PHT = 16:00 UTC previous day
MIDNIGHT_PHT_UTC = time(hour=16, minute=0, tzinfo=timezone.utc)

URL_PATTERN = re.compile(r'https?://\S+')


# ─── PERSISTENT VIEW FOR TRACKED LINK BUTTONS ──────────────────

class TrackedLinkView(discord.ui.View):
    """Persistent interceptor button for link click tracking."""
    def __init__(self):
        super().__init__(timeout=None)


class TrackedLinkButton(discord.ui.Button):
    """Individual tracked link button with unique custom_id."""
    def __init__(self, link_id: int, label: str):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label=label,
            custom_id=f"tracked_link_{link_id}",
            emoji="🔗"
        )
        self.link_id = link_id

    async def callback(self, interaction: discord.Interaction):
        # Increment click counter (unique per user via UNIQUE KEY)
        try:
            await db.execute(
                "INSERT IGNORE INTO analytics_link_clicks (link_id, user_id) VALUES (%s, %s)",
                (self.link_id, interaction.user.id)
            )
        except Exception:
            pass

        link = await db.fetch_one(
            "SELECT url FROM analytics_tracked_links WHERE id = %s", (self.link_id,))
        url = link['url'] if link else "https://example.com"
        await interaction.response.send_message(f"🔗 Here's your link: {url}", ephemeral=True)


class AnalyticsCog(commands.Cog, name="analytics"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # RAM write buffers (flushed every 60s)
        self.msg_buffer: list[dict] = []
        self.reaction_buffer: list[dict] = []
        self.voice_active: dict[int, dict] = {}  # user_id -> {channel_id, joined_at}

        # Invite cache for attribution
        self.invite_cache: dict[str, int] = {}  # code -> uses

        # Tracked keywords
        self.tracked_keywords: list[str] = []

    async def cog_load(self):
        """Initialize caches and start background loops."""
        import asyncio
        self.flush_buffer.start()
        self.midnight_jobs.start()
        asyncio.create_task(self._init_invite_cache())
        asyncio.create_task(self._init_keywords())
        asyncio.create_task(self._cleanup_orphaned_voice())
        asyncio.create_task(self._register_tracked_link_views())

    def cog_unload(self):
        self.flush_buffer.cancel()
        self.midnight_jobs.cancel()

    # ─── INITIALIZATION ─────────────────────────────────────────────

    async def _init_invite_cache(self):
        """Cache all guild invites for source attribution diffing."""
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            try:
                invites = await guild.invites()
                for inv in invites:
                    self.invite_cache[inv.code] = inv.uses or 0
            except discord.Forbidden:
                logger.warning(f"Missing Manage Server permission for invite tracking in {guild.name}")
            except Exception as e:
                logger.error(f"Invite cache init error: {e}")

    async def _init_keywords(self):
        """Load tracked keywords from settings."""
        await self.bot.wait_until_ready()
        kw_str = await settings_service.get("analytics_tracked_keywords")
        if kw_str:
            self.tracked_keywords = [k.strip().lower() for k in kw_str.split(",") if k.strip()]

    async def _cleanup_orphaned_voice(self):
        """Close voice sessions that were never properly ended (bot crash recovery)."""
        await self.bot.wait_until_ready()
        await db.execute('''
            UPDATE analytics_voice_sessions
            SET left_at = NOW()
            WHERE left_at IS NULL AND joined_at < DATE_SUB(NOW(), INTERVAL 24 HOUR)
        ''')
        logger.info("Cleaned up orphaned voice sessions")

    async def _register_tracked_link_views(self):
        """Re-register persistent views for existing tracked link buttons."""
        await self.bot.wait_until_ready()
        links = await db.fetch_all("SELECT id, label FROM analytics_tracked_links")
        for link in links:
            view = TrackedLinkView()
            view.add_item(TrackedLinkButton(link['id'], link['label']))
            self.bot.add_view(view)

    # ─── PASSIVE LISTENERS ──────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Buffer message metadata + content for analytics. Skips bots and DMs."""
        if message.author.bot or not message.guild:
            return

        now = datetime.now(PHT)
        content = message.content or ""
        self.msg_buffer.append({
            "channel_id": message.channel.id,
            "author_id": message.author.id,
            "content": content[:4000],  # Truncate to prevent DB overflow
            "has_link": bool(URL_PATTERN.search(content)),
            "word_count": min(len(content.split()), 32767),  # SMALLINT max
            "hour_of_day": now.hour,
            "day_of_week": now.weekday(),
        })

        # Keyword tracking — check against tracked keywords
        content_lower = content.lower()
        for kw in self.tracked_keywords:
            if kw in content_lower:
                # Write keyword matches immediately (low frequency)
                try:
                    await db.execute(
                        "INSERT INTO analytics_keywords (keyword, channel_id, author_id, message_content, created_at) VALUES (%s, %s, %s, %s, NOW())",
                        (kw, message.channel.id, message.author.id, content[:4000])
                    )
                except Exception:
                    pass

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        """Mark deleted messages for sentiment context."""
        if message.author and message.author.bot:
            return
        try:
            await db.execute(
                "UPDATE analytics_messages SET is_deleted = TRUE WHERE channel_id = %s AND author_id = %s AND created_at >= DATE_SUB(NOW(), INTERVAL 5 MINUTE) ORDER BY id DESC LIMIT 1",
                (message.channel.id, message.author.id)
            )
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        """Track voice session start/end for duration metrics. Skips bots."""
        if member.bot:
            return
        if before.channel == after.channel:
            return  # Mute/deafen only, ignore

        # User left a channel
        if before.channel and member.id in self.voice_active:
            session = self.voice_active.pop(member.id)
            try:
                await db.execute(
                    "UPDATE analytics_voice_sessions SET left_at = NOW() WHERE user_id = %s AND channel_id = %s AND left_at IS NULL ORDER BY id DESC LIMIT 1",
                    (member.id, session['channel_id'])
                )
            except Exception as e:
                logger.error(f"Voice session close error: {e}")

        # User joined a channel
        if after.channel:
            self.voice_active[member.id] = {
                "channel_id": after.channel.id,
                "joined_at": datetime.now(PHT),
            }
            try:
                await db.execute(
                    "INSERT INTO analytics_voice_sessions (user_id, channel_id, joined_at) VALUES (%s, %s, NOW())",
                    (member.id, after.channel.id)
                )
            except Exception as e:
                logger.error(f"Voice session open error: {e}")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Track joins with invite attribution. Writes immediately (low frequency)."""
        if member.bot:
            return

        # Invite attribution via cache diff
        invite_code = None
        inviter_id = None
        try:
            new_invites = await member.guild.invites()
            for inv in new_invites:
                old_uses = self.invite_cache.get(inv.code, 0)
                if (inv.uses or 0) > old_uses:
                    invite_code = inv.code
                    inviter_id = inv.inviter.id if inv.inviter else None
                    break
            # Refresh cache
            self.invite_cache = {inv.code: (inv.uses or 0) for inv in new_invites}
        except discord.Forbidden:
            pass
        except Exception as e:
            logger.error(f"Invite attribution error: {e}")

        await db.execute(
            "INSERT INTO analytics_member_joins (user_id, invite_code, inviter_id, joined_at) VALUES (%s, %s, %s, NOW())",
            (member.id, invite_code, inviter_id)
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        """Record leave timestamp for retention analysis."""
        if member.bot:
            return
        await db.execute(
            "UPDATE analytics_member_joins SET left_at = NOW() WHERE user_id = %s AND left_at IS NULL ORDER BY id DESC LIMIT 1",
            (member.id,)
        )

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """Buffer reaction events. Uses raw variant for uncached messages."""
        if payload.user_id == self.bot.user.id:
            return
        emoji_str = str(payload.emoji)
        self.reaction_buffer.append({
            "message_id": payload.message_id,
            "channel_id": payload.channel_id,
            "user_id": payload.user_id,
            "emoji": emoji_str[:100],
        })

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread):
        """Track thread creation as engagement signal (immediate write)."""
        if thread.owner_id:
            # We count threads in the daily rollup; for now just log it
            pass

    @commands.Cog.listener()
    async def on_scheduled_event_user_add(self, event: discord.ScheduledEvent, user: discord.User):
        """Track RSVP clicks for conversion analysis."""
        if user.bot:
            return
        try:
            await db.execute(
                "INSERT IGNORE INTO analytics_event_rsvps (event_id, user_id) VALUES (%s, %s)",
                (event.id, user.id)
            )
        except Exception:
            pass

    # ─── BACKGROUND LOOPS ───────────────────────────────────────────

    @tasks.loop(seconds=60)
    async def flush_buffer(self):
        """Flush RAM buffers to MySQL in batch every 60 seconds."""
        # Flush messages
        if self.msg_buffer:
            batch = self.msg_buffer.copy()
            self.msg_buffer.clear()
            for m in batch:
                try:
                    await db.execute('''
                        INSERT INTO analytics_messages (channel_id, author_id, content, has_link, word_count, hour_of_day, day_of_week)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ''', (m['channel_id'], m['author_id'], m['content'], m['has_link'], m['word_count'], m['hour_of_day'], m['day_of_week']))
                except Exception as e:
                    logger.error(f"Message flush error: {e}")

        # Flush reactions
        if self.reaction_buffer:
            batch = self.reaction_buffer.copy()
            self.reaction_buffer.clear()
            for r in batch:
                try:
                    await db.execute(
                        "INSERT INTO analytics_reactions (message_id, channel_id, user_id, emoji) VALUES (%s, %s, %s, %s)",
                        (r['message_id'], r['channel_id'], r['user_id'], r['emoji'])
                    )
                except Exception as e:
                    logger.error(f"Reaction flush error: {e}")

    @flush_buffer.before_loop
    async def before_flush(self):
        await self.bot.wait_until_ready()

    @tasks.loop(time=MIDNIGHT_PHT_UTC)
    async def midnight_jobs(self):
        """Runs at midnight PHT: daily rollup, auto-purge, sentiment export."""
        await self.bot.wait_until_ready()
        logger.info("Midnight PHT jobs triggered")

        # 1. Daily rollup
        try:
            await analytics_service.run_daily_rollup()
        except Exception as e:
            logger.error(f"Daily rollup failed: {e}")

        # 2. Auto-purge raw messages older than 30 days
        try:
            await analytics_service.purge_old_messages(30)
        except Exception as e:
            logger.error(f"Auto-purge failed: {e}")

        # 3. Auto-post daily sentiment export
        try:
            channel_id = await settings_service.get_int("analytics_sentiment_channel")
            if channel_id:
                guild = self.bot.guilds[0] if self.bot.guilds else None
                if guild:
                    channel = guild.get_channel(channel_id)
                    if channel:
                        buffer = await analytics_service.generate_sentiment_export(guild, days=1)
                        yesterday = (analytics_service.now_pht() - timedelta(days=1)).strftime('%Y-%m-%d')
                        filename = f"sentiment_daily_{yesterday}.txt"

                        # Check file size — split if >24MB
                        buffer.seek(0)
                        data = buffer.read()
                        if len(data) > 24_000_000:
                            # Split into 20MB chunks
                            parts = [data[i:i+20_000_000] for i in range(0, len(data), 20_000_000)]
                            for idx, part in enumerate(parts, 1):
                                import io
                                part_buf = io.BytesIO(part)
                                await channel.send(
                                    content=f"📊 Daily Sentiment Export — {yesterday} (Part {idx}/{len(parts)})",
                                    file=discord.File(part_buf, filename=f"sentiment_daily_{yesterday}_part{idx}.txt")
                                )
                        else:
                            buffer.seek(0)
                            await channel.send(
                                content=f"📊 **Daily Sentiment Export** — {yesterday}",
                                file=discord.File(buffer, filename=filename)
                            )
        except Exception as e:
            logger.error(f"Sentiment export failed: {e}")

    @midnight_jobs.before_loop
    async def before_midnight(self):
        await self.bot.wait_until_ready()

    # ─── SLASH COMMANDS ─────────────────────────────────────────────

    analytics_group = app_commands.Group(name="analytics", description="Community analytics and metrics dashboard.", default_permissions=discord.Permissions(administrator=True))

    @analytics_group.command(name="overview", description="7-day community health summary.")
    async def analytics_overview(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        stats = await analytics_service.get_overview(7)
        ratio = await analytics_service.get_communicator_ratio(interaction.guild.member_count, 7)

        embed = discord.Embed(title="📊 Community Analytics — 7 Day Overview", color=discord.Color.blue(), timestamp=discord.utils.utcnow())
        embed.add_field(name="💬 Messages", value=f"**{stats['messages']:,}**", inline=True)
        embed.add_field(name="🎙️ Voice Minutes", value=f"**{stats['voice_minutes']:,}**", inline=True)
        embed.add_field(name="👥 Communicator Ratio", value=f"**{ratio['ratio']}%** ({ratio['communicators']}/{ratio['total']})", inline=True)
        embed.add_field(name="📥 New Joins", value=f"**{stats['joins']}**", inline=True)
        embed.add_field(name="📤 Leaves", value=f"**{stats['leaves']}**", inline=True)
        embed.add_field(name="⭐ Reactions", value=f"**{stats['reactions']:,}**", inline=True)
        await interaction.followup.send(embed=embed)

    @analytics_group.command(name="retention", description="Day-1, Day-7, Day-30 new member retention rates.")
    async def analytics_retention(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        d1 = await analytics_service.get_retention(1)
        d7 = await analytics_service.get_retention(7)
        d30 = await analytics_service.get_retention(30)

        embed = discord.Embed(title="📈 Member Retention Analysis", color=discord.Color.green())
        embed.add_field(name="Day-1 Retention", value=f"**{d1['rate']}%** ({d1['retained']}/{d1['joined']} returned)", inline=False)
        embed.add_field(name="Day-7 Retention", value=f"**{d7['rate']}%** ({d7['retained']}/{d7['joined']} returned)", inline=False)
        embed.add_field(name="Day-30 Retention", value=f"**{d30['rate']}%** ({d30['retained']}/{d30['joined']} returned)", inline=False)
        await interaction.followup.send(embed=embed)

    @analytics_group.command(name="channels", description="Top 10 channels by message volume.")
    async def analytics_channels(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        data = await analytics_service.get_message_volume(7)
        lines = []
        for i, row in enumerate(data[:10], 1):
            ch = interaction.guild.get_channel(row['channel_id'])
            name = ch.mention if ch else f"#{row['channel_id']}"
            lines.append(f"**{i}.** {name} — **{row['msg_count']:,}** msgs ({row['unique_authors']} authors)")
        embed = discord.Embed(title="💬 Top Channels by Message Volume (7d)", description="\n".join(lines) or "No data yet.", color=discord.Color.blue())
        await interaction.followup.send(embed=embed)

    @analytics_group.command(name="voice", description="Top 10 channels by voice minutes.")
    async def analytics_voice(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        data = await analytics_service.get_voice_stats(7)
        lines = []
        for i, row in enumerate(data[:10], 1):
            ch = interaction.guild.get_channel(row['channel_id'])
            name = ch.mention if ch else f"#{row['channel_id']}"
            lines.append(f"**{i}.** {name} — **{row['total_minutes']:,}** min ({row['unique_users']} users)")
        embed = discord.Embed(title="🎙️ Top Channels by Voice Minutes (7d)", description="\n".join(lines) or "No data yet.", color=discord.Color.purple())
        await interaction.followup.send(embed=embed)

    @analytics_group.command(name="invites", description="Top invite sources by join count.")
    async def analytics_invites(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        data = await analytics_service.get_top_invites(30)
        lines = []
        for i, row in enumerate(data[:10], 1):
            inviter = f"<@{row['inviter_id']}>" if row['inviter_id'] else "Unknown"
            lines.append(f"**{i}.** `{row['invite_code']}` by {inviter} — **{row['join_count']}** joins")
        embed = discord.Embed(title="📥 Top Invite Sources (30d)", description="\n".join(lines) or "No data yet.", color=discord.Color.teal())
        await interaction.followup.send(embed=embed)

    @analytics_group.command(name="event", description="RSVP vs attendance conversion for an event.")
    @app_commands.describe(event_id="The Discord Scheduled Event ID")
    async def analytics_event(self, interaction: discord.Interaction, event_id: str):
        await interaction.response.defer(ephemeral=True)
        try:
            eid = int(event_id)
        except ValueError:
            return await interaction.followup.send("❌ Invalid event ID.")
        data = await analytics_service.get_event_conversion(eid)
        embed = discord.Embed(title="🎟️ Event Conversion Analysis", color=discord.Color.gold())
        embed.add_field(name="RSVPs (Interested)", value=f"**{data['rsvps']}**", inline=True)
        embed.add_field(name="Actual Attendees", value=f"**{data['attendees']}**", inline=True)
        embed.add_field(name="Conversion Rate", value=f"**{data['conversion']}%**", inline=True)
        await interaction.followup.send(embed=embed)

    @analytics_group.command(name="roles", description="Opt-in role adoption percentages.")
    async def analytics_roles(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        tracked_str = await settings_service.get("analytics_tracked_roles")
        if not tracked_str:
            return await interaction.followup.send("❌ No tracked roles configured. Use `/setup analytics_tracked_roles`.")

        role_ids = [int(r.strip()) for r in tracked_str.split(",") if r.strip().isdigit()]
        total = interaction.guild.member_count
        lines = []
        for rid in role_ids:
            role = interaction.guild.get_role(rid)
            if role:
                holders = len(role.members)
                pct = round(holders / total * 100, 1) if total > 0 else 0
                lines.append(f"**{role.name}** — {holders}/{total} members (**{pct}%**)")
        embed = discord.Embed(title="🏷️ Opt-In Role Adoption", description="\n".join(lines) or "No roles found.", color=discord.Color.dark_teal())
        await interaction.followup.send(embed=embed)

    # ─── SENTIMENT EXPORT ───────────────────────────────────────────

    sentiment_group = app_commands.Group(name="sentiment", description="Export messages for external LLM sentiment analysis.", parent=analytics_group)

    @sentiment_group.command(name="daily", description="Export yesterday's messages as a .txt file.")
    async def sentiment_daily(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        buffer = await analytics_service.generate_sentiment_export(interaction.guild, days=1)
        yesterday = (analytics_service.now_pht() - timedelta(days=1)).strftime('%Y-%m-%d')
        await interaction.followup.send(
            content="📊 Daily Sentiment Export",
            file=discord.File(buffer, filename=f"sentiment_daily_{yesterday}.txt")
        )

    @sentiment_group.command(name="weekly", description="Export last 7 days' messages as a .txt file.")
    async def sentiment_weekly(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        buffer = await analytics_service.generate_sentiment_export(interaction.guild, days=7)
        await interaction.followup.send(
            content="📊 Weekly Sentiment Export",
            file=discord.File(buffer, filename=f"sentiment_weekly_{analytics_service.now_pht().strftime('%Y-%m-%d')}.txt")
        )

    @sentiment_group.command(name="monthly", description="Export last 30 days' messages as a .txt file.")
    async def sentiment_monthly(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        buffer = await analytics_service.generate_sentiment_export(interaction.guild, days=30)
        await interaction.followup.send(
            content="📊 Monthly Sentiment Export",
            file=discord.File(buffer, filename=f"sentiment_monthly_{analytics_service.now_pht().strftime('%Y-%m-%d')}.txt")
        )

    # ─── KEYWORD MANAGEMENT ─────────────────────────────────────────

    keyword_group = app_commands.Group(name="keyword", description="Manage tracked keywords for sentiment analysis.", parent=analytics_group)

    @keyword_group.command(name="add", description="Add a keyword to track in community messages.")
    async def keyword_add(self, interaction: discord.Interaction, keyword: str):
        keyword = keyword.strip().lower()
        if keyword in self.tracked_keywords:
            return await interaction.response.send_message(f"❌ `{keyword}` is already tracked.", ephemeral=True)
        self.tracked_keywords.append(keyword)
        await settings_service.set("analytics_tracked_keywords", ",".join(self.tracked_keywords))
        await interaction.response.send_message(f"✅ Now tracking keyword: `{keyword}`", ephemeral=True)

    @keyword_group.command(name="remove", description="Stop tracking a keyword.")
    async def keyword_remove(self, interaction: discord.Interaction, keyword: str):
        keyword = keyword.strip().lower()
        if keyword not in self.tracked_keywords:
            return await interaction.response.send_message(f"❌ `{keyword}` is not being tracked.", ephemeral=True)
        self.tracked_keywords.remove(keyword)
        await settings_service.set("analytics_tracked_keywords", ",".join(self.tracked_keywords))
        await interaction.response.send_message(f"✅ Stopped tracking: `{keyword}`", ephemeral=True)

    @keyword_group.command(name="list", description="Show all tracked keywords.")
    async def keyword_list(self, interaction: discord.Interaction):
        if not self.tracked_keywords:
            return await interaction.response.send_message("No keywords are currently tracked.", ephemeral=True)
        lines = [f"• `{kw}`" for kw in self.tracked_keywords]
        await interaction.response.send_message(f"**Tracked Keywords:**\n" + "\n".join(lines), ephemeral=True)

    # ─── TRACKED LINK COMMANDS ──────────────────────────────────────

    @analytics_group.command(name="track_link", description="Create a tracked link button on an announcement message.")
    @app_commands.describe(message_id="The message ID to attach the button to", label="Button label", url="Destination URL")
    async def track_link(self, interaction: discord.Interaction, message_id: str, label: str, url: str):
        await interaction.response.defer(ephemeral=True)
        try:
            msg_id = int(message_id)
        except ValueError:
            return await interaction.followup.send("❌ Invalid message ID.")

        # Store tracked link
        await db.execute(
            "INSERT INTO analytics_tracked_links (label, url, message_id, channel_id, created_by) VALUES (%s, %s, %s, %s, %s)",
            (label, url, msg_id, interaction.channel.id, interaction.user.id)
        )
        link = await db.fetch_one("SELECT id FROM analytics_tracked_links WHERE message_id = %s ORDER BY id DESC LIMIT 1", (msg_id,))
        link_id = link['id']

        # Create persistent button view
        view = TrackedLinkView()
        view.add_item(TrackedLinkButton(link_id, label))
        self.bot.add_view(view)

        # Try to edit the target message to add the button
        try:
            msg = await interaction.channel.fetch_message(msg_id)
            await msg.edit(view=view)
            await interaction.followup.send(f"✅ Tracked link button `{label}` attached to message!")
        except discord.NotFound:
            await interaction.followup.send("❌ Message not found in this channel.")
        except discord.Forbidden:
            await interaction.followup.send("❌ Bot lacks permission to edit that message.")

    @analytics_group.command(name="link_stats", description="View click stats for a tracked link.")
    @app_commands.describe(link_id="The tracked link ID")
    async def link_stats(self, interaction: discord.Interaction, link_id: int):
        await interaction.response.defer(ephemeral=True)
        data = await analytics_service.get_link_ctr(link_id)
        embed = discord.Embed(title="🔗 Tracked Link Stats", color=discord.Color.blurple())
        embed.add_field(name="Label", value=data['label'], inline=True)
        embed.add_field(name="Unique Clicks", value=f"**{data['clicks']}**", inline=True)
        embed.add_field(name="URL", value=data['url'][:200], inline=False)
        await interaction.followup.send(embed=embed)

    # ─── PEAK HOURS HEATMAP ─────────────────────────────────────────

    @analytics_group.command(name="peak_hours", description="Message activity heatmap by hour and day.")
    async def analytics_peak_hours(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        data = await analytics_service.get_peak_hours(7)

        # Build 7x24 grid
        grid = {}
        for row in data:
            key = (row['day_of_week'], row['hour_of_day'])
            grid[key] = row['msg_count']

        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        max_val = max(grid.values()) if grid else 1

        lines = ["**Peak Activity Heatmap (7d, PHT)**\n```"]
        lines.append("Hour  " + " ".join(f"{d:>3}" for d in days))
        for h in range(0, 24, 2):  # Show every 2 hours to fit embed
            row_str = f"{h:02d}:00 "
            for d in range(7):
                count = grid.get((d, h), 0) + grid.get((d, h+1), 0)
                intensity = int(count / max_val * 9) if max_val > 0 else 0
                blocks = ["░", "░", "▒", "▒", "▓", "▓", "█", "█", "█", "█"]
                row_str += f"  {blocks[intensity]}"
            lines.append(row_str)
        lines.append("```")
        lines.append("░ = Low | ▒ = Medium | ▓ = High | █ = Peak")

        embed = discord.Embed(title="⏰ Peak Concurrency Windows", description="\n".join(lines), color=discord.Color.orange())
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(AnalyticsCog(bot))
