import discord
from discord.ext import commands, tasks
from discord import app_commands
import datetime
import logging
import pytz
import secrets
from collections import Counter

from services.database import db
from services.settings_service import settings_service
from utils.constants import TZ_MANILA

logger = logging.getLogger('mlbb_bot')

DIAMONDS_PER_WIN = 100  # MLBB Diamonds awarded per raffle slot
DEFAULT_WINNER_SLOTS = 25  # Configurable via settings: booster_raffle_slots


class BoosterRaffleCog(commands.Cog, name="Booster Raffle"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.weekly_raffle.start()
        
    def cog_unload(self):
        self.weekly_raffle.cancel()

    @tasks.loop(time=datetime.time(hour=8, minute=0, tzinfo=TZ_MANILA))
    async def weekly_raffle(self):
        """Executes automatically on Sunday at 8:00 AM UTC+8."""
        now = datetime.datetime.now(TZ_MANILA)
        if now.weekday() != 6:  # 0 is Monday, 6 is Sunday
            return

        # Skip if a raffle was already executed this ISO week
        # (e.g., admin used /force-booster-raffle earlier this week)
        # YEARWEEK(date, 1) uses ISO mode: Mon=start, Sun=end of week
        existing = await db.fetch_one('''
            SELECT COUNT(*) as cnt FROM booster_raffle_history
            WHERE YEARWEEK(won_at, 1) = YEARWEEK(CURRENT_DATE(), 1)
        ''')
        if existing and existing['cnt'] > 0:
            logger.info("Weekly auto-raffle skipped — already executed this ISO week (forced or prior auto).")
            return

        await self._execute_raffle(is_manual=False)

    @weekly_raffle.before_loop
    async def before_raffle(self):
        await self.bot.wait_until_ready()

    async def _get_target_slots(self) -> int:
        """Fetch configurable winner slot count (default 25)."""
        val = await settings_service.get_int("booster_raffle_slots")
        return val if val > 0 else DEFAULT_WINNER_SLOTS

    async def _execute_raffle(self, is_manual=False, target_channel=None, ignore_7day_rule=False):
        logger.info("Starting Weekly Booster Raffle execution...")
        
        target_slots = await self._get_target_slots()
        
        # 1. Fetch all currently active boosters with their active weights
        active_boosters = await db.fetch_all('''
            SELECT user_id, raffle_entries, boost_start_date 
            FROM users 
            WHERE boost_start_date IS NOT NULL AND raffle_entries > 0
        ''')
        
        if not active_boosters:
            logger.warning("No active boosters found for raffle.")
            return

        total_boosters = len(active_boosters)

        # 2. Fetch users who have won a NORMAL (non-excess) slot THIS calendar month
        won_normal_this_month = await db.fetch_all('''
            SELECT DISTINCT user_id 
            FROM booster_raffle_history 
            WHERE MONTH(won_at) = MONTH(CURRENT_DATE()) 
              AND YEAR(won_at) = YEAR(CURRENT_DATE())
              AND is_excess = FALSE
        ''')
        won_normal_ids = {row['user_id'] for row in won_normal_this_month}
        
        # 3. Fetch total excess wins per user THIS calendar month (for fairness prioritization)
        excess_this_month = await db.fetch_all('''
            SELECT user_id, COUNT(*) as excess_count
            FROM booster_raffle_history
            WHERE MONTH(won_at) = MONTH(CURRENT_DATE())
              AND YEAR(won_at) = YEAR(CURRENT_DATE())
              AND is_excess = TRUE
            GROUP BY user_id
        ''')
        excess_count_map = {row['user_id']: row['excess_count'] for row in excess_this_month}
        
        pool_a = []  # Priority: hasn't won this month + boosting >= 7 days
        pool_b = []  # Everyone else
        
        now = datetime.datetime.now(TZ_MANILA)
        cutoff_7_days = now - datetime.timedelta(days=7)
        
        for b in active_boosters:
            uid = b['user_id']
            
            # Convert MySQL datetime to tz-aware
            start_date = b['boost_start_date']
            if start_date.tzinfo is None:
                start_date = pytz.utc.localize(start_date).astimezone(TZ_MANILA)
                
            has_won = uid in won_normal_ids
            joined_early_enough = start_date <= cutoff_7_days
            
            # Priority Pool: NOT won this month AND been boosting >= 7 days
            if not has_won and (joined_early_enough or ignore_7day_rule):
                pool_a.append(b)
            else:
                pool_b.append(b)
                
        # 4. Weighted unique selection (cryptographic randomness)
        def select_unique_winners(pool, needed_slots):
            winners = []
            tickets = []
            for booster in pool:
                tickets.extend([booster['user_id']] * booster['raffle_entries'])
                
            while len(winners) < needed_slots and len(tickets) > 0:
                winner = secrets.choice(tickets)
                winners.append(winner)
                # De-duplication: each booster can only occupy one normal slot
                tickets = [t for t in tickets if t != winner]
            
            return winners
            
        # 5. Draw normal winners
        remaining_slots = target_slots
        normal_winners = []
        
        winners_a = select_unique_winners(pool_a, remaining_slots)
        normal_winners.extend(winners_a)
        remaining_slots -= len(winners_a)
        
        if remaining_slots > 0:
            winners_b = select_unique_winners(pool_b, remaining_slots)
            normal_winners.extend(winners_b)
            remaining_slots -= len(winners_b)
            
        if not normal_winners:
            logger.warning("Raffle drew 0 winners despite having active boosters.")
            return

        # 6. Excess allocation: if fewer boosters than slots, distribute extras fairly
        # win_counts maps user_id -> total slot count (1 for normal + extras)
        win_counts = Counter(normal_winners)
        excess_winners = []  # list of user_ids receiving excess (can have duplicates)
        
        if remaining_slots > 0 and total_boosters > 0:
            # All boosters are already normal winners. Distribute remaining_slots as excess.
            all_booster_ids = [b['user_id'] for b in active_boosters]
            
            for _ in range(remaining_slots):
                # Sort eligible boosters by: (excess this month + excess this draw) ASC
                # Ties broken randomly via secrets.choice
                candidates = []
                for uid in all_booster_ids:
                    monthly_excess = excess_count_map.get(uid, 0)
                    draw_excess = excess_winners.count(uid)
                    total_excess = monthly_excess + draw_excess
                    candidates.append((uid, total_excess))
                
                # Find minimum excess count among candidates
                min_excess = min(c[1] for c in candidates)
                # All candidates tied at minimum excess
                tied = [uid for uid, count in candidates if count == min_excess]
                
                chosen = secrets.choice(tied)
                excess_winners.append(chosen)
                win_counts[chosen] += 1

        # 7. Record all wins to database
        for wid in normal_winners:
            try:
                await db.execute(
                    "INSERT INTO booster_raffle_history (user_id, is_excess) VALUES (%s, FALSE)", 
                    (wid,)
                )
            except Exception as e:
                logger.error(f"Failed to record normal winner {wid}: {e}")
        
        for wid in excess_winners:
            try:
                await db.execute(
                    "INSERT INTO booster_raffle_history (user_id, is_excess) VALUES (%s, TRUE)", 
                    (wid,)
                )
            except Exception as e:
                logger.error(f"Failed to record excess winner {wid}: {e}")
                
        # 8. Public Announcement
        await self._announce_winners(win_counts, total_boosters, target_slots, target_channel)

    async def _announce_winners(self, win_counts: Counter, total_boosters: int, target_slots: int, manual_target_channel=None):
        out_channel_id = await settings_service.get_int("boost_public_channel_id")
        channel = manual_target_channel
        
        if not channel:
            if out_channel_id:
                channel = self.bot.get_channel(out_channel_id) or await self.bot.fetch_channel(out_channel_id)
                
        if not channel:
            logger.warning("No boost_public_channel_id configured for raffle announcement. Aborting log.")
            return

        # Sort by total wins descending for visual clarity
        sorted_winners = sorted(win_counts.items(), key=lambda x: x[1], reverse=True)
        
        lines = []
        total_diamonds = 0
        has_excess = any(count > 1 for _, count in sorted_winners)
        
        for uid, count in sorted_winners:
            diamonds = count * DIAMONDS_PER_WIN
            total_diamonds += diamonds
            
            if count > 1:
                excess_count = count - 1
                lines.append(
                    f"🏆 <@{uid}> — **{diamonds} 💎** "
                    f"(1 win + {excess_count} excess)"
                )
            else:
                lines.append(f"🏆 <@{uid}> — **{diamonds} 💎**")
        
        description_parts = [
            f"Thank you to everyone who boosts the server!\n"
            f"Here are this week's **{len(sorted_winners)}** lucky winners "
            f"across **{target_slots}** prize slots:\n"
        ]
        
        # Add excess context if applicable
        if has_excess:
            description_parts.append(
                f"*Since we have {total_boosters} booster(s) for {target_slots} slots, "
                f"the remaining {target_slots - total_boosters} excess slot(s) have been "
                f"fairly distributed.*\n"
            )
        
        description_parts.append("\n".join(lines))
        description_parts.append(f"\n\n**Total Diamonds this week:** 💎 **{total_diamonds:,}**")
            
        embed = discord.Embed(
            title="✨ Weekly Booster Raffle Winners! ✨",
            description="\n".join(description_parts),
            color=0xFFD700,
            timestamp=datetime.datetime.now(TZ_MANILA)
        )
        embed.set_footer(text=f"{DIAMONDS_PER_WIN} 💎 per slot • May your light guide us through the cosmos.")
        
        # Resolve Server Booster role for ping
        booster_role_id = await settings_service.get_int("server_booster_role_id")
        role_ping = f"<@&{booster_role_id}>" if booster_role_id else ""
        
        try:
            await channel.send(
                content=f"{role_ping}\n🎉 Congratulations to our celestial ascended boosters!".strip(),
                embed=embed
            )
        except Exception as e:
            logger.error(f"Failed to send raffle announcement: {e}")

    # ── Diagnostic Command ─────────────────────────────────────

    @app_commands.command(
        name="booster-raffle-status",
        description="Diagnostic check for the automated booster raffle system (Admin only)"
    )
    @app_commands.default_permissions(administrator=True)
    async def raffle_status(self, interaction: discord.Interaction):
        """Show full raffle system health: config, booster count, week status, schedule."""
        await interaction.response.defer(ephemeral=True)
        
        checks = []
        all_ok = True
        
        # 1. Channel config
        channel_id = await settings_service.get_int("boost_public_channel_id")
        if channel_id:
            ch = self.bot.get_channel(channel_id)
            if ch:
                checks.append(f"✅ **Announcement Channel:** {ch.mention}")
            else:
                # Try fetching — might be uncached
                try:
                    ch = await self.bot.fetch_channel(channel_id)
                    checks.append(f"✅ **Announcement Channel:** {ch.mention} *(fetched)*")
                except Exception:
                    checks.append(f"❌ **Announcement Channel:** ID `{channel_id}` — **not found / inaccessible**")
                    all_ok = False
        else:
            checks.append("❌ **Announcement Channel:** Not configured — run `/setup channel boost_public <#channel>`")
            all_ok = False
        
        # 2. Booster role config
        role_id = await settings_service.get_int("server_booster_role_id")
        if role_id:
            guild = interaction.guild
            role = guild.get_role(role_id) if guild else None
            if role:
                checks.append(f"✅ **Server Booster Role:** {role.mention}")
            else:
                checks.append(f"⚠️ **Server Booster Role:** ID `{role_id}` — **role not found in server**")
        else:
            checks.append("⚠️ **Server Booster Role:** Not configured — role ping will be skipped. Run `/setup role server <@role>`")
        
        # 3. Winner slots config
        target_slots = await self._get_target_slots()
        checks.append(f"ℹ️ **Winner Slots:** `{target_slots}` per draw")
        
        # 4. Active boosters in DB
        booster_count = await db.fetch_one('''
            SELECT COUNT(*) as cnt FROM users 
            WHERE boost_start_date IS NOT NULL AND raffle_entries > 0
        ''')
        cnt = booster_count['cnt'] if booster_count else 0
        if cnt > 0:
            if cnt < target_slots:
                checks.append(f"✅ **Eligible Boosters:** `{cnt}` *(excess allocation will activate: {target_slots - cnt} extra slot(s))*")
            else:
                checks.append(f"✅ **Eligible Boosters:** `{cnt}`")
        else:
            checks.append("❌ **Eligible Boosters:** `0` — no boosters with `raffle_entries > 0` in DB")
            all_ok = False
        
        # 5. This-week raffle status (ISO week: Mon–Sun)
        existing = await db.fetch_one('''
            SELECT COUNT(*) as cnt, MIN(won_at) as first_at FROM booster_raffle_history
            WHERE YEARWEEK(won_at, 1) = YEARWEEK(CURRENT_DATE(), 1)
        ''')
        raffle_ran = existing and existing['cnt'] > 0
        
        now = datetime.datetime.now(TZ_MANILA)
        # Calculate ISO week boundaries for display
        iso_year, iso_week, iso_day = now.isocalendar()
        # Monday of this ISO week
        week_start = now - datetime.timedelta(days=iso_day - 1)
        week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
        # Sunday end of this ISO week
        week_end = week_start + datetime.timedelta(days=6, hours=23, minutes=59, seconds=59)
        
        week_start_unix = int(week_start.timestamp())
        week_end_unix = int(week_end.timestamp())
        
        checks.append(f"\n📅 **Current ISO Week {iso_week} ({iso_year}):**")
        checks.append(f"  <t:{week_start_unix}:D> (Mon) → <t:{week_end_unix}:D> (Sun)")
        
        if raffle_ran:
            first_at = existing['first_at']
            if first_at:
                if first_at.tzinfo is None:
                    first_at = pytz.utc.localize(first_at).astimezone(TZ_MANILA)
                ran_unix = int(first_at.timestamp())
                checks.append(f"  ✅ **Raffle already ran** — `{existing['cnt']}` records from <t:{ran_unix}:F>")
            else:
                checks.append(f"  ✅ **Raffle already ran** — `{existing['cnt']}` records this week")
            checks.append(f"  🚫 **Auto raffle will be SKIPPED** this Sunday (already executed)")
        else:
            checks.append(f"  ⏳ **No raffle yet this week** — auto raffle is active")
        
        # 6. Next scheduled auto raffle time
        # Next Sunday at 08:00 AM PHT
        days_until_sunday = (6 - now.weekday()) % 7
        if days_until_sunday == 0 and now.hour >= 8:
            # It's Sunday past 8 AM — next is next Sunday
            days_until_sunday = 7
        elif days_until_sunday == 0 and now.hour < 8:
            # It's Sunday before 8 AM — today
            days_until_sunday = 0
        
        next_sunday = now.replace(hour=8, minute=0, second=0, microsecond=0) + datetime.timedelta(days=days_until_sunday)
        next_unix = int(next_sunday.timestamp())
        
        checks.append(f"\n⏰ **Next Auto Raffle:**")
        checks.append(f"  <t:{next_unix}:F> — <t:{next_unix}:R>")
        
        if raffle_ran:
            checks.append(f"  *(Will be skipped — already ran this week)*")
        
        # 7. ISO week explanation
        checks.append(
            f"\n📖 **Week Definition:** ISO 8601 — Monday through Sunday. "
            f"A `/force-booster-raffle` on any day Mon–Sun prevents the "
            f"auto raffle from running on that same week's Sunday."
        )
        
        # Build embed
        status_emoji = "✅" if all_ok else "⚠️"
        embed = discord.Embed(
            title=f"{status_emoji} Booster Raffle System Status",
            description="\n".join(checks),
            color=0x00FF00 if all_ok else 0xFFAA00,
            timestamp=now
        )
        embed.set_footer(text="Booster Raffle Diagnostics")
        
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(BoosterRaffleCog(bot))
