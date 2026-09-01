from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from pathlib import Path

from FunPayAPI import Account, Runner, enums


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
POLL_DELAY = float(os.getenv("POLL_DELAY", "6"))
DRY_RUN = os.getenv("DRY_RUN", "false").lower() in {"1", "true", "yes", "on"}
DOWNLOAD_URL = os.getenv("DOWNLOAD_URL", "").strip()
SEND_REVIEW_IMAGE = os.getenv("SEND_REVIEW_IMAGE", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
TEST_ALLOWED_USER = os.getenv("TEST_ALLOWED_USER", "").strip()
TEST_COMMAND = os.getenv("TEST_COMMAND", "").strip()

ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = Path("/app/data") if Path("/app").exists() else ROOT_DIR / "data"
DATA_DIR = Path(os.getenv("DATA_DIR", str(DEFAULT_DATA_DIR)))
DB_PATH = DATA_DIR / "orders.db"
REVIEW_IMAGE_PATH = Path(os.getenv("REVIEW_IMAGE_PATH", str(ROOT_DIR / "review.png")))

ORDER_ID_PATTERN = re.compile(r"#([A-Za-z0-9]+)")

DEFAULT_DELIVERY_MESSAGE = """📦 {buyer}, спасибо за покупку!

Скачать мод можно по ссылке:
{download_url}

Если появятся вопросы, напишите мне в этом чате."""

DEFAULT_REVIEW_MESSAGE = """⭐ {buyer}, спасибо за подтверждение заказа #{order_id}!

Если покупка вам понравилась, буду благодарен за честный отзыв 😊
Оставить его можно на странице заказа:
https://funpay.com/orders/{order_id}/"""

TEST_REPLY_MESSAGE = """✅ {buyer}, тестовая команда получена.

Бот видит входящие сообщения и умеет отвечать. Автовыдача, ссылка на товар и запрос отзыва не запускались."""


logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("funpay-review-bot")


def read_template(filename: str, fallback: str) -> str:
    path = ROOT_DIR / filename
    if not path.exists():
        return fallback
    return path.read_text(encoding="utf-8").strip()


def init_database() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_events (
            event_key TEXT PRIMARY KEY,
            order_id TEXT NOT NULL,
            event_name TEXT NOT NULL,
            buyer TEXT NOT NULL,
            processed_at INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_test_messages (
            message_id INTEGER NOT NULL,
            chat_id TEXT NOT NULL,
            processed_at INTEGER NOT NULL,
            PRIMARY KEY (message_id, chat_id)
        )
        """
    )
    connection.commit()
    return connection


def was_processed(connection: sqlite3.Connection, event_key: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM processed_events WHERE event_key = ?", (event_key,)
    ).fetchone()
    return row is not None


def mark_processed(
    connection: sqlite3.Connection,
    event_key: str,
    order_id: str,
    event_name: str,
    buyer: str,
) -> None:
    connection.execute(
        """
        INSERT OR IGNORE INTO processed_events
            (event_key, order_id, event_name, buyer, processed_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (event_key, order_id, event_name, buyer, int(time.time())),
    )
    connection.commit()


def was_test_message_processed(
    connection: sqlite3.Connection, message_id: int, chat_id: int | str
) -> bool:
    row = connection.execute(
        """
        SELECT 1 FROM processed_test_messages
        WHERE message_id = ? AND chat_id = ?
        """,
        (message_id, str(chat_id)),
    ).fetchone()
    return row is not None


def mark_test_message_processed(
    connection: sqlite3.Connection, message_id: int, chat_id: int | str
) -> None:
    connection.execute(
        """
        INSERT OR IGNORE INTO processed_test_messages
            (message_id, chat_id, processed_at)
        VALUES (?, ?, ?)
        """,
        (message_id, str(chat_id), int(time.time())),
    )
    connection.commit()


def extract_order_id(text: str | None) -> str | None:
    if not text:
        return None
    match = ORDER_ID_PATTERN.search(text)
    return match.group(1) if match else None


def send_with_retries(action_name: str, action, attempts: int = 3) -> None:
    for attempt in range(1, attempts + 1):
        try:
            action()
            return
        except Exception:
            logger.exception(
                "%s: попытка %s/%s завершилась ошибкой",
                action_name,
                attempt,
                attempts,
            )
            if attempt < attempts:
                time.sleep(5 * attempt)
    raise RuntimeError(f"Не удалось выполнить действие: {action_name}")


def handle_test_input(
    account: Account,
    connection: sqlite3.Connection,
    author: str,
    text: str | None,
    message_id: int,
    chat_id: int | str,
) -> None:
    if not TEST_ALLOWED_USER or not TEST_COMMAND:
        return

    author = author.strip()
    if author.casefold() != TEST_ALLOWED_USER.casefold():
        return
    if (text or "").strip() != TEST_COMMAND:
        return
    if was_test_message_processed(connection, message_id, chat_id):
        return

    reply = TEST_REPLY_MESSAGE.format(buyer=author)
    send_with_retries(
        "ответ на тестовую команду",
        lambda: account.send_message(chat_id, reply),
    )
    mark_test_message_processed(connection, message_id, chat_id)
    logger.info("Тестовая команда обработана для %s", author)


def handle_test_command(
    account: Account, connection: sqlite3.Connection, message
) -> None:
    handle_test_input(
        account,
        connection,
        message.author or message.chat_name or "",
        message.text,
        message.id,
        message.chat_id,
    )


def handle_test_chat_update(
    account: Account, connection: sqlite3.Connection, chat
) -> None:
    if chat.last_by_bot or chat.last_by_vertex:
        return
    handle_test_input(
        account,
        connection,
        chat.name or "",
        chat.last_message_text,
        chat.node_msg_id,
        chat.id,
    )


def handle_purchase(
    account: Account,
    connection: sqlite3.Connection,
    message,
    order_id: str,
    buyer: str,
) -> None:
    if not DOWNLOAD_URL:
        logger.info(
            "Заказ #%s оплачен, но DOWNLOAD_URL не задан — автовыдача пропущена",
            order_id,
        )
        return

    event_key = f"purchase:{order_id}"
    if was_processed(connection, event_key):
        return

    text = read_template("delivery_message.txt", DEFAULT_DELIVERY_MESSAGE).format(
        buyer=buyer,
        order_id=order_id,
        download_url=DOWNLOAD_URL,
    )

    if DRY_RUN:
        logger.info("DRY_RUN: автовыдача для %s, заказ #%s", buyer, order_id)
        return

    send_with_retries(
        f"автовыдача заказа #{order_id}",
        lambda: account.send_message(message.chat_id, text),
    )
    mark_processed(connection, event_key, order_id, "purchase", buyer)
    logger.info("Ссылка на товар отправлена %s по заказу #%s", buyer, order_id)


def handle_confirmation(
    account: Account,
    connection: sqlite3.Connection,
    message,
    order_id: str,
    buyer: str,
) -> None:
    event_key = f"confirmation:{order_id}"
    if was_processed(connection, event_key):
        return

    text = read_template("review_message.txt", DEFAULT_REVIEW_MESSAGE).format(
        buyer=buyer,
        order_id=order_id,
        download_url=DOWNLOAD_URL,
    )

    if DRY_RUN:
        logger.info("DRY_RUN: благодарность для %s, заказ #%s", buyer, order_id)
        return

    send_with_retries(
        f"сообщение после подтверждения заказа #{order_id}",
        lambda: account.send_message(message.chat_id, text),
    )

    if SEND_REVIEW_IMAGE:
        if REVIEW_IMAGE_PATH.exists():
            send_with_retries(
                f"картинка после подтверждения заказа #{order_id}",
                lambda: account.send_image(message.chat_id, str(REVIEW_IMAGE_PATH)),
            )
        else:
            logger.warning(
                "Картинка %s не найдена — отправлен только текст", REVIEW_IMAGE_PATH
            )

    mark_processed(connection, event_key, order_id, "confirmation", buyer)
    logger.info("Благодарность отправлена %s по заказу #%s", buyer, order_id)


def run() -> None:
    golden_key = os.getenv("FUNPAY_GOLDEN_KEY", "").strip()
    if not golden_key:
        raise RuntimeError("Не задана переменная окружения FUNPAY_GOLDEN_KEY")

    connection = init_database()
    account = Account(golden_key).get()
    logger.info("Авторизация выполнена: %s", account.username)
    logger.info("Режим проверки без отправки: %s", DRY_RUN)
    if TEST_ALLOWED_USER and TEST_COMMAND:
        logger.info("Тестовая команда включена для %s", TEST_ALLOWED_USER)
    elif TEST_ALLOWED_USER or TEST_COMMAND:
        logger.warning(
            "Тестовая команда отключена: нужны TEST_ALLOWED_USER и TEST_COMMAND"
        )

    runner = Runner(account)
    confirmed_types = {enums.MessageTypes.ORDER_CONFIRMED}
    confirmed_by_admin = getattr(
        enums.MessageTypes, "ORDER_CONFIRMED_BY_ADMIN", None
    )
    if confirmed_by_admin is not None:
        confirmed_types.add(confirmed_by_admin)

    last_session_refresh = time.monotonic()

    for event in runner.listen(requests_delay=POLL_DELAY):
        if time.monotonic() - last_session_refresh >= 45 * 60:
            account.get()
            last_session_refresh = time.monotonic()
            logger.info("Сессия FunPay обновлена")

        if event.type in {
            enums.EventTypes.INITIAL_CHAT,
            enums.EventTypes.LAST_CHAT_MESSAGE_CHANGED,
        }:
            try:
                handle_test_chat_update(account, connection, event.chat)
            except Exception:
                logger.exception("Не удалось обработать обновление тестового чата")
            continue

        if event.type is not enums.EventTypes.NEW_MESSAGE:
            continue

        message = event.message
        if message.author_id != 0:
            try:
                handle_test_command(account, connection, message)
            except Exception:
                logger.exception("Не удалось обработать тестовую команду")
            continue

        order_id = extract_order_id(message.text)
        if not order_id:
            continue

        buyer = message.chat_name or "покупатель"

        try:
            if message.type is enums.MessageTypes.ORDER_PURCHASED:
                handle_purchase(account, connection, message, order_id, buyer)
            elif message.type in confirmed_types:
                handle_confirmation(account, connection, message, order_id, buyer)
        except Exception:
            logger.exception("Не удалось обработать заказ #%s", order_id)


def main() -> None:
    while True:
        try:
            run()
        except KeyboardInterrupt:
            logger.info("Бот остановлен")
            return
        except Exception:
            logger.exception("Бот остановился с ошибкой; повторный запуск через 30 секунд")
            time.sleep(30)


if __name__ == "__main__":
    main()
