"""
XP/Leveling Cog - Handles XP gain from messages, reactions, and voice.
All commands are slash commands.
"""

import discord
import datetime
from random import randint
from discord.ext import commands, tasks
from discord import app_commands

import logging

from config import XP_CONFIG, BATCH_UPDATE_INTERVAL
from services.xp_service import xp_service
from services.settings_service import settings_service
from services.verification_service import verification_service
from services.quest_service import quest_service
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
        
        # Voice XP daily cap tracking
        self.daily_voice_time = {}  # {user_id: {"date": "YYYY-MM-DD", "seconds": int}}
        
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

    async def _get_all_tier_roles(self, guild: discord.Guild) -> dict[str, discord.Role]:
        """Build a lookup of all configured XP tier names → Role objects.
        Cached per call, not globally, to respect live setting changes."""
        ranks = ["Commoner", "Vassal", "Noble", "High Noble"]
        numerals = ["V", "IV", "III", "II", "I"]
        all_tiers = [f"{r} {n}" for r in ranks for n in numerals] + ["Monarch"]
        
        tier_roles: dict[str, discord.Role] = {}
        for name in all_tiers:
            r_id = await settings_service.get(f"xp_role_{name.replace(' ', '_')}")
            if r_id and r_id != "0":
                role = guild.get_role(int(r_id))
                if role:
                    tier_roles[name] = role
        return tier_roles

    async def _assign_tier_role(self, guild: discord.Guild, member: discord.Member, tier_name: str, old_tier: str = None):
        """Assign the correct tier role and strip ALL other stale tier roles.
        This ensures sync regardless of how the user's role state got corrupted."""
        tier_roles = await self._get_all_tier_roles(guild)
        correct_role = tier_roles.get(tier_name)
        if not correct_role:
            return
        
        # Already perfect — nothing to do
        if correct_role in member.roles:
            # Still strip any OTHER stale tier roles they might have accumulated
            stale = [r for name, r in tier_roles.items() if r in member.roles and name != tier_name]
            if stale:
                try:
                    await member.remove_roles(*stale, reason="XP Sync: Stripping stale tiers")
                except discord.Forbidden:
                    pass
            return
        
        try:
            # Strip ALL tier roles that aren't the correct one (single API call)
            stale = [r for name, r in tier_roles.items() if r in member.roles and name != tier_name]
            if stale:
                await member.remove_roles(*stale, reason="XP Tier Change")
            
            await member.add_roles(correct_role, reason=f"XP Tier: {tier_name}")
        except discord.Forbidden:
            pass

    async def _strip_all_tier_roles(self, guild: discord.Guild, member: discord.Member):
        """Remove ALL XP tier roles from a member (used for resets/demotions to level 0)."""
        tier_roles = await self._get_all_tier_roles(guild)
        roles_to_remove = [r for r in tier_roles.values() if r in member.roles]
        if roles_to_remove:
            try:
                await member.remove_roles(*roles_to_remove, reason="XP Reset: Tier roles stripped")
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
        Detect level/tier changes (up AND down), assign roles, and send notifications.
        Handles: first XP gain, level up, tier rank up, demotion (admin /xp set), and reset.
        """
        old_lvl = xp_service.get_level(old_xp)
        new_lvl = xp_service.get_level(new_xp)
        
        old_tier = xp_service.get_tier_name(old_lvl)
        new_tier = xp_service.get_tier_name(new_lvl)
        
        member = guild.get_member(user_id)
        if not member:
            return
        
        alert_channel = await self._get_alert_channel(guild)
        
        # ── DEMOTION / RESET: Tier went DOWN (e.g. admin /xp set lower or /xp reset) ──
        if new_lvl < old_lvl and new_tier != old_tier:
            if new_xp <= 0:
                # Full reset — strip all tier roles
                await self._strip_all_tier_roles(guild, member)
            else:
                # Demotion to a lower tier — assign the correct lower tier
                await self._assign_tier_role(guild, member, new_tier)
            return
        
        # ── Same level, but tier roles might be out of sync (e.g. /xp set to same level range) ──
        if new_lvl == old_lvl and new_tier:
            # Silently ensure role is correct without notifications
            await self._assign_tier_role(guild, member, new_tier)
            return
        
        # ── FIRST XP GAIN: 0 → positive ──
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
                title="✨ Tier Rank Up! ✨",
                description=(
                    f"**{old_tier or 'Unranked'}** ➔ **{new_tier}**\n\n"
                    f"**Level Reached:** `{new_lvl}`\n"
                    f"**Total XP:** `{new_xp:,}`"
                ),
                color=discord.Color.gold()
            )
            rank_embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
            rank_embed.set_thumbnail(url=member.display_avatar.url)
            
            target = alert_channel or notify_channel
            if interaction:
                try: await interaction.followup.send(content=member.mention, embed=rank_embed)
                except Exception: pass
            elif target:
                try: await target.send(content=member.mention, embed=rank_embed)
                except Exception: pass
        else:
            # LEVEL UP (same tier) → alert channel
            lvl_embed = discord.Embed(
                title="🎉 Level Up!",
                description=f"Reached **Level {new_lvl}**!\n\n**Total XP:** `{new_xp:,}`",
                color=discord.Color.green()
            )
            lvl_embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
            lvl_embed.set_thumbnail(url=member.display_avatar.url)
            
            target = alert_channel or notify_channel
            if interaction:
                try: await interaction.followup.send(content=member.mention, embed=lvl_embed)
                except Exception: pass
            elif target:
                try: await target.send(content=member.mention, embed=lvl_embed)
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
        daily_cap = voice_config.get("daily_cap_seconds", 14400)  # 4 hours default
        today = str(datetime.date.today())
        
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
                    
                    # ── Daily voice cap check ──
                    user_voice = self.daily_voice_time.get(member.id, {"date": today, "seconds": 0})
                    if user_voice["date"] != today:
                        user_voice = {"date": today, "seconds": 0}
                    
                    if user_voice["seconds"] >= daily_cap:
                        continue  # Capped for today
                    
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
                        # Increment daily voice time and award XP
                        user_voice["seconds"] += BATCH_UPDATE_INTERVAL
                        self.daily_voice_time[member.id] = user_voice
                        self.pending_xp[member.id] = (
                            self.pending_xp.get(member.id, 0) + xp_amt
                        )
                        
                        # Quest progress — +1 VC minute per 60s cycle
                        try:
                            completed = await quest_service.increment_progress(member.id, "vc_minutes", 1)
                            for q in completed:
                                await xp_service.add_xp(member.id, q["reward_xp"])
                        except Exception as e:
                            logger.error(f"Quest progress error (voice): {e}")
    
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
                await message.reply(f"🚀 **LIVE DEBUG:** Message approved! You earned `{xp_amount}` XP! (It is now in my RAM waiting for the 60s batch cycle to push to the database)", delete_after=15)
        else:
            if message.content.startswith("Xptest"):
                await message.reply(f"⏳ **LIVE DEBUG:** You are in `gained_msg_xp` (cooldown phase). You must wait for the background loop to clear this before earning again.", delete_after=15)
        
        # Quest progress — every qualifying message counts (independent of XP cooldown)
        try:
            completed = await quest_service.increment_progress(user_id, "message_count", 1)
            for q in completed:
                await xp_service.add_xp(user_id, q["reward_xp"])
        except Exception as e:
            logger.error(f"Quest progress error (message): {e}")
    
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
        
        # Quest progress — reaction count
        try:
            completed = await quest_service.increment_progress(user_id, "reaction_count", 1)
            for q in completed:
                await xp_service.add_xp(user_id, q["reward_xp"])
        except Exception as e:
            logger.error(f"Quest progress error (reaction): {e}")
    
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
        
        # ── XP Data ──
        rank, xp = await xp_service.get_rank(target.id)
        level = xp_service.get_level(xp)
        xp_tier = xp_service.get_tier_name(level)
        rank_display = f"#{rank}" if rank is not None and rank > 0 else "Unranked"
        
        # XP progress bar
        current_level_xp = xp_service.get_xp_for_level(level)
        next_level_xp = xp_service.get_xp_for_level(level + 1)
        xp_level_range = next_level_xp - current_level_xp
        
        if level >= 101:
            xp_progress = 1.0
            xp_remaining = 0
        elif xp_level_range > 0:
            xp_progress = min((xp - current_level_xp) / xp_level_range, 1.0)
            xp_remaining = next_level_xp - xp
        else:
            xp_progress = 1.0
            xp_remaining = 0
        
        xp_bar_fill = int(xp_progress * 16)
        xp_bar = "▰" * xp_bar_fill + "▱" * (16 - xp_bar_fill)
        xp_pct = int(xp_progress * 100)
        
        # ── EP Data ──
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
        
        # EP progress bar
        ep_bar_fill = int(ep_progress * 16)
        ep_bar = "▰" * ep_bar_fill + "▱" * (16 - ep_bar_fill)
        ep_pct = int(ep_progress * 100)
        
        # ── Verification ──
        is_verified = verification_service.is_verified(target.id)
        is_msl = False
        if is_verified:
            user_info = await verification_service.get_user_info(target.id)
            uid = user_info['mlbb_uid'] if user_info else None
            server = user_info['mlbb_server'] if user_info else None
            if hasattr(verification_service, 'is_msl') and uid and server:
                is_msl = verification_service.is_msl(uid, server)
        
        ver_icon = "✅" if is_verified else "❌"
        ver_label = "Verified" if is_verified else "Unverified"
        msl_tag = "  ⸱  🎓 MSL" if is_msl else ""
        
        # ── Badges ──
        from services.badge_service import badge_service
        badges = await badge_service.get_badges(target.id)
        
        BADGE_ICONS = {
            "Twilight Pilgrim": "🌅",
            "The First People": "🏛️",
            "Moniyan Sage": "📜",
            "Battlefield God": "⚔️",
            "Mogul of the Land": "💰",
            "Convivialist": "🎊",
        }
        
        # ── Themed Embed Color ──
        # Pick a rich color based on the user's highest tier progression
        tier_colors = {
            "Commoner": 0x8B8B8B,     # Stone grey
            "Vassal": 0x4A9E4A,        # Forest green
            "Noble": 0x3A7BD5,         # Royal blue
            "High Noble": 0x9B59B6,    # Amethyst purple
            "Monarch": 0xF1C40F,       # Gold
        }
        embed_color = discord.Color.blue()
        if xp_tier:
            main_rank = xp_tier.split(" ")[0]
            if main_rank in tier_colors:
                embed_color = discord.Color(tier_colors[main_rank])
        if target.color and target.color != discord.Color.default():
            embed_color = target.color
        
        # ═══════════════════════════════════════════════════════
        #  BUILD THE EMBED
        # ═══════════════════════════════════════════════════════
        
        embed = discord.Embed(color=embed_color)
        embed.set_author(
            name=f"{target.display_name}'s Community Profile",
            icon_url=target.display_avatar.url
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        
        # ── XP Section ──
        if level >= 101:
            xp_progress_line = f"`{xp_bar}` **MAX**\n> 👑 *Monarch — The Apex*"
        else:
            xp_progress_line = (
                f"`{xp_bar}` **{xp_pct}%**\n"
                f"> `{xp - current_level_xp:,}` / `{xp_level_range:,}` XP — **{xp_remaining:,}** to Lv. {level + 1}"
            )
        
        xp_section = (
            f"**⚡ Level {level}** ⸱ {xp_tier or 'None'}\n"
            f"🏅 Rank: **{rank_display}** ⸱ `{xp:,} XP`\n\n"
            f"{xp_progress_line}"
        )
        embed.add_field(name="── ⚔️ Experience (XP) ──", value=xp_section, inline=False)
        
        # ── EP Section ──
        if next_ep_tier:
            ep_progress_line = (
                f"`{ep_bar}` **{ep_pct}%**\n"
                f"> Next: **{next_ep_tier}**"
            )
        else:
            ep_progress_line = f"`{ep_bar}` **MAX**\n> 🔱 *Mythic — The Summit*"
        
        ep_section = (
            f"**🏆 {current_ep_tier}**\n"
            f"🏅 Rank: **{ep_rank_display}** ⸱ `{ep:,} EP`\n"
            f"📋 Events Attended: **{events_attended}**\n\n"
            f"{ep_progress_line}"
        )
        embed.add_field(name="── 🎯 Event Points (EP) ──", value=ep_section, inline=False)
        
        # ── Status Bar (Verification + Badges) ──
        status_parts = [f"{ver_icon} {ver_label}{msl_tag}"]
        
        if badges:
            badge_display = " ".join(BADGE_ICONS.get(b, "🏷️") + f" *{b}*" for b in badges)
            status_parts.append(f"\n🎖️ {badge_display}")
        
        embed.add_field(
            name="── 🛡️ Status ──",
            value="\n".join(status_parts),
            inline=False
        )
        
        # ── Footer ──
        embed.set_footer(
            text=f"Requested by {inter.user.display_name}  ⸱  /profile",
            icon_url=inter.user.display_avatar.url
        )
        
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
        new_total = await xp_service.add_xp(user.id, amount, bypass_lock=True, bypass_verification=True)
        
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
        await inter.response.defer()
        
        old_xp = await xp_service.get_xp(user.id)
        await xp_service.set_currency(user.id, xp=amount)
        
        embed = discord.Embed(
            title="⚙️ XP Overridden",
            description=f"Successfully set {user.mention}'s XP to **{amount}**.",
            color=discord.Color.orange()
        )
        await inter.followup.send(embed=embed)
        
        # Trigger role sync (handles promotions, demotions, and resets)
        if inter.guild:
            await self._handle_level_change(
                inter.guild, user.id, old_xp, amount,
                interaction=inter
            )
    
    @xp_group.command(name="reset", description="Reset XP for one user or EVERYONE (Admin only)")
    @app_commands.describe(user="The user to reset (leave blank to reset EVERYONE)")
    @require_admin_auth()
    @app_commands.default_permissions(administrator=True)
    async def xp_reset(self, inter: discord.Interaction, user: discord.Member = None):
        """Reset XP data. Requires confirmation if global."""
        if user:
            from services.xp_service import xp_service
            await inter.response.defer()
            
            old_xp = await xp_service.get_xp(user.id)
            await xp_service.set_currency(user.id, xp=0)
            
            # Strip all tier roles
            if inter.guild:
                member = inter.guild.get_member(user.id)
                if member:
                    await self._strip_all_tier_roles(inter.guild, member)
            
            embed = discord.Embed(
                title="🔄 XP Reset",
                description=f"Successfully reset {user.mention}'s XP to **0**.\nAll XP tier roles have been stripped.",
                color=discord.Color.red()
            )
            await inter.followup.send(embed=embed)
        else:
            from services.database import db
            # Create confirmation view
            class ConfirmView(discord.ui.View):
                def __init__(self):
                    super().__init__(timeout=30)
                    self.confirmed = False
                
                @discord.ui.button(label="Confirm Global Reset", style=discord.ButtonStyle.danger)
                async def confirm(btn_self, button_inter: discord.Interaction, button: discord.ui.Button):
                    btn_self.confirmed = True
                    btn_self.stop()
                    
                    # Reset all XP
                    await db.execute("UPDATE users SET xp = 0")
                    
                    # Strip all XP tier roles from every member
                    import asyncio
                    xp_cog = button_inter.client.get_cog("Leveling")
                    if xp_cog and button_inter.guild:
                        tier_roles = await xp_cog._get_all_tier_roles(button_inter.guild)
                        all_role_objs = set(tier_roles.values())
                        for member in button_inter.guild.members:
                            roles_to_strip = [r for r in all_role_objs if r in member.roles]
                            if roles_to_strip:
                                try:
                                    await member.remove_roles(*roles_to_strip, reason="Global XP Reset")
                                    await asyncio.sleep(0.3)  # Rate limit protection
                                except discord.Forbidden:
                                    pass
                    
                    embed = discord.Embed(
                        title="🚨 GLOBAL XP RESET",
                        description="**ALL user XP has been wiped to 0.**\nAll XP tier roles have been stripped.",
                        color=discord.Color.dark_red()
                    )
                    await button_inter.response.edit_message(embed=embed, view=None)
                
                @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
                async def cancel(self, button_inter: discord.Interaction, button: discord.ui.Button):
                    self.stop()
                    await button_inter.response.edit_message(
                        content="❌ Global XP reset cancelled.",
                        embed=None,
                        view=None
                    )
            
            view = ConfirmView()
            embed = discord.Embed(
                title="⚠️ Confirm Global XP Reset",
                description="**This will reset EVERYONE'S XP to 0.**\n\nThis action cannot be undone!",
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
