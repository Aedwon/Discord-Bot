import asyncio
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services.database import db

async def check_schemas():
    await db.get_pool()
    tables = [
        "verified_users",
        "referrals",
        "quest_progress",
        "member_names",
        "channel_names"
    ]
    
    for table in tables:
        try:
            res = await db.fetch_all(f"DESCRIBE {table}")
            print(f"\n--- {table} ---")
            for col in res:
                print(f"{col['Field']}: {col['Type']} (Null: {col['Null']})")
        except Exception as e:
            print(f"Error describing {table}: {e}")
            
    await db.close()

if __name__ == "__main__":
    asyncio.run(check_schemas())
