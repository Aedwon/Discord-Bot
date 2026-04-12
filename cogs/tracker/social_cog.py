import discord
from discord.ext import commands, tasks
from discord import app_commands
import logging
import random
import hashlib
from datetime import datetime, timedelta
import pytz

from services.database import db
from services.settings_service import settings_service
from services.xp_service import xp_service
from services.badge_service import badge_service

logger = logging.getLogger("mlbb_bot.social_cog")
TZ_MANILA = pytz.timezone("Asia/Manila")

# ─────────────────────────────────────────────────────────────────────
# Interaction Data — randomized response pools
# ─────────────────────────────────────────────────────────────────────

INTERACTIONS = {
    "hug": {
        "emoji": "🤗",
        "color": 0xFFB6C1,
        "responses": [
            "{user} wrapped {target} in a warm bear hug!",
            "{user} gave {target} the coziest hug ever! 🧸",
            "{user} pulled {target} into a big, tight hug!",
            "{user} hugged {target} like there's no tomorrow!",
            "{user} gave {target} a surprise hug from behind!",
        ],
    },
    "pat": {
        "emoji": "🫳",
        "color": 0xADD8E6,
        "responses": [
            "{user} gently patted {target} on the head!",
            "{user} gave {target} the most wholesome headpats!",
            "{user} softly patted {target}. There, there~ 💛",
            "{user} reached over and patted {target}!",
            "*pat pat pat* — {user} patted {target} with care!",
        ],
    },
    "poke": {
        "emoji": "👉",
        "color": 0xFFA500,
        "responses": [
            "{user} poked {target}! Hey, pay attention!",
            "{user} gave {target} a sneaky poke! 👀",
            "*poke poke* — {user} won't leave {target} alone!",
            "{user} poked {target} on the cheek!",
            "{user} is poking {target} relentlessly!",
        ],
    },
    "bonk": {
        "emoji": "🔨",
        "color": 0xFF6347,
        "responses": [
            "{user} bonked {target} on the head! 💫",
            "BONK! {user} smacked {target} with a squeaky hammer!",
            "{user} gave {target} a disciplinary bonk!",
            "{user} deployed the bonk hammer on {target}! 🔨",
            "{user} bonked {target}! Go to the shadow realm!",
        ],
    },
    "slap": {
        "emoji": "🫲",
        "color": 0xDC143C,
        "responses": [
            "{user} slapped {target} with a wet fish! 🐟",
            "{user} gave {target} a dramatic anime slap!",
            "SLAP! {user} hit {target} with a rubber chicken!",
            "{user} slapped sense into {target}! ✋",
            "{user} slapped {target} with a pillow! 🛏️",
        ],
    },
    "highfive": {
        "emoji": "🙌",
        "color": 0x32CD32,
        "responses": [
            "{user} and {target} high-fived! ✋🤚",
            "SMACK! {user} and {target} nailed the perfect high-five!",
            "{user} high-fived {target} so hard the server shook!",
            "{user} went for a high-five — {target} didn't leave them hanging! 🎉",
            "Epic high-five between {user} and {target}! 💥",
        ],
    },
    "tickle": {
        "emoji": "🤭",
        "color": 0xFFD700,
        "responses": [
            "{user} tickled {target}! No escape! 😂",
            "{user} launched a tickle attack on {target}!",
            "{user} found {target}'s ticklish spot!",
            "Tickle tickle! {user} is merciless against {target}!",
            "{user} tickled {target} until they couldn't breathe!",
        ],
    },
    "cuddle": {
        "emoji": "🥰",
        "color": 0xFF69B4,
        "responses": [
            "{user} cuddled up to {target}! So warm~ 💕",
            "{user} snuggled {target} under a cozy blanket!",
            "{user} pulled {target} into a comfy cuddle session!",
            "{user} and {target} are cuddling together! Adorable~ 🧡",
            "{user} cuddled {target} and won't let go!",
        ],
    },
    "wave": {
        "emoji": "👋",
        "color": 0x87CEEB,
        "responses": [
            "{user} waved at {target}! Hello~! 👋",
            "{user} gave {target} an enthusiastic wave!",
            "{user} is waving at {target} from across the server!",
            "Hey {target}! {user} is waving at you! 🙋",
            "{user} waved hello to {target}! 🌟",
        ],
    },
    "wink": {
        "emoji": "😉",
        "color": 0xDA70D6,
        "responses": [
            "{user} winked at {target}! 😏",
            "{user} gave {target} a sly wink~",
            "{user} sent a flirty wink {target}'s way! 😉",
            "{user} winked at {target}. What could it mean? 👀",
            "*wink wink* — {user} is up to something with {target}!",
        ],
    },
    "handhold": {
        "emoji": "🤝",
        "color": 0xFFC0CB,
        "responses": [
            "{user} held {target}'s hand! 🫶",
            "{user} and {target} are holding hands! How sweet~ 💞",
            "{user} gently took {target}'s hand!",
            "{user} interlocked fingers with {target}! 🤞",
            "{user} reached out and held {target}'s hand tightly!",
        ],
    },
    "nom": {
        "emoji": "😋",
        "color": 0xF4A460,
        "responses": [
            "{user} nommed on {target}! Tasty! 🍪",
            "Om nom nom! {user} took a bite of {target}!",
            "{user} is nibbling on {target}! 🥺",
            "{user} chomped {target}! They taste like victory!",
            "Nom! {user} snacked on {target}'s arm!",
        ],
    },
    "stare": {
        "emoji": "👁️",
        "color": 0x708090,
        "responses": [
            "{user} is staring intensely at {target}... 👁️👁️",
            "{user} locked eyes with {target}. Awkward silence. 😶",
            "{user} is staring deep into {target}'s soul...",
            "{user} won't stop staring at {target}! Creepy? Cute? Both?",
            "{user} gave {target} the most intense stare imaginable.",
        ],
    },
    "pout": {
        "emoji": "🥺",
        "color": 0xDDA0DD,
        "responses": [
            "{user} is pouting at {target}! Give them attention! 🥺",
            "{user} made the saddest puppy eyes at {target}!",
            "{user} pouted at {target}. How could you resist?",
            "{user} is pouting because {target} won't play with them!",
            "{user} puffed their cheeks and pouted at {target}!",
        ],
    },
}

# ─────────────────────────────────────────────────────────────────────
# Ship Compatibility Titles
# ─────────────────────────────────────────────────────────────────────

SHIP_TITLES = [
    (0,   "💔 Complete Strangers"),
    (10,  "😐 Acquaintances at Best"),
    (20,  "🤔 Just Friends... Maybe"),
    (30,  "🙂 Starting to Click"),
    (40,  "😊 Good Vibes Only"),
    (50,  "😏 There's Definitely Something"),
    (60,  "💛 Budding Romance"),
    (70,  "💕 Undeniable Chemistry"),
    (80,  "💗 Power Couple"),
    (90,  "💖 Soulmate Energy"),
    (100, "💘 Written in the Stars"),
]

# ─────────────────────────────────────────────────────────────────────
# 8-Ball Responses
# ─────────────────────────────────────────────────────────────────────

EIGHT_BALL_RESPONSES = [
    # Positive
    "🟢 It is certain.",
    "🟢 Without a doubt.",
    "🟢 You may rely on it.",
    "🟢 Yes, definitely!",
    "🟢 As I see it, yes.",
    "🟢 Most likely.",
    "🟢 The stars say yes! ✨",
    "🟢 Our marksman says: *Yes, definitely.* 🎯",
    "🟢 Even Tigreal would approve. 🛡️",
    # Neutral
    "🟡 Reply hazy, try again...",
    "🟡 Ask again later.",
    "🟡 Better not tell you now.",
    "🟡 Cannot predict now.",
    "🟡 Concentrate and ask again...",
    "🟡 The battlefield is unclear. 🌫️",
    # Negative
    "🔴 Don't count on it.",
    "🔴 My sources say no.",
    "🔴 Outlook not so good.",
    "🔴 Very doubtful.",
    "🔴 Not even a Fanny cable could save that idea. 💀",
    "🔴 Layla says: *Absolutely not.* 🙅‍♀️",
    "🔴 That's a wipeout. 💥",
]

# ─────────────────────────────────────────────────────────────────────
# Marriage Proposal View
# ─────────────────────────────────────────────────────────────────────

class ProposalView(discord.ui.View):
    """Accept/Decline buttons for a marriage proposal."""
    def __init__(self, proposer: discord.Member, target: discord.Member):
        super().__init__(timeout=120)
        self.proposer = proposer
        self.target = target
        self.responded = False

    @discord.ui.button(label="💍 Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.target.id:
            return await interaction.response.send_message("❌ This proposal isn't for you!", ephemeral=True)
        if self.responded:
            return
        self.responded = True
        self.stop()

        # Double-check neither party married someone else in the meantime
        existing1 = await db.fetch_one(
            "SELECT id FROM marriages WHERE user1_id = %s OR user2_id = %s", (self.proposer.id, self.proposer.id)
        )
        existing2 = await db.fetch_one(
            "SELECT id FROM marriages WHERE user1_id = %s OR user2_id = %s", (self.target.id, self.target.id)
        )
        if existing1 or existing2:
            return await interaction.response.edit_message(
                content="❌ One of you got married while this proposal was pending!", embed=None, view=None
            )

        # Store with lower ID first for consistent lookup
        u1, u2 = sorted([self.proposer.id, self.target.id])
        await db.execute(
            "INSERT INTO marriages (user1_id, user2_id) VALUES (%s, %s)", (u1, u2)
        )

        embed = discord.Embed(
            description=(
                f"## 💒  A New Union!\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"**{self.proposer.display_name}**  💍  **{self.target.display_name}**\n\n"
                f"*Are now united in marriage!* ✨\n"
                f"*May your bond be eternal~*\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━"
            ),
            color=0xFF69B4,
        )
        embed.set_footer(text="Use /social marriage status to view your info • MSL Network")
        await interaction.response.edit_message(content=None, embed=embed, view=None)

    @discord.ui.button(label="💔 Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.target.id:
            return await interaction.response.send_message("❌ This proposal isn't for you!", ephemeral=True)
        if self.responded:
            return
        self.responded = True
        self.stop()

        embed = discord.Embed(
            description=(
                f"## 💔  Proposal Declined\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"**{self.target.display_name}** declined **{self.proposer.display_name}**'s proposal.\n\n"
                f"*Maybe next time... the stars will align.*\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━"
            ),
            color=0x708090,
        )
        await interaction.response.edit_message(content=None, embed=embed, view=None)

    async def on_timeout(self):
        # Disable buttons on timeout
        for item in self.children:
            item.disabled = True


# ─────────────────────────────────────────────────────────────────────
# Adoption Request View
# ─────────────────────────────────────────────────────────────────────

class AdoptionView(discord.ui.View):
    """Accept/Decline buttons for an adoption request."""
    def __init__(self, parent: discord.Member, child: discord.Member):
        super().__init__(timeout=120)
        self.parent = parent
        self.child = child
        self.responded = False

    @discord.ui.button(label="🏠 Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.child.id:
            return await interaction.response.send_message("❌ This adoption request isn't for you!", ephemeral=True)
        if self.responded:
            return
        self.responded = True
        self.stop()

        # Check child isn't already adopted
        existing = await db.fetch_one(
            "SELECT id FROM family WHERE child_id = %s", (self.child.id,)
        )
        if existing:
            return await interaction.response.edit_message(
                content="❌ This person already has a parent!", embed=None, view=None
            )

        await db.execute(
            "INSERT INTO family (parent_id, child_id) VALUES (%s, %s)", (self.parent.id, self.child.id)
        )

        embed = discord.Embed(
            description=(
                f"## 🏠  A New Family Bond!\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"**{self.parent.display_name}** has adopted **{self.child.display_name}**! 🎉\n\n"
                f"*Welcome to the family~* 🥰\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━"
            ),
            color=0x87CEEB,
        )
        embed.set_footer(text="Use /social family tree to see your family • MSL Network")
        await interaction.response.edit_message(content=None, embed=embed, view=None)

    @discord.ui.button(label="❌ Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.child.id:
            return await interaction.response.send_message("❌ This request isn't for you!", ephemeral=True)
        if self.responded:
            return
        self.responded = True
        self.stop()

        embed = discord.Embed(
            description=(
                f"## ❌  Adoption Declined\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"**{self.child.display_name}** declined **{self.parent.display_name}**'s adoption request.\n\n"
                f"*Perhaps another time.*\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━"
            ),
            color=0x708090,
        )
        await interaction.response.edit_message(content=None, embed=embed, view=None)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


# ─────────────────────────────────────────────────────────────────────
# SocialCog
# ─────────────────────────────────────────────────────────────────────

class SocialCog(commands.GroupCog, name="social"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.weekly_contributor_loop.start()

    def cog_unload(self):
        self.weekly_contributor_loop.cancel()

    # ─── Subgroups ─────────────────────────────────────────────────
    marriage_group = app_commands.Group(name="marriage", description="Roleplay marriage system")
    family_group = app_commands.Group(name="family", description="Roleplay family & adoption system")

    # =================================================================
    # EXISTING: /social thank
    # =================================================================

    @app_commands.command(name="thank", description="Thank someone for their help and award them 10 XP!")
    async def thank_user(self, interaction: discord.Interaction, user: discord.Member, reason: str):
        # Prevent self
        if user.id == interaction.user.id:
            return await interaction.response.send_message("❌ You cannot thank yourself!", ephemeral=True)
            
        # Check 24H cooldown for sender
        recent_sends = await db.fetch_all("SELECT created_at FROM thanks_history WHERE sender_id = %s ORDER BY created_at DESC LIMIT 1", (interaction.user.id,))
        if recent_sends:
            last_sent = recent_sends[0]['created_at']
            if (datetime.now() - last_sent).total_seconds() < 86400:
                return await interaction.response.send_message("❌ You can only use the `/social thank` command once every 24 hours.", ephemeral=True)
                
        # Check 7 Day cooldown for target
        target_sends = await db.fetch_all("SELECT created_at FROM thanks_history WHERE sender_id = %s AND receiver_id = %s ORDER BY created_at DESC LIMIT 1", (interaction.user.id, user.id))
        if target_sends:
            last_target = target_sends[0]['created_at']
            if (datetime.now() - last_target).total_seconds() < 604800:
                return await interaction.response.send_message(f"❌ You cannot thank {user.mention} again so soon! You must wait 7 days between thanking the same person.", ephemeral=True)

        # Log to thanks_history
        await db.execute("INSERT INTO thanks_history (sender_id, receiver_id, reason) VALUES (%s, %s, %s)", (interaction.user.id, user.id, reason))
        
        # Award 10 XP and increment overall count natively
        await db.execute('''
            INSERT INTO users (user_id, xp, thanks_received) VALUES (%s, %s, 1)
            ON DUPLICATE KEY UPDATE xp = xp + 10, thanks_received = IFNULL(thanks_received, 0) + 1
        ''', (user.id, 10))
        
        # Badge Evaluation (Moniyan Sage requires 25 thanks)
        await badge_service.eval_sage(user)
        
        embed = discord.Embed(
            title="💖 Appreciation Sent!",
            description=f"{interaction.user.mention} thanked {user.mention} for:\n> *\"{reason}\"*",
            color=discord.Color.from_rgb(255, 105, 180)
        )
        embed.set_footer(text="The receiver has been mathematically awarded 10 XP!")
        await interaction.response.send_message(embed=embed)

    # =================================================================
    # EXISTING: /social bind-badges
    # =================================================================

    @app_commands.command(name="bind-badges", description="[Admin] Bind Discord Roles to dynamic Badges")
    @app_commands.default_permissions(administrator=True)
    async def bind_badges(
        self, interaction: discord.Interaction, 
        twilight_pilgrim: discord.Role = None,
        first_people: discord.Role = None,
        sage: discord.Role = None,
        battlefield: discord.Role = None,
        mogul: discord.Role = None,
        convivialist: discord.Role = None,
        mentor: discord.Role = None
    ):
        settings = {
            "badge_role_twilight": str(twilight_pilgrim.id) if twilight_pilgrim else "0",
            "badge_role_first_people": str(first_people.id) if first_people else "0",
            "badge_role_sage": str(sage.id) if sage else "0",
            "badge_role_battlefield": str(battlefield.id) if battlefield else "0",
            "badge_role_mogul": str(mogul.id) if mogul else "0",
            "badge_role_convivialist": str(convivialist.id) if convivialist else "0",
            "role_id_mentor": str(mentor.id) if mentor else "0"
        }
        for k, v in settings.items():
            await db.execute("INSERT INTO server_settings (`key`, value) VALUES (%s, %s) ON DUPLICATE KEY UPDATE value = VALUES(value)", (k, v))
        await interaction.response.send_message("✅ Dynamic Badge Roles and Mentor configurations have been mapped locally to the backend system.", ephemeral=True)

    # =================================================================
    # INTERACTION COMMANDS (hug, pat, poke, etc.)
    # =================================================================

    async def _do_interaction(self, interaction: discord.Interaction, target: discord.Member, action_key: str):
        """Central handler for all RP interaction commands."""
        if target.id == interaction.user.id:
            return await interaction.response.send_message("❌ You can't do that to yourself!", ephemeral=True)

        action = INTERACTIONS[action_key]
        text = random.choice(action["responses"]).format(
            user=interaction.user.display_name,
            target=target.display_name,
        )

        embed = discord.Embed(
            description=(
                f"## {action['emoji']}  {action_key.upper()}\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"{text}\n"
                f"━━━━━━━━━━━━━━━━━━━━━"
            ),
            color=action["color"],
            timestamp=datetime.now(TZ_MANILA),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
        embed.set_footer(text=f"/social {action_key} • MSL Network")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="hug", description="Give someone a warm hug!")
    @app_commands.checks.cooldown(1, 5, key=lambda i: i.user.id)
    async def hug(self, interaction: discord.Interaction, user: discord.Member):
        await self._do_interaction(interaction, user, "hug")

    @app_commands.command(name="pat", description="Pat someone on the head~")
    @app_commands.checks.cooldown(1, 5, key=lambda i: i.user.id)
    async def pat(self, interaction: discord.Interaction, user: discord.Member):
        await self._do_interaction(interaction, user, "pat")

    @app_commands.command(name="poke", description="Poke someone to get their attention!")
    @app_commands.checks.cooldown(1, 5, key=lambda i: i.user.id)
    async def poke(self, interaction: discord.Interaction, user: discord.Member):
        await self._do_interaction(interaction, user, "poke")

    @app_commands.command(name="bonk", description="Bonk someone with a hammer!")
    @app_commands.checks.cooldown(1, 5, key=lambda i: i.user.id)
    async def bonk(self, interaction: discord.Interaction, user: discord.Member):
        await self._do_interaction(interaction, user, "bonk")

    @app_commands.command(name="slap", description="Slap someone with a fish!")
    @app_commands.checks.cooldown(1, 5, key=lambda i: i.user.id)
    async def slap(self, interaction: discord.Interaction, user: discord.Member):
        await self._do_interaction(interaction, user, "slap")

    @app_commands.command(name="highfive", description="High-five someone!")
    @app_commands.checks.cooldown(1, 5, key=lambda i: i.user.id)
    async def highfive(self, interaction: discord.Interaction, user: discord.Member):
        await self._do_interaction(interaction, user, "highfive")

    @app_commands.command(name="tickle", description="Tickle someone until they can't breathe!")
    @app_commands.checks.cooldown(1, 5, key=lambda i: i.user.id)
    async def tickle(self, interaction: discord.Interaction, user: discord.Member):
        await self._do_interaction(interaction, user, "tickle")

    @app_commands.command(name="cuddle", description="Cuddle up to someone~")
    @app_commands.checks.cooldown(1, 5, key=lambda i: i.user.id)
    async def cuddle(self, interaction: discord.Interaction, user: discord.Member):
        await self._do_interaction(interaction, user, "cuddle")

    @app_commands.command(name="wave", description="Wave hello to someone!")
    @app_commands.checks.cooldown(1, 5, key=lambda i: i.user.id)
    async def wave(self, interaction: discord.Interaction, user: discord.Member):
        await self._do_interaction(interaction, user, "wave")

    @app_commands.command(name="wink", description="Wink at someone~")
    @app_commands.checks.cooldown(1, 5, key=lambda i: i.user.id)
    async def wink(self, interaction: discord.Interaction, user: discord.Member):
        await self._do_interaction(interaction, user, "wink")

    @app_commands.command(name="handhold", description="Hold someone's hand~")
    @app_commands.checks.cooldown(1, 5, key=lambda i: i.user.id)
    async def handhold(self, interaction: discord.Interaction, user: discord.Member):
        await self._do_interaction(interaction, user, "handhold")

    @app_commands.command(name="nom", description="Take a bite out of someone!")
    @app_commands.checks.cooldown(1, 5, key=lambda i: i.user.id)
    async def nom(self, interaction: discord.Interaction, user: discord.Member):
        await self._do_interaction(interaction, user, "nom")

    @app_commands.command(name="stare", description="Stare intensely at someone...")
    @app_commands.checks.cooldown(1, 5, key=lambda i: i.user.id)
    async def stare(self, interaction: discord.Interaction, user: discord.Member):
        await self._do_interaction(interaction, user, "stare")

    @app_commands.command(name="pout", description="Pout at someone until they give you attention!")
    @app_commands.checks.cooldown(1, 5, key=lambda i: i.user.id)
    async def pout(self, interaction: discord.Interaction, user: discord.Member):
        await self._do_interaction(interaction, user, "pout")

    # =================================================================
    # /social ship — Deterministic Love Calculator
    # =================================================================

    @app_commands.command(name="ship", description="Calculate love compatibility between two users!")
    @app_commands.checks.cooldown(1, 5, key=lambda i: i.user.id)
    async def ship(self, interaction: discord.Interaction, user1: discord.Member, user2: discord.Member = None):
        if user2 is None:
            user2 = interaction.user

        if user1.id == user2.id:
            return await interaction.response.send_message("❌ You can't ship someone with themselves!", ephemeral=True)

        # Deterministic score: hash the sorted pair so order doesn't matter
        pair = tuple(sorted([user1.id, user2.id]))
        seed = hashlib.md5(f"{pair[0]}x{pair[1]}".encode()).hexdigest()
        score = int(seed, 16) % 101  # 0-100

        # Build progress bar (10 segments)
        filled = score // 10
        remainder = score % 10
        bar = "█" * filled
        if remainder >= 5 and filled < 10:
            bar += "▒"
            empty = 10 - filled - 1
        else:
            empty = 10 - filled
        bar += "░" * empty

        # Get title
        title = SHIP_TITLES[0][1]
        for threshold, t in SHIP_TITLES:
            if score >= threshold:
                title = t

        # Ship name (first half of name1 + second half of name2)
        name1 = user1.display_name
        name2 = user2.display_name
        ship_name = name1[:len(name1)//2] + name2[len(name2)//2:]

        # Dynamic color based on score
        if score < 30:
            color = 0x708090  # slate grey
        elif score < 60:
            color = 0xFFD700  # gold
        elif score < 85:
            color = 0xFF69B4  # hot pink
        else:
            color = 0xFF1493  # deep pink

        embed = discord.Embed(
            description=(
                f"## 💘  Love Calculator\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"**{name1}**  ❤️‍🔥  **{name2}**\n"
                f"*✦ Ship Name: **{ship_name}** ✦*\n\n"
                f"`  [{bar}]  `  **{score}%**\n\n"
                f"**{title}**\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━"
            ),
            color=color,
            timestamp=datetime.now(TZ_MANILA),
        )
        embed.set_thumbnail(url=user1.display_avatar.url)
        embed.set_author(name=f"{name2}", icon_url=user2.display_avatar.url)
        embed.set_footer(text=f"Requested by {interaction.user.display_name} • MSL Network")

        await interaction.response.send_message(embed=embed)

    # =================================================================
    # /social 8ball — Magic 8-Ball
    # =================================================================

    @app_commands.command(name="8ball", description="Ask the Magic 8-Ball a question!")
    @app_commands.checks.cooldown(1, 5, key=lambda i: i.user.id)
    async def eight_ball(self, interaction: discord.Interaction, question: str):
        answer = random.choice(EIGHT_BALL_RESPONSES)

        # Color based on answer type
        if answer.startswith("🟢"):
            color = 0x2ECC71
        elif answer.startswith("🟡"):
            color = 0xF1C40F
        else:
            color = 0xE74C3C

        embed = discord.Embed(
            description=(
                f"## 🎱  Magic 8-Ball\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"**❓ Question:**\n> *{question}*\n\n"
                f"**🔮 The Oracle speaks:**\n"
                f"## {answer}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━"
            ),
            color=color,
            timestamp=datetime.now(TZ_MANILA),
        )
        embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
        embed.set_footer(text="The 8-Ball has spoken • MSL Network")

        await interaction.response.send_message(embed=embed)

    # =================================================================
    # /social marriage — Propose, Divorce, Status
    # =================================================================

    @marriage_group.command(name="propose", description="Propose marriage to another member!")
    @app_commands.checks.cooldown(1, 30, key=lambda i: i.user.id)
    async def marriage_propose(self, interaction: discord.Interaction, user: discord.Member):
        # Block self
        if user.id == interaction.user.id:
            return await interaction.response.send_message("❌ You can't marry yourself!", ephemeral=True)

        # Block bots
        if user.bot:
            return await interaction.response.send_message("❌ You can't marry a bot!", ephemeral=True)

        # Check if proposer is already married
        existing = await db.fetch_one(
            "SELECT * FROM marriages WHERE user1_id = %s OR user2_id = %s",
            (interaction.user.id, interaction.user.id)
        )
        if existing:
            return await interaction.response.send_message("❌ You're already married! Use `/social marriage divorce` first.", ephemeral=True)

        # Check if target is already married
        target_existing = await db.fetch_one(
            "SELECT * FROM marriages WHERE user1_id = %s OR user2_id = %s",
            (user.id, user.id)
        )
        if target_existing:
            return await interaction.response.send_message(f"❌ **{user.display_name}** is already married to someone else!", ephemeral=True)

        embed = discord.Embed(
            description=(
                f"## 💍  Marriage Proposal\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"**{interaction.user.display_name}** has gotten down on one knee and is proposing to **{user.display_name}**!\n\n"
                f"*Will you accept this sacred union?* 💒\n\n"
                f"{user.mention}, please respond below.\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━"
            ),
            color=0xFF69B4,
            timestamp=datetime.now(TZ_MANILA),
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
        embed.set_footer(text="⏳ This proposal expires in 2 minutes • MSL Network")

        view = ProposalView(interaction.user, user)
        await interaction.response.send_message(embed=embed, view=view)

    @marriage_group.command(name="divorce", description="End your current marriage.")
    async def marriage_divorce(self, interaction: discord.Interaction):
        existing = await db.fetch_one(
            "SELECT * FROM marriages WHERE user1_id = %s OR user2_id = %s",
            (interaction.user.id, interaction.user.id)
        )
        if not existing:
            return await interaction.response.send_message("❌ You're not married!", ephemeral=True)

        partner_id = existing['user2_id'] if existing['user1_id'] == interaction.user.id else existing['user1_id']

        await db.execute("DELETE FROM marriages WHERE id = %s", (existing['id'],))

        embed = discord.Embed(
            description=(
                f"## 💔  Divorce Finalized\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"**{interaction.user.display_name}** and <@{partner_id}> have parted ways.\n\n"
                f"*Sometimes, paths diverge... but the memories remain.*\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━"
            ),
            color=0x708090,
            timestamp=datetime.now(TZ_MANILA),
        )
        embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
        embed.set_footer(text="MSL Network")
        await interaction.response.send_message(embed=embed)

    @marriage_group.command(name="status", description="View your or someone's marriage status.")
    async def marriage_status(self, interaction: discord.Interaction, user: discord.Member = None):
        target = user or interaction.user

        existing = await db.fetch_one(
            "SELECT * FROM marriages WHERE user1_id = %s OR user2_id = %s",
            (target.id, target.id)
        )
        if not existing:
            name = "You're" if target.id == interaction.user.id else f"**{target.display_name}** is"
            return await interaction.response.send_message(f"💔 {name} not married.", ephemeral=True)

        partner_id = existing['user2_id'] if existing['user1_id'] == target.id else existing['user1_id']
        married_at = existing['married_at']
        days = (datetime.now() - married_at).days if married_at else 0

        # Anniversary milestones
        milestone = ""
        if days >= 365:
            milestone = f"\n🎊 **{days // 365} Year{'s' if days // 365 > 1 else ''} Anniversary!**"
        elif days >= 30:
            milestone = f"\n🌙 **{days // 30} Month{'s' if days // 30 > 1 else ''} Together!**"
        elif days >= 7:
            milestone = f"\n✨ **{days // 7} Week{'s' if days // 7 > 1 else ''} Strong!**"

        embed = discord.Embed(
            description=(
                f"## 💒  Marriage Certificate\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"<@{target.id}>  💍  <@{partner_id}>\n\n"
                f"📅 **Married Since:** {married_at.strftime('%B %d, %Y') if married_at else 'Unknown'}\n"
                f"⏰ **Duration:** {days:,} day{'s' if days != 1 else ''}\n"
                f"{milestone}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━"
            ),
            color=0xFF69B4,
            timestamp=datetime.now(TZ_MANILA),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.set_footer(text="MSL Network")

        await interaction.response.send_message(embed=embed)

    # =================================================================
    # /social family — Adopt, Disown, Tree
    # =================================================================

    @family_group.command(name="adopt", description="Adopt a member as your child!")
    @app_commands.checks.cooldown(1, 30, key=lambda i: i.user.id)
    async def family_adopt(self, interaction: discord.Interaction, user: discord.Member):
        # Block self
        if user.id == interaction.user.id:
            return await interaction.response.send_message("❌ You can't adopt yourself!", ephemeral=True)

        # Block bots
        if user.bot:
            return await interaction.response.send_message("❌ You can't adopt a bot!", ephemeral=True)

        # Check if target already has a parent
        existing_parent = await db.fetch_one(
            "SELECT * FROM family WHERE child_id = %s", (user.id,)
        )
        if existing_parent:
            return await interaction.response.send_message(f"❌ **{user.display_name}** already has a parent!", ephemeral=True)

        # Prevent circular: can't adopt your own parent
        my_parent = await db.fetch_one(
            "SELECT * FROM family WHERE child_id = %s", (interaction.user.id,)
        )
        if my_parent and my_parent['parent_id'] == user.id:
            return await interaction.response.send_message("❌ You can't adopt your own parent! That would break reality.", ephemeral=True)

        # Prevent adopting your spouse
        marriage = await db.fetch_one(
            "SELECT * FROM marriages WHERE (user1_id = %s AND user2_id = %s) OR (user1_id = %s AND user2_id = %s)",
            (interaction.user.id, user.id, user.id, interaction.user.id)
        )
        if marriage:
            return await interaction.response.send_message("❌ You can't adopt your spouse!", ephemeral=True)

        # Check for deeper circular chains: walk up the tree from the proposer
        # to make sure the target isn't an ancestor
        current_id = interaction.user.id
        for _ in range(20):  # safety limit to prevent infinite loops
            ancestor = await db.fetch_one("SELECT parent_id FROM family WHERE child_id = %s", (current_id,))
            if not ancestor:
                break
            if ancestor['parent_id'] == user.id:
                return await interaction.response.send_message("❌ You can't adopt someone who is already your ancestor!", ephemeral=True)
            current_id = ancestor['parent_id']

        embed = discord.Embed(
            description=(
                f"## 🏠  Adoption Request\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"**{interaction.user.display_name}** wants to adopt **{user.display_name}** into their family!\n\n"
                f"*Will you accept this family bond?* 🤝\n\n"
                f"{user.mention}, please respond below.\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━"
            ),
            color=0x87CEEB,
            timestamp=datetime.now(TZ_MANILA),
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
        embed.set_footer(text="⏳ This request expires in 2 minutes • MSL Network")

        view = AdoptionView(interaction.user, user)
        await interaction.response.send_message(embed=embed, view=view)

    @family_group.command(name="disown", description="Remove a child or leave your parent.")
    async def family_disown(self, interaction: discord.Interaction, user: discord.Member):
        # Check if I'm their parent
        as_parent = await db.fetch_one(
            "SELECT id FROM family WHERE parent_id = %s AND child_id = %s",
            (interaction.user.id, user.id)
        )
        # Check if they're my parent
        as_child = await db.fetch_one(
            "SELECT id FROM family WHERE parent_id = %s AND child_id = %s",
            (user.id, interaction.user.id)
        )

        if as_parent:
            await db.execute("DELETE FROM family WHERE id = %s", (as_parent['id'],))
            embed = discord.Embed(
                description=(
                    f"## 👋  Family Bond Severed\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"**{interaction.user.display_name}** has disowned **{user.display_name}**.\n\n"
                    f"*The bond has been released.*\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━━"
                ),
                color=0x708090,
                timestamp=datetime.now(TZ_MANILA),
            )
        elif as_child:
            await db.execute("DELETE FROM family WHERE id = %s", (as_child['id'],))
            embed = discord.Embed(
                description=(
                    f"## 🕊️  Left the Nest\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"**{interaction.user.display_name}** has left **{user.display_name}**'s family.\n\n"
                    f"*Time to spread those wings.*\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━━"
                ),
                color=0x708090,
                timestamp=datetime.now(TZ_MANILA),
            )
        else:
            return await interaction.response.send_message("❌ You have no family relationship with this person.", ephemeral=True)

        embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
        embed.set_footer(text="MSL Network")
        await interaction.response.send_message(embed=embed)

    @family_group.command(name="tree", description="View your or someone's family tree!")
    async def family_tree(self, interaction: discord.Interaction, user: discord.Member = None):
        target = user or interaction.user

        lines = []

        # Find spouse
        marriage = await db.fetch_one(
            "SELECT * FROM marriages WHERE user1_id = %s OR user2_id = %s",
            (target.id, target.id)
        )
        spouse_str = ""
        if marriage:
            partner_id = marriage['user2_id'] if marriage['user1_id'] == target.id else marriage['user1_id']
            spouse_str = f"  💍 <@{partner_id}>"

        # Find parent
        parent_row = await db.fetch_one("SELECT parent_id FROM family WHERE child_id = %s", (target.id,))
        if parent_row:
            lines.append(f"👑 **Parent:** <@{parent_row['parent_id']}>")

        # Self line
        lines.append(f"🧑 **{target.display_name}**{spouse_str}")

        # Find children
        children = await db.fetch_all("SELECT child_id FROM family WHERE parent_id = %s", (target.id,))
        if children:
            for i, child in enumerate(children):
                connector = "└" if i == len(children) - 1 else "├"
                lines.append(f"  {connector}── 🧒 <@{child['child_id']}>")

        if not parent_row and not children and not marriage:
            lines = [
                f"🧑 **{target.display_name}**\n",
                f"*No family connections yet.*",
                f"*Use `/social marriage propose` or `/social family adopt` to start!*"
            ]

        embed = discord.Embed(
            description=(
                f"## 🌳  {target.display_name}'s Family Tree\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                + "\n".join(lines) +
                f"\n\n━━━━━━━━━━━━━━━━━━━━━"
            ),
            color=0x2ECC71,
            timestamp=datetime.now(TZ_MANILA),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.set_footer(text="MSL Network")
        await interaction.response.send_message(embed=embed)

    # =================================================================
    # COOLDOWN ERROR HANDLER
    # =================================================================

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message(
                f"⏳ Slow down! Try again in **{error.retry_after:.1f}s**.",
                ephemeral=True,
            )
        else:
            raise error

    # =================================================================
    # WEEKLY CONTRIBUTOR (existing logic preserved)
    # =================================================================

    @tasks.loop(minutes=5)
    async def weekly_contributor_loop(self):
        """Runs every Sunday at 8 AM Manila Time to determine the Contributor of the Week."""
        now = datetime.now(TZ_MANILA)
        if now.weekday() == 6 and now.hour == 8 and 0 <= now.minute < 5:
            await self._run_weekly_contributor()
            
    @weekly_contributor_loop.before_loop
    async def before_weekly_contributor(self):
        await self.bot.wait_until_ready()

    async def _run_weekly_contributor(self):
        # 1. Fetch top receiver from the past 7 days
        target_span = datetime.now() - timedelta(days=7)
        rows = await db.fetch_all('''
            SELECT receiver_id, COUNT(*) as count 
            FROM thanks_history 
            WHERE created_at >= %s
            GROUP BY receiver_id 
            ORDER BY count DESC
        ''', (target_span,))
        
        if not rows: return
        
        max_count = rows[0]['count']
        winners = [r['receiver_id'] for r in rows if r['count'] == max_count]
        
        # 2. Assign Moniyan Mentor Role
        mentor_role_id = await settings_service.get_int("role_id_mentor")
        mentor_role = None
        
        guild = self.bot.guilds[0] if self.bot.guilds else None
        if not guild: return
        
        if mentor_role_id:
            mentor_role = guild.get_role(mentor_role_id)
            if mentor_role:
                # 3. Strip from previous week
                for member in mentor_role.members:
                    if member.id not in winners:
                        try: await member.remove_roles(mentor_role, reason="Weekly Contributor rotation")
                        except: pass
                
                # 4. Award to new winners
                for wid in winners:
                    try:
                        mem = guild.get_member(wid)
                        if mem: await mem.add_roles(mentor_role, reason="Contributor of the Week")
                    except: pass
                    
        # 5. Output announcement in public event log 
        out_channel_id = await settings_service.get_int("boost_public_channel_id")
        if out_channel_id:
            channel = guild.get_channel(out_channel_id)
            if channel:
                mentions = ", ".join([f"<@{w}>" for w in winners])
                embed = discord.Embed(
                    title="🏆 Contributors of the Week",
                    description=f"Congratulations to {mentions} for answering the most questions and helping the community this week! You have gained **{max_count} Thanks** and earned the **Moniyan Mentor** role for 7 days!",
                    color=discord.Color.gold()
                )
                await channel.send(embed=embed)

async def setup(bot: commands.Bot):
    await bot.add_cog(SocialCog(bot))
