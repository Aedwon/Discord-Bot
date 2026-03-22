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
                badges TEXT,
                consecutive_active_days INT DEFAULT 0,
                last_active_date DATE DEFAULT NULL,
                thanks_received INT DEFAULT 0,
                lifetime_tokens INT DEFAULT 0,
                consecutive_events_attended INT DEFAULT 0
            )
        ''')
        
        # Safe Migrations for new column additions
        new_columns = [
            ("badges", "TEXT"),
            ("consecutive_active_days", "INT DEFAULT 0"),
            ("last_active_date", "DATE DEFAULT NULL"),
            ("thanks_received", "INT DEFAULT 0"),
            ("lifetime_tokens", "INT DEFAULT 0"),
            ("consecutive_events_attended", "INT DEFAULT 0")
        ]
        
        for col_name, col_def in new_columns:
            try:
                await self.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_def}")
                if col_name == "badges":
                    await self.execute("UPDATE users SET badges = '[]' WHERE badges IS NULL")
            except Exception:
                pass
        
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
        
        # Thanks history for global and targeted cooldown tracking
        await self.execute('''
            CREATE TABLE IF NOT EXISTS thanks_history (
                id INT AUTO_INCREMENT PRIMARY KEY,
                sender_id BIGINT NOT NULL,
                receiver_id BIGINT NOT NULL,
                reason TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_ths_sender (sender_id),
                INDEX idx_ths_created (created_at)
            )
        ''')
        
        # Server settings table for storing role/channel IDs
        await self.execute('''
            CREATE TABLE IF NOT EXISTS server_settings (
                `key` VARCHAR(255) PRIMARY KEY,
                value TEXT NOT NULL
            )
        ''')
        
        # Verification table — links Discord users to MLBB accounts
        await self.execute('''
            CREATE TABLE IF NOT EXISTS verified_users (
                user_id BIGINT PRIMARY KEY,
                full_name VARCHAR(255) NOT NULL,
                mlbb_uid BIGINT NOT NULL UNIQUE,
                mlbb_server INT NOT NULL,
                verified_at DATETIME DEFAULT CURRENT_TIMESTAMP
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
        
        # Booster Raffle History
        await self.execute('''
            CREATE TABLE IF NOT EXISTS booster_raffle_history (
                id INT AUTO_INCREMENT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                won_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_brh_won_at (won_at)
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
        
        # ─── ANALYTICS ENGINE TABLES ────────────────────────────────────
        
        # Message metadata + content (30-day rolling window, purged nightly)
        await self.execute('''
            CREATE TABLE IF NOT EXISTS analytics_messages (
                id INT AUTO_INCREMENT PRIMARY KEY,
                channel_id BIGINT NOT NULL,
                author_id BIGINT NOT NULL,
                content TEXT,
                has_link BOOLEAN DEFAULT FALSE,
                word_count SMALLINT DEFAULT 0,
                is_deleted BOOLEAN DEFAULT FALSE,
                hour_of_day TINYINT NOT NULL,
                day_of_week TINYINT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_am_created (created_at),
                INDEX idx_am_channel (channel_id, created_at),
                INDEX idx_am_author (author_id)
            )
        ''')
        
        # Voice session duration tracking
        await self.execute('''
            CREATE TABLE IF NOT EXISTS analytics_voice_sessions (
                id INT AUTO_INCREMENT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                channel_id BIGINT NOT NULL,
                joined_at DATETIME NOT NULL,
                left_at DATETIME DEFAULT NULL,
                INDEX idx_avs_user (user_id),
                INDEX idx_avs_channel (channel_id, joined_at)
            )
        ''')
        
        # Member join/leave with invite attribution
        await self.execute('''
            CREATE TABLE IF NOT EXISTS analytics_member_joins (
                id INT AUTO_INCREMENT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                invite_code VARCHAR(20) DEFAULT NULL,
                inviter_id BIGINT DEFAULT NULL,
                joined_at DATETIME NOT NULL,
                left_at DATETIME DEFAULT NULL,
                INDEX idx_amj_joined (joined_at)
            )
        ''')
        
        # Reaction engagement tracking
        await self.execute('''
            CREATE TABLE IF NOT EXISTS analytics_reactions (
                id INT AUTO_INCREMENT PRIMARY KEY,
                message_id BIGINT NOT NULL,
                channel_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                emoji VARCHAR(100) NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_ar_created (created_at)
            )
        ''')
        
        # Event RSVP tracking
        await self.execute('''
            CREATE TABLE IF NOT EXISTS analytics_event_rsvps (
                event_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                rsvped_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (event_id, user_id)
            )
        ''')
        
        # Tracked link buttons (interceptor pattern for CTR)
        await self.execute('''
            CREATE TABLE IF NOT EXISTS analytics_tracked_links (
                id INT AUTO_INCREMENT PRIMARY KEY,
                label VARCHAR(200) NOT NULL,
                url TEXT NOT NULL,
                message_id BIGINT NOT NULL,
                channel_id BIGINT NOT NULL,
                created_by BIGINT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Per-user unique click deduplication
        await self.execute('''
            CREATE TABLE IF NOT EXISTS analytics_link_clicks (
                id INT AUTO_INCREMENT PRIMARY KEY,
                link_id INT NOT NULL,
                user_id BIGINT NOT NULL,
                clicked_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY unique_click (link_id, user_id)
            )
        ''')
        
        # Permanent daily aggregated summaries (never purged)
        await self.execute('''
            CREATE TABLE IF NOT EXISTS analytics_daily_rollups (
                date DATE PRIMARY KEY,
                total_messages INT DEFAULT 0,
                unique_messagers INT DEFAULT 0,
                total_voice_minutes INT DEFAULT 0,
                unique_voice_users INT DEFAULT 0,
                new_joins INT DEFAULT 0,
                new_leaves INT DEFAULT 0,
                total_reactions INT DEFAULT 0,
                total_threads INT DEFAULT 0
            )
        ''')
        
        # Keyword match tracking for sentiment export
        await self.execute('''
            CREATE TABLE IF NOT EXISTS analytics_keywords (
                id INT AUTO_INCREMENT PRIMARY KEY,
                keyword VARCHAR(100) NOT NULL,
                channel_id BIGINT NOT NULL,
                author_id BIGINT NOT NULL,
                message_content TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_ak_keyword (keyword, created_at)
            )
        ''')
        
        # Quiz daily payout deduplication (prevents double payouts for 2x daily sessions)
        await self.execute('''
            CREATE TABLE IF NOT EXISTS quiz_payouts (
                user_id BIGINT NOT NULL,
                payout_date DATE NOT NULL,
                ep_awarded INT DEFAULT 0,
                PRIMARY KEY (user_id, payout_date)
            )
        ''')
        
        # Ticket system tables
        await self.execute('''
            CREATE TABLE IF NOT EXISTS active_tickets (
                channel_id BIGINT PRIMARY KEY,
                creator_id BIGINT NOT NULL,
                category_key VARCHAR(10) NOT NULL,
                subject VARCHAR(255),
                claimed BOOLEAN DEFAULT FALSE,
                claimed_by BIGINT DEFAULT NULL,
                added_users TEXT DEFAULT '[]',
                is_test BOOLEAN DEFAULT FALSE,
                reminded_24h BOOLEAN DEFAULT FALSE,
                escalated_48h BOOLEAN DEFAULT FALSE,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        await self.execute('''
            CREATE TABLE IF NOT EXISTS ticket_ratings (
                id INT PRIMARY KEY AUTO_INCREMENT,
                ticket_name VARCHAR(255),
                user_id BIGINT NOT NULL,
                handler_id BIGINT,
                stars INT NOT NULL,
                remarks TEXT,
                rated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        await self.execute('''
            CREATE TABLE IF NOT EXISTS pending_ratings (
                id INT PRIMARY KEY AUTO_INCREMENT,
                ticket_name VARCHAR(255),
                handler_id BIGINT,
                handler_mention VARCHAR(255),
                is_test BOOLEAN DEFAULT FALSE,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
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
    
    async def insert_get_id(self, query: str, params: tuple = ()) -> int:
        """Execute an INSERT and return the AUTO_INCREMENT id."""
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, params)
                return cur.lastrowid
    
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
