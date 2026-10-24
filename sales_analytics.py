"""
SalesAnalytics — статистика продаж (плагин для FunPay Cardinal v0.1.17+)

📊 Сколько заработано вчера и какие лоты кормят магазин, обычно видно только
в выгрузках FunPay. Плагин считает продажи сам, в фоне, и присылает сводку
в Telegram по команде и каждое утро в заданное время.

Возможности:
 • Заказы, количество товара, выручка по каждой валюте, средний чек.
 • Подтверждения заказов, возвраты (с отдельным уведомлением) и отзывы покупателей.
 • Топ лотов: имя берётся из секции auto_delivery.cfg, которую нашёл Кардинал.
 • История ограничена (400 дней / 5000 заказов) — файл не разрастается.
 • Тест автовыдачи (ADTEST) и повторные события в статистику не попадают.
 • Выручка не конвертируется: видно реальные суммы в валютах заказа.

Команды Telegram:
 • /stats — сводка продаж: /stats today, /stats 30, /stats all.

Установка: положить этот файл в папку plugins/ и перезапустить Кардинал.
Настройки: storage/plugins/sales_analytics.cfg (перечитываются на лету).
Данные: storage/plugins/sales_analytics.json (запись атомарная).
Автор: yungspider
"""

from __future__ import annotations

import configparser
import datetime
import json
import logging
import os
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cardinal import Cardinal

from FunPayAPI import types
from FunPayAPI.updater.events import (NewOrderEvent, OrderStatusChangedEvent, NewMessageEvent,
                                      LastChatMessageChangedEvent)

from telebot.types import Message
from tg_bot import utils as tg_utils
from locales.localizer import Localizer

logger = logging.getLogger("FPC.SalesAnalytics")
localizer = Localizer()
_ = localizer.translate

# ═══════════════════════════════════ МЕТАДАННЫЕ ПЛАГИНА ═══════════════════════════════════
NAME = "SalesAnalytics (Sales Statistics & Daily Digest)"
VERSION = "1.1.0"
DESCRIPTION = ("Статистика продаж магазина: заказы, выручка по каждой валюте, средний чек, возвраты, отзывы " \
               "покупателей и топ-лоты. Ежедневная сводка за сутки в заданное время. Настройки: " \
               "storage/plugins/sales_analytics.cfg. Команда: /stats")
CREDITS = "yungspider"
UUID = "7756ed70-4ec6-4bac-a6a1-b2cb14cb9a31"
SETTINGS_PAGE = False

# ═══════════════════════════════════ НАСТРОЙКИ ПЛАГИНА ═══════════════════════════════════
STORAGE_FILE = "storage/plugins/sales_analytics.json"
CONFIG_FILE = "storage/plugins/sales_analytics.cfg"

MAX_ORDER_RECORDS = 5000
"""Макс. кол-во записей о заказах в файле (самые старые удаляются)."""
MAX_DAYS = 400
"""Сколько дней истории хранить."""

CURRENCY_SYMBOLS = {"rub": "₽", "usd": "$", "eur": "€", "unknown": "¤"}

DEFAULT_CONFIG_TEXT = """[SalesAnalytics]
; Включен ли плагин (1 / 0).
enabled = 1
; Присылать ли ежедневную сводку за прошедшие сутки (1 / 0).
dailyReport = 1
; Во сколько присылать сводку (ЧЧ:ММ, время сервера).
reportTime = 09:00
; Период по умолчанию для команды /stats (дней).
defaultPeriod = 7
; Сколько строк в топе лотов показывать.
topLots = 5
; Присылать ли в Telegram уведомление о каждом возврате (1 / 0).
notifyRefunds = 1
"""

_lock = threading.RLock()
_data: dict = {}
_cfg: dict = {}
_cfg_mtime: float = -1.0
_last_report_date: str | None = None
_review_marks: set[str] = set()
_thread_started = False
_cardinal: Cardinal | None = None


# ═════════════════════════════════════════ КОНФИГ ═════════════════════════════════════════
def load_config() -> dict:
    """
    Читает конфиг плагина (создает файл с настройками по умолчанию, если его нет).
    Результат кэшируется и перечитывается при изменении файла на диске.

    :return: словарь настроек.
    """
    global _cfg, _cfg_mtime
    with _lock:
        try:
            mtime = os.path.getmtime(CONFIG_FILE) if os.path.exists(CONFIG_FILE) else 0.0
        except OSError:
            mtime = 0.0
        if _cfg and mtime == _cfg_mtime:
            return _cfg

        if not os.path.exists(CONFIG_FILE):
            try:
                os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
                with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                    f.write(DEFAULT_CONFIG_TEXT)
                mtime = os.path.getmtime(CONFIG_FILE)
                logger.info(f"[SalesAnalytics] Создан файл настроек $YELLOW{CONFIG_FILE}$RESET")
            except Exception:
                logger.error("[SalesAnalytics] Не удалось создать файл настроек.")
                logger.debug("TRACEBACK", exc_info=True)

        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(CONFIG_FILE, encoding="utf-8")
        except Exception:
            logger.error(f"[SalesAnalytics] Не удалось прочитать $YELLOW{CONFIG_FILE}$RESET, "
                         f"использую значения по умолчанию.")
            logger.debug("TRACEBACK", exc_info=True)
            parser = configparser.ConfigParser(interpolation=None)
        if "SalesAnalytics" not in parser:
            parser.read_string(DEFAULT_CONFIG_TEXT)
        section = parser["SalesAnalytics"]

        def get_bool(name: str, default: bool) -> bool:
            try:
                return section.getboolean(name, fallback=default)
            except Exception:
                return default

        def get_int(name: str, default: int) -> int:
            try:
                return int(section.get(name, str(default)))
            except (ValueError, TypeError):
                return default

        report_time = str(section.get("reportTime", "09:00")).strip()
        try:
            datetime.datetime.strptime(report_time, "%H:%M")
        except ValueError:
            report_time = "09:00"

        _cfg = {
            "enabled": get_bool("enabled", True),
            "daily_report": get_bool("dailyReport", True),
            "report_time": report_time,
            "default_period": min(max(get_int("defaultPeriod", 7), 1), MAX_DAYS),
            "top_lots": min(max(get_int("topLots", 5), 1), 20),
            "notify_refunds": get_bool("notifyRefunds", True),
        }
        _cfg_mtime = mtime
        return _cfg


# ═══════════════════════════════════════ ХРАНИЛИЩЕ ═══════════════════════════════════════
def load_data() -> dict:
    """Загружает накопленные данные из файла (или создает пустую структуру)."""
    global _data, _last_report_date
    with _lock:
        if _data:
            return _data
        data = {"version": 1, "days": {}, "orders": {}, "last_report": None}
        if os.path.exists(STORAGE_FILE):
            try:
                with open(STORAGE_FILE, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    data["days"] = raw.get("days") if isinstance(raw.get("days"), dict) else {}
                    data["orders"] = raw.get("orders") if isinstance(raw.get("orders"), dict) else {}
                    data["last_report"] = raw.get("last_report")
                logger.info(f"[SalesAnalytics] Загружена история: $YELLOW{len(data['days'])}$RESET дн., "
                            f"$YELLOW{len(data['orders'])}$RESET заказов.")
            except Exception:
                logger.error("[SalesAnalytics] Не удалось прочитать файл статистики, начинаю с чистой истории.")
                logger.debug("TRACEBACK", exc_info=True)
        _data = data
        _last_report_date = data.get("last_report")
        return _data


def save_data() -> None:
    """Атомарно сохраняет накопленные данные в файл."""
    with _lock:
        if not _data:
            return
        _data["last_report"] = _last_report_date
        try:
            os.makedirs(os.path.dirname(STORAGE_FILE), exist_ok=True)
            tmp_file = f"{STORAGE_FILE}.tmp"
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(_data, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp_file, STORAGE_FILE)
        except Exception:
            logger.error("[SalesAnalytics] Не удалось сохранить статистику продаж.")
            logger.debug("TRACEBACK", exc_info=True)


def get_day_bucket(data: dict, day_key: str) -> dict:
    """
    Возвращает (и при необходимости создает) структуру дня.

    :param data: объект с данными плагина.
    :param day_key: дата в формате ГГГГ-ММ-ДД.

    :return: словарь показателей дня.
    """
    if day_key not in data["days"]:
        data["days"][day_key] = {"orders": 0, "items": 0, "closed": 0, "refunds": 0, "reviews": 0,
                                 "revenue": {}, "refunded": {}, "lots": {}}
        while len(data["days"]) > MAX_DAYS:
            data["days"].pop(min(data["days"]), None)
    return data["days"][day_key]


def add_money(bucket: dict, key: str, currency: str, amount: float) -> None:
    """Прибавляет сумму к словарю {валюта: сумма} внутри bucket[key]."""
    if not amount:
        return
    bucket[key][currency] = round(bucket[key].get(currency, 0.0) + amount, 2)


def get_lot_bucket(day: dict, lot_name: str) -> dict:
    """Возвращает (и при необходимости создает) структуру лота внутри дня."""
    if lot_name not in day["lots"]:
        day["lots"][lot_name] = {"orders": 0, "items": 0, "revenue": {}}
    return day["lots"][lot_name]


def currency_code(currency) -> str:
    """Возвращает текстовый код валюты (rub / usd / eur / unknown)."""
    try:
        return currency.code
    except Exception:
        return "unknown"


def lot_name_of(order: types.OrderShortcut, event=None) -> str:
    """
    Определяет название лота для статистики.
    Если автовыдача нашла лот в конфиге - берется имя секции из auto_delivery.cfg,
    иначе - описание заказа (обрезанное).

    :param order: объект заказа.
    :param event: событие нового заказа (опционально).

    :return: название лота.
    """
    config_section_name = getattr(event, "config_section_name", None) if event is not None else None
    if config_section_name:
        return str(config_section_name)[:120]
    description = (order.description or "").strip()
    return description[:120] if description else "Без описания"


# ═════════════════════════════════════ ФОРМАТИРОВАНИЕ ═════════════════════════════════════
def format_money(revenue: dict) -> str:
    """Форматирует словарь {валюта: сумма} в строку вида «1 250.00 ₽ + 12.00 $»."""
    if not revenue:
        return "0"
    parts = []
    for cur in sorted(revenue, key=lambda c: revenue[c], reverse=True):
        value = float(revenue[cur])
        if not value:
            continue
        parts.append(f"{value:,.2f}".replace(",", " ") + f" {CURRENCY_SYMBOLS.get(cur, cur)}")
    return " + ".join(parts) if parts else "0"


def total_of(revenue: dict) -> float:
    """Сумма по всем валютам (используется только для сортировки)."""
    return sum(float(i) for i in revenue.values()) if revenue else 0.0


def aggregate(data: dict, day_keys: list[str]) -> dict:
    """
    Складывает показатели за указанные дни.

    :param data: объект с данными плагина.
    :param day_keys: список дат (ГГГГ-ММ-ДД).

    :return: сводный словарь показателей.
    """
    result = {"orders": 0, "items": 0, "closed": 0, "refunds": 0, "reviews": 0,
              "revenue": {}, "refunded": {}, "lots": {}}
    for key in day_keys:
        day = data["days"].get(key)
        if not day:
            continue
        for field in ("orders", "items", "closed", "refunds", "reviews"):
            result[field] += int(day.get(field, 0))
        for cur, value in day.get("revenue", {}).items():
            result["revenue"][cur] = round(result["revenue"].get(cur, 0.0) + float(value), 2)
        for cur, value in day.get("refunded", {}).items():
            result["refunded"][cur] = round(result["refunded"].get(cur, 0.0) + float(value), 2)
        for lot_name, lot in day.get("lots", {}).items():
            bucket = result["lots"].setdefault(lot_name, {"orders": 0, "items": 0, "revenue": {}})
            bucket["orders"] += int(lot.get("orders", 0))
            bucket["items"] += int(lot.get("items", 0))
            for cur, value in lot.get("revenue", {}).items():
                bucket["revenue"][cur] = round(bucket["revenue"].get(cur, 0.0) + float(value), 2)
    return result


def build_report_text(summary: dict, period_text: str, cfg: dict, daily_breakdown: bool = False) -> str:
    """
    Собирает HTML-текст отчета.

    :param summary: сводные показатели (:func:`aggregate`).
    :param period_text: подпись периода.
    :param cfg: настройки плагина.
    :param daily_breakdown: добавлять ли разбивку по дням.

    :return: текст для Telegram.
    """
    orders = summary["orders"]
    avg_text = "—"
    if orders and total_of(summary["revenue"]):
        main_currency = max(summary["revenue"], key=lambda c: summary["revenue"][c])
        avg_text = f"{summary['revenue'][main_currency] / orders:,.2f}".replace(",", " ") + \
                   f" {CURRENCY_SYMBOLS.get(main_currency, main_currency)}"

    lines = [
        f"📊 <b>Статистика продаж</b> — {tg_utils.escape(period_text)}",
        "",
        f"💰 Выручка: <b>{format_money(summary['revenue'])}</b>",
        f"🛒 Заказов: <b>{orders}</b> (товара: {summary['items']} шт.)",
        f"📈 Средний чек: <b>{avg_text}</b>",
        f"✅ Подтверждено: <b>{summary['closed']}</b>  |  ⭐ Отзывов: <b>{summary['reviews']}</b>",
        f"↩️ Возвратов: <b>{summary['refunds']}</b>"
        + (f" ({format_money(summary['refunded'])})" if total_of(summary["refunded"]) else ""),
    ]

    if summary["lots"]:
        top = sorted(summary["lots"].items(),
                     key=lambda item: (item[1]["orders"], total_of(item[1]["revenue"])), reverse=True)
        lines += ["", "🏆 <b>Топ лоты:</b>"]
        for index, (lot, stat) in enumerate(top[:cfg["top_lots"]], start=1):
            lines.append(f"{index}. <code>{tg_utils.escape(lot)}</code> — "
                         f"{stat['orders']} зак. / {format_money(stat['revenue'])}")

    if daily_breakdown:
        data = load_data()
        days_with_sales = [(k, v) for k, v in sorted(data["days"].items(), reverse=True)
                           if v.get("orders")][:7]
        if days_with_sales:
            lines += ["", "📅 <b>Последние дни с продажами:</b>"]
            for key, day in days_with_sales:
                lines.append(f"• {key[8:]}.{key[5:7]} — {day['orders']} зак. / {format_money(day['revenue'])}")

    if not orders:
        lines += ["", "<i>Продаж за выбранный период пока нет.</i>"]
    return "\n".join(lines)


def day_range(days: int | None = None, today: bool = False, all_time: bool = False) -> tuple[list[str], str]:
    """
    Формирует список дат и подпись периода.

    :param days: кол-во последних дней (включая сегодня).
    :param today: считать только сегодняшний день.
    :param all_time: считать всю историю.

    :return: (список дат, подпись периода).
    """
    data = load_data()
    now = datetime.datetime.now()
    if all_time:
        return sorted(data["days"].keys()), "за всё время"
    if today:
        return [now.strftime("%Y-%m-%d")], f"за сегодня ({now.strftime('%d.%m.%Y')})"
    days = days or load_config()["default_period"]
    keys = [(now - datetime.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]
    return sorted(keys), f"за последние {days} дн."


# ════════════════════════════════════ ХЭНДЛЕРЫ СОБЫТИЙ ════════════════════════════════════
def on_new_order(c: Cardinal, e: NewOrderEvent) -> None:
    """Учитывает новый заказ."""
    cfg = load_config()
    if not cfg["enabled"]:
        return
    order = getattr(e, "order", None)
    if not order or order.id == "ADTEST":  # тест автовыдачи не учитываем
        return

    data = load_data()
    with _lock:
        if order.id in data["orders"]:  # защита от повторной обработки
            return
        date_obj = order.date if isinstance(order.date, datetime.datetime) else datetime.datetime.now()
        day_key = date_obj.strftime("%Y-%m-%d")
        cur = currency_code(order.currency)
        amount = int(order.amount) if order.amount else 1
        price = float(order.price or 0)
        lot_name = lot_name_of(order, e)

        day = get_day_bucket(data, day_key)
        day["orders"] += 1
        day["items"] += amount
        add_money(day, "revenue", cur, price)
        lot = get_lot_bucket(day, lot_name)
        lot["orders"] += 1
        lot["items"] += amount
        add_money(lot, "revenue", cur, price)

        data["orders"][order.id] = {
            "t": day_key,
            "b": order.buyer_username,
            "d": lot_name,
            "p": price,
            "c": cur,
            "a": amount,
            "s": str(order.status.name) if order.status else "UNKNOWN",
            "delivered": False,
            "refunded": False,
            "closed": False,
        }
        while len(data["orders"]) > MAX_ORDER_RECORDS:
            data["orders"].pop(next(iter(data["orders"])))
    save_data()
    logger.info(f"[SalesAnalytics] Учтен заказ $YELLOW#{order.id}$RESET ({price} {order.currency}) -> {day_key}")


def on_order_status_changed(c: Cardinal, e: OrderStatusChangedEvent) -> None:
    """Учитывает подтверждение заказа / возврат."""
    cfg = load_config()
    if not cfg["enabled"]:
        return
    order = getattr(e, "order", None)
    if not order or order.id == "ADTEST":
        return

    data = load_data()
    refund = order.status in (types.OrderStatuses.REFUNDED, types.OrderStatuses.PARTIALLY_REFUNDED)
    closed = order.status is types.OrderStatuses.CLOSED

    with _lock:
        record = data["orders"].get(order.id)
        day_key = record["t"] if record else datetime.datetime.now().strftime("%Y-%m-%d")
        day = get_day_bucket(data, day_key)
        cur = currency_code(order.currency)

        if record:
            record["s"] = str(order.status.name) if order.status else "UNKNOWN"
            if closed and not record.get("closed"):
                day["closed"] += 1
                record["closed"] = True
            if refund and not record.get("refunded"):
                day["refunds"] += 1
                add_money(day, "refunded", cur, float(order.price or 0))
                record["refunded"] = True
        else:
            # заказ был создан до установки плагина - учитываем хотя бы факт события
            if closed:
                day["closed"] += 1
            if refund:
                day["refunds"] += 1
                add_money(day, "refunded", cur, float(order.price or 0))
    save_data()

    if refund and cfg["notify_refunds"] and c.telegram:
        text = (f"↩️ <b>Возврат по заказу</b> <code>{tg_utils.escape(order.id)}</code>\n"
                f"Покупатель: <a href=\"https://funpay.com/users/{order.buyer_id}/\">"
                f"{tg_utils.escape(order.buyer_username)}</a>\n"
                f"Сумма: <b>{format_money({cur: float(order.price or 0)})}</b>\n"
                f"Статус: <code>{tg_utils.escape(str(order.status.name) if order.status else '?')}</code>")
        threading.Thread(target=c.telegram.send_notification, args=(text,),
                         kwargs={"notification_type": tg_utils.NotificationTypes.other}, daemon=True).start()
    if refund:
        logger.info(f"[SalesAnalytics] Возврат по заказу $YELLOW#{order.id}$RESET")


def on_post_delivery(c: Cardinal, e: NewOrderEvent) -> None:
    """Отмечает заказ как выданный (после автовыдачи)."""
    cfg = load_config()
    if not cfg["enabled"]:
        return
    order = getattr(e, "order", None)
    if not order or order.id == "ADTEST":
        return
    data = load_data()
    with _lock:
        record = data["orders"].get(order.id)
        if record and not record.get("delivered") and getattr(e, "delivered", False):
            record["delivered"] = True
            record["goods"] = int(getattr(e, "goods_delivered", 0) or 0)
    save_data()


def on_new_message(c: Cardinal, e: NewMessageEvent | LastChatMessageChangedEvent) -> None:
    """Считает отзывы, оставленные покупателями."""
    cfg = load_config()
    if not cfg["enabled"]:
        return

    if isinstance(e, NewMessageEvent):
        obj = e.message
        message_type = getattr(obj, "type", None)
        i_am_buyer = bool(getattr(obj, "i_am_buyer", False))
        mark = f"{obj.chat_id}:{getattr(obj, 'id', 0)}"
    else:
        if not c.old_mode_enabled:
            return
        obj = e.chat
        message_type = getattr(obj, "last_message_type", None)
        i_am_buyer = False
        mark = f"{obj.id}:{getattr(obj, 'node_msg_id', 0)}:{getattr(obj, 'user_msg_id', 0)}"

    if message_type not in (types.MessageTypes.NEW_FEEDBACK, types.MessageTypes.FEEDBACK_CHANGED) or i_am_buyer:
        return

    with _lock:
        if mark in _review_marks:
            return
        _review_marks.add(mark)
        if len(_review_marks) > 1000:
            _review_marks.pop()
        data = load_data()
        day = get_day_bucket(data, datetime.datetime.now().strftime("%Y-%m-%d"))
        day["reviews"] += 1
    save_data()
    logger.info("[SalesAnalytics] Засчитан новый отзыв покупателя.")


# ════════════════════════════════════════ TELEGRAM ════════════════════════════════════════
def is_authorized(c: Cardinal, m: Message) -> bool:
    """Проверяет, есть ли отправитель в списке авторизованных пользователей ПУ."""
    if not c.telegram or m.from_user is None:
        return False
    return m.from_user.id in c.telegram.authorized_users


def send_stats(m: Message) -> None:
    """Обрабатывает команду /stats."""
    c = _cardinal
    if c is None or not c.telegram:
        return
    if not is_authorized(c, m):
        logger.warning(f"[SalesAnalytics] Команда /stats от неавторизованного пользователя "
                       f"$YELLOW{m.from_user.id}$RESET проигнорирована.")
        return

    cfg = load_config()
    args = (m.text or "").split()[1:]
    argument = args[0].lower() if args else ""

    days, today, all_time = None, False, False
    if argument in ("today", "сегодня", "0"):
        today = True
    elif argument in ("all", "всё", "все", "alltime"):
        all_time = True
    elif argument.isdigit():
        days = min(max(int(argument), 1), MAX_DAYS)

    try:
        keys, period_text = day_range(days=days, today=today, all_time=all_time)
        summary = aggregate(load_data(), keys)
        text = build_report_text(summary, period_text, cfg, daily_breakdown=not today)
        if not today and not all_time:
            text += "\n\n<i>Варианты: /stats today • /stats 30 • /stats all</i>"
        c.telegram.bot.send_message(m.chat.id, text)
    except Exception:
        logger.error("[SalesAnalytics] Ошибка при формировании статистики.")
        logger.debug("TRACEBACK", exc_info=True)
        try:
            c.telegram.bot.send_message(m.chat.id,
                                        "❌ Не удалось сформировать статистику. Подробности в логах бота.")
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)


def daily_report_loop(c: Cardinal, run_id: int) -> None:
    """Поток ежедневной сводки за прошедшие сутки."""
    global _last_report_date
    logger.info("[SalesAnalytics] Поток ежедневных отчетов запущен.")
    while True:
        if c.run_id != run_id:
            logger.info("[SalesAnalytics] Поток ежедневных отчетов остановлен.")
            return
        try:
            cfg = load_config()
            now = datetime.datetime.now()
            today_key = now.strftime("%Y-%m-%d")
            if cfg["enabled"] and cfg["daily_report"] and c.telegram:
                if _last_report_date != today_key and now.strftime("%H:%M") >= cfg["report_time"]:
                    yesterday = now - datetime.timedelta(days=1)
                    summary = aggregate(load_data(), [yesterday.strftime("%Y-%m-%d")])
                    text = build_report_text(summary, f"за {yesterday.strftime('%d.%m.%Y')} (сутки)", cfg)
                    c.telegram.send_notification(text, notification_type=tg_utils.NotificationTypes.other)
                    with _lock:
                        _last_report_date = today_key
                    save_data()
                    logger.info(f"[SalesAnalytics] Отправлена ежедневная сводка за "
                                f"$YELLOW{yesterday.strftime('%Y-%m-%d')}$RESET")
        except Exception:
            logger.error("[SalesAnalytics] Ошибка в потоке ежедневных отчетов.")
            logger.debug("TRACEBACK", exc_info=True)
        for _ in range(30):  # проверяем раз в 30 секунд, быстро выходим при остановке
            if c.run_id != run_id:
                return
            time.sleep(1)


# ═════════════════════════════════════ ИНИЦИАЛИЗАЦИЯ ═════════════════════════════════════
def pre_init(c: Cardinal) -> None:
    """Регистрирует команду /stats и переводы для меню команд Telegram."""
    global _cardinal
    _cardinal = c
    load_config()
    load_data()

    for language, text in (("ru", "Статистика продаж (сегодня / N дней / всё время)"),
                           ("en", "Sales statistics (today / N days / all time)"),
                           ("uk", "Статистика продаж (сьогодні / N днів / весь час)")):
        try:
            localizer.add_translation(UUID, "cmd_stats", text, language)
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)

    if not c.telegram:
        logger.info("[SalesAnalytics] Telegram отключен, команда /stats не зарегистрирована.")
        return
    c.telegram.msg_handler(send_stats, commands=["stats"])
    c.add_telegram_commands(UUID, [("stats", f"{UUID}_cmd_stats", True)])
    logger.info("[SalesAnalytics] Команда $YELLOW/stats$RESET зарегистрирована.")


def post_start(c: Cardinal) -> None:
    """Запускает фоновый поток ежедневной сводки."""
    global _thread_started
    if _thread_started:
        return
    _thread_started = True
    threading.Thread(target=daily_report_loop, args=(c, c.run_id), daemon=True,
                     name="SalesAnalyticsDailyReport").start()


def on_delete(c: Cardinal, call) -> None:
    """Удаляет файлы плагина при его удалении через ПУ Telegram."""
    for file in (STORAGE_FILE, CONFIG_FILE, f"{STORAGE_FILE}.tmp"):
        try:
            if os.path.exists(file):
                os.remove(file)
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)
    try:
        c.telegram.bot.answer_callback_query(call.id, "Файлы плагина Sales Analytics удалены.")
    except Exception:
        logger.debug("TRACEBACK", exc_info=True)


BIND_TO_PRE_INIT = [pre_init]
BIND_TO_POST_START = [post_start]
BIND_TO_NEW_ORDER = [on_new_order]
BIND_TO_ORDER_STATUS_CHANGED = [on_order_status_changed]
BIND_TO_POST_DELIVERY = [on_post_delivery]
BIND_TO_NEW_MESSAGE = [on_new_message]
BIND_TO_LAST_CHAT_MESSAGE_CHANGED = [on_new_message]
BIND_TO_DELETE = on_delete
