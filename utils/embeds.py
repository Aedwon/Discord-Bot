"""
Reusable Discord embed builders.
Centralizes embed styling for consistency.
"""

import discord
from datetime import datetime


def create_leaderboard_embed(guild: discord.Guild, users: list) -> discord.Embed:
    """
    Create a leaderboard embed.
    
    Args:
        guild: The Discord guild
        users: List of (user_id, xp) tuples
    """
    from services.xp_service import xp_service
    
    embed = discord.Embed(
        title="🏆 Server Leaderboard",
        color=discord.Color.gold()
    )
    
    description_lines = []
    medals = ["🥇", "🥈", "🥉"]
    
    for index, (user_id, xp) in enumerate(users):
        member = guild.get_member(user_id)
        name = member.display_name if member else f"User {user_id}"
        level = xp_service.get_level(xp)
        tier = xp_service.get_tier_name(level)
        
        # Medal for top 3, number for rest
        rank_str = medals[index] if index < 3 else f"**{index + 1}.**"
        description_lines.append(f"{rank_str} **{name}** — {xp:,} XP • Lv. {level} • {tier}")
    
    embed.description = "\n".join(description_lines) or "No users yet!"
    embed.set_footer(text="Keep chatting to climb the ranks!")
    
    return embed


def create_rank_embed(member: discord.Member, rank: int | None, xp: int) -> discord.Embed:
    """
    Create a rank embed for a specific user.
    
    Args:
        member: The Discord member
        rank: User's rank (None if unranked)
        xp: User's total XP
    """
    from services.xp_service import xp_service
    
    level = xp_service.get_level(xp)
    tier = xp_service.get_tier_name(level)
    
    embed = discord.Embed(
        title=f"📊 {member.display_name}'s Stats",
        color=member.color if member.color != discord.Color.default() else discord.Color.blue()
    )
    
    if rank:
        embed.add_field(name="Rank", value=f"#{rank}", inline=True)
    else:
        embed.add_field(name="Rank", value="Unranked", inline=True)
    
    embed.add_field(name="XP", value=f"{xp:,}", inline=True)
    embed.add_field(name="Level", value=str(level), inline=True)
    embed.add_field(name="Tier", value=tier or "None", inline=True)
    
    embed.set_thumbnail(url=member.display_avatar.url)
    
    return embed


def create_mod_action_embed(
    action: str,
    target: discord.Member,
    moderator: discord.Member,
    reason: str = None,
    duration: str = None
) -> discord.Embed:
    """
    Create a moderation action embed.
    
    Args:
        action: Type of action (Kick, Ban, Mute, Warning)
        target: The target user
        moderator: The moderator who performed the action
        reason: Reason for the action
        duration: Optional duration (for mutes)
    """
    # Color based on severity
    colors = {
        "Kick": discord.Color.orange(),
        "Ban": discord.Color.red(),
        "Mute": discord.Color.dark_orange(),
        "Warning": discord.Color.yellow(),
    }
    
    embed = discord.Embed(
        title=f"⚠️ {action}",
        color=colors.get(action, discord.Color.red()),
        timestamp=datetime.now()
    )
    
    embed.add_field(name="User", value=f"{target.mention} ({target.id})", inline=True)
    embed.add_field(name="Moderator", value=moderator.mention, inline=True)
    
    if duration:
        embed.add_field(name="Duration", value=duration, inline=True)
    
    embed.add_field(
        name="Reason", 
        value=reason or "No reason provided", 
        inline=False
    )
    
    embed.set_thumbnail(url=target.display_avatar.url)
    
    return embed


def create_modlog_embed(member: discord.Member, history: list) -> discord.Embed:
    """
    Create a moderation history embed.
    
    Args:
        member: The target member
        history: List of moderation log entries
    """
    embed = discord.Embed(
        title=f"📋 Moderation History: {member.display_name}",
        color=discord.Color.blue()
    )
    
    if not history:
        embed.description = "No moderation history found."
        return embed
    
    embed.set_thumbnail(url=member.display_avatar.url)
    
    for entry in history[:10]:  # Limit to 10 entries
        action = entry.get('action_type', 'Unknown').upper()
        reason = entry.get('reason', 'No reason')
        timestamp = entry.get('timestamp', 'Unknown date')
        
        embed.add_field(
            name=f"{action} — {timestamp}",
            value=reason[:100],  # Truncate long reasons
            inline=False
        )
    
    embed.set_footer(text=f"User ID: {member.id}")
    
    return embed


def create_boost_announcement_embed(member: discord.Member) -> discord.Embed:
    """
    Create a server boost announcement embed.
    
    Args:
        member: The member who boosted
    """
    guild = member.guild
    
    embed = discord.Embed(
        title="🎉 New Server Boost!",
        description=(
            f"**{member.mention}** just boosted the server!\n\n"
            f"We now have **{guild.premium_subscription_count}** boosts!"
        ),
        color=discord.Color(0xf47fff),
        timestamp=datetime.now()
    )
    
    embed.set_thumbnail(url=member.display_avatar.url)
    
    # Show boost level
    level_names = {
        0: "No Level",
        1: "Level 1",
        2: "Level 2", 
        3: "Level 3"
    }
    embed.add_field(
        name="Server Level",
        value=level_names.get(guild.premium_tier, "Unknown"),
        inline=True
    )
    
    embed.add_field(
        name="Total Boosts",
        value=str(guild.premium_subscription_count),
        inline=True
    )
    
    embed.set_footer(text="Thank you for supporting the server! 💜")
    
    return embed
