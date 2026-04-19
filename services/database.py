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
            ("consecutive_events_attended", "INT DEFAULT 0"),
            ("last_ep_update", "DATETIME DEFAULT CURRENT_TIMESTAMP"),
            ("has_promo_status", "BOOLEAN DEFAULT FALSE"),
            ("ep_multiplier", "FLOAT DEFAULT 1.0"),
            ("notif_server_event", "BOOLEAN DEFAULT FALSE"),
            ("notif_quiz", "BOOLEAN DEFAULT FALSE"),
            ("notif_giveaway", "BOOLEAN DEFAULT FALSE"),
            ("notif_survey", "BOOLEAN DEFAULT FALSE"),
            ("notif_tournament", "BOOLEAN DEFAULT FALSE"),
            ("notif_partner_event", "BOOLEAN DEFAULT FALSE"),
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
                is_excess BOOLEAN DEFAULT FALSE,
                won_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_brh_won_at (won_at),
                INDEX idx_brh_excess (user_id, is_excess, won_at)
            )
        ''')
        
        # Safe migration: add is_excess column if missing
        try:
            await self.execute("ALTER TABLE booster_raffle_history ADD COLUMN is_excess BOOLEAN DEFAULT FALSE")
        except Exception:
            pass
        
        # Auto-create voice channel configs
        await self.execute('''
            CREATE TABLE IF NOT EXISTS autocreate_configs (
                voice_channel_id BIGINT PRIMARY KEY,
                category_id BIGINT
            )
        ''')
        
        # Track dynamically created virtual channels so they aren't orphaned on bot restart.
        await self.execute('''
            CREATE TABLE IF NOT EXISTS autocreate_active_vcs (
                channel_id BIGINT PRIMARY KEY
            )
        ''')
        
        # Quiz History table (for weekly leaderboards)
        await self.execute('''
            CREATE TABLE IF NOT EXISTS quiz_history (
                id INT AUTO_INCREMENT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                score INT NOT NULL,
                earned_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_qh_user_earned (user_id, earned_at)
            )
        ''')

        # Detailed answer logs (timestamped responses)
        await self.execute('''
            CREATE TABLE IF NOT EXISTS quiz_answer_logs (
                id INT AUTO_INCREMENT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                question_id VARCHAR(50),
                question_text TEXT,
                time_taken FLOAT NOT NULL,
                is_first BOOLEAN DEFAULT FALSE,
                earned_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_qal_time (time_taken),
                INDEX idx_qal_earned (earned_at)
            )
        ''')

        # Aggregated question performance
        await self.execute('''
            CREATE TABLE IF NOT EXISTS quiz_question_stats (
                question_id VARCHAR(50) PRIMARY KEY,
                question_text TEXT,
                times_asked INT DEFAULT 0,
                times_correct INT DEFAULT 0,
                total_time_taken FLOAT DEFAULT 0
            )
        ''')

        # Cross-session streak tracking
        await self.execute('''
            CREATE TABLE IF NOT EXISTS quiz_user_streaks (
                user_id BIGINT PRIMARY KEY,
                current_streak INT DEFAULT 0,
                max_streak INT DEFAULT 0,
                last_correct_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Message Cache for moderation logs
        # DEPRECATED: message_cache is now JSON-backed in log_cog.py (message_cache.json).
        # This table is kept for backward compatibility but is no longer written to.
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
        
        # ─── DUAL LEADERBOARD ENGINE TABLES ─────────────────────────────
        
        # Weekly snapshot: stores each user's XP/EP at the moment of the last
        # Monday 00:00 UTC+8 reset. Weekly delta = current_total - snapshot.
        await self.execute('''
            CREATE TABLE IF NOT EXISTS weekly_leaderboard_snapshots (
                user_id BIGINT PRIMARY KEY,
                xp_snapshot INT DEFAULT 0,
                ep_snapshot INT DEFAULT 0,
                snapshot_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Per-user counting contributions within the current ISO week.
        # Reset every Monday 00:00 UTC+8 alongside the leaderboard reset.
        await self.execute('''
            CREATE TABLE IF NOT EXISTS counting_weekly_contributors (
                guild_id BIGINT,
                user_id BIGINT,
                count INT DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )
        ''')
        
        # Archive of weekly leaderboard standings for reward processing.
        # Populated at Monday 00:00 UTC+8 before the weekly reset.
        await self.execute('''
            CREATE TABLE IF NOT EXISTS weekly_leaderboard_history (
                id INT AUTO_INCREMENT PRIMARY KEY,
                week_id VARCHAR(10) NOT NULL,
                category VARCHAR(20) NOT NULL,
                rank_position INT NOT NULL,
                user_id BIGINT NOT NULL,
                value BIGINT NOT NULL,
                extra_info VARCHAR(100) DEFAULT NULL,
                snapshot_at DATETIME NOT NULL,
                INDEX idx_wlh_week_cat (week_id, category),
                INDEX idx_wlh_user (user_id)
            )
        ''')
        
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
                total_threads INT DEFAULT 0,
                granular_json MEDIUMTEXT DEFAULT NULL
            )
        ''')
        
        # Safe migration for granular_json
        try:
            await self.execute("ALTER TABLE analytics_daily_rollups ADD COLUMN granular_json MEDIUMTEXT DEFAULT NULL")
        except Exception:
            pass
        
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
        
        # Event Raffles
        await self.execute('''
            CREATE TABLE IF NOT EXISTS event_raffles (
                id INT PRIMARY KEY AUTO_INCREMENT,
                host_id BIGINT NOT NULL,
                hosted_by BIGINT NULL,
                title VARCHAR(200) NOT NULL,
                prize TEXT NOT NULL,
                requirements TEXT NULL,
                winner_count INT NOT NULL DEFAULT 1,
                message_id BIGINT NULL,
                channel_id BIGINT NOT NULL,
                winners_thread_id BIGINT NULL,
                announcement_msg_id BIGINT NULL,
                welcome_msg_id BIGINT NULL,
                reroll_history TEXT NULL,
                ends_at DATETIME NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'active',
                winners TEXT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        try:
            await self.execute("ALTER TABLE event_raffles ADD COLUMN winners_thread_id BIGINT NULL")
        except Exception:
            pass
            
        try:
            await self.execute("ALTER TABLE event_raffles ADD COLUMN announcement_msg_id BIGINT NULL")
            await self.execute("ALTER TABLE event_raffles ADD COLUMN welcome_msg_id BIGINT NULL")
            await self.execute("ALTER TABLE event_raffles ADD COLUMN reroll_history TEXT DEFAULT '[]'")
        except Exception:
            pass
            
        await self.execute('''
            CREATE TABLE IF NOT EXISTS event_raffle_entries (
                raffle_id INT NOT NULL,
                user_id BIGINT NOT NULL,
                entered_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (raffle_id, user_id)
            )
        ''')
        
        # Counting game persistent state
        await self.execute('''
            CREATE TABLE IF NOT EXISTS counting_state (
                guild_id BIGINT PRIMARY KEY,
                current_count INT NOT NULL DEFAULT 0,
                last_user_id BIGINT DEFAULT NULL,
                last_message_id BIGINT DEFAULT NULL
            )
        ''')
        
        # Quest definition catalog (admin-managed, future progress/reward system builds on this)
        await self.execute('''
            CREATE TABLE IF NOT EXISTS quests (
                id INT AUTO_INCREMENT PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                description TEXT,
                tier ENUM('common', 'uncommon', 'rare') NOT NULL DEFAULT 'common',
                task_type ENUM('message_count', 'vc_minutes', 'reaction_count') NOT NULL,
                target_goal INT NOT NULL,
                is_active BOOLEAN DEFAULT TRUE,
                created_by BIGINT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Per-user daily quest assignments and progress tracking
        await self.execute('''
            CREATE TABLE IF NOT EXISTS quest_progress (
                id INT AUTO_INCREMENT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                quest_id INT NOT NULL,
                slot TINYINT NOT NULL,
                progress INT DEFAULT 0,
                completed BOOLEAN DEFAULT FALSE,
                completed_at DATETIME DEFAULT NULL,
                assigned_date DATE NOT NULL,
                INDEX idx_qp_user_date (user_id, assigned_date),
                FOREIGN KEY (quest_id) REFERENCES quests(id) ON DELETE CASCADE
            )
        ''')
        
        # Referral tracking system
        await self.execute('''
            CREATE TABLE IF NOT EXISTS referrals (
                user_id BIGINT PRIMARY KEY,
                own_code VARCHAR(20) NOT NULL UNIQUE,
                used_code VARCHAR(20) DEFAULT NULL,
                referred_by BIGINT DEFAULT NULL,
                total_referrals INT DEFAULT 0,
                prev_week_referrals INT DEFAULT 0,
                curr_week_referrals INT DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_ref_code (own_code),
                INDEX idx_ref_total (total_referrals)
            )
        ''')
        
        # New Unified Event Registration System
        await self.execute('''
            CREATE TABLE IF NOT EXISTS event_registrations (
                event_id BIGINT PRIMARY KEY,
                announcement_msg_id BIGINT NOT NULL,
                channel_id BIGINT NOT NULL,
                title VARCHAR(200) NOT NULL,
                thread_id BIGINT NULL,
                summary_msg_id BIGINT NULL,
                max_participants INT NULL,
                status VARCHAR(20) DEFAULT 'open',
                created_by BIGINT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY (announcement_msg_id)
            )
        ''')

        await self.execute('''
            CREATE TABLE IF NOT EXISTS event_registration_entries (
                event_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                registered_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (event_id, user_id),
                INDEX idx_event_reg_date (registered_at)
            )
        ''')

        await self.execute('''
            CREATE TABLE IF NOT EXISTS event_prize_pools (
                id INT AUTO_INCREMENT PRIMARY KEY,
                event_id BIGINT NOT NULL,
                placement_name VARCHAR(100) NOT NULL,
                ep_reward INT NOT NULL,
                max_winners INT NOT NULL DEFAULT 1,
                INDEX idx_epp_event (event_id)
            )
        ''')

        # ─── ANALYTICS IDENTITY CACHE ────────────────────────────────
        # Stores resolved Discord display names so the Vercel dashboard
        # (which has no bot connection) can show human-readable names.
        await self.execute('''
            CREATE TABLE IF NOT EXISTS member_names (
                user_id BIGINT PRIMARY KEY,
                display_name VARCHAR(255) NOT NULL,
                last_updated DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            )
        ''')

        # Channel name cache (same purpose as member_names)
        await self.execute('''
            CREATE TABLE IF NOT EXISTS channel_names (
                channel_id BIGINT PRIMARY KEY,
                channel_name VARCHAR(255) NOT NULL,
                last_updated DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            )
        ''')

        # ─── TICKET HISTORY (Permanent Archive) ─────────────────────
        # Active tickets are deleted when the channel is purged.
        # This table preserves records forever for analytics.
        await self.execute('''
            CREATE TABLE IF NOT EXISTS ticket_history (
                id INT AUTO_INCREMENT PRIMARY KEY,
                channel_name VARCHAR(255),
                creator_id BIGINT NOT NULL,
                category_key VARCHAR(10) NOT NULL,
                subject VARCHAR(255),
                handler_id BIGINT DEFAULT NULL,
                close_reason VARCHAR(100) DEFAULT NULL,
                is_test BOOLEAN DEFAULT FALSE,
                created_at DATETIME NOT NULL,
                closed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                closed_by BIGINT DEFAULT NULL,
                INDEX idx_th_created (created_at),
                INDEX idx_th_cat (category_key)
            )
        ''')

        # ─── SOCIAL RP SYSTEM ────────────────────────────────────────
        await self.execute('''
            CREATE TABLE IF NOT EXISTS marriages (
                id INT PRIMARY KEY AUTO_INCREMENT,
                user1_id BIGINT NOT NULL,
                user2_id BIGINT NOT NULL,
                married_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uk_user1 (user1_id),
                UNIQUE KEY uk_user2 (user2_id),
                INDEX idx_marriage_pair (user1_id, user2_id)
            )
        ''')

        await self.execute('''
            CREATE TABLE IF NOT EXISTS family (
                id INT PRIMARY KEY AUTO_INCREMENT,
                parent_id BIGINT NOT NULL,
                child_id BIGINT NOT NULL,
                adopted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uk_child (child_id),
                INDEX idx_family_parent (parent_id)
            )
        ''')

        await self.execute('''
            CREATE TABLE IF NOT EXISTS social_blocks (
                user_id BIGINT NOT NULL,
                blocked_id BIGINT NOT NULL,
                blocked_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, blocked_id),
                INDEX idx_sb_blocked (blocked_id)
            )
        ''')

        await self.execute('''
            CREATE TABLE IF NOT EXISTS analytics_social_interactions (
                id INT AUTO_INCREMENT PRIMARY KEY,
                action_type VARCHAR(50) NOT NULL,
                user_id BIGINT NOT NULL,
                target_id BIGINT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_asi_date (created_at),
                INDEX idx_asi_action (action_type)
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
    
    async def executemany(self, query: str, params_list: list[tuple]):
        """Execute the same query with multiple parameter sets in a single connection.
        
        Uses a single connection from the pool for all iterations,
        reducing connection acquisition overhead for batch operations.
        Autocommit is enabled in DB_CONFIG, so each execute commits individually.
        """
        if not params_list:
            return
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.executemany(query, params_list)
    
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

    # ─────────────────────────────────────────────────────────────────
    # Destructive Wipe Methods (Modular Wipe System)
    # ─────────────────────────────────────────────────────────────────

    async def wipe_xp(self):
        """Reset XP and Leveling related columns."""
        await self.execute("UPDATE users SET xp = 0, xp_multiplier = 1.0, xp_locked = FALSE, xp_lock_until = NULL")

    async def wipe_ep(self):
        """Reset Event Points and Placements, clear redemptions history."""
        await self.execute("UPDATE users SET event_points = 0, consecutive_events_attended = 0")
        await self.execute("DELETE FROM event_redemptions")

    async def wipe_event_codes(self):
        """Clear all active event codes."""
        await self.execute("DELETE FROM event_codes")

    async def wipe_economy(self):
        """Reset all tokens and shop discounts."""
        await self.execute("UPDATE users SET tokens = 0, lifetime_tokens = 0, shop_discount = 0.0")

    async def wipe_social(self):
        """Reset daily streaks, thanks, and badges. Clear thanks history & quiz history."""
        await self.execute("UPDATE users SET consecutive_active_days = 0, last_active_date = NULL, thanks_received = 0, badges = '[]'")
        await self.execute("DELETE FROM thanks_history")
        await self.execute("DELETE FROM quiz_history")

    async def wipe_boosters(self):
        """Reset booster statuses and raffle history."""
        await self.execute("UPDATE users SET boost_start_date = NULL")
        await self.execute("DELETE FROM booster_raffle_history")

    async def wipe_modlogs(self):
        """Erase moderation logs completely."""
        await self.execute("DELETE FROM mod_logs")

    async def wipe_verification(self):
        """Delete all Verification status data (Extreme)."""
        await self.execute("DELETE FROM verified_users")

    async def wipe_quests(self):
        """Delete all quest definitions and user progress."""
        await self.execute("DELETE FROM quest_progress")
        await self.execute("DELETE FROM quests")

    async def wipe_referrals(self):
        """Delete all referral data."""
        await self.execute("DELETE FROM referrals")


# Singleton instance
db = Database()
