"""
XP Service - Business logic for XP/Leveling system.
Handles all XP calculations and database operations.
"""

import bisect
from datetime import datetime
from services.database import db


# ─── Precomputed Cumulative XP Table ────────────────────────────────────
# Cost from level k to k+1 = 150 * k^1.9
# Cumulative XP to reach level L = sum(int(150 * k**1.9) for k in 1..L-1)
#
# _LEVEL_THRESHOLDS[i] = cumulative XP required to reach level (i + 1).
# So _LEVEL_THRESHOLDS[0] = 0 (level 1), _LEVEL_THRESHOLDS[1] = 150 (level 2), etc.
_MAX_PRECOMPUTED = 200

def _build_level_thresholds(max_level: int = _MAX_PRECOMPUTED) -> list[int]:
    """Build cumulative XP thresholds for levels 1 through max_level."""
    thresholds = [0]  # Level 1 requires 0 XP
    cumulative = 0
    for k in range(1, max_level):
        cumulative += int(150 * (k ** 1.9))
        thresholds.append(cumulative)
    return thresholds

_LEVEL_THRESHOLDS = _build_level_thresholds()


class XpService:
    """Handles XP-related business logic."""
    
    def get_level(self, xp: int) -> int:
        """Determine level from cumulative XP using precomputed thresholds.
        
        Uses bisect on the sorted threshold table for O(log n) lookup.
        Handles XP values beyond the precomputed range via dynamic extension.
        """
        if xp <= 0:
            return 1
        
        # bisect_right returns the insertion point; the level is that index.
        # e.g. xp=150 → bisect finds index 2 → level 2
        idx = bisect.bisect_right(_LEVEL_THRESHOLDS, xp) - 1
        
        # If XP exceeds precomputed range, extend dynamically
        if idx >= len(_LEVEL_THRESHOLDS) - 1 and xp >= _LEVEL_THRESHOLDS[-1]:
            level = len(_LEVEL_THRESHOLDS)
            cumulative = _LEVEL_THRESHOLDS[-1]
            while cumulative <= xp:
                cumulative += int(150 * (level ** 1.9))
                level += 1
            return level - 1
        
        return max(1, idx + 1)
    
    def get_xp_for_level(self, level: int) -> int:
        """Return the cumulative XP required to reach a given level.
        
        Cost from level k to k+1 = 150 * k^1.9.
        Total to level L = sum(int(150 * k**1.9) for k in 1..L-1).
        """
        if level <= 1:
            return 0
        
        if level <= len(_LEVEL_THRESHOLDS):
            return _LEVEL_THRESHOLDS[level - 1]
        
        # Dynamic computation for levels beyond precomputed range
        cumulative = _LEVEL_THRESHOLDS[-1]
        for k in range(len(_LEVEL_THRESHOLDS), level):
            cumulative += int(150 * (k ** 1.9))
        return cumulative
        
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
    
    async def add_xp(self, user_id: int, amount: int, bypass_lock: bool = False, bypass_verification: bool = False) -> int:
        """
        Add XP to a user (with multiplier applied) and return new total.
        Returns 0 if user is XP locked or unverified without a bypass.
        """
        from services.verification_service import verification_service
        if not bypass_verification and not verification_service.is_verified(user_id):
            return 0

        # Check XP lock
        if not bypass_lock and await self.is_xp_locked(user_id):
            return 0
        
        # Get user's multiplier
        multiplier = await self.get_multiplier(user_id)
        final_xp = int(amount * multiplier)
        
        await db.execute('''
            INSERT INTO users (user_id, xp, consecutive_active_days, last_active_date) 
            VALUES (%s, %s, 1, CURDATE())
            ON DUPLICATE KEY UPDATE 
                xp = users.xp + %s,
                consecutive_active_days = IF(users.last_active_date = CURDATE(), users.consecutive_active_days, IF(users.last_active_date = DATE_SUB(CURDATE(), INTERVAL 1 DAY), users.consecutive_active_days + 1, 1)),
                last_active_date = CURDATE()
        ''', (user_id, final_xp, final_xp))
        
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
                INSERT INTO users (user_id, xp, consecutive_active_days, last_active_date) 
                VALUES (%s, %s, 1, CURDATE())
                ON DUPLICATE KEY UPDATE 
                    xp = users.xp + %s,
                    consecutive_active_days = IF(users.last_active_date = CURDATE(), users.consecutive_active_days, IF(users.last_active_date = DATE_SUB(CURDATE(), INTERVAL 1 DAY), users.consecutive_active_days + 1, 1)),
                    last_active_date = CURDATE()
            ''', (user_id, final_xp, final_xp))
            
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
                xp_multiplier = %s,
                shop_discount = %s,
                boost_start_date = COALESCE(users.boost_start_date, %s)
        ''', (user_id, xp_multiplier, shop_discount, start_date, xp_multiplier, shop_discount, start_date))
    
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
        
    async def award_currency(self, user_id: int, xp: int = 0, tokens: int = 0, ep: int = 0, bypass_verification: bool = False):
        """Award arbitrary currency to a user (Event System)."""
        from services.verification_service import verification_service
        if not bypass_verification and not verification_service.is_verified(user_id):
            return
        await db.execute('''
            INSERT INTO users (user_id, xp, tokens, event_points, lifetime_tokens) 
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                xp = users.xp + %s,
                tokens = users.tokens + %s,
                event_points = users.event_points + %s,
                lifetime_tokens = users.lifetime_tokens + %s
        ''', (user_id, xp, tokens, ep, max(0, tokens), xp, tokens, ep, max(0, tokens)))
        
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
            INSERT INTO users (user_id) VALUES (%s)
            ON DUPLICATE KEY UPDATE user_id = VALUES(user_id)
        ''', (user_id,))
        
        query = f"UPDATE users SET {', '.join(updates)} WHERE user_id = %s"
        params.append(user_id)
        
        await db.execute(query, tuple(params))


# Singleton instance
xp_service = XpService()

