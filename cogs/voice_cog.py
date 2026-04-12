"""
Voice Cog - Auto-Create Voice Channels
When users join a designated "master" channel, a personal VC is created for them.
The channel is auto-deleted when empty.
"""


import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import logging

from services.database import db

logger = logging.getLogger('mlbb_bot')

# ─── UI COMPONENTS ──────────────────────────────────────────────────

class VCLimitModal(discord.ui.Modal, title="Set VC Limit"):
    limit_input = discord.ui.TextInput(
        label="Max Users (1-99, 0 for unlimited)",
        placeholder="e.g. 5",
        required=True,
        max_length=2,
    )

    def __init__(self, channel: discord.VoiceChannel):
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        try:
            val = int(self.limit_input.value.strip())
            if val < 0 or val > 99:
                return await interaction.response.send_message("❌ Must be between 0 and 99.", ephemeral=True)
                
            await self.channel.edit(user_limit=val)
            limit_text = "Unlimited" if val == 0 else str(val)
            await interaction.response.send_message(f"✅ User limit set to **{limit_text}**.", ephemeral=True)
        except ValueError:
            await interaction.response.send_message("❌ Please enter a valid number.", ephemeral=True)
        except discord.HTTPException as e:
            if e.status == 429:
                await interaction.response.send_message("⏳ Discord rate limit hit. Try again later.", ephemeral=True)
            else:
                await interaction.response.send_message("❌ Failed to edit channel.", ephemeral=True)


class OwnershipTransferView(discord.ui.View):
    def __init__(self, parent_panel):
        super().__init__(timeout=60)
        self.parent_panel = parent_panel

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Select new owner...")
    async def select_user(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        if interaction.user.id != self.parent_panel.owner_id:
            return await interaction.response.send_message("❌ Only the owner can transfer.", ephemeral=True)

        new_owner = select.values[0]
        if not isinstance(new_owner, discord.Member):
            return await interaction.response.send_message("❌ User must be in this server.", ephemeral=True)
            
        guild = interaction.guild
        channel = self.parent_panel.channel

        overwrites = channel.overwrites
        old_owner = guild.get_member(self.parent_panel.owner_id)
        if old_owner and old_owner in overwrites:
            del overwrites[old_owner]
            
        overwrites[new_owner] = discord.PermissionOverwrite(manage_channels=True, move_members=True)
        
        try:
            await channel.edit(overwrites=overwrites)
            self.parent_panel.owner_id = new_owner.id
            await interaction.response.send_message(f"✅ Ownership transferred to {new_owner.mention}.", ephemeral=True)
            
            for child in self.children:
                child.disabled = True
            await interaction.message.edit(view=self)
            
        except discord.HTTPException:
            await interaction.response.send_message("⏳ Rate limited. Try again later.", ephemeral=True)


class VCControlPanel(discord.ui.View):
    def __init__(self, owner_id: int, channel: discord.VoiceChannel):
        super().__init__(timeout=None)
        self.owner_id = owner_id
        self.channel = channel
        self.cooldown = commands.CooldownMapping.from_cooldown(1, 4.0, commands.BucketType.channel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ Only the channel owner can use this control panel.", ephemeral=True)
            return False
            
        bucket = self.cooldown.get_bucket(interaction.message)
        if bucket:
            retry_after = bucket.update_rate_limit()
            if retry_after:
                await interaction.response.send_message(f"⏳ Please wait {retry_after:.1f}s before pressing again.", ephemeral=True)
                return False
            
        return True

    @discord.ui.button(label="+1", style=discord.ButtonStyle.success, emoji="➕")
    async def btn_plus(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        current = self.channel.user_limit or 0
        new_limit = min(99, current + 1 if current > 0 else len(self.channel.members) + 1)
        
        try:
            await self.channel.edit(user_limit=new_limit)
            await interaction.followup.send(f"✅ Limit increased to {new_limit}.", ephemeral=True)
        except discord.HTTPException:
            await interaction.followup.send("⏳ Rate limit hit. Try again later.", ephemeral=True)

    @discord.ui.button(label="-1", style=discord.ButtonStyle.danger, emoji="➖")
    async def btn_minus(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        current = self.channel.user_limit or 0
        if current == 0:
            current = len(self.channel.members)
            
        new_limit = max(1, current - 1)
        try:
            await self.channel.edit(user_limit=new_limit)
            await interaction.followup.send(f"✅ Limit decreased to {new_limit}.", ephemeral=True)
        except discord.HTTPException:
            await interaction.followup.send("⏳ Rate limit hit. Try again later.", ephemeral=True)

    @discord.ui.button(label="Set", style=discord.ButtonStyle.secondary, emoji="🔢")
    async def btn_set(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(VCLimitModal(self.channel))

    @discord.ui.button(label="Unlimited", style=discord.ButtonStyle.primary, emoji="♾️")
    async def btn_unlim(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            await self.channel.edit(user_limit=0)
            await interaction.followup.send("✅ Limit removed (Unlimited).", ephemeral=True)
        except discord.HTTPException:
            await interaction.followup.send("⏳ Rate limit hit. Try again later.", ephemeral=True)
            
    @discord.ui.button(label="Transfer", style=discord.ButtonStyle.secondary, emoji="👑")
    async def btn_transfer(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Select the new owner:", view=OwnershipTransferView(self), ephemeral=True)

# ─── COG ────────────────────────────────────────────────────────────


class VoiceCog(commands.Cog, name="Voice"):
    """Auto-create voice channel management."""
    
    voice_group = app_commands.Group(name="voice", description="Voice channel management commands", default_permissions=discord.Permissions(administrator=True))
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config_cache = {}  # {voice_channel_id: category_id}
        self.temp_channels = set()  # {channel_id}
    
    async def cog_load(self):
        """Load configs into cache on cog load."""
        try:
            rows = await db.fetch_all("SELECT voice_channel_id, category_id FROM autocreate_configs")
            for row in rows:
                self.config_cache[row['voice_channel_id']] = row['category_id']
            logger.info(f"Loaded {len(self.config_cache)} autocreate configs.")
            
            # Load active virtual channels so they aren't orphaned
            active_vcs = await db.fetch_all("SELECT channel_id FROM autocreate_active_vcs")
            self.temp_channels = {row['channel_id'] for row in active_vcs}
            logger.info(f"Loaded {len(self.temp_channels)} active auto-created channels from DB.")
        except Exception as e:
            logger.error(f"Failed to load autocreate configs: {e}")
            
    def cog_unload(self):
        self.vc_cleanup_task.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.vc_cleanup_task.is_running():
            self.vc_cleanup_task.start()

    @tasks.loop(minutes=1)
    async def vc_cleanup_task(self):
        """Sweep orphaned empty VCs every minute."""
        guild = self.bot.guilds[0] if self.bot.guilds else None
        if not guild: return

        to_remove = []
        # Clone self.temp_channels to iterate safely
        for channel_id in list(self.temp_channels):
            channel = guild.get_channel(channel_id)
            if not channel:
                # Channel no longer exists (e.g. manually deleted)
                to_remove.append(channel_id)
            elif isinstance(channel, discord.VoiceChannel) and len(channel.members) == 0:
                # Channel is empty
                try:
                    await channel.delete(reason="Auto VC Sweep: Empty")
                    to_remove.append(channel_id)
                except discord.NotFound:
                    # Native deletion or manual deletion raced us
                    to_remove.append(channel_id)
                except discord.HTTPException as e:
                    # E.g. Rate limit, we will retry next sweep
                    logger.warning(f"Sweep: Failed to delete VC {channel_id}: {e}")
                    
        if to_remove:
            placeholders = ','.join(['%s'] * len(to_remove))
            try:
                await db.execute(f"DELETE FROM autocreate_active_vcs WHERE channel_id IN ({placeholders})", tuple(to_remove))
            except Exception as e:
                logger.error(f"Failed to flush sweat channels from DB: {e}")
            
            for cid in to_remove:
                self.temp_channels.discard(cid)

    @vc_cleanup_task.before_loop
    async def before_cleanup(self):
        await self.bot.wait_until_ready()
    
    @voice_group.command(name="setup", description="Setup a voice channel that auto-creates when joined")
    @app_commands.describe(channel="The master voice channel to use")
    async def autocreate_setup(self, interaction: discord.Interaction, channel: discord.VoiceChannel):
        """Set up a master voice channel for auto-creation."""
        await interaction.response.defer(ephemeral=True)
        
        try:
            category_id = channel.category_id if channel.category else None
            
            await db.execute("""
                INSERT INTO autocreate_configs (voice_channel_id, category_id)
                VALUES (%s, %s)
                ON DUPLICATE KEY UPDATE category_id = VALUES(category_id)
            """, (channel.id, category_id))
            
            # Update cache
            self.config_cache[channel.id] = category_id
            
            await interaction.followup.send(
                f"✅ Setup complete: {channel.mention} is now an Autocreate channel.",
                ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)
    
    @voice_group.command(name="remove", description="Remove autocreate from a voice channel")
    @app_commands.describe(channel="The voice channel to remove autocreate from")
    async def autocreate_remove(self, interaction: discord.Interaction, channel: discord.VoiceChannel):
        """Remove a master voice channel."""
        await interaction.response.defer(ephemeral=True)
        
        try:
            await db.execute(
                "DELETE FROM autocreate_configs WHERE voice_channel_id = %s",
                (channel.id,)
            )
            
            # Update cache
            self.config_cache.pop(channel.id, None)
            
            await interaction.followup.send(
                f"✅ Removed: {channel.mention} is no longer an Autocreate channel.",
                ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)
    
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        """Handle voice state changes for auto-create functionality."""
        
        # 1. Check if user joined a master channel
        if after.channel and after.channel.id in self.config_cache:
            try:
                category_id = self.config_cache[after.channel.id]
                guild = member.guild
                
                # Get category
                category = None
                if category_id:
                    category = guild.get_channel(category_id)
                if not category:
                    category = after.channel.category
                
                # Copy permissions and give user management rights
                overwrites = after.channel.overwrites.copy()
                overwrites[member] = discord.PermissionOverwrite(
                    manage_channels=True,
                    move_members=True
                )
                
                # Create temp channel
                temp_channel = await guild.create_voice_channel(
                    name=f"{member.display_name}'s VC",
                    category=category,
                    overwrites=overwrites
                )
                
                self.temp_channels.add(temp_channel.id)
                try:
                    await db.execute("INSERT IGNORE INTO autocreate_active_vcs (channel_id) VALUES (%s)", (temp_channel.id,))
                except Exception as e:
                    logger.error(f"Failed to save temp VC to DB: {e}")
                
                # Move member to their new channel
                await member.move_to(temp_channel)
                
                # Send control panel mapped to owner
                try:
                    view = VCControlPanel(owner_id=member.id, channel=temp_channel)
                    embed = discord.Embed(
                        description=f"👑 **Owner:** {member.mention}\nUse the buttons below to manage your temporary voice channel.",
                        color=discord.Color.blue()
                    )
                    await temp_channel.send(content=f"{member.mention} Your VC is ready!", embed=embed, view=view)
                except Exception as e:
                    logger.error(f"Failed to send control panel: {e}")
                
            except Exception as e:
                logger.error(f"Error in autocreate voice: {e}")
        
        # 2. Cleanup: Delete empty temp channels
        if before.channel and before.channel.id in self.temp_channels:
            if len(before.channel.members) == 0:
                try:
                    await before.channel.delete()
                    self.temp_channels.discard(before.channel.id)
                    await db.execute("DELETE FROM autocreate_active_vcs WHERE channel_id = %s", (before.channel.id,))
                except discord.NotFound:
                    self.temp_channels.discard(before.channel.id)
                    await db.execute("DELETE FROM autocreate_active_vcs WHERE channel_id = %s", (before.channel.id,))
                except discord.HTTPException as e:
                    # If rate limited, the 1-minute sweep will handle it safely instead of blocking.
                    if e.status != 429:
                        logger.error(f"Failed to delete channel immediately: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(VoiceCog(bot))
