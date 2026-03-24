"""
Moderation Cog - Visual moderation with slash commands and audit logging.
Includes /history, /warn (XP lock), /mute, /restrict, /ban (economy wipe).
"""

import discord
from datetime import datetime, timedelta
from discord.ext import commands
from discord import app_commands

from services.mod_service import mod_service
from services.database import db
from services.settings_service import settings_service
from utils.checks import require_admin_auth


class ModCog(commands.Cog, name="Moderation"):
    """Visual moderation system with full audit logging."""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
    
    # ─────────────────────────────────────────────────────────────────────
    # Helper Methods
    # ─────────────────────────────────────────────────────────────────────
    
    async def _notify_user(self, user: discord.Member, action: str, reason: str, guild_name: str):
        """DM the user about the moderation action."""
        try:
            embed = discord.Embed(
                title=f"You have been {action}",
                description=f"**Server:** {guild_name}\n**Reason:** {reason or 'No reason provided'}",
                color=discord.Color.red()
            )
            await user.send(embed=embed)
            return True
        except discord.Forbidden:
            return False
    
    async def _log_to_channel(self, guild: discord.Guild, embed: discord.Embed):
        """Log action to the mod log channel."""
        channel_id = await settings_service.get_int("mod_log_channel_id")
        if channel_id:
            channel = guild.get_channel(channel_id)
            if channel:
                try:
                    await channel.send(embed=embed)
                except discord.Forbidden:
                    pass
    
    def _parse_duration(self, duration_str: str) -> timedelta | None:
        """Parse duration string: 1m, 1h, 1d, 1w, perm"""
        if not duration_str or duration_str.lower() == "perm":
            return None
        
        try:
            unit = duration_str[-1].lower()
            amount = int(duration_str[:-1])
            
            if unit == 'm':
                return timedelta(minutes=amount)
            elif unit == 'h':
                return timedelta(hours=amount)
            elif unit == 'd':
                return timedelta(days=amount)
            elif unit == 'w':
                return timedelta(weeks=amount)
        except (ValueError, IndexError):
            return None
        return None
    
    def _get_action_icon(self, action: str) -> str:
        """Get visual icon for action type."""
        icons = {
            "warn": "⚠️",
            "mute": "🔇",
            "unmute": "🔊",
            "ban": "🚫",
            "unban": "✅",
            "kick": "👢",
            "restrict": "🔒",
            "unrestrict": "🔓",
        }
        return icons.get(action.lower(), "📋")
    
    def _get_action_color(self, action: str) -> discord.Color:
        """Get color for action type."""
        colors = {
            "warn": discord.Color.yellow(),
            "mute": discord.Color.red(),
            "unmute": discord.Color.green(),
            "ban": discord.Color.dark_red(),
            "unban": discord.Color.green(),
            "kick": discord.Color.orange(),
            "restrict": discord.Color.orange(),
            "unrestrict": discord.Color.green(),
        }
        return colors.get(action.lower(), discord.Color.greyple())
    
    async def _check_hierarchy(self, inter: discord.Interaction, target: discord.Member) -> bool:
        """Check if action is allowed based on role hierarchy."""
        if target.top_role >= inter.user.top_role:
            await inter.response.send_message("❌ Cannot target someone with equal or higher role.", ephemeral=True)
            return False
        if target.top_role >= inter.guild.me.top_role:
            await inter.response.send_message("❌ Target's role is higher than mine.", ephemeral=True)
            return False
        return True
    
    async def _add_role_with_verification(
        self, 
        guild: discord.Guild, 
        user_id: int, 
        role: discord.Role, 
        reason: str = None,
        max_retries: int = 2
    ) -> tuple[bool, str]:
        """
        Add a role to a member with verification.
        Returns (success: bool, error_message: str or None)
        
        Handles:
        - Member not found in guild
        - Role already assigned
        - Permission errors
        - Verification that role was actually applied
        - Retry logic for transient failures
        """
        for attempt in range(max_retries):
            try:
                # Fetch fresh member data
                try:
                    member = await guild.fetch_member(user_id)
                except discord.NotFound:
                    return False, "Member not found in the server."
                except discord.HTTPException as e:
                    return False, f"Failed to fetch member: {e}"
                
                # Check if role already assigned
                if role in member.roles:
                    return False, f"{member.mention} already has this role."
                
                # Check hierarchy before attempting
                if role.position >= guild.me.top_role.position:
                    return False, (
                        f"Cannot assign role: **{role.name}** (position {role.position}) "
                        f"is at or above my highest role (position {guild.me.top_role.position})."
                    )
                
                # Attempt to add the role
                await member.add_roles(role, reason=reason)
                
                # Small delay to allow Discord API to propagate
                import asyncio
                await asyncio.sleep(0.5)
                
                # Re-fetch and verify
                try:
                    member = await guild.fetch_member(user_id)
                except discord.NotFound:
                    return False, "Member left the server during role assignment."
                
                if role in member.roles:
                    return True, None  # Success!
                
                # Role not applied, retry if attempts remain
                if attempt < max_retries - 1:
                    continue
                
                return False, (
                    f"Role assignment completed but role was not applied.\n"
                    f"• Bot role position: {guild.me.top_role.position}\n"
                    f"• Target role position: {role.position}\n"
                    f"Please ensure the bot's role is above the target role."
                )
                
            except discord.Forbidden as e:
                # Gather detailed debug info
                bot_perms = guild.me.guild_permissions
                is_owner = member.id == guild.owner_id if guild.owner_id else False
                return False, (
                    f"Missing Permissions.\n"
                    f"• Bot role position: {guild.me.top_role.position}\n"
                    f"• Target role position: {role.position}\n"
                    f"• Member's top role position: {member.top_role.position}\n"
                    f"• Member is server owner: {is_owner}\n"
                    f"• Role managed by integration: {role.managed}\n"
                    f"• Bot has Manage Roles: {bot_perms.manage_roles}\n"
                    f"• Bot has Admin: {bot_perms.administrator}\n"
                    f"• Error: {e}"
                )
            except discord.HTTPException as e:
                if attempt < max_retries - 1:
                    continue
                return False, f"Discord API error: {e}"
        
        return False, "Failed after multiple attempts."
    
    async def _remove_role_with_verification(
        self,
        guild: discord.Guild,
        user_id: int,
        role: discord.Role,
        reason: str = None,
        max_retries: int = 2
    ) -> tuple[bool, str]:
        """
        Remove a role from a member with verification.
        Returns (success: bool, error_message: str or None)
        
        Handles:
        - Member not found in guild
        - Role not currently assigned
        - Permission errors
        - Verification that role was actually removed
        - Retry logic for transient failures
        """
        for attempt in range(max_retries):
            try:
                # Fetch fresh member data
                try:
                    member = await guild.fetch_member(user_id)
                except discord.NotFound:
                    return False, "Member not found in the server."
                except discord.HTTPException as e:
                    return False, f"Failed to fetch member: {e}"
                
                # Check if role is assigned
                if role not in member.roles:
                    return False, f"{member.mention} doesn't have this role."
                
                # Check hierarchy before attempting
                if role.position >= guild.me.top_role.position:
                    return False, (
                        f"Cannot remove role: **{role.name}** (position {role.position}) "
                        f"is at or above my highest role (position {guild.me.top_role.position})."
                    )
                
                # Attempt to remove the role
                await member.remove_roles(role, reason=reason)
                
                # Small delay to allow Discord API to propagate
                import asyncio
                await asyncio.sleep(0.5)
                
                # Re-fetch and verify
                try:
                    member = await guild.fetch_member(user_id)
                except discord.NotFound:
                    # Member left, but role removal was likely successful
                    return True, None
                
                if role not in member.roles:
                    return True, None  # Success!
                
                # Role still present, retry if attempts remain
                if attempt < max_retries - 1:
                    continue
                
                return False, (
                    f"Role removal completed but role is still present.\n"
                    f"• Bot role position: {guild.me.top_role.position}\n"
                    f"• Target role position: {role.position}\n"
                    f"Please ensure the bot's role is above the target role."
                )
                
            except discord.Forbidden as e:
                return False, (
                    f"Missing Permissions.\n"
                    f"• Bot role position: {guild.me.top_role.position}\n"
                    f"• Target role position: {role.position}\n"
                    f"• Error: {e}"
                )
            except discord.HTTPException as e:
                if attempt < max_retries - 1:
                    continue
                return False, f"Discord API error: {e}"
        
        return False, "Failed after multiple attempts."
    
    async def _apply_xp_lock(self, user_id: int, hours: int = 24):
        """Apply XP/Token lock to user."""
        lock_until = datetime.now() + timedelta(hours=hours)
        await db.execute('''
            INSERT INTO users (user_id, xp_locked, xp_lock_until) VALUES (%s, 1, %s)
            ON DUPLICATE KEY UPDATE xp_locked = 1, xp_lock_until = %s
        ''', (user_id, lock_until.isoformat(), lock_until.isoformat()))
    
    async def _wipe_economy(self, user_id: int):
        """Wipe all economy data for a user (perm ban)."""
        await db.execute('''
            UPDATE users SET 
                xp = 0, tokens = 0, xp_multiplier = 1.0, token_multiplier = 1.0,
                shop_discount = 0.0, raffle_entries = 0, pouches_today = 0
            WHERE user_id = %s
        ''', (user_id,))
    
    # ─────────────────────────────────────────────────────────────────────
    # Auto-Role Configuration
    # ─────────────────────────────────────────────────────────────────────
    
    AUTO_ROLE_ID = 1465984054049374218  # Role assigned to new members
    
    # ─────────────────────────────────────────────────────────────────────
    # Event Listeners
    # ─────────────────────────────────────────────────────────────────────
    
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Assign auto-role to new members and re-grant Verified role if applicable."""
        if member.bot:
            return
        
        role = member.guild.get_role(self.AUTO_ROLE_ID)
        if not role:
            print(f"[ModCog] Auto-role {self.AUTO_ROLE_ID} not found in guild")
            return
        
        try:
            await member.add_roles(role, reason="Auto-role on join")
            print(f"[ModCog] Assigned auto-role to {member.display_name}")
        except discord.Forbidden as e:
            print(f"[ModCog] Failed to assign auto-role to {member.display_name}: {e}")
        except discord.HTTPException as e:
            print(f"[ModCog] HTTP error assigning auto-role: {e}")

        # Re-grant Verified role if user was previously verified
        try:
            from services.verification_service import verification_service
            from services.settings_service import settings_service
            if verification_service.is_verified(member.id):
                verified_role_id = await settings_service.get_int("verified_role_id")
                if verified_role_id:
                    v_role = member.guild.get_role(verified_role_id)
                    if v_role:
                        await member.add_roles(v_role, reason="Re-grant: previously verified")
                        print(f"[ModCog] Re-granted Verified role to {member.display_name}")
        except Exception as e:
            print(f"[ModCog] Error re-granting Verified role: {e}")
    
    # ─────────────────────────────────────────────────────────────────────
    # Slash Commands
    # ─────────────────────────────────────────────────────────────────────
    
    @app_commands.command(name="history", description="View a user's moderation history (Rap Sheet)")
    @app_commands.default_permissions(moderate_members=True)
    async def history(self, inter: discord.Interaction, user: discord.Member):
        """Display visual moderation history with pagination."""
        # Fetch more history for pagination
        all_history = await mod_service.get_user_history(user.id, limit=50)
        
        # Count infractions by type
        action_counts = {}
        for entry in all_history:
            action = entry['action_type']
            action_counts[action] = action_counts.get(action, 0) + 1
        
        # ─────────────────────────────────────────────────────────────────
        # Pagination Setup
        # ─────────────────────────────────────────────────────────────────
        
        ITEMS_PER_PAGE = 5  # Actions per page to stay within embed limits
        total_pages = max(1, (len(all_history) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
        
        async def create_history_embed(page: int = 0) -> discord.Embed:
            """Create the embed for a specific page."""
            # Determine embed color based on severity
            if not all_history:
                color = discord.Color.green()
            elif len(all_history) >= 5:
                color = discord.Color.red()
            elif len(all_history) >= 2:
                color = discord.Color.orange()
            else:
                color = discord.Color.yellow()
            
            embed = discord.Embed(
                title="📋 Moderation History",
                color=color
            )
            
            # User info header
            account_age = (discord.utils.utcnow() - user.created_at).days
            joined_days = (discord.utils.utcnow() - user.joined_at).days if user.joined_at else 0
            
            embed.set_author(
                name=f"{user.display_name}",
                icon_url=user.display_avatar.url
            )
            embed.set_thumbnail(url=user.display_avatar.url)
            
            # Summary section (compact)
            summary_text = (
                f"**ID:** `{user.id}` • "
                f"**Account:** {account_age}d • "
                f"**Member:** {joined_days}d"
            )
            embed.description = summary_text
            
            # Infraction summary
            if action_counts:
                stats_parts = [f"{self._get_action_icon(k)} {k.title()}: **{v}**" for k, v in action_counts.items()]
                embed.add_field(name="📊 Infractions", value=" • ".join(stats_parts), inline=False)
            
            # Current status
            status_parts = []
            if user.is_timed_out():
                timeout_until = user.timed_out_until
                status_parts.append(f"🔇 Timeout <t:{int(timeout_until.timestamp())}:R>")
            
            result = await db.fetch_one('SELECT xp_locked, xp_lock_until, is_restricted FROM users WHERE user_id = %s', (user.id,))
            if result:
                if result['xp_locked']:
                    if result.get('xp_lock_until'):
                        status_parts.append(f"⛔ XP Lock <t:{int(result['xp_lock_until'].timestamp())}:R>")
                    else:
                        status_parts.append("⛔ XP Locked")
                if result['is_restricted']:
                    status_parts.append("🔒 Restricted")
            
            if status_parts:
                embed.add_field(name="⚠️ Active Penalties", value=" • ".join(status_parts), inline=False)
            
            # Moderation history entries
            if not all_history:
                embed.add_field(
                    name="📜 Recent Actions",
                    value="✨ Clean record — no moderation actions on file.",
                    inline=False
                )
            else:
                # Get entries for current page
                start_idx = page * ITEMS_PER_PAGE
                end_idx = start_idx + ITEMS_PER_PAGE
                page_entries = all_history[start_idx:end_idx]
                
                # Build action list as a single field (vertical)
                action_lines = []
                for entry in page_entries:
                    icon = self._get_action_icon(entry['action_type'])
                    action = entry['action_type'].upper()
                    mod_id = entry['moderator_id']
                    reason = entry['reason'] or "No reason"
                    
                    # Format timestamp
                    if entry['timestamp']:
                        ts = int(entry['timestamp'].timestamp())
                        time_str = f"<t:{ts}:d>"  # Short date format
                    else:
                        time_str = "?"
                    
                    # Truncate reason
                    if len(reason) > 60:
                        reason = reason[:57] + "..."
                    
                    action_lines.append(
                        f"{icon} **{action}** — {time_str}\n"
                        f"   ↳ By <@{mod_id}>: {reason}"
                    )
                
                embed.add_field(
                    name="📜 Recent Actions",
                    value="\n\n".join(action_lines),
                    inline=False
                )
            
            # Footer with pagination info
            if total_pages > 1:
                embed.set_footer(text=f"Page {page + 1}/{total_pages} • {len(all_history)} total action(s)")
            else:
                embed.set_footer(text=f"{len(all_history)} action(s) on record")
            
            return embed
        
        # ─────────────────────────────────────────────────────────────────
        # Pagination View
        # ─────────────────────────────────────────────────────────────────
        
        if total_pages <= 1:
            # No pagination needed
            embed = await create_history_embed(0)
            await inter.response.send_message(embed=embed)
            return
        
        # Create pagination view
        class HistoryPaginator(discord.ui.View):
            def __init__(self, author_id: int):
                super().__init__(timeout=120)
                self.current_page = 0
                self.author_id = author_id
            
            async def interaction_check(self, interaction: discord.Interaction) -> bool:
                if interaction.user.id != self.author_id:
                    await interaction.response.send_message(
                        "❌ Only the command user can navigate pages.",
                        ephemeral=True
                    )
                    return False
                return True
            
            def update_buttons(self):
                self.prev_btn.disabled = self.current_page == 0
                self.next_btn.disabled = self.current_page >= total_pages - 1
            
            @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
            async def prev_btn(self, button_inter: discord.Interaction, button: discord.ui.Button):
                self.current_page = max(0, self.current_page - 1)
                self.update_buttons()
                embed = await create_history_embed(self.current_page)
                await button_inter.response.edit_message(embed=embed, view=self)
            
            @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
            async def next_btn(self, button_inter: discord.Interaction, button: discord.ui.Button):
                self.current_page = min(total_pages - 1, self.current_page + 1)
                self.update_buttons()
                embed = await create_history_embed(self.current_page)
                await button_inter.response.edit_message(embed=embed, view=self)
            
            async def on_timeout(self):
                # Disable buttons on timeout
                for item in self.children:
                    item.disabled = True
        
        view = HistoryPaginator(inter.user.id)
        view.update_buttons()
        embed = await create_history_embed(0)
        await inter.response.send_message(embed=embed, view=view)
    
    @app_commands.command(name="warn", description="Issue a formal warning (applies 24h XP lock)")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(
        user="The member to warn",
        reason="Reason for the warning (required)"
    )
    async def warn(self, inter: discord.Interaction, user: discord.Member, reason: str):
        """Warn user with 24h XP/Token lock."""
        # Log action
        await mod_service.log_action("warn", inter.user.id, user.id, reason)
        
        # Apply 24h XP lock
        await self._apply_xp_lock(user.id, 24)
        
        # DM user
        dm_sent = await self._notify_user(user, "warned", reason, inter.guild.name)
        
        # Get warning count
        warn_count = await mod_service.get_action_count(user.id, "warn")
        
        embed = discord.Embed(
            title="⚠️ Warning Issued",
            color=discord.Color.yellow()
        )
        embed.add_field(name="User", value=f"{user.mention}", inline=True)
        embed.add_field(name="Moderator", value=f"{inter.user.mention}", inline=True)
        embed.add_field(name="Total Warnings", value=str(warn_count), inline=True)
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Penalty", value="24h XP/Token Lock Applied", inline=False)
        embed.set_footer(text="DM sent" if dm_sent else "DM failed (user has DMs closed)")
        
        await inter.response.send_message(embed=embed)
        await self._log_to_channel(inter.guild, embed)
    
    @app_commands.command(name="autorole", description="Assign auto-role to all members who don't have it")
    @require_admin_auth()
    @app_commands.default_permissions(administrator=True)
    async def assign_autorole(self, inter: discord.Interaction):
        """
        Bulk-assign the auto-role to existing members without it.
        
        Features:
        - Concurrent batching for ~5x speed improvement
        - Dynamic progress bar updates
        - Graceful error handling with detailed reporting
        - Rate limit aware with automatic throttling
        """
        import asyncio
        import time
        
        await inter.response.defer(ephemeral=True)
        
        # ─────────────────────────────────────────────────────────────────
        # Validation
        # ─────────────────────────────────────────────────────────────────
        
        role = inter.guild.get_role(self.AUTO_ROLE_ID)
        if not role:
            return await inter.followup.send(
                f"❌ Auto-role with ID `{self.AUTO_ROLE_ID}` not found.\n"
                f"Please verify the role exists and update `AUTO_ROLE_ID` in the code.",
                ephemeral=True
            )
        
        # Check bot permissions
        bot_member = inter.guild.me
        if not bot_member.guild_permissions.manage_roles:
            return await inter.followup.send(
                "❌ I don't have the **Manage Roles** permission.",
                ephemeral=True
            )
        
        # Check role hierarchy
        if role.position >= bot_member.top_role.position:
            return await inter.followup.send(
                f"❌ The role {role.mention} is higher than or equal to my highest role.\n"
                f"Please move my role above it in Server Settings → Roles.",
                ephemeral=True
            )
        
        # ─────────────────────────────────────────────────────────────────
        # Find members needing the role
        # ─────────────────────────────────────────────────────────────────
        
        members_without_role = [
            m for m in inter.guild.members 
            if not m.bot and role not in m.roles
        ]
        
        if not members_without_role:
            embed = discord.Embed(
                title="✅ All Set!",
                description=f"All members already have the {role.mention} role.",
                color=discord.Color.green()
            )
            return await inter.followup.send(embed=embed, ephemeral=True)
        
        total = len(members_without_role)
        
        # ─────────────────────────────────────────────────────────────────
        # Progress tracking
        # ─────────────────────────────────────────────────────────────────
        
        def create_progress_embed(processed: int, success: int, failed: int, skipped: int, elapsed: float) -> discord.Embed:
            """Create a progress embed with visual bar."""
            progress = processed / total if total > 0 else 1
            bar_length = 20
            filled = int(bar_length * progress)
            bar = "█" * filled + "░" * (bar_length - filled)
            
            # Estimate time remaining
            if processed > 0 and elapsed > 0:
                rate = processed / elapsed
                remaining = (total - processed) / rate if rate > 0 else 0
                eta = f"{remaining:.0f}s" if remaining < 60 else f"{remaining/60:.1f}m"
            else:
                eta = "calculating..."
            
            embed = discord.Embed(
                title="🔄 Assigning Roles...",
                description=f"`{bar}` {progress*100:.1f}%\n\n"
                           f"**Progress:** {processed}/{total} members",
                color=discord.Color.blue()
            )
            embed.add_field(name="✅ Assigned", value=str(success), inline=True)
            embed.add_field(name="❌ Failed", value=str(failed), inline=True)
            embed.add_field(name="⏭️ Skipped", value=str(skipped), inline=True)
            embed.add_field(name="⏱️ ETA", value=eta, inline=True)
            embed.add_field(name="⚡ Rate", value=f"{rate:.1f}/s" if processed > 0 else "—", inline=True)
            
            return embed
        
        def create_final_embed(success: int, failed: int, skipped: int, elapsed: float) -> discord.Embed:
            """Create the final results embed."""
            color = discord.Color.green() if failed == 0 else discord.Color.orange()
            
            embed = discord.Embed(
                title="✅ Auto-Role Assignment Complete",
                description=f"Finished assigning {role.mention} to members.",
                color=color
            )
            embed.add_field(name="✅ Assigned", value=str(success), inline=True)
            embed.add_field(name="❌ Failed", value=str(failed), inline=True)
            embed.add_field(name="⏭️ Skipped", value=str(skipped), inline=True)
            embed.add_field(name="⏱️ Duration", value=f"{elapsed:.1f}s", inline=True)
            
            if failed > 0:
                embed.add_field(
                    name="⚠️ Note",
                    value="Some members couldn't be assigned the role. This may be due to:\n"
                          "• Members with higher roles than the bot\n"
                          "• Server owner (cannot have roles modified)\n"
                          "• Rate limiting (try again later)",
                    inline=False
                )
            
            return embed
        
        # ─────────────────────────────────────────────────────────────────
        # Batch processing with concurrency
        # ─────────────────────────────────────────────────────────────────
        
        BATCH_SIZE = 5  # Concurrent operations per batch
        DELAY_BETWEEN_BATCHES = 0.6  # Seconds between batches (safe rate)
        UPDATE_INTERVAL = 15  # Update progress every N members
        
        success_count = 0
        fail_count = 0
        skip_count = 0
        processed_count = 0
        start_time = time.time()
        last_update_time = 0
        
        # Send initial progress message
        progress_msg = await inter.followup.send(
            embed=create_progress_embed(0, 0, 0, 0, 0),
            ephemeral=True,
            wait=True
        )
        
        async def assign_role_to_member(member: discord.Member) -> str:
            """Assign role to a single member. Returns 'success', 'failed', or 'skipped'."""
            try:
                # Skip if member now has the role (race condition check)
                if role in member.roles:
                    return 'skipped'
                
                # Skip server owner (can't modify their roles)
                if member.id == inter.guild.owner_id:
                    return 'skipped'
                
                # Skip if member's top role is higher than bot's
                if member.top_role.position >= bot_member.top_role.position:
                    return 'skipped'
                
                await member.add_roles(role, reason="Bulk auto-role assignment")
                return 'success'
                
            except discord.Forbidden:
                return 'failed'
            except discord.HTTPException as e:
                # Rate limit - wait and retry once
                if e.status == 429:
                    await asyncio.sleep(float(e.response.headers.get('Retry-After', 5)))
                    try:
                        await member.add_roles(role, reason="Bulk auto-role assignment (retry)")
                        return 'success'
                    except:
                        return 'failed'
                return 'failed'
            except Exception:
                return 'failed'
        
        # Process in batches
        for i in range(0, total, BATCH_SIZE):
            batch = members_without_role[i:i + BATCH_SIZE]
            
            # Process batch concurrently
            results = await asyncio.gather(*[assign_role_to_member(m) for m in batch])
            
            # Count results
            for result in results:
                processed_count += 1
                if result == 'success':
                    success_count += 1
                elif result == 'failed':
                    fail_count += 1
                else:
                    skip_count += 1
            
            # Update progress bar periodically
            elapsed = time.time() - start_time
            if processed_count % UPDATE_INTERVAL == 0 or processed_count == total:
                # Throttle message edits (max 1 per second)
                if elapsed - last_update_time >= 1.0:
                    try:
                        await progress_msg.edit(
                            embed=create_progress_embed(processed_count, success_count, fail_count, skip_count, elapsed)
                        )
                        last_update_time = elapsed
                    except discord.HTTPException:
                        pass  # Message edit failed, continue anyway
            
            # Delay between batches to respect rate limits
            if i + BATCH_SIZE < total:
                await asyncio.sleep(DELAY_BETWEEN_BATCHES)
        
        # ─────────────────────────────────────────────────────────────────
        # Final report
        # ─────────────────────────────────────────────────────────────────
        
        elapsed = time.time() - start_time
        try:
            await progress_msg.edit(
                embed=create_final_embed(success_count, fail_count, skip_count, elapsed)
            )
        except discord.HTTPException:
            # If edit fails, send a new message
            await inter.followup.send(
                embed=create_final_embed(success_count, fail_count, skip_count, elapsed),
                ephemeral=True
            )
    
    # ─────────────────────────────────────────────────────────────────────
    # DIAGNOSTIC COMMAND - Remove after debugging
    # ─────────────────────────────────────────────────────────────────────
    
    @app_commands.command(name="testrole", description="[DEBUG] Test role assignment with detailed checks")
    @require_admin_auth()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        user="The member to assign the role to",
        role="The role to assign"
    )
    async def testrole(self, inter: discord.Interaction, user: discord.Member, role: discord.Role):
        """Diagnostic command to test role assignment with granular checks."""
        await inter.response.defer(ephemeral=True)
        
        guild = inter.guild
        bot_member = guild.me
        
        # ─────────────────────────────────────────────────────────────────
        # Gather all diagnostic info
        # ─────────────────────────────────────────────────────────────────
        
        checks = []
        
        # 1. Bot's role info
        bot_roles = [r.name for r in bot_member.roles if r.name != "@everyone"]
        checks.append(f"**Bot's roles:** {', '.join(bot_roles) or 'None'}")
        checks.append(f"**Bot's top role:** {bot_member.top_role.name} (position {bot_member.top_role.position})")
        
        # 2. Target role info
        checks.append(f"**Target role:** {role.name} (position {role.position})")
        checks.append(f"**Role managed:** {role.managed}")
        checks.append(f"**Role is bot role:** {role.is_bot_managed()}")
        checks.append(f"**Role is integration:** {role.is_integration()}")
        checks.append(f"**Role is premium:** {role.is_premium_subscriber()}")
        
        # 3. Target user info
        checks.append(f"**Target user:** {user.display_name}")
        checks.append(f"**User's top role:** {user.top_role.name} (position {user.top_role.position})")
        checks.append(f"**User is owner:** {user.id == guild.owner_id}")
        checks.append(f"**User already has role:** {role in user.roles}")
        
        # 4. Bot permissions (detailed)
        perms = bot_member.guild_permissions
        checks.append(f"**Bot permissions:**")
        checks.append(f"  • Administrator: {perms.administrator}")
        checks.append(f"  • Manage Roles: {perms.manage_roles}")
        checks.append(f"  • Moderate Members: {perms.moderate_members}")
        checks.append(f"  • Manage Guild: {perms.manage_guild}")
        checks.append(f"  • Raw value: {perms.value}")
        
        # 5. Hierarchy checks
        can_manage = bot_member.top_role.position > role.position
        checks.append(f"**Hierarchy check (bot > role):** {'✅ PASS' if can_manage else '❌ FAIL'}")
        
        can_target = bot_member.top_role.position > user.top_role.position
        checks.append(f"**Hierarchy check (bot > user):** {'✅ PASS' if can_target else '❌ FAIL'}")
        
        # 6. Bot's role details
        checks.append(f"\n**Bot role permissions breakdown:**")
        for r in bot_member.roles:
            if r.name == "@everyone":
                continue
            checks.append(f"  • {r.name} (pos {r.position}): Admin={r.permissions.administrator}, ManageRoles={r.permissions.manage_roles}")
        
        # ─────────────────────────────────────────────────────────────────
        # Attempt the role assignment
        # ─────────────────────────────────────────────────────────────────
        
        checks.append(f"\n**Attempting role assignment...**")
        
        if role in user.roles:
            checks.append(f"⚠️ User already has role, skipping add.")
            result = "SKIPPED"
        else:
            try:
                await user.add_roles(role, reason=f"Test by {inter.user}")
                
                # Verify
                import asyncio
                await asyncio.sleep(0.5)
                refreshed = await guild.fetch_member(user.id)
                
                if role in refreshed.roles:
                    checks.append(f"✅ **SUCCESS!** Role was assigned.")
                    result = "SUCCESS"
                    # Remove the role since this was just a test
                    await refreshed.remove_roles(role, reason="Test cleanup")
                    checks.append(f"🧹 Cleaned up test role.")
                else:
                    checks.append(f"⚠️ No error thrown but role not found on user.")
                    result = "VERIFICATION_FAILED"
                    
            except discord.Forbidden as e:
                checks.append(f"❌ **FORBIDDEN:** {e}")
                checks.append(f"   Error code: {e.code}")
                checks.append(f"   Error text: {e.text}")
                
                # Try raw HTTP to get more info
                checks.append(f"\n**Trying raw HTTP request...**")
                try:
                    http = self.bot.http
                    # Get current roles and add the new one
                    current_role_ids = [r.id for r in user.roles]
                    new_roles = current_role_ids + [role.id]
                    
                    await http.edit_member(
                        guild.id,
                        user.id,
                        reason=f"Raw HTTP test by {inter.user}",
                        roles=new_roles
                    )
                    checks.append(f"✅ Raw HTTP worked! Library issue?")
                except Exception as http_e:
                    checks.append(f"❌ Raw HTTP also failed: {type(http_e).__name__}: {http_e}")
                    if hasattr(http_e, 'response'):
                        checks.append(f"   Response: {http_e.response}")
                
                result = "FORBIDDEN"
            except discord.HTTPException as e:
                checks.append(f"❌ **HTTP ERROR:** {e}")
                checks.append(f"   Status: {e.status}")
                checks.append(f"   Code: {e.code}")
                result = "HTTP_ERROR"
            except Exception as e:
                checks.append(f"❌ **ERROR:** {type(e).__name__}: {e}")
                result = "ERROR"
        
        # ─────────────────────────────────────────────────────────────────
        # Send results
        # ─────────────────────────────────────────────────────────────────
        
        # Split into multiple embeds if needed
        output = "\n".join(checks)
        
        embed = discord.Embed(
            title=f"🔧 Role Test: {result}",
            description=output[:4000],  # Discord limit
            color=discord.Color.green() if result == "SUCCESS" else discord.Color.red()
        )
        
        await inter.followup.send(embed=embed, ephemeral=True)
    
    @app_commands.command(name="mute", description="Mute a user (assigns Muted role)")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(
        user="The member to mute",
        duration="Duration (e.g., 1m, 1h, 1d, 1w)",
        reason="Optional reason for the mute"
    )
    async def mute(self, inter: discord.Interaction, user: discord.Member, duration: str, reason: str = None):
        """Mute user by assigning Muted role."""
        if not await self._check_hierarchy(inter, user):
            return
        
        # Validate duration format (for logging purposes)
        parsed = self._parse_duration(duration)
        if not parsed:
            return await inter.response.send_message("❌ Invalid duration. Use: `1m`, `1h`, `1d`, `1w`", ephemeral=True)
        
        # Get Muted role
        muted_role_id = await settings_service.get_int("muted_role_id")
        if not muted_role_id:
            return await inter.response.send_message(
                "❌ Muted role not configured. Use `/setup role muted @Muted`",
                ephemeral=True
            )
        
        muted_role = inter.guild.get_role(muted_role_id)
        if not muted_role:
            return await inter.response.send_message("❌ Muted role not found.", ephemeral=True)
        
        # Check if already muted
        if muted_role in user.roles:
            return await inter.response.send_message(f"❌ {user.mention} is already muted.", ephemeral=True)
        
        # Apply Muted role with verification
        success, error = await self._add_role_with_verification(
            guild=inter.guild,
            user_id=user.id,
            role=muted_role,
            reason=f"Muted by {inter.user}: {reason}"
        )
        
        if not success:
            return await inter.response.send_message(f"❌ {error}", ephemeral=True)
        
        # DM and log
        await self._notify_user(user, f"muted for {duration}", reason, inter.guild.name)
        await mod_service.log_action("mute", inter.user.id, user.id, f"{duration}: {reason or 'No reason'}")
        
        embed = discord.Embed(title="🔇 User Muted", color=discord.Color.red())
        embed.add_field(name="User", value=user.mention, inline=True)
        embed.add_field(name="Duration", value=duration, inline=True)
        embed.add_field(name="Moderator", value=inter.user.mention, inline=True)
        if reason:
            embed.add_field(name="Reason", value=reason, inline=False)
        embed.set_footer(text="Role-based mute applied")
        
        await inter.response.send_message(embed=embed)
        await self._log_to_channel(inter.guild, embed)
    
    @app_commands.command(name="restrict", description="Restrict user (block images/embeds)")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(
        user="The member to restrict",
        duration="Duration (e.g., 1h, 1d, 1w)",
        reason="Optional reason for the restriction"
    )
    async def restrict(self, inter: discord.Interaction, user: discord.Member, duration: str, reason: str = None):
        """Restrict user - adds restricted role."""
        if not await self._check_hierarchy(inter, user):
            return
        
        # Mark as restricted in DB
        await db.execute('''
            INSERT INTO users (user_id, is_restricted) VALUES (%s, 1)
            ON DUPLICATE KEY UPDATE is_restricted = 1
        ''', (user.id,))
        
        # Add Restricted role (blocks Attach Files & Embed Links)
        restricted_role_id = await settings_service.get_int("restricted_role_id")
        if not restricted_role_id:
            return await inter.response.send_message(
                "❌ Restricted role not configured. Use `/setup role restricted @Restricted`",
                ephemeral=True
            )
        
        restricted_role = inter.guild.get_role(restricted_role_id)
        if not restricted_role:
            return await inter.response.send_message("❌ Restricted role not found.", ephemeral=True)
        
        # Apply Restricted role with verification
        success, error = await self._add_role_with_verification(
            guild=inter.guild,
            user_id=user.id,
            role=restricted_role,
            reason=f"Restricted by {inter.user}: {reason}"
        )
        
        if not success:
            # Rollback DB change on failure
            await db.execute('UPDATE users SET is_restricted = 0 WHERE user_id = %s', (user.id,))
            return await inter.response.send_message(f"❌ {error}", ephemeral=True)
        
        await mod_service.log_action("restrict", inter.user.id, user.id, f"{duration}: {reason or 'No reason'}")
        await self._notify_user(user, "restricted", reason, inter.guild.name)
        
        embed = discord.Embed(title="🔒 User Restricted", color=discord.Color.orange())
        embed.add_field(name="User", value=user.mention, inline=True)
        embed.add_field(name="Duration", value=duration, inline=True)
        embed.add_field(name="Moderator", value=inter.user.mention, inline=True)
        embed.add_field(name="Effects", value="• Restricted role applied\n• Cannot send images/embeds", inline=False)
        if reason:
            embed.add_field(name="Reason", value=reason, inline=False)
        
        await inter.response.send_message(embed=embed)
        await self._log_to_channel(inter.guild, embed)
    
    @app_commands.command(name="unrestrict", description="Remove restriction from user")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(user="The member to unrestrict")
    async def unrestrict(self, inter: discord.Interaction, user: discord.Member):
        """Remove restriction from user."""
        # Remove Restricted role with verification
        restricted_role_id = await settings_service.get_int("restricted_role_id")
        if not restricted_role_id:
            return await inter.response.send_message("❌ Restricted role not configured.", ephemeral=True)
        
        restricted_role = inter.guild.get_role(restricted_role_id)
        if not restricted_role:
            return await inter.response.send_message("❌ Restricted role not found.", ephemeral=True)
        
        success, error = await self._remove_role_with_verification(
            guild=inter.guild,
            user_id=user.id,
            role=restricted_role,
            reason=f"Unrestricted by {inter.user}"
        )
        
        if not success:
            return await inter.response.send_message(f"❌ {error}", ephemeral=True)
        
        # Update DB after successful role removal
        await db.execute('UPDATE users SET is_restricted = 0 WHERE user_id = %s', (user.id,))
        
        await mod_service.log_action("unrestrict", inter.user.id, user.id, None)
        
        embed = discord.Embed(title="🔓 User Unrestricted", color=discord.Color.green())
        embed.add_field(name="User", value=user.mention, inline=True)
        embed.add_field(name="Moderator", value=inter.user.mention, inline=True)
        
        await inter.response.send_message(embed=embed)
        await self._log_to_channel(inter.guild, embed)
        
    @app_commands.command(name="ban", description="Ban a user (perm wipes economy)")
    @app_commands.default_permissions(ban_members=True)
    @app_commands.describe(
        user="The member to ban",
        duration="Duration or 'perm' (permanent, wipes economy data)",
        reason="Optional reason for the ban"
    )
    async def ban(self, inter: discord.Interaction, user: discord.Member, duration: str = "perm", reason: str = None):
        """Ban user. Duration 'perm' wipes economy data."""
        if not await self._check_hierarchy(inter, user):
            return
        
        is_perm = duration.lower() == "perm"
        
        # DM before ban
        await self._notify_user(user, "banned" + (" permanently" if is_perm else f" for {duration}"), reason, inter.guild.name)
        
        # Wipe economy on perm ban
        if is_perm:
            await self._wipe_economy(user.id)
        
        await user.ban(reason=reason, delete_message_days=0)
        await mod_service.log_action("ban", inter.user.id, user.id, f"{'PERM' if is_perm else duration}: {reason or 'No reason'}")
        
        embed = discord.Embed(
            title="🚫 User Banned",
            color=discord.Color.dark_red()
        )
        embed.add_field(name="User", value=f"{user} ({user.id})", inline=True)
        embed.add_field(name="Duration", value="Permanent" if is_perm else duration, inline=True)
        embed.add_field(name="Moderator", value=inter.user.mention, inline=True)
        if is_perm:
            embed.add_field(name="Economy", value="⚠️ All data wiped", inline=False)
        if reason:
            embed.add_field(name="Reason", value=reason, inline=False)
        
        await inter.response.send_message(embed=embed)
        await self._log_to_channel(inter.guild, embed)
    
    # ─────────────────────────────────────────────────────────────────────
    # Additional Slash Commands
    # ─────────────────────────────────────────────────────────────────────
    
    @app_commands.command(name="kick", description="Kick a member from the server")
    @app_commands.default_permissions(kick_members=True)
    @app_commands.describe(user="Member to kick", reason="Reason for kick")
    async def kick(self, inter: discord.Interaction, user: discord.Member, reason: str = None):
        """Kick a member from the server."""
        if not await self._check_hierarchy(inter, user):
            return
        
        await self._notify_user(user, "kicked", reason, inter.guild.name)
        await user.kick(reason=reason)
        await mod_service.log_action("kick", inter.user.id, user.id, reason)
        
        embed = discord.Embed(title="👢 User Kicked", color=discord.Color.orange())
        embed.add_field(name="User", value=user.mention, inline=True)
        embed.add_field(name="Moderator", value=inter.user.mention, inline=True)
        if reason:
            embed.add_field(name="Reason", value=reason, inline=False)
        
        await inter.response.send_message(embed=embed)
        await self._log_to_channel(inter.guild, embed)
    
    @app_commands.command(name="unban", description="Unban a user by their ID")
    @app_commands.default_permissions(ban_members=True)
    @app_commands.describe(user_id="User ID to unban", reason="Reason for unban")
    async def unban(self, inter: discord.Interaction, user_id: str, reason: str = None):
        """Unban a user by their ID."""
        try:
            uid = int(user_id)
            user = await self.bot.fetch_user(uid)
            await inter.guild.unban(user, reason=reason)
            await mod_service.log_action("unban", inter.user.id, uid, reason)
            await inter.response.send_message(f"✅ **{user}** has been unbanned.")
        except ValueError:
            await inter.response.send_message("❌ Invalid user ID.", ephemeral=True)
        except discord.NotFound:
            await inter.response.send_message("❌ User not found or not banned.", ephemeral=True)
    
    @app_commands.command(name="unmute", description="Remove Muted role from a member")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(user="Member to unmute", reason="Reason for unmute")
    async def unmute(self, inter: discord.Interaction, user: discord.Member, reason: str = None):
        """Remove Muted role from a member."""
        muted_role_id = await settings_service.get_int("muted_role_id")
        if not muted_role_id:
            return await inter.response.send_message("❌ Muted role not configured.", ephemeral=True)
        
        muted_role = inter.guild.get_role(muted_role_id)
        if not muted_role:
            return await inter.response.send_message("❌ Muted role not found.", ephemeral=True)
        
        # Remove Muted role with verification
        success, error = await self._remove_role_with_verification(
            guild=inter.guild,
            user_id=user.id,
            role=muted_role,
            reason=f"Unmuted by {inter.user}: {reason}"
        )
        
        if not success:
            return await inter.response.send_message(f"❌ {error}", ephemeral=True)
        await mod_service.log_action("unmute", inter.user.id, user.id, reason)
        
        embed = discord.Embed(title="🔊 User Unmuted", color=discord.Color.green())
        embed.add_field(name="User", value=user.mention, inline=True)
        embed.add_field(name="Moderator", value=inter.user.mention, inline=True)
        if reason:
            embed.add_field(name="Reason", value=reason, inline=False)
        
        await inter.response.send_message(embed=embed)
        await self._log_to_channel(inter.guild, embed)

    @app_commands.command(name="purge", description="Bulk delete messages in the current channel")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.describe(
        amount="Number of messages to delete (max 1000). Default: 10.",
        user="Optional: Only delete messages from this specific user"
    )
    async def purge(self, inter: discord.Interaction, amount: app_commands.Range[int, 1, 1000] = 10, user: discord.Member = None):
        """Purge messages in bulk, optionally filtered by user."""
        await inter.response.defer(ephemeral=True)
        
        def check(m):
            if user:
                return m.author.id == user.id
            return True
            
        try:
            # Add 1 if filtering to ensure we capture enough history efficiently
            search_limit = amount + 50 if user else amount
            deleted = await inter.channel.purge(limit=search_limit, check=check, bulk=True)
            
            # If we deleted more than requested due to the search limit padding, standard API behavior
            if len(deleted) > amount:
                deleted = deleted[:amount]
                
            reason = f"Purged {len(deleted)} messages"
            if user:
                reason += f" from {user}"
                
            # Log action against the channel structurally since there is no specific single offended user ID
            await mod_service.log_action("purge", inter.user.id, inter.channel.id, reason)
            
            await inter.followup.send(f"✅ Successfully deleted **{len(deleted)}** messages.")
        except discord.Forbidden:
            await inter.followup.send("❌ I lack the `Manage Messages` permission in this channel.", ephemeral=True)
        except discord.HTTPException as e:
            await inter.followup.send(f"❌ Failed to purge messages. Note: Discord prevents bulk-deleting messages older than 14 days.\nError: `{e}`", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ModCog(bot))
