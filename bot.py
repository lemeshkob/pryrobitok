import logging
import re
import sqlite3
from pathlib import Path

from telegram import Update
from telegram.constants import ChatType
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

# Regex for messages like:
#   "приробіток" / "+приробіток" / "- приробіток"
#   "плюс приробіток" / "мінус приробіток"
#   "+5 приробітків" / "-3 приробітку" / "плюс 5 приробітку"
PRYROBITOK_RE = re.compile(
    r"""
    ^\s*
    (?:
        (?P<sign_word>плюс|мінус|minus|plus)   # word form of sign
        |
        (?P<sign_symbol>[+\-−–—])               # symbol form of sign (incl. unicode dashes)
    )?
    \s*
    (?P<number>\d+)?                            # optional explicit amount
    \s*
    приробіт(?:ок|к(?:у|а|и|ом|ів|ах|ами))      # word "приробіток" and forms
    [\s.!]*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


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


def parse_delta(text: str) -> int | None:
    """Returns the signed integer delta if text matches a pryrobitok command, else None."""
    match = PRYROBITOK_RE.match(text.strip())
    if not match:
        return None

    sign_word = (match.group("sign_word") or "").lower()
    sign_symbol = match.group("sign_symbol")
    number_str = match.group("number")

    amount = int(number_str) if number_str else 1

    is_negative = sign_word in ("мінус", "minus") or sign_symbol in ("-", "−", "–", "—")
    is_positive = sign_word in ("плюс", "plus") or sign_symbol == "+"

    if not is_negative and not is_positive:
        # bare "приробіток" with no sign at all is not a valid command —
        # require an explicit +/- or плюс/мінус to avoid false positives.
        return None

    return -amount if is_negative else amount


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
# Main message handler — watches for boss's pryrobitok replies
# ---------------------------------------------------------------------------

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

    # Auto-register every non-boss participant who talks as a batrakan with 0,
    # so the roster fills in naturally without needing a separate command.
    boss_id = get_boss(chat.id)
    if boss_id is not None and sender.id != boss_id:
        ensure_batrakan(chat.id, sender.id, user_display_name(sender))

    # Only the boss's replies can adjust scores.
    if boss_id is None or sender.id != boss_id:
        return

    if not message.reply_to_message:
        return

    target_user = message.reply_to_message.from_user
    if target_user is None or target_user.is_bot:
        return

    if target_user.id == boss_id:
        await message.reply_text("Начальнік не може змінювати власний приробіток 🙂")
        return

    delta = parse_delta(message.text)
    if delta is None:
        return

    new_score = adjust_score(chat.id, target_user.id, user_display_name(target_user), delta)

    sign = "+" if delta > 0 else ""
    await message.reply_text(
        f"{user_display_name(target_user)}: {sign}{delta} приробітку → тепер {new_score}"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def check_privacy_mode(application: Application) -> None:
    if not application.bot.bot.can_read_all_group_messages:
        logger.warning(
            "Privacy Mode is ENABLED: the bot only sees commands in groups, so "
            "'+приробіток' replies will be ignored. Disable it via @BotFather "
            "(/setprivacy -> Disable), then remove the bot from the group and add it back."
        )


def build_app(token: str) -> Application:
    init_db()
    application = (
        Application.builder().token(token).post_init(check_privacy_mode).build()
    )

    application.add_handler(CommandHandler("start_boss", start_boss))
    application.add_handler(CommandHandler("pryrobitok", show_scores))
    application.add_handler(CommandHandler("myscore", my_score))
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