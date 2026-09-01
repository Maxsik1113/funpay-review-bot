from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from FunPayAPI import Account, enums, exceptions


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
POLL_DELAY = max(3.0, float(os.getenv("POLL_DELAY", "6")))
AUTH_RETRY_DELAY = max(60.0, float(os.getenv("AUTH_RETRY_DELAY", "900")))
DRY_RUN = os.getenv("DRY_RUN", "false").lower() in {"1", "true", "yes", "on"}
DOWNLOAD_URL = os.getenv("DOWNLOAD_URL", "").strip()
PRODUCTS_JSON = os.getenv("PRODUCTS_JSON", "").strip()
SEND_REVIEW_IMAGE = os.getenv("SEND_REVIEW_IMAGE", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}

ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = Path("/app/data") if Path("/app").exists() else ROOT_DIR / "data"
DATA_DIR = Path(os.getenv("DATA_DIR", str(DEFAULT_DATA_DIR)))
DB_PATH = DATA_DIR / "orders.db"
REVIEW_IMAGE_PATH = Path(os.getenv("REVIEW_IMAGE_PATH", str(ROOT_DIR / "review.png")))

ORDER_ID_PATTERN = re.compile(r"#([A-Za-z0-9]+)")

DEFAULT_DELIVERY_MESSAGE = """📦 {buyer}, спасибо за покупку «{product}»!

Скачать товар можно по ссылке:
{download_url}

Если появятся вопросы, напишите мне в этом чате."""

DEFAULT_REVIEW_MESSAGE = """⭐ {buyer}, спасибо за подтверждение заказа #{order_id}!

Если покупка вам понравилась, буду благодарен за честный отзыв 😊
Оставить его можно на странице заказа:
https://funpay.com/orders/{order_id}/"""

SERVICE_MESSAGES = {
    "custom_mod": """🛠️ {buyer}, спасибо за заказ «{product}»!

Чтобы я мог оценить и начать работу, ответьте, пожалуйста:
1. Какая версия Minecraft?
2. Какой загрузчик: Forge, Fabric или NeoForge?
3. Мод должен работать на клиенте, сервере или обеих сторонах?
4. Подробно опишите нужный функционал.
5. Есть ли примеры, референсы или готовое ТЗ?
6. Какой желаемый срок?""",
    "plugin": """🧩 {buyer}, спасибо за заказ «{product}»!

Пришлите, пожалуйста:
1. Версию Minecraft.
2. Ядро сервера: Paper, Spigot, Bukkit или Purpur.
3. Подробное описание команд, механик и GUI.
4. Нужные права, плейсхолдеры и интеграции.
5. Нужна ли база данных?
6. Желаемый срок.""",
    "translation": """🌐 {buyer}, спасибо за заказ «{product}»!

Для начала работы укажите:
1. Название и версию мода или плагина.
2. Исходный и требуемый язык.
3. Количество файлов или примерный объём.
4. Нужно ли сохранить цвета, эмодзи и фирменный стиль.
5. Прикрепите файлы для перевода.
6. Укажите желаемый срок.""",
    "crash_report": """🔍 {buyer}, спасибо за заказ «{product}»!

Для диагностики пришлите:
1. Версию Minecraft.
2. Загрузчик и его версию.
3. Полный crash-report или latest.log файлом.
4. На каком этапе происходит вылет?
5. Что изменилось перед появлением ошибки?""",
}


logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("funpay-review-bot")


@dataclass(frozen=True)
class ProductRule:
    key: str
    markers: tuple[str, ...]
    name: str
    download_url: str
    service: str


def normalize(value: str | None) -> str:
    return unicodedata.normalize("NFKC", value or "").strip().casefold()


def load_product_rules(raw: str) -> list[ProductRule]:
    if not raw:
        return []

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"PRODUCTS_JSON содержит ошибку JSON: {exc}") from exc

    if not isinstance(data, dict) or not data:
        raise RuntimeError("PRODUCTS_JSON должен быть непустым JSON-объектом")

    rules = []
    for key, config in data.items():
        key = str(key).strip()
        if isinstance(config, str):
            name = key
            download_url = config.strip()
            service = ""
            markers = (key,)
        elif isinstance(config, dict):
            name = str(config.get("name") or key).strip()
            download_url = str(config.get("url") or "").strip()
            service = str(config.get("service") or "").strip()
            raw_markers = config.get("markers", [key])
            if not isinstance(raw_markers, list):
                raise RuntimeError(f"markers для {key!r} должен быть JSON-массивом")
            markers = tuple(
                str(item).strip() for item in raw_markers if str(item).strip()
            )
        else:
            raise RuntimeError(
                f"Товар {key!r} в PRODUCTS_JSON должен быть ссылкой или объектом"
            )

        if service and service not in SERVICE_MESSAGES:
            raise RuntimeError(f"Неизвестный тип услуги {service!r} для {key!r}")
        if not key or not markers or not (download_url or service):
            raise RuntimeError(
                "Каждой записи PRODUCTS_JSON нужны markers и url или service"
            )
        rules.append(ProductRule(key, markers, name, download_url, service))
    return rules


def select_product(description: str, rules: list[ProductRule]) -> ProductRule | None:
    normalized_description = normalize(description)
    matches = [
        rule
        for rule in rules
        if any(normalize(marker) in normalized_description for marker in rule.markers)
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        names = ", ".join(rule.key for rule in matches)
        raise RuntimeError(f"В описании заказа найдено несколько маркеров: {names}")
    return None


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


def get_delivery_product(
    account: Account,
    order_id: str,
    product_rules: list[ProductRule],
    description: str | None = None,
) -> ProductRule | None:
    if not product_rules:
        return ProductRule("", ("",), "товар", DOWNLOAD_URL, "") if DOWNLOAD_URL else None

    if description is None:
        description = account.get_order_shortcut(order_id).description

    product = select_product(description, product_rules)
    if product is None:
        logger.error(
            "Заказ #%s не выдан: в описании нет маркера из PRODUCTS_JSON. "
            "Описание заказа: %r",
            order_id,
            description[:300],
        )
        return None
    return product


def handle_purchase(
    account: Account,
    connection: sqlite3.Connection,
    order_id: str,
    buyer: str,
    chat_id: int | str,
    product_rules: list[ProductRule],
    description: str | None = None,
) -> bool:
    event_key = f"purchase:{order_id}"
    if was_processed(connection, event_key):
        return True

    delivery = get_delivery_product(account, order_id, product_rules, description)
    if delivery is None:
        return False

    if delivery.service:
        text = SERVICE_MESSAGES[delivery.service].format(
            buyer=buyer,
            order_id=order_id,
            product=delivery.name,
            download_url=delivery.download_url,
        )
    else:
        text = read_template("delivery_message.txt", DEFAULT_DELIVERY_MESSAGE).format(
            buyer=buyer,
            order_id=order_id,
            product=delivery.name,
            download_url=delivery.download_url,
        )

    if DRY_RUN:
        logger.info(
            "DRY_RUN: автовыдача «%s» для %s, заказ #%s",
            delivery.name,
            buyer,
            order_id,
        )
        return True

    send_with_retries(
        f"автовыдача заказа #{order_id}",
        lambda: account.send_message(chat_id, text),
    )
    mark_processed(connection, event_key, order_id, "purchase", buyer)
    logger.info(
        "Сообщение по «%s» отправлено %s по заказу #%s",
        delivery.name,
        buyer,
        order_id,
    )
    return True


def handle_confirmation(
    account: Account,
    connection: sqlite3.Connection,
    order_id: str,
    buyer: str,
    chat_id: int | str,
) -> None:
    event_key = f"confirmation:{order_id}"
    if was_processed(connection, event_key):
        return

    text = read_template("review_message.txt", DEFAULT_REVIEW_MESSAGE).format(
        buyer=buyer,
        order_id=order_id,
        download_url=DOWNLOAD_URL,
        product="товар",
    )

    if DRY_RUN:
        logger.info("DRY_RUN: благодарность для %s, заказ #%s", buyer, order_id)
        return

    send_with_retries(
        f"сообщение после подтверждения заказа #{order_id}",
        lambda: account.send_message(chat_id, text),
    )

    if SEND_REVIEW_IMAGE:
        if REVIEW_IMAGE_PATH.exists():
            send_with_retries(
                f"картинка после подтверждения заказа #{order_id}",
                lambda: account.send_image(chat_id, str(REVIEW_IMAGE_PATH)),
            )
        else:
            logger.warning("Картинка %s не найдена — отправлен только текст", REVIEW_IMAGE_PATH)

    mark_processed(connection, event_key, order_id, "confirmation", buyer)
    logger.info("Благодарность отправлена %s по заказу #%s", buyer, order_id)


def process_order_event(
    account: Account,
    connection: sqlite3.Connection,
    order,
    product_rules: list[ProductRule],
) -> None:
    if order.status in {enums.OrderStatuses.PAID, enums.OrderStatuses.CLOSED}:
        handle_purchase(
            account,
            connection,
            order.id,
            order.buyer_username or "покупатель",
            order.chat_id,
            product_rules,
            order.description,
        )
    if order.status is enums.OrderStatuses.CLOSED:
        handle_confirmation(
            account,
            connection,
            order.id,
            order.buyer_username or "покупатель",
            order.chat_id,
        )


def run() -> None:
    golden_key = os.getenv("FUNPAY_GOLDEN_KEY", "").strip()
    if not golden_key:
        raise RuntimeError("Не задана переменная окружения FUNPAY_GOLDEN_KEY")

    product_rules = load_product_rules(PRODUCTS_JSON)
    connection = init_database()
    try:
        account = Account(golden_key).get()
        logger.info("Авторизация выполнена: %s", account.username)
        if DRY_RUN:
            logger.warning(
                "DRY_RUN включён: бот обнаруживает заказы, но ничего не отправляет"
            )
        else:
            logger.info("Автовыдача включена")
        if product_rules:
            logger.info("Загружено товаров: %s", len(product_rules))
        elif DOWNLOAD_URL:
            logger.info("Используется один товар из DOWNLOAD_URL")
        else:
            logger.warning("Не заданы PRODUCTS_JSON и DOWNLOAD_URL: автовыдача отключена")

        last_session_refresh = time.monotonic()
        last_sales_summary: tuple[int, int] | None = None
        unmatched_orders: set[str] = set()
        logger.info("Запущена прямая проверка продаж каждые %.0f сек.", POLL_DELAY)

        while True:
            if time.monotonic() - last_session_refresh >= 45 * 60:
                account.get()
                last_session_refresh = time.monotonic()
                logger.info("Сессия FunPay обновлена")

            try:
                sales = account.get_sales(include_refunded=False)[1]
            except exceptions.UnauthorizedError:
                raise
            except Exception:
                logger.exception(
                    "Не удалось проверить страницу «Мои продажи»; повтор через %.0f сек.",
                    POLL_DELAY,
                )
                time.sleep(POLL_DELAY)
                continue

            paid_count = sum(
                order.status is enums.OrderStatuses.PAID for order in sales
            )
            sales_summary = (len(sales), paid_count)
            if sales_summary != last_sales_summary:
                logger.info(
                    "Проверка продаж: найдено заказов — %s, активных оплаченных — %s",
                    len(sales),
                    paid_count,
                )
                last_sales_summary = sales_summary

            for order in sales:
                try:
                    if order.status is enums.OrderStatuses.PAID:
                        if order.id in unmatched_orders:
                            continue
                        matched = handle_purchase(
                            account,
                            connection,
                            order.id,
                            order.buyer_username or "покупатель",
                            order.chat_id,
                            product_rules,
                            order.description,
                        )
                        if not matched:
                            unmatched_orders.add(order.id)
                    elif (
                        order.status is enums.OrderStatuses.CLOSED
                        and was_processed(connection, f"purchase:{order.id}")
                    ):
                        handle_confirmation(
                            account,
                            connection,
                            order.id,
                            order.buyer_username or "покупатель",
                            order.chat_id,
                        )
                except Exception:
                    logger.exception("Не удалось обработать заказ #%s", order.id)

            time.sleep(POLL_DELAY)
    finally:
        connection.close()


def main() -> None:
    while True:
        try:
            run()
        except KeyboardInterrupt:
            logger.info("Бот остановлен")
            return
        except exceptions.UnauthorizedError:
            logger.error(
                "Авторизация FunPay отклонена: FUNPAY_GOLDEN_KEY недействителен "
                "или истёк. Обновите ключ в Bothost и перезапустите бота. "
                "Следующая автоматическая попытка через %.0f минут.",
                AUTH_RETRY_DELAY / 60,
            )
            time.sleep(AUTH_RETRY_DELAY)
        except Exception:
            logger.exception("Бот остановился с ошибкой; повторный запуск через 30 секунд")
            time.sleep(30)


if __name__ == "__main__":
    main()
