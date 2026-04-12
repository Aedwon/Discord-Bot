import asyncio
import sys
import os
import json
from datetime import date, timedelta

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.database import db
from services.analytics_service import analytics_service

async def run_backfill(days=14):
    await db.get_pool()
    print(f"Starting granular_json backfill for the last {days} days...")
    
    today = date.today()
    for i in range(1, days + 1):
        target_date = today - timedelta(days=i)
        target_str = str(target_date)
        
        # Check if row exists
        row = await db.fetch_one("SELECT * FROM analytics_daily_rollups WHERE date = %s", (target_str,))
        if not row:
            print(f"[{target_str}] SKIP: No base core data found.")
            continue
            
        print(f"[{target_str}] Processing granular data...")
        try:
            granular_stats = await analytics_service.get_exhaustive_daily_stats(target_str)
            
            def json_serial(obj):
                import decimal, datetime
                if isinstance(obj, decimal.Decimal):
                    return float(obj)
                if isinstance(obj, (datetime.datetime, datetime.date)):
                    return obj.isoformat()
                return str(obj)
                
            granular_json_str = json.dumps(granular_stats, default=json_serial)
            
            await db.execute(
                "UPDATE analytics_daily_rollups SET granular_json = %s WHERE date = %s",
                (granular_json_str, target_str)
            )
            print(f"[{target_str}] SUCCESS: Backfilled successfully.")
        except Exception as e:
            print(f"[{target_str}] ERROR: {e}")

if __name__ == "__main__":
    asyncio.run(run_backfill(14))
