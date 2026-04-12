"""
One-time repair script: Populate member_names and channel_names cache tables,
then re-generate granular_json for existing rollups so historical data shows
resolved names instead of raw IDs.

Usage:
    python scripts/repair_analytics.py [--days 30]

This script connects directly to the database (no bot required).
It reads the existing granular_json, resolves any numeric-only name fields
against the freshly populated cache tables, and saves the corrected JSON back.
"""

import asyncio
import sys
import os
import json
import argparse
from datetime import date, timedelta

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.database import db


async def populate_caches_from_rollups():
    """
    Scan existing granular_json data for user_ids and channel_ids that appear
    as names. This serves as a bootstrap for the cache tables before the bot
    does a full sync on next startup.
    """
    print("Phase 1: Bootstrapping name caches from granular_json...")
    rows = await db.fetch_all(
        "SELECT date, granular_json FROM analytics_daily_rollups WHERE granular_json IS NOT NULL"
    )
    user_ids_seen = set()
    channel_ids_seen = set()

    for row in rows:
        try:
            g = json.loads(row['granular_json']) if isinstance(row['granular_json'], str) else row['granular_json']
        except (json.JSONDecodeError, TypeError):
            continue

        # Collect unique IDs from various sections
        for entry in g.get('quiz_top_3', []):
            uid = entry.get('user_id')
            name = entry.get('name', '')
            if uid and name and not str(name).isdigit():
                user_ids_seen.add((uid, name))

        for entry in g.get('thanks_top_3', []):
            uid = entry.get('user_id')
            name = entry.get('name', '')
            if uid and name and not str(name).isdigit():
                user_ids_seen.add((uid, name))

        for entry in g.get('top_invites', []):
            uid = entry.get('inviter')
            name = entry.get('name', '')
            if uid and name and not str(name).isdigit():
                user_ids_seen.add((uid, name))

        for entry in g.get('top_text_channels', []):
            cid = entry.get('channel_id')
            name = entry.get('name', '')
            if cid and name and not str(name).isdigit():
                channel_ids_seen.add((cid, name))

        for entry in g.get('top_voice_channels', []):
            cid = entry.get('channel_id')
            name = entry.get('name', '')
            if cid and name and not str(name).isdigit():
                channel_ids_seen.add((cid, name))

    # Insert discovered names into cache tables
    count = 0
    for uid, name in user_ids_seen:
        try:
            await db.execute(
                "INSERT INTO member_names (user_id, display_name) VALUES (%s, %s) "
                "ON DUPLICATE KEY UPDATE display_name = VALUES(display_name)",
                (uid, name)
            )
            count += 1
        except Exception:
            pass
    print(f"  Cached {count} member names from historical data.")

    count = 0
    for cid, name in channel_ids_seen:
        try:
            await db.execute(
                "INSERT INTO channel_names (channel_id, channel_name) VALUES (%s, %s) "
                "ON DUPLICATE KEY UPDATE channel_name = VALUES(channel_name)",
                (cid, name)
            )
            count += 1
        except Exception:
            pass
    print(f"  Cached {count} channel names from historical data.")


async def repair_granular_json(days: int):
    """
    Re-process existing granular_json entries: look up IDs in the cache tables
    and replace numeric-only name fields with resolved names.
    """
    print(f"\nPhase 2: Repairing granular_json for the last {days} days...")

    # Build lookup dicts from cache tables
    member_rows = await db.fetch_all("SELECT user_id, display_name FROM member_names")
    member_cache = {r['user_id']: r['display_name'] for r in member_rows}
    print(f"  Member cache: {len(member_cache)} entries")

    channel_rows = await db.fetch_all("SELECT channel_id, channel_name FROM channel_names")
    channel_cache = {r['channel_id']: r['channel_name'] for r in channel_rows}
    print(f"  Channel cache: {len(channel_cache)} entries")

    today = date.today()
    repaired = 0

    for i in range(1, days + 1):
        target_date = today - timedelta(days=i)
        target_str = str(target_date)

        row = await db.fetch_one(
            "SELECT granular_json FROM analytics_daily_rollups WHERE date = %s", (target_str,)
        )
        if not row or not row.get('granular_json'):
            continue

        try:
            g = json.loads(row['granular_json']) if isinstance(row['granular_json'], str) else row['granular_json']
        except (json.JSONDecodeError, TypeError):
            continue

        changed = False

        # Fix user names in quiz_top_3, thanks_top_3, top_invites
        for section_key, id_field in [('quiz_top_3', 'user_id'), ('thanks_top_3', 'user_id'), ('top_invites', 'inviter')]:
            for entry in g.get(section_key, []):
                uid = entry.get(id_field)
                current_name = str(entry.get('name', ''))
                if uid and (current_name.isdigit() or current_name == str(uid)):
                    resolved = member_cache.get(uid)
                    if resolved:
                        entry['name'] = resolved
                        changed = True

        # Fix channel names in top_text_channels, top_voice_channels
        for section_key in ['top_text_channels', 'top_voice_channels']:
            for entry in g.get(section_key, []):
                cid = entry.get('channel_id')
                current_name = str(entry.get('name', ''))
                if cid and (current_name.isdigit() or current_name == str(cid)):
                    resolved = channel_cache.get(cid)
                    if resolved:
                        entry['name'] = resolved
                        changed = True

        # Ensure all counts are integers
        for key in ['total_mod_actions', 'new_verifications', 'new_tickets', 'quiz_sessions',
                     'quiz_score', 'thanks_given', 'quests_completed', 'new_referrals',
                     'ep_redemptions', 'booster_raffle_wins', 'event_raffles_created',
                     'event_raffle_entries', 'event_participation_claims', 'event_ep_distributed',
                     'event_registrations', 'ticket_ratings_count']:
            if key in g and g[key] is not None:
                try:
                    g[key] = int(g[key])
                    changed = True
                except (ValueError, TypeError):
                    pass

        if g.get('mod_actions'):
            for action in g['mod_actions']:
                try:
                    g['mod_actions'][action] = int(g['mod_actions'][action])
                    changed = True
                except (ValueError, TypeError):
                    pass

        if changed:
            import decimal, datetime as dt
            def json_serial(obj):
                if isinstance(obj, decimal.Decimal):
                    return float(obj)
                if isinstance(obj, (dt.datetime, dt.date)):
                    return obj.isoformat()
                return str(obj)

            new_json = json.dumps(g, default=json_serial)
            await db.execute(
                "UPDATE analytics_daily_rollups SET granular_json = %s WHERE date = %s",
                (new_json, target_str)
            )
            repaired += 1
            print(f"  [{target_str}] REPAIRED")
        else:
            print(f"  [{target_str}] OK (no changes needed)")

    print(f"\nDone. Repaired {repaired} rollup(s).")


async def main(days: int):
    await db.get_pool()
    await populate_caches_from_rollups()
    await repair_granular_json(days)
    await db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Repair analytics granular_json with resolved names")
    parser.add_argument("--days", type=int, default=30, help="Number of days to scan back (default: 30)")
    args = parser.parse_args()
    asyncio.run(main(args.days))
