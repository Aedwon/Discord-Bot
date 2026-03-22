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


class XpCog(commands.Cog, name="Leveling"):
    """XP and Leveling system for the server."""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        
        # Spam prevention caches
        self.gained_msg_xp = set()
        self.pending_xp = {}
        
        # Reaction XP tracking
        self.message_reaction_xp = {}
        self.user_reacted_to_message = set()
        self.daily_reaction_cache = {}
    
    async def cog_load(self):
        """Called when cog is loaded - start background task."""
        self.batch_update_db.start()
    
    def cog_unload(self):
        self.batch_update_db.cancel()
    
    # ─────────────────────────────────────────────────────────────────────
    # Background Task - Batch XP Updates
    # ─────────────────────────────────────────────────────────────────────
    
    @tasks.loop(seconds=BATCH_UPDATE_INTERVAL)
    async def batch_update_db(self):
        """Process pending XP and award voice XP."""
        self.gained_msg_xp.clear()
        
        # Clear large caches
        if len(self.message_reaction_xp) > 10000:
            self.message_reaction_xp.clear()
        if len(self.user_reacted_to_message) > 50000:
            self.user_reacted_to_message.clear()
        
        await self._process_voice_xp()
        
        if self.pending_xp:
            updates = await xp_service.batch_update(self.pending_xp.copy())
            self.pending_xp.clear()
            
            # Mathematical Auto-Leveling Role Assigner
            guild = self.bot.guilds[0] if self.bot.guilds else None
            if guild:
                for user_id, data in updates.items():
                    old_lvl = xp_service.get_level(data["old_xp"])
                    new_lvl = xp_service.get_level(data["new_xp"])
                    
                    if new_lvl > old_lvl:
                        old_tier = xp_service.get_tier_name(old_lvl)
                        new_tier = xp_service.get_tier_name(new_lvl)
                        
                        if new_tier and new_tier != old_tier:
                            r_id = await settings_service.get(f"xp_role_{new_tier.replace(' ', '_')}")
                            if r_id:
                                member = guild.get_member(user_id)
                                if member:
                                    role = guild.get_role(int(r_id))
                                    if role:
                                        try:
                                            # Strip the old outdated Tier if mapped
                                            if old_tier:
                                                o_id = await settings_service.get(f"xp_role_{old_tier.replace(' ', '_')}")
                                                if o_id:
                                                    o_role = guild.get_role(int(o_id))
                                                    if o_role: await member.remove_roles(o_role, reason="XP Tier Upgraded")
                                            # Directly embed the new correct tier natively
                                            await member.add_roles(role, reason="XP Auto-Leveling Tier Up")
                                        except discord.Forbidden:
                                            pass
    @batch_update_db.before_loop
    async def before_batch_update(self):
        await self.bot.wait_until_ready()
    
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
            
            valid_members = [
                m for m in vc.members
                if not m.bot
                and not m.voice.mute and not m.voice.self_mute
                and not m.voice.deaf and not m.voice.self_deaf
                and not m.voice.suppress
                and verification_service.is_verified(m.id)
            ]
            
            if len(valid_members) >= voice_config["min_members"]:
                for member in valid_members:
                    self.pending_xp[member.id] = (
                        self.pending_xp.get(member.id, 0) + 
                        voice_config["xp_per_cycle"]
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
        
        if len(message.content) < msg_config["min_length"]:
            return
        
        # Ignore bot channel
        bot_channel_id = await settings_service.get_int("bot_channel_id")
        if message.channel.id == bot_channel_id:
            return
        
        user_id = message.author.id
        
        if user_id not in self.gained_msg_xp:
            self.gained_msg_xp.add(user_id)
            xp_amount = randint(msg_config["min_xp"], msg_config["max_xp"])
            self.pending_xp[user_id] = self.pending_xp.get(user_id, 0) + xp_amount
    
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
    
    @app_commands.command(name="leaderboard", description="Show the server XP leaderboard")
    async def leaderboard(self, inter: discord.Interaction):
        """Show the server XP leaderboard."""
        top_users = await xp_service.get_leaderboard(limit=10)
        embed = create_leaderboard_embed(inter.guild, top_users)
        await inter.response.send_message(embed=embed)
    
    @app_commands.command(name="rank", description="Show your or another user's rank")
    async def rank(self, inter: discord.Interaction, member: discord.Member = None):
        """Show your or another user's rank."""
        target = member or inter.user
        rank, xp = await xp_service.get_rank(target.id)
        embed = create_rank_embed(target, rank, xp)
        await inter.response.send_message(embed=embed)
    
    # ─────────────────────────────────────────────────────────────────────
    # Admin Commands - XP System Control
    # ─────────────────────────────────────────────────────────────────────
    
    @app_commands.command(name="xp-start", description="Start the XP system (enable XP gain)")
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
    
    @app_commands.command(name="xp-stop", description="Stop the XP system (disable XP gain)")
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

    @app_commands.command(name="xp-add", description="Add XP to a specific user (Admin only)")
    @app_commands.describe(user="The user to grant XP to", amount="Amount of XP to add")
    @app_commands.default_permissions(administrator=True)
    async def xp_add(self, inter: discord.Interaction, user: discord.Member, amount: int):
        from services.xp_service import xp_service
        new_total = await xp_service.add_xp(user.id, amount)
        embed = discord.Embed(
            title="✨ XP Granted",
            description=f"Successfully added {amount} XP to {user.mention}.\nNew Total: **{new_total} XP**",
            color=discord.Color.green()
        )
        await inter.response.send_message(embed=embed)
        
    @app_commands.command(name="xp-set", description="Set a specific user's XP (Admin only)")
    @app_commands.describe(user="The user to modify", amount="Exact XP amount to set")
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
    
    @app_commands.command(name="xp-reset", description="Reset all user XP to zero")
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
    
    @app_commands.command(name="xp-status", description="Check if the XP system is running")
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
