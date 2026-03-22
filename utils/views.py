"""
Discord UI Views for the bot.
"""

import discord


class ManageScheduledEmbedView(discord.ui.View):
    """Dropdown view for managing scheduled embeds."""
    
    def __init__(self, scheduled_embeds: list, cog, user: discord.User):
        super().__init__(timeout=60)
        self.cog = cog
        self.user = user
        
        # Create options from scheduled embeds
        options = [
            discord.SelectOption(
                label=f"ID: {row['identifier']}",
                value=row['identifier'],
                description=f"Scheduled for: {row['schedule_for']}"
            )
            for row in scheduled_embeds[:25]  # Max 25 options
        ]
        
        self.select = discord.ui.Select(
            placeholder="Select an embed to preview...",
            options=options,
            min_values=1,
            max_values=1
        )
        self.select.callback = self.select_callback
        self.add_item(self.select)
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Ensure only the original user can use this view."""
        return interaction.user.id == self.user.id
    
    async def select_callback(self, interaction: discord.Interaction):
        """Handle selection."""
        identifier = self.select.values[0]
        await self.cog.preview_scheduled_embed_action(interaction, identifier)
        self.stop()


class ManageEmbedActionView(discord.ui.View):
    """Buttons to confirm cancellation or keep a previewed embed."""
    def __init__(self, identifier: str, cog, user: discord.User):
        super().__init__(timeout=60)
        self.identifier = identifier
        self.cog = cog
        self.user = user
        
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user.id
        
    @discord.ui.button(label="Cancel Embed", style=discord.ButtonStyle.danger, custom_id="cancel_embed")
    async def confirm_cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.cancel_scheduled_embed_action(interaction, self.identifier)
        self.stop()
        
    @discord.ui.button(label="Post Now", style=discord.ButtonStyle.primary, custom_id="post_now")
    async def post_now(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.post_scheduled_embed_action(interaction, self.identifier)
        self.stop()
        
    @discord.ui.button(label="Keep Scheduled", style=discord.ButtonStyle.secondary, custom_id="keep_embed")
    async def keep_embed(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="✅ Embed will remain scheduled.", embeds=[], view=None)
        self.stop()
