"""
Verification Cog.
Persistent verification panel with button + modal.
Admin commands for lookup, edit, and unverify.
"""

import discord
from discord.ext import commands, tasks
from discord import app_commands
import logging

from services.verification_service import verification_service
from services.settings_service import settings_service
from services.referral_service import referral_service
from utils.checks import require_admin_auth

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
    referral_code = discord.ui.TextInput(
        label="Referral Code (optional)",
        placeholder="e.g. MSL-21I3V9",
        required=False,
        max_length=20,
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

            # ── MSL Cross-Reference ──
            msl_status = ""
            if verification_service.is_msl(uid, server):
                msl_nickname = verification_service.get_msl_nickname(uid, server)
                msl_role_id = await settings_service.get_int("msl_role_id")
                if msl_role_id:
                    msl_role = interaction.guild.get_role(msl_role_id)
                    if msl_role:
                        try:
                            await interaction.user.add_roles(msl_role, reason="MSL Verification")
                            msl_status = f"\n\n🎓 **Moonton Student Leader Detected!**\nMSL Name: **{msl_nickname}**"
                        except discord.Forbidden:
                            logger.error(f"Cannot grant MSL role to {interaction.user.id}")
                    else:
                        msl_status = f"\n\n🎓 **Moonton Student Leader Detected!**\nMSL Name: **{msl_nickname}**"
                else:
                    msl_status = f"\n\n🎓 **Moonton Student Leader Detected!**\nMSL Name: **{msl_nickname}**"

            # ── Referral Code (non-blocking) ──
            referral_status = ""
            ref_code = self.referral_code.value.strip()
            if ref_code:
                try:
                    ref_result = await referral_service.link_referral(
                        interaction.user.id,
                        ref_code,
                        interaction.user.joined_at,
                    )
                    if ref_result is None:
                        referral_status = "\n\n🔗 Referral code applied!"
                    else:
                        referral_status = "\n\n⚠️ Referral code invalid — use `/referral-link` to try again."
                except Exception as e:
                    logger.error(f"Referral link error during verification: {e}")
                    referral_status = ""

            embed = discord.Embed(
                title="✅ Verification Successful!",
                description=(
                    f"**Name:** {name}\n"
                    f"**MLBB UID:** {uid}\n"
                    f"**Server:** {server}\n\n"
                    f"You can now earn XP and Event Points. Have fun! 🎉{msl_status}{referral_status}"
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
        """Register persistent view and load verification + MSL caches on startup."""
        self.bot.add_view(VerifyView())
        await verification_service.load_cache()
        await verification_service.load_msl_cache()
        self.msl_refresh_loop.start()
        logger.info("Verification system ready")

    @tasks.loop(hours=6)
    async def msl_refresh_loop(self):
        """Periodically refresh the MSL cache from Google Sheets."""
        count = await verification_service.load_msl_cache()
        logger.info(f"MSL cache refreshed: {count} entries")

    @msl_refresh_loop.before_loop
    async def before_msl_refresh(self):
        await self.bot.wait_until_ready()

    verify_group = app_commands.Group(name="verify", description="MLBB verification system", default_permissions=discord.Permissions(administrator=True))

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

    # ─── MSL SUBGROUP ────────────────────────────────────────────────────

    msl_group = app_commands.Group(
        name="msl", description="Moonton Student Leader verification",
        parent=verify_group
    )

    @msl_group.command(name="setup", description="Configure the MSL spreadsheet and role")
    @require_admin_auth()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        sheet_url="Google Sheets URL (must be public / anyone with link)",
        role="The MSL role to assign"
    )
    async def msl_setup(self, interaction: discord.Interaction, sheet_url: str, role: discord.Role):
        await interaction.response.defer(ephemeral=True)

        # Validate the URL looks like a Google Sheet
        if 'spreadsheets/d/' not in sheet_url:
            return await interaction.followup.send(
                "❌ That doesn't look like a Google Sheets URL. "
                "It should contain `spreadsheets/d/`.",
                ephemeral=True
            )

        await settings_service.set("msl_sheet_url", sheet_url)
        await settings_service.set("msl_role_id", str(role.id))

        # Immediately load the cache to validate
        count = await verification_service.load_msl_cache()

        await interaction.followup.send(
            f"✅ **MSL Verification configured!**\n\n"
            f"📄 Sheet: {sheet_url}\n"
            f"🏷️ Role: {role.mention}\n"
            f"👥 **{count}** MSL entries loaded from the FINAL tab.",
            ephemeral=True
        )

    @msl_group.command(name="refresh", description="Force refresh the MSL cache from Google Sheets")
    @require_admin_auth()
    @app_commands.default_permissions(administrator=True)
    async def msl_refresh(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        count = await verification_service.load_msl_cache()
        await interaction.followup.send(
            f"✅ MSL cache refreshed — **{count}** entries loaded.",
            ephemeral=True
        )

    @msl_group.command(name="check", description="Check if a verified user is an MSL member")
    @require_admin_auth()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(user="The user to check")
    async def msl_check(self, interaction: discord.Interaction, user: discord.Member):
        info = await verification_service.get_user_info(user.id)
        if not info:
            return await interaction.response.send_message(
                f"❌ {user.mention} is not verified.", ephemeral=True
            )

        mlbb_uid = info['mlbb_uid']
        mlbb_server = info['mlbb_server']
        if verification_service.is_msl(mlbb_uid, mlbb_server):
            nickname = verification_service.get_msl_nickname(mlbb_uid, mlbb_server)
            msl_role_id = await settings_service.get_int("msl_role_id")

            # Grant role if not already assigned
            if msl_role_id:
                msl_role = interaction.guild.get_role(msl_role_id)
                if msl_role and msl_role not in user.roles:
                    try:
                        await user.add_roles(msl_role, reason="MSL manual check")
                    except discord.Forbidden:
                        pass

            await interaction.response.send_message(
                f"🎓 **{user.mention}** is an MSL member!\n"
                f"MSL Nickname: **{nickname}**\n"
                f"MLBB UID: **{mlbb_uid}**",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"❌ {user.mention} (UID: {mlbb_uid}) is **not** in the MSL spreadsheet.",
                ephemeral=True
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(VerificationCog(bot))
