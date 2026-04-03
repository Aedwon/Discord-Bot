"""
Counting Cog - Channel-based counting game.
Users count sequentially starting from 1 in a designated channel.
The bot validates each number and reacts accordingly.

Rules:
  - Wrong number → immediate chain break (❌ + reply + reset to 1)
  - Same user counting twice in a row → warn first, break on repeat
  - Non-number message → warn first, break on repeat
  - Warnings reset when the user counts correctly or the chain resets
"""

import discord
from discord.ext import commands
import logging

from services.database import db
from services.settings_service import settings_service

logger = logging.getLogger("mlbb_bot.counting")


class CountingCog(commands.Cog, name="Counting"):
    """Channel-based counting game."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # RAM cache: {guild_id: {"count": int, "last_user_id": int|None}}
        self._state: dict[int, dict] = {}

        # Warned users per guild: {guild_id: set(user_id)}
        # Tracks users who have already received a warning for the CURRENT chain.
        self._warned: dict[int, set[int]] = {}

        # Cached counting channel ID (loaded once, invalidated on setting change)
        self._channel_id: int | None = None

    async def cog_load(self):
        """Load counting state from DB into RAM cache."""
        try:
            # Safe migration: add high score columns if missing
            for col, col_def in [("high_score", "INT NOT NULL DEFAULT 0"), 
                                 ("high_score_broken_by", "BIGINT DEFAULT NULL"),
                                 ("last_message_id", "BIGINT DEFAULT NULL")]:
                try:
                    await db.execute(f"ALTER TABLE counting_state ADD COLUMN {col} {col_def}")
                except Exception:
                    pass  # Column already exists


            # Safe migration: create contributor tables
            try:
                await db.execute('''
                    CREATE TABLE IF NOT EXISTS counting_current_contributors (
                        guild_id BIGINT,
                        user_id BIGINT,
                        count INT DEFAULT 0,
                        PRIMARY KEY (guild_id, user_id)
                    )
                ''')
                await db.execute('''
                    CREATE TABLE IF NOT EXISTS counting_highscore_contributors (
                        guild_id BIGINT,
                        user_id BIGINT,
                        count INT DEFAULT 0,
                        PRIMARY KEY (guild_id, user_id)
                    )
                ''')
            except Exception as e:
                logger.error(f"Counting: Failed to create contributor tables: {e}")

            rows = await db.fetch_all(
                "SELECT guild_id, current_count, last_user_id, high_score, high_score_broken_by, last_message_id FROM counting_state"
            )
            for row in rows:
                self._state[row["guild_id"]] = {
                    "count": row["current_count"],
                    "last_user_id": row["last_user_id"],
                    "high_score": row.get("high_score", 0) or 0,
                    "high_score_broken_by": row.get("high_score_broken_by"),
                    "last_message_id": row.get("last_message_id"),
                }
            logger.info(f"Counting: Loaded state for {len(rows)} guild(s).")
        except Exception as e:
            logger.error(f"Counting: Failed to load state: {e}")

    # ─── Helpers ─────────────────────────────────────────────────────

    async def _get_channel_id(self) -> int | None:
        """Get the counting channel ID, with simple caching."""
        if self._channel_id is None:
            self._channel_id = await settings_service.get_int("counting_channel_id") or 0
        return self._channel_id if self._channel_id != 0 else None

    def _get_state(self, guild_id: int) -> dict:
        """Get or initialise the counting state for a guild."""
        if guild_id not in self._state:
            self._state[guild_id] = {
                "count": 0,
                "last_user_id": None,
                "last_message_id": None,
                "high_score": 0,
                "high_score_broken_by": None,
            }
        return self._state[guild_id]

    async def _save_state(self, guild_id: int, count: int, last_user_id: int | None, last_message_id: int | None):
        """Persist counting state to DB."""
        state = self._get_state(guild_id)
        state["count"] = count
        state["last_user_id"] = last_user_id
        state["last_message_id"] = last_message_id

        try:
            await db.execute(
                """INSERT INTO counting_state (guild_id, current_count, last_user_id, high_score, high_score_broken_by, last_message_id)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON DUPLICATE KEY UPDATE current_count = VALUES(current_count),
                                           last_user_id = VALUES(last_user_id),
                                           high_score = VALUES(high_score),
                                           high_score_broken_by = VALUES(high_score_broken_by),
                                           last_message_id = VALUES(last_message_id)""",
                (guild_id, count, last_user_id, state["high_score"], state["high_score_broken_by"], last_message_id),
            )
        except Exception as e:
            logger.error(f"Counting: Failed to save state for guild {guild_id}: {e}")

    async def _react_safe(self, message: discord.Message, emoji: str):
        """Add a reaction, silently ignoring permission/rate-limit errors."""
        try:
            await message.add_reaction(emoji)
        except (discord.Forbidden, discord.NotFound):
            pass
        except discord.HTTPException as e:
            logger.warning(f"Counting: Reaction failed ({e.status}): {e.text}")

    async def _reply_safe(self, message: discord.Message, content: str):
        """Reply to a message, silently ignoring errors."""
        try:
            await message.reply(content, mention_author=False)
        except (discord.Forbidden, discord.NotFound):
            pass
        except discord.HTTPException as e:
            logger.warning(f"Counting: Reply failed ({e.status}): {e.text}")

    async def _break_chain(self, message: discord.Message, guild_id: int, reason: str, is_offline_sync: bool = False):
        """Break the chain: check high score, react ❌, reply, reset count."""
        state = self._get_state(guild_id)
        old_count = state["count"]
        new_record = False

        # Check if this was a new high score
        if old_count > state["high_score"]:
            state["high_score"] = old_count
            state["high_score_broken_by"] = message.author.id
            new_record = True
            try:
                # Copy current contributors to highscore contributors
                await db.execute("DELETE FROM counting_highscore_contributors WHERE guild_id = %s", (guild_id,))
                await db.execute(
                    "INSERT INTO counting_highscore_contributors SELECT * FROM counting_current_contributors WHERE guild_id = %s", 
                    (guild_id,)
                )
            except Exception as e:
                logger.error(f"Counting: Failed to save highscore contributors: {e}")

        # Reset state
        state["count"] = 0
        state["last_user_id"] = None
        self._warned.pop(guild_id, None)
        try:
            await db.execute("DELETE FROM counting_current_contributors WHERE guild_id = %s", (guild_id,))
        except Exception as e:
            logger.error(f"Counting: Failed to reset current contributors: {e}")

        await self._save_state(guild_id, 0, None, message.id)
        if not is_offline_sync:
            await self._react_safe(message, "❌")

        reply_text = (
            f"💥 **{message.author.display_name}** broke the chain at **{old_count}**! "
            f"{reason} The count resets to **1**."
        )
        if new_record and old_count > 0:
            reply_text += f"\n🏆 **New record!** Previous best was beaten — highest streak is now **{old_count}**!"
        await self._reply_safe(message, reply_text)

    async def _warn_user(self, message: discord.Message, guild_id: int, warning: str):
        """Warn a user without breaking the chain."""
        warned = self._warned.setdefault(guild_id, set())
        warned.add(message.author.id)

        await self._react_safe(message, "⚠️")
        await self._reply_safe(message, warning)

    async def _process_counting_message(self, message: discord.Message, is_offline_sync: bool = False):
        guild_id = message.guild.id
        state = self._get_state(guild_id)
        user_id = message.author.id
        content = message.content.strip()
        warned_set = self._warned.get(guild_id, set())



        # ── Non-number message ──
        if not content.isdigit():
            if user_id in warned_set:
                await self._break_chain(message, guild_id, "Non-number messages aren't allowed here.", is_offline_sync)
            else:
                if not is_offline_sync:
                    await self._warn_user(
                        message, guild_id,
                        "⚠️ Only numbers are allowed in the counting channel! "
                        "Next time, the chain will break."
                    )
            return

        number = int(content)
        expected = state["count"] + 1

        # ── Same user counting twice in a row ──
        if state["last_user_id"] == user_id:
            if user_id in warned_set:
                await self._break_chain(message, guild_id, "You can't count twice in a row!", is_offline_sync)
            else:
                if not is_offline_sync:
                    await self._warn_user(
                        message, guild_id,
                        "⚠️ You can't count twice in a row! "
                        "Let someone else go next. Another attempt will break the chain."
                    )
            return

        # ── Wrong number ──
        if number != expected:
            await self._break_chain(
                message, guild_id,
                f"Expected **{expected}**, got **{number}**.",
                is_offline_sync
            )
            return

        # ── Correct number ──
        state["count"] = number
        state["last_user_id"] = user_id

        # Update contributors
        try:
            await db.execute(
                """INSERT INTO counting_current_contributors (guild_id, user_id, count)
                   VALUES (%s, %s, 1)
                   ON DUPLICATE KEY UPDATE count = count + 1""",
                (guild_id, user_id)
            )
        except Exception as e:
            logger.error(f"Counting: Failed to update current contributors: {e}")

        # Clear this user's warning if they had one (they counted correctly)
        if user_id in warned_set:
            warned_set.discard(user_id)

        if not is_offline_sync:
            await self._react_safe(message, "✅")
        await self._save_state(guild_id, number, user_id, message.id)

    # ─── Listeners ───────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Ignore bots and DMs
        if message.author.bot or not message.guild:
            return

        # Check if this is the counting channel
        channel_id = await self._get_channel_id()
        if not channel_id or message.channel.id != channel_id:
            return

        await self._process_counting_message(message, is_offline_sync=False)

    @commands.Cog.listener()
    async def on_ready(self):
        """When bot boots, sweep counting channels to catch up securely."""
        import asyncio
        asyncio.create_task(self._sync_offline_counts())

    async def _sync_offline_counts(self):
        """Retroactively digest messages posted while the bot was offline."""
        await self.bot.wait_until_ready()
        channel_id = await self._get_channel_id()
        if not channel_id:
            return

        for guild in self.bot.guilds:
            channel = guild.get_channel(channel_id)
            if not channel:
                continue
            
            state = self._get_state(guild.id)
            last_msg_id = state.get("last_message_id")
            
            if not last_msg_id:
                # Can't sync without an anchored starting point
                continue
                
            try:
                # Fetch history ascending purely after the last verified DB node
                logger.info(f"Counting: Initializing offline sync for guild {guild.id} starting from {last_msg_id}...")
                offline_messages = []
                async for msg in channel.history(limit=100, after=discord.Object(id=last_msg_id), oldest_first=True):
                    if not msg.author.bot:
                        offline_messages.append(msg)
                
                # Sequentially digest and validate the backlog
                if offline_messages:
                    for msg in offline_messages:
                        await self._process_counting_message(msg, is_offline_sync=True)
                    logger.info(f"Counting: Offline sync digested {len(offline_messages)} missed valid/invalid inputs.")
            except Exception as e:
                logger.error(f"Counting: Offline sync collapsed cleanly {e}")

    # ─── Invalidate channel cache when settings change ────────────

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        """Invalidate the channel ID cache if an admin changes the counting channel setting."""
        if interaction.type != discord.InteractionType.application_command:
            return

        data = interaction.data or {}
        cmd_name = data.get("name", "")

        # If the setup command was used, clear the cache so it reloads
        if cmd_name == "setup":
            self._channel_id = None


async def setup(bot: commands.Bot):
    await bot.add_cog(CountingCog(bot))
