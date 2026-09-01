# FunPay delivery and review bot

Бот выдаёт нужную ссылку после оплаты, а после подтверждения заказа просит о честном отзыве. Обработанные заказы хранятся в `/app/data/orders.db`, поэтому двойная выдача блокируется. Тестовые команды и постоянный опрос чата удалены.

## Один товар

```text
DOWNLOAD_URL=https://example.com/item-scroller.zip
PRODUCTS_JSON=
```

## Несколько товаров

В конец описания каждого лота добавьте свой уникальный маркер, например:

```text
[ITEM_SCROLLER_1165]
```

Второй лот должен иметь другой маркер, например `[APPLE_SKIN_1165]`. Затем задайте в Bothost одну переменную `PRODUCTS_JSON` в одну строку:

```json
{"[ITEM_SCROLLER_1165]":{"name":"Item Scroller 1.16.5","url":"https://example.com/item-scroller.zip"},"[APPLE_SKIN_1165]":{"name":"AppleSkin 1.16.5","url":"https://example.com/appleskin.zip"}}
```

Бот ищет маркер в описании оплаченного заказа. Если маркер не найден или найдено несколько, бот не выдаст неправильный товар и напишет ошибку в лог.

Для услуг вместо `url` укажите встроенный тип `service`: `custom_mod`, `plugin`, `translation` или `crash_report`.

```json
{"[SERVICE_CUSTOM_MOD]":{"name":"Разработка мода","service":"custom_mod"}}
```

## Bothost

```text
FUNPAY_GOLDEN_KEY=<секретная cookie FunPay>
DRY_RUN=true
SEND_REVIEW_IMAGE=false
DATA_DIR=/app/data
POLL_DELAY=6
AUTH_RETRY_DELAY=900
```

С `DRY_RUN=true` бот только пишет обнаруженную покупку в лог. Для реальной выдачи поставьте `DRY_RUN=false` и выполните новый деплой.
Бот напрямую проверяет страницу «Мои продажи» с интервалом `POLL_DELAY`. После перезапуска он подхватывает активные оплаченные заказы, но не отправляет сообщения по старым закрытым заказам.

Не сохраняйте `golden_key`, Telegram-токен и рабочие ссылки в GitHub. Бот использует неофициальную FunPayAPI.
