import discord
from discord.ext import commands, tasks
from discord import app_commands
import datetime
import logging
import pytz
import secrets

from services.database import db
from services.settings_service import settings_service
from utils.constants import TZ_MANILA

logger = logging.getLogger('mlbb_bot')

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

    async def _execute_raffle(self, is_manual=False, target_channel=None, ignore_7day_rule=False):
        logger.info("Starting Weekly Booster Raffle execution...")
        
        # 1. Fetch all currently active boosters with their active weights
        active_boosters = await db.fetch_all('''
            SELECT user_id, raffle_entries, boost_start_date 
            FROM users 
            WHERE boost_start_date IS NOT NULL AND raffle_entries > 0
        ''')
        
        if not active_boosters:
            logger.warning("No active boosters found for raffle.")
            return

        # 2. Fetch users who have won THIS calendar month
        won_this_month = await db.fetch_all('''
            SELECT DISTINCT user_id 
            FROM booster_raffle_history 
            WHERE MONTH(won_at) = MONTH(CURRENT_DATE()) 
              AND YEAR(won_at) = YEAR(CURRENT_DATE())
        ''')
        won_ids = {row['user_id'] for row in won_this_month}
        
        pool_a = []
        pool_b = []
        
        now = datetime.datetime.now(TZ_MANILA)
        cutoff_7_days = now - datetime.timedelta(days=7)
        
        for b in active_boosters:
            uid = b['user_id']
            
            # Convert MySQL datetime to tz-aware depending on DB connection config
            start_date = b['boost_start_date']
            if start_date.tzinfo is None:
                start_date = pytz.utc.localize(start_date).astimezone(TZ_MANILA)
                
            has_won = uid in won_ids
            joined_early_enough = start_date <= cutoff_7_days
            
            # Priority Pool: Needs to have NOT won this month AND been boosting for >= 7 days
            if not has_won and (joined_early_enough or ignore_7day_rule):
                pool_a.append(b)
            else:
                pool_b.append(b)
                
        # 3. Dedicated unique selection function using cryptographic randomized weights
        def select_unique_winners(pool, needed_slots):
            winners = []
            tickets = []
            for booster in pool:
                # Add ticket multiple times based on booster tier weight
                tickets.extend([booster['user_id']] * booster['raffle_entries'])
                
            while len(winners) < needed_slots and len(tickets) > 0:
                winner = secrets.choice(tickets)
                winners.append(winner)
                
                # De-duplication: Ensure this winner cannot occupy a second slot this week
                tickets = [t for t in tickets if t != winner]
            
            return winners
            
        # 4. Draw sequence (Monthly Guarantee enforcement)
        target_winners = 25
        final_winners = []
        
        winners_a = select_unique_winners(pool_a, target_winners)
        final_winners.extend(winners_a)
        target_winners -= len(winners_a)
        
        if target_winners > 0:
            winners_b = select_unique_winners(pool_b, target_winners)
            final_winners.extend(winners_b)
            
        if not final_winners:
            logger.warning("Raffle drew 0 winners despite having active boosters.")
            return
            
        # 5. Lock in winners to database constraint
        for wid in final_winners:
            try:
                await db.execute(
                    "INSERT INTO booster_raffle_history (user_id) VALUES (%s)", 
                    (wid,)
                )
            except Exception as e:
                logger.error(f"Failed to record winner {wid}: {e}")
                
        # 6. Public Announcement
        await self._announce_winners(final_winners, target_channel)

    async def _announce_winners(self, winner_ids, manual_target_channel=None):
        out_channel_id = await settings_service.get_int("boost_public_channel_id")
        channel = manual_target_channel
        
        if not channel:
            if out_channel_id:
                channel = self.bot.get_channel(out_channel_id) or await self.bot.fetch_channel(out_channel_id)
                
        if not channel:
            logger.warning("No boost_public_channel_id configured for raffle announcement. Aborting log.")
            return

        # Format mentions cleanly, handle large limits reasonably
        winner_mentions = [f"<@{wid}>" for wid in winner_ids]
        
        if len(winner_mentions) > 10:
            description = ",\n".join(winner_mentions)
        else:
            description = "\n".join(f"🏆 {m}" for m in winner_mentions)
            
        embed = discord.Embed(
            title="✨ Weekly Booster Raffle Winners! ✨",
            description=f"Thank you to everyone who boosts the server!\nHere are this week's {len(winner_ids)} lucky ascendants:\n\n{description}",
            color=0xFFD700,
            timestamp=datetime.datetime.now(TZ_MANILA)
        )
        embed.set_footer(text="May your light guide us through the cosmos.")
        
        try:
            await channel.send(
                content="🎉 Congratulations to our celestial ascended boosters!", 
                embed=embed
            )
        except Exception as e:
            logger.error(f"Failed to send raffle announcement: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(BoosterRaffleCog(bot))
