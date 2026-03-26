"""
Notification Cog — Self-service role assignment panel.

Deploys a persistent embed with 6 buttons that allow community members
to toggle notification roles on/off. Role preferences are tracked in
the database for analytics and persistence.
"""

import discord
import logging
from discord import app_commands
from discord.ext import commands

from services.database import db

logger = logging.getLogger("mlbb_bot.notification_cog")

# ─── ROLE ↔ DB COLUMN MAPPING ────────────────────────────────────────
# Each entry: (button_label, emoji, discord_role_name, db_column, button_row)
NOTIFICATION_ROLES = [
    ("Server Events",   "📅", "Server Event Notification",  "notif_server_event",  0),
    ("Quiz",            "🧠", "Quiz Notification",          "notif_quiz",           0),
    ("Giveaways",       "🎁", "Giveaway Notification",      "notif_giveaway",       0),
    ("Surveys",         "📋", "Survey Notification",         "notif_survey",         1),
    ("Tournaments",     "⚔️", "Tournament Notification",    "notif_tournament",     1),
    ("Partner Events",  "🤝", "Partner Event Notification",  "notif_partner_event",  1),
]


# ─── PERSISTENT VIEW ─────────────────────────────────────────────────

class NotificationPanelView(discord.ui.View):
    """Persistent view with 6 toggle buttons for notification roles."""

    def __init__(self):
        super().__init__(timeout=None)

        for label, emoji, role_name, db_col, row in NOTIFICATION_ROLES:
            button = NotificationToggleButton(
                label=label,
                emoji=emoji,
                role_name=role_name,
                db_column=db_col,
                row=row,
            )
            self.add_item(button)


class NotificationToggleButton(discord.ui.Button):
    """A single notification role toggle button."""

    def __init__(self, label: str, emoji: str, role_name: str, db_column: str, row: int):
        # custom_id must be globally unique and stable across restarts
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=label,
            emoji=emoji,
            custom_id=f"notif_toggle:{db_column}",
            row=row,
        )
        self.role_name = role_name
        self.db_column = db_column

    async def callback(self, interaction: discord.Interaction):
        """Toggle the notification role for the user."""
        guild = interaction.guild
        member = interaction.user

        if not guild or not isinstance(member, discord.Member):
            return await interaction.response.send_message(
                "❌ This can only be used in a server.", ephemeral=True
            )

        # Look up the role by exact name
        role = discord.utils.get(guild.roles, name=self.role_name)
        if not role:
            return await interaction.response.send_message(
                f"❌ Role **{self.role_name}** not found. Please contact an admin.",
                ephemeral=True,
            )

        # Check bot hierarchy — can we manage this role?
        if role >= guild.me.top_role:
            return await interaction.response.send_message(
                f"❌ I cannot manage the **{self.role_name}** role (it is above my highest role).",
                ephemeral=True,
            )

        # Toggle logic
        try:
            if role in member.roles:
                # ── UNSUBSCRIBE ──
                await member.remove_roles(role, reason="Notification panel: unsubscribed")
                await self._update_db(member.id, False)

                embed = discord.Embed(
                    description=f"{self.emoji} You have **unsubscribed** from **{self.role_name}**.",
                    color=discord.Color.light_grey(),
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
            else:
                # ── SUBSCRIBE ──
                await member.add_roles(role, reason="Notification panel: subscribed")
                await self._update_db(member.id, True)

                embed = discord.Embed(
                    description=f"{self.emoji} You have **subscribed** to **{self.role_name}**!",
                    color=discord.Color.green(),
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)

        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I don't have permission to manage roles.", ephemeral=True
            )
        except discord.HTTPException as e:
            logger.error(f"Failed to toggle notification role for {member.id}: {e}")
            await interaction.response.send_message(
                "❌ Something went wrong. Please try again later.", ephemeral=True
            )

    async def _update_db(self, user_id: int, subscribed: bool):
        """Update the user's notification preference in the database."""
        await db.execute(f'''
            INSERT INTO users (user_id, {self.db_column})
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE
                {self.db_column} = %s
        ''', (user_id, subscribed, subscribed))


# ─── COG ──────────────────────────────────────────────────────────────

class NotificationCog(commands.Cog, name="Notifications"):
    """Self-service notification role management."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        """Register the persistent view when the cog loads."""
        self.bot.add_view(NotificationPanelView())

    notification_group = app_commands.Group(
        name="notification",
        description="Notification role management.",
        default_permissions=discord.Permissions(administrator=True),
    )

    @notification_group.command(name="deploy", description="Deploy the notification role panel.")
    @app_commands.describe(channel="Channel to post the panel in (defaults to current)")
    async def notification_deploy(
        self, interaction: discord.Interaction, channel: discord.TextChannel = None
    ):
        """Post the notification role selection panel."""
        target = channel or interaction.channel
        await interaction.response.defer(ephemeral=True)

        # Validate that all 6 roles exist before deploying
        guild = interaction.guild
        missing = []
        for _, _, role_name, _, _ in NOTIFICATION_ROLES:
            if not discord.utils.get(guild.roles, name=role_name):
                missing.append(role_name)

        if missing:
            embed = discord.Embed(
                title="⚠️ Missing Roles",
                description=(
                    "The following roles were not found in the server. "
                    "Please create them with **exact** names before deploying:\n\n"
                    + "\n".join(f"• `{r}`" for r in missing)
                ),
                color=discord.Color.orange(),
            )
            return await interaction.followup.send(embed=embed, ephemeral=True)

        # Build the panel embed
        embed = discord.Embed(
            title="🔔 Notification Preferences",
            description=(
                "Choose which notifications you want to receive!\n\n"
                "Click a button below to **subscribe** or **unsubscribe**. "
                "Your preferences are saved automatically.\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            ),
            color=discord.Color.blue(),
        )

        role_lines = []
        for label, emoji, role_name, _, _ in NOTIFICATION_ROLES:
            role = discord.utils.get(guild.roles, name=role_name)
            role_lines.append(f"{emoji} **{label}** — {role.mention}")

        embed.add_field(
            name="Available Notifications",
            value="\n".join(role_lines),
            inline=False,
        )
        embed.set_footer(text="Click a button to toggle your subscription.")

        view = NotificationPanelView()
        await target.send(embed=embed, view=view)
        await interaction.followup.send(
            f"✅ Notification panel deployed in {target.mention}!", ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(NotificationCog(bot))
