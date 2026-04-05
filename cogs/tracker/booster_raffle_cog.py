import discord
from discord.ext import commands, tasks
from discord import app_commands
import datetime
import logging
import pytz
import secrets
import csv
import io
from collections import Counter

from services.database import db
from services.settings_service import settings_service
from services.verification_service import verification_service
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

        # Explicitly filter out any verified MSL members from eligible boosters
        booster_ids = [b['user_id'] for b in active_boosters]
        placeholders = ",".join(["%s"] * len(booster_ids))
        verified_rows = await db.fetch_all(
            f"SELECT user_id, mlbb_uid, mlbb_server FROM verified_users WHERE user_id IN ({placeholders})",
            tuple(booster_ids)
        )
        msl_users = set()
        for r in verified_rows:
            if verification_service.is_msl(r['mlbb_uid'], r['mlbb_server']):
                msl_users.add(r['user_id'])
                
        active_boosters = [b for b in active_boosters if b['user_id'] not in msl_users]
        
        if not active_boosters:
            logger.warning("No active non-MSL boosters found after filtering.")
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
                    candidates.append((uid, draw_excess, monthly_excess))
                
                # Find minimum (draw_excess, monthly_excess)
                min_sort_key = min((c[1], c[2]) for c in candidates)
                # Filter candidates tied for absolute parity priority
                tied = [uid for uid, c_draw, c_month in candidates if (c_draw, c_month) == min_sort_key]
                
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
            
            user_obj = self.bot.get_user(uid)
            if not user_obj:
                try: user_obj = await self.bot.fetch_user(uid)
                except: pass
            name_disp = user_obj.display_name if user_obj else f"User {uid}"
            name_disp = name_disp.replace("*", "").replace("_", "").replace("`", "")
            
            if count > 1:
                excess_count = count - 1
                lines.append(f"🏆 **{name_disp}** — **{diamonds} 💎** (1 win + {excess_count} excess)")
            else:
                lines.append(f"🏆 **{name_disp}** — **{diamonds} 💎**")
        
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
        
        winner_pings = " ".join([f"<@{uid}>" for uid, _ in sorted_winners])
        
        try:
            await channel.send(
                content=f"{role_ping}\n🎉 Congratulations to our celestial ascended boosters!\n\n{winner_pings}".strip(),
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

    @app_commands.command(
        name="booster-raffle-export",
        description="Export the latest booster raffle winners to CSVs (Admin only)"
    )
    @app_commands.default_permissions(administrator=True)
    async def raffle_export(self, interaction: discord.Interaction):
        """Build MSL and Non-MSL CSVs for the most recent booster raffle draw."""
        await interaction.response.defer(ephemeral=True)
        
        # 1. Get the latest raffle execution date
        latest_record = await db.fetch_one('''
            SELECT MAX(DATE(won_at)) as latest_date 
            FROM booster_raffle_history
        ''')
        
        if not latest_record or not latest_record['latest_date']:
            return await interaction.followup.send("❌ No booster raffle history found.", ephemeral=True)
            
        latest_date = latest_record['latest_date']
        
        # 2. Fetch all wins from that date
        wins_records = await db.fetch_all('''
            SELECT user_id, COUNT(*) as total_wins 
            FROM booster_raffle_history 
            WHERE DATE(won_at) = %s
            GROUP BY user_id
        ''', (latest_date,))
        
        if not wins_records:
            return await interaction.followup.send("❌ No winners found for the latest raffle.", ephemeral=True)
            
        winner_ids = [r['user_id'] for r in wins_records]
        wins_map = {r['user_id']: r['total_wins'] for r in wins_records}
        
        # 3. Identify verification data for all winners
        placeholders = ",".join(["%s"] * len(winner_ids))
        verified_rows = await db.fetch_all(
            f"SELECT user_id, full_name, mlbb_uid, mlbb_server FROM verified_users WHERE user_id IN ({placeholders})",
            tuple(winner_ids)
        )
        verified_map = {rk['user_id']: rk for rk in verified_rows}
        
        unverified_ids = [wid for wid in winner_ids if wid not in verified_map]
        
        # Block export if any unverified users found
        if unverified_ids:
            pings = " ".join([f"<@{uid}>" for uid in unverified_ids])
            msg = (
                f"❌ **Export Blocked — Unverified Winners Detected.**\n"
                f"The following winners have not completed MLBB verification.\n\n"
                f"**Copy/Paste this to the booster channel:**\n"
                f"```\n"
                f"Please verify to claim your Server Booster Raffle rewards: {pings}\n"
                f"```"
            )
            return await interaction.followup.send(msg, ephemeral=True)
            
        # 4. Filter into MSL and Non-MSL arrays
        msl_list = []
        non_msl_list = []
        
        date_str = latest_date.strftime("%Y/%m/%d")
        remarks_str = f"MSL Network Discord - Server Booster Raffle - ({date_str})"
        
        for wid in winner_ids:
            v_info = verified_map[wid]
            uid = v_info['mlbb_uid']
            amount = wins_map[wid] * DIAMONDS_PER_WIN
            
            if verification_service.is_msl(uid):
                msl_nickname = verification_service.get_msl_nickname(uid)
                msl_list.append([
                    msl_nickname,
                    amount,
                    remarks_str
                ])
            else:
                non_msl_list.append([
                    v_info['full_name'],
                    uid,
                    v_info['mlbb_server'],
                    amount,
                    remarks_str
                ])
                
        # 5. Build attachments
        files = []
        file_date = date_str.replace('/', '-')
        
        if msl_list:
            msl_out = io.StringIO()
            msl_out.write('\ufeff') # UTF-8 BOM
            msl_writer = csv.writer(msl_out)
            msl_writer.writerow(["MSL Nickname", "Amount", "Remarks"])
            msl_writer.writerows(msl_list)
            msl_out.seek(0)
            files.append(
                discord.File(
                    fp=io.BytesIO(msl_out.getvalue().encode('utf-8-sig')), 
                    filename=f"msl_booster_raffle_{file_date}.csv"
                )
            )
            
        if non_msl_list:
            non_msl_out = io.StringIO()
            non_msl_out.write('\ufeff')
            non_msl_writer = csv.writer(non_msl_out)
            non_msl_writer.writerow(["Full Name", "UID", "Server", "Amount", "Remarks"])
            non_msl_writer.writerows(non_msl_list)
            non_msl_out.seek(0)
            files.append(
                discord.File(
                    fp=io.BytesIO(non_msl_out.getvalue().encode('utf-8-sig')), 
                    filename=f"non_msl_booster_raffle_{file_date}.csv"
                )
            )
            
        response_msg = (
            f"✅ Exported **{len(winner_ids)}** winners from the **{date_str}** draw.\n"
            f"Included **{len(msl_list)}** MSL members and **{len(non_msl_list)}** non-MSL members."
        )
        await interaction.followup.send(response_msg, files=files, ephemeral=True)

    @app_commands.command(
        name="booster-raffle-reroll-msl",
        description="Retroactively exclude MSL members from the latest draw and reallocate slots (Admin only)"
    )
    @app_commands.default_permissions(administrator=True)
    async def reroll_msl(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)

        # 1. Fetch latest draw timestamp
        latest_record = await db.fetch_one('''
            SELECT MAX(won_at) as latest_time 
            FROM booster_raffle_history
        ''')
        
        if not latest_record or not latest_record['latest_time']:
            return await interaction.followup.send("❌ No booster raffle history found.")
            
        latest_time = latest_record['latest_time']
        
        # 2. Fetch winners from that exact batch (within a 60 second window)
        wins_records = await db.fetch_all('''
            SELECT user_id, is_excess 
            FROM booster_raffle_history 
            WHERE won_at >= DATE_SUB(%s, INTERVAL 60 SECOND)
              AND won_at <= DATE_ADD(%s, INTERVAL 60 SECOND)
        ''', (latest_time, latest_time))
        
        if not wins_records:
            return await interaction.followup.send("❌ No winners found for the latest raffle.")
            
        winner_ids = list({r['user_id'] for r in wins_records})
        
        # 3. Identify MSL members among winners
        placeholders = ",".join(["%s"] * len(winner_ids))
        verified_rows = await db.fetch_all(
            f"SELECT user_id, mlbb_uid, mlbb_server FROM verified_users WHERE user_id IN ({placeholders})",
            tuple(winner_ids)
        )
        
        msl_winners = set()
        for r in verified_rows:
            if verification_service.is_msl(r['mlbb_uid'], r['mlbb_server']):
                msl_winners.add(r['user_id'])
                
        if not msl_winners:
            return await interaction.followup.send("✅ No MSL members won in the latest booster raffle draw.", ephemeral=True)
            
        # 4. Filter records strictly belonging to MSL winners to calculate stripped slots
        stripped_slots = 0
        for row in wins_records:
            if row['user_id'] in msl_winners:
                stripped_slots += 1
                    
        # 5. Delete their records from this specific batch
        await db.execute('''
            DELETE FROM booster_raffle_history 
            WHERE won_at >= DATE_SUB(%s, INTERVAL 60 SECOND)
              AND won_at <= DATE_ADD(%s, INTERVAL 60 SECOND)
              AND user_id IN %s
        ''', (latest_time, latest_time, tuple(msl_winners)))
        
        # 6. Fetch legitimate active boosters to distribute the stripped slots
        active_boosters = await db.fetch_all('''
            SELECT user_id, raffle_entries, boost_start_date 
            FROM users 
            WHERE boost_start_date IS NOT NULL AND raffle_entries > 0
        ''')
        
        # Filter active boosters
        booster_ids = [b['user_id'] for b in active_boosters]
        placeholders2 = ",".join(["%s"] * len(booster_ids))
        all_ver_rows = await db.fetch_all(
            f"SELECT user_id, mlbb_uid, mlbb_server FROM verified_users WHERE user_id IN ({placeholders2})",
            tuple(booster_ids)
        )
        msl_active = set()
        for r in all_ver_rows:
            if verification_service.is_msl(r['mlbb_uid'], r['mlbb_server']):
                msl_active.add(r['user_id'])
                
        pool = [b for b in active_boosters if b['user_id'] not in msl_active]
        
        if not pool:
            return await interaction.followup.send(f"❌ Stripped {stripped_slots} slots from MSL, but no valid boosters exist to receive them!")
            
        # Reallocate
        tickets = []
        for booster in pool:
            tickets.extend([booster['user_id']] * booster['raffle_entries'])
            
        new_winners = []
        for _ in range(stripped_slots):
            if not tickets:
                break
            winner = secrets.choice(tickets)
            new_winners.append(winner)
            
        if not new_winners:
            return await interaction.followup.send("❌ Could not draw new winners.")
            
        # 7. Insert new winners using the EXACT same timestamp so they merge into the batch cleanly
        for wid in new_winners:
            await db.execute(
                "INSERT INTO booster_raffle_history (user_id, is_excess, won_at) VALUES (%s, TRUE, %s)", 
                (wid, latest_time)
            )
            
        # 8. Fetch updated complete tallies for THIS EXACT BATCH to reconstruct the embed
        updated_records = await db.fetch_all('''
            SELECT user_id, COUNT(*) as total_wins 
            FROM booster_raffle_history 
            WHERE won_at >= DATE_SUB(%s, INTERVAL 60 SECOND)
              AND won_at <= DATE_ADD(%s, INTERVAL 60 SECOND)
            GROUP BY user_id
        ''', (latest_time, latest_time))
        
        win_counts = {r['user_id']: r['total_wins'] for r in updated_records}
        sorted_winners = sorted(win_counts.items(), key=lambda x: x[1], reverse=True)
        
        lines = []
        total_diamonds = 0
        for uid, count in sorted_winners:
            diamonds = count * DIAMONDS_PER_WIN
            total_diamonds += diamonds
            
            user_obj = self.bot.get_user(uid)
            if not user_obj:
                try: user_obj = await self.bot.fetch_user(uid)
                except: pass
            name_disp = user_obj.display_name if user_obj else f"User {uid}"
            name_disp = name_disp.replace("*", "").replace("_", "").replace("`", "")
            
            if count > 1:
                excess_count = count - 1
                lines.append(f"🏆 **{name_disp}** — **{diamonds} 💎** (1 win + {excess_count} excess)")
            else:
                lines.append(f"🏆 **{name_disp}** — **{diamonds} 💎**")
                
        winner_pings = " ".join([f"<@{uid}>" for uid, _ in sorted_winners])
                
        # 9. Find the original message
        out_channel_id = await settings_service.get_int("boost_public_channel_id")
        channel = self.bot.get_channel(out_channel_id) or await self.bot.fetch_channel(out_channel_id)
        target_msg = None
        
        if channel:
            async for msg in channel.history(limit=100):
                if msg.author.id == self.bot.user.id and msg.embeds:
                    if msg.embeds[0].title and "Booster Raffle Winners!" in msg.embeds[0].title:
                        if msg.created_at.astimezone(TZ_MANILA).date() == latest_time.date():
                            target_msg = msg
                            break
                            
        if target_msg:
            embed = target_msg.embeds[0]
            # Replace description
            parts = embed.description.split("\n🏆")
            header = parts[0]
            
            new_desc = header + "\n" + "\n".join(lines) + f"\n\n**Total Diamonds this week:** 💎 **{total_diamonds:,}**"
            embed.description = new_desc
            embed.title = "✨ Weekly Booster Raffle Winners! (UPDATED) ✨"
            
            base_content = target_msg.content.split("\n\n")[0] if "\n\n" in target_msg.content else target_msg.content
            new_content = f"{base_content}\n\n{winner_pings}"
            
            try:
                await target_msg.edit(content=new_content, embed=embed)
            except Exception as e:
                logger.error(f"Failed to edit target msg: {e}")
                
        stripped_str = " ".join([f"<@{u}>" for u in msl_winners])
        new_str = " ".join([f"<@{u}>" for u in set(new_winners)])
                
        await interaction.followup.send(
            f"✅ **MSL Reroll Complete**\n"
            f"**Excluded MSL:** {stripped_str}\n"
            f"**Voided Slots:** `{stripped_slots}`\n"
            f"**Reallocated To:** {new_str}\n"
            f"{'(Edited original message!)' if target_msg else '(Original message not found)'}"
        )

    @app_commands.command(
        name="booster-raffle-surgeon",
        description="Emergency fix to purge test rounds and restore the target message's integrity."
    )
    @app_commands.describe(message_id="The discord message ID of the booster raffle announcement to fix")
    @app_commands.default_permissions(administrator=True)
    async def surgeon_msl(self, interaction: discord.Interaction, message_id: str):
        await interaction.response.defer(ephemeral=False)
        
        # 1. Look up the message
        try:
            msg_id_int = int(message_id)
            out_channel_id = await settings_service.get_int("boost_public_channel_id")
            channel = self.bot.get_channel(out_channel_id) or await self.bot.fetch_channel(out_channel_id)
            target_msg = await channel.fetch_message(msg_id_int)
        except Exception:
            return await interaction.followup.send("❌ Cannot find the specified message in the booster channel.")
            
        target_time = target_msg.created_at.astimezone(TZ_MANILA).replace(tzinfo=None)
        target_date = target_time.date()
        target_slots = await self._get_target_slots()
        
        # 2. Delete ALL rows drawn on that day EXCEPT the ones within 60s of the target message
        await db.execute('''
            DELETE FROM booster_raffle_history
            WHERE DATE(won_at) = %s 
              AND (won_at < DATE_SUB(%s, INTERVAL 60 SECOND) OR won_at > DATE_ADD(%s, INTERVAL 60 SECOND))
        ''', (target_date, target_time, target_time))
        
        # 3. Check how many slots remain in the target batch
        rem_records = await db.fetch_all('''
            SELECT user_id FROM booster_raffle_history
            WHERE won_at >= DATE_SUB(%s, INTERVAL 60 SECOND)
              AND won_at <= DATE_ADD(%s, INTERVAL 60 SECOND)
        ''', (target_time, target_time))
        
        current_count = len(rem_records)
        missing = target_slots - current_count
        
        # 4. Filter MSL from the target batch explicitly just in case any remained
        winner_ids = list({r['user_id'] for r in rem_records})
        if winner_ids:
            placeholders = ",".join(["%s"] * len(winner_ids))
            ver_rows = await db.fetch_all(f"SELECT user_id, mlbb_uid, mlbb_server FROM verified_users WHERE user_id IN ({placeholders})", tuple(winner_ids))
            for r in ver_rows:
                if verification_service.is_msl(r['mlbb_uid'], r['mlbb_server']):
                    await db.execute("DELETE FROM booster_raffle_history WHERE won_at >= DATE_SUB(%s, INTERVAL 60 SECOND) AND won_at <= DATE_ADD(%s, INTERVAL 60 SECOND) AND user_id = %s", (target_time, target_time, r['user_id']))
                    missing += list(x['user_id'] == r['user_id'] for x in rem_records).count(True)
                    
        # 5. Bring batch up to target_slots
        if missing > 0:
            # Re-fetch active pool
            active_boosters = await db.fetch_all("SELECT user_id, raffle_entries, boost_start_date FROM users WHERE boost_start_date IS NOT NULL AND raffle_entries > 0")
            booster_ids = [b['user_id'] for b in active_boosters]
            placeholders2 = ",".join(["%s"] * len(booster_ids))
            all_ver_rows = await db.fetch_all(f"SELECT user_id, mlbb_uid, mlbb_server FROM verified_users WHERE user_id IN ({placeholders2})", tuple(booster_ids))
            msl_active = set()
            for r in all_ver_rows:
                if verification_service.is_msl(r['mlbb_uid'], r['mlbb_server']): msl_active.add(r['user_id'])
            pool = [b for b in active_boosters if b['user_id'] not in msl_active]
            
            # Fetch baseline excess for historical fairness fallback
            excess_this_month = await db.fetch_all('''
                SELECT user_id, COUNT(*) as excess_count
                FROM booster_raffle_history
                WHERE MONTH(won_at) = MONTH(%s)
                  AND YEAR(won_at) = YEAR(%s)
                  AND is_excess = TRUE
                GROUP BY user_id
            ''', (target_time, target_time))
            monthly_excess = {row['user_id']: row['excess_count'] for row in excess_this_month}
            
            # Track purely localized draw excess for immediate draw parity
            draw_excess = {}
                
            for _ in range(missing):
                candidates = []
                for b_user in pool:
                    uid = b_user['user_id']
                    c_draw = draw_excess.get(uid, 0)
                    c_month = monthly_excess.get(uid, 0)
                    candidates.append((uid, c_draw, c_month))
                if not candidates: break
                
                min_sort_key = min((c[1], c[2]) for c in candidates)
                tied = [uid for uid, c_draw, c_month in candidates if (c_draw, c_month) == min_sort_key]
                w = secrets.choice(tied)
                
                await db.execute("INSERT INTO booster_raffle_history (user_id, is_excess, won_at) VALUES (%s, TRUE, %s)", (w, target_time))
                draw_excess[w] = draw_excess.get(w, 0) + 1
                monthly_excess[w] = monthly_excess.get(w, 0) + 1
                
        # 6. Rebuild Embed
        updated_records = await db.fetch_all('''
            SELECT user_id, COUNT(*) as total_wins 
            FROM booster_raffle_history 
            WHERE won_at >= DATE_SUB(%s, INTERVAL 60 SECOND)
              AND won_at <= DATE_ADD(%s, INTERVAL 60 SECOND)
            GROUP BY user_id
        ''', (target_time, target_time))
        
        win_counts = {r['user_id']: r['total_wins'] for r in updated_records}
        sorted_winners = sorted(win_counts.items(), key=lambda x: x[1], reverse=True)
        
        lines = []
        total_diamonds = 0
        for uid, count in sorted_winners:
            diamonds = count * DIAMONDS_PER_WIN
            total_diamonds += diamonds
            
            user_obj = self.bot.get_user(uid)
            if not user_obj:
                try: user_obj = await self.bot.fetch_user(uid)
                except: pass
            name_disp = user_obj.display_name if user_obj else f"User {uid}"
            name_disp = name_disp.replace("*", "").replace("_", "").replace("`", "")
            
            if count > 1:
                lines.append(f"🏆 **{name_disp}** — **{diamonds} 💎** (1 win + {count - 1} excess)")
            else:
                lines.append(f"🏆 **{name_disp}** — **{diamonds} 💎**")
                
        winner_pings = " ".join([f"<@{uid}>" for uid, _ in sorted_winners])
                
        # Rebuild embed description accurately
        total_boosters = len(sorted_winners)
        has_excess = any(count > 1 for _, count in sorted_winners)
        
        description_parts = [
            f"Thank you to everyone who boosts the server!\n"
            f"Here are this week's **{total_boosters}** lucky winners "
            f"across **{target_slots}** prize slots:\n"
        ]
        
        if has_excess:
            description_parts.append(
                f"\n*Since we have {total_boosters} booster(s) for {target_slots} slots, "
                f"the remaining {target_slots - total_boosters} excess slot(s) have been "
                f"fairly distributed.*\n\n"
            )
        else:
            description_parts.append("\n")
            
        description_parts.append("\n".join(lines))
        description_parts.append(f"\n\n**Total Diamonds this week:** 💎 **{total_diamonds:,}**")
        
        embed = target_msg.embeds[0]
        embed.description = "".join(description_parts)
        embed.title = "✨ Weekly Booster Raffle Winners! (SURGEON CLEAN) ✨"
        
        base_content = target_msg.content.split("\n\n")[0] if "\n\n" in target_msg.content else target_msg.content
        new_content = f"{base_content}\n\n{winner_pings}"
        
        try:
            await target_msg.edit(content=new_content, embed=embed)
        except Exception as e:
            logger.error(f"Surgeon msg edit failed: {e}")
        
        await interaction.followup.send(f"✅ **Surgical Repair Complete!**\nPurged all anomalies generated during testing and restored the specific message `{message_id}` back exactly to {len(updated_records)} slots (accounting for MSL removal).")


async def setup(bot: commands.Bot):
    await bot.add_cog(BoosterRaffleCog(bot))
