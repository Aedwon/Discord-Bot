"""
Analytics Service — Query layer for aggregating community metrics.
Handles retention calculations, peak hour analysis, sentiment exports, and rollup generation.
All time boundaries aligned to Asia/Manila (UTC+8).
"""

import io
from datetime import datetime, timedelta, timezone
from services.database import db
import logging

logger = logging.getLogger("mlbb_bot.analytics_service")

# Philippine Standard Time offset
PHT = timezone(timedelta(hours=8))


class AnalyticsService:

    def now_pht(self) -> datetime:
        """Get current time in Asia/Manila."""
        return datetime.now(PHT)

    # ─── MESSAGE METRICS ────────────────────────────────────────────

    async def get_message_volume(self, days: int = 7) -> list:
        """Message count per channel over the last N days."""
        return await db.fetch_all('''
            SELECT channel_id, COUNT(*) as msg_count, COUNT(DISTINCT author_id) as unique_authors
            FROM analytics_messages
            WHERE created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
            GROUP BY channel_id ORDER BY msg_count DESC LIMIT 15
        ''', (days,))

    async def get_peak_hours(self, days: int = 7) -> list:
        """Heatmap data: message count by hour_of_day and day_of_week."""
        return await db.fetch_all('''
            SELECT hour_of_day, day_of_week, COUNT(*) as msg_count
            FROM analytics_messages
            WHERE created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
            GROUP BY hour_of_day, day_of_week
        ''', (days,))

    async def get_communicator_ratio(self, guild_member_count: int, days: int = 7) -> dict:
        """Calculate visitor vs communicator ratio."""
        msg_users = await db.fetch_one('''
            SELECT COUNT(DISTINCT author_id) as c FROM analytics_messages
            WHERE created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
        ''', (days,))
        voice_users = await db.fetch_one('''
            SELECT COUNT(DISTINCT user_id) as c FROM analytics_voice_sessions
            WHERE joined_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
        ''', (days,))
        msg_c = msg_users['c'] if msg_users else 0
        voice_c = voice_users['c'] if voice_users else 0
        # Union of message + voice users is an approximation (some overlap)
        communicators = msg_c + voice_c  # Upper bound; exact would need UNION query
        ratio = (communicators / guild_member_count * 100) if guild_member_count > 0 else 0
        return {"communicators": communicators, "total": guild_member_count, "ratio": round(ratio, 1)}

    # ─── RETENTION ──────────────────────────────────────────────────

    async def get_retention(self, day_n: int) -> dict:
        """Calculate Day-N retention: of users who joined N days ago, how many messaged since?"""
        target_date = self.now_pht() - timedelta(days=day_n)
        date_str = target_date.strftime('%Y-%m-%d')
        
        joined = await db.fetch_one('''
            SELECT COUNT(*) as c FROM analytics_member_joins
            WHERE DATE(joined_at) = %s
        ''', (date_str,))
        
        retained = await db.fetch_one('''
            SELECT COUNT(DISTINCT am.author_id) as c
            FROM analytics_messages am
            INNER JOIN analytics_member_joins amj ON am.author_id = amj.user_id
            WHERE DATE(amj.joined_at) = %s AND am.created_at > amj.joined_at
        ''', (date_str,))
        
        total = joined['c'] if joined else 0
        kept = retained['c'] if retained else 0
        rate = (kept / total * 100) if total > 0 else 0
        return {"joined": total, "retained": kept, "rate": round(rate, 1)}

    # ─── INVITE ATTRIBUTION ─────────────────────────────────────────

    async def get_top_invites(self, days: int = 30) -> list:
        """Top invite codes by join count."""
        return await db.fetch_all('''
            SELECT invite_code, inviter_id, COUNT(*) as join_count
            FROM analytics_member_joins
            WHERE invite_code IS NOT NULL AND joined_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
            GROUP BY invite_code, inviter_id ORDER BY join_count DESC LIMIT 10
        ''', (days,))

    # ─── VOICE METRICS ──────────────────────────────────────────────

    async def get_voice_stats(self, days: int = 7) -> list:
        """Top channels by total voice minutes."""
        return await db.fetch_all('''
            SELECT channel_id,
                   ROUND(SUM(TIMESTAMPDIFF(MINUTE, joined_at, COALESCE(left_at, NOW())))) as total_minutes,
                   COUNT(DISTINCT user_id) as unique_users
            FROM analytics_voice_sessions
            WHERE joined_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
            GROUP BY channel_id ORDER BY total_minutes DESC LIMIT 10
        ''', (days,))

    # ─── EVENT RSVP CONVERSION ──────────────────────────────────────

    async def get_event_conversion(self, event_id: int) -> dict:
        """RSVP vs actual attendance for an event."""
        rsvps = await db.fetch_one(
            "SELECT COUNT(*) as c FROM analytics_event_rsvps WHERE event_id = %s", (event_id,))
        attendees = await db.fetch_one(
            "SELECT COUNT(DISTINCT user_id) as c FROM guild_event_rewards WHERE event_id = %s", (event_id,))
        r = rsvps['c'] if rsvps else 0
        a = attendees['c'] if attendees else 0
        rate = (a / r * 100) if r > 0 else 0
        return {"rsvps": r, "attendees": a, "conversion": round(rate, 1)}

    # ─── LINK CTR ───────────────────────────────────────────────────

    async def get_link_ctr(self, link_id: int) -> dict:
        """Click-through stats for a tracked link."""
        clicks = await db.fetch_one(
            "SELECT COUNT(*) as c FROM analytics_link_clicks WHERE link_id = %s", (link_id,))
        link = await db.fetch_one(
            "SELECT label, url, created_at FROM analytics_tracked_links WHERE id = %s", (link_id,))
        return {
            "clicks": clicks['c'] if clicks else 0,
            "label": link['label'] if link else "Unknown",
            "url": link['url'] if link else "",
        }

    # ─── SENTIMENT EXPORT ───────────────────────────────────────────

    async def generate_sentiment_export(self, guild, days: int = 1) -> io.BytesIO:
        """
        Generate a structured .txt file of messages for LLM sentiment analysis.
        Groups by channel, sorted chronologically within each channel.
        """
        cutoff = self.now_pht() - timedelta(days=days)
        cutoff_str = cutoff.strftime('%Y-%m-%d %H:%M:%S')

        rows = await db.fetch_all('''
            SELECT channel_id, author_id, content, is_deleted, created_at
            FROM analytics_messages
            WHERE created_at >= %s
            ORDER BY channel_id, created_at
        ''', (cutoff_str,))

        # Group by channel
        channels = {}
        for row in rows:
            cid = row['channel_id']
            if cid not in channels:
                channels[cid] = []
            channels[cid].append(row)

        # Build export
        end_date = self.now_pht().strftime('%Y-%m-%d')
        start_date = cutoff.strftime('%Y-%m-%d')
        period = "Daily" if days == 1 else f"{days}-Day"
        
        lines = []
        lines.append(f"=== Community Sentiment Report: {start_date} to {end_date} (Asia/Manila) ===")
        lines.append(f"=== Period: {period} | Total Messages: {len(rows)} ===")
        lines.append("")

        for cid, messages in channels.items():
            # Resolve channel name
            ch = guild.get_channel(cid)
            ch_name = f"#{ch.name}" if ch else f"#unknown-{cid}"
            lines.append(f"--- {ch_name} ({len(messages)} messages) ---")
            for msg in messages:
                ts = msg['created_at'].strftime('%H:%M') if msg['created_at'] else "??:??"
                member = guild.get_member(msg['author_id'])
                author = member.display_name if member else str(msg['author_id'])
                content = msg['content'] or "[empty]"
                deleted_tag = " [DELETED]" if msg['is_deleted'] else ""
                lines.append(f"[{ts}] {author}: {content}{deleted_tag}")
            lines.append("")

        text = "\n".join(lines)
        buffer = io.BytesIO(text.encode('utf-8-sig'))  # BOM for Excel compatibility
        buffer.seek(0)
        return buffer

    # ─── DAILY ROLLUP ───────────────────────────────────────────────

    async def run_daily_rollup(self, bot=None):
        """Aggregate yesterday's raw data into permanent daily summaries."""
        yesterday = (self.now_pht() - timedelta(days=1)).strftime('%Y-%m-%d')
        
        msgs = await db.fetch_one('''
            SELECT COUNT(*) as total, COUNT(DISTINCT author_id) as uniq
            FROM analytics_messages WHERE DATE(created_at) = %s
        ''', (yesterday,))
        
        voice = await db.fetch_one('''
            SELECT COALESCE(ROUND(SUM(TIMESTAMPDIFF(MINUTE, joined_at, COALESCE(left_at, NOW())))), 0) as mins,
                   COUNT(DISTINCT user_id) as uniq
            FROM analytics_voice_sessions WHERE DATE(joined_at) = %s
        ''', (yesterday,))
        
        joins = await db.fetch_one(
            "SELECT COUNT(*) as c FROM analytics_member_joins WHERE DATE(joined_at) = %s", (yesterday,))
        leaves = await db.fetch_one(
            "SELECT COUNT(*) as c FROM analytics_member_joins WHERE DATE(left_at) = %s", (yesterday,))
        reactions = await db.fetch_one(
            "SELECT COUNT(*) as c FROM analytics_reactions WHERE DATE(created_at) = %s", (yesterday,))

        import json
        def json_serial(obj):
            import decimal, datetime
            if isinstance(obj, decimal.Decimal):
                return float(obj)
            if isinstance(obj, (datetime.datetime, datetime.date)):
                return obj.isoformat()
            return str(obj)

        try:
            granular_stats = await self.get_exhaustive_daily_stats(yesterday, bot=bot)
            granular_json_str = json.dumps(granular_stats, default=json_serial)
        except Exception as e:
            logger.error(f"Failed to generate granular json for rollup: {e}")
            granular_json_str = None

        await db.execute('''
            INSERT INTO analytics_daily_rollups 
            (date, total_messages, unique_messagers, total_voice_minutes, unique_voice_users, new_joins, new_leaves, total_reactions, granular_json)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                total_messages = VALUES(total_messages),
                unique_messagers = VALUES(unique_messagers),
                total_voice_minutes = VALUES(total_voice_minutes),
                unique_voice_users = VALUES(unique_voice_users),
                new_joins = VALUES(new_joins),
                new_leaves = VALUES(new_leaves),
                total_reactions = VALUES(total_reactions),
                granular_json = VALUES(granular_json)
        ''', (
            yesterday,
            msgs['total'] if msgs else 0,
            msgs['uniq'] if msgs else 0,
            voice['mins'] if voice else 0,
            voice['uniq'] if voice else 0,
            joins['c'] if joins else 0,
            leaves['c'] if leaves else 0,
            reactions['c'] if reactions else 0,
            granular_json_str
        ))
        logger.info(f"Daily rollup completed for {yesterday}")
        
        return {
            "date": yesterday,
            "total_messages": msgs['total'] if msgs else 0,
            "unique_messagers": msgs['uniq'] if msgs else 0,
            "total_voice_minutes": voice['mins'] if voice else 0,
            "unique_voice_users": voice['uniq'] if voice else 0,
            "new_joins": joins['c'] if joins else 0,
            "new_leaves": leaves['c'] if leaves else 0,
            "total_reactions": reactions['c'] if reactions else 0,
        }

    async def purge_old_messages(self, retention_days: int = 30):
        """Auto-purge raw message data older than retention window."""
        await db.execute('''
            DELETE FROM analytics_messages 
            WHERE created_at < DATE_SUB(NOW(), INTERVAL %s DAY)
        ''', (retention_days,))
        logger.info(f"Purged analytics_messages older than {retention_days} days")

    # ─── ACTIVE USER METRICS (DAU / WAU / MAU) ────────────────────

    async def get_active_users(self) -> dict:
        """
        Compute DAU, WAU, MAU using a de-duplicated UNION across
        message authors and voice session participants.

        Also returns:
          - 7-day DAU trend from rollup snapshots
          - Period-over-period change percentages for WAU and MAU
        """
        # ── Live counts (UNION de-duplicates users across both tables) ──

        dau = await db.fetch_one('''
            SELECT COUNT(*) AS c FROM (
                SELECT author_id AS uid FROM analytics_messages
                WHERE created_at >= CURDATE()
                UNION
                SELECT user_id AS uid FROM analytics_voice_sessions
                WHERE joined_at >= CURDATE()
            ) AS today_active
        ''')

        wau = await db.fetch_one('''
            SELECT COUNT(*) AS c FROM (
                SELECT author_id AS uid FROM analytics_messages
                WHERE created_at >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
                UNION
                SELECT user_id AS uid FROM analytics_voice_sessions
                WHERE joined_at >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
            ) AS week_active
        ''')

        mau = await db.fetch_one('''
            SELECT COUNT(*) AS c FROM (
                SELECT author_id AS uid FROM analytics_messages
                WHERE created_at >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)
                UNION
                SELECT user_id AS uid FROM analytics_voice_sessions
                WHERE joined_at >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)
            ) AS month_active
        ''')

        # ── Previous-period WAU/MAU for trend comparison ──

        prev_wau = await db.fetch_one('''
            SELECT COUNT(*) AS c FROM (
                SELECT author_id AS uid FROM analytics_messages
                WHERE created_at >= DATE_SUB(CURDATE(), INTERVAL 14 DAY)
                  AND created_at < DATE_SUB(CURDATE(), INTERVAL 7 DAY)
                UNION
                SELECT user_id AS uid FROM analytics_voice_sessions
                WHERE joined_at >= DATE_SUB(CURDATE(), INTERVAL 14 DAY)
                  AND joined_at < DATE_SUB(CURDATE(), INTERVAL 7 DAY)
            ) AS prev_week
        ''')

        prev_mau = await db.fetch_one('''
            SELECT COUNT(*) AS c FROM (
                SELECT author_id AS uid FROM analytics_messages
                WHERE created_at >= DATE_SUB(CURDATE(), INTERVAL 60 DAY)
                  AND created_at < DATE_SUB(CURDATE(), INTERVAL 30 DAY)
                UNION
                SELECT user_id AS uid FROM analytics_voice_sessions
                WHERE joined_at >= DATE_SUB(CURDATE(), INTERVAL 60 DAY)
                  AND joined_at < DATE_SUB(CURDATE(), INTERVAL 30 DAY)
            ) AS prev_month
        ''')

        # ── 7-day DAU trend from rollup snapshots ──
        # Note: rollups track messagers + voice users separately.
        # We approximate DAU from rollups as max(unique_messagers, unique_voice_users)
        # since a precise UNION isn't available from pre-aggregated data.
        # The "today" live count above is always exact.

        dau_trend = await db.fetch_all('''
            SELECT date,
                   (unique_messagers + unique_voice_users) AS approx_dau
            FROM analytics_daily_rollups
            WHERE date >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
            ORDER BY date ASC
        ''')

        # ── Percentage changes ──
        dau_val = dau['c'] if dau else 0
        wau_val = wau['c'] if wau else 0
        mau_val = mau['c'] if mau else 0
        prev_wau_val = prev_wau['c'] if prev_wau else 0
        prev_mau_val = prev_mau['c'] if prev_mau else 0

        def pct_change(current: int, previous: int) -> float | None:
            if previous == 0:
                return None  # No baseline to compare
            return round((current - previous) / previous * 100, 1)

        # DAU/WAU ratio = "stickiness" — industry standard engagement metric
        stickiness = round(dau_val / wau_val * 100, 1) if wau_val > 0 else 0.0

        return {
            "dau": dau_val,
            "wau": wau_val,
            "mau": mau_val,
            "wau_change": pct_change(wau_val, prev_wau_val),
            "mau_change": pct_change(mau_val, prev_mau_val),
            "stickiness": stickiness,
            "dau_trend": [
                {"date": row['date'].strftime('%m/%d') if hasattr(row['date'], 'strftime') else str(row['date']),
                 "count": row['approx_dau']}
                for row in dau_trend
            ],
        }

    # ─── OVERVIEW ───────────────────────────────────────────────────

    async def get_overview(self, days: int = 7) -> dict:
        """Compound overview stats for the last N days."""
        rollups = await db.fetch_all('''
            SELECT * FROM analytics_daily_rollups
            WHERE date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
            ORDER BY date DESC
        ''', (days,))
        
        totals = {
            "messages": sum(r['total_messages'] for r in rollups),
            "communicators": max((r['unique_messagers'] for r in rollups), default=0),
            "voice_minutes": sum(r['total_voice_minutes'] for r in rollups),
            "joins": sum(r['new_joins'] for r in rollups),
            "leaves": sum(r['new_leaves'] for r in rollups),
            "reactions": sum(r['total_reactions'] for r in rollups),
            "days_tracked": len(rollups),
        }
        return totals


    async def get_exhaustive_daily_stats(self, date_str: str, bot=None) -> dict:
        """Fetch multiple exhaustive data points for a specific date (YYYY-MM-DD).
        Name resolution uses the persistent member_names / channel_names DB cache
        so the Vercel dashboard (which has no bot connection) can display them."""
        stats = {}
        
        # Helper to resolve names — tries bot first, then falls back to DB cache
        async def resolve_member_name(user_id):
            if bot:
                for g in bot.guilds:
                    m = g.get_member(user_id)
                    if m: return m.display_name
            # Fallback: DB cache
            row = await db.fetch_one(
                "SELECT display_name FROM member_names WHERE user_id = %s", (user_id,)
            )
            return row['display_name'] if row else str(user_id)

        async def resolve_channel_name(channel_id):
            if bot:
                for g in bot.guilds:
                    c = g.get_channel(channel_id)
                    if c: return c.name
            # Fallback: DB cache
            row = await db.fetch_one(
                "SELECT channel_name FROM channel_names WHERE channel_id = %s", (channel_id,)
            )
            return row['channel_name'] if row else str(channel_id)

        # 1. Moderation Actions — cast to int to guarantee whole numbers
        mods = await db.fetch_all('''
            SELECT action_type, COUNT(*) as c 
            FROM mod_logs 
            WHERE DATE(timestamp) = %s 
            GROUP BY action_type
        ''', (date_str,))
        stats['mod_actions'] = {row['action_type']: int(row['c']) for row in mods}
        stats['total_mod_actions'] = int(sum(row['c'] for row in mods))
        
        # 2. Verifications
        v = await db.fetch_one("SELECT COUNT(*) as c FROM verified_users WHERE DATE(verified_at) = %s", (date_str,))
        stats['new_verifications'] = int(v['c']) if v else 0
        
        # 3. Tickets & Category Breakdown — query BOTH active_tickets and ticket_history
        # ticket_history captures closed tickets; active_tickets captures still-open ones
        t_active = await db.fetch_one(
            "SELECT COUNT(*) as c FROM active_tickets WHERE DATE(created_at) = %s AND is_test = FALSE", (date_str,))
        t_history = await db.fetch_one(
            "SELECT COUNT(*) as c FROM ticket_history WHERE DATE(created_at) = %s AND is_test = FALSE", (date_str,))
        stats['new_tickets'] = int(t_active['c'] if t_active else 0) + int(t_history['c'] if t_history else 0)
        
        # Category breakdown from both tables
        tc_active = await db.fetch_all(
            "SELECT category_key, COUNT(*) as c FROM active_tickets WHERE DATE(created_at) = %s AND is_test = FALSE GROUP BY category_key", (date_str,))
        tc_history = await db.fetch_all(
            "SELECT category_key, COUNT(*) as c FROM ticket_history WHERE DATE(created_at) = %s AND is_test = FALSE GROUP BY category_key", (date_str,))
        merged_cats = {}
        for row in tc_active:
            merged_cats[row['category_key']] = int(row['c'])
        for row in tc_history:
            merged_cats[row['category_key']] = merged_cats.get(row['category_key'], 0) + int(row['c'])
        stats['tickets_by_category'] = merged_cats
        
        tr = await db.fetch_one("SELECT COUNT(*) as c, AVG(stars) as avg_rating FROM ticket_ratings WHERE DATE(rated_at) = %s", (date_str,))
        stats['ticket_ratings_count'] = int(tr['c']) if tr else 0
        stats['ticket_avg_rating'] = round(float(tr['avg_rating']), 1) if tr and tr['avg_rating'] else 0.0
        
        # 4. Quiz History & Top 3
        q = await db.fetch_one("SELECT COUNT(*) as sessions, SUM(score) as total_score FROM quiz_history WHERE DATE(earned_at) = %s", (date_str,))
        stats['quiz_sessions'] = int(q['sessions']) if q and q['sessions'] else 0
        stats['quiz_score'] = int(q['total_score']) if q and q['total_score'] else 0
        
        q_top = await db.fetch_all('''
            SELECT user_id, SUM(score) as sum_score 
            FROM quiz_history 
            WHERE DATE(earned_at) = %s 
            GROUP BY user_id 
            ORDER BY sum_score DESC LIMIT 3
        ''', (date_str,))
        stats['quiz_top_3'] = [{"user_id": r['user_id'], "name": await resolve_member_name(r['user_id']), "score": int(r['sum_score'])} for r in q_top]
        
        # 5. Thanks System & Top 3 Receivers
        th = await db.fetch_one("SELECT COUNT(*) as c FROM thanks_history WHERE DATE(created_at) = %s", (date_str,))
        stats['thanks_given'] = int(th['c']) if th else 0
        
        th_top = await db.fetch_all('''
            SELECT receiver_id, COUNT(*) as received 
            FROM thanks_history 
            WHERE DATE(created_at) = %s 
            GROUP BY receiver_id 
            ORDER BY received DESC LIMIT 3
        ''', (date_str,))
        stats['thanks_top_3'] = [{"user_id": r['receiver_id'], "name": await resolve_member_name(r['receiver_id']), "count": int(r['received'])} for r in th_top]
        
        # 6. Quest Progress
        qp = await db.fetch_one("SELECT COUNT(*) as c FROM quest_progress WHERE completed = TRUE AND DATE(completed_at) = %s", (date_str,))
        stats['quests_completed'] = int(qp['c']) if qp else 0
        
        # 7. Referrals & Invites
        ref = await db.fetch_one("SELECT COUNT(*) as c FROM referrals WHERE DATE(created_at) = %s", (date_str,))
        stats['new_referrals'] = int(ref['c']) if ref else 0
        
        invites = await db.fetch_all('''
            SELECT invite_code, inviter_id, COUNT(*) as c 
            FROM analytics_member_joins 
            WHERE DATE(joined_at) = %s AND invite_code IS NOT NULL 
            GROUP BY invite_code, inviter_id 
            ORDER BY c DESC LIMIT 3
        ''', (date_str,))
        stats['top_invites'] = [{"code": r['invite_code'], "inviter": r['inviter_id'], "name": await resolve_member_name(r['inviter_id']), "count": int(r['c'])} for r in invites]
        
        # 8. Event Redemptions (EP Economy)
        ep = await db.fetch_one("SELECT COUNT(*) as redemptions FROM event_redemptions WHERE DATE(redeemed_at) = %s", (date_str,))
        stats['ep_redemptions'] = int(ep['redemptions']) if ep and ep['redemptions'] else 0
        
        # 9. Day-1 Retention
        ret_1 = await self.get_retention(1)
        stats['retention_day_1'] = ret_1
        
        # 10. Top 5 Text Channels — with resolved names
        tx = await db.fetch_all('''
            SELECT channel_id, COUNT(*) as c 
            FROM analytics_messages 
            WHERE DATE(created_at) = %s 
            GROUP BY channel_id 
            ORDER BY c DESC LIMIT 5
        ''', (date_str,))
        stats['top_text_channels'] = [{"channel_id": r['channel_id'], "name": await resolve_channel_name(r['channel_id']), "count": int(r['c'])} for r in tx]
        
        # 11. Top 3 Voice Channels — with resolved names
        vx = await db.fetch_all('''
            SELECT channel_id, ROUND(SUM(TIMESTAMPDIFF(MINUTE, joined_at, COALESCE(left_at, NOW())))) as mins 
            FROM analytics_voice_sessions 
            WHERE DATE(joined_at) = %s 
            GROUP BY channel_id 
            ORDER BY mins DESC LIMIT 3
        ''', (date_str,))
        stats['top_voice_channels'] = [{"channel_id": r['channel_id'], "name": await resolve_channel_name(r['channel_id']), "mins": int(r['mins'] or 0)} for r in vx]

        # 12. Booster Raffle Performance
        br_wins = await db.fetch_one("SELECT COUNT(*) as c FROM booster_raffle_history WHERE DATE(won_at) = %s", (date_str,))
        stats['booster_raffle_wins'] = int(br_wins['c']) if br_wins else 0

        # 13. Event Raffle Performance
        er_created = await db.fetch_one("SELECT COUNT(*) as c FROM event_raffles WHERE DATE(created_at) = %s", (date_str,))
        stats['event_raffles_created'] = int(er_created['c']) if er_created else 0
        
        er_entries = await db.fetch_one("SELECT COUNT(*) as c FROM event_raffle_entries WHERE DATE(entered_at) = %s", (date_str,))
        stats['event_raffle_entries'] = int(er_entries['c']) if er_entries else 0

        # 14. Event EP Claims
        ep_claims = await db.fetch_one("SELECT COUNT(*) as c, SUM(ep_awarded) as total_ep FROM guild_event_rewards WHERE DATE(awarded_at) = %s", (date_str,))
        stats['event_participation_claims'] = int(ep_claims['c']) if ep_claims else 0
        stats['event_ep_distributed'] = int(ep_claims['total_ep']) if ep_claims and ep_claims['total_ep'] else 0

        # 15. New Event Registrations
        reg = await db.fetch_one("SELECT COUNT(*) as c FROM event_registration_entries WHERE DATE(registered_at) = %s", (date_str,))
        stats['event_registrations'] = int(reg['c']) if reg else 0

        return stats

    # ─── ALL-TIME RANKING QUERIES (for API / Dashboard) ─────────

    async def get_top_raffles(self, limit: int = 5) -> list:
        """Return the top raffles of all time ranked by total entries.
        Includes resolved raffle title and entry count."""
        rows = await db.fetch_all('''
            SELECT er.raffle_id,
                   r.title,
                   COUNT(*) as total_entries
            FROM event_raffle_entries er
            JOIN event_raffles r ON r.id = er.raffle_id
            GROUP BY er.raffle_id, r.title
            ORDER BY total_entries DESC
            LIMIT %s
        ''', (limit,))
        return [{"raffle_id": r['raffle_id'], "title": r['title'] or f"Raffle #{r['raffle_id']}", "entries": int(r['total_entries'])} for r in rows]

    async def get_top_events(self, limit: int = 5) -> list:
        """Return the top events of all time ranked by total registrations.
        Includes resolved event title and participant count."""
        rows = await db.fetch_all('''
            SELECT ere.event_id,
                   COALESCE(er.title, CONCAT('Event #', ere.event_id)) as title,
                   COUNT(*) as total_participants
            FROM event_registration_entries ere
            LEFT JOIN event_registrations er ON er.event_id = ere.event_id
            GROUP BY ere.event_id, er.title
            ORDER BY total_participants DESC
            LIMIT %s
        ''', (limit,))
        return [{"event_id": r['event_id'], "title": r['title'], "participants": int(r['total_participants'])} for r in rows]

# Singleton export
analytics_service = AnalyticsService()

