"""
Rebuild Analytics Rollups — Complete historical data regeneration for the exhaustive dashboard.
Usage:
    python scripts/rebuild_rollups.py [--days 7]
"""

import asyncio
import sys
import os
import json
import argparse
from datetime import date, timedelta
import decimal
import datetime as dt

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.database import db
from services.analytics_service import AnalyticsService

analytics_service = AnalyticsService()

def json_serial(obj):
    """JSON serializer for objects not serializable by default json code"""
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    if isinstance(obj, (dt.datetime, dt.date)):
        return obj.isoformat()
    return str(obj)

async def rebuild_rollups(days: int):
    print(f"\n🚀 Phase: EXHAUSTIVE REBUILD (Last {days} days)")
    print("This will regenerate all granular_json fields to include Social, Heatmap, and Economy data.\n")

    today = date.today()
    rebuild_count = 0

    for i in range(1, days + 1):
        target_date = today - timedelta(days=i)
        target_str = str(target_date)

        print(f"  [ {target_str} ] Aggregating data...", end="\r")
        
        try:
            # 1. Fetch the exhaustive stats for this date from the service
            stats = await analytics_service.get_exhaustive_daily_stats(target_str)
            
            # 2. Serialize to JSON
            stats_json = json.dumps(stats, default=json_serial)

            # 3. Update the existing rollup (or insert if missing)
            # We use ON DUPLICATE KEY UPDATE to ensure we don't break existing total_messages/etc.
            # but we overwrite the granular_json with the new exhaustive one.
            await db.execute('''
                INSERT INTO analytics_daily_rollups (date, total_messages, total_voice_minutes, new_joins, new_leaves, unique_messagers, unique_voice_users, total_reactions, granular_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE 
                    granular_json = VALUES(granular_json),
                    total_reactions = VALUES(total_reactions)
            ''', (
                target_str, 
                stats.get('total_messages', 0),
                stats.get('total_voice_minutes', 0),
                stats.get('new_joins', 0),
                stats.get('new_leaves', 0),
                stats.get('unique_messagers', 0),
                stats.get('unique_voice_users', 0),
                stats.get('total_reactions', 0),
                stats_json
            ))
            
            print(f"  [ {target_str} ] ✅ REBUILT")
            rebuild_count += 1
        except Exception as e:
            print(f"  [ {target_str} ] ❌ FAILED: {e}")

    print(f"\nDone! Successfully rebuilt {rebuild_count} historical day(s).")

async def main(days: int):
    await db.get_pool()
    await rebuild_rollups(days)
    await db.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Exhaustively rebuild historical analytics rollups")
    parser.add_argument("--days", type=int, default=7, help="Number of days to rebuild (default: 7)")
    args = parser.parse_args()
    asyncio.run(main(args.days))
