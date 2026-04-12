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
                "INSERT INTO analytics_link_clicks (link_id, user_id) VALUES (%s, %s) ON DUPLICATE KEY UPDATE link_id = VALUES(link_id)",
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
        self.name_cache_buffer: dict[int, str] = {}  # user_id -> display_name
        self.channel_name_buffer: dict[int, str] = {}  # channel_id -> name
        self.voice_active: dict[int, dict] = {}  # user_id -> {channel_id, joined_at}

        # Invite cache for attribution
        self.invite_cache: dict[str, int] = {}  # code -> uses

        # Tracked keywords
        self.tracked_keywords: list[str] = []

    async def cog_load(self):
        """Register persistent views on load (no bot readiness needed)."""
        pass

    def cog_unload(self):
        self.flush_buffer.cancel()
        self.midnight_jobs.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        """Start background loops and init tasks after bot is fully connected."""
        if not self.flush_buffer.is_running():
            self.flush_buffer.start()
        if not self.midnight_jobs.is_running():
            self.midnight_jobs.start()
        import asyncio
        asyncio.create_task(self._init_invite_cache())
        asyncio.create_task(self._init_keywords())
        asyncio.create_task(self._cleanup_orphaned_voice())
        asyncio.create_task(self._register_tracked_link_views())
        asyncio.create_task(self._sync_identity_caches())

    # ─── INITIALIZATION ─────────────────────────────────────────────

    async def _init_invite_cache(self):
        """Cache all guild invites for source attribution diffing."""
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
        kw_str = await settings_service.get("analytics_tracked_keywords")
        if kw_str:
            self.tracked_keywords = [k.strip().lower() for k in kw_str.split(",") if k.strip()]

    async def _cleanup_orphaned_voice(self):
        """Close voice sessions that were never properly ended (bot crash recovery)."""
        await db.execute('''
            UPDATE analytics_voice_sessions
            SET left_at = NOW()
            WHERE left_at IS NULL AND joined_at < DATE_SUB(NOW(), INTERVAL 24 HOUR)
        ''')
        logger.info("Cleaned up orphaned voice sessions")

    async def _register_tracked_link_views(self):
        """Re-register persistent views for existing tracked link buttons."""
        links = await db.fetch_all("SELECT id, label FROM analytics_tracked_links")
        for link in links:
            view = TrackedLinkView()
            view.add_item(TrackedLinkButton(link['id'], link['label']))
            self.bot.add_view(view)

    async def _sync_identity_caches(self):
        """Bulk-sync all guild members' display names and channel names into the DB cache.
        Runs once on startup. Processes in chunks to avoid memory pressure."""
        try:
            member_count = 0
            channel_count = 0
            for guild in self.bot.guilds:
                # Sync member names in batches of 500
                batch = []
                for member in guild.members:
                    if member.bot:
                        continue
                    batch.append((member.id, member.display_name))
                    if len(batch) >= 500:
                        await self._flush_name_batch(batch)
                        member_count += len(batch)
                        batch = []
                if batch:
                    await self._flush_name_batch(batch)
                    member_count += len(batch)

                # Sync channel names
                ch_batch = []
                for channel in guild.channels:
                    if hasattr(channel, 'name') and channel.name:
                        ch_batch.append((channel.id, channel.name))
                        if len(ch_batch) >= 500:
                            await self._flush_channel_name_batch(ch_batch)
                            channel_count += len(ch_batch)
                            ch_batch = []
                if ch_batch:
                    await self._flush_channel_name_batch(ch_batch)
                    channel_count += len(ch_batch)

            logger.info(f"Identity cache synced: {member_count} members, {channel_count} channels")

            # Auto-repair historical rollups now that caches are fresh
            await self._repair_granular_json()
        except Exception as e:
            logger.error(f"Identity cache sync error: {e}")

    async def _repair_granular_json(self):
        """Walk existing rollups and replace numeric-only names with resolved names
        from the cache tables. Idempotent — already-resolved entries are skipped."""
        try:
            import json as _json
            member_rows = await db.fetch_all("SELECT user_id, display_name FROM member_names")
            member_cache = {r['user_id']: r['display_name'] for r in member_rows}
            channel_rows = await db.fetch_all("SELECT channel_id, channel_name FROM channel_names")
            channel_cache = {r['channel_id']: r['channel_name'] for r in channel_rows}

            if not member_cache and not channel_cache:
                return

            rows = await db.fetch_all(
                "SELECT date, granular_json FROM analytics_daily_rollups WHERE granular_json IS NOT NULL"
            )
            repaired = 0
            for row in rows:
                try:
                    g = _json.loads(row['granular_json']) if isinstance(row['granular_json'], str) else row['granular_json']
                except (TypeError, _json.JSONDecodeError):
                    continue
                if not g:
                    continue

                changed = False

                # Fix user names
                for section_key, id_field in [('quiz_top_3', 'user_id'), ('thanks_top_3', 'user_id'), ('top_invites', 'inviter')]:
                    for entry in g.get(section_key, []):
                        uid = entry.get(id_field)
                        cur = str(entry.get('name', ''))
                        if uid and (cur.isdigit() or cur == str(uid)):
                            resolved = member_cache.get(uid)
                            if resolved:
                                entry['name'] = resolved
                                changed = True

                # Fix channel names
                for section_key in ['top_text_channels', 'top_voice_channels']:
                    for entry in g.get(section_key, []):
                        cid = entry.get('channel_id')
                        cur = str(entry.get('name', ''))
                        if cid and (cur.isdigit() or cur == str(cid)):
                            resolved = channel_cache.get(cid)
                            if resolved:
                                entry['name'] = resolved
                                changed = True

                # Ensure counts are ints
                for key in ['total_mod_actions', 'new_verifications', 'new_tickets',
                            'quiz_sessions', 'quiz_score', 'thanks_given', 'quests_completed',
                            'new_referrals', 'ep_redemptions', 'booster_raffle_wins',
                            'event_raffles_created', 'event_raffle_entries',
                            'event_participation_claims', 'event_ep_distributed',
                            'event_registrations', 'ticket_ratings_count']:
                    if key in g and g[key] is not None:
                        try:
                            g[key] = int(g[key])
                        except (ValueError, TypeError):
                            pass

                if g.get('mod_actions'):
                    for action in g['mod_actions']:
                        try:
                            g['mod_actions'][action] = int(g['mod_actions'][action])
                        except (ValueError, TypeError):
                            pass

                if changed:
                    import decimal, datetime as _dt
                    def json_serial(obj):
                        if isinstance(obj, decimal.Decimal): return float(obj)
                        if isinstance(obj, (_dt.datetime, _dt.date)): return obj.isoformat()
                        return str(obj)
                    new_json = _json.dumps(g, default=json_serial)
                    await db.execute(
                        "UPDATE analytics_daily_rollups SET granular_json = %s WHERE date = %s",
                        (new_json, row['date'])
                    )
                    repaired += 1

            if repaired:
                logger.info(f"Granular JSON repair: fixed {repaired} rollup(s)")
            else:
                logger.info("Granular JSON repair: all rollups already clean")
        except Exception as e:
            logger.error(f"Granular JSON repair error: {e}")

    async def _flush_name_batch(self, batch: list[tuple[int, str]]):
        """Upsert a batch of (user_id, display_name) into member_names."""
        for user_id, name in batch:
            try:
                await db.execute(
                    "INSERT INTO member_names (user_id, display_name) VALUES (%s, %s) "
                    "ON DUPLICATE KEY UPDATE display_name = VALUES(display_name), last_updated = NOW()",
                    (user_id, name)
                )
            except Exception:
                pass

    async def _flush_channel_name_batch(self, batch: list[tuple[int, str]]):
        """Upsert a batch of (channel_id, channel_name) into channel_names."""
        for channel_id, name in batch:
            try:
                await db.execute(
                    "INSERT INTO channel_names (channel_id, channel_name) VALUES (%s, %s) "
                    "ON DUPLICATE KEY UPDATE channel_name = VALUES(channel_name), last_updated = NOW()",
                    (channel_id, name)
                )
            except Exception:
                pass

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

        # Buffer name cache update (deduplicated per flush cycle)
        self.name_cache_buffer[message.author.id] = message.author.display_name
        if hasattr(message.channel, 'name') and message.channel.name:
            self.channel_name_buffer[message.channel.id] = message.channel.name

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

        # Cache the new member's name immediately (low frequency event)
        try:
            await db.execute(
                "INSERT INTO member_names (user_id, display_name) VALUES (%s, %s) "
                "ON DUPLICATE KEY UPDATE display_name = VALUES(display_name), last_updated = NOW()",
                (member.id, member.display_name)
            )
        except Exception:
            pass

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
                "INSERT INTO analytics_event_rsvps (event_id, user_id) VALUES (%s, %s) ON DUPLICATE KEY UPDATE event_id = VALUES(event_id)",
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

        # Flush name cache buffer
        if self.name_cache_buffer:
            names = self.name_cache_buffer.copy()
            self.name_cache_buffer.clear()
            for user_id, name in names.items():
                try:
                    await db.execute(
                        "INSERT INTO member_names (user_id, display_name) VALUES (%s, %s) "
                        "ON DUPLICATE KEY UPDATE display_name = VALUES(display_name), last_updated = NOW()",
                        (user_id, name)
                    )
                except Exception:
                    pass

        # Flush channel name buffer
        if self.channel_name_buffer:
            channels = self.channel_name_buffer.copy()
            self.channel_name_buffer.clear()
            for channel_id, name in channels.items():
                try:
                    await db.execute(
                        "INSERT INTO channel_names (channel_id, channel_name) VALUES (%s, %s) "
                        "ON DUPLICATE KEY UPDATE channel_name = VALUES(channel_name), last_updated = NOW()",
                        (channel_id, name)
                    )
                except Exception:
                    pass

    @flush_buffer.before_loop
    async def before_flush(self):
        await self.bot.wait_until_ready()

    @tasks.loop(time=MIDNIGHT_PHT_UTC)
    async def midnight_jobs(self):
        """Runs at midnight PHT: daily rollup, auto-purge, sentiment export."""
        await self.bot.wait_until_ready()
        logger.info("Midnight PHT jobs triggered")

        # 1. Daily rollup
        rollup_data = None
        try:
            rollup_data = await analytics_service.run_daily_rollup(bot=self.bot)
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

        # 4. Auto-post exhaustive Analytics Mega-Log
        try:
            log_channel_id = await settings_service.get_int("analytics_log_channel_id")
            if log_channel_id and rollup_data:
                guild = self.bot.guilds[0] if self.bot.guilds else None
                if guild:
                    yesterday_str = rollup_data['date']
                    await self._post_daily_analytics_log(guild, log_channel_id, rollup_data, yesterday_str)
        except Exception as e:
            logger.error(f"Mega-Log export failed: {e}")

    @midnight_jobs.before_loop
    async def before_midnight(self):
        await self.bot.wait_until_ready()

    async def _post_daily_analytics_log(self, guild: discord.Guild, channel_id: int, rollup_data: dict, yesterday: str):
        """Builds and sends the exhaustive Mega-Embed of daily stats to the specified channel."""
        try:
            channel = guild.get_channel(channel_id)
            if not channel:
                logger.warning(f"Analytics log channel {channel_id} not found in guild {guild.name}.")
                return

            # Fetch extra exhaustive stats
            extra = await analytics_service.get_exhaustive_daily_stats(yesterday)
            
            # Live state snapshots
            boosts = guild.premium_subscription_count
            threads = len(guild.threads)
            events = len(guild.scheduled_events)
            total_members = guild.member_count
            
            # ──📱 Embed 1: Traffic & Engagement ──
            
            # Format text channels
            top_tx = "\n".join([f"<#{c['channel_id']}>: {c['count']} msgs" for c in extra['top_text_channels']]) or "No messages"
            top_vc = "\n".join([f"<#{c['channel_id']}>: {c['mins']} mins" for c in extra['top_voice_channels']]) or "No voice activity"
            
            e1 = discord.Embed(title=f"📱 Traffic & Engagement ({yesterday})", color=discord.Color.from_str("#F2C21A"))
            e1.add_field(name="Total Traffic", value=f"**Messages:** {rollup_data['total_messages']:,} (by {rollup_data['unique_messagers']:,} unique)\n**Voice Mins:** {rollup_data['total_voice_minutes']:,} (by {rollup_data['unique_voice_users']:,} unique)", inline=False)
            e1.add_field(name="Top 5 Text Channels", value=top_tx, inline=True)
            e1.add_field(name="Top 3 Voice Channels", value=top_vc, inline=True)
            
            # ── 📈 Embed 2: Growth & Retention ──
            ret = extra['retention_day_1']
            ret_str = f"{ret['rate']}% ({ret['retained']}/{ret['joined']} returned)" if ret else "No data"
            
            invs = "\n".join([f"`{i['code']}` (by <@{i['inviter']}>): {i['count']} joins" for i in extra['top_invites']]) or "No invites tracked"
            
            e2 = discord.Embed(title=f"📈 Growth & Retention", color=discord.Color.from_str("#2ECC71"))
            e2.add_field(name="Daily Growth", value=f"**New Joins:** {rollup_data['new_joins']} | **Leaves:** {rollup_data['new_leaves']}\n**Day-1 Retention:** {ret_str}\n**New Verifications:** {extra['new_verifications']}", inline=False)
            e2.add_field(name="Top 3 Invite Codes", value=invs, inline=False)
            
            # ── ⚔️ Embed 3: Economy & Gameplay ──
            qt = "\n".join([f"<@{q['user_id']}>: {q['score']} pts" for q in extra['quiz_top_3']]) or "None"
            tht = "\n".join([f"<@{t['user_id']}>: {t['count']} times" for t in extra['thanks_top_3']]) or "None"
            
            e3 = discord.Embed(title=f"⚔️ Economy & Gameplay", color=discord.Color.from_str("#E74C3C"))
            e3.add_field(name="System Totals", value=f"**Quests Done:** {extra['quests_completed']:,}\n**EP Redemptions:** {extra['ep_redemptions']:,}\n**Total Thanks:** {extra['thanks_given']}\n**Referrals Linked:** {extra['new_referrals']}", inline=False)
            e3.add_field(name="Top 3 Quiz Players", value=f"Total matches: {extra['quiz_sessions']}\n{qt}", inline=True)
            e3.add_field(name="Most Thanked Members", value=tht, inline=True)
            
            # ── 🛡️ Embed 4: Operations & Moderation ──
            tc_map = extra['tickets_by_category']
            tc_str = "\n".join([f"{k}: {v}" for k, v in tc_map.items()]) if tc_map else "No new tickets"
            ma_str = ", ".join(f"{k}: {v}" for k,v in extra['mod_actions'].items()) if extra['mod_actions'] else "None"
            
            e4 = discord.Embed(title=f"🛡️ Operations & Moderation", color=discord.Color.from_str("#3498DB"))
            e4.add_field(name="Tickets Opened By Category", value=tc_str, inline=True)
            e4.add_field(name="Support metrics", value=f"**Total Ratings:** {extra['ticket_ratings_count']}\n**Average Rating:** ⭐ {extra['ticket_avg_rating']}/5", inline=True)
            e4.add_field(name="Moderation Log", value=f"**Total Actions:** {extra['total_mod_actions']} ({ma_str})", inline=False)
            e4.add_field(name="📌 Live Midnight Snapshot", value=f"**Total Members:** {total_members:,} | **Server Boosts:** {boosts} | **Active Threads:** {threads} | **Scheduled Events:** {events}", inline=False)
            
            await channel.send(embeds=[e1, e2, e3, e4])
        except Exception as e:
            logger.error(f"Failed to post daily analytics embed: {e}")

    # ─── SLASH COMMANDS ─────────────────────────────────────────────

    analytics_group = app_commands.Group(name="analytics", description="Community analytics and metrics dashboard.", default_permissions=discord.Permissions(administrator=True))

    @analytics_group.command(name="fetch_date", description="Fetch the granular daily dashboard for a specific date.")
    @app_commands.describe(date="The date to fetch (Format: YYYY-MM-DD)")
    async def analytics_fetch_date(self, interaction: discord.Interaction, date: str):
        import re
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            await interaction.response.send_message("Invalid date format. Please use YYYY-MM-DD.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False)
        
        # Check if the core rollup data exists for this past date
        from services.database import db
        row = await db.fetch_one("SELECT * FROM analytics_daily_rollups WHERE date = %s", (date,))
        
        if not row:
            await interaction.followup.send(f"❌ No aggregated log data found for `{date}`. Make sure it is a valid date from the past.", ephemeral=True)
            return
            
        rollup_data = {
            "date": date,
            "total_messages": row.get('total_messages', 0),
            "unique_messagers": row.get('unique_messagers', 0),
            "total_voice_minutes": row.get('total_voice_minutes', 0),
            "unique_voice_users": row.get('unique_voice_users', 0),
            "new_joins": row.get('new_joins', 0),
            "new_leaves": row.get('new_leaves', 0),
            "total_reactions": row.get('total_reactions', 0)
        }
        
        try:
            await self._post_daily_analytics_log(interaction.guild, interaction.channel.id, rollup_data, date)
            await interaction.followup.send(f"✅ Successfully extracted and generated the Mega-Log for `{date}`.", ephemeral=True)
        except Exception as e:
            logger.error(f"Failed to generate explicit fetch_date dashboard: {e}")
            await interaction.followup.send(f"❌ Encountered an error generating the log: {e}", ephemeral=True)

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

        # Promo adoption stats
        from services.promo_service import promo_service
        promo_stats = await promo_service.get_promo_stats()
        total_members = interaction.guild.member_count or 1
        promo_pct = round(promo_stats['promoters'] / total_members * 100, 1)
        embed.add_field(
            name="📣 Status Promoters",
            value=f"**{promo_stats['promoters']}** ({promo_pct}% of server)",
            inline=True
        )

        await interaction.followup.send(embed=embed)

    @analytics_group.command(name="notifications", description="Notification role subscription analytics.")
    async def analytics_notifications(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        # Query each notification column count
        notif_columns = [
            ("📅 Server Events",  "notif_server_event"),
            ("🧠 Quiz",           "notif_quiz"),
            ("🎁 Giveaways",      "notif_giveaway"),
            ("📋 Surveys",        "notif_survey"),
            ("⚔️ Tournaments",   "notif_tournament"),
            ("🤝 Partner Events", "notif_partner_event"),
        ]

        total_members = interaction.guild.member_count or 1
        results = []

        for label, col in notif_columns:
            row = await db.fetch_one(
                f"SELECT COUNT(*) as c FROM users WHERE {col} = TRUE"
            )
            count = row['c'] if row else 0
            pct = round(count / total_members * 100, 1)
            results.append((label, count, pct))

        # Sort by count descending for the bar chart
        results.sort(key=lambda x: x[1], reverse=True)
        max_count = results[0][1] if results[0][1] > 0 else 1

        embed = discord.Embed(
            title="🔔 Notification Subscription Analytics",
            color=discord.Color.blue(),
            timestamp=discord.utils.utcnow(),
        )

        # Summary field
        lines = []
        for label, count, pct in results:
            bar_len = int((count / max_count) * 10)
            bar = "█" * bar_len + "░" * (10 - bar_len)
            lines.append(f"{label}\n`{bar}` **{count}** ({pct}%)")

        embed.add_field(
            name="Subscriptions by Category",
            value="\n\n".join(lines),
            inline=False,
        )

        total_subs = sum(r[1] for r in results)
        embed.set_footer(text=f"Total subscriptions: {total_subs} | Server members: {total_members}")

        await interaction.followup.send(embed=embed)

    @analytics_group.command(name="active_users", description="DAU, WAU, MAU with trends and stickiness ratio.")
    async def analytics_active_users(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        data = await analytics_service.get_active_users()

        # ── Trend arrows ──
        def trend_str(change: float | None) -> str:
            if change is None:
                return "🆕 *No prior data*"
            arrow = "📈" if change > 0 else "📉" if change < 0 else "➡️"
            sign = "+" if change > 0 else ""
            return f"{arrow} {sign}{change}% vs prior period"

        embed = discord.Embed(
            title="📊 Active Users — DAU / WAU / MAU",
            description="Real-time de-duplicated counts across **messages** and **voice**.",
            color=discord.Color.blue(),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(
            name="📅 DAU (Today)",
            value=f"**{data['dau']:,}**",
            inline=True
        )
        embed.add_field(
            name="📆 WAU (7 Days)",
            value=f"**{data['wau']:,}**\n{trend_str(data['wau_change'])}",
            inline=True
        )
        embed.add_field(
            name="🗓️ MAU (30 Days)",
            value=f"**{data['mau']:,}**\n{trend_str(data['mau_change'])}",
            inline=True
        )

        # Stickiness = DAU / WAU — how "daily" the weekly users are
        embed.add_field(
            name="🧲 Stickiness (DAU ÷ WAU)",
            value=f"**{data['stickiness']}%**",
            inline=True
        )

        # WAU/MAU ratio — breadth of monthly engagement
        wau_mau = round(data['wau'] / data['mau'] * 100, 1) if data['mau'] > 0 else 0
        embed.add_field(
            name="🔄 WAU ÷ MAU",
            value=f"**{wau_mau}%**",
            inline=True
        )

        # Blank field for alignment
        embed.add_field(name="\u200b", value="\u200b", inline=True)

        # ── 7-day trend mini-chart ──
        trend = data['dau_trend']
        if trend:
            max_count = max(t['count'] for t in trend) or 1
            chart_lines = []
            for t in trend:
                bar_len = int(t['count'] / max_count * 12)
                bar = "█" * bar_len + "░" * (12 - bar_len)
                chart_lines.append(f"`{t['date']}` {bar} **{t['count']}**")
            embed.add_field(
                name="📉 7-Day DAU Trend",
                value="\n".join(chart_lines),
                inline=False
            )
        else:
            embed.add_field(
                name="📉 7-Day DAU Trend",
                value="*No rollup data yet — trends appear after the first midnight rollup.*",
                inline=False
            )

        embed.set_footer(text="DAU = users active today • WAU = last 7d • MAU = last 30d")
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
        
        # Calculate true max over 2-hour aggregated blocks
        max_val = 1
        for h in range(0, 24, 2):
            for d in range(7):
                val = grid.get((d, h), 0) + grid.get((d, h+1), 0)
                if val > max_val:
                    max_val = val

        lines = ["**Peak Activity Heatmap (7d, PHT)**\n```"]
        lines.append("Hour  " + " ".join(f"{d:>3}" for d in days))
        for h in range(0, 24, 2):  # Show every 2 hours to fit embed
            row_str = f"{h:02d}:00 "
            for d in range(7):
                count = grid.get((d, h), 0) + grid.get((d, h+1), 0)
                intensity = int(count / max_val * 9) if max_val > 0 else 0
                intensity = min(intensity, 9) # Prevent out-of-bounds
                blocks = ["░", "░", "▒", "▒", "▓", "▓", "█", "█", "█", "█"]
                row_str += f"  {blocks[intensity]}"
            lines.append(row_str)
        lines.append("```")
        lines.append("░ = Low | ▒ = Medium | ▓ = High | █ = Peak")

        embed = discord.Embed(title="⏰ Peak Concurrency Windows", description="\n".join(lines), color=discord.Color.orange())
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(AnalyticsCog(bot))
