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
from datetime import datetime, timedelta

from services.database import db
from services.settings_service import settings_service
from utils.checks import require_admin_auth

logger = logging.getLogger("mlbb_bot.event_cog")

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
        
    async def cog_load(self):
        self.bot.add_view(PersistentEventView())

    @commands.Cog.listener()
    async def on_ready(self):
        import asyncio
        asyncio.create_task(self._initialize_peak_tracking())

    async def _initialize_peak_tracking(self):
        """Builds the ultra-optimized RAM Cache mapping channel joins to events."""
        
        self.channel_to_events.clear()
        self.event_peaks.clear()
        self.event_to_channels.clear()
        
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
            
        for eid in events_to_evaluate:
            await self._evaluate_peak(eid, member.guild)
            
    @commands.Cog.listener()
    async def on_scheduled_event_update(self, before: discord.ScheduledEvent, after: discord.ScheduledEvent):
        # Refresh the RAM mapping architecture entirely if an event ends or begins globally
        if before.status != after.status:
            await self._initialize_peak_tracking()
            
            if after.status == discord.EventStatus.completed:
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

    # --- CLI COMMANDS ---

    @event_group.command(name="kiosk", description="Spawn a Participation Button for a Native Discord Event.")
    @app_commands.autocomplete(event_id=event_autocomplete)
    @app_commands.default_permissions(administrator=True)
    async def event_kiosk(self, interaction: discord.Interaction, event_id: str, ep: int, description: str = "Click the button below to claim your participation points!"):
        try: event_id_int = int(event_id)
        except ValueError: return await interaction.response.send_message("❌ Select an event from autocomplete.", ephemeral=True)
        if not (1 <= ep <= 100000): return await interaction.response.send_message("❌ EP bounds violation.", ephemeral=True)
            
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
    @app_commands.default_permissions(administrator=True)
    async def cap_placement(self, interaction: discord.Interaction, event_id: str, total_budget: int):
        try: event_id_int = int(event_id)
        except ValueError: return await interaction.response.send_message("❌ Select from autocomplete.", ephemeral=True)
        if total_budget < 0 or total_budget > 10000000: return await interaction.response.send_message("❌ Budget absurd.", ephemeral=True)
            
        await db.execute("""
            INSERT INTO guild_event_caps (event_id, total_budget, set_by) 
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE total_budget = VALUES(total_budget), set_by = VALUES(set_by)
        """, (event_id_int, total_budget, interaction.user.id))
        
        await self.send_audit_log(interaction, "Placement Budget Locked", f"**Event ID:** `{event_id}`\n**Total Budget Set:** `{total_budget} EP`", discord.Color.teal())
        await interaction.response.send_message(f"🔒 **Event Budget Locked:** Budget capped at **{total_budget} EP**.", ephemeral=True)

    @event_group.command(name="placement", description="Award a Winner's Placement (Strict Check against Event Budgets).")
    @app_commands.autocomplete(event_id=event_autocomplete)
    @app_commands.default_permissions(administrator=True)
    async def event_placement(self, interaction: discord.Interaction, event_id: str, user: discord.Member, placement: str, total_ep_value: int):
        try: event_id_int = int(event_id)
        except ValueError: return await interaction.response.send_message("❌ Select from autocomplete.", ephemeral=True)
        if not (1 <= total_ep_value <= 100000): return await interaction.response.send_message("❌ EP Bounds Violation.", ephemeral=True)
            
        await interaction.response.defer() 
            
        history = await db.fetch_one("SELECT SUM(ep_awarded) as total FROM guild_event_rewards WHERE event_id = %s AND user_id = %s", (event_id_int, user.id))
        already_earned = history['total'] if history and history['total'] else 0
        bonus_to_award = total_ep_value - already_earned
        
        if bonus_to_award <= 0:
            return await interaction.followup.send(f"❌ User already earned `{already_earned} EP` which cleanly exceeds/secures the global `{total_ep_value} EP` value of this placement.", ephemeral=True)
            
        cap = await db.fetch_one("SELECT total_budget FROM guild_event_caps WHERE event_id = %s", (event_id_int,))
        if cap:
            spent = await db.fetch_one("SELECT SUM(ep_awarded) as t FROM guild_event_rewards WHERE event_id = %s AND reward_type != 'participation'", (event_id_int,))
            total_spent = spent['t'] if spent and spent['t'] else 0
            if total_spent + bonus_to_award > cap['total_budget']:
                return await interaction.followup.send(f"❌ **Budget Block:** Payout (`{bonus_to_award} EP`) completely exceeds Manager Budget (`{cap['total_budget']} EP`).", ephemeral=True)

        claimed = await db.fetch_one("SELECT * FROM guild_event_rewards WHERE event_id = %s AND user_id = %s AND reward_type = %s", (event_id_int, user.id, placement))
        if claimed: return await interaction.followup.send(f"❌ User already received explicitly `{placement}`!", ephemeral=True)

        try:
            await db.execute("INSERT INTO guild_event_rewards (event_id, user_id, reward_type, ep_awarded) VALUES (%s, %s, %s, %s)", (event_id_int, user.id, placement, bonus_to_award))
            from services.ep_service import ep_service
            await ep_service.process_ep_update(interaction.guild, user.id, bonus_to_award, bypass_verification=True, is_placement=True)
            
            discord_event = interaction.guild.get_scheduled_event(event_id_int)
            event_name = discord_event.name if discord_event else f"Event Profile {event_id}"
            
            embed = discord.Embed(
                title="🏆 Event Winner Announced!",
                description=f"Congratulations to {user.mention} for miraculously securing **{placement}** in **{event_name}**!\n\nThey have been awarded **{total_ep_value} Total EP** for their incredible victory! 🎉",
                color=discord.Color.gold(),
                timestamp=discord.utils.utcnow()
            )
            embed.set_thumbnail(url=user.display_avatar.url)
            await interaction.followup.send(content=user.mention, embed=embed)
            await self.send_audit_log(interaction, "Placement Disbursed", f"**Mod:** {interaction.user.mention}\n**Victor:** {user.mention}\n**Bonus Paid:** `{bonus_to_award} EP`", discord.Color.purple())
        except Exception as e:
            logger.error(f"Failed to award placement: {e}")
            await interaction.followup.send("❌ **Fatal DB Error**.", ephemeral=True)

    @event_group.command(name="revoke", description="Senior Admins: Erase a false payout entirely.")
    @app_commands.autocomplete(event_id=event_autocomplete)
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
        hosted_by="Community member hosting this raffle (optional)"
    )
    async def raffle_create(
        self, interaction: discord.Interaction,
        title: str, prize: str, channel: discord.TextChannel,
        winners: int = 1,
        duration_minutes: int = None,
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
    @app_commands.describe(raffle_id="The raffle ID (shown in /event raffle list)")
    async def raffle_draw(self, interaction: discord.Interaction, raffle_id: int):
        await interaction.response.defer(ephemeral=True)

        raffle = await db.fetch_one(
            "SELECT * FROM event_raffles WHERE id = %s AND status = 'active'", (raffle_id,)
        )
        if not raffle:
            return await interaction.followup.send(
                f"❌ Raffle #{raffle_id} not found or already drawn.", ephemeral=True
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
    @app_commands.describe(
        raffle_id="The ID of the drawn raffle",
        reason="Required reason for disqualifying current winners / authorizing a reroll.",
        disqualified_winner="Optional: A specific winner to disqualify and replace. Leave blank for full reroll."
    )
    async def raffle_reroll(self, interaction: discord.Interaction, raffle_id: int, reason: str, disqualified_winner: discord.Member = None):
        await interaction.response.defer(ephemeral=True)

        raffle = await db.fetch_one(
            "SELECT * FROM event_raffles WHERE id = %s", (raffle_id,)
        )
        if not raffle:
            return await interaction.followup.send(f"❌ Raffle #{raffle_id} not found.", ephemeral=True)
            
        if raffle['status'] != 'drawn':
            return await interaction.followup.send(f"❌ Raffle #{raffle_id} has not been drawn yet. Its status is '{raffle['status']}'.", ephemeral=True)

        raffle_cog = self.bot.get_cog("Event Raffle")
        if not raffle_cog:
            return await interaction.followup.send("❌ Raffle engine not loaded.", ephemeral=True)

        await raffle_cog.execute_reroll(raffle, disqualified_winner, reason, interaction.guild, interaction)

    @raffle_group.command(name="cancel", description="Cancel an active raffle")
    @require_admin_auth()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(raffle_id="The raffle ID to cancel")
    async def raffle_cancel(self, interaction: discord.Interaction, raffle_id: int):
        await interaction.response.defer(ephemeral=True)

        raffle = await db.fetch_one(
            "SELECT * FROM event_raffles WHERE id = %s AND status = 'active'", (raffle_id,)
        )
        if not raffle:
            return await interaction.followup.send(
                f"❌ Raffle #{raffle_id} not found or not active.", ephemeral=True
            )

        await db.execute("UPDATE event_raffles SET status = 'cancelled' WHERE id = %s", (raffle_id,))

        # Update the embed to show cancelled
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

    @raffle_group.command(name="force_sync", description="Surgically overwrite the text of legacy announcement messages to match current DB winners.")
    @require_admin_auth()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(raffle_id="The ID of the corrupted raffle whose messages are out of date")
    async def raffle_force_sync(self, interaction: discord.Interaction, raffle_id: int):
        await interaction.response.defer(ephemeral=True)
        # 1. Fetch DB
        raffle = await db.fetch_one("SELECT * FROM event_raffles WHERE id = %s", (raffle_id,))
        if not raffle:
            return await interaction.followup.send(f"❌ Raffle #{raffle_id} missing.", ephemeral=True)
            
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
    hosted_by_id = config['hosted_by_id']
    creator_id = config['creator_id']
    requirements = config.get('requirements')

    ends_at = None
    if duration_minutes and duration_minutes > 0:
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

    await interaction.followup.send(
        f"✅ Raffle **{title}** deployed in {channel.mention}!", ephemeral=True
    )


async def setup(bot: commands.Bot):
    await bot.add_cog(EventCog(bot))

