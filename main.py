"""
RoastBot — a Telegram bot whose entire job is roasting people 💀🔥

Add it to a group chat and it roasts members on command. DM it and it roasts
every message you send. Roasts are personalized: the bot quietly remembers the
last few things each person said and uses those as ammo. If it has nothing to
work with, it falls back to their name, then to a generic burn.

Reading order (top to bottom matches how the bot actually flows):
  1. Constants        — settings + in-memory state
  2. Database         — SQLite: remembered messages + roast leaderboard
  3. Hardcoded replies — match_keyword() using replies.py
  4. AI fallback chain — ask_ai(): DeepSeek -> Gemini -> Groq -> NVIDIA
  5. Roast prompts     — build the instructions we send to the AI
  6. Cooldown helper   — one roast per 15s per chat
  7. Commands          — /start /help /roast /roastme /battle /versus /leaderboard
  8. Message handler   — stores messages, runs battles, roasts on mention/DM
  9. Wiring            — register handlers + run_polling
"""

import asyncio
import random
import re
import sqlite3
import time
from datetime import datetime
from typing import Final, Optional

import requests
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from config import (
    TELEGRAM_TOKEN,
    DEEPSEEK_API_KEY,
    GEMINI_API_KEY,
    GROQ_API_KEY,
    NVIDIA_API_KEY,
)
from replies import KEYWORD_REPLIES, COOLDOWN_LINES, FALLBACK_ROASTS

# ---------- 1. Constants ----------
TOKEN: Final = TELEGRAM_TOKEN
# Fill this in after BotFather gives you the real @name (keep the @).
BOT_USERNAME: Final = "@YourRoastBot"
DB_PATH: Final = "roastbot.db"

MAX_STORED_MSGS: Final = 30   # how many recent messages we keep per person per chat
COOLDOWN_SECONDS: Final = 15  # one roast per chat per this many seconds
BATTLE_TIMEOUT: Final = 300   # a /battle auto-expires after 5 minutes of silence

if not TOKEN or "paste_your" in TOKEN:
    raise SystemExit("Set TELEGRAM_TOKEN in config.py (get it from @BotFather).")


def _key_ok(value: Optional[str]) -> bool:
    """True only if a config key was actually filled in (not blank/placeholder)."""
    return bool(value) and "paste_your" not in value


# In-memory state. Lost on restart, and that's fine — none of it is precious.
#   last_roast_at[chat_id]  -> unix timestamp of the last roast (for cooldown)
#   pending_battles[chat_id] -> a dict describing an in-progress /battle
last_roast_at: dict[int, float] = {}
pending_battles: dict[int, dict] = {}


# ---------- 2. Database ----------
def db() -> sqlite3.Connection:
    """Open a connection. One per call, closed by the caller — keeps things simple."""
    return sqlite3.connect(DB_PATH)


def init_db() -> None:
    """Create the tables if they don't exist. Safe to call every startup."""
    conn = db()
    # Everything is scoped by chat_id (like the todo bot): a private DM has its
    # own chat_id, and a group shares one chat_id — so 'this chat' is always the
    # right unit for both memory and the leaderboard.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id    INTEGER NOT NULL,
            user_id    INTEGER NOT NULL,
            username   TEXT,
            first_name TEXT,
            text       TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS roast_log (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id        INTEGER NOT NULL,
            target_user_id INTEGER,
            target_name    TEXT NOT NULL,
            created_at     TEXT NOT NULL
        )
        """
    )
    # Every chat the bot has seen — so the online/offline announcements know
    # where to post. (A bot can only message chats it has already encountered.)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chats (
            chat_id   INTEGER PRIMARY KEY,
            type      TEXT,
            title     TEXT,
            last_seen TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def remember_message(chat_id: int, user, text: str) -> None:
    """Store a message as roast ammo, then trim that person down to the newest 30."""
    conn = db()
    conn.execute(
        """INSERT INTO messages (chat_id, user_id, username, first_name, text, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (chat_id, user.id, user.username, user.first_name, text,
         datetime.now().isoformat()),
    )
    # Delete everything for this person in this chat EXCEPT their 30 newest rows.
    conn.execute(
        """DELETE FROM messages
           WHERE chat_id = ? AND user_id = ? AND id NOT IN (
               SELECT id FROM messages
               WHERE chat_id = ? AND user_id = ?
               ORDER BY id DESC LIMIT ?
           )""",
        (chat_id, user.id, chat_id, user.id, MAX_STORED_MSGS),
    )
    conn.commit()
    conn.close()


def recent_messages(chat_id: int, user_id: int, limit: int = 15) -> list[str]:
    """The person's most recent messages in this chat — the roast ammo."""
    conn = db()
    rows = conn.execute(
        """SELECT text FROM messages
           WHERE chat_id = ? AND user_id = ?
           ORDER BY id DESC LIMIT ?""",
        (chat_id, user_id, limit),
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def find_user_by_username(chat_id: int, username: str) -> Optional[tuple]:
    """Look up someone we've SEEN talking in this chat by their @username.

    Telegram bots can't ask 'who is @someone' out of the blue — we can only
    know people who have spoken while the bot was watching. Returns
    (user_id, first_name, username) or None if we've never seen them here.
    """
    username = username.lstrip("@").lower()
    conn = db()
    row = conn.execute(
        """SELECT user_id, first_name, username FROM messages
           WHERE chat_id = ? AND LOWER(username) = ?
           ORDER BY id DESC LIMIT 1""",
        (chat_id, username),
    ).fetchone()
    conn.close()
    return row


def log_roast(chat_id: int, target_user_id: Optional[int], target_name: str) -> None:
    """Record that someone got roasted — powers /leaderboard."""
    conn = db()
    conn.execute(
        """INSERT INTO roast_log (chat_id, target_user_id, target_name, created_at)
           VALUES (?, ?, ?, ?)""",
        (chat_id, target_user_id, target_name, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def top_roasted(chat_id: int, limit: int = 5) -> list[tuple]:
    """The most-roasted people IN THIS CHAT ONLY. Returns [(name, count), ...]."""
    conn = db()
    rows = conn.execute(
        """SELECT target_name, COUNT(*) AS n FROM roast_log
           WHERE chat_id = ?
           GROUP BY target_name
           ORDER BY n DESC LIMIT ?""",
        (chat_id, limit),
    ).fetchall()
    conn.close()
    return rows


def remember_chat(chat) -> None:
    """Record a chat we've seen (group or DM). INSERT OR REPLACE = upsert."""
    title = chat.title or getattr(chat, "first_name", None) or ""
    conn = db()
    conn.execute(
        "INSERT OR REPLACE INTO chats (chat_id, type, title, last_seen) VALUES (?, ?, ?, ?)",
        (chat.id, chat.type, title, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def all_chat_ids() -> list[int]:
    """Every chat the bot has ever seen — the audience for online/offline pings."""
    conn = db()
    rows = conn.execute("SELECT chat_id FROM chats").fetchall()
    conn.close()
    return [r[0] for r in rows]


# ---------- 3. Hardcoded replies ----------
def match_keyword(text: str) -> Optional[str]:
    """Return a hardcoded reply if the message contains a known keyword, else None.

    Whole-word match (not substring): we break the text into words first, so the
    keyword "hi" matches "hi there" but NOT "this". Same trick the todo bot uses.
    """
    words = re.findall(r"[a-z']+", text.lower())
    for keyword, replies in KEYWORD_REPLIES.items():
        if keyword in words:
            return random.choice(replies)
    return None


# ---------- 4. AI fallback chain ----------
def _deepseek(system: str, user: str) -> Optional[str]:
    if not _key_ok(DEEPSEEK_API_KEY):
        return None
    resp = requests.post(
        "https://api.deepseek.com/chat/completions",
        headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
        json={
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 1.3,
            "max_tokens": 300,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def _gemini(system: str, user: str) -> Optional[str]:
    if not _key_ok(GEMINI_API_KEY):
        return None
    # Imported lazily so a missing 'google-genai' install doesn't crash the bot
    # when you're only using the other providers.
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=GEMINI_API_KEY)
    result = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=user,
        config=types.GenerateContentConfig(
            system_instruction=system,
            temperature=1.3,
            max_output_tokens=300,
        ),
    )
    return (result.text or "").strip() or None


def _groq(system: str, user: str) -> Optional[str]:
    if not _key_ok(GROQ_API_KEY):
        return None
    from groq import Groq

    client = Groq(api_key=GROQ_API_KEY)
    result = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=1.2,
        max_tokens=300,
    )
    return result.choices[0].message.content.strip()


def _nvidia(system: str, user: str) -> Optional[str]:
    if not _key_ok(NVIDIA_API_KEY):
        return None
    resp = requests.post(
        "https://integrate.api.nvidia.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {NVIDIA_API_KEY}"},
        json={
            "model": "meta/llama-3.1-70b-instruct",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 1.2,
            "max_tokens": 300,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


# Best roast quality first, then the fast/reliable free tiers as backup.
AI_PROVIDERS = (_deepseek, _gemini, _groq, _nvidia)


def ask_ai(system: str, user: str) -> Optional[str]:
    """Try each provider in order; return the first reply that works.

    Any failure (no key, rate limit, network, bad response) is swallowed and we
    move to the next provider. If they ALL fail we return None and the caller
    uses a canned fallback roast.
    """
    for provider in AI_PROVIDERS:
        try:
            reply = provider(system, user)
            if reply:
                return reply
        except Exception as e:
            # Print for your own debugging; never shown to users.
            print(f"[ai] {provider.__name__} failed: {e}")
    return None


# ---------- 5. Roast prompts ----------
# The tone lives here. Brutal, but with a hard floor: no slurs / hate speech /
# attacks on protected characteristics — that keeps the bot inside Telegram's
# rules so it doesn't get banned. Everything else is fair game.
ROAST_SYSTEM: Final = (
    "You are RoastBot, a merciless stand-up roast comedian in a group chat. "
    "Your job is to absolutely COOK the target — brutal, savage, no mercy, no "
    "softening, no compliments. Be clever and specific, not just mean. "
    "Rules: 1-3 short punchy sentences max. Add one or two emojis like 💀🔥😭. "
    "Reply in the SAME language the target uses (English, Khmer, or a mix — match them). "
    "Never use slurs, hate speech, or attacks on race, religion, gender, sexuality, "
    "or disability. Attack their vibe, choices, and words instead. Output ONLY the "
    "roast, nothing else."
)


def build_roast_user_prompt(target_name: str, username: Optional[str],
                            ammo: list[str]) -> str:
    """Assemble the 'who to roast + what we know' half of the prompt."""
    who = target_name or (f"@{username}" if username else "this person")
    if ammo:
        quoted = "\n".join(f'- "{m}"' for m in ammo)
        return (
            f"Roast {who}. Here are recent things they actually said in the chat — "
            f"use these as ammo and reference what they said:\n{quoted}"
        )
    if username:
        return (
            f"Roast the user @{username}. You have nothing they've said to work with, "
            f"so roast them based on their username and the fact that they're too "
            f"boring to have said anything worth remembering."
        )
    return (
        f"Roast {who}. You know nothing about them, so hit them with a brutal, "
        f"general-purpose roast about how forgettable and average they are."
    )


def roast_or_fallback(system: str, user: str) -> str:
    """Get an AI roast, or a canned burn if every provider is down."""
    return ask_ai(system, user) or random.choice(FALLBACK_ROASTS)


# ---------- 6. Cooldown helper ----------
def on_cooldown(chat_id: int) -> bool:
    """True if this chat roasted too recently. Also refreshes the timer if OK."""
    now = time.time()
    last = last_roast_at.get(chat_id, 0)
    if now - last < COOLDOWN_SECONDS:
        return True
    last_roast_at[chat_id] = now
    return False


# ---------- 6b. Typing indicator + non-blocking AI ----------
async def _keep_typing(chat) -> None:
    """Keep the 'RoastBot is typing…' bubble alive until this task is cancelled.

    Telegram's typing status only lasts about 5 seconds, so we re-send it every
    few seconds. Without this the bubble would disappear while a slow AI call is
    still thinking, and the roast would land with no build-up.
    """
    try:
        while True:
            await chat.send_action(ChatAction.TYPING)
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass


async def run_with_typing(chat, func, *args):
    """Run a BLOCKING function (an AI call) while showing a live typing bubble.

    Two problems solved at once:
      1. The user sees 'typing…' for the WHOLE time the AI is thinking, however
         long that takes (see _keep_typing).
      2. The blocking network call runs in a background thread (asyncio.to_thread)
         instead of freezing the event loop — so the bot stays responsive to
         everyone else in the chat while one roast is cooking.
    """
    typing = asyncio.create_task(_keep_typing(chat))
    try:
        return await asyncio.to_thread(func, *args)
    finally:
        typing.cancel()


# ---------- 7. Commands ----------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "😈 I'm RoastBot. I exist to roast you and everyone you know.\n\n"
        "Add me to a group and I'll cook your friends on demand. Fair warning: "
        "feelings WILL be hurt, and that's the point 💀\n\n"
        "Type /help to see how to get roasted."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔥 *How to get roasted* 🔥\n\n"
        "/roast — reply to someone's message with this to roast THEM, "
        "or `/roast @username` to target them by name.\n"
        "/roastme — roast yourself (brave).\n"
        "/battle @username — challenge someone: you both roast each other, "
        "then I judge who won 🥊\n"
        "/versus @a @b — I roast both of them myself and pick a winner.\n"
        "/leaderboard — this chat's Hall of Shame (most roasted).\n\n"
        "In a group I stay quiet unless you use a command, @mention me, or reply "
        "to one of my messages. In a private chat I roast everything you send 😌",
        parse_mode="Markdown",
    )


async def _do_roast(update, chat_id, target_user_id, target_name, username, ammo):
    """Shared roast-and-send used by /roast, /roastme, and the message handler."""
    prompt = build_roast_user_prompt(target_name, username, ammo)
    roast = await run_with_typing(update.effective_chat, roast_or_fallback,
                                  ROAST_SYSTEM, prompt)
    log_roast(chat_id, target_user_id, target_name or (username or "someone"))
    await update.message.reply_text(roast)


async def roast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    chat_type = update.message.chat.type

    if on_cooldown(chat_id):
        await update.message.reply_text(random.choice(COOLDOWN_LINES))
        return

    # Case 1: replying to someone's message -> roast that someone.
    reply = update.message.reply_to_message
    if reply and reply.from_user:
        target = reply.from_user
        ammo = recent_messages(chat_id, target.id)
        # The replied-to message itself is prime ammo — put it first.
        if reply.text:
            ammo = [reply.text] + ammo
        await _do_roast(update, chat_id, target.id, target.first_name,
                        target.username, ammo)
        return

    # Case 2: /roast @username -> look them up in what we've seen.
    if context.args:
        username = context.args[0].lstrip("@")
        row = find_user_by_username(chat_id, username)
        if row is None:
            await update.message.reply_text(
                f"I haven't seen @{username} say anything here yet — "
                f"they're too irrelevant to roast 💀"
            )
            return
        target_user_id, first_name, uname = row
        ammo = recent_messages(chat_id, target_user_id)
        await _do_roast(update, chat_id, target_user_id, first_name, uname, ammo)
        return

    # Case 3: plain /roast in a private chat -> roast the sender.
    if chat_type == "private":
        user = update.message.from_user
        ammo = recent_messages(chat_id, user.id)
        await _do_roast(update, chat_id, user.id, user.first_name,
                        user.username, ammo)
        return

    # Case 4: plain /roast in a group with no target -> explain how.
    await update.message.reply_text(
        "Reply to someone's message with /roast, or use /roast @username 🎯"
    )


async def roastme_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    if on_cooldown(chat_id):
        await update.message.reply_text(random.choice(COOLDOWN_LINES))
        return
    user = update.message.from_user
    ammo = recent_messages(chat_id, user.id)
    await _do_roast(update, chat_id, user.id, user.first_name, user.username, ammo)


async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    rows = top_roasted(chat_id)
    if not rows:
        await update.message.reply_text(
            "Nobody's been roasted here yet. Use /roast and let's fix that 💀"
        )
        return
    medals = ["🥇", "🥈", "🥉", "4.", "5."]
    lines = ["🏆 *Hall of Shame* — most roasted in this chat:\n"]
    for i, (name, count) in enumerate(rows):
        badge = medals[i] if i < len(medals) else f"{i + 1}."
        lines.append(f"{badge} {name} — roasted {count}×")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ----- /battle: two humans roast each other, the AI judges -----
BATTLE_JUDGE_SYSTEM: Final = (
    "You are the judge and hype-man of a roast battle in a group chat. Two people "
    "each wrote one roast of the other. Score EACH roast out of 10 (be harsh and "
    "fair), commentate like an excited boxing announcer, then declare ONE winner. "
    "Keep it under 6 short lines, use emojis like 🥊🔥💀🏆. Match the language of "
    "the roasts. Do not add extra roasts of your own — just judge theirs."
)


async def battle_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    challenger = update.message.from_user

    # /battle cancel -> clear any stuck battle in this chat.
    if context.args and context.args[0].lower() == "cancel":
        pending_battles.pop(chat_id, None)
        await update.message.reply_text("Battle cancelled. Cowards, both of you 💀")
        return

    if chat_id in pending_battles:
        await update.message.reply_text(
            "A battle's already going in this chat — finish it first, "
            "or /battle cancel to reset it."
        )
        return

    # Figure out the opponent: either a replied-to user or /battle @username.
    opponent = None
    reply = update.message.reply_to_message
    if reply and reply.from_user and not reply.from_user.is_bot:
        opponent = {
            "user_id": reply.from_user.id,
            "name": reply.from_user.first_name,
            "username": reply.from_user.username,
        }
    elif context.args:
        username = context.args[0].lstrip("@")
        row = find_user_by_username(chat_id, username)
        if row is None:
            await update.message.reply_text(
                f"I haven't seen @{username} talk here yet, so they can't fight. "
                f"Pick someone who actually shows up 💀"
            )
            return
        opponent = {"user_id": row[0], "name": row[1], "username": row[2]}
    else:
        await update.message.reply_text(
            "Start a battle by replying to someone with /battle, "
            "or /battle @username 🥊"
        )
        return

    if opponent["user_id"] == challenger.id:
        await update.message.reply_text("You can't battle yourself. Try /roastme 💀")
        return

    # Set up the battle. challenger roasts first; then the opponent; then we judge.
    pending_battles[chat_id] = {
        "started_at": time.time(),
        "turn_user_id": challenger.id,          # whose roast we're waiting for
        "fighters": {
            challenger.id: {"name": challenger.first_name, "roast": None},
            opponent["user_id"]: {"name": opponent["name"], "roast": None},
        },
        "order": [challenger.id, opponent["user_id"]],
    }
    await update.message.reply_text(
        f"🥊 ROAST BATTLE 🥊\n"
        f"{challenger.first_name} 🆚 {opponent['name']}\n\n"
        f"{challenger.first_name}, you're up first — send your roast now. "
        f"Then {opponent['name']} gets to fire back. I'll judge who won 🔥"
    )


def _battle_expired(battle: dict) -> bool:
    return time.time() - battle["started_at"] > BATTLE_TIMEOUT


async def _handle_battle_turn(update: Update, battle: dict) -> None:
    """Called from the message handler when it's the sender's turn in a battle."""
    chat_id = update.message.chat_id
    user = update.message.from_user
    text = update.message.text

    battle["fighters"][user.id]["roast"] = text
    battle["started_at"] = time.time()  # reset the expiry clock on activity
    order = battle["order"]

    # If the first fighter just went, hand the mic to the second.
    if user.id == order[0]:
        battle["turn_user_id"] = order[1]
        opponent_name = battle["fighters"][order[1]]["name"]
        await update.message.reply_text(
            f"🔥 Noted. {opponent_name}, your turn — cook them back!"
        )
        return

    # Second fighter just went -> both roasts are in, time to judge.
    del pending_battles[chat_id]
    f1_id, f2_id = order
    f1, f2 = battle["fighters"][f1_id], battle["fighters"][f2_id]
    judge_prompt = (
        f"{f1['name']}'s roast: \"{f1['roast']}\"\n\n"
        f"{f2['name']}'s roast: \"{f2['roast']}\"\n\n"
        f"Score both, commentate, and crown the winner."
    )
    verdict = await run_with_typing(update.effective_chat, ask_ai,
                                    BATTLE_JUDGE_SYSTEM, judge_prompt)
    if not verdict:
        verdict = "Judges are asleep 💀 Call it a draw — you both need practice."
    await update.message.reply_text(f"🏆 *THE VERDICT* 🏆\n\n{verdict}",
                                    parse_mode="Markdown")


# ----- /versus: the AI roasts both people itself and picks a winner -----
VERSUS_SYSTEM: Final = (
    "You are RoastBot refereeing a roast showdown between two people in a group "
    "chat. Write a brutal, savage roast of EACH person (1-2 sentences each) using "
    "the ammo provided, then declare a winner — the one you roasted harder or who's "
    "the bigger disaster. Use emojis like 🥊🔥💀🏆, keep it under 6 lines total. "
    "Match the language they use. No slurs or hate speech. "
    "Format: name, their roast, then the next name, then the winner."
)


async def versus_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    if on_cooldown(chat_id):
        await update.message.reply_text(random.choice(COOLDOWN_LINES))
        return

    # Collect the two fighters. Accept: reply + one @username, or two @usernames.
    fighters = []
    reply = update.message.reply_to_message
    if reply and reply.from_user and not reply.from_user.is_bot:
        u = reply.from_user
        fighters.append({"user_id": u.id, "name": u.first_name,
                         "username": u.username})

    for arg in context.args:
        username = arg.lstrip("@")
        row = find_user_by_username(chat_id, username)
        if row is None:
            await update.message.reply_text(
                f"I haven't seen @{username} talk here yet — can't roast a ghost 💀"
            )
            return
        fighters.append({"user_id": row[0], "name": row[1], "username": row[2]})
        if len(fighters) == 2:
            break

    if len(fighters) < 2:
        await update.message.reply_text(
            "Give me two people: /versus @alice @bob "
            "(or reply to one and add /versus @bob) 🥊"
        )
        return

    a, b = fighters[0], fighters[1]
    a_ammo = recent_messages(chat_id, a["user_id"])
    b_ammo = recent_messages(chat_id, b["user_id"])

    def _ammo_block(f, ammo):
        if ammo:
            quoted = "; ".join(f'"{m}"' for m in ammo[:8])
            return f"{f['name']} recently said: {quoted}"
        return f"{f['name']} has said nothing memorable — roast them for being boring."

    versus_prompt = (
        f"Fighter 1: {a['name']}. {_ammo_block(a, a_ammo)}\n\n"
        f"Fighter 2: {b['name']}. {_ammo_block(b, b_ammo)}\n\n"
        f"Roast both and pick a winner."
    )
    result = await run_with_typing(update.effective_chat, roast_or_fallback,
                                   VERSUS_SYSTEM, versus_prompt)
    log_roast(chat_id, a["user_id"], a["name"])
    log_roast(chat_id, b["user_id"], b["name"])
    await update.message.reply_text(f"🥊 {a['name']} 🆚 {b['name']} 🥊\n\n{result}")


# ---------- 8. Message handler (non-command text) ----------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Ignore anything without text (stickers, photos, etc).
    if not update.message or not update.message.text:
        return

    chat_id = update.message.chat_id
    chat_type = update.message.chat.type
    user = update.message.from_user
    text = update.message.text
    is_group = chat_type in ("group", "supergroup")

    # 1. Always remember group messages — this is the roast ammo. (In private
    #    chats we don't need history; the current message is enough.)
    if is_group:
        remember_message(chat_id, user, text)

    # 2. Is a battle waiting on THIS person's roast? If so, this message is it.
    battle = pending_battles.get(chat_id)
    if battle:
        if _battle_expired(battle):
            pending_battles.pop(chat_id, None)
        elif battle["turn_user_id"] == user.id:
            await _handle_battle_turn(update, battle)
            return

    # 3. Private chat: roast every message. Hardcoded reply first, then AI.
    if chat_type == "private":
        canned = match_keyword(text)
        if canned:
            await update.message.reply_text(canned)
            return
        if on_cooldown(chat_id):
            await update.message.reply_text(random.choice(COOLDOWN_LINES))
            return
        await _do_roast(update, chat_id, user.id, user.first_name,
                        user.username, [text])
        return

    # 4. Group chat: only speak up if the bot is mentioned or replied to.
    replied_to_bot = (
        update.message.reply_to_message
        and update.message.reply_to_message.from_user
        and update.message.reply_to_message.from_user.is_bot
    )
    mentioned = BOT_USERNAME.lower() in text.lower()
    if not (mentioned or replied_to_bot):
        return  # stay quiet — just kept the message as ammo

    # We were summoned. Hardcoded reply first, then roast the sender.
    canned = match_keyword(text)
    if canned:
        await update.message.reply_text(canned)
        return
    if on_cooldown(chat_id):
        await update.message.reply_text(random.choice(COOLDOWN_LINES))
        return
    ammo = [text] + recent_messages(chat_id, user.id)
    await _do_roast(update, chat_id, user.id, user.first_name, user.username, ammo)


async def track_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Record every chat the bot sees (messages, commands, being added to a group).

    Registered in group=-1 so it runs first for EVERY update but does NOT consume
    it — the real command/message handlers still run afterwards. This is how the
    online/offline announcements learn which chats to post to.
    """
    if update.effective_chat:
        remember_chat(update.effective_chat)


# ---------- Online / offline announcements ----------
async def _announce(app, text: str) -> None:
    """Send `text` to every chat the bot has seen. Failures (kicked from a group,
    chat deleted, etc.) are ignored so one dead chat can't stop the rest."""
    for chat_id in all_chat_ids():
        try:
            await app.bot.send_message(chat_id, text)
        except Exception as e:
            print(f"[announce] couldn't reach chat {chat_id}: {e}")


async def on_startup(app) -> None:
    """Runs once the bot is initialized (post_init) — tells every chat it's back."""
    print("announcing ONLINE...")
    await _announce(app, "🟢 RoastBot is ONLINE. Who wants to get cooked first? 💀🔥")


async def on_shutdown(app) -> None:
    """Runs on graceful shutdown (post_stop, e.g. Ctrl+C) — says goodbye while the
    bot can still send. A hard kill / crash skips this, so 'offline' isn't
    guaranteed — but Ctrl+C and normal stops will announce it."""
    print("announcing OFFLINE...")
    await _announce(app, "🔴 RoastBot is going OFFLINE. Saved by the bell 😌")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    print(f"Update {update} caused error {context.error}")


# ---------- 9. Wiring ----------
if __name__ == "__main__":
    print("starting roast bot...")
    init_db()
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("roast", roast_command))
    app.add_handler(CommandHandler("roastme", roastme_command))
    app.add_handler(CommandHandler("battle", battle_command))
    app.add_handler(CommandHandler("versus", versus_command))
    app.add_handler(CommandHandler("leaderboard", leaderboard_command))

    # Every non-command text message flows through here.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.add_error_handler(error_handler)

    print("polling... (Ctrl+C to stop)")
    app.run_polling(poll_interval=3)
