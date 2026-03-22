"""
Log Cog - Comprehensive message edit and delete logging.
"""

import discord
from discord.ext import commands
import logging
import asyncio

from services.settings_service import settings_service

logger = logging.getLogger("mlbb_bot.log_cog")

class LogCog(commands.Cog, name="Logging"):
    """Comprehensive message edit and deletion logging."""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        
    async def get_log_channel(self) -> discord.TextChannel:
        """Helper to get the configured message log channel."""
        channel_id = await settings_service.get_int("message_log_channel_id")
        if not channel_id:
            return None
        return self.bot.get_channel(channel_id)
        
    def extract_media(self, message: discord.Message) -> str:
        """Extract attachment URLs and Embed URLs (like Tenor GIFs)."""
        media = []
        for a in message.attachments:
            media.append(a.url)
        for e in message.embeds:
            if e.url:
                media.append(e.url)
            elif e.image and e.image.url:
                media.append(e.image.url)
            elif e.thumbnail and e.thumbnail.url:
                media.append(e.thumbnail.url)
        
        # Deduplicate and format
        valid_media = list(set([m for m in media if m]))
        if valid_media:
            return "\n".join([f"📎 [Media Link]({m})" for m in valid_media])
        return ""

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if message.author.bot:
            return
            
        channel = await self.get_log_channel()
        if not channel:
            return
            
        embed = discord.Embed(
            title="🗑️ Message Deleted",
            description=message.content or "*No text content*",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_author(name=f"{message.author.name} ({message.author.id})", icon_url=message.author.display_avatar.url)
        embed.add_field(name="Channel", value=message.channel.mention, inline=True)
        embed.add_field(name="Message ID", value=str(message.id), inline=True)
        
        media_text = self.extract_media(message)
        if media_text:
            embed.add_field(name="Attachments & Media", value=media_text, inline=False)
            
        try:
            await channel.send(embed=embed)
        except Exception as e:
            logger.error(f"Failed to log message delete: {e}")

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        # If the message IS cached, on_message_delete handles it. We can ignore it here to prevent duplicates.
        if payload.cached_message is not None:
            return
            
        channel = await self.get_log_channel()
        if not channel:
            return
            
        # Uncached delete. We don't have author or content.
        embed = discord.Embed(
            title="🗑️ Old Message Deleted (Uncached)",
            description="*This message was sent before the bot most recently restarted, so its precise text and author are unknown.*",
            color=discord.Color.dark_red(),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="Channel", value=f"<#{payload.channel_id}>", inline=True)
        embed.add_field(name="Message ID", value=str(payload.message_id), inline=True)
        
        try:
            await channel.send(embed=embed)
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages: list[discord.Message]):
        channel = await self.get_log_channel()
        if not channel:
            return
            
        # Ignore bots from the purge logs if desired, but mod purges usually include them. Let's include everything in a purge log.
        # Create rich embeds for all purged messages. Sort by time.
        messages.sort(key=lambda m: m.created_at)
        
        embeds = []
        for msg in messages:
            if not msg.content and not msg.attachments and not msg.embeds:
                # Ghost message, ignore
                continue
                
            embed = discord.Embed(
                description=msg.content[:4000] if msg.content else "*No text*",
                color=discord.Color.dark_orange(),
                timestamp=msg.created_at
            )
            name = f"{msg.author.name} (Bot)" if msg.author.bot else f"{msg.author.name}"
            # Safely handle users with no avatar
            avatar_url = msg.author.display_avatar.url if msg.author.display_avatar else None
            if avatar_url:
                embed.set_author(name=name, icon_url=avatar_url)
            else:
                embed.set_author(name=name)
            
            media = self.extract_media(msg)
            if media:
                embed.add_field(name="Media", value=media, inline=False)
                
            embed.set_footer(text=f"Msg ID: {msg.id}")
            embeds.append(embed)
            
        if not embeds:
            return
            
        # Batch send 10 embeds at a time
        purge_title = discord.Embed(
            title=f"🧹 Bulk Delete: {len(messages)} Messages Purged",
            description=f"Channel: {messages[0].channel.mention}",
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow()
        )
        
        # Send title message first
        try:
            await channel.send(embed=purge_title)
        except Exception:
            return
            
        # Send batched embeds
        for i in range(0, len(embeds), 10):
            batch = embeds[i:i+10]
            try:
                await channel.send(embeds=batch)
                await asyncio.sleep(1) # Rate limit protection
            except Exception as e:
                logger.error(f"Failed to log bulk delete batch: {e}")

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.author.bot:
            return
            
        # Ghost edit / Link preview expansion check
        if before.content == after.content:
            return
            
        channel = await self.get_log_channel()
        if not channel:
            return
            
        embed = discord.Embed(
            title="✏️ Message Edited",
            color=discord.Color.yellow(),
            url=after.jump_url,
            timestamp=discord.utils.utcnow()
        )
        embed.set_author(name=f"{before.author.name} ({before.author.id})", icon_url=before.author.display_avatar.url)
        embed.add_field(name="Channel", value=before.channel.mention, inline=True)
        embed.add_field(name="Jump to Message", value=f"[Click Here]({after.jump_url})", inline=True)
        
        # We need to show Before and After. Embeds allow multiple to bypass limits.
        
        def split_content(text: str, chunk_size: int = 4000) -> list[str]:
            if not text:
                return ["*No text*"]
            return [text[i:i+chunk_size] for i in range(0, len(text), chunk_size)]
            
        before_chunks = split_content(before.content)
        after_chunks = split_content(after.content)
        
        embeds = []
        
        # Main Info Embed
        embeds.append(embed)
        
        # Before Embeds
        for i, chunk in enumerate(before_chunks):
            e = discord.Embed(title=f"Before (Part {i+1})" if len(before_chunks) > 1 else "Before", description=chunk, color=discord.Color.light_grey())
            embeds.append(e)
            
        # After Embeds
        for i, chunk in enumerate(after_chunks):
            e = discord.Embed(title=f"After (Part {i+1})" if len(after_chunks) > 1 else "After", description=chunk, color=discord.Color.yellow())
            embeds.append(e)
            
        # Batch send 10 embeds at a time 
        for i in range(0, len(embeds), 10):
            batch = embeds[i:i+10]
            try:
                await channel.send(embeds=batch)
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"Failed to log message edit: {e}")

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        # Handled by cached event already if cached
        if payload.cached_message is not None:
            return
            
        channel = await self.get_log_channel()
        if not channel:
            return
            
        if "content" not in payload.data:
            return # Likely just an embed unrolling
            
        embed = discord.Embed(
            title="✏️ Old Message Edited (Uncached)",
            description="*This message was sent before the bot restarted. We only know its new content.*",
            color=discord.Color.gold(),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="Channel", value=f"<#{payload.channel_id}>", inline=True)
        embed.add_field(name="Message ID", value=str(payload.message_id), inline=True)
        
        new_content = payload.data.get("content", "*Unknown*")
        
        embeds = [embed]
        
        # Slice very long content
        for i in range(0, max(len(new_content), 1), 4000):
            chunk = new_content[i:i+4000] if new_content else "*No text*"
            e = discord.Embed(title="New Content", description=chunk, color=discord.Color.yellow())
            embeds.append(e)
            
        for i in range(0, len(embeds), 10):
            batch = embeds[i:i+10]
            try:
                await channel.send(embeds=batch)
            except Exception:
                pass

async def setup(bot: commands.Bot):
    await bot.add_cog(LogCog(bot))
