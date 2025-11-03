import asyncio
import json

import discord
import random
import re
import aiosqlite
from datetime import datetime, timedelta
import logging
from tars import tars_text
from config import STAFF_ROLES_FOR_PING, recent_messages, recent_message_timestamps, \
    NWORD_PATTERN, SUICIDE_PATTERNS, DRUG_KEYWORDS, DB_FILE

logger = logging.getLogger("tars")


async def handle_moderation(message):
    if message.author.bot:
        return
    if any(role.name in STAFF_ROLES_FOR_PING for role in message.author.roles):
        return
    text = message.content or ""
    banned_words = await get_banned_words()
    for word in banned_words:
        if re.search(rf"\b{re.escape(word)}\b", text, re.IGNORECASE):
            count = await get_warnings(str(message.author.id))
            count += 1
            await set_warnings(str(message.author.id), count)
            await add_warn_log(str(message.author.id), f"Use of banned word: {word}")
            try:
                await message.delete()
            except Exception as e:
                logger.warning(f"Failed to delete banned word message: {e}")
            await message.channel.send(
                tars_text(f"{message.author.mention}, watch your language. [Warning {count}/3]"))
            await dm_send_safe(
                message.author,
                f"T.A.R.S. Warning {count}/3: Use of banned word (‘{word}’). Message deleted."
            )
            await send_mod_log(
                f"Banned word '{word}' used by {message.author} in {message.channel.mention}. Warnings {count}."
            )
            return
    g = message.guild
    if len(text.splitlines()) > 10 or len(text) > 500:
        count = await get_warnings(str(message.author.id))
        count += 1
        await set_warnings(str(message.author.id), count)
        await add_warn_log(str(message.author.id), "Text wall / spam")
        await message.channel.send(
            tars_text(f"{message.author.mention}, sending large text walls or spam is prohibited. [Warning {count}/3]"))
        await dm_send_safe(message.author, f"T.A.R.S. Warning {count}/3: Text wall or spam detected.")
        await send_mod_log(g,
                           f"Text wall by {message.author} in {message.channel.mention}: {len(text)} chars / {len(text.splitlines())} lines. Warnings {count}")
        return
    ping_count = 0
    for role in g.roles:
        if role.name in STAFF_ROLES_FOR_PING and role.mention in text:
            ping_count += text.count(role.mention)
    if ping_count > 4:
        count = await get_warnings(str(message.author.id))
        count += 1
        await set_warnings(str(message.author.id), count)
        await add_warn_log(str(message.author.id), "Excessive staff pinging")
        await message.channel.send(
            tars_text(f"{message.author.mention}, excessive staff pinging is not allowed. [Warning {count}/3]"))
        await dm_send_safe(message.author, f"T.A.R.S. Warning {count}/3: Excessive staff pinging.")
        await send_mod_log(g,
                           f"Excessive staff pings by {message.author} in {message.channel.mention}. Count: {ping_count}. Warnings {count}")
        return
    uid = str(message.author.id)
    now = datetime.utcnow()
    msg_list = recent_messages.get(uid, [])
    ts_list = recent_message_timestamps.get(uid, [])
    msg_list.append(text)
    ts_list.append(now)
    recent_messages[uid] = msg_list[-6:]
    recent_message_timestamps[uid] = ts_list[-6:]
    if len(set(recent_messages[uid])) == 1 and len(recent_messages[uid]) >= 3:
        count = await get_warnings(uid)
        count += 1
        await set_warnings(uid, count)
        await add_warn_log(uid, "Repeated message spam")
        await message.channel.send(
            tars_text(f"{message.author.mention}, repeated messages detected. [Warning {count}/3]"))
        await dm_send_safe(message.author, f"T.A.R.S. Warning {count}/3: Repeated message spam.")
        await send_mod_log(g, f"Repeated messages by {message.author} in {message.channel.mention}. Warnings {count}")
        return

    links = re.findall(r"https?://\S+", text)
    if len(links) > 2:
        count = await get_warnings(uid)
        count += 1
        await set_warnings(uid, count)
        await add_warn_log(uid, "Link spam")
        await message.channel.send(
            tars_text(f"{message.author.mention}, excessive links detected. [Warning {count}/3]"))
        await dm_send_safe(message.author, f"T.A.R.S. Warning {count}/3: Excessive link posting.")
        await send_mod_log(g,
                           f"Link spam by {message.author} in {message.channel.mention}. Links: {len(links)}. Warnings {count}")
        return

    if NWORD_PATTERN.search(text):
        reaction = random.choice(
            ["Interesting choice of words… not recommended.", "Attempting human chaos detected. Deleting."])
        count = await helper_warn(message, reaction, uid)
        await add_warn_log(uid, "Prohibited slur")
        await dm_send_safe(message.author, f"T.A.R.S. Warning {count}/3: Use of prohibited slur.")
        await send_mod_log(g,
                           f"Banned word by {message.author} in {message.channel.mention}: \"{text}\". Warnings {count}")
        return

    for pat in SUICIDE_PATTERNS:
        if pat.search(text):
            reaction = random.choice(["Protocol violation. That’s a negative.",
                                      "Error detected: inappropriate content. Executing deletion."])
            count = await helper_warn(message, reaction, uid)
            await add_warn_log(uid, "Self-harm encouragement")
            await dm_send_safe(message.author,
                               f"T.A.R.S. Warning {count}/3: Promoting self-harm is prohibited. If you are in danger, contact emergency services or the suicide hotline.")
            await send_mod_log(g,
                               f"Self-harm phrase by {message.author} in {message.channel.mention}. Warnings {count}. Message: \"{text}\"")
            return

    if any(w in text for w in DRUG_KEYWORDS):
        count = await get_warnings(uid)
        count += 1
        await set_warnings(uid, count)
        await add_warn_log(uid, "Drug mention")
        await message.channel.send(
            tars_text(f"{message.author.mention}, discussion of drugs is prohibited. [Warning {count}/3]"))
        await dm_send_safe(message.author, f"T.A.R.S. Warning {count}/3: Discussion of drugs prohibited.")
        await send_mod_log(g, f"Drug mention by {message.author} in {message.channel.mention}. Warnings {count}.")
        return
    current_warnings = await get_warnings(uid)
    if current_warnings >= 3:
        try:
            await message.author.timeout(discord.utils.utcnow() + timedelta(minutes=10),
                                         reason="T.A.R.S. automated enforcement")
            await send_mod_log(g, f"{message.author} was timed out for 10 minutes (3 warnings).")
            await set_warnings(uid, 0)
        except Exception as e:
            await send_mod_log(g, f"Failed to timeout {message.author}: " + str(e))
        return


async def get_banned_words():
    """Fetch the banned word list from config (SQLite)."""
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT value FROM config WHERE key = 'banned_words'")
        row = await cur.fetchone()
        if not row:
            return []
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return []


async def get_warnings(user_id: str) -> int:
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT count FROM warnings WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        return row[0] if row else 0


async def set_warnings(user_id: str, count: int):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("INSERT OR REPLACE INTO warnings(user_id, count) VALUES(?,?)", (user_id, count))
        await db.commit()


async def add_warn_log(user_id: str, reason: str, moderator: str = "T.A.R.S."):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("INSERT INTO warns_log(user_id, reason, time, moderator) VALUES(?,?,?,?)",
                         (user_id, reason, datetime.utcnow().isoformat(), moderator))
        await db.commit()


async def dm_send_safe(user: discord.User, text: str):
    try:
        await user.send(tars_text(text))
    except Exception as e:
        logger.info(f"Could not DM user {user}: " + str(e))


async def helper_warn(message, reaction, uid):
    await message.channel.send(tars_text(reaction))
    await asyncio.sleep(0.4)
    try:
        await message.delete()
    except Exception as e:
        e.args = (f"{e.args[0]} (while deleting message)",)
        logger.exception("Could not delete message: " + str(e))
    count = await get_warnings(uid)
    count += 1
    await set_warnings(uid, count)
    return count


async def dm_send_safe(user: discord.User, text: str):
    try:
        await user.send(tars_text(text))
    except Exception as e:
        logger.info(f"Could not DM user {user}: " + str(e))


async def send_mod_log(guild: discord.Guild, message: str):
    log_channel = discord.utils.get(guild.text_channels, name="╰-︰🤖tars-logs")
    if not log_channel:
        if not guild.me.guild_permissions.manage_channels:
            logger.warning("Bot lacks permission to create log channel.")
            return
        try:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                guild.me: discord.PermissionOverwrite(view_channel=True)
            }
            log_channel = await guild.create_text_channel("tars-logs", overwrites=overwrites)
        except Exception as e:
            logger.exception(f"Could not create log channel: {e}")
            return
    mentions = []
    for rname in ("Moderator", "Admin", "Senior Admin", "Head Admin", "Co Owner", "Owner"):
        r = discord.utils.get(guild.roles, name=rname)
        if r:
            mentions.append(r.mention)
    mention_text = " ".join(mentions) + ("\n" if mentions else "")
    await log_channel.send(f"{mention_text}{message}")
