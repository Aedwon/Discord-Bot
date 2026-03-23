"""
Event Points (EP) Service.
Handles MLBB-style tiered sub-rank calculations (V→I per tier),
dynamic Top-50 Mythic ladder assignments, and database interactions.
"""

from services.database import db
from services.settings_service import settings_service
from services.verification_service import verification_service
import discord
import logging

logger = logging.getLogger("mlbb_bot.ep_service")

# ─── TIER DEFINITIONS ──────────────────────────────────────────────────

# Main tiers with their EP floor values (ordered low → high)
MAIN_TIERS = [
    ("Warrior",      0),
    ("Elite",        500),
    ("Master",       1500),
    ("Grandmaster",  3000),
    ("Epic",         5000),
    ("Legend",       7500),
]

MYTHIC_FLOOR = 10000

# Sub-tier breakpoints: each main tier is split into V, IV, III, II, I
# Generated programmatically from the floor/ceiling of each tier.
def _build_sub_tiers():
    """Build the full sub-tier table with EP breakpoints."""
    tiers = []
    for i, (name, floor) in enumerate(MAIN_TIERS):
        # Ceiling is next tier's floor - 1, or MYTHIC_FLOOR - 1 for Legend
        if i + 1 < len(MAIN_TIERS):
            ceiling = MAIN_TIERS[i + 1][1]
        else:
            ceiling = MYTHIC_FLOOR

        tier_range = ceiling - floor
        step = tier_range // 5  # Even split into 5 sub-tiers

        numerals = ["V", "IV", "III", "II", "I"]
        for j, numeral in enumerate(numerals):
            sub_floor = floor + (step * j)
            # Last sub-tier (I) extends to the tier ceiling
            sub_ceiling = floor + (step * (j + 1)) if j < 4 else ceiling
            tiers.append({
                "name": f"{name} {numeral}",
                "main_tier": name,
                "floor": sub_floor,
                "ceiling": sub_ceiling,
            })
    return tiers

SUB_TIERS = _build_sub_tiers()

# Mythic ladder tiers (position-based, not EP-based)
MYTHIC_LADDER = ["Mythic", "Mythical Honor", "Mythical Glory", "Mythical Immortal"]

# All 10 main tier names for Peak Rank / legacy purposes
MAIN_TIER_NAMES = [t[0] for t in MAIN_TIERS] + MYTHIC_LADDER

# All 34 role names the bot manages
ALL_EP_ROLE_NAMES = [t["name"] for t in SUB_TIERS] + MYTHIC_LADDER


class EPService:

    # ─── TIER CALCULATION ──────────────────────────────────────────────

    def get_sub_tier(self, ep: int) -> str:
        """
        Get the precise sub-tier name for a given EP value.
        Returns e.g. "Warrior V", "Epic III", "Legend I".
        For 10000+ EP, returns "Mythic" (ladder position is resolved separately).
        """
        if ep >= MYTHIC_FLOOR:
            return "Mythic"

        # Walk backwards through sub-tiers to find the correct bracket
        for tier in reversed(SUB_TIERS):
            if ep >= tier["floor"]:
                return tier["name"]

        return SUB_TIERS[0]["name"]  # Fallback: Warrior V

    def get_main_tier(self, ep: int) -> str:
        """
        Get the main tier name (without sub-tier numeral).
        Used for Peak Rank comparisons and legacy badge assignment.
        """
        if ep >= MYTHIC_FLOOR:
            return "Mythic"  # Specific Mythic position resolved by ladder

        for name, floor in reversed(MAIN_TIERS):
            if ep >= floor:
                return name

        return "Warrior"  # Fallback

    def get_main_tier_rank(self, tier_name: str) -> int:
        """
        Get a numeric rank value for a main tier (higher = better).
        Used for Peak Rank upgrade comparisons.
        """
        rank_order = {name: i for i, name in enumerate(MAIN_TIER_NAMES)}
        return rank_order.get(tier_name, 0)

    def get_all_ep_role_names(self) -> list[str]:
        """Return all 34 EP role names for setup/discovery."""
        return ALL_EP_ROLE_NAMES.copy()

    def get_all_main_tier_names(self) -> list[str]:
        """Return all 10 main tier names for Peak Rank discovery."""
        return MAIN_TIER_NAMES.copy()

    # ─── EP UPDATE PROCESSING ──────────────────────────────────────────

    async def process_ep_update(self, guild: discord.Guild, user_id: int, ep_change: int) -> int:
        """
        Process an EP change for a user: update DB, assign correct sub-tier role,
        and send rank-up notifications.
        Returns the user's new EP total.
        """
        if not guild:
            return 0

        # Verification gate — unverified users earn no EP
        if not verification_service.is_verified(user_id):
            return 0

        # 0. Capture old EP for tier-change detection
        old_row = await db.fetch_one(
            "SELECT event_points FROM users WHERE user_id = %s", (user_id,)
        )
        old_ep = old_row['event_points'] if old_row else 0
        old_sub_tier = self.get_sub_tier(old_ep) if old_ep > 0 else None

        # 1. Atomic EP update with tie-breaker timestamp
        await db.execute('''
            INSERT INTO users (user_id, event_points) 
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE 
                event_points = GREATEST(0, users.event_points + %s),
                last_ep_update = CURRENT_TIMESTAMP
        ''', (user_id, ep_change, ep_change))

        row = await db.fetch_one(
            "SELECT event_points FROM users WHERE user_id = %s", (user_id,)
        )
        new_ep = row['event_points'] if row else 0

        # 2. Resolve the correct sub-tier
        new_sub_tier = self.get_sub_tier(new_ep)

        # 3. Get the member and identify their current EP role
        member = guild.get_member(user_id)
        if not member:
            return new_ep

        # Build lookup of all 34 EP roles
        all_ep_roles = []
        current_held_role = None

        for role_name in ALL_EP_ROLE_NAMES:
            settings_key = f"ep_role_{role_name.replace(' ', '_')}"
            role_id = await settings_service.get_int(settings_key)
            if role_id:
                role_obj = guild.get_role(role_id)
                if role_obj:
                    all_ep_roles.append(role_obj)
                    if role_obj in member.roles:
                        current_held_role = (role_name, role_obj)

        # 4. Sub-10k: assign the correct sub-tier role
        if new_ep < MYTHIC_FLOOR:
            current_name = current_held_role[0] if current_held_role else None
            if current_name != new_sub_tier:
                new_role_id = await settings_service.get_int(
                    f"ep_role_{new_sub_tier.replace(' ', '_')}"
                )
                if new_role_id:
                    new_role_obj = guild.get_role(new_role_id)
                    if new_role_obj:
                        try:
                            # Strip ALL EP roles first, then add the correct one
                            roles_to_remove = [r for r in all_ep_roles if r in member.roles]
                            if roles_to_remove:
                                await member.remove_roles(
                                    *roles_to_remove, reason="EP Sub-Tier Shift"
                                )
                            await member.add_roles(
                                new_role_obj, reason=f"EP Sub-Tier: {new_sub_tier}"
                            )
                        except discord.Forbidden:
                            logger.error(
                                f"Missing permissions to update EP roles for {user_id}"
                            )

        # 5. If they crossed the Mythic threshold (up or down), recalculate the ladder
        was_mythic = current_held_role and current_held_role[0] in MYTHIC_LADDER
        is_mythic = new_ep >= MYTHIC_FLOOR

        if is_mythic or (not is_mythic and was_mythic):
            await self.recalculate_mythic_roles(guild)

        # 6. EP rank-up notification → alert channel
        if new_ep > old_ep and new_sub_tier != old_sub_tier:
            await self._send_ep_rank_notification(guild, member, old_sub_tier, new_sub_tier, new_ep)

        return new_ep

    async def _send_ep_rank_notification(
        self, guild: discord.Guild, member: discord.Member,
        old_tier: str, new_tier: str, new_ep: int
    ):
        """Send an EP rank-up notification to the level alerts channel."""
        try:
            alert_ch_id = await settings_service.get_int("level_alerts_channel_id")
            if not alert_ch_id:
                return
            channel = guild.get_channel(alert_ch_id)
            if not channel:
                return

            embed = discord.Embed(
                title="🏅 EP Rank Up!",
                description=(
                    f"**{old_tier or 'Unranked'}** → **{new_tier}**\n"
                    f"`{new_ep:,}` Event Points"
                ),
                color=discord.Color.purple()
            )
            embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
            embed.set_thumbnail(url=member.display_avatar.url)
            await channel.send(content=member.mention, embed=embed)
        except Exception as e:
            logger.error(f"Failed to send EP rank notification for {member.id}: {e}")

    # ─── MYTHIC LADDER ─────────────────────────────────────────────────

    async def recalculate_mythic_roles(self, guild: discord.Guild):
        """
        Re-evaluate the Top 50 EP leaders and assign Mythic ladder roles.
        Ties broken by earliest last_ep_update (first to reach the EP wins).
        
        Mythical Immortal: Top 1-10
        Mythical Glory:    Top 11-25
        Mythical Honor:    Top 26-50
        Mythic:            51+ (still above 10k but outside Top 50)
        """
        top_players = await db.fetch_all('''
            SELECT user_id, event_points 
            FROM users 
            WHERE event_points >= %s 
            ORDER BY event_points DESC, last_ep_update ASC
        ''', (MYTHIC_FLOOR,))

        if not top_players:
            return

        # Resolve all 4 Mythic ladder roles
        role_map = {}
        for tier_name in MYTHIC_LADDER:
            key = f"ep_role_{tier_name.replace(' ', '_')}"
            role_id = await settings_service.get_int(key)
            if role_id:
                role_obj = guild.get_role(role_id)
                if role_obj:
                    role_map[tier_name] = role_obj

        mythic_roles = list(role_map.values())
        if not mythic_roles:
            return

        for index, row in enumerate(top_players, 1):
            user_id = row['user_id']
            member = guild.get_member(user_id)
            if not member:
                continue

            # Determine correct Mythic tier by position
            if index <= 10:
                correct_tier = "Mythical Immortal"
            elif index <= 25:
                correct_tier = "Mythical Glory"
            elif index <= 50:
                correct_tier = "Mythical Honor"
            else:
                correct_tier = "Mythic"

            correct_role = role_map.get(correct_tier)
            if not correct_role:
                continue

            # Only update if they don't already have the correct role
            if correct_role not in member.roles:
                try:
                    # Strip any existing Mythic ladder roles
                    roles_to_strip = [
                        r for r in mythic_roles
                        if r in member.roles and r != correct_role
                    ]
                    if roles_to_strip:
                        await member.remove_roles(
                            *roles_to_strip,
                            reason=f"Mythic Ladder Shift (was #{index})"
                        )
                    await member.add_roles(
                        correct_role,
                        reason=f"Mythic Ladder: #{index} → {correct_tier}"
                    )
                except discord.Forbidden:
                    logger.error(
                        f"Cannot assign Mythic role for user {user_id} (#{index})"
                    )

    # ─── PEAK RANK HELPERS ─────────────────────────────────────────────

    def resolve_eos_tier(self, ep: int, position: int) -> str:
        """
        Resolve a user's main tier for EOS Peak Rank purposes.
        For Mythic+ users, position determines the specific Mythic ladder tier.
        """
        if ep >= MYTHIC_FLOOR:
            if position <= 10:
                return "Mythical Immortal"
            elif position <= 25:
                return "Mythical Glory"
            elif position <= 50:
                return "Mythical Honor"
            else:
                return "Mythic"
        return self.get_main_tier(ep)


# Core API Export
ep_service = EPService()
