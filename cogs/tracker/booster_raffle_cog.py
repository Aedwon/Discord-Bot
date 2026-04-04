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
        
        try:
            await channel.send(
                content="🎉 Congratulations to our celestial ascended boosters!", 
                embed=embed
            )
        except Exception as e:
            logger.error(f"Failed to send raffle announcement: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(BoosterRaffleCog(bot))
