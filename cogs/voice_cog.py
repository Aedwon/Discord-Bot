"""
Voice Cog - Auto-Create Voice Channels
When users join a designated "master" channel, a personal VC is created for them.
The channel is auto-deleted when empty.
"""

import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import logging

from services.database import db

logger = logging.getLogger('mlbb_bot')


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
        except Exception as e:
            logger.error(f"Failed to load autocreate configs: {e}")
    
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
                
                # Move member to their new channel
                await member.move_to(temp_channel)
                
            except Exception as e:
                logger.error(f"Error in autocreate voice: {e}")
        
        # 2. Cleanup: Delete empty temp channels
        if before.channel and before.channel.id in self.temp_channels:
            if len(before.channel.members) == 0:
                try:
                    await before.channel.delete()
                    self.temp_channels.discard(before.channel.id)
                except discord.NotFound:
                    self.temp_channels.discard(before.channel.id)
                except discord.HTTPException as e:
                    if e.status == 429:  # Rate limited
                        logger.warning(f"Rate limited on channel delete. Retrying in {e.retry_after}s")
                        await asyncio.sleep(e.retry_after)
                        try:
                            await before.channel.delete()
                            self.temp_channels.discard(before.channel.id)
                        except:
                            pass
                    else:
                        logger.error(f"Failed to delete channel: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(VoiceCog(bot))
