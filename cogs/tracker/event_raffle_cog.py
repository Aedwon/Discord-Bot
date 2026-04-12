"""
Event Raffle Cog — Core raffle logic, persistent views, and auto-draw task.
Slash commands are registered as a subgroup on EventCog in event_cog.py.
This cog provides the engine: draw logic, persistent button handlers, and the background task.
"""

import discord
import secrets
from datetime import datetime, timezone
from discord.ext import commands, tasks
from discord import app_commands
import json
import logging

from services.database import db
from services.verification_service import verification_service

logger = logging.getLogger("mlbb_bot.event_raffle")


# ─── PERSISTENT VIEWS ──────────────────────────────────────────────────

class PersistentRaffleView(discord.ui.View):
    """Persistent Join/Leave buttons. Re-registered on bot restart."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Join Raffle", style=discord.ButtonStyle.success,
        custom_id="event_raffle:join", emoji="🎟️"
    )
    async def join_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        raffle = await db.fetch_one(
            "SELECT * FROM event_raffles WHERE message_id = %s AND status = 'active'",
            (interaction.message.id,)
        )
        if not raffle:
            return await interaction.followup.send("❌ This raffle is no longer active.", ephemeral=True)

        # Check if already joined
        existing = await db.fetch_one(
            "SELECT 1 FROM event_raffle_entries WHERE raffle_id = %s AND user_id = %s",
            (raffle['id'], interaction.user.id)
        )
        if existing:
            return await interaction.followup.send("⚠️ You've already joined this raffle!", ephemeral=True)

        # Record entry
        await db.execute(
            "INSERT INTO event_raffle_entries (raffle_id, user_id) VALUES (%s, %s) "
            "ON DUPLICATE KEY UPDATE raffle_id = raffle_id",
            (raffle['id'], interaction.user.id)
        )

        # If raffle has requirements & proof thread, add user to it
        if raffle['requirements'] and raffle['proof_thread_id']:
            try:
                thread = interaction.guild.get_thread(raffle['proof_thread_id'])
                if not thread:
                    thread = await interaction.guild.fetch_channel(raffle['proof_thread_id'])
                if thread:
                    await thread.add_user(interaction.user)
                    await thread.send(
                        f"📸 {interaction.user.mention} — Please post your proof screenshot below "
                        f"to confirm you've completed the requirements."
                    )
            except Exception as e:
                logger.warning(f"Could not add user to proof thread: {e}")

        # Update embed with new participant count
        count = await db.fetch_one(
            "SELECT COUNT(*) as total FROM event_raffle_entries WHERE raffle_id = %s",
            (raffle['id'],)
        )
        total = count['total'] if count else 0

        try:
            embed = interaction.message.embeds[0]
            for i, field in enumerate(embed.fields):
                if field.name == "Participants":
                    embed.set_field_at(i, name="Participants", value=f"**{total}**", inline=True)
                    break
            await interaction.message.edit(embed=embed)
        except Exception as e:
            logger.warning(f"Could not update raffle embed: {e}")

        await interaction.followup.send("✅ You've joined the raffle! Good luck! 🍀", ephemeral=True)

    @discord.ui.button(
        label="Leave", style=discord.ButtonStyle.secondary,
        custom_id="event_raffle:leave", emoji="❌"
    )
    async def leave_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        raffle = await db.fetch_one(
            "SELECT * FROM event_raffles WHERE message_id = %s AND status = 'active'",
            (interaction.message.id,)
        )
        if not raffle:
            return await interaction.followup.send("❌ This raffle is no longer active.", ephemeral=True)

        result = await db.execute(
            "DELETE FROM event_raffle_entries WHERE raffle_id = %s AND user_id = %s",
            (raffle['id'], interaction.user.id)
        )
        if not result:
            return await interaction.followup.send("⚠️ You weren't in this raffle.", ephemeral=True)

        # Update embed participant count
        count = await db.fetch_one(
            "SELECT COUNT(*) as total FROM event_raffle_entries WHERE raffle_id = %s",
            (raffle['id'],)
        )
        total = count['total'] if count else 0

        try:
            embed = interaction.message.embeds[0]
            for i, field in enumerate(embed.fields):
                if field.name == "Participants":
                    embed.set_field_at(i, name="Participants", value=f"**{total}**", inline=True)
                    break
            await interaction.message.edit(embed=embed)
        except Exception:
            pass

        await interaction.followup.send("👋 You've left the raffle.", ephemeral=True)


# ─── SERVICE COG ────────────────────────────────────────────────────────

class EventRaffleCog(commands.Cog, name="Event Raffle"):
    """Core raffle engine: persistent views, auto-draw task, draw logic."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_view(PersistentRaffleView())

    def cog_unload(self):
        self.auto_draw_loop.cancel()

    # ─── CRASH RECOVERY ─────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.auto_draw_loop.is_running():
            self.auto_draw_loop.start()
            
        """Draw any raffles that expired while the bot was offline."""
        try:
            overdue = await db.fetch_all(
                "SELECT * FROM event_raffles "
                "WHERE status = 'active' AND ends_at IS NOT NULL AND ends_at <= UTC_TIMESTAMP()"
            )
            if overdue:
                logger.info(f"Crash recovery: found {len(overdue)} overdue raffle(s) to draw")
                guild = self.bot.guilds[0] if self.bot.guilds else None
                if guild:
                    for raffle in overdue:
                        try:
                            await self.execute_draw(raffle, guild)
                            logger.info(f"Crash recovery: drew raffle #{raffle['id']} '{raffle['title']}'")
                        except Exception as e:
                            logger.error(f"Crash recovery: failed to draw raffle #{raffle['id']}: {e}")
        except Exception as e:
            logger.error(f"Crash recovery check failed: {e}")

    # ─── AUTO-DRAW TASK ─────────────────────────────────────────────────

    @tasks.loop(seconds=30)
    async def auto_draw_loop(self):
        """Check for raffles that need auto-drawing."""
        try:
            # Use UTC_TIMESTAMP() to correctly compare against UTC-stored ends_at
            due = await db.fetch_all(
                "SELECT * FROM event_raffles "
                "WHERE status = 'active' AND ends_at IS NOT NULL AND ends_at <= UTC_TIMESTAMP()"
            )
            for raffle in due:
                guild = self.bot.guilds[0] if self.bot.guilds else None
                if guild:
                    try:
                        await self.execute_draw(raffle, guild)
                        logger.info(f"Auto-draw: drew raffle #{raffle['id']} '{raffle['title']}'")
                    except Exception as e:
                        logger.error(f"Auto-draw: failed to draw raffle #{raffle['id']}: {e}")
        except Exception as e:
            logger.error(f"Auto-draw loop error: {e}")

    @auto_draw_loop.before_loop
    async def before_auto_draw(self):
        await self.bot.wait_until_ready()

    # ─── DRAW ENGINE ────────────────────────────────────────────────────

    async def execute_draw(self, raffle: dict, guild: discord.Guild):
        """Core draw logic — used by both manual draw and auto-draw."""
        raffle_id = raffle['id']

        entries = await db.fetch_all(
            "SELECT user_id FROM event_raffle_entries WHERE raffle_id = %s",
            (raffle_id,)
        )

        channel = guild.get_channel(raffle['channel_id'])
        if not channel:
            try:
                channel = await guild.fetch_channel(raffle['channel_id'])
            except Exception:
                logger.error(f"Cannot find channel for raffle #{raffle_id}")
                # Still mark as drawn so we don't retry forever
                await db.execute(
                    "UPDATE event_raffles SET status = 'drawn', winners = '' WHERE id = %s",
                    (raffle_id,)
                )
                return

        if not entries:
            await db.execute(
                "UPDATE event_raffles SET status = 'drawn', winners = '' WHERE id = %s",
                (raffle_id,)
            )
            try:
                msg = await channel.fetch_message(raffle['message_id'])
                embed = msg.embeds[0]
                embed.color = 0x95A5A6
                embed.set_footer(text="No participants — raffle ended with no winners.")
                await msg.edit(embed=embed, view=None)
            except Exception:
                pass
            await channel.send(f"📭 Raffle **{raffle['title']}** ended with no participants.")
            return

        # Select winners with cryptographic randomness
        participant_ids = [e['user_id'] for e in entries]
        
        # Determine MSL members and exclude them from the pool
        placeholders = ",".join(["%s"] * len(participant_ids))
        verified_rows = await db.fetch_all(
            f"SELECT user_id, mlbb_uid, mlbb_server FROM verified_users WHERE user_id IN ({placeholders})",
            tuple(participant_ids)
        )
        msl_users = set()
        for r in verified_rows:
            if verification_service.is_msl(r['mlbb_uid'], r['mlbb_server']):
                msl_users.add(r['user_id'])
                
        pool = [uid for uid in participant_ids if uid not in msl_users]
        winner_count = min(raffle['winner_count'], len(pool))

        winners = []
        for _ in range(winner_count):
            winner = secrets.choice(pool)
            winners.append(winner)
            pool.remove(winner)

        winner_str = ",".join(str(w) for w in winners)
        winner_mentions = " ".join(f"<@{w}>" for w in winners)

        await db.execute(
            "UPDATE event_raffles SET status = 'drawn', winners = %s WHERE id = %s",
            (winner_str, raffle_id)
        )

        # Update original embed
        try:
            msg = await channel.fetch_message(raffle['message_id'])
            embed = msg.embeds[0]
            embed.color = 0x2ECC71
            embed.title = f"🏆 {raffle['title']} — Winners Drawn!"
            embed.add_field(
                name="🎉 Winners",
                value=winner_mentions,
                inline=False
            )
            embed.set_footer(text=f"Drawn from {len(participant_ids)} participants")
            await msg.edit(embed=embed, view=None)
        except Exception as e:
            logger.warning(f"Could not update drawn raffle embed: {e}")

        # Announcement
        announcement_msg = await channel.send(
            f"🎉 **{raffle['title']}** raffle results!\n\n"
            f"🏆 Congratulations to: {winner_mentions}\n\n"
            f"*You've been added to a private thread below for prize coordination.*"
        )

        # Create private winner thread
        try:
            host_mention = f"<@{raffle['hosted_by']}>" if raffle['hosted_by'] else f"<@{raffle['host_id']}>"

            winner_thread = await channel.create_thread(
                name=f"🏆 Winners: {raffle['title'][:80]}",
                type=discord.ChannelType.private_thread,
                auto_archive_duration=4320
            )
            welcome_msg = await winner_thread.send(
                f"🎉 **Congratulations!** You've won the **{raffle['title']}** raffle!\n\n"
                f"**Prize:** {raffle['prize']}\n"
                f"**Host:** {host_mention}\n\n"
                f"Please coordinate with the host here for prize delivery.\n\n"
                f"Winners: {winner_mentions}"
            )

            for wid in winners:
                try:
                    member = guild.get_member(wid)
                    if member:
                        await winner_thread.add_user(member)
                except Exception:
                    pass

            host_id = raffle['hosted_by'] or raffle['host_id']
            host_member = guild.get_member(host_id)
            if host_member:
                await winner_thread.add_user(host_member)
                
            await db.execute(
                "UPDATE event_raffles SET winners_thread_id = %s, announcement_msg_id = %s, welcome_msg_id = %s WHERE id = %s", 
                (winner_thread.id, announcement_msg.id, welcome_msg.id, raffle_id)
            )

        except Exception as e:
            logger.error(f"Failed to create winner thread: {e}")

    async def execute_reroll(self, raffle: dict, disqualified_user: discord.Member, reason: str, guild: discord.Guild, interaction: discord.Interaction):
        """Rerolls an ended raffle. If disqualified_user is provided, replaces only them. Otherwise, full reroll."""
        raffle_id = raffle['id']
        current_winners_str = raffle.get('winners', "")
        current_winners = [int(w) for w in current_winners_str.split(",")] if current_winners_str else []

        # Target replacement logic
        if disqualified_user:
            if disqualified_user.id not in current_winners:
                return await interaction.followup.send(f"❌ {disqualified_user.mention} is not one of the current winners.", ephemeral=True)
            
            # Fetch all participants
            entries = await db.fetch_all("SELECT user_id FROM event_raffle_entries WHERE raffle_id = %s", (raffle_id,))
            pool = [e['user_id'] for e in entries]

            # Exclude ALL current valid winners from the pool
            for w in current_winners:
                if w in pool:
                    pool.remove(w)
            
            if not pool:
                return await interaction.followup.send("❌ Cannot reroll: there are no other participants left in the pool to take their place.", ephemeral=True)

            new_winner_id = secrets.choice(pool)
            
            # Swap them
            new_winners_list = current_winners.copy()
            new_winners_list.remove(disqualified_user.id)
            new_winners_list.append(new_winner_id)
            
            new_winner_str = ",".join(str(w) for w in new_winners_list)
            
            # Target Replacement Logic Data Updates
            history_str = raffle.get('reroll_history')
            history = json.loads(history_str) if history_str else []
            history.append({
                "type": "targeted",
                "replaced_name": disqualified_user.display_name,
                "new_id": new_winner_id,
                "reason": reason
            })
            history_json = json.dumps(history)
            
            await db.execute("UPDATE event_raffles SET winners = %s, reroll_history = %s WHERE id = %s", (new_winner_str, history_json, raffle_id))
            
            # Format ledger
            ledger_lines = []
            for item in history:
                if item.get('type') == 'targeted':
                    ledger_lines.append(f"- <@{item['new_id']}> replaced **{item['replaced_name']}** *(Reason: {item['reason']})*")
                else:
                    ledger_lines.append(f"- **Full Reroll Executed** *(Reason: {item['reason']})*")
                    
            ledger_text = "\n\n---\n**Rerolls History:**\n" + "\n".join(ledger_lines) if ledger_lines else ""
            
            channel = guild.get_channel(raffle['channel_id'])
            if channel:
                # Update main Embed
                try:
                    msg = await channel.fetch_message(raffle['message_id'])
                    embed = msg.embeds[0]
                    winner_mentions = " ".join(f"<@{w}>" for w in new_winners_list)
                    
                    for i, field in enumerate(embed.fields):
                        if getattr(field, 'name', None) == "🎉 Winners":
                            embed.set_field_at(i, name="🎉 Winners", value=winner_mentions, inline=False)
                    await msg.edit(embed=embed)
                except Exception as e:
                    logger.warning(f"Could not update rerolled embed: {e}")

                # Update Original Public Announcement (Silent)
                announcement_msg_id = raffle.get('announcement_msg_id')
                if announcement_msg_id:
                    try:
                        announcement_msg = await channel.fetch_message(announcement_msg_id)
                        await announcement_msg.edit(content=
                            f"🎉 **{raffle['title']}** raffle results!\n\n"
                            f"🏆 Congratulations to: {winner_mentions}\n\n"
                            f"*You've been added to a private thread below for prize coordination.*{ledger_text}"
                        )
                    except Exception:
                        pass
                
                # Update Winners Thread
                if raffle.get('winners_thread_id'):
                    thread = guild.get_thread(raffle.get('winners_thread_id'))
                    if thread:
                        # Update thread welcome message (Silent)
                        welcome_msg_id = raffle.get('welcome_msg_id')
                        if welcome_msg_id:
                            try:
                                welcome_msg = await thread.fetch_message(welcome_msg_id)
                                host_id = raffle['hosted_by'] or raffle['host_id']
                                host_mention = f"<@{host_id}>"
                                await welcome_msg.edit(content=
                                    f"🎉 **Congratulations!** You've won the **{raffle['title']}** raffle!\n\n"
                                    f"**Prize:** {raffle['prize']}\n"
                                    f"**Host:** {host_mention}\n\n"
                                    f"Please coordinate with the host here for prize delivery.\n\n"
                                    f"Winners: {winner_mentions}{ledger_text}"
                                )
                            except Exception:
                                pass

                        # Attempt to remove disqualified
                        try:
                            member_to_remove = guild.get_member(disqualified_user.id)
                            if member_to_remove:
                                await thread.remove_user(member_to_remove)
                        except Exception:
                            pass
                        
                        # Add new winner
                        new_winner_name = f"User {new_winner_id}"
                        try:
                            new_member = guild.get_member(new_winner_id)
                            if new_member:
                                await thread.add_user(new_member)
                                new_winner_name = new_member.display_name
                        except Exception:
                            pass
                        
                        # Internal info message for transparency inside loop lounge
                        await thread.send(f"🎲 **Reroll:** **{disqualified_user.display_name}** was disqualified for: *{reason}*. Welcome **{new_winner_name}** to the winners lounge!")
                        
            await interaction.followup.send(f"✅ Replaced {disqualified_user.mention} with <@{new_winner_id}>. (Reroll logged silently).", ephemeral=True)

        else:
            # Full Reroll Logic Updates
            entries = await db.fetch_all("SELECT user_id FROM event_raffle_entries WHERE raffle_id = %s", (raffle_id,))
            pool = [e['user_id'] for e in entries]
            
            if not pool:
                return await interaction.followup.send("❌ Cannot perform full reroll: there are no participants.", ephemeral=True)
                
            winner_count = min(raffle['winner_count'], len(pool))
            new_winners_list = []
            for _ in range(winner_count):
                w = secrets.choice(pool)
                new_winners_list.append(w)
                pool.remove(w)
                
            new_winner_str = ",".join(str(w) for w in new_winners_list)
            
            history_str = raffle.get('reroll_history')
            history = json.loads(history_str) if history_str else []
            history.append({
                "type": "full",
                "reason": reason
            })
            history_json = json.dumps(history)
            
            await db.execute("UPDATE event_raffles SET winners = %s, reroll_history = %s WHERE id = %s", (new_winner_str, history_json, raffle_id))
            
            # Format ledger
            ledger_lines = []
            for item in history:
                if item.get('type') == 'targeted':
                    ledger_lines.append(f"- <@{item['new_id']}> replaced **{item['replaced_name']}** *(Reason: {item['reason']})*")
                else:
                    ledger_lines.append(f"- **Full Reroll Executed** *(Reason: {item['reason']})*")
                    
            ledger_text = "\n\n---\n**Rerolls History:**\n" + "\n".join(ledger_lines) if ledger_lines else ""
            
            channel = guild.get_channel(raffle['channel_id'])
            if channel:
                winner_mentions = " ".join(f"<@{w}>" for w in new_winners_list)
                
                # Update embed
                try:
                    msg = await channel.fetch_message(raffle['message_id'])
                    embed = msg.embeds[0]
                    for i, field in enumerate(embed.fields):
                        if getattr(field, 'name', None) == "🎉 Winners":
                            embed.set_field_at(i, name="🎉 Winners", value=winner_mentions, inline=False)
                    await msg.edit(embed=embed)
                except Exception:
                    pass
                    
                # Update Original Public Announcement (Silent)
                announcement_msg_id = raffle.get('announcement_msg_id')
                if announcement_msg_id:
                    try:
                        announcement_msg = await channel.fetch_message(announcement_msg_id)
                        await announcement_msg.edit(content=
                            f"🎉 **{raffle['title']}** raffle results!\n\n"
                            f"🏆 Congratulations to: {winner_mentions}\n\n"
                            f"*You've been added to a private thread below for prize coordination.*{ledger_text}"
                        )
                    except Exception:
                        pass
                
                # Update Winners Thread
                if raffle.get('winners_thread_id'):
                    thread = guild.get_thread(raffle.get('winners_thread_id'))
                    if thread:
                        # Update thread welcome message (Silent)
                        welcome_msg_id = raffle.get('welcome_msg_id')
                        if welcome_msg_id:
                            try:
                                welcome_msg = await thread.fetch_message(welcome_msg_id)
                                host_id = raffle['hosted_by'] or raffle['host_id']
                                host_mention = f"<@{host_id}>"
                                await welcome_msg.edit(content=
                                    f"🎉 **Congratulations!** You've won the **{raffle['title']}** raffle!\n\n"
                                    f"**Prize:** {raffle['prize']}\n"
                                    f"**Host:** {host_mention}\n\n"
                                    f"Please coordinate with the host here for prize delivery.\n\n"
                                    f"Winners: {winner_mentions}{ledger_text}"
                                )
                            except Exception:
                                pass

                        # Attempt to remove all old
                        for old_w in current_winners:
                            try:
                                member_to_remove = guild.get_member(old_w)
                                if member_to_remove:
                                    await thread.remove_user(member_to_remove)
                            except Exception:
                                pass
                                
                        # Add new
                        winner_names_list = []
                        for new_w in new_winners_list:
                            try:
                                new_member = guild.get_member(new_w)
                                if new_member:
                                    await thread.add_user(new_member)
                                    winner_names_list.append(f"**{new_member.display_name}**")
                                else:
                                    winner_names_list.append(f"**User {new_w}**")
                            except Exception:
                                winner_names_list.append(f"**User {new_w}**")
                                
                        winner_names_str = ", ".join(winner_names_list)
                        
                        await thread.send(f"🎲 **Full Reroll Executed!** Reason: *{reason}*. Previous winners were removed. Welcome the new winners: {winner_names_str}")
                        
            await interaction.followup.send("✅ Full reroll complete. (Reroll logged silently).", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(EventRaffleCog(bot))
