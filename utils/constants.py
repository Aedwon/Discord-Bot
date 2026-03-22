"""
Utility constants for the bot.
"""

import datetime
import pytz

# Philippine Standard Time
TZ_MANILA = pytz.timezone('Asia/Manila')

def now_manila():
    """Get current time in Manila timezone."""
    return datetime.datetime.now(TZ_MANILA)

# Centralized Setup Checker Schema
SETUP_SCHEMA = {
    "📢 Channels": [
        {"key": "bot_channel_id", "name": "Bot Commands (XP excluded)", "type": "channel", "cmd": "`/setup channel bot <#channel>`"},
        {"key": "mod_log_channel_id", "name": "Mod Log", "type": "channel", "cmd": "`/setup channel modlog <#channel>`"},
        {"key": "command_log_channel_id", "name": "Command Log", "type": "channel", "cmd": "`/setup channel cmdlog <#channel>`"},
        {"key": "message_log_channel_id", "name": "Message Logs", "type": "channel", "cmd": "`/setup channel message_log <#channel>`"},
        {"key": "ticket_log_channel_id", "name": "Ticket Logs", "type": "channel", "cmd": "`/setup channel ticket_log <#channel>`"},
        {"key": "voice_log_channel_id", "name": "Voice Logs", "type": "channel", "cmd": "`/setup channel voice_log <#channel>`"},
        {"key": "event_log_channel_id", "name": "Event Logs", "type": "channel", "cmd": "`/setup channel event_log <#channel>`"},
        {"key": "giveaway_log_channel_id", "name": "Giveaway Logs", "type": "channel", "cmd": "`/setup channel giveaway_log <#channel>`"},
        {"key": "leaderboard_channel_id", "name": "Leaderboard", "type": "channel", "cmd": "`/setup channel leaderboard <#channel>`"},
    ],
    "💎 Boost Channels": [
        {"key": "boost_announce_channel_id", "name": "Boost Announce", "type": "channel", "cmd": "`/setup channel announce <#channel>`"},
        {"key": "boost_public_channel_id", "name": "Boost Public", "type": "channel", "cmd": "`/setup channel boost_public <#channel>`"},
        {"key": "boost_admin_channel_id", "name": "Boost Admin", "type": "channel", "cmd": "`/setup channel boost_admin <#channel>`"},
        {"key": "booster_chat_channel_id", "name": "Booster Chat", "type": "channel", "cmd": "`/setup channel booster_chat <#channel>`"},
        {"key": "booster_lounge_vc_id", "name": "Booster Lounge VC", "type": "channel", "cmd": "`/setup vc booster_lounge <#channel>`"},
    ],
    "🎭 Roles (Boosters)": [
        {"key": "server_booster_role_id", "name": "Server Booster", "type": "role", "cmd": "`/setup role server <@role>`"},
        {"key": "veteran_booster_role_id", "name": "Veteran Booster", "type": "role", "cmd": "`/setup role veteran <@role>`"},
        {"key": "mythic_booster_role_id", "name": "Mythic Booster", "type": "role", "cmd": "`/setup role mythic <@role>`"},
        {"key": "booster_spotlight_role_id", "name": "Spotlight", "type": "role", "cmd": "`/setup role spotlight <@role>`"},
    ],
    "🛡️ Roles (Moderation)": [
        {"key": "muted_role_id", "name": "Muted", "type": "role", "cmd": "`/setup role muted <@role>`"},
        {"key": "restricted_role_id", "name": "Restricted", "type": "role", "cmd": "`/setup role restricted <@role>`"},
    ]
}
