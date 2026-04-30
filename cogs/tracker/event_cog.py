"""
Event Tracker Cog - Hooks natively into Discord Scheduled Events for Kiosks and Placements.
Built with robust anti-spasm race condition locks, validation boundaries, and UI defers.
Features an End-To-End Security Toolkit: Dashboards, Revocation, Budgets, Audit Logs.
Includes an ultra-efficient RAM Cached Peak Voice Tracking system for Overflow channels.
Raffle creation uses a two-step Modal flow for multiline requirements support.
"""


import discord
from discord.ext import commands
from discord import app_commands
import logging
import csv
import io
import json
from datetime import datetime, timedelta, timezone
import base64
from urllib.parse import urlparse, parse_qs
import asyncio

from services.database import db
from services.settings_service import settings_service
from services.verification_service import verification_service
from utils.checks import require_admin_auth

logger = logging.getLogger("mlbb_bot.event_cog")

class EventAwardView(discord.ui.View):
    def __init__(self, ev_id: int):
        super().__init__(timeout=180)
        self.ev_id = ev_id

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Select User to Award")
    async def select_user(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        self.user = select.values[0]
        await interaction.response.defer()

    @discord.ui.button(label="Next: Select Prize", style=discord.ButtonStyle.primary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not hasattr(self, 'user'):
            return await interaction.response.send_message("❌ Select a user first.", ephemeral=True)

        pools = await db.fetch_all("SELECT * FROM event_prize_pools WHERE event_id = %s AND LOWER(placement_name) != 'participation'", (self.ev_id,))
        if not pools:
            return await interaction.response.send_message("❌ No prize pools defined for this event.", ephemeral=True)

        options = []
        for p in pools:
            c = await db.fetch_one("SELECT COUNT(*) as c FROM guild_event_rewards WHERE event_id=%s AND reward_type=%s", (self.ev_id, p['placement_name']))
            cnt = c['c'] if c else 0
            avail = p['max_winners'] - cnt
            if avail > 0:
                desc = f"{p['ep_reward']} EP"
                if p['diamond_reward'] > 0:
                    desc += f" | {p['diamond_reward']} 💎"
                desc += f" ({avail} remaining)"
                options.append(discord.SelectOption(label=p['placement_name'][:25], description=desc, value=str(p['id'])))

        if not options:
            return await interaction.response.send_message("❌ All prizes have been exhausted for this event.", ephemeral=True)

        view = discord.ui.View(timeout=180)
        select = discord.ui.Select(placeholder="Select Prize Tier", options=options[:25])

        async def prize_callback(i: discord.Interaction):
            await i.response.defer(ephemeral=True)
            pid = int(select.values[0])
            prize = await db.fetch_one("SELECT * FROM event_prize_pools WHERE id=%s", (pid,))

            c = await db.fetch_one("SELECT COUNT(*) as c FROM guild_event_rewards WHERE event_id=%s AND reward_type=%s", (self.ev_id, prize['placement_name']))
            if c and c['c'] >= prize['max_winners']:
                return await i.followup.send("❌ This prize tier is now fully exhausted.", ephemeral=True)

            from services.verification_service import verification_service
            v_info = await verification_service.get_user_info(self.user.id)
            if v_info and verification_service.is_msl(v_info['mlbb_uid'], v_info['mlbb_server']):
                return await i.followup.send("❌ **Blocked:** MSL team members cannot receive event placements.")

            claimed = await db.fetch_one("SELECT * FROM guild_event_rewards WHERE event_id = %s AND user_id = %s AND reward_type = %s", (self.ev_id, self.user.id, prize['placement_name']))
            if claimed: return await i.followup.send("❌ User already received this tier!")

            try:
                await db.execute("INSERT INTO guild_event_rewards (event_id, user_id, reward_type, ep_awarded, diamonds_awarded) VALUES (%s, %s, %s, %s, %s)", 
                    (self.ev_id, self.user.id, prize['placement_name'], prize['ep_reward'], prize['diamond_reward']))
                from services.ep_service import ep_service
                await ep_service.process_ep_update(i.guild, self.user.id, prize['ep_reward'], bypass_verification=True, is_placement=True)
                
                msg = f"✅ Securely awarded **{self.user.mention}** with **{prize['placement_name']}** ({prize['ep_reward']} EP"
                if prize['diamond_reward'] > 0:
                    msg += f" | {prize['diamond_reward']} 💎"
                msg += ")."
                await i.followup.send(msg)
            except Exception as e:
                logger.error(f"Prize DB err: {e}")
                await i.followup.send("❌ Database error awarding prize.")

        select.callback = prize_callback
        view.add_item(select)
        await interaction.response.edit_message(content=f"Selected **{self.user.mention}**. Now choose prize:", view=view)

class PersistentEventView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        
    @discord.ui.button(label="Claim Participation", style=discord.ButtonStyle.success, custom_id="persistent_event_claim_btn", emoji="🎉")
    async def claim_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        user_id = interaction.user.id
        message_id = interaction.message.id
        
        try:
            kiosk = await db.fetch_one("SELECT event_id, ep_amount FROM guild_event_kiosks WHERE message_id = %s", (message_id,))
            if not kiosk:
                return await interaction.followup.send("❌ This event kiosk is no longer active or could not be found. It may have expired.")
                
            event_id = kiosk['event_id']
            ep_amount = kiosk['ep_amount']
            
            # Workflow Check: Restricted Kiosk
            wf = await db.fetch_one("SELECT archetype, metadata FROM event_workflows WHERE event_id = %s", (event_id,))
            if wf and wf['archetype'] == 'kiosk':
                meta = json.loads(wf['metadata']) if wf['metadata'] else {}
                if meta.get('require_registration'):
                    reg = await db.fetch_one("SELECT 1 FROM event_registration_entries WHERE event_id = %s AND user_id = %s", (event_id, user_id))
                    if not reg:
                        return await interaction.followup.send("❌ **Access Denied:** You must be registered for this event to claim participation points.", ephemeral=True)

            history = await db.fetch_one("SELECT SUM(ep_awarded) as total FROM guild_event_rewards WHERE event_id = %s AND user_id = %s", (event_id, user_id))
            if history and history['total'] and history['total'] >= ep_amount:
                return await interaction.followup.send("❌ You already received a high-tier Placement reward for this event! Participation points do not geometrically stack with placement victories.")
                 
            affected_rows = await db.execute(
                "INSERT INTO guild_event_rewards (event_id, user_id, reward_type, ep_awarded) VALUES (%s, %s, %s, %s) ON DUPLICATE KEY UPDATE event_id = VALUES(event_id)", 
                (event_id, user_id, 'participation', ep_amount)
            )
            
            if affected_rows == 0:
                return await interaction.followup.send("❌ You have already securely claimed your participation points for this event!")
            
            from services.ep_service import ep_service
            await ep_service.process_ep_update(interaction.guild, user_id, ep_amount)
            
            from services.badge_service import badge_service
            await badge_service.eval_battlefield(interaction.user)
            
            await interaction.followup.send(f"✅ Successfully claimed **{ep_amount} Participation EP**! Thank you for natively participating!")
            
        except Exception as e:
            logger.error(f"Critical error during EP claim for user {user_id}: {e}")
            await interaction.followup.send("❌ A critical database error occurred while processing your claim.")

class PersistentRegistrationView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        
    @discord.ui.button(label="Register", style=discord.ButtonStyle.primary, custom_id="persistent_event_register_btn", emoji="📋")
    async def register_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        user_id = interaction.user.id
        msg_id = interaction.message.id
        
        try:
            reg = await db.fetch_one("SELECT * FROM event_registrations WHERE announcement_msg_id = %s", (msg_id,))
            if not reg: return await interaction.followup.send("❌ Registration not found or expired.")
            if reg['status'] != 'open': return await interaction.followup.send("❌ Registration is closed.")
            
            # Block MSL
            v_info = await verification_service.get_user_info(user_id)
            if v_info and verification_service.is_msl(v_info['mlbb_uid'], v_info['mlbb_server']):
                return await interaction.followup.send("❌ **Blocked:** MSL team members cannot register for events.")
            
            # Check duplicate
            dup = await db.fetch_one("SELECT * FROM event_registration_entries WHERE event_id = %s AND user_id = %s", (reg['event_id'], user_id))
            if dup: return await interaction.followup.send("❌ You're already registered!")
            
            # Check cap (transaction safety needed in extreme concurrency, but COUNT is okay for discord events)
            if reg['max_participants']:
                count = await db.fetch_one("SELECT COUNT(*) as c FROM event_registration_entries WHERE event_id = %s", (reg['event_id'],))
                if count and count['c'] >= reg['max_participants']:
                    return await interaction.followup.send("❌ This event is fully capped! Try again later if a slot opens up.")
            
            await db.execute("INSERT IGNORE INTO event_registration_entries (event_id, user_id) VALUES (%s, %s)", (reg['event_id'], user_id))
            
            # Thread management if needed
            if reg['thread_id']:
                channel = interaction.guild.get_channel(reg['channel_id']) or await interaction.guild.fetch_channel(reg['channel_id'])
                thread = channel.get_thread(reg['thread_id'])
                if thread:
                    await thread.add_user(interaction.user)
            
            # Update Embed
            new_c = await db.fetch_one("SELECT COUNT(*) as c FROM event_registration_entries WHERE event_id = %s", (reg['event_id'],))
            cnt = new_c['c'] if new_c else 0
            embed = interaction.message.embeds[0]
            if reg['max_participants']: embed.set_footer(text=f"📋 {cnt}/{reg['max_participants']} registered")
            else: embed.set_footer(text=f"📋 {cnt} registered")
            
            try: await interaction.message.edit(embed=embed)
            except discord.HTTPException: pass # Catch rate limits if spam-clicked
            
            await interaction.followup.send("✅ You are registered! Your spot is secured.")
            
        except Exception as e:
            logger.error(f"Registration err: {e}")
            await interaction.followup.send("❌ Database error during registration.")

    @discord.ui.button(label="Unregister", style=discord.ButtonStyle.secondary, custom_id="persistent_event_unregister_btn", emoji="❌")
    async def unregister_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        user_id = interaction.user.id
        msg_id = interaction.message.id
        
        try:
            reg = await db.fetch_one("SELECT * FROM event_registrations WHERE announcement_msg_id = %s", (msg_id,))
            if not reg: return await interaction.followup.send("❌ Registration not found or expired.")
            if reg['status'] != 'open': return await interaction.followup.send("❌ Registration is closed.")
            
            deleted = await db.execute("DELETE FROM event_registration_entries WHERE event_id = %s AND user_id = %s", (reg['event_id'], user_id))
            if deleted == 0: return await interaction.followup.send("❌ You are not registered for this event.")
            
            if reg['thread_id']:
                channel = interaction.guild.get_channel(reg['channel_id']) or await interaction.guild.fetch_channel(reg['channel_id'])
                thread = channel.get_thread(reg['thread_id'])
                if thread:
                    try: await thread.remove_user(interaction.user)
                    except: pass
            
            new_c = await db.fetch_one("SELECT COUNT(*) as c FROM event_registration_entries WHERE event_id = %s", (reg['event_id'],))
            cnt = new_c['c'] if new_c else 0
            embed = interaction.message.embeds[0]
            if reg['max_participants']: embed.set_footer(text=f"📋 {cnt}/{reg['max_participants']} registered")
            else: embed.set_footer(text=f"📋 {cnt} registered")
            
            try: await interaction.message.edit(embed=embed)
            except discord.HTTPException: pass
            
            await interaction.followup.send("✅ You have been unregistered.")
            
        except Exception as e:
            logger.error(f"Unregister err: {e}")
            await interaction.followup.send("❌ Database error during unregistration.")

class EventCog(commands.Cog, name="Event"):
    """Native Discord Event Points ecosystem."""
    
    event_group = app_commands.Group(name="event", description="Manage Event Points ecosystem", default_permissions=discord.Permissions(administrator=True))
    overflow_group = app_commands.Group(name="overflow", description="Manage Overflow voice channels", parent=event_group)
    raffle_group = app_commands.Group(name="raffle", description="Create and manage event raffles", parent=event_group)
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.channel_to_events = {} 
        self.event_peaks = {}       
        self.event_to_channels = {} 
        
        # New Workflow Tracking Attributes
        self.active_workflows = {}   # event_id -> {archetype, threshold, reward, target_channel_id, metadata}
        self.user_join_times = {}    # user_id -> datetime (last join time for duration tracking)
        self.tracking_metrics = {}   # event_id -> {user_id -> value (minutes/messages)}
        
    async def cog_load(self):
        self.bot.add_view(PersistentEventView())
        self.bot.add_view(PersistentRegistrationView())

    @commands.Cog.listener()
    async def on_ready(self):
        import asyncio
        asyncio.create_task(self._initialize_peak_tracking())

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild: return
        
        # Check if this channel is linked to an active text workflow
        for eid, wf in self.active_workflows.items():
            if wf['archetype'] == 'text' and wf['target_channel_id'] == message.channel.id:
                # Basic anti-spam: ignore messages < 5 chars
                if len(message.content) < 5: return
                
                if eid not in self.tracking_metrics: self.tracking_metrics[eid] = {}
                current = self.tracking_metrics[eid].get(message.author.id, 0)
                self.tracking_metrics[eid][message.author.id] = current + 1
                
                await db.execute(
                    "INSERT INTO event_tracking_metrics (event_id, user_id, metric_value) VALUES (%s, %s, 1) ON DUPLICATE KEY UPDATE metric_value = metric_value + 1",
                    (eid, message.author.id)
                )
                break

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """Handle moderator validation for Forum/Asynchronous entries."""
        if payload.user_id == self.bot.user.id: return

        # Check if emoji is ✅
        if str(payload.emoji) != "✅": return

        guild = self.bot.get_guild(payload.guild_id)
        if not guild: return

        member = guild.get_member(payload.user_id)
        if not member or not member.guild_permissions.administrator: return

        # Check if the channel is a thread (forum post)
        channel = self.bot.get_channel(payload.channel_id)
        if not isinstance(channel, discord.Thread) or not channel.parent_id: return

        # Check if parent channel is linked to a forum workflow
        for eid, wf in self.active_workflows.items():
            if wf['archetype'] == 'forum' and wf['target_channel_id'] == channel.parent_id:
                # The user being validated is the thread owner
                target_user_id = channel.owner_id
                if not target_user_id: return

                if eid not in self.tracking_metrics: self.tracking_metrics[eid] = {}

                # If already validated, ignore
                if self.tracking_metrics[eid].get(target_user_id, 0) >= 1: return

                self.tracking_metrics[eid][target_user_id] = 1
                await db.execute(
                    "INSERT INTO event_tracking_metrics (event_id, user_id, metric_value) VALUES (%s, %s, 1) ON DUPLICATE KEY UPDATE metric_value = 1",
                    (eid, target_user_id)
                )

                try:
                    await channel.send(f"✅ **Entry Validated:** {member.mention} has approved this entry for the event.")
                except: pass
                break

    async def _initialize_peak_tracking(self):

        """Builds the ultra-optimized RAM Cache mapping channel joins to events and loads workflows."""
        
        self.channel_to_events.clear()
        self.event_peaks.clear()
        self.event_to_channels.clear()
        self.active_workflows.clear()
        
        # Load Workflow Mappings
        workflows = await db.fetch_all("SELECT * FROM event_workflows WHERE status = 'active'")
        for w in workflows:
            self.active_workflows[w['event_id']] = {
                'archetype': w['archetype'],
                'target_channel_id': w['target_channel_id'],
                'threshold': w['threshold_value'],
                'reward': w['reward_ep'],
                'metadata': json.loads(w['metadata']) if w['metadata'] else {}
            }
            if w['event_id'] not in self.tracking_metrics:
                self.tracking_metrics[w['event_id']] = {}
                # Pre-load existing metrics from DB
                metrics = await db.fetch_all("SELECT user_id, metric_value FROM event_tracking_metrics WHERE event_id = %s", (w['event_id'],))
                for m in metrics:
                    self.tracking_metrics[w['event_id']][m['user_id']] = m['metric_value']
            
        stats = await db.fetch_all("SELECT event_id, peak_concurrent FROM guild_event_stats")
        for s in stats:
            self.event_peaks[s['event_id']] = s['peak_concurrent']
            
        for guild in self.bot.guilds:
            for event in guild.scheduled_events:
                if event.status == discord.EventStatus.active:
                    channels = set()
                    if event.channel_id:
                        channels.add(event.channel_id)
                        
                    overflows = await db.fetch_all("SELECT channel_id FROM guild_event_overflows WHERE event_id = %s", (event.id,))
                    for row in overflows:
                        channels.add(row['channel_id'])
                        
                    self.event_to_channels[event.id] = channels
                    for cid in channels:
                        if cid not in self.channel_to_events:
                            self.channel_to_events[cid] = set()
                        self.channel_to_events[cid].add(event.id)
                        
                    # Duration Tracking Initialization: If event is active, record current time for anyone already in VC
                    if event.status == discord.EventStatus.active:
                        now = datetime.now()
                        for cid in channels:
                            channel = guild.get_channel(cid)
                            if channel and isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                                for m in channel.members:
                                    if not m.bot and m.id not in self.user_join_times:
                                        self.user_join_times[m.id] = now

                    # Re-calculate absolute peak physically upon boot
                    await self._evaluate_peak(event.id, guild)
                    
    async def _evaluate_peak(self, event_id: int, guild: discord.Guild):
        """Assembles total non-bot attendees globally across all mapped channels silently."""
        channels = self.event_to_channels.get(event_id, set())
        if not channels: return
        
        total_humans = 0
        for cid in channels:
            channel = guild.get_channel(cid)
            if channel and isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                humans = len([m for m in channel.members if not m.bot])
                total_humans += humans
                
        current_peak = self.event_peaks.get(event_id, 0)
        if total_humans > current_peak:
            self.event_peaks[event_id] = total_humans
            await db.execute("""
                INSERT INTO guild_event_stats (event_id, peak_concurrent) 
                VALUES (%s, %s)
                ON DUPLICATE KEY UPDATE peak_concurrent = GREATEST(peak_concurrent, %s)
            """, (event_id, total_humans, total_humans))

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot: return
        if before.channel == after.channel: return
        
        events_to_evaluate = set()
        if before.channel and before.channel.id in self.channel_to_events:
            events_to_evaluate.update(self.channel_to_events[before.channel.id])
        if after.channel and after.channel.id in self.channel_to_events:
            events_to_evaluate.update(self.channel_to_events[after.channel.id])
            
        # Peak Evaluation (Existing)
        for eid in events_to_evaluate:
            await self._evaluate_peak(eid, member.guild)

        # Duration Tracking (Workflow Overhaul)
        # 1. If leaving a tracked event VC, calculate and record duration
        if before.channel and before.channel.id in self.channel_to_events:
            join_time = self.user_join_times.pop(member.id, None)
            if join_time:
                duration_mins = int((datetime.now() - join_time).total_seconds() / 60)
                if duration_mins > 0:
                    for eid in self.channel_to_events.get(before.channel.id, set()):
                        wf = self.active_workflows.get(eid)
                        if wf and wf['archetype'] == 'audio':
                            if eid not in self.tracking_metrics: self.tracking_metrics[eid] = {}
                            current = self.tracking_metrics[eid].get(member.id, 0)
                            self.tracking_metrics[eid][member.id] = current + duration_mins
                            # Sync to DB
                            await db.execute(
                                "INSERT INTO event_tracking_metrics (event_id, user_id, metric_value) VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE metric_value = metric_value + %s",
                                (eid, member.id, duration_mins, duration_mins)
                            )
            
        # 2. If joining a tracked event VC, record the join time
        if after.channel and after.channel.id in self.channel_to_events:
            self.user_join_times[member.id] = datetime.now()
            
    @commands.Cog.listener()
    async def on_scheduled_event_update(self, before: discord.ScheduledEvent, after: discord.ScheduledEvent):
        # Refresh the RAM mapping architecture entirely if an event ends or begins globally
        if before.status != after.status:
            await self._initialize_peak_tracking()
            
            if after.status == discord.EventStatus.completed:
                # 1. Process Automated Workflow Payouts
                await self._process_workflow_payout(after)

                from services.badge_service import badge_service
                
                # Fetch participants of THIS event
                participants = await db.fetch_all("SELECT DISTINCT user_id FROM guild_event_rewards WHERE event_id = %s", (after.id,))
                participant_ids = [str(r['user_id']) for r in participants]

                if participant_ids:
                    placeholders = ','.join(['%s'] * len(participant_ids))
                    await db.execute(f"UPDATE users SET consecutive_events_attended = consecutive_events_attended + 1 WHERE user_id IN ({placeholders})", tuple(participant_ids))
                    await db.execute(f"UPDATE users SET consecutive_events_attended = 0 WHERE consecutive_events_attended > 0 AND user_id NOT IN ({placeholders})", tuple(participant_ids))
                    
                    for pid in participant_ids:
                        member = after.guild.get_member(int(pid))
                        if member: await badge_service.eval_convivialist(member)
                else:
                    await db.execute("UPDATE users SET consecutive_events_attended = 0 WHERE consecutive_events_attended > 0")
                
                # Revoke badge from users who lost their streak but still have the badge visually
                lost_badge_rows = await db.fetch_all("SELECT user_id FROM users WHERE consecutive_events_attended < 10 AND badges LIKE '%Convivialist%'")
                for r in lost_badge_rows:
                    member = after.guild.get_member(int(r['user_id']))
                    if member: await badge_service.eval_convivialist(member, force_revocation=True)
    async def _process_workflow_payout(self, event: discord.ScheduledEvent):
        """Evaluates tracking metrics and disburses rewards for completed event workflows."""
        wf = self.active_workflows.get(event.id)
        if not wf: return
        
        # Mark workflow as completed in DB first to prevent double-runs
        await db.execute("UPDATE event_workflows SET status = 'completed' WHERE event_id = %s", (event.id,))
        
        # Flush any remaining voice duration for users currently in VC
        if wf['archetype'] == 'audio':
            now = datetime.now()
            for uid, join_time in list(self.user_join_times.items()):
                # Only if they are in one of the tracked channels for THIS event
                member = event.guild.get_member(uid)
                if member and member.voice and member.voice.channel and member.voice.channel.id in self.event_to_channels.get(event.id, set()):
                    duration_mins = int((now - join_time).total_seconds() / 60)
                    if duration_mins > 0:
                        if event.id not in self.tracking_metrics: self.tracking_metrics[event.id] = {}
                        self.tracking_metrics[event.id][uid] = self.tracking_metrics[event.id].get(uid, 0) + duration_mins
                        await db.execute(
                            "INSERT INTO event_tracking_metrics (event_id, user_id, metric_value) VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE metric_value = metric_value + %s",
                            (event.id, uid, duration_mins, duration_mins)
                        )
                    self.user_join_times[uid] = now # Reset join time to 'now' for other potentially overlapping events

        # Fetch all metrics for this event
        metrics = await db.fetch_all("SELECT user_id, metric_value FROM event_tracking_metrics WHERE event_id = %s", (event.id,))
        
        eligible_users = []
        for m in metrics:
            if m['metric_value'] >= wf['threshold']:
                eligible_users.append(m['user_id'])
                
        if not eligible_users:
            logger.info(f"Workflow Payout: No users met threshold for event {event.id}")
            return
            
        from services.ep_service import ep_service
        from services.badge_service import badge_service
        
        reward_count = 0
        for uid in eligible_users:
            # Check for existing reward to prevent double-pay (e.g. if they already claimed a kiosk or placement)
            existing = await db.fetch_one("SELECT 1 FROM guild_event_rewards WHERE event_id = %s AND user_id = %s", (event.id, uid))
            if existing: continue
            
            await db.execute(
                "INSERT IGNORE INTO guild_event_rewards (event_id, user_id, reward_type, ep_awarded) VALUES (%s, %s, %s, %s)",
                (event.id, uid, f"auto_{wf['archetype']}", wf['reward'])
            )
            await ep_service.process_ep_update(event.guild, uid, wf['reward'])
            
            member = event.guild.get_member(uid)
            if member: await badge_service.eval_battlefield(member)
            reward_count += 1
            
        # Log Summary
        log_channel_id = await settings_service.get_int("event_log_channel_id")
        if log_channel_id:
            channel = event.guild.get_channel(log_channel_id)
            if channel:
                embed = discord.Embed(
                    title=f"📈 Workflow Summary: {event.name}",
                    description=f"Automated `{wf['archetype']}` payout completed.",
                    color=discord.Color.gold(),
                    timestamp=discord.utils.utcnow()
                )
                embed.add_field(name="Eligible Users", value=f"`{len(eligible_users)}`", inline=True)
                embed.add_field(name="Rewards Disbursed", value=f"`{reward_count}`", inline=True)
                embed.add_field(name="Threshold", value=f"`{wf['threshold']}`", inline=True)
                
                try: await channel.send(embed=embed)
                except: pass

    async def send_audit_log(self, interaction: discord.Interaction, title: str, description: str, color: discord.Color):
        if not interaction.guild: return
        log_channel_id = await settings_service.get_int("event_log_channel_id")
        if log_channel_id:
            channel = interaction.guild.get_channel(log_channel_id)
            if channel:
                embed = discord.Embed(title=f"🛡️ Event Audit: {title}", description=description, color=color, timestamp=discord.utils.utcnow())
                embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
                embed.set_footer(text=f"Mod ID: {interaction.user.id}")
                try: await channel.send(embed=embed)
                except discord.Forbidden: pass
        
    async def event_autocomplete(self, interaction: discord.Interaction, current: str):
        if not interaction.guild: return []
        events = interaction.guild.scheduled_events
        choices = []
        for event in events:
            if current.lower() in event.name.lower():
                choices.append(app_commands.Choice(name=event.name[:100], value=str(event.id)))
                if len(choices) >= 25: break
        return choices

    async def active_raffle_autocomplete(self, interaction: discord.Interaction, current: str):
        """Autocomplete for active raffles (status='active')."""
        rows = await db.fetch_all(
            "SELECT id, title FROM event_raffles WHERE status = 'active' ORDER BY created_at DESC LIMIT 25"
        )
        choices = []
        for r in rows:
            label = f"#{r['id']} — {r['title']}"
            if current.lower() in label.lower():
                choices.append(app_commands.Choice(name=label[:100], value=str(r['id'])))
        return choices[:25]

    async def drawn_raffle_autocomplete(self, interaction: discord.Interaction, current: str):
        """Autocomplete for drawn raffles (status='drawn')."""
        rows = await db.fetch_all(
            "SELECT id, title FROM event_raffles WHERE status = 'drawn' ORDER BY created_at DESC LIMIT 25"
        )
        choices = []
        for r in rows:
            label = f"#{r['id']} — {r['title']}"
            if current.lower() in label.lower():
                choices.append(app_commands.Choice(name=label[:100], value=str(r['id'])))
        return choices[:25]

    # --- CLI COMMANDS ---

    @event_group.command(name="setup-workflow", description="Configure automated tracking rules for a scheduled event.")
    @app_commands.autocomplete(event_id=event_autocomplete)
    @app_commands.describe(
        event_id="The scheduled Discord event to track.",
        archetype="The type of activity to track (Audio, Text, Forum, or Kiosk).",
        threshold="Goal for the reward (Minutes in VC, Message count, or Validated posts).",
        reward_ep="The amount of EP to award automatically once the threshold is met.",
        target_channel="The specific channel to monitor for activity.",
        require_registration="If True, only users who registered via /event register will earn points."
    )
    @app_commands.choices(archetype=[
        app_commands.Choice(name="Audio/Stage (Minutes in Voice)", value="audio"),
        app_commands.Choice(name="Text Activity (Message Count)", value="text"),
        app_commands.Choice(name="Forum/Asynchronous (Moderator Validated)", value="forum"),
        app_commands.Choice(name="Restricted Kiosk (Code Required)", value="kiosk")
    ])
    @app_commands.default_permissions(administrator=True)
    async def event_setup_workflow(self, interaction: discord.Interaction, event_id: str, archetype: str, threshold: int, reward_ep: int, target_channel: discord.abc.GuildChannel = None, require_registration: bool = False):
        try: ev_id = int(event_id)
        except: return await interaction.response.send_message("❌ Select an event from autocomplete.", ephemeral=True)
        
        discord_event = interaction.guild.get_scheduled_event(ev_id)
        if not discord_event: return await interaction.response.send_message("❌ Discord event not found.", ephemeral=True)
        
        # Validation
        if threshold <= 0: return await interaction.response.send_message("❌ Threshold must be greater than 0.", ephemeral=True)
        if reward_ep < 0 or reward_ep > 100000: return await interaction.response.send_message("❌ Reward EP must be between 0 and 100,000.", ephemeral=True)
        
        if archetype in ['audio', 'text', 'forum'] and not target_channel:
            return await interaction.response.send_message(f"❌ Archetype `{archetype}` requires a `target_channel`.", ephemeral=True)

        metadata = {'require_registration': require_registration}
        
        await db.execute("""
            INSERT INTO event_workflows (event_id, archetype, target_channel_id, threshold_value, reward_ep, metadata, status)
            VALUES (%s, %s, %s, %s, %s, %s, 'active')
            ON DUPLICATE KEY UPDATE archetype=VALUES(archetype), target_channel_id=VALUES(target_channel_id), 
                                    threshold_value=VALUES(threshold_value), reward_ep=VALUES(reward_ep),
                                    metadata=VALUES(metadata), status='active'
        """, (ev_id, archetype, target_channel.id if target_channel else None, threshold, reward_ep, json.dumps(metadata)))
        
        await self._initialize_peak_tracking() # Refresh RAM cache
        
        embed = discord.Embed(
            title="✅ Workflow Initialized",
            description=f"Automated tracking is now active for **{discord_event.name}**.",
            color=discord.Color.green()
        )
        embed.add_field(name="Archetype", value=archetype.capitalize(), inline=True)
        embed.add_field(name="Threshold", value=f"{threshold} (mins/msgs/posts)", inline=True)
        embed.add_field(name="Reward", value=f"{reward_ep} EP", inline=True)
        if target_channel: embed.add_field(name="Target Channel", value=target_channel.mention, inline=False)
        
        await interaction.response.send_message(embed=embed)
        await self.send_audit_log(interaction, "Workflow Setup", f"**Event:** `{discord_event.name}`\n**Type:** `{archetype}`\n**Reward:** `{reward_ep} EP`", discord.Color.green())

    @event_group.command(name="status-monitor", description="Check real-time tracking progress for an active event workflow.")
    @app_commands.autocomplete(event_id=event_autocomplete)
    @app_commands.describe(event_id="The event to monitor progress for.")
    @app_commands.default_permissions(administrator=True)
    async def event_status_monitor(self, interaction: discord.Interaction, event_id: str):
        try: ev_id = int(event_id)
        except: return await interaction.response.send_message("❌ Select an event from autocomplete.", ephemeral=True)
        
        discord_event = interaction.guild.get_scheduled_event(ev_id)
        if not discord_event: return await interaction.response.send_message("❌ Event not found.", ephemeral=True)
        
        wf = self.active_workflows.get(ev_id)
        if not wf: return await interaction.response.send_message("❌ No active workflow found for this event.", ephemeral=True)
        
        metrics = self.tracking_metrics.get(ev_id, {})
        eligible_count = sum(1 for v in metrics.values() if v >= wf['threshold'])
        
        embed = discord.Embed(
            title=f"📊 Monitor: {discord_event.name}",
            description=f"Tracking `{wf['archetype']}` metrics in real-time.",
            color=discord.Color.blue()
        )
        embed.add_field(name="Archetype", value=wf['archetype'].capitalize(), inline=True)
        embed.add_field(name="Threshold", value=f"`{wf['threshold']}`", inline=True)
        embed.add_field(name="Tracked Users", value=f"`{len(metrics)}`", inline=True)
        embed.add_field(name="Eligible So Far", value=f"✅ `{eligible_count}`", inline=True)
        
        if metrics:
            # Show top 5 participants
            sorted_m = sorted(metrics.items(), key=lambda x: x[1], reverse=True)[:5]
            lines = [f"<@{uid}>: `{val}`" for uid, val in sorted_m]
            embed.add_field(name="Top Progress", value="\n".join(lines), inline=False)
            
        await interaction.response.send_message(embed=embed)

    @event_group.command(name="setup-rewards", description="Define the exact prize pool structure before an event starts.")
    @app_commands.autocomplete(event_id=event_autocomplete)
    @app_commands.describe(event_id="The event to define prize pools for.")
    @app_commands.default_permissions(administrator=True)
    async def event_setup_rewards(self, interaction: discord.Interaction, event_id: str):
        try: event_id_int = int(event_id)
        except ValueError: return await interaction.response.send_message("❌ Select from autocomplete.", ephemeral=True)
        
        discord_event = interaction.guild.get_scheduled_event(event_id_int)
        if not discord_event: return await interaction.response.send_message("❌ Event not found.", ephemeral=True)
        
        class EventPrizeSetupModal(discord.ui.Modal, title=f"🏆 Setup: {discord_event.name[:25]}"):
            prizes = discord.ui.TextInput(
                label="Prize Structure",
                style=discord.TextStyle.long,
                placeholder="Name | EP | Diamonds | Max\n1st Place | 5000 | 500 | 1\nRunner Up | 1000 | 250 | 2",
                required=True,
                max_length=1500,
            )

            def __init__(self, ev_id: int):
                super().__init__()
                self.ev_id = ev_id

            async def on_submit(self, interaction: discord.Interaction):
                await interaction.response.defer(ephemeral=True, thinking=True)
                lines = self.prizes.value.strip().split("\n")

                pools = []
                for line in lines:
                    if not line.strip(): continue
                    parts = [p.strip() for p in line.split("|")]
                    if len(parts) not in [3, 4]:
                        return await interaction.followup.send(f"❌ Invalid format on line: `{line}`\nExpected `Name | EP | Max` or `Name | EP | Diamonds | Max`.", ephemeral=True)

                    name = parts[0]
                    try:
                        ep = int(parts[1])
                        if len(parts) == 3:
                            diamonds = 0
                            max_w = int(parts[2])
                        else:
                            diamonds = int(parts[2])
                            max_w = int(parts[3])
                    except ValueError:
                        return await interaction.followup.send(f"❌ Invalid numbers on line: `{line}`\nEP, Diamonds, and Max Winners must be valid numbers.", ephemeral=True)

                    pools.append((self.ev_id, name, ep, diamonds, max_w))

                try:
                    await db.execute("DELETE FROM event_prize_pools WHERE event_id = %s", (self.ev_id,))
                    for p in pools:
                        await db.execute(
                            "INSERT INTO event_prize_pools (event_id, placement_name, ep_reward, diamond_reward, max_winners) VALUES (%s, %s, %s, %s, %s)",
                            p
                        )
                    await interaction.followup.send(f"✅ Successfully set up **{len(pools)}** prize tiers for this event!")
                except Exception as e:
                    logger.error(f"Failed to setup prize pool: {e}")
                    await interaction.followup.send("❌ Database error while saving prize pools.", ephemeral=True)

        existing = await db.fetch_all("SELECT * FROM event_prize_pools WHERE event_id = %s", (event_id_int,))
        modal = EventPrizeSetupModal(event_id_int)
        if existing:
            prefill = "\n".join([f"{r['placement_name']} | {r['ep_reward']} | {r['diamond_reward']} | {r['max_winners']}" for r in existing])
            modal.prizes.default = prefill
        await interaction.response.send_modal(modal)

    @event_group.command(name="register", description="Deploy a Registration Embed for a Native Discord Event.")
    @app_commands.autocomplete(event_id=event_autocomplete)
    @app_commands.describe(
        event_id="The scheduled event to open registration for.",
        channel="The channel where the registration embed will be sent.",
        discohook_link="Paste the 'Share' URL from Discohook.org (JSON/Base64) to customize the embed.",
        max_participants="Optional: The maximum number of users allowed to register.",
        thread_mode="Choose if a private thread should be created for each registrant."
    )
    @app_commands.choices(thread_mode=[
        app_commands.Choice(name="None (Best for Forums)", value="none"),
        app_commands.Choice(name="Private Thread", value="private")
    ])
    @app_commands.default_permissions(administrator=True)
    async def event_register(self, interaction: discord.Interaction, event_id: str, channel: discord.TextChannel, discohook_link: str, max_participants: int = None, thread_mode: str = "none"):
        try: event_id_int = int(event_id)
        except ValueError: return await interaction.response.send_message("❌ Select from autocomplete.", ephemeral=True)
        
        if max_participants is not None and max_participants <= 0:
            return await interaction.response.send_message("❌ max_participants must be positive.", ephemeral=True)
        
        discord_event = interaction.guild.get_scheduled_event(event_id_int)
        if not discord_event: return await interaction.response.send_message("❌ Event not found natively.", ephemeral=True)
        
        pools = await db.fetch_all("SELECT * FROM event_prize_pools WHERE event_id = %s", (event_id_int,))
        if not pools:
            return await interaction.response.send_message("❌ You must setup Prize Pools via `/event setup-rewards` before deploying registration.", ephemeral=True)
            
        await interaction.response.defer(ephemeral=True)
        
        # Link logic copied from embed_cog
        try:
            parsed = urlparse(discohook_link)
            qs = parse_qs(parsed.query)
            encoded = qs.get("data", [None])[0]
            if not encoded: return await interaction.followup.send("❌ No valid data in Discohook link.")
            missing = len(encoded) % 4
            if missing: encoded += "=" * (4 - missing)
            decoded = base64.urlsafe_b64decode(encoded).decode("utf-8")
            data = json.loads(decoded)
            msg_data = data["messages"][0]["data"]
        except Exception as e:
            return await interaction.followup.send(f"❌ Failed parsing discohook: {e}")
            
        content = msg_data.get("content", None)
        embeds_data = msg_data.get("embeds", [])
        embeds = [discord.Embed.from_dict(ed) for ed in embeds_data]
        
        if embeds:
            if max_participants: embeds[0].set_footer(text=f"📋 0/{max_participants} registered")
            else: embeds[0].set_footer(text=f"📋 0 registered")
            embeds[0].color = discord.Color.brand_green()
            
        view = PersistentRegistrationView()
        
        try:
            msg = await channel.send(content=content, embeds=embeds, view=view)
        except Exception as e:
            return await interaction.followup.send(f"❌ Failed to send announcement: {e}")
            
        thread_id = None
        if thread_mode == "private" and not isinstance(channel, discord.ForumChannel):
            try:
                thread = await msg.create_thread(name=f"🔒 {discord_event.name.strip()[:90]}", auto_archive_duration=10080)
                thread_id = thread.id
            except: pass
            
        try:
            await db.execute("""
                INSERT INTO event_registrations (event_id, announcement_msg_id, channel_id, title, thread_id, max_participants, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE announcement_msg_id=VALUES(announcement_msg_id), thread_id=VALUES(thread_id)
            """, (event_id_int, msg.id, channel.id, discord_event.name, thread_id, max_participants, interaction.user.id))
            
            await interaction.followup.send(f"✅ Event Registration Deployed to {channel.mention}!")
            await self.send_audit_log(interaction, "Registration Deployed", f"**Event:** `{discord_event.name}`\n**Channel:** {channel.mention}", discord.Color.blue())
        except Exception as e:
            logger.error(f"Event deploy DB err: {e}")
            await msg.delete()
            await interaction.followup.send("❌ DB Error saving registration.")

    @event_group.command(name="kiosk", description="Spawn a Participation Button for a Native Discord Event.")
    @app_commands.autocomplete(event_id=event_autocomplete)
    @app_commands.describe(
        event_id="The event associated with this participation button.",
        ep="The amount of EP to award when the button is clicked.",
        description="The message to display in the kiosk embed."
    )
    @app_commands.default_permissions(administrator=True)
    async def event_kiosk(self, interaction: discord.Interaction, event_id: str, ep: int, description: str = "Click the button below to claim your participation points!"):
        try: event_id_int = int(event_id)
        except ValueError: return await interaction.response.send_message("❌ Select an event from autocomplete.", ephemeral=True)
        if not (1 <= ep <= 100000): return await interaction.response.send_message("❌ EP bounds violation (Must be 1 - 100,000).", ephemeral=True)
        if len(description) > 2000: return await interaction.response.send_message("❌ Description too long (Max 2000 characters).", ephemeral=True)
            
        discord_event = interaction.guild.get_scheduled_event(event_id_int)
        event_name = discord_event.name if discord_event else f"Scheduled Event: {event_id}"
        
        embed = discord.Embed(
            title=f"🎉 Event: {event_name}",
            description=f"{description}\n\n**Participation Reward:** 🏆 `{ep} EP`",
            color=discord.Color.brand_green(),
            timestamp=discord.utils.utcnow()
        )
        if discord_event and discord_event.cover_image:
            embed.set_image(url=discord_event.cover_image.url)
            
        embed.set_footer(text="Community Events System")
        view = PersistentEventView()
        
        await interaction.response.send_message(embed=embed, view=view)
        msg = await interaction.original_response()
        
        try:
            await db.execute("INSERT INTO guild_event_kiosks (message_id, event_id, ep_amount) VALUES (%s, %s, %s)", (msg.id, event_id_int, ep))
            await self.send_audit_log(interaction, "Kiosk Created", f"**Event:** `{event_name}`\n**Payload:** `{ep} EP`", discord.Color.green())
        except Exception as e:
            logger.error(f"Failed DB Kiosk: {e}")
            await msg.delete()
            await interaction.followup.send("❌ **Database Failure:** Kiosk aborted securely.", ephemeral=True)

    @event_group.command(name="cap-placement", description="Lock a strict Budget limit on an Event's Placements.")
    @app_commands.autocomplete(event_id=event_autocomplete)
    @app_commands.describe(
        event_id="The event to apply the budget cap to.",
        total_budget="The maximum total EP that can be awarded for placements in this event."
    )
    @app_commands.default_permissions(administrator=True)
    async def cap_placement(self, interaction: discord.Interaction, event_id: str, total_budget: int):
        try: event_id_int = int(event_id)
        except ValueError: return await interaction.response.send_message("❌ Select from autocomplete.", ephemeral=True)
        if total_budget < 0 or total_budget > 10000000: return await interaction.response.send_message("❌ Budget absurd (Must be 0 - 10,000,000).", ephemeral=True)
            
        await db.execute("""
            INSERT INTO guild_event_caps (event_id, total_budget, set_by) 
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE total_budget = VALUES(total_budget), set_by = VALUES(set_by)
        """, (event_id_int, total_budget, interaction.user.id))
        
        await self.send_audit_log(interaction, "Placement Budget Locked", f"**Event ID:** `{event_id}`\n**Total Budget Set:** `{total_budget} EP`", discord.Color.teal())
        await interaction.response.send_message(f"🔒 **Event Budget Locked:** Budget capped at **{total_budget} EP**.", ephemeral=True)

    @event_group.command(name="award", description="Award a predefined prize to an event winner.")
    @app_commands.autocomplete(event_id=event_autocomplete)
    @app_commands.describe(event_id="The event to award a prize for.")
    @app_commands.default_permissions(administrator=True)
    async def event_award(self, interaction: discord.Interaction, event_id: str):
        try: ev_id = int(event_id)
        except ValueError: return await interaction.response.send_message("❌ Select from autocomplete.", ephemeral=True)
        await interaction.response.send_message("Select the user to award:", view=EventAwardView(ev_id), ephemeral=True)

    @event_group.command(name="close-registration", description="Close an event, post results, and optionally payout participation.")
    @app_commands.autocomplete(event_id=event_autocomplete)
    @app_commands.describe(
        event_id="The event to close.",
        payout_participation="If True, automatically awards participation EP to all registrants."
    )
    @app_commands.default_permissions(administrator=True)
    async def event_close(self, interaction: discord.Interaction, event_id: str, payout_participation: bool = True):
        try: ev_id = int(event_id)
        except ValueError: return await interaction.response.send_message("❌ Select from autocomplete.", ephemeral=True)
        
        await interaction.response.defer(ephemeral=True)
        reg = await db.fetch_one("SELECT * FROM event_registrations WHERE event_id = %s", (ev_id,))
        if not reg: return await interaction.followup.send("❌ Registration not found.")
        if reg['status'] == 'closed': return await interaction.followup.send("❌ Already closed.")
        
        await db.execute("UPDATE event_registrations SET status = 'closed' WHERE event_id = %s", (ev_id,))
        
        # Payout Participation if requested
        if payout_participation:
            part_prize = await db.fetch_one("SELECT ep_reward, diamond_reward FROM event_prize_pools WHERE event_id=%s AND LOWER(placement_name)='participation'", (ev_id,))
            if part_prize:
                entries = await db.fetch_all("SELECT user_id FROM event_registration_entries WHERE event_id=%s", (ev_id,))
                ep = part_prize['ep_reward']
                diamonds = part_prize['diamond_reward']
                if entries:
                    batch = [(ev_id, r['user_id'], 'Participation', ep, diamonds) for r in entries]
                    try:
                        await db.executemany("INSERT INTO guild_event_rewards (event_id, user_id, reward_type, ep_awarded, diamonds_awarded) VALUES (%s, %s, %s, %s, %s) ON DUPLICATE KEY UPDATE event_id=VALUES(event_id)", batch)
                    except Exception as e:
                        logger.error(f"Batch payout fail: {e}")

        # Evolve the embed natively
        try:
            channel = interaction.guild.get_channel(reg['channel_id']) or await interaction.guild.fetch_channel(reg['channel_id'])
            if channel:
                msg = await channel.fetch_message(reg['announcement_msg_id'])
                embed = msg.embeds[0]
                view = discord.ui.View() # removes buttons

                # Fetch winners
                placements = await db.fetch_all("SELECT user_id, reward_type, ep_awarded, diamonds_awarded FROM guild_event_rewards WHERE event_id=%s AND LOWER(reward_type)!='participation' ORDER BY ep_awarded DESC, diamonds_awarded DESC", (ev_id,))

                if placements:
                    res = []
                    for i, p in enumerate(placements):
                        emoji = "🥇" if i==0 else "🥈" if i==1 else "🥉" if i==2 else "🌟"
                        reward_text = f"({p['ep_awarded']} EP"
                        if p['diamonds_awarded'] > 0:
                            reward_text += f" | {p['diamonds_awarded']} 💎"
                        reward_text += ")"
                        res.append(f"{emoji} **{p['reward_type']}** — <@{p['user_id']}> {reward_text}")
                    embed.add_field(name="🏆 Official Results", value="\n".join(res), inline=False)
                else:
                    embed.add_field(name="🏆 Official Results", value="Event Concluded natively.", inline=False)
                count = await db.fetch_one("SELECT COUNT(*) as c FROM event_registration_entries WHERE event_id = %s", (ev_id,))
                embed.set_footer(text=f"🔒 Automatically Closed · {count['c'] if count else 0} Participants")
                embed.color = discord.Color.gold()
                
                try: await msg.edit(embed=embed, view=view)
                except discord.HTTPException: pass
        except Exception as e:
            logger.error(f"Embed evolution err: {e}")
            
        await interaction.followup.send("✅ Event closed organically. Results posted to embed, and participation payouts deployed.")
    @event_group.command(name="placement", description="Award a Winner's Placement (Strict Check against Event Budgets).")
    @app_commands.autocomplete(event_id=event_autocomplete)
    @app_commands.describe(
        event_id="The event to award a placement for.",
        user="The member who earned the placement.",
        placement="The name of the placement (e.g., '1st Place', 'MVP').",
        total_ep_value="The total EP amount this placement should receive (adjusts for previous rewards).",
        diamonds="Optional: The amount of Diamonds to award for this placement."
    )
    @app_commands.default_permissions(administrator=True)
    async def event_placement(self, interaction: discord.Interaction, event_id: str, user: discord.Member, placement: str, total_ep_value: int, diamonds: int = 0):
        try: event_id_int = int(event_id)
        except ValueError: return await interaction.response.send_message("❌ Select from autocomplete.", ephemeral=True)
        if not (1 <= total_ep_value <= 100000): return await interaction.response.send_message("❌ EP Bounds Violation (Must be 1 - 100,000).", ephemeral=True)
        if not (0 <= diamonds <= 50000): return await interaction.response.send_message("❌ Diamond Bounds Violation (Must be 0 - 50,000).", ephemeral=True)
        if len(placement) > 100: return await interaction.response.send_message("❌ Placement name too long (Max 100 characters).", ephemeral=True)
            
        await interaction.response.defer() 
        
        # Prevent MSL members from winning placements
        v_info = await verification_service.get_user_info(user.id)
        if v_info and verification_service.is_msl(v_info['mlbb_uid'], v_info['mlbb_server']):
            return await interaction.followup.send(
                f"❌ **Blocked:** {user.mention} is an MSL member and cannot receive event placement rewards.",
                ephemeral=True
            )
            
        history = await db.fetch_one("SELECT SUM(ep_awarded) as total FROM guild_event_rewards WHERE event_id = %s AND user_id = %s", (event_id_int, user.id))
        already_earned = history['total'] if history and history['total'] else 0
        bonus_to_award = max(0, total_ep_value - already_earned)
        
        if bonus_to_award <= 0 and diamonds <= 0:
            return await interaction.followup.send(f"❌ User already earned `{already_earned} EP` which cleanly exceeds/secures the global `{total_ep_value} EP` value of this placement, and no diamonds were provided.", ephemeral=True)
            
        cap = await db.fetch_one("SELECT total_budget FROM guild_event_caps WHERE event_id = %s", (event_id_int,))
        if cap:
            spent = await db.fetch_one("SELECT SUM(ep_awarded) as t FROM guild_event_rewards WHERE event_id = %s AND reward_type != 'participation'", (event_id_int,))
            total_spent = spent['t'] if spent and spent['t'] else 0
            if total_spent + bonus_to_award > cap['total_budget']:
                return await interaction.followup.send(f"❌ **Budget Block:** Payout (`{bonus_to_award} EP`) completely exceeds Manager Budget (`{cap['total_budget']} EP`).", ephemeral=True)

        claimed = await db.fetch_one("SELECT * FROM guild_event_rewards WHERE event_id = %s AND user_id = %s AND reward_type = %s", (event_id_int, user.id, placement))
        if claimed: return await interaction.followup.send(f"❌ User already received explicitly `{placement}`!", ephemeral=True)

        try:
            await db.execute("INSERT INTO guild_event_rewards (event_id, user_id, reward_type, ep_awarded, diamonds_awarded) VALUES (%s, %s, %s, %s, %s)", 
                (event_id_int, user.id, placement, bonus_to_award, diamonds))
            from services.ep_service import ep_service
            if bonus_to_award > 0:
                await ep_service.process_ep_update(interaction.guild, user.id, bonus_to_award, bypass_verification=True, is_placement=True)
            
            discord_event = interaction.guild.get_scheduled_event(event_id_int)
            event_name = discord_event.name if discord_event else f"Event Profile {event_id}"
            
            award_value = f"**{total_ep_value} Total EP**"
            if diamonds > 0:
                award_value += f" and **{diamonds} 💎**"

            embed = discord.Embed(
                title="🏆 Event Winner Announced!",
                description=f"Congratulations to {user.mention} for miraculously securing **{placement}** in **{event_name}**!\n\nThey have been awarded {award_value} for their incredible victory! 🎉",
                color=discord.Color.gold(),
                timestamp=discord.utils.utcnow()
            )
            embed.set_thumbnail(url=user.display_avatar.url)
            await interaction.followup.send(content=user.mention, embed=embed)
            await self.send_audit_log(interaction, "Placement Disbursed", f"**Mod:** {interaction.user.mention}\n**Victor:** {user.mention}\n**Paid:** `{bonus_to_award} EP` | `{diamonds} 💎`", discord.Color.purple())
        except Exception as e:
            logger.error(f"Failed to award placement: {e}")
            await interaction.followup.send("❌ **Fatal DB Error**.", ephemeral=True)

    @event_group.command(name="revoke", description="Senior Admins: Erase a false payout entirely.")
    @app_commands.autocomplete(event_id=event_autocomplete)
    @app_commands.describe(
        event_id="The event to revoke rewards from.",
        user="The member whose rewards will be stripped."
    )
    @app_commands.default_permissions(administrator=True)
    async def event_revoke(self, interaction: discord.Interaction, event_id: str, user: discord.Member):
        try: event_id_int = int(event_id)
        except ValueError: return await interaction.response.send_message("❌ Select from autocomplete.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        
        history = await db.fetch_one("SELECT SUM(ep_awarded) as t FROM guild_event_rewards WHERE event_id = %s AND user_id = %s", (event_id_int, user.id))
        total_revoked = history['t'] if history and history['t'] else 0
        if total_revoked == 0: return await interaction.followup.send(f"❌ User has 0 EP traced to this event.")
            
        await db.execute("DELETE FROM guild_event_rewards WHERE event_id = %s AND user_id = %s", (event_id_int, user.id))
        from services.ep_service import ep_service
        await ep_service.process_ep_update(interaction.guild, user.id, -total_revoked, bypass_verification=True)
        
        await self.send_audit_log(interaction, "Payout UNDO", f"**Admin:** {interaction.user.mention}\n**Target:** {user.mention}\n**Erased:** `{total_revoked} EP`", discord.Color.red())
        await interaction.followup.send(f"🚨 **Revocation Complete:** Stripped `{total_revoked} EP` from {user.mention}.")

    @event_group.command(name="status", description="Generate a Live Dashboard measuring Event Health and Peak VC Trackers.")
    @app_commands.autocomplete(event_id=event_autocomplete)
    @app_commands.describe(event_id="The event to generate a status dashboard for.")
    @app_commands.default_permissions(administrator=True)
    async def event_status(self, interaction: discord.Interaction, event_id: str):
        try: event_id_int = int(event_id)
        except ValueError: return await interaction.response.send_message("❌ Invalid Event ID.", ephemeral=True)
        
        discord_event = interaction.guild.get_scheduled_event(event_id_int)
        event_name = discord_event.name if discord_event else f"Event UUID: {event_id}"
        
        cap = await db.fetch_one("SELECT total_budget FROM guild_event_caps WHERE event_id = %s", (event_id_int,))
        budget_text = f"**{cap['total_budget']} EP**" if cap else "Unlimited (No Budget Configured)"
        
        parts = await db.fetch_all("SELECT user_id FROM guild_event_rewards WHERE event_id = %s AND reward_type = 'participation'", (event_id_int,))
        places = await db.fetch_all("SELECT user_id, reward_type, ep_awarded FROM guild_event_rewards WHERE event_id = %s AND reward_type != 'participation'", (event_id_int,))
        spent_placements = sum([p['ep_awarded'] for p in places])
        
        # PEAK VC TRACKER INJECTION
        peak_data = await db.fetch_one("SELECT peak_concurrent FROM guild_event_stats WHERE event_id = %s", (event_id_int,))
        peak_members = peak_data['peak_concurrent'] if peak_data else 0
        
        embed = discord.Embed(title=f"📊 Live Security Dashboard: {event_name}", color=discord.Color.blue())
        embed.add_field(name="Peak Voice Concurrency", value=f"🎙️ **{peak_members} Verified Humans**", inline=False)
        embed.add_field(name="Total Check-Ins", value=f"{len(parts)} unique accounts", inline=True)
        embed.add_field(name="Hard EP Limit", value=budget_text, inline=True)
        embed.add_field(name="Budget Disbursed", value=f"{spent_placements} EP", inline=True)
        
        if places:
            p_lines = [f"• <@{p['user_id']}>: `{p['reward_type']}` (+{p['ep_awarded']} EP)" for p in places]
            embed.add_field(name="Verified Paid Victor Ledgers", value="\n".join(p_lines), inline=False)
        else:
            embed.add_field(name="Verified Paid Victor Ledgers", value="*No placements have been formally awarded yet.*", inline=False)
            
        await interaction.response.send_message(embed=embed)

    # --- OVERFLOW CATEGORY ---
    
    @overflow_group.command(name="add", description="Link an additional overflow Voice Channel instantly to a scheduled event.")
    @app_commands.autocomplete(event_id=event_autocomplete)
    @app_commands.describe(
        event_id="The event to link this overflow channel to.",
        channel="The voice channel to add as an overflow for the peak tracker."
    )
    @app_commands.default_permissions(administrator=True)
    async def overflow_add(self, interaction: discord.Interaction, event_id: str, channel: discord.VoiceChannel):
        try: event_id_int = int(event_id)
        except ValueError: return await interaction.response.send_message("❌ Invalid Event ID.", ephemeral=True)
        
        await db.execute("INSERT INTO guild_event_overflows (event_id, channel_id) VALUES (%s, %s) ON DUPLICATE KEY UPDATE event_id = VALUES(event_id)", (event_id_int, channel.id))
        await self._initialize_peak_tracking() # Reboot RAM Cache globally to include the newly linked overflow channel immediately!
        
        await self.send_audit_log(interaction, "VC Overflow Mapped", f"**Event ID:** `{event_id}`\n**Overflow Linked:** {channel.mention}", discord.Color.blurple())
        await interaction.response.send_message(f"🌊 **Overflow Locked:** {channel.mention} is now actively aggregated into the global Peak Voice Tracker for `{event_id}`.", ephemeral=True)
        
    @overflow_group.command(name="remove", description="Revoke an overflow Voice Channel mapping.")
    @app_commands.autocomplete(event_id=event_autocomplete)
    @app_commands.describe(
        event_id="The event to detach this overflow channel from.",
        channel="The voice channel to remove from the peak tracker."
    )
    @app_commands.default_permissions(administrator=True)
    async def overflow_remove(self, interaction: discord.Interaction, event_id: str, channel: discord.VoiceChannel):
        try: event_id_int = int(event_id)
        except ValueError: return await interaction.response.send_message("❌ Invalid Event ID.", ephemeral=True)
        
        await db.execute("DELETE FROM guild_event_overflows WHERE event_id = %s AND channel_id = %s", (event_id_int, channel.id))
        await self._initialize_peak_tracking() # Reboot RAM Cache globally
        
        await interaction.response.send_message(f"✂️ **Overflow Severed:** {channel.mention} has been cleanly detached from the Peak Voice Tracker.", ephemeral=True)

    @app_commands.command(name="event-leaderboard", description="Show the Top 10 most active Event attendees!")
    async def event_leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer()
        top_users = await db.fetch_all("SELECT user_id, event_points FROM users WHERE event_points > 0 ORDER BY event_points DESC LIMIT 10")
        if not top_users: return await interaction.followup.send("No Event Points exist natively yet!", ephemeral=True)
        
        embed = discord.Embed(title="🏆 Event Participation Leaderboard", description="The most dedicated community event attendees currently residing in the server!", color=discord.Color.gold(), timestamp=discord.utils.utcnow())
        lines = [f"**{i}.** {'🥇' if i==1 else '🥈' if i==2 else '🥉' if i==3 else '🏅'} <@{u['user_id']}> — **{u['event_points']} EP**" for i, u in enumerate(top_users, 1)]
        embed.add_field(name="Top 10 Attendees", value="\n".join(lines), inline=False)
        
        # Calculate Invoker's Server-Wide Rank
        user_data = await db.fetch_one("SELECT event_points FROM users WHERE user_id = %s", (interaction.user.id,))
        ep = user_data['event_points'] if user_data else 0
        if ep > 0:
            rank_data = await db.fetch_one("SELECT COUNT(*) as pos FROM users WHERE event_points > %s", (ep,))
            rank = (rank_data['pos'] + 1) if rank_data else 1
            embed.set_footer(text=f"Your Server-Wide Event Rank: #{rank} | {ep} EP", icon_url=interaction.user.display_avatar.url)
        else:
            embed.set_footer(text="Your Server-Wide Event Rank: Unranked | 0 EP", icon_url=interaction.user.display_avatar.url)
            
        await interaction.followup.send(embed=embed)

    # ─────────────────────────────────────────────────────────────────────
    # Raffle Subgroup
    # ─────────────────────────────────────────────────────────────────────

    @raffle_group.command(name="create", description="Create and deploy a new event raffle")
    @require_admin_auth()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        title="Name of the raffle",
        prize="What the winner(s) receive",
        channel="Channel to post the raffle in",
        winners="Number of winners (default: 1)",
        duration_minutes="Auto-draw after N minutes (leave empty for manual draw)",
        end_time_utc8="Specific UTC+8 Date & Time (YYYY-MM-DD HH:MM) (Overrides duration)",
        hosted_by="Community member hosting this raffle (optional)"
    )
    async def raffle_create(
        self, interaction: discord.Interaction,
        title: str, prize: str, channel: discord.TextChannel,
        winners: int = 1,
        duration_minutes: int = None,
        end_time_utc8: str = None,
        hosted_by: discord.Member = None
    ):
        """Step 1: Collect core params, then offer to add multiline requirements."""
        # Stash raffle config for the follow-up view/modal
        raffle_config = {
            'title': title,
            'prize': prize,
            'channel_id': channel.id,
            'winners': winners,
            'duration_minutes': duration_minutes,
            'end_time_utc8': end_time_utc8,
            'hosted_by_id': hosted_by.id if hosted_by else None,
            'creator_id': interaction.user.id,
        }

        view = RaffleDeployView(raffle_config, self.bot)
        await interaction.response.send_message(
            f"🎟️ **Raffle: {title}**\n\n"
            f"Would you like to add requirements/mechanics (with multiline support)?\n\n"
            f"• **Add Requirements** — opens a text box where you can type instructions with line breaks\n"
            f"• **Skip — Deploy Now** — deploys the raffle immediately without requirements",
            view=view,
            ephemeral=True
        )

    @raffle_group.command(name="draw", description="Manually draw winners for a raffle")
    @require_admin_auth()
    @app_commands.default_permissions(administrator=True)
    @app_commands.autocomplete(raffle_id=active_raffle_autocomplete)
    @app_commands.describe(raffle_id="The active raffle to draw")
    async def raffle_draw(self, interaction: discord.Interaction, raffle_id: str):
        await interaction.response.defer(ephemeral=True)
        try: raffle_id_int = int(raffle_id)
        except ValueError: return await interaction.followup.send("❌ Please select a raffle from the autocomplete list.", ephemeral=True)

        raffle = await db.fetch_one(
            "SELECT * FROM event_raffles WHERE id = %s AND status = 'active'", (raffle_id_int,)
        )
        if not raffle:
            return await interaction.followup.send(
                f"❌ Raffle not found or already drawn.", ephemeral=True
            )

        raffle_cog = self.bot.get_cog("Event Raffle")
        if not raffle_cog:
            return await interaction.followup.send("❌ Raffle engine not loaded.", ephemeral=True)

        await raffle_cog.execute_draw(raffle, interaction.guild)
        await interaction.followup.send(
            f"✅ Raffle **{raffle['title']}** drawn! Check the channel for results.",
            ephemeral=True
        )

    @raffle_group.command(name="reroll", description="Reroll an ended raffle. Disqualify someone or reroll all.")
    @require_admin_auth()
    @app_commands.default_permissions(administrator=True)
    @app_commands.autocomplete(raffle_id=drawn_raffle_autocomplete)
    @app_commands.describe(
        raffle_id="The drawn raffle to reroll",
        reason="Required reason for disqualifying current winners / authorizing a reroll.",
        disqualified_winner="Optional: A specific winner to disqualify and replace. Leave blank for full reroll."
    )
    async def raffle_reroll(self, interaction: discord.Interaction, raffle_id: str, reason: str, disqualified_winner: discord.Member = None):
        await interaction.response.defer(ephemeral=True)
        try: raffle_id_int = int(raffle_id)
        except ValueError: return await interaction.followup.send("❌ Please select a raffle from the autocomplete list.", ephemeral=True)

        raffle = await db.fetch_one(
            "SELECT * FROM event_raffles WHERE id = %s", (raffle_id_int,)
        )
        if not raffle:
            return await interaction.followup.send(f"❌ Raffle not found.", ephemeral=True)
            
        if raffle['status'] != 'drawn':
            return await interaction.followup.send(f"❌ Raffle has not been drawn yet. Its status is '{raffle['status']}'.", ephemeral=True)

        raffle_cog = self.bot.get_cog("Event Raffle")
        if not raffle_cog:
            return await interaction.followup.send("❌ Raffle engine not loaded.", ephemeral=True)

        await raffle_cog.execute_reroll(raffle, disqualified_winner, reason, interaction.guild, interaction)

    @raffle_group.command(name="cancel", description="Cancel an active raffle")
    @require_admin_auth()
    @app_commands.default_permissions(administrator=True)
    @app_commands.autocomplete(raffle_id=active_raffle_autocomplete)
    @app_commands.describe(raffle_id="The active raffle to cancel")
    async def raffle_cancel(self, interaction: discord.Interaction, raffle_id: str):
        await interaction.response.defer(ephemeral=True)
        try: raffle_id_int = int(raffle_id)
        except ValueError: return await interaction.followup.send("❌ Please select a raffle from the autocomplete list.", ephemeral=True)

        raffle = await db.fetch_one(
            "SELECT * FROM event_raffles WHERE id = %s AND status = 'active'", (raffle_id_int,)
        )
        if not raffle:
            return await interaction.followup.send(
                f"❌ Raffle not found or not active.", ephemeral=True
            )

        await db.execute("UPDATE event_raffles SET status = 'cancelled' WHERE id = %s", (raffle_id_int,))

        # Evaluate giveaway milestones for the host since their count decreased
        from services.giveaway_milestone_service import giveaway_milestone_service
        effective_host_id = raffle.get('hosted_by') or raffle['host_id']
        try:
            await giveaway_milestone_service.evaluate_milestones(interaction.guild, effective_host_id)
        except Exception as e:
            logger.warning(f"Milestone eval on cancel failed: {e}")

        try:
            channel = interaction.guild.get_channel(raffle['channel_id'])
            if channel:
                msg = await channel.fetch_message(raffle['message_id'])
                embed = msg.embeds[0]
                embed.color = 0x95A5A6
                embed.title = f"~~{embed.title}~~ — Cancelled"
                embed.set_footer(text=f"Cancelled by {interaction.user.display_name}")
                await msg.edit(embed=embed, view=None)
        except Exception as e:
            logger.warning(f"Could not update cancelled raffle embed: {e}")

        await interaction.followup.send(f"✅ Raffle **{raffle['title']}** cancelled.", ephemeral=True)

    @raffle_group.command(name="sync_legacy", description="Retroactively locate and cache message IDs for previously drawn raffles.")
    @require_admin_auth()
    @app_commands.default_permissions(administrator=True)
    async def raffle_sync_legacy(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        # Fetch raffles where status='drawn' and either tracking ID is missing
        raffles = await db.fetch_all("SELECT * FROM event_raffles WHERE status = 'drawn' AND (winners_thread_id IS NULL OR announcement_msg_id IS NULL)")
        if not raffles:
            return await interaction.followup.send("✅ All drawn raffles are already fully synchronized!")
            
        synced = 0
        for raffle in raffles:
            channel = interaction.guild.get_channel(raffle['channel_id'])
            if not channel:
                continue
                
            announcement_id = raffle.get('announcement_msg_id')
            thread_id = raffle.get('winners_thread_id')
            welcome_id = raffle.get('welcome_msg_id')
            
            # Find Thread
            if not thread_id:
                target_name = f"🏆 Winners: {raffle['title'][:80]}"
                for t in channel.threads:
                    if t.name == target_name:
                        thread_id = t.id
                        break
                if not thread_id:
                    async for t in channel.archived_threads(limit=100):
                        if t.name == target_name:
                            thread_id = t.id
                            break
                            
            if thread_id and not welcome_id:
                t_obj = interaction.guild.get_thread(thread_id)
                if t_obj:
                    async for msg in t_obj.history(limit=5, oldest_first=True):
                        if msg.author.id == self.bot.user.id and "Congratulations!" in msg.content:
                            welcome_id = msg.id
                            break
                            
            if not announcement_id:
                # Search channel near message_id
                base_msg_id = raffle.get('message_id')
                if base_msg_id:
                    try:
                        base_msg = await channel.fetch_message(base_msg_id)
                        async for msg in channel.history(after=base_msg, limit=20):
                            if msg.author.id == self.bot.user.id and "raffle results!" in msg.content:
                                announcement_id = msg.id
                                break
                    except Exception:
                        pass
                        
            await db.execute(
                "UPDATE event_raffles SET winners_thread_id = %s, announcement_msg_id = %s, welcome_msg_id = %s WHERE id = %s",
                (thread_id, announcement_id, welcome_id, raffle['id'])
            )
            synced += 1
            
        await interaction.followup.send(f"✅ Successfully retro-synchronized {synced} legacy raffle(s)!")

    @raffle_group.command(name="backfill_milestones", description="Retroactively assign giveaway milestone roles to all qualifying hosts.")
    @require_admin_auth()
    @app_commands.default_permissions(administrator=True)
    async def raffle_backfill_milestones(self, interaction: discord.Interaction):
        await interaction.response.send_message("⏳ Scanning historical raffles and assigning milestone roles... This may take a moment due to rate limits.", ephemeral=True)
        
        from services.giveaway_milestone_service import giveaway_milestone_service
        stats = await giveaway_milestone_service.backfill_all(interaction.guild)
        
        embed = discord.Embed(
            title="✅ Milestone Backfill Complete",
            description=(
                f"**Users Updated:** {stats['updated']}\n"
                f"**Users Skipped:** {stats['skipped']} (not found in server)\n"
                f"**Errors:** {stats['errors']}"
            ),
            color=discord.Color.green()
        )
        await interaction.edit_original_response(content=None, embed=embed)

    @raffle_group.command(name="force_sync", description="Surgically overwrite the text of legacy announcement messages to match current DB winners.")
    @require_admin_auth()
    @app_commands.default_permissions(administrator=True)
    @app_commands.autocomplete(raffle_id=drawn_raffle_autocomplete)
    @app_commands.describe(raffle_id="The drawn raffle whose messages are out of date")
    async def raffle_force_sync(self, interaction: discord.Interaction, raffle_id: str):
        await interaction.response.defer(ephemeral=True)
        try: raffle_id_int = int(raffle_id)
        except ValueError: return await interaction.followup.send("❌ Please select a raffle from the autocomplete list.", ephemeral=True)
        raffle = await db.fetch_one("SELECT * FROM event_raffles WHERE id = %s", (raffle_id_int,))
        if not raffle:
            return await interaction.followup.send(f"❌ Raffle not found.", ephemeral=True)
            
        current_winners_str = raffle.get('winners', "")
        if not current_winners_str:
            return await interaction.followup.send("❌ No winners securely attached to Database for this raffle.", ephemeral=True)
            
        current_winners = [int(w) for w in current_winners_str.split(",")]
        winner_mentions = " ".join(f"<@{w}>" for w in current_winners)
        
        channel = interaction.guild.get_channel(raffle['channel_id'])
        if not channel:
            return await interaction.followup.send("❌ Channel no longer exists.", ephemeral=True)
            
        # Compile Ledger text if any historic rerolls exist
        import json
        history_str = raffle.get('reroll_history')
        ledger_text = ""
        if history_str:
            try:
                history = json.loads(history_str)
                ledger_lines = []
                for item in history:
                    if item.get('type') == 'targeted':
                        ledger_lines.append(f"- <@{item['new_id']}> replaced **{item['replaced_name']}** *(Reason: {item['reason']})*")
                    else:
                        ledger_lines.append(f"- **Full Reroll Executed** *(Reason: {item['reason']})*")
                if ledger_lines:
                    ledger_text = "\n\n---\n**Rerolls History:**\n" + "\n".join(ledger_lines)
            except Exception:
                pass

        # Ensure we have the IDs (Radar Sync if currently missing)
        announcement_id = raffle.get('announcement_msg_id')
        thread_id = raffle.get('winners_thread_id')
        welcome_id = raffle.get('welcome_msg_id')

        if not thread_id:
            target_name = f"🏆 Winners: {raffle['title'][:80]}"
            for t in channel.threads:
                if t.name == target_name: thread_id = t.id; break
            if not thread_id:
                async for t in channel.archived_threads(limit=100):
                    if t.name == target_name: thread_id = t.id; break

        if thread_id and not welcome_id:
            t_obj = interaction.guild.get_thread(thread_id)
            if t_obj:
                async for msg in t_obj.history(limit=5, oldest_first=True):
                    if msg.author.id == self.bot.user.id and "Congratulations!" in msg.content:
                        welcome_id = msg.id; break

        if not announcement_id:
            base_msg_id = raffle.get('message_id')
            if base_msg_id:
                try:
                    base_msg = await channel.fetch_message(base_msg_id)
                    async for msg in channel.history(after=base_msg, limit=20):
                        if msg.author.id == self.bot.user.id and "raffle results!" in msg.content:
                            announcement_id = msg.id; break
                except Exception:
                    pass

        # Update DB if IDs were discovered
        await db.execute(
            "UPDATE event_raffles SET winners_thread_id = %s, announcement_msg_id = %s, welcome_msg_id = %s WHERE id = %s",
            (thread_id, announcement_id, welcome_id, raffle['id'])
        )
        
        # --- Overwrite Data Phase ---
        actions_taken = []
        
        # 1. Embed "🎉 Winners"
        if raffle.get('message_id'):
            try:
                msg = await channel.fetch_message(raffle['message_id'])
                embed = msg.embeds[0]
                for i, field in enumerate(embed.fields):
                    if getattr(field, 'name', None) == "🎉 Winners":
                        embed.set_field_at(i, name="🎉 Winners", value=winner_mentions, inline=False)
                await msg.edit(embed=embed)
                actions_taken.append("Embed Field")
            except Exception:
                pass
                
        # 2. Public Announcement Text
        if announcement_id:
            try:
                announcement_msg = await channel.fetch_message(announcement_id)
                await announcement_msg.edit(content=
                    f"🎉 **{raffle['title']}** raffle results!\n\n"
                    f"🏆 Congratulations to: {winner_mentions}\n\n"
                    f"*You've been added to a private thread below for prize coordination.*{ledger_text}"
                )
                actions_taken.append("Public Announcement")
            except Exception:
                pass
                
        # 3. Lounge Welcome Code
        if thread_id and welcome_id:
            try:
                t_obj = interaction.guild.get_thread(thread_id)
                if t_obj:
                    welcome_msg = await t_obj.fetch_message(welcome_id)
                    host_id = raffle['hosted_by'] or raffle['host_id']
                    host_mention = f"<@{host_id}>"
                    await welcome_msg.edit(content=
                        f"🎉 **Congratulations!** You've won the **{raffle['title']}** raffle!\n\n"
                        f"**Prize:** {raffle['prize']}\n"
                        f"**Host:** {host_mention}\n\n"
                        f"Please coordinate with the host here for prize delivery.\n\n"
                        f"Winners: {winner_mentions}{ledger_text}"
                    )
                    actions_taken.append("Lounge Welcome")
            except Exception:
                pass
                
        summary = ", ".join(actions_taken) if actions_taken else "No messages found to gracefully edit."
        await interaction.followup.send(f"✅ Force Sync fully executed! Payloads overridden: {summary}", ephemeral=True)

    @raffle_group.command(name="export_winners", description="Export a drawn raffle's winners as a CSV file.")
    @require_admin_auth()
    @app_commands.default_permissions(administrator=True)
    @app_commands.autocomplete(raffle_id=drawn_raffle_autocomplete)
    @app_commands.describe(raffle_id="The drawn raffle to export")
    async def raffle_export_winners(self, interaction: discord.Interaction, raffle_id: str):
        await interaction.response.defer(ephemeral=True)
        try: raffle_id_int = int(raffle_id)
        except ValueError: return await interaction.followup.send("❌ Please select a raffle from the autocomplete list.", ephemeral=True)

        raffle = await db.fetch_one("SELECT * FROM event_raffles WHERE id = %s", (raffle_id_int,))
        if not raffle:
            return await interaction.followup.send("❌ Raffle not found.", ephemeral=True)
        if not raffle.get('winners'):
            return await interaction.followup.send("❌ This raffle has no winners recorded.", ephemeral=True)

        winner_ids = [int(w) for w in raffle['winners'].split(",")]

        # Fetch verification data for all winners in one query
        placeholders = ",".join(["%s"] * len(winner_ids))
        verified_rows = await db.fetch_all(
            f"SELECT user_id, full_name, mlbb_uid, mlbb_server FROM verified_users WHERE user_id IN ({placeholders})",
            tuple(winner_ids)
        )
        verified_map = {r['user_id']: r for r in verified_rows}

        unverified_ids = [wid for wid in winner_ids if wid not in verified_map]

        # Build the CSV date from ends_at or now
        draw_date = raffle['ends_at'] or datetime.now(timezone.utc)
        date_str = draw_date.strftime("%Y/%m/%d")
        activity = raffle['title']

        output = io.StringIO()
        output.write('\ufeff')  # UTF-8 BOM for Excel compatibility
        writer = csv.writer(output)
        writer.writerow(["Full Name", "UID", "Server", "Amount", "Remarks"])

        # Verified winners first
        verified_winner_ids = [wid for wid in winner_ids if wid in verified_map]
        for wid in verified_winner_ids:
            v = verified_map[wid]
            writer.writerow([
                v['full_name'],
                v['mlbb_uid'],
                v['mlbb_server'],
                "",  # Amount — blank for manual input
                f"MSL Network Discord - {activity} - ({date_str})"
            ])
        
        # Unverified winners at the bottom
        for wid in unverified_ids:
            user_obj = interaction.guild.get_member(wid)
            display = user_obj.display_name if user_obj else f"User {wid}"
            writer.writerow([
                f"UNVERIFIED — {display}",
                "N/A",
                "N/A",
                "",
                f"MSL Network Discord - {activity} - ({date_str})"
            ])

        output.seek(0)
        filename = f"raffle_winners_{raffle_id_int}_{date_str.replace('/', '-')}.csv"
        file = discord.File(fp=io.BytesIO(output.getvalue().encode('utf-8-sig')), filename=filename)
        
        response_msg = f"✅ Exported **{len(winner_ids)}** winner(s) from **{raffle['title']}**."
        if unverified_ids:
            pings = " ".join([f"<@{uid}>" for uid in unverified_ids])
            response_msg += (
                f"\n\n⚠️ **{len(unverified_ids)} unverified winner(s)** are tagged as `UNVERIFIED` at the bottom of the CSV.\n\n"
                f"**Copy/Paste this to tag them:**\n"
                f"```\nPlease verify to claim your raffle rewards: {pings}\n```"
            )
        await interaction.followup.send(response_msg, file=file, ephemeral=True)

    @event_group.command(name="export_winners", description="Export an event's placement winners as a CSV file.")
    @require_admin_auth()
    @app_commands.default_permissions(administrator=True)
    @app_commands.autocomplete(event_id=event_autocomplete)
    @app_commands.describe(event_id="The Discord event to export placement winners from")
    async def event_export_winners(self, interaction: discord.Interaction, event_id: str):
        await interaction.response.defer(ephemeral=True)
        try: event_id_int = int(event_id)
        except ValueError: return await interaction.followup.send("❌ Please select an event from the autocomplete list.", ephemeral=True)

        # Fetch event name
        discord_event = interaction.guild.get_scheduled_event(event_id_int)
        event_name = discord_event.name if discord_event else f"Event {event_id_int}"
        draw_date = discord_event.end_time if discord_event and discord_event.end_time else datetime.now(timezone.utc)
        date_str = draw_date.strftime("%Y/%m/%d")

        rewards = await db.fetch_all(
            "SELECT user_id, reward_type, diamonds_awarded FROM guild_event_rewards WHERE event_id = %s ORDER BY awarded_at",
            (event_id_int,)
        )
        if not rewards:
            return await interaction.followup.send("❌ No placement winners found for this event.", ephemeral=True)

        winner_ids = list({r['user_id'] for r in rewards})
        placeholders = ",".join(["%s"] * len(winner_ids))
        verified_rows = await db.fetch_all(
            f"SELECT user_id, full_name, mlbb_uid, mlbb_server FROM verified_users WHERE user_id IN ({placeholders})",
            tuple(winner_ids)
        )
        verified_map = {r['user_id']: r for r in verified_rows}

        unverified_ids = [wid for wid in winner_ids if wid not in verified_map]

        output = io.StringIO()
        output.write('\ufeff')
        writer = csv.writer(output)
        writer.writerow(["Full Name", "UID", "Server", "Amount", "Remarks"])

        # Verified reward entries first
        for row in rewards:
            if row['user_id'] in verified_map:
                v = verified_map[row['user_id']]
                placement = row['reward_type']
                writer.writerow([
                    v['full_name'],
                    v['mlbb_uid'],
                    v['mlbb_server'],
                    row['diamonds_awarded'] if row['diamonds_awarded'] > 0 else "",
                    f"MSL Network Discord - {event_name} - {placement} - ({date_str})"
                ])
        
        # Unverified reward entries at the bottom
        for row in rewards:
            if row['user_id'] not in verified_map:
                user_obj = interaction.guild.get_member(row['user_id'])
                display = user_obj.display_name if user_obj else f"User {row['user_id']}"
                placement = row['reward_type']
                writer.writerow([
                    f"UNVERIFIED — {display}",
                    "N/A",
                    "N/A",
                    row['diamonds_awarded'] if row['diamonds_awarded'] > 0 else "",
                    f"MSL Network Discord - {event_name} - {placement} - ({date_str})"
                ])

        output.seek(0)
        filename = f"event_winners_{event_id_int}_{date_str.replace('/', '-')}.csv"
        file = discord.File(fp=io.BytesIO(output.getvalue().encode('utf-8-sig')), filename=filename)
        
        response_msg = f"✅ Exported **{len(rewards)}** placement row(s) from **{event_name}**."
        if unverified_ids:
            pings = " ".join([f"<@{uid}>" for uid in unverified_ids])
            response_msg += (
                f"\n\n⚠️ **{len(unverified_ids)} unverified winner(s)** are tagged as `UNVERIFIED` at the bottom of the CSV.\n\n"
                f"**Copy/Paste this to tag them:**\n"
                f"```\nPlease verify to claim your event rewards: {pings}\n```"
            )
        await interaction.followup.send(response_msg, file=file, ephemeral=True)

    @raffle_group.command(name="set_timer", description="Set or update the end time on an existing active raffle.")
    @require_admin_auth()
    @app_commands.default_permissions(administrator=True)
    @app_commands.autocomplete(raffle_id=active_raffle_autocomplete)
    @app_commands.describe(
        raffle_id="The active raffle to add a timer to",
        duration_or_time="Countdown (1d, 6h) OR exact UTC+8 time (YYYY-MM-DD HH:MM)"
    )
    async def raffle_set_timer(self, interaction: discord.Interaction, raffle_id: str, duration_or_time: str):
        await interaction.response.defer(ephemeral=True)
        try: raffle_id_int = int(raffle_id)
        except ValueError: return await interaction.followup.send("❌ Please select a raffle from the autocomplete list.", ephemeral=True)

        raffle = await db.fetch_one("SELECT * FROM event_raffles WHERE id = %s AND status = 'active'", (raffle_id_int,))
        if not raffle:
            return await interaction.followup.send("❌ Active raffle not found.", ephemeral=True)

        # Parse duration string: supports days (d), hours (h), minutes (m) or a specific datetime
        import re
        total_seconds = 0
        ends_at = None

        try:
            dt = datetime.strptime(duration_or_time, "%Y-%m-%d %H:%M")
            ends_at = dt - timedelta(hours=8)
            if ends_at <= datetime.utcnow():
                return await interaction.followup.send("❌ The provided time must be in the future.", ephemeral=True)
        except ValueError:
            pattern = re.findall(r'(\d+)([dhm])', duration_or_time.lower())
            if not pattern:
                return await interaction.followup.send(
                    "❌ Invalid format. Use `YYYY-MM-DD HH:MM` for exact UTC+8 time, or `1d`, `6h`, `30m` for countdown.",
                    ephemeral=True
                )
            for value, unit in pattern:
                v = int(value)
                if unit == 'd': total_seconds += v * 86400
                elif unit == 'h': total_seconds += v * 3600
                elif unit == 'm': total_seconds += v * 60

            if total_seconds < 60:
                return await interaction.followup.send("❌ Minimum duration is 1 minute.", ephemeral=True)
            if total_seconds > 86400 * 30:
                return await interaction.followup.send("❌ Maximum duration is 30 days.", ephemeral=True)

            ends_at = datetime.utcnow() + timedelta(seconds=total_seconds)

        ends_at_naive = ends_at.replace(tzinfo=None)  # Store as UTC naive for MySQL

        await db.execute(
            "UPDATE event_raffles SET ends_at = %s WHERE id = %s",
            (ends_at_naive, raffle_id_int)
        )

        # Update embed to display the countdown
        channel = interaction.guild.get_channel(raffle['channel_id'])
        if channel and raffle.get('message_id'):
            try:
                msg = await channel.fetch_message(raffle['message_id'])
                if msg.embeds:
                    embed = msg.embeds[0]
                    # Update or add the ends_at field
                    ends_dt_discord = discord.utils.format_dt(ends_at, style='R')
                    ends_dt_full = discord.utils.format_dt(ends_at, style='F')
                    field_updated = False
                    for i, field in enumerate(embed.fields):
                        if getattr(field, 'name', None) in ('⏰ Ends', '⏰ Auto-Draws'):
                            embed.set_field_at(i, name="⏰ Ends", value=f"{ends_dt_full} ({ends_dt_discord})", inline=False)
                            field_updated = True
                            break
                    if not field_updated:
                        embed.add_field(name="⏰ Ends", value=f"{ends_dt_full} ({ends_dt_discord})", inline=False)
                    await msg.edit(embed=embed)
            except Exception as e:
                logger.warning(f"Could not update raffle embed with timer: {e}")

        ends_dt_discord = discord.utils.format_dt(ends_at, style='R')
        await interaction.followup.send(
            f"✅ Timer set! Raffle **{raffle['title']}** will auto-draw {ends_dt_discord}.",
            ephemeral=True
        )

    @app_commands.command(name="raffles", description="Show all active raffles")
    async def raffle_list(self, interaction: discord.Interaction):
        raffles = await db.fetch_all(
            "SELECT id, title, prize, winner_count, ends_at, created_at "
            "FROM event_raffles WHERE status = 'active' ORDER BY created_at DESC"
        )

        if not raffles:
            return await interaction.response.send_message(
                "📭 No active raffles right now.", ephemeral=True
            )

        embed = discord.Embed(title="🎟️ Active Raffles", color=0xFFD700)

        for r in raffles[:10]:
            count = await db.fetch_one(
                "SELECT COUNT(*) as total FROM event_raffle_entries WHERE raffle_id = %s",
                (r['id'],)
            )
            participants = count['total'] if count else 0
            ends = discord.utils.format_dt(r['ends_at'], style="R") if r['ends_at'] else "Manual"
            embed.add_field(
                name=f"#{r['id']} — {r['title']}",
                value=f"🎁 {r['prize']}\n👥 {participants} joined • 🏆 {r['winner_count']} winner(s) • ⏰ {ends}",
                inline=False
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)


# ─── RAFFLE MODAL & DEPLOY HELPERS ──────────────────────────────────────

class RaffleRequirementsModal(discord.ui.Modal, title="📋 Raffle Requirements"):
    """Multiline text input for raffle mechanics/requirements."""

    requirements = discord.ui.TextInput(
        label="Requirements / Mechanics",
        style=discord.TextStyle.long,
        placeholder="E.g.\n1. Like this post\n2. Share to your story\n3. Tag 2 friends",
        required=True,
        max_length=1500,
    )

    def __init__(self, raffle_config: dict, bot):
        super().__init__()
        self.raffle_config = raffle_config
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        self.raffle_config['requirements'] = self.requirements.value
        await deploy_raffle(interaction, self.raffle_config, self.bot)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        logger.error(f"RaffleRequirementsModal error: {error}")
        try:
            await interaction.followup.send("❌ Failed to process requirements.", ephemeral=True)
        except Exception:
            pass


class RaffleDeployView(discord.ui.View):
    """Ephemeral view: 'Add Requirements' or 'Skip — Deploy Now'."""

    def __init__(self, raffle_config: dict, bot):
        super().__init__(timeout=120)
        self.raffle_config = raffle_config
        self.bot = bot

    async def on_timeout(self):
        """Disable buttons after timeout."""
        for item in self.children:
            item.disabled = True

    @discord.ui.button(label="Add Requirements", style=discord.ButtonStyle.primary, emoji="📋")
    async def add_requirements(self, interaction: discord.Interaction, button: discord.ui.Button):
        # We cannot disable buttons here because we must respond with the modal.
        modal = RaffleRequirementsModal(self.raffle_config, self.bot)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Skip — Deploy Now", style=discord.ButtonStyle.secondary, emoji="🚀")
    async def skip_deploy(self, interaction: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        # Edit the ephemeral message to disable buttons (this also acknowledges the interaction)
        await interaction.response.edit_message(view=self)
        self.raffle_config['requirements'] = None
        await deploy_raffle(interaction, self.raffle_config, self.bot)



async def deploy_raffle(interaction: discord.Interaction, config: dict, bot):
    """
    Core raffle deployment logic.
    Builds the embed, sends it with persistent buttons, creates proof thread,
    pings the Giveaway Notification role, and stores everything in the DB.
    """
    from cogs.tracker.event_raffle_cog import PersistentRaffleView

    guild = interaction.guild
    channel = guild.get_channel(config['channel_id'])
    if not channel:
        return await interaction.followup.send("❌ Channel not found.", ephemeral=True)

    title = config['title']
    prize = config['prize']
    winners = config['winners']
    duration_minutes = config['duration_minutes']
    end_time_utc8 = config.get('end_time_utc8')
    hosted_by_id = config['hosted_by_id']
    creator_id = config['creator_id']
    requirements = config.get('requirements')

    ends_at = None
    if end_time_utc8:
        try:
            dt = datetime.strptime(end_time_utc8, "%Y-%m-%d %H:%M")
            ends_at = dt - timedelta(hours=8)
            if ends_at <= datetime.utcnow():
                return await interaction.followup.send("❌ The provided time must be in the future.", ephemeral=True)
        except ValueError:
            return await interaction.followup.send("❌ Invalid date format. Please use YYYY-MM-DD HH:MM (e.g., 2026-04-10 15:30).", ephemeral=True)
    elif duration_minutes and duration_minutes > 0:
        ends_at = datetime.utcnow() + timedelta(minutes=duration_minutes)

    # Build embed
    host_display = f"<@{hosted_by_id}>" if hosted_by_id else f"<@{creator_id}>"
    embed = discord.Embed(
        title=f"🎟️ {title}",
        description=f"**Prize:** {prize}",
        color=0xFFD700,
        timestamp=datetime.utcnow()
    )
    embed.add_field(name="Hosted By", value=host_display, inline=True)
    embed.add_field(name="Winners", value=f"**{winners}**", inline=True)
    embed.add_field(name="Participants", value="**0**", inline=True)

    if requirements:
        # Preserve multiline: format each line as a blockquote line
        formatted_lines = "\n".join(f"> {line}" for line in requirements.split("\n"))
        embed.add_field(
            name="📋 Requirements",
            value=f"{formatted_lines}\n\n*You'll be added to a proof thread upon joining.*",
            inline=False
        )

    if ends_at:
        embed.add_field(
            name="⏰ Ends",
            value=discord.utils.format_dt(ends_at, style="R"),
            inline=False
        )
    else:
        embed.add_field(name="⏰ Draw", value="Manual — an admin will draw when ready", inline=False)

    embed.set_footer(text="Click 🎟️ Join Raffle to enter!")

    # Resolve Giveaway Notification role for the ping
    ping_content = None
    giveaway_role = discord.utils.get(guild.roles, name="Giveaway Notification")
    if giveaway_role:
        ping_content = f"{giveaway_role.mention} 🎁 **New Raffle!**"

    # Send embed (with role ping in content if available)
    msg = await channel.send(content=ping_content, embed=embed, view=PersistentRaffleView())

    # Create proof thread if requirements exist
    proof_thread_id = None
    if requirements:
        try:
            proof_thread = await msg.create_thread(
                name=f"📸 Proof: {title[:80]}",
                auto_archive_duration=4320
            )
            proof_thread_id = proof_thread.id
            formatted_for_thread = "\n".join(f"> {line}" for line in requirements.split("\n"))
            await proof_thread.send(
                f"📋 **Raffle Requirements:**\n{formatted_for_thread}\n\n"
                f"When you join the raffle, you'll be added here. "
                f"Please post a screenshot proving you've completed the requirements."
            )
        except Exception as e:
            logger.error(f"Failed to create proof thread: {e}")

    # Save to database
    await db.execute('''
        INSERT INTO event_raffles 
            (host_id, hosted_by, title, prize, requirements, winner_count,
             message_id, channel_id, proof_thread_id, ends_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ''', (
        creator_id,
        hosted_by_id,
        title, prize, requirements, winners,
        msg.id, channel.id, proof_thread_id, ends_at
    ))

    # Evaluate giveaway milestones for the host
    from services.giveaway_milestone_service import giveaway_milestone_service
    effective_host_id = hosted_by_id or creator_id
    try:
        await giveaway_milestone_service.evaluate_milestones(guild, effective_host_id)
    except Exception as e:
        logger.warning(f"Milestone eval on deploy failed: {e}")

    await interaction.followup.send(
        f"✅ Raffle **{title}** deployed in {channel.mention}!", ephemeral=True
    )


async def setup(bot: commands.Bot):
    await bot.add_cog(EventCog(bot))

