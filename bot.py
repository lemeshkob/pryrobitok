import logging
import math
import random
import re
import sqlite3
import time
from collections import defaultdict, deque
from functools import wraps
from pathlib import Path

from telegram import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeDefault,
    Update,
)
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"  # or set via env var, see run.py
DB_PATH = Path(__file__).parent / "data" / "pryrobitok.db"

# The word "приробіток" in all its case forms, tolerating common typos:
# "приробток" (dropped і), "пріробіток" / "приробиток" (і/и mixed up), latin "i" for "і",
# surzhyk "прірабіток" / "пріработок" (а for о, о for і),
# "при робіток" (split prefix — "робіток" is not a word on its own, so this is safe).
PRYROBITOK_WORD = r"пр[иіiы]\s?р[оа]б[іiиоы]?т(?:ок|к(?:у|а|и|ом|[іi]в|ах|ами))"
SIGN = r"(?P<sign>плюс|мінус|plus|minus|[+\-−–—])"  # incl. unicode dashes
NEGATIVE_SIGNS = ("мінус", "minus", "-", "−", "–", "—")

# A signed pryrobitok command anywhere in a message. The sign is mandatory —
# a bare "приробіток" is just a mention, not a command.
#
# Sign before the word:
#   "+приробіток" / "мінус приробіток" / "+5 приробітків" / "плюс 5 приробітку"
#   "Барис барбарис - приробіток" / "молодець, +2 приробітки тобі"
# (?<!\w) keeps hyphenated words like "супер-приробіток" from counting as a minus.
SIGN_FIRST_RE = re.compile(
    rf"(?<!\w){SIGN}\s*(?P<number>\d+)?\s*{PRYROBITOK_WORD}(?!\w)",
    re.IGNORECASE,
)

# Sign after the word:
#   "приробіток -" / "приробіток мінус" / "приробіток +5" / "приробіток мінус 3!"
# A dash after a noun is ordinary punctuation ("приробіток — це святе"), so the sign only
# counts when it is followed by a number or by nothing but punctuation till the end.
SIGN_LAST_RE = re.compile(
    rf"(?<!\w){PRYROBITOK_WORD}\s*{SIGN}\s*(?:(?P<number>\d+)(?!\w)|(?=[\s.!?,)]*$))",
    re.IGNORECASE,
)

# Begging for pryrobitok anywhere in a message: "хочу приробіток", "дайте приробітку",
# "дай мені ще приробітків" (up to two words in between).
WANT_RE = re.compile(
    r"(?<!\w)(?:хочу|хочемо|хочеться|хочется|дай|дайте)[\s,]+(?:[\w']+[\s,]+){0,2}"
    rf"{PRYROBITOK_WORD}(?!\w)",
    re.IGNORECASE,
)
GIFS_DIR = Path(__file__).parent / "gifs"
WANT_GIF_PATH = GIFS_DIR / "want.gif"
DMG_GIF_PATH = GIFS_DIR / "dmg.gif"

# Replies to a batrakan who tries to hand out pryrobitok; one is picked at random.
IMPOSTOR_TEXTS = [
    "Ти чьо пьос, возомніл себе начальніком!? Мінус приробіток",
    "Куда ти лєзєш, салага? Приробітки тут роздаю я. Мінус приробіток",
    "Їдрить твою наліво, ще один начальнік знайшовся! Мінус приробіток",
    "Ти шо, безсмертний? Марш до станка! Мінус приробіток",
    "Йошкін кіт, батракан командує! Рило не треснуло? Мінус приробіток",
    "Хто тобі, хрєн моржовий, давав право голосу? Мінус приробіток",
    "Ти диви, яке начальство вилупилось! Лопату в зуби — і в цех. Мінус приробіток",
    "Шо за самодєятєльность, мать-перемать?! Мінус приробіток",
    "Не по чину береш, гніда цехова. Мінус приробіток",
    "А нє пашол би ти... план виконувати? Мінус приробіток",
    "Губу закатай, стахановець хрєнов. Мінус приробіток",
    "Твоє діло — пахати й не гавкати. Мінус приробіток",
    "Ще раз побачу — підеш у нічну без обіду, падлюка. Мінус приробіток",
    "Ти в табелі хто? Батракан! От і не рипайся, йоб твою дивізію. Мінус приробіток",
    "Начальнік тут один, а ти — розхідний матеріал. Мінус приробіток",
]

# A single penalty of this size or harsher (delta <= threshold) gets the "YOU DIED" gif.
DMG_THRESHOLD = -10


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS chats (
            chat_id INTEGER PRIMARY KEY,
            boss_user_id INTEGER
        );

        CREATE TABLE IF NOT EXISTS batrakany (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            display_name TEXT NOT NULL,
            score INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, user_id),
            FOREIGN KEY (chat_id) REFERENCES chats (chat_id)
        );
        """
    )
    conn.commit()
    conn.close()


def set_boss(chat_id: int, boss_user_id: int) -> bool:
    """Assigns the boss only if the chat has none yet. Returns False if a boss already exists."""
    conn = get_db()
    cursor = conn.execute(
        """
        INSERT INTO chats (chat_id, boss_user_id) VALUES (?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET boss_user_id = excluded.boss_user_id
        WHERE chats.boss_user_id IS NULL
        """,
        (chat_id, boss_user_id),
    )
    conn.commit()
    assigned = cursor.rowcount > 0
    conn.close()
    return assigned


def get_boss(chat_id: int) -> int | None:
    conn = get_db()
    row = conn.execute(
        "SELECT boss_user_id FROM chats WHERE chat_id = ?", (chat_id,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


def ensure_batrakan(chat_id: int, user_id: int, display_name: str) -> None:
    """Registers a user with 0 score if not already tracked. Never overwrites an existing score."""
    conn = get_db()
    # the chat row may not exist yet if nobody has run /start_boss
    conn.execute("INSERT OR IGNORE INTO chats (chat_id, boss_user_id) VALUES (?, NULL)", (chat_id,))
    conn.execute(
        """
        INSERT INTO batrakany (chat_id, user_id, display_name, score)
        VALUES (?, ?, ?, 0)
        ON CONFLICT(chat_id, user_id) DO UPDATE SET display_name = excluded.display_name
        """,
        (chat_id, user_id, display_name),
    )
    conn.commit()
    conn.close()


def adjust_score(chat_id: int, user_id: int, display_name: str, delta: int) -> int:
    ensure_batrakan(chat_id, user_id, display_name)
    conn = get_db()
    conn.execute(
        "UPDATE batrakany SET score = score + ? WHERE chat_id = ? AND user_id = ?",
        (delta, chat_id, user_id),
    )
    conn.commit()
    row = conn.execute(
        "SELECT score FROM batrakany WHERE chat_id = ? AND user_id = ?",
        (chat_id, user_id),
    ).fetchone()
    conn.close()
    return row[0]


def get_all_scores(chat_id: int) -> list[tuple[str, int]]:
    """Scores of everyone except the current boss (a demoted boss keeps their old row)."""
    conn = get_db()
    rows = conn.execute(
        """
        SELECT b.display_name, b.score
        FROM batrakany b
        JOIN chats c ON c.chat_id = b.chat_id
        WHERE b.chat_id = ? AND b.user_id IS NOT c.boss_user_id
        ORDER BY b.score DESC
        """,
        (chat_id,),
    ).fetchall()
    conn.close()
    return rows


def get_score(chat_id: int, user_id: int) -> int | None:
    conn = get_db()
    row = conn.execute(
        "SELECT score FROM batrakany WHERE chat_id = ? AND user_id = ?",
        (chat_id, user_id),
    ).fetchone()
    conn.close()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def user_display_name(user) -> str:
    if user.username:
        return f"@{user.username}"
    return user.full_name or str(user.id)


def pick_impostor_text(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> str:
    """Random phrase, never the same one twice in a row in a chat."""
    last = context.bot_data.setdefault("last_impostor_text", {})
    text = random.choice([t for t in IMPOSTOR_TEXTS if t != last.get(chat_id)])
    last[chat_id] = text
    return text


def parse_delta(text: str) -> int | None:
    """Returns the signed delta of the first pryrobitok command found in text, else None."""
    matches = [m for m in (SIGN_FIRST_RE.search(text), SIGN_LAST_RE.search(text)) if m]
    if not matches:
        return None

    match = min(matches, key=lambda m: m.start())
    amount = int(match.group("number")) if match.group("number") else 1
    return -amount if match.group("sign").lower() in NEGATIVE_SIGNS else amount


# ---------------------------------------------------------------------------
# Flood protection
# ---------------------------------------------------------------------------

class RateLimiter:
    """
    Sliding window: at most `limit` allowed events per `window` seconds per key.
    Rejected events don't extend the window, so a spammer is let back in as soon
    as their earlier hits expire rather than being locked out for as long as they spam.
    """

    ALLOW, WARN, DROP = "allow", "warn", "drop"

    def __init__(self, limit: int, window: float) -> None:
        self.limit = limit
        self.window = window
        self._hits: dict[tuple, deque[float]] = defaultdict(deque)
        self._warned: set[tuple] = set()

    def check(self, key: tuple) -> str:
        """ALLOW — go ahead; WARN — first rejection in this burst; DROP — ignore silently."""
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] >= self.window:
            hits.popleft()

        if len(hits) < self.limit:
            hits.append(now)
            self._warned.discard(key)
            return self.ALLOW

        if key in self._warned:
            return self.DROP
        self._warned.add(key)
        return self.WARN

    def retry_after(self, key: tuple) -> int:
        hits = self._hits.get(key)
        if not hits:
            return 0
        return max(1, math.ceil(self.window - (time.monotonic() - hits[0])))


# Commands: per user in a chat, plus a chat-wide cap so a group of people
# (or one person with several accounts) can't flood the chat together.
USER_COMMAND_LIMITER = RateLimiter(limit=3, window=20)
CHAT_COMMAND_LIMITER = RateLimiter(limit=10, window=30)
# Bot replies triggered by plain text from batrakany (want-gif, impostor fine).
USER_REACTION_LIMITER = RateLimiter(limit=2, window=30)


def rate_limited(handler):
    """Wraps a command handler: over the limit a user gets one warning, then silence."""

    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        user = update.effective_user
        message = update.effective_message
        if not chat or not user or not message:
            return

        user_key = (chat.id, user.id)
        verdict = USER_COMMAND_LIMITER.check(user_key)
        if verdict == RateLimiter.ALLOW:
            # chat-wide cap is only charged for commands that passed the per-user check
            verdict = CHAT_COMMAND_LIMITER.check((chat.id,))
            if verdict == RateLimiter.ALLOW:
                await handler(update, context)
                return
            if verdict == RateLimiter.WARN:
                await message.reply_text(
                    "Забагато команд у чаті, перекур "
                    f"{CHAT_COMMAND_LIMITER.retry_after((chat.id,))} с. Ідіть працювати."
                )
            return

        if verdict == RateLimiter.WARN:
            await message.reply_text(
                f"Не дудось, {user_display_name(user)}. "
                f"Наступна команда — через {USER_COMMAND_LIMITER.retry_after(user_key)} с."
            )
        logger.info("Rate-limited command from %s in chat %s", user.id, chat.id)

    return wrapper


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def start_boss(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Usage:
      /start_boss            (as a reply to the future boss's message)
      /start_boss @username  (mention the future boss)
    """
    chat = update.effective_chat
    message = update.effective_message

    if chat.type == ChatType.PRIVATE:
        await message.reply_text("Ця команда працює тільки в групових чатах.")
        return

    if get_boss(chat.id) is not None:
        await message.reply_text("Начальнік у цьому чаті вже призначений — змінити його не можна.")
        return

    boss_user = None

    if message.reply_to_message:
        boss_user = message.reply_to_message.from_user
    elif context.args:
        # crude @username resolution: requires that user has spoken in chat before
        # so python-telegram-bot / Telegram can't resolve username -> id directly
        # without extra API calls; ask them to reply instead if this fails.
        username = context.args[0].lstrip("@")
        await message.reply_text(
            f"Щоб призначити @{username} начальніком, зроби reply цією командою "
            f"на будь-яке повідомлення цієї людини в чаті: /start_boss"
        )
        return
    else:
        boss_user = update.effective_user

    if boss_user.is_bot:
        await message.reply_text("Бот не може бути начальніком.")
        return

    if not set_boss(chat.id, boss_user.id):
        # lost a race with a concurrent /start_boss
        await message.reply_text("Начальнік у цьому чаті вже призначений — змінити його не можна.")
        return

    await message.reply_text(
        f"👑 {user_display_name(boss_user)} тепер начальнік цього чату.\n"
        f"Всі інші учасники — батракани, стартують з 0 приробітком."
    )


async def show_scores(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    rows = get_all_scores(chat.id)

    if not rows:
        await update.effective_message.reply_text(
            "Поки що немає жодного зареєстрованого батракана. "
            "Начальнік має спершу комусь відповісти командою приробітку."
        )
        return

    lines = ["📊 Приробіток батраканів:"]
    for name, score in rows:
        lines.append(f"  {name}: {score}")

    await update.effective_message.reply_text("\n".join(lines))


async def my_score(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user

    if user.id == get_boss(chat.id):
        await update.effective_message.reply_text("Ти начальнік — у начальніка приробітку немає 👑")
        return

    score = get_score(chat.id, user.id)

    if score is None:
        await update.effective_message.reply_text(
            "Ти ще не зареєстрований(-а) як батракан у цьому чаті."
        )
        return

    await update.effective_message.reply_text(f"Твій приробіток: {score}")


# ---------------------------------------------------------------------------
# Roster — register everyone the bot sees in a group chat
# ---------------------------------------------------------------------------

async def register_participants(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Runs before all other handlers on every group message (text, media, commands,
    join service messages). Telegram gives bots no way to list chat members, so the
    roster is built from everyone who shows up in any update.
    """
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return

    seen = [message.from_user, *(message.new_chat_members or [])]
    if message.reply_to_message:
        seen.append(message.reply_to_message.from_user)

    boss_id = get_boss(chat.id)
    for user in seen:
        if user is None or user.is_bot or user.id == boss_id:
            continue
        ensure_batrakan(chat.id, user.id, user_display_name(user))


# ---------------------------------------------------------------------------
# Main message handler — watches for boss's pryrobitok replies
# ---------------------------------------------------------------------------

async def reply_gif(
    message, context: ContextTypes.DEFAULT_TYPE, path: Path, caption: str | None = None
) -> bool:
    """Replies with a gif. Returns False if the file is missing, so the caller can fall back to text."""
    # After the first upload reuse Telegram's file_id instead of re-sending the file.
    cache = context.bot_data.setdefault("gif_file_ids", {})
    file_id = cache.get(path.name)
    if file_id:
        await message.reply_animation(file_id, caption=caption)
        return True

    if not path.exists():
        logger.warning("Gif not found at %s", path)
        return False

    with path.open("rb") as gif:
        sent = await message.reply_animation(gif, filename=path.name, caption=caption)
    media = sent.animation or sent.document
    if media:
        cache[path.name] = media.file_id
    return True


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    sender = update.effective_user

    if not message or not message.text or not sender:
        return

    logger.info(
        "Message in chat %s from %s (reply: %s)",
        chat.id, sender.id, bool(message.reply_to_message),
    )

    boss_id = get_boss(chat.id)

    if sender.id != boss_id:
        # A batrakan playing boss (with or without a reply) gets fined instead.
        # Only once a boss exists — before that there is nobody to impersonate.
        if boss_id is not None and parse_delta(message.text) is not None:
            # The fine always applies; only the reply is throttled, so spamming
            # fake commands costs points without flooding the chat.
            new_score = adjust_score(chat.id, sender.id, user_display_name(sender), -1)
            if USER_REACTION_LIMITER.check((chat.id, sender.id)) == RateLimiter.ALLOW:
                await message.reply_text(
                    f"{pick_impostor_text(context, chat.id)}\n"
                    f"{user_display_name(sender)}: -1 приробітку → тепер {new_score}"
                )
        elif WANT_RE.search(message.text):
            if USER_REACTION_LIMITER.check((chat.id, sender.id)) == RateLimiter.ALLOW:
                await reply_gif(message, context, WANT_GIF_PATH)
        return

    # Only the boss's replies can adjust scores.

    if not message.reply_to_message:
        return

    target_user = message.reply_to_message.from_user
    if target_user is None or target_user.is_bot:
        return

    delta = parse_delta(message.text)
    if delta is None:
        return

    if target_user.id == boss_id:
        await message.reply_text("Начальнік не може змінювати власний приробіток 🙂")
        return

    new_score = adjust_score(chat.id, target_user.id, user_display_name(target_user), delta)

    sign = "+" if delta > 0 else ""
    result_text = f"{user_display_name(target_user)}: {sign}{delta} приробітку → тепер {new_score}"

    if delta <= DMG_THRESHOLD:
        # The score is already saved — if the gif fails, still confirm it with plain text.
        try:
            if await reply_gif(message, context, DMG_GIF_PATH, caption=result_text):
                return
        except TelegramError:
            logger.exception("Failed to send dmg gif")

    await message.reply_text(result_text)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

BOT_COMMANDS = [
    BotCommand("pryrobitok", "Приробіток усіх батраканів"),
    BotCommand("myscore", "Мій приробіток"),
    BotCommand("start_boss", "Призначити начальніка (reply або себе, один раз)"),
]


async def on_startup(application: Application) -> None:
    # The menu users see when they type "/" in a group chat.
    try:
        await application.bot.set_my_commands(BOT_COMMANDS, scope=BotCommandScopeAllGroupChats())
        # The bot only works in groups — don't advertise commands in private chats.
        await application.bot.delete_my_commands(scope=BotCommandScopeDefault())
    except TelegramError:
        logger.exception("Failed to register the command menu")

    if not application.bot.bot.can_read_all_group_messages:
        logger.warning(
            "Privacy Mode is ENABLED: the bot only sees commands in groups, so "
            "'+приробіток' replies will be ignored. Disable it via @BotFather "
            "(/setprivacy -> Disable), then remove the bot from the group and add it back."
        )


def build_app(token: str) -> Application:
    init_db()
    application = (
        Application.builder().token(token).post_init(on_startup).build()
    )

    # group=-1: runs first and doesn't stop the handlers below from firing
    application.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & filters.UpdateType.MESSAGE, register_participants
        ),
        group=-1,
    )
    # UpdateType.MESSAGE: editing an old command must not re-run it (a free way to spam).
    commands = {"start_boss": start_boss, "pryrobitok": show_scores, "myscore": my_score}
    for name, handler in commands.items():
        application.add_handler(
            CommandHandler(name, rate_limited(handler), filters=filters.UpdateType.MESSAGE)
        )
    # UpdateType.MESSAGE excludes edited messages — otherwise editing
    # a "+приробіток" reply would count it a second time.
    application.add_handler(
        MessageHandler(
            filters.UpdateType.MESSAGE & filters.TEXT & ~filters.COMMAND,
            handle_message,
        )
    )

    return application


def main() -> None:
    import os

    token = os.environ.get("BOT_TOKEN", BOT_TOKEN)
    if token == "YOUR_BOT_TOKEN_HERE":
        raise RuntimeError(
            "Set BOT_TOKEN environment variable or edit BOT_TOKEN in bot.py"
        )

    app = build_app(token)
    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()