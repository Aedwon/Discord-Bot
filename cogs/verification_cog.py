"""
Verification Cog.
Persistent verification panel with button + modal.
Admin commands for lookup, edit, and unverify.
"""

import discord
from discord.ext import commands
from discord import app_commands
import logging

from services.verification_service import verification_service
from services.settings_service import settings_service

logger = logging.getLogger("mlbb_bot.verification_cog")

# Reference image showing where to find MLBB UID and Server
MLBB_REFERENCE_IMAGE = (
    "https://media.discordapp.net/attachments/1471519234608861264/"
    "1471519333837701272/Screenshot_2026-02-12_at_22.51.43.png"
    "?ex=69c153ac&is=69c0022c"
    "&hm=398671c5b2665b2ab7e10f91cc08ef3b3682e3289df6b5b6df6839d5fdc22bbc"
    "&=&format=webp&quality=lossless&width=1824&height=940"
)


# ─── PERSISTENT VIEW ────────────────────────────────────────────────────

class VerificationModal(discord.ui.Modal, title="📝 MLBB Account Verification"):
    """Three-field modal for collecting MLBB account info."""

    full_name = discord.ui.TextInput(
        label="Full Name",
        placeholder="e.g. Juan Dela Cruz",
        required=True,
        max_length=255,
    )
    mlbb_uid = discord.ui.TextInput(
        label="MLBB UID (Game ID number)",
        placeholder="e.g. 123456789",
        required=True,
        max_length=20,
    )
    mlbb_server = discord.ui.TextInput(
        label="MLBB Server ID (number next to UID)",
        placeholder="e.g. 3456",
        required=True,
        max_length=10,
    )

    async def on_submit(self, interaction: discord.Interaction):
        # ── Validate UID is numeric ──
        uid_str = self.mlbb_uid.value.strip()
        if not uid_str.isdigit():
            return await interaction.response.send_message(
                "❌ **MLBB UID must be a number.** Check the reference image above and try again.",
                ephemeral=True,
            )

        # ── Validate Server is numeric ──
        server_str = self.mlbb_server.value.strip()
        if not server_str.isdigit():
            return await interaction.response.send_message(
                "❌ **MLBB Server ID must be a number.** Check the reference image above and try again.",
                ephemeral=True,
            )

        uid = int(uid_str)
        server = int(server_str)
        name = self.full_name.value.strip()

        # ── Attempt verification ──
        result = await verification_service.verify_user(
            interaction.user.id, name, uid, server
        )

        if result is None:
            # Success — grant the Verified role
            verified_role_id = await settings_service.get_int("verified_role_id")
            if verified_role_id:
                role = interaction.guild.get_role(verified_role_id)
                if role:
                    try:
                        await interaction.user.add_roles(role, reason="MLBB Verification")
                    except discord.Forbidden:
                        logger.error(f"Cannot grant Verified role to {interaction.user.id}")

            embed = discord.Embed(
                title="✅ Verification Successful!",
                description=(
                    f"**Name:** {name}\n"
                    f"**MLBB UID:** {uid}\n"
                    f"**Server:** {server}\n\n"
                    "You can now earn XP and Event Points. Have fun! 🎉"
                ),
                color=discord.Color.green(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)

        elif result == "already_verified":
            info = await verification_service.get_user_info(interaction.user.id)
            embed = discord.Embed(
                title="ℹ️ Already Verified",
                description=(
                    f"You're already verified with:\n"
                    f"**Name:** {info['full_name']}\n"
                    f"**MLBB UID:** {info['mlbb_uid']}\n"
                    f"**Server:** {info['mlbb_server']}\n\n"
                    "Need to update your info? Contact an admin."
                ),
                color=discord.Color.blue(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)

        elif result.startswith("uid_taken:"):
            other_id = result.split(":")[1]
            await interaction.response.send_message(
                f"❌ **This MLBB UID is already linked to another account** (<@{other_id}>).\n"
                f"If this is your account, contact an admin for help.",
                ephemeral=True,
            )


class VerifyView(discord.ui.View):
    """Persistent view with a single Verify button. Survives bot restarts."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Verify",
        style=discord.ButtonStyle.green,
        emoji="✅",
        custom_id="verification:verify_button",
    )
    async def verify_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(VerificationModal())


class AdminEditModal(discord.ui.Modal, title="✏️ Edit Verification Info"):
    """Admin-only modal for editing a user's verification info."""

    def __init__(self, target_user_id: int, current_info: dict):
        super().__init__()
        self.target_user_id = target_user_id

        self.full_name = discord.ui.TextInput(
            label="Full Name",
            default=current_info['full_name'],
            required=True,
            max_length=255,
        )
        self.mlbb_uid = discord.ui.TextInput(
            label="MLBB UID",
            default=str(current_info['mlbb_uid']),
            required=True,
            max_length=20,
        )
        self.mlbb_server = discord.ui.TextInput(
            label="MLBB Server ID",
            default=str(current_info['mlbb_server']),
            required=True,
            max_length=10,
        )

        self.add_item(self.full_name)
        self.add_item(self.mlbb_uid)
        self.add_item(self.mlbb_server)

    async def on_submit(self, interaction: discord.Interaction):
        uid_str = self.mlbb_uid.value.strip()
        server_str = self.mlbb_server.value.strip()

        if not uid_str.isdigit() or not server_str.isdigit():
            return await interaction.response.send_message(
                "❌ UID and Server must be numbers.", ephemeral=True
            )

        result = await verification_service.update_user_info(
            self.target_user_id,
            self.full_name.value.strip(),
            int(uid_str),
            int(server_str),
        )

        if result is None:
            await interaction.response.send_message(
                f"✅ Updated verification info for <@{self.target_user_id}>.",
                ephemeral=True,
            )
        elif result.startswith("uid_taken:"):
            other_id = result.split(":")[1]
            await interaction.response.send_message(
                f"❌ UID already linked to <@{other_id}>.", ephemeral=True
            )


# ─── COG ────────────────────────────────────────────────────────────────

class VerificationCog(commands.Cog, name="verification"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        """Register persistent view and load verification cache on startup."""
        self.bot.add_view(VerifyView())
        await verification_service.load_cache()
        logger.info("Verification system ready")

    verify_group = app_commands.Group(name="verify", description="MLBB verification system")

    # ─── SETUP COMMANDS ─────────────────────────────────────────────────

    @verify_group.command(name="deploy", description="Post the verification panel in a channel.")
    @app_commands.default_permissions(administrator=True)
    async def setup_verification(self, interaction: discord.Interaction, channel: discord.TextChannel):
        """Post the persistent verification embed + button in the specified channel."""
        embed = discord.Embed(
            title="📋 Server Verification",
            description=(
                "To participate fully in this server, you need to verify your MLBB account.\n\n"
                "**What you'll need:**\n"
                "• Your **Full Name**\n"
                "• Your **MLBB Game ID (UID)**\n"
                "• Your **Server ID**\n\n"
                "**How to find your UID and Server:**\n"
                "Open MLBB → Profile → Your ID and Server are shown below your username "
                "(see the image below).\n\n"
                "Click the button below to get started! 👇"
            ),
            color=discord.Color.blurple(),
        )
        embed.set_image(url=MLBB_REFERENCE_IMAGE)
        embed.set_footer(text="You only need to verify once. Your data is stored securely.")

        await channel.send(embed=embed, view=VerifyView())
        await interaction.response.send_message(
            f"✅ Verification panel posted in {channel.mention}.", ephemeral=True
        )

    # ─── ADMIN LOOKUP COMMANDS ──────────────────────────────────────────

    @verify_group.command(name="status", description="Check a user's verification status.")
    @app_commands.default_permissions(administrator=True)
    async def verified(self, interaction: discord.Interaction, user: discord.Member):
        info = await verification_service.get_user_info(user.id)
        if not info:
            return await interaction.response.send_message(
                f"❌ {user.mention} is **not verified**.", ephemeral=True
            )

        embed = discord.Embed(
            title=f"📋 Verification — {user.display_name}",
            color=discord.Color.green(),
        )
        embed.add_field(name="Full Name", value=info['full_name'], inline=False)
        embed.add_field(name="MLBB UID", value=str(info['mlbb_uid']), inline=True)
        embed.add_field(name="Server", value=str(info['mlbb_server']), inline=True)
        embed.add_field(
            name="Verified At",
            value=discord.utils.format_dt(info['verified_at'], style="F"),
            inline=False,
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @verify_group.command(name="whois", description="Look up a Discord user by their MLBB UID.")
    @app_commands.default_permissions(administrator=True)
    async def whois(self, interaction: discord.Interaction, mlbb_uid: int):
        info = await verification_service.lookup_by_uid(mlbb_uid)
        if not info:
            return await interaction.response.send_message(
                f"❌ No user found with MLBB UID `{mlbb_uid}`.", ephemeral=True
            )

        member = interaction.guild.get_member(info['user_id'])
        name_display = member.mention if member else f"User ID: `{info['user_id']}`"

        embed = discord.Embed(
            title=f"🔍 MLBB UID Lookup — {mlbb_uid}",
            color=discord.Color.blue(),
        )
        embed.add_field(name="Discord User", value=name_display, inline=False)
        embed.add_field(name="Full Name", value=info['full_name'], inline=True)
        embed.add_field(name="Server", value=str(info['mlbb_server']), inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ─── ADMIN EDIT / UNVERIFY ──────────────────────────────────────────

    @verify_group.command(name="update", description="Edit a user's MLBB verification info.")
    @app_commands.default_permissions(administrator=True)
    async def update_verification(self, interaction: discord.Interaction, user: discord.Member):
        info = await verification_service.get_user_info(user.id)
        if not info:
            return await interaction.response.send_message(
                f"❌ {user.mention} is not verified. Nothing to edit.", ephemeral=True
            )

        modal = AdminEditModal(user.id, info)
        await interaction.response.send_modal(modal)

    @verify_group.command(name="remove", description="Remove a user's verification.")
    @app_commands.default_permissions(administrator=True)
    async def unverify(self, interaction: discord.Interaction, user: discord.Member):
        removed = await verification_service.unverify_user(user.id)
        if not removed:
            return await interaction.response.send_message(
                f"❌ {user.mention} is not verified.", ephemeral=True
            )

        # Strip the Verified role
        verified_role_id = await settings_service.get_int("verified_role_id")
        if verified_role_id:
            role = interaction.guild.get_role(verified_role_id)
            if role and role in user.roles:
                try:
                    await user.remove_roles(role, reason="Admin unverification")
                except discord.Forbidden:
                    pass

        await interaction.response.send_message(
            f"✅ {user.mention} has been **unverified**. They will no longer earn XP or EP.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(VerificationCog(bot))
