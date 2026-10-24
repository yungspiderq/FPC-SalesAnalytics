# 📊 SalesAnalytics — статистика продаж

Плагин для [FunPay Cardinal](https://github.com/sidor0912/FunPayCardinal) **v0.1.17+**

**Автор:** yungspider

Считать продажи руками по выгрузкам FunPay — занятие на вечер. Плагин считает всё сам,
в фоне: заказы, выручку, средний чек, возвраты, отзывы и топ-лоты — и присылает
сводку в Telegram по команде и каждое утро в заданное время.

## Возможности

- 📊 **Заказы, количество товара, выручка по каждой валюте** и средний чек за любой период.
- ✅ **Подтверждения заказов** и ↩️ **возвраты** (о каждом возврате — отдельное уведомление).
- ⭐ **Отзывы покупателей** — считаются по системным сообщениям FunPay, дубли отбрасываются.
- 🏆 **Топ лотов** по числу заказов и выручке: имя берётся из секции `auto_delivery.cfg`,
  которую нашёл Кардинал, а не из «сырого» описания заказа.
- 📅 **Ежедневная сводка** за прошедшие сутки в заданное время (`reportTime`).
- 🧮 Тест автовыдачи (`ADTEST`) в статистику не попадает, повторная обработка заказа исключена.
- 💾 История ограничена (400 дней / 5000 заказов) — файл не разрастается, запись атомарная.

## Установка

```bash
cd /home/fpc/FunPayCardinal
wget https://raw.githubusercontent.com/yungspiderq/FPC-SalesAnalytics/main/sales_analytics.py -O plugins/sales_analytics.py
sudo systemctl restart FunPayCardinal@fpc
```

или отправьте файл боту в Telegram → `🔌 Плагины` → `➕ Добавить плагин`.

## Использование

Команда **`/stats`**:

| Вариант | Период |
|---|---|
| `/stats` | последние `defaultPeriod` дней (по умолчанию 7) |
| `/stats today` | только сегодня |
| `/stats 30` | последние 30 дней |
| `/stats all` | вся история |

### Пример ответа

```text
📊 Статистика продаж — за последние 7 дн.

💰 Выручка: 6 100.00 ₽ + 84.20 $
🛒 Заказов: 5 (товара: 7 шт.)
📈 Средний чек: 1 220.00 ₽
✅ Подтверждено: 1  |  ⭐ Отзывов: 3
↩️ Возвратов: 1 (700.00 ₽)

🏆 Топ лоты:
1. Аккаунт Steam | Level 10+ — 3 зак. / 5 400.00 ₽
2. Ключи PUBG — 1 зак. / 700.00 ₽
3. Золото WoW 1000к — 1 зак. / 84.20 $

📅 Последние дни с продажами:
• 24.10 — 5 зак. / 6 100.00 ₽ + 84.20 $
```

## Настройки

Файл `storage/plugins/sales_analytics.cfg` создаётся при первом запуске и **перечитывается на лету** — перезапуск бота не нужен.

| Параметр | По умолчанию | Что делает |
|---|---|---|
| `enabled` | `1` | собирать ли статистику |
| `dailyReport` | `1` | присылать ли ежедневную сводку |
| `reportTime` | `09:00` | время сводки, ЧЧ:ММ по времени сервера |
| `defaultPeriod` | `7` | период для `/stats` без аргумента, дней |
| `topLots` | `5` | сколько строк в топе лотов |
| `notifyRefunds` | `1` | уведомление о каждом возврате |

## Важно знать

- Выручка считается **в валютах заказа и не конвертируется** — ничего не округляется в вашу
  пользу: смешанные продажи видно как `6 100.00 ₽ + 84.20 $`.
- Заказ относится к тому дню, когда он **создан** (`order.date`), а подтверждение/возврат
  попадают в тот же день — статистика не «размазывается».
- Заказы, созданные до установки плагина, в выручку не попадут (их просто не было в истории),
  но подтверждения и возвраты по ним учитываются.
- Все уведомления идут в те же чаты Telegram, что и уведомления Кардинала (тип «прочие»);
  возвраты можно выключить (`notifyRefunds: 0`).
- Настройки: `storage/plugins/sales_analytics.cfg`, данные: `storage/plugins/sales_analytics.json`.

## Состав репозитория

- `sales_analytics.py` — плагин (класть в `plugins/`).
- `tests_plugin_sales_analytics.py` — функциональные тесты на моках (47 проверок: метаданные и загрузка
  через `Cardinal.load_plugin()`, все хэндлеры событий, команды Telegram, фоновые потоки и выход
  по смене `run_id`, алерты, cooldown'ы, дедупликация событий, удаление плагина).
- `tests_integration_sales_analytics.py` — интеграционные тесты (27 проверок): плагин грузится
  **настоящим** `Cardinal` и проходит боевой путь `load_plugins()` → `add_handlers()` →
  `run_handlers()` → `toggle_plugin()`, плюс утверждение «в логах бота ноль ошибок».
  Запуск из корня FunPayCardinal:
  ```bash
  cp tests_plugin_sales_analytics.py tests_integration_sales_analytics.py /path/to/FunPayCardinal/
  cd /path/to/FunPayCardinal && pip install -r requirements.txt
  python3 tests_plugin_sales_analytics.py && python3 tests_integration_sales_analytics.py
  ```
  Тесты сами найдут Кардинал и в другом месте: `FPC_PATH=/opt/FunPayCardinal python3 tests_plugin_sales_analytics.py`.
  Сетевые объекты подменены заглушками — реальных запросов к FunPay и Telegram тесты не делают.
- `LICENSE` — MIT.

### Другие плагины автора

* [FPC-DailyDigest](https://github.com/yungspiderq/FPC-DailyDigest) — утренняя сводка магазина (продажи, активные заказы, остатки, баланс) одним сообщением.
* [FPC-SalesExport](https://github.com/yungspiderq/FPC-SalesExport) — экспорт истории продаж в CSV.
* [FPC-BalanceGuard](https://github.com/yungspiderq/FPC-BalanceGuard) — контроль баланса, [FPC-HealthMonitor](https://github.com/yungspiderq/FPC-HealthMonitor) — диагностика бота, [FPC-StockWatcher](https://github.com/yungspiderq/FPC-StockWatcher) — остатки автовыдачи.
