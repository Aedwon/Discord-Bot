"""
XP/Leveling Cog - Handles XP gain from messages, reactions, and voice.
All commands are slash commands.
"""

import discord
import datetime
from random import randint
from discord.ext import commands, tasks
from discord import app_commands

from config import XP_CONFIG, BATCH_UPDATE_INTERVAL
from services.xp_service import xp_service
from services.settings_service import settings_service
from services.verification_service import verification_service
from utils.embeds import create_leaderboard_embed, create_rank_embed
from utils.checks import require_admin_auth


class XpCog(commands.Cog, name="Leveling"):
    """XP and Leveling system for the server."""
    
    xp_group = app_commands.Group(name="xp", description="XP & Leveling system commands", default_permissions=discord.Permissions(administrator=True))
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        
        # Spam prevention caches
        self.gained_msg_xp = set()
        self.pending_xp = {}
        
        # Reaction XP tracking
        self.message_reaction_xp = {}
        self.user_reacted_to_message = set()
        self.daily_reaction_cache = {}
        
        # Track which channel each user last chatted in (for level-up messages)
        self._last_message_channel = {}
        
    async def cog_load(self):
        """Called when cog is loaded - start background task natively."""
        if not self.batch_update_db.is_running():
            self.batch_update_db.start()
    
    def cog_unload(self):
        self.batch_update_db.cancel()
    
    # ─────────────────────────────────────────────────────────────────────
    # Background Task - Batch XP Updates
    # ─────────────────────────────────────────────────────────────────────
    
    @tasks.loop(seconds=BATCH_UPDATE_INTERVAL)
    async def batch_update_db(self):
        """Process pending XP and award voice XP."""
        if not self.bot.is_ready():
            return
            
        self.gained_msg_xp.clear()
        
        # Clear large caches
        if len(self.message_reaction_xp) > 10000:
            self.message_reaction_xp.clear()
        if len(self.user_reacted_to_message) > 50000:
            self.user_reacted_to_message.clear()
        
        try:
            await self._process_voice_xp()
        except Exception as e:
            print(f"[XP Loop] Voice processing error: {e}")
        
        if self.pending_xp:
            # Snapshot which channels users were chatting in for level-up messages
            user_channels = dict(self._last_message_channel) if hasattr(self, '_last_message_channel') else {}
            
            try:
                updates = await xp_service.batch_update(self.pending_xp.copy())
            except Exception as e:
                print(f"[XP Loop] Database batch_update error: {e}")
                updates = {}
                
            self.pending_xp.clear()
            
            # Process level changes, role assignments, and notifications
            guild = self.bot.guilds[0] if self.bot.guilds else None
            if guild:
                from services.badge_service import badge_service
                for user_id, data in updates.items():
                    member = guild.get_member(user_id)
                    if member:
                        await badge_service.eval_twilight(member)
                    
                    channel_id = user_channels.get(user_id)
                    notify_channel = guild.get_channel(channel_id) if channel_id else None
                    
                    await self._handle_level_change(
                        guild, user_id, data["old_xp"], data["new_xp"],
                        notify_channel=notify_channel
                    )

    async def _assign_tier_role(self, guild: discord.Guild, member: discord.Member, tier_name: str, old_tier: str = None):
        """Assign a tier role to a member, stripping the old one if present. Respects rate limits."""
        r_id = await settings_service.get(f"xp_role_{tier_name.replace(' ', '_')}")
        if not r_id or r_id == "0":
            return
        
        role = guild.get_role(int(r_id))
        if not role:
            return
        
        # Skip if they already have it
        if role in member.roles:
            return
        
        try:
            # Strip old tier role if present
            if old_tier:
                o_id = await settings_service.get(f"xp_role_{old_tier.replace(' ', '_')}")
                if o_id and o_id != "0":
                    o_role = guild.get_role(int(o_id))
                    if o_role and o_role in member.roles:
                        await member.remove_roles(o_role, reason="XP Tier Change")
            
            await member.add_roles(role, reason=f"XP Tier: {tier_name}")
        except discord.Forbidden:
            pass

    async def _get_alert_channel(self, guild: discord.Guild):
        """Get the configured level alerts channel, if any."""
        ch_id = await settings_service.get_int("level_alerts_channel_id")
        if ch_id:
            return guild.get_channel(ch_id)
        return None

    async def _handle_level_change(
        self, guild: discord.Guild, user_id: int,
        old_xp: int, new_xp: int,
        notify_channel: discord.TextChannel = None,
        interaction: discord.Interaction = None
    ):
        """
        Detect level/tier changes, assign roles, and send notifications.
        Routing:
          - Level up (same tier) → chat channel / interaction
          - Rank up (tier changed) → dedicated alert channel
          - First XP gain → alert channel
        """
        old_lvl = xp_service.get_level(old_xp)
        new_lvl = xp_service.get_level(new_xp)
        
        old_tier = xp_service.get_tier_name(old_lvl)
        new_tier = xp_service.get_tier_name(new_lvl)
        
        member = guild.get_member(user_id)
        if not member:
            return
        
        alert_channel = await self._get_alert_channel(guild)
        
        # DEFAULT TIER: First XP gain → assign starting tier role + welcome in alert channel
        if old_xp == 0 and new_xp > 0 and new_tier:
            await self._assign_tier_role(guild, member, new_tier)
            target = alert_channel or notify_channel
            if target:
                try:
                    embed = discord.Embed(
                        description=f"🌟 Joined the ranks as **{new_tier}** (Level {new_lvl})!",
                        color=discord.Color.blue()
                    )
                    embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
                    await target.send(content=member.mention, embed=embed)
                except Exception:
                    pass
            return
        
        if new_lvl <= old_lvl:
            return
        
        # RANK UP (tier changed) → alert channel
        if new_tier and new_tier != old_tier:
            await self._assign_tier_role(guild, member, new_tier, old_tier)
            
            rank_embed = discord.Embed(
                title="⚔️ EXP Rank Up!",
                description=(
                    f"**{old_tier or 'Unranked'}** → **{new_tier}**\n"
                    f"Level **{new_lvl}** • `{new_xp:,}` XP"
                ),
                color=discord.Color.gold()
            )
            rank_embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
            rank_embed.set_thumbnail(url=member.display_avatar.url)
            
            target = alert_channel or notify_channel
            if interaction:
                try: await interaction.followup.send(content=member.mention, embed=rank_embed)
                except Exception: pass
            if target:
                try: await target.send(content=member.mention, embed=rank_embed)
                except Exception: pass
        else:
            # LEVEL UP (same tier) → chat channel
            lvl_embed = discord.Embed(
                description=f"⬆️ Reached **Level {new_lvl}**! ({new_xp:,} XP)",
                color=discord.Color.green()
            )
            lvl_embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
            
            if interaction:
                try: await interaction.followup.send(content=member.mention, embed=lvl_embed)
                except Exception: pass
            elif notify_channel:
                try: await notify_channel.send(content=member.mention, embed=lvl_embed)
                except Exception: pass


    async def _is_xp_enabled(self) -> bool:
        """Check if the XP system is enabled."""
        return await settings_service.get_int("xp_system_enabled") == 1
    
    async def _process_voice_xp(self):
        """Award XP to active voice channel participants."""
        # Check if XP system is enabled
        if not await self._is_xp_enabled():
            return
        
        if not self.bot.guilds:
            return
        
        guild = self.bot.guilds[0]
        afk_channel = guild.afk_channel
        voice_config = XP_CONFIG["voice"]
        
        for vc in guild.voice_channels:
            if afk_channel and vc.id == afk_channel.id:
                continue
            
            # Get all verified human members in the channel
            human_members = [
                m for m in vc.members
                if not m.bot and verification_service.is_verified(m.id)
            ]
            
            if len(human_members) >= voice_config["min_members"]:
                for member in human_members:
                    # Skip stage listeners who are suppressed
                    if member.voice.suppress:
                        continue
                        
                    # Tier 1: Streaming or Video (Highest)
                    if member.voice.self_stream or member.voice.self_video:
                        xp_amt = voice_config.get("xp_stream_video", 4)
                    # Tier 4: Deafened (Lowest/None)
                    elif member.voice.deaf or member.voice.self_deaf:
                        xp_amt = voice_config.get("xp_deafened", 0)
                    # Tier 3: Muted (Low)
                    elif member.voice.mute or member.voice.self_mute:
                        xp_amt = voice_config.get("xp_muted", 1)
                    # Tier 2: Unmuted (Normal)
                    else:
                        xp_amt = voice_config.get("xp_unmuted", 2)
                        
                    if xp_amt > 0:
                        self.pending_xp[member.id] = (
                            self.pending_xp.get(member.id, 0) + xp_amt
                        )
    
    # ─────────────────────────────────────────────────────────────────────
    # Event Listeners
    # ─────────────────────────────────────────────────────────────────────
    
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Award XP for messages (with spam prevention)."""
        if message.author.bot:
            return
        
        # Check if XP system is enabled
        if not await self._is_xp_enabled():
            return
        
        # Verification gate — unverified users earn no XP
        if not verification_service.is_verified(message.author.id):
            return
        
        msg_config = XP_CONFIG["message"]
        
        # User requested to remove the minimum 10 characters check.
        # Cooldown is handled globally by self.gained_msg_xp which clears every 10s cycle.
        
        # Ignore bot channel
        bot_channel_id = await settings_service.get_int("bot_channel_id")
        if message.channel.id == bot_channel_id:
            return
        
        user_id = message.author.id
        
        # Track which channel the user is chatting in for level-up messages
        self._last_message_channel[user_id] = message.channel.id
        
        # Automatically un-blacklist users if the background loop died
        if not self.batch_update_db.is_running() and user_id in self.gained_msg_xp:
            self.gained_msg_xp.remove(user_id)
            self.batch_update_db.start()
        
        if user_id not in self.gained_msg_xp:
            self.gained_msg_xp.add(user_id)
            xp_amount = randint(msg_config["min_xp"], msg_config["max_xp"])
            self.pending_xp[user_id] = self.pending_xp.get(user_id, 0) + xp_amount
            
            if message.content.startswith("Xptest"):
                await message.reply(f"🚀 **LIVE DEBUG:** Message approved! You earned `{xp_amount}` XP! (It is now in my RAM waiting for the 10s batch cycle to push to the database)", delete_after=15)
        else:
            if message.content.startswith("Xptest"):
                await message.reply(f"⏳ **LIVE DEBUG:** You are in `gained_msg_xp` (cooldown phase). You must wait for the background loop to clear this before earning again.", delete_after=15)
    
    @commands.Cog.listener()
    async def on_reaction_add(self, reaction: discord.Reaction, user: discord.User):
        """Award XP for reactions with caps."""
        if user.bot:
            return
        
        # Check if XP system is enabled
        if not await self._is_xp_enabled():
            return
        
        # Verification gate
        if not verification_service.is_verified(user.id):
            return
        
        react_config = XP_CONFIG["reaction"]
        msg_id = reaction.message.id
        user_id = user.id
        today = str(datetime.date.today())
        
        reaction_key = (user_id, msg_id)
        if reaction_key in self.user_reacted_to_message:
            return
        
        user_daily = self.daily_reaction_cache.get(user_id, {'date': today, 'xp': 0})
        if user_daily['date'] != today:
            user_daily = {'date': today, 'xp': 0}
        
        if user_daily['xp'] >= react_config["daily_cap"]:
            return
        
        msg_total_xp = self.message_reaction_xp.get(msg_id, 0)
        if msg_total_xp >= react_config["max_xp_per_message"]:
            return
        
        xp_amount = react_config["xp_per_reaction"]
        
        self.user_reacted_to_message.add(reaction_key)
        self.message_reaction_xp[msg_id] = msg_total_xp + xp_amount
        user_daily['xp'] += xp_amount
        self.daily_reaction_cache[user_id] = user_daily
        
        self.pending_xp[user_id] = self.pending_xp.get(user_id, 0) + xp_amount
    
    # ─────────────────────────────────────────────────────────────────────
    # Slash Commands
    # ─────────────────────────────────────────────────────────────────────
    
    @app_commands.command(name="levels-leaderboard", description="Show the top 10 server XP leaderboard")
    async def leaderboard(self, inter: discord.Interaction):
        """Show the server XP leaderboard."""
        await inter.response.defer()
        top_users = await xp_service.get_leaderboard(limit=10)
        embed = create_leaderboard_embed(inter.guild, top_users)
        
        # Calculate invoker rank
        rank, xp = await xp_service.get_rank(inter.user.id)
        rank_str = f"#{rank}" if rank > 0 else "Unranked"
        embed.set_footer(text=f"Your Server-Wide Level Rank: {rank_str} | {xp:,} XP", icon_url=inter.user.display_avatar.url)
        
        await inter.followup.send(embed=embed)
    
    @app_commands.command(name="profile", description="Show your unified Community Profile (XP, Events, Verification)")
    async def profile(self, inter: discord.Interaction, member: discord.Member = None):
        """Show unified Community Profile."""
        await inter.response.defer()
        target = member or inter.user
        
        # XP
        rank, xp = await xp_service.get_rank(target.id)
        level = xp_service.get_level(xp)
        xp_tier = xp_service.get_tier_name(level)
        rank_display = f"#{rank}" if rank > 0 else "Unranked"
            
        # Events
        from services.database import db
        user_data = await db.fetch_one("SELECT event_points FROM users WHERE user_id = %s", (target.id,))
        ep = (user_data['event_points'] or 0) if user_data else 0
        
        ep_rank_data = await db.fetch_one("SELECT COUNT(*) as pos FROM users WHERE event_points > %s", (ep,))
        ep_rank = (ep_rank_data['pos'] + 1) if ep_rank_data and ep > 0 else "Unranked"
        ep_rank_display = f"#{ep_rank}" if isinstance(ep_rank, int) else ep_rank
        
        event_data = await db.fetch_one("SELECT COUNT(*) as total FROM event_redemptions WHERE user_id = %s", (target.id,))
        events_attended = event_data['total'] if event_data else 0
        
        from services.ep_service import ep_service
        current_ep_tier, next_ep_tier, ep_progress = ep_service.get_tier_progress(ep)
        
        # Verification
        is_verified = verification_service.is_verified(target.id)
        is_msl = False
        if is_verified:
            user_info = await verification_service.get_user_info(target.id)
            uid = user_info['mlbb_uid'] if user_info else None
            if hasattr(verification_service, 'is_msl') and uid:
                is_msl = verification_service.is_msl(uid)
                
        ver_status = "✅ Verified" if is_verified else "❌ Unverified"
        if is_msl:
            ver_status += " 🎓 MSL"
            
        # Build Embed
        embed = discord.Embed(title=f"👤 Community Profile: {target.display_name}", color=target.color or discord.Color.blue())
        embed.set_thumbnail(url=target.display_avatar.url)
        
        # XP Field
        embed.add_field(name="Experience (XP)", value=f"**Level {level}** • `{xp:,} XP`\nGlobal Rank: **{rank_display}**\nTier: **{xp_tier or 'None'}**", inline=True)
        
        # Events Field
        embed.add_field(name="Event Points (EP)", value=f"**{ep:,} EP**\nGlobal Rank: **{ep_rank_display}**\nAttended: **{events_attended} Events**\nTier: **{current_ep_tier}**", inline=True)
        
        # Verification Field
        embed.add_field(name="MLBB Verification", value=f"**Status:** {ver_status}", inline=False)
        
        embed.set_footer(text=f"Requested by {inter.user.display_name}", icon_url=inter.user.display_avatar.url)
        await inter.followup.send(embed=embed)
    
    # ─────────────────────────────────────────────────────────────────────
    # Admin Commands - XP System Control
    # ─────────────────────────────────────────────────────────────────────
    
    @xp_group.command(name="start", description="Start the XP system (enable XP gain)")
    @require_admin_auth()
    @app_commands.default_permissions(administrator=True)
    async def xp_start(self, inter: discord.Interaction):
        """Enable the XP system."""
        current = await settings_service.get_int("xp_system_enabled")
        if current == 1:
            return await inter.response.send_message("⚠️ XP system is already running.", ephemeral=True)
        
        await settings_service.set("xp_system_enabled", "1")
        
        embed = discord.Embed(
            title="✅ XP System Started",
            description="Users can now earn XP from messages, voice, and reactions.",
            color=discord.Color.green()
        )
        await inter.response.send_message(embed=embed)
    
    @xp_group.command(name="stop", description="Stop the XP system (disable XP gain)")
    @require_admin_auth()
    @app_commands.default_permissions(administrator=True)
    async def xp_stop(self, inter: discord.Interaction):
        """Disable the XP system."""
        current = await settings_service.get_int("xp_system_enabled")
        if current == 0:
            return await inter.response.send_message("⚠️ XP system is already stopped.", ephemeral=True)
        
        await settings_service.set("xp_system_enabled", "0")
        
        # Clear pending XP so nothing gets processed
        self.pending_xp.clear()
        
        embed = discord.Embed(
            title="⏹️ XP System Stopped",
            description="XP gain is now disabled. Existing XP is preserved.",
            color=discord.Color.orange()
        )
        await inter.response.send_message(embed=embed)

    @xp_group.command(name="add", description="Add XP to a specific user (Admin only)")
    @app_commands.describe(user="The user to grant XP to", amount="Amount of XP to add")
    @require_admin_auth()
    @app_commands.default_permissions(administrator=True)
    async def xp_add(self, inter: discord.Interaction, user: discord.Member, amount: int):
        from services.xp_service import xp_service
        await inter.response.defer()
        
        old_xp = await xp_service.get_xp(user.id)
        new_total = await xp_service.add_xp(user.id, amount)
        
        embed = discord.Embed(
            title="✨ XP Granted",
            description=f"Successfully added {amount} XP to {user.mention}.\nNew Total: **{new_total} XP**",
            color=discord.Color.green()
        )
        await inter.followup.send(embed=embed)
        
        # Trigger level-up detection, role assignment, and notifications
        if inter.guild:
            await self._handle_level_change(
                inter.guild, user.id, old_xp, new_total,
                interaction=inter
            )
        
    @xp_group.command(name="set", description="Set a specific user's XP (Admin only)")
    @app_commands.describe(user="The user to modify", amount="Exact XP amount to set")
    @require_admin_auth()
    @app_commands.default_permissions(administrator=True)
    async def xp_set(self, inter: discord.Interaction, user: discord.Member, amount: int):
        from services.xp_service import xp_service
        await xp_service.set_currency(user.id, xp=amount)
        embed = discord.Embed(
            title="⚙️ XP Overridden",
            description=f"Successfully set {user.mention}'s XP to **{amount}**.",
            color=discord.Color.orange()
        )
        await inter.response.send_message(embed=embed)
    
    @xp_group.command(name="reset", description="Reset all user XP to zero")
    @require_admin_auth()
    @app_commands.default_permissions(administrator=True)
    async def xp_reset(self, inter: discord.Interaction):
        """Reset all XP data. Requires confirmation."""
        from services.database import db
        
        # Create confirmation view
        class ConfirmView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=30)
                self.confirmed = False
            
            @discord.ui.button(label="Confirm Reset", style=discord.ButtonStyle.danger)
            async def confirm(self, button_inter: discord.Interaction, button: discord.ui.Button):
                self.confirmed = True
                self.stop()
                
                # Reset all XP
                await db.execute("UPDATE users SET xp = 0")
                
                embed = discord.Embed(
                    title="🔄 XP Reset Complete",
                    description="All user XP has been reset to 0.",
                    color=discord.Color.red()
                )
                await button_inter.response.edit_message(embed=embed, view=None)
            
            @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
            async def cancel(self, button_inter: discord.Interaction, button: discord.ui.Button):
                self.stop()
                await button_inter.response.edit_message(
                    content="❌ XP reset cancelled.",
                    embed=None,
                    view=None
                )
        
        view = ConfirmView()
        embed = discord.Embed(
            title="⚠️ Confirm XP Reset",
            description="**This will reset ALL user XP to 0.**\n\nThis action cannot be undone!",
            color=discord.Color.red()
        )
        await inter.response.send_message(embed=embed, view=view, ephemeral=True)
    
    @xp_group.command(name="status", description="Check if the XP system is running")
    @require_admin_auth()
    @app_commands.default_permissions(administrator=True)
    async def xp_status(self, inter: discord.Interaction):
        """Show XP system status."""
        enabled = await settings_service.get_int("xp_system_enabled") == 1
        pending_count = len(self.pending_xp)
        pending_total = sum(self.pending_xp.values())
        
        embed = discord.Embed(
            title="📊 XP System Status",
            color=discord.Color.green() if enabled else discord.Color.red()
        )
        embed.add_field(name="Status", value="🟢 Running" if enabled else "🔴 Stopped", inline=True)
        embed.add_field(name="Pending Users", value=str(pending_count), inline=True)
        embed.add_field(name="Pending XP", value=str(pending_total), inline=True)
        
        await inter.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(XpCog(bot))
