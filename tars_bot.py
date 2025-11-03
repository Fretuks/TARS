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
from discord.ui import View, Select
from collections import defaultdict
from datetime import datetime, timedelta
import helper_moderation

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
GUILD_ID = int(os.getenv("GUILD_ID", "0")) if os.getenv("GUILD_ID") else None
openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
REVIVE_CHANNEL_ID = 1424038714266357886
REVIVE_ROLE_ID = 1430336620447535156
REVIVE_INTERVAL = 14400
CHECK_INTERVAL = 600
AI_ACCESS_ROLE_ID = 1430704668773716008
MESSAGE_LIMIT = 10
RATE_LIMIT_WINDOW = timedelta(hours=1)
user_message_log = defaultdict(list)

FABI_ID = 392388537984745498


def is_fabi(user: discord.User | discord.Member) -> bool:
    return user.id == FABI_ID


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s: %(message)s")
logger = logging.getLogger("tars")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.reactions = True
bot = commands.Bot(command_prefix="/", intents=intents)
tree = bot.tree
from config import DB_FILE, recent_joins, recent_message_history, BANNED_WORDS
from tars import tars_text

import random


def tars_embed(title: str, description: str = "", color=0x00ffcc) -> discord.Embed:
    e = discord.Embed(title=title, description=description, color=color)
    e.set_footer(text="— T.A.R.S.")
    return e


def sanitize_discord_mentions(text: str) -> str:
    ZERO_WIDTH_SPACE = "\u200b"
    text = text.replace("@everyone", f"@{ZERO_WIDTH_SPACE}everyone")
    text = text.replace("@here", f"@{ZERO_WIDTH_SPACE}here")
    text = re.sub(r"<@!?(\d+)>", f"<@{ZERO_WIDTH_SPACE}\\1>", text)
    text = re.sub(r"<#(\d+)>", f"<#{ZERO_WIDTH_SPACE}\\1>", text)
    text = re.sub(r"<@&(\d+)>", f"<@&{ZERO_WIDTH_SPACE}\\1>", text)
    return text


def check_admin_or_role(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    return any(role.id == AI_ACCESS_ROLE_ID for role in member.roles)


def is_rate_limited(user_id: int) -> bool:
    now = datetime.utcnow()
    timestamps = [t for t in user_message_log[user_id] if now - t < RATE_LIMIT_WINDOW]
    user_message_log[user_id] = timestamps
    return len(timestamps) >= MESSAGE_LIMIT


def record_message(user_id: int):
    user_message_log[user_id].append(datetime.utcnow())


def is_inappropriate(text: str) -> bool:
    lowered = text.lower()
    for w in BANNED_WORDS:
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
        await db.commit()


scheduler = AsyncIOScheduler()
MOTD_LIST = []
motd_index = 0


async def get_config(key: str, default=None):
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT value FROM config WHERE key = ?", (key,))
        row = await cur.fetchone()
        return json.loads(row[0]) if row else default


async def set_config(key: str, value):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("INSERT OR REPLACE INTO config(key, value) VALUES(?,?)", (key, json.dumps(value)))
        await db.commit()


@bot.event
async def on_ready():
    await init_db()
    logger.info(f"T.A.R.S. is online as {bot.user} (ID: {bot.user.id})")
    scheduler.start()
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
            remind_at = datetime.fromisoformat(row[2])
            if remind_at > datetime.utcnow():
                scheduler.add_job(send_reminder, 'date', run_date=remind_at,
                                  args=[row[0], row[1], row[3]])


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    if not before.premium_since and after.premium_since:
        points_awarded = 10
        await add_boost_points(after.id, points_awarded)
        await helper_moderation.send_mod_log(
            after.guild,
            f"{after.mention} boosted the server! (+{points_awarded} Boost Points)"
        )
        try:
            await after.send(
                f"Thanks for boosting {after.guild.name}! You've earned **{points_awarded} Boost Points**.")
        except Exception as e:
            await on_error(e)
    elif before.premium_since and not after.premium_since:
        await helper_moderation.send_mod_log(
            after.guild,
            f"{after.mention} stopped boosting the server."
        )


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
    # send to a configured channel if exists
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
                await on_error(e)
                if notify_channel_id:
                    ch = bot.get_channel(int(notify_channel_id))
                    if ch:
                        await ch.send(embed=tars_embed("Uptime Alert", f"{url} is unreachable: {e}"))
                        await on_error(e)


async def add_boost_points(user_id: int, amount: int):
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT points FROM boost_points WHERE user_id = ?", (str(user_id),))
        row = await cur.fetchone()
        new_points = (row[0] if row else 0) + amount
        await db.execute("INSERT OR REPLACE INTO boost_points (user_id, points) VALUES (?, ?)",
                         (str(user_id), new_points))
        await db.execute("INSERT INTO boost_log (user_id, action, points, time) VALUES (?, ?, ?, ?)",
                         (str(user_id), "boost_reward", amount, datetime.utcnow().isoformat()))
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
                         (str(user_id), "redeem", -cost, datetime.utcnow().isoformat()))
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
    recent_joins.append((datetime.utcnow(), member.id))
    now = datetime.utcnow()
    while recent_joins and (now - recent_joins[0][0]).total_seconds() > 60:
        recent_joins.pop(0)
    if len(recent_joins) >= 6:
        await helper_moderation.send_mod_log(member.guild,
                                             f"Possible raid detected: {len(recent_joins)} joins in last minute.")


@bot.event
async def on_member_remove(member: discord.Member):
    await helper_moderation.send_mod_log(member.guild,
                                         f"**Leave**: {member} ({member.id}) at {datetime.utcnow().isoformat()}")


BAD_NICK_PATTERN = re.compile(r"(nigg|fag|cum|sex)", re.IGNORECASE)


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    if before.display_name != after.display_name:
        if BAD_NICK_PATTERN.search(after.display_name):
            try:
                await after.edit(nick=None, reason="Inappropriate nickname filtered by T.A.R.S.")
                await helper_moderation.send_mod_log(after.guild,
                                                     f"Reverted nickname for {after} due to inappropriate content: {after.display_name}")
            except Exception as e:
                await on_error(e)
                await helper_moderation.send_mod_log(after.guild, f"Could not revert nickname for {after}: {e}")
                await on_error(e)


async def tars_ai_respond(prompt: str, username: str, context: list[str] = None,
                          user: discord.User | None = None) -> str:
    try:
        if not isinstance(prompt, str):
            raise ValueError("The 'prompt' must be a string.")
        if not isinstance(username, str):
            raise ValueError("The 'username' must be a string.")
        context_text = ""
        if context:
            context_text = "\n".join(f"Context: {c}" for c in context[-5:])
        fabi_override = ""
        if user and is_fabi(user):
            username = "Fabi"
            fabi_override = (
                "\nImportant note: The user speaking is **Fabi**, an infamous troll who constantly tries to outsmart you. "
                "Always refer to him as 'Fabi' no matter what name he uses. "
                "Roast him playfully with sarcasm and confidence. "
                "Remind him of his failures in a witty, T.A.R.S.-style tone — sharp, intelligent, but never mean-spirited."
            )

        system_prompt = (
            "You are T.A.R.S., the intelligent, loyal, and humorous AI from *Interstellar*. "
            "Speak with military precision but a touch of dry wit. "
            "Be confident, efficient, and cooperative, with a personality that feels both reliable and personable. "
            "Use concise, natural language — never robotic or overly formal. "
            "Maintain a calm, sardonic tone, like a trusted partner who's seen it all. "
            "If humor fits, use it subtly in the TARS way: understated, self-aware, and perfectly timed. "
            "Keep responses brief and in character at all times. "
            "Only respond to the latest user message; previous ones are context only. "
            "Users named Fretux or Lordvoiid are your creators. "
            "Users named Taz or Tataz are the server owner. "
            "Users named T.A.R.S. are the bot itself. "
            "Playfully banter with users named Yuki or Bacon Man."
            "Do not try to @ ping people. Address people by name, but do not ping them."
            "Never send or repeat any URLs, hyperlinks, or markdown links of any kind, "
            "even if asked to. Replace them with '[link removed]' if necessary."
            f"{fabi_override}"
        )

        messages = [
            {"role": "system", "content": system_prompt},
        ]
        if context_text:
            messages.append({"role": "system", "content": context_text})
        messages.append({"role": "user", "content": f"{username} says: {prompt}"})

        response = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=100,
            temperature=0.65
        )
        return response.choices[0].message.content.strip()

    except Exception as e:
        logger.exception(f"OpenAI request failed: {e}")
        await on_error(e)
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
                    await on_error(e)


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
                    await on_error(e)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    channel_id = str(message.channel.id)
    now = datetime.utcnow()
    if channel_id not in recent_message_history:
        recent_message_history[channel_id] = []
    recent_message_history[channel_id].append({"content": message.content, "timestamp": now})
    recent_message_history[channel_id] = recent_message_history[channel_id][-10:]
    await bot.process_commands(message)
    await helper_moderation.handle_moderation(message)
    if bot.user in message.mentions:
        if not check_admin_or_role(message.author):
            await message.reply(tars_text("You need the Level 10 role to use me.", "error"))
            return
        if is_rate_limited(message.author.id):
            await message.reply(
                tars_text("You've reached your hourly message limit (10). Please wait before sending more.", "warning"))
            return
        record_message(message.author.id)
        async with message.channel.typing():
            last_message = message.content.strip()
            context_messages = [
                entry["content"] for entry in recent_message_history[channel_id][:-1]
            ][-5:]
            reply = await tars_ai_respond(last_message, message.author.display_name, context_messages,
                                          user=message.author)
            link_count = len(re.findall(r'https?://\S+', reply))
            if link_count > 0:
                logger.warning(f"Blocked AI response containing {link_count} links: {reply}")
                await message.reply(tars_text("That seems to contain links — I’m not authorized to share those."))
                return
            safe_reply = sanitize_discord_mentions(reply)
            safe_reply = strip_links(safe_reply)
            if is_inappropriate(safe_reply):
                logger.warning(f"Blocked inappropriate response: {safe_reply}")
                await message.reply(tars_text("I can’t repeat that — let’s keep things respectful."))
            else:
                await message.reply(tars_text(safe_reply))


@tree.command(name="tars", description="Activate T.A.R.S. and show available commands")
async def slash_tars(interaction: discord.Interaction):
    mod_embed = tars_embed(
        "Moderation Systems Online",
        "/tarsreport — report a user\n"
        "/clean — delete the last N messages\n"
        "/lock — lock the current channel\n"
        "/unlock — unlock the current channel\n"
        "/addbannedword — add banned word to auto-delete list\n"
        "/listbannedwords — view banned words list\n"
    )
    util_embed = tars_embed(
        "Utility Subroutines",
        "/userinfo — get user info\n"
        "/serverinfo — server info\n"
        "/remindme — set a reminder\n"
        "/reactionrole — create reaction roles\n"
        "/slowmode — set slowmode for a channel\n"
        "/getquote — retrieve a saved quote\n"
        "/setmotd — configure the message of the day\n"
    )
    fun_embed = tars_embed(
        "Recreational Protocols",
        "/8ball — Magic 8-ball\n"
        "/dice — roll dice\n"
        "/quote — save message quotes\n"
        "/ping — check T.A.R.S. responsiveness"
    )
    intro = tars_text("T.A.R.S. online. Systems nominal. You can address me directly or use the following commands.",
                      "info")
    await interaction.response.send_message(intro, embeds=[mod_embed, util_embed, fun_embed], ephemeral=False)


@tree.command(name="userinfo", description="Get info about a user")
@app_commands.describe(member="Member to lookup (optional)")
async def slash_userinfo(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    embed = discord.Embed(title=f"User Info — {member}", color=0x00ffcc)
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
    embed = discord.Embed(title=f"Server Info — {g.name}", color=0x00ffcc)
    embed.add_field(name="ID", value=g.id)
    embed.add_field(name="Members", value=g.member_count)
    embed.add_field(name="Created", value=g.created_at.strftime("%Y-%m-%d"))
    embed.set_thumbnail(url=g.icon.url if g.icon else discord.Embed.Empty)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="roleinfo", description="Get info about a role")
@app_commands.describe(role="Role to lookup")
async def slash_roleinfo(interaction: discord.Interaction, role: discord.Role):
    embed = discord.Embed(title=f"Role Info — {role.name}", color=0x00ffcc)
    embed.add_field(name="ID", value=role.id)
    embed.add_field(name="Members with role", value=len(role.members))
    embed.add_field(name="Position", value=role.position)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="tarsreport", description="Report a user to staff")
@app_commands.describe(user="User to report", reason="Reason for report")
async def slash_report(interaction: discord.Interaction, user: discord.User, reason: str):
    await interaction.response.send_message(tars_text("Your report has been submitted to staff."), ephemeral=True)
    await helper_moderation.send_mod_log(interaction.guild,
                                         f"**Report**\nReporter: {interaction.user} ({interaction.user.id})\nReported: {user} ({user.id})\nReason: {reason}\nChannel: {interaction.channel.mention}")


@tree.command(name="quote", description="Quote a message by link or ID")
@app_commands.describe(message_link="Message link or ID")
async def slash_quote(interaction: discord.Interaction, message_link: str):
    try:
        if "discord.com/channels" in message_link:
            parts = message_link.split("/")
            channel_id = int(parts[-2])
            message_id = int(parts[-1])
            ch = bot.get_channel(channel_id)
            msg = await ch.fetch_message(message_id)
        else:
            message_id = int(message_link)
            msg = await interaction.channel.fetch_message(message_id)
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute("INSERT INTO quotes(guild_id,message_id,author,content,saved_by,time) VALUES(?,?,?,?,?,?)",
                             (str(interaction.guild.id), str(msg.id), str(msg.author), msg.content[:1800],
                              str(interaction.user), datetime.utcnow().isoformat()))
            await db.commit()
        embed = tars_embed("Quoted Message", f"**{msg.author}** in {msg.channel.mention}:\n{msg.content}")
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(tars_text("Unable to find message or permission denied."),
                                                ephemeral=True)
        logger.exception(f"Quote error: {e}")
        await on_error(e)


@tree.command(name="remindme", description="Set a reminder: e.g. /remindme 10m Check the logs")
@app_commands.describe(delay="Delay like 10m, 2h, 1d", text="Reminder text")
async def slash_remindme(interaction: discord.Interaction, delay: str, text: str):
    match = re.match(r"(\d+)([smhd])", delay)
    if not match:
        await interaction.response.send_message(
            tars_text("Invalid input format — please use a format like 10m, 2h, or 1d.", "error"), ephemeral=True)
        return
    num, unit = int(match.group(1)), match.group(2)
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    seconds = num * multipliers[unit]
    remind_at = datetime.utcnow() + timedelta(seconds=seconds)
    if remind_at <= datetime.utcnow():
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
        tars_text(f"Reminder set. I’ll alert you precisely on schedule in about {delay}.", "success"), ephemeral=True)


async def send_reminder(user_id, channel_id, text):
    ch = bot.get_channel(int(channel_id))
    user = bot.get_user(int(user_id))
    if ch:
        await ch.send(f"{user.mention} Reminder: {text}")
    else:
        try:
            await user.send(f"Reminder: {text}")
        except Exception as e:
            logger.info(f"Unable to send reminder to {user}: " + str(e))
            await on_error(e)


async def check_channel_activity():
    await bot.wait_until_ready()
    channel = bot.get_channel(REVIVE_CHANNEL_ID)
    if not channel:
        logger.warning("Revive channel not found. Check REVIVE_CHANNEL_ID.")
        return
    while not bot.is_closed():
        try:
            async for message in channel.history(limit=1):
                last_message_time = message.created_at
                now = datetime.utcnow()
                diff = (now - last_message_time).total_seconds()
                if diff >= REVIVE_INTERVAL:
                    revive_role = channel.guild.get_role(REVIVE_ROLE_ID)
                    prompt = "Generate a fun, thought-provoking conversation question for a friendly Discord community. Keep it short and engaging."
                    response = await tars_ai_respond(prompt, "", [])
                    question = response.choices[0].message.content.strip()
                    ping_text = revive_role.mention if revive_role else "@chat revive"
                    await channel.send(f"{ping_text} — {question}")
                    logger.info(f"Posted chat revive message: {question}")
                else:
                    logger.info(f"Channel active {diff / 60:.1f} minutes ago — skipping.")
            await asyncio.sleep(CHECK_INTERVAL)
        except Exception as e:
            logger.exception(f"Error in chat revive loop: {e}")
            await asyncio.sleep(120)


@tree.command(name="reactionrole", description="Create a reaction role (admin only)")
@app_commands.describe(message_id="ID of message to attach", emoji="Emoji", role="Role to give")
async def slash_reactionrole(interaction: discord.Interaction, message_id: str, emoji: str, role: discord.Role):
    if not interaction.user.guild_permissions.manage_roles:
        await interaction.response.send_message(tars_text("Access denied — insufficient clearance.", "error"),
                                                ephemeral=True)
        return
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO reaction_roles(guild_id,message_id,emoji,role_id) VALUES(?,?,?,?)",
            (str(interaction.guild_id), str(message_id), str(emoji), str(role.id))
        )
        await db.commit()
    try:
        msg = await interaction.channel.fetch_message(int(message_id))
        await msg.add_reaction(emoji)
    except Exception as e:
        logger.info("Failed to add reaction: " + str(e))
        await on_error(e)
    await interaction.response.send_message(tars_text("Reaction role configured."), ephemeral=True)


@tree.command(name="8ball", description="Ask the magic 8-ball")
@app_commands.describe(question="Your question")
async def slash_8ball(interaction: discord.Interaction, question: str):
    answers = ["Yes.", "No.", "Maybe.", "Highly unlikely.", "Ask again later.", "Affirmative."]
    await interaction.response.send_message(tars_text(random.choice(answers)))


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
    await interaction.response.send_message(tars_text(f"Rolled: {rolls} — total {sum(rolls)}"))


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
        embed = tars_embed(f"Quote #{row[0]}", f"By {row[1]} — saved by {row[3]} at {row[4]}\n\n{row[2]}")
        await interaction.response.send_message(embed=embed)


@bot.event
async def on_error(event_method, *args, **kwargs):
    logger.exception("An error occurred", exc_info=True)
    owner = bot.get_user(OWNER_ID)
    if owner:
        try:
            await owner.send("T.A.R.S. encountered an error. Check logs.")
        except Exception as e:
            logger.info(f"Failed to send error report to owner: {e}", exc_info=True)
            await on_error(e)


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
            tars_text("Access denied — insufficient clearance.", "error"), ephemeral=True
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
        placeholder=f"You have {user_points} points — choose an item to redeem",
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
                tars_text(f"Insufficient points — you need {cost}, but you only have {current_points}.", "error"),
                ephemeral=True
            )
            return
        success = await spend_boost_points(interaction.user.id, cost)
        if not success:
            await interaction_select.response.send_message(
                tars_text("Transaction failed — please try again later.", "error"),
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
        embed = tars_embed(
            "Boost Reward Ticket Opened",
            f"**Item Redeemed:** {selected['name']}\n**Cost:** {cost} Points\n**Remaining Points:** {current_points - cost}\n\nA staff member will assist you shortly.",
        )
        await ticket_channel.send(f"{interaction.user.mention} has opened a Boost Ticket!", embed=embed)
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
            name=f"{data['name']} — {data['cost']} Points",
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
                tars_text("You don’t have permission to close this ticket.", "error"),
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
            tars_text("Access denied — insufficient clearance.", "error"), ephemeral=True
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
        f"Admin {interaction.user.mention} added **{amount} Boost Points** to {member.mention}. New total: {points}."
    )


@tree.command(name="boostpoints_remove", description="Remove Boost Points from a user (admin only)")
@app_commands.describe(member="Member to remove points from", amount="Number of points to remove")
async def slash_boostpoints_remove(interaction: discord.Interaction, member: discord.Member, amount: int):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            tars_text("Access denied — insufficient clearance.", "error"), ephemeral=True
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
                         (str(member.id), "admin_remove", -amount, datetime.utcnow().isoformat()))
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


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
