"""
Pomodoro Cog - VC-based Pomodoro timer.
Users start a session in a temp VC; the bot cycles through work/break phases,
renames the VC, and pings participants at each transition.

Cycle: 25 min work → 5 min break (×4), then 15 min long break.
"""

import asyncio
import discord
import json
from dataclasses import dataclass, field
from discord.ext import commands
from discord import app_commands
import logging

from services.database import db

logger = logging.getLogger("mlbb_bot.pomodoro")

# Phase durations in seconds
WORK_DURATION = 25 * 60
SHORT_BREAK = 5 * 60
LONG_BREAK = 15 * 60
POMODOROS_BEFORE_LONG_BREAK = 4


@dataclass
class PomodoroSession:
    vc_id: int
    creator_id: int
    participants: set[int] = field(default_factory=set)
    pomodoro_count: int = 0
    phase: str = "work"  # "work" | "break" | "long_break"
    task: asyncio.Task | None = None
    original_vc_name: str = ""


class PomodoroCog(commands.Cog, name="Pomodoro"):
    """VC-based Pomodoro timer system."""

    pomodoro_group = app_commands.Group(
        name="pomodoro",
        description="Pomodoro timer for voice channels",
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Active sessions keyed by VC channel ID
        self._sessions: dict[int, PomodoroSession] = {}

    @commands.Cog.listener()
    async def on_ready(self):
        """Restore sessions from DB on startup or handle orphaned ones."""
        # Wait a bit for bot to fully connect to guilds
        await asyncio.sleep(2)
        
        rows = await db.fetch_all("SELECT * FROM pomodoro_sessions")
        if not rows:
            return
            
        logger.info(f"Pomodoro: Found {len(rows)} potential sessions in DB.")
        for row in rows:
            vc_id = row['vc_id']
            guild = self.bot.guilds[0] if self.bot.guilds else None
            channel = guild.get_channel(vc_id) if guild else None
            
            # If channel no longer exists or is empty, clean up
            if not channel or not isinstance(channel, discord.VoiceChannel) or not channel.members:
                logger.info(f"Pomodoro: Cleaning up orphaned session in VC {vc_id}")
                if channel:
                    await self._rename_vc_safe(channel, row['original_name'])
                await db.execute("DELETE FROM pomodoro_sessions WHERE vc_id = %s", (vc_id,))
                continue
                
            # Restore session object
            try:
                participants_list = json.loads(row['participants'])
                participants = set(participants_list)
            except:
                participants = {row['creator_id']}
                
            session = PomodoroSession(
                vc_id=vc_id,
                creator_id=row['creator_id'],
                participants=participants,
                pomodoro_count=row['pomodoro_count'],
                original_vc_name=row['original_name']
            )
            self._sessions[vc_id] = session
            # Start the background task to resume the cycle
            session.task = asyncio.create_task(self._run_session(vc_id))
            logger.info(f"Pomodoro: Restored active session in VC {vc_id}")

    def cog_unload(self):
        """Cancel all running sessions on cog unload."""
        for session in self._sessions.values():
            if session.task and not session.task.done():
                session.task.cancel()
        self._sessions.clear()

    # ─── Helpers ─────────────────────────────────────────────────────

    def _get_voice_cog(self):
        """Get the VoiceCog instance to check temp_channels."""
        return self.bot.get_cog("Voice")

    def _is_temp_vc(self, channel_id: int) -> bool:
        """Check if a channel is a temp VC (managed by VoiceCog)."""
        voice_cog = self._get_voice_cog()
        if not voice_cog:
            return False
        return channel_id in voice_cog.temp_channels

    async def _rename_vc_safe(self, channel: discord.VoiceChannel, name: str):
        """Rename a VC, silently handling rate limits and errors."""
        try:
            await channel.edit(name=name)
        except discord.HTTPException as e:
            if e.status == 429:
                logger.warning(f"Pomodoro: Rate limited on VC rename, skipping.")
            else:
                logger.warning(f"Pomodoro: Failed to rename VC: {e}")

    async def _ping_participants(
        self, channel: discord.VoiceChannel, session: PomodoroSession, message: str
    ):
        """Send a ping to all active participants in the VC text chat."""
        # Filter to only participants still in the VC
        in_vc = {m.id for m in channel.members}
        active = session.participants & in_vc

        if not active:
            return

        mentions = " ".join(f"<@{uid}>" for uid in active)
        try:
            await channel.send(f"{mentions} {message}")
        except discord.Forbidden:
            logger.warning(f"Pomodoro: Cannot send messages in VC {channel.id}")
        except discord.HTTPException as e:
            logger.warning(f"Pomodoro: Failed to send ping: {e}")

    async def _end_session(self, vc_id: int, reason: str = "Session ended."):
        """End a pomodoro session: cancel task, restore VC name, notify, and clear DB."""
        session = self._sessions.pop(vc_id, None)
        if not session:
            return

        # Cancel the background task
        if session.task and not session.task.done():
            session.task.cancel()

        # Remove from DB
        try:
            await db.execute("DELETE FROM pomodoro_sessions WHERE vc_id = %s", (vc_id,))
        except Exception as e:
            logger.error(f"Pomodoro: Failed to remove session {vc_id} from DB: {e}")

        # Restore VC name
        guild = self.bot.guilds[0] if self.bot.guilds else None
        if guild:
            channel = guild.get_channel(vc_id)
            if channel:
                await self._rename_vc_safe(channel, session.original_vc_name)
                try:
                    await channel.send(f"🛑 **Pomodoro ended.** {reason}")
                except (discord.Forbidden, discord.HTTPException):
                    pass

    # ─── Background Session Task ─────────────────────────────────────

    async def _run_session(self, vc_id: int):
        """Main loop: cycle through work/break phases until cancelled."""
        try:
            session = self._sessions.get(vc_id)
            if not session:
                return

            guild = self.bot.guilds[0] if self.bot.guilds else None
            if not guild:
                return

            while vc_id in self._sessions:
                session = self._sessions.get(vc_id)
                if not session or not session.participants:
                    break

                channel = guild.get_channel(vc_id)
                if not channel:
                    break

                # ── WORK PHASE ──
                session.phase = "work"
                pomo_num = (session.pomodoro_count % POMODOROS_BEFORE_LONG_BREAK) + 1
                display_name = session.original_vc_name

                await self._rename_vc_safe(
                    channel, f"{display_name} 🍅 Work ({pomo_num}/{POMODOROS_BEFORE_LONG_BREAK})"
                )
                await self._ping_participants(
                    channel, session,
                    f"🍅 **Work time!** {WORK_DURATION // 60} minutes — stay focused! "
                    f"(Pomodoro {pomo_num}/{POMODOROS_BEFORE_LONG_BREAK})"
                )

                await asyncio.sleep(WORK_DURATION)

                # Re-check session still exists after sleep
                if vc_id not in self._sessions:
                    break
                session = self._sessions[vc_id]
                channel = guild.get_channel(vc_id)
                if not channel or not session.participants:
                    break

                # ── BREAK PHASE ──
                session.pomodoro_count += 1
                # Persist progress to DB
                try:
                    await db.execute("UPDATE pomodoro_sessions SET pomodoro_count = %s WHERE vc_id = %s", 
                                     (session.pomodoro_count, vc_id))
                except Exception as e:
                    logger.error(f"Pomodoro: Failed to update count for {vc_id} in DB: {e}")

                is_long = (session.pomodoro_count % POMODOROS_BEFORE_LONG_BREAK) == 0
                break_duration = LONG_BREAK if is_long else SHORT_BREAK
                phase_label = "long_break" if is_long else "break"
                session.phase = phase_label

                if is_long:
                    emoji, label = "🌴", "Long Break"
                else:
                    emoji, label = "☕", "Break"

                await self._rename_vc_safe(channel, f"{display_name} {emoji} {label}")
                await self._ping_participants(
                    channel, session,
                    f"{emoji} **{label}!** {break_duration // 60} minutes — relax! "
                    f"(Completed {session.pomodoro_count} pomodoro{'s' if session.pomodoro_count != 1 else ''})"
                )

                await asyncio.sleep(break_duration)

                # Re-check after sleep
                if vc_id not in self._sessions:
                    break

        except asyncio.CancelledError:
            pass  # Clean cancellation
        except Exception as e:
            logger.error(f"Pomodoro: Session error in VC {vc_id}: {e}")
        finally:
            # Cleanup if session is still tracked (abnormal exit)
            if vc_id in self._sessions:
                await self._end_session(vc_id, "Session ended unexpectedly.")

    # ─── Commands ────────────────────────────────────────────────────

    @pomodoro_group.command(name="start", description="Start a Pomodoro session in your current VC")
    @app_commands.describe(
        user1="Optional user to include",
        user2="Optional user to include",
        user3="Optional user to include",
        user4="Optional user to include",
    )
    async def pomodoro_start(
        self,
        interaction: discord.Interaction,
        user1: discord.Member | None = None,
        user2: discord.Member | None = None,
        user3: discord.Member | None = None,
        user4: discord.Member | None = None,
    ):
        """Start a Pomodoro timer in the user's current temp VC."""
        await interaction.response.defer(ephemeral=True)

        # Must be in a voice channel
        if not interaction.user.voice or not interaction.user.voice.channel:
            return await interaction.followup.send(
                "❌ You must be in a voice channel to start a Pomodoro.", ephemeral=True
            )

        vc = interaction.user.voice.channel
        if not isinstance(vc, discord.VoiceChannel):
            return await interaction.followup.send(
                "❌ Pomodoro can only be used in voice channels.", ephemeral=True
            )

        # Must be a temp VC
        if not self._is_temp_vc(vc.id):
            return await interaction.followup.send(
                "❌ Pomodoro can only be started in auto-created voice channels.",
                ephemeral=True,
            )

        # No duplicate sessions
        if vc.id in self._sessions:
            return await interaction.followup.send(
                "❌ A Pomodoro session is already running in this VC. "
                "Use `/pomodoro end all` to stop it first.",
                ephemeral=True,
            )

        # Check bot can send messages in the VC text chat
        permissions = vc.permissions_for(interaction.guild.me)
        if not permissions.send_messages:
            return await interaction.followup.send(
                "❌ I don't have permission to send messages in this VC's text chat.",
                ephemeral=True,
            )

        # Build participant set — creator + additional users (must be in the same VC)
        participants = {interaction.user.id}
        added = []
        skipped = []

        for user in [user1, user2, user3, user4]:
            if user is None or user.id == interaction.user.id:
                continue
            if user.bot:
                skipped.append(f"{user.display_name} (bot)")
                continue
            if user.voice and user.voice.channel and user.voice.channel.id == vc.id:
                participants.add(user.id)
                added.append(user.display_name)
            else:
                skipped.append(f"{user.display_name} (not in this VC)")

        # Create session
        session = PomodoroSession(
            vc_id=vc.id,
            creator_id=interaction.user.id,
            participants=participants,
            original_vc_name=vc.name,
        )
        self._sessions[vc.id] = session

        # Persist to DB
        try:
            await db.execute(
                "INSERT INTO pomodoro_sessions (vc_id, creator_id, original_name, participants, pomodoro_count) VALUES (%s, %s, %s, %s, %s)",
                (vc.id, interaction.user.id, vc.name, json.dumps(list(participants)), 0)
            )
        except Exception as e:
            logger.error(f"Pomodoro: Failed to save session {vc.id} to DB: {e}")

        # Start the background task
        session.task = asyncio.create_task(self._run_session(vc.id))

        # Confirmation
        parts = [f"✅ **Pomodoro started!** Cycle: 25 min work → 5 min break (15 min break every 4th)."]
        parts.append(f"👥 **Participants:** {', '.join(f'<@{uid}>' for uid in participants)}")
        if skipped:
            parts.append(f"⚠️ **Skipped:** {', '.join(skipped)}")
        parts.append("Use `/pomodoro leave` to leave, or `/pomodoro stop` to end for everyone.")

        await interaction.followup.send("\n".join(parts), ephemeral=True)

    @pomodoro_group.command(name="add", description="Add a user to the active Pomodoro session")
    @app_commands.describe(user="The user to add (must be in the same VC)")
    async def pomodoro_add(self, interaction: discord.Interaction, user: discord.Member):
        """Add a new user to the session. Creator only."""
        if not interaction.user.voice or not interaction.user.voice.channel:
            return await interaction.response.send_message(
                "❌ You must be in a voice channel.", ephemeral=True
            )

        vc = interaction.user.voice.channel
        session = self._sessions.get(vc.id)

        if not session:
            return await interaction.response.send_message(
                "❌ No active Pomodoro session in this VC.", ephemeral=True
            )

        if interaction.user.id != session.creator_id:
            return await interaction.response.send_message(
                "❌ Only the session creator can add people.", ephemeral=True
            )

        if user.bot:
            return await interaction.response.send_message(
                "❌ Cannot add bots to a Pomodoro session.", ephemeral=True
            )

        if user.id in session.participants:
            return await interaction.response.send_message(
                f"❌ {user.display_name} is already in this session.", ephemeral=True
            )

        if not user.voice or not user.voice.channel or user.voice.channel.id != vc.id:
            return await interaction.response.send_message(
                f"❌ {user.display_name} is not in this VC.", ephemeral=True
            )

        session.participants.add(user.id)
        # Update DB
        try:
            await db.execute(
                "UPDATE pomodoro_sessions SET participants = %s WHERE vc_id = %s",
                (json.dumps(list(session.participants)), vc.id)
            )
        except Exception as e:
            logger.error(f"Pomodoro: Failed to update participants for {vc.id} in DB: {e}")

        await interaction.response.send_message(
            f"✅ **{user.display_name}** has been added to the Pomodoro session.", ephemeral=True
        )

        # Notify the added user in the VC text chat
        try:
            await vc.send(
                f"📢 {user.mention} has been added to the Pomodoro session! "
                f"Currently in **{session.phase.replace('_', ' ')}** phase."
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

    @pomodoro_group.command(name="leave", description="Leave the Pomodoro session (stop getting pinged)")
    async def pomodoro_leave(self, interaction: discord.Interaction):
        """Remove yourself from the active session."""
        if not interaction.user.voice or not interaction.user.voice.channel:
            return await interaction.response.send_message(
                "❌ You must be in a voice channel.", ephemeral=True
            )

        vc = interaction.user.voice.channel
        session = self._sessions.get(vc.id)

        if not session:
            return await interaction.response.send_message(
                "❌ No active Pomodoro session in this VC.", ephemeral=True
            )

        if interaction.user.id not in session.participants:
            return await interaction.response.send_message(
                "❌ You're not in this Pomodoro session.", ephemeral=True
            )

        session.participants.discard(interaction.user.id)
        # Update DB
        try:
            await db.execute(
                "UPDATE pomodoro_sessions SET participants = %s WHERE vc_id = %s",
                (json.dumps(list(session.participants)), vc.id)
            )
        except Exception as e:
            logger.error(f"Pomodoro: Failed to update participants for {vc.id} in DB: {e}")

        await interaction.response.send_message(
            "✅ You've left the Pomodoro session. You will no longer be pinged.",
            ephemeral=True,
        )

        # If creator leaves, end for all
        if interaction.user.id == session.creator_id:
            await self._end_session(vc.id, "The session creator has left.")
        # If no participants remain, end session
        elif not session.participants:
            await self._end_session(vc.id, "No participants remaining.")

    @pomodoro_group.command(name="stop", description="End the Pomodoro session for everyone (creator only)")
    async def pomodoro_stop(self, interaction: discord.Interaction):
        """End the entire session. Creator only."""
        if not interaction.user.voice or not interaction.user.voice.channel:
            return await interaction.response.send_message(
                "❌ You must be in a voice channel.", ephemeral=True
            )

        vc = interaction.user.voice.channel
        session = self._sessions.get(vc.id)

        if not session:
            return await interaction.response.send_message(
                "❌ No active Pomodoro session in this VC.", ephemeral=True
            )

        if interaction.user.id != session.creator_id:
            return await interaction.response.send_message(
                "❌ Only the session creator can stop the session for everyone.",
                ephemeral=True,
            )

        await interaction.response.send_message(
            "✅ Ending Pomodoro session for everyone...", ephemeral=True
        )
        await self._end_session(vc.id, f"Ended by {interaction.user.display_name}.")

    # ─── Voice State Listener ────────────────────────────────────────

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ):
        """Handle participants leaving the VC."""
        # Only care about leaves/moves FROM a VC with an active session
        if not before.channel:
            return

        vc_id = before.channel.id
        session = self._sessions.get(vc_id)
        if not session:
            return

        # User stayed in the same channel (mute/deaf change)
        if after.channel and after.channel.id == vc_id:
            return

        # User left or moved to a different channel
        if member.id == session.creator_id:
            # Creator left → end entire session
            await self._end_session(vc_id, f"{member.display_name} (creator) left the VC.")
        elif member.id in session.participants:
            # Participant left → remove them
            session.participants.discard(member.id)

            if not session.participants:
                await self._end_session(vc_id, "All participants have left.")

        # If the VC is now empty, the voice_cog will delete it.
        # The channel deletion will cause our task to fail on the next
        # channel lookup and clean up via the finally block.


async def setup(bot: commands.Bot):
    await bot.add_cog(PomodoroCog(bot))
