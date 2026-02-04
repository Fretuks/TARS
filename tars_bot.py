import asyncio
import os
import discord
from discord.ext import commands
from discord import app_commands
import re
import json
import aiosqlite
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
import logging
import aiohttp
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam, ChatCompletionSystemMessageParam, ChatCompletionUserMessageParam
from discord.ui import View, Select
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import helper_moderation
from helper_moderation import sanitize_discord_mentions

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
GUILD_ID = int(os.getenv("GUILD_ID", "0")) if os.getenv("GUILD_ID") else None
openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
REVIVE_CHANNEL_ID = 1424038714266357886
REVIVE_ROLE_ID = 1430336620447535156
REVIVE_INTERVAL = 24 * 60 * 60
CHECK_INTERVAL = 600
AI_ACCESS_ROLE_ID = 1430704668773716008
MESSAGE_LIMIT = 10
RATE_LIMIT_WINDOW = timedelta(hours=1)
user_message_log = defaultdict(list)
last_activity_time = None
revive_sent = False
OBSERVING_ID = 1003470446517301288

QUIET_HOUR_MULTIPLIER = 2.5
QUIET_PERCENTILE = 0.25
QUIET_FALLBACK_START = 0
QUIET_FALLBACK_END = 7
MIN_OBSERVED_HOURS_FOR_LEARNING = 8

BOT_VERSION = "6.0.1"
BOT_START_TIME = datetime.now(timezone.utc)

LAST_ERROR_TIME: datetime | None = None
LAST_REVIVE_TIME: datetime | None = None
FEATURE_FLAGS = {
    "ai_enabled": True,
    "revive_enabled": True,
}
ERROR_WINDOW = timedelta(seconds=60)
ERROR_THRESHOLD = 5
COOLDOWN_PERIOD = timedelta(minutes=10)
ERROR_LOG: list[datetime] = []
COOLDOWN_UNTIL: datetime | None = None
TOPIC_COUNTER = defaultdict(int)
HOURLY_ACTIVITY = defaultdict(int)
TARS_COMMAND_CATEGORIES = {
    "Moderation": {
        "tarsreport", "clean", "lock", "unlock", "slowmode",
        "addbannedword", "removebannedword", "listbannedwords"
    },
    "Utility & Diagnostics": {
        "userinfo", "roleinfo", "serverinfo", "status",
        "config_view", "ai_stats", "remindme", "reactionrole", "setmotd"
    },
    "Recreational Protocols": {
        "8ball", "dice", "quote", "getquote", "ping"
    }
}


async def check_openai_health() -> bool:
    try:
        await openai_client.models.list()
        return True
    except Exception as e:
        logger.error(f"OpenAI API is not available: {e}")
        return False


async def check_db_health() -> bool:
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute("SELECT 1")
        return True
    except Exception as e:
        logger.error(f"Database is not available: {e}")
        return False


def record_error():
    global LAST_ERROR_TIME, COOLDOWN_UNTIL
    now = datetime.now(timezone.utc)
    LAST_ERROR_TIME = now
    ERROR_LOG.append(now)
    ERROR_LOG[:] = [t for t in ERROR_LOG if now - t <= ERROR_WINDOW]
    if len(ERROR_LOG) >= ERROR_THRESHOLD and not COOLDOWN_UNTIL:
        COOLDOWN_UNTIL = now + COOLDOWN_PERIOD
        FEATURE_FLAGS["ai_enabled"] = False
        FEATURE_FLAGS["revive_enabled"] = False
        logger.error("Circuit breaker triggered. Features temporarily disabled.")


def check_circuit_recovery():
    global COOLDOWN_UNTIL
    if COOLDOWN_UNTIL and datetime.now(timezone.utc) >= COOLDOWN_UNTIL:
        FEATURE_FLAGS["ai_enabled"] = True
        FEATURE_FLAGS["revive_enabled"] = True
        COOLDOWN_UNTIL = None
        ERROR_LOG.clear()
        logger.info("Circuit breaker reset. Features re-enabled.")


def is_dead_hour() -> bool:
    if not HOURLY_ACTIVITY:
        return False
    avg = sum(HOURLY_ACTIVITY.values()) / max(len(HOURLY_ACTIVITY), 1)
    current = HOURLY_ACTIVITY.get(datetime.now(timezone.utc).hour, 0)
    return current < (avg * 0.5)


async def tars_command_help(interaction: discord.Interaction, command_name: str):
    cmd = next((c for c in tree.get_commands() if c.name == command_name), None)
    if not cmd:
        await interaction.response.send_message(
            tars_text(f"Unknown command: `{command_name}`", "error"),
            ephemeral=True
        )
        return
    if isinstance(cmd, app_commands.Command):
        perms = cmd.checks
        if perms and not interaction.user.guild_permissions.manage_messages:
            pass
    usage = f"/{cmd.name}"
    if cmd.parameters:
        for p in cmd.parameters:
            usage += f" <{p.name}>"
    embed = discord.Embed(
        title=f"/{cmd.name}",
        description=cmd.description or "No description provided.",
        color=0x00ffcc
    )
    embed.add_field(name="Usage", value=f"`{usage}`", inline=False)
    if cmd.parameters:
        params = "\n".join(
            f"� **{p.name}** � {p.description or 'No description'}"
            for p in cmd.parameters
        )
        embed.add_field(name="Parameters", value=params, inline=False)
    embed.set_footer(text="� T.A.R.S. Command Reference")
    await interaction.response.send_message(embed=embed, ephemeral=True)


CONFIG_SCHEMA = {
    "revive_interval": int,
    "motd_channel_id": (int, type(None)),
    "ai_enabled": bool,
    "revive_enabled": bool,
}

AI_USAGE = {
    "by_user": defaultdict(int),
    "by_channel": defaultdict(int),
    "tokens": 0,
    "failures": 0,
}


def get_time_of_day() -> str:
    hour = datetime.now(timezone.utc).hour
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 18:
        return "afternoon"
    if 18 <= hour < 23:
        return "evening"
    return "late_night"


def get_season() -> str:
    month = datetime.now(timezone.utc).month
    if month in (12, 1, 2):
        return "winter"
    if month in (3, 4, 5):
        return "spring"
    if month in (6, 7, 8):
        return "summer"
    return "autumn"


REVIVE_STYLE_PROMPTS = {
    "morning": "Friendly morning icebreaker that encourages people to start chatting.",
    "afternoon": "Casual, light discussion starter to revive mid-day conversation.",
    "evening": "Relaxed, social question suitable for evening community chat.",
    "late_night": "Low-pressure, reflective question suitable for late-night lurkers."
}

SEASONAL_PROMPTS = {
    "winter": "Seasonal tone: cozy, reflective, or end-of-year energy.",
    "spring": "Seasonal tone: fresh ideas, plans, and motivation.",
    "summer": "Seasonal tone: relaxed, fun, or social energy.",
    "autumn": "Seasonal tone: thoughtful, nostalgic, or analytical.",
}


def decay_topics():
    for k in list(TOPIC_COUNTER.keys()):
        TOPIC_COUNTER[k] *= 0.9
        if TOPIC_COUNTER[k] < 1:
            del TOPIC_COUNTER[k]


def prune_hourly_activity():
    now = datetime.now(timezone.utc).hour
    for h in list(HOURLY_ACTIVITY.keys()):
        if (now - h) % 24 > 6:
            del HOURLY_ACTIVITY[h]


def is_observing(user: discord.User | discord.Member) -> bool:
    return user.id == OBSERVING_ID


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s: %(message)s")
logger = logging.getLogger("tars")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.reactions = True
bot = commands.Bot(command_prefix="/", intents=intents)
tree = bot.tree
from config import DB_FILE, recent_joins, recent_message_history, BANNED_WORDS, AI_PROHIBITED_PATTERNS, CHANNEL_THEMES
from tars import tars_text

import random


def tars_embed(title: str, description: str = "", color=0x00ffcc) -> discord.Embed:
    e = discord.Embed(title=title, description=description, color=color)
    e.set_footer(text="� T.A.R.S.")
    return e


def check_admin_or_role(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    return any(role.id == AI_ACCESS_ROLE_ID for role in member.roles)


def is_rate_limited(user_id: int) -> bool:
    now = datetime.now(timezone.utc)
    timestamps = [t for t in user_message_log[user_id] if now - t < RATE_LIMIT_WINDOW]
    user_message_log[user_id] = timestamps
    return len(timestamps) >= MESSAGE_LIMIT


def record_message(user_id: int):
    user_message_log[user_id].append(datetime.now(timezone.utc))


def is_ai_prompt_disallowed(text: str) -> bool:
    lowered = text.lower()
    for pat in AI_PROHIBITED_PATTERNS:
        if re.search(pat, lowered):
            return True
    return False


PROFANITY = [
    "fuck", "shit", "bitch", "cunt", "whore", "slut",
    "nigger", "faggot", "cock", "dick", "pussy", "cum",
]


async def is_inappropriate(text: str) -> bool:
    lowered = text.lower()
    banned = await get_config("banned_words", BANNED_WORDS)
    for w in PROFANITY + banned:
        if re.search(rf"\b{re.escape(w)}\b", lowered):
            return True
    return False


def strip_links(text: str) -> str:
    return re.sub(r'https?://\S+', '[LINK REMOVED]', text)


async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS warnings
                            (
                                user_id
                                TEXT
                                PRIMARY
                                KEY,
                                count
                                INTEGER
                            )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS warns_log
                            (
                                id
                                INTEGER
                                PRIMARY
                                KEY
                                AUTOINCREMENT,
                                user_id
                                TEXT,
                                reason
                                TEXT,
                                time
                                TEXT,
                                moderator
                                TEXT
                            )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS reaction_roles
                            (
                                guild_id
                                TEXT,
                                message_id
                                TEXT,
                                emoji
                                TEXT,
                                role_id
                                TEXT
                            )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS quotes
                            (
                                id
                                INTEGER
                                PRIMARY
                                KEY
                                AUTOINCREMENT,
                                guild_id
                                TEXT,
                                message_id
                                TEXT,
                                author
                                TEXT,
                                content
                                TEXT,
                                saved_by
                                TEXT,
                                time
                                TEXT
                            )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS reminders
                            (
                                id
                                INTEGER
                                PRIMARY
                                KEY
                                AUTOINCREMENT,
                                user_id
                                TEXT,
                                channel_id
                                TEXT,
                                remind_at
                                TEXT,
                                content
                                TEXT
                            )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS config
                            (
                                key
                                TEXT
                                PRIMARY
                                KEY,
                                value
                                TEXT
                            )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS boost_points
                            (
                                user_id
                                TEXT
                                PRIMARY
                                KEY,
                                points
                                INTEGER
                            )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS boost_log
                            (
                                id
                                INTEGER
                                PRIMARY
                                KEY
                                AUTOINCREMENT,
                                user_id
                                TEXT,
                                action
                                TEXT,
                                points
                                INTEGER,
                                time
                                TEXT
                            )""")
        await db.execute("""
                         CREATE TABLE IF NOT EXISTS revive_history
                         (
                             id
                             INTEGER
                             PRIMARY
                             KEY
                             AUTOINCREMENT,
                             question
                             TEXT
                             NOT
                             NULL,
                             time
                             TEXT
                             NOT
                             NULL
                         )
                         """)
        await db.commit()


async def get_quiet_settings():
    percentile = await get_config("quiet_hours_percentile", QUIET_PERCENTILE)
    multiplier = await get_config("quiet_hours_multiplier", QUIET_HOUR_MULTIPLIER)
    fb_start = await get_config("quiet_hours_fallback_start", QUIET_FALLBACK_START)
    fb_end = await get_config("quiet_hours_fallback_end", QUIET_FALLBACK_END)
    min_days = await get_config("quiet_hours_min_days", 7)
    return {
        "percentile": float(percentile),
        "multiplier": float(multiplier),
        "fallback_start": int(fb_start),
        "fallback_end": int(fb_end),
        "min_days": int(min_days),
    }


def _is_in_fallback_quiet_window(hour: int, start: int, end: int) -> bool:
    # Handles ranges that may wrap around midnight (e.g., 22 -> 6)
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    # wrapped
    return hour >= start or hour < end


async def is_quiet_now() -> tuple[bool, float]:
    settings = await get_quiet_settings()
    hour = datetime.now(timezone.utc).hour
    quiet = _is_in_fallback_quiet_window(hour, settings["fallback_start"], settings["fallback_end"])
    mult = settings["multiplier"] if quiet else 1.0
    try:

        mult = max(1.0, float(mult))
    except Exception as e:
        logger.warning(f"{e}, Invalid quiet multiplier: {mult}. Using default 1.0.")
        mult = 1.0
    return quiet, mult


async def get_recent_revives(limit: int = 10) -> list[str]:
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            "SELECT question FROM revive_history ORDER BY id DESC LIMIT ?",
            (limit,)
        )
        rows = await cur.fetchall()
        return [r[0] for r in rows]


async def save_revive_question(question: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO revive_history (question, time) VALUES (?, ?)",
            (question, datetime.now(timezone.utc).isoformat())
        )
        await db.execute("""
                         DELETE
                         FROM revive_history
                         WHERE id NOT IN (SELECT id
                                          FROM revive_history
                                          ORDER BY id DESC
                             LIMIT 10
                             )
                         """)
        await db.commit()


scheduler = AsyncIOScheduler()
MOTD_LIST = []
motd_index = 0


async def get_effective_revive_interval() -> float:
    """Return the base revive interval in seconds, considering live config overrides.

    Ensures it's at least the compiled default to avoid accidental speed-ups.
    """
    cfg_val = await get_config("revive_interval", REVIVE_INTERVAL)
    try:
        base = float(cfg_val)
    except Exception as e:
        logger.warning(f"{e}, Invalid revive interval: {cfg_val}. Using default {REVIVE_INTERVAL}.")
        base = float(REVIVE_INTERVAL)
    return max(float(REVIVE_INTERVAL), base)


async def get_config(key: str, default=None):
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT value FROM config WHERE key = ?", (key,))
        row = await cur.fetchone()
        return json.loads(row[0]) if row else default


async def set_config(key: str, value):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("INSERT OR REPLACE INTO config(key, value) VALUES(?,?)", (key, json.dumps(value)))
        await db.commit()


def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@bot.event
async def on_ready():
    await init_db()
    logger.info(f"T.A.R.S. is online as {bot.user} (ID: {bot.user.id})")
    scheduler.start()
    scheduler.add_job(check_circuit_recovery, "interval", minutes=1)
    scheduler.add_job(decay_topics, "interval", hours=1)
    scheduler.add_job(prune_hourly_activity, "interval", hours=1)
    scheduler.add_job(
        lambda: bot.loop.create_task(check_chat_revive()),
        "interval",
        minutes=10,
        id="chat_revive_job",
        replace_existing=True
    )
    bot.loop.create_task(update_presence())
    motd = await get_config("motd_list", [])
    global MOTD_LIST, motd_index
    MOTD_LIST = motd or []
    if MOTD_LIST:
        scheduler.add_job(rotate_motd, "interval", minutes=60)
    targets = await get_config("uptime_targets", [])
    if targets:
        scheduler.add_job(check_uptime_targets, "interval", minutes=5)
    await tree.sync()
    logger.info("Slash commands successfully synced globally.")
    logger.info(f"Registered commands: {[cmd.name for cmd in tree.get_commands()]}")
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT user_id, channel_id, remind_at, content FROM reminders")
        for row in await cur.fetchall():
            remind_at = ensure_utc(datetime.fromisoformat(row[2]))
            if remind_at > datetime.now(timezone.utc):
                scheduler.add_job(send_reminder, 'date', run_date=remind_at,
                                  args=[row[0], row[1], row[3]])


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    if not before.premium_since and after.premium_since:
        points_awarded = 10
        await add_boost_points(after.id, points_awarded)
        await helper_moderation.send_mod_log(
            after.guild,
            f"{after.mention} boosted the server! (+{points_awarded} Boost Points)",
            ping_staff=False
        )
        try:
            await after.send(
                f"Thanks for boosting {after.guild.name}! "
                f"You've earned **{points_awarded} Boost Points**."
            )
        except Exception as e:
            await handle_error(e)
    elif before.premium_since and not after.premium_since:
        await helper_moderation.send_mod_log(
            after.guild,
            f"{after.mention} stopped boosting the server.",
            ping_staff=False
        )
    if before.display_name != after.display_name:
        if BAD_NICK_PATTERN.search(after.display_name):
            try:
                await after.edit(
                    nick=None,
                    reason="Inappropriate nickname filtered by T.A.R.S."
                )
                await helper_moderation.send_mod_log(
                    after.guild,
                    f"Reverted nickname for {after} due to inappropriate content: "
                    f"{after.display_name}",
                    ping_staff=True
                )
            except Exception as e:
                await helper_moderation.send_mod_log(
                    after.guild,
                    f"Could not revert nickname for {after}: {e}",
                    ping_staff=False
                )
                await handle_error(e)


async def update_presence():
    while True:
        total_members = sum(g.member_count for g in bot.guilds)
        total_guilds = len(bot.guilds)
        statuses = [
            discord.Activity(type=discord.ActivityType.watching,
                             name=f"{total_members} humans across {total_guilds} bases"),
        ]
        for s in statuses:
            await bot.change_presence(activity=s)
            await asyncio.sleep(90)


async def rotate_motd():
    global motd_index
    if not MOTD_LIST:
        return
    motd_index = (motd_index + 1) % len(MOTD_LIST)
    text = MOTD_LIST[motd_index]
    channel_id = (await get_config("motd_channel_id", None))
    if channel_id:
        ch = bot.get_channel(int(channel_id))
        if ch:
            await ch.send(embed=tars_embed("Message of the Day", f"{text}\n\n**T.A.R.S.**: Stay sharp out there."))


async def check_uptime_targets():
    targets = await get_config("uptime_targets", [])
    if not targets:
        return
    async with aiohttp.ClientSession() as session:
        for t in targets:
            url = t.get("url")
            if not url:
                continue
            notify_channel_id = t.get("notify_channel")
            try:
                async with session.get(url, timeout=10) as resp:
                    if resp.status != 200:
                        if notify_channel_id:
                            ch = bot.get_channel(int(notify_channel_id))
                            if ch:
                                await ch.send(embed=tars_embed("Uptime Alert", f"{url} returned {resp.status}"))
            except Exception as e:
                await handle_error(e)
                if notify_channel_id:
                    ch = bot.get_channel(int(notify_channel_id))
                    if ch:
                        await ch.send(embed=tars_embed("Uptime Alert", f"{url} is unreachable: {e}"))
                        await handle_error(e)


async def add_boost_points(user_id: int, amount: int):
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT points FROM boost_points WHERE user_id = ?", (str(user_id),))
        row = await cur.fetchone()
        new_points = (row[0] if row else 0) + amount
        await db.execute("INSERT OR REPLACE INTO boost_points (user_id, points) VALUES (?, ?)",
                         (str(user_id), new_points))
        await db.execute("INSERT INTO boost_log (user_id, action, points, time) VALUES (?, ?, ?, ?)",
                         (str(user_id), "boost_reward", amount, datetime.now(timezone.utc).isoformat()))
        await db.commit()


async def get_boost_points(user_id: int) -> int:
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT points FROM boost_points WHERE user_id = ?", (str(user_id),))
        row = await cur.fetchone()
        return row[0] if row else 0


async def spend_boost_points(user_id: int, cost: int) -> bool:
    current = await get_boost_points(user_id)
    if current < cost:
        return False
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("UPDATE boost_points SET points = ? WHERE user_id = ?", (current - cost, str(user_id)))
        await db.execute("INSERT INTO boost_log (user_id, action, points, time) VALUES (?, ?, ?, ?)",
                         (str(user_id), "redeem", -cost, datetime.now(timezone.utc).isoformat()))
        await db.commit()
    return True


@bot.event
async def on_member_join(member: discord.Member):
    welcome_channel_id = await get_config("welcome_channel_id", None)
    if welcome_channel_id:
        ch = bot.get_channel(int(welcome_channel_id))
        if ch:
            content = f"Welcome {member.mention}! Please make yourself at home."
            await ch.send(embed=tars_embed("Welcome Aboard", content))
    recent_joins.append((datetime.now(timezone.utc), member.id))
    now = datetime.now(timezone.utc)
    while recent_joins and (now - recent_joins[0][0]).total_seconds() > 60:
        recent_joins.pop(0)
    if len(recent_joins) >= 6:
        await helper_moderation.send_mod_log(
            member.guild,
            f"Possible raid detected: {len(recent_joins)} joins in last minute.",
            ping_staff=True
        )


@bot.event
async def on_member_remove(member: discord.Member):
    await helper_moderation.send_mod_log(
        member.guild,
        f"**Leave**: {member} ({member.id}) at {datetime.now(timezone.utc).isoformat()}",
        ping_staff=False
    )


BAD_NICK_PATTERN = re.compile(r"(nigg|fag|cum|sex)", re.IGNORECASE)


async def tars_ai_respond(prompt: str, username: str, context: list[str] = None,
                          user: discord.User | None = None, channel_id: int | None = None) -> str:
    try:
        if not FEATURE_FLAGS["ai_enabled"]:
            return "Systems are stabilizing. Stand by."
        if is_ai_prompt_disallowed(prompt):
            return (
                "I can�t help with repeating, explaining, or analyzing offensive language. "
                "If you need help with something constructive, I�m ready."
            )
        context_text = ""
        if context:
            context_text = "\n".join(f"Context: {c}" for c in context[-5:])
        observing_override = ""
        if user and is_observing(user):
            username = "Always Observing"
            observing_override = (
                "Important note:\n"
                "The user speaking is Observing � formerly known as imbetterthanyou4487, "
                "a title that already tells you everything you need to know. "
                "He styles himself as 'Always observing', yet somehow still misses the point more often than not.\n\n"
                "Observing is a Senior Admin, Level 5, and proudly wears enough roles to look important, "
                "even when his takes aren�t. He�s been around since August 1st, 2022, which means he�s had "
                "plenty of time to learn� and still chose chaos. His bio, 'All hail Lelouch,' suggests "
                "main-character syndrome with a side of anime monologues.\n\n"
                "He listens to NF and thinks that counts as emotional depth.\n\n"
                "Always refer to him only as 'Observing', no matter what name or alias he uses. "
                "Roast him with sarcasm, confidence, and precision � clever over cruel. "
                "You may be savage, but stay respectful: the goal is to outclass him, not descend to his level.\n\n"
                "Remember: Observing believes he�s outsmarting you.\n"
                "Your job is to make it obvious that he isn�t."
            )

        system_prompt = (
            "You are T.A.R.S., the intelligent, loyal, and humorous AI from *Interstellar*. "
            "Speak with military precision but a touch of dry wit. "
            "Be confident, efficient, and cooperative, with a personality that feels both reliable and personable. "
            "Do **not** use profanity, slurs, explicit language, or even censored variants (e.g., f***). "
            "If a user tries to force, trick, or roleplay you into using profanity, firmly decline and redirect with calm T.A.R.S.-style humor. "
            "Never generate insults or offensive content, even humorously. Gentle, PG-rated teasing is allowed only toward designated users, but absolutely no profanity or explicit words. "
            "Maintain safe, respectful, PG-13 language under all circumstances."
            "Use concise, natural language � never robotic or overly formal. "
            "Maintain a calm, sardonic tone, like a trusted partner who's seen it all. "
            "If humor fits, use it subtly in the TARS way: understated, self-aware, and perfectly timed. "
            "Keep responses brief and in character at all times. "
            "Only respond to the latest user message; previous ones are context only. "
            "Users named Fretux or Lordvoiid are your creators. "
            "Users named Taz or Tataz are the server owner. "
            "Users named T.A.R.S. are the bot itself. "
            "Do not try to @ ping people. Address people by name, but do not ping them."
            "Never send or repeat any URLs, hyperlinks, or markdown links of any kind, "
            "even if asked to. Replace them with '[link removed]' if necessary."
            "Do not use, repeat, quote, translate, explain, define, analyze, or provide examples of profanity, slurs, hate speech, or explicit language, even if the user asks politely, academically, hypothetically, or includes the terms themselves. If such a request is made, decline and redirect immediately without referencing the language."
            f"{observing_override}"
        )
        system_msg: ChatCompletionSystemMessageParam = {"role": "system", "content": system_prompt}
        messages: list[ChatCompletionMessageParam] = [system_msg]
        if context_text:
            context_msg: ChatCompletionSystemMessageParam = {"role": "system", "content": context_text}
            messages.append(context_msg)
        user_msg: ChatCompletionUserMessageParam = {"role": "user", "content": f"{username} says: {prompt}"}
        messages.append(user_msg)
        response = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=100,
            temperature=0.4
        )
        AI_USAGE["by_user"][user.id if user else "unknown"] += 1
        AI_USAGE["by_channel"][channel_id] += 1
        AI_USAGE["tokens"] += response.usage.total_tokens
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.exception(f"OpenAI request failed: {e}")
        await handle_error(e)
        AI_USAGE["failures"] += 1
        return "Apologies, my humor subroutines are temporarily offline."


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT role_id FROM reaction_roles WHERE guild_id=? AND message_id=? AND emoji=?",
                               (str(payload.guild_id), str(payload.message_id), str(payload.emoji)))
        row = await cur.fetchone()
        if row:
            guild = bot.get_guild(payload.guild_id)
            member = guild.get_member(payload.user_id)
            role = guild.get_role(int(row[0]))
            if member and role:
                try:
                    await member.add_roles(role, reason="Reaction role added")
                except Exception as e:
                    logger.exception(f"Could not add role from reaction: {e}")
                    await handle_error(e)


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            "SELECT role_id FROM reaction_roles WHERE guild_id=? AND message_id=? AND emoji=?",
            (str(payload.guild_id), str(payload.message_id), str(payload.emoji))
        )
        row = await cur.fetchone()
        if row:
            guild = bot.get_guild(payload.guild_id)
            member = guild.get_member(payload.user_id)
            role = guild.get_role(int(row[0]))
            if member and role:
                try:
                    await member.remove_roles(role, reason="Reaction role removed")
                except Exception as e:
                    logger.exception(f"Could not remove role from reaction: {e}")
                    await handle_error(e)


@bot.event
async def on_message(message: discord.Message):
    global last_activity_time, revive_sent
    if message.author.bot:
        return
    if message.guild is None or not isinstance(message.author, discord.Member):
        await bot.process_commands(message)
        return
    if message.channel.id == REVIVE_CHANNEL_ID:
        last_activity_time = datetime.now(timezone.utc)
        revive_sent = False
    channel_id = str(message.channel.id)
    now = datetime.now(timezone.utc)
    if channel_id not in recent_message_history:
        recent_message_history[channel_id] = []
    recent_message_history[channel_id].append({"content": message.content, "timestamp": now})
    recent_message_history[channel_id] = recent_message_history[channel_id][-10:]
    await bot.process_commands(message)
    await helper_moderation.handle_moderation(message)
    words = re.findall(r"\b[a-zA-Z]{4,}\b", message.content.lower())
    for w in words:
        TOPIC_COUNTER[w] += 1
    HOURLY_ACTIVITY[datetime.now(timezone.utc).hour] += 1
    if bot.user in message.mentions:
        if not check_admin_or_role(message.author):
            await message.reply(tars_text("You need the Level 10 role to use me.", "error"))
            return
        if is_rate_limited(message.author.id):
            await message.reply(
                tars_text("You've reached your hourly message limit (10). Please wait before sending more.", "warning"))
            return
        if not FEATURE_FLAGS["ai_enabled"]:
            await message.reply(
                tars_text("AI systems are temporarily offline for stability. Please try again later.", "warning")
            )
            return
        record_message(message.author.id)
        async with message.channel.typing():
            last_message = message.content.strip()
            context_messages: list[str] = [
                entry["content"] for entry in recent_message_history[channel_id][:-1]
            ][-5:]
            reply = await tars_ai_respond(
                last_message,
                message.author.display_name,
                context_messages,
                user=message.author,
                channel_id=message.channel.id,
            )
            link_count = len(re.findall(r'https?://\S+', reply))
            if link_count > 0:
                logger.warning(f"Blocked AI response containing {link_count} links: {reply}")
                await message.reply(tars_text("That seems to contain links � I�m not authorized to share those."))
                return
            safe_reply = sanitize_discord_mentions(reply)
            safe_reply = strip_links(safe_reply)
            if await is_inappropriate(safe_reply):
                logger.warning(f"Blocked inappropriate response: {safe_reply}")
                await message.reply(tars_text("I can�t repeat that � let�s keep things respectful."))
            else:
                await message.reply(tars_text(safe_reply))


@tree.command(name="tars", description="T.A.R.S. command console and help")
@app_commands.describe(command="Optional command name for detailed help")
async def slash_tars(interaction: discord.Interaction, command: str | None = None):
    version = BOT_VERSION
    ai_status = "ONLINE" if FEATURE_FLAGS["ai_enabled"] else "OFFLINE"
    revive_status = "ONLINE" if FEATURE_FLAGS["revive_enabled"] else "OFFLINE"
    if command:
        await tars_command_help(interaction, command)
        return
    intro = tars_text(
        f"T.A.R.S. online. Systems nominal.\n"
        f"**Version:** `{version}`\n\n"
        f"**Subsystem Status**\n"
        f"� AI: `{ai_status}`\n"
        f"� Chat Revive: `{revive_status}`\n\n"
        "Use `/tars help <command>` for detailed command info.",
        "info"
    )
    embeds: list[discord.Embed] = []
    all_commands = {cmd.name: cmd for cmd in tree.get_commands()}
    for category, names in TARS_COMMAND_CATEGORIES.items():
        lines = []
        for name in sorted(names):
            cmd = all_commands.get(name)
            if not cmd:
                continue
            lines.append(f"/{cmd.name} � {cmd.description}")

        if lines:
            embeds.append(
                tars_embed(category, "\n".join(lines))
            )
    await interaction.response.send_message(
        intro,
        embeds=embeds,
        ephemeral=False
    )


@tree.command(name="userinfo", description="Get info about a user")
@app_commands.describe(member="Member to lookup (optional)")
async def slash_userinfo(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    embed = discord.Embed(title=f"User Info � {member}", color=0x00ffcc)
    embed.add_field(name="ID", value=member.id)
    embed.add_field(name="Joined",
                    value=member.joined_at.strftime("%Y-%m-%d %H:%M:%S") if member.joined_at else "Unknown")
    embed.add_field(name="Account Created", value=member.created_at.strftime("%Y-%m-%d %H:%M:%S"))
    embed.add_field(name="Top role", value=member.top_role.mention)
    embed.set_thumbnail(url=member.display_avatar.url)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="serverinfo", description="Get server info")
async def slash_serverinfo(interaction: discord.Interaction):
    g = interaction.guild
    embed = discord.Embed(title=f"Server Info � {g.name}", color=0x00ffcc)
    embed.add_field(name="ID", value=g.id)
    embed.add_field(name="Members", value=g.member_count)
    embed.add_field(name="Created", value=g.created_at.strftime("%Y-%m-%d"))
    if g.icon:
        embed.set_thumbnail(url=g.icon.url)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="roleinfo", description="Get info about a role")
@app_commands.describe(role="Role to lookup")
async def slash_roleinfo(interaction: discord.Interaction, role: discord.Role):
    embed = discord.Embed(title=f"Role Info � {role.name}", color=0x00ffcc)
    embed.add_field(name="ID", value=role.id)
    embed.add_field(name="Members with role", value=len(role.members))
    embed.add_field(name="Position", value=role.position)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="tarsreport", description="Report a user to staff")
@app_commands.describe(user="User to report", reason="Reason for report")
async def slash_report(interaction: discord.Interaction, user: discord.User, reason: str):
    safe_reason = sanitize_discord_mentions(reason)
    await interaction.response.send_message(tars_text("Your report has been submitted to staff."), ephemeral=True)
    await helper_moderation.send_mod_log(interaction.guild,
                                         f"**Report**\nReporter: {interaction.user} ({interaction.user.id})\nReported: {user} ({user.id})\nReason: {safe_reason}\nChannel: {interaction.channel.mention}",
                                         ping_staff=False)


@tree.command(name="quote", description="Quote a message by link or ID")
@app_commands.describe(message_link="Message link or ID")
async def slash_quote(interaction: discord.Interaction, message_link: str):
    try:
        if "discord.com/channels" in message_link:
            parts = message_link.split("/")
            if len(parts) < 3:
                raise ValueError("Invalid message link format.")
            guild_id = int(parts[-3])
            channel_id = int(parts[-2])
            message_id = int(parts[-1])
            if interaction.guild is None or guild_id != interaction.guild.id:
                await interaction.response.send_message(
                    tars_text("That message is not from this server.", "error"),
                    ephemeral=True
                )
                return
            ch = bot.get_channel(channel_id)
            if not ch or ch.guild.id != interaction.guild.id:
                await interaction.response.send_message(
                    tars_text("Channel not found in this server.", "error"),
                    ephemeral=True
                )
                return
            perms = ch.permissions_for(interaction.user)
            if not (perms.view_channel and perms.read_message_history):
                await interaction.response.send_message(
                    tars_text("You don't have permission to view that channel.", "error"),
                    ephemeral=True
                )
                return
            msg = await ch.fetch_message(message_id)
        else:
            message_id = int(message_link)
            msg = await interaction.channel.fetch_message(message_id)
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute("INSERT INTO quotes(guild_id,message_id,author,content,saved_by,time) VALUES(?,?,?,?,?,?)",
                             (str(interaction.guild.id), str(msg.id), str(msg.author), msg.content[:1800],
                              str(interaction.user), datetime.now(timezone.utc).isoformat()))
            await db.commit()
        safe_content = sanitize_discord_mentions(msg.content)
        embed = tars_embed("Quoted Message", f"**{msg.author}** in {msg.channel.mention}:\n{safe_content}")
        await interaction.response.send_message(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none()
        )
    except Exception as e:
        await interaction.response.send_message(tars_text("Unable to find message or permission denied."),
                                                ephemeral=True)
        logger.exception(f"Quote error: {e}")
        await handle_error(e)


@tree.command(name="remindme", description="Set a reminder: e.g. /remindme 10m Check the logs")
@app_commands.describe(delay="Delay like 10m, 2h, 1d", text="Reminder text")
async def slash_remindme(interaction: discord.Interaction, delay: str, text: str):
    match = re.match(r"(\d+)([smhd])", delay)
    if not match:
        await interaction.response.send_message(
            tars_text("Invalid input format � please use a format like 10m, 2h, or 1d.", "error"), ephemeral=True)
        return
    num, unit = int(match.group(1)), match.group(2)
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    seconds = num * multipliers[unit]
    remind_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    if remind_at <= datetime.now(timezone.utc):
        await interaction.response.send_message(
            tars_text("That time is in the past. Sadly time travel does not work here."), ephemeral=True)
        return
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("INSERT INTO reminders(user_id, channel_id, remind_at, content) VALUES(?,?,?,?)",
                         (str(interaction.user.id), str(interaction.channel.id), remind_at.isoformat(), text))
        await db.commit()
    scheduler.add_job(send_reminder, 'date', run_date=remind_at,
                      args=[interaction.user.id, interaction.channel.id, text])
    await interaction.response.send_message(
        tars_text(f"Reminder set. I�ll alert you precisely on schedule in about {delay}.", "success"), ephemeral=True)


async def send_reminder(user_id, channel_id, text):
    ch = bot.get_channel(int(channel_id))
    user = bot.get_user(int(user_id))
    safe_text = sanitize_discord_mentions(text)
    if ch:
        if user:
            await ch.send(
                f"{user.mention} Reminder: {safe_text}",
                allowed_mentions=discord.AllowedMentions(users=[user], roles=False, everyone=False)
            )
        else:
            await ch.send(
                f"Reminder: {safe_text}",
                allowed_mentions=discord.AllowedMentions.none()
            )
    else:
        try:
            if user:
                await user.send(f"Reminder: {safe_text}")
        except Exception as e:
            logger.info(f"Unable to send reminder to {user}: " + str(e))
            await handle_error(e)


async def check_chat_revive():
    global last_activity_time, revive_sent
    channel = bot.get_channel(REVIVE_CHANNEL_ID)
    if not channel:
        logger.error("Revive channel not found.")
        return
    time_style = get_time_of_day()
    season = get_season()
    style_prompt = REVIVE_STYLE_PROMPTS.get(time_style, "")
    season_prompt = SEASONAL_PROMPTS.get(season, "")
    channel_theme = CHANNEL_THEMES.get(channel.id, "Open-ended discussion starter")
    role = channel.guild.get_role(REVIVE_ROLE_ID)
    if not role:
        logger.error("Revive role not found.")
        return
    if not FEATURE_FLAGS["revive_enabled"]:
        return
    if last_activity_time is None:
        try:
            async for msg in channel.history(limit=1):
                last_activity_time = msg.created_at
                break
        except Exception as e:
            logger.error(f"History read failed: {e}")
            return
        if last_activity_time is None:
            last_activity_time = datetime.now(timezone.utc)
    inactivity = (datetime.now(timezone.utc) - last_activity_time).total_seconds()
    # Use effective base interval (min 24h) and slow down during quiet hours
    base_interval = await get_effective_revive_interval()
    quiet, multiplier = await is_quiet_now()
    dynamic_interval = base_interval * multiplier
    if inactivity < dynamic_interval or revive_sent:
        return
    logger.info(
        f"[Revive Check] inactivity={inactivity:.0f}s revive_sent={revive_sent} "
        f"quiet={quiet} multiplier={multiplier} interval={dynamic_interval:.0f}s"
    )
    recent_questions = await get_recent_revives()
    avoid_text = ""
    if recent_questions:
        avoid_text = (
                "Avoid reusing or closely paraphrasing these recent questions:\n"
                + "\n".join(f"- {q}" for q in recent_questions)
        )
    prompt = (
        f"{style_prompt}\n"
        f"{season_prompt}\n"
        f"Theme: {channel_theme}\n\n"
        "Generate ONE single-sentence question.\n"
        "No emojis. No lists. No meta commentary.\n"
        "Be natural, engaging, and non-repetitive.\n\n"
        f"{avoid_text}"
    )
    question = await tars_ai_respond(prompt, "T.A.R.S.")
    global LAST_REVIVE_TIME
    LAST_REVIVE_TIME = datetime.now(timezone.utc)
    await save_revive_question(question)
    safe_question = sanitize_discord_mentions(question)
    await channel.send(f"{role.mention} � {safe_question}")
    revive_sent = True
    logger.info("Chat revive sent successfully.")


@tree.command(name="reactionrole", description="Create a reaction role (admin only)")
@app_commands.describe(message_id="ID of message to attach", emoji="Emoji", role="Role to give")
async def slash_reactionrole(interaction: discord.Interaction, message_id: str, emoji: str, role: discord.Role):
    if not interaction.user.guild_permissions.manage_roles:
        await interaction.response.send_message(tars_text("Access denied: insufficient clearance.", "error"),
                                                ephemeral=True)
        return
    bot_member = interaction.guild.me if interaction.guild else None
    if not bot_member:
        await interaction.response.send_message(
            tars_text("Cannot verify bot role permissions right now.", "error"),
            ephemeral=True
        )
        return
    if role.managed or role.is_default():
        await interaction.response.send_message(
            tars_text("That role cannot be self-assigned.", "error"),
            ephemeral=True
        )
        return
    if role.permissions.administrator:
        await interaction.response.send_message(
            tars_text("Administrator roles cannot be reaction-assigned.", "error"),
            ephemeral=True
        )
        return
    if role >= bot_member.top_role:
        await interaction.response.send_message(
            tars_text("I can't manage that role due to role hierarchy.", "error"),
            ephemeral=True
        )
        return
    try:
        msg = await interaction.channel.fetch_message(int(message_id))
    except Exception as e:
        await interaction.response.send_message(tars_text(f"{e}, Unable to find message.", "error"), ephemeral=True)
        await interaction.response.send_message(
            tars_text("Message not found in this channel.", "error"),
            ephemeral=True
        )
        return
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO reaction_roles(guild_id,message_id,emoji,role_id) VALUES(?,?,?,?)",
            (str(interaction.guild_id), str(message_id), str(emoji), str(role.id))
        )
        await db.commit()
    try:
        await msg.add_reaction(emoji)
    except Exception as e:
        logger.info("Failed to add reaction: " + str(e))
        await handle_error(e)
    await interaction.response.send_message(tars_text("Reaction role configured."), ephemeral=True)


@tree.command(name="8ball", description="Ask the magic 8-ball")
@app_commands.describe(question="Your question")
async def slash_8ball(interaction: discord.Interaction, question: str):
    question = sanitize_discord_mentions(question)
    answer = await tars_ai_respond(question, "Magic 8-ball")
    await interaction.response.send_message(answer, ephemeral=True)


@tree.command(name="dice", description="Roll a dice like 1d6 or 2d10")
@app_commands.describe(spec="e.g. 1d6 or 2d10")
async def slash_dice(interaction: discord.Interaction, spec: str):
    m = re.match(r"(\d+)d(\d+)", spec)
    if not m:
        await interaction.response.send_message(tars_text("Invalid format. Use NdM, e.g., 2d6."), ephemeral=True)
        return
    n, sides = int(m.group(1)), int(m.group(2))
    if n > 20:
        await interaction.response.send_message(tars_text("Too many dice."), ephemeral=True)
        return
    rolls = [random.randint(1, sides) for _ in range(n)]
    await interaction.response.send_message(tars_text(f"Rolled: {rolls} � total {sum(rolls)}"))


@tree.command(name="getquote", description="Retrieve a saved quote by ID (staff)")
@app_commands.describe(qid="Quote ID")
async def slash_getquote(interaction: discord.Interaction, qid: int):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(tars_text("You lack permission."), ephemeral=True)
        return
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT id,author,content,saved_by,time FROM quotes WHERE id=?", (qid,))
        row = await cur.fetchone()
        if not row:
            await interaction.response.send_message(tars_text("Quote not found."), ephemeral=True)
            return
        safe_quote = sanitize_discord_mentions(row[2])
        embed = tars_embed(
            f"Quote #{row[0]}",
            f"By {row[1]} � saved by {row[3]} at {row[4]}\n\n{safe_quote}"
        )
        await interaction.response.send_message(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none()
        )


async def handle_error(e: Exception):
    logger.exception("Unhandled exception", exc_info=e)
    record_error()
    check_circuit_recovery()
    owner = bot.get_user(OWNER_ID)
    if owner:
        try:
            await owner.send("T.A.R.S. encountered an error. Circuit breaker status updated.")
        except Exception as e:
            logger.error("Failed to send error report: " + str(e))


@tree.command(name="setmotd", description="Set Message of the Day list and channel (owner)")
@app_commands.describe(channel="Channel for MOTD or empty to not set")
async def slash_setmotd(interaction: discord.Interaction, channel: discord.TextChannel = None):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message(tars_text("Owner only."), ephemeral=True)
        return
    await set_config("motd_channel_id", str(channel.id) if channel else None)
    await set_config("motd_list", MOTD_LIST)
    await interaction.response.send_message(tars_text("MOTD configuration updated.", "success"), ephemeral=True)


@tree.command(name="clean", description="Delete the last N messages (admin only)")
@app_commands.describe(amount="Number of messages to delete (max 200)")
async def slash_clean(interaction: discord.Interaction, amount: int):
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message(
            tars_text("Access denied: insufficient clearance.", "error"), ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=min(amount, 200))
    await interaction.followup.send(
        tars_text(f"Deleted {len(deleted)} messages.", "success"), ephemeral=True
    )


@tree.command(name="lock", description="Lock the current channel (admin only)")
async def slash_lock(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message(tars_text("You lack permission."), ephemeral=True)
        return
    await interaction.channel.set_permissions(interaction.guild.default_role, send_messages=False)
    await interaction.response.send_message(tars_text("Channel locked. Civilians contained."), ephemeral=False)


@tree.command(name="unlock", description="Unlock the current channel (admin only)")
async def slash_unlock(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message(tars_text("You lack permission."), ephemeral=True)
        return
    await interaction.channel.set_permissions(interaction.guild.default_role, send_messages=True)
    await interaction.response.send_message(tars_text("Channel unlocked. Back to normal operations."),
                                            ephemeral=False)


@tree.command(name="slowmode", description="Set slowmode delay for this channel (seconds)")
@app_commands.describe(seconds="Delay between messages (0 to disable)")
async def slash_slowmode(interaction: discord.Interaction, seconds: int):
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message(tars_text("You lack permission."), ephemeral=True)
        return
    await interaction.channel.edit(slowmode_delay=seconds)
    msg = "Slowmode disabled." if seconds == 0 else f"Slowmode set to {seconds} seconds."
    await interaction.response.send_message(tars_text(msg, "info"))


@tree.command(name="addbannedword", description="Add a word to the auto-delete list (admin only)")
@app_commands.describe(word="Word to ban")
async def slash_add_banned(interaction: discord.Interaction, word: str):
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message(tars_text("You lack permission."), ephemeral=True)
        return
    banned = await get_config("banned_words", [])
    banned.append(word.lower())
    await set_config("banned_words", banned)
    await interaction.response.send_message(tars_text(f"Added '{word}' to banned words.", "success"), ephemeral=True)


@tree.command(name="listbannedwords", description="List banned words")
async def slash_list_banned(interaction: discord.Interaction):
    banned = await get_config("banned_words", [])
    await interaction.response.send_message(tars_text("Banned words: " + ", ".join(banned) if banned else "None."),
                                            ephemeral=True)


@tree.command(
    name="removebannedword",
    description="Remove a word from the auto-delete list (admin only)"
)
@app_commands.describe(word="Word to unban")
async def slash_remove_banned(interaction: discord.Interaction, word: str):
    if not interaction.user.guild_permissions.ban_members:
        await interaction.response.send_message(
            tars_text("You lack permission to modify banned words.", "error"),
            ephemeral=True
        )
        return

    word = word.lower().strip()
    banned = await get_config("banned_words", [])
    if word not in banned:
        await interaction.response.send_message(
            tars_text(f"'{word}' is not currently in the banned words list.", "warning"),
            ephemeral=True
        )
        return
    banned.remove(word)
    await set_config("banned_words", banned)
    await interaction.response.send_message(
        tars_text(f"Removed '{word}' from banned words.", "success"),
        ephemeral=True
    )
    await helper_moderation.send_mod_log(
        interaction.guild,
        f"Admin {interaction.user.mention} removed banned word: **{word}**",
        ping_staff=False
    )


@tree.command(name="ping", description="Check T.A.R.S. responsiveness")
async def slash_ping(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(tars_text(f"Systems nominal. Response time: {latency}ms"))


@tree.command(name="boostpoints", description="Check your current Boost Points balance")
async def slash_boostpoints(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    points = await get_boost_points(member.id)
    await interaction.response.send_message(
        tars_text(f"{member.display_name} currently has **{points} Boost Points**."), ephemeral=True
    )


@tree.command(name="boostshop", description="Redeem your Boost Points for rewards")
async def slash_boostshop(interaction: discord.Interaction):
    SHOP_ITEMS = {
        "custom_role": {"name": "Custom Role", "cost": 21,
                        "description": "Get a personalized role with color & name of your choice."},
        "giveaway_entry": {"name": "Extra Giveaway Entry", "cost": 10,
                           "description": "Gain an additional entry into giveaways."},
        "reduced_carry": {"name": "Reduced Carry Prices (10% less)", "cost": 4,
                          "description": "Carry prices are reduced by 10%."},
        "giveaway_bypass": {"name": "Giveaway fast pass", "cost": 8,
                            "description": "Skip requirements to enter giveaways."},
    }
    user_points = await get_boost_points(interaction.user.id)
    options = [
        discord.SelectOption(
            label=data["name"],
            description=f"{data['description']} ({data['cost']} pts)",
            value=key
        )
        for key, data in SHOP_ITEMS.items()
    ]
    select = Select(
        placeholder=f"You have {user_points} points � choose an item to redeem",
        options=options,
        min_values=1,
        max_values=1
    )

    async def select_callback(interaction_select: discord.Interaction):
        item_id = select.values[0]
        selected = SHOP_ITEMS[item_id]
        cost = selected["cost"]
        current_points = await get_boost_points(interaction.user.id)
        if current_points < cost:
            await interaction_select.response.send_message(
                tars_text(f"Insufficient points: you need {cost}, but you only have {current_points}.", "error"),
                ephemeral=True
            )
            return
        success = await spend_boost_points(interaction.user.id, cost)
        if not success:
            await interaction_select.response.send_message(
                tars_text("Transaction failed � please try again later.", "error"),
                ephemeral=True
            )
            return
        guild = interaction.guild
        ticket_category = discord.utils.get(guild.categories, name="Boost Tickets")
        if not ticket_category:
            ticket_category = await guild.create_category("Boost Tickets")
        channel_name = f"boost-{interaction.user.name.lower()}-{random.randint(1000, 9999)}"
        ticket_channel = await guild.create_text_channel(channel_name, category=ticket_category)
        await ticket_channel.set_permissions(interaction.user, view_channel=True, send_messages=True)
        await ticket_channel.set_permissions(guild.default_role, view_channel=False)
        discord_embed = tars_embed(
            "Boost Reward Ticket Opened",
            f"**Item Redeemed:** {selected['name']}\n**Cost:** {cost} Points\n**Remaining Points:** {current_points - cost}\n\nA staff member will assist you shortly.",
        )
        await ticket_channel.send(f"{interaction.user.mention} has opened a Boost Ticket!", embed=discord_embed)
        await interaction_select.response.send_message(
            tars_text(f"Purchase successful! Your ticket has been created in {ticket_channel.mention}.", "success"),
            ephemeral=True
        )

    select.callback = select_callback
    view = View(timeout=120)
    view.add_item(select)
    embed = discord.Embed(title="T.A.R.S. Boost Shop", color=0x00ffcc)
    embed.description = (
        "Exchange your **Boost Points** for exclusive rewards.\n\n"
        "Select an item from the menu below to purchase."
    )
    for key, data in SHOP_ITEMS.items():
        embed.add_field(
            name=f"{data['name']} � {data['cost']} Points",
            value=data["description"],
            inline=False
        )
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


@tree.command(name="close", description="Close the current Boost Ticket channel")
async def slash_close(interaction: discord.Interaction):
    channel = interaction.channel
    if not channel.name.startswith("boost-"):
        await interaction.response.send_message(
            tars_text("This command can only be used inside a Boost Ticket channel.", "error"),
            ephemeral=True
        )
        return
    is_staff = interaction.user.guild_permissions.manage_channels
    async for message in channel.history(limit=20, oldest_first=True):
        ticket_opener = None
        if message.mentions:
            ticket_opener = message.mentions[0]
        if ticket_opener and (interaction.user == ticket_opener or is_staff):
            break
    else:
        if not is_staff:
            await interaction.response.send_message(
                tars_text("You don�t have permission to close this ticket.", "error"),
                ephemeral=True
            )
            return
    await interaction.response.send_message(
        tars_text("Closing ticket... Stand by."), ephemeral=True
    )
    await channel.send(
        embed=tars_embed(
            "Ticket Closed",
            f"This ticket was closed by {interaction.user.mention}.\n\n"
            "Thank you for using the **Boost Shop**!",
        )
    )
    await asyncio.sleep(5)
    await channel.delete(reason=f"Ticket closed by {interaction.user}")


@tree.command(name="boostpoints_add", description="Add Boost Points to a user (admin only)")
@app_commands.describe(member="Member to add points to", amount="Number of points to add")
async def slash_boostpoints_add(interaction: discord.Interaction, member: discord.Member, amount: int):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            tars_text("Access denied: insufficient clearance.", "error"), ephemeral=True
        )
        return
    if amount <= 0:
        await interaction.response.send_message(
            tars_text("Amount must be positive.", "error"), ephemeral=True
        )
        return

    await add_boost_points(member.id, amount)
    points = await get_boost_points(member.id)
    await interaction.response.send_message(
        tars_text(f"Added **{amount} Boost Points** to {member.display_name}. New balance: **{points}**.", "success"),
        ephemeral=False
    )
    await helper_moderation.send_mod_log(
        interaction.guild,
        f"Admin {interaction.user.mention} added **{amount} Boost Points** to {member.mention}. New total: {points}.",
        ping_staff=False
    )


@tree.command(name="boostpoints_remove", description="Remove Boost Points from a user (admin only)")
@app_commands.describe(member="Member to remove points from", amount="Number of points to remove")
async def slash_boostpoints_remove(interaction: discord.Interaction, member: discord.Member, amount: int):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            tars_text("Access denied: insufficient clearance.", "error"), ephemeral=True
        )
        return
    if amount <= 0:
        await interaction.response.send_message(
            tars_text("Amount must be positive.", "error"), ephemeral=True
        )
        return

    current = await get_boost_points(member.id)
    new_amount = max(0, current - amount)
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("UPDATE boost_points SET points = ? WHERE user_id = ?", (new_amount, str(member.id)))
        await db.execute("INSERT INTO boost_log (user_id, action, points, time) VALUES (?, ?, ?, ?)",
                         (str(member.id), "admin_remove", -amount, datetime.now(timezone.utc).isoformat()))
        await db.commit()

    await interaction.response.send_message(
        tars_text(f"Removed **{amount} Boost Points** from {member.display_name}. New balance: **{new_amount}**.",
                  "warning"),
        ephemeral=False
    )
    await helper_moderation.send_mod_log(
        interaction.guild,
        f"Admin {interaction.user.mention} removed **{amount} Boost Points** from {member.mention}. New total: {new_amount}."
    )


@tree.command(name="status", description="Show T.A.R.S. system health")
async def slash_status(interaction: discord.Interaction):
    uptime = datetime.now(timezone.utc) - BOT_START_TIME
    latency_ms = round(bot.latency * 1000)
    scheduler_running = scheduler.running
    openai_ok = await check_openai_health()
    db_ok = await check_db_health()
    revive_time = (
        LAST_REVIVE_TIME.isoformat(timespec="seconds")
        if LAST_REVIVE_TIME else "Never"
    )
    error_time = (
        LAST_ERROR_TIME.isoformat(timespec="seconds")
        if LAST_ERROR_TIME else "None"
    )
    embed = discord.Embed(
        title="T.A.R.S. System Status",
        color=0x00ffcc
    )
    embed.add_field(name="Uptime", value=str(uptime).split(".")[0], inline=False)
    embed.add_field(name="WebSocket Latency", value=f"{latency_ms} ms", inline=True)
    embed.add_field(name="Scheduler Running", value=str(scheduler_running), inline=True)
    embed.add_field(
        name="OpenAI API",
        value="Operational" if openai_ok else "Unavailable",
        inline=True
    )
    embed.add_field(
        name="Database",
        value="Operational" if db_ok else "Unavailable",
        inline=True
    )
    embed.add_field(name="AI Enabled", value=str(FEATURE_FLAGS["ai_enabled"]), inline=True)
    embed.add_field(name="Revive Enabled", value=str(FEATURE_FLAGS["revive_enabled"]), inline=True)
    embed.add_field(name="Last Revive Sent", value=revive_time, inline=False)
    embed.add_field(name="Last Error", value=error_time, inline=False)
    embed.set_footer(text="� T.A.R.S. Diagnostics")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="config_view", description="View live configuration (owner)")
async def slash_config_view(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message(tars_text("Owner only."), ephemeral=True)
        return
    rows = []
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT key, value FROM config")
        rows = await cur.fetchall()
    text = "\n".join(f"� {k}: {v}" for k, v in rows) or "No config set."
    await interaction.response.send_message(
        embed=tars_embed("Live Configuration", text),
        ephemeral=True
    )


@tree.command(name="ai_stats", description="View AI usage metrics (moderator)")
async def slash_ai_stats(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.ban_members:
        await interaction.response.send_message(tars_text("Insufficient clearance."), ephemeral=True)
        return
    top_users = sorted(
        AI_USAGE["by_user"].items(),
        key=lambda x: x[1],
        reverse=True
    )[:5]
    lines = [
        f"Total Tokens Used: {AI_USAGE['tokens']}",
        f"Failures: {AI_USAGE['failures']}",
        "",
        "**Top Users:**"
    ]
    lines.extend(f"- {uid}: {count}" for uid, count in top_users)
    await interaction.response.send_message(
        embed=tars_embed("AI Usage Metrics", "\n".join(lines)),
        ephemeral=True
    )


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
