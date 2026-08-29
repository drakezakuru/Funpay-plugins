"""
AutoSMM plugin for FunPayCardinal
=================================

Автоматическая накрутка (подписчики/лайки/просмотры и т.п.) через стандартные
SMM-панели с API «perfect-panel» (`action=add/status/refill/balance`).

Поток: новый оплаченный заказ → плагин просит у покупателя ссылку → (опц.)
подтверждение → создаёт заказ у поставщика → поллит статус → при провале делает
авто-возврат на FunPay. Несколько поставщиков на установку, привязка лот→услуга,
рандомизация шаблонов сообщений, один активный заказ на покупателя, команда
`!прогресс`, меню полезных ссылок (реф-ссылки).

"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import threading
import time
from urllib.parse import urlparse
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import requests
from telebot.types import InlineKeyboardButton as B
from telebot.types import InlineKeyboardMarkup as K

try:
    from tg_bot import CBT
except Exception:
    class CBT:
        PLUGIN_SETTINGS = "PLUGIN_SETTINGS"
        CLEAR_STATE = "CLEAR_STATE"

if TYPE_CHECKING:
    from cardinal import Cardinal


# ══════════════════════════════════════════════════════════════════════════════
# 💛 DONATION BANNER — защита реквизитов автора.
# Реквизиты закодированы (base64 + SHA-256 подпись) и лежат ВНИЗУ файла в
# _donation_details(): если их подменить на свои, подпись не сойдётся и
# баннер НЕ отправится. True = 1 (вкл), False = 0 (выкл).
# ══════════════════════════════════════════════════════════════════════════════
DONATION_ENABLED = True                # True = 1 (показывать баннер), False = 0
DONATION_SHOW_ON_START = True         # True = 1 (слать при старте плагина)
DONATION_DAILY_ENABLED = True          # True = 1 (напоминание раз в сутки)
DONATION_DAILY_HOUR = 16               # час напоминания (0-23, МСК)
DONATION_CALLBACK_PREFIX = "asm_dn"    # префикс колбэков кнопок баннера
DONATION_PLUGIN_NAME = "AutoSMM"       # имя плагина в шапке баннера
AUTHOR_CHANNEL_URL = "https://t.me/pluginsdrake"  # канал с другими плагинами автора
AUTHOR_CHANNEL_USERNAME = "@pluginsdrake"

_donation_thread: "threading.Thread | None" = None
_donation_cardinal = None


def _donation_tampered() -> bool:
    """True если реквизиты подменены (подпись не сошлась)."""
    try:
        return not _donation_details()
    except Exception:
        return True


def _donation_banner_text() -> str:
    """Текст донат-баннера (реквизиты — в <code>, копируются тапом)."""
    _d = _donation_details()
    if not _d:
        return (
            "⚠️ <b>Баннер повреждён.</b>\n\n"
            "Реквизиты донат-баннера были подменены — подпись не сошлась, "
            "поэтому баннер не отправляется. Восстанови оригинальные "
            "значения в <code>_donation_details()</code> (внизу файла)."
        )
    return (
        f"💛 <b>{DONATION_PLUGIN_NAME}</b> — бесплатный плагин для FunPay!\n"
        "Если он помог тебе заработать — поддержи автора донатом:\n\n"
        f"💳 Карта (европейская): <code>{_d['card']}</code>\n"
        f"💎 Gram (TON): <code>{_d['ton']}</code>\n"
        f"💵 USDT (TON): <code>{_d['usdt_ton']}</code>\n"
        f"🪙 USDT (TRC20): <code>{_d['usdt']}</code>\n"
        f"📮 Пожелания и фичи: {_d['contact']}\n\n"
        "Спасибо за поддержку! ❤️\n\n"
        "🔧 Как убрать баннер: <tg-spoiler>найди в этом файле блок "
        "«DONATION BANNER» и поставь DONATION_ENABLED = False</tg-spoiler>"
    )


def _donation_banner_kb():
    """Кнопки-приколы под баннером."""
    from telebot import types as tbtypes  # type: ignore
    kb = tbtypes.InlineKeyboardMarkup(row_width=2)
    kb.add(
        tbtypes.InlineKeyboardButton(
            f"📦 Другие плагины автора ({AUTHOR_CHANNEL_USERNAME})",
            url=AUTHOR_CHANNEL_URL),
    )
    kb.add(
        tbtypes.InlineKeyboardButton(
            "😢 Я нищий",
            callback_data=f"{DONATION_CALLBACK_PREFIX}:donate_broke"),
        tbtypes.InlineKeyboardButton(
            "😎 Я не нищий, но не задоначу",
            callback_data=f"{DONATION_CALLBACK_PREFIX}:donate_rich"),
    )
    return kb


def _send_donation_banner(cardinal, chat_id=None) -> bool:
    """Шлёт донат-баннер оператору (всем authorized_users или конкретному chat_id)."""
    if not DONATION_ENABLED:
        return False
    if _donation_tampered():
        return False
    tg = getattr(cardinal, "telegram", None)
    if not tg or not getattr(tg, "bot", None):
        return False
    targets = ([chat_id] if chat_id is not None
               else list(getattr(tg, "authorized_users", []) or []))
    if not targets:
        return False
    text = _donation_banner_text()
    kb = None
    try:
        kb = _donation_banner_kb()
    except Exception:
        kb = None
    for uid in targets:
        try:
            tg.bot.send_message(uid, text, parse_mode="HTML",
                                reply_markup=kb,
                                disable_web_page_preview=True)
        except Exception:
            logging.getLogger(__name__).debug(
                "donation banner failed for uid=%s", uid, exc_info=True)
    return True


def _donation_callback_reply(data: str) -> str:
    """Ответ на кнопки-приколы баннера."""
    if (data or "").endswith("donate_broke"):
        return "😢 Нищета — не порок. Разбогатеешь — реквизиты ждут 😉"
    if (data or "").endswith("donate_rich"):
        return "😎 Ок, но мы всё равно тебя любим ❤️"
    return ""


def _donation_reminder_text() -> str:
    """Текст напоминания: реквизиты — скрытой цитатой (спойлер), жмутся тапом."""
    _d = _donation_details()
    if not _d:
        return "😄 Улыбнись! Тебя снимает скрытая камера 📷"
    return (
        "😄 Улыбнись! Тебя снимает скрытая камера 📷\n\n"
        "А если захочешь отблагодарить за бесплатный плагин — "
        "реквизиты в спойлере ниже 😉\n\n"
        "<tg-spoiler>"
        f"💳 Карта (европейская): <code>{_d['card']}</code>\n"
        f"💎 Gram (TON): <code>{_d['ton']}</code>\n"
        f"💵 USDT (TON): <code>{_d['usdt_ton']}</code>\n"
        f"🪙 USDT (TRC20): <code>{_d['usdt']}</code>\n"
        f"📮 Пожелания и фичи: {_d['contact']}"
        "</tg-spoiler>"
    )


def _donation_reminder_kb():
    """Кнопки под напоминанием: «Получить реквизиты» + приколы."""
    from telebot import types as tbtypes  # type: ignore
    kb = tbtypes.InlineKeyboardMarkup(row_width=2)
    kb.add(
        tbtypes.InlineKeyboardButton(
            "💳 Получить реквизиты",
            callback_data=f"{DONATION_CALLBACK_PREFIX}:donate"),
    )
    kb.add(
        tbtypes.InlineKeyboardButton(
            f"📦 Другие плагины ({AUTHOR_CHANNEL_USERNAME})",
            url=AUTHOR_CHANNEL_URL),
    )
    kb.add(
        tbtypes.InlineKeyboardButton(
            "😢 Я нищий",
            callback_data=f"{DONATION_CALLBACK_PREFIX}:donate_broke"),
        tbtypes.InlineKeyboardButton(
            "😎 Я не нищий, но не задоначу",
            callback_data=f"{DONATION_CALLBACK_PREFIX}:donate_rich"),
    )
    return kb


def _donation_claim_today() -> bool:
    """True если донат-рассылка за сегодня ещё не отправлялась.

    Общий для всех плагинов файл-замок (storage/plugins/_donation_mail/)
    создаётся атомарно через O_CREAT|O_EXCL: первый плагин, добежавший
    до рассылки, создаёт файл sent_<дата>.lock и шлёт; остальные видят,
    что файл уже есть, и пропускают. Каждый плагин остаётся автономным,
    но за сутки уходит только одна рассылка.
    """
    import datetime as _dt
    _dir = os.path.join("storage", "plugins", "_donation_mail")
    try:
        os.makedirs(_dir, exist_ok=True)
    except Exception:
        pass
    _lock = os.path.join(_dir, f"sent_{_dt.date.today().isoformat()}.lock")
    try:
        _fd = os.open(_lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(_fd, "w") as _f:
            _f.write(__name__)
        return True
    except FileExistsError:
        return False
    except Exception:
        return True


def _donation_reminder_loop(cardinal) -> None:
    """Раз в сутки в DONATION_DAILY_HOUR шлёт шуточное напоминание."""
    import datetime as _dt
    while True:
        try:
            now = _dt.datetime.now()
            nxt = now.replace(hour=DONATION_DAILY_HOUR, minute=0,
                              second=0, microsecond=0)
            if nxt <= now:
                nxt += _dt.timedelta(days=1)
            time.sleep(max(1, (nxt - now).total_seconds()))
            if not DONATION_ENABLED or not DONATION_DAILY_ENABLED:
                continue
            if _donation_tampered():
                continue
            if not _donation_claim_today():
                continue
            tg = getattr(cardinal, "telegram", None)
            for uid in list(getattr(tg, "authorized_users", []) or []):
                try:
                    tg.bot.send_message(
                        uid,
                        _donation_reminder_text(),
                        parse_mode="HTML",
                        reply_markup=_donation_reminder_kb(),
                        disable_web_page_preview=True)
                except Exception:
                    logging.getLogger(__name__).debug(
                        "donation reminder failed for uid=%s",
                        uid, exc_info=True)
        except Exception:
            logging.getLogger(__name__).debug("donation reminder error",
                                              exc_info=True)
            time.sleep(3600)


def _start_donation_reminder(cardinal) -> None:
    """Запускает фоновый тред ежедневного напоминания (если включено)."""
    global _donation_thread
    if not (DONATION_ENABLED and DONATION_DAILY_ENABLED):
        return
    if _donation_thread and _donation_thread.is_alive():
        return
    _donation_thread = threading.Thread(
        target=_donation_reminder_loop, args=(cardinal,), daemon=True,
        name="donation-reminder")
    _donation_thread.start()


def _donation_on_cb(call) -> None:
    """Колбэки кнопок баннера: :donate — показать, :donate_broke/:donate_rich — шутка."""
    try:
        data = call.data or ""
        if not data.startswith(DONATION_CALLBACK_PREFIX + ":"):
            return
        action = data[len(DONATION_CALLBACK_PREFIX) + 1:]
        tg = getattr(_donation_cardinal, "telegram", None)
        if not tg or not getattr(tg, "bot", None):
            return
        if action == "donate":
            try:
                _send_donation_banner(_donation_cardinal, call.message.chat.id)
            except Exception:
                pass
            try:
                tg.bot.answer_callback_query(call.id)
            except Exception:
                pass
            return
        reply = _donation_callback_reply(data)
        try:
            tg.bot.answer_callback_query(call.id, reply or "")
        except Exception:
            pass
    except Exception:
        pass


# =========================================================================
# Метаданные плагина (обязательные для FPC)
# =========================================================================

def _welcome_startup_text() -> str:
    """Текст приветственного сообщения при первом запуске плагина."""
    _ver = globals().get("VERSION", "?")
    return (
        "✨ <b>" + DONATION_PLUGIN_NAME + "</b> v" + str(_ver) + " запущен!\n\n"
        "Спасибо что выбрал этот плагин. 🎉\n\n"
        "📦 <b>Другие плагины автора</b> и обновления — "
        "в канале " + AUTHOR_CHANNEL_USERNAME + ":\n"
        '<a href="' + AUTHOR_CHANNEL_URL + '">' + AUTHOR_CHANNEL_URL + '</a>\n\n'
        "Подписывайся, чтобы не пропустить новые плагины и фичи. "
        "Если есть идеи/баги — пиши в канал 🙌"
    )


NAME = "AutoSMM"
VERSION = "1.2.1"
DESCRIPTION = (
    "Авто-накрутка через SMM-панели (perfect-panel API): несколько поставщиков, "
    "привязка лот→услуга, проверка ссылок, подтверждение, авто-возврат при провале, "
    "один активный заказ на покупателя, команда !прогресс, рандомизация шаблонов. "
    "Без бэкдоров и удалённой активации. Управление через Telegram-меню."
)
CREDITS = "@drakelovc"
UUID = "c5d2a8f4-3e91-4b6d-9c08-2a7f1e6b4d83"
SETTINGS_PAGE = True

logger = logging.getLogger(f"FPC.{__name__}")
LOGGER_PREFIX = "[AUTOSMM]"


# =========================================================================
# Хранилище: пути и дефолты
# =========================================================================

PLUGIN_DIR = Path("storage/plugins/autosmm")
SETTINGS_PATH = PLUGIN_DIR / "settings.json"
ORDERS_PATH = PLUGIN_DIR / "orders.json"
ACTIVE_ORDERS_PATH = PLUGIN_DIR / "active_orders.json"

RANGE_LINK_TIMEOUT = (3600, 7 * 86400)     # 1 час .. 7 суток
RANGE_POLL_INTERVAL = (30, 3600)           # сек
RANGE_MAX_BACKOFF = (60, 86400)            # сек
RANGE_AUTO_LOTS_INTERVAL = (5, 1440)       # мин

_SUCCESS_STATUSES = {"completed", "done", "success", "partial"}
_FAILURE_STATUSES = {"failed", "error", "canceled", "cancelled"}

DEFAULT_LINK_DOMAINS = [
    "t.me", "vk.com", "instagram.com", "tiktok.com", "youtube.com",
    "youtu.be", "twitch.tv", "twitter.com", "x.com", "vt.tiktok.com", "vm.tiktok.com",
]

DEFAULT_SETTINGS: dict[str, Any] = {
    "providers": [
        {"id": "p1", "name": "Twiboost", "api_url": "https://twiboost.com/api/v2", "api_key": ""},
    ],
    "lot_mappings": [],
    "allowed_link_domains": list(DEFAULT_LINK_DOMAINS),
    "confirm_link": True,
    "auto_refund": True,
    "link_wait_timeout_sec": 86400,
    "status_poll_interval_sec": 300,
    "max_backoff_sec": 3600,
    "auto_lots_enabled": False,
    "auto_lots_interval_min": 30,
    "auto_lots_extra_ids": [],
    "new_order_notifications": False,
    "messages": {
        "after_payment": [
            "❤️ Спасибо за оплату! Пришлите корректную ссылку (https://...).",
            "✅ Заказ оплачен. Пришлите ссылку на цель накрутки (https://...).",
        ],
        "after_confirmation": [
            "✅ Заказ создан. ID у поставщика: {provider_order_id}\n🔗 Отслеживание: !прогресс",
            "🚀 Заказ запущен в работу. ID: {provider_order_id}\nКоманда отслеживания: !прогресс",
        ],
        "success": [
            "🎉 Заказ выполнен! ID: {provider_order_id}\nПожалуйста, подтвердите заказ на FunPay.",
            "✅ Готово! ID: {provider_order_id}. Подтвердите заказ на FunPay.",
        ],
        "failure": [
            "❌ Заказ не выполнен. Средства возвращены.",
            "❌ К сожалению, заказ не был выполнен. Деньги вернулись на ваш баланс FunPay.",
        ],
    },
    "links_menu": [
        {"label": "🌐 Twiboost (реф)", "url": "https://twiboost.com/ref4266612"},
        {"label": "🌐 Vexboost (реф)", "url": "https://vexboost.ru/ref4268384"},
        {"label": "🛡 Proxy6 (реф)", "url": "https://proxy6.net/?r=865936"},
    ],
    "operator_chat_id": None,
}

_MESSAGE_SLOTS = ("after_payment", "after_confirmation", "success", "failure")

_io_lock = threading.RLock()
_rng = random.Random()


class RateLimited(Exception):
    """Поставщик ответил 429 — нужен backoff."""


# =========================================================================
# Чистое ядро (pure core)
# =========================================================================

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def _is_valid_link(text: str, allowed: list[str]) -> str | None:
    """Возвращает первый URL из text, чей host == или поддомен одного из
    разрешённых доменов; иначе None."""
    if not text:
        return None
    allowed_l = [d.lower().lstrip(".") for d in (allowed or []) if d]
    for m in _URL_RE.finditer(text):
        url = m.group(0).rstrip(".,);]")
        try:
            host = (urlparse(url).hostname or "").lower()
        except Exception:
            continue
        if not host:
            continue
        for d in allowed_l:
            if host == d or host.endswith("." + d):
                return url
    return None


def _classify_provider_status(status: str, remains: int | None) -> str:
    """'in_progress' | 'success' | 'failure'."""
    s = (status or "").strip().lower()
    if s in _SUCCESS_STATUSES:
        return "success"
    if remains is not None:
        try:
            if int(remains) == 0:
                return "success"
        except Exception:
            pass
    if s in _FAILURE_STATUSES:
        return "failure"
    return "in_progress"


def _aggregate_profit(records: list[dict], since_ts: float) -> dict:
    """Суммирует (sold_price - provider_cost) по успешным записям с
    finalized_at >= since_ts. Возвращает {count, revenue, cost, profit}."""
    count = 0
    revenue = 0.0
    cost = 0.0
    for r in records or []:
        try:
            if str(r.get("status")) != "success":
                continue
            fin = float(r.get("finalized_at", 0) or 0)
            if fin < since_ts:
                continue
            count += 1
            revenue += float(r.get("sold_price", 0) or 0)
            cost += float(r.get("provider_cost", 0) or 0)
        except Exception:
            continue
    return {
        "count": count,
        "revenue": round(revenue, 2),
        "cost": round(cost, 2),
        "profit": round(revenue - cost, 2),
    }


def _pick_variant(variants: list[str], rng: random.Random | None = None) -> str:
    """Случайный вариант из списка. Для одного элемента — он же. Пустой — ValueError."""
    if not variants:
        raise ValueError("variants must be non-empty")
    if len(variants) == 1:
        return variants[0]
    return (rng or _rng).choice(variants)


def _has_active(buyer_id: Any, active_orders: dict) -> bool:
    return str(buyer_id) in (active_orders or {})


_DIGITS_RE = re.compile(r"[0-9]+")


def _reactivatable_lot_ids(lot_mappings: list[dict]) -> list[int]:
    """Различимые числовые id лотов из lot_mappings, чей `lot_match` —
    целое число (int или ASCII-строка из цифр), в порядке первого появления.
    Подстрочные (нечисловые) `lot_match` исключаются. (Property 8, Req 12.3/12.4)"""
    seen: set[int] = set()
    out: list[int] = []
    for m in lot_mappings or []:
        if not isinstance(m, dict):
            continue
        match = m.get("lot_match")
        lot_id: int | None = None
        if isinstance(match, bool):
            # bool — подкласс int, но это не корректный id лота
            continue
        if isinstance(match, int):
            lot_id = match
        elif isinstance(match, str) and _DIGITS_RE.fullmatch(match.strip()):
            lot_id = int(match.strip())
        if lot_id is None:
            continue
        if lot_id not in seen:
            seen.add(lot_id)
            out.append(lot_id)
    return out


def _coerce_numeric_lot_id(value: Any) -> int | None:
    """Возвращает числовой id лота из int или ASCII-строки из цифр; иначе None.
    bool/None/нечисловое отбрасываются."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _DIGITS_RE.fullmatch(value.strip()):
        return int(value.strip())
    return None


def _auto_lots_target_ids(lot_mappings: list[dict], extra_ids: list) -> list[int]:
    """Полный набор Reactivatable_Lots, который обходит цикл (Req 12.3/12.4):
    order-preserving, без дублей — объединение `_reactivatable_lot_ids(lot_mappings)`
    и числовых id из `extra_ids` (int или ASCII-строка из цифр; прочее пропускается).
    Дедуп выполняется по обоим источникам. (Property 8)"""
    out = _reactivatable_lot_ids(lot_mappings)
    seen: set[int] = set(out)
    for value in extra_ids or []:
        lot_id = _coerce_numeric_lot_id(value)
        if lot_id is None:
            continue
        if lot_id not in seen:
            seen.add(lot_id)
            out.append(lot_id)
    return out


def _is_url_https(url: Any) -> bool:
    return isinstance(url, str) and url.startswith("https://")


def _mask_secret(s: str | None, head: int = 4, tail: int = 2) -> str:
    if not s:
        return ""
    s = str(s)
    if len(s) <= head + tail:
        return "***"
    return s[:head] + "…" + s[-tail:]


def _exp_backoff(attempt: int, base: float = 1.0, cap: float = 3600.0) -> float:
    try:
        return min(cap, base * (2 ** max(0, int(attempt))))
    except Exception:
        return cap


def _html_escape(s: str) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# =========================================================================
# Хранилище: load/save
# =========================================================================

def _ensure_dir() -> None:
    PLUGIN_DIR.mkdir(parents=True, exist_ok=True)


def _load_json(path: Path, default):
    _ensure_dir()
    if not path.exists():
        return json.loads(json.dumps(default))
    try:
        with _io_lock, open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logger.warning(f"{LOGGER_PREFIX} Не удалось прочитать {path}", exc_info=True)
        return json.loads(json.dumps(default))


def _save_json(path: Path, data) -> None:
    _ensure_dir()
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with _io_lock:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
    except Exception:
        logger.warning(f"{LOGGER_PREFIX} Не удалось записать {path}", exc_info=True)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _load_settings() -> dict[str, Any]:
    merged = json.loads(json.dumps(DEFAULT_SETTINGS))
    data = _load_json(SETTINGS_PATH, {})
    if isinstance(data, dict):
        for k, v in data.items():
            merged[k] = v
    # setdefault-миграция новых ключей для старых конфигов
    merged.setdefault("auto_lots_enabled", DEFAULT_SETTINGS["auto_lots_enabled"])
    merged.setdefault("auto_lots_interval_min", DEFAULT_SETTINGS["auto_lots_interval_min"])
    merged.setdefault("auto_lots_extra_ids", list(DEFAULT_SETTINGS["auto_lots_extra_ids"]))
    merged.setdefault("new_order_notifications", DEFAULT_SETTINGS["new_order_notifications"])
    # ensure message slots are non-empty lists (Req 7.3)
    msgs = merged.get("messages") or {}
    for slot in _MESSAGE_SLOTS:
        val = msgs.get(slot)
        if not isinstance(val, list) or not val:
            msgs[slot] = list(DEFAULT_SETTINGS["messages"][slot])
    merged["messages"] = msgs
    # clamp numeric ranges
    merged["link_wait_timeout_sec"] = _clamp(merged.get("link_wait_timeout_sec"), *RANGE_LINK_TIMEOUT)
    merged["status_poll_interval_sec"] = _clamp(merged.get("status_poll_interval_sec"), *RANGE_POLL_INTERVAL)
    merged["max_backoff_sec"] = _clamp(merged.get("max_backoff_sec"), *RANGE_MAX_BACKOFF)
    merged["auto_lots_interval_min"] = _clamp(merged.get("auto_lots_interval_min"), *RANGE_AUTO_LOTS_INTERVAL)
    return merged


def _save_settings(s: dict[str, Any]) -> None:
    _save_json(SETTINGS_PATH, s)


def _load_orders() -> list[dict]:
    data = _load_json(ORDERS_PATH, [])
    return data if isinstance(data, list) else []


def _save_orders(orders: list[dict]) -> None:
    _save_json(ORDERS_PATH, orders)


def _load_active() -> dict:
    data = _load_json(ACTIVE_ORDERS_PATH, {})
    return data if isinstance(data, dict) else {}


def _save_active(d: dict) -> None:
    _save_json(ACTIVE_ORDERS_PATH, d)


def set_buyer_active_order(buyer_id: Any, entry: dict) -> None:
    with _io_lock:
        d = _load_active()
        d[str(buyer_id)] = entry
        _save_active(d)


def get_buyer_active_order(buyer_id: Any) -> dict | None:
    return _load_active().get(str(buyer_id))


def remove_buyer_active_order(buyer_id: Any) -> None:
    with _io_lock:
        d = _load_active()
        if str(buyer_id) in d:
            del d[str(buyer_id)]
            _save_active(d)


def _clamp(v: Any, lo: int, hi: int) -> int:
    try:
        x = int(v)
    except Exception:
        return lo
    return max(lo, min(hi, x))


# =========================================================================
# SMM-клиент
# =========================================================================

class SMMClient:
    def __init__(self, api_url: str, api_key: str):
        self.api_url = api_url
        self.api_key = api_key
        self.session = requests.Session()

    def _post(self, data: dict) -> dict:
        data = dict(data)
        data["key"] = self.api_key
        attempts = 3
        last_exc = None
        for attempt in range(attempts):
            try:
                resp = self.session.post(self.api_url, data=data, timeout=15)
                if resp.status_code == 429:
                    raise RateLimited("429 Too Many Requests")
                resp.raise_for_status()
                return resp.json()
            except RateLimited:
                raise
            except Exception as e:
                last_exc = e
                if attempt < attempts - 1:
                    time.sleep(1 + attempt)
                    continue
        raise RuntimeError(f"SMM API error: {last_exc}")

    def add(self, service_id: int, link: str, quantity: int, **extras) -> dict:
        payload = {"action": "add", "service": service_id, "link": link, "quantity": quantity}
        payload.update(extras)
        return self._post(payload)

    def status(self, order_id: Any) -> dict:
        return self._post({"action": "status", "order": order_id})

    def refill(self, order_id: Any) -> dict:
        return self._post({"action": "refill", "order": order_id})

    def balance(self) -> dict:
        return self._post({"action": "balance"})


def _client_for_provider(provider: dict) -> SMMClient:
    return SMMClient(provider.get("api_url", ""), provider.get("api_key", ""))


def _find_provider(settings: dict, provider_id: str) -> dict | None:
    for p in settings.get("providers", []):
        if p.get("id") == provider_id:
            return p
    return None


# =========================================================================
# Привязка лот → услуга
# =========================================================================

def _match_mapping(settings: dict, lot_id: Any, lot_desc: str) -> dict | None:
    """Возвращает первый Lot_Mapping, чей `lot_match` совпадает с id лота
    (строго) ИЛИ содержится (без регистра) в названии лота.

    FunPay в событии заказа отдаёт НАЗВАНИЕ лота (`order.description`), а не
    его числовой id, поэтому основной способ привязки — по названию (или его
    части). Числовой id берётся из полного заказа (см. `_extract_lot_id`)."""
    lid = str(lot_id or "")
    desc = (lot_desc or "").lower()
    for m in settings.get("lot_mappings", []):
        match = str(m.get("lot_match", "")).strip()
        if not match:
            continue
        if match == lid or match.lower() in desc:
            return m
    return None


def _extract_lot_id(cardinal: "Cardinal", order: Any) -> Any:
    """Реальный id лота FunPay.

    `OrderShortcut` из события заказа НЕ содержит id лота (есть только
    `subcategory.id` — это id подкатегории, а не лота!). Поэтому, как в
    ns_gifts/steam_*, тянем полный заказ и читаем `lot_id`, с фоллбэком на
    парсинг html и (в крайнем случае) `subcategory.id` для совместимости."""
    order_id = getattr(order, "id", None)
    # 1) полный заказ через FunPayAPI
    try:
        getter = getattr(getattr(cardinal, "account", None), "get_order", None)
        if callable(getter) and order_id is not None:
            full = getter(order_id)
            lid = getattr(full, "lot_id", None)
            if lid:
                return str(lid)
    except Exception:
        logger.debug(f"{LOGGER_PREFIX} get_order для lot_id не удался", exc_info=True)
    # 2) html виджета заказа
    html = getattr(order, "html", "") or ""
    for pat in (r'data-offer="(\d+)"', r"offer\?id=(\d+)", r"offers/(\d+)"):
        m = re.search(pat, html)
        if m:
            return m.group(1)
    # 3) legacy-фоллбэк: id подкатегории (неточный, но лучше None)
    sub = getattr(order, "subcategory", None)
    return getattr(sub, "id", None) if sub is not None else None


def _order_quantity(mapping: dict, funpay_units: int) -> int:
    mult = float(mapping.get("qty_multiplier", 1.0) or 1.0)
    return max(1, int(round(funpay_units * mult)))


# =========================================================================
# Состояние ожидания ссылки (в памяти)
# =========================================================================

# buyer_id(str) -> {chat_id, order_id, mapping, units, sold_price, link, confirm, ts}
_waiting: dict[str, dict] = {}
_waiting_lock = threading.Lock()


# =========================================================================
# Поллер статусов (фоновый)
# =========================================================================

_poll_thread: threading.Thread | None = None
_poll_stop = threading.Event()


def _record_order(entry: dict) -> None:
    with _io_lock:
        orders = _load_orders()
        orders.append(entry)
        if len(orders) > 2000:
            orders = orders[-2000:]
        _save_orders(orders)


def _finalize_order(provider_order_id: Any, status: str, *, finalized_at: float | None = None,
                    provider_cost: float | None = None, cost_currency: str | None = None) -> None:
    with _io_lock:
        orders = _load_orders()
        for o in orders:
            if str(o.get("provider_order_id")) == str(provider_order_id) and o.get("status") not in ("success", "failure"):
                o["status"] = status
                o["finalized_at"] = finalized_at or time.time()
                if provider_cost is not None:
                    o["provider_cost"] = float(provider_cost)
                if cost_currency is not None:
                    o["cost_currency"] = cost_currency
                break
        _save_orders(orders)


def _active_records() -> list[dict]:
    """Заказы, ещё не достигшие терминального статуса."""
    return [o for o in _load_orders() if o.get("status") not in ("success", "failure")]


def _poll_loop(cardinal: "Cardinal") -> None:
    backoff_attempt = 0
    while not _poll_stop.is_set():
        settings = _load_settings()
        interval = float(settings.get("status_poll_interval_sec", 300))
        # ждём интервал кусочками
        waited = 0.0
        while waited < interval and not _poll_stop.is_set():
            time.sleep(min(5.0, interval - waited))
            waited += 5.0
        if _poll_stop.is_set():
            break
        try:
            settings = _load_settings()
            for rec in _active_records():
                if _poll_stop.is_set():
                    break
                provider = _find_provider(settings, rec.get("provider_id"))
                if not provider:
                    continue
                client = _client_for_provider(provider)
                try:
                    data = client.status(rec.get("provider_order_id"))
                    backoff_attempt = 0
                except RateLimited:
                    delay = _exp_backoff(backoff_attempt, cap=float(settings.get("max_backoff_sec", 3600)))
                    backoff_attempt += 1
                    logger.info(f"{LOGGER_PREFIX} 429 при опросе, backoff {delay:.0f}s")
                    time.sleep(delay)
                    break
                except Exception:
                    logger.debug(f"{LOGGER_PREFIX} ошибка опроса заказа", exc_info=True)
                    continue
                _handle_status_update(cardinal, settings, rec, data)
        except Exception:
            logger.warning(f"{LOGGER_PREFIX} Ошибка в цикле опроса", exc_info=True)


def _handle_status_update(cardinal: "Cardinal", settings: dict, rec: dict, data: dict) -> None:
    status_raw = str(data.get("status", "")) if isinstance(data, dict) else ""
    remains = None
    if isinstance(data, dict):
        try:
            remains = int(data.get("remains")) if data.get("remains") is not None else None
        except Exception:
            remains = None
    verdict = _classify_provider_status(status_raw, remains)
    if verdict == "in_progress":
        return

    buyer_id = rec.get("buyer_id")
    chat_id = rec.get("chat_id")
    poid = rec.get("provider_order_id")

    # себестоимость от поставщика (поле charge в ответе status), если есть
    charge = None
    cost_currency = None
    if isinstance(data, dict):
        try:
            charge = float(data.get("charge")) if data.get("charge") not in (None, "") else None
        except Exception:
            charge = None
        cost_currency = data.get("currency")

    if verdict == "success":
        _finalize_order(poid, "success", provider_cost=charge, cost_currency=cost_currency)
        remove_buyer_active_order(buyer_id)
        _send_buyer(cardinal, chat_id, _pick_variant(settings["messages"]["success"]).format(provider_order_id=poid))
    else:  # failure
        _finalize_order(poid, "failure", provider_cost=charge, cost_currency=cost_currency)
        remove_buyer_active_order(buyer_id)
        _send_buyer(cardinal, chat_id, _pick_variant(settings["messages"]["failure"]))
        _do_refund(cardinal, settings, rec.get("order_id"), chat_id, reason=f"статус поставщика: {status_raw}")


def _ensure_poll_thread(cardinal: "Cardinal") -> None:
    global _poll_thread
    if _poll_thread and _poll_thread.is_alive():
        return
    _poll_stop.clear()
    _poll_thread = threading.Thread(target=_poll_loop, args=(cardinal,), name="autosmm-poll", daemon=True)
    _poll_thread.start()


# =========================================================================
# Возврат + уведомления
# =========================================================================

def _send_buyer(cardinal: "Cardinal", chat_id: Any, text: str) -> None:
    try:
        cardinal.send_message(chat_id, text)
    except Exception:
        logger.debug(f"{LOGGER_PREFIX} send_buyer failed", exc_info=True)


def _notify_operator(cardinal: "Cardinal", text: str) -> None:
    s = _load_settings()
    chat_id = s.get("operator_chat_id")
    if not chat_id:
        return
    try:
        cardinal.telegram.bot.send_message(chat_id, text, parse_mode="HTML")
    except Exception:
        logger.debug(f"{LOGGER_PREFIX} notify_operator failed", exc_info=True)


def _notify_new_order(cardinal: "Cardinal", order, mapping: dict) -> None:
    """Уведомление оператора о новом оплаченном заказе по совпавшему лоту (Req 13.2)."""
    order_id = getattr(order, "id", None)
    buyer_name = getattr(order, "buyer_username", None)
    buyer_id = getattr(order, "buyer_id", None)
    amount = getattr(order, "amount", 1) or 1
    price = getattr(order, "price", 0) or 0
    currency = getattr(order, "currency", "") or ""
    lot_title = (mapping or {}).get("lot_match", "")
    lot_desc = getattr(order, "description", "") or getattr(order, "title", "") or ""

    buyer_line = buyer_name or "—"
    if buyer_id is not None:
        buyer_line = f"{buyer_line} (id {buyer_id})"

    price_line = f"{price} {currency}".strip()
    lot_line = lot_desc or str(lot_title)

    text = (
        "🔔 <b>Новый заказ</b>\n"
        f"🧾 Заказ FunPay: #{order_id}\n"
        f"👤 Покупатель: {buyer_line}\n"
        f"💰 Сумма: {price_line} (кол-во: {amount})\n"
        f"🗂 Лот: {lot_line}"
    )
    _notify_operator(cardinal, text)


def _do_refund(cardinal: "Cardinal", settings: dict, order_id: Any, chat_id: Any, reason: str) -> None:
    order_url = f"https://funpay.com/orders/{order_id}/"
    if settings.get("auto_refund", True):
        try:
            cardinal.account.refund(order_id)
            _notify_operator(cardinal, f"💸 Авто-возврат по заказу #{order_id}.\nПричина: {reason}\n{order_url}")
        except Exception as e:
            _notify_operator(cardinal, f"⚠️ Не удалось вернуть #{order_id}: {e}\nВерните вручную: {order_url}")
    else:
        _notify_operator(cardinal, f"⚠️ Требуется ручной возврат #{order_id}.\nПричина: {reason}\n{order_url}")


# =========================================================================
# Размещение заказа
# =========================================================================

def _place_order(cardinal: "Cardinal", settings: dict, w: dict, link: str) -> None:
    mapping = w["mapping"]
    provider = _find_provider(settings, mapping.get("provider_id"))
    chat_id = w["chat_id"]
    order_id = w["order_id"]
    buyer_id = w["buyer_id"]
    if not provider:
        _send_buyer(cardinal, chat_id, "❌ Поставщик не настроен. Свяжитесь с продавцом.")
        _do_refund(cardinal, settings, order_id, chat_id, reason="поставщик не найден")
        return

    qty = _order_quantity(mapping, w.get("units", 1))
    client = _client_for_provider(provider)
    try:
        resp = client.add(int(mapping.get("service_id")), link, qty)
    except RateLimited:
        _send_buyer(cardinal, chat_id, "⏳ Сервис временно перегружен, попробуем чуть позже.")
        return
    except Exception as e:
        _send_buyer(cardinal, chat_id, "❌ Ошибка при создании заказа. Средства будут возвращены.")
        _do_refund(cardinal, settings, order_id, chat_id, reason=f"ошибка add: {e}")
        return

    provider_order_id = resp.get("order") if isinstance(resp, dict) else None
    if not provider_order_id:
        err = resp.get("error") if isinstance(resp, dict) else resp
        _send_buyer(cardinal, chat_id, "❌ Поставщик отклонил заказ. Средства будут возвращены.")
        _do_refund(cardinal, settings, order_id, chat_id, reason=f"поставщик: {err}")
        return

    _record_order({
        "order_id": str(order_id),
        "buyer_id": str(buyer_id),
        "chat_id": chat_id,
        "provider_id": provider.get("id"),
        "provider_order_id": provider_order_id,
        "link": link,
        "qty": qty,
        "sold_price": float(w.get("sold_price", 0) or 0),
        "provider_cost": 0.0,
        "status": "in_progress",
        "created_at": time.time(),
        "finalized_at": None,
    })
    set_buyer_active_order(buyer_id, {
        "order_id_funpay": str(order_id),
        "provider_order_id": provider_order_id,
        "provider_id": provider.get("id"),
        "status": "processing",
        "created_at": time.time(),
    })
    msg = _pick_variant(settings["messages"]["after_confirmation"]).format(provider_order_id=provider_order_id)
    _send_buyer(cardinal, chat_id, msg)


# =========================================================================
# Хендлеры событий FunPay
# =========================================================================

def _on_new_order(cardinal: "Cardinal", event) -> None:
    try:
        order = getattr(event, "order", None)
        if order is None:
            return
        order_id = getattr(order, "id", None)
        buyer_id = getattr(order, "buyer_id", None)
        lot_desc = getattr(order, "description", "") or getattr(order, "title", "") or ""
        amount = getattr(order, "amount", 1) or 1
        price = getattr(order, "price", 0) or 0
        lot_id = _extract_lot_id(cardinal, order)

        settings = _load_settings()
        mapping = _match_mapping(settings, lot_id, lot_desc)
        if not mapping:
            return

        # уведомление оператора о новом заказе — независимо от гейта активного заказа (Req 13.5)
        if settings.get("new_order_notifications"):
            _notify_new_order(cardinal, order, mapping)

        # один активный заказ на покупателя
        if _has_active(buyer_id, _load_active()):
            chat_id = _buyer_chat_id(cardinal, order)
            _send_buyer(cardinal, chat_id,
                        "⚠️ У вас уже есть активный заказ в обработке.\n"
                        "Дождитесь его завершения. Проверить прогресс: !прогресс")
            return

        chat_id = _buyer_chat_id(cardinal, order)
        with _waiting_lock:
            _waiting[str(buyer_id)] = {
                "chat_id": chat_id,
                "order_id": order_id,
                "buyer_id": buyer_id,
                "mapping": mapping,
                "units": int(amount),
                "sold_price": float(price),
                "confirm_pending": False,
                "candidate_link": None,
                "ts": time.time(),
            }
        _send_buyer(cardinal, chat_id, _pick_variant(settings["messages"]["after_payment"]))
    except Exception:
        logger.warning(f"{LOGGER_PREFIX} on_new_order error", exc_info=True)


def _buyer_chat_id(cardinal: "Cardinal", order) -> Any:
    # пытаемся получить chat_id по покупателю
    try:
        return cardinal.account.get_chat_by_name(order.buyer_username).id
    except Exception:
        return getattr(order, "chat_id", None) or getattr(order, "buyer_id", None)


def _on_new_message(cardinal: "Cardinal", event) -> None:
    try:
        msg = getattr(event, "message", None)
        if msg is None:
            return
        text = (getattr(msg, "text", "") or "").strip()
        if not text:
            return
        author_id = getattr(msg, "author_id", None)
        chat_id = getattr(msg, "chat_id", None)
        my_id = getattr(cardinal.account, "id", None)
        if author_id == my_id:
            return

        settings = _load_settings()

        # --- команда !прогресс ---
        if text.lower() == "!прогресс":
            _cmd_progress(cardinal, settings, author_id, chat_id)
            return

        # --- команды чек/рефилл ---
        m_check = re.match(r"^чек\s+(\S+)$", text.lower())
        if m_check:
            _cmd_check(cardinal, settings, chat_id, m_check.group(1))
            return
        m_refill = re.match(r"^рефилл\s+(\S+)$", text.lower())
        if m_refill:
            _cmd_refill(cardinal, settings, chat_id, m_refill.group(1))
            return

        # --- ожидание ссылки/подтверждения ---
        with _waiting_lock:
            w = _waiting.get(str(author_id))
        if not w:
            return

        # подтверждение
        if w.get("confirm_pending"):
            low = text.lower()
            if low in ("+", "да", "yes", "ок", "ok"):
                link = w.get("candidate_link")
                with _waiting_lock:
                    _waiting.pop(str(author_id), None)
                _place_order(cardinal, settings, w, link)
                return
            if low in ("-", "нет", "no", "отмена", "cancel"):
                with _waiting_lock:
                    _waiting.pop(str(author_id), None)
                _send_buyer(cardinal, chat_id, "🚫 Отменено. Средства будут возвращены.")
                _do_refund(cardinal, settings, w["order_id"], chat_id, reason="покупатель отменил")
                return
            # иначе считаем что прислали новую ссылку — упадём ниже

        link = _is_valid_link(text, settings.get("allowed_link_domains", []))
        if not link:
            _send_buyer(cardinal, chat_id,
                        "❗ Не вижу корректной ссылки. Пришлите ссылку на разрешённый сайт (https://...).")
            return

        if settings.get("confirm_link", True):
            with _waiting_lock:
                w["confirm_pending"] = True
                w["candidate_link"] = link
                _waiting[str(author_id)] = w
            _send_buyer(cardinal, chat_id, f"🔗 Запускаем накрутку на:\n{link}\nВсё верно? Ответьте «+» или «-».")
        else:
            with _waiting_lock:
                _waiting.pop(str(author_id), None)
            _place_order(cardinal, settings, w, link)
    except Exception:
        logger.warning(f"{LOGGER_PREFIX} on_new_message error", exc_info=True)


def _cmd_progress(cardinal: "Cardinal", settings: dict, buyer_id: Any, chat_id: Any) -> None:
    active = get_buyer_active_order(buyer_id)
    if not active:
        _send_buyer(cardinal, chat_id, "ℹ️ У вас нет активных заказов.")
        return
    provider = _find_provider(settings, active.get("provider_id"))
    poid = active.get("provider_order_id")
    if not provider:
        _send_buyer(cardinal, chat_id, f"📊 Заказ #{active.get('order_id_funpay')} в обработке (ID: {poid}).")
        return
    try:
        data = _client_for_provider(provider).status(poid)
        st = data.get("status", "?")
        rem = data.get("remains", "?")
        _send_buyer(cardinal, chat_id,
                    f"📊 Прогресс заказа #{active.get('order_id_funpay')}\n"
                    f"🔢 ID у поставщика: {poid}\n📈 Статус: {st}\n📉 Осталось: {rem}")
    except Exception:
        _send_buyer(cardinal, chat_id, "❌ Не удалось получить статус, попробуйте позже.")


def _cmd_check(cardinal: "Cardinal", settings: dict, chat_id: Any, provider_order_id: str) -> None:
    rec = next((o for o in _load_orders() if str(o.get("provider_order_id")) == str(provider_order_id)), None)
    provider = _find_provider(settings, rec.get("provider_id")) if rec else None
    if not provider and settings.get("providers"):
        provider = settings["providers"][0]
    if not provider:
        _send_buyer(cardinal, chat_id, "❌ Поставщик не настроен.")
        return
    try:
        data = _client_for_provider(provider).status(provider_order_id)
        _send_buyer(cardinal, chat_id, f"Статус заказа {provider_order_id}: {data.get('status', '?')} "
                                       f"(осталось: {data.get('remains', '?')})")
    except Exception:
        _send_buyer(cardinal, chat_id, "❌ Ошибка при проверке.")


def _cmd_refill(cardinal: "Cardinal", settings: dict, chat_id: Any, provider_order_id: str) -> None:
    rec = next((o for o in _load_orders() if str(o.get("provider_order_id")) == str(provider_order_id)), None)
    provider = _find_provider(settings, rec.get("provider_id")) if rec else None
    if not provider:
        _send_buyer(cardinal, chat_id, "❌ Заказ не найден.")
        return
    try:
        data = _client_for_provider(provider).refill(provider_order_id)
        if isinstance(data, dict) and (data.get("refill") or data.get("status")):
            _send_buyer(cardinal, chat_id, "🔄 Запрос на рефилл отправлен.")
        else:
            _send_buyer(cardinal, chat_id, f"⚠️ Рефилл недоступен: {data}")
    except Exception:
        _send_buyer(cardinal, chat_id, "❌ Ошибка при запросе рефилла.")


# =========================================================================
# Фоновая проверка таймаутов ожидания ссылки
# =========================================================================

_timeout_thread: threading.Thread | None = None


def _timeout_loop(cardinal: "Cardinal") -> None:
    while not _poll_stop.is_set():
        waited = 0.0
        while waited < 60 and not _poll_stop.is_set():
            time.sleep(5)
            waited += 5
        if _poll_stop.is_set():
            break
        try:
            settings = _load_settings()
            timeout = float(settings.get("link_wait_timeout_sec", 86400))
            now = time.time()
            expired = []
            with _waiting_lock:
                for bid, w in list(_waiting.items()):
                    if now - float(w.get("ts", now)) >= timeout:
                        expired.append((bid, w))
                        _waiting.pop(bid, None)
            for bid, w in expired:
                _send_buyer(cardinal, w["chat_id"], "⌛ Время на отправку ссылки истекло. Средства возвращены.")
                _do_refund(cardinal, settings, w["order_id"], w["chat_id"], reason="таймаут ожидания ссылки")
        except Exception:
            logger.debug(f"{LOGGER_PREFIX} timeout loop error", exc_info=True)


def _ensure_timeout_thread(cardinal: "Cardinal") -> None:
    global _timeout_thread
    if _timeout_thread and _timeout_thread.is_alive():
        return
    _timeout_thread = threading.Thread(target=_timeout_loop, args=(cardinal,), name="autosmm-timeout", daemon=True)
    _timeout_thread.start()


# =========================================================================
# Авто-поднятие лотов (фоновый поток)
# =========================================================================

_auto_lots_thread: threading.Thread | None = None
_auto_lots_stop = threading.Event()


def _auto_lots_loop(cardinal: "Cardinal", stop_event: threading.Event) -> None:
    """Периодически переактивирует лоты, привязанные числовым `lot_match`,
    а также явные id из `auto_lots_extra_ids`.

    Следует паттерну minecraft_donate.py / vip_roblox.py:
    get_lot_fields → fields.active = True → save_lot. Интервал перечитывается
    каждый цикл, чтобы правки из меню применялись без перезапуска (Req 12.5).
    Останов — через переданный threading.Event (Req 12.6)."""
    while not stop_event.is_set():
        s = _load_settings()
        if s.get("auto_lots_enabled"):
            for lot_id in _auto_lots_target_ids(s.get("lot_mappings", []), s.get("auto_lots_extra_ids", [])):
                if stop_event.is_set():
                    break
                try:
                    lf = cardinal.account.get_lot_fields(lot_id)
                    lf.active = True
                    cardinal.account.save_lot(lf)
                except Exception as e:
                    # одиночный сбой логируем и продолжаем с остальными (Req 12.7)
                    logger.warning(f"{LOGGER_PREFIX} авто-поднятие: лот {lot_id} не удался: {e}")
        # интервал перечитывается каждый цикл (Req 12.2/12.5)
        interval_min = _clamp(s.get("auto_lots_interval_min", 30), *RANGE_AUTO_LOTS_INTERVAL)
        stop_event.wait(interval_min * 60)


def _ensure_auto_lots_thread(cardinal: "Cardinal") -> None:
    global _auto_lots_thread
    if _auto_lots_thread and _auto_lots_thread.is_alive():
        return
    _auto_lots_stop.clear()
    _auto_lots_thread = threading.Thread(
        target=_auto_lots_loop, args=(cardinal, _auto_lots_stop),
        name="autosmm-autolots", daemon=True)
    _auto_lots_thread.start()


# =========================================================================
# Telegram-меню оператора
# =========================================================================

CBP = "autosmm"
CBT_TOGGLE_CONFIRM = f"{CBP}:toggle_confirm"
CBT_TOGGLE_REFUND = f"{CBP}:toggle_refund"
CBT_EDIT_TIMEOUT = f"{CBP}:edit_timeout"
CBT_EDIT_POLL = f"{CBP}:edit_poll"
CBT_TOGGLE_AUTOLOTS = f"{CBP}:toggle_autolots"
CBT_EDIT_AUTOLOTS_INT = f"{CBP}:edit_autolots_int"
CBT_AUTOLOTS_IDS = f"{CBP}:autolots_ids"
CBT_TOGGLE_NEWORDER = f"{CBP}:toggle_neworder"
CBT_PROVIDERS = f"{CBP}:providers"
CBT_PROVIDER_BAL = f"{CBP}:pbal"          # + :provider_id
CBT_PROVIDER_ADD = f"{CBP}:padd"
CBT_PROVIDER_VIEW = f"{CBP}:pview"        # + :provider_id
CBT_PROVIDER_DEL = f"{CBP}:pdel"          # + :provider_id
CBT_PROVIDER_EDIT_NAME = f"{CBP}:pename"  # + :provider_id
CBT_PROVIDER_EDIT_URL = f"{CBP}:peurl"    # + :provider_id
CBT_PROVIDER_EDIT_KEY = f"{CBP}:pekey"    # + :provider_id
CBT_MAPPINGS = f"{CBP}:mappings"
CBT_MAPPING_ADD = f"{CBP}:madd"
CBT_MAPPING_DEL = f"{CBP}:mdel"           # + :mapping_id
CBT_MAPPING_EXPORT = f"{CBP}:mexport"
CBT_MAPPING_IMPORT = f"{CBP}:mimport"
CBT_MSGS = f"{CBP}:msgs"
CBT_MSG_SLOT = f"{CBP}:mslot"             # + :slot
CBT_MSG_ADD = f"{CBP}:msgadd"             # + :slot
CBT_MSG_DEL = f"{CBP}:msgdel"             # + :slot:idx
CBT_LINKS = f"{CBP}:links"
CBT_LINK_ADD = f"{CBP}:ladd"
CBT_LINK_DEL = f"{CBP}:ldel"              # + :idx
CBT_STATS = f"{CBP}:stats"
CBT_ACTIVE = f"{CBP}:active"
CBT_ACTIVE_CLEAR = f"{CBP}:aclr"          # + :buyer_id
CBT_DOMAINS = f"{CBP}:domains"
CBT_ADVANCED = f"{CBP}:advanced"
CBT_HELP = f"{CBP}:help"
CBT_HOME = f"{CBP}:home"

_SLOT_LABELS = {
    "after_payment": "После оплаты",
    "after_confirmation": "После подтверждения",
    "success": "Успех",
    "failure": "Провал",
}


def _new_id(prefix: str, existing: list[dict]) -> str:
    nums = []
    for e in existing:
        eid = str(e.get("id", ""))
        if eid.startswith(prefix):
            try:
                nums.append(int(eid[len(prefix):]))
            except Exception:
                pass
    return f"{prefix}{(max(nums) + 1) if nums else 1}"


def _fmt_duration(seconds: Any) -> str:
    """Человекочитаемая длительность: сутки/часы/минуты/секунды."""
    try:
        sec = int(seconds)
    except Exception:
        return f"{seconds}"
    if sec and sec % 86400 == 0:
        return f"{sec // 86400} дн"
    if sec and sec % 3600 == 0:
        return f"{sec // 3600} ч"
    if sec and sec % 60 == 0:
        return f"{sec // 60} мин"
    return f"{sec} сек"


def _onoff(flag: Any) -> str:
    return "🟢 вкл" if flag else "🔴 выкл"


def _home_text() -> str:
    s = _load_settings()
    providers = s.get("providers", [])
    mappings = s.get("lot_mappings", [])
    # подсказка для первого запуска: чего не хватает до рабочего состояния
    no_key = [p.get("name", "?") for p in providers if not (p.get("api_key") or "").strip()]
    hints = []
    if not providers:
        hints.append("• добавьте поставщика (✏️ Поставщики)")
    elif no_key:
        hints.append("• впишите API-ключ: " + ", ".join(no_key))
    if not mappings:
        hints.append("• привяжите лот к услуге (🗺 Привязки лотов)")
    hint_block = ("\n<b>⚠️ Чтобы заработало:</b>\n" + "\n".join(hints)) if hints else \
        "\n✅ Всё готово к работе."
    return (
        f"<b>🚀 AutoSMM v{VERSION}</b> — авто-накрутка через SMM-панели\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"✏️ Поставщики: <code>{len(providers)}</code>     "
        f"🗺 Привязки лотов: <code>{len(mappings)}</code>\n"
        f"🔁 Подтверждение ссылки: {_onoff(s.get('confirm_link'))}\n"
        f"💸 Авто-возврат: {_onoff(s.get('auto_refund'))}\n"
        f"\n"
        f"<i>Доп. настройки (⚙️ Ещё):</i>\n"
        f"🔁 Авто-поднятие лотов: {_onoff(s.get('auto_lots_enabled'))} "
        f"(интервал авто-поднятия: {int(s.get('auto_lots_interval_min', 30))} мин)\n"
        f"🔔 Уведомления о заказах: {_onoff(s.get('new_order_notifications'))}\n"
        f"⏱ Ожидание ссылки: {_fmt_duration(s.get('link_wait_timeout_sec', 86400))} · "
        f"🔄 опрос: {_fmt_duration(s.get('status_poll_interval_sec', 300))}\n"
        f"{hint_block}"
    )


def _home_kb() -> "K":
    s = _load_settings()
    kb = K(row_width=2)
    kb.row(B("✏️ Поставщики", callback_data=CBT_PROVIDERS),
           B("🗺 Привязки лотов", callback_data=CBT_MAPPINGS))
    kb.row(
        B(("🔁 Подтверждение: 🟢" if s.get("confirm_link") else "🔁 Подтверждение: 🔴"), callback_data=CBT_TOGGLE_CONFIRM),
        B(("💸 Авто-возврат: 🟢" if s.get("auto_refund") else "💸 Авто-возврат: 🔴"), callback_data=CBT_TOGGLE_REFUND),
    )
    kb.row(B("📦 Активные заказы", callback_data=CBT_ACTIVE),
           B("📊 Статистика", callback_data=CBT_STATS))
    kb.row(B("⚙️ Ещё настройки", callback_data=CBT_ADVANCED),
           B("🔗 Полезные ссылки", callback_data=CBT_LINKS))
    kb.row(B("❓ Как настроить", callback_data=CBT_HELP),
           B("💛 Донат", callback_data=f"{DONATION_CALLBACK_PREFIX}:donate"))
    return kb


def _advanced_text() -> str:
    s = _load_settings()
    return (
        f"<b>⚙️ Дополнительные настройки</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔁 Авто-поднятие лотов: {_onoff(s.get('auto_lots_enabled'))}\n"
        f"⏱ Интервал авто-поднятия: <code>{int(s.get('auto_lots_interval_min', 30))}</code> мин\n"
        f"🆔 Доп. ID лотов: <code>{len(s.get('auto_lots_extra_ids', []))}</code>\n"
        f"🔔 Уведомления о заказах: {_onoff(s.get('new_order_notifications'))}\n"
        f"⏱ Ожидание ссылки: <code>{_fmt_duration(s.get('link_wait_timeout_sec', 86400))}</code>\n"
        f"🔄 Опрос статусов: <code>{_fmt_duration(s.get('status_poll_interval_sec', 300))}</code>\n"
        f"🌐 Разрешённых доменов: <code>{len(s.get('allowed_link_domains', []))}</code>"
    )


def _advanced_kb() -> "K":
    s = _load_settings()
    kb = K(row_width=2)
    kb.row(
        B(("🔁 Авто-поднятие: 🟢" if s.get("auto_lots_enabled") else "🔁 Авто-поднятие: 🔴"), callback_data=CBT_TOGGLE_AUTOLOTS),
        B("⏱ Интервал авто-поднятия", callback_data=CBT_EDIT_AUTOLOTS_INT),
    )
    kb.row(B("🆔 Доп. ID лотов", callback_data=CBT_AUTOLOTS_IDS))
    kb.row(B(("🔔 Уведомления о заказах: 🟢" if s.get("new_order_notifications") else "🔔 Уведомления о заказах: 🔴"), callback_data=CBT_TOGGLE_NEWORDER))
    kb.row(B("📜 Шаблоны сообщений", callback_data=CBT_MSGS),
           B("🌐 Домены ссылок", callback_data=CBT_DOMAINS))
    kb.row(B("⏱ Таймаут ссылки", callback_data=CBT_EDIT_TIMEOUT),
           B("🔄 Интервал опроса", callback_data=CBT_EDIT_POLL))
    kb.row(B("◀️ Назад", callback_data=CBT_HOME))
    return kb


def _help_text() -> str:
    return (
        "<b>❓ Как настроить AutoSMM за 4 шага</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "1️⃣ <b>Поставщик.</b> «✏️ Поставщики» → откройте поставщика → "
        "впишите API-ключ от вашей SMM-панели (по умолчанию уже добавлен Twiboost). "
        "Кнопка «💰 Баланс» проверит, что ключ рабочий.\n\n"
        "2️⃣ <b>Привязка лота.</b> «🗺 Привязки лотов» → «➕ Добавить привязку». "
        "Введите <b>название лота точно как на FunPay</b> (или его часть), затем "
        "<code>service_id</code> услуги из панели и множитель количества.\n\n"
        "3️⃣ <b>Проверка.</b> Купите свой лот с другого аккаунта — бот попросит "
        "ссылку, создаст заказ у поставщика и сам отследит статус.\n\n"
        "4️⃣ <b>По желанию (⚙️ Ещё настройки):</b> авто-поднятие лотов, "
        "уведомления о заказах, свои тексты сообщений, домены ссылок, таймауты.\n\n"
        "💡 Покупатель отслеживает заказ командой <code>!прогресс</code>."
    )


def _links_kb() -> "K":
    s = _load_settings()
    kb = K(row_width=1)
    for item in s.get("links_menu", []):
        url = item.get("url", "")
        if _is_url_https(url):
            kb.add(B(item.get("label", url), url=url))
    kb.add(B("◀️ Назад", callback_data=CBT_HOME))
    return kb


def _stats_text() -> str:
    orders = _load_orders()
    now = time.time()
    day = _aggregate_profit(orders, now - 86400)
    week = _aggregate_profit(orders, now - 7 * 86400)
    month = _aggregate_profit(orders, now - 30 * 86400)
    def fmt(a):
        return f"{a['count']} шт · доход {a['revenue']} · профит {a['profit']}"
    return (
        f"<b>📊 Статистика AutoSMM</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📅 День: {fmt(day)}\n"
        f"🗓 Неделя: {fmt(week)}\n"
        f"📆 Месяц: {fmt(month)}"
    )


def init(cardinal: "Cardinal", *args) -> None:
    if not getattr(cardinal, "telegram", None):
        return
    tg = cardinal.telegram
    bot = tg.bot

    def _answer(call, text: str | None = None) -> None:
        try:
            bot.answer_callback_query(call.id, text or "")
        except Exception:
            pass

    def _persist_op(chat_id: int) -> None:
        s = _load_settings()
        if not s.get("operator_chat_id"):
            s["operator_chat_id"] = chat_id
            _save_settings(s)

    def _edit_home(call) -> None:
        try:
            bot.edit_message_text(_home_text(), call.message.chat.id, call.message.id,
                                  parse_mode="HTML", reply_markup=_home_kb())
        except Exception:
            bot.send_message(call.message.chat.id, _home_text(), parse_mode="HTML", reply_markup=_home_kb())

    def _edit_advanced(call) -> None:
        try:
            bot.edit_message_text(_advanced_text(), call.message.chat.id, call.message.id,
                                  parse_mode="HTML", reply_markup=_advanced_kb())
        except Exception:
            bot.send_message(call.message.chat.id, _advanced_text(), parse_mode="HTML", reply_markup=_advanced_kb())

    def _edit_or_send(call, text: str, kb) -> None:
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.id, parse_mode="HTML", reply_markup=kb)
        except Exception:
            bot.send_message(call.message.chat.id, text, parse_mode="HTML", reply_markup=kb)

    def open_settings_cb(call) -> None:
        _persist_op(call.message.chat.id)
        _edit_home(call)
        _answer(call)

    def home_cb(call) -> None:
        _edit_home(call)
        _answer(call)

    def advanced_cb(call) -> None:
        _edit_advanced(call)
        _answer(call)

    def help_cb(call) -> None:
        kb = K().add(B("◀️ Назад", callback_data=CBT_HOME))
        _edit_or_send(call, _help_text(), kb)
        _answer(call)

    def toggle_confirm_cb(call) -> None:
        s = _load_settings()
        s["confirm_link"] = not s.get("confirm_link", True)
        _save_settings(s)
        _answer(call, "🔁 " + ("вкл" if s["confirm_link"] else "выкл"))
        _edit_home(call)

    def toggle_refund_cb(call) -> None:
        s = _load_settings()
        s["auto_refund"] = not s.get("auto_refund", True)
        _save_settings(s)
        _answer(call, "💸 " + ("вкл" if s["auto_refund"] else "выкл"))
        _edit_home(call)

    def toggle_autolots_cb(call) -> None:
        s = _load_settings()
        s["auto_lots_enabled"] = not s.get("auto_lots_enabled", False)
        _save_settings(s)
        _answer(call, "🔁 " + ("вкл" if s["auto_lots_enabled"] else "выкл"))
        _edit_advanced(call)

    def toggle_neworder_cb(call) -> None:
        s = _load_settings()
        s["new_order_notifications"] = not s.get("new_order_notifications", False)
        _save_settings(s)
        _answer(call, "🔔 " + ("вкл" if s["new_order_notifications"] else "выкл"))
        _edit_advanced(call)

    def _make_numeric_editor(key: str, lo: int, hi: int, label: str):
        def cb(call) -> None:
            msg = bot.send_message(call.message.chat.id, f"{label}\nВведите число от {lo} до {hi}:")
            _answer(call)

            def handle(m) -> None:
                try:
                    v = int((m.text or "").strip())
                    if not lo <= v <= hi:
                        raise ValueError
                except Exception:
                    bot.reply_to(m, f"❌ Вне диапазона ({lo}–{hi}). Прежнее значение сохранено.")
                    return
                s = _load_settings()
                s[key] = v
                _save_settings(s)
                bot.reply_to(m, f"✅ Обновлено: <code>{v}</code>", parse_mode="HTML")
            bot.register_next_step_handler(msg, handle)
        return cb

    def stats_cb(call) -> None:
        kb = K().add(B("◀️ Назад", callback_data=CBT_HOME))
        try:
            bot.edit_message_text(_stats_text(), call.message.chat.id, call.message.id,
                                  parse_mode="HTML", reply_markup=kb)
        except Exception:
            bot.send_message(call.message.chat.id, _stats_text(), parse_mode="HTML", reply_markup=kb)
        _answer(call)

    def links_cb(call) -> None:
        s = _load_settings()
        kb = K(row_width=1)
        for item in s.get("links_menu", []):
            url = item.get("url", "")
            if _is_url_https(url):
                kb.add(B(item.get("label", url), url=url))
        # управляющие кнопки удаления
        for i, item in enumerate(s.get("links_menu", [])):
            kb.add(B(f"🗑 Удалить: {item.get('label', '')[:24]}", callback_data=f"{CBT_LINK_DEL}:{i}"))
        kb.add(B("➕ Добавить ссылку", callback_data=CBT_LINK_ADD))
        kb.add(B("◀️ Назад", callback_data=CBT_HOME))
        _edit_or_send(call, "🔗 Полезные ссылки (реф-ссылки и прочее):", kb)
        _answer(call)

    def active_cb(call) -> None:
        active = _load_active()
        kb = K(row_width=1)
        if not active:
            text = "📦 Активных заказов нет."
        else:
            lines = ["<b>📦 Активные заказы</b>", ""]
            for bid, a in list(active.items())[:30]:
                lines.append(f"• покупатель <code>{bid}</code> → заказ #{a.get('order_id_funpay')} (ID {a.get('provider_order_id')})")
                kb.add(B(f"🧹 Очистить {bid}", callback_data=f"{CBT_ACTIVE_CLEAR}:{bid}"))
            text = "\n".join(lines)
        kb.add(B("◀️ Назад", callback_data=CBT_HOME))
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.id, parse_mode="HTML", reply_markup=kb)
        except Exception:
            bot.send_message(call.message.chat.id, text, parse_mode="HTML", reply_markup=kb)
        _answer(call)

    def active_clear_cb(call) -> None:
        bid = call.data.split(":", 2)[-1]
        remove_buyer_active_order(bid)
        _answer(call, "🧹 Очищено")
        active_cb(call)

    def domains_cb(call) -> None:
        s = _load_settings()
        text = "🌐 Разрешённые домены:\n" + "\n".join(f"• {d}" for d in s.get("allowed_link_domains", []))
        text += "\n\nОтправьте «+домен» чтобы добавить или «-домен» чтобы удалить."
        kb = K().add(B("◀️ Назад", callback_data=CBT_ADVANCED))
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.id, reply_markup=kb)
        except Exception:
            bot.send_message(call.message.chat.id, text, reply_markup=kb)
        _answer(call)
        msg = bot.send_message(call.message.chat.id, "Введите «+домен» / «-домен» (или /cancel):")

        def handle(m) -> None:
            t = (m.text or "").strip()
            if t.startswith("/"):
                return
            s2 = _load_settings()
            domains = s2.get("allowed_link_domains", [])
            if t.startswith("+"):
                d = t[1:].strip().lower()
                if d and d not in domains:
                    domains.append(d)
                    s2["allowed_link_domains"] = domains
                    _save_settings(s2)
                    bot.reply_to(m, f"✅ Домен {d} добавлен.")
                else:
                    bot.reply_to(m, "ℹ️ Уже есть или пусто.")
            elif t.startswith("-"):
                d = t[1:].strip().lower()
                if d in domains:
                    domains.remove(d)
                    s2["allowed_link_domains"] = domains
                    _save_settings(s2)
                    bot.reply_to(m, f"✅ Домен {d} удалён.")
                else:
                    bot.reply_to(m, "ℹ️ Домен не найден.")
            else:
                bot.reply_to(m, "❌ Нужен формат «+домен» или «-домен».")
        bot.register_next_step_handler(msg, handle)

    def autolots_ids_cb(call) -> None:
        s = _load_settings()
        ids = s.get("auto_lots_extra_ids", [])
        if ids:
            text = "🆔 Доп. ID лотов для авто-поднятия:\n" + "\n".join(f"• <code>{i}</code>" for i in ids)
        else:
            text = "🆔 Доп. ID лотов для авто-поднятия:\n(список пуст)"
        text += "\n\nОтправьте «+id» чтобы добавить или «-id» чтобы удалить (только числа)."
        kb = K().add(B("◀️ Назад", callback_data=CBT_ADVANCED))
        _edit_or_send(call, text, kb)
        _answer(call)
        msg = bot.send_message(call.message.chat.id, "Введите «+id» / «-id» (или /cancel):")

        def handle(m) -> None:
            t = (m.text or "").strip()
            if t.startswith("/"):
                return
            if not (t.startswith("+") or t.startswith("-")):
                bot.reply_to(m, "❌ Нужен формат «+id» или «-id». Прежний список сохранён.")
                return
            raw = t[1:].strip()
            lot_id = _coerce_numeric_lot_id(raw)
            if lot_id is None:
                bot.reply_to(m, "❌ ID лота должен быть числом. Прежний список сохранён.")
                return
            s2 = _load_settings()
            ids2 = list(s2.get("auto_lots_extra_ids", []))
            # нормализуем к числам для сравнения/дедупликации
            norm = [_coerce_numeric_lot_id(x) for x in ids2]
            norm = [x for x in norm if x is not None]
            if t.startswith("+"):
                if lot_id in norm:
                    bot.reply_to(m, "ℹ️ Такой ID уже есть.")
                    return
                norm.append(lot_id)
                s2["auto_lots_extra_ids"] = norm
                _save_settings(s2)
                bot.reply_to(m, f"✅ ID {lot_id} добавлен.")
            else:  # удаление
                if lot_id not in norm:
                    bot.reply_to(m, "ℹ️ Такого ID нет.")
                    return
                norm = [x for x in norm if x != lot_id]
                s2["auto_lots_extra_ids"] = norm
                _save_settings(s2)
                bot.reply_to(m, f"✅ ID {lot_id} удалён.")
        bot.register_next_step_handler(msg, handle)

    def providers_cb(call) -> None:
        s = _load_settings()
        lines = ["<b>✏️ Поставщики SMM</b>", ""]
        kb = K(row_width=1)
        for p in s.get("providers", []):
            lines.append(f"• <b>{p.get('name')}</b> — <code>{p.get('api_url')}</code>\n  ключ: <code>{_mask_secret(p.get('api_key'))}</code>")
            kb.add(B(f"⚙️ {p.get('name')}", callback_data=f"{CBT_PROVIDER_VIEW}:{p.get('id')}"))
        if not s.get("providers"):
            lines.append("(пусто)")
        kb.add(B("➕ Добавить поставщика", callback_data=CBT_PROVIDER_ADD))
        kb.add(B("◀️ Назад", callback_data=CBT_HOME))
        _edit_or_send(call, "\n".join(lines), kb)
        _answer(call)

    def provider_view_cb(call) -> None:
        pid = call.data.split(":", 2)[-1]
        s = _load_settings()
        p = _find_provider(s, pid)
        if not p:
            _answer(call, "не найден")
            return providers_cb(call)
        text = (f"<b>⚙️ {p.get('name')}</b>\n"
                f"URL: <code>{p.get('api_url')}</code>\n"
                f"Ключ: <code>{_mask_secret(p.get('api_key'))}</code>")
        kb = K(row_width=2)
        kb.row(B("✏️ Имя", callback_data=f"{CBT_PROVIDER_EDIT_NAME}:{pid}"),
               B("🔗 URL", callback_data=f"{CBT_PROVIDER_EDIT_URL}:{pid}"))
        kb.row(B("🔑 Ключ", callback_data=f"{CBT_PROVIDER_EDIT_KEY}:{pid}"),
               B("💰 Баланс", callback_data=f"{CBT_PROVIDER_BAL}:{pid}"))
        kb.row(B("🗑 Удалить", callback_data=f"{CBT_PROVIDER_DEL}:{pid}"))
        kb.row(B("◀️ Назад", callback_data=CBT_PROVIDERS))
        _edit_or_send(call, text, kb)
        _answer(call)

    def provider_add_cb(call) -> None:
        msg = bot.send_message(call.message.chat.id, "➕ Введите название поставщика:")
        _answer(call)

        def step_name(m):
            name = (m.text or "").strip()[:64]
            if not name:
                return bot.reply_to(m, "❌ Пусто. Отменено.")
            msg2 = bot.reply_to(m, "🔗 Введите API URL (https://...):")

            def step_url(m2):
                url = (m2.text or "").strip()[:255]
                if not url.startswith("http"):
                    return bot.reply_to(m2, "❌ Некорректный URL. Отменено.")
                msg3 = bot.reply_to(m2, "🔑 Введите API-ключ:")

                def step_key(m3):
                    key = (m3.text or "").strip()[:255]
                    s = _load_settings()
                    s["providers"].append({
                        "id": _new_id("p", s["providers"]),
                        "name": name, "api_url": url, "api_key": key,
                    })
                    _save_settings(s)
                    bot.reply_to(m3, f"✅ Поставщик «{name}» добавлен.")

                bot.register_next_step_handler(msg3, step_key)

            bot.register_next_step_handler(msg2, step_url)

        bot.register_next_step_handler(msg, step_name)

    def _provider_field_editor(field: str, label: str, maxlen: int):
        def cb(call) -> None:
            pid = call.data.split(":", 2)[-1]
            msg = bot.send_message(call.message.chat.id, f"{label}")
            _answer(call)

            def handle(m):
                val = (m.text or "").strip()[:maxlen]
                s = _load_settings()
                p = _find_provider(s, pid)
                if not p:
                    return bot.reply_to(m, "❌ Поставщик не найден.")
                p[field] = val
                _save_settings(s)
                shown = _mask_secret(val) if field == "api_key" else val
                bot.reply_to(m, f"✅ Обновлено: <code>{shown}</code>", parse_mode="HTML")

            bot.register_next_step_handler(msg, handle)
        return cb

    def provider_del_cb(call) -> None:
        pid = call.data.split(":", 2)[-1]
        s = _load_settings()
        s["providers"] = [p for p in s.get("providers", []) if p.get("id") != pid]
        _save_settings(s)
        _answer(call, "🗑 Удалён")
        providers_cb(call)

    def provider_balance_cb(call) -> None:
        pid = call.data.split(":", 2)[-1]
        s = _load_settings()
        provider = _find_provider(s, pid)
        _answer(call, "⏳")
        if not provider:
            bot.send_message(call.message.chat.id, "❌ Поставщик не найден.")
            return
        try:
            data = _client_for_provider(provider).balance()
            bot.send_message(call.message.chat.id, f"💰 Баланс {provider.get('name')}: {data}")
        except Exception as e:
            bot.send_message(call.message.chat.id, f"❌ Ошибка получения баланса: {e}")

    def mappings_cb(call) -> None:
        s = _load_settings()
        lines = ["<b>🗺 Лоты ↔ услуги</b>", ""]
        kb = K(row_width=1)
        for m in s.get("lot_mappings", []):
            pname = (_find_provider(s, m.get("provider_id")) or {}).get("name", m.get("provider_id"))
            lines.append(f"• <code>{m.get('lot_match')}</code> → {pname}/svc {m.get('service_id')} ×{m.get('qty_multiplier', 1)}")
            kb.add(B(f"🗑 Удалить {m.get('lot_match')}", callback_data=f"{CBT_MAPPING_DEL}:{m.get('id')}"))
        if not s.get("lot_mappings"):
            lines.append("(пусто)")
        kb.add(B("➕ Добавить привязку", callback_data=CBT_MAPPING_ADD))
        kb.row(B("📤 Экспорт JSON", callback_data=CBT_MAPPING_EXPORT),
               B("📥 Импорт JSON", callback_data=CBT_MAPPING_IMPORT))
        kb.add(B("◀️ Назад", callback_data=CBT_HOME))
        _edit_or_send(call, "\n".join(lines), kb)
        _answer(call)

    def mapping_add_cb(call) -> None:
        s = _load_settings()
        providers = s.get("providers", [])
        if not providers:
            _answer(call, "нет поставщиков")
            return bot.send_message(call.message.chat.id,
                                    "❌ Сначала добавьте хотя бы одного поставщика.")
        msg = bot.send_message(
            call.message.chat.id,
            "➕ Введите <b>название лота</b> — точно как на FunPay (или его часть),\n"
            "либо числовой ID лота:",
            parse_mode="HTML")
        _answer(call)

        def _ask_service(m_reply, match: str, pid: str):
            pname = (_find_provider(_load_settings(), pid) or {}).get("name", pid)
            msg3 = bot.reply_to(m_reply, f"🏷 Поставщик: {pname}\n🔢 Введите service_id (число):")

            def step_svc(m3):
                try:
                    svc = int((m3.text or "").strip())
                except Exception:
                    return bot.reply_to(m3, "❌ Не число. Отменено.")
                msg4 = bot.reply_to(m3, "✖️ Введите множитель количества (например 1 или 1000):")

                def step_mult(m4):
                    try:
                        mult = float((m4.text or "").strip())
                        if mult <= 0:
                            raise ValueError
                    except Exception:
                        return bot.reply_to(m4, "❌ Некорректный множитель. Отменено.")
                    s2 = _load_settings()
                    s2["lot_mappings"].append({
                        "id": _new_id("m", s2["lot_mappings"]),
                        "lot_match": match, "provider_id": pid,
                        "service_id": svc, "qty_multiplier": mult,
                    })
                    _save_settings(s2)
                    pname2 = (_find_provider(s2, pid) or {}).get("name", pid)
                    bot.reply_to(m4, f"✅ Привязка добавлена: «{match}» → {pname2}/svc {svc} ×{mult}")

                bot.register_next_step_handler(msg4, step_mult)

            bot.register_next_step_handler(msg3, step_svc)

        def step_match(m):
            match = (m.text or "").strip()
            if not match:
                return bot.reply_to(m, "❌ Пусто. Отменено.")
            provs = _load_settings().get("providers", [])
            # один поставщик — выбираем автоматически, без лишнего вопроса
            if len(provs) == 1:
                _ask_service(m, match, provs[0].get("id"))
                return
            prov_hint = "\n".join(f"• <code>{p.get('id')}</code> — {p.get('name')}" for p in provs)
            msg2 = bot.reply_to(m, f"🏷 Введите id поставщика:\n{prov_hint}", parse_mode="HTML")

            def step_prov(m2):
                pid = (m2.text or "").strip()
                if not _find_provider(_load_settings(), pid):
                    return bot.reply_to(m2, "❌ Такого поставщика нет. Отменено.")
                _ask_service(m2, match, pid)

            bot.register_next_step_handler(msg2, step_prov)

        bot.register_next_step_handler(msg, step_match)

    def mapping_del_cb(call) -> None:
        mid = call.data.split(":", 2)[-1]
        s = _load_settings()
        s["lot_mappings"] = [m for m in s.get("lot_mappings", []) if m.get("id") != mid]
        _save_settings(s)
        _answer(call, "🗑 Удалено")
        mappings_cb(call)

    def mapping_export_cb(call) -> None:
        s = _load_settings()
        blob = json.dumps(s.get("lot_mappings", []), ensure_ascii=False, indent=2).encode("utf-8")
        _answer(call)
        try:
            import io
            bio = io.BytesIO(blob)
            bio.name = "lot_mappings.json"
            bot.send_document(call.message.chat.id, bio, caption="🗺 Текущие привязки (JSON)")
        except Exception:
            bot.send_message(call.message.chat.id, f"<pre>{_html_escape(blob.decode('utf-8'))}</pre>", parse_mode="HTML")

    def mapping_import_cb(call) -> None:
        msg = bot.send_message(call.message.chat.id,
                               "📥 Пришлите JSON-документ со списком привязок (заменит текущие):")
        _answer(call)

        def handle(m):
            try:
                if getattr(m, "document", None):
                    file_info = bot.get_file(m.document.file_id)
                    raw = bot.download_file(file_info.file_path)
                    data = json.loads(raw.decode("utf-8"))
                else:
                    data = json.loads((m.text or "").strip())
                if not isinstance(data, list):
                    raise ValueError("ожидался список")
            except Exception as e:
                return bot.reply_to(m, f"❌ Не удалось разобрать JSON: {e}. Прежние привязки сохранены.")
            s = _load_settings()
            s["lot_mappings"] = data
            _save_settings(s)
            bot.reply_to(m, f"✅ Импортировано привязок: {len(data)}")

        bot.register_next_step_handler(msg, handle)

    # ---------- редактор шаблонов сообщений ----------
    def msgs_cb(call) -> None:
        kb = K(row_width=1)
        for slot, label in _SLOT_LABELS.items():
            kb.add(B(f"📜 {label}", callback_data=f"{CBT_MSG_SLOT}:{slot}"))
        kb.add(B("◀️ Назад", callback_data=CBT_ADVANCED))
        _edit_or_send(call, "<b>📜 Шаблоны сообщений</b>\nВыберите слот для редактирования вариантов:", kb)
        _answer(call)

    def msg_slot_cb(call) -> None:
        slot = call.data.split(":", 2)[-1]
        s = _load_settings()
        variants = s["messages"].get(slot, [])
        lines = [f"<b>📜 {_SLOT_LABELS.get(slot, slot)}</b>", "Выбирается случайный вариант:", ""]
        kb = K(row_width=1)
        for i, v in enumerate(variants):
            lines.append(f"{i + 1}. {_html_escape(v[:60])}")
            if len(variants) > 1:  # запрещаем удалить последний (Req 7.3)
                kb.add(B(f"🗑 Удалить #{i + 1}", callback_data=f"{CBT_MSG_DEL}:{slot}:{i}"))
        kb.add(B("➕ Добавить вариант", callback_data=f"{CBT_MSG_ADD}:{slot}"))
        kb.add(B("◀️ Назад", callback_data=CBT_MSGS))
        _edit_or_send(call, "\n".join(lines), kb)
        _answer(call)

    def msg_add_cb(call) -> None:
        slot = call.data.split(":", 2)[-1]
        hint = ""
        if slot in ("after_confirmation", "success"):
            hint = "\nМожно использовать плейсхолдер {provider_order_id}."
        msg = bot.send_message(call.message.chat.id, f"➕ Введите новый вариант для «{_SLOT_LABELS.get(slot, slot)}».{hint}")
        _answer(call)

        def handle(m):
            text = (m.text or "").strip()
            if not text:
                return bot.reply_to(m, "❌ Пусто.")
            s = _load_settings()
            s["messages"].setdefault(slot, []).append(text)
            _save_settings(s)
            bot.reply_to(m, "✅ Вариант добавлен.")

        bot.register_next_step_handler(msg, handle)

    def msg_del_cb(call) -> None:
        _, _, rest = call.data.partition(f"{CBT_MSG_DEL}:")
        slot, _, idx = rest.partition(":")
        s = _load_settings()
        variants = s["messages"].get(slot, [])
        try:
            i = int(idx)
        except Exception:
            return _answer(call, "ошибка")
        if len(variants) <= 1:
            return _answer(call, "Нельзя удалить последний вариант")
        if 0 <= i < len(variants):
            variants.pop(i)
            _save_settings(s)
        _answer(call, "🗑 Удалено")
        # перерисовать слот
        call.data = f"{CBT_MSG_SLOT}:{slot}"
        msg_slot_cb(call)

    # ---------- редактор полезных ссылок ----------
    def link_add_cb(call) -> None:
        msg = bot.send_message(call.message.chat.id,
                               "➕ Введите ссылку в формате: Название | https://...")
        _answer(call)

        def handle(m):
            text = (m.text or "").strip()
            if "|" in text:
                label, url = [x.strip() for x in text.split("|", 1)]
            else:
                label, url = text, text
            if not _is_url_https(url):
                return bot.reply_to(m, "❌ Ссылка должна начинаться с https://. Прежний список сохранён.")
            s = _load_settings()
            s.setdefault("links_menu", []).append({"label": label or url, "url": url})
            _save_settings(s)
            bot.reply_to(m, "✅ Ссылка добавлена.")

        bot.register_next_step_handler(msg, handle)

    def link_del_cb(call) -> None:
        idx = call.data.split(":", 2)[-1]
        s = _load_settings()
        try:
            i = int(idx)
            if 0 <= i < len(s.get("links_menu", [])):
                s["links_menu"].pop(i)
                _save_settings(s)
        except Exception:
            pass
        _answer(call, "🗑 Удалено")
        links_cb(call)

    # --- регистрация ---
    tg.cbq_handler(open_settings_cb, lambda c: f"{CBT.PLUGIN_SETTINGS}:{UUID}" in (c.data or ""))
    tg.cbq_handler(home_cb, lambda c: c.data == CBT_HOME)
    tg.cbq_handler(advanced_cb, lambda c: c.data == CBT_ADVANCED)
    tg.cbq_handler(help_cb, lambda c: c.data == CBT_HELP)
    tg.cbq_handler(toggle_confirm_cb, lambda c: c.data == CBT_TOGGLE_CONFIRM)
    tg.cbq_handler(toggle_refund_cb, lambda c: c.data == CBT_TOGGLE_REFUND)
    tg.cbq_handler(toggle_autolots_cb, lambda c: c.data == CBT_TOGGLE_AUTOLOTS)
    tg.cbq_handler(toggle_neworder_cb, lambda c: c.data == CBT_TOGGLE_NEWORDER)
    tg.cbq_handler(_make_numeric_editor("auto_lots_interval_min", *RANGE_AUTO_LOTS_INTERVAL, "⏱ Интервал авто-поднятия лотов (мин)."),
                   lambda c: c.data == CBT_EDIT_AUTOLOTS_INT)
    tg.cbq_handler(autolots_ids_cb, lambda c: c.data == CBT_AUTOLOTS_IDS)
    tg.cbq_handler(_make_numeric_editor("link_wait_timeout_sec", *RANGE_LINK_TIMEOUT, "⏱ Таймаут ожидания ссылки (сек)."),
                   lambda c: c.data == CBT_EDIT_TIMEOUT)
    tg.cbq_handler(_make_numeric_editor("status_poll_interval_sec", *RANGE_POLL_INTERVAL, "🔄 Интервал опроса статусов (сек)."),
                   lambda c: c.data == CBT_EDIT_POLL)
    tg.cbq_handler(stats_cb, lambda c: c.data == CBT_STATS)
    tg.cbq_handler(links_cb, lambda c: c.data == CBT_LINKS)
    tg.cbq_handler(active_cb, lambda c: c.data == CBT_ACTIVE)
    tg.cbq_handler(active_clear_cb, lambda c: (c.data or "").startswith(f"{CBT_ACTIVE_CLEAR}:"))
    tg.cbq_handler(domains_cb, lambda c: c.data == CBT_DOMAINS)
    tg.cbq_handler(providers_cb, lambda c: c.data == CBT_PROVIDERS)
    tg.cbq_handler(provider_view_cb, lambda c: (c.data or "").startswith(f"{CBT_PROVIDER_VIEW}:"))
    tg.cbq_handler(provider_add_cb, lambda c: c.data == CBT_PROVIDER_ADD)
    tg.cbq_handler(provider_del_cb, lambda c: (c.data or "").startswith(f"{CBT_PROVIDER_DEL}:"))
    tg.cbq_handler(_provider_field_editor("name", "✏️ Введите новое имя:", 64),
                   lambda c: (c.data or "").startswith(f"{CBT_PROVIDER_EDIT_NAME}:"))
    tg.cbq_handler(_provider_field_editor("api_url", "🔗 Введите новый API URL:", 255),
                   lambda c: (c.data or "").startswith(f"{CBT_PROVIDER_EDIT_URL}:"))
    tg.cbq_handler(_provider_field_editor("api_key", "🔑 Введите новый API-ключ:", 255),
                   lambda c: (c.data or "").startswith(f"{CBT_PROVIDER_EDIT_KEY}:"))
    tg.cbq_handler(provider_balance_cb, lambda c: (c.data or "").startswith(f"{CBT_PROVIDER_BAL}:"))
    tg.cbq_handler(mappings_cb, lambda c: c.data == CBT_MAPPINGS)
    tg.cbq_handler(mapping_add_cb, lambda c: c.data == CBT_MAPPING_ADD)
    tg.cbq_handler(mapping_del_cb, lambda c: (c.data or "").startswith(f"{CBT_MAPPING_DEL}:"))
    tg.cbq_handler(mapping_export_cb, lambda c: c.data == CBT_MAPPING_EXPORT)
    tg.cbq_handler(mapping_import_cb, lambda c: c.data == CBT_MAPPING_IMPORT)
    tg.cbq_handler(msgs_cb, lambda c: c.data == CBT_MSGS)
    tg.cbq_handler(msg_slot_cb, lambda c: (c.data or "").startswith(f"{CBT_MSG_SLOT}:"))
    tg.cbq_handler(msg_add_cb, lambda c: (c.data or "").startswith(f"{CBT_MSG_ADD}:"))
    tg.cbq_handler(msg_del_cb, lambda c: (c.data or "").startswith(f"{CBT_MSG_DEL}:"))
    tg.cbq_handler(link_add_cb, lambda c: c.data == CBT_LINK_ADD)
    tg.cbq_handler(link_del_cb, lambda c: (c.data or "").startswith(f"{CBT_LINK_DEL}:"))

    # 💛 Донат-баннер (защита реквизитов автора)
    global _donation_cardinal
    _donation_cardinal = cardinal
    try:
        tg.cbq_handler(
            _donation_on_cb,
            lambda c: (c.data or "").startswith(DONATION_CALLBACK_PREFIX + ":"))
        _start_donation_reminder(cardinal)
    except Exception:
        logging.getLogger(__name__).debug("donation banner register failed",
                                          exc_info=True)
    # 📦 Одноразовое приветствие с рекламой канала автора
    if DONATION_SHOW_ON_START:
        try:
            _send_startup_welcome(cardinal)
        except Exception:
            logger.debug("startup welcome send failed", exc_info=True)


    # --- слэш-команда открытия меню ---
    def cmd_open(m) -> None:
        _persist_op(m.chat.id)
        try:
            bot.send_message(m.chat.id, _home_text(), reply_markup=_home_kb(), parse_mode="HTML")
        except Exception:
            logger.exception(f"{LOGGER_PREFIX} cmd_open failed")
    tg.msg_handler(cmd_open, commands=["autosmm"])
    try:
        cardinal.add_telegram_commands(UUID, [
            ("autosmm", "AutoSMM: открыть меню", True),
        ])
    except Exception:
        logger.exception("add_telegram_commands failed")

    # event handlers
    _ensure_poll_thread(cardinal)
    _ensure_timeout_thread(cardinal)
    _ensure_auto_lots_thread(cardinal)

    logger.info(f"{LOGGER_PREFIX} v{VERSION} запущен")


def _on_delete(cardinal: "Cardinal", *args) -> None:
    _poll_stop.set()
    _auto_lots_stop.set()


BIND_TO_PRE_INIT = [init]
BIND_TO_NEW_ORDER = [_on_new_order]
BIND_TO_NEW_MESSAGE = [_on_new_message]


# ─────────────────────────────────────────────────────────────────────────────
# Внутренние данные донат-баннера (внизу файла, закодированы + подпись):
# если реквизиты подменят на свои, подпись не сойдётся и баннер не отправится.
# ─────────────────────────────────────────────────────────────────────────────
_DONATION_SIGNATURE = "e7de0933f4b729405e4d55b5df9fc37b7dd39eafee1ff250d3005371cc24338a"


def _donation_details() -> dict:
    """Реквизиты донат-баннера (base64 + подпись — защита от подмены)."""
    import base64 as _b64
    import hashlib as _hl
    _raw = {
        "card": _b64.b64decode(
            "NDg3NCAwNzAwIDIzMDAgMDQ3Mg==").decode("utf-8"),
        "ton": _b64.b64decode(
            "VVFEYkpKTDd0cGxMU1hOdnVoQ29odDdOTnZfbHJ0U2ZCcmFyR2RIU2hsZFlNTmlK"
        ).decode("utf-8"),
        "usdt_ton": _b64.b64decode(
            "VVFEYkpKTDd0cGxMU1hOdnVoQ29odDdOTnZfbHJ0U2ZCcmFyR2RIU2hsZFlNTmlK"
        ).decode("utf-8"),
        "usdt": _b64.b64decode(
            "VFg2dVpmWkR0N1pHZmJhQThaVDhTRndkUERhTmRwRzlSNw==").decode("utf-8"),
        "contact": _b64.b64decode(
            "QHpha3VydWxpZmU=").decode("utf-8"),
    }
    _canon = "|".join(
        _raw[k] for k in ("card", "ton", "usdt_ton", "usdt", "contact"))
    if _hl.sha256(_canon.encode("utf-8")).hexdigest() != _DONATION_SIGNATURE:
        return {}
    return _raw
BIND_TO_DELETE = _on_delete
