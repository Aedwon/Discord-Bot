import discord
from discord import app_commands
import time

# Store active sessions in memory: {user_id: expire_timestamp}
ACTIVE_ADMIN_SESSIONS = {}

ADMIN_PASSWORD = "@MSLPhilippines2026"
SESSION_DURATION_MINUTES = 15

class AdminAuthError(app_commands.CheckFailure):
    pass

def is_admin_authenticated(interaction: discord.Interaction) -> bool:
    """Check if the user has an active, unexpired admin session."""
    expire_time = ACTIVE_ADMIN_SESSIONS.get(interaction.user.id)
    if expire_time and time.time() < expire_time:
        # Extend session slightly on active use, or just let it expire.
        # We'll just let it expire as a strict 15-min window for security.
        return True
    
    # If expired or not present, remove them securely
    if interaction.user.id in ACTIVE_ADMIN_SESSIONS:
        del ACTIVE_ADMIN_SESSIONS[interaction.user.id]
    
    return False

def require_admin_auth():
    """Decorator to require an active Admin Auth Session for a command."""
    def predicate(interaction: discord.Interaction) -> bool:
        if not is_admin_authenticated(interaction):
            raise AdminAuthError()
        return True
    return app_commands.check(predicate)
