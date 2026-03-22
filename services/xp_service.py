"""
XP Service - Business logic for XP/Leveling system.
Handles all XP calculations and database operations.
"""

from datetime import datetime
from services.database import db


class XpService:
    """Handles XP-related business logic."""
    
    def get_level(self, xp: int) -> int:
        """Calculate mathematical level using the custom exponential curve 150 * (Lvl^1.9)."""
        if xp <= 0: return 1
        return int((xp / 150) ** (1 / 1.9)) + 1
        
    def get_tier_name(self, level: int) -> str | None:
        """Mathematical Mapping: Level 1-100 to 22 distinctly named Role Tiers."""
        if level <= 0: return None
        if level >= 101: return "Monarch"
        
        ranks = ["Commoner", "Vassal", "Noble", "High Noble"]
        rank_idx = min((level - 1) // 25, 3)
        tier = ((level - 1) % 25) // 5 
        numerals = ["V", "IV", "III", "II", "I"]
        
        return f"{ranks[rank_idx]} {numerals[tier]}"
        
    def get_tier_name(self, level: int) -> str | None:
        """Mathematical Mapping: Level 1-100 to 22 distinctly named Role Tiers."""
        if level <= 0: return None
        if level >= 101: return "Monarch"
        
        ranks = ["Commoner", "Vassal", "Noble", "High Noble"]
        rank_idx = min((level - 1) // 25, 3)
        tier = ((level - 1) % 25) // 5 
        numerals = ["V", "IV", "III", "II", "I"]
        
        return f"{ranks[rank_idx]} {numerals[tier]}"

    
    async def get_multiplier(self, user_id: int) -> float:
        """Get a user's current XP multiplier."""
        result = await db.fetch_one(
            'SELECT xp_multiplier FROM users WHERE user_id = %s',
            (user_id,)
        )
        return result['xp_multiplier'] if result and result['xp_multiplier'] else 1.0
    
    async def is_xp_locked(self, user_id: int) -> bool:
        """Check if user is XP locked (from warn)."""
        result = await db.fetch_one(
            'SELECT xp_locked, xp_lock_until FROM users WHERE user_id = %s',
            (user_id,)
        )
        if not result or not result['xp_locked']:
            return False
        
        # Check if lock expired
        if result['xp_lock_until']:
            # In MySQL, DATETIME comes back as datetime object or needs parsing
            # aiosqlite returned str, aiomysql usually returns datetime
            lock_until = result['xp_lock_until']
            if isinstance(lock_until, str):
                lock_until = datetime.fromisoformat(lock_until)
                
            if datetime.now() > lock_until:
                # Auto-remove expired lock
                await db.execute('UPDATE users SET xp_locked = 0, xp_lock_until = NULL WHERE user_id = %s', (user_id,))
                return False
        return True
    
    async def add_xp(self, user_id: int, amount: int) -> int:
        """
        Add XP to a user (with multiplier applied) and return new total.
        Returns 0 if user is XP locked.
        """
        # Check XP lock
        if await self.is_xp_locked(user_id):
            return 0
        
        # Get user's multiplier
        multiplier = await self.get_multiplier(user_id)
        final_xp = int(amount * multiplier)
        
        await db.execute('''
            INSERT INTO users (user_id, xp) VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE xp = xp + VALUES(xp)
        ''', (user_id, final_xp))
        
        result = await db.fetch_one(
            'SELECT xp FROM users WHERE user_id = %s', 
            (user_id,)
        )
        return result['xp'] if result else final_xp
    
    async def get_xp(self, user_id: int) -> int:
        """Get a user's current XP."""
        result = await db.fetch_one(
            'SELECT xp FROM users WHERE user_id = %s', 
            (user_id,)
        )
        return result['xp'] if result else 0
    
    async def get_leaderboard(self, limit: int = 10) -> list:
        """Get the top users by XP."""
        rows = await db.fetch_all(
            'SELECT user_id, xp FROM users ORDER BY xp DESC LIMIT %s',
            (limit,)
        )
        return [(row['user_id'], row['xp']) for row in rows]
    
    async def batch_update(self, pending_xp: dict) -> dict:
        """
        Batch update XP for multiple users (with multipliers applied).
        Returns dictionary of user_id -> {'old_xp': int, 'new_xp': int} for role assignment UI hook.
        """
        results = {}
        for user_id, xp in pending_xp.items():
            old_xp = await self.get_xp(user_id)
            multiplier = await self.get_multiplier(user_id)
            final_xp = int(xp * multiplier)
            
            await db.execute('''
                INSERT INTO users (user_id, xp) VALUES (%s, %s)
                ON DUPLICATE KEY UPDATE xp = xp + VALUES(xp)
            ''', (user_id, final_xp))
            
            new_xp = await self.get_xp(user_id)
            results[user_id] = {"old_xp": old_xp, "new_xp": new_xp}
        return results
    
    async def get_rank(self, user_id: int) -> tuple:
        """Get a user's rank and XP."""
        xp = await self.get_xp(user_id)
        if xp == 0:
            return (None, 0)
        
        result = await db.fetch_one('''
            SELECT COUNT(*) + 1 as rank 
            FROM users 
            WHERE xp > (SELECT xp FROM users WHERE user_id = %s)
        ''', (user_id,))
        
        return (result['rank'], xp) if result else (None, xp)
    
    # ─────────────────────────────────────────────────────────────────────
    # Booster Perks Methods
    # ─────────────────────────────────────────────────────────────────────
    
    async def set_booster_perks(
        self, 
        user_id: int, 
        xp_multiplier: float, 
        shop_discount: float,
        boost_start_date: datetime = None
    ) -> None:
        """
        Set booster perks for a user.
        """
        start_date = boost_start_date or datetime.now()
        
        await db.execute('''
            INSERT INTO users (user_id, xp_multiplier, shop_discount, boost_start_date)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE 
                xp_multiplier = VALUES(xp_multiplier),
                shop_discount = VALUES(shop_discount),
                boost_start_date = COALESCE(boost_start_date, VALUES(boost_start_date))
        ''', (user_id, xp_multiplier, shop_discount, start_date))
    
    async def remove_booster_perks(self, user_id: int) -> None:
        """Remove all booster perks from a user."""
        await db.execute('''
            UPDATE users SET 
                xp_multiplier = 1.0,
                shop_discount = 0.0,
                boost_start_date = NULL
            WHERE user_id = %s
        ''', (user_id,))
    
    async def get_boost_start_date(self, user_id: int) -> datetime | None:
        """Get when a user started boosting."""
        result = await db.fetch_one(
            'SELECT boost_start_date FROM users WHERE user_id = %s',
            (user_id,)
        )
        if result and result['boost_start_date']:
            return result['boost_start_date'] # aiomysql returns datetime object
        return None
    
    async def get_user_perks(self, user_id: int) -> dict:
        """Get all perks for a user."""
        result = await db.fetch_one('''
            SELECT xp_multiplier, shop_discount, boost_start_date
            FROM users WHERE user_id = %s
        ''', (user_id,))
        
        if result:
            return {
                'xp_multiplier': result['xp_multiplier'] or 1.0,
                'shop_discount': result['shop_discount'] or 0.0,
                'boost_start_date': result['boost_start_date'],
            }
        return {'xp_multiplier': 1.0, 'shop_discount': 0.0, 'boost_start_date': None}
        
    async def award_currency(self, user_id: int, xp: int = 0, tokens: int = 0, ep: int = 0):
        """Award arbitrary currency to a user (Event System)."""
        await db.execute('''
            INSERT INTO users (user_id, xp, tokens, event_points) 
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                xp = xp + VALUES(xp),
                tokens = tokens + VALUES(tokens),
                event_points = event_points + VALUES(event_points)
        ''', (user_id, xp, tokens, ep))
        
    async def set_currency(self, user_id: int, xp: int = None, tokens: int = None, ep: int = None):
        """Force set arbitrary currency for a user. None means unchanged."""
        updates = []
        params = []
        if xp is not None:
            updates.append("xp = %s")
            params.append(xp)
        if tokens is not None:
            updates.append("tokens = %s")
            params.append(tokens)
        if ep is not None:
            updates.append("event_points = %s")
            params.append(ep)
            
        if not updates:
            return
            
        # Ensure row exists first
        await db.execute('''
            INSERT IGNORE INTO users (user_id) VALUES (%s)
        ''', (user_id,))
        
        query = f"UPDATE users SET {', '.join(updates)} WHERE user_id = %s"
        params.append(user_id)
        
        await db.execute(query, tuple(params))


# Singleton instance
xp_service = XpService()

