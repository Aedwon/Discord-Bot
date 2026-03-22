"""
Boost Tracker Cog - Full booster rewards system.
Manages tiered roles, color/emblem customization, badges, and perks.
Uses database-stored settings configured via !setup commands.
"""

import discord
import json
from datetime import datetime, timedelta
from discord.ext import commands, tasks
from discord import app_commands

from config import BOOSTER_TIERS
from services.xp_service import xp_service
from services.database import db
from services.settings_service import settings_service
from utils.embeds import create_boost_announcement_embed


class BoostCog(commands.Cog, name="Boost Tracker"):
    """Full booster rewards system with tiered perks."""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.recent_boosts = {}
        self.cooldown_seconds = 60
    
    async def cog_load(self):
        """Cog loaded — tasks start in on_ready."""
        pass
    
    def cog_unload(self):
        self.check_tier_promotions.cancel()
        self.weekly_spotlight.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        """Start background tasks after bot is fully connected."""
        if not self.check_tier_promotions.is_running():
            self.check_tier_promotions.start()
        if not self.weekly_spotlight.is_running():
            self.weekly_spotlight.start()
    
    # ─────────────────────────────────────────────────────────────────────
    # Settings Helpers (load from database)
    # ─────────────────────────────────────────────────────────────────────
    
    async def _get_tier_role_ids(self) -> dict:
        """Get tier role IDs from database."""
        return {
            "server": await settings_service.get_int("server_booster_role_id"),
            "veteran": await settings_service.get_int("veteran_booster_role_id"),
            "mythic": await settings_service.get_int("mythic_booster_role_id"),
        }
    
    async def _get_spotlight_role_id(self) -> int:
        return await settings_service.get_int("booster_spotlight_role_id")
    
    async def _get_boost_public_channel_id(self) -> int:
        return await settings_service.get_int("boost_public_channel_id")
    
    async def _get_boost_admin_channel_id(self) -> int:
        return await settings_service.get_int("boost_admin_channel_id")
    
    async def _get_announce_channel_id(self) -> int:
        return await settings_service.get_int("boost_public_channel_id")
    
    # ─────────────────────────────────────────────────────────────────────
    # Helper Methods
    # ─────────────────────────────────────────────────────────────────────
    
    def _get_tier_for_months(self, months: int) -> tuple[str, dict]:
        """Get tier key and config based on months boosting."""
        if months >= BOOSTER_TIERS["mythic"]["months_required"]:
            return "mythic", BOOSTER_TIERS["mythic"]
        elif months >= BOOSTER_TIERS["veteran"]["months_required"]:
            return "veteran", BOOSTER_TIERS["veteran"]
        else:
            return "server", BOOSTER_TIERS["server"]
    
    def _get_member_tier(self, member: discord.Member) -> tuple[str, dict] | tuple[None, None]:
        """Get a member's current booster tier."""
        if member.premium_since is None:
            return None, None
        days = (datetime.now() - member.premium_since.replace(tzinfo=None)).days
        return self._get_tier_for_months(days // 30)
    
    async def _grant_tier_role(self, member: discord.Member, tier_key: str, tier: dict, max_retries: int = 2) -> bool:
        """
        Grant tier role and remove other booster tier roles.
        Includes verification and retry logic for robustness.
        """
        import asyncio
        guild = member.guild
        role_ids = await self._get_tier_role_ids()
        target_role_id = role_ids.get(tier_key, 0)
        
        if not target_role_id:
            return False
        
        target_role = guild.get_role(target_role_id)
        if not target_role:
            return False
        
        # Check hierarchy before attempting
        if target_role.position >= guild.me.top_role.position:
            print(f"[BoostTracker] Cannot grant {target_role.name}: role position too high")
            return False
        
        for attempt in range(max_retries):
            try:
                # Fetch fresh member data
                try:
                    member = await guild.fetch_member(member.id)
                except discord.NotFound:
                    return False
                
                # Remove other tier roles first
                for key, rid in role_ids.items():
                    if rid and rid != target_role_id:
                        role = guild.get_role(rid)
                        if role and role in member.roles:
                            try:
                                await member.remove_roles(role, reason="Tier role update")
                            except discord.Forbidden:
                                pass  # Continue even if removal fails
                
                # Check if target role already assigned
                if target_role in member.roles:
                    return False  # Already has the role
                
                # Add the new tier role
                await member.add_roles(target_role, reason=f"Booster: {tier['name']}")
                
                # Small delay for API propagation
                await asyncio.sleep(0.5)
                
                # Re-fetch and verify
                try:
                    member = await guild.fetch_member(member.id)
                except discord.NotFound:
                    return False
                
                if target_role in member.roles:
                    return True  # Success!
                
                # Retry if not applied
                if attempt < max_retries - 1:
                    continue
                    
                print(f"[BoostTracker] Failed to grant {target_role.name} to {member.display_name} after {max_retries} attempts")
                return False
                
            except discord.Forbidden as e:
                print(f"[BoostTracker] Permission error granting tier role: {e}")
                return False
            except discord.HTTPException as e:
                if attempt < max_retries - 1:
                    continue
                print(f"[BoostTracker] HTTP error granting tier role: {e}")
                return False
        
        return False
    
    async def _remove_all_booster_roles(self, member: discord.Member):
        """
        Remove all booster-related roles with robust error handling.
        Continues even if individual role removals fail.
        """
        guild = member.guild
        roles_to_remove = []
        
        # Fetch fresh member data
        try:
            member = await guild.fetch_member(member.id)
        except discord.NotFound:
            print(f"[BoostTracker] Member {member.id} not found during role cleanup")
            return
        except discord.HTTPException as e:
            print(f"[BoostTracker] Failed to fetch member for role cleanup: {e}")
            return
        
        # Tier roles
        role_ids = await self._get_tier_role_ids()
        for rid in role_ids.values():
            if rid:
                role = guild.get_role(rid)
                if role and role in member.roles:
                    roles_to_remove.append(role)
        
        # Color role
        color_role_id = await self._get_user_color_role(member.id)
        if color_role_id:
            color_role = guild.get_role(color_role_id)
            if color_role and color_role in member.roles:
                roles_to_remove.append(color_role)
        
        # Emblem role
        emblem_role_id = await self._get_user_emblem_role(member.id)
        if emblem_role_id:
            emblem_role = guild.get_role(emblem_role_id)
            if emblem_role and emblem_role in member.roles:
                roles_to_remove.append(emblem_role)
        
        # Spotlight role
        spotlight_id = await self._get_spotlight_role_id()
        if spotlight_id:
            spotlight_role = guild.get_role(spotlight_id)
            if spotlight_role and spotlight_role in member.roles:
                roles_to_remove.append(spotlight_role)
        
        # Remove roles with individual error handling
        removed_count = 0
        for role in roles_to_remove:
            try:
                # Check hierarchy before attempting
                if role.position >= guild.me.top_role.position:
                    print(f"[BoostTracker] Cannot remove {role.name}: role position too high")
                    continue
                await member.remove_roles(role, reason="Boost expired - role cleanup")
                removed_count += 1
            except discord.Forbidden as e:
                print(f"[BoostTracker] Permission error removing {role.name}: {e}")
            except discord.HTTPException as e:
                print(f"[BoostTracker] HTTP error removing {role.name}: {e}")
        
        if roles_to_remove:
            print(f"[BoostTracker] Removed {removed_count}/{len(roles_to_remove)} roles from {member.display_name}")
    
    async def _get_user_color_role(self, user_id: int) -> int | None:
        result = await db.fetch_one('SELECT color_role_id FROM users WHERE user_id = %s', (user_id,))
        return result['color_role_id'] if result else None
    
    async def _get_user_emblem_role(self, user_id: int) -> int | None:
        result = await db.fetch_one('SELECT emblem_role_id FROM users WHERE user_id = %s', (user_id,))
        return result['emblem_role_id'] if result else None
    
    async def _add_badge(self, user_id: int, badge: str):
        """Add a badge to user's profile."""
        result = await db.fetch_one('SELECT badges FROM users WHERE user_id = %s', (user_id,))
        badges = json.loads(result['badges']) if result and result['badges'] else []
        if badge not in badges:
            badges.append(badge)
            await db.execute('UPDATE users SET badges = %s WHERE user_id = %s', (json.dumps(badges), user_id))
    
    async def _get_badges(self, user_id: int) -> list:
        result = await db.fetch_one('SELECT badges FROM users WHERE user_id = %s', (user_id,))
        return json.loads(result['badges']) if result and result['badges'] else []
    
    # ─────────────────────────────────────────────────────────────────────
    # Event Listeners
    # ─────────────────────────────────────────────────────────────────────
    
    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.premium_since == after.premium_since:
            return
        
        if before.premium_since is None and after.premium_since is not None:
            await self._handle_new_boost(after)
        elif before.premium_since is not None and after.premium_since is None:
            await self._handle_boost_expired(after)
    
    async def _handle_new_boost(self, member: discord.Member):
        """Handle new boost: grant roles, set perks, add badge, announce."""
        user_id = member.id
        now = datetime.now()
        
        # Anti-spam
        if user_id in self.recent_boosts:
            if (now - self.recent_boosts[user_id]).total_seconds() < self.cooldown_seconds:
                return
        self.recent_boosts[user_id] = now
        
        tier_key = "server"
        tier = BOOSTER_TIERS["server"]
        
        # Grant role
        await self._grant_tier_role(member, tier_key, tier)
        
        # Set DB perks
        await xp_service.set_booster_perks(
            user_id=user_id,
            xp_multiplier=tier["xp_multiplier"],
            shop_discount=tier["shop_discount"]
        )
        
        # Update token multiplier
        await db.execute('''
            UPDATE users SET token_multiplier = %s, raffle_entries = %s
            WHERE user_id = %s
        ''', (tier["token_multiplier"], tier["raffle_entries"], user_id))
        
        # Add S1 Booster badge
        await self._add_badge(user_id, "S1 Booster")
        
        # Public announcement (simple - who boosted)
        public_channel_id = await self._get_boost_public_channel_id()
        if public_channel_id:
            channel = self.bot.get_channel(public_channel_id)
            if channel:
                public_embed = discord.Embed(
                    title="✨ Celestial Ascendance",
                    description=f"**{member.display_name}** has transcended and boosted the server!\n*May their light guide us through the cosmos.*",
                    color=0xFFD700
                )
                public_embed.set_thumbnail(url=member.display_avatar.url)
                try:
                    await channel.send(embed=public_embed)
                except discord.Forbidden:
                    pass
        
        # Admin announcement (detailed)
        admin_channel_id = await self._get_boost_admin_channel_id()
        if admin_channel_id:
            channel = self.bot.get_channel(admin_channel_id)
            if channel:
                admin_embed = create_boost_announcement_embed(member)
                admin_embed.add_field(name="Tier Assigned", value=tier["name"], inline=True)
                admin_embed.add_field(name="XP Multiplier", value=f"{tier['xp_multiplier']}x", inline=True)
                admin_embed.add_field(name="Token Multiplier", value=f"{tier['token_multiplier']}x", inline=True)
                try:
                    await channel.send(embed=admin_embed)
                except discord.Forbidden:
                    pass
        
        print(f"[BoostTracker] {member.display_name} boosted! Badge added.")
    
    async def _handle_boost_expired(self, member: discord.Member):
        """Handle boost expiration: remove roles, reset perks (keep badges)."""
        await self._remove_all_booster_roles(member)
        await xp_service.remove_booster_perks(member.id)
        
        # Reset token multiplier and raffle entries
        await db.execute('''
            UPDATE users SET token_multiplier = 1.0, raffle_entries = 0,
                color_role_id = NULL, emblem_role_id = NULL
            WHERE user_id = %s
        ''', (member.id,))
        
        # Public notification (simple)
        public_channel_id = await self._get_boost_public_channel_id()
        if public_channel_id:
            channel = self.bot.get_channel(public_channel_id)
            if channel:
                public_embed = discord.Embed(
                    title="🌌 A Light Dims...",
                    description=f"**{member.display_name}**'s celestial ascendance has faded. Their boost has expired.",
                    color=discord.Color.dark_grey()
                )
                public_embed.set_thumbnail(url=member.display_avatar.url)
                try:
                    await channel.send(embed=public_embed)
                except discord.Forbidden:
                    pass
        
        # Admin notification (detailed)
        admin_channel_id = await self._get_boost_admin_channel_id()
        if admin_channel_id:
            channel = self.bot.get_channel(admin_channel_id)
            if channel:
                admin_embed = discord.Embed(
                    title="💔 Boost Expired",
                    color=discord.Color.red()
                )
                admin_embed.add_field(name="User", value=f"{member.mention} ({member})", inline=True)
                admin_embed.add_field(name="User ID", value=str(member.id), inline=True)
                admin_embed.add_field(name="Action", value="Perks removed, badges kept", inline=False)
                try:
                    await channel.send(embed=admin_embed)
                except discord.Forbidden:
                    pass
        
        print(f"[BoostTracker] {member.display_name}'s boost expired. Perks removed, badges kept.")
    
    # ─────────────────────────────────────────────────────────────────────
    # Background Tasks
    # ─────────────────────────────────────────────────────────────────────
    
    @tasks.loop(hours=24)
    async def check_tier_promotions(self):
        """Daily tier promotion check."""
        if not self.bot.guilds:
            return
        
        guild = self.bot.guilds[0]
        now = datetime.now()
        
        for member in guild.members:
            if member.premium_since is None:
                continue
            
            days = (now - member.premium_since.replace(tzinfo=None)).days
            months = days // 30
            tier_key, tier = self._get_tier_for_months(months)
            
            current = await xp_service.get_user_perks(member.id)
            if tier["xp_multiplier"] > current.get('xp_multiplier', 1.0):
                await self._grant_tier_role(member, tier_key, tier)
                await xp_service.set_booster_perks(
                    member.id, tier["xp_multiplier"], tier["shop_discount"]
                )
                await db.execute('''
                    UPDATE users SET token_multiplier = %s, raffle_entries = %s
                    WHERE user_id = %s
                ''', (tier["token_multiplier"], tier["raffle_entries"], member.id))
                print(f"[BoostTracker] Promoted {member.display_name} to {tier['name']}")
    
    @check_tier_promotions.before_loop
    async def before_tier_check(self):
        await self.bot.wait_until_ready()
    
    @tasks.loop(hours=168)  # Weekly
    async def weekly_spotlight(self):
        """Select Booster of the Week from Mythic boosters with robust role handling."""
        import asyncio
        
        if not self.bot.guilds:
            return
        
        guild = self.bot.guilds[0]
        spotlight_id = await self._get_spotlight_role_id()
        if not spotlight_id:
            return
        
        spotlight_role = guild.get_role(spotlight_id)
        if not spotlight_role:
            return
        
        # Check hierarchy before attempting
        if spotlight_role.position >= guild.me.top_role.position:
            print(f"[BoostTracker] Cannot manage spotlight role: position too high")
            return
        
        # Remove previous spotlight with verification
        for member in spotlight_role.members:
            try:
                await member.remove_roles(spotlight_role, reason="Weekly spotlight rotation")
                print(f"[BoostTracker] Removed spotlight from {member.display_name}")
            except discord.Forbidden as e:
                print(f"[BoostTracker] Failed to remove spotlight from {member.display_name}: {e}")
            except discord.HTTPException as e:
                print(f"[BoostTracker] HTTP error removing spotlight: {e}")
        
        # Find Mythic boosters
        mythic_members = []
        for member in guild.members:
            if member.premium_since:
                days = (datetime.now() - member.premium_since.replace(tzinfo=None)).days
                if days >= BOOSTER_TIERS["mythic"]["months_required"] * 30:
                    mythic_members.append(member)
        
        if mythic_members:
            import random
            winner = random.choice(mythic_members)
            
            try:
                # Fetch fresh member data
                winner = await guild.fetch_member(winner.id)
                
                await winner.add_roles(spotlight_role, reason="Booster of the Week")
                
                # Verify role was applied
                await asyncio.sleep(0.5)
                winner = await guild.fetch_member(winner.id)
                
                if spotlight_role not in winner.roles:
                    print(f"[BoostTracker] Failed to verify spotlight role on {winner.display_name}")
                    return
                
                print(f"[BoostTracker] Spotlight role assigned to {winner.display_name}")
                
                channel_id = await self._get_announce_channel_id()
                if channel_id:
                    channel = self.bot.get_channel(channel_id)
                    if channel:
                        embed = discord.Embed(
                            title="🌟 Booster of the Week!",
                            description=f"Congratulations {winner.mention}!\n\nThank you for your continued support! 💜",
                            color=discord.Color.gold()
                        )
                        embed.set_thumbnail(url=winner.display_avatar.url)
                        await channel.send(embed=embed)
                        
            except discord.NotFound:
                print(f"[BoostTracker] Winner left the server during spotlight assignment")
            except discord.Forbidden as e:
                print(f"[BoostTracker] Permission error assigning spotlight: {e}")
            except discord.HTTPException as e:
                print(f"[BoostTracker] HTTP error assigning spotlight: {e}")
    
    @weekly_spotlight.before_loop
    async def before_spotlight(self):
        await self.bot.wait_until_ready()
    
    # ─────────────────────────────────────────────────────────────────────
    # Slash Commands - Cosmetics
    # ─────────────────────────────────────────────────────────────────────
    
    @app_commands.command(name="booster-color", description="Choose an exclusive booster color role")
    async def booster_color(self, interaction: discord.Interaction):
        """Let boosters choose from exclusive color roles."""
        member = interaction.user
        tier_key, tier = self._get_member_tier(member)
        
        if not tier:
            return await interaction.response.send_message(
                "❌ You must be a Server Booster to use this!", ephemeral=True
            )
        
        color_roles = await settings_service.get_color_roles()
        if not color_roles:
            return await interaction.response.send_message(
                "❌ No color roles configured. Ask an admin to run `!setup color add`.", ephemeral=True
            )
        
        options = [
            discord.SelectOption(label=name, value=str(role_id))
            for name, role_id in color_roles.items()
            if role_id
        ]
        
        if not options:
            return await interaction.response.send_message(
                "❌ No color roles available.", ephemeral=True
            )
        
        select = discord.ui.Select(placeholder="Choose your color...", options=options[:25])
        
        async def callback(inter: discord.Interaction):
            import asyncio
            role_id = int(select.values[0])
            role = inter.guild.get_role(role_id)
            
            if not role:
                return await inter.response.send_message("❌ Role not found.", ephemeral=True)
            
            # Check hierarchy
            if role.position >= inter.guild.me.top_role.position:
                return await inter.response.send_message(
                    f"❌ Cannot assign **{role.name}**: role position is too high in the hierarchy.",
                    ephemeral=True
                )
            
            try:
                # Remove old color role
                old_color_id = await self._get_user_color_role(inter.user.id)
                if old_color_id:
                    old_role = inter.guild.get_role(old_color_id)
                    if old_role and old_role in inter.user.roles:
                        try:
                            await inter.user.remove_roles(old_role, reason="Changing booster color")
                        except discord.Forbidden:
                            pass  # Continue even if old role removal fails
                
                # Add new color role
                await inter.user.add_roles(role, reason="Booster color selection")
                
                # Verify role was applied
                await asyncio.sleep(0.5)
                member = await inter.guild.fetch_member(inter.user.id)
                
                if role not in member.roles:
                    return await inter.response.send_message(
                        f"❌ Failed to apply color role. Please contact an admin.",
                        ephemeral=True
                    )
                
                # Update DB only after confirmed success
                await db.execute('UPDATE users SET color_role_id = %s WHERE user_id = %s', (role_id, inter.user.id))
                
                await inter.response.send_message(f"✅ Your color is now **{role.name}**!", ephemeral=True)
                
            except discord.Forbidden as e:
                await inter.response.send_message(f"❌ Permission error: {e}", ephemeral=True)
            except discord.HTTPException as e:
                await inter.response.send_message(f"❌ Discord error: {e}", ephemeral=True)
        
        select.callback = callback
        view = discord.ui.View()
        view.add_item(select)
        
        await interaction.response.send_message("🎨 Choose your exclusive color:", view=view, ephemeral=True)
    
    @app_commands.command(name="booster-emblem", description="Choose an exclusive emblem (Tier 2+ only)")
    async def booster_emblem(self, interaction: discord.Interaction):
        """Let Tier 2+ boosters choose an emblem icon."""
        member = interaction.user
        tier_key, tier = self._get_member_tier(member)
        
        if tier_key not in ["veteran", "mythic"]:
            return await interaction.response.send_message(
                "❌ This is for **Veteran** and **Mythic** boosters only!\n"
                "Keep boosting to unlock at 3 months! 💎", ephemeral=True
            )
        
        emblem_roles = await settings_service.get_emblem_roles()
        if not emblem_roles:
            return await interaction.response.send_message(
                "❌ No emblem roles configured. Ask an admin to run `!setup emblem add`.", ephemeral=True
            )
        
        options = [
            discord.SelectOption(label=f"Emblem {emoji}", value=str(role_id), emoji=emoji)
            for emoji, role_id in emblem_roles.items()
            if role_id
        ]
        
        if not options:
            return await interaction.response.send_message(
                "❌ No emblem roles available.", ephemeral=True
            )
        
        select = discord.ui.Select(placeholder="Choose your emblem...", options=options)
        
        async def callback(inter: discord.Interaction):
            import asyncio
            role_id = int(select.values[0])
            role = inter.guild.get_role(role_id)
            
            if not role:
                return await inter.response.send_message("❌ Role not found.", ephemeral=True)
            
            # Check hierarchy
            if role.position >= inter.guild.me.top_role.position:
                return await inter.response.send_message(
                    f"❌ Cannot assign **{role.name}**: role position is too high in the hierarchy.",
                    ephemeral=True
                )
            
            try:
                # Remove old emblem
                old_emblem_id = await self._get_user_emblem_role(inter.user.id)
                if old_emblem_id:
                    old_role = inter.guild.get_role(old_emblem_id)
                    if old_role and old_role in inter.user.roles:
                        try:
                            await inter.user.remove_roles(old_role, reason="Changing booster emblem")
                        except discord.Forbidden:
                            pass  # Continue even if old role removal fails
                
                # Add new emblem role
                await inter.user.add_roles(role, reason="Booster emblem selection")
                
                # Verify role was applied
                await asyncio.sleep(0.5)
                member = await inter.guild.fetch_member(inter.user.id)
                
                if role not in member.roles:
                    return await inter.response.send_message(
                        f"❌ Failed to apply emblem role. Please contact an admin.",
                        ephemeral=True
                    )
                
                # Update DB only after confirmed success
                await db.execute('UPDATE users SET emblem_role_id = %s WHERE user_id = %s', (role_id, inter.user.id))
                
                await inter.response.send_message(f"✅ Your emblem is now **{role.name}**!", ephemeral=True)
                
            except discord.Forbidden as e:
                await inter.response.send_message(f"❌ Permission error: {e}", ephemeral=True)
            except discord.HTTPException as e:
                await inter.response.send_message(f"❌ Discord error: {e}", ephemeral=True)
        
        select.callback = callback
        view = discord.ui.View()
        view.add_item(select)
        
        await interaction.response.send_message("⚜️ Choose your exclusive emblem:", view=view, ephemeral=True)
    
    # ─────────────────────────────────────────────────────────────────────
    # Slash Commands - Perks & Info
    # ─────────────────────────────────────────────────────────────────────
    
    @app_commands.command(name="boostperks", description="Show your booster perks and progress")
    async def boostperks(self, inter: discord.Interaction):
        """Show your booster perks and progress."""
        member = inter.user
        tier_key, tier = self._get_member_tier(member)
        
        if not tier:
            embed = discord.Embed(
                title="💔 Not a Booster",
                description="Boost the server to unlock exclusive perks!",
                color=discord.Color.greyple()
            )
            embed.add_field(
                name="Perks Include",
                value="• Up to 2x XP & Token Multiplier\n• 20% Shop Discount\n• Exclusive Colors & Emblems\n• Weekly Raffle Entries\n• Daily Mystery Pouches",
                inline=False
            )
            return await inter.response.send_message(embed=embed)
        
        perks = await xp_service.get_user_perks(member.id)
        badges = await self._get_badges(member.id)
        days = (datetime.now() - member.premium_since.replace(tzinfo=None)).days
        
        embed = discord.Embed(
            title=f"💎 {tier['name']} Perks",
            color=discord.Color(0xf47fff)
        )
        embed.add_field(name="XP Multiplier", value=f"{tier['xp_multiplier']}x", inline=True)
        embed.add_field(name="Token Multiplier", value=f"{tier['token_multiplier']}x", inline=True)
        embed.add_field(name="Shop Discount", value=f"{int(tier['shop_discount'] * 100)}%", inline=True)
        embed.add_field(name="Raffle Entries/Week", value=str(tier['raffle_entries']), inline=True)
        embed.add_field(name="Daily Pouches", value=str(tier['daily_pouches']), inline=True)
        embed.add_field(name="Days Boosting", value=str(days), inline=True)
        
        if badges:
            embed.add_field(name="🏅 Badges", value=", ".join(badges), inline=False)
        
        # Next tier progress
        if tier_key == "server":
            next_days = BOOSTER_TIERS["veteran"]["months_required"] * 30 - days
            embed.add_field(name="Next Tier", value=f"Veteran in {next_days} days", inline=False)
        elif tier_key == "veteran":
            next_days = BOOSTER_TIERS["mythic"]["months_required"] * 30 - days
            embed.add_field(name="Next Tier", value=f"Mythic in {next_days} days", inline=False)
        
        embed.set_thumbnail(url=member.display_avatar.url)
        await inter.response.send_message(embed=embed)
    
    @app_commands.command(name="boosters", description="List all server boosters with their tier")
    @app_commands.default_permissions(administrator=True)
    async def boosters(self, inter: discord.Interaction):
        """List all boosters with their tier."""
        boosters = [m for m in inter.guild.members if m.premium_since]
        
        if not boosters:
            return await inter.response.send_message("No boosters yet! 💔")
        
        boosters.sort(key=lambda m: m.premium_since)
        
        embed = discord.Embed(
            title="💎 Server Boosters",
            description=f"**{len(boosters)}** members boosting!",
            color=discord.Color(0xf47fff)
        )
        
        lines = []
        for i, m in enumerate(boosters[:25], 1):
            days = (datetime.now() - m.premium_since.replace(tzinfo=None)).days
            tier_key, tier = self._get_tier_for_months(days // 30)
            emoji = "🥇" if tier_key == "mythic" else "🥈" if tier_key == "veteran" else "🥉"
            lines.append(f"**{i}.** {emoji} {m.mention} — {days}d ({tier['xp_multiplier']}x)")
        
        embed.add_field(name="Members", value="\n".join(lines), inline=False)
        await inter.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(BoostCog(bot))
