"""
Log Cog — Comprehensive message edit, delete, and voice logging.

Message cache is backed by a local JSON file (message_cache.json) instead of
the database. This eliminates per-message DB writes and survives bot restarts.
Attachments are downloaded and re-uploaded to the log channel on delete so they
persist permanently (Discord CDN URLs expire after deletion).
"""

import discord
from discord.ext import commands, tasks
import logging
import asyncio
import json
import os
import io
from pathlib import Path
from datetime import datetime, timezone

import aiohttp

from services.settings_service import settings_service

logger = logging.getLogger("mlbb_bot.log_cog")

# Discord epoch for snowflake → timestamp conversion
DISCORD_EPOCH = 1420070400000

# Storage path — same level as scheduled_embeds.json (project root)
STORAGE_DIR = Path(__file__).resolve().parent.parent.parent
STORAGE_FILE = STORAGE_DIR / "message_cache.json"

# Limits
MAX_REUPLOAD_SIZE = 25 * 1024 * 1024   # 25 MB (Discord non-Nitro upload cap)
CACHE_TTL_DAYS = 30                      # How long to keep messages in cache
FLUSH_INTERVAL_SECONDS = 60              # How often to write cache to disk
BULK_ATTACHMENT_BATCH = 5                # Max concurrent attachment downloads in bulk


def snowflake_to_timestamp(snowflake_id: int) -> float:
    """Extract Unix timestamp from a Discord snowflake ID."""
    return ((snowflake_id >> 22) + DISCORD_EPOCH) / 1000.0


def snowflake_age_days(snowflake_id: int) -> float:
    """Return how many days old a snowflake ID is."""
    ts = snowflake_to_timestamp(snowflake_id)
    return (datetime.now(timezone.utc).timestamp() - ts) / 86400.0


class LogCog(commands.Cog, name="Logging"):
    """Comprehensive message edit, deletion, and voice logging with
    JSON-backed cache and attachment preservation."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._cache: dict[str, dict] = {}
        self._dirty: bool = False

    # ─── LIFECYCLE ─────────────────────────────────────────────────────

    async def cog_load(self):
        self._load_cache()

    def cog_unload(self):
        self._flush_and_prune.cancel()
        # Final save on unload
        if self._dirty:
            self._save_cache()

    @commands.Cog.listener()
    async def on_ready(self):
        if not self._flush_and_prune.is_running():
            self._flush_and_prune.start()

    # ─── JSON FILE PERSISTENCE ─────────────────────────────────────────

    def _load_cache(self):
        """Load message cache from JSON file on disk."""
        path = str(STORAGE_FILE)
        if os.path.exists(path):
            try:
                with open(path, 'r') as f:
                    raw = f.read().strip()
                    if raw:
                        self._cache = json.loads(raw)
                    else:
                        self._cache = {}
                logger.info(f"Message cache loaded: {len(self._cache)} entries from {path}")
            except (json.JSONDecodeError, OSError) as e:
                logger.error(f"Failed to load message cache from {path}: {e}")
                self._cache = {}
        else:
            self._cache = {}
            logger.info(f"No message cache file found at {path}, starting fresh.")

    def _save_cache(self):
        """Atomically persist the message cache to disk."""
        path = str(STORAGE_FILE)
        try:
            temp_file = path + ".tmp"
            with open(temp_file, 'w') as f:
                json.dump(self._cache, f, separators=(',', ':'))  # Compact JSON
            os.replace(temp_file, path)
            self._dirty = False
        except OSError as e:
            logger.error(f"Failed to save message cache to {path}: {e}")

    @tasks.loop(seconds=FLUSH_INTERVAL_SECONDS)
    async def _flush_and_prune(self):
        """Every 60s: prune old entries and write cache to disk if dirty."""
        # Prune entries older than CACHE_TTL_DAYS
        cutoff = CACHE_TTL_DAYS
        to_remove = []
        for msg_id_str in list(self._cache.keys()):
            try:
                age = snowflake_age_days(int(msg_id_str))
                if age > cutoff:
                    to_remove.append(msg_id_str)
            except (ValueError, OverflowError):
                to_remove.append(msg_id_str)  # Invalid snowflake, remove

        if to_remove:
            for key in to_remove:
                self._cache.pop(key, None)
            self._dirty = True
            logger.info(f"Message cache pruned: removed {len(to_remove)} entries older than {cutoff} days")

        if self._dirty:
            self._save_cache()

    @_flush_and_prune.before_loop
    async def _before_flush(self):
        await self.bot.wait_until_ready()

    # ─── HELPERS ───────────────────────────────────────────────────────

    async def get_log_channel(self) -> discord.TextChannel | None:
        """Helper to get the configured message log channel."""
        channel_id = await settings_service.get_int("message_log_channel_id")
        if not channel_id:
            return None
        return self.bot.get_channel(channel_id)

    def _extract_attachment_meta(self, message: discord.Message) -> list[dict]:
        """Extract structured attachment metadata from a message."""
        atts = []
        for a in message.attachments:
            atts.append({
                "fn": a.filename,
                "url": a.url,
                "sz": a.size,
                "ct": a.content_type or "unknown",
            })
        return atts

    def _extract_sticker_names(self, message: discord.Message) -> list[str]:
        """Extract sticker names from a message."""
        return [s.name for s in message.stickers] if message.stickers else []

    def _extract_embed_urls(self, message: discord.Message) -> list[str]:
        """Extract media URLs from rich embeds (Tenor GIFs, etc.)."""
        urls = []
        for e in message.embeds:
            if e.url:
                urls.append(e.url)
            elif e.image and e.image.url:
                urls.append(e.image.url)
            elif e.thumbnail and e.thumbnail.url:
                urls.append(e.thumbnail.url)
        return list(set(urls))

    async def _try_download_attachment(self, att_info: dict) -> discord.File | None:
        """Attempt to download an attachment before its CDN URL expires.
        Returns a discord.File ready for re-upload, or None on failure."""
        size = att_info.get('sz', 0)
        if size > MAX_REUPLOAD_SIZE:
            return None  # Too large for Discord upload

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(att_info['url'], timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        return discord.File(io.BytesIO(data), filename=att_info.get('fn', 'attachment'))
        except Exception:
            return None

    def _format_file_size(self, size_bytes: int) -> str:
        """Human-readable file size."""
        if size_bytes < 1024:
            return f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        else:
            return f"{size_bytes / (1024 * 1024):.1f} MB"

    def _build_attachment_field(self, atts: list[dict], recovered_names: set[str]) -> str:
        """Build the embed field text for attachments, marking which were recovered vs expired."""
        if not atts:
            return ""
        lines = []
        for att in atts:
            fn = att.get('fn', 'unknown')
            sz = self._format_file_size(att.get('sz', 0))
            ct = att.get('ct', 'unknown')
            if fn in recovered_names:
                lines.append(f"✅ `{fn}` ({sz}, {ct}) — **Recovered ↓**")
            elif att.get('sz', 0) > MAX_REUPLOAD_SIZE:
                lines.append(f"⚠️ `{fn}` ({sz}, {ct}) — **Too large to recover**")
            else:
                lines.append(f"❌ `{fn}` ({sz}, {ct}) — **Expired, could not download**")
        return "\n".join(lines)

    def _build_sticker_field(self, stickers: list[str]) -> str:
        """Build the embed field text for stickers."""
        if not stickers:
            return ""
        return ", ".join([f"🏷️ `{s}`" for s in stickers])

    def _build_edit_history_field(self, edits: list[dict]) -> list[discord.Embed]:
        """Build embed(s) showing the full edit trail for a message."""
        if not edits:
            return []
        embeds = []
        for i, edit in enumerate(edits):
            e = discord.Embed(
                title=f"Edit #{i + 1}" if len(edits) > 1 else "Edit History",
                color=discord.Color.dark_gold(),
            )
            old_text = edit.get('old', '*empty*')[:1024]
            new_text = edit.get('new', '*empty*')[:1024]
            e.add_field(name="Before", value=old_text or "*empty*", inline=False)
            e.add_field(name="After", value=new_text or "*empty*", inline=False)
            e.set_footer(text=f"Edited at {edit.get('at', '?')}")
            embeds.append(e)
        return embeds

    # ─── MESSAGE CACHING (on_message) ──────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Cache every non-bot guild message into RAM dict."""
        if message.author.bot or message.guild is None:
            return

        avatar = message.author.display_avatar.url if message.author.display_avatar else ""
        self._cache[str(message.id)] = {
            "cid": message.channel.id,
            "aid": message.author.id,
            "aname": message.author.name,
            "aav": avatar,
            "text": message.content or "",
            "atts": self._extract_attachment_meta(message),
            "stickers": self._extract_sticker_names(message),
            "embed_urls": self._extract_embed_urls(message),
            "ts": message.created_at.isoformat(),
            "edits": [],
        }
        self._dirty = True

    # ─── MESSAGE DELETE ────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if message.author.bot:
            return

        channel = await self.get_log_channel()
        if not channel:
            return

        # Build base embed from live message object
        embed = discord.Embed(
            title="🗑️ Message Deleted",
            description=message.content or "*No text content*",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_author(
            name=f"{message.author.name} ({message.author.id})",
            icon_url=message.author.display_avatar.url if message.author.display_avatar else None
        )
        embed.add_field(name="Channel", value=message.channel.mention, inline=True)
        embed.add_field(name="Message ID", value=str(message.id), inline=True)

        # Get cached data for edit history and attachment metadata
        cached = self._cache.pop(str(message.id), None)
        self._dirty = True

        # Attempt to download and re-upload attachments
        files = []
        recovered_names = set()
        att_list = self._extract_attachment_meta(message) if message.attachments else []
        if not att_list and cached:
            att_list = cached.get('atts', [])

        for att in att_list:
            f = await self._try_download_attachment(att)
            if f:
                files.append(f)
                recovered_names.add(att.get('fn', ''))

        # Attachment field
        att_field = self._build_attachment_field(att_list, recovered_names)
        if att_field:
            embed.add_field(name="Attachments", value=att_field, inline=False)

        # Embed URLs (Tenor GIFs, etc.)
        embed_urls = self._extract_embed_urls(message)
        if not embed_urls and cached:
            embed_urls = cached.get('embed_urls', [])
        if embed_urls:
            embed.add_field(
                name="Embedded Media",
                value="\n".join([f"🔗 [Link]({u})" for u in embed_urls[:5]]),
                inline=False
            )

        # Stickers
        sticker_names = self._extract_sticker_names(message)
        if not sticker_names and cached:
            sticker_names = cached.get('stickers', [])
        sticker_field = self._build_sticker_field(sticker_names)
        if sticker_field:
            embed.add_field(name="Stickers", value=sticker_field, inline=False)

        # Build embeds list (main + edit history)
        embeds = [embed]
        if cached and cached.get('edits'):
            history_embeds = self._build_edit_history_field(cached['edits'])
            if history_embeds:
                embeds.append(discord.Embed(
                    title=f"📝 This message was edited {len(cached['edits'])} time(s) before deletion",
                    color=discord.Color.dark_gold()
                ))
                embeds.extend(history_embeds)

        # Send in batches of 10 embeds (Discord limit)
        try:
            for i in range(0, len(embeds), 10):
                batch = embeds[i:i + 10]
                # Attach files only to the first batch
                if i == 0 and files:
                    await channel.send(embeds=batch, files=files)
                else:
                    await channel.send(embeds=batch)
                if len(embeds) > 10:
                    await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"Failed to log message delete: {e}")

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        """Handle deletes for messages not in Discord.py's RAM cache."""
        # If Discord.py had the message cached, on_message_delete already handled it
        if payload.cached_message is not None:
            return

        channel = await self.get_log_channel()
        if not channel:
            return

        cached = self._cache.pop(str(payload.message_id), None)
        self._dirty = True

        if cached:
            embed = discord.Embed(
                title="🗑️ Message Deleted (Recovered from Cache)",
                description=cached.get('text') or "*No text content*",
                color=discord.Color.red(),
                timestamp=discord.utils.utcnow()
            )
            embed.set_author(
                name=f"{cached.get('aname', '?')} ({cached.get('aid', '?')})",
                icon_url=cached.get('aav') or None
            )
            embed.add_field(name="Channel", value=f"<#{payload.channel_id}>", inline=True)
            embed.add_field(name="Message ID", value=str(payload.message_id), inline=True)

            # Attempt attachment downloads
            files = []
            recovered_names = set()
            att_list = cached.get('atts', [])
            for att in att_list:
                f = await self._try_download_attachment(att)
                if f:
                    files.append(f)
                    recovered_names.add(att.get('fn', ''))

            att_field = self._build_attachment_field(att_list, recovered_names)
            if att_field:
                embed.add_field(name="Attachments", value=att_field, inline=False)

            # Embed URLs
            embed_urls = cached.get('embed_urls', [])
            if embed_urls:
                embed.add_field(
                    name="Embedded Media",
                    value="\n".join([f"🔗 [Link]({u})" for u in embed_urls[:5]]),
                    inline=False
                )

            # Stickers
            sticker_field = self._build_sticker_field(cached.get('stickers', []))
            if sticker_field:
                embed.add_field(name="Stickers", value=sticker_field, inline=False)

            # Edit history
            embeds = [embed]
            if cached.get('edits'):
                history_embeds = self._build_edit_history_field(cached['edits'])
                if history_embeds:
                    embeds.append(discord.Embed(
                        title=f"📝 This message was edited {len(cached['edits'])} time(s) before deletion",
                        color=discord.Color.dark_gold()
                    ))
                    embeds.extend(history_embeds)

            try:
                for i in range(0, len(embeds), 10):
                    batch = embeds[i:i + 10]
                    if i == 0 and files:
                        await channel.send(embeds=batch, files=files)
                    else:
                        await channel.send(embeds=batch)
                    if len(embeds) > 10:
                        await asyncio.sleep(0.5)
            except Exception:
                pass
        else:
            # Complete unknown — not in our cache at all
            embed = discord.Embed(
                title="🗑️ Old Message Deleted (Unknown)",
                description="*This message is older than the 30-day cache window. Its content is unknown.*",
                color=discord.Color.dark_red(),
                timestamp=discord.utils.utcnow()
            )
            embed.add_field(name="Channel", value=f"<#{payload.channel_id}>", inline=True)
            embed.add_field(name="Message ID", value=str(payload.message_id), inline=True)

            try:
                await channel.send(embed=embed)
            except Exception:
                pass

    # ─── BULK DELETE ───────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages: list[discord.Message]):
        channel = await self.get_log_channel()
        if not channel:
            return

        messages.sort(key=lambda m: m.created_at)

        # Title embed
        purge_title = discord.Embed(
            title=f"🧹 Bulk Delete: {len(messages)} Messages Purged",
            description=f"Channel: {messages[0].channel.mention}",
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow()
        )
        try:
            await channel.send(embed=purge_title)
        except Exception:
            return

        # Process each message — batch attachment downloads
        embeds_batch = []
        files_batch = []

        for msg in messages:
            cached = self._cache.pop(str(msg.id), None)
            self._dirty = True

            if not msg.content and not msg.attachments and not msg.embeds and not msg.stickers:
                if not cached or (not cached.get('text') and not cached.get('atts')):
                    continue

            content = msg.content or (cached.get('text') if cached else '') or "*No text*"

            embed = discord.Embed(
                description=content[:4000],
                color=discord.Color.dark_orange(),
                timestamp=msg.created_at
            )
            name = f"{msg.author.name} (Bot)" if msg.author.bot else f"{msg.author.name}"
            avatar_url = msg.author.display_avatar.url if msg.author.display_avatar else None
            if avatar_url:
                embed.set_author(name=name, icon_url=avatar_url)
            else:
                embed.set_author(name=name)

            # Attempt attachment downloads
            att_list = self._extract_attachment_meta(msg) if msg.attachments else []
            if not att_list and cached:
                att_list = cached.get('atts', [])

            recovered_names = set()
            for att in att_list:
                if len(files_batch) < BULK_ATTACHMENT_BATCH:
                    f = await self._try_download_attachment(att)
                    if f:
                        files_batch.append(f)
                        recovered_names.add(att.get('fn', ''))

            att_field = self._build_attachment_field(att_list, recovered_names)
            if att_field:
                embed.add_field(name="Attachments", value=att_field, inline=False)

            # Stickers
            sticker_names = self._extract_sticker_names(msg)
            if not sticker_names and cached:
                sticker_names = cached.get('stickers', [])
            sticker_field = self._build_sticker_field(sticker_names)
            if sticker_field:
                embed.add_field(name="Stickers", value=sticker_field, inline=False)

            embed.set_footer(text=f"Msg ID: {msg.id}")
            embeds_batch.append(embed)

            # Send in batches of 10 embeds
            if len(embeds_batch) >= 10:
                try:
                    await channel.send(embeds=embeds_batch, files=files_batch if files_batch else [])
                    await asyncio.sleep(1)  # Rate limit protection
                except Exception as e:
                    logger.error(f"Failed to log bulk delete batch: {e}")
                embeds_batch = []
                files_batch = []

        # Final remaining batch
        if embeds_batch:
            try:
                await channel.send(embeds=embeds_batch, files=files_batch if files_batch else [])
            except Exception as e:
                logger.error(f"Failed to log final bulk delete batch: {e}")

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        """Handle bulk deletions for messages NOT in Discord.py's RAM cache."""
        cached_ids = {m.id for m in payload.cached_messages}
        uncached_ids = payload.message_ids - cached_ids
        if not uncached_ids:
            return  # on_bulk_message_delete handled everything

        channel = await self.get_log_channel()
        if not channel:
            return

        # Recover from our JSON cache
        recovered = []
        for msg_id in sorted(uncached_ids):  # Sort by snowflake = chronological
            entry = self._cache.pop(str(msg_id), None)
            if entry:
                recovered.append((msg_id, entry))
                self._dirty = True

        if not recovered:
            return

        purge_title = discord.Embed(
            title=f"🧹 Purge Log (Cache Recovery): {len(recovered)} Messages",
            description=f"Channel: <#{payload.channel_id}>\n*These messages fell out of Discord's RAM but were recovered from the JSON cache.*",
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow()
        )
        try:
            await channel.send(embed=purge_title)
        except Exception:
            return

        embeds_batch = []
        files_batch = []

        for msg_id, entry in recovered:
            if not entry.get('text') and not entry.get('atts'):
                continue

            embed = discord.Embed(
                description=(entry.get('text') or "*No text*")[:4000],
                color=discord.Color.dark_orange()
            )
            embed.set_author(
                name=f"{entry.get('aname', '?')} ({entry.get('aid', '?')})",
                icon_url=entry.get('aav') or None
            )

            # Attachment downloads
            att_list = entry.get('atts', [])
            recovered_names = set()
            for att in att_list:
                if len(files_batch) < BULK_ATTACHMENT_BATCH:
                    f = await self._try_download_attachment(att)
                    if f:
                        files_batch.append(f)
                        recovered_names.add(att.get('fn', ''))

            att_field = self._build_attachment_field(att_list, recovered_names)
            if att_field:
                embed.add_field(name="Attachments", value=att_field, inline=False)

            sticker_field = self._build_sticker_field(entry.get('stickers', []))
            if sticker_field:
                embed.add_field(name="Stickers", value=sticker_field, inline=False)

            embed.set_footer(text=f"Msg ID: {msg_id} [Cache Recovered]")
            embeds_batch.append(embed)

            if len(embeds_batch) >= 10:
                try:
                    await channel.send(embeds=embeds_batch, files=files_batch if files_batch else [])
                    await asyncio.sleep(1)
                except Exception as e:
                    logger.error(f"Failed to log DB-recovered bulk delete batch: {e}")
                embeds_batch = []
                files_batch = []

        if embeds_batch:
            try:
                await channel.send(embeds=embeds_batch, files=files_batch if files_batch else [])
            except Exception as e:
                logger.error(f"Failed to log final recovered bulk delete batch: {e}")

    # ─── MESSAGE EDIT ──────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.author.bot:
            return

        # Ghost edit / Link preview expansion check
        if before.content == after.content:
            return

        channel = await self.get_log_channel()
        if not channel:
            return

        # Update cache with edit history
        msg_key = str(before.id)
        if msg_key in self._cache:
            self._cache[msg_key]['edits'].append({
                "old": before.content or "",
                "new": after.content or "",
                "at": discord.utils.utcnow().isoformat()
            })
            self._cache[msg_key]['text'] = after.content or ""
            self._dirty = True

        # Build log embeds
        embed = discord.Embed(
            title="✏️ Message Edited",
            color=discord.Color.yellow(),
            url=after.jump_url,
            timestamp=discord.utils.utcnow()
        )
        embed.set_author(
            name=f"{before.author.name} ({before.author.id})",
            icon_url=before.author.display_avatar.url if before.author.display_avatar else None
        )
        embed.add_field(name="Channel", value=before.channel.mention, inline=True)
        embed.add_field(name="Jump to Message", value=f"[Click Here]({after.jump_url})", inline=True)

        def split_content(text: str, chunk_size: int = 4000) -> list[str]:
            if not text:
                return ["*No text*"]
            return [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]

        before_chunks = split_content(before.content)
        after_chunks = split_content(after.content)

        embeds = [embed]

        for i, chunk in enumerate(before_chunks):
            e = discord.Embed(
                title=f"Before (Part {i+1})" if len(before_chunks) > 1 else "Before",
                description=chunk,
                color=discord.Color.light_grey()
            )
            embeds.append(e)

        for i, chunk in enumerate(after_chunks):
            e = discord.Embed(
                title=f"After (Part {i+1})" if len(after_chunks) > 1 else "After",
                description=chunk,
                color=discord.Color.yellow()
            )
            embeds.append(e)

        # Show edit count if this message has been edited multiple times
        cached = self._cache.get(msg_key)
        if cached and len(cached.get('edits', [])) > 1:
            embeds.append(discord.Embed(
                description=f"ℹ️ This message has been edited **{len(cached['edits'])}** time(s) total.",
                color=discord.Color.dark_gold()
            ))

        for i in range(0, len(embeds), 10):
            batch = embeds[i:i + 10]
            try:
                await channel.send(embeds=batch)
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"Failed to log message edit: {e}")

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        """Handle edits for messages not in Discord.py's RAM cache."""
        if payload.cached_message is not None:
            return  # on_message_edit already handled it

        channel = await self.get_log_channel()
        if not channel:
            return

        if "content" not in payload.data:
            return  # Likely just an embed unrolling

        # Skip bot-authored messages
        author_data = payload.data.get("author")
        if author_data and author_data.get("bot", False):
            return

        new_content = payload.data.get("content", "*Unknown*")
        msg_key = str(payload.message_id)

        cached = self._cache.get(msg_key)
        if cached:
            old_content = cached.get('text', '')
            if old_content == new_content:
                return  # Ghost edit bypass

            # Append to edit history
            cached['edits'].append({
                "old": old_content,
                "new": new_content,
                "at": discord.utils.utcnow().isoformat()
            })
            cached['text'] = new_content
            self._dirty = True

            embed = discord.Embed(
                title="✏️ Message Edited (Recovered from Cache)",
                color=discord.Color.yellow(),
                url=f"https://discord.com/channels/{payload.guild_id}/{payload.channel_id}/{payload.message_id}",
                timestamp=discord.utils.utcnow()
            )
            embed.set_author(
                name=f"{cached.get('aname', '?')} ({cached.get('aid', '?')})",
                icon_url=cached.get('aav') or None
            )
            embed.add_field(name="Channel", value=f"<#{payload.channel_id}>", inline=True)

            def split_content(text: str, chunk_size: int = 4000) -> list[str]:
                if not text:
                    return ["*No text*"]
                return [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]

            before_chunks = split_content(old_content)
            after_chunks = split_content(new_content)

            embeds = [embed]
            for i, chunk in enumerate(before_chunks):
                e = discord.Embed(
                    title=f"Before (Part {i+1})" if len(before_chunks) > 1 else "Before",
                    description=chunk,
                    color=discord.Color.light_grey()
                )
                embeds.append(e)
            for i, chunk in enumerate(after_chunks):
                e = discord.Embed(
                    title=f"After (Part {i+1})" if len(after_chunks) > 1 else "After",
                    description=chunk,
                    color=discord.Color.yellow()
                )
                embeds.append(e)

            if len(cached.get('edits', [])) > 1:
                embeds.append(discord.Embed(
                    description=f"ℹ️ This message has been edited **{len(cached['edits'])}** time(s) total.",
                    color=discord.Color.dark_gold()
                ))

            for i in range(0, len(embeds), 10):
                batch = embeds[i:i + 10]
                try:
                    await channel.send(embeds=batch)
                except Exception:
                    pass
        else:
            # No cache record at all
            embed = discord.Embed(
                title="✏️ Old Message Edited (Uncached)",
                description="*This message is older than the cache window. Only the new content is available.*",
                color=discord.Color.gold(),
                timestamp=discord.utils.utcnow()
            )
            embed.add_field(name="Channel", value=f"<#{payload.channel_id}>", inline=True)
            embed.add_field(name="Message ID", value=str(payload.message_id), inline=True)

            embeds = [embed]
            for i in range(0, max(len(new_content), 1), 4000):
                chunk = new_content[i:i + 4000] if new_content else "*No text*"
                e = discord.Embed(title="New Content", description=chunk, color=discord.Color.yellow())
                embeds.append(e)

            for i in range(0, len(embeds), 10):
                batch = embeds[i:i + 10]
                try:
                    await channel.send(embeds=batch)
                except Exception:
                    pass

    # ─── VOICE LOGGING ─────────────────────────────────────────────────

    async def get_voice_log_channel(self) -> discord.TextChannel | None:
        """Helper to get the configured voice log channel."""
        channel_id = await settings_service.get_int("voice_log_channel_id")
        if not channel_id:
            return None
        return self.bot.get_channel(channel_id)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        """Log voice channel joins, leaves, and moves."""
        if member.bot:
            return

        channel = await self.get_voice_log_channel()
        if not channel:
            return

        timestamp = discord.utils.utcnow()
        avatar_url = member.display_avatar.url if member.display_avatar else None

        try:
            # User joined a VC (was not in one before)
            if before.channel is None and after.channel is not None:
                embed = discord.Embed(
                    title="🎙️ Voice Channel Join",
                    color=discord.Color.green(),
                    timestamp=timestamp,
                )
                embed.set_author(name=f"{member.name} ({member.id})", icon_url=avatar_url)
                embed.add_field(name="Channel", value=after.channel.mention, inline=True)
                embed.add_field(name="Members", value=str(len(after.channel.members)), inline=True)
                await channel.send(embed=embed)

            # User left a VC (not in one after)
            elif before.channel is not None and after.channel is None:
                embed = discord.Embed(
                    title="🔇 Voice Channel Leave",
                    color=discord.Color.red(),
                    timestamp=timestamp,
                )
                embed.set_author(name=f"{member.name} ({member.id})", icon_url=avatar_url)
                embed.add_field(name="Channel", value=before.channel.mention, inline=True)
                embed.add_field(name="Members Left", value=str(len(before.channel.members)), inline=True)
                await channel.send(embed=embed)

            # User moved between VCs
            elif before.channel is not None and after.channel is not None and before.channel.id != after.channel.id:
                embed = discord.Embed(
                    title="🔀 Voice Channel Move",
                    color=discord.Color.blue(),
                    timestamp=timestamp,
                )
                embed.set_author(name=f"{member.name} ({member.id})", icon_url=avatar_url)
                embed.add_field(name="From", value=before.channel.mention, inline=True)
                embed.add_field(name="To", value=after.channel.mention, inline=True)
                await channel.send(embed=embed)

        except discord.HTTPException as e:
            logger.warning(f"Failed to log voice state update: {e}")

    @commands.Cog.listener()
    async def on_guild_channel_create(self, created_channel: discord.abc.GuildChannel):
        """Log voice channel creation."""
        if not isinstance(created_channel, discord.VoiceChannel):
            return

        channel = await self.get_voice_log_channel()
        if not channel:
            return

        embed = discord.Embed(
            title="🔊 Voice Channel Created",
            color=discord.Color.teal(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Channel", value=f"{created_channel.mention} (`{created_channel.name}`)", inline=True)
        if created_channel.category:
            embed.add_field(name="Category", value=created_channel.category.name, inline=True)

        try:
            await channel.send(embed=embed)
        except discord.HTTPException as e:
            logger.warning(f"Failed to log VC creation: {e}")

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, deleted_channel: discord.abc.GuildChannel):
        """Log voice channel deletion."""
        if not isinstance(deleted_channel, discord.VoiceChannel):
            return

        channel = await self.get_voice_log_channel()
        if not channel:
            return

        embed = discord.Embed(
            title="🔈 Voice Channel Deleted",
            color=discord.Color.dark_grey(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Channel", value=f"`{deleted_channel.name}`", inline=True)
        if deleted_channel.category:
            embed.add_field(name="Category", value=deleted_channel.category.name, inline=True)

        try:
            await channel.send(embed=embed)
        except discord.HTTPException as e:
            logger.warning(f"Failed to log VC deletion: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(LogCog(bot))
