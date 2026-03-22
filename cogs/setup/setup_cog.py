"""
Setup Cog - Admin slash commands to configure bot settings.
All commands are slash commands under /setup group.
"""

import discord
from discord.ext import commands
from discord import app_commands
from typing import Literal

from services.settings_service import settings_service


class SetupCog(commands.Cog, name="Setup"):
    """Admin slash commands for bot configuration."""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
    
    setup_group = app_commands.Group(name="setup", description="Configure bot settings", default_permissions=discord.Permissions(administrator=True))
    
    # ─────────────────────────────────────────────────────────────────────
    # View Settings
    # ─────────────────────────────────────────────────────────────────────
    
    @setup_group.command(name="view", description="View all current bot settings & setup checklist")
    async def setup_view(self, inter: discord.Interaction):
        """View current bot settings and setup checklist."""
        settings = await settings_service.get_all()
        from utils.constants import SETUP_SCHEMA
        
        embed = discord.Embed(
            title="⚙️ Bot Setup Checklist", 
            description="A dynamic tracker of all configured features.",
            color=discord.Color.blue()
        )
        
        # Iterate over categories from schema
        for category, items in SETUP_SCHEMA.items():
            lines = []
            for item in items:
                val = settings.get(item["key"], "0")
                if val != "0":
                    mapped = f"<#{val}>" if item["type"] == "channel" else f"<@&{val}>"
                    lines.append(f"✅ **{item['name']}:** {mapped}")
                else:
                    lines.append(f"❌ **{item['name']}:** Missing! → Use {item['cmd']}")
            
            embed.add_field(name=category, value="\n".join(lines), inline=False)
            
        # Handle Cosmetics
        color_roles = await settings_service.get_color_roles()
        emblem_roles = await settings_service.get_emblem_roles()
        
        cosmetics_lines = []
        if color_roles:
            c_list = ", ".join([f"<@&{rid}>" for rid in color_roles.values() if rid])
            cosmetics_lines.append(f"✅ **Colors ({len(color_roles)}):** {c_list}")
        else:
            cosmetics_lines.append("❌ **Colors:** None configured → Use `/setup color-add`")
            
        if emblem_roles:
            e_list = ", ".join([f"{emoji} <@&{rid}>" for emoji, rid in emblem_roles.items() if rid])
            cosmetics_lines.append(f"✅ **Emblems ({len(emblem_roles)}):** {e_list}")
        else:
            cosmetics_lines.append("❌ **Emblems:** None configured → Use `/setup emblem-add`")
            
        embed.add_field(name="🎨 Cosmetics", value="\n".join(cosmetics_lines), inline=False)
        
        await inter.response.send_message(embed=embed, ephemeral=True)
    
    # ─────────────────────────────────────────────────────────────────────
    # Channel Setup
    # ─────────────────────────────────────────────────────────────────────
    
    @setup_group.command(name="channel", description="Set a text channel")
    @app_commands.describe(
        setting="Which channel setting to configure",
        channel="The channel to set"
    )
    async def setup_channel(
        self, 
        inter: discord.Interaction, 
        setting: Literal[
            "message_log", "ticket_log", "voice_log", "giveaway_log",
            "boost_public", "boost_admin",
            "modlog", "cmdlog", "event_log",
            "leaderboard", "bot", "announce", "booster_chat"
        ],
        channel: discord.TextChannel
    ):
        key_map = {
            # Log channels
            "message_log": "message_log_channel_id",
            "ticket_log": "ticket_log_channel_id",
            "voice_log": "voice_log_channel_id",
            "giveaway_log": "giveaway_log_channel_id",
            # Boost channels
            "boost_public": "boost_public_channel_id",
            "boost_admin": "boost_admin_channel_id",
            # Mod channels
            "modlog": "mod_log_channel_id",
            "cmdlog": "command_log_channel_id",
            "event_log": "event_log_channel_id",
            # System channels
            "leaderboard": "leaderboard_channel_id",
            "bot": "bot_channel_id",
            "announce": "boost_announce_channel_id",
            "booster_chat": "booster_chat_channel_id",
        }
        await settings_service.set(key_map[setting], str(channel.id))
        await inter.response.send_message(f"✅ **{setting}** channel set to {channel.mention}", ephemeral=True)
    
    # ─────────────────────────────────────────────────────────────────────
    # Role Setup
    # ─────────────────────────────────────────────────────────────────────
    
    @setup_group.command(name="role", description="Set a role")
    @app_commands.describe(
        setting="Which role setting to configure",
        role="The role to set"
    )
    async def setup_role(
        self, 
        inter: discord.Interaction, 
        setting: Literal["server", "veteran", "mythic", "spotlight", "muted", "restricted", "verified", "support"],
        role: discord.Role
    ):
        key_map = {
            "server": "server_booster_role_id",
            "veteran": "veteran_booster_role_id",
            "mythic": "mythic_booster_role_id",
            "spotlight": "booster_spotlight_role_id",
            "muted": "muted_role_id",
            "restricted": "restricted_role_id",
            "verified": "verified_role_id",
            "support": "support_role_id",
        }
        await settings_service.set(key_map[setting], str(role.id))
        await inter.response.send_message(f"✅ **{setting}** role set to {role.mention}", ephemeral=True)
    
    # ─────────────────────────────────────────────────────────────────────
    # Voice Channel Setup
    # ─────────────────────────────────────────────────────────────────────
    
    @setup_group.command(name="vc", description="Set a voice channel")
    @app_commands.describe(
        setting="Which voice channel setting to configure",
        channel="The voice channel to set"
    )
    async def setup_vc(
        self,
        inter: discord.Interaction,
        setting: Literal["booster_lounge"],
        channel: discord.VoiceChannel
    ):
        key_map = {
            "booster_lounge": "booster_lounge_vc_id",
        }
        await settings_service.set(key_map[setting], str(channel.id))
        await inter.response.send_message(f"✅ **{setting}** VC set to {channel.mention}", ephemeral=True)
    
    # ─────────────────────────────────────────────────────────────────────
    # Color Role Setup
    # ─────────────────────────────────────────────────────────────────────
    
    @setup_group.command(name="color-add", description="Add a booster color role")
    async def setup_color_add(self, inter: discord.Interaction, name: str, role: discord.Role):
        await settings_service.set_color_role(name, role.id)
        await inter.response.send_message(f"✅ Added color **{name}** → {role.mention}", ephemeral=True)
    
    @setup_group.command(name="color-remove", description="Remove a booster color role")
    async def setup_color_remove(self, inter: discord.Interaction, name: str):
        await settings_service.remove_color_role(name)
        await inter.response.send_message(f"✅ Removed color **{name}**", ephemeral=True)
    
    @setup_group.command(name="color-list", description="List all booster color roles")
    async def setup_color_list(self, inter: discord.Interaction):
        colors = await settings_service.get_color_roles()
        if not colors:
            return await inter.response.send_message("No color roles configured.", ephemeral=True)
        
        lines = [f"**{n}:** <@&{rid}>" for n, rid in colors.items()]
        embed = discord.Embed(
            title="🎨 Color Roles",
            description="\n".join(lines),
            color=discord.Color.purple()
        )
        await inter.response.send_message(embed=embed, ephemeral=True)
    
    # ─────────────────────────────────────────────────────────────────────
    # Emblem Role Setup
    # ─────────────────────────────────────────────────────────────────────
    
    @setup_group.command(name="emblem-add", description="Add a booster emblem role")
    async def setup_emblem_add(self, inter: discord.Interaction, emoji: str, role: discord.Role):
        await settings_service.set_emblem_role(emoji, role.id)
        await inter.response.send_message(f"✅ Added emblem {emoji} → {role.mention}", ephemeral=True)
    
    @setup_group.command(name="emblem-remove", description="Remove a booster emblem role")
    async def setup_emblem_remove(self, inter: discord.Interaction, emoji: str):
        emblems = await settings_service.get_emblem_roles()
        emblems.pop(emoji, None)
        import json
        await settings_service.set("booster_emblem_roles", json.dumps(emblems))
        await inter.response.send_message(f"✅ Removed emblem {emoji}", ephemeral=True)
    
    @setup_group.command(name="emblem-list", description="List all booster emblem roles")
    async def setup_emblem_list(self, inter: discord.Interaction):
        emblems = await settings_service.get_emblem_roles()
        if not emblems:
            return await inter.response.send_message("No emblem roles configured.", ephemeral=True)
        
        lines = [f"{e} → <@&{rid}>" for e, rid in emblems.items()]
        embed = discord.Embed(
            title="⚜️ Emblem Roles",
            description="\n".join(lines),
            color=discord.Color.gold()
        )
        await inter.response.send_message(embed=embed, ephemeral=True)

    # ─────────────────────────────────────────────────────────────────────
    # XP Role Auto-Discovery
    # ─────────────────────────────────────────────────────────────────────

    @setup_group.command(name="xp_roles", description="Auto-discover and map the 21 EXP Role Tiers dynamically.")
    async def setup_xp_roles(self, inter: discord.Interaction):
        await inter.response.defer(ephemeral=True)
        
        expected = []
        ranks = ["Commoner", "Vassal", "Noble", "High Noble"]
        numerals = ["V", "IV", "III", "II", "I"]
        for r in ranks:
            for n in numerals:
                expected.append(f"{r} {n}")
        expected.append("Monarch")
        
        found = 0
        log = []
        for name in expected:
            role = discord.utils.get(inter.guild.roles, name=name)
            if role:
                await settings_service.set(f"xp_role_{name.replace(' ', '_')}", str(role.id))
                found += 1
                log.append(f"✅ **{name}**")
            else:
                log.append(f"❌ **{name}** — role not found")
                
        embed = discord.Embed(title="⚙️ Auto-Map XP Roles", description="\n".join(log), color=discord.Color.brand_green())
        embed.set_footer(text=f"Linked: {found}/21 Roles")
        await inter.followup.send(embed=embed)

    # ─────────────────────────────────────────────────────────────────────
    # EP Sub-Tier Role Auto-Discovery (34 roles)
    # ─────────────────────────────────────────────────────────────────────

    @setup_group.command(name="ep_roles", description="Auto-map all 34 EP sub-tier roles (Warrior V → Legend I + Mythic ladder).")
    async def setup_ep_roles(self, inter: discord.Interaction):
        await inter.response.defer(ephemeral=True)
        from services.ep_service import ep_service

        expected = ep_service.get_all_ep_role_names()
        found, log = 0, []

        for name in expected:
            role = discord.utils.get(inter.guild.roles, name=name)
            if role:
                await settings_service.set(f"ep_role_{name.replace(' ', '_')}", str(role.id))
                found += 1
                log.append(f"✅ **{name}**")
            else:
                log.append(f"❌ **{name}** — role not found")

        total = len(expected)
        embed = discord.Embed(
            title="⚙️ EP Sub-Tier Roles Auto-Mapped",
            description="\n".join(log),
            color=discord.Color.brand_green() if found == total else discord.Color.orange()
        )
        embed.set_footer(text=f"Linked: {found}/{total} roles")
        await inter.followup.send(embed=embed)

    # ─────────────────────────────────────────────────────────────────────
    # Peak Rank Role Auto-Discovery (10 roles)
    # ─────────────────────────────────────────────────────────────────────

    @setup_group.command(name="peak_roles", description="Auto-map the 10 Peak Rank legacy roles (Peak Warrior → Peak Mythical Immortal).")
    async def setup_peak_roles(self, inter: discord.Interaction):
        await inter.response.defer(ephemeral=True)
        from services.ep_service import ep_service

        expected = ep_service.get_all_main_tier_names()
        found, log = 0, []

        for name in expected:
            role = discord.utils.get(inter.guild.roles, name=f"Peak: {name}")
            if role:
                await settings_service.set(f"peak_role_{name.replace(' ', '_')}", str(role.id))
                found += 1
                log.append(f"✅ **Peak: {name}**")
            else:
                log.append(f"❌ **Peak: {name}** — role not found")

        total = len(expected)
        embed = discord.Embed(
            title="⚙️ Peak Rank Roles Auto-Mapped",
            description="\n".join(log),
            color=discord.Color.gold() if found == total else discord.Color.orange()
        )
        embed.set_footer(text=f"Linked: {found}/{total} roles")
        await inter.followup.send(embed=embed)

    # ─────────────────────────────────────────────────────────────────────
    # End of Season Trigger
    # ─────────────────────────────────────────────────────────────────────

    @setup_group.command(name="trigger_eos", description="Force trigger End-of-Season: assign Peak Ranks, reset EP, advance season.")
    @app_commands.default_permissions(administrator=True)
    async def trigger_eos(self, inter: discord.Interaction):
        current_season = await settings_service.get_int("current_season")
        if current_season == 0:
            current_season = 1
        await settings_service.set("eos_reset_triggered", "1")
        await inter.response.send_message(
            f"🚨 **End of Season {current_season} triggered.**\n"
            f"The background engine will:\n"
            f"• Upgrade Peak Rank roles for all qualifying users\n"
            f"• Strip all seasonal EP roles\n"
            f"• Reset EP to 0\n"
            f"• Advance to Season {current_season + 1}\n\n"
            f"This will process within the next 24 hours (or on the next loop cycle).",
            ephemeral=True
        )

    # ─────────────────────────────────────────────────────────────────────
    # Analytics Setup
    # ─────────────────────────────────────────────────────────────────────

    @setup_group.command(name="analytics_sentiment_channel", description="Set the channel for automatic daily sentiment exports.")
    async def setup_sentiment_channel(self, inter: discord.Interaction, channel: discord.TextChannel):
        await settings_service.set("analytics_sentiment_channel", str(channel.id))
        await inter.response.send_message(f"✅ Daily sentiment exports will auto-post to {channel.mention}.", ephemeral=True)

    @setup_group.command(name="analytics_tracked_roles", description="Set which opt-in roles to track adoption rates for.")
    async def setup_tracked_roles(self, inter: discord.Interaction, role1: discord.Role, role2: discord.Role = None, role3: discord.Role = None, role4: discord.Role = None, role5: discord.Role = None):
        roles = [r for r in [role1, role2, role3, role4, role5] if r]
        role_ids = ",".join(str(r.id) for r in roles)
        await settings_service.set("analytics_tracked_roles", role_ids)
        names = ", ".join(f"**{r.name}**" for r in roles)
        await inter.response.send_message(f"✅ Now tracking adoption rates for: {names}", ephemeral=True)

    @setup_group.command(name="analytics_regions", description="Set which role names represent geographic regions.")
    async def setup_regions(self, inter: discord.Interaction, region_roles: str):
        """Comma-separated list of role names that represent regions (e.g. 'Luzon,Visayas,Mindanao,SEA,Europe')"""
        await settings_service.set("analytics_region_roles", region_roles)
        await inter.response.send_message(f"✅ Region roles configured: `{region_roles}`", ephemeral=True)

    # ─────────────────────────────────────────────────────────────────────
    # Quiz Setup
    # ─────────────────────────────────────────────────────────────────────

    @setup_group.command(name="quiz_channel", description="Set the channel for automated quiz sessions.")
    async def setup_quiz_channel(self, inter: discord.Interaction, channel: discord.TextChannel):
        await settings_service.set("quiz_channel_id", str(channel.id))
        await inter.response.send_message(f"✅ Quiz sessions will run in {channel.mention} (Noon & 8PM PHT daily).", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(SetupCog(bot))

