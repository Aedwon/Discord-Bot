"""
Quest Management Cog - Admin-only CRUD interface for the quest definition catalog.
Provides /manage-quests with interactive Add, Edit, and Delete via modals and buttons.
"""

import discord
from discord.ext import commands
from discord import app_commands
import logging

from services.quest_service import (
    quest_service,
    QUEST_TIERS,
    QUEST_TASK_TYPES,
    TIER_DISPLAY,
    TASK_TYPE_DISPLAY,
)
from utils.checks import require_admin_auth

logger = logging.getLogger("mlbb_bot.quest_cog")

# Max quests per page (2 buttons per quest + 1 Add = 5 per action row × 5 rows = 25 max)
# Layout: each quest takes 1 action row with [Edit] [Delete], plus 1 row for [Add]
# Discord limit: 5 action rows → 4 quest rows + 1 control row = 4 quests per page
# Actually we can fit edit+delete in one row per quest, so 4 quests + 1 add row
QUESTS_PER_PAGE = 4


# ─── MODALS ─────────────────────────────────────────────────────────────


class QuestAddModal(discord.ui.Modal, title="➕ Create New Quest"):
    """Collects quest name, description, and target goal. Tier and task type
    are collected via a follow-up Select view (modals don't support dropdowns)."""

    quest_name = discord.ui.TextInput(
        label="Quest Name",
        placeholder="e.g. Chatterbox Challenge",
        required=True,
        max_length=100,
    )
    quest_description = discord.ui.TextInput(
        label="Description",
        style=discord.TextStyle.paragraph,
        placeholder="Describe what the user needs to do...",
        required=False,
        max_length=500,
    )
    quest_target = discord.ui.TextInput(
        label="Target Goal (number)",
        placeholder="e.g. 50",
        required=True,
        max_length=10,
    )

    async def on_submit(self, interaction: discord.Interaction):
        # Validate target goal is a positive integer
        try:
            target = int(self.quest_target.value.strip())
            if target <= 0:
                raise ValueError
        except ValueError:
            return await interaction.response.send_message(
                "❌ Target goal must be a positive number.",
                ephemeral=True,
            )

        # Proceed to tier + task type selection
        view = QuestAddSelectView(
            quest_name=self.quest_name.value.strip(),
            quest_description=(self.quest_description.value or "").strip(),
            target_goal=target,
            creator_id=interaction.user.id,
        )
        await interaction.response.send_message(
            "**Select the quest tier and task type:**",
            view=view,
            ephemeral=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        logger.error(f"QuestAddModal error: {error}")
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ An error occurred. Please try again.", ephemeral=True
                )
        except discord.HTTPException:
            pass


class QuestEditModal(discord.ui.Modal, title="✏️ Edit Quest"):
    """Pre-filled modal for editing quest name, description, and target goal."""

    quest_name = discord.ui.TextInput(
        label="Quest Name",
        required=True,
        max_length=100,
    )
    quest_description = discord.ui.TextInput(
        label="Description",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
    )
    quest_target = discord.ui.TextInput(
        label="Target Goal (number)",
        required=True,
        max_length=10,
    )

    def __init__(self, quest: dict):
        super().__init__()
        self.quest_id = quest["id"]
        self.quest_name.default = quest["name"]
        self.quest_description.default = quest.get("description") or ""
        self.quest_target.default = str(quest["target_goal"])

    async def on_submit(self, interaction: discord.Interaction):
        try:
            target = int(self.quest_target.value.strip())
            if target <= 0:
                raise ValueError
        except ValueError:
            return await interaction.response.send_message(
                "❌ Target goal must be a positive number.",
                ephemeral=True,
            )

        # Show tier + task type selects (pre-selected via follow-up)
        quest = await quest_service.get_quest(self.quest_id)
        if not quest:
            return await interaction.response.send_message(
                "❌ Quest no longer exists.", ephemeral=True
            )

        view = QuestEditSelectView(
            quest_id=self.quest_id,
            quest_name=self.quest_name.value.strip(),
            quest_description=(self.quest_description.value or "").strip(),
            target_goal=target,
            current_tier=quest["tier"],
            current_task_type=quest["task_type"],
        )
        await interaction.response.send_message(
            "**Update tier and task type (or keep current):**",
            view=view,
            ephemeral=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        logger.error(f"QuestEditModal error: {error}")
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ An error occurred. Please try again.", ephemeral=True
                )
        except discord.HTTPException:
            pass


# ─── SELECT VIEWS (Tier + Task Type) ────────────────────────────────────


class QuestAddSelectView(discord.ui.View):
    """Follow-up view after the Add modal — collects tier and task type via selects."""

    def __init__(self, quest_name: str, quest_description: str, target_goal: int, creator_id: int):
        super().__init__(timeout=120)
        self.quest_name = quest_name
        self.quest_description = quest_description
        self.target_goal = target_goal
        self.creator_id = creator_id
        self.selected_tier = None
        self.selected_task_type = None

    @discord.ui.select(
        placeholder="Select quest tier...",
        options=[
            discord.SelectOption(
                label=TIER_DISPLAY[t]["label"],
                value=t,
                emoji=TIER_DISPLAY[t]["emoji"],
            )
            for t in QUEST_TIERS
        ],
        row=0,
    )
    async def tier_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.selected_tier = select.values[0]
        await interaction.response.defer()

    @discord.ui.select(
        placeholder="Select task type...",
        options=[
            discord.SelectOption(
                label=TASK_TYPE_DISPLAY[t]["label"],
                value=t,
                description=f"Track {TASK_TYPE_DISPLAY[t]['unit']}",
            )
            for t in QUEST_TASK_TYPES
        ],
        row=1,
    )
    async def task_type_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.selected_task_type = select.values[0]
        await interaction.response.defer()

    @discord.ui.button(label="Create Quest", style=discord.ButtonStyle.success, emoji="✅", row=2)
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.selected_tier:
            return await interaction.response.send_message("❌ Please select a tier.", ephemeral=True)
        if not self.selected_task_type:
            return await interaction.response.send_message("❌ Please select a task type.", ephemeral=True)

        try:
            quest_id = await quest_service.create_quest(
                name=self.quest_name,
                description=self.quest_description,
                tier=self.selected_tier,
                task_type=self.selected_task_type,
                target_goal=self.target_goal,
                created_by=self.creator_id,
            )

            tier_info = TIER_DISPLAY[self.selected_tier]
            task_info = TASK_TYPE_DISPLAY[self.selected_task_type]

            embed = discord.Embed(
                title="✅ Quest Created",
                description=(
                    f"**{self.quest_name}**\n"
                    f"{tier_info['emoji']} {tier_info['label']} │ "
                    f"`{self.target_goal} {task_info['unit']}` │ {self.selected_task_type}\n\n"
                    f"*{self.quest_description or 'No description'}*"
                ),
                color=discord.Color.green(),
            )
            embed.set_footer(text=f"Quest ID: {quest_id}")

            # Disable all controls
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(content=None, embed=embed, view=self)

        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        except Exception as e:
            logger.error(f"Failed to create quest: {e}")
            await interaction.response.send_message("❌ Failed to create quest. Check logs.", ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=2)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="❌ Quest creation cancelled.", view=self)


class QuestEditSelectView(discord.ui.View):
    """Follow-up view after the Edit modal — tier and task type selects with current values."""

    def __init__(
        self,
        quest_id: int,
        quest_name: str,
        quest_description: str,
        target_goal: int,
        current_tier: str,
        current_task_type: str,
    ):
        super().__init__(timeout=120)
        self.quest_id = quest_id
        self.quest_name = quest_name
        self.quest_description = quest_description
        self.target_goal = target_goal
        self.selected_tier = current_tier
        self.selected_task_type = current_task_type

        # Build tier select with current value pre-selected
        tier_options = [
            discord.SelectOption(
                label=TIER_DISPLAY[t]["label"],
                value=t,
                emoji=TIER_DISPLAY[t]["emoji"],
                default=(t == current_tier),
            )
            for t in QUEST_TIERS
        ]
        self.tier_select_menu = discord.ui.Select(
            placeholder="Select quest tier...",
            options=tier_options,
            row=0,
        )
        self.tier_select_menu.callback = self._tier_callback
        self.add_item(self.tier_select_menu)

        # Build task type select with current value pre-selected
        task_options = [
            discord.SelectOption(
                label=TASK_TYPE_DISPLAY[t]["label"],
                value=t,
                description=f"Track {TASK_TYPE_DISPLAY[t]['unit']}",
                default=(t == current_task_type),
            )
            for t in QUEST_TASK_TYPES
        ]
        self.task_select_menu = discord.ui.Select(
            placeholder="Select task type...",
            options=task_options,
            row=1,
        )
        self.task_select_menu.callback = self._task_callback
        self.add_item(self.task_select_menu)

    async def _tier_callback(self, interaction: discord.Interaction):
        self.selected_tier = self.tier_select_menu.values[0]
        await interaction.response.defer()

    async def _task_callback(self, interaction: discord.Interaction):
        self.selected_task_type = self.task_select_menu.values[0]
        await interaction.response.defer()

    @discord.ui.button(label="Save Changes", style=discord.ButtonStyle.success, emoji="💾", row=2)
    async def save_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            success = await quest_service.update_quest(
                self.quest_id,
                name=self.quest_name,
                description=self.quest_description,
                tier=self.selected_tier,
                task_type=self.selected_task_type,
                target_goal=self.target_goal,
            )

            if not success:
                return await interaction.response.send_message(
                    "❌ Quest not found or no changes made.", ephemeral=True
                )

            tier_info = TIER_DISPLAY[self.selected_tier]
            task_info = TASK_TYPE_DISPLAY[self.selected_task_type]

            embed = discord.Embed(
                title="✅ Quest Updated",
                description=(
                    f"**{self.quest_name}**\n"
                    f"{tier_info['emoji']} {tier_info['label']} │ "
                    f"`{self.target_goal} {task_info['unit']}` │ {self.selected_task_type}\n\n"
                    f"*{self.quest_description or 'No description'}*"
                ),
                color=discord.Color.green(),
            )
            embed.set_footer(text=f"Quest ID: {self.quest_id}")

            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(content=None, embed=embed, view=self)

        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        except Exception as e:
            logger.error(f"Failed to update quest #{self.quest_id}: {e}")
            await interaction.response.send_message("❌ Failed to update quest.", ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=2)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="❌ Edit cancelled.", view=self)


# ─── QUEST LIST VIEW ────────────────────────────────────────────────────


class QuestManageView(discord.ui.View):
    """Main management view showing active quests with Edit/Delete buttons per quest,
    plus an Add button and pagination if needed."""

    def __init__(self, quests: list[dict], page: int = 0):
        super().__init__(timeout=300)
        self.all_quests = quests
        self.page = page
        self.total_pages = max(1, (len(quests) + QUESTS_PER_PAGE - 1) // QUESTS_PER_PAGE)
        self._build_buttons()

    def _build_buttons(self):
        """Dynamically build Edit/Delete buttons for quests on the current page."""
        start = self.page * QUESTS_PER_PAGE
        end = start + QUESTS_PER_PAGE
        page_quests = self.all_quests[start:end]

        # Add Edit/Delete buttons for each quest (1 action row per quest)
        for i, quest in enumerate(page_quests):
            edit_btn = discord.ui.Button(
                label=f"Edit: {quest['name'][:30]}",
                style=discord.ButtonStyle.primary,
                emoji="✏️",
                custom_id=f"quest_edit_{quest['id']}",
                row=i,
            )
            edit_btn.callback = self._make_edit_callback(quest["id"])
            self.add_item(edit_btn)

            delete_btn = discord.ui.Button(
                label="Delete",
                style=discord.ButtonStyle.danger,
                emoji="🗑️",
                custom_id=f"quest_delete_{quest['id']}",
                row=i,
            )
            delete_btn.callback = self._make_delete_callback(quest["id"], quest["name"])
            self.add_item(delete_btn)

        # Control row (last row) — row index = len(page_quests)
        control_row = min(len(page_quests), 4)

        add_btn = discord.ui.Button(
            label="Add Quest",
            style=discord.ButtonStyle.success,
            emoji="➕",
            custom_id="quest_add",
            row=control_row,
        )
        add_btn.callback = self._add_callback
        self.add_item(add_btn)

        # Pagination buttons (if needed)
        if self.total_pages > 1:
            prev_btn = discord.ui.Button(
                label="Prev",
                style=discord.ButtonStyle.secondary,
                emoji="◀️",
                custom_id="quest_prev",
                disabled=(self.page == 0),
                row=control_row,
            )
            prev_btn.callback = self._prev_callback
            self.add_item(prev_btn)

            next_btn = discord.ui.Button(
                label="Next",
                style=discord.ButtonStyle.secondary,
                emoji="▶️",
                custom_id="quest_next",
                disabled=(self.page >= self.total_pages - 1),
                row=control_row,
            )
            next_btn.callback = self._next_callback
            self.add_item(next_btn)

    def _make_edit_callback(self, quest_id: int):
        async def callback(interaction: discord.Interaction):
            quest = await quest_service.get_quest(quest_id)
            if not quest:
                return await interaction.response.send_message(
                    "❌ Quest no longer exists.", ephemeral=True
                )
            await interaction.response.send_modal(QuestEditModal(quest))

        return callback

    def _make_delete_callback(self, quest_id: int, quest_name: str):
        async def callback(interaction: discord.Interaction):
            view = QuestDeleteConfirmView(quest_id, quest_name, interaction.message)
            await interaction.response.send_message(
                f"⚠️ Are you sure you want to **permanently delete** quest **{quest_name}**?",
                view=view,
                ephemeral=True,
            )

        return callback

    async def _add_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(QuestAddModal())

    async def _prev_callback(self, interaction: discord.Interaction):
        await _refresh_quest_list(interaction, self.page - 1)

    async def _next_callback(self, interaction: discord.Interaction):
        await _refresh_quest_list(interaction, self.page + 1)


class QuestDeleteConfirmView(discord.ui.View):
    """Confirmation view for quest deletion."""

    def __init__(self, quest_id: int, quest_name: str, original_message: discord.Message):
        super().__init__(timeout=30)
        self.quest_id = quest_id
        self.quest_name = quest_name
        self.original_message = original_message

    @discord.ui.button(label="Confirm Delete", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        deleted = await quest_service.delete_quest(self.quest_id)

        if deleted:
            # Disable buttons
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(
                content=f"✅ Quest **{self.quest_name}** has been deleted.",
                view=self,
            )
            # Refresh the main quest list
            await _refresh_quest_list_message(self.original_message)
        else:
            await interaction.response.edit_message(
                content="❌ Quest was already deleted.",
                view=None,
            )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="❌ Deletion cancelled.", view=self)


# ─── HELPERS ────────────────────────────────────────────────────────────


def _build_quest_embed(quests: list[dict], page: int = 0) -> discord.Embed:
    """Build the quest list embed for a given page."""
    total_pages = max(1, (len(quests) + QUESTS_PER_PAGE - 1) // QUESTS_PER_PAGE)
    start = page * QUESTS_PER_PAGE
    end = start + QUESTS_PER_PAGE
    page_quests = quests[start:end]

    embed = discord.Embed(
        title="📋 Quest Catalog",
        color=discord.Color.blue(),
    )

    if not quests:
        embed.description = (
            "*No quests have been created yet.*\n\n"
            "Click **➕ Add Quest** below to create one."
        )
        return embed

    lines = []
    for quest in page_quests:
        line = quest_service.format_quest_line(quest)
        desc_preview = ""
        if quest.get("description"):
            desc_preview = f"\n> *{quest['description'][:80]}{'...' if len(quest.get('description', '')) > 80 else ''}*"
        lines.append(f"**{quest['name']}**\n{line}{desc_preview}")

    embed.description = "\n\n".join(lines)

    if total_pages > 1:
        embed.set_footer(text=f"Page {page + 1}/{total_pages} • {len(quests)} total quests")
    else:
        embed.set_footer(text=f"{len(quests)} active quest{'s' if len(quests) != 1 else ''}")

    return embed


async def _refresh_quest_list(interaction: discord.Interaction, page: int = 0):
    """Refresh the quest list embed and view (for pagination or after edits)."""
    quests = await quest_service.get_active_quests()
    total_pages = max(1, (len(quests) + QUESTS_PER_PAGE - 1) // QUESTS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))

    embed = _build_quest_embed(quests, page)
    view = QuestManageView(quests, page)
    await interaction.response.edit_message(embed=embed, view=view)


async def _refresh_quest_list_message(message: discord.Message, page: int = 0):
    """Refresh the quest list by editing the original message directly (no interaction)."""
    try:
        quests = await quest_service.get_active_quests()
        total_pages = max(1, (len(quests) + QUESTS_PER_PAGE - 1) // QUESTS_PER_PAGE)
        page = max(0, min(page, total_pages - 1))

        embed = _build_quest_embed(quests, page)
        view = QuestManageView(quests, page)
        await message.edit(embed=embed, view=view)
    except discord.HTTPException as e:
        logger.error(f"Failed to refresh quest list: {e}")


# ─── COG ────────────────────────────────────────────────────────────────


class QuestCog(commands.Cog, name="Quests"):
    """Admin quest catalog management."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="manage-quests",
        description="View and manage the quest definition catalog (Admin only)",
    )
    @require_admin_auth()
    @app_commands.default_permissions(administrator=True)
    async def manage_quests(self, interaction: discord.Interaction):
        """Display the quest catalog with CRUD controls."""
        await interaction.response.defer(ephemeral=True)

        quests = await quest_service.get_active_quests()
        embed = _build_quest_embed(quests)
        view = QuestManageView(quests)

        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(QuestCog(bot))
