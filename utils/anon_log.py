"""
Shared utility for logging anonymous messages (confessions + anon messages)
to an admin-only log channel for abuse tracking.
"""

import discord
import datetime
import logging

from services.settings_service import settings_service

logger = logging.getLogger("mlbb_bot.anon_log")


async def log_anonymous_action(
    bot: discord.Client,
    *,
    user: discord.User | discord.Member,
    action_type: str,
    content: str,
    channel: discord.TextChannel,
    reference_label: str | None = None,
):
    """
    Send a log embed to the configured anonymous log channel.

    Parameters
    ----------
    bot : The bot instance (used to resolve the log channel).
    user : The user who performed the action (identity revealed to admins).
    action_type : Short label, e.g. "Confession", "Anon Message", "Anon Reply".
    content : The text content of the anonymous message.
    channel : The public channel the message was posted in.
    reference_label : Optional label like "Reply to Message #3" for context.
    """
    try:
        log_channel_id = await settings_service.get_int("anon_log_channel_id")
        if not log_channel_id:
            return

        log_channel = bot.get_channel(log_channel_id)
        if not log_channel:
            return

        embed = discord.Embed(
            title=f"🔒 {action_type}",
            description=content if len(content) <= 1024 else content[:1021] + "...",
            color=discord.Color.orange(),
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.add_field(name="Author", value=f"{user.mention} (`{user.id}`)", inline=True)
        embed.add_field(name="Channel", value=channel.mention, inline=True)
        if reference_label:
            embed.add_field(name="Context", value=reference_label, inline=False)

        embed.set_author(
            name=str(user),
            icon_url=user.display_avatar.url if user.display_avatar else None,
        )
        embed.set_footer(text="Anonymous Activity Log")

        await log_channel.send(embed=embed)
    except discord.HTTPException as e:
        logger.warning(f"Failed to send anon log: {e}")
    except Exception as e:
        logger.error(f"Anon log error: {e}")
