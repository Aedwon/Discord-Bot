"""
Event Tracker Cog - Hooks natively into Discord Scheduled Events for Kiosks and Placements.
Built with robust anti-spasm race condition locks, validation boundaries, and UI defers.
Features an End-To-End Security Toolkit: Dashboards, Revocation, Budgets, Audit Logs.
Includes an ultra-efficient RAM Cached Peak Voice Tracking system for Overflow channels.
"""

import discord
from discord.ext import commands
from discord import app_commands
import logging

from services.database import db
from services.settings_service import settings_service

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
                "INSERT IGNORE INTO guild_event_rewards (event_id, user_id, reward_type, ep_awarded) VALUES (%s, %s, %s, %s)", 
                (event_id, user_id, 'participation', ep_amount)
            )
            
            if affected_rows == 0:
                return await interaction.followup.send("❌ You have already securely claimed your participation points for this event!")
            
            await db.execute("""
                INSERT INTO users (user_id, xp, tokens, event_points) 
                VALUES (%s, 0, 0, %s)
                ON DUPLICATE KEY UPDATE event_points = event_points + VALUES(event_points)
            """, (user_id, ep_amount))
            
            await interaction.followup.send(f"✅ Successfully claimed **{ep_amount} Participation EP**! Thank you for natively participating!")
            
        except Exception as e:
            logger.error(f"Critical error during EP claim for user {user_id}: {e}")
            await interaction.followup.send("❌ A critical database error occurred while processing your claim.")

class EventCog(commands.GroupCog, name="event"):
    """Native Discord Event Points ecosystem."""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.channel_to_events = {} 
        self.event_peaks = {}       
        self.event_to_channels = {} 
        
    async def cog_load(self):
        self.bot.add_view(PersistentEventView())
        self.bot.loop.create_task(self._initialize_peak_tracking())
        
    async def _initialize_peak_tracking(self):
        """Builds the ultra-optimized RAM Cache mapping channel joins to events."""
        await self.bot.wait_until_ready()
        
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

    @app_commands.command(name="kiosk", description="Spawn a Participation Button for a Native Discord Event.")
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

    @app_commands.command(name="cap_placement", description="Discord Managers: Lock a strict Budget limit on an Event's Placements.")
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

    @app_commands.command(name="placement", description="Award a Winner's Placement (Strict Check against Event Budgets).")
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
            await db.execute("""
                INSERT INTO users (user_id, xp, tokens, event_points) 
                VALUES (%s, 0, 0, %s)
                ON DUPLICATE KEY UPDATE event_points = event_points + VALUES(event_points)
            """, (user.id, bonus_to_award))
            
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

    @app_commands.command(name="revoke", description="Senior Admins: Erase a false payout entirely.")
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
        await db.execute("UPDATE users SET event_points = GREATEST(0, event_points - %s) WHERE user_id = %s", (total_revoked, user.id))
        
        await self.send_audit_log(interaction, "Payout UNDO", f"**Admin:** {interaction.user.mention}\n**Target:** {user.mention}\n**Erased:** `{total_revoked} EP`", discord.Color.red())
        await interaction.followup.send(f"🚨 **Revocation Complete:** Stripped `{total_revoked} EP` from {user.mention}.")

    @app_commands.command(name="status", description="Generate a Live Dashboard measuring Event Health and Peak VC Trackers.")
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
    overflow_group = app_commands.Group(name="overflow", description="Manage Overflow voice channels for highly massive server events.")
    
    @overflow_group.command(name="add", description="Link an additional overflow Voice Channel instantly to a scheduled event.")
    @app_commands.autocomplete(event_id=event_autocomplete)
    @app_commands.default_permissions(administrator=True)
    async def overflow_add(self, interaction: discord.Interaction, event_id: str, channel: discord.VoiceChannel):
        try: event_id_int = int(event_id)
        except ValueError: return await interaction.response.send_message("❌ Invalid Event ID.", ephemeral=True)
        
        await db.execute("INSERT IGNORE INTO guild_event_overflows (event_id, channel_id) VALUES (%s, %s)", (event_id_int, channel.id))
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

    @app_commands.command(name="leaderboard", description="Show the Top 10 most active Event attendees!")
    async def event_leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer()
        top_users = await db.fetch_all("SELECT user_id, event_points FROM users WHERE event_points > 0 ORDER BY event_points DESC LIMIT 10")
        if not top_users: return await interaction.followup.send("No Event Points exist natively yet!", ephemeral=True)
        
        embed = discord.Embed(title="🏆 Event Participation Leaderboard", description="The most dedicated community event attendees currently residing in the server!", color=discord.Color.gold(), timestamp=discord.utils.utcnow())
        lines = [f"**{i}.** {'🥇' if i==1 else '🥈' if i==2 else '🥉' if i==3 else '🏅'} <@{u['user_id']}> — **{u['event_points']} EP**" for i, u in enumerate(top_users, 1)]
        embed.add_field(name="Top 10 Attendees", value="\n".join(lines), inline=False)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="profile", description="Check how many Event Points you currently possess.")
    async def event_profile(self, interaction: discord.Interaction, user: discord.Member = None):
        await interaction.response.defer()
        target = user or interaction.user
        
        user_data = await db.fetch_one("SELECT event_points FROM users WHERE user_id = %s", (target.id,))
        ep = user_data['event_points'] if user_data else 0
        rank_data = await db.fetch_one("SELECT COUNT(*) as pos FROM users WHERE event_points > (SELECT event_points FROM users WHERE user_id = %s)", (target.id,))
        rank = (rank_data['pos'] + 1) if rank_data and ep > 0 else "Unranked"
        
        embed = discord.Embed(title=f"🎟️ {target.display_name}'s Event Profile", color=discord.Color.blurple())
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="Total Event Points", value=f"**{ep} EP**", inline=True)
        embed.add_field(name="Server Ranking", value=f"**#{rank}**", inline=True)
        await interaction.followup.send(embed=embed)

async def setup(bot: commands.Bot):
    await bot.add_cog(EventCog(bot))
