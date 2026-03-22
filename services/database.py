"""
Async database connection management using aiomysql.
Provides a centralized database pool for all services.
"""

import aiomysql
import logging
from config import DB_CONFIG

logger = logging.getLogger('mlbb_bot')

class Database:
    """Async MySQL database wrapper."""
    
    _instance = None
    _pool = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    async def get_pool(self):
        """Initialize the database connection pool."""
        if self._pool is None:
            try:
                self._pool = await aiomysql.create_pool(**DB_CONFIG)
                logger.info(f"Connected to MySQL database: {DB_CONFIG['db']}")
                await self._init_tables()
            except Exception as e:
                logger.error(f"Failed to connect to MySQL: {e}")
                raise
        return self._pool
    
    async def _init_tables(self):
        # Users table for XP logging, Economy tokens, and native Event Points
        await self.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                xp INT DEFAULT 0,
                tokens INT DEFAULT 0,
                event_points INT DEFAULT 0,
                last_ep_update DATETIME DEFAULT CURRENT_TIMESTAMP,
                xp_multiplier FLOAT DEFAULT 1.0,
                shop_discount FLOAT DEFAULT 0.0,
                boost_start_date DATETIME DEFAULT NULL,
                xp_locked BOOLEAN DEFAULT FALSE,
                xp_lock_until DATETIME DEFAULT NULL,
        ''')
        
        await self.execute('''
            CREATE TABLE IF NOT EXISTS mod_logs (
                id INT PRIMARY KEY AUTO_INCREMENT,
                action_type VARCHAR(50) NOT NULL,
                moderator_id BIGINT NOT NULL,
                target_id BIGINT NOT NULL,
                reason TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Server settings table for storing role/channel IDs
        await self.execute('''
            CREATE TABLE IF NOT EXISTS server_settings (
                `key` VARCHAR(255) PRIMARY KEY,
                value TEXT NOT NULL
            )
        ''')
        
        # Event system tables
        await self.execute('''
            CREATE TABLE IF NOT EXISTS event_codes (
                code VARCHAR(50) PRIMARY KEY,
                reward_tokens INT DEFAULT 0,
                reward_ep INT DEFAULT 0,
                expires_at DATETIME,
                max_uses INT DEFAULT 0,
                uses_count INT DEFAULT 0,
                required_vc_id BIGINT,
                creator_id BIGINT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        await self.execute('''
            CREATE TABLE IF NOT EXISTS event_redemptions (
                id INT PRIMARY KEY AUTO_INCREMENT,
                code VARCHAR(50),
                user_id BIGINT,
                redeemed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (code) REFERENCES event_codes(code) ON DELETE CASCADE
            )
        ''')
        
        # Scheduled embeds table (for Discohook embed scheduling)
        await self.execute('''
            CREATE TABLE IF NOT EXISTS scheduled_embeds (
                identifier VARCHAR(10) PRIMARY KEY,
                channel_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                content TEXT,
                embed_json MEDIUMTEXT NOT NULL,
                schedule_for DATETIME NOT NULL,
                status VARCHAR(20) DEFAULT 'pending'
            )
        ''')
        
        # Guild settings table (for per-guild configuration)
        await self.execute('''
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id BIGINT PRIMARY KEY,
                embed_log_channel_id BIGINT DEFAULT NULL
            )
        ''')
        
        # Auto-create voice channel configs
        await self.execute('''
            CREATE TABLE IF NOT EXISTS autocreate_configs (
                voice_channel_id BIGINT PRIMARY KEY,
                category_id BIGINT
            )
        ''')
        
        # Message Cache for moderation logs
        await self.execute('''
            CREATE TABLE IF NOT EXISTS message_cache (
                message_id BIGINT PRIMARY KEY,
                channel_id BIGINT NOT NULL,
                author_id BIGINT NOT NULL,
                author_name VARCHAR(255),
                author_avatar TEXT,
                content TEXT,
                media_urls TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Create indices for the Universal Leaderboard engine to optimize memory
        await self.execute('CREATE INDEX IF NOT EXISTS idx_users_xp ON users (xp)')
        await self.execute('CREATE INDEX IF NOT EXISTS idx_users_ep ON users (event_points)')
        
        # Event Kiosks (Linked to Native Discord Events)
        await self.execute('''
            CREATE TABLE IF NOT EXISTS guild_event_kiosks (
                message_id BIGINT PRIMARY KEY,
                event_id BIGINT NOT NULL,
                ep_amount INT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Event Rewards Tracker (Anti-cheat & No-Stacking)
        await self.execute('''
            CREATE TABLE IF NOT EXISTS guild_event_rewards (
                event_id BIGINT,
                user_id BIGINT,
                reward_type VARCHAR(50),
                ep_awarded INT,
                awarded_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (event_id, user_id, reward_type)
            )
        ''')
        
        # Event Placement Manager Caps
        await self.execute('''
            CREATE TABLE IF NOT EXISTS guild_event_caps (
                event_id BIGINT PRIMARY KEY,
                total_budget INT NOT NULL,
                set_by BIGINT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Event Analytics Tracking (Peak VC)
        await self.execute('''
            CREATE TABLE IF NOT EXISTS guild_event_stats (
                event_id BIGINT PRIMARY KEY,
                peak_concurrent INT DEFAULT 0,
                last_updated DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            )
        ''')
        
        # Event Overflow Channel Links
        await self.execute('''
            CREATE TABLE IF NOT EXISTS guild_event_overflows (
                event_id BIGINT,
                channel_id BIGINT,
                PRIMARY KEY (event_id, channel_id)
            )
        ''')
    
    async def close(self):
        """Close the database connection."""
        if self._pool:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None
    
    async def execute(self, query: str, params: tuple = ()):
        """Execute a query and return cursor (or lastrowid for inserts)."""
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, params)
                # For inserts, return the last ID
                if query.strip().upper().startswith("INSERT"):
                    return cur
                return cur
    
    async def fetch_one(self, query: str, params: tuple = ()):
        """Fetch a single row."""
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(query, params)
                return await cur.fetchone()
    
    async def fetch_all(self, query: str, params: tuple = ()):
        """Fetch all rows."""
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(query, params)
                return await cur.fetchall()


# Singleton instance
db = Database()
