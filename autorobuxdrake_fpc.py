"""
AutoRobux RBXCrate plugin for FunPay Cardinal.

Авто-выдача Robux через внешний RBXCrate/API-провайдер.

Поток:
  новый оплаченный заказ -> матчинг лота по названию -> бот просит Roblox username
  -> создаёт заказ у RBXCrate -> пишет покупателю/оператору результат.

Так как публичная документация RBXCrate не отдала читаемый API, HTTP-интеграция
сделана настраиваемой через Telegram-меню: base URL, токен, path, auth mode и
JSON payload с плейсхолдерами.
"""

from __future__ import annotations

import importlib
import csv
import io
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

_BOOT_LOGGER = logging.getLogger("FPC.autorobux_rbxcrate")


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
DONATION_CALLBACK_PREFIX = "arbx_dn"   # префикс колбэков кнопок баннера
DONATION_PLUGIN_NAME = "AutoRobux"     # имя плагина в шапке баннера
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


def _ensure_dependency(pip_name: str, import_name: str | None = None) -> bool:
    mod_name = import_name or pip_name
    try:
        importlib.import_module(mod_name)
        return True
    except ImportError:
        pass
    _BOOT_LOGGER.warning("autorobux_rbxcrate: ставлю зависимость %r...", pip_name)
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--quiet", pip_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
    except Exception as exc:
        _BOOT_LOGGER.error("Не удалось поставить %r: %s", pip_name, exc)
        return False
    importlib.invalidate_caches()
    try:
        importlib.import_module(mod_name)
        return True
    except ImportError:
        return False


_ensure_dependency("httpx")

import httpx
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


NAME = "autorobuxdrake"
VERSION = "1.0.0"
DESCRIPTION = "autorobuxdrake: RBXCrate API + встроенный GamePass buyout в одном файле."
CREDITS = "@drakelovc"
UUID = "6a3f7b34-7b7e-4d8a-a9e5-2f4c8d0b7a11"
SETTINGS_PAGE = True

logger = logging.getLogger(f"FPC.{__name__}")
LOGGER_PREFIX = "[AUTOROBUX_RBXCRATE]"

PLUGIN_DIR = Path("storage/plugins/autorobux_rbxcrate")
SETTINGS_PATH = PLUGIN_DIR / "settings.json"
ORDERS_PATH = PLUGIN_DIR / "orders.json"
BLACKLIST_PATH = PLUGIN_DIR / "blacklist.json"
BUYER_LANG_PATH = PLUGIN_DIR / "buyer_lang.json"
ACTIONS_LOG_PATH = PLUGIN_DIR / "actions.log"
PROBLEMS_PATH = PLUGIN_DIR / "problems.json"

DEFAULT_MESSAGES = {
    "ask_username": (
        "💎 Спасибо за оплату!\n"
        "К выдаче: {robux} Robux.\n\n"
        "Создайте GamePass на {gamepass_price} Robux, чтобы после комиссии Roblox 30% вышло {robux} Robux.\n"
        "Если Roblox не даёт включить продажу GamePass, сначала откройте настройки плейса и заполните Experience Rating / Questionnaire.\n"
        "После создания пришлите Roblox username + placeId/ссылку на плейс.\n"
        "Инструкция: https://telegra.ph/Kak-sozdat-GamePass-dlya-AutoRobux-07-06\n\n"
        "Пример:\n"
        "MyRobloxNick\n"
        "https://www.roblox.com/games/1234567890/..."
    ),
    "accepted": (
        "✅ Заказ принят в обработку.\n"
        "Robux будут начислены через RBXCrate. Статус можно уточнить у продавца."
    ),
    "failed": (
        "❌ Не удалось создать выдачу Robux автоматически.\n"
        "Продавец уже получил уведомление и проверит заказ вручную."
    ),
    "bad_username": "❗ Не смог распознать Roblox username и placeId. Пришлите ник и ссылку на плейс/числовой placeId.",
    "timeout": "⌛ Время на отправку Roblox username/placeId истекло. Напишите продавцу.",
    "blacklisted": "⛔ Ваш заказ не может быть обработан автоматически. Продавец проверит его вручную.",
    "completed": "✅ Robux успешно отправлены! Подтвердите заказ на FunPay.",
    "provider_error": "⚠️ RBXCrate сообщил об ошибке выдачи. Продавец уже уведомлён и проверит заказ.",
    "gamepass_not_found": "⚠️ GamePass пока не найден. Проверьте, что он создан, выставлен на продажу и находится на указанном плейсе. После исправления напишите !resend.",
    "seller_mismatch": "⚠️ GamePass должен принадлежать аккаунту {username}. Проверьте аккаунт и пришлите данные заново или напишите !resend после исправления.",
    "regional_pricing": "⚠️ Для GamePass включены региональные цены. Отключите их или создайте новый GamePass, затем напишите !resend.",
    "roblox_user_not_found": "⚠️ Roblox username не найден. Проверьте ник и пришлите данные заново.",
    "status": "📦 Статус заказа #{order_id}: {status}",
    "reminder": "⏳ Напоминание: пришлите Roblox username и placeId/ссылку на плейс для выдачи Robux.",
    "guide": (
        "📘 Как подготовить данные для выдачи Robux через GamePass:\n"
        "1. Открой Roblox Studio или страницу своего плейса.\n"
        "2. Создай GamePass на нужном плейсе.\n"
        "3. Включи продажу GamePass.\n"
        "4. Пришли сюда свой Roblox username и ссылку на плейс вида:\n"
        "https://www.roblox.com/games/1234567890/name\n"
        "Если кнопка продажи GamePass недоступна, сначала заполни Experience Rating / Questionnaire в настройках плейса.\n"
        "Подробная инструкция: https://telegra.ph/Kak-sozdat-GamePass-dlya-AutoRobux-07-06"
    ),
}

DEFAULT_MESSAGES_EN = {
    "ask_username": (
        "💎 Thanks for your payment!\n"
        "Delivery amount: {robux} Robux.\n\n"
        "Create a GamePass priced at {gamepass_price} Robux so after Roblox 30% fee you receive {robux} Robux.\n"
        "If Roblox does not allow you to put the GamePass on sale, complete the place Experience Rating / Questionnaire first.\n"
        "Then send your Roblox username and placeId/place link.\n"
        "Guide: https://telegra.ph/Kak-sozdat-GamePass-dlya-AutoRobux-07-06"
    ),
    "accepted": "✅ Order accepted. RBXCrate will process the Robux delivery.",
    "failed": "❌ Could not create the RBXCrate delivery automatically. Seller has been notified.",
    "bad_username": "❗ Could not parse Roblox username and placeId. Send username and place link/placeId.",
    "timeout": "⌛ Time to send Roblox username/placeId expired. Contact the seller.",
    "blacklisted": "⛔ Your order cannot be processed automatically. Seller will review it manually.",
    "completed": "✅ Robux delivered! Please confirm the FunPay order.",
    "provider_error": "⚠️ RBXCrate reported a delivery error. Seller has been notified.",
    "gamepass_not_found": "⚠️ GamePass not found yet. Make sure it is created and on sale, then type !resend.",
    "seller_mismatch": "⚠️ GamePass must belong to {username}. Check the account, then type !resend.",
    "regional_pricing": "⚠️ Regional pricing is enabled. Disable it or create a new GamePass, then type !resend.",
    "roblox_user_not_found": "⚠️ Roblox username not found. Check the username and send it again.",
    "status": "📦 Order #{order_id} status: {status}",
    "reminder": "⏳ Reminder: send Roblox username and placeId/place link for Robux delivery.",
    "guide": "📘 Create the GamePass, put it on sale, and send your Roblox username + place link. If selling is blocked, complete the place Experience Rating / Questionnaire first. Guide: https://telegra.ph/Kak-sozdat-GamePass-dlya-AutoRobux-07-06",
}

_SLOT_LABELS = {
    "ask_username": "Запрос данных RBXCrate",
    "accepted": "Успех / заказ создан",
    "failed": "Провал / API ошибка",
    "bad_username": "Неверные данные",
    "timeout": "Таймаут ожидания",
    "blacklisted": "Blacklist",
    "completed": "RBXCrate Completed",
    "provider_error": "RBXCrate Error",
    "gamepass_not_found": "GamePass not found",
    "seller_mismatch": "Seller mismatch",
    "regional_pricing": "Regional pricing",
    "roblox_user_not_found": "Roblox user not found",
    "status": "Команда !status",
    "reminder": "Напоминание покупателю",
    "guide": "Инструкция покупателю",
}

DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "rbxcrate_enabled": False,
    "gamepass_enabled": True,
    "test_mode": True,
    "auto_refund_on_api_error": False,
    "blacklist_enabled": True,
    "operator_chat_id": None,
    "wait_timeout_sec": 86400,
    "polling_enabled": True,
    "polling_interval_sec": 60,
    "auto_cancel_error_after_min": 0,
    "out_of_stock_policy": "wait",  # wait | refund | ask_operator
    "max_resend_attempts": 3,
    "auto_resend_gamepass_not_found": True,
    "blacklist_after_errors": 0,
    "low_balance_threshold_usd": 0,
    "low_stock_threshold_robux": 0,
    "reminder_after_sec": 600,
    "roblox_precheck_enabled": False,
    "manual_approve": False,
    "auto_resend_delay_sec": 300,
    "auto_resend_enabled": True,
    "auto_manage_funpay_lots": False,
    "min_robux": 1,
    "max_robux": 100000,
    "default_language": "ru",
    "auto_base_description_enabled": False,
    "base_description_keys": ["робукс", "robux", "game pass", "gamepass"],
    "lot_mappings": [],  # [{id, lot_match, lot_id?, robux_per_unit, extra_robux}]
    "api": {
        "base_url": "https://rbxcrate.com",
        "create_order_path": "/api/orders/gamepass",
        "order_info_path": "/api/orders/info",
        "cancel_order_path": "/api/orders/cancel",
        "resend_order_path": "/api/orders/gamepass/resend",
        "stock_path": "/api/orders/stock",
        "detailed_stock_path": "/api/orders/detailed-stock",
        "balance_path": "/api/shared/balance",
        "auth_mode": "api-key",
        "token": "",
        "timeout_sec": 30,
        "is_pre_order": True,
        "check_ownership": True,
    },
    "messages": dict(DEFAULT_MESSAGES),
    "messages_en": dict(DEFAULT_MESSAGES_EN),
}

_io_lock = threading.RLock()
_waiting_lock = threading.Lock()
_waiting: dict[str, dict] = {}
_stop = threading.Event()
_timeout_thread: threading.Thread | None = None

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")
PROFILE_RE = re.compile(r"roblox\.com/(?:users/(\d+)/(?:profile)?|@([A-Za-z0-9_]{3,20}))", re.I)
PLACE_RE = re.compile(r"(?:roblox\.com/(?:games|places)/(\d+)|[?&]placeId=(\d+)|\bplace(?:id)?\s*[:=]?\s*(\d+)\b)", re.I)


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
        logger.warning("%s Не удалось прочитать %s", LOGGER_PREFIX, path, exc_info=True)
        return json.loads(json.dumps(default))


def _save_json(path: Path, data) -> None:
    _ensure_dir()
    tmp = path.with_suffix(path.suffix + ".tmp")
    with _io_lock:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)


def _load_settings() -> dict[str, Any]:
    merged = json.loads(json.dumps(DEFAULT_SETTINGS))
    data = _load_json(SETTINGS_PATH, {})
    if isinstance(data, dict):
        for k, v in data.items():
            if k == "api" and isinstance(v, dict):
                merged["api"].update(v)
            elif k == "messages" and isinstance(v, dict):
                merged["messages"].update(v)
            elif k == "messages_en" and isinstance(v, dict):
                merged["messages_en"].update(v)
            else:
                merged[k] = v
    merged.setdefault("lot_mappings", [])
    merged.setdefault("api", json.loads(json.dumps(DEFAULT_SETTINGS["api"])))
    merged.setdefault("messages", dict(DEFAULT_MESSAGES))
    merged.setdefault("messages_en", dict(DEFAULT_MESSAGES_EN))
    ru_ask = str(merged.get("messages", {}).get("ask_username", ""))
    if "Experience Rating" not in ru_ask and "Создайте GamePass" in ru_ask and "telegra.ph/Kak-sozdat-GamePass" in ru_ask:
        merged.setdefault("messages", {})["ask_username"] = DEFAULT_MESSAGES["ask_username"]
    ru_guide = str(merged.get("messages", {}).get("guide", ""))
    if "Experience Rating" not in ru_guide and "telegra.ph/Kak-sozdat-GamePass" in ru_guide:
        merged.setdefault("messages", {})["guide"] = DEFAULT_MESSAGES["guide"]
    en_ask = str(merged.get("messages_en", {}).get("ask_username", ""))
    if "Experience Rating" not in en_ask and "Create a GamePass" in en_ask and "telegra.ph/Kak-sozdat-GamePass" in en_ask:
        merged.setdefault("messages_en", {})["ask_username"] = DEFAULT_MESSAGES_EN["ask_username"]
    en_guide = str(merged.get("messages_en", {}).get("guide", ""))
    if "Experience Rating" not in en_guide and "telegra.ph/Kak-sozdat-GamePass" in en_guide:
        merged.setdefault("messages_en", {})["guide"] = DEFAULT_MESSAGES_EN["guide"]
    return merged


def _load_buyer_lang() -> dict[str, str]:
    data = _load_json(BUYER_LANG_PATH, {})
    return data if isinstance(data, dict) else {}


def _save_buyer_lang(data: dict[str, str]) -> None:
    _save_json(BUYER_LANG_PATH, data)


def _set_buyer_lang(buyer_id: Any, lang: str) -> None:
    data = _load_buyer_lang()
    data[str(buyer_id)] = "en" if lang == "en" else "ru"
    _save_buyer_lang(data)


def _get_buyer_lang(buyer_id: Any, settings: dict) -> str:
    return _load_buyer_lang().get(str(buyer_id), settings.get("default_language", "ru"))


def _msg(settings: dict, key: str, buyer_id: Any = None) -> str:
    lang = _get_buyer_lang(buyer_id, settings) if buyer_id is not None else settings.get("default_language", "ru")
    if lang == "en":
        return settings.get("messages_en", {}).get(key, DEFAULT_MESSAGES_EN.get(key, DEFAULT_MESSAGES.get(key, "")))
    return settings.get("messages", {}).get(key, DEFAULT_MESSAGES.get(key, ""))


def _readiness(settings: dict) -> list[tuple[str, bool]]:
    api = settings.get("api") or {}
    return [
        ("API key", bool(api.get("token"))),
        ("Лоты", bool(settings.get("lot_mappings"))),
        ("Шаблоны RU", bool(settings.get("messages"))),
        ("Шаблоны EN", bool(settings.get("messages_en"))),
        ("Polling", bool(settings.get("polling_enabled"))),
        ("Min/Max корректны", int(settings.get("max_robux", 0) or 0) >= int(settings.get("min_robux", 0) or 0)),
    ]


def _save_settings(settings: dict[str, Any]) -> None:
    _save_json(SETTINGS_PATH, settings)


def _load_orders() -> list[dict]:
    data = _load_json(ORDERS_PATH, [])
    return data if isinstance(data, list) else []


def _save_orders(orders: list[dict]) -> None:
    _save_json(ORDERS_PATH, orders[-2000:])


def _record_order(entry: dict) -> None:
    orders = _load_orders()
    orders.append(entry)
    _save_orders(orders)


def _load_problems() -> list[dict]:
    data = _load_json(PROBLEMS_PATH, [])
    return data if isinstance(data, list) else []


def _save_problems(items: list[dict]) -> None:
    _save_json(PROBLEMS_PATH, items[-300:])


def _record_problem(kind: str, order_id: Any = None, buyer_username: str = "", lot_desc: str = "", detail: str = "") -> None:
    items = _load_problems()
    key = (str(kind), str(order_id or ""), str(lot_desc or ""))
    for item in reversed(items[-50:]):
        if (str(item.get("kind")), str(item.get("order_id") or ""), str(item.get("lot_desc") or "")) == key:
            item["updated_at"] = time.time()
            item["count"] = int(item.get("count", 1) or 1) + 1
            _save_problems(items)
            return
    items.append({
        "id": _new_id("p", items),
        "kind": kind,
        "order_id": order_id,
        "buyer_username": buyer_username,
        "lot_desc": lot_desc,
        "detail": detail,
        "created_at": time.time(),
        "updated_at": time.time(),
        "count": 1,
    })
    _save_problems(items)


def _find_last_order_for_buyer(buyer_id: Any = None, buyer_username: Any = None) -> dict | None:
    bid = str(buyer_id or "").strip()
    bun = str(buyer_username or "").casefold().strip()
    for order in reversed(_load_orders()):
        if bid and str(order.get("buyer_id") or "").strip() == bid:
            return order
        if bun and str(order.get("buyer_username") or "").casefold().strip() == bun:
            return order
    return None


def _find_order(order_id: Any) -> dict | None:
    for order in reversed(_load_orders()):
        if str(order.get("order_id")) == str(order_id):
            return order
    return None


def _save_order_update(order_id: Any, updates: dict[str, Any]) -> bool:
    orders = _load_orders()
    for order in orders:
        if str(order.get("order_id")) == str(order_id):
            order.update(updates)
            _save_orders(orders)
            return True
    return False


def _append_action(action: str, **fields) -> None:
    _ensure_dir()
    entry = {"ts": time.time(), "action": action, **fields}
    try:
        with _io_lock, open(ACTIONS_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        logger.debug("%s action log write failed", LOGGER_PREFIX, exc_info=True)


def _provider_price(order: dict) -> float:
    data = order.get("provider_response") or {}
    if isinstance(data, dict):
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        for key in ("price", "cost"):
            try:
                if payload.get(key) is not None:
                    return float(payload.get(key) or 0)
            except Exception:
                pass
    return 0.0


def _revenue_stats(orders: list[dict]) -> dict[str, float]:
    count = 0
    revenue = 0.0
    cost = 0.0
    robux = 0
    completed = 0
    failed = 0
    for order in orders:
        count += 1
        revenue += float(order.get("sold_price", 0) or 0)
        cost += _provider_price(order)
        robux += int(order.get("robux", 0) or 0)
        status = str(order.get("status") or "")
        if status == "Completed":
            completed += 1
        if status in {"failed", "Error", "Cancelled"}:
            failed += 1
    profit = revenue - cost
    margin = (profit / revenue * 100) if revenue > 0 else 0.0
    return {
        "count": count,
        "completed": completed,
        "failed": failed,
        "revenue": round(revenue, 2),
        "cost": round(cost, 2),
        "profit": round(profit, 2),
        "margin": round(margin, 2),
        "robux": robux,
    }


def _validate_template(slot: str, text: str) -> tuple[bool, str]:
    samples = {
        "ask_username": {"robux": 70, "gamepass_price": 100, "order_id": "TEST"},
        "seller_mismatch": {"username": "Player"},
        "status": {"order_id": "TEST", "status": "Queued"},
    }
    try:
        text.format(**samples.get(slot, {}))
    except Exception as exc:
        return False, str(exc)
    return True, ""


def _buyer_error_message(settings: dict, reason: str, username: str = "") -> str:
    key_by_reason = {
        "gamepass_not_found": "gamepass_not_found",
        "GAMEPASS_NOT_FOUND": "gamepass_not_found",
        "GAMEPASS_SELLER_MISMATCH": "seller_mismatch",
        "gamepass_seller_mismatch": "seller_mismatch",
        "REGIONAL_PRICING_NOT_SUPPORTED": "regional_pricing",
        "ROBLOX_USER_NOT_FOUND": "roblox_user_not_found",
    }
    slot = key_by_reason.get(str(reason), "provider_error")
    return _msg(settings, slot).format(username=username)


def _roblox_username_exists(username: str) -> bool | None:
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(
                "https://users.roblox.com/v1/usernames/users",
                json={"usernames": [username], "excludeBannedUsers": False},
            )
        if resp.status_code != 200:
            return None
        return bool((resp.json().get("data") or []))
    except Exception:
        return None


def _roblox_place_exists(place_id: int) -> bool | None:
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(f"https://apis.roblox.com/universes/v1/places/{int(place_id)}/universe")
        if resp.status_code == 200:
            return True
        if resp.status_code == 404:
            return False
        return None
    except Exception:
        return None


def _load_blacklist() -> list[dict]:
    data = _load_json(BLACKLIST_PATH, [])
    return data if isinstance(data, list) else []


def _save_blacklist(items: list[dict]) -> None:
    _save_json(BLACKLIST_PATH, items)


def _is_blacklisted(buyer_id: Any, buyer_username: Any) -> bool:
    bid = str(buyer_id or "").strip()
    bun = str(buyer_username or "").casefold().strip()
    for item in _load_blacklist():
        if bid and str(item.get("buyer_id") or "").strip() == bid:
            return True
        if bun and str(item.get("buyer_username") or "").casefold().strip() == bun:
            return True
    return False


def _add_blacklist(buyer_id: Any = None, buyer_username: Any = None, reason: str = "manual") -> bool:
    items = _load_blacklist()
    bid = str(buyer_id or "").strip()
    bun = str(buyer_username or "").strip()
    if not bid and not bun:
        return False
    if _is_blacklisted(bid, bun):
        return False
    items.append({"buyer_id": bid, "buyer_username": bun, "reason": reason, "created_at": time.time()})
    _save_blacklist(items)
    return True


def _remove_blacklist(key: str) -> bool:
    old = _load_blacklist()
    k = str(key or "").casefold().strip()
    new = [i for i in old if str(i.get("buyer_id") or "").casefold().strip() != k and str(i.get("buyer_username") or "").casefold().strip() != k]
    if len(new) == len(old):
        return False
    _save_blacklist(new)
    return True


def _html_escape(s: Any) -> str:
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _mask_secret(s: Any, head: int = 4, tail: int = 2) -> str:
    raw = str(s or "")
    if not raw:
        return "—"
    if len(raw) <= head + tail:
        return "***"
    return raw[:head] + "..." + raw[-tail:]


def _normalize_lot_text(text: Any) -> str:
    s = str(text or "").replace("\u200c", "").replace("\u200d", "")
    return " ".join(s.casefold().split())


def _new_id(prefix: str, existing: list[dict]) -> str:
    nums = []
    for item in existing:
        eid = str(item.get("id", ""))
        if eid.startswith(prefix):
            try:
                nums.append(int(eid[len(prefix):]))
            except Exception:
                pass
    return f"{prefix}{max(nums) + 1 if nums else 1}"


def _expected_robux(mapping: dict, amount: int) -> int:
    if mapping.get("fixed_robux") is not None:
        return int(mapping.get("fixed_robux") or 0) * int(amount or 1)
    return int(amount) * int(mapping.get("robux_per_unit", 0) or 0) + int(mapping.get("extra_robux", 0) or 0)


def _gamepass_price_for_net_robux(robux: int) -> int:
    value = int(robux)
    return (value * 10 + 6) // 7


def _match_mapping(settings: dict, lot_desc: str, lot_id: Any = None) -> dict | None:
    """Ищет привязку лота.

    Основной режим — Cardinal-style матчинг по названию/подстроке лота
    (`lot_match`). Реальный `lot_id` поддерживается как точный fallback, чтобы
    числовые названия лотов не путались с id категории FunPay.
    """
    lid = str(lot_id or "").strip()
    desc = _normalize_lot_text(lot_desc)

    # 1) Сначала название/подстрока лота — самый стабильный способ для order shortcut.
    for m in settings.get("lot_mappings", []):
        match_raw = str(m.get("lot_match", "")).strip()
        match = _normalize_lot_text(match_raw)
        if match and (match == desc or match in desc or desc in match):
            logger.debug("%s mapping найден по lot_match=%r", LOGGER_PREFIX, match_raw)
            return m

    # 2) Затем точный lot_id, если он указан в привязке и его удалось достать.
    if lid:
        for m in settings.get("lot_mappings", []):
            mapping_lot_id = str(m.get("lot_id", "") or "").strip()
            if mapping_lot_id and mapping_lot_id == lid:
                logger.debug("%s mapping найден по lot_id=%s", LOGGER_PREFIX, lid)
                return m

    # 3) Legacy fallback: раньше оператор мог ввести id прямо в lot_match.
    if lid:
        for m in settings.get("lot_mappings", []):
            match_raw = str(m.get("lot_match", "") or "").strip()
            if match_raw and match_raw == lid:
                logger.debug("%s mapping найден по legacy lot_match ID=%s", LOGGER_PREFIX, lid)
                return m

    if not settings.get("auto_base_description_enabled", False):
        return None
    return _auto_mapping_from_desc(settings, lot_desc)


def _auto_mapping_from_desc(settings: dict, lot_desc: str) -> dict | None:
    desc = _normalize_lot_text(lot_desc)
    keys = [_normalize_lot_text(k) for k in settings.get("base_description_keys", [])]
    if not any(k and k in desc for k in keys):
        return None
    patterns = (
        r"(\d{1,6})\s*(?:ед\.?\s*)?(?:робукс(?:ов|ы|а)?|robux|rbx)\b",
        r"(?:робукс(?:ов|ы|а)?|robux|rbx)\D{0,12}(\d{1,6})",
    )
    for pattern in patterns:
        m = re.search(pattern, desc, re.I)
        if m:
            return {
                "id": "auto_base",
                "lot_match": "auto:base_description_keys",
                "fixed_robux": int(m.group(1)),
                "robux_per_unit": 0,
                "extra_robux": 0,
                "auto": True,
            }
    return None


def _mapped_lot_ids(settings: dict) -> list[int]:
    ids: list[int] = []
    for mapping in settings.get("lot_mappings", []):
        raw = mapping.get("lot_id") or mapping.get("lot_match")
        if str(raw or "").isdigit():
            lid = int(raw)
            if lid not in ids:
                ids.append(lid)
    return ids


def _set_funpay_lot_active(cardinal: "Cardinal", lot_id: int, active: bool) -> bool:
    try:
        fields = cardinal.account.get_lot_fields(int(lot_id))
        fields.active = bool(active)
        cardinal.account.save_lot(fields)
        return True
    except Exception:
        logger.debug("%s set lot %s active=%s failed", LOGGER_PREFIX, lot_id, active, exc_info=True)
        return False


def _manage_funpay_lots_by_stock(cardinal: "Cardinal", settings: dict, robux_available: int | float | None) -> None:
    if not settings.get("auto_manage_funpay_lots"):
        return
    if robux_available is None:
        return
    want_active = float(robux_available) > 0
    for lot_id in _mapped_lot_ids(settings):
        if _set_funpay_lot_active(cardinal, lot_id, want_active):
            _notify_operator(cardinal, f"{'🟢 Включил' if want_active else '🔴 Отключил'} FunPay лот <code>{lot_id}</code> по stock={robux_available}")


def _extract_username(text: str) -> str | None:
    raw = (text or "").strip()
    if not raw:
        return None
    m = PROFILE_RE.search(raw)
    if m and m.group(2):
        return m.group(2)
    # Для ссылок /users/<id>/profile username из URL не достать безопасно.
    if m and m.group(1):
        return None
    candidate = raw.lstrip("@").strip()
    return candidate if USERNAME_RE.match(candidate) else None


def _extract_place_id(text: str) -> int | None:
    raw = text or ""
    for m in PLACE_RE.finditer(raw):
        for group in m.groups():
            if group and group.isdigit():
                return int(group)
    # Fallback: отдельная длинная цифра в сообщении. Roblox placeId обычно 6+ цифр.
    for token in re.findall(r"\b\d{6,}\b", raw):
        return int(token)
    return None


def _extract_order_inputs(text: str) -> tuple[str | None, int | None]:
    username = _extract_username(text)
    if not username:
        for token in re.findall(r"@?[A-Za-z0-9_]{3,20}", text or ""):
            candidate = token.lstrip("@")
            if candidate.lower() in {"roblox", "games", "places", "placeid", "www", "https", "http"}:
                continue
            if USERNAME_RE.match(candidate):
                username = candidate
                break
    return username, _extract_place_id(text)


def _api_url(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + "/" + path.lstrip("/")


def _rbxcrate_headers(api: dict) -> dict[str, str]:
    headers = {"User-Agent": f"FunPayCardinal AutoRobuxRBXCrate/{VERSION}", "Content-Type": "application/json"}
    token = str(api.get("token") or "").strip()
    mode = str(api.get("auth_mode") or "api-key").strip().lower()
    if token and mode == "api-key":
        headers["api-key"] = token
    elif token and mode == "bearer":
        headers["Authorization"] = f"Bearer {token}"
    elif token and mode == "x-api-key":
        headers["X-API-Key"] = token
    return headers


def _rbxcrate_create_order(settings: dict, order_ctx: dict[str, Any]) -> dict[str, Any]:
    api = settings.get("api") or {}
    base_url = str(api.get("base_url") or "").strip()
    path = str(api.get("create_order_path") or "").strip()
    if not base_url or not path:
        return {"ok": False, "reason": "RBXCrate API base/path не настроены", "data": {}}
    payload = {
        "orderId": str(order_ctx["order_id"]),
        "robloxUsername": str(order_ctx["username"]),
        "robuxAmount": int(order_ctx["robux"]),
        "placeId": int(order_ctx["place_id"]),
        "isPreOrder": bool(api.get("is_pre_order", True)),
        "checkOwnership": bool(api.get("check_ownership", True)),
    }
    if settings.get("test_mode", True):
        return {
            "ok": True,
            "reason": "test_mode: request not sent",
            "data": {"test_mode": True, "url": _api_url(base_url, path), "headers": {"api-key": "***"}, "payload": payload},
            "status_code": 0,
        }
    url = _api_url(base_url, path)
    try:
        timeout = float(api.get("timeout_sec") or 30)
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, headers=_rbxcrate_headers(api), json=payload)
        try:
            data = resp.json()
        except Exception:
            data = {"text": resp.text[:1000]}
        if 200 <= resp.status_code < 300:
            return {"ok": True, "reason": None, "data": data, "status_code": resp.status_code}
        if resp.status_code == 409 and isinstance(data, dict) and data.get("code") == "ORDER_ALREADY_EXISTS":
            return {"ok": True, "reason": "ORDER_ALREADY_EXISTS", "data": data, "status_code": resp.status_code}
        return {"ok": False, "reason": f"HTTP {resp.status_code}: {data}", "data": data, "status_code": resp.status_code}
    except Exception as exc:
        return {"ok": False, "reason": str(exc), "data": {}}


def _rbxcrate_order_info(settings: dict, order_id: Any) -> dict[str, Any]:
    api = settings.get("api") or {}
    path = str(api.get("order_info_path") or "").strip()
    if not path:
        return {"ok": False, "reason": "order_info_path пустой", "data": {}}
    try:
        with httpx.Client(timeout=float(api.get("timeout_sec") or 30)) as client:
            resp = client.post(
                _api_url(str(api.get("base_url") or ""), path),
                headers=_rbxcrate_headers(api),
                json={"orderId": str(order_id)},
            )
        try:
            data = resp.json()
        except Exception:
            data = {"text": resp.text[:1000]}
        return {"ok": 200 <= resp.status_code < 300, "reason": None if 200 <= resp.status_code < 300 else f"HTTP {resp.status_code}", "data": data}
    except Exception as exc:
        return {"ok": False, "reason": str(exc), "data": {}}


def _rbxcrate_cancel_order(settings: dict, order_id: Any) -> dict[str, Any]:
    api = settings.get("api") or {}
    path = str(api.get("cancel_order_path") or "").strip()
    if not path:
        return {"ok": False, "reason": "cancel_order_path пустой", "data": {}}
    try:
        with httpx.Client(timeout=float(api.get("timeout_sec") or 30)) as client:
            resp = client.post(
                _api_url(str(api.get("base_url") or ""), path),
                headers=_rbxcrate_headers(api),
                json={"orderId": str(order_id)},
            )
        try:
            data = resp.json()
        except Exception:
            data = {"text": resp.text[:1000]}
        return {"ok": 200 <= resp.status_code < 300, "reason": None if 200 <= resp.status_code < 300 else f"HTTP {resp.status_code}", "data": data}
    except Exception as exc:
        return {"ok": False, "reason": str(exc), "data": {}}


def _rbxcrate_resend_order(settings: dict, order_id: Any, place_id: Any) -> dict[str, Any]:
    api = settings.get("api") or {}
    path = str(api.get("resend_order_path") or "").strip()
    if not path:
        return {"ok": False, "reason": "resend_order_path пустой", "data": {}}
    try:
        with httpx.Client(timeout=float(api.get("timeout_sec") or 30)) as client:
            resp = client.post(
                _api_url(str(api.get("base_url") or ""), path),
                headers=_rbxcrate_headers(api),
                json={"orderId": str(order_id), "placeId": int(place_id)},
            )
        try:
            data = resp.json()
        except Exception:
            data = {"text": resp.text[:1000]}
        return {"ok": 200 <= resp.status_code < 300, "reason": None if 200 <= resp.status_code < 300 else f"HTTP {resp.status_code}", "data": data}
    except Exception as exc:
        return {"ok": False, "reason": str(exc), "data": {}}


def _rbxcrate_get_configured_path(settings: dict, path_key: str) -> dict[str, Any]:
    api = settings.get("api") or {}
    path = str(api.get(path_key) or "").strip()
    if not path:
        return {"ok": False, "reason": f"{path_key} не настроен", "data": {}}
    try:
        with httpx.Client(timeout=float(api.get("timeout_sec") or 30)) as client:
            resp = client.get(_api_url(str(api.get("base_url") or ""), path), headers=_rbxcrate_headers(api))
        try:
            data = resp.json()
        except Exception:
            data = {"text": resp.text[:1000]}
        return {"ok": 200 <= resp.status_code < 300, "reason": None if 200 <= resp.status_code < 300 else f"HTTP {resp.status_code}", "data": data}
    except Exception as exc:
        return {"ok": False, "reason": str(exc), "data": {}}


def _send_buyer(cardinal: "Cardinal", chat_id: Any, text: str) -> None:
    try:
        cardinal.send_message(chat_id, text)
    except Exception:
        logger.debug("%s send_buyer failed", LOGGER_PREFIX, exc_info=True)


def _notify_operator(cardinal: "Cardinal", text: str) -> None:
    settings = _load_settings()
    targets: list[Any] = []
    chat_id = settings.get("operator_chat_id")
    if chat_id:
        targets.append(chat_id)
    try:
        targets.extend(getattr(cardinal.telegram, "authorized_users", []) or [])
    except Exception:
        pass
    seen = set()
    for target in targets:
        if not target or str(target) in seen:
            continue
        seen.add(str(target))
        try:
            cardinal.telegram.bot.send_message(target, text, parse_mode="HTML")
        except Exception:
            logger.debug("%s notify_operator failed for %s", LOGGER_PREFIX, target, exc_info=True)


def _do_refund(cardinal: "Cardinal", order_id: Any, reason: str) -> None:
    try:
        cardinal.account.refund(order_id)
        _notify_operator(cardinal, f"💸 Авто-возврат #{order_id}.\nПричина: {_html_escape(reason)}")
    except Exception as exc:
        _notify_operator(cardinal, f"⚠️ Не удалось вернуть #{order_id}: {_html_escape(exc)}")


def _buyer_chat_id(cardinal: "Cardinal", order) -> Any:
    try:
        return cardinal.account.get_chat_by_name(order.buyer_username).id
    except Exception:
        return getattr(order, "chat_id", None) or getattr(order, "buyer_id", None)


def _extract_lot_id(cardinal: "Cardinal", order: Any) -> Any:
    """Пытается достать реальный id лота FunPay.

    Важно: `order.subcategory.id` — это id подкатегории, а не id лота, поэтому
    его намеренно не возвращаем. Сначала пробуем полный заказ Cardinal
    (`get_order_from_object`, как в steam_rental), затем account.get_order и
    HTML-паттерны.
    """
    order_id = getattr(order, "id", None)
    full_order = None

    try:
        getter = getattr(cardinal, "get_order_from_object", None)
        if callable(getter):
            full_order = getter(order)
    except Exception:
        logger.debug("%s get_order_from_object для lot_id не удался", LOGGER_PREFIX, exc_info=True)

    if full_order is None:
        try:
            getter = getattr(getattr(cardinal, "account", None), "get_order", None)
            if callable(getter) and order_id is not None:
                full_order = getter(order_id)
        except Exception:
            logger.debug("%s account.get_order для lot_id не удался", LOGGER_PREFIX, exc_info=True)

    if full_order is not None:
        for attr in ("lot_id", "subcategory_id"):
            lid = getattr(full_order, attr, None)
            if lid:
                logger.debug("%s lot_id из full_order.%s = %s", LOGGER_PREFIX, attr, lid)
                return str(lid)

        for attr in ("html", "raw_html"):
            html = getattr(full_order, attr, "") or ""
            for pat in (r'data-offer="(\d+)"', r"offer\?id=(\d+)", r"offers/(\d+)"):
                m = re.search(pat, html)
                if m:
                    logger.debug("%s lot_id из full_order.%s по паттерну %s", LOGGER_PREFIX, attr, pat)
                    return m.group(1)

    html = getattr(order, "html", "") or ""
    for pat in (r'data-offer="(\d+)"', r"offer\?id=(\d+)", r"offers/(\d+)"):
        m = re.search(pat, html)
        if m:
            logger.debug("%s lot_id из order.html по паттерну %s", LOGGER_PREFIX, pat)
            return m.group(1)

    sub = getattr(order, "subcategory", None)
    sub_id = getattr(sub, "id", None) if sub is not None else None
    if sub_id:
        logger.debug("%s subcategory.id=%s — это НЕ lot_id, пропускаю", LOGGER_PREFIX, sub_id)
    return None


def _on_new_order(cardinal: "Cardinal", event) -> None:
    try:
        settings = _load_settings()
        if not settings.get("enabled", True) or not settings.get("rbxcrate_enabled", False):
            return
        order = getattr(event, "order", None)
        if order is None:
            return
        order_id = getattr(order, "id", None)
        buyer_id = getattr(order, "buyer_id", None)
        buyer_username = getattr(order, "buyer_username", "") or ""
        if any(str(o.get("order_id")) == str(order_id) for o in _load_orders()):
            logger.info("%s заказ #%s уже обработан, пропускаю duplicate event", LOGGER_PREFIX, order_id)
            return
        lot_desc = getattr(order, "description", "") or getattr(order, "title", "") or ""
        amount = int(getattr(order, "amount", 1) or 1)
        price = float(getattr(order, "price", 0) or 0)
        lot_id = _extract_lot_id(cardinal, order)
        if settings.get("blacklist_enabled", True) and _is_blacklisted(buyer_id, buyer_username):
            chat_id = _buyer_chat_id(cardinal, order)
            _send_buyer(cardinal, chat_id, _msg(settings, "blacklisted", buyer_id))
            _notify_operator(cardinal, f"⛔ AutoRobux RBXCrate: заказ #{order_id} от <code>{_html_escape(buyer_username)}</code> заблокирован blacklist.")
            return
        mapping = _match_mapping(settings, lot_desc, lot_id)
        if not mapping:
            logger.info("%s заказ #%s игнорирован: lot_id=%s, desc='%s' не привязан", LOGGER_PREFIX, order_id, lot_id, lot_desc)
            _record_problem("rbxcrate_lot_not_found", order_id, buyer_username, lot_desc, "RBXCrate lot mapping not found")
            return
        robux = _expected_robux(mapping, amount)
        if robux < int(settings.get("min_robux", 1) or 1) or robux > int(settings.get("max_robux", 100000) or 100000):
            _notify_operator(cardinal, f"⚠️ Заказ #{order_id}: robux={robux} вне лимитов min/max, автообработка остановлена.")
            return
        gamepass_price = _gamepass_price_for_net_robux(robux)
        chat_id = _buyer_chat_id(cardinal, order)
        with _waiting_lock:
            _waiting[str(buyer_id)] = {
                "chat_id": chat_id,
                "order_id": order_id,
                "buyer_id": buyer_id,
                "buyer_username": buyer_username,
                "lot_id": lot_id,
                "lot_desc": lot_desc,
                "amount": amount,
                "sold_price": price,
                "robux": robux,
                "gamepass_price": gamepass_price,
                "ts": time.time(),
            }
        _send_buyer(cardinal, chat_id, _msg(settings, "ask_username", buyer_id).format(robux=robux, gamepass_price=gamepass_price, order_id=order_id))
        _notify_operator(cardinal, f"🟡 AutoRobux RBXCrate: заказ #{order_id}, к выдаче <code>{robux}</code> Robux. Ждём username + placeId.")
    except Exception:
        logger.warning("%s on_new_order error", LOGGER_PREFIX, exc_info=True)


def _on_new_message(cardinal: "Cardinal", event) -> None:
    try:
        msg = getattr(event, "message", None)
        if msg is None:
            return
        author_id = getattr(msg, "author_id", None)
        my_id = getattr(cardinal.account, "id", None)
        if author_id is not None and author_id == my_id:
            return
        text = (getattr(msg, "text", "") or "").strip()
        chat_id = getattr(msg, "chat_id", None)
        settings = _load_settings()
        if not settings.get("rbxcrate_enabled", False):
            return
        low = text.casefold()
        if low in {"!eng", "!en", "!english"}:
            _set_buyer_lang(author_id, "en")
            _send_buyer(cardinal, chat_id, "✅ Language switched to English.")
            return
        if low in {"!ru", "!rus", "!рус"}:
            _set_buyer_lang(author_id, "ru")
            _send_buyer(cardinal, chat_id, "✅ Язык переключён на русский.")
            return
        if low in {"!guide", "!гайд", "!help", "!помощь"}:
            _send_buyer(cardinal, chat_id, _msg(settings, "guide", author_id))
            return
        if low in {"!status", "!статус"}:
            last = _find_last_order_for_buyer(author_id, None)
            if last:
                _send_buyer(cardinal, chat_id, _msg(settings, "status", author_id).format(order_id=last.get("order_id"), status=last.get("status")))
            return
        if low in {"!resend", "!перепроверить", "!повтор"}:
            last = _find_last_order_for_buyer(author_id, None)
            if last and last.get("place_id"):
                attempts = int(last.get("resend_attempts", 0) or 0)
                if attempts >= int(settings.get("max_resend_attempts", 3) or 3):
                    _send_buyer(cardinal, chat_id, "❌ Лимит повторных проверок исчерпан. Напишите продавцу.")
                    return
                res = _rbxcrate_resend_order(settings, last.get("order_id"), last.get("place_id"))
                _save_order_update(last.get("order_id"), {"resend_attempts": attempts + 1, "last_resend_response": res, "updated_at": time.time()})
                _send_buyer(cardinal, chat_id, "🔁 Отправил заказ на повторную проверку." if res.get("ok") else "❌ Не удалось отправить повторную проверку. Продавец уведомлён.")
                _notify_operator(cardinal, f"🔁 Resend по #{last.get('order_id')}: <code>{_html_escape(res.get('reason') or res.get('data'))}</code>")
            return
        with _waiting_lock:
            w = _waiting.get(str(author_id))
        if not w:
            return
        if any(q in low for q in ("за сколько", "сколько став", "какая цена", "цена пас", "price", "how much")):
            _send_buyer(cardinal, w["chat_id"], f"Создайте GamePass на ровно {int(w.get('gamepass_price') or 0)} Robux.")
            return
        username, place_id = _extract_order_inputs(text)
        if w.get("username") and not username:
            username = w.get("username")
        if username and not place_id:
            with _waiting_lock:
                w["username"] = username
                _waiting[str(author_id)] = w
            _send_buyer(cardinal, w["chat_id"], "✅ Username принят. Теперь пришлите placeId или ссылку на плейс.")
            return
        if not username or not place_id:
            _send_buyer(cardinal, w["chat_id"], _msg(settings, "bad_username", author_id))
            return
        if settings.get("roblox_precheck_enabled"):
            user_ok = _roblox_username_exists(username)
            place_ok = _roblox_place_exists(place_id)
            if user_ok is False:
                _send_buyer(cardinal, w["chat_id"], _msg(settings, "roblox_user_not_found", author_id))
                return
            if place_ok is False:
                _send_buyer(cardinal, w["chat_id"], "❗ Плейс не найден. Проверьте placeId/ссылку на плейс.")
                return
        ctx = {
            "username": username,
            "place_id": int(place_id),
            "robux": int(w["robux"]),
            "gamepass_price": int(w.get("gamepass_price") or _gamepass_price_for_net_robux(int(w["robux"]))),
            "order_id": w["order_id"],
            "buyer_id": w.get("buyer_id"),
            "buyer_username": w.get("buyer_username") or "",
            "lot_id": w.get("lot_id"),
            "lot_desc": w.get("lot_desc") or "",
            "amount": w.get("amount") or 1,
        }
        res = _rbxcrate_create_order(settings, ctx)
        with _waiting_lock:
            _waiting.pop(str(author_id), None)
        record = {
            **ctx,
            "status": (res.get("data", {}).get("data", {}) or res.get("data", {})).get("status") or ("accepted" if res.get("ok") else "failed"),
            "provider_response": res.get("data"),
            "reason": res.get("reason"),
            "sold_price": w.get("sold_price", 0),
            "chat_id": w.get("chat_id"),
            "created_at": time.time(),
        }
        _record_order(record)
        if res.get("ok"):
            _send_buyer(cardinal, w["chat_id"], _msg(settings, "accepted", author_id))
            prefix = "🧪 TEST payload сформирован" if res.get("data", {}).get("test_mode") else "✅ RBXCrate заказ создан"
            _notify_operator(cardinal, f"{prefix}: FunPay #{w['order_id']} -> <code>{_html_escape(username)}</code>, place <code>{place_id}</code>, <code>{w['robux']}</code> Robux.")
        else:
            _send_buyer(cardinal, w["chat_id"], _msg(settings, "failed", author_id))
            _notify_operator(cardinal, f"❌ RBXCrate ошибка по #{w['order_id']}: <code>{_html_escape(res.get('reason'))}</code>")
            _record_problem("rbxcrate_api_error", w.get("order_id"), w.get("buyer_username") or "", w.get("lot_desc") or "", str(res.get("reason") or res.get("data") or ""))
            reason_text = str(res.get("reason") or res.get("data") or "")
            if "OUT_OF_STOCK" in reason_text or "HTTP 402" in reason_text:
                policy = str(settings.get("out_of_stock_policy") or "wait")
                if policy == "refund":
                    _do_refund(cardinal, w["order_id"], "RBXCrate OUT_OF_STOCK")
                elif policy == "ask_operator":
                    _notify_operator(cardinal, f"📦 OUT_OF_STOCK по #{w['order_id']}. Политика: спросить оператора, возврат не выполнен.")
            if settings.get("auto_refund_on_api_error"):
                _do_refund(cardinal, w["order_id"], str(res.get("reason") or "api error"))
    except Exception:
        logger.warning("%s on_new_message error", LOGGER_PREFIX, exc_info=True)


def _timeout_loop(cardinal: "Cardinal") -> None:
    last_poll = 0.0
    while not _stop.is_set():
        time.sleep(10)
        try:
            settings = _load_settings()
            timeout = float(settings.get("wait_timeout_sec", 86400) or 86400)
            now = time.time()
            expired = []
            with _waiting_lock:
                for key, w in list(_waiting.items()):
                    reminder_after = float(settings.get("reminder_after_sec", 600) or 0)
                    if reminder_after > 0 and not w.get("reminded") and now - float(w.get("ts", now)) >= reminder_after:
                        _send_buyer(cardinal, w["chat_id"], _msg(settings, "reminder", w.get("buyer_id")))
                        w["reminded"] = True
                        _waiting[key] = w
                    if now - float(w.get("ts", now)) >= timeout:
                        expired.append(w)
                        _waiting.pop(key, None)
            for w in expired:
                _send_buyer(cardinal, w["chat_id"], _msg(settings, "timeout", w.get("buyer_id")))
                _notify_operator(cardinal, f"⌛ Таймаут username по заказу #{w.get('order_id')}")
            if settings.get("polling_enabled", True) and now - last_poll >= float(settings.get("polling_interval_sec", 60) or 60):
                last_poll = now
                orders = _load_orders()
                changed = False
                for order in orders:
                    if str(order.get("status")) not in {"accepted", "Pending", "Queued", "Queued_Deferred"}:
                        continue
                    info = _rbxcrate_order_info(settings, order.get("order_id"))
                    if not info.get("ok"):
                        continue
                    data = info.get("data") or {}
                    payload = data.get("data") if isinstance(data.get("data"), dict) else data
                    new_status = payload.get("status") or payload.get("order", {}).get("status")
                    if new_status and new_status != order.get("status"):
                        order["status"] = new_status
                        order["provider_response"] = data
                        order["updated_at"] = time.time()
                        changed = True
                        _notify_operator(cardinal, f"🔄 RBXCrate #{order.get('order_id')}: статус <code>{_html_escape(new_status)}</code>")
                        if new_status == "Completed" and order.get("chat_id") and not order.get("buyer_completed_notified"):
                            _send_buyer(cardinal, order.get("chat_id"), _msg(settings, "completed", order.get("buyer_id")))
                            order["buyer_completed_notified"] = True
                        if new_status == "Error":
                            err = payload.get("error") or {}
                            reason = err.get("reason") or payload.get("reason") or "unknown_error"
                            msg = err.get("message") or payload.get("message") or ""
                            order["error_seen_at"] = order.get("error_seen_at") or time.time()
                            _notify_operator(cardinal, f"⚠️ RBXCrate #{order.get('order_id')}: Error <code>{_html_escape(reason)}</code> {_html_escape(msg)}")
                            if order.get("chat_id") and not order.get("buyer_error_notified"):
                                _send_buyer(cardinal, order.get("chat_id"), _msg(settings, "provider_error", order.get("buyer_id")))
                                order["buyer_error_notified"] = True
                            threshold = int(settings.get("blacklist_after_errors", 0) or 0)
                            if threshold > 0 and order.get("buyer_id"):
                                buyer_errors = [o for o in orders if str(o.get("buyer_id")) == str(order.get("buyer_id")) and str(o.get("status")) == "Error"]
                                if len(buyer_errors) >= threshold:
                                    _add_blacklist(buyer_id=order.get("buyer_id"), buyer_username=order.get("buyer_username"), reason=f"{len(buyer_errors)} RBXCrate errors")
                    if str(order.get("status")) == "Error":
                        err_seen = float(order.get("error_seen_at") or time.time())
                        if not order.get("error_seen_at"):
                            order["error_seen_at"] = err_seen
                            changed = True
                        cancel_after = int(settings.get("auto_cancel_error_after_min", 0) or 0)
                        if cancel_after > 0 and now - err_seen >= cancel_after * 60 and not order.get("auto_cancel_attempted"):
                            cancel_res = _rbxcrate_cancel_order(settings, order.get("order_id"))
                            order["auto_cancel_attempted"] = True
                            order["auto_cancel_response"] = cancel_res
                            changed = True
                            _notify_operator(cardinal, f"🛑 Auto-cancel RBXCrate #{order.get('order_id')}: <code>{_html_escape(cancel_res.get('reason') or cancel_res.get('data'))}</code>")
                if changed:
                    _save_orders(orders)
                bal_threshold = float(settings.get("low_balance_threshold_usd", 0) or 0)
                if bal_threshold > 0 and settings.get("api", {}).get("token"):
                    bal = _rbxcrate_get_configured_path(settings, "balance_path")
                    amount = (bal.get("data") or {}).get("balance")
                    if isinstance(amount, (int, float)) and amount < bal_threshold:
                        _notify_operator(cardinal, f"💰 Низкий RBXCrate баланс: <code>{amount}</code> USD")
                stock_threshold = int(settings.get("low_stock_threshold_robux", 0) or 0)
                if stock_threshold > 0 and settings.get("api", {}).get("token"):
                    stock = _rbxcrate_get_configured_path(settings, "stock_path")
                    amount = (stock.get("data") or {}).get("robuxAvailable")
                    if isinstance(amount, (int, float)) and amount < stock_threshold:
                        _notify_operator(cardinal, f"📦 Низкий RBXCrate stock: <code>{amount}</code> Robux")
                    if isinstance(amount, (int, float)):
                        _manage_funpay_lots_by_stock(cardinal, settings, amount)
                elif settings.get("auto_manage_funpay_lots") and settings.get("api", {}).get("token"):
                    stock = _rbxcrate_get_configured_path(settings, "stock_path")
                    amount = (stock.get("data") or {}).get("robuxAvailable")
                    if isinstance(amount, (int, float)):
                        _manage_funpay_lots_by_stock(cardinal, settings, amount)
        except Exception:
            logger.debug("%s timeout loop error", LOGGER_PREFIX, exc_info=True)


def _ensure_timeout_thread(cardinal: "Cardinal") -> None:
    global _timeout_thread
    if _timeout_thread and _timeout_thread.is_alive():
        return
    _stop.clear()
    _timeout_thread = threading.Thread(target=_timeout_loop, args=(cardinal,), name="autorobux-rbxcrate-timeout", daemon=True)
    _timeout_thread.start()


CBP = "autorobux_rbxcrate"
CBT_HOME = f"{CBP}:home"
CBT_RBXCRATE_SECTION = f"{CBP}:section:rbxcrate"
CBT_GAMEPASS_SECTION = f"{CBP}:section:gamepass"
CBT_MODE_SCREEN = f"{CBP}:mode_screen"
CBT_MODE_RBXCRATE = f"{CBP}:mode:rbxcrate"
CBT_MODE_GAMEPASS = f"{CBP}:mode:gamepass"
CBT_MODE_PAUSE = f"{CBP}:mode:pause"
CBT_MODE_TEST = f"{CBP}:mode:test"
GP_CBT_MAPPINGS = "autorobux:mappings"
GP_CBT_MAPPING_ADD = "autorobux:madd"
GP_CBT_TEST_DESC = f"{CBP}:gp_test_desc"
GP_CBT_QUICK_ADD = f"{CBP}:gp_quick_add"
GP_CBT_MAPPING_VIEW = f"{CBP}:gp_mapping_view"
GP_CBT_MAPPING_EDIT = f"{CBP}:gp_mapping_edit"
GP_CBT_MAPPING_DEL = f"{CBP}:gp_mapping_del"
CBT_SETTINGS = f"{CBP}:settings"
CBT_CONFIG = f"{CBP}:config"
CBT_HEALTH = f"{CBP}:health"
CBT_API = f"{CBP}:api"
CBT_TOGGLE = f"{CBP}:toggle"
CBT_TEST_MODE = f"{CBP}:test_mode"
CBT_AUTO_BASE = f"{CBP}:auto_base"
CBT_REFUND = f"{CBP}:refund"
CBT_BALANCE = f"{CBP}:balance"
CBT_STOCK = f"{CBP}:stock"
CBT_DETAILED_STOCK = f"{CBP}:detailed_stock"
CBT_CANCEL = f"{CBP}:cancel"
CBT_RESEND = f"{CBP}:resend"
CBT_LOTS = f"{CBP}:lots"
CBT_LOT_ADD = f"{CBP}:lot_add"
CBT_LOT_DEL = f"{CBP}:lot_del"
CBT_MSGS = f"{CBP}:msgs"
CBT_MSG_EDIT = f"{CBP}:msg_edit"
CBT_MSG_RESET = f"{CBP}:msg_reset"
CBT_ORDERS = f"{CBP}:orders"
CBT_ORDER_VIEW = f"{CBP}:order_view"
CBT_ORDER_ACT = f"{CBP}:order_act"
CBT_QUEUE = f"{CBP}:queue"
CBT_MARGIN = f"{CBP}:margin"
CBT_EXPORT_JSON = f"{CBP}:export_json"
CBT_EXPORT_CSV = f"{CBP}:export_csv"
CBT_ORDER_INFO = f"{CBP}:order_info"
CBT_API_EDIT = f"{CBP}:api_edit"
CBT_BLACKLIST = f"{CBP}:blacklist"
CBT_BL_ADD = f"{CBP}:bl_add"
CBT_BL_DEL = f"{CBP}:bl_del"
CBT_REFUND_MANUAL = f"{CBP}:refund_manual"
CBT_TIMEOUT = f"{CBP}:timeout"
CBT_POLLING = f"{CBP}:polling"
CBT_POLLING_INTERVAL = f"{CBP}:polling_interval"
CBT_AUTOCANCEL = f"{CBP}:autocancel"
CBT_OOS_POLICY = f"{CBP}:oos_policy"
CBT_MANAGE_LOTS = f"{CBP}:manage_lots"
CBT_LIMITS = f"{CBP}:limits"
CBT_LANG = f"{CBP}:lang"
CBT_CONFIG_EXPORT = f"{CBP}:config_export"
CBT_CONFIG_IMPORT = f"{CBP}:config_import"
CBT_HELP = f"{CBP}:help"
CBT_PROBLEMS = f"{CBP}:problems"
CBT_PROBLEM_ACT = f"{CBP}:problem_act"
CBT_CALC = f"{CBP}:calc"
CBT_SIMULATE = f"{CBP}:simulate"
CBT_TEMPLATE_PRESETS = f"{CBP}:template_presets"
CBT_TEMPLATE_PRESET_APPLY = f"{CBP}:template_preset_apply"
CBT_REFUND_CONFIRM = f"{CBP}:refund_confirm"

API_FIELDS = {
    "base_url": "API base URL",
    "create_order_path": "Create order path",
    "order_info_path": "Order info path",
    "cancel_order_path": "Cancel order path",
    "resend_order_path": "Resend order path",
    "stock_path": "Stock path",
    "detailed_stock_path": "Detailed stock path",
    "balance_path": "Balance path",
    "auth_mode": "Auth mode: api-key (official)",
    "token": "API token/key",
    "is_pre_order": "isPreOrder: true/false",
    "check_ownership": "checkOwnership: true/false",
}


def _home_text() -> str:
    s = _load_settings()
    api = s.get("api") or {}
    return (
        f"<b>💎 autorobuxdrake RBXCrate v{VERSION}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Статус: {'🟢 вкл' if s.get('enabled') else '🔴 выкл'}\n"
        f"Тестовый режим: {'🟢 ON' if s.get('test_mode', True) else '🔴 OFF'}\n"
        f"Лотов: <code>{len(s.get('lot_mappings', []))}</code>\n"
        f"API: <code>{_html_escape(api.get('base_url'))}</code>\n"
        f"Auth: <code>{_html_escape(api.get('auth_mode'))}</code>, token: <code>{_mask_secret(api.get('token'))}</code>\n"
        f"Авто-возврат при API ошибке: {'🟢' if s.get('auto_refund_on_api_error') else '🔴'}\n"
        f"Blacklist: {'🟢' if s.get('blacklist_enabled', True) else '🔴'}\n"
        f"Таймаут: <code>{int(s.get('wait_timeout_sec', 86400))}</code> сек"
    )


def _home_kb() -> K:
    kb = K(row_width=2)
    kb.row(B("📦 Заказы", callback_data=CBT_ORDERS), B("📋 Очередь", callback_data=CBT_QUEUE))
    kb.row(B("📊 Маржа", callback_data=CBT_MARGIN))
    kb.row(B("⚙️ Настройки", callback_data=CBT_SETTINGS), B("📄 Конфиг", callback_data=CBT_CONFIG))
    kb.row(B("📘 Помощь", callback_data=CBT_HELP), B("🩺 Health", callback_data=CBT_HEALTH))
    kb.row(B("📦 Stock", callback_data=CBT_STOCK))
    kb.row(B("📊 Detailed stock", callback_data=CBT_DETAILED_STOCK), B("💰 Баланс", callback_data=CBT_BALANCE))
    kb.row(B("🔗 RBXCrate (реф)", url="https://rbxcrate.com/r/drake42"))
    kb.row(B("💛 Донат", callback_data=f"{DONATION_CALLBACK_PREFIX}:donate"))
    kb.row(B("⬅️ К разделам", callback_data=CBT_HOME))
    return kb


def _main_text() -> str:
    s = _load_settings()
    rbx_on = bool(s.get("rbxcrate_enabled", False))
    gp_on = bool(s.get("gamepass_enabled", True))
    mode = "💎 RBXCrate" if rbx_on else "🎮 GamePass аккаунты" if gp_on else "⏸ пауза"
    orders = _load_orders()
    active = sum(1 for o in orders if str(o.get("status")) in {"waiting_input", "accepted", "Pending", "Queued", "Queued_Deferred"})
    errors = sum(1 for o in orders if "error" in str(o.get("status", "")).casefold() or str(o.get("status")) == "failed")
    api = s.get("api") or {}
    gp_accounts = 0
    gp_lots = 0
    try:
        gp_settings = _load_embedded_gamepass()._load_settings()
        gp_accounts = len(gp_settings.get("accounts", []))
        gp_lots = len(gp_settings.get("lot_mappings", []))
    except Exception:
        pass
    return (
        f"<b>💎 autorobuxdrake v{VERSION}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Режим: <b>{mode}</b>\n"
        f"Статус: {'🟢 авто-выдача включена' if s.get('enabled', True) and (rbx_on or gp_on) else '🔴 авто-выдача выключена'}\n"
        f"Тест RBXCrate: {'🟢 ON — реальные запросы НЕ отправляются' if s.get('test_mode', True) else '🔴 OFF — реальные заказы уходят в API'}\n"
        f"Очередь RBXCrate: <code>{active}</code> активных / <code>{errors}</code> ошибок\n\n"
        "<b>GamePass</b>\n"
        f"Аккаунтов: <code>{gp_accounts}</code> | Привязок: <code>{gp_lots}</code>\n\n"
        "<b>RBXCrate</b>\n"
        f"API key: <code>{_mask_secret(api.get('token'))}</code> | Привязок: <code>{len(s.get('lot_mappings', []))}</code>"
    )


def _main_kb() -> K:
    kb = K(row_width=2)
    kb.row(B("📦 Заказы", callback_data=CBT_ORDERS), B("🧯 Проблемы", callback_data=CBT_PROBLEMS))
    kb.row(B("🎛 Режим", callback_data=CBT_MODE_SCREEN), B("🧮 Калькулятор", callback_data=CBT_CALC))
    kb.row(B("🎮 GamePass", callback_data=CBT_GAMEPASS_SECTION), B("💎 RBXCrate", callback_data=CBT_RBXCRATE_SECTION))
    kb.row(B("🧪 Симуляция", callback_data=CBT_SIMULATE), B("📜 Пресеты", callback_data=CBT_TEMPLATE_PRESETS))
    kb.row(B("📊 Статистика", callback_data=CBT_MARGIN), B("🩺 Health", callback_data=CBT_HEALTH))
    kb.row(B("⚙️ RBX настройки", callback_data=CBT_SETTINGS), B("📘 Помощь", callback_data=CBT_HELP))
    return kb


def _mode_text() -> str:
    s = _load_settings()
    rbx_on = bool(s.get("rbxcrate_enabled", False))
    gp_on = bool(s.get("gamepass_enabled", True))
    mode = "💎 RBXCrate" if rbx_on else "🎮 GamePass аккаунты" if gp_on else "⏸ пауза"
    return (
        "<b>🎛 Режим работы</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Сейчас работает: <b>{mode}</b>\n\n"
        "Выбери, кто будет обрабатывать новые заказы:\n"
        "🎮 GamePass аккаунты — бот просит GamePass и выкупает его через Roblox cookie.\n"
        "💎 RBXCrate — бот создаёт заказ через RBXCrate API.\n"
        "⏸ Пауза — новые заказы не выдаются автоматически."
    )


def _mode_kb() -> K:
    s = _load_settings()
    kb = K(row_width=1)
    kb.add(B(f"{'✅' if s.get('gamepass_enabled', True) else '⬜'} GamePass аккаунты", callback_data=CBT_MODE_GAMEPASS))
    kb.add(B(f"{'✅' if s.get('rbxcrate_enabled', False) else '⬜'} RBXCrate", callback_data=CBT_MODE_RBXCRATE))
    kb.add(B("⏸ Пауза авто-выдачи", callback_data=CBT_MODE_PAUSE))
    kb.add(B(f"🧪 Тест RBXCrate: {'ON' if s.get('test_mode', True) else 'OFF'}", callback_data=CBT_MODE_TEST))
    kb.add(B("⬅️ Назад", callback_data=CBT_HOME))
    return kb


def _gamepass_text() -> str:
    enabled = bool(_load_settings().get("gamepass_enabled", True))
    try:
        gp = _load_embedded_gamepass()
        s = gp._load_settings()
        accounts = len(s.get("accounts", []))
        mappings = len(s.get("lot_mappings", []))
        auto_refund = bool(s.get("auto_refund", True))
        timeout_days = round(int(s.get("wait_timeout_sec", 86400) or 86400) / 86400, 1)
        attempts = int(s.get("max_attempts", 5) or 5)
    except Exception:
        accounts = mappings = attempts = 0
        auto_refund = False
        timeout_days = 0
    return (
        "<b>🎮 GamePass аккаунты</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Статус движка: {'🟢 включен' if enabled else '🔴 выключен'}\n"
        f"Аккаунтов Roblox: <code>{accounts}</code>\n"
        f"Привязок лотов: <code>{mappings}</code>\n"
        f"Авто-возврат: {'🟢 вкл' if auto_refund else '🔴 выкл'}\n"
        f"Таймаут GamePass: <code>{timeout_days}</code> дн\n"
        f"Макс. попыток цены: <code>{attempts}</code>\n\n"
        "Здесь старый GamePass-выкуп, но меню собрано в один понятный раздел."
    )


def _gamepass_kb() -> K:
    kb = K(row_width=2)
    kb.row(B("👤 Аккаунты", callback_data="autorobux:accounts"), B("🎯 Лоты", callback_data=GP_CBT_MAPPINGS))
    kb.row(B("📜 Шаблоны", callback_data="autorobux:msgs"), B("📊 Статистика", callback_data="autorobux:stats"))
    kb.row(B("🧪 Проверить описание", callback_data=GP_CBT_TEST_DESC), B("🧮 Калькулятор", callback_data=CBT_CALC))
    kb.row(B("🧪 Симуляция", callback_data=CBT_SIMULATE), B("💰 Балансы", callback_data="autorobux:balances"))
    kb.row(B("⚙️ Доп. настройки", callback_data="autorobux:advanced"))
    kb.row(B("⬅️ Назад", callback_data=CBT_HOME))
    return kb


def _settings_text() -> str:
    s = _load_settings()
    api = s.get("api") or {}
    return (
        "<b>⚙️ Настройки autorobuxdrake</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"API key: <code>{_mask_secret(api.get('token'))}</code>\n"
        f"Endpoint: <code>{_html_escape(api.get('base_url'))}</code>\n"
        f"Лотов: <code>{len(s.get('lot_mappings', []))}</code>\n"
        f"Polling: {'🟢 ON' if s.get('polling_enabled', True) else '🔴 OFF'}\n"
        f"Интервал polling: <code>{int(s.get('polling_interval_sec', 60))}</code> сек\n"
        f"Авто-ключи описания: {'🟢 ON' if s.get('auto_base_description_enabled', False) else '🔴 OFF'}\n"
        f"Auto-cancel Error: <code>{int(s.get('auto_cancel_error_after_min', 0))}</code> мин\n"
        f"OUT_OF_STOCK: <code>{_html_escape(s.get('out_of_stock_policy', 'wait'))}</code>"
    )


def _settings_kb() -> K:
    kb = K(row_width=2)
    kb.row(B("🔑 RBXcrate API key", callback_data=f"{CBT_API_EDIT}:token"))
    kb.row(B("🌐 RBXcrate endpoint", callback_data=f"{CBT_API_EDIT}:base_url"))
    kb.row(B("🎯 ID лотов", callback_data=CBT_LOTS), B("🔎 Ключи описания", callback_data=CBT_LOTS))
    kb.row(B("🔄 Polling ON/OFF", callback_data=CBT_POLLING))
    kb.row(B("⏱️ Интервал polling", callback_data=CBT_POLLING_INTERVAL))
    kb.row(B("🛑 Auto-cancel Error", callback_data=CBT_AUTOCANCEL))
    kb.row(B("📦 OUT_OF_STOCK policy", callback_data=CBT_OOS_POLICY))
    kb.row(B("🔌 Автоуправление лотами", callback_data=CBT_MANAGE_LOTS))
    kb.row(B("📏 Min/Max Robux", callback_data=CBT_LIMITS), B("🌐 RU/EN", callback_data=CBT_LANG))
    kb.row(B("🧪 Тестовый режим ON/OFF", callback_data=CBT_TEST_MODE))
    kb.row(B("🔎 Авто-ключи описания ON/OFF", callback_data=CBT_AUTO_BASE))
    kb.row(B("⬅️ Назад", callback_data=CBT_RBXCRATE_SECTION))
    return kb


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
        try:
            gp = _load_embedded_gamepass()
            gs = gp._load_settings()
            if not gs.get("operator_chat_id"):
                gs["operator_chat_id"] = chat_id
                gp._save_settings(gs)
        except Exception:
            logger.debug("%s persist embedded GamePass operator failed", LOGGER_PREFIX, exc_info=True)

    def _clear_rbxcrate_waiting() -> None:
        with _waiting_lock:
            _waiting.clear()

    def _clear_gamepass_waiting() -> None:
        try:
            gp = _load_embedded_gamepass()
            lock = getattr(gp, "_waiting_lock", None)
            waiting = getattr(gp, "_waiting", None)
            if waiting is None:
                return
            if lock:
                with lock:
                    waiting.clear()
            else:
                waiting.clear()
        except Exception:
            logger.debug("%s clear embedded GamePass waiting failed", LOGGER_PREFIX, exc_info=True)

    _gp_last_test_desc: dict[int, str] = {}

    def _edit_or_send(call, text: str, kb) -> None:
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.id, parse_mode="HTML", reply_markup=kb)
        except Exception:
            bot.send_message(call.message.chat.id, text, parse_mode="HTML", reply_markup=kb)

    def home_cb(call) -> None:
        _persist_op(call.message.chat.id)
        _edit_or_send(call, _main_text(), _main_kb())
        _answer(call)

    def mode_screen_cb(call) -> None:
        _persist_op(call.message.chat.id)
        _edit_or_send(call, _mode_text(), _mode_kb())
        _answer(call)

    def mode_rbxcrate_cb(call) -> None:
        _persist_op(call.message.chat.id)
        s = _load_settings()
        new_value = not bool(s.get("rbxcrate_enabled", False))
        s["rbxcrate_enabled"] = new_value
        if new_value:
            s["gamepass_enabled"] = False
            _clear_gamepass_waiting()
        else:
            _clear_rbxcrate_waiting()
        _save_settings(s)
        _edit_or_send(call, _mode_text(), _mode_kb())
        _answer(call, "RBXCrate " + ("включён" if new_value else "выключен"))

    def mode_gamepass_cb(call) -> None:
        _persist_op(call.message.chat.id)
        s = _load_settings()
        new_value = not bool(s.get("gamepass_enabled", True))
        s["gamepass_enabled"] = new_value
        if new_value:
            s["rbxcrate_enabled"] = False
            _clear_rbxcrate_waiting()
        else:
            _clear_gamepass_waiting()
        _save_settings(s)
        _edit_or_send(call, _mode_text(), _mode_kb())
        _answer(call, "GamePass " + ("включён" if new_value else "выключен"))

    def mode_pause_cb(call) -> None:
        _persist_op(call.message.chat.id)
        s = _load_settings()
        s["rbxcrate_enabled"] = False
        s["gamepass_enabled"] = False
        _save_settings(s)
        _clear_rbxcrate_waiting()
        _clear_gamepass_waiting()
        _edit_or_send(call, _mode_text(), _mode_kb())
        _answer(call, "Авто-выдача на паузе")

    def rbxcrate_section_cb(call) -> None:
        _persist_op(call.message.chat.id)
        _edit_or_send(call, _home_text(), _home_kb())
        _answer(call)

    def gamepass_section_cb(call) -> None:
        _persist_op(call.message.chat.id)
        try:
            _load_embedded_gamepass()
            _edit_or_send(call, _gamepass_text(), _gamepass_kb())
        except Exception as exc:
            text = (
                "<b>🎮 GamePass</b>\n"
                "Не смог открыть встроенное GamePass-меню.\n"
                f"Ошибка: <code>{_html_escape(exc)}</code>"
            )
            kb = K().add(B("⬅️ К разделам", callback_data=CBT_HOME))
            _edit_or_send(call, text, kb)
        _answer(call)

    def _gp_module():
        return _load_embedded_gamepass()

    def _gp_load_settings() -> dict:
        return _gp_module()._load_settings()

    def _gp_save_settings(settings: dict) -> None:
        _gp_module()._save_settings(settings)

    def _gp_find_mapping(settings: dict, mapping_id: str) -> dict | None:
        for mapping in settings.get("lot_mappings", []):
            if str(mapping.get("id")) == str(mapping_id):
                return mapping
        return None

    def _gp_mapping_label(mapping: dict) -> str:
        match = mapping.get("lot_match") or "—"
        lot_id = mapping.get("lot_id")
        suffix = f" / ID:{lot_id}" if lot_id else ""
        return f"'{match}'{suffix}"

    def _gp_extract_order_units(desc: str) -> int:
        normalized = _normalize_lot_text(desc)
        patterns = (
            r"(\d{1,6})\s*(?:ед\.?\s*)?(?:робукс(?:ов|ы|а)?|robux|rbx)\b",
            r"(?:робукс(?:ов|ы|а)?|robux|rbx)\D{0,12}(\d{1,6})",
        )
        for pattern in patterns:
            m = re.search(pattern, normalized, re.I)
            if m:
                return int(m.group(1))
        return 1

    def _gp_suggest_lot_key(desc: str) -> str:
        parts = [p.strip() for p in (desc or "").split(",") if p.strip()]
        if len(parts) >= 2:
            return ", ".join(parts[:-1])
        return (desc or "Game Pass").strip()[:120]

    def _gp_check_description_result(desc: str) -> tuple[str, K]:
        gp = _gp_module()
        s = gp._load_settings()
        units = _gp_extract_order_units(desc)
        mapping = gp._match_mapping(s, None, desc)
        kb = K(row_width=1)
        if mapping:
            expected_price = int(gp._mapping_expected_price(mapping, units))
            template = s.get("messages", {}).get("ask_gamepass", "").format(expected_price=expected_price)
            text = (
                "<b>🧪 Проверка описания GamePass</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"✅ Найдена привязка: <code>{_html_escape(_gp_mapping_label(mapping))}</code>\n"
                f"Количество из описания: <code>{units}</code>\n"
                f"Цена GamePass с комиссией Roblox: <code>{expected_price}</code> Robux\n\n"
                "<b>Покупателю уйдёт:</b>\n"
                f"<code>{_html_escape(template)}</code>"
            )
        else:
            suggested = _gp_suggest_lot_key(desc)
            text = (
                "<b>🧪 Проверка описания GamePass</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "❌ Привязка не найдена.\n"
                f"Количество из описания: <code>{units}</code>\n"
                f"Предлагаемый ключ: <code>{_html_escape(suggested)}</code>\n\n"
                "Можно сразу создать привязку по этому описанию. Формула будет <code>1×кол-во + 0</code>, чтобы заказ на 170 Robux дал GamePass price 243."
            )
            kb.add(B("🎯 Создать привязку из описания", callback_data=GP_CBT_QUICK_ADD))
        kb.add(B("🎮 К лотам GamePass", callback_data=GP_CBT_MAPPINGS))
        kb.add(B("⬅️ Главное меню", callback_data=CBT_HOME))
        return text, kb

    def gp_mappings_cb(call) -> None:
        s = _gp_load_settings()
        mappings = s.get("lot_mappings", [])
        lines = [
            "<b>🎮 GamePass: лоты ↔ Robux</b>",
            "",
            "Шаг 1 при добавлении — это <b>название лота или кусок названия</b>.",
            "ID лота вида <code>4355531-141-99-2356-0</code> вводи на шаге 4.",
            "",
        ]
        kb = K(row_width=1)
        for mapping in mappings:
            lines.append(
                f"• <code>{_html_escape(_gp_mapping_label(mapping))}</code> → "
                f"<code>{int(mapping.get('robux_per_unit', 0) or 0)}×кол-во + "
                f"{int(mapping.get('extra_robux', 0) or 0)}</code> Robux"
            )
            kb.add(B(f"⚙️ Изменить {_gp_mapping_label(mapping)}", callback_data=f"{GP_CBT_MAPPING_VIEW}:{mapping.get('id')}"))
        if not mappings:
            lines.append("(пусто)")
        kb.add(B("➕ Добавить привязку", callback_data=GP_CBT_MAPPING_ADD))
        kb.add(B("🧪 Проверить описание заказа", callback_data=GP_CBT_TEST_DESC))
        kb.add(B("◀️ Назад", callback_data=CBT_GAMEPASS_SECTION))
        _edit_or_send(call, "\n".join(lines), kb)
        _answer(call)

    def gp_mapping_view_cb(call) -> None:
        mapping_id = (call.data or "").split(":")[-1]
        s = _gp_load_settings()
        mapping = _gp_find_mapping(s, mapping_id)
        if not mapping:
            _answer(call, "не найдено")
            return gp_mappings_cb(call)
        text = (
            f"<b>⚙️ Привязка GamePass</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Название/ключ: <code>{_html_escape(mapping.get('lot_match') or '—')}</code>\n"
            f"FunPay lot ID: <code>{_html_escape(mapping.get('lot_id') or '—')}</code>\n"
            f"Robux за 1 ед.: <code>{int(mapping.get('robux_per_unit', 0) or 0)}</code>\n"
            f"Extra Robux: <code>{int(mapping.get('extra_robux', 0) or 0)}</code>"
        )
        kb = K(row_width=2)
        kb.row(B("✏️ Название", callback_data=f"{GP_CBT_MAPPING_EDIT}:{mapping_id}:lot_match"),
               B("🔢 Lot ID", callback_data=f"{GP_CBT_MAPPING_EDIT}:{mapping_id}:lot_id"))
        kb.row(B("💎 Robux/ед.", callback_data=f"{GP_CBT_MAPPING_EDIT}:{mapping_id}:robux_per_unit"),
               B("➕ Extra", callback_data=f"{GP_CBT_MAPPING_EDIT}:{mapping_id}:extra_robux"))
        kb.row(B("🗑 Удалить", callback_data=f"{GP_CBT_MAPPING_DEL}:{mapping_id}"))
        kb.row(B("◀️ Назад", callback_data=GP_CBT_MAPPINGS))
        _edit_or_send(call, text, kb)
        _answer(call)

    def gp_mapping_edit_cb(call) -> None:
        parts = (call.data or "").split(":")
        if len(parts) < 4:
            return _answer(call, "ошибка")
        mapping_id, field = parts[-2], parts[-1]
        prompts = {
            "lot_match": "Введите новое название лота или уникальную подстроку названия:",
            "lot_id": "Введите новый ID лота FunPay. Чтобы убрать ID, отправьте «-»: ",
            "robux_per_unit": "Введите новое количество Robux за 1 единицу заказа:",
            "extra_robux": "Введите новую фиксированную добавку Robux, можно 0:",
        }
        if field not in prompts:
            return _answer(call, "неизвестное поле")
        msg = bot.send_message(call.message.chat.id, prompts[field])
        _answer(call)

        def handle(m) -> None:
            value = (m.text or "").strip()
            s = _gp_load_settings()
            mapping = _gp_find_mapping(s, mapping_id)
            if not mapping:
                return bot.reply_to(m, "❌ Привязка не найдена.")
            try:
                if field == "lot_match":
                    if not value:
                        raise ValueError("пустое название")
                    mapping[field] = value
                elif field == "lot_id":
                    mapping[field] = None if value in ("", "-", "—", "нет") else value
                else:
                    number = int(value)
                    if number < 0:
                        raise ValueError("число меньше 0")
                    mapping[field] = number
            except Exception as exc:
                return bot.reply_to(m, f"❌ Не сохранил: {exc}. Прежнее значение оставлено.")
            _gp_save_settings(s)
            bot.reply_to(m, f"✅ Обновлено: <code>{_html_escape(_gp_mapping_label(mapping))}</code>", parse_mode="HTML")

        bot.register_next_step_handler(msg, handle)

    def gp_mapping_add_cb(call) -> None:
        msg = bot.send_message(
            call.message.chat.id,
            "➕ Шаг 1/4: Введите <b>название лота или уникальную подстроку названия</b>.\n"
            "Не ID. Например: <code>Roblox, Робуксы, Game Pass</code>",
            parse_mode="HTML",
        )
        _answer(call)

        def step_match(m):
            match = (m.text or "").strip()
            if not match:
                return bot.reply_to(m, "❌ Пусто. Отменено.")
            msg2 = bot.reply_to(m, "💎 Шаг 2/4: Введите количество Robux за 1 единицу заказа (число):")

            def step_per_unit(m2):
                try:
                    per_unit = int((m2.text or "").strip())
                    if per_unit < 0:
                        raise ValueError
                except Exception:
                    return bot.reply_to(m2, "❌ Не число. Отменено.")
                msg3 = bot.reply_to(m2, "➕ Шаг 3/4: Введите фиксированную добавку Robux (extra, можно 0):")

                def step_extra(m3):
                    try:
                        extra = int((m3.text or "").strip())
                        if extra < 0:
                            raise ValueError
                    except Exception:
                        return bot.reply_to(m3, "❌ Не число. Отменено.")
                    msg4 = bot.reply_to(
                        m3,
                        "🔢 Шаг 4/4: Введите ID лота FunPay, если знаете. Можно с дефисами. Если нет — отправьте «-»:"
                    )

                    def step_lot_id(m4):
                        lot_id_raw = (m4.text or "").strip()
                        lot_id = None if lot_id_raw in ("", "-", "—", "нет") else lot_id_raw
                        s = _gp_load_settings()
                        s.setdefault("lot_mappings", []).append({
                            "id": _gp_module()._new_id("m", s.get("lot_mappings", [])),
                            "lot_match": match,
                            "lot_id": lot_id,
                            "robux_per_unit": per_unit,
                            "extra_robux": extra,
                        })
                        _gp_save_settings(s)
                        suffix = f" / ID:{lot_id}" if lot_id else ""
                        bot.reply_to(m4, f"✅ Привязка добавлена: '{match}'{suffix} → {per_unit}×кол-во + {extra} Robux")

                    bot.register_next_step_handler(msg4, step_lot_id)

                bot.register_next_step_handler(msg3, step_extra)

            bot.register_next_step_handler(msg2, step_per_unit)

        bot.register_next_step_handler(msg, step_match)

    def gp_mapping_del_cb(call) -> None:
        mapping_id = (call.data or "").split(":")[-1]
        s = _gp_load_settings()
        s["lot_mappings"] = [m for m in s.get("lot_mappings", []) if str(m.get("id")) != str(mapping_id)]
        _gp_save_settings(s)
        _answer(call, "🗑 Удалено")
        gp_mappings_cb(call)

    def gp_test_desc_cb(call) -> None:
        _persist_op(call.message.chat.id)
        msg = bot.send_message(
            call.message.chat.id,
            "🧪 Пришли описание заказа FunPay.\n\n"
            "Пример:\n"
            "Game Pass (5 дней), 170 ед. робуксов, username",
        )
        _answer(call)

        def handle(m):
            desc = (m.text or "").strip()
            if not desc:
                return bot.reply_to(m, "❌ Пусто. Отменено.")
            _gp_last_test_desc[m.chat.id] = desc
            text, kb = _gp_check_description_result(desc)
            bot.reply_to(m, text, parse_mode="HTML", reply_markup=kb)
        bot.register_next_step_handler(msg, handle)

    def gp_quick_add_cb(call) -> None:
        desc = _gp_last_test_desc.get(call.message.chat.id, "")
        if not desc:
            _answer(call, "сначала пришлите описание")
            return gp_test_desc_cb(call)
        suggested = _gp_suggest_lot_key(desc)
        units = _gp_extract_order_units(desc)
        msg = bot.send_message(
            call.message.chat.id,
            "🎯 Создание привязки из описания\n\n"
            f"Предлагаемый ключ: {suggested}\n"
            f"Пример количества: {units}\n\n"
            "Пришлите ключ/подстроку для лота или отправьте + чтобы принять предложенный.",
        )
        _answer(call)

        def step_key(m):
            key = (m.text or "").strip()
            if key == "+":
                key = suggested
            if not key:
                return bot.reply_to(m, "❌ Пусто. Отменено.")
            msg2 = bot.reply_to(
                m,
                "Robux за 1 единицу заказа.\n"
                "Обычно для FunPay-лотов с количеством Robux ставь 1. Для фиксированного лота можно поставить нужное число.",
            )

            def step_per(m2):
                try:
                    per = int((m2.text or "").strip())
                    if per < 0:
                        raise ValueError
                except Exception:
                    return bot.reply_to(m2, "❌ Нужно число. Отменено.")
                msg3 = bot.reply_to(m2, "Extra Robux, если нужна фиксированная добавка. Обычно 0:")

                def step_extra(m3):
                    try:
                        extra = int((m3.text or "").strip())
                        if extra < 0:
                            raise ValueError
                    except Exception:
                        return bot.reply_to(m3, "❌ Нужно число. Отменено.")
                    s = _gp_load_settings()
                    mapping = {
                        "id": _gp_module()._new_id("m", s.get("lot_mappings", [])),
                        "lot_match": key,
                        "lot_id": None,
                        "robux_per_unit": per,
                        "extra_robux": extra,
                    }
                    s.setdefault("lot_mappings", []).append(mapping)
                    _gp_save_settings(s)
                    expected = int(_gp_module()._mapping_expected_price(mapping, units))
                    bot.reply_to(
                        m3,
                        f"✅ Привязка создана: {key} → {per}×кол-во + {extra}.\n"
                        f"Проверка на {units} Robux: GamePass price {expected} Robux.",
                    )
                bot.register_next_step_handler(msg3, step_extra)
            bot.register_next_step_handler(msg2, step_per)
        bot.register_next_step_handler(msg, step_key)

    def calc_cb(call) -> None:
        msg = bot.send_message(call.message.chat.id, "🧮 Введите net Robux, которые должен получить покупатель. Пример: 170")
        _answer(call)

        def handle(m):
            raw = (m.text or "").strip()
            if not raw.isdigit():
                return bot.reply_to(m, "❌ Нужно число Robux.")
            net = int(raw)
            price = _gamepass_price_for_net_robux(net)
            fee = price - net
            bot.reply_to(
                m,
                f"🧮 Калькулятор Robux\n\n"
                f"К выдаче: {net} Robux\n"
                f"Цена GamePass: {price} Robux\n"
                f"Комиссия Roblox 30%: {fee} Robux\n\n"
                f"Шаблон: Создайте GamePass на ровно {price} Robux и пришлите ссылку/ID.",
            )
        bot.register_next_step_handler(msg, handle)

    def simulate_cb(call) -> None:
        msg = bot.send_message(
            call.message.chat.id,
            "🧪 Симуляция заказа\n\nПришли описание заказа FunPay, например:\nGame Pass (5 дней), 170 ед. робуксов, username",
        )
        _answer(call)

        def handle(m):
            desc = (m.text or "").strip()
            if not desc:
                return bot.reply_to(m, "❌ Пусто. Отменено.")
            _gp_last_test_desc[m.chat.id] = desc
            text, kb = _gp_check_description_result(desc)
            bot.reply_to(m, "<b>🧪 Симуляция</b>\n\n" + text, parse_mode="HTML", reply_markup=kb)
        bot.register_next_step_handler(msg, handle)

    def problems_cb(call) -> None:
        items = list(reversed(_load_problems()[-20:]))
        lines = ["<b>🧯 Проблемы AutoRobux</b>", ""]
        kb = K(row_width=1)
        if not items:
            lines.append("Пока пусто.")
        for item in items:
            pid = item.get("id")
            kind = item.get("kind") or "problem"
            oid = item.get("order_id") or "—"
            desc = str(item.get("lot_desc") or "")[:90]
            lines.append(f"• <code>{_html_escape(kind)}</code> #{_html_escape(oid)} — {_html_escape(desc)}")
            if item.get("lot_desc"):
                kb.add(B(f"🎯 Создать привязку из #{oid}", callback_data=f"{CBT_PROBLEM_ACT}:map:{pid}"))
        kb.add(B("🧪 Симуляция", callback_data=CBT_SIMULATE))
        kb.add(B("⬅️ Назад", callback_data=CBT_HOME))
        _edit_or_send(call, "\n".join(lines), kb)
        _answer(call)

    def problem_action_cb(call) -> None:
        tail = (call.data or "")[len(CBT_PROBLEM_ACT) + 1:]
        action, pid = tail.split(":", 1)
        item = next((p for p in _load_problems() if str(p.get("id")) == str(pid)), None)
        if not item:
            _answer(call, "не найдено")
            return problems_cb(call)
        if action == "map":
            _gp_last_test_desc[call.message.chat.id] = str(item.get("lot_desc") or "")
            return gp_quick_add_cb(call)

    def template_presets_cb(call) -> None:
        text = (
            "<b>📜 Пресеты шаблонов</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Выбери, какой стандартный шаблон поставить."
        )
        kb = K(row_width=1)
        kb.add(B("🎮 GamePass короткий", callback_data=f"{CBT_TEMPLATE_PRESET_APPLY}:gp_short"))
        kb.add(B("🎮 GamePass подробный", callback_data=f"{CBT_TEMPLATE_PRESET_APPLY}:gp_full"))
        kb.add(B("💎 RBXCrate подробный RU/EN", callback_data=f"{CBT_TEMPLATE_PRESET_APPLY}:rbx_full"))
        kb.add(B("⬅️ Назад", callback_data=CBT_HOME))
        _edit_or_send(call, text, kb)
        _answer(call)

    def template_preset_apply_cb(call) -> None:
        preset = (call.data or "").split(":")[-1]
        if preset in {"gp_short", "gp_full"}:
            gp = _gp_module()
            s = gp._load_settings()
            if preset == "gp_short":
                s.setdefault("messages", {})["ask_gamepass"] = "💎 Спасибо за оплату! Создайте GamePass на ровно {expected_price} Robux и пришлите ссылку/ID. Если продажа недоступна — заполните Experience Rating / Questionnaire в настройках плейса."
            else:
                s.setdefault("messages", {})["ask_gamepass"] = gp.DEFAULT_MESSAGES["ask_gamepass"]
            gp._save_settings(s)
            _answer(call, "GamePass шаблон обновлён")
            return gamepass_section_cb(call)
        if preset == "rbx_full":
            s = _load_settings()
            s["messages"] = dict(DEFAULT_MESSAGES)
            s["messages_en"] = dict(DEFAULT_MESSAGES_EN)
            _save_settings(s)
            _answer(call, "RBXCrate шаблоны обновлены")
            return home_cb(call)
        _answer(call, "unknown")

    def settings_cb(call) -> None:
        _persist_op(call.message.chat.id)
        _edit_or_send(call, _settings_text(), _settings_kb())
        _answer(call)

    def config_cb(call) -> None:
        s = _load_settings()
        safe = json.loads(json.dumps(s))
        if safe.get("api", {}).get("token"):
            safe["api"]["token"] = _mask_secret(safe["api"]["token"])
        text = "<b>📄 Конфиг</b>\n<code>" + _html_escape(json.dumps(safe, ensure_ascii=False, indent=2)[:3500]) + "</code>"
        kb = K(row_width=1)
        kb.add(B("📤 Export config", callback_data=CBT_CONFIG_EXPORT))
        kb.add(B("📥 Import config", callback_data=CBT_CONFIG_IMPORT))
        kb.add(B("⬅️ Назад", callback_data=CBT_RBXCRATE_SECTION))
        _edit_or_send(call, text, kb)
        _answer(call)

    def config_export_cb(call) -> None:
        safe = json.loads(json.dumps(_load_settings()))
        if safe.get("api", {}).get("token"):
            safe["api"]["token"] = ""
        bio = io.BytesIO(json.dumps(safe, ensure_ascii=False, indent=2).encode("utf-8"))
        bio.name = "autorobux_rbxcrate_config.json"
        bot.send_document(call.message.chat.id, bio)
        _answer(call)

    def config_import_cb(call) -> None:
        msg = bot.send_message(call.message.chat.id, "Пришлите JSON-конфиг текстом. API key можно оставить пустым.")
        _answer(call)

        def handle(m):
            try:
                data = json.loads(m.text or "")
                if not isinstance(data, dict):
                    raise ValueError("root must be object")
            except Exception as exc:
                return bot.reply_to(m, f"❌ JSON ошибка: {exc}")
            cur = _load_settings()
            if not data.get("api", {}).get("token") and cur.get("api", {}).get("token"):
                data.setdefault("api", {})["token"] = cur["api"]["token"]
            _save_settings(data)
            bot.reply_to(m, "✅ Конфиг импортирован")
        bot.register_next_step_handler(msg, handle)

    def health_cb(call) -> None:
        s = _load_settings()
        api = s.get("api") or {}
        checks = [
            ("API key", bool(api.get("token"))),
            ("Лоты", bool(s.get("lot_mappings"))),
            ("Test mode", bool(s.get("test_mode", True))),
            ("Polling", bool(s.get("polling_enabled", True))),
            ("Blacklist", bool(s.get("blacklist_enabled", True))),
        ]
        lines = ["<b>🩺 Health-check</b>", ""]
        for label, ok in checks:
            lines.append(f"{'✅' if ok else '❌'} {label}")
        try:
            gp_s = _gp_load_settings()
            gp_msg = str(gp_s.get("messages", {}).get("ask_gamepass", ""))
            lines.append(f"{'✅' if gp_s.get('accounts') else '❌'} GamePass аккаунты")
            lines.append(f"{'✅' if gp_s.get('lot_mappings') else '❌'} GamePass привязки")
            lines.append(f"{'✅' if 'Experience Rating' in gp_msg else '⚠️'} GamePass шаблон: Experience Rating")
        except Exception:
            lines.append("⚠️ GamePass health недоступен")
        rbx_msg = str(s.get("messages", {}).get("ask_username", ""))
        lines.append(f"{'✅' if 'Experience Rating' in rbx_msg else '⚠️'} RBXCrate шаблон: Experience Rating")
        problems_count = len(_load_problems())
        lines.append(f"{'✅' if problems_count == 0 else '⚠️'} Проблемы: <code>{problems_count}</code>")
        bal = _rbxcrate_get_configured_path(s, "balance_path") if api.get("token") and not s.get("test_mode", True) else {"ok": False, "reason": "skip in test/no key"}
        stock = _rbxcrate_get_configured_path(s, "stock_path") if api.get("token") and not s.get("test_mode", True) else {"ok": False, "reason": "skip in test/no key"}
        lines.append(f"{'✅' if bal.get('ok') else '⚪'} Balance API: {_html_escape(bal.get('reason') or 'ok')}")
        lines.append(f"{'✅' if stock.get('ok') else '⚪'} Stock API: {_html_escape(stock.get('reason') or 'ok')}")
        kb = K(row_width=1)
        kb.add(B("🧯 Проблемы", callback_data=CBT_PROBLEMS))
        kb.add(B("⬅️ Назад", callback_data=CBT_HOME))
        _edit_or_send(call, "\n".join(lines), kb)
        _answer(call)

    def toggle_cb(call) -> None:
        s = _load_settings()
        s["enabled"] = not bool(s.get("enabled", True))
        _save_settings(s)
        home_cb(call)

    def test_mode_cb(call) -> None:
        s = _load_settings()
        s["test_mode"] = not bool(s.get("test_mode", True))
        _save_settings(s)
        settings_cb(call)

    def mode_test_cb(call) -> None:
        s = _load_settings()
        s["test_mode"] = not bool(s.get("test_mode", True))
        _save_settings(s)
        _edit_or_send(call, _mode_text(), _mode_kb())
        _answer(call, "Тест RBXCrate " + ("включён" if s["test_mode"] else "выключен"))

    def auto_base_cb(call) -> None:
        s = _load_settings()
        s["auto_base_description_enabled"] = not bool(s.get("auto_base_description_enabled", False))
        _save_settings(s)
        _answer(call, "авто-ключи " + ("вкл" if s["auto_base_description_enabled"] else "выкл"))
        settings_cb(call)

    def refund_cb(call) -> None:
        s = _load_settings()
        s["auto_refund_on_api_error"] = not bool(s.get("auto_refund_on_api_error"))
        _save_settings(s)
        home_cb(call)

    def manage_lots_cb(call) -> None:
        s = _load_settings()
        s["auto_manage_funpay_lots"] = not bool(s.get("auto_manage_funpay_lots"))
        _save_settings(s)
        settings_cb(call)

    def lang_cb(call) -> None:
        s = _load_settings()
        s["default_language"] = "en" if s.get("default_language", "ru") == "ru" else "ru"
        _save_settings(s)
        settings_cb(call)

    def limits_cb(call) -> None:
        msg = bot.send_message(call.message.chat.id, "Введите min и max Robux через пробел. Пример: 10 100000")
        _answer(call)

        def handle(m):
            parts = (m.text or "").split()
            if len(parts) != 2 or not all(p.isdigit() for p in parts):
                return bot.reply_to(m, "❌ Формат: min max")
            mn, mx = int(parts[0]), int(parts[1])
            if mn < 0 or mx < mn:
                return bot.reply_to(m, "❌ Некорректные лимиты")
            s = _load_settings()
            s["min_robux"] = mn
            s["max_robux"] = mx
            _save_settings(s)
            bot.reply_to(m, f"✅ Лимиты сохранены: {mn}-{mx}")
        bot.register_next_step_handler(msg, handle)

    def polling_cb(call) -> None:
        s = _load_settings()
        s["polling_enabled"] = not bool(s.get("polling_enabled", True))
        _save_settings(s)
        settings_cb(call)

    def polling_interval_cb(call) -> None:
        msg = bot.send_message(call.message.chat.id, "Введите интервал polling в секундах (10-3600):")
        _answer(call)

        def handle(m):
            try:
                value = int((m.text or "").strip())
                if not 10 <= value <= 3600:
                    raise ValueError
            except Exception:
                return bot.reply_to(m, "❌ Нужно число от 10 до 3600.")
            s = _load_settings()
            s["polling_interval_sec"] = value
            _save_settings(s)
            bot.reply_to(m, f"✅ Интервал polling сохранён: {value} сек")
        bot.register_next_step_handler(msg, handle)

    def autocancel_cb(call) -> None:
        msg = bot.send_message(call.message.chat.id, "Через сколько минут Error автоматически отменять в RBXCrate? 0 = выключить:")
        _answer(call)

        def handle(m):
            try:
                value = int((m.text or "").strip())
                if not 0 <= value <= 10080:
                    raise ValueError
            except Exception:
                return bot.reply_to(m, "❌ Нужно число от 0 до 10080.")
            s = _load_settings()
            s["auto_cancel_error_after_min"] = value
            _save_settings(s)
            bot.reply_to(m, f"✅ Auto-cancel Error: {value} мин")
        bot.register_next_step_handler(msg, handle)

    def oos_policy_cb(call) -> None:
        msg = bot.send_message(call.message.chat.id, "Политика OUT_OF_STOCK: wait / refund / ask_operator")
        _answer(call)

        def handle(m):
            value = (m.text or "").strip().lower()
            if value not in {"wait", "refund", "ask_operator"}:
                return bot.reply_to(m, "❌ Введите wait, refund или ask_operator.")
            s = _load_settings()
            s["out_of_stock_policy"] = value
            _save_settings(s)
            bot.reply_to(m, f"✅ OUT_OF_STOCK policy: {value}")
        bot.register_next_step_handler(msg, handle)

    def api_cb(call) -> None:
        s = _load_settings()
        api = s.get("api") or {}
        lines = ["<b>🔌 API RBXCrate</b>", ""]
        kb = K(row_width=1)
        for key, label in API_FIELDS.items():
            val = _mask_secret(api.get(key)) if key == "token" else _html_escape(str(api.get(key, ""))[:350])
            lines.append(f"• {label}: <code>{val or '—'}</code>")
            kb.add(B(f"✏️ {label}", callback_data=f"{CBT_API_EDIT}:{key}"))
        kb.add(B("◀️ Назад", callback_data=CBT_RBXCRATE_SECTION))
        _edit_or_send(call, "\n".join(lines), kb)
        _answer(call)

    def api_edit_cb(call) -> None:
        key = call.data.split(":", 2)[-1]
        if key not in API_FIELDS:
            return _answer(call, "unknown")
        msg = bot.send_message(call.message.chat.id, f"Введите новое значение для {API_FIELDS[key]}:")
        _answer(call)

        def handle(m):
            s = _load_settings()
            value = (m.text or "").strip()
            if key in {"is_pre_order", "check_ownership"}:
                s.setdefault("api", {})[key] = value.lower() in {"1", "true", "yes", "y", "да", "д", "вкл", "on"}
            else:
                s.setdefault("api", {})[key] = value
            _save_settings(s)
            if key == "token":
                check = _rbxcrate_get_configured_path(s, "balance_path")
                if check.get("ok"):
                    bot.reply_to(m, "✅ API key сохранён и проверен: баланс доступен.")
                else:
                    bot.reply_to(m, f"⚠️ API key сохранён, но проверка баланса не прошла: {check.get('reason')}")
            else:
                bot.reply_to(m, "✅ Сохранено")
        bot.register_next_step_handler(msg, handle)

    def balance_cb(call) -> None:
        _answer(call, "⏳")
        res = _rbxcrate_get_configured_path(_load_settings(), "balance_path")
        text = "<b>💰 Баланс</b>\n" if res.get("ok") else f"<b>💰 Баланс</b>\n❌ {_html_escape(res.get('reason'))}\n"
        text += f"<code>{_html_escape(json.dumps(res.get('data'), ensure_ascii=False)[:1500])}</code>"
        kb = K().add(B("⬅️ Назад", callback_data=CBT_RBXCRATE_SECTION))
        _edit_or_send(call, text, kb)

    def stock_cb(call) -> None:
        _answer(call, "⏳")
        res = _rbxcrate_get_configured_path(_load_settings(), "stock_path")
        text = "<b>📦 Stock</b>\n" if res.get("ok") else f"<b>📦 Stock</b>\n❌ {_html_escape(res.get('reason'))}\n"
        text += f"<code>{_html_escape(json.dumps(res.get('data'), ensure_ascii=False)[:1500])}</code>"
        kb = K().add(B("⬅️ Назад", callback_data=CBT_RBXCRATE_SECTION))
        _edit_or_send(call, text, kb)

    def detailed_stock_cb(call) -> None:
        _answer(call, "⏳")
        res = _rbxcrate_get_configured_path(_load_settings(), "detailed_stock_path")
        text = "<b>📊 Detailed stock</b>\n" if res.get("ok") else f"<b>📊 Detailed stock</b>\n❌ {_html_escape(res.get('reason'))}\n"
        text += f"<code>{_html_escape(json.dumps(res.get('data'), ensure_ascii=False)[:2500])}</code>"
        kb = K().add(B("⬅️ Назад", callback_data=CBT_RBXCRATE_SECTION))
        _edit_or_send(call, text, kb)

    def order_info_prompt_cb(call) -> None:
        msg = bot.send_message(call.message.chat.id, "Введите FunPay orderId для проверки в RBXCrate:")
        _answer(call)

        def handle(m):
            order_id = (m.text or "").strip().lstrip("#")
            res = _rbxcrate_order_info(_load_settings(), order_id)
            text = "✅ Order info:\n" if res.get("ok") else f"❌ API ошибка: {_html_escape(res.get('reason'))}\n"
            text += f"<code>{_html_escape(json.dumps(res.get('data'), ensure_ascii=False)[:1500])}</code>"
            bot.reply_to(m, text, parse_mode="HTML")
        bot.register_next_step_handler(msg, handle)

    def cancel_cb(call) -> None:
        msg = bot.send_message(call.message.chat.id, "Введите FunPay orderId для отмены в RBXCrate:")
        _answer(call)

        def handle(m):
            order_id = (m.text or "").strip().lstrip("#")
            res = _rbxcrate_cancel_order(_load_settings(), order_id)
            text = "✅ Заказ отменён:\n" if res.get("ok") else f"❌ API ошибка: {_html_escape(res.get('reason'))}\n"
            text += f"<code>{_html_escape(json.dumps(res.get('data'), ensure_ascii=False)[:1500])}</code>"
            bot.reply_to(m, text, parse_mode="HTML")
        bot.register_next_step_handler(msg, handle)

    def resend_cb(call) -> None:
        msg = bot.send_message(call.message.chat.id, "Введите orderId и placeId через пробел для resend:\nПример: UN123ABC 1234567890")
        _answer(call)

        def handle(m):
            parts = (m.text or "").strip().replace("#", "").split()
            if len(parts) < 2 or not parts[1].isdigit():
                return bot.reply_to(m, "❌ Формат: orderId placeId")
            res = _rbxcrate_resend_order(_load_settings(), parts[0], parts[1])
            text = "✅ Resend отправлен:\n" if res.get("ok") else f"❌ API ошибка: {_html_escape(res.get('reason'))}\n"
            text += f"<code>{_html_escape(json.dumps(res.get('data'), ensure_ascii=False)[:1500])}</code>"
            bot.reply_to(m, text, parse_mode="HTML")
        bot.register_next_step_handler(msg, handle)

    def refund_manual_cb(call) -> None:
        msg = bot.send_message(call.message.chat.id, "Введите FunPay orderId для возврата денег покупателю:")
        _answer(call)

        def handle(m):
            order_id = (m.text or "").strip().lstrip("#")
            if not order_id:
                return bot.reply_to(m, "❌ Пустой orderId.")
            _do_refund(cardinal, order_id, "manual refund from AutoRobux RBXCrate menu")
            bot.reply_to(m, f"✅ Запрос возврата отправлен для #{order_id}")
        bot.register_next_step_handler(msg, handle)

    def timeout_cb(call) -> None:
        msg = bot.send_message(call.message.chat.id, "Введите новый таймаут ожидания username/placeId в секундах:")
        _answer(call)

        def handle(m):
            try:
                value = int((m.text or "").strip())
                if not 60 <= value <= 604800:
                    raise ValueError
            except Exception:
                return bot.reply_to(m, "❌ Нужно число от 60 до 604800.")
            s = _load_settings()
            s["wait_timeout_sec"] = value
            _save_settings(s)
            bot.reply_to(m, f"✅ Таймаут сохранён: {value} сек")
        bot.register_next_step_handler(msg, handle)

    def help_cb(call) -> None:
        s = _load_settings()
        text = (
            "<b>❓ AutoRobux RBXCrate — гайд</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "1. В 🔌 API RBXCrate вставьте merchant API key.\n"
            "2. В 🗺 Лоты добавьте название/подстроку FunPay-лота и формулу Robux.\n"
            "3. После оплаты бот попросит у покупателя Roblox username и placeId/ссылку на плейс.\n"
            "4. Плагин создаст заказ в RBXCrate: /api/orders/gamepass.\n"
            "5. Через меню можно проверить, отменить RBXCrate-заказ, вернуть деньги FunPay и вести blacklist.\n\n"
            "<b>Текст гайда покупателю:</b>\n"
            f"<code>{_html_escape(s['messages'].get('guide', DEFAULT_MESSAGES['guide']))}</code>"
        )
        kb = K().add(B("◀️ Назад", callback_data=CBT_HOME))
        _edit_or_send(call, text, kb)
        _answer(call)

    def blacklist_cb(call) -> None:
        s = _load_settings()
        items = _load_blacklist()
        lines = ["<b>⛔ Blacklist покупателей</b>", f"Статус: {'🟢 вкл' if s.get('blacklist_enabled', True) else '🔴 выкл'}", ""]
        kb = K(row_width=1)
        for item in items:
            key = item.get("buyer_id") or item.get("buyer_username") or "?"
            lines.append(f"• <code>{_html_escape(key)}</code> — {_html_escape(item.get('reason', ''))}")
            kb.add(B(f"🗑 {key}", callback_data=f"{CBT_BL_DEL}:{key}"))
        if not items:
            lines.append("(пусто)")
        kb.add(B("➕ Добавить", callback_data=CBT_BL_ADD))
        kb.add(B("🟢/🔴 Вкл blacklist", callback_data=f"{CBT_BLACKLIST}:toggle"))
        kb.add(B("◀️ Назад", callback_data=CBT_HOME))
        _edit_or_send(call, "\n".join(lines), kb)
        _answer(call)

    def blacklist_toggle_cb(call) -> None:
        s = _load_settings()
        s["blacklist_enabled"] = not bool(s.get("blacklist_enabled", True))
        _save_settings(s)
        blacklist_cb(call)

    def bl_add_cb(call) -> None:
        msg = bot.send_message(call.message.chat.id, "Введите buyer_id или username покупателя для blacklist:")
        _answer(call)

        def step_key(m):
            key = (m.text or "").strip()
            if not key:
                return bot.reply_to(m, "❌ Пусто. Отменено.")
            msg2 = bot.reply_to(m, "Причина blacklist:")

            def step_reason(m2):
                reason = (m2.text or "manual").strip() or "manual"
                if key.isdigit():
                    added = _add_blacklist(buyer_id=key, reason=reason)
                else:
                    added = _add_blacklist(buyer_username=key, reason=reason)
                bot.reply_to(m2, "✅ Добавлено" if added else "ℹ Уже есть или пустой ключ")
            bot.register_next_step_handler(msg2, step_reason)
        bot.register_next_step_handler(msg, step_key)

    def bl_del_cb(call) -> None:
        key = call.data.split(":", 2)[-1]
        _remove_blacklist(key)
        blacklist_cb(call)

    def lots_cb(call) -> None:
        s = _load_settings()
        lines = ["<b>🗺 Привязки лотов</b>", ""]
        kb = K(row_width=1)
        for m in s.get("lot_mappings", []):
            label = m.get("lot_match")
            lines.append(f"• <code>{_html_escape(label)}</code> -> {m.get('robux_per_unit')}×кол-во + {m.get('extra_robux', 0)} Robux")
            kb.add(B(f"🗑 {label}", callback_data=f"{CBT_LOT_DEL}:{m.get('id')}"))
        if not s.get("lot_mappings"):
            lines.append("(пусто)")
        kb.add(B("➕ Добавить лот", callback_data=CBT_LOT_ADD))
        kb.add(B("◀️ Назад", callback_data=CBT_HOME))
        _edit_or_send(call, "\n".join(lines), kb)
        _answer(call)

    def lot_add_cb(call) -> None:
        msg = bot.send_message(call.message.chat.id, "Введите название/уникальную подстроку лота:")
        _answer(call)

        def step_match(m):
            match = (m.text or "").strip()
            if not match:
                return bot.reply_to(m, "❌ Пусто. Отменено.")
            msg2 = bot.reply_to(m, "Robux за 1 единицу заказа:")

            def step_per(m2):
                try:
                    per = int((m2.text or "").strip())
                    if per < 0:
                        raise ValueError
                except Exception:
                    return bot.reply_to(m2, "❌ Не число. Отменено.")
                msg3 = bot.reply_to(m2, "Extra Robux (можно 0):")

                def step_extra(m3):
                    try:
                        extra = int((m3.text or "").strip())
                        if extra < 0:
                            raise ValueError
                    except Exception:
                        return bot.reply_to(m3, "❌ Не число. Отменено.")
                    s = _load_settings()
                    s.setdefault("lot_mappings", []).append({"id": _new_id("m", s.get("lot_mappings", [])), "lot_match": match, "robux_per_unit": per, "extra_robux": extra})
                    _save_settings(s)
                    bot.reply_to(m3, f"✅ Добавлено: {match} -> {per}×кол-во + {extra}")
                bot.register_next_step_handler(msg3, step_extra)
            bot.register_next_step_handler(msg2, step_per)
        bot.register_next_step_handler(msg, step_match)

    def lot_del_cb(call) -> None:
        mid = call.data.split(":", 2)[-1]
        s = _load_settings()
        s["lot_mappings"] = [m for m in s.get("lot_mappings", []) if str(m.get("id")) != str(mid)]
        _save_settings(s)
        lots_cb(call)

    def msgs_cb(call) -> None:
        s = _load_settings()
        kb = K(row_width=1)
        lines = ["<b>📜 Сообщения</b>", ""]
        for key in DEFAULT_MESSAGES:
            label = _SLOT_LABELS.get(key, key)
            lines.append(f"• <b>{_html_escape(label)}</b> (<code>{key}</code>): {_html_escape(s['messages'].get(key, ''))[:120]}")
            kb.add(B(f"✏️ {label}", callback_data=f"{CBT_MSG_EDIT}:{key}"))
        kb.add(B("♻️ Сбросить шаблоны", callback_data=CBT_MSG_RESET))
        kb.add(B("◀️ Назад", callback_data=CBT_HOME))
        _edit_or_send(call, "\n".join(lines), kb)
        _answer(call)

    def msg_edit_cb(call) -> None:
        key = call.data.split(":", 2)[-1]
        if key not in DEFAULT_MESSAGES:
            return _answer(call, "unknown")
        current = _load_settings()["messages"].get(key, DEFAULT_MESSAGES[key])
        hint = ""
        if key == "ask_username":
            hint = "\nДоступны плейсхолдеры: {robux}, {gamepass_price}, {order_id}."
        label = _SLOT_LABELS.get(key, key)
        msg = bot.send_message(call.message.chat.id, f"📜 Текущий «{label}»:\n{current}\n\nПришлите новый текст.{hint}")
        _answer(call)

        def handle(m):
            text = (m.text or "").strip()
            if not text:
                return bot.reply_to(m, "❌ Пусто. Отменено.")
            ok, err = _validate_template(key, text)
            if not ok:
                return bot.reply_to(m, f"❌ Ошибка плейсхолдера: {err}")
            s = _load_settings()
            s.setdefault("messages", {})[key] = text
            _save_settings(s)
            bot.reply_to(m, "✅ Сохранено")
        bot.register_next_step_handler(msg, handle)

    def msg_reset_cb(call) -> None:
        s = _load_settings()
        s["messages"] = dict(DEFAULT_MESSAGES)
        _save_settings(s)
        _answer(call, "Сброшено")
        msgs_cb(call)

    def orders_cb(call) -> None:
        orders = _load_orders()[-10:]
        lines = ["<b>📦 Последние заказы</b>", ""]
        for o in reversed(orders):
            lines.append(f"#{_html_escape(o.get('order_id'))}: {_html_escape(o.get('username'))} -> {o.get('robux')} Robux / {o.get('status')}")
        if not orders:
            lines.append("(пусто)")
        kb = K(row_width=1)
        for o in reversed(orders):
            oid = o.get("order_id")
            kb.add(B(f"📄 #{oid} / {o.get('status')}", callback_data=f"{CBT_ORDER_VIEW}:{oid}"))
        kb.add(B("🔎 Проверить orderId", callback_data=CBT_ORDER_INFO))
        kb.add(B("🔁 Resend order", callback_data=CBT_RESEND))
        kb.add(B("❌ Отменить orderId", callback_data=CBT_CANCEL))
        kb.add(B("📤 Export JSON", callback_data=CBT_EXPORT_JSON))
        kb.add(B("📤 Export CSV", callback_data=CBT_EXPORT_CSV))
        kb.add(B("◀️ Назад", callback_data=CBT_HOME))
        _edit_or_send(call, "\n".join(lines), kb)
        _answer(call)

    def order_view_cb(call) -> None:
        order_id = call.data.split(":", 2)[-1]
        order = _find_order(order_id)
        if not order:
            _answer(call, "не найден")
            return orders_cb(call)
        text = (
            f"<b>📄 Заказ #{_html_escape(order.get('order_id'))}</b>\n"
            f"Статус: <code>{_html_escape(order.get('status'))}</code>\n"
            f"Buyer: <code>{_html_escape(order.get('buyer_username') or order.get('buyer_id'))}</code>\n"
            f"Roblox: <code>{_html_escape(order.get('username'))}</code>\n"
            f"Place: <code>{_html_escape(order.get('place_id'))}</code>\n"
            f"Robux: <code>{_html_escape(order.get('robux'))}</code>\n"
            f"GamePass price: <code>{_html_escape(order.get('gamepass_price'))}</code>\n"
            f"FunPay revenue: <code>{_html_escape(order.get('sold_price'))}</code>\n"
            f"RBXCrate cost: <code>{_provider_price(order)}</code>"
        )
        kb = K(row_width=2)
        kb.row(B("🔎 Проверить", callback_data=f"{CBT_ORDER_ACT}:info:{order_id}"), B("🔁 Resend", callback_data=f"{CBT_ORDER_ACT}:resend:{order_id}"))
        kb.row(B("❌ Cancel", callback_data=f"{CBT_ORDER_ACT}:cancel:{order_id}"), B("💸 Refund", callback_data=f"{CBT_ORDER_ACT}:refund:{order_id}"))
        kb.row(B("⛔ Blacklist buyer", callback_data=f"{CBT_ORDER_ACT}:blacklist:{order_id}"))
        kb.row(B("◀️ Назад", callback_data=CBT_ORDERS))
        _edit_or_send(call, text, kb)
        _answer(call)

    def order_action_cb(call) -> None:
        tail = (call.data or "")[len(CBT_ORDER_ACT) + 1:]
        action, order_id = tail.split(":", 1)
        order = _find_order(order_id)
        if not order:
            _answer(call, "не найден")
            return orders_cb(call)
        if action == "info":
            res = _rbxcrate_order_info(_load_settings(), order_id)
            _save_order_update(order_id, {"last_info_response": res, "updated_at": time.time()})
            _answer(call, "Проверено")
            return order_view_cb(call)
        if action == "resend":
            res = _rbxcrate_resend_order(_load_settings(), order_id, order.get("place_id") or 0)
            _save_order_update(order_id, {"resend_attempts": int(order.get("resend_attempts", 0) or 0) + 1, "last_resend_response": res, "updated_at": time.time()})
            _append_action("resend", order_id=order_id, ok=res.get("ok"))
            _answer(call, "Resend отправлен" if res.get("ok") else "Resend ошибка")
            return order_view_cb(call)
        if action == "cancel":
            if order.get("cancel_attempted"):
                _answer(call, "cancel уже был")
                return
            res = _rbxcrate_cancel_order(_load_settings(), order_id)
            _save_order_update(order_id, {"cancel_attempted": True, "cancel_response": res, "updated_at": time.time()})
            _append_action("cancel", order_id=order_id, ok=res.get("ok"))
            _answer(call, "Cancel отправлен" if res.get("ok") else "Cancel ошибка")
            return order_view_cb(call)
        if action == "refund":
            if order.get("refunded_at"):
                _answer(call, "refund уже был")
                return
            kb = K(row_width=1)
            kb.add(B("✅ Да, вернуть деньги", callback_data=f"{CBT_REFUND_CONFIRM}:{order_id}"))
            kb.add(B("❌ Отмена", callback_data=f"{CBT_ORDER_VIEW}:{order_id}"))
            _edit_or_send(call, f"⚠️ Точно вернуть деньги по заказу #{_html_escape(order_id)}?", kb)
            _answer(call)
            return

        if action == "blacklist":
            _add_blacklist(buyer_id=order.get("buyer_id"), buyer_username=order.get("buyer_username"), reason=f"manual from order {order_id}")
            _append_action("blacklist", order_id=order_id, buyer_id=order.get("buyer_id"))
            _answer(call, "Добавлен в blacklist")
            return order_view_cb(call)

    def refund_confirm_cb(call) -> None:
        order_id = (call.data or "").split(":", 2)[-1]
        order = _find_order(order_id)
        if not order:
            _answer(call, "не найден")
            return orders_cb(call)
        if order.get("refunded_at"):
            _answer(call, "refund уже был")
            return order_view_cb(call)
        _do_refund(cardinal, order_id, "manual refund from order card")
        _save_order_update(order_id, {"refunded_at": time.time()})
        _append_action("refund", order_id=order_id)
        _answer(call, "Refund отправлен")
        return order_view_cb(call)

    def margin_cb(call) -> None:
        orders = _load_orders()
        now = time.time()
        day = _revenue_stats([o for o in orders if now - float(o.get("created_at", 0) or 0) <= 86400])
        week = _revenue_stats([o for o in orders if now - float(o.get("created_at", 0) or 0) <= 7 * 86400])
        all_time = _revenue_stats(orders)

        def block(title: str, data: dict) -> str:
            return (
                f"<b>{title}</b>\n"
                f"Заказов: <code>{data['count']}</code> | Completed: <code>{data['completed']}</code> | Failed: <code>{data['failed']}</code>\n"
                f"Robux: <code>{data['robux']}</code>\n"
                f"FunPay revenue: <code>{data['revenue']}</code>\n"
                f"RBXCrate cost: <code>{data['cost']}</code>\n"
                f"Profit: <code>{data['profit']}</code>\n"
                f"Margin: <code>{data['margin']}%</code>"
            )

        text = "<b>📊 Revenue / Margin</b>\n━━━━━━━━━━━━━━━━━━\n" + "\n\n".join([
            block("24h", day),
            block("7d", week),
            block("All time", all_time),
        ])
        kb = K().add(B("◀️ Назад", callback_data=CBT_HOME))
        _edit_or_send(call, text, kb)
        _answer(call)

    def queue_cb(call) -> None:
        queued_statuses = {"Pending", "Queued", "Queued_Deferred", "accepted"}
        items = [o for o in _load_orders() if str(o.get("status")) in queued_statuses]
        lines = ["<b>📋 Очередь RBXCrate</b>", ""]
        for o in items[-20:]:
            lines.append(f"#{_html_escape(o.get('order_id'))}: {_html_escape(o.get('username'))} -> {o.get('robux')} Robux / <code>{_html_escape(o.get('status'))}</code>")
        if not items:
            lines.append("(пусто)")
        kb = K(row_width=1)
        kb.add(B("🔁 Resend order", callback_data=CBT_RESEND))
        kb.add(B("◀️ Назад", callback_data=CBT_HOME))
        _edit_or_send(call, "\n".join(lines), kb)
        _answer(call)

    def export_json_cb(call) -> None:
        data = json.dumps(_load_orders(), ensure_ascii=False, indent=2).encode("utf-8")
        bio = io.BytesIO(data)
        bio.name = "autorobux_rbxcrate_orders.json"
        bot.send_document(call.message.chat.id, bio)
        _answer(call)

    def export_csv_cb(call) -> None:
        rows = _load_orders()
        out = io.StringIO()
        fields = ["order_id", "username", "place_id", "robux", "gamepass_price", "status", "reason", "created_at", "updated_at"]
        writer = csv.DictWriter(out, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
        bio = io.BytesIO(out.getvalue().encode("utf-8"))
        bio.name = "autorobux_rbxcrate_orders.csv"
        bot.send_document(call.message.chat.id, bio)
        _answer(call)

    def cmd_open(m):
        _persist_op(m.chat.id)
        bot.send_message(m.chat.id, _main_text(), parse_mode="HTML", reply_markup=_main_kb())

    tg.cbq_handler(home_cb, lambda c: f"{CBT.PLUGIN_SETTINGS}:{UUID}" in (c.data or ""))
    tg.cbq_handler(home_cb, lambda c: c.data == CBT_HOME)
    tg.cbq_handler(mode_screen_cb, lambda c: c.data == CBT_MODE_SCREEN)
    tg.cbq_handler(mode_rbxcrate_cb, lambda c: c.data == CBT_MODE_RBXCRATE)
    tg.cbq_handler(mode_gamepass_cb, lambda c: c.data == CBT_MODE_GAMEPASS)
    tg.cbq_handler(mode_pause_cb, lambda c: c.data == CBT_MODE_PAUSE)
    tg.cbq_handler(mode_test_cb, lambda c: c.data == CBT_MODE_TEST)
    tg.cbq_handler(rbxcrate_section_cb, lambda c: c.data == CBT_RBXCRATE_SECTION)
    tg.cbq_handler(gamepass_section_cb, lambda c: c.data == CBT_GAMEPASS_SECTION)
    tg.cbq_handler(gamepass_section_cb, lambda c: c.data == "autorobux:home")
    tg.cbq_handler(gp_mappings_cb, lambda c: c.data == GP_CBT_MAPPINGS)
    tg.cbq_handler(gp_mapping_add_cb, lambda c: c.data == GP_CBT_MAPPING_ADD)
    tg.cbq_handler(gp_test_desc_cb, lambda c: c.data == GP_CBT_TEST_DESC)
    tg.cbq_handler(gp_quick_add_cb, lambda c: c.data == GP_CBT_QUICK_ADD)
    tg.cbq_handler(gp_mapping_view_cb, lambda c: (c.data or "").startswith(f"{GP_CBT_MAPPING_VIEW}:"))
    tg.cbq_handler(gp_mapping_edit_cb, lambda c: (c.data or "").startswith(f"{GP_CBT_MAPPING_EDIT}:"))
    tg.cbq_handler(gp_mapping_del_cb, lambda c: (c.data or "").startswith(f"{GP_CBT_MAPPING_DEL}:"))
    tg.cbq_handler(settings_cb, lambda c: c.data == CBT_SETTINGS)
    tg.cbq_handler(config_cb, lambda c: c.data == CBT_CONFIG)
    tg.cbq_handler(config_export_cb, lambda c: c.data == CBT_CONFIG_EXPORT)
    tg.cbq_handler(config_import_cb, lambda c: c.data == CBT_CONFIG_IMPORT)
    tg.cbq_handler(health_cb, lambda c: c.data == CBT_HEALTH)
    tg.cbq_handler(simulate_cb, lambda c: c.data == CBT_SIMULATE)
    tg.cbq_handler(toggle_cb, lambda c: c.data == CBT_TOGGLE)
    tg.cbq_handler(test_mode_cb, lambda c: c.data == CBT_TEST_MODE)
    tg.cbq_handler(auto_base_cb, lambda c: c.data == CBT_AUTO_BASE)
    tg.cbq_handler(refund_cb, lambda c: c.data == CBT_REFUND)
    tg.cbq_handler(manage_lots_cb, lambda c: c.data == CBT_MANAGE_LOTS)
    tg.cbq_handler(limits_cb, lambda c: c.data == CBT_LIMITS)
    tg.cbq_handler(lang_cb, lambda c: c.data == CBT_LANG)
    tg.cbq_handler(polling_cb, lambda c: c.data == CBT_POLLING)
    tg.cbq_handler(polling_interval_cb, lambda c: c.data == CBT_POLLING_INTERVAL)
    tg.cbq_handler(autocancel_cb, lambda c: c.data == CBT_AUTOCANCEL)
    tg.cbq_handler(oos_policy_cb, lambda c: c.data == CBT_OOS_POLICY)
    tg.cbq_handler(api_cb, lambda c: c.data == CBT_API)
    tg.cbq_handler(api_edit_cb, lambda c: (c.data or "").startswith(f"{CBT_API_EDIT}:"))
    tg.cbq_handler(balance_cb, lambda c: c.data == CBT_BALANCE)
    tg.cbq_handler(stock_cb, lambda c: c.data == CBT_STOCK)
    tg.cbq_handler(detailed_stock_cb, lambda c: c.data == CBT_DETAILED_STOCK)
    tg.cbq_handler(order_info_prompt_cb, lambda c: c.data == CBT_ORDER_INFO)
    tg.cbq_handler(cancel_cb, lambda c: c.data == CBT_CANCEL)
    tg.cbq_handler(resend_cb, lambda c: c.data == CBT_RESEND)
    tg.cbq_handler(refund_manual_cb, lambda c: c.data == CBT_REFUND_MANUAL)
    tg.cbq_handler(timeout_cb, lambda c: c.data == CBT_TIMEOUT)
    tg.cbq_handler(help_cb, lambda c: c.data == CBT_HELP)
    tg.cbq_handler(blacklist_cb, lambda c: c.data == CBT_BLACKLIST)
    tg.cbq_handler(blacklist_toggle_cb, lambda c: c.data == f"{CBT_BLACKLIST}:toggle")
    tg.cbq_handler(bl_add_cb, lambda c: c.data == CBT_BL_ADD)
    tg.cbq_handler(bl_del_cb, lambda c: (c.data or "").startswith(f"{CBT_BL_DEL}:"))
    tg.cbq_handler(lots_cb, lambda c: c.data == CBT_LOTS)
    tg.cbq_handler(lot_add_cb, lambda c: c.data == CBT_LOT_ADD)
    tg.cbq_handler(lot_del_cb, lambda c: (c.data or "").startswith(f"{CBT_LOT_DEL}:"))
    tg.cbq_handler(msgs_cb, lambda c: c.data == CBT_MSGS)
    tg.cbq_handler(msg_edit_cb, lambda c: (c.data or "").startswith(f"{CBT_MSG_EDIT}:"))
    tg.cbq_handler(msg_reset_cb, lambda c: c.data == CBT_MSG_RESET)
    tg.cbq_handler(orders_cb, lambda c: c.data == CBT_ORDERS)
    tg.cbq_handler(order_view_cb, lambda c: (c.data or "").startswith(f"{CBT_ORDER_VIEW}:"))
    tg.cbq_handler(order_action_cb, lambda c: (c.data or "").startswith(f"{CBT_ORDER_ACT}:"))
    tg.cbq_handler(refund_confirm_cb, lambda c: (c.data or "").startswith(f"{CBT_REFUND_CONFIRM}:"))
    tg.cbq_handler(queue_cb, lambda c: c.data == CBT_QUEUE)
    tg.cbq_handler(margin_cb, lambda c: c.data == CBT_MARGIN)
    tg.cbq_handler(export_json_cb, lambda c: c.data == CBT_EXPORT_JSON)
    tg.cbq_handler(export_csv_cb, lambda c: c.data == CBT_EXPORT_CSV)
    try:
        tg.msg_handler(cmd_open, commands=["autorobuxdrake", "autorobuxcrate", "rbxcrate", "autorobux"])
        cardinal.add_telegram_commands(UUID, [
            ("autorobuxdrake", "autorobuxdrake: меню", True),
            ("autorobux", "AutoRobux: открыть меню", True),
        ])
    except Exception:
        logger.exception("%s add_telegram_commands failed", LOGGER_PREFIX)
    # 💛 Донат-баннер (защита реквизитов автора)
    global _donation_cardinal
    _donation_cardinal = cardinal
    try:
        tg.cbq_handler(
            _donation_on_cb,
            lambda c: (c.data or "").startswith(DONATION_CALLBACK_PREFIX + ":"))
        _start_donation_reminder(cardinal)
    except Exception:
        logger.exception("%s donation banner register failed", LOGGER_PREFIX)
    _ensure_timeout_thread(cardinal)
    logger.info("%s v%s запущен", LOGGER_PREFIX, VERSION)
    # 📦 Одноразовое приветствие с рекламой канала автора
    if DONATION_SHOW_ON_START:
        try:
            _send_startup_welcome(cardinal)
        except Exception:
            logger.debug("startup welcome send failed", exc_info=True)



def _on_delete(cardinal: "Cardinal", *args) -> None:
    _stop.set()




# =========================================================================
# Embedded legacy GamePass engine (autorobux_fpc.py) — bundled for one-file mode
# =========================================================================
_EMBEDDED_GAMEPASS_SOURCE = (
    '"""\nAutoRobux plugin for FunPayCardinal\n===================================\n\nАвто-доставка Robux покупателям через схему выкупа GamePass.\n\nПоток: новый оплаченный заказ → плагин просит покупателя создать GamePass на\nровно нужное число Robux и прислать его ID/ссылку → плагин проверяет цену через\n`apis.roblox.com/game-passes/v1/game-passes/{id}/product-info` → получает\n`X-CSRF-Token` POST-пингом на `economy.roblox.com/v1/purchases/products/{product_id}`\n→ выкупает GamePass с одного из настроенных Roblox-аккаунтов (несколько аккаунтов\nс failover по балансу). При неудаче/таймауте — авто-возврат на FunPay и\nуведомление оператора. Управление — русскоязычное Telegram-меню.\n\nЧисто, без бэкдоров: НЕТ подключения к БД, НЕТ удалённой «активации»/лицензий и\nkill-switch. Outbound только к *.roblox.com / *.roblox.com.br, настроенным\nпрокси, funpay.com и Telegram Bot API. Cookie `.ROBLOSECURITY` никогда не\nлогируется и маскируется в любом отображении оператору.\n"""\n\nfrom __future__ import annotations\n\nimport importlib\nimport json\nimport logging\nimport os\nimport re\nimport subprocess\nimport sys\nimport threading\nimport time\nfrom pathlib import Path\nfrom urllib.parse import urlparse\nfrom typing import TYPE_CHECKING, Any\n\n# ── Авто-установка внешних зависимостей ──────────────────────────────────────\n# Плагин использует httpx. Если его нет в окружении FPC — ставим автоматически\n# в тот же Python, который запустил Cardinal, чтобы не пришлось делать pip\n# install руками. Стиль повторяет steam_rental._ensure_dependency.\n_BOOT_LOGGER = logging.getLogger("FPC.autorobux")\n\n\ndef _ensure_dependency(pip_name: str, import_name: "str | None" = None) -> bool:\n    """Гарантирует наличие пакета. Возвращает True если модуль доступен."""\n    mod_name = import_name or pip_name\n    try:\n        importlib.import_module(mod_name)\n        return True\n    except ImportError:\n        pass\n    _BOOT_LOGGER.warning(\n        "autorobux: модуль %r не найден, ставлю %r через pip...",\n        mod_name, pip_name)\n    try:\n        subprocess.check_call(\n            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",\n             "--quiet", pip_name],\n            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)\n    except Exception as exc:\n        _BOOT_LOGGER.error(\n            "autorobux: не удалось установить %r автоматически: %s. "\n            "Поставь вручную: %s -m pip install %s",\n            pip_name, exc, sys.executable, pip_name)\n        return False\n    importlib.invalidate_caches()\n    try:\n        importlib.import_module(mod_name)\n        _BOOT_LOGGER.info("autorobux: %r успешно установлен.", pip_name)\n        return True\n    except ImportError as exc:\n        _BOOT_LOGGER.error(\n            "autorobux: %r поставился, но импорт всё равно падает: %s",\n            pip_name, exc)\n        return False\n\n\n_ensure_dependency("httpx")\n\nimport httpx\nfrom telebot.types import InlineKeyboardButton as B\nfrom telebot.types import InlineKeyboardMarkup as K\n\ntry:\n    from tg_bot import CBT\nexcept Exception:\n    class CBT:\n        PLUGIN_SETTINGS = "PLUGIN_SETTINGS"\n        CLEAR_STATE = "CLEAR_STATE"\n\nif TYPE_CHECKING:\n    from cardinal import Cardinal\n\n\n# =========================================================================\n# Метаданные плагина (обязательные для FPC)\n# =========================================================================\n\nNAME = "AutoRobux"\nVERSION = "1.2.1"\nDESCRIPTION = (\n    "Авто-доставка Robux через выкуп GamePass: несколько Roblox-акк'
    'аунтов с "\n    "failover по балансу, проверка цены GamePass, CSRF-выкуп, авто-возврат при "\n    "провале/таймауте, русскоязычное Telegram-меню. Без бэкдоров и удалённой "\n    "активации."\n)\nCREDITS = "@drakelovc"\nUUID = "3e139f4a-7bf7-4031-842a-7b59150c82c9"\nSETTINGS_PAGE = True\n\nlogger = logging.getLogger(f"FPC.{__name__}")\nLOGGER_PREFIX = "[AUTOROBUX]"\n\n\n# =========================================================================\n# Хранилище: пути, диапазоны и дефолты\n# =========================================================================\n\nPLUGIN_DIR = Path("storage/plugins/autorobux")\nSETTINGS_PATH = PLUGIN_DIR / "settings.json"\nORDERS_PATH = PLUGIN_DIR / "orders.json"\n\nRANGE_WAIT_TIMEOUT = (3600, 7 * 86400)     # 1 час .. 7 суток\nRANGE_MAX_ATTEMPTS = (1, 20)\n\nDEFAULT_MESSAGES = {\n    "ask_gamepass": (\n        "💎 Спасибо за оплату!\\n"\n        "Создайте GamePass на ровно {expected_price} Robux и пришлите его ID или ссылку.\\n"\n        "Инструкция: https://telegra.ph/Kak-sozdat-GamePass-dlya-AutoRobux-07-06"\n    ),\n    "wrong_price": (\n        "❗ Цена GamePass {actual} ≠ требуемой {expected}. "\n        "Создайте новый GamePass с правильной ценой и пришлите ID снова."\n    ),\n    "success": "✅ Robux отправлены! Подтвердите заказ на FunPay.",\n    "failure": "❌ Не удалось приобрести GamePass. Средства возвращены.",\n}\n\nDEFAULT_SETTINGS: dict[str, Any] = {\n    "accounts": [],                 # [{id, label, cookie, proxy}]\n    "lot_mappings": [],             # [{id, lot_match, lot_id?, robux_per_unit, extra_robux}]\n    "auto_refund": True,\n    "wait_timeout_sec": 86400,\n    "max_attempts": 5,\n    "messages": dict(DEFAULT_MESSAGES),\n    "operator_chat_id": None,\n    "last_used_index": 0,\n}\n\n_MESSAGE_SLOTS = ("ask_gamepass", "wrong_price", "success", "failure")\n\n_io_lock = threading.RLock()\n\n# хосты, на которые плагину разрешён исходящий трафик (Req 6.2)\n_ALLOWED_HOST_SUFFIXES = (\n    ".roblox.com",\n    ".roblox.com.br",\n    "funpay.com",\n    "api.telegram.org",\n)\n_ALLOWED_HOSTS_EXACT = (\n    "roblox.com",\n    "roblox.com.br",\n)\n\n\n# =========================================================================\n# Чистое ядро (pure core)\n# =========================================================================\n\nGAMEPASS_URL_RE = re.compile(r"/game-pass/(\\d+)", re.IGNORECASE)\n\n\ndef _extract_gamepass_id(text: str) -> int | None:\n    """Возвращает целочисленный id GamePass, если `text` — строка из ASCII-цифр\n    ИЛИ содержит подстроку `/game-pass/<цифры>`; иначе None. (Property 1, Req 3.2)"""\n    s = (text or "").strip()\n    if s.isascii() and s.isdigit():\n        return int(s)\n    m = GAMEPASS_URL_RE.search(s)\n    return int(m.group(1)) if m else None\n\n\ndef _expected_price(funpay_units: int, robux_per_unit: int, extra_robux: int = 0) -> int:\n    """Линейная формула цены: units * per_unit + extra. (Property 2, Req 2.2)"""\n    return int(funpay_units) * int(robux_per_unit) + int(extra_robux)\n\n\ndef _account_order(accounts: list[dict], start_index: int = 0) -> list[dict]:\n    """Ротация аккаунтов: перестановка списка, покрывающая каждый аккаунт ровно\n    один раз, начиная с start_index (по модулю). Пустой список → []. (Property 3, Req 4.5)"""\n    n = len(accounts)\n    if not n:\n        return []\n    start = start_index % n\n    return [accounts[(start + i) % n] for i in range(n)]\n\n\ndef _mask_secret(s: str | None, head: int = 4, tail: int = 2) -> str:\n    """Маскирует секрет: оставляет первые head и последние tail символов.\n    Полный секрет никогда не ра'
    'скрывается. (Property 4, Req 6.3)"""\n    if not s:\n        return ""\n    s = str(s)\n    if len(s) <= head + tail:\n        return "***"\n    return s[:head] + "…" + s[-tail:]\n\n\ndef _host_allowed(host: str | None, proxies: list[str] | None = None) -> bool:\n    """True, если host входит в allow-list: *.roblox.com / *.roblox.com.br /\n    funpay.com / Telegram Bot API / хосты настроенных прокси. (Req 6.2)"""\n    h = (host or "").strip().lower()\n    if not h:\n        return False\n    if h in _ALLOWED_HOSTS_EXACT:\n        return True\n    for suf in _ALLOWED_HOST_SUFFIXES:\n        if h == suf.lstrip(".") or h.endswith(suf):\n            return True\n    for proxy in (proxies or []):\n        try:\n            phost = (urlparse(proxy).hostname or "").lower()\n        except Exception:\n            phost = ""\n        if phost and h == phost:\n            return True\n    return False\n\n\ndef _classify_purchase_reason(reason: str | None) -> str:\n    """\'insufficient_funds\' | \'error\' по тексту причины отказа Roblox."""\n    r = (reason or "").lower()\n    if "insufficient" in r or "not enough" in r or "недостаточно" in r:\n        return "insufficient_funds"\n    return "error"\n\n\ndef _aggregate_stats(records: list[dict], since_ts: float) -> dict:\n    """Суммирует успешные доставки с finalized_at >= since_ts:\n    {count, revenue, robux}."""\n    count = 0\n    revenue = 0.0\n    robux = 0\n    for r in records or []:\n        try:\n            if str(r.get("status")) != "success":\n                continue\n            fin = float(r.get("finalized_at", 0) or 0)\n            if fin < since_ts:\n                continue\n            count += 1\n            revenue += float(r.get("sold_price", 0) or 0)\n            robux += int(r.get("expected_price", 0) or 0)\n        except Exception:\n            continue\n    return {"count": count, "revenue": round(revenue, 2), "robux": robux}\n\n\ndef _html_escape(s: str) -> str:\n    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))\n\n\ndef _normalize_lot_text(text: Any) -> str:\n    """Нормализация названия лота для Cardinal-style привязки по имени."""\n    s = str(text or "").replace("\\u200c", "").replace("\\u200d", "")\n    return " ".join(s.casefold().split())\n\n\n# =========================================================================\n# Хранилище: load/save (атомарная запись tmp + os.replace)\n# =========================================================================\n\ndef _ensure_dir() -> None:\n    PLUGIN_DIR.mkdir(parents=True, exist_ok=True)\n\n\ndef _clamp(v: Any, lo: int, hi: int) -> int:\n    try:\n        x = int(v)\n    except Exception:\n        return lo\n    return max(lo, min(hi, x))\n\n\ndef _load_json(path: Path, default):\n    _ensure_dir()\n    if not path.exists():\n        return json.loads(json.dumps(default))\n    try:\n        with _io_lock, open(path, "r", encoding="utf-8") as f:\n            return json.load(f)\n    except Exception:\n        logger.warning(f"{LOGGER_PREFIX} Не удалось прочитать {path}", exc_info=True)\n        return json.loads(json.dumps(default))\n\n\ndef _save_json(path: Path, data) -> None:\n    _ensure_dir()\n    tmp = path.with_suffix(path.suffix + ".tmp")\n    try:\n        with _io_lock:\n            with open(tmp, "w", encoding="utf-8") as f:\n                json.dump(data, f, ensure_ascii=False, indent=2)\n            os.replace(tmp, path)\n    except Exception:\n        logger.warning(f"{LOGGER_PREFIX} Не удалось записать {path}", exc_info=True)\n        try:\n            tmp.unlink(missing_ok'
    '=True)\n        except Exception:\n            pass\n\n\ndef _load_settings() -> dict[str, Any]:\n    merged = json.loads(json.dumps(DEFAULT_SETTINGS))\n    data = _load_json(SETTINGS_PATH, {})\n    if isinstance(data, dict):\n        for k, v in data.items():\n            merged[k] = v\n    # setdefault-миграция новых ключей для старых конфигов\n    merged.setdefault("accounts", [])\n    merged.setdefault("lot_mappings", [])\n    merged.setdefault("auto_refund", DEFAULT_SETTINGS["auto_refund"])\n    merged.setdefault("operator_chat_id", None)\n    merged.setdefault("last_used_index", 0)\n    # сообщения: гарантируем все слоты непустыми строками\n    msgs = merged.get("messages") or {}\n    if not isinstance(msgs, dict):\n        msgs = {}\n    for slot in _MESSAGE_SLOTS:\n        val = msgs.get(slot)\n        if not isinstance(val, str) or not val.strip():\n            msgs[slot] = DEFAULT_MESSAGES[slot]\n    merged["messages"] = msgs\n    # числовые диапазоны\n    merged["wait_timeout_sec"] = _clamp(merged.get("wait_timeout_sec"), *RANGE_WAIT_TIMEOUT)\n    merged["max_attempts"] = _clamp(merged.get("max_attempts"), *RANGE_MAX_ATTEMPTS)\n    return merged\n\n\ndef _save_settings(s: dict[str, Any]) -> None:\n    _save_json(SETTINGS_PATH, s)\n\n\ndef _load_orders() -> list[dict]:\n    data = _load_json(ORDERS_PATH, [])\n    return data if isinstance(data, list) else []\n\n\ndef _save_orders(orders: list[dict]) -> None:\n    _save_json(ORDERS_PATH, orders)\n\n\ndef _record_order(entry: dict) -> None:\n    with _io_lock:\n        orders = _load_orders()\n        orders.append(entry)\n        if len(orders) > 2000:\n            orders = orders[-2000:]\n        _save_orders(orders)\n\n\ndef _proxy_list(settings: dict) -> list[str]:\n    out = []\n    for acc in settings.get("accounts", []):\n        p = acc.get("proxy")\n        if p:\n            out.append(p)\n    return out\n\n\n# =========================================================================\n# Roblox-клиент\n# =========================================================================\n\nROBLOX_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"\n\n\ndef _cookie_header(cookie: str | None) -> str:\n    raw = (cookie or "").strip()\n    if not raw:\n        return ""\n    return raw if raw.startswith(".ROBLOSECURITY=") else f".ROBLOSECURITY={raw}"\n\n\nclass RobloxClient:\n    """Тонкий клиент Roblox поверх httpx. Cookie `.ROBLOSECURITY` никогда не\n    логируется (нигде не печатаем self.cookie)."""\n\n    def __init__(self, cookie: str | None = None, proxy: str | None = None):\n        self.cookie = cookie or ""\n        self.proxy = proxy or None\n        self._client: httpx.Client | None = None\n\n    # --- low-level ---\n    def _http(self) -> httpx.Client:\n        if self._client is None:\n            try:\n                self._client = (httpx.Client(timeout=15, proxy=self.proxy)\n                                if self.proxy else httpx.Client(timeout=15))\n            except TypeError:\n                # старые/новые версии httpx с другой сигнатурой прокси\n                self._client = httpx.Client(timeout=15)\n        return self._client\n\n    def _headers(self, extra: dict | None = None) -> dict:\n        h = {"User-Agent": ROBLOX_UA}\n        ch = _cookie_header(self.cookie)\n        if ch:\n            h["Cookie"] = ch\n        if extra:\n            h.update(extra)\n        return h\n\n    def close(self) -> None:\n        try:\n            if self._client is not None:\n                self._client.close()\n        except Exception:\n            pass\n\n    #'
    ' --- high-level ---\n    def whoami(self) -> dict | None:\n        """{\'id\', \'name\'} текущего аккаунта или None."""\n        r = self._http().get("https://users.roblox.com/v1/users/authenticated",\n                             headers=self._headers())\n        if r.status_code != 200:\n            logger.warning(f"{LOGGER_PREFIX} whoami: статус {r.status_code}")\n            return None\n        data = r.json()\n        return {"id": data.get("id"), "name": data.get("name") or data.get("displayName")}\n\n    def balance(self, user_id: Any) -> int:\n        """Баланс Robux аккаунта; -1 при ошибке."""\n        r = self._http().get(f"https://economy.roblox.com/v1/users/{user_id}/currency",\n                             headers=self._headers())\n        if r.status_code != 200:\n            logger.warning(f"{LOGGER_PREFIX} balance: статус {r.status_code}")\n            return -1\n        try:\n            return int(r.json().get("robux", 0))\n        except Exception:\n            return -1\n\n    def gamepass_info(self, gamepass_id: Any) -> dict | None:\n        """{\'ProductId\', \'PriceInRobux\', \'CreatorId\'} или None."""\n        url = f"https://apis.roblox.com/game-passes/v1/game-passes/{gamepass_id}/product-info"\n        r = self._http().get(url, headers=self._headers())\n        if r.status_code != 200:\n            logger.warning(f"{LOGGER_PREFIX} gamepass_info: статус {r.status_code}")\n            return None\n        info = r.json()\n        creator = info.get("Creator") or {}\n        price = info.get("PriceInRobux")\n        return {\n            "ProductId": info.get("ProductId"),\n            "PriceInRobux": int(price) if price is not None else None,\n            "CreatorId": creator.get("Id"),\n        }\n\n    def csrf_then_purchase(self, product_id: Any, expected_price: int,\n                           expected_seller_id: Any) -> dict:\n        """Двухшаговый выкуп: 1) POST для получения X-CSRF-Token, 2) POST с телом\n        и токеном. Один повтор при \'Token Validation Failed\' со свежим токеном.\n        Возвращает {\'ok\': bool, \'reason\': str|None, \'data\': dict}. (Req 4.2/4.3)"""\n        url = f"https://economy.roblox.com/v1/purchases/products/{product_id}"\n        # 1) пинг для получения токена\n        try:\n            ping = self._http().post(url, headers=self._headers())\n        except Exception as e:\n            return {"ok": False, "reason": f"csrf error: {e}", "data": {}}\n        token = ping.headers.get("x-csrf-token") or ping.headers.get("X-CSRF-Token")\n        if not token:\n            return {"ok": False, "reason": "no_csrf_token", "data": {}}\n\n        result = self._purchase(url, token, expected_price, expected_seller_id)\n        if not result["ok"] and result.get("reason") == "Token Validation Failed":\n            new_token = result.get("csrf") or token\n            result = self._purchase(url, new_token, expected_price, expected_seller_id)\n        return result\n\n    def _purchase(self, url: str, token: str, expected_price: int,\n                  expected_seller_id: Any) -> dict:\n        headers = self._headers({"X-CSRF-Token": token, "Content-Type": "application/json"})\n        body = {\n            "expectedCurrency": 1,\n            "expectedPrice": int(expected_price),\n            "expectedSellerId": expected_seller_id,\n        }\n        resp = self._http().post(url, headers=headers, json=body)\n        new_token = resp.headers.get("x-csrf-token") or resp.headers.get("X-CSRF-Token")\n        try:\n            data = resp.json()\n        except Exception:\n    '
    '        data = {}\n        if isinstance(data, dict) and data.get("purchased"):\n            return {"ok": True, "reason": None, "data": data}\n        reason = ""\n        if isinstance(data, dict):\n            reason = data.get("errorMsg") or data.get("reason") or ""\n        # ротация CSRF-токена: 403 без тела или явный текст\n        if "token validation failed" in str(reason).lower() or (resp.status_code == 403 and new_token):\n            return {"ok": False, "reason": "Token Validation Failed", "csrf": new_token, "data": data}\n        return {"ok": False, "reason": reason or f"status {resp.status_code}", "data": data}\n\n\ndef _client_for_account(account: dict) -> RobloxClient:\n    return RobloxClient(account.get("cookie", ""), account.get("proxy"))\n\n\n# =========================================================================\n# Привязка лот → цена в Robux (Lot_Mapping)\n# =========================================================================\n\ndef _match_mapping(settings: dict, lot_id: Any, lot_desc: str) -> dict | None:\n    """Cardinal-style поиск mapping: основной ключ — название лота (`lot_match`).\n    `lot_id` поддерживается только как дополнительный fallback."""\n    lid = str(lot_id or "").strip()\n    desc = _normalize_lot_text(lot_desc)\n\n    # Приоритет 1: привязка по названию/подстроке лота, как в Cardinal.\n    for m in settings.get("lot_mappings", []):\n        match_raw = str(m.get("lot_match", "")).strip()\n        match = _normalize_lot_text(match_raw)\n        if not match:\n            continue\n        if match == desc or match in desc or desc in match:\n            logger.debug(f"{LOGGER_PREFIX} mapping найден по названию lot_match=\'{match_raw}\'")\n            return m\n\n    # Приоритет 2: точный lot_id, если оператор его указал.\n    if lid:\n        for m in settings.get("lot_mappings", []):\n            mapping_lot_id = str(m.get("lot_id", "") or "").strip()\n            if mapping_lot_id and mapping_lot_id == lid:\n                logger.debug(f"{LOGGER_PREFIX} mapping найден по lot_id={lid}")\n                return m\n\n    # Legacy fallback: раньше числовой lot_match мог быть ID лота.\n    for m in settings.get("lot_mappings", []):\n        match = str(m.get("lot_match", "")).strip()\n        if match and lid and match == lid:\n            logger.debug(f"{LOGGER_PREFIX} mapping найден по legacy lot_match ID=\'{match}\'")\n            return m\n\n    logger.debug(f"{LOGGER_PREFIX} mapping НЕ найден для lot_id={lid}, desc=\'{desc[:100]}\'")\n    return None\n\n\ndef _mapping_expected_price(mapping: dict, funpay_units: int) -> int:\n    """Expected_Price для заказа по совпавшему маппингу. (Req 2.3)"""\n    return _expected_price(\n        funpay_units,\n        mapping.get("robux_per_unit", 0),\n        mapping.get("extra_robux", 0),\n    )\n\n\ndef _find_account(settings: dict, account_id: str) -> dict | None:\n    for a in settings.get("accounts", []):\n        if a.get("id") == account_id:\n            return a\n    return None\n\n\n# =========================================================================\n# Конвейер выкупа с failover по аккаунтам\n# =========================================================================\n\ndef _account_label(account: dict) -> str:\n    return account.get("label") or account.get("id") or "?"\n\n\ndef _run_buyout(settings: dict, gp_info: dict, expected_price: int,\n                client_factory=_client_for_account, start_index: int = 0) -> dict:\n    """Выкупает GamePass, перебирая аккаунты по ротации `_account_order`.\n\n    Возвращает '
    'dict со статусом:\n      - {\'status\': \'success\', \'account\': acc, \'account_index\': i, \'data\': {...}}\n      - {\'status\': \'refuse\', \'reasons\': [...]}        — self-owned GamePass\n      - {\'status\': \'refund\', \'reasons\': [...]}        — все аккаунты исчерпаны / цена изменилась\n\n    (Req 4.1–4.7)\n    """\n    accounts = settings.get("accounts", [])\n    if not accounts:\n        return {"status": "refund", "reasons": ["нет настроенных Roblox-аккаунтов"]}\n\n    product_id = gp_info.get("ProductId")\n    price = gp_info.get("PriceInRobux")\n    seller_id = gp_info.get("CreatorId")\n\n    # Req 4.7: цена GamePass изменилась между приёмкой и выкупом → возврат\n    if price != expected_price:\n        return {"status": "refund",\n                "reasons": [f"цена GamePass изменилась: {price} ≠ {expected_price}"]}\n\n    reasons: list[str] = []\n    order = _account_order(accounts, start_index)\n    for acc in order:\n        label = _account_label(acc)\n        client = client_factory(acc)\n        try:\n            who = client.whoami()\n        except Exception as e:\n            reasons.append(f"{label}: ошибка авторизации ({e})")\n            client.close()\n            continue\n        if not who or who.get("id") is None:\n            reasons.append(f"{label}: не удалось авторизоваться")\n            client.close()\n            continue\n        uid = who.get("id")\n\n        # self-owned GamePass: покупка своего пасса бессмысленна → отказ (Req 3.5)\n        if seller_id is not None and uid == seller_id:\n            client.close()\n            return {"status": "refuse",\n                    "reasons": [f"GamePass принадлежит аккаунту {label} (id {uid})"]}\n\n        try:\n            bal = client.balance(uid)\n        except Exception as e:\n            reasons.append(f"{label}: ошибка баланса ({e})")\n            client.close()\n            continue\n        if bal < expected_price:\n            reasons.append(f"{label}: недостаточно средств ({bal} < {expected_price})")\n            client.close()\n            continue\n\n        try:\n            res = client.csrf_then_purchase(product_id, price, seller_id)\n        except Exception as e:\n            reasons.append(f"{label}: ошибка выкупа ({e})")\n            client.close()\n            continue\n        client.close()\n\n        if res.get("ok"):\n            idx = accounts.index(acc) if acc in accounts else start_index\n            return {"status": "success", "account": acc, "account_index": idx,\n                    "data": res.get("data", {})}\n\n        kind = _classify_purchase_reason(res.get("reason"))\n        if kind == "insufficient_funds":\n            reasons.append(f"{label}: недостаточно средств (по ответу Roblox)")\n            continue\n        reasons.append(f"{label}: {res.get(\'reason\')}")\n        continue\n\n    return {"status": "refund", "reasons": reasons or ["не удалось выкупить GamePass"]}\n\n\n# =========================================================================\n# Состояние ожидания GamePass (в памяти)\n# =========================================================================\n\n# buyer_id(str) -> {chat_id, order_id, buyer_id, expected_price, units, sold_price, attempts, ts}\n_waiting: dict[str, dict] = {}\n_waiting_lock = threading.Lock()\n\n\n# =========================================================================\n# Уведомления и возврат\n# =========================================================================\n\ndef _send_buyer(cardinal: "Cardinal", chat_id: Any, text: str) -> None:\n    try:\n        cardinal.send_message('
    'chat_id, text)\n    except Exception:\n        logger.debug(f"{LOGGER_PREFIX} send_buyer failed", exc_info=True)\n\n\ndef _notify_operator(cardinal: "Cardinal", text: str) -> None:\n    s = _load_settings()\n    chat_id = s.get("operator_chat_id")\n    if not chat_id:\n        return\n    try:\n        cardinal.telegram.bot.send_message(chat_id, text, parse_mode="HTML")\n    except Exception:\n        logger.debug(f"{LOGGER_PREFIX} notify_operator failed", exc_info=True)\n\n\ndef _do_refund(cardinal: "Cardinal", settings: dict, order_id: Any, reason: str) -> None:\n    order_url = f"https://funpay.com/orders/{order_id}/"\n    if settings.get("auto_refund", True):\n        try:\n            cardinal.account.refund(order_id)\n            _notify_operator(cardinal, f"💸 Авто-возврат по заказу #{order_id}.\\nПричина: {reason}\\n{order_url}")\n        except Exception as e:\n            _notify_operator(cardinal, f"⚠️ Не удалось вернуть #{order_id}: {e}\\nВерните вручную: {order_url}")\n    else:\n        _notify_operator(cardinal, f"⚠️ Требуется ручной возврат #{order_id}.\\nПричина: {reason}\\n{order_url}")\n\n\ndef _buyer_chat_id(cardinal: "Cardinal", order) -> Any:\n    try:\n        return cardinal.account.get_chat_by_name(order.buyer_username).id\n    except Exception:\n        return getattr(order, "chat_id", None) or getattr(order, "buyer_id", None)\n\n\n# =========================================================================\n# Финализация заказа (запуск выкупа)\n# =========================================================================\n\ndef _finalize_buyout(cardinal: "Cardinal", settings: dict, w: dict, gp_info: dict) -> None:\n    """Запускает конвейер выкупа и обрабатывает исход. Вызывается из приёмки,\n    когда GamePass прошёл проверку цены."""\n    order_id = w["order_id"]\n    chat_id = w["chat_id"]\n    buyer_id = w["buyer_id"]\n    expected_price = int(w["expected_price"])\n    start_index = int(settings.get("last_used_index", 0) or 0)\n\n    result = _run_buyout(settings, gp_info, expected_price,\n                         _client_for_account, start_index=start_index)\n\n    if result["status"] == "success":\n        acc = result["account"]\n        data = result.get("data", {})\n        # ротация last_used_index на следующий аккаунт\n        accounts = settings.get("accounts", [])\n        if accounts:\n            settings["last_used_index"] = (result.get("account_index", 0) + 1) % len(accounts)\n            _save_settings(settings)\n        _record_order({\n            "order_id": str(order_id),\n            "buyer_id": str(buyer_id),\n            "gamepass_id": w.get("gamepass_id"),\n            "expected_price": expected_price,\n            "account_used": acc.get("id"),\n            "transaction_id": data.get("purchaseId") or data.get("transactionId") or data.get("productPurchaseId"),\n            "status": "success",\n            "sold_price": float(w.get("sold_price", 0) or 0),\n            "created_at": float(w.get("ts", time.time())),\n            "finalized_at": time.time(),\n        })\n        _send_buyer(cardinal, chat_id, settings["messages"]["success"])\n        _notify_operator(cardinal,\n                         f"✅ Заказ #{order_id}: выкуплен GamePass {w.get(\'gamepass_id\')} "\n                         f"на {expected_price} Robux (аккаунт {_account_label(acc)}).")\n    elif result["status"] == "refuse":\n        reason = "; ".join(result.get("reasons", []))\n        _send_buyer(cardinal, chat_id,\n                    "❌ Этот GamePass принадлежит нашему аккаунту — выкуп невозможен. "'
    '\n                    "Создайте GamePass на другом аккаунте.")\n        _notify_operator(cardinal,\n                         f"🚫 Заказ #{order_id}: отказ от выкупа (self-owned).\\nПричина: {reason}")\n    else:  # refund\n        reason = "; ".join(result.get("reasons", []))\n        _send_buyer(cardinal, chat_id, settings["messages"]["failure"])\n        _notify_operator(cardinal,\n                         f"❌ Заказ #{order_id}: не удалось выкупить GamePass.\\nПричины: {reason}")\n        _do_refund(cardinal, settings, order_id, reason=f"выкуп не удался: {reason}")\n\n\ndef _gamepass_info_lookup(settings: dict, gamepass_id: int) -> dict | None:\n    """Читает product-info GamePass. Использует прокси первого аккаунта, если он\n    задан (эндпоинт публичный, cookie не обязателен)."""\n    accounts = settings.get("accounts", [])\n    proxy = accounts[0].get("proxy") if accounts else None\n    client = RobloxClient(cookie="", proxy=proxy)\n    try:\n        return client.gamepass_info(gamepass_id)\n    finally:\n        client.close()\n\n\n# =========================================================================\n# Хендлеры событий FunPay\n# =========================================================================\n\ndef _extract_lot_id(cardinal: "Cardinal", order: Any) -> Any:\n    """Реальный id лота FunPay. Использует get_order_from_object (как в steam_rental)\n    с fallback на get_order и парсинг HTML. НЕ возвращает subcategory.id как lot_id."""\n    order_id = getattr(order, "id", None)\n    full_order = None\n\n    # Пробуем get_order_from_object как в steam_rental\n    try:\n        getter = getattr(cardinal, "get_order_from_object", None)\n        if callable(getter):\n            full_order = getter(order)\n    except Exception:\n        logger.debug(f"{LOGGER_PREFIX} get_order_from_object не удался", exc_info=True)\n\n    # Fallback на старый метод\n    if full_order is None:\n        try:\n            getter = getattr(getattr(cardinal, "account", None), "get_order", None)\n            if callable(getter) and order_id is not None:\n                full_order = getter(order_id)\n        except Exception:\n            logger.debug(f"{LOGGER_PREFIX} get_order для lot_id не удался", exc_info=True)\n\n    # Извлекаем lot_id из full_order\n    if full_order is not None:\n        for attr in ("lot_id", "subcategory_id"):\n            lid = getattr(full_order, attr, None)\n            if lid:\n                logger.debug(f"{LOGGER_PREFIX} lot_id из full_order.{attr} = {lid}")\n                return str(lid)\n\n        # Парсим HTML из full_order (как в steam_rental)\n        for attr in ("html", "raw_html"):\n            html = getattr(full_order, attr, "") or ""\n            for pat in (r\'data-offer="(\\d+)"\', r"offer\\?id=(\\d+)", r"offers/(\\d+)"):\n                m = re.search(pat, html)\n                if m:\n                    logger.debug(f"{LOGGER_PREFIX} lot_id из full_order.{attr} паттерн {pat}")\n                    return m.group(1)\n\n    # Fallback: парсим HTML из order\n    html = getattr(order, "html", "") or ""\n    for pat in (r\'data-offer="(\\d+)"\', r"offer\\?id=(\\d+)", r"offers/(\\d+)"):\n        m = re.search(pat, html)\n        if m:\n            logger.debug(f"{LOGGER_PREFIX} lot_id из order.html паттерн {pat}")\n            return m.group(1)\n\n    # НЕ возвращаем subcategory.id — это НЕ lot_id, а id категории\n    sub = getattr(order, "subcategory", None)\n    sub_id = getattr(sub, "id", None) if sub is not None else None\n    if sub_id:\n        logger.debug(f"{LOGGER_PREFIX} subcategory.id='
    '{sub_id} — это НЕ lot_id, пропускаем")\n    return None\n\n\ndef _on_new_order(cardinal: "Cardinal", event) -> None:\n    try:\n        order = getattr(event, "order", None)\n        if order is None:\n            return\n        order_id = getattr(order, "id", None)\n        buyer_id = getattr(order, "buyer_id", None)\n        lot_desc = getattr(order, "description", "") or getattr(order, "title", "") or ""\n        amount = getattr(order, "amount", 1) or 1\n        price = getattr(order, "price", 0) or 0\n        lot_id = _extract_lot_id(cardinal, order)\n\n        settings = _load_settings()\n        mapping = _match_mapping(settings, lot_id, lot_desc)\n        if not mapping:\n            logger.info(\n                f"{LOGGER_PREFIX} Заказ #{order_id} игнорирован: лот не найден в конфиге. "\n                f"lot_id={lot_id}, desc=\'{lot_desc[:120]}\'. "\n                f"Добавьте привязку через Telegram → 🗺 Лоты ↔ Robux."\n            )\n            _notify_operator(\n                cardinal,\n                f"⚠️ <b>AutoRobux</b>: заказ #{order_id} — лот не найден в конфиге.\\n"\n                f"lot_id=<code>{lot_id or \'—\'}</code>\\n"\n                f"desc=<code>{_html_escape(lot_desc[:100])}</code>\\n\\n"\n                f"Добавьте привязку в 🗺 Лоты ↔ Robux."\n            )\n            return  # Req 2.4: не совпало — игнорируем\n\n        if not settings.get("accounts"):\n            _notify_operator(cardinal,\n                             f"⚠️ Заказ #{order_id}: нет настроенных Roblox-аккаунтов, "\n                             f"выкуп невозможен. Добавьте аккаунт в настройках.")\n            return  # Req 1.5\n\n        expected_price = _mapping_expected_price(mapping, int(amount))\n        chat_id = _buyer_chat_id(cardinal, order)\n        with _waiting_lock:\n            _waiting[str(buyer_id)] = {\n                "chat_id": chat_id,\n                "order_id": order_id,\n                "buyer_id": buyer_id,\n                "expected_price": expected_price,\n                "units": int(amount),\n                "sold_price": float(price),\n                "attempts": 0,\n                "gamepass_id": None,\n                "ts": time.time(),\n            }\n        text = settings["messages"]["ask_gamepass"].format(expected_price=expected_price)\n        _send_buyer(cardinal, chat_id, text)\n    except Exception:\n        logger.warning(f"{LOGGER_PREFIX} on_new_order error", exc_info=True)\n\n\ndef _on_new_message(cardinal: "Cardinal", event) -> None:\n    try:\n        msg = getattr(event, "message", None)\n        if msg is None:\n            return\n        text = (getattr(msg, "text", "") or "").strip()\n        if not text:\n            return\n        author_id = getattr(msg, "author_id", None)\n        chat_id = getattr(msg, "chat_id", None)\n        my_id = getattr(cardinal.account, "id", None)\n        if author_id is not None and author_id == my_id:\n            return\n\n        with _waiting_lock:\n            w = _waiting.get(str(author_id))\n        if not w:\n            return\n\n        settings = _load_settings()\n        gid = _extract_gamepass_id(text)\n        if gid is None:\n            return  # ждём корректный id/ссылку\n\n        info = _gamepass_info_lookup(settings, gid)\n        if info is None or info.get("PriceInRobux") is None:\n            _send_buyer(cardinal, w["chat_id"],\n                        "⚠️ Не удалось проверить GamePass. Убедитесь, что ID/ссылка корректны, и пришлите снова.")\n            return\n\n        expected_price = int(w["expected_price"])\n        act'
    'ual = int(info["PriceInRobux"])\n        if actual != expected_price:\n            # Req 3.4: неверная цена → просим пересоздать, до max_attempts попыток\n            with _waiting_lock:\n                w["attempts"] = int(w.get("attempts", 0)) + 1\n                attempts = w["attempts"]\n                _waiting[str(author_id)] = w\n            max_attempts = int(settings.get("max_attempts", 5))\n            if attempts >= max_attempts:\n                with _waiting_lock:\n                    _waiting.pop(str(author_id), None)\n                _send_buyer(cardinal, w["chat_id"], settings["messages"]["failure"])\n                _do_refund(cardinal, settings, w["order_id"],\n                           reason=f"превышено число попыток ({max_attempts}) с неверной ценой GamePass")\n            else:\n                _send_buyer(cardinal, w["chat_id"],\n                            settings["messages"]["wrong_price"].format(actual=actual, expected=expected_price))\n            return\n\n        # цена совпала → выкуп\n        with _waiting_lock:\n            w["gamepass_id"] = gid\n            _waiting.pop(str(author_id), None)\n        _finalize_buyout(cardinal, settings, w, info)\n    except Exception:\n        logger.warning(f"{LOGGER_PREFIX} on_new_message error", exc_info=True)\n\n\n# =========================================================================\n# Фоновый цикл таймаутов ожидания GamePass\n# =========================================================================\n\n_timeout_thread: threading.Thread | None = None\n_stop = threading.Event()\n\n\ndef _timeout_loop(cardinal: "Cardinal") -> None:\n    while not _stop.is_set():\n        waited = 0.0\n        while waited < 60 and not _stop.is_set():\n            time.sleep(5)\n            waited += 5\n        if _stop.is_set():\n            break\n        try:\n            settings = _load_settings()\n            timeout = float(settings.get("wait_timeout_sec", 86400))\n            now = time.time()\n            expired = []\n            with _waiting_lock:\n                for bid, w in list(_waiting.items()):\n                    if now - float(w.get("ts", now)) >= timeout:\n                        expired.append(w)\n                        _waiting.pop(bid, None)\n            for w in expired:\n                _send_buyer(cardinal, w["chat_id"],\n                            "⌛ Время на отправку GamePass истекло. Средства возвращены.")\n                _do_refund(cardinal, settings, w["order_id"], reason="таймаут ожидания GamePass")\n        except Exception:\n            logger.debug(f"{LOGGER_PREFIX} timeout loop error", exc_info=True)\n\n\ndef _ensure_timeout_thread(cardinal: "Cardinal") -> None:\n    global _timeout_thread\n    if _timeout_thread and _timeout_thread.is_alive():\n        return\n    _stop.clear()\n    _timeout_thread = threading.Thread(target=_timeout_loop, args=(cardinal,),\n                                       name="autorobux-timeout", daemon=True)\n    _timeout_thread.start()\n\n\n# =========================================================================\n# Telegram-меню оператора (RU)\n# =========================================================================\n\nCBP = "autorobux"\nCBT_HOME = f"{CBP}:home"\nCBT_ADVANCED = f"{CBP}:advanced"\nCBT_HELP = f"{CBP}:help"\nCBT_TOGGLE_REFUND = f"{CBP}:toggle_refund"\nCBT_EDIT_TIMEOUT = f"{CBP}:edit_timeout"\nCBT_EDIT_ATTEMPTS = f"{CBP}:edit_attempts"\nCBT_ACCOUNTS = f"{CBP}:accounts"\nCBT_ACCOUNT_ADD = f"{CBP}:aadd"\nCBT_ACCOUNT_VIEW = f"{CBP}:aview"        # + :account_id\nCBT_ACCOUNT_DE'
    'L = f"{CBP}:adel"          # + :account_id\nCBT_ACCOUNT_BAL = f"{CBP}:abal"          # + :account_id\nCBT_ACCOUNT_EDIT_LABEL = f"{CBP}:aelabel"  # + :account_id\nCBT_ACCOUNT_EDIT_COOKIE = f"{CBP}:aecookie"  # + :account_id\nCBT_ACCOUNT_EDIT_PROXY = f"{CBP}:aeproxy"  # + :account_id\nCBT_BALANCES = f"{CBP}:balances"\nCBT_MAPPINGS = f"{CBP}:mappings"\nCBT_MAPPING_ADD = f"{CBP}:madd"\nCBT_MAPPING_DEL = f"{CBP}:mdel"          # + :mapping_id\nCBT_MSGS = f"{CBP}:msgs"\nCBT_MSG_EDIT = f"{CBP}:msgedit"          # + :slot\nCBT_ORDERS = f"{CBP}:orders"\nCBT_STATS = f"{CBP}:stats"\n\n_SLOT_LABELS = {\n    "ask_gamepass": "Запрос GamePass",\n    "wrong_price": "Неверная цена",\n    "success": "Успех",\n    "failure": "Провал",\n}\n\n\ndef _new_id(prefix: str, existing: list[dict]) -> str:\n    nums = []\n    for e in existing:\n        eid = str(e.get("id", ""))\n        if eid.startswith(prefix):\n            try:\n                nums.append(int(eid[len(prefix):]))\n            except Exception:\n                pass\n    return f"{prefix}{(max(nums) + 1) if nums else 1}"\n\n\ndef _fmt_duration(seconds: Any) -> str:\n    """Человекочитаемая длительность."""\n    try:\n        sec = int(seconds)\n    except Exception:\n        return f"{seconds}"\n    if sec and sec % 86400 == 0:\n        return f"{sec // 86400} дн"\n    if sec and sec % 3600 == 0:\n        return f"{sec // 3600} ч"\n    if sec and sec % 60 == 0:\n        return f"{sec // 60} мин"\n    return f"{sec} сек"\n\n\ndef _onoff(flag: Any) -> str:\n    return "🟢 вкл" if flag else "🔴 выкл"\n\n\ndef _home_text() -> str:\n    s = _load_settings()\n    accounts = s.get("accounts", [])\n    mappings = s.get("lot_mappings", [])\n    hints = []\n    if not accounts:\n        hints.append("• добавьте аккаунт Roblox (🔑 Аккаунты)")\n    if not mappings:\n        hints.append("• привяжите лот к Robux (🗺 Лоты ↔ Robux)")\n    hint_block = ("\\n<b>⚠️ Чтобы заработало:</b>\\n" + "\\n".join(hints)) if hints else \\\n        "\\n✅ Всё готово к работе."\n    return (\n        f"<b>💎 AutoRobux v{VERSION}</b> — авто-выдача Robux через GamePass\\n"\n        f"━━━━━━━━━━━━━━━━━━\\n"\n        f"🔑 Аккаунты Roblox: <code>{len(accounts)}</code>     "\n        f"🗺 Привязки лотов: <code>{len(mappings)}</code>\\n"\n        f"💸 Авто-возврат: {_onoff(s.get(\'auto_refund\'))}\\n"\n        f"\\n"\n        f"<i>Доп. настройки (⚙️ Ещё):</i>\\n"\n        f"⏱ Таймаут ожидания GamePass: {_fmt_duration(s.get(\'wait_timeout_sec\', 86400))}\\n"\n        f"🔁 Макс. попыток цены: {int(s.get(\'max_attempts\', 5))}\\n"\n        f"{hint_block}"\n    )\n\n\ndef _home_kb() -> "K":\n    s = _load_settings()\n    kb = K(row_width=2)\n    kb.row(B("🔑 Аккаунты Roblox", callback_data=CBT_ACCOUNTS),\n           B("🗺 Лоты ↔ Robux", callback_data=CBT_MAPPINGS))\n    kb.row(B(("💸 Авто-возврат: 🟢" if s.get("auto_refund") else "💸 Авто-возврат: 🔴"),\n             callback_data=CBT_TOGGLE_REFUND),\n           B("💰 Балансы аккаунтов", callback_data=CBT_BALANCES))\n    kb.row(B("📦 Последние заказы", callback_data=CBT_ORDERS),\n           B("📊 Статистика", callback_data=CBT_STATS))\n    kb.row(B("⚙️ Ещё настройки", callback_data=CBT_ADVANCED),\n           B("❓ Как настроить", callback_data=CBT_HELP))\n    return kb\n\n\ndef _advanced_text() -> str:\n    s = _load_settings()\n    return (\n        f"<b>⚙️ Дополнительные настройки</b>\\n"\n        f"━━━━━━━━━━━━━━━━━━\\n"\n        f"⏱ Таймаут ожидания GamePass: <code>{_fmt_duration(s.get(\'wait_timeout_sec\', 86400))}</code>\\n"\n        f"🔁 Макс. попыток цены: <code>{int(s.get(\'max_attempts\', 5))}</code>"\n    )\n\n\ndef _adva'
    'nced_kb() -> "K":\n    kb = K(row_width=2)\n    kb.row(B("📜 Шаблоны сообщений", callback_data=CBT_MSGS))\n    kb.row(B("⏱ Таймаут GamePass", callback_data=CBT_EDIT_TIMEOUT),\n           B("🔁 Макс. попыток", callback_data=CBT_EDIT_ATTEMPTS))\n    kb.row(B("◀️ Назад", callback_data=CBT_HOME))\n    return kb\n\n\ndef _help_text() -> str:\n    return (\n        "<b>❓ Как настроить AutoRobux за 4 шага</b>\\n"\n        "━━━━━━━━━━━━━━━━━━\\n"\n        "1️⃣ <b>Аккаунт Roblox.</b> «🔑 Аккаунты Roblox» → добавьте аккаунт "\n        "(cookie .ROBLOSECURITY, при необходимости прокси). «💰 Балансы» покажет, "\n        "что аккаунт рабочий и сколько на нём Robux.\\n\\n"\n        "2️⃣ <b>Привязка лота.</b> «🗺 Лоты ↔ Robux» → добавьте привязку: "\n        "<b>название лота точно как на FunPay</b> (или часть) и сколько Robux выдавать.\\n\\n"\n        "3️⃣ <b>Проверка.</b> Купите свой лот — бот попросит у покупателя ссылку на "\n        "GamePass, выкупит её и выдаст Robux.\\n\\n"\n        "4️⃣ <b>По желанию (⚙️ Ещё настройки):</b> тексты сообщений, таймаут "\n        "ожидания GamePass, число попыток подбора цены."\n    )\n\n\ndef _stats_text() -> str:\n    orders = _load_orders()\n    now = time.time()\n    day = _aggregate_stats(orders, now - 86400)\n    week = _aggregate_stats(orders, now - 7 * 86400)\n    month = _aggregate_stats(orders, now - 30 * 86400)\n\n    def fmt(a):\n        return f"{a[\'count\']} шт · {a[\'robux\']} Robux · доход {a[\'revenue\']}"\n\n    return (\n        f"<b>📊 Статистика AutoRobux</b>\\n"\n        f"━━━━━━━━━━━━━━━━━━\\n"\n        f"📅 День: {fmt(day)}\\n"\n        f"🗓 Неделя: {fmt(week)}\\n"\n        f"📆 Месяц: {fmt(month)}"\n    )\n\n\ndef init(cardinal: "Cardinal", *args) -> None:\n    if not getattr(cardinal, "telegram", None):\n        return\n    tg = cardinal.telegram\n    bot = tg.bot\n\n    def _answer(call, text: str | None = None) -> None:\n        try:\n            bot.answer_callback_query(call.id, text or "")\n        except Exception:\n            pass\n\n    def _persist_op(chat_id: int) -> None:\n        s = _load_settings()\n        if not s.get("operator_chat_id"):\n            s["operator_chat_id"] = chat_id\n            _save_settings(s)\n\n    def _edit_home(call) -> None:\n        try:\n            bot.edit_message_text(_home_text(), call.message.chat.id, call.message.id,\n                                  parse_mode="HTML", reply_markup=_home_kb())\n        except Exception:\n            bot.send_message(call.message.chat.id, _home_text(), parse_mode="HTML", reply_markup=_home_kb())\n\n    def _edit_or_send(call, text: str, kb) -> None:\n        try:\n            bot.edit_message_text(text, call.message.chat.id, call.message.id, parse_mode="HTML", reply_markup=kb)\n        except Exception:\n            bot.send_message(call.message.chat.id, text, parse_mode="HTML", reply_markup=kb)\n\n    def _edit_advanced(call) -> None:\n        _edit_or_send(call, _advanced_text(), _advanced_kb())\n\n    def open_settings_cb(call) -> None:\n        _persist_op(call.message.chat.id)\n        _edit_home(call)\n        _answer(call)\n\n    def home_cb(call) -> None:\n        _edit_home(call)\n        _answer(call)\n\n    def advanced_cb(call) -> None:\n        _edit_advanced(call)\n        _answer(call)\n\n    def help_cb(call) -> None:\n        kb = K().add(B("◀️ Назад", callback_data=CBT_HOME))\n        _edit_or_send(call, _help_text(), kb)\n        _answer(call)\n\n    def toggle_refund_cb(call) -> None:\n        s = _load_settings()\n        s["auto_refund"] = not s.get("auto_refund", True)\n        _save_settings(s)\n        '
    '_answer(call, "💸 " + ("вкл" if s["auto_refund"] else "выкл"))\n        _edit_home(call)\n\n    def _make_numeric_editor(key: str, lo: int, hi: int, label: str):\n        def cb(call) -> None:\n            msg = bot.send_message(call.message.chat.id, f"{label}\\nВведите число от {lo} до {hi}:")\n            _answer(call)\n\n            def handle(m) -> None:\n                try:\n                    v = int((m.text or "").strip())\n                    if not lo <= v <= hi:\n                        raise ValueError\n                except Exception:\n                    bot.reply_to(m, f"❌ Значение вне диапазона ({lo}–{hi}). Прежнее значение сохранено.")\n                    return\n                s = _load_settings()\n                s[key] = v\n                _save_settings(s)\n                bot.reply_to(m, f"✅ Обновлено: <code>{v}</code>", parse_mode="HTML")\n            bot.register_next_step_handler(msg, handle)\n        return cb\n\n    # ---------- аккаунты Roblox ----------\n    def accounts_cb(call) -> None:\n        s = _load_settings()\n        lines = ["<b>🔑 Аккаунты Roblox</b>", ""]\n        kb = K(row_width=1)\n        for a in s.get("accounts", []):\n            lines.append(f"• <b>{a.get(\'label\') or a.get(\'id\')}</b> — cookie: <code>{_mask_secret(a.get(\'cookie\'))}</code>"\n                         + (f"\\n  прокси: <code>{a.get(\'proxy\')}</code>" if a.get("proxy") else ""))\n            kb.add(B(f"⚙️ {a.get(\'label\') or a.get(\'id\')}", callback_data=f"{CBT_ACCOUNT_VIEW}:{a.get(\'id\')}"))\n        if not s.get("accounts"):\n            lines.append("(пусто)")\n        kb.add(B("➕ Добавить аккаунт", callback_data=CBT_ACCOUNT_ADD))\n        kb.add(B("◀️ Назад", callback_data=CBT_HOME))\n        _edit_or_send(call, "\\n".join(lines), kb)\n        _answer(call)\n\n    def account_view_cb(call) -> None:\n        aid = call.data.split(":", 2)[-1]\n        s = _load_settings()\n        a = _find_account(s, aid)\n        if not a:\n            _answer(call, "не найден")\n            return accounts_cb(call)\n        text = (f"<b>⚙️ {a.get(\'label\') or a.get(\'id\')}</b>\\n"\n                f"Cookie: <code>{_mask_secret(a.get(\'cookie\'))}</code>\\n"\n                f"Прокси: <code>{a.get(\'proxy\') or \'—\'}</code>")\n        kb = K(row_width=2)\n        kb.row(B("✏️ Метка", callback_data=f"{CBT_ACCOUNT_EDIT_LABEL}:{aid}"),\n               B("🍪 Cookie", callback_data=f"{CBT_ACCOUNT_EDIT_COOKIE}:{aid}"))\n        kb.row(B("🌐 Прокси", callback_data=f"{CBT_ACCOUNT_EDIT_PROXY}:{aid}"),\n               B("💰 Ник + баланс", callback_data=f"{CBT_ACCOUNT_BAL}:{aid}"))\n        kb.row(B("🗑 Удалить", callback_data=f"{CBT_ACCOUNT_DEL}:{aid}"))\n        kb.row(B("◀️ Назад", callback_data=CBT_ACCOUNTS))\n        _edit_or_send(call, text, kb)\n        _answer(call)\n\n    def account_add_cb(call) -> None:\n        msg = bot.send_message(call.message.chat.id,\n                               "➕ Пришлите cookie .ROBLOSECURITY (можно с префиксом «.ROBLOSECURITY=» или без):")\n        _answer(call)\n\n        def step_cookie(m):\n            cookie = (m.text or "").strip()\n            if not (1 <= len(cookie) <= 4096):\n                return bot.reply_to(m, "❌ Cookie должен быть длиной 1–4096 символов. Отменено.")\n            s = _load_settings()\n            aid = _new_id("a", s.get("accounts", []))\n            s.setdefault("accounts", []).append({\n                "id": aid, "label": aid, "cookie": cookie, "proxy": None,\n            })\n            _save_settings(s)\n            bot.reply_to(m, f"✅ Аккаунт «{aid}» добавлен. Cook'
    'ie: <code>{_mask_secret(cookie)}</code>",\n                         parse_mode="HTML")\n\n        bot.register_next_step_handler(msg, step_cookie)\n\n    def _account_field_editor(field: str, label: str, maxlen: int, allow_empty=False):\n        def cb(call) -> None:\n            aid = call.data.split(":", 2)[-1]\n            msg = bot.send_message(call.message.chat.id, label)\n            _answer(call)\n\n            def handle(m):\n                val = (m.text or "").strip()[:maxlen]\n                if not val and not allow_empty:\n                    return bot.reply_to(m, "❌ Пусто. Прежнее значение сохранено.")\n                s = _load_settings()\n                a = _find_account(s, aid)\n                if not a:\n                    return bot.reply_to(m, "❌ Аккаунт не найден.")\n                a[field] = (val or None) if (allow_empty and not val) else val\n                _save_settings(s)\n                shown = _mask_secret(val) if field == "cookie" else (val or "—")\n                bot.reply_to(m, f"✅ Обновлено: <code>{shown}</code>", parse_mode="HTML")\n\n            bot.register_next_step_handler(msg, handle)\n        return cb\n\n    def account_del_cb(call) -> None:\n        aid = call.data.split(":", 2)[-1]\n        s = _load_settings()\n        s["accounts"] = [a for a in s.get("accounts", []) if a.get("id") != aid]\n        _save_settings(s)\n        _answer(call, "🗑 Удалён")\n        accounts_cb(call)\n\n    def account_balance_cb(call) -> None:\n        aid = call.data.split(":", 2)[-1]\n        s = _load_settings()\n        a = _find_account(s, aid)\n        _answer(call, "⏳")\n        if not a:\n            bot.send_message(call.message.chat.id, "❌ Аккаунт не найден.")\n            return\n        client = _client_for_account(a)\n        try:\n            who = client.whoami()\n            if not who or who.get("id") is None:\n                bot.send_message(call.message.chat.id, "❌ Не удалось авторизоваться (проверьте cookie).")\n                return\n            bal = client.balance(who.get("id"))\n            bot.send_message(call.message.chat.id,\n                             f"👤 {who.get(\'name\')} (id {who.get(\'id\')})\\n💰 Баланс: {bal} Robux")\n        except Exception as e:\n            bot.send_message(call.message.chat.id, f"❌ Ошибка: {e}")\n        finally:\n            client.close()\n\n    def balances_cb(call) -> None:\n        s = _load_settings()\n        _answer(call, "⏳")\n        lines = ["<b>💰 Балансы аккаунтов</b>", ""]\n        if not s.get("accounts"):\n            lines.append("(нет аккаунтов)")\n        for a in s.get("accounts", []):\n            client = _client_for_account(a)\n            try:\n                who = client.whoami()\n                if who and who.get("id") is not None:\n                    bal = client.balance(who.get("id"))\n                    lines.append(f"• {who.get(\'name\')}: {bal} Robux")\n                else:\n                    lines.append(f"• {a.get(\'label\') or a.get(\'id\')}: ошибка авторизации")\n            except Exception:\n                lines.append(f"• {a.get(\'label\') or a.get(\'id\')}: ошибка")\n            finally:\n                client.close()\n        kb = K().add(B("◀️ Назад", callback_data=CBT_HOME))\n        _edit_or_send(call, "\\n".join(lines), kb)\n\n    # ---------- лоты ↔ Robux ----------\n    def mappings_cb(call) -> None:\n        s = _load_settings()\n        lines = ["<b>🗺 Лоты ↔ цена в Robux</b>", ""]\n        kb = K(row_width=1)\n        for m in s.get("lot_mappings", []):\n            lot_id = m.get("lot_id")\n  '
    '          match = m.get("lot_match")\n            label = f"\'{match}\'" + (f" / ID:{lot_id}" if lot_id else "")\n            lines.append(f"• <code>{label}</code> → "\n                         f"{m.get(\'robux_per_unit\')}×кол-во + {m.get(\'extra_robux\', 0)} Robux")\n            kb.add(B(f"🗑 Удалить {label}", callback_data=f"{CBT_MAPPING_DEL}:{m.get(\'id\')}"))\n        if not s.get("lot_mappings"):\n            lines.append("(пусто)")\n        kb.add(B("➕ Добавить привязку", callback_data=CBT_MAPPING_ADD))\n        kb.add(B("◀️ Назад", callback_data=CBT_HOME))\n        _edit_or_send(call, "\\n".join(lines), kb)\n        _answer(call)\n\n    def mapping_add_cb(call) -> None:\n        msg = bot.send_message(call.message.chat.id,\n                               "➕ Шаг 1/4: Введите название лота или уникальную подстроку названия.\\n"\n                               "Например: Roblox, Робуксы, Game Pass")\n        _answer(call)\n\n        def step_match(m):\n            match = (m.text or "").strip()\n            if not match:\n                return bot.reply_to(m, "❌ Пусто. Отменено.")\n            msg2 = bot.reply_to(m, "💎 Шаг 2/4: Введите количество Robux за 1 единицу заказа (число):")\n\n            def step_per_unit(m2):\n                try:\n                    per_unit = int((m2.text or "").strip())\n                    if per_unit < 0:\n                        raise ValueError\n                except Exception:\n                    return bot.reply_to(m2, "❌ Не число. Отменено.")\n                msg3 = bot.reply_to(m2, "➕ Шаг 3/4: Введите фиксированную добавку Robux (extra, можно 0):")\n\n                def step_extra(m3):\n                    try:\n                        extra = int((m3.text or "").strip())\n                        if extra < 0:\n                            raise ValueError\n                    except Exception:\n                        return bot.reply_to(m3, "❌ Не число. Отменено.")\n                    msg4 = bot.reply_to(m3, "🔢 Шаг 4/4: Введите ID лота FunPay, если знаете. Если нет — отправьте «-»:")\n\n                    def step_lot_id(m4):\n                        lot_id_raw = (m4.text or "").strip()\n                        lot_id = lot_id_raw if lot_id_raw.isdigit() else None\n                        s = _load_settings()\n                        s["lot_mappings"].append({\n                            "id": _new_id("m", s["lot_mappings"]),\n                            "lot_match": match,\n                            "lot_id": lot_id,\n                            "robux_per_unit": per_unit,\n                            "extra_robux": extra,\n                        })\n                        _save_settings(s)\n                        suffix = f" / ID:{lot_id}" if lot_id else ""\n                        bot.reply_to(m4, f"✅ Привязка добавлена: \'{match}\'{suffix} → {per_unit}×кол-во + {extra} Robux")\n\n                    bot.register_next_step_handler(msg4, step_lot_id)\n\n                bot.register_next_step_handler(msg3, step_extra)\n\n            bot.register_next_step_handler(msg2, step_per_unit)\n\n        bot.register_next_step_handler(msg, step_match)\n\n    def mapping_del_cb(call) -> None:\n        mid = call.data.split(":", 2)[-1]\n        s = _load_settings()\n        s["lot_mappings"] = [m for m in s.get("lot_mappings", []) if m.get("id") != mid]\n        _save_settings(s)\n        _answer(call, "🗑 Удалено")\n        mappings_cb(call)\n\n    # ---------- шаблоны сообщений ----------\n    def msgs_cb(call) -> None:\n        kb = K(row_width=1)\n        for slot, label in '
    '_SLOT_LABELS.items():\n            kb.add(B(f"📜 {label}", callback_data=f"{CBT_MSG_EDIT}:{slot}"))\n        kb.add(B("◀️ Назад", callback_data=CBT_HOME))\n        _edit_or_send(call, "<b>📜 Шаблоны сообщений</b>\\nВыберите шаблон для редактирования:", kb)\n        _answer(call)\n\n    def msg_edit_cb(call) -> None:\n        slot = call.data.split(":", 2)[-1]\n        s = _load_settings()\n        current = s["messages"].get(slot, "")\n        hint = ""\n        if slot == "ask_gamepass":\n            hint = "\\nДоступен плейсхолдер {expected_price}."\n        elif slot == "wrong_price":\n            hint = "\\nДоступны плейсхолдеры {actual} и {expected}."\n        msg = bot.send_message(call.message.chat.id,\n                               f"📜 Текущий «{_SLOT_LABELS.get(slot, slot)}»:\\n{current}\\n\\n"\n                               f"Пришлите новый текст.{hint}")\n        _answer(call)\n\n        def handle(m):\n            text = (m.text or "").strip()\n            if not text:\n                return bot.reply_to(m, "❌ Пусто. Прежний шаблон сохранён.")\n            # валидация плейсхолдеров: формат не должен падать\n            try:\n                if slot == "ask_gamepass":\n                    text.format(expected_price=0)\n                elif slot == "wrong_price":\n                    text.format(actual=0, expected=0)\n            except Exception:\n                return bot.reply_to(m, "❌ Некорректные плейсхолдеры. Прежний шаблон сохранён.")\n            s2 = _load_settings()\n            s2["messages"][slot] = text\n            _save_settings(s2)\n            bot.reply_to(m, "✅ Шаблон обновлён.")\n\n        bot.register_next_step_handler(msg, handle)\n\n    # ---------- заказы / статистика ----------\n    def orders_cb(call) -> None:\n        orders = _load_orders()\n        lines = ["<b>📦 Последние заказы</b>", ""]\n        for o in orders[-15:][::-1]:\n            lines.append(f"• #{o.get(\'order_id\')} → GamePass {o.get(\'gamepass_id\')} "\n                         f"({o.get(\'expected_price\')} Robux, {o.get(\'status\')}, акк {o.get(\'account_used\')})")\n        if not orders:\n            lines.append("(пусто)")\n        kb = K().add(B("◀️ Назад", callback_data=CBT_HOME))\n        _edit_or_send(call, "\\n".join(lines), kb)\n        _answer(call)\n\n    def stats_cb(call) -> None:\n        kb = K().add(B("◀️ Назад", callback_data=CBT_HOME))\n        _edit_or_send(call, _stats_text(), kb)\n        _answer(call)\n\n    # --- регистрация ---\n    tg.cbq_handler(open_settings_cb, lambda c: f"{CBT.PLUGIN_SETTINGS}:{UUID}" in (c.data or ""))\n    tg.cbq_handler(home_cb, lambda c: c.data == CBT_HOME)\n    tg.cbq_handler(advanced_cb, lambda c: c.data == CBT_ADVANCED)\n    tg.cbq_handler(help_cb, lambda c: c.data == CBT_HELP)\n    tg.cbq_handler(toggle_refund_cb, lambda c: c.data == CBT_TOGGLE_REFUND)\n    tg.cbq_handler(_make_numeric_editor("wait_timeout_sec", *RANGE_WAIT_TIMEOUT, "⏱ Таймаут ожидания GamePass (сек)."),\n                   lambda c: c.data == CBT_EDIT_TIMEOUT)\n    tg.cbq_handler(_make_numeric_editor("max_attempts", *RANGE_MAX_ATTEMPTS, "🔁 Макс. попыток с неверной ценой."),\n                   lambda c: c.data == CBT_EDIT_ATTEMPTS)\n    tg.cbq_handler(accounts_cb, lambda c: c.data == CBT_ACCOUNTS)\n    tg.cbq_handler(account_view_cb, lambda c: (c.data or "").startswith(f"{CBT_ACCOUNT_VIEW}:"))\n    tg.cbq_handler(account_add_cb, lambda c: c.data == CBT_ACCOUNT_ADD)\n    tg.cbq_handler(account_del_cb, lambda c: (c.data or "").startswith(f"{CBT_ACCOUNT_DEL}:"))\n    tg.cbq_handler(account_balance_cb'
    ', lambda c: (c.data or "").startswith(f"{CBT_ACCOUNT_BAL}:"))\n    tg.cbq_handler(_account_field_editor("label", "✏️ Введите новую метку:", 64),\n                   lambda c: (c.data or "").startswith(f"{CBT_ACCOUNT_EDIT_LABEL}:"))\n    tg.cbq_handler(_account_field_editor("cookie", "🍪 Пришлите новый cookie .ROBLOSECURITY:", 4096),\n                   lambda c: (c.data or "").startswith(f"{CBT_ACCOUNT_EDIT_COOKIE}:"))\n    tg.cbq_handler(_account_field_editor("proxy", "🌐 Введите прокси (или «-» чтобы убрать):", 255, allow_empty=True),\n                   lambda c: (c.data or "").startswith(f"{CBT_ACCOUNT_EDIT_PROXY}:"))\n    tg.cbq_handler(balances_cb, lambda c: c.data == CBT_BALANCES)\n    tg.cbq_handler(mappings_cb, lambda c: c.data == CBT_MAPPINGS)\n    tg.cbq_handler(mapping_add_cb, lambda c: c.data == CBT_MAPPING_ADD)\n    tg.cbq_handler(mapping_del_cb, lambda c: (c.data or "").startswith(f"{CBT_MAPPING_DEL}:"))\n    tg.cbq_handler(msgs_cb, lambda c: c.data == CBT_MSGS)\n    tg.cbq_handler(msg_edit_cb, lambda c: (c.data or "").startswith(f"{CBT_MSG_EDIT}:"))\n    tg.cbq_handler(orders_cb, lambda c: c.data == CBT_ORDERS)\n    tg.cbq_handler(stats_cb, lambda c: c.data == CBT_STATS)\n\n    # --- слэш-команда открытия меню настроек ---\n    def cmd_open(m):\n        _persist_op(m.chat.id)\n        bot.send_message(m.chat.id, _home_text(), reply_markup=_home_kb(), parse_mode="HTML")\n    try:\n        tg.msg_handler(cmd_open, commands=["autorobux"])\n        cardinal.add_telegram_commands(UUID, [\n            ("autorobux", "AutoRobux: открыть меню", True),\n        ])\n    except Exception:\n        logger.exception("add_telegram_commands failed")\n\n    _ensure_timeout_thread(cardinal)\n    logger.info(f"{LOGGER_PREFIX} v{VERSION} запущен")\n\n\ndef _on_delete(cardinal: "Cardinal", *args) -> None:\n    _stop.set()\n\n\nBIND_TO_PRE_INIT = [init]\nBIND_TO_NEW_ORDER = [_on_new_order]\nBIND_TO_NEW_MESSAGE = [_on_new_message]\nBIND_TO_DELETE = _on_delete\n'
)
_EMBEDDED_GAMEPASS_MODULE = None


def _load_embedded_gamepass():
    global _EMBEDDED_GAMEPASS_MODULE
    if _EMBEDDED_GAMEPASS_MODULE is not None:
        return _EMBEDDED_GAMEPASS_MODULE
    import types as _types
    mod = _types.ModuleType("autorobux_gamepass_embedded")
    mod.__file__ = __file__ + "#embedded-gamepass"
    exec(_EMBEDDED_GAMEPASS_SOURCE, mod.__dict__)
    try:
        mod.PLUGIN_DIR = Path("storage/plugins/autorobuxdrake/gamepass")
        mod.SETTINGS_PATH = mod.PLUGIN_DIR / "settings.json"
        mod.ORDERS_PATH = mod.PLUGIN_DIR / "orders.json"
        mod.PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
        for old_dir in (Path("storage/plugins/autorobux_unified/gamepass"), Path("storage/plugins/autorobux")):
            old_settings = old_dir / "settings.json"
            old_orders = old_dir / "orders.json"
            if not mod.SETTINGS_PATH.exists() and old_settings.exists():
                mod.SETTINGS_PATH.write_bytes(old_settings.read_bytes())
            if not mod.ORDERS_PATH.exists() and old_orders.exists():
                mod.ORDERS_PATH.write_bytes(old_orders.read_bytes())
    except Exception:
        pass
    try:
        gp_ask_gamepass = (
            "💎 Спасибо за оплату!\n"
            "Создайте GamePass на ровно {expected_price} Robux и пришлите ссылку/ID сюда.\n\n"
            "Перед продажей Roblox может попросить заполнить Experience Rating / Questionnaire для плейса. "
            "Если кнопка продажи GamePass недоступна — сначала откройте настройки плейса, заполните рейтинг/возрастные вопросы, "
            "сохраните и только потом выставляйте GamePass на продажу.\n\n"
            "Проверяю цену автоматически. Если цена другая — подскажу правильную."
        )
        if isinstance(getattr(mod, "DEFAULT_MESSAGES", None), dict):
            mod.DEFAULT_MESSAGES["ask_gamepass"] = gp_ask_gamepass
        original_load_settings = mod._load_settings

        def _load_settings_with_template_migration():
            settings = original_load_settings()
            try:
                msgs = settings.setdefault("messages", {})
                current = str(msgs.get("ask_gamepass", ""))
                if "Experience Rating" not in current and "Создайте GamePass" in current:
                    msgs["ask_gamepass"] = gp_ask_gamepass
            except Exception:
                pass
            return settings

        mod._load_settings = _load_settings_with_template_migration
    except Exception:
        pass
    try:
        def _expected_price_with_roblox_fee(funpay_units, robux_per_unit, extra_robux=0):
            net = int(funpay_units) * int(robux_per_unit) + int(extra_robux)
            return _gamepass_price_for_net_robux(net)
        mod._expected_price = _expected_price_with_roblox_fee
    except Exception:
        pass
    try:
        original_mapping_expected_price = mod._mapping_expected_price

        def _mapping_expected_price_with_fixed_robux(mapping, funpay_units):
            fixed = mapping.get("fixed_robux")
            if fixed is not None:
                net = int(fixed or 0) * int(funpay_units or 1)
                return _gamepass_price_for_net_robux(net)
            return original_mapping_expected_price(mapping, funpay_units)

        mod._mapping_expected_price = _mapping_expected_price_with_fixed_robux
    except Exception:
        pass
    try:
        original_match_mapping = mod._match_mapping

        def _match_mapping_with_single_lot_fallback(settings, lot_id, lot_desc):
            found = original_match_mapping(settings, lot_id, lot_desc)
            if found:
                return found
            auto = _auto_mapping_from_desc(settings, lot_desc)
            if auto:
                logger.info("%s GamePass: auto-mapping из описания '%s' → fixed_robux=%s",
                            LOGGER_PREFIX, lot_desc[:120], auto.get("fixed_robux"))
                return auto
            if str(lot_id or "").strip():
                return None
            mappings = settings.get("lot_mappings", []) or []
            desc = _normalize_lot_text(lot_desc)
            looks_like_robux_order = bool(re.search(r"\d{1,6}\s*(?:ед\.?\s*)?(?:робукс|robux|rbx)", desc, re.I))
            if len(mappings) == 1 and looks_like_robux_order:
                logger.info("%s GamePass: lot_id пустой, использую единственную привязку по описанию '%s'", LOGGER_PREFIX, desc[:120])
                return mappings[0]
            return None

        mod._match_mapping = _match_mapping_with_single_lot_fallback
    except Exception:
        pass
    _EMBEDDED_GAMEPASS_MODULE = mod
    return mod


def _autorobuxdrake_init(cardinal: "Cardinal", *args) -> None:
    init(cardinal, *args)
    try:
        gp = _load_embedded_gamepass()
        gp.init(cardinal, *args)
        logger.info("%s embedded GamePass init ok", LOGGER_PREFIX)
    except Exception:
        logger.exception("%s embedded GamePass init failed", LOGGER_PREFIX)


def _autorobuxdrake_on_new_order(cardinal: "Cardinal", event) -> None:
    try:
        _on_new_order(cardinal, event)
    except Exception:
        logger.exception("%s RBXCrate order handler failed", LOGGER_PREFIX)
    try:
        if _load_settings().get("gamepass_enabled", True):
            _load_embedded_gamepass()._on_new_order(cardinal, event)
    except Exception:
        logger.exception("%s GamePass order handler failed", LOGGER_PREFIX)


def _autorobuxdrake_on_new_message(cardinal: "Cardinal", event) -> None:
    try:
        _on_new_message(cardinal, event)
    except Exception:
        logger.exception("%s RBXCrate message handler failed", LOGGER_PREFIX)
    try:
        if _load_settings().get("gamepass_enabled", True):
            _load_embedded_gamepass()._on_new_message(cardinal, event)
    except Exception:
        logger.exception("%s GamePass message handler failed", LOGGER_PREFIX)


def _autorobuxdrake_on_delete(cardinal: "Cardinal", *args) -> None:
    try:
        _on_delete(cardinal, *args)
    finally:
        try:
            _load_embedded_gamepass()._on_delete(cardinal, *args)
        except Exception:
            pass


BIND_TO_PRE_INIT = [_autorobuxdrake_init]
BIND_TO_NEW_ORDER = [_autorobuxdrake_on_new_order]
BIND_TO_NEW_MESSAGE = [_autorobuxdrake_on_new_message]
BIND_TO_DELETE = _autorobuxdrake_on_delete


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
