"""
Centralized configuration for the MLBB Discord Bot.
Loads environment variables from .env file.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


# Discord Bot Settings
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    raise ValueError("DISCORD_TOKEN not found in environment variables!")

# Target Guild (single-server bot)
GUILD_ID = 1214845175960961025

# Database Configuration
DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", 3306)),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", ""),
    "db": os.getenv("DB_NAME", "discord_bot"),
    "autocommit": True,
}


# XP Settings
XP_CONFIG = {
    "message": {
        "min_xp": 10,
        "max_xp": 15,
        "cooldown_seconds": 10,  # One XP gain per cycle
    },
    "reaction": {
        "xp_per_reaction": 5,
        "max_xp_per_message": 50,   # Max XP a message can generate from reactions
        "daily_cap": 100,           # User's daily cap for reaction XP
    },
    "voice": {
        "xp_stream_video": 4,       # High XP for screen share / camera
        "xp_unmuted": 2,            # Normal XP for active talking
        "xp_muted": 1,              # Low XP for just listening
        "xp_deafened": 1,           # Minimal XP for deafened/AFK
        "min_members": 2,           # Minimum active members in VC
        "cycle_seconds": 10,
    },
}

# Batch update interval (seconds)
BATCH_UPDATE_INTERVAL = 10

# ─────────────────────────────────────────────────────────────────────
# Booster Tier Configuration (role IDs stored in database via !setup)
# ─────────────────────────────────────────────────────────────────────

BOOSTER_TIERS = {
    "server": {
        "name": "Server Booster",
        "months_required": 0,
        "xp_multiplier": 1.5,
        "token_multiplier": 1.5,
        "shop_discount": 0.20,
        "raffle_entries": 1,
        "daily_pouches": 1,
    },
    "veteran": {
        "name": "Veteran Booster",
        "months_required": 3,
        "xp_multiplier": 1.75,
        "token_multiplier": 1.75,
        "shop_discount": 0.20,
        "raffle_entries": 4,
        "daily_pouches": 2,
    },
    "mythic": {
        "name": "Mythic Booster",
        "months_required": 5,
        "xp_multiplier": 2.0,
        "token_multiplier": 2.0,
        "shop_discount": 0.20,
        "raffle_entries": 8,
        "daily_pouches": 3,
        "spotlight_eligible": True,
    },
}
