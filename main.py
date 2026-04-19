"""
MLBB Community Discord Bot
Main entry point with dynamic cog loading.
"""

import discord
import asyncio
import logging
from pathlib import Path
from discord.ext import commands

from config import DISCORD_TOKEN, GUILD_ID
from services.database import db
from services.settings_service import settings_service

# Setup logging
import sys
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger('mlbb_bot')

# Configure intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.reactions = True
intents.presences = True

# Create bot instance
bot = commands.Bot(command_prefix='!', intents=intents)

# Target guild object (created after bot connects)
TARGET_GUILD = None

from utils.checks import AdminAuthError

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: discord.app_commands.AppCommandError):
    if isinstance(error, discord.app_commands.CheckFailure) and isinstance(error.original if hasattr(error, 'original') else error, AdminAuthError):
        embed = discord.Embed(
            title="🔒 Unauthorized",
            description="This command requires an active Admin Session.\n\nPlease use `/admin auth` to unlock it.",
            color=discord.Color.red()
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"Failed to send auth error: {e}")
        return

    # Fallback for other errors
    logger.error(f"Command execution failed: {error}")
    try:
        msg = f"❌ An error occurred: {error}"
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass

@bot.event
async def on_ready():
    """Called when the bot is ready."""
    global TARGET_GUILD
    logger.info(f'Bot started as {bot.user}')
    
    # Initialize database
    await db.get_pool()
    logger.info('Database connected')
    
    # Register persistent views (must be done before sync)
    from cogs.tracker.notification_cog import NotificationPanelView
    bot.add_view(NotificationPanelView())
    
    # Check for missing settings
    await check_missing_settings()
    
    # Get target guild
    TARGET_GUILD = bot.get_guild(GUILD_ID)
    
    # Sync slash commands to the specific guild only
    try:
        if TARGET_GUILD:
            # Debug: Show how many commands are in the tree
            all_commands = bot.tree.get_commands()
            logger.info(f'Commands in tree before sync: {len(all_commands)}')
            for cmd in all_commands:
                logger.info(f'  - /{cmd.name}')
            
            bot.tree.copy_global_to(guild=TARGET_GUILD)
            synced = await bot.tree.sync(guild=TARGET_GUILD)
            logger.info(f'Synced {len(synced)} slash commands to {TARGET_GUILD.name}')
        else:
            logger.warning(f'Target guild {GUILD_ID} not found! Bot may not be in the server.')
    except Exception as e:
        logger.error(f'Failed to sync commands: {e}')
    
    # Check if historic granular data is missing and quietly auto-fill it
    from scripts.backfill_granular import run_backfill
    asyncio.create_task(run_backfill(14))

    logger.info('✅ Bot startup complete!')


@bot.event
async def on_interaction(interaction: discord.Interaction):
    """Log all slash command usage to the command log channel."""
    # Only log application commands (not autocomplete, modals, etc.)
    if interaction.type != discord.InteractionType.application_command:
        return
    
    cmd_log_channel_id = await settings_service.get_int("command_log_channel_id")
    if not cmd_log_channel_id:
        return
    
    channel = bot.get_channel(cmd_log_channel_id)
    if not channel:
        return
    
    # Build the command string with arguments
    command_name = interaction.data.get("name", "Unknown")
    
    # Extract arguments from the interaction data
    args_list = []
    if "options" in interaction.data:
        options = interaction.data.get("options", [])
        for opt in options:
            # Handle subcommands
            if opt.get("type") in [1, 2]:  # SUB_COMMAND or SUB_COMMAND_GROUP
                command_name += f" {opt['name']}"
                if "options" in opt:
                    for sub_opt in opt["options"]:
                        value = sub_opt.get('value', 'N/A')
                        args_list.append(f"{sub_opt['name']}={value}")
            else:
                value = opt.get('value', 'N/A')
                args_list.append(f"{opt['name']}={value}")
    
    args_str = " ".join(args_list) if args_list else ""
    
    # Create log embed
    embed = discord.Embed(
        title="📝 Command Used",
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="User", value=f"{interaction.user.mention} ({interaction.user})", inline=True)
    embed.add_field(name="Channel", value=f"<#{interaction.channel_id}>", inline=True)
    embed.add_field(name="Command", value=f"```/{command_name} {args_str}```" if args_str else f"```/{command_name}```", inline=False)
    embed.set_footer(text=f"User ID: {interaction.user.id}")
    
    try:
        await channel.send(embed=embed)
    except Exception:
        pass  # Silently fail if logging fails


async def check_missing_settings():
    """Log warnings for settings that haven't been configured."""
    from utils.constants import SETUP_SCHEMA
    
    missing = []
    for category, items in SETUP_SCHEMA.items():
        for item in items:
            value = await settings_service.get_int(item["key"])
            if value == 0:
                missing.append(f"{item['name']} ({item['cmd'].replace('`', '')})")
                
    # Also check cosmetics
    color_roles = await settings_service.get_color_roles()
    if not color_roles:
        missing.append("Color Roles (/setup color-add)")
        
    emblem_roles = await settings_service.get_emblem_roles()
    if not emblem_roles:
        missing.append("Emblem Roles (/setup emblem-add)")
    
    if missing:
        logger.warning("⚠️ Missing setup configurations:")
        for item in missing:
            logger.warning(f"   - {item}")


async def load_extensions():
    """Dynamically load all cogs from the cogs directory."""
    cogs_dir = Path(__file__).parent / "cogs"
    
    # List of cog modules to load
    cog_modules = [
        "cogs.leveling.xp_cog",
        "cogs.moderation.mod_cog",
        "cogs.moderation.log_cog",
        "cogs.tracker.boost_cog",
        "cogs.tracker.booster_raffle_cog",
        "cogs.tracker.event_cog",
        "cogs.tracker.event_raffle_cog",
        "cogs.tracker.leaderboard_cog",
        "cogs.tracker.ep_cog",
        "cogs.tracker.analytics_cog",
        "cogs.tracker.quiz_cog",
        "cogs.tracker.social_cog",
        "cogs.tracker.promo_cog",
        "cogs.tracker.notification_cog",
        "cogs.setup.setup_cog",
        "cogs.setup.auth_cog",
        "cogs.setup.test_cog",
        "cogs.embed_cog",
        "cogs.voice_cog",
        "cogs.verification_cog",
        "cogs.ticket_cog",
        "cogs.confession_cog",
        "cogs.counting_cog",
        "cogs.anon_message_cog",
        "cogs.pomodoro_cog",
        "cogs.quest_cog",
        "cogs.referral_cog",
    ]
    
    success_count = 0
    failed_cogs = []
    
    for cog in cog_modules:
        try:
            await bot.load_extension(cog)
            logger.info(f'Loaded extension: {cog}')
            success_count += 1
        except Exception as e:
            logger.error(f"Extension '{cog}' raised an error: {e}")
            failed_cogs.append(cog)
            
    total = len(cog_modules)
    if success_count == total:
        logger.info(f'✅ All {total}/{total} cogs loaded successfully.')
    else:
        logger.error(f'⚠️ Only {success_count}/{total} cogs loaded successfully.')
        logger.error(f'❌ Failed cogs list: {", ".join(failed_cogs)}')


@bot.tree.command(name="reload", description="Reload bot cogs (Admin only)")
@discord.app_commands.default_permissions(administrator=True)
@discord.app_commands.describe(cog="Cog to reload (leave empty for all)")
async def reload(inter: discord.Interaction, cog: str = None):
    """Reload cogs."""
    cog_mapping = {
        "xp": "cogs.leveling.xp_cog",
        "leveling": "cogs.leveling.xp_cog",
        "mod": "cogs.moderation.mod_cog",
        "moderation": "cogs.moderation.mod_cog",
        "logs": "cogs.moderation.log_cog",
        "logging": "cogs.moderation.log_cog",
        "boost": "cogs.tracker.boost_cog",
        "raffle": "cogs.tracker.booster_raffle_cog",
        "booster_raffle": "cogs.tracker.booster_raffle_cog",
        "event_raffle": "cogs.tracker.event_raffle_cog",
        "tracker": "cogs.tracker.boost_cog",
        "event": "cogs.tracker.event_cog",
        "events": "cogs.tracker.event_cog",
        "ep": "cogs.tracker.ep_cog",
        "ep_core": "cogs.tracker.ep_cog",
        "leaderboard": "cogs.tracker.leaderboard_cog",
        "leaderboards": "cogs.tracker.leaderboard_cog",
        "analytics": "cogs.tracker.analytics_cog",
        "metrics": "cogs.tracker.analytics_cog",
        "quiz": "cogs.tracker.quiz_cog",
        "trivia": "cogs.tracker.quiz_cog",
        "social": "cogs.tracker.social_cog",
        "promo": "cogs.tracker.promo_cog",
        "promotion": "cogs.tracker.promo_cog",
        "notification": "cogs.tracker.notification_cog",
        "notifications": "cogs.tracker.notification_cog",
        "setup": "cogs.setup.setup_cog",
        "auth": "cogs.setup.auth_cog",
        "test": "cogs.setup.test_cog",
        "embed": "cogs.embed_cog",
        "embeds": "cogs.embed_cog",
        "voice": "cogs.voice_cog",
        "verify": "cogs.verification_cog",
        "verification": "cogs.verification_cog",
        "ticket": "cogs.ticket_cog",
        "tickets": "cogs.ticket_cog",
        "confessions": "cogs.confession_cog",
        "confession": "cogs.confession_cog",
        "counting": "cogs.counting_cog",
        "anon": "cogs.anon_message_cog",
        "anon_messages": "cogs.anon_message_cog",
        "pomodoro": "cogs.pomodoro_cog",
        "quest": "cogs.quest_cog",
        "quests": "cogs.quest_cog",
        "referral": "cogs.referral_cog",
        "referrals": "cogs.referral_cog",
    }
    
    if cog:
        cog_path = cog_mapping.get(cog.lower())
        if not cog_path:
            return await inter.response.send_message(f"❌ Unknown cog: `{cog}`", ephemeral=True)
        
        try:
            await bot.reload_extension(cog_path)
            await inter.response.send_message(f"✅ Reloaded `{cog}`", ephemeral=True)
            logger.info(f'Reloaded: {cog_path}')
        except Exception as e:
            await inter.response.send_message(f"❌ Failed to reload: {e}", ephemeral=True)
    else:
        reloaded = []
        failed = []
        unique_paths = list(dict.fromkeys(cog_mapping.values()))
        
        for path in unique_paths:
            try:
                await bot.reload_extension(path)
                reloaded.append(path.split('.')[-1])
            except Exception as e:
                failed.append(f"{path}: {e}")
        
        msg = f"✅ Reloaded: {', '.join(reloaded)}" if reloaded else ""
        if failed:
            msg += f"\n❌ Failed: {', '.join(failed)}"
        
        await inter.response.send_message(msg or "No cogs to reload", ephemeral=True)


@bot.tree.command(name="ping", description="Check bot latency")
async def ping(inter: discord.Interaction):
    """Check bot latency."""
    latency = round(bot.latency * 1000)
    await inter.response.send_message(f"🏓 Pong! Latency: `{latency}ms`")


@bot.tree.command(name="help", description="View all available commands")
async def help_command(inter: discord.Interaction):
    """Display commands based on user's roles and permissions."""
    
    # ─────────────────────────────────────────────────────────────────
    # Command Metadata: Define all commands with their category
    # Categories: 'general', 'booster', 'admin'
    # ─────────────────────────────────────────────────────────────────
    
    COMMANDS = {
        "general": {
            "emoji": "🎯",
            "title": "General",
            "commands": [
                ("**`/help`**", "Show this help menu"),
                ("**`/ping`**", "Check bot response time"),
                ("**`/profile [user]`**", "View unified MLBB profile and ranks"),
                ("**`/levels-leaderboard`**", "View top 10 XP earners"),
                ("**`/event-leaderboard`**", "View top event attendees"),
                ("**`/raffles`**", "View active raffles"),
                ("**`/quiz-leaderboard`**", "View top MLBB quiz scorers"),
                ("**`/thank <user>`**", "Thank someone (+10 XP)"),
                ("**`/pomodoro start [users]`**", "Start a Pomodoro timer in your VC"),
                ("**`/pomodoro add <user>`**", "Add someone to your Pomodoro session"),
                ("**`/pomodoro leave`**", "Leave the Pomodoro session"),
                ("**`/pomodoro stop`**", "End Pomodoro for everyone (creator only)"),
                ("**`/quests`**", "View your daily quests and progress"),
                ("**`/referral view`**", "View your referral code and stats"),
                ("**`/referral link <code>`**", "Link a referral code"),
                ("**`/referral leaderboard`**", "View top referrers"),
            ]
        },
        "booster": {
            "emoji": "💎",
            "title": "Booster Perks",
            "commands": [
                ("**`/booster perks`**", "View your tier and multipliers"),
                ("**`/booster color`**", "Choose a custom name color"),
                ("**`/booster emblem`**", "Choose an emblem badge"),
            ]
        },
        "verification": {
            "emoji": "📋",
            "title": "Verification",
            "commands": [
                ("**`/verify deploy <channel>`**", "Post verification panel"),
                ("**`/verify whois <uid>`**", "Look up user by MLBB UID"),
                ("**`/verify update <user>`**", "Edit verification info"),
                ("**`/verify remove <user>`**", "Remove verification"),
            ]
        },
        "admin_voice": {
            "emoji": "🎤",
            "title": "Voice Channels",
            "commands": [
                ("**`/voice setup <channel>`**", "Set up auto-create VC"),
                ("**`/voice remove <channel>`**", "Remove auto-create"),
            ]
        },
        "admin_embeds": {
            "emoji": "📝",
            "title": "Embeds",
            "commands": [
                ("**`/embed send <channel> <link>`**", "Send/schedule embed"),
                ("**`/embed edit <link>`**", "Edit existing embed"),
                ("**`/embed download <link>`**", "Extract to Discohook"),
                ("**`/embed manage`**", "Manage scheduled embeds"),
                ("**`/embed logs <channel>`**", "Set embed log channel"),
            ]
        },
        "admin_mod": {
            "emoji": "🛡️",
            "title": "Moderation",
            "commands": [
                ("**`/warn <user> <reason>`**", "Warning + 24h XP lock"),
                ("**`/mute <user> <duration> [reason]`**", "Timeout (native Discord mute)"),
                ("**`/unmute <user>`**", "Remove timeout"),
                ("**`/restrict <user> <duration> [reason]`**", "Block images/embeds"),
                ("**`/unrestrict <user>`**", "Restore access"),
                ("**`/kick <user> [reason]`**", "Kick from server"),
                ("**`/ban <user> [duration] [reason]`**", "Ban (perm wipes data)"),
                ("**`/unban <user_id>`**", "Unban a user by ID"),
                ("**`/purge <amount> [user]`**", "Bulk delete messages"),
                ("**`/history <user>`**", "View mod history"),
            ]
        },
        "admin_setup": {
            "emoji": "⚙️",
            "title": "Setup & Admin",
            "commands": [
                ("**`/admin auth`**", "Unlock heavy admin commands"),
                ("**`/admin logout`**", "End admin session"),
                ("**`/setup view`**", "View all current settings"),
                ("**`/setup channel <type> <#ch>`**", "Set channels"),
                ("**`/setup role <type> <@role>`**", "Set roles"),
                ("**`/setup vc <type> <vc>`**", "Set voice channels"),
                ("**`/xp start / stop / status`**", "Control XP system"),
                ("**`/xp add / set / reset`**", "Manage user XP"),
                ("**`/ep add / set / reset`**", "Manage user EP"),
                ("**`/autorole`**", "Bulk-assign auto-role"),
                ("**`/booster list`**", "List all server boosters"),
                ("**`/force-booster-raffle`**", "Force execute weekly raffle"),
                ("**`/booster-raffle-status`**", "Diagnose auto raffle system"),
                ("**`/booster-raffle-export`**", "Export latest raffle to CSV"),
                ("**`/event …`**", "Event kiosk, placement, status + 8 more"),
                ("**`/event raffle …`**", "Create, draw, reroll, cancel raffles"),
                ("**`/analytics …`**", "12+ server analytics commands"),
                ("**`/quiz start / stop / reload`**", "Quiz session controls"),
                ("**`/ticket deploy / config / stats`**", "Ticket system"),
                ("**`/verify msl …`**", "MSL sheet setup, refresh, check"),
                ("**`/notification deploy [channel]`**", "Post notification role panel"),
                ("**`/confessions deploy / sync`**", "Confessions panel + sync"),
                ("**`/anon deploy / sync`**", "Anonymous messages panel + sync"),
                ("**`/manage-quests`**", "Manage quest definition catalog"),
                ("**`/referral previous`**", "Last week's referral stats"),
                ("**`/reload [cog]`**", "Hot-reload cogs"),
            ]
        },
    }
    
    # ─────────────────────────────────────────────────────────────────
    # Determine User's Access Level
    # ─────────────────────────────────────────────────────────────────
    
    member = inter.user
    is_admin = member.guild_permissions.administrator if inter.guild else False
    is_booster = member.premium_since is not None if hasattr(member, 'premium_since') else False
    
    # Determine which categories to show
    visible_categories = ["general"]
    
    if is_booster or is_admin:
        visible_categories.append("booster")
    
    if is_admin:
        visible_categories.extend(["admin_voice", "admin_embeds", "admin_mod", "admin_setup"])
    
    # ─────────────────────────────────────────────────────────────────
    # Build the Embed
    # ─────────────────────────────────────────────────────────────────
    
    # Set title and color based on access level
    if is_admin:
        title = "📖 Bot Commands (Admin View)"
        color = discord.Color.red()
        description = "You have **admin** access. Showing all commands."
    elif is_booster:
        title = "📖 Bot Commands (Booster View)"
        color = discord.Color(0xf47fff)  # Nitro pink
        description = "You have **booster** perks! Showing general + booster commands."
    else:
        title = "📖 Bot Commands"
        color = discord.Color.blue()
        description = "Showing public commands available to everyone."
    
    embed = discord.Embed(title=title, description=description, color=color)
    
    # Add fields for each visible category
    for cat_key in visible_categories:
        cat = COMMANDS[cat_key]
        
        # Format commands as "command — description"
        lines = [f"{cmd} — {desc}" for cmd, desc in cat["commands"]]
        value = "\n".join(lines)
        
        embed.add_field(
            name=f"{cat['emoji']} {cat['title']}",
            value=value,
            inline=False
        )
    
    # Footer based on access level
    if not is_admin and not is_booster:
        embed.set_footer(text="� Boost the server to unlock booster-exclusive commands!")
    elif is_booster and not is_admin:
        embed.set_footer(text="💜 Thank you for boosting!")
    else:
        embed.set_footer(text="🔐 Administrator access granted")
    
    await inter.response.send_message(embed=embed, ephemeral=True)


async def main():
    """Main entry point."""
    await load_extensions()
    await bot.start(DISCORD_TOKEN)


async def shutdown():
    """Graceful shutdown - close bot and database."""
    logger.info('Shutting down gracefully...')
    
    # Close bot first to trigger cog_unload (and their DB flushes)
    if not bot.is_closed():
        await bot.close()
        
    # Then close database connection
    try:
        await db.close()
    except Exception as e:
        logger.error(f"Error closing database: {e}")
        
    logger.info('Bot stopped')


if __name__ == '__main__':
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info('Received Ctrl+C, shutting down...')
    finally:
        # Cleanup
        loop.run_until_complete(shutdown())
        
        # Cancel all pending tasks
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        
        # Wait for cancellation
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        
        loop.close()
        logger.info('Shutdown complete')