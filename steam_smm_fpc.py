from __future__ import annotations

import copy
import json
import logging
import os
import random
import re
import threading
import time
import secrets
from urllib.parse import urlparse
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
DONATION_CALLBACK_PREFIX = "ssm_dn"    # префикс колбэков кнопок баннера
DONATION_PLUGIN_NAME = "Steam SMM"  # имя плагина в шапке баннера
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
            if now.hour == DONATION_DAILY_HOUR and now.minute == 0:
                if _donation_claim_today():
                    _send_donation_banner(cardinal)
        except Exception:
            pass
        time.sleep(60)



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


NAME = "Steam SMM"
VERSION = "1.1.0"
DESCRIPTION = (
    "Продажа накрутки Steam (комментарии, лайки, подписчики, обзоры, похвала CS2) "
    "и авторегов на полном автопилоте. Несколько SMM-провайдеров с редактируемыми "
    "API-ключами через Telegram-меню, привязка лот→услуга, проверка ссылок, "
    "прибыль-гейт перед каждым заказом, авто-возврат при провале, контроль "
    "баланса, команда !статус для покупателя, редактируемые тексты."
)
CREDITS = "@drakelovc"
UUID = "7a1c9d2f-4b8e-4c3a-9f61-2e8b5a0d7c41"
SETTINGS_PAGE = True

logger = logging.getLogger(f"FPC.{__name__}")
LOGGER_PREFIX = "[STEAMSMM]"


# =========================================================================
# Хранилище и дефолты
# =========================================================================

PLUGIN_DIR = Path("storage/plugins/steam_smm")
SETTINGS_PATH = PLUGIN_DIR / "settings.json"
ORDERS_PATH = PLUGIN_DIR / "orders.json"
ACCOUNTS_PATH = PLUGIN_DIR / "accounts.json"
ACTIVE_ORDERS_PATH = PLUGIN_DIR / "active_orders.json"
# журнал действий плагина (кнопка «📜 Логи плагина» в настройках)
ACTIONS_LOG_PATH = PLUGIN_DIR / "actions.log"
ACTIONS_LOG_MAX_BYTES = 2 * 1024 * 1024   # 2 MiB
ACTIONS_LOG_BACKUPS = 5                    # actions.log.1 … actions.log.5

RANGE_LINK_TIMEOUT = (600, 7 * 86400)
RANGE_POLL_INTERVAL = (20, 3600)
RANGE_MIN_PROFIT = (-1000.0, 100000.0)
RANGE_BALANCE_ALERT = (0.0, 1000000.0)

_SUCCESS_STATUSES = {"completed", "done", "success", "partial"}
_FAILURE_STATUSES = {"failed", "error", "canceled", "cancelled"}

DEFAULT_LINK_DOMAINS = [
    "t.me", "vk.com", "instagram.com", "tiktok.com", "youtube.com",
    "youtu.be", "twitch.tv", "twitter.com", "x.com", "steamcommunity.com",
    "steampowered.com", "vt.tiktok.com", "vm.tiktok.com",
]

# Каталог услуг с предустановленными объёмами для меню «🎯 Услуги».
# Объём = количество единиц услуги на 1 заказ лота (qty_multiplier привязки).
SERVICE_PRESETS: dict[str, dict] = {
    "comment":        {"name": "💬 Комментарии +rep",       "volumes": [1, 10, 50, 100, 200, 300, 500, 1000, 2000]},
    "comment_rep":    {"name": "💬 Комментарии -rep",       "volumes": [1, 10, 50, 100, 200, 300, 500, 1000, 2000]},
    "comment_random": {"name": "💬 Случайные комментарии",  "volumes": [1, 10, 50, 100, 200, 300, 500, 1000, 2000]},
    "comment_premium": {"name": "⭐ Премиум-комментарии",   "volumes": [1, 10, 50, 100, 200, 300, 500, 1000, 2000]},
    "like":           {"name": "👍 Лайки",                 "volumes": [1, 10, 50, 100, 200, 300, 500, 1000, 2000]},
    "dis":            {"name": "👎 Дизлайки",              "volumes": [1, 10, 50, 100, 200, 300, 500, 1000, 2000]},
    "subscribe":      {"name": "👥 Участники группы",      "volumes": [1, 10, 50, 100, 200, 300, 500, 1000, 2000]},
    "review":         {"name": "📝 Лайки на обзоры",       "volumes": [1, 10, 50, 100, 200, 300, 500, 1000, 2000]},
    "commend_cs2":    {"name": "🎖️ Похвала CS2",            "volumes": [1, 15, 30, 50, 100, 150, 300]},
}

# ── Пресеты описаний лотов для автосоздания (по образцу реальных лотов FunPay) ──
# title — «Краткое описание» (название лота), desc — «Подробное описание».
LOT_PRESETS: dict[str, dict] = {
    "comment": {
        "title": "🖤Steam КОММЕНТАРИИ🖤[+REP]🖤ЦЕНА ЗА 1🖤АВТОНАКРУТКА🖤",
        "title_en": "🖤Steam COMMENTS🖤[+REP]🖤PRICE PER 1🖤AUTO BOOST🖤",
        "desc_ru": "Вы получите положительные комментарии +REP к профилю Steam.\nСсылка: полный URL профиля Steam.\nMIN: 10\nУсловия: профиль без ограничения Steam $5; профили отправителей могут повторяться, к +REP может добавляться защитный текст.\nПри задержке напишите продавцу в чат заказа.",
        "desc_en": "You will receive positive +REP comments on your Steam profile.\nLink: the full Steam profile URL.\nMIN: 10\nRequirements: the profile must not have the Steam $5 restriction; sender profiles may repeat and safety text may be added to +REP.\nIf delayed, message the seller in the order chat.",
    },
    "comment_rep": {
        "title": "🖤Steam КОММЕНТАРИИ🖤[-REP]🖤ЦЕНА ЗА 1🖤АВТОНАКРУТКА🖤",
        "title_en": "🖤Steam COMMENTS🖤[-REP]🖤PRICE PER 1🖤AUTO BOOST🖤",
        "desc_ru": "Вы получите негативные комментарии -REP к профилю Steam.\nСсылка: полный URL профиля Steam.\nMIN: 10\nУсловия: профиль без ограничения Steam $5; профили отправителей могут повторяться, к -REP может добавляться защитный текст.\nПри задержке напишите продавцу в чат заказа.",
        "desc_en": "You will receive negative -REP comments on your Steam profile.\nLink: the full Steam profile URL.\nMIN: 10\nRequirements: the profile must not have the Steam $5 restriction; sender profiles may repeat and safety text may be added to -REP.\nIf delayed, message the seller in the order chat.",
    },
    "comment_random": {
        "title": "🖤Steam СЛУЧАЙНЫЕ КОММЕНТАРИИ🖤[REP]🖤ЦЕНА ЗА 1🖤АВТОНАКРУТКА🖤",
        "title_en": "🖤Steam RANDOM COMMENTS🖤[REP]🖤PRICE PER 1🖤AUTO BOOST🖤",
        "desc_ru": "Вы получите случайные положительные, нейтральные или негативные комментарии к профилю Steam.\nСсылка: полный URL профиля Steam.\nMIN: 10\nУсловия: профиль без ограничения Steam $5; текст выбрать нельзя, профили отправителей могут повторяться.\nПри задержке напишите продавцу в чат заказа.",
        "desc_en": "You will receive random positive, neutral, or negative comments on your Steam profile.\nLink: the full Steam profile URL.\nMIN: 10\nRequirements: the profile must not have the Steam $5 restriction; text cannot be selected and sender profiles may repeat.\nIf delayed, message the seller in the order chat.",
    },
    "comment_premium": {
        "title": "⭐Steam PREMIUM КОММЕНТАРИИ⭐[+REP]⭐ЦЕНА ЗА 1⭐АВТОНАКРУТКА⭐",
        "title_en": "⭐Steam PREMIUM COMMENTS⭐[+REP]⭐PRICE PER 1⭐AUTO BOOST⭐",
        "desc_ru": "Вы получите +REP комментарии с высокоуровневых аккаунтов без повторов.\nСсылка: полный URL профиля Steam.\nMIN: 10\nУсловия: профиль без ограничения Steam $5; желаемый вариант текста и задержку укажите в чате заказа.\nПри задержке сверх выбранного времени напишите продавцу.",
        "desc_en": "You will receive +REP comments from high-level accounts without repeats.\nLink: the full Steam profile URL.\nMIN: 10\nRequirements: the profile must not have the Steam $5 restriction; specify the preferred text option and delay in the order chat.\nIf it exceeds the selected delay, message the seller.",
    },
    "like": {
        "title": "👍Steam ЛАЙКИ👍ЦЕНА ЗА 1👍АВТОНАКРУТКА👍",
        "title_en": "👍Steam LIKES👍PRICE PER 1👍AUTO BOOST👍",
        "desc_ru": "Вы получите лайки на указанную страницу Steam.\nСсылка: полный URL профиля или страницы игры Steam.\nMIN: 10\nПрофиль или страница должны быть открыты; профили отправителей могут повторяться.\nПри задержке напишите продавцу в чат заказа.",
        "desc_en": "You will receive likes on the specified Steam page.\nLink: the full Steam profile or game-page URL.\nMIN: 10\nThe profile or page must be public; sender profiles may repeat.\nIf delayed, message the seller in the order chat.",
    },
    "dis": {
        "title": "👎Steam ДИЗЛАЙКИ👎ЦЕНА ЗА 1👎АВТОНАКРУТКА👎",
        "title_en": "👎Steam DISLIKES👎PRICE PER 1👎AUTO BOOST👎",
        "desc_ru": "Вы получите дизлайки на указанную страницу Steam.\nСсылка: полный URL профиля, игры или обзора Steam.\nMIN: 10\nСтраница должна быть открыта; профили отправителей могут повторяться.\nПри задержке напишите продавцу в чат заказа.",
        "desc_en": "You will receive dislikes on the specified Steam page.\nLink: the full Steam profile, game, or review URL.\nMIN: 10\nThe page must be public; sender profiles may repeat.\nIf delayed, message the seller in the order chat.",
    },
    "subscribe": {
        "title": "👥Steam УЧАСТНИКИ ГРУППЫ👥ЦЕНА ЗА 1👥АВТОНАКРУТКА👥",
        "title_en": "👥Steam GROUP MEMBERS👥PRICE PER 1👥AUTO BOOST👥",
        "desc_ru": "Вы получите новых участников группы Steam.\nСсылка: полный URL группы Steam.\nMIN: 10\nГруппа обязательно должна быть открытой; аккаунты участников могут повторяться.\nПри задержке напишите продавцу в чат заказа.",
        "desc_en": "You will receive new members for your Steam group.\nLink: the full Steam group URL.\nMIN: 10\nThe group must be public; member accounts may repeat.\nIf delayed, message the seller in the order chat.",
    },
    "review": {
        "title": "📝Steam ЛАЙКИ НА ОБЗОР📝ЦЕНА ЗА 1📝АВТОНАКРУТКА📝",
        "title_en": "📝Steam REVIEW LIKES📝PRICE PER 1📝AUTO BOOST📝",
        "desc_ru": "Вы получите лайки на выбранный обзор Steam.\nСсылка: полный URL конкретного обзора Steam.\nMIN: 10\nОбзор должен быть доступен всем; профили отправителей могут повторяться.\nПри задержке напишите продавцу в чат заказа.",
        "desc_en": "You will receive likes on the selected Steam review.\nLink: the full URL of the specific Steam review.\nMIN: 10\nThe review must be public; sender profiles may repeat.\nIf delayed, message the seller in the order chat.",
    },
    "commend_cs2": {
        "title": "🎖️CS2 ПОХВАЛА🎖️[FRIENDLY/TEACHER/LEADER]🎖️ЦЕНА ЗА 1🎖️АВТОНАКРУТКА🎖️",
        "title_en": "🎖️CS2 COMMENDS🎖️[FRIENDLY/TEACHER/LEADER]🎖️PRICE PER 1🎖️AUTO BOOST🎖️",
        "desc_ru": "Вы получите похвалы CS2 Friendly/Teacher/Leader для одного профиля.\nСсылка: полный URL профиля Steam.\nMIN: 15\nВо время выполнения игрок должен находиться на указанном продавцом сервере. Если заказ задержался, проверьте сервер и напишите продавцу.",
        "desc_en": "You will receive CS2 Friendly/Teacher/Leader commends for one profile.\nLink: the full Steam profile URL.\nMIN: 15\nDuring delivery, the player must be on the server specified by the seller. If delayed, check the server and message the seller.",
    },
}

# Сообщение покупателю после оплаты (обязательное поле формы FunPay — «Заполните все поля»).
DEFAULT_PAYMENT_MSG_RU = (
    "Спасибо за покупку! 🎁 После оплаты: 1) откройте чат заказа; "
    "2) отправьте одну полную ссылку https:// на нужный профиль, группу, игру или обзор Steam; "
    "3) если бот попросит подтверждение — проверьте ссылку и подтвердите её; "
    "4) дождитесь сообщения о запуске и завершении. Статус: !статус. "
    "Для похвалы CS2 игрок должен находиться на указанном сервере. "
    "Если ссылка отклонена или заказ не запустился — напишите продавцу."
)
DEFAULT_PAYMENT_MSG_EN = (
    "Thank you for your purchase! 🎁 After payment: 1) open the order chat; "
    "2) send one complete https:// link to the target Steam profile, group, game, or review; "
    "3) if asked, verify and confirm the link; 4) wait for start and completion messages. "
    "Use !status to check progress. For CS2 commends, the player must be on the configured server. "
    "If the link is rejected or the order does not start, message the seller."
)

DEFAULT_SETTINGS: dict[str, Any] = {
    "providers": [],
    "lot_mappings": [],
    "sales_enabled": True,
    "maintenance_mode": False,
    "maintenance_message": "Сервис временно на обслуживании. Попробуйте позже.",
    "allowed_link_domains": list(DEFAULT_LINK_DOMAINS),
    "confirm_link": True,
    "auto_refund": True,
    "min_profit": 0.0,
    "profit_guard": True,
    "balance_alert_enabled": True,
    "balance_alert_threshold": 50.0,
    "auto_pause_low_balance": True,
    "auto_pause_active": False,
    "auto_pause_grace_hours": 24.0,
    "auto_pause_grace_until": 0.0,
    "link_wait_timeout_sec": 1200,
    "status_poll_interval_sec": 120,
    "balance_check_interval_min": 10,
    "max_backoff_sec": 3600,
    "new_order_notifications": False,
    # Команды оператора в чате FunPay: !чек <ID> — статус, рефилл <ID> — долив
    # (по умолчанию выкл: рефилл тратит средства — включать осознанно)
    "operator_commands": False,
    "auto_lots_enabled": False,
    "auto_lots_interval_min": 30,
    "auto_lots_markup_percent": 30.0,
    # Подкатегория FunPay, куда «авто» создаёт новые лоты (базовая — Steam, 1009; 0 — выкл)
    "auto_lot_node_id": 1009,
    # Базовый объём авто-лота: 1 шт — цена за единицу, покупатель сам умножает
    # количество на кассе FunPay (order.amount → сколько единиц выдастся)
    "auto_lot_base_volume": 1,
    "qty_rounding": True,
    "prices_recalc_interval_min": 60,
    "auto_raise_enabled": False,
    "auto_raise_interval_min": 60,
    "balance_gate": True,
    "funpay_fee_percent": 7.5,
    "api_retries": 2,
    "provider_balance_precheck": True,
    "stuck_threshold_sec": 1200,
    "auto_cancel_stuck_enabled": True,
    "auto_cancel_stuck_sec": 2700,
    "lot_desc_match_limit": 200,
    "price_check_enabled": False,
    "commend_server": "185.9.145.248:24673",
    "commend_max_restarts": 2,
    "operator_chat_id": None,
    "blacklist": [],
    "messages": {
        "after_payment": [
            "❤️ Спасибо за оплату! Пришлите ссылку на цель накрутки (https://...).",
            "✅ Заказ оплачен. Пришлите ссылку на аккаунт/страницу (https://...).",
        ],
        "after_confirmation": [
            "✅ Заказ создан. ID у поставщика: {provider_order_id}\n🔗 Статус: !статус",
            "🚀 Заказ запущен в работу. ID: {provider_order_id}\nСтатус — команда !статус",
        ],
        "success": [
            "🎉 Заказ выполнен! ID: {provider_order_id}\nПожалуйста, подтвердите заказ на FunPay.",
            "✅ Готово! ID: {provider_order_id}. Подтвердите заказ на FunPay.",
        ],
        "failure": [
            "❌ Заказ не выполнен. Средства возвращены.",
            "❌ К сожалению, заказ не был выполнен. Деньги вернулись на ваш баланс FunPay.",
        ],
        "account_issue": [
            "❌ Аккаунт не выдан. Средства будут возвращены.",
            "❌ Ошибка выдачи. Деньги вернутся на ваш баланс FunPay.",
        ],
    },
    # базовая реф-ссылка Team SMM (steamsmm.ru) — всегда в списке первой;
    # оператор добавляет свои ссылки поверх неё
    "links_menu": [
        {"label": "🌐 Team SMM (реф)", "url": "https://steamsmm.ru/register?ref=uPt4oCkV"},
    ],
}

_MESSAGE_SLOTS = ("after_payment", "after_confirmation", "success", "failure", "account_issue")

_io_lock = threading.RLock()
_rng = random.Random()

# Кеш настроек/авторегов в памяти: инвалидится при сохранении (см. _save_settings/_save_accounts).
_settings_cache: dict | None = None
_accounts_cache: list | None = None

# chat_id -> message_id последнего отрисованного меню (чтобы /steamsmm правил уже открытое).
_menu_msg_ids: dict[int, int] = {}

# chat_id -> одноразовое состояние тройного подтверждения удаления своих лотов.
_delete_all_states: dict[int, dict[str, Any]] = {}
_DELETE_ALL_STATE_TTL = 10 * 60


class RateLimited(Exception):
    """Поставщик ответил 429 — нужен backoff."""


class CommendOffline(Exception):
    """Игрок не на сервере — похвала CS2 невозможна (нужно зайти на сервер)."""


class PersistenceError(RuntimeError):
    """Критическое состояние не удалось надёжно сохранить на диск."""


_PROTECTIVE_DEFAULTS: dict[str, Any] = {
    "provider_balance_precheck": True,
    "auto_lot_base_volume": 1,
    "stuck_threshold_sec": 1200,
    "auto_cancel_stuck_enabled": True,
    "auto_cancel_stuck_sec": 2700,
}


def _normalize_protective_settings(settings: dict) -> dict:
    """Сохраняет защитные ключи старых настроек, не меняя значения пользователя."""
    for key, value in _PROTECTIVE_DEFAULTS.items():
        settings.setdefault(key, value)
    return settings


def _choose_autoreg_mode(mapping: dict) -> tuple[str, Any, Any]:
    """Выбирает покупку у поставщика либо выдачу из локального пула."""
    provider_id = mapping.get("provider_id")
    category_id = mapping.get("autoreg_category_id")
    if provider_id and category_id:
        return "provider", provider_id, category_id
    return "local_pool", None, None


def _legacy_multi_ids(record: dict) -> list[Any]:
    """Читает старый durable-формат списка provider_order_ids."""
    ids = record.get("provider_order_ids") or []
    return list(ids) if isinstance(ids, (list, tuple)) else []


def _legacy_task_interval(settings: dict, name: str) -> float | None:
    """Разрешает исторические интервалы фоновых задач без миграции формата."""
    keys = {
        "autolots": ("auto_lots_interval_min", 30, 60.0),
        "prices_recalc": ("prices_recalc_interval_min", 60, 60.0),
        "raise": ("auto_raise_interval_min", 60, 60.0),
        "balance": ("balance_check_interval_min", 10, 60.0),
    }
    spec = keys.get(name)
    if spec is None:
        return None
    key, default, multiplier = spec
    try:
        return float(settings.get(key, default) or default) * multiplier
    except (TypeError, ValueError):
        return float(default) * multiplier


# =========================================================================
# Чистые утилиты
# =========================================================================

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def _is_valid_link(text: str, allowed: list[str], mapping: dict | None = None) -> str | None:
    m = _URL_RE.search(text or "")
    if not m:
        return None
    url = m.group(0).rstrip(".,;!?)]}>")
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
    except Exception:
        return None
    if not host:
        return None

    service_id = str((mapping or {}).get("service_id") or "")
    is_steam = service_id in SERVICE_PRESETS
    if is_steam:
        if parsed.scheme.lower() != "https" or parsed.username or parsed.password or parsed.port is not None:
            return None
        path = parsed.path or "/"
        if host == "steamcommunity.com" or host.endswith(".steamcommunity.com"):
            valid_prefixes = ("/id/", "/profiles/", "/groups/", "/sharedfiles/", "/app/")
            if not path.startswith(valid_prefixes):
                return None
        elif host == "store.steampowered.com":
            if not path.startswith(("/app/", "/recommended/")):
                return None
        else:
            return None
        return url

    for d in allowed:
        d = (d or "").strip().lower().lstrip(".")
        if d and (host == d or host.endswith("." + d)):
            return url
    return None


def _is_url_https(url: Any) -> bool:
    return isinstance(url, str) and url.strip().lower().startswith("https://")


def _mask_secret(s: str | None, head: int = 4, tail: int = 2) -> str:
    s = (s or "").strip()
    if not s:
        return "—"
    if len(s) <= head + tail:
        return "*" * len(s)
    return f"{s[:head]}{'*' * (len(s) - head - tail)}{s[-tail:]}"


def _html_escape(s: Any) -> str:
    return (
        str(s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _exp_backoff(attempt: int, base: float = 1.0, cap: float = 3600.0) -> float:
    return min(cap, base * (2 ** attempt))


def _normalize_lot_text(text: Any) -> str:
    """Нормализация названия лота для привязки по имени (как в autorobux_fpc):
    срезка ZWJ-символов (\u200c/\u200d), casefold, схлопывание пробелов."""
    s = str(text or "").replace("\u200c", "").replace("\u200d", "")
    return " ".join(s.casefold().split())


def _classify_provider_status(status: str, remains: int | None) -> str:
    s = (status or "").strip().lower()
    if any(tok in s for tok in _SUCCESS_STATUSES):
        return "success"
    if any(tok in s for tok in _FAILURE_STATUSES):
        return "failure"
    if s in ("inprogress", "processing", "pending", "in_progress", "active", ""):
        return "in_progress"
    return "in_progress"


def _aggregate_profit(records: list[dict], since_ts: float) -> dict:
    count = 0
    revenue = 0.0
    profit = 0.0
    fees = 0.0
    for o in records:
        created = float(o.get("created_at", 0) or 0)
        if created < since_ts:
            continue
        if o.get("status") not in ("success", "failure"):
            continue
        count += 1
        revenue += float(o.get("sold_price", 0) or 0)
        profit += float(o.get("profit", 0) or 0)
        fees += float(o.get("funpay_fee", 0) or 0)
    return {"count": count, "revenue": round(revenue, 2), "profit": round(profit, 2),
            "fees": round(fees, 2)}


def _pick_variant(variants: list[str]) -> str:
    if not variants:
        return ""
    return _rng.choice(variants)


def _fmt_ts(ts: Any) -> str:
    try:
        return time.strftime("%d.%m.%Y %H:%M", time.localtime(float(ts)))
    except Exception:
        return "—"


# =========================================================================
# JSON-хранилище
# =========================================================================

def _ensure_dir() -> None:
    try:
        PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


# =========================================================================
# Журнал действий плагина (actions.log) + просмотр через кнопку «📜 Логи»
# =========================================================================

def _rotate_actions_log() -> None:
    """Ротация actions.log: при превышении лимита сдвигаем .1 → .2 … .N."""
    try:
        if not ACTIONS_LOG_PATH.exists() \
                or ACTIONS_LOG_PATH.stat().st_size < ACTIONS_LOG_MAX_BYTES:
            return
        for i in range(ACTIONS_LOG_BACKUPS, 0, -1):
            dst = Path(f"{ACTIONS_LOG_PATH}.{i}")
            src = Path(f"{ACTIONS_LOG_PATH}.{i - 1}") if i > 1 else ACTIONS_LOG_PATH
            if src.exists():
                if dst.exists():
                    dst.unlink()
                src.rename(dst)
    except Exception:
        logger.debug(f"{LOGGER_PREFIX} ротация actions.log не удалась", exc_info=True)


_SECRET_KEY_RE = re.compile(r"(?i)(api[_-]?key|golden[_-]?key|token|secret|password)")
_SECRET_VALUE_RE = re.compile(r"(?i)(api[_-]?key|golden[_-]?key|token|secret|password)=([^\s|&]+)")


def _audit_value(value: Any) -> str:
    """Безопасное значение аудита: без секретов, query и полных URL."""
    sv = str(value)
    sv = _SECRET_VALUE_RE.sub(r"\1=[REDACTED]", sv)
    sv = re.sub(r"https?://([^/\s]+)(?:/[^\s|]*)?", r"https://\1/…", sv)
    return sv[:117] + "…" if len(sv) > 120 else sv


def _log_action(action: str, text: str = "", *, raw: dict | None = None, **fields: Any) -> None:
    """Пишет безопасную строку аудита в actions.log.

    raw — поля, которые НЕ обрезаются и НЕ сокращаются (нужны для полной
    диагностики, например полный текст ошибки FunPay с «Текст ответа:»).
    Секреты (ключи/токены/пароли) в raw-полях всё равно маскируются.
    """
    try:
        _ensure_dir()
        parts = [f"[{action}]"]
        if text:
            parts.append(_audit_value(text))
        for k, v in fields.items():
            if v is None or v == "":
                continue
            if _SECRET_KEY_RE.search(str(k)):
                sv = "[REDACTED]"
            else:
                sv = _audit_value(v)
            parts.append(f"{k}={sv}")
        for k, v in (raw or {}).items():
            if v is None or v == "":
                continue
            if _SECRET_KEY_RE.search(str(k)):
                sv = "[REDACTED]"
            else:
                sv = _SECRET_VALUE_RE.sub(r"\1=[REDACTED]", str(v))
            parts.append(f"{k}={sv}")
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} | " + " | ".join(parts) + "\n"
        with _io_lock:
            _rotate_actions_log()
            with open(ACTIONS_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        logger.debug(f"{LOGGER_PREFIX} запись actions.log не удалась", exc_info=True)


def _read_actions_log() -> str:
    """Содержимое actions.log для кнопки «📜 Логи плагина»."""
    try:
        if not ACTIONS_LOG_PATH.exists():
            return "Логи отсутствуют"
        with _io_lock, open(ACTIONS_LOG_PATH, "r", encoding="utf-8") as f:
            return f.read().strip() or "Логи отсутствуют"
    except Exception:
        return "Логи отсутствуют"


def _read_actions_log_chunks() -> list[str]:
    """Строки лога для выгрузки файлом: текущий actions.log + бэкапы ротации."""
    lines: list[str] = []
    try:
        with _io_lock:
            for i in range(ACTIONS_LOG_BACKUPS, -1, -1):
                p = ACTIONS_LOG_PATH if i == 0 else Path(f"{ACTIONS_LOG_PATH}.{i}")
                if not p.exists():
                    continue
                with open(p, "r", encoding="utf-8") as f:
                    part = f.read().strip()
                if part:
                    lines.append(f"----- actions.log.{i} -----")
                    lines.append(part)
    except Exception:
        logger.debug(f"{LOGGER_PREFIX} чтение actions.log для выгрузки не удалось",
                     exc_info=True)
    return lines


def _clear_actions_log() -> None:
    """Удаляет actions.log и бэкапы (кнопка «🧹 Очистить»)."""
    try:
        with _io_lock:
            for i in range(0, ACTIONS_LOG_BACKUPS + 1):
                p = ACTIONS_LOG_PATH if i == 0 else Path(f"{ACTIONS_LOG_PATH}.{i}")
                if p.exists():
                    p.unlink()
    except Exception:
        logger.debug(f"{LOGGER_PREFIX} очистка actions.log не удалась", exc_info=True)


def _load_json(path: Path, default):
    _ensure_dir()
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        logger.warning(f"{LOGGER_PREFIX} не смог прочитать {path}", exc_info=True)
    return default


def _save_json(path: Path, data) -> None:
    _ensure_dir()
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception as e:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            logger.debug(f"{LOGGER_PREFIX} не смог удалить временный файл {tmp}", exc_info=True)
        logger.warning(f"{LOGGER_PREFIX} не смог сохранить {path}", exc_info=True)
        raise PersistenceError(f"не удалось сохранить {path}") from e


def _load_settings() -> dict[str, Any]:
    global _settings_cache
    with _io_lock:
        cached = _settings_cache
    if cached is not None:
        return cached
    s = _load_json(SETTINGS_PATH, None)
    if not isinstance(s, dict):
        s = {}
    # ГЛУБОКАЯ копия дефолтов: иначе append в lot_mappings/blacklist/домены
    # мутирует общий DEFAULT_SETTINGS и «протекает» между загрузками.
    merged = copy.deepcopy(DEFAULT_SETTINGS)
    merged.update(s)
    _normalize_protective_settings(merged)
    for slot in _MESSAGE_SLOTS:
        merged.setdefault("messages", {}).setdefault(slot, list(DEFAULT_SETTINGS["messages"].get(slot, [])))
    # Legacy global threshold remains the migration/fallback source, while every
    # provider owns its independent gate configuration and durable state.
    legacy_threshold = float(merged.get("balance_alert_threshold", 50.0) or 0)
    for provider in merged.get("providers", []):
        provider.setdefault("min_balance", legacy_threshold)
        # Legacy providers are migrated only after the API explicitly reports a
        # currency. Until then the provider gate remains fail-closed.
        provider.setdefault("low_balance_pause_enabled", True)
        provider.setdefault("balance_snapshot", {"amount": None, "currency": None,
                                                  "fetched_at": None, "error": "unknown"})
        provider.setdefault("balance_hold", None)
    # базовая реф-ссылка Team SMM: если в конфиге ссылок нет (или ключа нет вовсе)
    # — подсеваем дефолт, чтобы «Полезные ссылки» не были пустыми
    if not isinstance(merged.get("links_menu"), list) or not merged["links_menu"]:
        merged["links_menu"] = copy.deepcopy(DEFAULT_SETTINGS["links_menu"])
    with _io_lock:
        _settings_cache = merged
    return merged


def _save_settings(s: dict[str, Any]) -> None:
    global _settings_cache
    _save_json(SETTINGS_PATH, s)
    _settings_cache = s


def _load_orders() -> list[dict]:
    data = _load_json(ORDERS_PATH, [])
    return data if isinstance(data, list) else []


def _save_orders(orders: list[dict]) -> None:
    _save_json(ORDERS_PATH, orders)


def _load_active() -> dict:
    data = _load_json(ACTIVE_ORDERS_PATH, {})
    if not isinstance(data, dict):
        return {}
    # v2: buyer -> list. Legacy buyer -> object is migrated on read.
    migrated = False
    for buyer, value in list(data.items()):
        if isinstance(value, dict):
            data[buyer] = [value]
            migrated = True
        elif not isinstance(value, list):
            data[buyer] = []
            migrated = True
    if migrated:
        _save_active(data)
    return data


def _save_active(d: dict) -> None:
    _save_json(ACTIVE_ORDERS_PATH, d)


def _load_accounts() -> list[dict]:
    global _accounts_cache
    with _io_lock:
        cached = _accounts_cache
    if cached is not None:
        return cached
    data = _load_json(ACCOUNTS_PATH, [])
    data = data if isinstance(data, list) else []
    with _io_lock:
        _accounts_cache = data
    return data


def _save_accounts(accounts: list[dict]) -> None:
    global _accounts_cache
    _save_json(ACCOUNTS_PATH, accounts)
    _accounts_cache = accounts


def _build_backup() -> dict:
    return {
        "version": 1,
        "settings": _load_settings(),
        "accounts": _load_accounts(),
        "active_orders": _load_active(),
        "orders": _load_orders(),
    }


def _restore_backup(data: dict) -> str:
    settings = data.get("settings")
    accounts = data.get("accounts")
    if not isinstance(settings, dict) or not isinstance(accounts, list):
        raise ValueError("некорректный формат бэкапа")
    _save_settings(dict(settings))
    _save_accounts(list(accounts))
    if isinstance(data.get("active_orders"), dict):
        _save_active(data["active_orders"])
    if isinstance(data.get("orders"), list):
        _save_orders(data["orders"])
    return f"✅ Восстановлено: {len(settings.get('providers', []))} поставщиков, " \
           f"{len(settings.get('lot_mappings', []))} привязок, {len(accounts)} авторегов"


def get_buyer_active_orders(buyer_id: Any) -> list[dict]:
    return list(_load_active().get(str(buyer_id)) or [])


def set_buyer_active_order(buyer_id: Any, entry: dict) -> None:
    with _io_lock:
        d = _load_active()
        items = list(d.get(str(buyer_id)) or [])
        entry = dict(entry)
        if not entry.get("order_id_funpay") and entry.get("order_id") is not None:
            entry["order_id_funpay"] = str(entry.get("order_id"))
        oid = str(entry.get("order_id_funpay") or "")
        items = [x for x in items if str(x.get("order_id_funpay") or "") != oid]
        items.append(entry)
        d[str(buyer_id)] = items
        _save_active(d)


def get_buyer_active_order(buyer_id: Any) -> dict | None:
    items = get_buyer_active_orders(buyer_id)
    return items[-1] if items else None


def remove_buyer_active_order(buyer_id: Any, order_id: Any = None) -> None:
    with _io_lock:
        d = _load_active()
        key = str(buyer_id)
        if order_id is None:
            d.pop(key, None)
        else:
            items = [x for x in d.get(key, [])
                     if str(x.get("order_id_funpay")) != str(order_id).strip(" #№")]
            if items:
                d[key] = items
            else:
                d.pop(key, None)
        _save_active(d)


# =========================================================================
# Чёрный список покупателей
# =========================================================================

def _bl_key(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().lower()


def _add_blacklist(settings: dict, buyer_id: Any = None, username: Any = None,
                   reason: str = "вручную") -> bool:
    """Добавляет покупателя без дублей, сохраняя структурированный формат.

    Старые конфиги могли содержать строки с username; они поддерживаются при
    проверке, удалении и импорте, но новые записи сохраняются как объекты.
    """
    bid = _bl_key(buyer_id)
    uname = _bl_key(username)
    if not bid and not uname:
        return False
    if _is_blacklisted(settings, buyer_id=buyer_id, username=username):
        return False
    settings.setdefault("blacklist", []).append({
        "buyer_id": int(buyer_id) if str(buyer_id or "").isdigit() else None,
        "username": str(username).strip().lstrip("@") if username else None,
        "reason": str(reason or "вручную"),
        "ts": time.time(),
    })
    return True


def _remove_blacklist(settings: dict, buyer_id: Any = None, username: Any = None) -> bool:
    """Удаляет совпадения из нового object-формата и legacy-списка строк."""
    bid = _bl_key(buyer_id)
    uname = _bl_key(username)
    if not bid and not uname:
        return False
    out = []
    removed = False
    for entry in settings.get("blacklist", []):
        if isinstance(entry, dict):
            hit = (bid and entry.get("buyer_id") is not None and
                   _bl_key(entry.get("buyer_id")) == bid) or \
                  (uname and entry.get("username") and
                   _bl_key(entry.get("username")) == uname)
        else:
            key = _bl_key(entry).lstrip("@")
            hit = bool((uname and key == uname.lstrip("@")) or (bid and key == bid))
        if hit:
            removed = True
        else:
            out.append(entry)
    if removed:
        settings["blacklist"] = out
    return removed


def _is_blacklisted(settings: dict, buyer_id: Any = None, username: Any = None) -> bool:
    bid = _bl_key(buyer_id)
    uname = _bl_key(username).lstrip("@")
    if not bid and not uname:
        return False
    for entry in settings.get("blacklist", []):
        if isinstance(entry, dict):
            if bid and entry.get("buyer_id") is not None and _bl_key(entry.get("buyer_id")) == bid:
                return True
            if uname and entry.get("username") and _bl_key(entry.get("username")).lstrip("@") == uname:
                return True
        else:
            key = _bl_key(entry).lstrip("@")
            if (uname and key == uname) or (bid and key == bid):
                return True
    return False


# =========================================================================
# SMM-клиент (perfect-panel / smmpanel стандарт)
# =========================================================================

class SMMClient:
    """Клиент стандартного perfect-panel/smmpanel API."""

    def __init__(self, api_url: str, api_key: str, api_retries: int = 2):
        self.api_url = api_url
        self.api_key = api_key
        self.session = requests.Session()
        self.api_retries = max(0, min(2, int(api_retries or 0)))

    def _post(self, data: dict, *, safe: bool = False) -> Any:
        data = dict(data)
        data["key"] = self.api_key
        attempts = 1 + (self.api_retries if safe else 0)
        for attempt in range(attempts):
            try:
                resp = self.session.post(self.api_url, data=data, timeout=20)
                if resp.status_code == 429:
                    raise RateLimited("429 Too Many Requests")
                resp.raise_for_status()
                return resp.json()
            except (requests.Timeout, requests.ConnectionError, RateLimited):
                if attempt + 1 >= attempts:
                    raise
                time.sleep(1 + attempt)
            except requests.HTTPError as exc:
                code = getattr(exc.response, "status_code", None)
                if safe and (code == 429 or (code is not None and 500 <= code < 600)) \
                        and attempt + 1 < attempts:
                    time.sleep(1 + attempt)
                    continue
                raise
        return {}

    def add(self, service_id: int, link: str, quantity: int, **extras) -> Any:
        payload = {"action": "add", "service": service_id, "link": link,
                   "quantity": quantity}
        payload.update(extras)
        return self._post(payload)

    def status(self, order_id: Any, **kwargs) -> Any:
        return self._post({"action": "status", "order": order_id}, safe=True)

    def refill(self, order_id: Any) -> Any:
        return self._post({"action": "refill", "order": order_id})

    def balance(self) -> Any:
        return self._post({"action": "balance"}, safe=True)

    def cancel(self, order_id: Any) -> Any:
        return self._post({"action": "cancel", "order": order_id})

    def price(self, service_id: Any, quantity: int, **extras) -> Any:
        payload = {"action": "price", "service": service_id, "quantity": quantity}
        payload.update(extras)
        return self._post(payload, safe=True)

    def services(self) -> list[dict]:
        data = self._post({"action": "services"}, safe=True)
        return data if isinstance(data, list) else []


# Каталог услуг steamsmm.ru (REST). Код услуги = action_type из доков API.
_STEAMSMM_CATALOG = [
    {"service": "comment", "name": "Комментарии +rep (профиль с открытыми комментариями)"},
    {"service": "comment_rep", "name": "Комментарии -rep"},
    {"service": "comment_random", "name": "Случайные комментарии"},
    {"service": "comment_premium", "name": "Premium-комментарии (вариант/задержка)"},
    {"service": "like", "name": "Лайки (страница Steam Community)"},
    {"service": "dis", "name": "Дизлайки"},
    {"service": "subscribe", "name": "Участники группы"},
    {"service": "review", "name": "Лайки на обзор"},
    {"service": "commend_cs2", "name": "Похвала CS2 (friendly/teacher/leader)"},
]


class SteamSmmClient:
    """REST-клиент для steamsmm.ru (Authorization: Bearer, JSON-тело).

    Реализует тот же контракт, что и SMMClient (add/status/refill/balance/
    services/cancel/price/buy_autoregs), поэтому подставляется в
    _client_for_provider по типу поставщика.
    """

    def __init__(self, api_url: str, api_key: str, api_retries: int = 2):
        self.api_url = (api_url or "").rstrip("/")
        self.api_key = api_key
        self.api_retries = max(0, min(2, int(api_retries or 0)))
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        url = f"{self.api_url}{path}"
        safe = method.upper() == "GET"
        attempts = 1 + (self.api_retries if safe else 0)
        for attempt in range(attempts):
            try:
                resp = self.session.request(method, url, json=payload, timeout=20)
                if resp.status_code == 429:
                    raise RateLimited("429 Too Many Requests")
                if resp.status_code == 401:
                    raise RuntimeError("401 — неверный/истёкший API-ключ")
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, dict) and data.get("status") == "error":
                    code = data.get("code", "")
                    msg = data.get("message", "") or data.get("error", "")
                    if not msg and isinstance(data.get("data"), str):
                        msg = data["data"]
                    err = f"{code} {msg}".strip()
                    return {"error": err or "API вернул ошибку"}
                return data
            except (requests.Timeout, requests.ConnectionError, RateLimited):
                if attempt + 1 >= attempts:
                    raise
                time.sleep(1 + attempt)
            except requests.HTTPError as exc:
                code = getattr(exc.response, "status_code", None)
                if safe and code is not None and 500 <= code < 600 and attempt + 1 < attempts:
                    time.sleep(1 + attempt)
                    continue
                raise

    def _post(self, path: str, payload: dict) -> dict:
        return self._request("POST", path, payload)

    def _get(self, path: str) -> dict:
        return self._request("GET", path)

    # --- общий контракт ---

    def add(self, service_id: Any, link: str, quantity: int, **extras) -> dict:
        action = str(service_id or "")
        if action == "commend_cs2":
            return self._add_commend(link, extras)
        payload = {"action_type": action, "quantity": int(quantity), "target_link": link}
        if action == "comment_premium":
            for k in ("comment_variant", "delay_min_minutes", "delay_max_minutes"):
                if extras.get(k) is not None:
                    payload[k] = extras[k]
        resp = self._post("/order/create", payload)
        if resp.get("error"):
            return resp
        data = resp.get("data") or {}
        oid = data.get("order_id")
        return {"order": oid} if oid else resp

    def _add_commend(self, link: str, extras: dict) -> dict:
        target = link
        try:
            self.commend_check(target)
        except CommendOffline:
            raise
        except Exception as e:
            return {"error": _short_err(e) or "не удалось проверить игрока на сервере"}
        try:
            friendly = int(extras.get("commend_friendly") or 0)
            teacher = int(extras.get("commend_teacher") or 0)
            leader = int(extras.get("commend_leader") or 0)
        except (TypeError, ValueError):
            friendly = teacher = leader = 0
        if max(friendly, teacher, leader) <= 0:
            return {"error": "для похвалы CS2 нужно хотя бы одно значение > 0 (friendly/teacher/leader)"}
        resp = self._post("/commend/create", {
            "target": target, "friendly": friendly, "teacher": teacher, "leader": leader,
        })
        if resp.get("error"):
            return resp
        data = resp.get("data") or {}
        oid = data.get("order_id")
        return {"order": oid} if oid else resp

    def commend_check(self, link: str) -> None:
        """Проверка, что игрок на сервере для похвалы CS2 (POST /commend/check).

        Бросает CommendOffline, если игрока нет на сервере, и RuntimeError при
        ошибке API. Используется и в _add_commend, и для пред-проверки всех
        целей мульти-целевой похвалы ДО размещения (чтобы не оставить
        частично размещённый заказ)."""
        resp = self._post("/commend/check", {"target": link})
        if resp.get("error"):
            raise RuntimeError(resp["error"])
        if not (resp.get("data") or {}).get("on_server"):
            raise CommendOffline("игрок не на сервере для похвалы CS2")

    def status(self, order_id: Any, logs_limit: int = 100, commend: bool = False) -> dict:
        """Статус заказа. Для похвалы CS2 (commend=True или fallback) тянет
        /commend/{id}?logs_limit=N: прогресс, done/failed, журнал."""
        try:
            logs_limit = max(1, min(200, int(logs_limit or 100)))
        except (TypeError, ValueError):
            logs_limit = 100
        if commend:
            try:
                resp = self._get(f"/commend/{order_id}?logs_limit={logs_limit}")
            except Exception:
                return {"error": "не удалось получить статус похвалы"}
        else:
            try:
                resp = self._get(f"/order/status/{order_id}")
            except Exception:
                resp = {"error": "order/status failed"}
            if resp.get("error"):
                # для похвалы CS2 статус лежит в /commend/{order_id}
                try:
                    resp = self._get(f"/commend/{order_id}?logs_limit={logs_limit}")
                    commend = True
                except Exception:
                    return {"error": "не удалось получить статус"}
        data = resp.get("data") or {}
        st = data.get("status") or "processing"
        rem = None
        qty = None
        done = None
        failed = None
        try:
            qty = int(data.get("quantity") or 0) or None
        except (TypeError, ValueError):
            qty = None
        try:
            done = int(data.get("done") or data.get("completed") or 0) or None
        except (TypeError, ValueError):
            done = None
        try:
            failed = int(data.get("failed") or 0) or None
        except (TypeError, ValueError):
            failed = None
        if qty is None and done is not None and failed is not None:
            # у /commend/{id} может не быть quantity — total = выполнено + неудачно
            qty = done + failed
        if qty is not None and done is not None:
            rem = max(0, qty - done)
        charge = data.get("cost")
        if isinstance(charge, str):
            try:
                charge = float(charge)
            except ValueError:
                charge = None
        out = {"status": st, "remains": rem, "charge": charge,
                "currency": data.get("currency")}
        if commend:
            out.update({
                "commend": True,
                "done": done,
                "failed": failed,
                "progress": data.get("progress"),
                "logs": data.get("logs") or [],
                "logs_limit": logs_limit,
            })
        return out

    def refill(self, order_id: Any) -> dict:
        # REST-доки steamsmm не описывают refill для обычных заказов
        return {"error": "refill недоступен в REST-API"}

    def cancel(self, order_id: Any) -> dict:
        # REST-доки steamsmm не описывают отмену заказа
        return {"error": "cancel недоступен в REST-API"}

    def restart(self, order_id: Any) -> dict:
        """Перезапуск невыполненного остатка похвалы CS2 (POST /commend/{id}/restart)."""
        resp = self._post(f"/commend/{order_id}/restart", {})
        if resp.get("error"):
            return resp
        return resp

    def balance(self) -> dict:
        resp = self._get("/user/balance")
        if resp.get("error"):
            return resp
        data = resp.get("data") or {}
        return {"balance": data.get("balance"), "currency": data.get("currency")}

    def services(self) -> list[dict]:
        out = [dict(x) for x in _STEAMSMM_CATALOG]
        try:
            prod = self._get("/autoreg/products")
            groups = []
            if isinstance(prod, dict) and isinstance(prod.get("data"), dict):
                groups = prod["data"].get("groups") or []
            for group in groups:
                for it in (group.get("items") or []):
                    cat = it.get("category_id")
                    title = it.get("title") or f"Авторег {it.get('region', '')}".strip()
                    if cat:
                        out.append({
                            "service": f"autoreg:{cat}",
                            "name": f"Авторег: {title} (ост. {it.get('stock', '?')}, "
                                     f"{it.get('price_per_item', '?')}₽/шт)",
                        })
        except Exception:
            pass
        return out

    def price(self, service_id: Any, quantity: int, **extras) -> dict:
        action = str(service_id or "")
        if action == "commend_cs2":
            payload = {
                "friendly": int(extras.get("commend_friendly") or 0),
                "teacher": int(extras.get("commend_teacher") or 0),
                "leader": int(extras.get("commend_leader") or 0),
            }
            resp = self._post("/commend/price", payload)
        elif action.startswith("autoreg:"):
            # автореги считаются отдельным методом /autoreg/price по category_id
            try:
                cat = int(action.split(":", 1)[-1])
            except (TypeError, ValueError):
                return {"error": "autoreg category_id должен быть числом"}
            resp = self._post("/autoreg/price",
                              {"category_id": cat, "quantity": int(quantity)})
        else:
            payload = {"action_type": action, "quantity": int(quantity)}
            if action == "comment_premium":
                for k in ("comment_variant", "delay_min_minutes", "delay_max_minutes"):
                    if extras.get(k) is not None:
                        payload[k] = extras[k]
            resp = self._post("/order/price", payload)
        if resp.get("error"):
            return resp
        data = resp.get("data") or {}
        total = data.get("total_cost")
        if isinstance(total, str):
            try:
                total = float(total)
            except ValueError:
                total = None
        return {"total_cost": total, "price_per_item": data.get("price_per_item")}

    def buy_autoregs(self, category_id: Any, quantity: int) -> dict:
        try:
            cat = int(category_id)
        except (TypeError, ValueError):
            return {"error": "autoreg category_id должен быть числом"}
        resp = self._post("/autoreg/create", {"category_id": cat, "quantity": int(quantity)})
        if resp.get("error"):
            return resp
        data = resp.get("data") or {}
        accounts = data.get("accounts")
        if not isinstance(accounts, list):
            accounts = []
        return {"accounts": accounts, "order_id": data.get("order_id"),
                "accounts_text": data.get("accounts_text", "")}

    def autoreg_products(self) -> list[dict]:
        """Все товары авторегов из каталога /autoreg/products (один запрос).

        Возвращает список словарей (category_id, title, region, stock,
        min_count, max_count, price_per_item) или пустой список при ошибке.
        """
        try:
            prod = self._get("/autoreg/products")
        except Exception:
            return []
        if not isinstance(prod, dict) or prod.get("error") or \
                not isinstance(prod.get("data"), dict):
            return []
        out: list[dict] = []
        for group in prod["data"].get("groups") or []:
            for it in (group.get("items") or []):
                if it.get("category_id") is not None:
                    out.append(dict(it))
        return out

    def autoreg_product(self, category_id: Any) -> dict | None:
        """Товар авторегов из каталога /autoreg/products по category_id."""
        try:
            cat = int(category_id)
        except (TypeError, ValueError):
            return None
        return next((x for x in self.autoreg_products()
                     if str(x.get("category_id")) == str(cat)), None)


def _provider_style(provider: dict) -> str:
    st = (provider.get("style") or "").strip().lower()
    if st in ("rest", "steamsmm", "steamsmm_rest"):
        return "rest"
    return "smmpanel"


def _client_for_provider(provider: dict, settings: dict | None = None):
    settings = settings or _load_settings()
    retries = settings.get("api_retries", 2)
    if _provider_style(provider) == "rest":
        return SteamSmmClient(provider.get("api_url", ""), provider.get("api_key", ""), retries)
    return SMMClient(provider.get("api_url", ""), provider.get("api_key", ""), retries)


def _steamsmm_preset(key: str) -> dict:
    """Готовый пресет поставщика steamsmm.ru: URL + тип API + ключ."""
    return {
        "name": "steamsmm.ru",
        "api_url": "https://steamsmm.ru/api",
        "api_key": (key or "").strip()[:255],
        "style": "rest",
    }


def _format_autoreg_catalog(client) -> str:
    """Текст каталога авторегов (GET /autoreg/products) для подсказки оператору."""
    try:
        prod = client._get("/autoreg/products")
    except Exception as e:
        return f"— каталог авторегов не подтянулся: {_short_err(e)}"
    if not isinstance(prod, dict) or prod.get("error"):
        err = prod.get("error") if isinstance(prod, dict) else prod
        return f"— каталог авторегов недоступен: {err or '—'}"
    data = prod.get("data")
    if not isinstance(data, dict):
        if data is None:
            return "— каталог авторегов пуст (групп: 0)"
        return f"— каталог авторегов недоступен: {data!r}"
    groups = data.get("groups") or []
    lines: list[str] = []
    total = 0
    shown = 0
    for g in groups:
        for it in (g.get("items") or []):
            cat = it.get("category_id")
            if not cat:
                continue
            total += 1
            if shown >= 20:
                continue
            shown += 1
            title = it.get("title") or f"Авторег {it.get('region', '')}".strip()
            lines.append(f"• <code>{cat}</code> — {_html_escape(title)} "
                         f"(ост. {it.get('stock', '?')}, {it.get('price_per_item', '?')}₽/шт)")
    if not lines:
        return f"— каталог авторегов пуст (групп: {len(groups)})"
    if total > shown:
        lines.append(f"… и ещё {total - shown}")
    return "🛒 <b>Автореги steamsmm</b> (category_id для привязки «account → покупка»):\n" \
           + "\n".join(lines)


def _catalog_page(services: list[dict], offset: int, per_page: int = 6) -> tuple[str, list[dict]]:
    """Страница каталога услуг для визарда: (текст позиций, кнопки [{sid, name}])."""
    page = services[offset:offset + per_page]
    lines: list[str] = []
    buttons: list[dict] = []
    for svc in page:
        sid = svc.get("service", "?")
        name = str(svc.get("name", "—"))
        lines.append(f"• <code>{_html_escape(str(sid))}</code> — {_html_escape(name[:42])}")
        buttons.append({"sid": sid, "name": name[:42]})
    if not lines:
        lines.append("(пусто)")
    return "\n".join(lines), buttons


def _account_purchase_mapping(provider_id: Any, category_id: Any, lot_match: str,
                              cost: float | None = None) -> dict:
    """Словарь account-привязки «покупка авторегов» (визард «🎯 Привязать»)."""
    mapping = {
        "lot_match": lot_match,
        "mode": "account",
        "provider_id": provider_id,
        "autoreg_category_id": int(category_id),
    }
    if cost is not None:
        mapping["cost_per_unit"] = cost
    return mapping


def _parse_svc_pick(callback_data: str) -> tuple[str, str]:
    """Разбирает данные кнопки выбора услуги каталога: (pid, svc).
    svc может содержать ':' (например autoreg:2450503) — берём всё после pid."""
    _, _, rest = (callback_data or "").partition(f"{CBT_SVC_PICK}:")
    parts = rest.split(":", 1)
    pid = parts[0]
    svc = parts[1] if len(parts) > 1 else ""
    return pid, svc


def _find_default_provider(s: dict) -> dict | None:
    """Первый поставщик с API-ключом (для меню «Услуги»/«Цены»)."""
    for p in s.get("providers", []):
        if (p.get("api_key") or "").strip():
            return p
    return None


def _bind_service_lot(cardinal: "Cardinal", chat_id: Any, svc: str, volume: int,
                      lot_id: int, msg: Any | None = None,
                      min_qty: int | None = None,
                      node_id: int | None = None) -> bool:
    """Создаёт привязку лот→услуга для заданного объёма (меню «🎯 Услуги»).

    Если такой объём уже привязан — обновляем lot_id. min_qty — минимальный
    объём заказа (для авто-созданных лотов): меньше него бот шлёт покупателю
    ошибку без возврата денег. Для похвалы CS2 в привязку копируются параметры
    friendly/teacher/leader из уже существующей привязки похвалы (иначе при
    заказе поставщик отклоняет без параметров и деньги возвращаются).
    Возвращает True при успехе.
    """
    s = _load_settings()
    provider = _find_default_provider(s)
    if not provider:
        if msg is not None:
            try:
                bot = getattr(cardinal, "telegram", None)
                if bot is not None:
                    bot.bot.reply_to(msg, "❌ Сначала введите API-ключ: ⚙️ Настройки → 🔑 API-ключ steamsmm.ru.")
            except Exception:
                pass
        return False

    def _commend_params_source() -> dict | None:
        """Первая привязка похвалы CS2 с заданными friendly/teacher/leader."""
        if str(svc) != "commend_cs2":
            return None
        return next((m for m in s.get("lot_mappings", [])
                     if str(m.get("service_id") or "") == "commend_cs2"
                     and max(int(m.get("commend_friendly", 0) or 0),
                             int(m.get("commend_teacher", 0) or 0),
                             int(m.get("commend_leader", 0) or 0)) > 0), None)

    mapping = None
    for m in s.get("lot_mappings", []):
        if str(m.get("service_id") or "") == str(svc) and \
                int(m.get("qty_multiplier", 1) or 1) == int(volume) and \
                (str(svc) != "commend_cs2" or m.get("funpay_node_id") == node_id):
            mapping = m
            break
    if mapping is None:
        mapping = {
            "id": _new_id("m", s.get("lot_mappings", [])),
            "lot_match": str(lot_id),
            "mode": "service",
            "provider_id": provider.get("id"),
            "service_id": svc,
            "qty_multiplier": float(volume),
            "cost_per_unit": None,
            "target_lot_id": int(lot_id),
        }
        if str(svc) == "commend_cs2" and node_id is not None:
            mapping["funpay_node_id"] = int(node_id)
        if str(svc) == "commend_cs2":
            src = _commend_params_source()
            if src is not None:
                mapping["commend_friendly"] = src.get("commend_friendly")
                mapping["commend_teacher"] = src.get("commend_teacher")
                mapping["commend_leader"] = src.get("commend_leader")
            else:
                # без заданных параметров — дефолтный пакет, чтобы авто-лот
                # создался и заказы не отклонялись поставщиком
                mapping.update(_commend_params(s, mapping))
        if min_qty is not None:
            mapping["min_qty"] = int(min_qty)
        s["lot_mappings"].append(mapping)
    else:
        mapping["lot_match"] = str(lot_id)
        mapping["target_lot_id"] = int(lot_id)
        mapping["provider_id"] = provider.get("id")
        mapping["cost_per_unit"] = None  # свежая цена пересчитается по живому тарифу
        if str(svc) == "commend_cs2" and \
                max(int(mapping.get("commend_friendly", 0) or 0),
                    int(mapping.get("commend_teacher", 0) or 0),
                    int(mapping.get("commend_leader", 0) or 0)) <= 0:
            src = _commend_params_source()
            if src is not None:
                mapping["commend_friendly"] = src.get("commend_friendly")
                mapping["commend_teacher"] = src.get("commend_teacher")
                mapping["commend_leader"] = src.get("commend_leader")
            else:
                mapping.update(_commend_params(s, mapping))
        if min_qty is not None:
            mapping["min_qty"] = int(min_qty)
    _save_settings(s)

    # Установить стартовую цену лота по наценке (если доступен FunPay-аккаунт)
    try:
        price = _volume_price(s, provider, svc, int(volume))
        acc = getattr(cardinal, "account", None)
        if price is not None and acc is not None and hasattr(acc, "get_lot_fields") \
                and hasattr(acc, "save_lot"):
            fields = acc.get_lot_fields(int(lot_id))
            fields.price = price
            fields.amount = 1
            fields.active = True
            acc.save_lot(fields)
    except Exception:
        pass

    _log_action("lot_bound", f"лот {lot_id}", svc=svc, vol=volume)
    if msg is not None:
        try:
            getattr(cardinal, "telegram", None).bot.reply_to(
                msg,
                f"✅ Лот <code>{lot_id}</code> привязан: {svc} × {volume} шт. "
                f"Цена выставлена по наценке.",
                parse_mode="HTML")
        except Exception:
            pass
    return True


# последние опции select'ов формы нового лота (диагностика fields[type] и пр.)
_last_form_selects: dict[str, list[tuple[str, str]]] = {}

# ключевые слова для подбора fields[type] по услуге (в подкатегории Steam услуг)
_TYPE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "comment": ("+rep", "positive rep", "положительн", "комментар", "comment"),
    "comment_rep": ("-rep", "negative rep", "негативн", "отрицательн", "комментар", "comment"),
    "comment_random": ("случайн", "random", "комментар", "comment"),
    "comment_premium": ("премиум", "premium", "комментар", "comment"),
    "like": ("лайк", "like"),
    "dis": ("дизлайк", "dislike", "не нравится"),
    "subscribe": ("участник", "member", "групп", "group", "подписч", "subscriber"),
    "review": ("обзор", "review", "рецензи", "recommendation"),
    "commend_cs2": ("похвал", "комментар", "commend", "friendly", "teacher", "leader"),
}
_TYPE_FORBIDDEN = (
    "регистрация аккаунта", "account registration", "создание аккаунта",
    "продажа аккаунта", "account sale", "добавление в семью", "family sharing",
)


def _normalise_type_text(value: Any) -> str:
    """Нормализация label/value FunPay для устойчивого смыслового сравнения."""
    text = str(value or "").casefold().replace("ё", "е")
    text = re.sub(r"[‐‑‒–—−]", "-", text)
    text = re.sub(r"[^a-zа-я0-9+\-]+", " ", text)
    return " ".join(text.split())


def _resolve_type_option(options: list[tuple[Any, Any]], svc: str) -> str:
    """Возвращает value лучшего семантического типа либо пустую строку.

    Сравниваются и label, и value, но возвращается только фактический value
    формы. Типы аккаунтов явно запрещены и никогда не служат fallback.
    """
    keywords = tuple(_normalise_type_text(k) for k in _TYPE_KEYWORDS.get(svc, ()))
    best_value, best_score = "", 0
    for raw_value, raw_label in options:
        value = str(raw_value or "")
        haystack = _normalise_type_text(f"{raw_label or ''} {value}")
        if not value or not haystack or any(x in haystack for x in _TYPE_FORBIDDEN):
            continue
        score = sum(1 for keyword in keywords if keyword and keyword in haystack)
        # Уточнения не позволяют соседним REP-вариантам победить общий тип.
        if svc == "comment" and "-rep" in haystack:
            score = 0
        elif svc == "comment_rep" and "+rep" in haystack:
            score = 0
        elif svc == "like" and ("дизлайк" in haystack or "dislike" in haystack):
            score = 0
        elif svc == "review" and not any(x in haystack for x in ("обзор", "review", "рецензи", "recommendation")):
            score = 0
        if score > best_score:
            best_score, best_value = score, value
    return best_value


def _pick_type_option(select, svc: str, node_id: int | None = None) -> str:
    """Выбирает совместимый ``fields[type]`` по label/value без fallback."""
    options = [(opt.get("value", ""), opt.get_text(" ", strip=True))
               for opt in select.find_all("option")]
    if svc == "commend_cs2":
        # Категории имеют разные живые подписи. Разрешаем только известные
        # безопасные значения; первый/произвольный option никогда не берём.
        node = int(node_id or 0)
        if node == 1351:
            # В этой подкатегории безопасен только точный нормализованный тип
            # «Прочее»; совпадения по value и расширенные алиасы запрещены.
            for value, label in options:
                if value and _normalise_type_text(label) == "прочее":
                    return value
            return ""
        safe = (("комментар", "comment") if node == 1009 else
                ("похвал", "commend", "friendly", "teacher", "leader"))
        filtered = []
        for value, label in options:
            haystack = _normalise_type_text(f"{label} {value}")
            if value and not any(x in haystack for x in _TYPE_FORBIDDEN) and \
                    any(keyword in haystack for keyword in safe):
                filtered.append((value, label))
        return _resolve_type_option(filtered, svc) if filtered else ""
    return _resolve_type_option(options, svc)


def _normalise_funpay_offer_payload(payload: Any) -> Any:
    """Приводит form data к семантике HTML-формы FunPay.

    ``fields[images]`` — CSV идентификаторов, ``secrets`` — строки товаров,
    а checkbox-поля передаются как ``on`` только во включённом состоянии.
    Пустые optional controls не должны отправляться как ``key=``: именно так
    браузер сериализует выключенные/пустые условные поля. Список пар сохраняем
    списком, поэтому повторяющиеся ключи не схлопываются.
    """
    optional_empty = {
        "fields[images]", "secrets", "deactivate_after_sale", "auto_delivery"
    }
    if isinstance(payload, dict):
        result = copy.copy(payload)
        for key in optional_empty:
            if result.get(key) in (None, "", False):
                result.pop(key, None)
        return result
    if isinstance(payload, (list, tuple)):
        return [(key, value) for key, value in payload
                if not (key in optional_empty and value in (None, "", False))]
    return payload


def _funpay_missing_fields(exc: Exception) -> list[str]:
    """Возвращает реальные поля из структурированной ошибки FunPayAPI."""
    errors = getattr(exc, "errors", None)
    if not isinstance(errors, dict):
        return []
    return [str(key) for key, value in errors.items() if value]


def _fetch_new_lot_form(acc, node_id: int, svc: str | None = None) -> dict | None:
    """Форма нового лота через низкоуровневый acc.method (GET lots/offerEdit?node=...).

    Работает на любой версии FunPayAPI (сигнатура method() стабильна), в том
    числе там, где get_lot_fields не принимает node_id или падает на парсинге
    формы (select без выбранного option → 'NoneType' object is not subscriptable).
    Парсим поля формы защитно. Если у select «fields[type]» нет выбранного
    option — подбираем по ключевым словам услуги (svc). Возвращает dict полей
    или None.
    """
    method = getattr(acc, "method", None)
    if not callable(method):
        return None
    try:
        resp = method("get", f"lots/offerEdit?node={node_id}", {}, {},
                      raise_not_200=True)
    except Exception:
        return None
    try:
        from bs4 import BeautifulSoup
    except Exception:
        return None
    try:
        html = resp.content.decode()
        try:
            bs = BeautifulSoup(html, "lxml")
        except Exception:
            bs = BeautifulSoup(html, "html.parser")
        if bs.find("p", class_="lead"):
            return None
        form = bs.find("form", class_="form-offer-editor")
        if form is None:
            return None
        result: dict[str, str] = {}
        for field in form.find_all("input"):
            name = field.get("name")
            if name and name != "query":
                result[name] = field.get("value") or ""
        for field in form.find_all("textarea"):
            name = field.get("name")
            if name:
                result[name] = field.text or ""
        selects: dict[str, list[tuple[str, str]]] = {}
        for field in form.find_all("select"):
            name = field.get("name")
            if not name:
                continue
            opts = [(o.get("value", ""), o.get_text(strip=True))
                    for o in field.find_all("option")]
            selects[name] = opts
            group = field.find_parent(class_="form-group")
            hidden = group is not None and "hidden" in (group.get("class") or [])
            # fields[type] обязателен для lots/offerSave — заполняем его даже
            # если группа скрыта (иначе FunPay: «Заполните все поля»)
            if hidden and name != "fields[type]":
                continue
            opt = field.find("option", selected=True)
            if name == "fields[type]":
                # Выбранный браузером option может быть первым пунктом формы
                # («Регистрация аккаунта»). Для каждой услуги всегда выполняем
                # семантический подбор по полному списку label/value.
                result[name] = _pick_type_option(field, svc, node_id)
            elif opt is not None and opt.get("value", ""):
                result[name] = opt.get("value", "")
            elif not hidden:
                result[name] = ""
        for field in form.find_all("input", {"type": "checkbox"}, checked=True):
            name = field.get("name")
            if name:
                result[name] = "on"
        if not result.get("node_id"):
            result["node_id"] = str(node_id)
        if result.get("csrf_token"):
            try:
                acc.csrf_token = result["csrf_token"]
            except Exception:
                pass
        _last_form_selects.clear()
        _last_form_selects.update(selects)
        return result
    except Exception:
        return None


def _build_lot_fields(acc, form: dict):
    """Создаёт настоящий FunPayAPI.LotFields из словаря полей формы.

    Сначала пробуем импорт FunPayAPI.types (в рантайме Cardinal он доступен),
    иначе — класс из образца get_lot_fields(0). Возвращает объект или None.
    """
    try:
        from FunPayAPI.types import LotFields
        return LotFields(0, form)
    except Exception:
        pass
    getter = getattr(acc, "get_lot_fields", None)
    if callable(getter):
        try:
            sample = getter(0)
            return type(sample)(0, form)
        except Exception:
            return None
    return None


def _set_lot_min_line(desc: str, min_line: str) -> str:
    """Вписывает строку минимума в описание лота.

    Заменяет существующую строку «MIN: N» (из пресета-образца), иначе добавляет
    минимум в конец — чтобы описание всегда совпадало с тем, что реально
    проверяет бот при заказе.
    """
    new = re.sub(r"(?m)^.*\bMIN:\s*\d+.*$", min_line, desc)
    if new == desc:
        new = f"{desc}\n{min_line}"
    return new


def _existing_lot_for_service(cardinal: "Cardinal", settings: dict,
                              svc: str, volume: int) -> int | None:
    """Ищет существующую привязку/лот до POST, чтобы не плодить дубликаты."""
    for mapping in settings.get("lot_mappings", []):
        if (str(mapping.get("service_id") or "") == str(svc)
                and int(float(mapping.get("qty_multiplier", 1) or 1)) == int(volume)):
            lot_id = _lot_target_id(mapping)
            if lot_id is not None:
                return lot_id
            match = str(mapping.get("lot_match") or "").strip()
            if match.isdigit():
                return int(match)
    expected = _normalise_type_text((LOT_PRESETS.get(svc) or {}).get("title"))
    if not expected:
        return None
    for lot in _account_lots(cardinal):
        title = re.sub(r"\s*\(\s*\d+\s*(?:шт|pcs?)\s*\)\s*$", "", _lot_title(lot),
                       flags=re.IGNORECASE)
        if _normalise_type_text(title) == expected:
            try:
                return int(lot.id)
            except (TypeError, ValueError):
                continue
    return None


def _creation_success_text(svc: str, lot_id: int, volume: int) -> str:
    """Корректное сообщение об объёме обычной услуги и пакетной похвалы CS2."""
    head = (f"✅ Лот <code>{lot_id}</code> создан на FunPay и привязан: "
            f"{SERVICE_PRESETS[svc]['name']} × <code>{volume}</code>. ")
    if svc == "commend_cs2":
        return (head + "Цена указана за 1 похвалу. Минимум API — 15; количество "
                "на кассе означает число похвал одному профилю, а пакет "
                "Friendly/Teacher/Leader масштабируется по настройкам.")
    return (head + "Цена указана за 1 единицу. Количество на кассе умножает "
            "объём услуги; заказ ниже минимального лимита API не запускается.")


def _auto_create_lot(cardinal: "Cardinal", svc: str, volume: int,
                     min_out: list | None = None,
                     node_id_override: int | None = None) -> tuple[int | None, str]:
    """Пытается создать лот на FunPay автоматически.

    1) Если у FunPay-модуля есть create_lot — используем его (форки Cardinal).
    2) Иначе получаем пустую форму нового лота: сначала низкоуровнево через
       acc.method (GET lots/offerEdit?node=...) — работает на любой версии
       FunPayAPI, затем штатный get_lot_fields(0, node_id=...) как запасной
       путь. Заполняем → save_lot (offer_id=0 означает создание). ID нового
       лота достаём из ответа lots/offerSave (перехватываем response через
       acc.method), а если ответ не парсится — диффом списков
       get_my_subcategory_lots до/после.

    Возвращает (ID созданного лота или None, причина отказа/'' при успехе).
    Причина — короткая фраза для оператора (показывается в сообщении).
    """
    acc = getattr(cardinal, "account", None)
    if acc is None:
        return None, "нет доступа к FunPay-аккаунту"
    s = _load_settings()
    # базовый объём авто-лота — выбранный оператором (кнопка «➕ Создать N шт»),
    # иначе из настроек (auto_lot_base_volume, по умолчанию 1 шт): цену/название
    # считаем за этот объём, покупатель сам умножает количество на кассе FunPay
    base_vol = max(1, int(volume or s.get("auto_lot_base_volume", 1) or 1))
    preset = LOT_PRESETS.get(svc) or {}
    base_title = preset.get("title") or SERVICE_PRESETS.get(svc, {}).get("name", svc)
    base_title_en = preset.get("title_en") or SERVICE_PRESETS.get(svc, {}).get("name", svc)
    # Название сообщает цену за единицу; объём/минимум описаны отдельно и не
    # дублируются вводящим в заблуждение суффиксом «(N шт)».
    summary = str(base_title)
    summary_en = str(base_title_en)
    desc_ru = preset.get("desc_ru") or "Steam SMM"
    desc_en = preset.get("desc_en") or "Steam SMM"
    # Минимум для покупки в описании — покупатель видит, сколько минимум нужно
    # оформить (проверяется при заказе): 10 для обычных услуг, 15 для похвалы
    # CS2 (количество = число похвал одному профилю), min_count из каталога
    # для авторегов. min_out возвращает его наружу, чтобы привязка сохранила
    # тот же минимум в mapping.
    desc_min = _auto_lot_min_qty(s, _find_default_provider(s), svc)
    if desc_min:
        # минимум в описании = не меньше объёма лота (лот создаётся на N шт —
        # один «пакет» уже покрывает минимум API)
        desc_min = max(int(desc_min), base_vol)
        desc_ru = _set_lot_min_line(desc_ru, f"⚡️ Минимум для покупки: {desc_min} шт")
        desc_en = _set_lot_min_line(desc_en, f"⚡️ Minimum order: {desc_min} pcs")
    if min_out is not None and desc_min:
        min_out.append(int(desc_min))

    # 1) create_lot — есть в некоторых форках/модулях
    create = getattr(acc, "create_lot", None)
    if callable(create):
        provider = _find_default_provider(s)
        price = _volume_price(s, provider, svc, base_vol) if provider else None
        try:
            lot_id = create(summary=summary, description=desc_ru,
                            price=price or 1.0, amount=1, active=True)
            _log_action("lot_auto_created", f"лот {lot_id}", svc=svc, vol=volume)
            return int(lot_id), ""
        except Exception as e:
            logger.debug(f"{LOGGER_PREFIX} auto create_lot не удался", exc_info=True)
            _log_action("lot_auto_create_failed", f"{svc}×{volume}",
                        method="create_lot", error=_short_err(e))
            return None, f"ошибка FunPay create_lot: {_short_err(e)}"

    # 2) bundled FunPayAPI: get_lot_fields(0, node_id) + save_lot
    get_fields = getattr(acc, "get_lot_fields", None)
    save_lot = getattr(acc, "save_lot", None)
    if not callable(get_fields) or not callable(save_lot):
        _log_action("lot_auto_create_failed", f"{svc}×{volume}",
                    reason="FunPayAPI без get_lot_fields/save_lot")
        return None, "ваш FunPayAPI не поддерживает создание лотов (get_lot_fields/save_lot)"
    # 0 в конфиге (старый дефолт / не настроено) = базовая подкатегория из DEFAULT_SETTINGS
    node_id = (node_id_override if node_id_override is not None else
               (s.get("auto_lot_node_id") or DEFAULT_SETTINGS.get("auto_lot_node_id") or None))
    if not node_id:
        logger.info(f"{LOGGER_PREFIX} автосоздание лота: не задан auto_lot_node_id "
                    f"(⚙️ Настройки → Подкатегория авто-лотов) — создайте лот вручную")
        _log_action("lot_auto_create_failed", f"{svc}×{volume}",
                    reason="не задана подкатегория (auto_lot_node_id)")
        return None, "не задана подкатегория авто-лотов (⚙️ Настройки → 🌐 Подкатегория авто-лотов)"
    if svc == "commend_cs2" and int(node_id) == 1836:
        reason = "узел 1836 не поддерживается для похвалы"
        _log_action("lot_auto_create_skipped", f"{svc}×{base_vol}",
                    node=node_id, reason=reason)
        return None, reason
    provider = _find_default_provider(s)
    if provider is None:
        logger.info(f"{LOGGER_PREFIX} автосоздание лота: не задан API-ключ "
                    f"steamsmm.ru — создайте лот вручную")
        _log_action("lot_auto_create_failed", f"{svc}×{base_vol}",
                    reason="цена не считается: не задан API-ключ")
        return None, ("цена не считается: не задан API-ключ "
                      "(⚙️ Настройки → 🔑 API-ключ steamsmm.ru)")
    price_reason: list = []
    price = _volume_price(s, provider, svc, base_vol, reason_out=price_reason)
    if price is None:
        cause = price_reason[0] if price_reason else \
            "проверьте API-ключ и привязку услуги (⚙️ Настройки)"
        logger.info(f"{LOGGER_PREFIX} автосоздание лота: не вышла цена "
                    f"{svc}×{base_vol}: {cause} — создайте лот вручную")
        _log_action("lot_auto_create_failed", f"{svc}×{base_vol}",
                    reason=f"цена не считается: {cause}")
        return None, f"цена не считается: {cause}"

    # FunPay account prices are sent in RUB in this integration. Apply the
    # marketplace minimum only after provider cost, markup/conversion and
    # rounding; the same final value is then assigned to both price aliases.
    price = _funpay_unit_price(price, "RUB")

    # Форма нового лота: сначала низкоуровнево через acc.method (работает на
    # любой версии FunPayAPI), затем штатный get_lot_fields как запасной путь.
    fields = None
    last_err = ""
    form = _fetch_new_lot_form(acc, int(node_id), svc)
    volume = base_vol
    if form and not str(form.get("fields[type]") or "").strip():
        options = [text for value, text in _last_form_selects.get("fields[type]", [])
                   if value]
        option_hint = ", ".join(options[:5]) or "нет доступных типов"
        if svc == "commend_cs2" and int(node_id) == 1009:
            reason = ("CS2: подкатегория 1009 не подходит — семантически "
                      f"подходящего fields[type] нет; нужна другая подкатегория. "
                      f"Доступные типы: {option_hint}")
        else:
            reason = (f"категория {svc}: подкатегория FunPay {node_id} не содержит "
                      f"подходящий fields[type]. Доступные типы: {option_hint}")
        _log_action("lot_auto_create_failed", f"{svc}×{volume}",
                    node=node_id, missing_fields="fields[type]", error=reason)
        return None, reason
    if form:
        fields = _build_lot_fields(acc, form)
    # Без разобранных option нельзя доказать семантику числового value.
    # Поэтому штатный get_lot_fields не используется как слепой fallback:
    # иначе первый option снова мог бы незаметно стать регистрацией аккаунта.
    if fields is None:
        reason = last_err or "форма нового лота с вариантами fields[type] не получена"
        logger.info(f"{LOGGER_PREFIX} автосоздание лота: {reason} — создайте лот вручную")
        _log_action("lot_auto_create_failed", f"{svc}×{volume}",
                    node=node_id, error=reason)
        return None, (f"не удалось получить форму нового лота на FunPay: {reason} — "
                      "попробуйте позже или создайте лот вручную")
    try:
        fields.title_ru = summary
        if getattr(fields, "title_en", None) is not None:
            fields.title_en = summary_en
        if getattr(fields, "description_ru", None) is not None:
            fields.description_ru = desc_ru
        if getattr(fields, "description_en", None) is not None:
            fields.description_en = desc_en
        # «Сообщение покупателю» — обязательное поле формы FunPay (иначе «Заполните все поля»)
        if getattr(fields, "payment_msg_ru", None) is not None:
            fields.payment_msg_ru = DEFAULT_PAYMENT_MSG_RU
        if getattr(fields, "payment_msg_en", None) is not None:
            fields.payment_msg_en = DEFAULT_PAYMENT_MSG_EN
        # Лот-услуга остаётся в продаже после продажи. У выключенного checkbox
        # нет form-data пары: LotFields.renew_fields() временно создаст "", а
        # перехватчик ниже удалит её перед POST. Непустое значение исходной
        # формы, напротив, сохраняется без подмены.
        if getattr(fields, "deactivate_after_sale", None) is not None:
            fields.deactivate_after_sale = False
        if getattr(fields, "auto_delivery", None) is not None:
            fields.auto_delivery = False
        fields.price = price
        fields.amount = 1
        fields.active = True
        # Форма услуг Steam (node 1009) для похвалы CS2 валидирует цену также
        # через обязательный ключ ``fields[price]``. Передаём туда ту же уже
        # нормализованную строку, которая назначена верхнеуровневому ``price``.
        price_aliases = {
            key: str(price) for key in fields.fields
            if key != "price" and re.fullmatch(r"fields\[price\]", str(key))
        }
        if svc == "commend_cs2" and int(node_id) == 1009:
            price_aliases["fields[price]"] = str(price)
        # location/deleted — настоящие hidden-поля offerSave. Изображения и
        # secrets не hidden defaults, а условные controls типов товара; пустые
        # значения для лота-услуги не добавляем.
        try:
            fields.edit_fields({"location": "trade", "deleted": "0", **price_aliases})
        except Exception:
            logger.debug(f"{LOGGER_PREFIX} не удалось дописать hidden-поля формы", exc_info=True)
    except Exception as e:
        logger.debug(f"{LOGGER_PREFIX} не удалось заполнить форму нового лота", exc_info=True)
        _log_action("lot_auto_create_failed", f"{svc}×{volume}",
                    node=node_id, error="не удалось заполнить форму")
        return None, f"не удалось заполнить форму лота: {_short_err(e)}"

    before = _my_subcategory_lots(acc, int(node_id))
    captured: dict = {}
    method = getattr(acc, "method", None)
    original = None
    if callable(method):
        original = method

        def spy(req_method, api_method, headers, payload, **kw):
            if str(api_method).endswith("offerSave"):
                payload = _normalise_funpay_offer_payload(payload)
                captured["request_payload"] = payload
            resp = original(req_method, api_method, headers, payload, **kw)
            if str(api_method).endswith("offerSave"):
                captured["response"] = resp
            return resp

        acc.method = spy
    try:
        save_lot(fields)
    except Exception as e:
        logger.debug(f"{LOGGER_PREFIX} save_lot (создание лота) не удался", exc_info=True)
        # В сообщении показываем только структурированные ошибки FunPay, а не
        # все корректно отсутствующие checkbox/optional controls.
        missing = _funpay_missing_fields(e)
        empty = ""
        try:
            sent = captured.get("request_payload", fields.fields)
            if isinstance(sent, dict):
                empty = ", ".join(k for k, v in sent.items() if v in (None, ""))
        except Exception:
            pass
        type_opts = _last_form_selects.get("fields[type]", [])
        payload_log = ""
        try:
            pl = {}
            logged_payload = captured.get("request_payload", fields.fields)
            payload_items = logged_payload.items() if isinstance(logged_payload, dict) else logged_payload
            for k, v in payload_items:
                if _SECRET_KEY_RE.search(str(k)):
                    pl[k] = "[REDACTED]"
                else:
                    pl[k] = str(v)[:120]
            payload_log = json.dumps(pl, ensure_ascii=False)
        except Exception:
            payload_log = "<payload недоступен>"
        _log_action("lot_auto_create_failed", f"{svc}×{volume}",
                    node=node_id, error=f"save_lot: {_short_err(e)}",
                    missing_fields=", ".join(missing) or "-",
                    empty_sent_fields=empty or "-",
                    type_options=str(type_opts) if type_opts else "-",
                    raw={
                        "full_error": str(e),
                        "type_options_all": str(type_opts),
                        "payload": payload_log,
                    })
        suffix = f" Поля FunPay: {', '.join(missing)}." if missing else ""
        return None, (f"категория {svc}: ошибка FunPay при сохранении лота: "
                      f"{_short_err(e)}{suffix}")
    finally:
        if original is not None:
            try:
                del acc.method
            except Exception:
                pass

    # ID нового лота: сначала из ответа offerSave, затем диффом по списку лотов
    new_id = None
    resp = captured.get("response")
    if resp is not None:
        try:
            data = resp.json() if hasattr(resp, "json") else {}
            new_id = _extract_new_lot_id(data)
        except Exception:
            logger.debug(f"{LOGGER_PREFIX} не удалось распарсить ответ offerSave", exc_info=True)
    if new_id is None and before is not None:
        try:
            after = _my_subcategory_lots(acc, int(node_id))
            if after is not None:
                candidates = [i for i in after if i not in before]
                if len(candidates) == 1:
                    new_id = candidates[0]
                elif len(candidates) > 1:
                    for i in candidates:
                        if (after.get(i) or "").strip() == summary.strip():
                            new_id = i
                            break
        except Exception:
            logger.debug(f"{LOGGER_PREFIX} не удалось найти новый лот диффом", exc_info=True)
    if new_id is None:
        logger.warning(f"{LOGGER_PREFIX} лот создан, но его ID не определён — "
                       f"проверьте список лотов вручную")
        _log_action("lot_auto_created_unknown_id", f"{svc}×{volume}",
                    node=node_id, note="лот создан, ID не определён — проверьте вручную")
        return None, ("лот, похоже, создан на FunPay, но его ID не определился — "
                      "найдите лот в списке и пришлите ID")
    _log_action("lot_auto_created", f"лот {new_id}", svc=svc, vol=volume, node=node_id)
    return int(new_id), ""


def _extract_new_lot_id(data: Any) -> int | None:
    """Достаёт ID созданного лота из JSON-ответа lots/offerSave.

    FunPay может вернуть id/offer/url — перебираем все варианты, включая
    вложенные словари (например data: {...})."""
    if not isinstance(data, dict):
        return None
    for key in ("offer", "offer_id", "id", "lot_id", "offerId", "lotId"):
        v = data.get(key)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, str)) and str(v).strip().isdigit():
            return int(str(v).strip())
    for key in ("url", "redirect", "location", "link"):
        v = data.get(key)
        if isinstance(v, str):
            m = re.search(r"(?:[?&]id=|/offers/|offer\?id=)(\d+)", v)
            if m:
                return int(m.group(1))
    for v in data.values():
        if isinstance(v, dict):
            inner = _extract_new_lot_id(v)
            if inner is not None:
                return inner
    return None


def _my_subcategory_lots(acc, node_id: int) -> dict[int, str] | None:
    """ID лотов продавца в подкатегории → название (для диффа после создания).

    Возвращает None, если FunPayAPI не умеет (старая версия)."""
    getter = getattr(acc, "get_my_subcategory_lots", None)
    if not callable(getter):
        return None
    try:
        lots = getter(node_id)
    except Exception:
        return None
    out: dict[int, str] = {}
    for lot in lots or []:
        oid = getattr(lot, "offer_id", None) or getattr(lot, "id", None)
        if oid is None:
            continue
        try:
            oid = int(str(oid))
        except (TypeError, ValueError):
            continue
        desc = getattr(lot, "description", None) or getattr(lot, "title", None) or None
        out[oid] = str(desc) if desc is not None else ""
    return out


def _hint_autoreg_catalog(bot, chat_id: Any, provider: dict) -> None:
    """После добавления пресета steamsmm подтягивает каталог авторегов и шлёт подсказку."""
    try:
        text = _format_autoreg_catalog(_client_for_provider(provider))
    except Exception as e:
        text = f"— каталог авторегов не подтянулся: {_short_err(e)}"
    try:
        bot.send_message(
            chat_id,
            text + "\n\nИспользуйте category_id при привязке лота: "
                   "🗺 Привязки → ➕ Добавить → account → «покупка».",
            parse_mode="HTML")
    except Exception:
        pass


def _find_provider(settings: dict, provider_id: str) -> dict | None:
    for p in settings.get("providers", []):
        if p.get("id") == provider_id:
            return p
    return None


def _provider_balance(settings: dict) -> list[dict]:
    out = []
    for p in settings.get("providers", []):
        if not (p.get("api_key") or "").strip() or not (p.get("api_url") or "").strip():
            continue
        try:
            data = _client_for_provider(p).balance()
            if isinstance(data, dict) and data.get("error"):
                out.append({"provider": p, "error": str(data["error"])})
            else:
                out.append({"provider": p, "data": data})
        except Exception as e:
            out.append({"provider": p, "error": str(e)})
    return out


# =========================================================================
# Привязка лот → услуга
# =========================================================================

def _match_mapping(settings: dict, lot_id: Any, lot_desc: str) -> dict | None:
    """Cardinal-style поиск привязки лота.

    `lot_match` в конфиге — либо название лота (или его часть), либо
    числовой ID лота (визард допускает оба варианта). Поэтому:
      Приоритет 1 — точный матч по ID лота (lot_match == lot_id);
      Приоритет 2 — по названию: нормализованные lot_match и описание
    ищутся друг в друге в обе стороны (match in desc / desc in match),
    как в autorobux_fpc. Чисто числовые lot_match считаются ID и в поиске
    по названию не участвуют (та же договорённость, что в _match_lots_by_name).
    """
    lid = str(lot_id or "").strip()
    limit = max(1, int(settings.get("lot_desc_match_limit", 200) or 200))
    desc = _normalize_lot_text(lot_desc)[:limit]

    # Приоритет 1: lot_match == реальный ID лота.
    if lid:
        for m in settings.get("lot_mappings", []):
            match = str(m.get("lot_match", "")).strip()
            if match and match == lid:
                return m

    # Приоритет 2: название/подстрока в обе стороны, с нормализацией.
    # Пустое описание не матчим: `"" in match` в Python — True, и пустой
    # lot_desc случайно поймал бы первую привязку по названию.
    if not desc:
        return None
    for m in settings.get("lot_mappings", []):
        match_raw = str(m.get("lot_match", "")).strip()
        if not match_raw:
            continue
        if match_raw.isdigit():
            continue  # числовой lot_match — это ID, а не название для поиска
        match = _normalize_lot_text(match_raw)
        if match in desc or desc in match:
            return m
    return None


def _extract_lot_id(cardinal: "Cardinal", order: Any) -> Any:
    """Реальный id лота FunPay.

    Приоритет: cardinal.get_order_from_object (как в steam_rental/autorobux) →
    lot_id полного заказа → парсинг HTML. subcategory.id — это НЕ lot_id
    (id подкатегории), в фоллбэк НЕ берём.
    """
    full_order = None
    try:
        getter = getattr(cardinal, "get_order_from_object", None)
        if callable(getter):
            full_order = getter(order)
    except Exception:
        logger.debug(f"{LOGGER_PREFIX} get_order_from_object не удался", exc_info=True)
    if full_order is not None:
        lid = getattr(full_order, "lot_id", None)
        if lid:
            return str(lid)
        for attr in ("html", "raw_html"):
            html = getattr(full_order, attr, "") or ""
            for pat in (r'data-offer="(\d+)"', r"offer\?id=(\d+)", r"offers/(\d+)"):
                m = re.search(pat, html)
                if m:
                    return m.group(1)
    html = getattr(order, "html", "") or ""
    for pat in (r'data-offer="(\d+)"', r"offer\?id=(\d+)", r"offers/(\d+)"):
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return None


def _order_quantity(mapping: dict, funpay_units: int) -> int:
    mult = float(mapping.get("qty_multiplier", 1.0) or 1.0)
    return max(1, int(round(int(funpay_units or 1) * mult)))


def _min_order_qty(svc: str) -> int | None:
    """Минимальный объём заказа для услуги steamsmm (лимит API).

    quantity в /order/price и /order/create — от 10 до 2000. Для похвалы CS2
    минимум 15 похвал одному профилю (тарифный минимум пакета steamsmm,
    максимум из friendly/teacher/leader от 15). Для авторегов свой минимум
    у каждого товара — там возвращаем None (проверка не применяется).
    """
    if str(svc).startswith("autoreg:"):
        return None
    if svc == "commend_cs2":
        return 15
    return 10


def _auto_lot_min_qty(s: dict, provider: dict | None, svc: str) -> int | None:
    """Минимум для авто-создаваемого лота: в описание и в привязку (min_qty).

    10 — обычные услуги (лимит API steamsmm), 15 — похвала CS2 (количество
    на кассе = число похвал одному профилю), автореги — min_count товара из
    каталога /autoreg/products. None, если минимум неизвестен (автореги без
    каталога).
    """
    base = _min_order_qty(svc)
    if base is not None:
        return base
    if str(svc).startswith("autoreg:") and provider is not None and \
            _provider_style(provider) == "rest":
        try:
            item = _client_for_provider(provider).autoreg_product(
                str(svc).split(":", 1)[-1])
        except Exception:
            return None
        if item is None:
            return None
        try:
            mc = int(item.get("min_count") or 0)
        except (TypeError, ValueError):
            return None
        return max(1, mc) if mc else None
    return None


def _mapping_cost(mapping: dict, qty: int) -> float | None:
    c = mapping.get("cost_per_unit")
    if c is None or c == "":
        return None
    try:
        return max(0.0, float(c) * qty)
    except Exception:
        return None


def _max_affordable_qty(settings: dict, mapping: dict, provider: dict | None) -> int | None:
    """Максимум единиц услуги, выполнимых по текущему балансу поставщика.

    floor(баланс / себестоимость 1 ед.). None — если баланс/цена неизвестны
    (тогда лимит не применяется).
    """
    if provider is None:
        return None
    bal = _balance_cached(provider)
    if bal is None:
        return None
    cost = _mapping_cost(mapping, 1)
    if cost in (None, 0):
        return None
    try:
        return max(0, int(float(bal) // float(cost)))
    except Exception:
        return None


# =========================================================================
# Состояние ожидания ссылки
# =========================================================================
#
# _waiting — рабочий кеш в памяти для быстрых хендлеров сообщений, но
# ИСТОЧНИК ПРАВДЫ — запись заказа в orders.json со статусом waiting_link.
# Каждая мутация _waiting зеркалится в поле wait записи (_persist_waiting),
# поэтому рестарт бота не теряет ни оплаченный заказ, ни визард-флаги
# (confirm/commend/cancel/retry восстанавливаются в _reconcile_waiting).

_waiting: dict[str, dict] = {}  # order_id -> waiting state
_waiting_lock = threading.Lock()

# chat_id -> callable(svc): продолжение визарда привязки после выбора service_id
# кнопкой «🎯 Каталог услуг» (вместо ручного ввода)
_wizard_svc_continue: dict[int, Any] = {}


def _w_to_wait(w: dict) -> dict:
    """Снимок состояния ожидающего заказа для записи в orders.json."""
    return {
        "mapping": copy.deepcopy(w.get("mapping") or {}),
        "units": w.get("units", 1),
        "confirm_pending": bool(w.get("confirm_pending")),
        "candidate_link": w.get("candidate_link"),
        "candidate_links": list(w.get("candidate_links") or []),
        "invalid_link_count": int(w.get("invalid_link_count", 0) or 0),
        "invalid_link_operator_notified": bool(w.get("invalid_link_operator_notified")),
        "attempt": w.get("attempt", 0),
        "retry_after": w.get("retry_after"),
        "commend_pending": bool(w.get("commend_pending")),
        "commend_targets": list(w.get("commend_targets") or []),
        "commend_targets_total": int(w.get("commend_targets_total", 1) or 1),
        "cancel_pending": bool(w.get("cancel_pending")),
        "cancel_order_id": w.get("cancel_order_id"),
        "cancel_provider_id": w.get("cancel_provider_id"),
        "cancel_provider_order_id": w.get("cancel_provider_order_id"),
        "add_claim_token": w.get("add_claim_token"),
    }


def _wait_to_w(record: dict) -> dict:
    """Восстанавливает рабочий w из waiting_link-записи orders.json."""
    wt = record.get("wait") or {}
    return {
        "chat_id": record.get("chat_id"),
        "order_id": record.get("order_id"),
        "buyer_id": record.get("buyer_id"),
        "mapping": wt.get("mapping") or {},
        "units": wt.get("units", 1),
        "sold_price": float(record.get("sold_price", 0) or 0),
        "confirm_pending": bool(wt.get("confirm_pending")),
        "candidate_link": wt.get("candidate_link"),
        "candidate_links": list(wt.get("candidate_links") or []),
        "invalid_link_count": int(wt.get("invalid_link_count", 0) or 0),
        "invalid_link_operator_notified": bool(wt.get("invalid_link_operator_notified")),
        "ts": float(record.get("created_at", 0) or 0) or time.time(),
        "attempt": wt.get("attempt", 0),
        "retry_after": wt.get("retry_after"),
        "commend_pending": bool(wt.get("commend_pending")),
        "commend_targets": list(wt.get("commend_targets") or []),
        "commend_targets_total": int(wt.get("commend_targets_total", 1) or 1),
        "cancel_pending": bool(wt.get("cancel_pending")),
        "cancel_order_id": wt.get("cancel_order_id"),
        "cancel_provider_id": wt.get("cancel_provider_id"),
        "cancel_provider_order_id": wt.get("cancel_provider_order_id"),
        "add_claim_token": wt.get("add_claim_token"),
    }


def _persist_waiting(w: dict) -> None:
    """Зеркалит текущее состояние ожидающего заказа в orders.json (поле wait)."""
    oid = str(w.get("order_id") or "")
    if not oid:
        return
    with _io_lock:
        orders = _load_orders()
        for o in orders:
            if str(o.get("order_id")) == oid and o.get("status") == "waiting_link":
                o["wait"] = _w_to_wait(w)
                break
        _save_orders(orders)


def _buyer_open_orders(buyer_id: Any) -> list[dict]:
    bid = str(buyer_id or "")
    return [o for o in _load_orders() if str(o.get("buyer_id")) == bid
            and o.get("status") not in ("success", "failure", "refunded")]


def _waiting_for_buyer(buyer_id: Any) -> list[dict]:
    bid = str(buyer_id or "")
    return [w for w in _waiting.values() if str(w.get("buyer_id")) == bid]


def _waiting_put(w: dict) -> None:
    _waiting[str(w.get("order_id"))] = w


def _waiting_pop(w: dict) -> None:
    _waiting.pop(str(w.get("order_id")), None)


def _record_status(order_id: Any) -> str | None:
    """Статус самой свежей записи заказа в orders.json (или None)."""
    oid = str(order_id or "")
    st = None
    for o in _load_orders():
        if str(o.get("order_id")) == oid:
            st = o.get("status")
    return st


def _promote_waiting_record(entry: dict) -> bool:
    """Переводит waiting_link-запись заказа в in_progress (True), либо False,
    если такой записи нет (старый формат/прямой вызов — тогда _record_order)."""
    oid = str(entry.get("order_id") or "")
    found = False
    with _io_lock:
        orders = _load_orders()
        for o in orders:
            if str(o.get("order_id")) == oid and o.get("status") == "waiting_link":
                created = o.get("created_at")
                o.update(entry)
                if created is not None:
                    o["created_at"] = created  # сохраняем время приёма заказа
                o["status"] = "in_progress"
                o.pop("wait", None)
                found = True
                break
        if found:
            _save_orders(orders)
    return found


# =========================================================================
# Заказы: запись и финализация
# =========================================================================

def _funpay_fee(sold_price: float, fee_percent: float) -> float:
    """Комиссия FunPay за продажу (по умолчанию ~7.5% с цены лота)."""
    try:
        pct = float(fee_percent or 0)
    except Exception:
        pct = 0.0
    return round(float(sold_price or 0) * pct / 100.0, 2)


def _net_profit(sold_price: float, cost: float, fee_percent: float) -> float:
    return round(float(sold_price or 0) - float(cost or 0) - _funpay_fee(sold_price, fee_percent), 2)


def _record_order(entry: dict) -> None:
    with _io_lock:
        orders = _load_orders()
        orders.append(entry)
        if len(orders) > 3000:
            orders = orders[-3000:]
        _save_orders(orders)


def _finalize_order(provider_order_id: Any, status: str, *, finalized_at: float | None = None,
                    provider_cost: float | None = None, cost_currency: str | None = None) -> bool:
    """Atomically finalize once. True means this call performed the transition."""
    changed = False
    order_id = None
    with _io_lock:
        orders = _load_orders()
        for o in orders:
            if str(o.get("provider_order_id")) == str(provider_order_id) and o.get("status") not in ("success", "failure", "refunded"):
                order_id = o.get("order_id")
                old_status = o.get("status")
                o["status"] = status
                o["finalized_at"] = finalized_at or time.time()
                sold = float(o.get("sold_price", 0) or 0)
                cost = float(o.get("provider_cost", 0) or 0)
                if provider_cost is not None:
                    o["provider_cost"] = float(provider_cost)
                    cost = float(provider_cost)
                if cost_currency is not None:
                    o["cost_currency"] = cost_currency
                settings = _load_settings()
                o["funpay_fee"] = _funpay_fee(sold, settings.get("funpay_fee_percent", 7.5))
                o["profit"] = _net_profit(sold, cost, settings.get("funpay_fee_percent", 7.5))
                changed = True
                break
        if changed:
            _save_orders(orders)
    if changed:
        _log_action("status_changed", f"заказ #{order_id}", old=old_status, new=status)
        _log_action({"success": "completed", "failure": "failed", "refunded": "refunded"}.get(status, status),
                    f"заказ #{order_id}", provider_order=provider_order_id)
    return changed


def _bump_refill_count(order_id: Any) -> int:
    """Возвращает счётчик доливов заказа после инкремента."""
    with _io_lock:
        orders = _load_orders()
        count = 0
        for o in orders:
            if str(o.get("order_id")) == str(order_id):
                count = int(o.get("refill_attempts", 0) or 0) + 1
                o["refill_attempts"] = count
                break
        _save_orders(orders)
        return count


def _active_records() -> list[dict]:
    # waiting_link — заказ ещё не размещён у поставщика, поллеру его опрашивать нечего.
    # auto-cancel confirmed остаётся активным до durable FunPay refund recovery.
    return [o for o in _load_orders()
            if o.get("status") not in ("success", "failure", "refunded", "waiting_link")]


def _canonical_progress_marker(data: dict) -> list[Any]:
    """Only provider-observed fields define a stuck epoch."""
    status = str(data.get("status", "")).strip().lower()
    remains = data.get("remains")
    progress = data.get("progress")
    return [status, remains, progress]


def _persist_auto_cancel_state(order_id: Any, mutate) -> dict | None:
    """Atomically mutate one order and durably save it before returning."""
    with _io_lock:
        orders = _load_orders()
        target = next((o for o in orders if str(o.get("order_id")) == str(order_id)), None)
        if target is None:
            return None
        mutate(target)
        _save_orders(orders)
        return dict(target)


def _auto_cancel_notify_once(cardinal: "Cardinal", rec: dict, result: str, error: str = "") -> None:
    state = rec.get("auto_cancel") or {}
    if state.get("operator_notified"):
        return
    def mark(item):
        ac = item.setdefault("auto_cancel", {})
        ac["operator_notified"] = True
        ac["operator_notified_at"] = time.time()
    saved = _persist_auto_cancel_state(rec.get("order_id"), mark)
    if saved is None:
        return
    _notify_operator(cardinal, f"⚠️ <b>Steam SMM</b>: автоотмена #{rec.get('order_id')} — "
                     f"<b>{result}</b>{': ' + _html_escape(error) if error else ''}. "
                     "Заказ оставлен активным, нужен ручной контроль.")


def _recover_confirmed_auto_cancel(cardinal: "Cardinal", settings: dict, rec: dict) -> bool:
    """Refund-only recovery. Never calls provider cancel."""
    ac = rec.get("auto_cancel") or {}
    if ac.get("result") != "confirmed" or ac.get("refunded_at"):
        return False
    try:
        cardinal.account.refund(rec.get("order_id"))
    except Exception as exc:
        def failed(item):
            state = item.setdefault("auto_cancel", {})
            state["refund_error"] = _short_err(exc)
            state["refund_failed_at"] = time.time()
        _persist_auto_cancel_state(rec.get("order_id"), failed)
        _log_action("auto_cancel_refund_failed", f"заказ #{rec.get('order_id')}", error=_short_err(exc))
        return True
    now = time.time()
    def refunded(item):
        state = item.setdefault("auto_cancel", {})
        state["refunded_at"] = now
        state.pop("refund_error", None)
        item["status"] = "refunded"
        item["refund_reason"] = "подтверждённая автоотмена зависшего заказа"
        item["finalized_at"] = now
    _persist_auto_cancel_state(rec.get("order_id"), refunded)
    remove_buyer_active_order(rec.get("buyer_id"), rec.get("order_id"))
    _log_action("auto_cancel_refunded", f"заказ #{rec.get('order_id')}")
    return True


def _auto_cancel_stuck(cardinal: "Cardinal", settings: dict, rec: dict,
                       marker: list[Any], changed_at: float, now: float) -> None:
    if not settings.get("auto_cancel_stuck_enabled", True):
        return
    threshold = float(settings.get("auto_cancel_stuck_sec", 2700) or 2700)
    if now - changed_at < threshold:
        return
    ac = rec.get("auto_cancel") or {}
    if ac.get("marker") == marker and ac.get("result"):
        return
    provider = _find_provider(settings, rec.get("provider_id"))
    unsupported = (not provider or not rec.get("provider_order_id") or
                   _provider_style(provider) == "rest" or provider.get("supports_cancel") is not True)
    claimed_at = now
    def claim(item):
        state = item.setdefault("auto_cancel", {})
        if state.get("marker") == marker and state.get("result"):
            raise RuntimeError("already claimed")
        state.clear()
        state.update({"result": "claimed", "claimed_at": claimed_at, "marker": marker,
                      "attempt_count": int(ac.get("attempt_count", 0) or 0) + 1,
                      "operator_notified": False})
    try:
        claimed = _persist_auto_cancel_state(rec.get("order_id"), claim)
    except RuntimeError:
        return
    if claimed is None:
        return
    _log_action("auto_cancel_claimed", f"заказ #{rec.get('order_id')}", marker=marker)
    if unsupported:
        result, error = "unsupported", "provider cancel unsupported"
    else:
        try:
            response = _client_for_provider(provider).cancel(rec.get("provider_order_id"))
            if not isinstance(response, dict):
                result, error = "indeterminate", "non-object response"
            elif response.get("error"):
                result, error = "failed", str(response.get("error"))
            elif response.get("ok") is True or response.get("success") is True or \
                    str(response.get("status", "")).lower() in ("success", "cancelled", "canceled"):
                result, error = "confirmed", ""
            else:
                result, error = "indeterminate", str(response)
        except Exception as exc:
            result, error = "indeterminate", _short_err(exc)
    finished = time.time()
    def store_result(item):
        state = item.setdefault("auto_cancel", {})
        state["result"] = result
        state[f"{result}_at"] = finished
        if error:
            state["error"] = error
    saved = _persist_auto_cancel_state(rec.get("order_id"), store_result)
    _log_action(f"auto_cancel_{result}", f"заказ #{rec.get('order_id')}", error=error or "-")
    if saved is None:
        return
    if result == "confirmed":
        _recover_confirmed_auto_cancel(cardinal, settings, saved)
    else:
        _auto_cancel_notify_once(cardinal, saved, result, error)


# =========================================================================
# Возврат + уведомления
# =========================================================================

# Страховка от авторефанда чужих заказов: сюда попадают order_id заказов,
# которые плагин подтвердил как свои (совпал mapping в _on_new_order).
# Если какой-то путь рефанда вызовет _do_refund для чужого заказа (лот другого
# плагина — Steam Rental, AutoRobux и т.п.), пишем предупреждение и НЕ
# возвращаем деньги. Set ограничен: при переполнении очищается.
_OWNED_ORDER_IDS: set[str] = set()
_OWNED_ORDER_IDS_MAX = 1000


def _normalize_order_id(order_id: Any) -> str:
    return str(order_id or "").strip().lstrip("#")


def _mark_owned_order(order_id: Any) -> None:
    """Помечает заказ как принадлежащий этому плагину (совпал mapping)."""
    oid = _normalize_order_id(order_id)
    if not oid:
        return
    with _io_lock:
        if len(_OWNED_ORDER_IDS) >= _OWNED_ORDER_IDS_MAX:
            _OWNED_ORDER_IDS.clear()
        _OWNED_ORDER_IDS.add(oid)


def _is_owned_order(order_id: Any) -> bool:
    """Заказ считается «своим», если он был сопоставлен mapping'у (в памяти),
    либо записан в orders.json / active.json / _waiting."""
    oid = _normalize_order_id(order_id)
    if not oid:
        return False
    with _io_lock:
        if oid in _OWNED_ORDER_IDS:
            return True
    try:
        for rec in _load_orders():
            if _normalize_order_id(rec.get("order_id")) == oid:
                return True
        for entry in _load_active().values():
            if not isinstance(entry, dict):
                continue
            if _normalize_order_id(entry.get("order_id") or entry.get("id")) == oid:
                return True
        with _waiting_lock:
            for w in _waiting.values():
                if _normalize_order_id(w.get("order_id")) == oid:
                    return True
    except Exception:
        logger.debug(f"{LOGGER_PREFIX} проверка «свой заказ» не удалась", exc_info=True)
    return False


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


def _do_refund(cardinal: "Cardinal", settings: dict, order_id: Any, chat_id: Any, reason: str,
               record: bool = True) -> None:
    order_url = f"https://funpay.com/orders/{order_id}/"
    # Страховка: никогда не возвращаем деньги за чужой лот (другого плагина).
    if not _is_owned_order(order_id):
        logger.warning(
            f"{LOGGER_PREFIX} ПРОПУЩЕН авторефанд #{order_id}: заказ не принадлежит "
            f"этому плагину (чужой лот?). Причина: {reason} {order_url}"
        )
        _notify_operator(
            cardinal,
            f"⚠️ <b>Steam SMM</b>: авторефанд #{order_id} пропущен — заказ не этого "
            f"плагина (чужой лот).\nПричина: {reason}\n{order_url}",
        )
        _log_action("refund_skipped_foreign", f"заказ #{order_id}", reason=reason)
        return
    if record:
        _record_refund(order_id, chat_id, reason)
    if settings.get("auto_refund", True):
        try:
            cardinal.account.refund(order_id)
            _log_action("refund", f"заказ #{order_id}", reason=reason)
            _log_action("refunded", f"заказ #{order_id}", reason=reason)
            _notify_operator(cardinal, f"💸 Авто-возврат по заказу #{order_id}.\nПричина: {reason}\n{order_url}")
        except Exception as e:
            _log_action("refund_failed", f"заказ #{order_id}", reason=reason,
                        error=_short_err(e))
            _notify_operator(cardinal, f"⚠️ Не удалось вернуть #{order_id}: {e}\nВерните вручную: {order_url}")
    else:
        _log_action("refund_manual", f"заказ #{order_id}", reason=reason)
        _notify_operator(cardinal, f"⚠️ Требуется ручной возврат #{order_id}.\nПричина: {reason}\n{order_url}")


def _record_refund(order_id: Any, chat_id: Any, reason: str) -> None:
    with _io_lock:
        orders = _load_orders()
        # помечаем существующий заказ, если он есть в истории и ещё не финализирован;
        # для уже помеченных refunded дописываем причину, если её не было
        existing = False
        for o in orders:
            if str(o.get("order_id")) == str(order_id):
                existing = True
                if o.get("status") not in ("success", "failure", "refunded"):
                    o["status"] = "refunded"
                    o["refund_reason"] = reason
                    o["finalized_at"] = time.time()
                elif o.get("status") == "refunded" and not o.get("refund_reason"):
                    o["refund_reason"] = reason
                break
        if not existing:
            orders.append({
                "order_id": str(order_id),
                "buyer_id": None,
                "chat_id": chat_id,
                "status": "refunded",
                "refund_reason": reason,
                "sold_price": 0.0,
                "provider_cost": 0.0,
                "profit": 0.0,
                "created_at": time.time(),
                "finalized_at": time.time(),
            })
        if len(orders) > 3000:
            orders = orders[-3000:]
        _save_orders(orders)


def _notify_new_order(cardinal: "Cardinal", order, mapping: dict) -> None:
    order_id = getattr(order, "id", None)
    buyer_name = getattr(order, "buyer_username", None)
    buyer_id = getattr(order, "buyer_id", None)
    amount = getattr(order, "amount", 1) or 1
    price = getattr(order, "price", 0) or 0
    currency = getattr(order, "currency", "") or ""
    lot_desc = getattr(order, "description", "") or getattr(order, "title", "") or ""
    buyer_line = f"{buyer_name}" if buyer_name else "—"
    if buyer_id is not None:
        buyer_line = f"{buyer_line} (id {buyer_id})"
    text = (
        "🔔 <b>Новый заказ</b>\n"
        f"🧾 Заказ FunPay: #{order_id}\n"
        f"👤 Покупатель: {buyer_line}\n"
        f"💰 Сумма: {price} {currency} (кол-во: {amount})\n"
        f"🗂 Лот: {_html_escape(lot_desc or (mapping or {}).get('lot_match', ''))}"
    )
    _notify_operator(cardinal, text)


# =========================================================================
# Размещение заказа у поставщика (с прибыль-гейтом)
# =========================================================================

def _short_err(e: Exception, limit: int = 200) -> str:
    """Короткое описание ошибки для сообщений оператору.

    Для ошибок FunPayAPI (RequestFailedError/LotSavingError) в конце str(e) лежит
    «Текст ответа: <json>» — там реальная причина от FunPay (error/errors/message).
    Показываем её, а не технические заголовки запроса.
    """
    text = str(e) or type(e).__name__
    marker = "Текст ответа:"
    if marker in text:
        body = text.split(marker, 1)[1].strip()
        try:
            import json as _json
            data = _json.loads(body)
            if isinstance(data, dict):
                for k in ("error", "errors", "message", "msg"):
                    v = data.get(k)
                    if v:
                        body = str(v)[:limit]
                        break
        except Exception:
            pass
        if body and body != "None":
            return body[:limit]
    return text[:limit]


def _service_id_for(mapping: dict, provider: dict) -> Any:
    """service_id: для REST-провайдера это код услуги (строка), для панели — число."""
    sid = mapping.get("service_id")
    if _provider_style(provider) == "rest":
        return str(sid or "")
    try:
        return int(sid)
    except (TypeError, ValueError):
        return sid


def _mapping_extras(mapping: dict) -> dict:
    out = {}
    for k in ("commend_friendly", "commend_teacher", "commend_leader",
              "comment_variant", "delay_min_minutes", "delay_max_minutes"):
        v = mapping.get(k)
        if v is not None and v != "":
            out[k] = v
    return out


# Пакет похвалы CS2 по умолчанию (friendly/teacher/leader) для авто-создания
# лота и запросов цены, когда в привязках параметры не заданы. Совпадает с
# примером из визарда привязки: максимум из значений (15) — нижняя граница
# тарифа steamsmm (максимум из friendly/teacher/leader от 15 до 5000).
_COMMEND_DEFAULT = {"commend_friendly": 15, "commend_teacher": 5, "commend_leader": 10}


def _commend_params(s: dict, mapping: dict | None = None) -> dict:
    """Разрешает параметры похвалы CS2 (friendly/teacher/leader) для запросов.

    Приоритет: параметры самой привязки → параметры любой другой привязки
    похвалы → дефолтный пакет. Всегда возвращает хотя бы одно значение > 0,
    чтобы авто-создание лота, проверка цены и размещение заказа не падали с
    «задайте friendly/teacher/leader».
    """
    candidates: list[dict | None] = [mapping]
    if s is not None:
        candidates.extend(
            m for m in s.get("lot_mappings", [])
            if str(m.get("service_id") or "") == "commend_cs2")
    for cand in candidates:
        if not cand:
            continue
        try:
            f = int(cand.get("commend_friendly") or 0)
            t = int(cand.get("commend_teacher") or 0)
            l = int(cand.get("commend_leader") or 0)
        except (TypeError, ValueError):
            continue
        if max(f, t, l) > 0:
            return {"commend_friendly": f, "commend_teacher": t, "commend_leader": l}
    return dict(_COMMEND_DEFAULT)


def _random_commend_package(base: dict) -> dict:
    """Случайный пакет похвалы CS2 для режима «🎲 Случайный пакет».

    Максимум из значений и сумма сохраняются как у базового пакета (цена
    стабильна, тарифный диапазон steamsmm не нарушается), а распределение по
    friendly/teacher/leader меняется при каждом заказе/цели.
    """
    def _v(k: str) -> int:
        try:
            return max(0, int(base.get(k) or 0))
        except (TypeError, ValueError):
            return 0

    vals = [_v("commend_friendly"), _v("commend_teacher"), _v("commend_leader")]
    scale = max(vals)
    total = sum(vals)
    if scale <= 0 or total <= 0:
        scale, total = 15, 30  # защита от невалидной базы
    rest = total - scale
    r1 = _rng.randint(0, rest)
    r2 = rest - r1
    keys = ["commend_friendly", "commend_teacher", "commend_leader"]
    big = _rng.choice(keys)
    out = {k: 0 for k in keys}
    out[big] = scale
    small = [k for k in keys if k != big]
    out[small[0]] = r1
    out[small[1]] = r2
    return out


def _commend_package_for_qty(s: dict, mapping: dict | None, qty: int,
                             base: dict | None = None) -> dict:
    """Пакет похвалы CS2 под количество заказа (похвалы ОДНОМУ профилю).

    Количество на кассе = сколько похвал получит один профиль. Базовый пакет
    привязки (friendly/teacher/leader) задаёт ПРОПОРЦИЮ, масштабируемую под
    qty. При включённом «🎲 Случайный пакет» пропорции случайные. При
    qty >= 15 гарантируется максимум пакета >= 15 — нижняя граница тарифа
    steamsmm (чтобы API не отклонял заказ).
    """
    if base is None:
        base = _commend_params(s, mapping)
    if mapping is not None and mapping.get("commend_random"):
        base = _random_commend_package(base)
    try:
        qty = max(1, int(qty))
    except (TypeError, ValueError):
        qty = 1
    total_base = max(1, sum(base.values()))
    keys = ("commend_friendly", "commend_teacher", "commend_leader")
    out = {k: int(round(base.get(k, 0) * qty / total_base)) for k in keys}
    # компенсировать погрешность округления, чтобы сумма вышла ровно qty
    diff = qty - sum(out.values())
    if diff:
        big = max(out, key=out.get)
        out[big] += diff
    # тарифный минимум: максимум пакета >= 15 при qty >= 15
    if qty >= 15:
        deficit = 15 - max(out.values())
        if deficit > 0:
            big = max(out, key=out.get)
            for k in sorted(out, key=out.get):
                if k == big or deficit <= 0:
                    continue
                take = min(out[k], deficit)
                out[k] -= take
                deficit -= take
            out[big] += 15 - max(out.values())
    for k in keys:
        out[k] = max(0, out[k])
    return out


def _live_cost(cardinal: "Cardinal", settings: dict, provider: dict,
               mapping: dict, qty: int, package: dict | None = None) -> float | None:
    """Актуальная себестоимость через API цены (order/price, commend/price).
    package — готовый пакет похвалы (масштабированный под qty); без него для
    похвалы используется базовый пакет привязки. Возвращает None, если
    поставщик не отдал цену — тогда берём cost_per_unit."""
    try:
        client = _client_for_provider(provider)
        extras = _mapping_extras(mapping)
        if str(mapping.get("service_id") or "") == "commend_cs2":
            extras.update(package or _commend_params(settings, mapping))
        data = client.price(_service_id_for(mapping, provider), qty, **extras)
        if isinstance(data, dict) and data.get("error"):
            return None
        total = data.get("total_cost")
        if total is None:
            total = data.get("price")
        if total is None:
            per = data.get("price_per_item")
            total = per * qty if per is not None else None
        if total is None:
            return None
        return max(0.0, float(total))
    except Exception:
        logger.debug(f"{LOGGER_PREFIX} price check не удался", exc_info=True)
        return None


def _requeue_commend(cardinal: "Cardinal", w: dict, settings: dict) -> None:
    server = settings.get("commend_server") or "185.9.145.248:24673"
    w["commend_pending"] = True
    w["retry_after"] = None
    with _waiting_lock:
        _waiting_put(w)
    _persist_waiting(w)
    _send_buyer(cardinal, w["chat_id"],
                f"🎮 Для похвалы CS2 игрок должен находиться на сервере <code>{_html_escape(server)}</code>.\n"
                f"Зайдите на сервер и напишите «готово».")


def _place_order(cardinal: "Cardinal", settings: dict, w: dict, link: str) -> None:
    order_id = w["order_id"]
    claim_token = f"{threading.get_ident()}:{time.time_ns()}"
    with _io_lock:
        orders = _load_orders()
        record = next((o for o in orders
                       if str(o.get("order_id")) == str(order_id)
                       and o.get("status") == "waiting_link"), None)
        if record is not None:
            wait = record.setdefault("wait", {})
            if wait.get("add_claim_token"):
                return
            wait["add_claim_token"] = claim_token
            w["add_claim_token"] = claim_token
            _save_orders(orders)
        elif _record_status(order_id) in ("in_progress", "success", "failure", "refunded"):
            if _record_status(order_id) == "refunded":
                _notify_operator(
                    cardinal,
                    f"⚠️ <b>Steam SMM</b>: заказ #{order_id} уже закрыт возвратом. "
                    f"Если поставщик получил заказ — Долите вручную: "
                    f"https://funpay.com/orders/{order_id}/",
                )
            return

    mapping = w["mapping"]
    provider = _find_provider(settings, mapping.get("provider_id"))
    chat_id = w["chat_id"]
    order_id = w["order_id"]
    buyer_id = w["buyer_id"]
    # этот заказ уже подтверждён как «наш» (в _on_new_order совпал mapping,
    # покупатель прислал ссылку) — помечаем, чтобы _do_refund не счёл его чужим
    _mark_owned_order(order_id)
    if not provider:
        _send_buyer(cardinal, chat_id, "❌ Поставщик не настроен. Свяжитесь с продавцом.")
        _do_refund(cardinal, settings, order_id, chat_id, reason="поставщик не найден")
        return

    # Похвала CS2: количество на кассе = число похвал ОДНОМУ профилю; разные
    # аккаунты — отдельными заказами (мульти-целевой режим убран)
    links = list(link) if isinstance(link, list) else [link]
    is_commend_multi = False
    qty = _order_quantity(mapping, w.get("units", 1))
    sold_price = float(w.get("sold_price", 0) or 0)

    # страховка: лимит количества по балансу поставщика
    cap = _max_affordable_qty(settings, mapping, provider)
    if cap is not None and qty > cap:
        _send_buyer(cardinal, chat_id,
                    f"⚠️ Максимум <b>{cap} шт</b> можно выполнить по текущему "
                    "балансу продавца. Средства будут возвращены.")
        _do_refund(cardinal, settings, order_id, chat_id,
                   reason=f"превышен лимит по балансу: {qty} > {cap}")
        return

    # пакет похвалы под количество заказа (пропорция из привязки, 🎲 — случайная)
    commend_package = None
    if str(mapping.get("service_id") or "") == "commend_cs2":
        commend_package = _commend_package_for_qty(settings, mapping, qty)

    expected_cost = _mapping_cost(mapping, qty)
    if settings.get("price_check_enabled", False):
        live = _live_cost(cardinal, settings, provider, mapping, qty,
                          package=commend_package)
        if live is not None:
            expected_cost = live
    if settings.get("profit_guard", True) and expected_cost is not None:
        min_profit = float(settings.get("min_profit", 0) or 0)
        fee = _funpay_fee(sold_price, settings.get("funpay_fee_percent", 7.5))
        if _net_profit(sold_price, expected_cost, settings.get("funpay_fee_percent", 7.5)) < min_profit:
            _send_buyer(cardinal, chat_id, "❌ Заказ невыгоден. Средства будут возвращены.")
            _do_refund(cardinal, settings, order_id, chat_id,
                       reason=f"прибыль-гейт: продано {sold_price}, себестоимость {expected_cost}, комиссия {fee}")
            return

    w["link"] = link
    client = _client_for_provider(provider)
    # Mandatory fresh, provider-local fail-closed gate.  A blocked order remains
    # durable in waiting_link/hold and is never refunded automatically.
    allowed, gate_reason, snapshot = _provider_balance_gate(provider, expected_cost, fresh=True)
    if not allowed:
        w["balance_hold"] = {"reason": gate_reason, "snapshot": snapshot, "at": time.time()}
        _persist_waiting(w)
        _save_settings(settings)
        _log_action("balance_precheck", f"заказ #{order_id}", outcome=gate_reason,
                    provider=provider.get("id"))
        if not w.get("balance_hold_notified"):
            _notify_operator(cardinal, f"⚠️ Steam SMM: #{order_id} удержан до пополнения/исправления баланса ({gate_reason}); возврат не выполнен.")
            w["balance_hold_notified"] = True
            _persist_waiting(w)
        _send_buyer(cardinal, chat_id, "⚠️ Заказ временно удержан поставщиком; продавец уведомлён.")
        return
    _log_action("balance_precheck", f"заказ #{order_id}", outcome="passed")
    attempt = int(w.get("attempt", 0) or 0)
    sid = _service_id_for(mapping, provider)
    extras = _mapping_extras(mapping)
    if str(mapping.get("service_id") or "") == "commend_cs2":
        # у похвалы пакет всегда разрешается и масштабируется под qty похвал
        # (пропорция из привязки; 🎲 — случайная) — иначе поставщик отклоняет
        # заказ без friendly/teacher/leader
        extras.update(commend_package or _commend_package_for_qty(settings, mapping, qty))

    resp = None
    provider_order_ids: list = []
    _log_action("add_attempt", f"заказ #{order_id}", provider=provider.get("id"), svc=mapping.get("service_id"), qty=qty)
    # похвала CS2: пред-проверка игрока до размещения — если не на сервере,
    # ничего не размещено, заказ ждёт «готово»
    if is_commend_multi and hasattr(client, "commend_check"):
        try:
            for target in links:
                client.commend_check(target)
        except CommendOffline:
            _requeue_commend(cardinal, w, settings)
            return
    try:
        for target in links:
            # обычные услуги — весь объём заказа одной ссылке; похвала — один
            # профиль получает пакет из extras, масштабированный под qty
            resp = client.add(sid, target, qty, **extras)
            if not isinstance(resp, dict) or not resp.get("order"):
                err = (resp or {}).get("error") if isinstance(resp, dict) else resp
                if provider_order_ids:
                    _notify_operator(
                        cardinal,
                        f"🚨 Steam SMM: #{order_id} — похвала размещена частично "
                        f"({len(provider_order_ids)} из {len(links)}: {provider_order_ids}), "
                        f"затем поставщик отклонил цель: {err}. "
                        f"Средства возвращаются; проверьте поставщика вручную.",
                    )
                _send_buyer(cardinal, chat_id, "❌ Поставщик отклонил заказ. Средства будут возвращены.")
                _do_refund(cardinal, settings, order_id, chat_id, reason=f"поставщик: {err}")
                return
            provider_order_ids.append(resp.get("order"))
    except CommendOffline:
        if provider_order_ids:
            # редкая гонка: пред-проверка прошла, но цель «упала» с сервера уже
            # после частичного размещения — fail-closed, без авто-повтора/возврата
            _log_action("add_ambiguous", f"заказ #{order_id}",
                        error="CommendOffline после частичного размещения")
            _notify_operator(
                cardinal,
                f"🚨 Steam SMM: размещение #{order_id} прервано «игрок не на сервере» "
                f"после {len(provider_order_ids)} из {len(links)} целей "
                f"({provider_order_ids}). Не повторять и не возвращать автоматически; "
                "проверьте поставщика вручную.",
            )
            _send_buyer(cardinal, chat_id, "⚠️ Результат размещения уточняется продавцом.")
        else:
            _requeue_commend(cardinal, w, settings)
        return
    except Exception as exc:
        with _io_lock:
            orders = _load_orders()
            item = next((item for item in orders
                         if str(item.get("order_id")) == str(order_id)), None)
            if item is None:
                item = {
                    "order_id": str(order_id), "buyer_id": str(buyer_id),
                    "chat_id": chat_id, "provider_id": provider.get("id"),
                    "provider_order_id": None, "service_id": mapping.get("service_id"),
                    "link": links[0], "qty": qty, "sold_price": sold_price,
                    "provider_cost": expected_cost or 0.0, "created_at": time.time(),
                }
                orders.append(item)
            item["status"] = "placement_unknown"
            item["placement_error"] = _short_err(exc)
            item["placement_unknown_at"] = time.time()
            if provider_order_ids:
                item["provider_order_ids"] = list(provider_order_ids)
                item["commend_links"] = list(links[:len(provider_order_ids)])
            _save_orders(orders)
        with _waiting_lock:
            _waiting.pop(str(order_id), None)
        _log_action("add_ambiguous", f"заказ #{order_id}", error=_short_err(exc))
        _notify_operator(cardinal, f"🚨 Steam SMM: размещение #{order_id} имеет неизвестный результат ({_short_err(exc)}). Не повторять и не возвращать автоматически; проверьте поставщика вручную.")
        _send_buyer(cardinal, chat_id, "⚠️ Результат размещения уточняется продавцом.")
        return

    fallback_used = False
    provider_order_id = provider_order_ids[0]
    _invalidate_balance_cache(str(provider.get("id")))
    _log_action("order_placed", f"заказ #{order_id}",
                provider=provider.get("id"), provider_order=provider_order_id,
                svc=mapping.get("service_id"), qty=qty,
                provider_orders=provider_order_ids if is_commend_multi else None)

    entry = {
        "order_id": str(order_id),
        "buyer_id": str(buyer_id),
        "chat_id": chat_id,
        "provider_id": provider.get("id"),
        "provider_order_id": provider_order_id,
        "service_id": mapping.get("service_id"),
        "link": links[0],
        "qty": qty,
        "sold_price": sold_price,
        "provider_cost": expected_cost or 0.0,
        "funpay_fee": _funpay_fee(sold_price, settings.get("funpay_fee_percent", 7.5)),
        "fallback_used": fallback_used,
        "status": "in_progress",
        "created_at": time.time(),
        "finalized_at": None,
    }
    if is_commend_multi:
        entry["provider_order_ids"] = list(provider_order_ids)
        entry["commend_links"] = list(links)
    if _promote_waiting_record(entry):
        pass
    else:
        existing = _record_status(str(order_id))
        if existing in ("in_progress", "success", "failure", "refunded"):
            # запись уже закрыта (таймаут-возврат успел раньше) или размещена
            # параллельно — дубликат не плодим, повторный возврат не делаем
            if existing == "refunded":
                _notify_operator(
                    cardinal,
                    f"⚠️ <b>Steam SMM</b>: заказ #{order_id} размещён у поставщика "
                    f"(ID <code>{provider_order_id}</code>), но запись уже закрыта возвратом. "
                    f"Долите вручную: https://funpay.com/orders/{order_id}/",
                )
            return
        # старый формат / прямой вызов без waiting_link-записи — добавляем новую
        _record_order(entry)
    active_entry = {
        "order_id_funpay": str(order_id),
        "provider_order_id": provider_order_id,
        "provider_id": provider.get("id"),
        "service_id": mapping.get("service_id"),
        "status": "processing",
        "created_at": time.time(),
    }
    if is_commend_multi:
        active_entry["provider_order_ids"] = list(provider_order_ids)
    set_buyer_active_order(buyer_id, active_entry)
    msg = _pick_variant(settings["messages"]["after_confirmation"]).format(provider_order_id=provider_order_id)
    msg += f"\n⏱ ETA: {_eta_text(mapping, resp if isinstance(resp, dict) else None)}"
    _send_buyer(cardinal, chat_id, msg)


# =========================================================================
# Автовыдача авторегов
# =========================================================================

def _account_pool(mapping: dict) -> list[dict]:
    tag = (mapping.get("pool_tag") or "").strip().lower()
    if not tag:
        return []
    return [a for a in _load_accounts() if (a.get("pool") or "").strip().lower() == tag]


def _find_free_account(mapping: dict) -> dict | None:
    for a in _account_pool(mapping):
        if a.get("sold"):
            continue
        if a.get("frozen"):
            continue
        return a
    return None


def _mark_account_sold(account: dict, order_id: Any, buyer_id: Any) -> None:
    with _io_lock:
        accounts = _load_accounts()
        for a in accounts:
            if a.get("id") == account.get("id"):
                a["sold"] = True
                a["sold_at"] = time.time()
                a["order_id"] = str(order_id)
                a["buyer_id"] = str(buyer_id)
                break
        _save_accounts(accounts)


def _buy_and_deliver_accounts(cardinal: "Cardinal", settings: dict, w: dict,
                             provider_id: Any, category_id: Any) -> None:
    """Покупка авторегов у REST-провайдера (autoreg/create) и выдача покупателю."""
    mapping = w["mapping"]
    chat_id = w["chat_id"]
    order_id = w["order_id"]
    buyer_id = w["buyer_id"]
    provider = _find_provider(settings, provider_id)
    if not provider or _provider_style(provider) != "rest":
        _send_buyer(cardinal, chat_id, _pick_variant(settings["messages"]["account_issue"]))
        _do_refund(cardinal, settings, order_id, chat_id,
                   reason="автореги: покупка недоступна у этого провайдера")
        return
    qty = max(1, int(w.get("units", 1) or 1))
    try:
        resp = _client_for_provider(provider).buy_autoregs(category_id, qty)
    except Exception as e:
        _send_buyer(cardinal, chat_id, _pick_variant(settings["messages"]["account_issue"]))
        _do_refund(cardinal, settings, order_id, chat_id, reason=f"ошибка покупки авторегов: {_short_err(e)}")
        return
    if isinstance(resp, dict) and resp.get("error"):
        _send_buyer(cardinal, chat_id, _pick_variant(settings["messages"]["account_issue"]))
        _do_refund(cardinal, settings, order_id, chat_id, reason=f"автореги: {resp.get('error')}")
        return
    accounts = resp.get("accounts") or []
    if not accounts:
        _send_buyer(cardinal, chat_id, _pick_variant(settings["messages"]["account_issue"]))
        _do_refund(cardinal, settings, order_id, chat_id, reason="автореги: поставщик не выдал аккаунты")
        return
    delivered = accounts[:qty]
    lines = ["🟩 <b>АККАУНТЫ ВЫДАНЫ!</b>", "━━━━━━━━━━━━━━━━━━"]
    for acc in delivered:
        parts = [x.strip() for x in str(acc).split(":")]
        login = parts[0] if parts else acc
        pwd = parts[1] if len(parts) > 1 else ""
        lines.append(f"🔑 Логин: <code>{_html_escape(login)}</code>")
        if pwd:
            lines.append(f"🔒 Пароль: <code>{_html_escape(pwd)}</code>")
        lines.append("")
    lines.append("⚠️ Пароли смените сразу после входа.")
    _send_buyer(cardinal, chat_id, "\n".join(lines))

    cost = _mapping_cost(mapping, len(delivered)) or 0.0
    sold = float(w.get("sold_price", 0) or 0)
    _record_order({
        "order_id": str(order_id),
        "buyer_id": str(buyer_id),
        "chat_id": chat_id,
        "provider_id": provider.get("id"),
        "provider_order_id": resp.get("order_id"),
        "account_id": None,
        "link": "",
        "qty": len(delivered),
        "sold_price": sold,
        "provider_cost": cost,
        "funpay_fee": _funpay_fee(sold, settings.get("funpay_fee_percent", 7.5)),
        "status": "success",
        "created_at": time.time(),
        "finalized_at": time.time(),
        "profit": _net_profit(sold, cost, settings.get("funpay_fee_percent", 7.5)),
    })
    pname = provider.get("name", provider.get("id"))
    _notify_operator(cardinal,
                     f"🟢 Куплено авторегов: {len(delivered)} по заказу #{order_id} "
                     f"(поставщик <b>{_html_escape(pname)}</b>, category {category_id}).")
    try:
        cardinal.send_message(chat_id, "✅ Не забудьте подтвердить заказ на FunPay.")
    except Exception:
        pass


def _deliver_account(cardinal: "Cardinal", settings: dict, w: dict) -> None:
    mapping = w["mapping"]
    chat_id = w["chat_id"]
    order_id = w["order_id"]
    buyer_id = w["buyer_id"]

    mode, provider_id, category_id = _choose_autoreg_mode(mapping)
    if mode == "provider":
        _buy_and_deliver_accounts(cardinal, settings, w, provider_id, category_id)
        return

    account = _find_free_account(mapping)
    if not account:
        _send_buyer(cardinal, chat_id, _pick_variant(settings["messages"]["account_issue"]))
        _do_refund(cardinal, settings, order_id, chat_id, reason="автореги закончились")
        return

    login = account.get("login", "")
    password = account.get("password", "")
    _mark_account_sold(account, order_id, buyer_id)

    lines = [
        "🟩 <b>АККАУНТ ВЫДАН!</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"🔑 Логин: <code>{_html_escape(login)}</code>",
        f"🔒 Пароль: <code>{_html_escape(password)}</code>",
        "",
        "⚠️ Пароль смените сразу после входа.",
    ]
    if account.get("note"):
        lines.append(f"📝 {_html_escape(account['note'])}")
    text = "\n".join(lines)
    _send_buyer(cardinal, chat_id, text)

    _record_order({
        "order_id": str(order_id),
        "buyer_id": str(buyer_id),
        "chat_id": chat_id,
        "provider_id": None,
        "provider_order_id": None,
        "account_id": account.get("id"),
        "link": "",
        "qty": 1,
        "sold_price": float(w.get("sold_price", 0) or 0),
        "provider_cost": 0.0,
        "status": "success",
        "created_at": time.time(),
        "finalized_at": time.time(),
        "profit": float(w.get("sold_price", 0) or 0),
    })
    _notify_operator(cardinal, f"🟢 Выдан авторег <code>{_html_escape(login)}</code> по заказу #{order_id}.")
    try:
        cardinal.send_message(chat_id, "✅ Не забудьте подтвердить заказ на FunPay.")
    except Exception:
        pass


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
        # ВАЖНО: сначала проверяем, что заказ относится к лотам ЭТОГО плагина.
        # Иначе при остановленных продажах (или ч/списке и т.п.) мы вернули бы
        # деньги за лоты других плагинов (Steam Rental, AutoRobux и т.д.).
        mapping = _match_mapping(settings, lot_id, lot_desc)
        if not mapping:
            logger.info(
                f"{LOGGER_PREFIX} Заказ #{order_id} игнорирован: лот не найден в конфиге. "
                f"lot_id={lot_id}, desc='{lot_desc[:120]}'."
            )
            _notify_operator(
                cardinal,
                f"⚠️ <b>Steam SMM</b>: заказ #{order_id} — лот не найден в конфиге.\n"
                f"lot_id=<code>{_html_escape(str(lot_id)) if lot_id else '—'}</code>\n"
                f"desc=<code>{_html_escape(lot_desc[:100])}</code>\n\n"
                f"Добавьте привязку в 🗺 Лоты ↔ услуги.",
            )
            _log_action("order_ignored", f"заказ #{order_id}", lot=lot_id or "—")
            return
        # страховка: с этого момента заказ считается «своим» — только его можно
        # авто-рефандить (см. _do_refund / _is_owned_order)
        _mark_owned_order(order_id)
        _log_action("order_received", f"заказ #{order_id}", lot=lot_id or "—")
        if settings.get("maintenance_mode", False):
            chat_id = _buyer_chat_id(cardinal, order)
            reason = "режим обслуживания"
            try:
                _record_refund(order_id, chat_id, reason)
            except PersistenceError:
                _notify_operator(cardinal, f"🚨 Steam SMM: #{order_id} отклонён обслуживанием, но состояние не сохранено; возврат не выполнен.")
                return
            _log_action("maintenance_rejected", f"заказ #{order_id}")
            _send_buyer(cardinal, chat_id, settings.get("maintenance_message") or DEFAULT_SETTINGS["maintenance_message"])
            _do_refund(cardinal, settings, order_id, chat_id, reason=reason, record=False)
            return
        if not settings.get("sales_enabled", True):
            chat_id = _buyer_chat_id(cardinal, order)
            logger.info(f"{LOGGER_PREFIX} Заказ #{order_id} отклонён: продажи остановлены")
            _send_buyer(cardinal, chat_id,
                        "🔴 Продажи временно остановлены. Средства будут возвращены.")
            _do_refund(cardinal, settings, order_id, chat_id,
                       reason="продажи остановлены", record=False)
            return

        svc = mapping.get("service_id")
        if svc and not _service_sales_enabled(settings, str(svc)):
            chat_id = _buyer_chat_id(cardinal, order)
            logger.info(f"{LOGGER_PREFIX} Заказ #{order_id} отклонён: услуга {svc} выключена")
            _send_buyer(cardinal, chat_id,
                        "🔴 Эта услуга временно недоступна. Средства будут возвращены.")
            _do_refund(cardinal, settings, order_id, chat_id,
                       reason=f"услуга {svc} выключена", record=False)
            return

        buyer_name = getattr(order, "buyer_username", None)
        if _is_blacklisted(settings, buyer_id=buyer_id, username=buyer_name):
            _log_action("blacklist_rejected", f"заказ #{order_id}", buyer=buyer_id)
            chat_id = _buyer_chat_id(cardinal, order)
            _send_buyer(cardinal, chat_id, "🚫 Покупки для вас недоступны. Средства будут возвращены.")
            _do_refund(cardinal, settings, order_id, chat_id,
                       reason=f"покупатель в чёрном списке (id {buyer_id}, {buyer_name})",
                       record=False)
            return

        if settings.get("new_order_notifications"):
            _notify_new_order(cardinal, order, mapping)

        # До трёх незакрытых заказов на покупателя.
        if len(_buyer_open_orders(buyer_id)) >= 3:
            _log_action("limit_rejected", f"заказ #{order_id}", limit=3)
            chat_id = _buyer_chat_id(cardinal, order)
            _send_buyer(cardinal, chat_id,
                        "⚠️ У вас уже 3 активных заказа. Дождитесь завершения одного из них.\n"
                        "Проверить: !статус [ID]")
            return

        chat_id = _buyer_chat_id(cardinal, order)
        qty = _order_quantity(mapping, int(amount))
        # Минимум для покупки (записан в описании авто-лота): меньше минимума —
        # покупателю пишется ошибка, БЕЗ возврата денег (отменить заказ может
        # сам покупатель на FunPay). Для похвалы CS2 минимум — 15 похвал
        # одному профилю (тарифный минимум пакета steamsmm).
        min_qty = mapping.get("min_qty")
        if str(mapping.get("service_id") or "") == "commend_cs2":
            min_qty = max(int(min_qty or 0), 15)
        if min_qty is not None and qty < int(min_qty):
            _log_action("min_qty_rejected", f"заказ #{order_id}",
                        qty=qty, min_qty=int(min_qty))
            _send_buyer(cardinal, chat_id,
                        f"⚠️ Минимальный заказ для этой услуги — <b>{int(min_qty)} шт</b>, "
                        f"вы оформили <b>{qty} шт</b>.\n"
                        "Оформите заказ не меньше минимума.")
            return
        # лимит количества по балансу steamsmm.ru (сколько единиц можно выполнить)
        cap = _max_affordable_qty(settings, mapping,
                                  _find_provider(settings, mapping.get("provider_id")))
        if cap is not None and qty > cap:
            _log_action("limit_rejected", f"заказ #{order_id}", qty=qty, cap=cap)
            _send_buyer(cardinal, chat_id,
                        f"⚠️ Максимум <b>{cap} шт</b> можно выполнить по текущему "
                        "балансу продавца. Средства будут возвращены.")
            _do_refund(cardinal, settings, order_id, chat_id,
                       reason=f"превышен лимит по балансу: {qty} > {cap}", record=False)
            return
        # Похвала CS2: количество на кассе = число похвал ОДНОМУ профилю.
        # Разные аккаунты — отдельными заказами (по одной ссылке на заказ).
        w = {
            "chat_id": chat_id,
            "order_id": order_id,
            "buyer_id": buyer_id,
            "mapping": mapping,
            "units": int(amount),
            "sold_price": float(price),
            "confirm_pending": False,
            "candidate_link": None,
            "candidate_links": [],
            "invalid_link_count": 0,
            "invalid_link_operator_notified": False,
            "commend_targets": [],
            "commend_targets_total": 1,
            "ts": time.time(),
        }
        if mapping.get("mode") == "account":
            _deliver_account(cardinal, settings, w)
            return

        # оплаченный заказ сразу попадает в orders.json (status=waiting_link):
        # рестарт бота не потеряет его, а _reconcile_waiting вернёт в память.
        _record_order({
            "order_id": str(order_id),
            "buyer_id": str(buyer_id),
            "chat_id": chat_id,
            "provider_id": mapping.get("provider_id"),
            "provider_order_id": None,
            "service_id": mapping.get("service_id"),
            "link": "",
            "qty": qty,
            "sold_price": float(price),
            "provider_cost": _mapping_cost(mapping, qty) or 0.0,
            "funpay_fee": _funpay_fee(float(price), settings.get("funpay_fee_percent", 7.5)),
            "status": "waiting_link",
            "created_at": time.time(),
            "finalized_at": None,
            "wait": _w_to_wait(w),
        })
        with _waiting_lock:
            _waiting_put(w)
        ask = _pick_variant(settings["messages"]["after_payment"])
        if str(mapping.get("service_id") or "") == "commend_cs2":
            ask += (f"\n\n🎮 Похвала CS2: <b>{qty} похвал</b> одному профилю.\n"
                    f"Пришлите ссылку на профиль "
                    f"(формат https://steamcommunity.com/...)\n"
                    f"Игрок должен быть на сервере в момент выполнения.")
        _send_buyer(cardinal, chat_id, ask)
    except Exception:
        logger.warning(f"{LOGGER_PREFIX} on_new_order error", exc_info=True)


def _buyer_chat_id(cardinal: "Cardinal", order) -> Any:
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

        low = text.lower()
        if low == "!помощь":
            _send_buyer(cardinal, chat_id,
                        "ℹ️ Команды покупателя:\n!помощь\n!статус [ID]\n!ссылка [ID]\n!отмена [ID]")
            return
        m_status = re.match(r"^!(?:статус|прогресс)(?:\s+(\S+))?$", text, re.IGNORECASE)
        if m_status:
            _cmd_status(cardinal, settings, author_id, chat_id, m_status.group(1))
            return

        m_link = re.match(r"^!ссылка(?:\s+(\S+))?$", text, re.IGNORECASE)
        if m_link:
            waits = _waiting_for_buyer(author_id)
            req_id = (m_link.group(1) or "").strip(" #№")
            link_wait = next((w for w in waits if str(w.get("order_id")) == req_id), None) if req_id else (waits[0] if len(waits) == 1 else None)
            if not req_id and len(waits) > 1:
                _send_buyer(cardinal, chat_id, "📋 Несколько заказов ждут ссылку: " + ", ".join("#" + str(w.get("order_id")) for w in waits) + ".\nУкажите: !ссылка ID")
                return
            if link_wait:
                if link_wait.get("confirm_pending"):
                    candidate = link_wait.get("candidate_link") or "—"
                    _send_buyer(cardinal, chat_id,
                                f"🔗 Текущая ссылка:\n{candidate}\n"
                                "Подтвердите «+» или отклоните «-».")
                else:
                    timeout = float(settings.get("link_wait_timeout_sec", 1200) or 1200)
                    remaining = max(0, int(timeout - (time.time() - float(link_wait.get("ts", time.time())))))
                    minutes, seconds = divmod(remaining, 60)
                    _send_buyer(cardinal, chat_id,
                                "❗ Пришлите Steam-ссылку в формате https://...\n"
                                "Например: https://steamcommunity.com/profiles/123456789/\n"
                                f"⏱ Осталось: {minutes} мин {seconds} сек.")
            elif get_buyer_active_order(author_id):
                _send_buyer(cardinal, chat_id, "✅ Ссылка уже принята, заказ выполняется.")
            else:
                _send_buyer(cardinal, chat_id, "ℹ️ Сейчас ссылка не ожидается.")
            return

        if low.startswith("!лот"):
            _cmd_lot_list(cardinal, settings, chat_id)
            return

        if low.startswith("!отмена") or low.startswith("!отменить") or low.startswith("!cancel"):
            _cmd_cancel(cardinal, settings, author_id, chat_id, text)
            return

        # команды оператора: статус/долив по ID у поставщика (включаются в настройках).
        # ID матчим по оригинальному тексту (не по low) — у поставщиков ID могут
        # быть регистрозависимыми (CM1 / API_x), а lower их ломает.
        if settings.get("operator_commands", False):
            m_check = re.match(r"^!?чек\s+(\S+)$", text, re.IGNORECASE)
            if m_check:
                _cmd_check(cardinal, settings, chat_id, m_check.group(1))
                return
            m_refill = re.match(r"^!?рефилл\s+(\S+)$", text, re.IGNORECASE)
            if m_refill:
                _cmd_refill(cardinal, settings, chat_id, m_refill.group(1))
                return

        waits = _waiting_for_buyer(author_id)
        if not waits:
            return
        if len(waits) > 1:
            _send_buyer(cardinal, chat_id,
                        "📋 Несколько заказов ждут ссылку: " +
                        ", ".join("#" + str(x.get("order_id")) for x in waits) +
                        ".\nНе отправляйте ссылку без ID: используйте !ссылка ID, затем отправьте ссылку.")
            return
        w = waits[0]

        if w.get("commend_pending"):
            if text.lower() in ("готово", "готов", "на сервере", "зашёл", "зашла",
                                "зашел", "зашла", "+", "да", "yes", "ок", "ok"):
                with _waiting_lock:
                    _waiting_pop(w)
                w.pop("commend_pending", None)
                w["attempt"] = 0
                _place_order(cardinal, settings, w, w.get("link") or "")
            else:
                _send_buyer(cardinal, chat_id, "🎮 Зайдите на сервер и напишите «готово».")
            return

        if w.get("retry_after"):
            # заказ уже в очереди на автоматический повтор — новые сообщения игнорируем
            return

        if w.get("cancel_pending"):
            low = text.lower()
            oid = w.get("cancel_order_id")
            if low in ("+", "да", "yes", "ок", "ok"):
                provider_order_id = w.get("cancel_provider_order_id")
                if provider_order_id:
                    result = _try_cancel_provider(cardinal, settings, {
                        "provider_id": w.get("cancel_provider_id"),
                        "provider_order_id": provider_order_id,
                        "order_id_funpay": oid,
                    })
                    if result != "success":
                        w["cancel_pending"] = False
                        _persist_waiting(w)
                        _send_buyer(cardinal, chat_id,
                                    "⚠️ Автоматическая отмена у поставщика не подтверждена. "
                                    "Заказ и средства оставлены без изменений; продавец решит вопрос вручную.")
                        _notify_operator(
                            cardinal,
                            f"⚠️ <b>Steam SMM</b>: отмена заказа #{_html_escape(oid)} у поставщика "
                            f"не подтверждена ({result}). Нужна ручная проверка; возврат не выполнен.",
                        )
                        return
                with _waiting_lock:
                    _waiting_pop(w)
                remove_buyer_active_order(author_id, oid)
                _send_buyer(cardinal, chat_id,
                            f"🚫 Заказ #{oid} отменён. Средства будут возвращены.")
                _do_refund(cardinal, settings, oid, chat_id, reason="покупатель отменил через !отмена")
                return
            with _waiting_lock:
                w["cancel_pending"] = False
                w.pop("cancel_order_id", None)
            _persist_waiting(w)
            _send_buyer(cardinal, chat_id, "✅ Отмена отменена, заказ продолжает выполняться.")
            return

        if w.get("confirm_pending"):
            low = text.lower()
            if low in ("+", "да", "yes", "ок", "ok"):
                # похвала CS2: одна ссылка (candidate_link); для обычных заказов тоже
                links = w.get("candidate_links") or [w.get("candidate_link")]
                with _waiting_lock:
                    _waiting_pop(w)
                _place_order(cardinal, settings, w, links)
                return
            if low in ("-", "нет", "no", "отмена", "cancel"):
                with _waiting_lock:
                    _waiting_pop(w)
                _send_buyer(cardinal, chat_id, "🚫 Отменено. Средства будут возвращены.")
                _do_refund(cardinal, settings, w["order_id"], chat_id, reason="покупатель отменил")
                return

        link = _is_valid_link(text, settings.get("allowed_link_domains", []), w.get("mapping"))
        if not link:
            with _waiting_lock:
                invalid_count = int(w.get("invalid_link_count", 0) or 0) + 1
                w["invalid_link_count"] = invalid_count
                notify_operator = invalid_count == 3 and not w.get("invalid_link_operator_notified")
                if notify_operator:
                    w["invalid_link_operator_notified"] = True
            _persist_waiting(w)
            if invalid_count <= 2:
                _send_buyer(
                    cardinal,
                    chat_id,
                    "❗ Нужна корректная ссылка в формате https://...\n"
                    "Например: https://steamcommunity.com/profiles/123456789/",
                )
            elif notify_operator:
                _notify_operator(
                    cardinal,
                    f"⚠️ <b>Steam SMM</b>: покупатель трижды прислал некорректную "
                    f"ссылку для заказа #{_html_escape(w.get('order_id'))}. Проверьте заказ вручную.",
                )
            return

        with _waiting_lock:
            w["invalid_link_count"] = 0
            w["invalid_link_operator_notified"] = False

        # Похвала CS2: одна ссылка на заказ (количество на кассе = число похвал
        # одному профилю). Разные аккаунты — отдельными заказами.
        if settings.get("confirm_link", True):
            with _waiting_lock:
                w["confirm_pending"] = True
                w["candidate_link"] = link
                _waiting_put(w)
            _persist_waiting(w)
            _send_buyer(cardinal, chat_id, f"🔗 Запускаем накрутку на:\n{link}\nВсё верно? Ответьте «+» или «-».")
        else:
            with _waiting_lock:
                _waiting_pop(w)
            _place_order(cardinal, settings, w, link)
    except Exception:
        logger.warning(f"{LOGGER_PREFIX} on_new_message error", exc_info=True)


def _fmt_log_entry(entry: Any) -> str:
    """Одна запись журнала похвалы: dict → читабельная строка, строка — как есть.
    Всё провайдер-контролируемое эскейпится для HTML (покупателю шлём с parse_mode)."""
    if isinstance(entry, dict):
        bits: list[str] = []
        for k in ("time", "ts", "date", "datetime"):
            if entry.get(k):
                bits.append(_html_escape(str(entry[k])))
                break
        bot = entry.get("bot_id") or entry.get("bot") or entry.get("steam_id")
        if bot:
            bits.append(_html_escape(f"бот {bot}"))
        if entry.get("status"):
            bits.append(_html_escape(str(entry["status"])))
        msg = entry.get("message") or entry.get("text") or entry.get("event") or ""
        if msg:
            bits.append(_html_escape(str(msg)))
        if not bits:
            return _html_escape(str(entry))
        return " · ".join(bits)
    return _html_escape(str(entry))


def _format_commend_status(data: dict, logs_limit: int = 8) -> str:
    """Детали похвалы CS2 для покупателя: прогресс, done/failed, журнал."""
    lines: list[str] = []
    progress = data.get("progress")
    if progress is not None and progress != "":
        lines.append(f"📊 Прогресс: {_html_escape(str(progress))}")
    done = data.get("done")
    failed = data.get("failed")
    if done is not None or failed is not None:
        parts = []
        if done is not None:
            parts.append(f"✅ выполнено: {done}")
        if failed is not None:
            parts.append(f"❌ неудачно: {failed}")
        lines.append(" | ".join(parts))
    rem = data.get("remains")
    if rem is not None:
        lines.append(f"📉 Осталось: {rem}")
    logs = data.get("logs") or []
    if isinstance(logs, list) and logs:
        lines.append("📜 Журнал:")
        for entry in logs[:max(1, min(30, logs_limit))]:
            lines.append(f"  • {_fmt_log_entry(entry)}")
    return "\n".join(lines)


def _active_order_card(bid: Any, a: dict, provider: dict | None,
                       data: dict | None = None) -> str:
    """Карточка активного заказа для меню «📦 Активные заказы» (HTML).

    Для похвалы CS2 (data['commend']) добавляет прогресс и журнал.
    data=None — статус не запрашивался (нет поставщика/ID).
    """
    poid = a.get("provider_order_id")
    if data is not None and data.get("error"):
        status_line = f"⚠️ {_html_escape(str(data['error']))}"
        detail = ""
    elif data is not None:
        st = _html_escape(str(data.get("status", "?")))
        if data.get("commend"):
            status_line = st
            detail = _format_commend_status(data)
        else:
            rem = data.get("remains")
            rem = _html_escape(str(rem)) if rem is not None else "?"
            status_line = f"{st} (осталось: {rem})"
            detail = ""
    else:
        status_line = "— (нет поставщика/ID)"
        detail = ""
    provider_name = _html_escape(provider.get("name")) if provider else \
        (_html_escape(str(a.get("provider_id") or "—")))
    text = (
        f"<b>🔎 Заказ покупателя {_html_escape(str(bid))}</b>\n"
        f"FunPay: #{_html_escape(str(a.get('order_id_funpay') or ''))}\n"
        f"Поставщик: {provider_name}\n"
        f"ID у поставщика: <code>{_html_escape(str(poid or '—'))}</code>\n"
        f"Статус: <code>{status_line}</code>\n"
        f"Создан: <code>{_fmt_ts(a.get('created_at'))}</code>"
    )
    if detail:
        text += f"\n{detail}"
    return text


def _cmd_lot_list(cardinal: "Cardinal", settings: dict, chat_id: Any) -> None:
    """Команда !лот — показать доступные услуги и лоты."""
    mappings = settings.get("lot_mappings", [])
    if not mappings:
        _send_buyer(cardinal, chat_id, "📋 Нет привязанных лотов. Доступные услуги:\n"
                    "• Комментарии +rep\n• Комментарии -rep\n• Случайные комментарии\n"
                    "• Премиум комментарии\n• Лайки\n• Дизлайки\n• Участники группы")
        return
    lines = ["📋 <b>Доступные лоты:</b>\n"]
    for m in mappings:
        svc = m.get("service_id", "?")
        lot_id = m.get("lot_id", "—")
        preset = LOT_PRESETS.get(svc, {})
        title = preset.get("title", svc)
        # убираем эмодзи для краткости
        clean = title.replace("🖤", "").replace("👍", "").replace("👎", "")
        clean = clean.replace("👥", "").replace("⭐", "").replace("🔥", "")
        clean = clean.strip()
        lines.append(f"• {clean} (лот #{lot_id})")
    _send_buyer(cardinal, chat_id, "\n".join(lines))



def _eta_text(mapping: dict | None = None, provider_data: dict | None = None) -> str:
    data = provider_data or {}
    eta = data.get("eta") or data.get("estimated_time") or data.get("estimated_completion")
    if eta is None and mapping:
        eta = mapping.get("eta")
    if eta in (None, ""):
        return "оценка недоступна"
    return f"примерно {eta} (это ориентир, точность не гарантируется)"

def _cmd_status(cardinal: "Cardinal", settings: dict, buyer_id: Any, chat_id: Any, order_id: Any = None) -> None:
    items = get_buyer_active_orders(buyer_id)
    if order_id is None and len(items) > 1:
        _send_buyer(cardinal, chat_id, "📋 Активные заказы: " + ", ".join("#" + str(x.get("order_id_funpay")) for x in items) + ".\nУкажите: !статус ID")
        return
    active = next((x for x in items if str(x.get("order_id_funpay")) == str(order_id).strip(" #№")), None) if order_id else (items[0] if items else None)
    if not active:
        _send_buyer(cardinal, chat_id, "ℹ️ У вас нет активных заказов.")
        return
    provider = _find_provider(settings, active.get("provider_id"))
    poid = active.get("provider_order_id")
    if not provider or not poid:
        _send_buyer(cardinal, chat_id, f"📊 Заказ #{active.get('order_id_funpay')} в обработке.")
        return
    multi_poids = _legacy_multi_ids(active)
    if len(multi_poids) > 1:
        # мульти-целевая похвала CS2: статус по каждой цели отдельно
        lines = [f"📊 Статус заказа #{active.get('order_id_funpay')} — похвала CS2 "
                 f"({len(multi_poids)} целей)"]
        for i, mp in enumerate(multi_poids, 1):
            try:
                data = _client_for_provider(provider).status(mp, commend=True)
                st = data.get("status", "?")
                lines.append(f"{i}. <code>{_html_escape(str(mp))}</code>: {_html_escape(str(st))}")
                if data.get("commend"):
                    detail = _format_commend_status(data)
                    if detail:
                        lines.append(f"   {detail}")
            except Exception:
                lines.append(f"{i}. <code>{_html_escape(str(mp))}</code>: ошибка статуса")
        _send_buyer(cardinal, chat_id, "\n".join(lines))
        return
    try:
        data = _client_for_provider(provider).status(
            poid, commend=(str(active.get("service_id") or "") == "commend_cs2"))
        st = data.get("status", "?")
        base = (f"📊 Статус заказа #{active.get('order_id_funpay')}\n"
                f"🔢 ID у поставщика: {poid}\n📈 Статус: {st}\n"
                f"⏱ ETA: {_eta_text(None, data)}")
        if data.get("commend"):
            detail = _format_commend_status(data)
            base = f"{base}\n{detail}" if detail else base
        else:
            rem = data.get("remains", "?")
            base = f"{base}\n📉 Осталось: {rem}"
        _send_buyer(cardinal, chat_id, base)
    except Exception:
        _send_buyer(cardinal, chat_id, "❌ Не удалось получить статус, попробуйте позже.")


def _try_cancel_provider(cardinal: "Cardinal", settings: dict, active: dict) -> str:
    """Возвращает unsupported/success/failed; успех требует явного подтверждения."""
    provider = _find_provider(settings, active.get("provider_id"))
    poid = active.get("provider_order_id")
    if not provider or not poid:
        return "unsupported"
    if _provider_style(provider) == "rest" or provider.get("supports_cancel") is not True:
        return "unsupported"
    try:
        resp = _client_for_provider(provider).cancel(poid)
        if not isinstance(resp, dict) or resp.get("error"):
            return "failed"
        ok = resp.get("ok") is True or resp.get("success") is True or \
            str(resp.get("status", "")).lower() in ("success", "cancelled", "canceled")
        return "success" if ok else "failed"
    except Exception:
        logger.warning(f"{LOGGER_PREFIX} provider cancel failed", exc_info=True)
        return "failed"


def _cmd_cancel(cardinal: "Cardinal", settings: dict, buyer_id: Any, chat_id: Any, text: str) -> None:
    items = get_buyer_active_orders(buyer_id)
    words = text.strip().split()
    req_id = words[1].strip(" #№") if len(words) > 1 else None
    if not req_id and len(items) > 1:
        _send_buyer(cardinal, chat_id, "📋 Активные заказы: " +
                    ", ".join("#" + str(x.get("order_id_funpay")) for x in items) +
                    ".\nУкажите: !отмена ID")
        return
    active = next((x for x in items if str(x.get("order_id_funpay")) == str(req_id)), None) if req_id else (items[0] if items else None)
    if not active:
        _send_buyer(cardinal, chat_id, "ℹ️ Активный заказ для отмены не найден.")
        return
    with _waiting_lock:
        w = next((x for x in _waiting_for_buyer(buyer_id)
                  if str(x.get("order_id")) == str(active.get("order_id_funpay"))), {})
        w.setdefault("buyer_id", buyer_id)
        w.setdefault("order_id", active.get("order_id_funpay"))
        w.setdefault("chat_id", chat_id)
        # снимаем retry-очередь/commend: подтверждение отмены не должно теряться
        w.pop("retry_after", None)
        w.pop("commend_pending", None)
        w["cancel_pending"] = True
        w["cancel_order_id"] = active.get("order_id_funpay")
        w["cancel_provider_id"] = active.get("provider_id")
        w["cancel_provider_order_id"] = active.get("provider_order_id")
        _waiting_put(w)
    _persist_waiting(w)
    _send_buyer(cardinal, chat_id,
                f"🚫 Запросить отмену заказа #{active.get('order_id_funpay')} и возврат средств?\n"
                f"Ответьте «+» или «-».")


def _cmd_check(cardinal: "Cardinal", settings: dict, chat_id: Any, provider_order_id: str) -> None:
    """Команда оператора: !чек <ID у поставщика> — статус заказа по ID.

    Всё провайдер-контролируемое эскейпится для HTML.
    """
    rec = next((o for o in _load_orders()
                if str(o.get("provider_order_id")) == str(provider_order_id)), None)
    provider = _find_provider(settings, rec.get("provider_id")) if rec else None
    if not provider:
        _send_buyer(cardinal, chat_id,
                    f"❌ Заказ с ID <code>{_html_escape(provider_order_id)}</code> "
                    f"не найден в истории (или поставщик удалён).")
        return
    try:
        data = _client_for_provider(provider).status(provider_order_id)
        if isinstance(data, dict) and data.get("error"):
            _send_buyer(cardinal, chat_id,
                        f"⚠️ <code>{_html_escape(str(data['error']))}</code>")
            return
        st = _html_escape(str(data.get("status", "?")))
        rem = data.get("remains")
        rem_s = _html_escape(str(rem)) if rem is not None else "?"
        _send_buyer(cardinal, chat_id,
                    f"📊 Статус заказа <code>{_html_escape(provider_order_id)}</code>: "
                    f"<b>{st}</b>\n📉 Осталось: {rem_s}")
    except Exception:
        _send_buyer(cardinal, chat_id, "❌ Не удалось получить статус, попробуйте позже.")


def _cmd_refill(cardinal: "Cardinal", settings: dict, chat_id: Any, provider_order_id: str) -> None:
    """Команда оператора: рефилл <ID у поставщика> — ручной долив заказа.

    Считает долив попыткой (refill_attempts), чтобы авто-логика partial не
    доливала повторно поверх ручного долива.
    """
    rec = next((o for o in _load_orders()
                if str(o.get("provider_order_id")) == str(provider_order_id)), None)
    provider = _find_provider(settings, rec.get("provider_id")) if rec else None
    if not provider:
        _send_buyer(cardinal, chat_id,
                    f"❌ Заказ с ID <code>{_html_escape(provider_order_id)}</code> не найден в истории.")
        return
    try:
        data = _client_for_provider(provider).refill(provider_order_id)
        if isinstance(data, dict) and data.get("error"):
            _send_buyer(cardinal, chat_id,
                        f"⚠️ Рефилл недоступен: <code>{_html_escape(str(data['error']))}</code>")
            return
        # успех — только при подтверждённом refill в ответе панели ({"refill": 1});
        # {"refill": "0"} (строка/число) / {"status": "failed"} без error-ключа — НЕ успех
        refill_val = data.get("refill") if isinstance(data, dict) else None
        ok = refill_val not in (None, "", 0, "0", False)
        if not ok:
            _send_buyer(cardinal, chat_id,
                        f"⚠️ Рефилл не подтверждён: <code>{_html_escape(str(data))}</code>")
            return
        if rec is not None:
            _bump_refill_count(rec.get("order_id"))
        _send_buyer(cardinal, chat_id,
                    f"🔄 Запрос на рефилл <code>{_html_escape(provider_order_id)}</code> отправлен. "
                    f"Ответ: <code>{_html_escape(str(data))}</code>")
    except Exception:
        _send_buyer(cardinal, chat_id, "❌ Ошибка при запросе рефилла.")


# =========================================================================
# Поллер статусов
# =========================================================================

# Кулдаун после 429 поставщика: не блокируем планировщик сном, а просто
# пропускаем опрос до окончания паузы (см. _poll_pass).
_poll_429_until: float = 0.0
_poll_backoff_attempt: int = 0


def _poll_pass(cardinal: "Cardinal") -> None:
    global _poll_429_until, _poll_backoff_attempt
    if time.time() < _poll_429_until:
        return
    settings = _load_settings()
    for rec in _active_records():
        ac = rec.get("auto_cancel") or {}
        if ac.get("result") == "claimed":
            def indeterminate(item):
                state = item.setdefault("auto_cancel", {})
                state["result"] = "indeterminate"
                state["indeterminate_at"] = time.time()
                state["error"] = "restart after durable claim; provider outcome unknown"
            saved = _persist_auto_cancel_state(rec.get("order_id"), indeterminate)
            if saved:
                _auto_cancel_notify_once(cardinal, saved, "indeterminate", "restart after claim")
            continue
        if ac.get("result") == "confirmed" and not ac.get("refunded_at"):
            _recover_confirmed_auto_cancel(cardinal, settings, rec)
            continue
        provider = _find_provider(settings, rec.get("provider_id"))
        if not provider:
            continue
        client = _client_for_provider(provider)
        # мульти-целевая похвала CS2: 1 FunPay-заказ = N заказов поставщика,
        # статусы агрегируются (все успешны / partial → restart / fail → возврат)
        multi_poids = _legacy_multi_ids(rec)
        if len(multi_poids) > 1:
            if not _poll_commend_multi(cardinal, settings, rec, provider,
                                       client, multi_poids):
                _poll_backoff_attempt += 1
                delay = _exp_backoff(_poll_backoff_attempt,
                                     cap=float(settings.get("max_backoff_sec", 3600)))
                _poll_429_until = time.time() + delay
                logger.info(f"{LOGGER_PREFIX} 429 при опросе, пауза {delay:.0f}s")
                return
            _poll_backoff_attempt = 0
            continue
        try:
            data = client.status(
                rec.get("provider_order_id"),
                commend=(str(rec.get("service_id") or "") == "commend_cs2"))
        except RateLimited:
            _poll_backoff_attempt += 1
            delay = _exp_backoff(_poll_backoff_attempt,
                                 cap=float(settings.get("max_backoff_sec", 3600)))
            _poll_429_until = time.time() + delay
            logger.info(f"{LOGGER_PREFIX} 429 при опросе, пауза {delay:.0f}s")
            return
        except Exception:
            logger.debug(f"{LOGGER_PREFIX} ошибка опроса заказа", exc_info=True)
            continue
        _poll_backoff_attempt = 0
        _handle_status_update(cardinal, settings, rec, data)


def _poll_commend_multi(cardinal: "Cardinal", settings: dict, rec: dict,
                        provider: dict, client, poids: list) -> bool:
    """Один проход опроса мульти-целевой похвалы CS2 (N заказов поставщика).

    Возвращает False при 429 (нужен backoff), иначе True. Терминальные
    переходы (все успешны / restart / возврат) выполняются ровно один раз:
    финализация — через _finalize_order по первому ID (атомарно)."""
    results: list[tuple[Any, dict]] = []
    for poid in poids:
        try:
            data = client.status(poid, commend=True)
        except RateLimited:
            return False
        except Exception:
            logger.debug(f"{LOGGER_PREFIX} ошибка опроса цели похвалы", exc_info=True)
            return True  # пропускаем проход, попробуем в следующем
        if not isinstance(data, dict):
            data = {"status": str(data)}
        results.append((poid, data))

    buyer_id = rec.get("buyer_id")
    chat_id = rec.get("chat_id")
    done = 0
    partial_poid: Any = None
    partial_raw = ""
    failed_poid: Any = None
    failed_raw = ""
    total_charge = 0.0
    charge_ok = True
    currency = None
    for poid, data in results:
        raw = str(data.get("status") or "").strip().lower()
        verdict = _classify_provider_status(raw, data.get("remains"))
        charge = data.get("charge")
        try:
            charge = float(charge) if charge not in (None, "") else None
        except (TypeError, ValueError):
            charge = None
        if charge is not None:
            total_charge += charge
        else:
            charge_ok = False
        currency = currency or data.get("currency")
        if verdict == "success" and "partial" not in raw:
            done += 1
        elif "partial" in raw:
            if partial_poid is None:
                partial_poid, partial_raw = poid, raw
        elif verdict == "failure":
            if failed_poid is None:
                failed_poid, failed_raw = poid, raw

    # все цели выполнены — успех
    if done == len(poids) and partial_poid is None and failed_poid is None:
        if not _finalize_order(poids[0], "success",
                               provider_cost=total_charge if charge_ok else None,
                               cost_currency=currency):
            return True
        remove_buyer_active_order(buyer_id, rec.get("order_id"))
        ids = ", ".join(str(p) for p in poids)
        _send_buyer(cardinal, chat_id,
                    _pick_variant(settings["messages"]["success"]).format(provider_order_id=ids))
        return True

    # цель не выполнена / отменена — restart остатка, потом возврат
    if failed_poid is not None:
        if _try_commend_restart(cardinal, settings, rec, provider,
                                failed_poid, failed_raw):
            return True
        if not _finalize_order(poids[0], "failure",
                               provider_cost=total_charge if charge_ok else None,
                               cost_currency=currency):
            return True
        remove_buyer_active_order(buyer_id, rec.get("order_id"))
        _send_buyer(cardinal, chat_id, _pick_variant(settings["messages"]["failure"]))
        _do_refund(cardinal, settings, rec.get("order_id"), chat_id,
                   reason=f"цель похвалы {failed_poid}: {failed_raw}")
        return True

    # частичное выполнение цели — restart этой цели, потом возврат
    if partial_poid is not None:
        if _try_commend_restart(cardinal, settings, rec, provider,
                                partial_poid, partial_raw):
            return True
        if not _finalize_order(poids[0], "refunded",
                               provider_cost=total_charge if charge_ok else None,
                               cost_currency=currency):
            return True
        remove_buyer_active_order(buyer_id, rec.get("order_id"))
        _send_buyer(cardinal, chat_id,
                    "⚠️ Похвала CS2 выполнена не полностью, перезапуск не удался. Средства будут возвращены.")
        _do_refund(cardinal, settings, rec.get("order_id"), chat_id,
                   reason="частичная похвала CS2, restart не удался", record=False)
        return True

    return True  # всё ещё в работе


def _bump_restart_count(order_id: Any) -> int:
    """Возвращает счётчик перезапусков похвалы CS2 после инкремента."""
    with _io_lock:
        orders = _load_orders()
        count = 0
        for o in orders:
            if str(o.get("order_id")) == str(order_id):
                count = int(o.get("restart_attempts", 0) or 0) + 1
                o["restart_attempts"] = count
                break
        _save_orders(orders)
        return count


def _try_commend_restart(cardinal: "Cardinal", settings: dict, rec: dict,
                         provider: dict, poid: Any, status_raw: str) -> bool:
    """Перезапуск невыполненного остатка похвалы CS2 (POST /commend/{id}/restart).

    True — restart отправлен, заказ остаётся в работе; False — нужен возврат средств
    (restart не удался или лимит попыток исчерпан).
    """
    already = int(rec.get("restart_attempts", 0) or 0)
    max_restarts = max(1, int(settings.get("commend_max_restarts", 2) or 1))
    if already >= max_restarts:
        _notify_operator(
            cardinal,
            f"⛔ <b>Steam SMM</b>: похвала #{rec.get('order_id')} (ID <code>{poid}</code>) — "
            f"restart исчерпан ({already}/{max_restarts}), статус «{status_raw}». Возврат средств.",
        )
        return False
    try:
        resp = _client_for_provider(provider).restart(poid)
        if isinstance(resp, dict) and resp.get("error"):
            raise RuntimeError(f"restart: {resp['error']}")
    except Exception as e:
        _notify_operator(
            cardinal,
            f"⚠️ <b>Steam SMM</b>: не удалось перезапустить похвалу #{rec.get('order_id')} "
            f"(ID <code>{poid}</code>): {e}. Верните средства или перезапустите вручную.",
        )
        return False
    _bump_restart_count(rec.get("order_id"))
    _send_buyer(cardinal, rec.get("chat_id"),
                "🔄 Похвала CS2 выполнена не полностью — запрошен перезапуск остатка, продолжаю следить.")
    _notify_operator(
        cardinal,
        f"🔄 <b>Steam SMM</b>: похвала #{rec.get('order_id')} (ID <code>{poid}</code>, "
        f"«{status_raw}») — отправлен restart. Ответ: <code>{resp}</code>",
    )
    return True


def _handle_status_update(cardinal: "Cardinal", settings: dict, rec: dict, data: dict) -> None:
    status_raw = str(data.get("status", "")) if isinstance(data, dict) else ""
    remains = None
    charge = None
    cost_currency = None
    if isinstance(data, dict):
        try:
            remains = int(data.get("remains")) if data.get("remains") is not None else None
        except Exception:
            remains = None
        try:
            charge = float(data.get("charge")) if data.get("charge") not in (None, "") else None
        except Exception:
            charge = None
        cost_currency = data.get("currency")

    verdict = _classify_provider_status(status_raw, remains)
    if verdict == "in_progress":
        # Error payloads are not successful observations and cannot advance timers.
        if not isinstance(data, dict) or data.get("error"):
            return
        now = time.time()
        marker = _canonical_progress_marker(data)
        old_marker = list(rec.get("progress_marker") or [])
        # Legacy two-field markers migrate without losing their existing epoch.
        comparable_old = old_marker if len(old_marker) == 3 else old_marker + [None]
        changed_at = float(rec.get("progress_changed_at") or now)
        alerted = bool(rec.get("stuck_alerted"))
        if marker != comparable_old:
            changed_at, alerted = now, False
        threshold = float(settings.get("stuck_threshold_sec", 1200) or 1200)
        if not alerted and now - changed_at >= threshold:
            _notify_operator(cardinal, f"⏳ Steam SMM: заказ #{rec.get('order_id')} не меняется {int(threshold)} секунд (статус {status_raw}, остаток {remains}).")
            _log_action("stuck_notified", f"заказ #{rec.get('order_id')}", status=status_raw, remains=remains)
            alerted = True
        def observe(item):
            item["progress_marker"] = marker
            item["progress_changed_at"] = changed_at
            item["stuck_alerted"] = alerted
            if marker != old_marker:
                item.pop("auto_cancel", None)
        saved = _persist_auto_cancel_state(rec.get("order_id"), observe)
        if saved is not None:
            _auto_cancel_stuck(cardinal, settings, saved, marker, changed_at, now)
        return

    buyer_id = rec.get("buyer_id")
    chat_id = rec.get("chat_id")
    poid = rec.get("provider_order_id")

    provider = _find_provider(settings, rec.get("provider_id"))
    is_commend_rest = (
        provider is not None
        and _provider_style(provider) == "rest"
        and str(rec.get("service_id") or "") == "commend_cs2"
    )

    is_partial = "partial" in (status_raw or "").strip().lower()
    if verdict == "success" and is_partial:
        client = _client_for_provider(provider) if provider else None
        if client is not None and is_commend_rest:
            # похвала CS2: каждый partial — перезапуск остатка (до лимита), потом возврат
            if _try_commend_restart(cardinal, settings, rec, provider, poid, status_raw):
                return
            if not _finalize_order(poid, "refunded", provider_cost=charge, cost_currency=cost_currency):
                return
            remove_buyer_active_order(buyer_id, rec.get("order_id"))
            _send_buyer(cardinal, chat_id,
                        "⚠️ Заказ выполнен частично, перезапуск не удался. Средства будут возвращены.")
            _do_refund(cardinal, settings, rec.get("order_id"), chat_id,
                       reason="частичная похвала CS2, restart не удался", record=False)
            return
        already = int(rec.get("refill_attempts", 0) or 0)
        if already > 0:
            if not _finalize_order(poid, "success", provider_cost=charge, cost_currency=cost_currency):
                return
            remove_buyer_active_order(buyer_id, rec.get("order_id"))
            _send_buyer(cardinal, chat_id,
                        "✅ Заказ долит повторно (долив отправлен). Пожалуйста, подтвердите заказ на FunPay.")
            return
        if client is not None:
            try:
                refill_resp = client.refill(poid)
                if isinstance(refill_resp, dict) and refill_resp.get("error"):
                    raise RuntimeError(f"refill: {refill_resp['error']}")
                _bump_refill_count(rec.get("order_id"))
                _send_buyer(cardinal, chat_id,
                            "🔄 Заказ выполнен частично — запрошен долив, продолжаю следить.")
                _notify_operator(
                    cardinal,
                    f"🔄 <b>Steam SMM</b>: заказ #{rec.get('order_id')} частичный "
                    f"(ID поставщика <code>{poid}</code>) — отправлен refill. "
                    f"Ответ: <code>{refill_resp}</code>",
                )
                return
            except Exception as e:
                _notify_operator(
                    cardinal,
                    f"⚠️ <b>Steam SMM</b>: не удалось долить #{rec.get('order_id')} "
                    f"(ID <code>{poid}</code>): {e}. Верните средства или долийте вручную.",
                )
                if not _finalize_order(poid, "refunded", provider_cost=charge, cost_currency=cost_currency):
                    return
                remove_buyer_active_order(buyer_id, rec.get("order_id"))
                _send_buyer(cardinal, chat_id,
                            "⚠️ Заказ выполнен частично, долив не удался. Средства будут возвращены.")
                _do_refund(cardinal, settings, rec.get("order_id"), chat_id,
                           reason="частичный заказ, долив не удался", record=False)
                return

    if verdict == "success":
        if not _finalize_order(poid, "success", provider_cost=charge, cost_currency=cost_currency):
            return
        remove_buyer_active_order(buyer_id, rec.get("order_id"))
        _send_buyer(cardinal, chat_id, _pick_variant(settings["messages"]["success"]).format(provider_order_id=poid))
    else:
        cancelled_commend = is_commend_rest and ("cancel" in (status_raw or "").lower())
        if cancelled_commend and _try_commend_restart(cardinal, settings, rec, provider, poid, status_raw):
            return
        if not _finalize_order(poid, "failure", provider_cost=charge, cost_currency=cost_currency):
            return
        remove_buyer_active_order(buyer_id, rec.get("order_id"))
        _send_buyer(cardinal, chat_id, _pick_variant(settings["messages"]["failure"]))
        reason = f"похвала CS2 отменена, restart не удался: {status_raw}" if cancelled_commend \
            else f"статус поставщика: {status_raw}"
        _do_refund(cardinal, settings, rec.get("order_id"), chat_id, reason=reason)


# =========================================================================
# Контроль баланса поставщиков (оповещение оператора)
# =========================================================================

# Предыдущее состояние алерта по провайдеру ("ok"/"low"/"error") — шлём только при переходе.
_balance_alert_state: dict[str, str] = {}


def _autopause_check(s: dict, cardinal: "Cardinal", provider: dict,
                     bal: float, threshold: float,
                     now: float | None = None) -> str:
    """Автопауза продаж при низком балансе (⏸️ Остановить продажи).

    При падении ниже порога ставит sales_enabled=False (один раз) и шлёт
    алерт; при восстановлении — автоматически запускает продажи, но только
    если пауза была выставлена этой автопаузой (auto_pause_active), чтобы
    не перезапускать продажи, остановленные оператором вручную.
    Период «торгуем остатком» (auto_pause_grace_until): после ручного
    запуска при низком балансе автопауза молчит N часов и не ставит паузу
    снова. Возвращает "pause" / "resume" / "none".
    """
    if not s.get("auto_pause_low_balance", True):
        return "none"
    if now is None:
        now = time.time()
    paused = bool(s.get("auto_pause_active", False))
    grace_until = float(s.get("auto_pause_grace_until") or 0)
    if bal < threshold and not paused:
        if now < grace_until:
            return "none"  # оператор вручную запустил — период «торгуем остатком»
        s["sales_enabled"] = False
        s["auto_pause_active"] = True
        _notify_operator(
            cardinal,
            f"⏸️ <b>Steam SMM</b>: баланс <b>{_html_escape(provider.get('name'))}</b> "
            f"<b>{bal:.2f}</b> ₽ ниже порога <b>{threshold:.2f}</b> — продажи "
            f"автоматически остановлены. Пополните баланс — продажи запустятся сами.",
        )
        return "pause"
    if bal >= threshold and paused:
        s["sales_enabled"] = True
        s["auto_pause_active"] = False
        s["auto_pause_grace_until"] = 0  # баланс восстановлен — грейс больше не нужен
        _notify_operator(
            cardinal,
            f"▶️ <b>Steam SMM</b>: баланс <b>{_html_escape(provider.get('name'))}</b> "
            f"восстановлен (<b>{bal:.2f}</b> ₽) — продажи автоматически запущены.",
        )
        return "resume"
    return "none"


def _manual_restart_grace(s: dict, bal: float | None) -> bool:
    """Взводит период «торгуем остатком» при ручном запуске продаж.

    Срабатывает, если баланс низкий или неизвестен (None). Если баланс в
    норме — грейс сбрасывается. Возвращает True, если грейс взведён.
    """
    grace_hours = float(s.get("auto_pause_grace_hours", 24.0) or 0)
    threshold = float(s.get("balance_alert_threshold", 50.0) or 0)
    if grace_hours <= 0:
        return False
    if bal is not None and bal >= threshold:
        s["auto_pause_grace_until"] = 0
        return False
    s["auto_pause_grace_until"] = time.time() + grace_hours * 3600
    return True


def _balance_pass(cardinal: "Cardinal") -> None:
    """Один проход контроля баланса (планировщик вызывает раз в час)."""
    s = _load_settings()
    threshold = float(s.get("balance_alert_threshold", 50.0) or 0)
    alerts_on = s.get("balance_alert_enabled", True)
    autopause_on = s.get("auto_pause_low_balance", True)
    # если выключены и алерты, и автопауза — не дёргаем API поставщиков
    if not alerts_on and not autopause_on:
        _balance_alert_state.clear()
        return
    for item in _provider_balance(s):
        p = item["provider"]
        pid = str(p.get("id") or "")
        prev = _balance_alert_state.get(pid)
        if item.get("error"):
            if alerts_on and prev != "error":
                _notify_operator(cardinal,
                                 f"⚠️ Не удалось проверить баланс <b>{_html_escape(p.get('name'))}</b>: {item['error']}")
            _balance_alert_state[pid] = "error"
            continue
        try:
            bal = float(item["data"].get("balance"))
        except Exception:
            continue
        new_state = "low" if bal < threshold else "ok"
        if alerts_on:
            if new_state == "low" and prev != "low":
                _notify_operator(
                    cardinal,
                    f"⚠️ <b>Steam SMM</b>: пополните баланс — у "
                    f"<b>{_html_escape(p.get('name'))}</b> осталось <b>{bal:.2f}</b> "
                    f"₽, порог <b>{threshold:.2f}</b> ₽. Лоты могут не оформляться.",
                )
            elif new_state == "ok" and prev == "low":
                _notify_operator(
                    cardinal,
                    f"✅ <b>Steam SMM</b>: у поставщика "
                    f"<b>{_html_escape(p.get('name'))}</b> баланс восстановлен: <b>{bal:.2f}</b>.",
                )
        _balance_alert_state[pid] = new_state

        # Автопауза продаж: стоп при низком балансе, авто-запуск при пополнении.
        # Работает независимо от balance_alert_enabled.
        if _autopause_check(s, cardinal, p, bal, threshold) != "none":
            _save_settings(s)


# =========================================================================
# Авто-цены по наценке + гейт баланса (гашение лотов)
# =========================================================================

def _mapping_markup(mapping: dict, settings: dict) -> float:
    m = mapping.get("markup_percent")
    if m is None or m == "":
        return float(settings.get("auto_lots_markup_percent", 30.0) or 0.0)
    try:
        return float(m)
    except Exception:
        return float(settings.get("auto_lots_markup_percent", 30.0) or 0.0)


def _compute_lot_price(cost_per_unit: float, markup_percent: float) -> float:
    return round(max(0.01, float(cost_per_unit) * (1 + markup_percent / 100.0)), 2)


def _funpay_unit_price(price: float, currency: Any) -> float:
    """Apply FunPay's minimum unit price only to outgoing RUB prices.

    Call this after all provider pricing, markup, conversion and rounding so
    non-RUB prices and valid RUB prices remain unchanged.
    """
    rounded = round(float(price), 2)
    return max(1.0, rounded) if _currency(currency) == "RUB" else rounded


def _lot_target_id(mapping: dict) -> int | None:
    t = mapping.get("target_lot_id")
    if t is None or t == "":
        return None
    try:
        return int(t)
    except (TypeError, ValueError):
        return None


def _get_lot_price(cardinal: "Cardinal", lot_id: int) -> float | None:
    account = getattr(cardinal, "account", None)
    if account is None:
        return None
    try:
        fields = account.get_lot_fields(int(lot_id))
        price = getattr(fields, "price", None)
        return float(price) if price is not None else None
    except Exception:
        logger.debug(f"{LOGGER_PREFIX} get_lot_price({lot_id}) неудачный", exc_info=True)
        return None


def _set_lot_price(cardinal: "Cardinal", lot_id: int, price: float) -> bool:
    account = getattr(cardinal, "account", None)
    if account is None:
        return False
    try:
        fields = account.get_lot_fields(int(lot_id))
        fields.price = price
        account.save_lot(fields)
        return True
    except Exception:
        logger.warning(f"{LOGGER_PREFIX} set_lot_price({lot_id}={price}) не удалось", exc_info=True)
        return False


def _set_lot_active(cardinal: "Cardinal", lot_id: int, active: bool) -> bool:
    account = getattr(cardinal, "account", None)
    if account is None:
        return False
    try:
        fields = account.get_lot_fields(int(lot_id))
        fields.active = bool(active)
        account.save_lot(fields)
        return True
    except Exception:
        logger.warning(f"{LOGGER_PREFIX} set_lot_active({lot_id}={active}) не удалось", exc_info=True)
        return False


def _check_provider_balance(provider: dict) -> tuple[bool, str]:
    """Проверяет ключ через баланс: (True, '123.45') или (False, 'причина')."""
    try:
        data = _client_for_provider(provider).balance()
        if isinstance(data, dict) and data.get("error"):
            return False, str(data["error"])
        try:
            bal = float(data.get("balance"))
        except Exception:
            return False, "не удалось разобрать ответ API"
        return True, f"{bal:.2f}"
    except Exception as e:
        return False, _short_err(e)


# Balance snapshots are provider-scoped and persisted in settings.  The memory
# cache mirrors them for compatibility with existing pricing/UI helpers.
_balance_cache: dict[str, dict] = {}
_balance_cache_lock = threading.Lock()
_BALANCE_CACHE_TTL = 300.0


def _currency(value: Any) -> str | None:
    value = str(value or "").strip().upper()
    return value or None


def _balance_snapshot(provider: dict, *, fresh: bool = False) -> dict:
    pid = str(provider.get("id") or "")
    now = time.time()
    if not fresh:
        with _balance_cache_lock:
            hit = _balance_cache.get(pid)
        if isinstance(hit, tuple):  # legacy tests/cache entries
            hit = {"amount": hit[0], "currency": provider.get("expected_currency", "RUB"),
                   "fetched_at": hit[1], "error": None}
        if hit and now - float(hit.get("fetched_at") or 0) < _BALANCE_CACHE_TTL:
            return copy.deepcopy(hit)
    snap = {"amount": None, "currency": None, "fetched_at": now, "error": None}
    try:
        data = _client_for_provider(provider).balance()
        if not isinstance(data, dict) or data.get("error"):
            raise ValueError(str((data or {}).get("error") if isinstance(data, dict) else "invalid response"))
        snap["amount"] = float(data.get("balance"))
        snap["currency"] = _currency(data.get("currency"))
        if snap["currency"] is None:
            raise ValueError("unknown currency")
        # Safe one-way migration for legacy providers: trust only an explicit
        # currency returned by the provider API, never a local default.
        if not _currency(provider.get("expected_currency")):
            provider["expected_currency"] = snap["currency"]
    except Exception as exc:
        snap["amount"] = None
        snap["error"] = _short_err(exc)
    with _balance_cache_lock:
        _balance_cache[pid] = copy.deepcopy(snap)
    provider["balance_snapshot"] = copy.deepcopy(snap)
    try:
        settings = _load_settings()
        stored = _find_provider(settings, pid)
        if stored is not None:
            stored["balance_snapshot"] = copy.deepcopy(snap)
            _save_settings(settings)
    except Exception:
        logger.warning(f"{LOGGER_PREFIX} balance snapshot persistence failed", exc_info=True)
    return snap


def _balance_cached(provider: dict) -> float | None:
    snap = _balance_snapshot(provider)
    return snap.get("amount") if not snap.get("error") else None


def _invalidate_balance_cache(provider_id: str | None = None) -> None:
    with _balance_cache_lock:
        if provider_id is None:
            _balance_cache.clear()
        else:
            _balance_cache.pop(str(provider_id), None)


def _provider_balance_gate(provider: dict, expected_cost: float | None, *, fresh: bool) -> tuple[bool, str, dict]:
    if not provider.get("low_balance_pause_enabled", True):
        return True, "disabled", _balance_snapshot(provider, fresh=fresh)
    snap = _balance_snapshot(provider, fresh=fresh)
    threshold = float(provider.get("min_balance", 0) or 0)
    expected = _currency(provider.get("expected_currency"))
    age = time.time() - float(snap.get("fetched_at") or 0)
    reason = ""
    if snap.get("error") or snap.get("amount") is None:
        reason = "balance_error"
    elif age > _BALANCE_CACHE_TTL:
        reason = "balance_stale"
    elif not expected or _currency(snap.get("currency")) != expected:
        reason = "currency_mismatch"
    elif float(snap["amount"]) <= threshold:
        reason = "at_or_below_threshold"
    elif expected_cost is None:
        reason = "cost_unknown"
    elif float(snap["amount"]) - float(expected_cost) < threshold:
        reason = "reserve_below_threshold"
    if not reason:
        provider["balance_hold"] = None
        return True, "ok", snap
    provider["balance_hold"] = {"reason": reason, "since": time.time(), "snapshot": copy.deepcopy(snap)}
    return False, reason, snap


def _current_lot_active(cardinal: "Cardinal", lot_id: int) -> bool:
    try:
        account = getattr(cardinal, "account", None)
        if account is None:
            return True
        fields = account.get_lot_fields(int(lot_id))
        return bool(getattr(fields, "active", True))
    except Exception:
        return True


def _auto_lots_pass_all(cardinal: "Cardinal") -> None:
    """Один проход авто-цен (планировщик вызывает по auto_lots_interval_min)."""
    settings = _load_settings()
    if not settings.get("auto_lots_enabled", False):
        return
    account = getattr(cardinal, "account", None)
    if account is None or not hasattr(account, "get_lot_fields") \
            or not hasattr(account, "save_lot"):
        return
    _auto_lots_pass(cardinal, settings)


def _auto_raise_pass(cardinal: "Cardinal", settings: dict) -> None:
    """Переактивация лотов (твист active=False→True) поднимает лот в топ выдачи FunPay."""
    for mapping in settings.get("lot_mappings", []):
        if mapping.get("mode") != "service":
            continue
        lot_id = _lot_target_id(mapping)
        if lot_id is None:
            continue
        try:
            if not _current_lot_active(cardinal, lot_id):
                continue
            if _set_lot_active(cardinal, lot_id, False) and _set_lot_active(cardinal, lot_id, True):
                _notify_operator(
                    cardinal,
                    f"⬆️ <b>Steam SMM</b>: лот <code>{lot_id}</code> поднят в топ "
                    f"(привязка «{_html_escape(mapping.get('lot_match') or '?')}»).",
                )
        except Exception:
            logger.debug(f"{LOGGER_PREFIX} raise lot {lot_id} не удался", exc_info=True)


def _autoreg_catalog_prices(provider: dict) -> dict[str, float]:
    """category_id -> price_per_item из каталога авторегов (пусто при ошибке/недоступности)."""
    try:
        prod = _client_for_provider(provider)._get("/autoreg/products")
    except Exception:
        logger.debug(f"{LOGGER_PREFIX} autoreg каталог недоступен "
                     f"(provider={provider.get('id')}, style={_provider_style(provider)})",
                     exc_info=True)
        return {}
    if not isinstance(prod, dict) or prod.get("error"):
        logger.debug(f"{LOGGER_PREFIX} autoreg каталог вернул ошибку: "
                     f"{(prod or {}).get('error') if isinstance(prod, dict) else prod!r}")
        return {}
    data = prod.get("data")
    if not isinstance(data, dict):
        logger.debug(f"{LOGGER_PREFIX} autoreg каталог: data не словарь: {data!r}")
        return {}
    out: dict[str, float] = {}
    for g in data.get("groups") or []:
        for it in (g.get("items") or []):
            cat = it.get("category_id")
            try:
                price = max(0.0, float(it.get("price_per_item")))
            except (TypeError, ValueError):
                continue
            out[str(cat)] = price
    return out


def _autoreg_live_cost(provider: dict, category_id: Any,
                       cache: dict[str, dict[str, float]]) -> float | None:
    """Актуальная себестоимость 1 авторега из каталога (кеш на один проход)."""
    pid = str(provider.get("id") or "")
    prices = cache.get(pid)
    if prices is None:
        prices = _autoreg_catalog_prices(provider)
        cache[pid] = prices
    return prices.get(str(category_id))


def _auto_lots_pass(cardinal: "Cardinal", settings: dict) -> None:
    """Один проход: цена по наценке + гейт баланса для service-привязок
    и account-покупок авторегов (себестоимость — живая цена из каталога)."""
    catalog_cache: dict[str, dict[str, float]] = {}
    for mapping in settings.get("lot_mappings", []):
        mode = mapping.get("mode", "service")
        lot_id = _lot_target_id(mapping)
        if lot_id is None:
            continue
        provider = _find_provider(settings, mapping.get("provider_id"))
        if not provider:
            continue

        if mode == "account":
            cat = mapping.get("autoreg_category_id")
            if not cat:
                continue
            cost = _autoreg_live_cost(provider, cat, catalog_cache)
            if cost is None:
                # каталог недоступен — фоллбэк на статичную себестоимость, если задана
                cost = _mapping_cost(mapping, 1)
                if cost is None:
                    continue
        else:
            cost = _mapping_cost(mapping, 1)
            if cost is None:
                continue

        gate_enabled = settings.get("balance_gate", True)
        allowed = True
        gate_reason = "disabled"
        snap = {"amount": None}
        if gate_enabled:
            allowed, gate_reason, snap = _provider_balance_gate(provider, cost, fresh=True)
        if not allowed:
            try:
                active = _current_lot_active(cardinal, lot_id)
            except Exception:
                active = True
            if active and _set_lot_active(cardinal, lot_id, False):
                amount = snap.get("amount")
                shown = f"{float(amount):.2f}" if amount is not None else "—"
                _notify_operator(
                    cardinal,
                    f"⚪️ <b>Steam SMM</b>: гейт поставщика "
                    f"<b>{_html_escape(provider.get('name'))}</b> заблокирован "
                    f"(<b>{_html_escape(gate_reason)}</b>, баланс <b>{shown}</b>) — "
                    f"лот <code>{lot_id}</code> скрыт.",
                )
            continue

        markup = _mapping_markup(mapping, settings)
        price = _compute_lot_price(cost, markup)
        current = _get_lot_price(cardinal, lot_id)
        if current is None or abs(current - price) > 0.01:
            if _set_lot_price(cardinal, lot_id, price):
                _set_lot_active(cardinal, lot_id, True)
                _notify_operator(
                    cardinal,
                    f"🏷 <b>Steam SMM</b>: лот <code>{lot_id}</code> → цена "
                    f"<b>{price:.2f}</b> (себестоимость <code>{cost}</code> × наценка "
                    f"<code>{markup:.0f}%</code>).",
                )
            continue

        try:
            active = _current_lot_active(cardinal, lot_id)
        except Exception:
            active = True
        if not active and _set_lot_active(cardinal, lot_id, True):
            _notify_operator(
                cardinal, f"⚪ <b>Steam SMM</b>: лот <code>{lot_id}</code> восстановлен (баланс ок).")


# =========================================================================
# Таймаут ожидания ссылки (задача планировщика)
# =========================================================================

def _timeout_pass(cardinal: "Cardinal") -> None:
    settings = _load_settings()
    timeout = float(settings.get("link_wait_timeout_sec", 86400))
    now = time.time()
    refunds = []  # (order_id, chat_id, bid)
    # 1) истёкшие waiting_link-записи в orders.json — включая те, что
    #    восстановились после рестарта (работает без _waiting вообще).
    #    Запись помечается refunded СРАЗУ под _io_lock: ни _promote_waiting_record,
    #    ни хендлер сообщения уже не смогут разместить заказ после решения о возврате.
    with _io_lock:
        orders = _load_orders()
        for o in orders:
            if o.get("status") != "waiting_link":
                continue
            created = float(o.get("created_at", 0) or 0)
            if created and now - created > timeout:
                o["status"] = "refunded"
                o["refund_reason"] = "покупатель не прислал ссылку"
                o["finalized_at"] = now
                refunds.append((o.get("order_id"), o.get("chat_id"), str(o.get("buyer_id") or "")))
        _save_orders(orders)
    # 2) убираем зеркальные записи из памяти (и зависшие/истёкшие без записи)
    with _waiting_lock:
        for _oid, _chat, bid in refunds:
            _waiting.pop(str(_oid), None)
        for bid, w in list(_waiting.items()):
            ra = w.get("retry_after")
            if ra and now > float(ra) + 600:
                # retry-задача не обработала (планировщик упал/завис) — освобождаем
                _waiting.pop(bid, None)
                refunds.append((w.get("order_id"), w.get("chat_id"), bid))
            elif not ra and not w.get("commend_pending") \
                    and now - float(w.get("ts", now) or now) > timeout:
                _waiting.pop(bid, None)
                refunds.append((w.get("order_id"), w.get("chat_id"), bid))
    seen: set = set()
    for oid, chat_id, _bid in refunds:
        if oid is None or str(oid) in seen:
            continue
        seen.add(str(oid))
        _send_buyer(cardinal, chat_id,
                    "⏳ Время ожидания ссылки истекло. Средства будут возвращены.")
        _do_refund(cardinal, settings, oid, chat_id, reason="покупатель не прислал ссылку")


def _reconcile_waiting(cardinal: "Cardinal") -> None:
    """При старте бота: восстанавливает ожидающие ссылку заказы в память
    и возвращает средства по зависшим (переживает рестарт)."""
    settings = _load_settings()
    timeout = float(settings.get("link_wait_timeout_sec", 86400))
    now = time.time()
    active = _load_active()
    for o in _load_orders():
        if o.get("status") != "waiting_link":
            continue
        oid = o.get("order_id")
        bid = str(o.get("buyer_id") or "")
        chat_id = o.get("chat_id")
        created = float(o.get("created_at", 0) or 0)
        if created and now - created > timeout:
            _send_buyer(cardinal, chat_id,
                        "⏳ Время ожидания ссылки истекло. Средства будут возвращены.")
            _do_refund(cardinal, settings, oid, chat_id,
                       reason="покупатель не прислал ссылку (зависший заказ при рестарте)")
            continue
        w = _wait_to_w(o)
        if not w.get("mapping") or not w.get("order_id"):
            continue
        if any(str(x.get("order_id_funpay")) == str(oid) for x in active.get(bid, [])) or str(oid) in _waiting:
            continue
        with _waiting_lock:
            _waiting_put(w)
        if not w.get("confirm_pending"):
            _send_buyer(cardinal, chat_id,
                        f"🔄 Бот перезапущен. Заказ #{oid} ещё ждёт ссылку — пришлите её, пожалуйста.")


# =========================================================================
# Очередь повторов размещения (задача планировщика, без сна в событийном потоке)
# =========================================================================

def _retry_pass(cardinal: "Cardinal") -> None:
    now = time.time()
    due: list[tuple[str, dict]] = []
    with _waiting_lock:
        for bid, w in list(_waiting.items()):
            ra = w.get("retry_after")
            if ra and now >= float(ra):
                due.append((bid, w))
                _waiting.pop(bid, None)
    if not due:
        return
    settings = _load_settings()
    for _bid, w in due:
        if w.get("commend_pending") or w.get("cancel_pending"):
            continue
        _place_order(cardinal, settings, w, w.get("link") or "")


# =========================================================================
# Планировщик фоновых задач (один поток вместо пяти)
# =========================================================================

_scheduler_thread: threading.Thread | None = None
_scheduler_stop = threading.Event()


def _auto_raise_pass_all(cardinal: "Cardinal") -> None:
    """Один проход поднятия лотов (по auto_raise_interval_min)."""
    settings = _load_settings()
    if not settings.get("auto_lots_enabled", False) or not settings.get("auto_raise_enabled", False):
        return
    _auto_raise_pass(cardinal, settings)


def _task_interval(name: str) -> float:
    """Интервал задачи в секундах (настраиваемые интервалы читаются заново).

    Битое значение в settings.json не должно убивать планировщик (раньше
    каждый тред умирал поодиночке, теперь поток один) — при ошибке парсинга
    подставляется дефолт 60 сек.
    """
    s = _load_settings()
    try:
        if name == "poll":
            return float(s.get("status_poll_interval_sec", 120))
        legacy_interval = _legacy_task_interval(s, name)
        if legacy_interval is not None:
            return legacy_interval
        if name == "timeout":
            return 60.0
        if name == "retry":
            return 5.0
    except (TypeError, ValueError):
        pass
    return 60.0


# (имя, функция) — порядок задаёт стартовые сдвиги, чтобы при запуске
# не бить по API поставщиков всем задачам сразу.
_SCHEDULED_TASKS: list[tuple[str, Any]] = [
    ("poll", _poll_pass),
    ("balance", _balance_pass),
    ("autolots", _auto_lots_pass_all),
    ("raise", _auto_raise_pass_all),
    ("timeout", _timeout_pass),
    ("retry", _retry_pass),
]


def _initial_delay(name: str, index: int) -> float:
    """Стартовая задержка задачи при запуске планировщика: poll — через полный
    интервал, остальные — с небольшим сдвигом, чтобы не бить по API всем сразу."""
    if name == "poll":
        return _task_interval(name)
    return 2.0 + index * 3.0


def _scheduler_loop(cardinal: "Cardinal") -> None:
    now0 = time.time()
    next_runs: dict[str, float] = {
        name: now0 + _initial_delay(name, i)
        for i, (name, _fn) in enumerate(_SCHEDULED_TASKS)
    }
    while not _scheduler_stop.is_set():
        now = time.time()
        for name, fn in _SCHEDULED_TASKS:
            if now < next_runs[name]:
                continue
            try:
                fn(cardinal)
            except Exception:
                logger.debug(f"{LOGGER_PREFIX} задача {name} упала", exc_info=True)
            next_runs[name] = time.time() + _task_interval(name)
        nearest = min(next_runs.values())
        _scheduler_stop.wait(min(5.0, max(0.2, nearest - time.time())))


def _ensure_scheduler(cardinal: "Cardinal") -> None:
    global _scheduler_thread
    if _scheduler_thread and _scheduler_thread.is_alive():
        return
    _scheduler_stop.clear()
    _scheduler_thread = threading.Thread(
        target=_scheduler_loop, args=(cardinal,), name="steamsmm-scheduler", daemon=True)
    _scheduler_thread.start()


# =========================================================================
# Меню (Telegram)
# =========================================================================

CBP = f"STEAMSMM_{UUID[:8]}"
CBT_TOGGLE_CONFIRM = f"{CBP}:tconfirm"
CBT_TOGGLE_REFUND = f"{CBP}:trefund"
CBT_TOGGLE_PROFIT = f"{CBP}:tprofit"
CBT_EDIT_MINPROFIT = f"{CBP}:minprofit"
CBT_TOGGLE_BALALERT = f"{CBP}:tbalalert"
CBT_EDIT_BALTHRESH = f"{CBP}:balthresh"
CBT_PROVIDERS = f"{CBP}:providers"
CBT_PROVIDER_BAL = f"{CBP}:pbal"
CBT_PROVIDER_SERVICES = f"{CBP}:pserv"
CBT_PROVIDER_ADD = f"{CBP}:padd"
CBT_PROVIDER_PRESET = f"{CBP}:ppreset"
CBT_PROVIDER_ADD_MANUAL = f"{CBP}:paddmanual"
CBT_PROVIDER_VIEW = f"{CBP}:pview"
CBT_PROVIDER_DEL = f"{CBP}:pdel"
CBT_PROVIDER_EDIT_NAME = f"{CBP}:pename"
CBT_PROVIDER_EDIT_URL = f"{CBP}:peurl"
CBT_PROVIDER_EDIT_KEY = f"{CBP}:pekey"
CBT_PROVIDER_EDIT_STYLE = f"{CBP}:pestyle"
CBT_PROVIDER_EDIT_MINBAL = f"{CBP}:pminbal"
CBT_PROVIDER_EDIT_CURRENCY = f"{CBP}:pcurrency"
CBT_PROVIDER_TOGGLE_LOWBAL = f"{CBP}:plowbal"
CBT_TOGGLE_PRICE = f"{CBP}:tprice"
CBT_SETS = f"{CBP}:sets"
CBT_MAPPINGS = f"{CBP}:mappings"
CBT_MAPPING_ADD = f"{CBP}:madd"
CBT_MAPPING_DEL = f"{CBP}:mdel"
CBT_MAPPING_MODE = f"{CBP}:mmode"
CBT_MAPPING_EXPORT = f"{CBP}:mexport"
CBT_MAPPING_IMPORT = f"{CBP}:mimport"
CBT_SVC_CATALOG = f"{CBP}:svccat"
CBT_SVC_PICK = f"{CBP}:svcpick"
CBT_SVC_CATALOG_BACK = f"{CBP}:svccatb"
CBT_AUTOREG_BIND = f"{CBP}:abind"
CBT_ACCOUNTS = f"{CBP}:accounts"
CBT_ACCOUNT_ADD = f"{CBP}:aadd"
CBT_ACCOUNT_DEL = f"{CBP}:adel"
CBT_ACCOUNT_IMPORT = f"{CBP}:aimport"
CBT_ACCOUNT_EXPORT = f"{CBP}:aexport"
CBT_MSGS = f"{CBP}:msgs"
CBT_MSG_SLOT = f"{CBP}:mslot"
CBT_MSG_ADD = f"{CBP}:msgadd"
CBT_MSG_DEL = f"{CBP}:msgdel"
CBT_LINKS = f"{CBP}:links"
CBT_LINK_ADD = f"{CBP}:ladd"
CBT_LINK_DEL = f"{CBP}:ldel"
CBT_LOGS = f"{CBP}:logs"
CBT_LOGS_CLEAR = f"{CBP}:logsclear"
CBT_LOGS_DOWNLOAD = f"{CBP}:logsdl"
CBT_DOMAINS = f"{CBP}:domains"
CBT_DOMAINS_EDIT = f"{CBP}:dedit"
CBT_STATS = f"{CBP}:stats"
CBT_ACTIVE = f"{CBP}:active"
CBT_HISTORY = f"{CBP}:history"
CBT_ORDER_DETAIL = f"{CBP}:odet"
CBT_BACKUP_EXPORT = f"{CBP}:bkp_exp"
CBT_BACKUP_IMPORT = f"{CBP}:bkp_imp"
CBT_ACTIVE_CLEAR = f"{CBP}:aclr"
CBT_ADVANCED = f"{CBP}:advanced"
CBT_HELP = f"{CBP}:help"
CBT_HOME = f"{CBP}:home"
CBT_TOGGLE_SALES = f"{CBP}:tsales"
CBT_TOGGLE_MAINTENANCE = f"{CBP}:tmaint"
CBT_SERVICES = f"{CBP}:services"
CBT_SERVICE_DETAIL = f"{CBP}:svc"
CBT_SERVICE_CREATE = f"{CBP}:svccreate"
CBT_SERVICE_CREATE_CONFIRM = f"{CBP}:svcconfirm"
CBT_SERVICE_DEL = f"{CBP}:svcdel"
CBT_SERVICE_TOGGLE_SALES = f"{CBP}:svcsales"
CBT_SERVICE_LOTID = f"{CBP}:svclotid"
CBT_SERVICE_LOTID_PICK = f"{CBP}:svclotidpick"
CBT_IMPORT = f"{CBP}:import"
CBT_IMPORT_SRC = f"{CBP}:import_src"
CBT_IMPORT_RESOLVE = f"{CBP}:import_resolve"
CBT_EXPORT = f"{CBP}:export"
CBT_PRICES = f"{CBP}:prices"
CBT_PRICES_CHECK = f"{CBP}:prices_check"
CBT_PRICES_RECALC = f"{CBP}:prices_recalc"
CBT_PRICES_SVC = f"{CBP}:prices_svc"
CBT_PRICES_SVC_DETAIL = f"{CBP}:prices_svcdet"
CBT_PRICES_MARGIN = f"{CBP}:prices_margin"
CBT_TOGGLE_ROUND = f"{CBP}:tround"
CBT_TOGGLE_AUTOPRICES = f"{CBP}:tautoprices"
CBT_EDIT_RECALC_INT = f"{CBP}:recalc_int"
CBT_TOGGLE_AUTOLOTS = f"{CBP}:tautolots"
CBT_EDIT_AUTOLOTS_INT = f"{CBP}:autolots_int"
CBT_EDIT_MARKUP = f"{CBP}:markup"
CBT_EDIT_LOT_NODE = f"{CBP}:lotnode"
CBT_TOGGLE_BALGATE = f"{CBP}:tbalgate"
CBT_TOGGLE_AUTOPAUSE = f"{CBP}:tautopause"
CBT_EDIT_GRACE = f"{CBP}:grace"
CBT_EDIT_BALINT = f"{CBP}:balint"
CBT_TOGGLE_NEWORDER = f"{CBP}:tneworder"
CBT_EDIT_TIMEOUT = f"{CBP}:timeout"
CBT_EDIT_POLL = f"{CBP}:poll"
CBT_MAPPING_LOT = f"{CBP}:mlot"
CBT_MAPPING_LOTHIDE = f"{CBP}:mlhide"
CBT_MAPPING_LOTCLEAR = f"{CBP}:mlotclear"
CBT_MAPPING_COMMEND = f"{CBP}:mcomm"
CBT_MAPPING_COMMEND_RANDOM = f"{CBP}:mcommr"
CBT_DELETE_ALL_LOTS = f"{CBP}:delall"
CBT_DELETE_ALL_LOTS_CONFIRM = f"{CBP}:delallc"
CBT_DELETE_ALL_LOTS_CANCEL = f"{CBP}:delallx"

# --- улучшения меню ---
CBT_BALANCES = f"{CBP}:balances"
CBT_DOMAINS_RESET = f"{CBP}:dreset"
CBT_ACTIVE_DETAIL = f"{CBP}:adet"
CBT_ACTIVE_CLEAR_CONFIRM = f"{CBP}:aclrc"
CBT_PROVIDER_DEL_CONFIRM = f"{CBP}:pdelc"
CBT_MAPPING_DEL_CONFIRM = f"{CBP}:mdelc"
CBT_MSG_DEL_CONFIRM = f"{CBP}:msgdelc"
CBT_LINK_DEL_CONFIRM = f"{CBP}:ldelc"
CBT_ACCOUNT_POOL = f"{CBP}:apool"
CBT_MSG_PREVIEW = f"{CBP}:mprev"
CBT_SETF = f"{CBP}:setf"
CBT_SETI = f"{CBP}:seti"
CBT_ACCOUNT_DEL_CONFIRM = f"{CBP}:adelc"
CBT_TOGGLE_RAISE = f"{CBP}:traise"
CBT_TOGGLE_OPCMDS = f"{CBP}:topcmds"
CBT_EDIT_RAISE_INT = f"{CBP}:raise_int"
CBT_EDIT_FEE = f"{CBP}:fee"
CBT_EDIT_RETRIES = f"{CBP}:retries"
CBT_EDIT_DESC_LIMIT = f"{CBP}:desclim"
CBT_BLACKLIST = f"{CBP}:blist"
CBT_BLACKLIST_ADD = f"{CBP}:blad"
CBT_BLACKLIST_DEL = f"{CBP}:bldel"
CBT_BLACKLIST_EXPORT = f"{CBP}:blexp"
# --- разделы «⚙️ Настройки» ---
CBT_ADV_PRICES = f"{CBP}:adv_prices"
CBT_ADV_BALANCE = f"{CBP}:adv_balance"
CBT_ADV_ORDERS = f"{CBP}:adv_orders"
CBT_ADV_EXTRA = f"{CBP}:adv_extra"
CBT_BLACKLIST_IMPORT = f"{CBP}:blimp"

_SLOT_LABELS = {
    "after_payment": "После оплаты",
    "after_confirmation": "После подтверждения",
    "success": "Успех",
    "failure": "Провал",
    "account_issue": "Проблема с авторегом",
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


def _managed_lot_mappings(settings: dict) -> list[dict]:
    """Только привязки с числовым ID лота, которыми управляет этот плагин."""
    result = []
    for mapping in settings.get("lot_mappings", []):
        lot_id = _lot_target_id(mapping)
        if lot_id is not None:
            result.append(mapping)
    return result


def _delete_all_state_start(chat_id: int, now: float | None = None) -> dict:
    state = {"token": secrets.token_urlsafe(8), "step": 1,
             "created_at": float(time.time() if now is None else now)}
    _delete_all_states[int(chat_id)] = state
    return state


def _delete_all_state_advance(chat_id: int, token: str, expected_step: int,
                              now: float | None = None) -> dict | None:
    state = _delete_all_states.get(int(chat_id))
    current = float(time.time() if now is None else now)
    if not state or state.get("token") != token or state.get("step") != expected_step \
            or current - float(state.get("created_at", 0)) > _DELETE_ALL_STATE_TTL:
        _delete_all_states.pop(int(chat_id), None)
        return None
    state["step"] = expected_step + 1
    return state


def _delete_all_state_cancel(chat_id: int) -> None:
    _delete_all_states.pop(int(chat_id), None)


def _lot_label(mapping: dict, lot_id: int) -> str:
    return str(mapping.get("lot_match") or mapping.get("title") or lot_id).strip()


def _delete_lot_confirmed(cardinal: "Cardinal", lot_id: int) -> tuple[bool, str]:
    """Удаляет лот штатной операцией FunPayAPI и подтверждает результат чтением.

    Если API не умеет delete_lot, безопасно деактивирует и также проверяет
    active=False. Не считает один лишь успешный POST подтверждением.
    """
    account = getattr(cardinal, "account", None)
    if account is None:
        return False, "нет доступа к FunPay-аккаунту"
    try:
        fields = account.get_lot_fields(int(lot_id))
    except Exception as exc:
        return False, f"не удалось прочитать лот: {_short_err(exc)}"
    if not bool(getattr(fields, "active", False)):
        return True, "уже неактивен; привязка очищена"
    delete = getattr(account, "delete_lot", None)
    if callable(delete):
        try:
            delete(int(lot_id))
            try:
                check = account.get_lot_fields(int(lot_id))
            except Exception:
                return True, "удалён (лот больше не читается)"
            if not bool(getattr(check, "active", False)):
                return True, "удалён штатной операцией FunPay"
            return False, "FunPay не подтвердил удаление: лот всё ещё активен"
        except Exception as exc:
            return False, f"ошибка удаления: {_short_err(exc)}"
    try:
        fields.active = False
        account.save_lot(fields)
        check = account.get_lot_fields(int(lot_id))
        if bool(getattr(check, "active", True)):
            return False, "FunPay не подтвердил деактивацию"
        return True, "деактивирован (API не поддерживает удаление)"
    except Exception as exc:
        return False, f"ошибка деактивации: {_short_err(exc)}"


def _safe_orphan_candidates(cardinal: "Cardinal", settings: dict) -> list[dict]:
    """Ищет только однозначные plugin-created лоты без mapping.

    Требуются одновременно: известная категория авто-лотов и точное
    нормализованное совпадение заголовка с LOT_PRESETS. Никаких fuzzy-совпадений.
    """
    account = getattr(cardinal, "account", None)
    node_ids = {int(x) for x in ([settings.get("auto_lot_node_id")] +
                [m.get("funpay_node_id") for m in settings.get("lot_mappings", [])])
                if str(x or "").isdigit() and int(x) > 0}
    if account is None or not node_ids:
        return []
    tracked = {_lot_target_id(m) for m in settings.get("lot_mappings", [])}
    titles = {_normalize_lot_text(p.get(k)): svc for svc, p in LOT_PRESETS.items()
              for k in ("title", "title_en") if p.get(k)}
    found = []
    for node_id in sorted(node_ids):
        lots = _my_subcategory_lots(account, node_id) or {}
        for lot_id, title in lots.items():
            try: lot_id = int(lot_id)
            except (TypeError, ValueError): continue
            svc = titles.get(_normalize_lot_text(title))
            if svc and lot_id not in tracked:
                found.append({"lot_id": lot_id, "title": str(title),
                              "service_id": svc, "node_id": node_id})
    return found


def _delete_managed_lots(cardinal: "Cardinal", settings: dict,
                         include_orphans: bool = False) -> dict:
    """Удаляет/деактивирует tracked лоты; mapping очищает только после проверки."""
    managed = _managed_lot_mappings(settings)
    by_id: dict[int, dict] = {}
    for mapping in managed:
        by_id.setdefault(_lot_target_id(mapping), mapping)
    if include_orphans:
        for orphan in _safe_orphan_candidates(cardinal, settings):
            by_id.setdefault(orphan["lot_id"], orphan)
    successful_ids: set[int] = set()
    statuses: list[dict] = []
    tracked_ids = {_lot_target_id(m) for m in managed}
    for lot_id, mapping in by_id.items():
        ok, detail = _delete_lot_confirmed(cardinal, lot_id)
        statuses.append({"lot_id": lot_id, "label": _lot_label(mapping, lot_id),
                         "ok": ok, "detail": detail,
                         "orphan": lot_id not in tracked_ids})
        if ok:
            successful_ids.add(lot_id)
    retained, cleared = [], 0
    for mapping in settings.get("lot_mappings", []):
        if _lot_target_id(mapping) in successful_ids:
            cleared += 1
        else:
            retained.append(mapping)
    if cleared:
        settings["lot_mappings"] = retained
        _save_settings(settings)
    return {"tracked": len(tracked_ids), "processed": len(by_id),
            "succeeded": len(successful_ids), "bindings_cleared": cleared,
            "statuses": statuses,
            "failed": [(x["lot_id"], x["detail"]) for x in statuses if not x["ok"]],
            "orphans": sum(1 for x in statuses if x["orphan"])}

def _delete_all_confirm_text(step: int, count: int) -> str:
    return (f"<b>⚠️ Удаление всех лотов плагина — {step}/3</b>\n\n"
            f"Будут удалены штатной операцией FunPay <b>только</b> отслеживаемые "
            f"Steam SMM лоты (<code>{count}</code> шт.). Если удаление недоступно, "
            "лот будет деактивирован и останется видимым в «Ваших предложениях», "
            "но не будет продаваться. Привязка очищается только после проверки.\n"
            "Чужие лоты FunPay затронуты не будут. Действие разрушительное.")


def _delete_all_confirm_kb(step: int, token: str) -> "K":
    kb = K(row_width=1)
    kb.add(B(f"⚠️ Подтверждаю удаление ({step}/3)",
             callback_data=f"{CBT_DELETE_ALL_LOTS_CONFIRM}:{step}:{token}"))
    kb.add(B("❌ Отмена", callback_data=f"{CBT_DELETE_ALL_LOTS_CANCEL}:{token}"))
    kb.add(B("◀️ Назад", callback_data=f"{CBT_DELETE_ALL_LOTS_CANCEL}:{token}"))
    return kb


def _active_lots_count(s: dict) -> int:
    """Число привязанных лотов (mappings с target_lot_id)."""
    return sum(1 for m in s.get("lot_mappings", []) if m.get("target_lot_id") not in (None, ""))


def _service_state(s: dict, svc: str) -> bool:
    """Услуга активна, если есть хотя бы одна привязка с этим service_id
    и хотя бы с одним target_lot_id (реально созданным лотом)."""
    for m in s.get("lot_mappings", []):
        if str(m.get("service_id") or "") == str(svc) and \
                m.get("target_lot_id") not in (None, ""):
            return True
    return False


def _service_sales_enabled(s: dict, svc: str) -> bool:
    """Тумблер продаж услуги (включён по умолчанию)."""
    return bool(s.get("services_enabled", {}).get(svc, True))


def _set_service_sales(s: dict, svc: str, enabled: bool) -> None:
    s.setdefault("services_enabled", {})[svc] = bool(enabled)


def _volume_margin(s: dict, svc: str, volume: int,
                   cache: dict | None = None) -> float | None:
    """Маржа объёма услуги в процентах (или None, если себестоимость недоступна)."""
    b = _volume_breakdown(s, _find_default_provider(s), svc, volume, cache)
    return float(b["margin"]) if b is not None else None


def _price_fail_reason(svc: str, volume: int, mapping: dict | None) -> str:
    """Локальная причина, по которой живой запрос цены заведомо не удастся.

    Сейчас всегда пустая строка: объёмы ниже минимума API (10 шт для обычных
    услуг, 15 похвал для CS2) считаются через запрос по минимуму с делением
    (_service_volume_cost), а параметры похвалы всегда разрешаются дефолтом
    (_commend_params). Возвращает "" — пусть API сам отвечает.
    """
    return ""


def _service_volume_cost(s: dict, provider: dict | None, svc: str, volume: int,
                         cache: dict | None = None,
                         reason_out: list | None = None) -> float | None:
    """Себестоимость закупки объёма услуги (₽).

    Источник: cost_per_unit привязки (если есть), иначе живая цена
    provider.price() (кеш по (svc, volume) на показ меню). Для похвалы CS2
    в живой запрос передаётся базовый пакет привязки (friendly/teacher/leader)
    — без него steamsmm не считает цену — а себестоимость объёма считается
    как цена пакета × объём / сумма пакета (1 ед = 1 похвала одному профилю).
    При неудаче живого запроса reason_out (если передан) заполняется
    конкретной причиной: 401 → ключ, объём < минимума API, текст ошибки.
    """
    cost = None
    mapping = None
    for m in s.get("lot_mappings", []):
        if str(m.get("service_id") or "") != str(svc):
            continue
        # у похвалы CS2 параметры (friendly/teacher/leader) не зависят от объёма —
        # ищем привязку по услуге, а не по qty_multiplier (иначе привязка на
        # 15 шт не подойдёт для автосоздания с базовым объёмом 1)
        if svc != "commend_cs2" and \
                int(m.get("qty_multiplier", 1) or 1) != int(volume):
            continue
        mapping = m
        c = m.get("cost_per_unit")
        if c not in (None, ""):
            try:
                cost = float(c)
            except (TypeError, ValueError):
                cost = None
        break
    if cost is None and provider:
        key = f"{svc}:{volume}"
        if cache is not None and key in cache:
            cost = cache[key]
        else:
            reason = (_price_fail_reason(svc, volume, mapping)
                      if _provider_style(provider) == "rest" else "")
            if reason:
                if reason_out is not None:
                    reason_out.append(reason)
            else:
                try:
                    extras = _mapping_extras(mapping or {})
                    base_total = None
                    scale_total = None
                    if svc == "commend_cs2":
                        base = _commend_params(s, mapping)
                        base_total = max(1, sum(base.values()))
                        extras.update(base)
                    # обычные услуги: API не котирует объём ниже минимума (10 шт) —
                    # запрашиваем цену по минимуму и делим на него, чтобы получить
                    # цену за 1 шт (авто-лот создаётся «цена за 1»)
                    qty_for_api = int(volume)
                    if svc != "commend_cs2" and not str(svc).startswith("autoreg:"):
                        mn = _min_order_qty(svc) or 0
                        if mn and int(volume) < mn:
                            qty_for_api = mn
                            scale_total = mn
                    data = _client_for_provider(provider).price(
                        _service_id_for({"service_id": svc}, provider), qty_for_api,
                        **extras)
                    if not (isinstance(data, dict) and data.get("error")):
                        cost = data.get("total_cost")
                        if cost is None:
                            per = data.get("price_per_item")
                            cost = per * qty_for_api if per is not None else None
                        if cost is not None:
                            cost = max(0.0, float(cost))
                        if svc == "commend_cs2" and base_total:
                            # /commend/price отдаёт цену базового пакета; 1 ед =
                            # 1 похвала → себестоимость объёма = цена × объём / сумма
                            cost = cost * float(volume) / base_total
                        elif scale_total:
                            # себестоимость объёма = цена(min) × объём / min
                            cost = cost * float(volume) / scale_total
                    elif reason_out is not None:
                        reason_out.append(_short_err(data.get("error"))
                                          or "поставщик не вернул цену")
                except Exception as e:
                    cost = None
                    if reason_out is not None:
                        msg = str(e)
                        reason_out.append(
                            "API-ключ неверный или истёк (401) — проверьте "
                            "⚙️ Настройки → 🔑 API-ключ steamsmm.ru"
                            if "401" in msg
                            else (_short_err(e) or "ошибка запроса цены"))
            if cache is not None:
                cache[key] = cost
    if cost is None:
        return None
    return float(cost)


def _volume_price(s: dict, provider: dict | None, svc: str, volume: int,
                  cache: dict | None = None,
                  reason_out: list | None = None) -> float | None:
    """Расчётная цена лота объёма: себестоимость объёма × (1 + наценка).

    reason_out (если передан) при неудаче заполняется конкретной причиной —
    см. _service_volume_cost.
    """
    cost = _service_volume_cost(s, provider, svc, volume, cache, reason_out)
    if cost is None:
        return None
    markup = float(s.get("auto_lots_markup_percent", 30.0) or 0)
    return round(cost * (1 + markup / 100.0), 2)


def _volume_breakdown(s: dict, provider: dict | None, svc: str, volume: int,
                      cache: dict | None = None) -> dict | None:
    """Детальный расчёт объёма: закупка → продажа → комиссия → чистая прибыль.

    Возвращает {"cost", "price", "fee", "profit", "margin"} (₽, маржа — %)
    или None, если себестоимость недоступна. Цена берётся из _volume_price —
    единый источник формулы. Маржа = прибыль / продажа × 100 (отрицательная,
    если цена ниже себестоимости с учётом комиссии).
    """
    cost = _service_volume_cost(s, provider, svc, volume, cache)
    price = _volume_price(s, provider, svc, volume, cache)
    if cost is None or price is None:
        return None
    fee_pct = float(s.get("funpay_fee_percent", 7.5) or 0)
    profit = _net_profit(price, cost, fee_pct)
    return {
        "cost": cost,
        "price": price,
        "fee": _funpay_fee(price, fee_pct),
        "profit": profit,
        "margin": round(profit / price * 100.0, 1) if price > 0 else 0.0,
    }


def _home_text() -> str:
    s = _load_settings()
    providers = s.get("providers", [])
    mappings = s.get("lot_mappings", [])
    accounts = _load_accounts()
    sales = s.get("sales_enabled", True)
    markup = float(s.get("auto_lots_markup_percent", 30.0) or 0)
    rest = next((p for p in providers if _provider_style(p) == "rest" and (p.get("api_key") or "").strip()), None)
    bal = _balance_cached(rest) if rest else None
    threshold = float(s.get("balance_alert_threshold", 50.0) or 0)
    low_balance_paused = (not sales) and s.get("auto_pause_active")
    if sales:
        status_line = "🟢 Продажи идут"
    elif low_balance_paused:
        bal_s = f"{bal:.2f}" if bal is not None else "—"
        status_line = (f"⏸️ Продажи на паузе: низкий баланс "
                       f"<code>{bal_s}</code> ₽ &lt; порога <code>{threshold:.2f}</code> ₽")
    else:
        status_line = "🔴 Продажи остановлены"
    # предупреждение о низком балансе (кроме случая, когда пауза уже показывает причину)
    warn_line = ""
    if bal is not None and bal < threshold and not low_balance_paused:
        warn_line = (f"⚠️ <b>Пополните баланс</b>: <code>{bal:.2f}</code> ₽ &lt; "
                     f"порога <code>{threshold:.2f}</code> ₽\n")
    grace_line = ""
    if sales:
        grace_until = float(s.get("auto_pause_grace_until") or 0)
        if grace_until > time.time():
            hours = (grace_until - time.time()) / 3600.0
            grace_line = (f"⏳ Автопауза отложена (торгуем остатком): ещё "
                          f"<code>{hours:.1f}</code> ч\n")
    bal_line = f"<b>{bal:.2f}</b> ₽" if bal is not None else "—"
    day = _aggregate_profit(_load_orders(), time.time() - 86400)
    no_key = [p.get("name", "?") for p in providers if not (p.get("api_key") or "").strip()]
    hints = []
    if not providers:
        hints.append("• введите API-ключ: ⚙️ Настройки → 🔑 API-ключ steamsmm.ru")
    elif no_key:
        hints.append("• впишите API-ключ: " + ", ".join(no_key)
                     + " → ⚙️ Настройки → 🔑 API-ключ steamsmm.ru")
    if not _active_lots_count(s):
        hints.append("• создайте лоты: 🎯 Услуги → ➕ Создать (или ⚙️ Настройки → 🗺 Привязки)")
    hint_block = ("\n<b>⚠️ Чтобы заработало:</b>\n" + "\n".join(hints)) if hints else \
        "\n✅ Всё готово к работе."
    return (
        f"<b>🚀 Auto Steam SMM v{VERSION}</b>\n"
        f"{status_line}\n"
        f"{warn_line}"
        f"{grace_line}"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💰 Баланс steamsmm: {bal_line}\n"
        f"📦 Активных лотов: <code>{_active_lots_count(s)}</code>     "
        f"🏷 Наценка: <code>{markup:.0f}%</code>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📅 За сутки: заказов <code>{day['count']}</code>, "
        f"прибыль <code>{day['profit']:.2f}</code> ₽\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔑 API-ключ: <code>{'задан' if providers else 'не задан'}</code> · "
        f"🗺 Привязок: <code>{len(mappings)}</code> · "
        f"👥 Авторегов: <code>{sum(1 for a in accounts if not a.get('sold'))}</code>\n"
        f"{hint_block}"
    )


def _home_kb() -> "K":
    s = _load_settings()
    kb = K(row_width=2)
    kb.row(B(("🛑 Остановить продажи" if s.get("sales_enabled", True)
              else "▶️ Запустить продажи"), callback_data=CBT_TOGGLE_SALES))
    kb.row(B(("🛠 Выключить обслуживание" if s.get("maintenance_mode", False)
              else "🛠 Включить обслуживание"), callback_data=CBT_TOGGLE_MAINTENANCE))
    kb.row(B("🎯 Услуги", callback_data=CBT_SERVICES),
           B("📦 Активные заказы", callback_data=CBT_ACTIVE))
    kb.row(B("📊 Статистика", callback_data=CBT_STATS),
           B("💰 Цены", callback_data=CBT_PRICES))
    kb.row(B("📖 Инструкция", callback_data=CBT_HELP),
           B("⚙️ Настройки", callback_data=CBT_ADVANCED))
    kb.row(B("🌐 steamsmm.ru (реф)", url="https://steamsmm.ru/register?ref=uPt4oCkV"))
    return kb


def _service_has_drafts(s: dict, svc: str) -> bool:
    """Есть service-привязки услуги без ID лота (импортированные по названию)."""
    for m in s.get("lot_mappings", []):
        if m.get("mode", "service") == "account":
            continue
        if str(m.get("service_id") or "") == str(svc) and \
                m.get("target_lot_id") in (None, ""):
            return True
    return False


def _services_text() -> str:
    s = _load_settings()
    lines = ["<b>🎯 Услуги</b>", "", "Настройка и активация типов SMM-услуг для Steam."]
    for svc, meta in SERVICE_PRESETS.items():
        has_lots = _service_state(s, svc)
        has_drafts = _service_has_drafts(s, svc)
        selling = _service_sales_enabled(s, svc)
        if has_lots and selling:
            ind = "🟢"
        elif has_lots:
            ind = "⏸️"
        elif has_drafts:
            ind = "🟡"
        else:
            ind = "🔴"
        lines.append(f"{ind} {meta['name']}")
    lines.append("")
    lines.append("🟢 — продажи идут · ⏸️ — лоты есть, продажа выключена · "
                 "🟡 — привязки без ID лота · 🔴 — нет лотов")
    return "\n".join(lines)


def _services_kb() -> "K":
    kb = K(row_width=1)
    for svc in SERVICE_PRESETS:
        meta = SERVICE_PRESETS[svc]
        kb.add(B(meta["name"], callback_data=f"{CBT_SERVICE_DETAIL}:{svc}"))
    kb.add(B("📥 Импорт лотов из других плагинов", callback_data=CBT_IMPORT))
    kb.add(B("◀️ Назад", callback_data=CBT_HOME))
    return kb


# =========================================================================
# Импорт привязок лотов из других плагинов (autosmm_fpc / Auto SMM.py)
# =========================================================================

# Ключевые слова для определения услуги по названию/коду лота.
# Порядок важен: сначала более специфичные (premium/random/-rep), потом общие.
_IMPORT_SERVICE_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("commend_cs2",     ("похвал", "commend", "cs2")),
    ("comment_premium", ("премиум", "premium")),
    ("comment_random",  ("случайн", "рандом", "random")),
    ("comment_rep",     ("-rep", "минус", "негатив")),
    ("comment",         ("+rep", "comment", "комментар", "коммент")),
    ("review",          ("обзор", "реценз", "review", "отзыв")),
    ("subscribe",       ("участник", "подписчик", "вступлен", "group", "subscribe")),
    ("dis",             ("дизлайк", "dislike", "диз")),
    ("like",            ("лайк", "like")),
]

_IMPORT_SOURCE_LABELS = {
    "autosmm": "autosmm_fpc (storage/plugins/autosmm)",
    "legacy": "Auto SMM.py (storage/cache/auto_lots.json)",
}

_IMPORT_SOURCE_SHORT_LABELS = {
    "autosmm": "autosmm_fpc",
    "legacy": "Auto SMM.py",
}


def _import_source_paths() -> dict[str, Path]:
    """Пути к историческим источникам импорта."""
    return {
        "autosmm": PLUGIN_DIR.parent / "autosmm" / "settings.json",
        "legacy": PLUGIN_DIR.parents[1] / "cache" / "auto_lots.json",
    }


def _match_service_preset(text: Any) -> str | None:
    """Определяет код услуги (SERVICE_PRESETS) по названию или коду услуги лота.

    Сначала точное совпадение с кодом пресета, затем ключевые слова.
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    low = raw.casefold()
    if low in SERVICE_PRESETS:
        return low
    for preset, keywords in _IMPORT_SERVICE_KEYWORDS:
        for kw in keywords:
            if kw in low:
                return preset
    return None


def _parse_import_source(key: str) -> list[dict] | None:
    """Читает привязки из конфига источника.

    Возвращает нормализованные элементы
    [{name, service_id, qty_multiplier, lot_id}] или None, если файла нет.
    """
    path = _import_source_paths().get(key)
    if path is None:
        return None
    data = _load_json(path, None)
    if not isinstance(data, dict):
        return None
    items: list[dict] = []
    if key == "autosmm":
        for mapping in data.get("lot_mappings") or []:
            if not isinstance(mapping, dict):
                continue
            match = str(mapping.get("lot_match") or "").strip()
            if match:
                items.append({
                    "name": match,
                    "service_id": mapping.get("service_id"),
                    "qty_multiplier": mapping.get("qty_multiplier", 1),
                    "lot_id": int(match) if match.isdigit() else None,
                })
    elif key == "legacy":
        for lot_data in (data.get("lot_mapping") or {}).values():
            if not isinstance(lot_data, dict):
                continue
            name = str(lot_data.get("name") or "").strip()
            if name:
                items.append({
                    "name": name,
                    "service_id": lot_data.get("service_id"),
                    "qty_multiplier": lot_data.get("quantity", 1),
                    "lot_id": None,
                })
    return items


def _probe_import_sources() -> list[dict]:
    """Сканирует источники импорта: [{key, label, path, count, items}]."""
    out: list[dict] = []
    for key, path in _import_source_paths().items():
        items = _parse_import_source(key)
        if not items:
            continue
        out.append({
            "key": key,
            "label": _IMPORT_SOURCE_LABELS.get(key, key),
            "path": str(path),
            "count": len(items),
            "items": items,
        })
    return out


def _import_items(s: dict, items: list[dict], label: str) -> dict:
    """Добавляет привязки из items в settings (дедупликация по услуге+объёму+лоту).

    Возвращает отчёт {"added", "drafts", "skipped_dup", "skipped_unmapped",
    "skipped_bad"}. `drafts` — привязки без ID лота (работают по названию, но
    цену лота нужно привязать в «🎯 Услуги»).
    """
    report = {"added": 0, "drafts": 0, "skipped_dup": 0,
              "skipped_unmapped": 0, "skipped_bad": 0}
    existing = s.setdefault("lot_mappings", [])

    def _dup(m: dict) -> bool:
        for e in existing:
            if (str(e.get("service_id") or "") == str(m.get("service_id") or "")
                    and float(e.get("qty_multiplier", 1) or 1) == float(m.get("qty_multiplier", 1) or 1)
                    and str(e.get("lot_match") or "") == str(m.get("lot_match") or "")):
                return True
        return False

    for item in items:
        preset = _match_service_preset(item.get("name")) or _match_service_preset(item.get("service_id"))
        if preset is None:
            report["skipped_unmapped"] += 1
            continue
        qty = item.get("qty_multiplier", 1)
        if qty in (None, ""):
            qty = 1
        try:
            volume = float(qty)
        except (TypeError, ValueError):
            volume = 0.0
        if volume <= 0:
            report["skipped_bad"] += 1
            continue
        lot_id = item.get("lot_id")
        mapping = {
            "id": _new_id("m", existing),
            "lot_match": str(lot_id) if lot_id else item.get("name"),
            "mode": "service",
            "service_id": preset,
            "qty_multiplier": float(volume),
            "cost_per_unit": None,
            "imported_from": label,
        }
        if lot_id:
            mapping["target_lot_id"] = int(lot_id)
        if _dup(mapping):
            report["skipped_dup"] += 1
            continue
        existing.append(mapping)
        report["added"] += 1
        if not lot_id:
            report["drafts"] += 1
    return report


def _account_lots(cardinal: "Cardinal") -> list:
    """Каталог лотов FunPay через FPC (cardinal.profile.get_lots()).

    Возвращает список объектов лотов (с .id и .title) или [].
    """
    profile = getattr(cardinal, "profile", None)
    if profile is None or not callable(getattr(profile, "get_lots", None)):
        return []
    try:
        lots = profile.get_lots() or []
        return [l for l in lots if getattr(l, "id", None) is not None]
    except Exception:
        logger.debug(f"{LOGGER_PREFIX} get_lots неудачный", exc_info=True)
        return []


def _lot_title(lot) -> str:
    """Название лота FunPay (title/description/name)."""
    for attr in ("title", "description", "name"):
        v = getattr(lot, attr, None)
        if v:
            return str(v)
    return ""


def _resolve_draft_lot_ids(s: dict, lots: list) -> dict:
    """Авто-подстановка target_lot_id черновикам (привязкам без ID лота).

    Для каждой service-привязки без target_lot_id ищем в каталоге лот, чьё
    название равно lot_match (casefold, точно) — если уникален, берём его;
    иначе по подстроке — тоже только при единственном совпадении.
    Меняет s на месте. Возвращает отчёт {resolved, ambiguous, not_found}.
    """
    report = {"resolved": 0, "ambiguous": 0, "not_found": 0}
    if not lots:
        return report

    def _lid(lot):
        try:
            return int(lot.id)
        except (TypeError, ValueError):
            return None

    by_name: dict[str, list] = {}
    for lot in lots:
        if _lid(lot) is None:
            continue
        t = _lot_title(lot).casefold().strip()
        if t:
            by_name.setdefault(t, []).append(lot)
    for m in s.get("lot_mappings", []):
        if m.get("mode", "service") == "account":
            continue
        if m.get("target_lot_id") not in (None, ""):
            continue
        name = str(m.get("lot_match") or "").strip()
        if not name or name.isdigit():
            continue  # числовой lot_match — это уже ID, а не название для поиска
        low = name.casefold()
        exact = by_name.get(low, [])
        if len(exact) == 1:
            m["target_lot_id"] = _lid(exact[0])
            m["draft_resolved"] = True  # цена выставится после авто-поиска
            report["resolved"] += 1
            continue
        if len(exact) > 1:
            report["ambiguous"] += 1
            continue
        subs = [lot for t, batch in by_name.items() if low in t for lot in batch]
        if len(subs) == 1:
            m["target_lot_id"] = _lid(subs[0])
            m["draft_resolved"] = True  # цена выставится после авто-поиска
            report["resolved"] += 1
        elif len(subs) > 1:
            report["ambiguous"] += 1
        else:
            report["not_found"] += 1
    return report


def _price_one_mapping(cardinal: "Cardinal", s: dict, m: dict) -> str:
    """Выставляет цену по наценке одному разрешённому черновику.

    Как в _bind_service_lot: price, amount=1, active=True через
    get_lot_fields/save_lot. Возвращает "ok" | "failed" | "no_account".
    При успехе снимает флаг draft_resolved — повторный запуск не
    пересчитает уже обработанный лот.
    """
    account = getattr(cardinal, "account", None)
    if account is None or not hasattr(account, "get_lot_fields") \
            or not hasattr(account, "save_lot"):
        return "no_account"
    lot_id = _lot_target_id(m)
    if lot_id is None:
        return "failed"
    try:
        volume = int(float(m.get("qty_multiplier", 1) or 1))
    except (TypeError, ValueError):
        volume = 1
    provider = _find_default_provider(s)
    price = _volume_price(s, provider, m.get("service_id"), volume)
    if price is None:
        return "failed"
    try:
        fields = account.get_lot_fields(lot_id)
        fields.price = price
        fields.amount = 1
        fields.active = True
        account.save_lot(fields)
    except Exception:
        return "failed"
    m.pop("draft_resolved", None)
    return "ok"


def _price_resolved_lots(cardinal: "Cardinal", s: dict) -> dict:
    """Выставляет цены по наценке лотам, ID которых разрешён авто-поиском.

    Обрабатывает привязки с флагом draft_resolved (ставится при авто-поиске
    ID): цена = себестоимость объёма × наценка (_volume_price), выставляется
    как в _bind_service_lot — price, amount=1, active=True через
    get_lot_fields/save_lot. Флаг снимается при успехе, чтобы повторный
    запуск не пересчитывал уже обработанные лоты; при сбое/нет аккаунта
    флаг остаётся — следующий запуск повторит попытку.
    Возвращает {"priced", "failed", "no_account"}.
    """
    report = {"priced": 0, "failed": 0, "no_account": False}
    for m in s.get("lot_mappings", []):
        if not m.get("draft_resolved"):
            continue
        if m.get("mode", "service") == "account":
            continue
        status = _price_one_mapping(cardinal, s, m)
        if status == "ok":
            report["priced"] += 1
        elif status == "failed":
            report["failed"] += 1
        else:
            report["no_account"] = True
    return report


def _match_lots_by_name(lots: list, name: str) -> list:
    """Лоты FunPay, чьё название совпадает с name (casefold): точное — первыми,
    затем по подстроке. Лоты с нечисловым id пропускаются. Пустое/числовое
    name → [] (числовой lot_match — это ID, а не название для поиска)."""
    name = (name or "").strip()
    if not name or name.isdigit():
        return []
    low = _normalize_lot_text(name)
    exact: list = []
    subs: list = []
    for lot in lots:
        try:
            int(lot.id)
        except (TypeError, ValueError):
            continue
        t = _normalize_lot_text(_lot_title(lot))
        if not t:
            continue
        if t == low:
            exact.append(lot)
        elif low in t:
            subs.append(lot)
    return exact + subs


def _mapping_for_volume(s: dict, svc: str, volume: int) -> dict | None:
    """Привязка услуги заданного объёма (или None)."""
    for m in s.get("lot_mappings", []):
        if str(m.get("service_id") or "") == str(svc) and \
                int(m.get("qty_multiplier", 1) or 1) == int(volume):
            return m
    return None


def _bind_draft_lot_id(cardinal: "Cardinal", s: dict, mapping: dict,
                       volume: int, lot_id: int) -> str:
    """Привязывает черновик к найденному лоту: target_lot_id + цена по наценке.

    Название (lot_match) сохраняется — привязка по названию продолжает
    работать, а ID даёт точное совпадение и включает автоцены. Возвращает
    статус цены: "ok" | "no_account" | "failed" (флаг draft_resolved
    снимается только при успешной установке цены — иначе повторится позже).
    """
    mapping["target_lot_id"] = int(lot_id)
    mapping["draft_resolved"] = True
    return _price_one_mapping(cardinal, s, mapping)


def _import_text() -> str:
    sources = _probe_import_sources()
    if not sources:
        return (
            "<b>📥 Импорт лотов</b>\n\n"
            "Не найдено конфигов других плагинов.\n"
            "Ищу привязки в:\n"
            "• <code>storage/plugins/autosmm/settings.json</code> (autosmm_fpc)\n"
            "• <code>storage/cache/auto_lots.json</code> (Auto SMM.py)\n\n"
            "Запустите плагин-источник хотя бы раз, чтобы его конфиг появился."
        )
    lines = ["<b>📥 Импорт лотов из других плагинов</b>", "",
             "Найдены привязки. Перенесу их как привязки этого плагина "
             "(услуга определится по названию лота):", ""]
    for src in sources:
        with_id = sum(1 for it in src["items"] if it.get("lot_id"))
        num_svc = sum(1 for it in src["items"]
                      if not _match_service_preset(it.get("name"))
                      and str(it.get("service_id") or "").strip().isdigit())
        lines.append(f"• <b>{src['label']}</b> — <code>{src['count']}</code> привязок, "
                     f"из них с ID лота: <code>{with_id}</code>")
        if num_svc:
            lines.append(f"   ⚠️ <code>{num_svc}</code> с числовым ID услуги — "
                         f"услугу не распознать, привяжите их вручную")
    lines.append("")
    lines.append("Привязки без ID лота будут работать по названию лота — "
                 "затем привяжите ID в карточке услуги, чтобы включились автоцены.")
    return "\n".join(lines)


def _import_kb(sources: list[dict] | None = None) -> "K":
    if sources is None:
        sources = _probe_import_sources()
    kb = K(row_width=1)
    for src in sources:
        short = _IMPORT_SOURCE_SHORT_LABELS.get(src["key"], src["key"])
        kb.add(B(f"📥 Перенести {src['count']} из {short}",
                 callback_data=f"{CBT_IMPORT_SRC}:{src['key']}"))
    s = _load_settings()
    drafts = _draft_mapping_count(s)
    if drafts:
        kb.add(B(f"🔍 Найти ID лотов ({drafts} черновиков)",
                 callback_data=CBT_IMPORT_RESOLVE))
    exportable = len(_exportable_mappings(s))
    if exportable:
        kb.add(B(f"📤 Экспорт в autosmm_fpc ({exportable})",
                 callback_data=CBT_EXPORT))
    kb.add(B("◀️ Назад", callback_data=CBT_SERVICES))
    return kb


def _draft_mapping_count(s: dict) -> int:
    """Число service-привязок без ID лота (черновиков)."""
    return sum(1 for m in s.get("lot_mappings", [])
               if m.get("mode", "service") != "account"
               and m.get("target_lot_id") in (None, "")
               and not str(m.get("lot_match") or "").isdigit())


def _exportable_mappings(s: dict) -> list[dict]:
    """Service-привязки (не account), которые можно перенести в autosmm_fpc."""
    return [m for m in s.get("lot_mappings", [])
            if isinstance(m, dict) and m.get("mode", "service") != "account"]


def _export_mappings_to_autosmm(settings: dict | None = None,
                                path: Path | None = None) -> dict:
    """Экспорт service-привязок steam_smm в конфиг autosmm_fpc.

    Формат autosmm: {id, lot_match, provider_id, service_id, qty_multiplier}.
    Мержит с существующими привязками autosmm (дедуп по
    lot_match+service_id+qty_multiplier). Возвращает отчёт
    {exported, skipped_dup, skipped_empty, existing_total, path}.
    """
    s = settings if settings is not None else _load_settings()
    if path is None:
        path = _import_source_paths()["autosmm"]
    target = _load_json(path, None)
    if not isinstance(target, dict):
        target = {}
    existing = target.get("lot_mappings")
    if not isinstance(existing, list):
        existing = []
        target["lot_mappings"] = existing
    provider_id = next((p.get("id") for p in target.get("providers") or []), None)
    exported = skipped_dup = skipped_empty = non_numeric = 0
    for mapping in _exportable_mappings(s):
        lot_match = str(mapping.get("lot_match") or mapping.get("target_lot_id") or "").strip()
        if not lot_match:
            skipped_empty += 1
            continue
        service_id = mapping.get("service_id")
        qty = mapping.get("qty_multiplier", 1)
        duplicate = any(
            str(item.get("lot_match") or "") == lot_match
            and str(item.get("service_id") or "") == str(service_id or "")
            and float(item.get("qty_multiplier", 1) or 1) == float(qty or 1)
            for item in existing if isinstance(item, dict)
        )
        if duplicate:
            skipped_dup += 1
            continue
        entry = {"id": _new_id("m", existing), "lot_match": lot_match,
                 "service_id": service_id, "qty_multiplier": qty}
        if provider_id:
            entry["provider_id"] = provider_id
        existing.append(entry)
        exported += 1
        if not str(service_id or "").strip().isdigit():
            non_numeric += 1
    saved = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _save_json(path, target)
        saved = path.exists()
    except Exception:
        saved = False
    return {"exported": exported, "skipped_dup": skipped_dup,
            "skipped_empty": skipped_empty, "non_numeric": non_numeric,
            "saved": saved, "existing_total": len(existing), "path": str(path)}


def _import_done_kb() -> "K":
    kb = K(row_width=2)
    kb.row(B("🎯 В услуги", callback_data=CBT_SERVICES),
           B("🏠 Главное", callback_data=CBT_HOME))
    return kb


def _service_detail_text(s: dict, svc: str) -> str:
    meta = SERVICE_PRESETS.get(svc, {"name": svc, "volumes": []})
    lines = [f"<b>{meta['name']}</b>", "",
             "Плагин работает только со своими лотами: создайте лот на FunPay "
             "и привяжите его здесь, либо укажите ID существующего лота.", ""]
    cache: dict = {}
    provider = _find_default_provider(s)
    for vol in meta.get("volumes", []):
        b = _volume_breakdown(s, provider, svc, vol, cache)
        price = b["price"] if b else None
        margin = float(b["margin"]) if b else None
        warn = (f" · маржа <code>{margin:.1f}%</code> 🔴"
                if margin is not None and margin < 0 else "")
        bound = False
        has_id = True
        for m in s.get("lot_mappings", []):
            if str(m.get("service_id") or "") == str(svc) and \
                    int(m.get("qty_multiplier", 1) or 1) == int(vol):
                bound = True
                has_id = bool(m.get("target_lot_id"))
                break
        if bound:
            if has_id:
                line = f"• {vol} шт — <code>{price:.2f}</code> ₽ (лот привязан)" if price else f"• {vol} шт — привязан"
            else:
                line = f"• {vol} шт — привязан без ID лота (работает по названию)"
        else:
            line = f"• {vol} шт — " + (f"<code>{price:.2f}</code> ₽" if price else "цена по запросу")
        lines.append(line + warn)
    return "\n".join(lines)


def _service_detail_kb(s: dict, svc: str) -> "K":
    kb = K(row_width=2)
    meta = SERVICE_PRESETS.get(svc, {"volumes": []})
    for vol in meta.get("volumes", []):
        m = _mapping_for_volume(s, svc, vol)
        if m is not None:
            kb.add(B(f"🗑 Отвязать {vol} шт", callback_data=f"{CBT_SERVICE_DEL}:{svc}:{vol}"))
            if _lot_target_id(m) is None:
                kb.add(B(f"🔍 Найти ID {vol} шт",
                         callback_data=f"{CBT_SERVICE_LOTID}:{svc}:{vol}"))
            elif m.get("draft_resolved"):
                # цена не выставилась (не было аккаунта/сбоя) — повторная попытка
                kb.add(B(f"🔁 Выставить цену {vol} шт",
                         callback_data=f"{CBT_SERVICE_LOTID}:{svc}:{vol}"))
        else:
            kb.add(B(f"➕ Создать {vol} шт", callback_data=f"{CBT_SERVICE_CREATE}:{svc}:{vol}"))
    on = _service_sales_enabled(s, svc)
    kb.row(B(("🟢 Продажа включена" if on else "🔴 Продажа выключена"),
             callback_data=f"{CBT_SERVICE_TOGGLE_SALES}:{svc}"))
    kb.row(B("◀️ Назад", callback_data=CBT_SERVICES))
    return kb


def _prices_text() -> str:
    s = _load_settings()
    markup = float(s.get("auto_lots_markup_percent", 30.0) or 0)
    recalc_min = int(s.get("prices_recalc_interval_min", 60) or 60)
    return (
        f"<b>💰 Цены</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Наценка — заработок поверх закупки. Комиссия FunPay уже учтена; "
        f"ниже себестоимости закупки плагин товар не продаст.\n\n"
        f"🏷 Наценка: <code>{markup:.0f}%</code>\n"
        f"🔄 Пересчёт: <code>{_fmt_duration(recalc_min * 60)}</code>\n"
        f"🔢 Округление объёма: {_onoff(s.get('qty_rounding', True))} — если у поставщика "
        f"200 шт дешевле, чем 199, покупатель получит больше, а вы заплатите меньше.\n"
        f"⚡ Автоцены: {_onoff(s.get('auto_lots_enabled'))} — плагин автоматически "
        f"удерживает актуальные цены на созданных лотах.\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📊 Детальный расчёт закупки, комиссии FunPay и прибыли — "
        f"кнопка «Расчёт по услуге» ниже."
    )


def _prices_kb(s: dict) -> "K":
    kb = K(row_width=2)
    kb.add(B(f"🏷 Наценка: {float(s.get('auto_lots_markup_percent', 30) or 0):.0f}%",
             callback_data=CBT_EDIT_MARKUP))
    kb.add(B(f"🔄 Пересчёт: {_fmt_duration(int(s.get('prices_recalc_interval_min', 60) or 60) * 60)}",
             callback_data=CBT_EDIT_RECALC_INT))
    kb.add(B(f"🔢 Округление объёма: {'🟢' if s.get('qty_rounding', True) else '🔴'}",
             callback_data=CBT_TOGGLE_ROUND))
    kb.add(B(f"⚡ Автоцены: {'🟢' if s.get('auto_lots_enabled') else '🔴'}",
             callback_data=CBT_TOGGLE_AUTOPRICES))
    kb.add(B("🛰 Проверить цены (живые)", callback_data=CBT_PRICES_CHECK))
    kb.add(B(f"🔄 Пересчитать ({_active_lots_count(s)})", callback_data=CBT_PRICES_RECALC))
    kb.add(B("📊 Маржа по услугам", callback_data=CBT_PRICES_MARGIN))
    kb.add(B("📊 Расчёт по услуге", callback_data=CBT_PRICES_SVC))
    kb.add(B("◀️ Назад", callback_data=CBT_HOME))
    return kb


def _live_price_check(provider: dict, svc: str, volume: int,
                      extras: dict | None = None) -> tuple[float | None, str]:
    """Живая цена услуги у поставщика (без кеша и без cost_per_unit).

    Возвращает (себестоимость объёма, '') или (None, причина). Причина
    конкретная: 401 → ключ, объём < минимума, текст ошибки API. Для похвалы
    CS2 запрашивается базовый пакет привязки, а себестоимость объёма =
    цена пакета × объём / сумма пакета (1 ед = 1 похвала одному профилю).
    """
    if _provider_style(provider) == "rest":
        local = _price_fail_reason(svc, volume, extras or {})
        if local:
            return None, local
    base_total = None
    scale_total = None
    try:
        api_extras = _mapping_extras(extras or {})
        if svc == "commend_cs2":
            base = _commend_params(_load_settings(), extras or {})
            base_total = max(1, sum(base.values()))
            api_extras.update(base)
        # обычные услуги: объём ниже минимума API (10 шт) считаем через минимум
        qty_for_api = int(volume)
        if svc != "commend_cs2" and not str(svc).startswith("autoreg:"):
            mn = _min_order_qty(svc) or 0
            if mn and int(volume) < mn:
                qty_for_api = mn
                scale_total = mn
        data = _client_for_provider(provider).price(
            _service_id_for({"service_id": svc}, provider), qty_for_api, **api_extras)
    except Exception as e:
        msg = str(e)
        if "401" in msg:
            return None, ("401 — неверный/истёкший API-ключ "
                          "(⚙️ Настройки → 🔑 API-ключ steamsmm.ru)")
        return None, _short_err(e) or "ошибка запроса цены"
    if not isinstance(data, dict):
        return None, "некорректный ответ API"
    if data.get("error"):
        return None, _short_err(data.get("error")) or "API вернул ошибку"
    total = data.get("total_cost")
    if total is None:
        per = data.get("price_per_item")
        total = per * qty_for_api if per is not None else None
    if total is None:
        return None, "поставщик не вернул цену"
    try:
        total = max(0.0, float(total))
        if svc == "commend_cs2" and base_total:
            total = total * float(volume) / base_total
        elif scale_total:
            total = total * float(volume) / scale_total
        return total, ""
    except (TypeError, ValueError):
        return None, "поставщик вернул некорректную цену"


def _prices_check_text() -> str:
    s = _load_settings()
    provider = _find_default_provider(s)
    lines = ["<b>🛰 Проверка цен (живые тарифы steamsmm)</b>", ""]
    if provider is None:
        lines.append("Нет поставщика с API-ключом — проверка невозможна.\n"
                     "⚙️ Настройки → 🔑 API-ключ steamsmm.ru")
        return "\n".join(lines)
    errors: list[str] = []
    checked = 0
    # обычные услуги — по первому объёму пресета (≥ минимума API)
    for svc, meta in SERVICE_PRESETS.items():
        vols = meta.get("volumes") or [10]
        vol = int(vols[0])
        mapping = _mapping_for_volume(s, svc, vol) or _mapping_for_volume(s, svc, 1)
        price, err = _live_price_check(provider, svc, vol, mapping)
        checked += 1
        if price is None:
            errors.append(f"❌ <b>{_html_escape(meta['name'])}</b> ({svc}×{vol}): "
                          f"{_html_escape(err)}")
        else:
            lines.append(f"✅ {_html_escape(meta['name'])} — <code>{vol}</code> шт: "
                         f"<b><code>{price:.2f}</code></b> ₽")
    # автореги — каталог /autoreg/products, цена по min_count товара
    if _provider_style(provider) == "rest":
        items = _client_for_provider(provider).autoreg_products()
        for it in items:
            cat = it.get("category_id")
            if cat is None:
                continue
            svc = f"autoreg:{cat}"
            try:
                vol = max(1, int(it.get("min_count") or 0)) or 1
            except (TypeError, ValueError):
                vol = 1
            title = (it.get("title") or f"Авторег {it.get('region', '')}".strip()
                     or f"autoreg:{cat}")
            checked += 1
            price, err = _live_price_check(provider, svc, vol)
            if price is None:
                errors.append(f"❌ <b>{_html_escape(str(title))}</b> ({svc}×{vol}): "
                              f"{_html_escape(err)}")
            else:
                per = price / vol if vol else price
                lines.append(f"✅ {_html_escape(str(title))} — {vol} шт: "
                             f"<b><code>{price:.2f}</code></b> ₽ "
                             f"(<code>{per:.2f}</code> ₽/шт)")
    lines.append("")
    if errors:
        lines.append(f"⚠️ Ошибок: <code>{len(errors)}</code> из {checked}:")
        lines.extend(errors[:15])
        if len(errors) > 15:
            lines.append(f"… и ещё {len(errors) - 15}")
        lines.append("")
        lines.append("Частые причины: 401 — API-ключ; похвала CS2 — параметры "
                     "friendly/teacher/leader в 🗺 Привязки; объём < 10 — минимум API.")
    else:
        lines.append(f"✅ Все <code>{checked}</code> услуг ответили ценой.")
    return "\n".join(lines)


def _prices_check_kb() -> "K":
    kb = K(row_width=2)
    kb.row(B("🔄 Обновить", callback_data=CBT_PRICES_CHECK),
           B("◀️ Цены", callback_data=CBT_PRICES))
    return kb


def _margin_summary(s: dict) -> list[dict]:
    """Худшая маржа по каждой услуге (по всем её объёмам).

    Возвращает [{svc, name, margin, volume, price}], отсортированные по
    возрастанию маржи (худшие первыми). Учитываются только услуги, у которых
    хотя бы один объём имеет расчётную маржу.
    """
    provider = _find_default_provider(s)
    cache: dict = {}
    out: list[dict] = []
    for svc, meta in SERVICE_PRESETS.items():
        worst = None
        for vol in meta.get("volumes", []):
            b = _volume_breakdown(s, provider, svc, vol, cache)
            if b is None:
                continue
            if worst is None or b["margin"] < worst["margin"]:
                worst = {"margin": b["margin"], "volume": vol, "price": b["price"]}
        if worst is not None:
            out.append({"svc": svc, "name": meta["name"], **worst})
    out.sort(key=lambda r: r["margin"])
    return out


def _prices_margin_text() -> str:
    s = _load_settings()
    markup = float(s.get("auto_lots_markup_percent", 30.0) or 0)
    fee_pct = float(s.get("funpay_fee_percent", 7.5) or 0)
    rows = _margin_summary(s)
    lines = [
        "<b>📊 Маржа по услугам</b>",
        f"Наценка <code>{markup:.0f}%</code> · комиссия <code>{fee_pct:.1f}%</code> — "
        f"маржа зависит от них и одинакова для всех услуг.",
        "",
    ]
    if not rows:
        lines.append("Нет данных: добавьте поставщика с API-ключом или "
                     "cost_per_unit в привязках.")
    else:
        negative = 0
        for r in rows:
            if r["margin"] < 0:
                negative += 1
                lines.append(f"🔴 {r['name']} — маржа <code>{r['margin']:.1f}%</code> "
                             f"(объём {r['volume']} шт, продажа {r['price']:.2f} ₽)")
            else:
                lines.append(f"• {r['name']} — маржа <code>{r['margin']:.1f}%</code> "
                             f"(объём {r['volume']} шт)")
        missing = len(SERVICE_PRESETS) - len(rows)
        if missing:
            lines.append("")
            lines.append(f"— {missing} услуг без данных (нет привязок/провайдера).")
        lines.append("")
        if negative:
            lines.append(f"⚠️ Убыточных услуг: <code>{negative}</code> — повысьте "
                         f"наценку или смените тариф.")
        else:
            lines.append("✅ Убыточных услуг нет.")
    return "\n".join(lines)


def _prices_margin_kb() -> "K":
    kb = K(row_width=2)
    kb.row(B("🔄 Обновить", callback_data=CBT_PRICES_MARGIN),
           B("🎯 В услуги", callback_data=CBT_SERVICES))
    kb.add(B("◀️ Цены", callback_data=CBT_PRICES))
    return kb


def _prices_svc_pick_text() -> str:
    return ("<b>📊 Расчёт по услуге</b>\n\n"
            "Выберите услугу — покажу по каждому объёму: себестоимость "
            "закупки, продажную цену, комиссию FunPay и чистую прибыль.")


def _prices_svc_pick_kb() -> "K":
    kb = K(row_width=1)
    for svc in SERVICE_PRESETS:
        kb.add(B(SERVICE_PRESETS[svc]["name"],
                 callback_data=f"{CBT_PRICES_SVC_DETAIL}:{svc}"))
    kb.add(B("◀️ Назад", callback_data=CBT_PRICES))
    return kb


def _prices_detail_text(s: dict, svc: str) -> str:
    meta = SERVICE_PRESETS.get(svc, {"name": svc, "volumes": []})
    markup = float(s.get("auto_lots_markup_percent", 30.0) or 0)
    fee_pct = float(s.get("funpay_fee_percent", 7.5) or 0)
    lines = [
        f"<b>📊 {meta['name']} — расчёт</b>",
        f"Наценка <code>{markup:.0f}%</code> · комиссия FunPay <code>{fee_pct:.1f}%</code> с продажи",
        "",
    ]
    cache: dict = {}
    provider = _find_default_provider(s)
    for vol in meta.get("volumes", []):
        b = _volume_breakdown(s, provider, svc, vol, cache)
        if b is None:
            lines.append(f"• {vol} шт — цена по запросу")
            continue
        margin = float(b["margin"])
        margin_part = f"маржа <code>{margin:.1f}%</code>" + (" 🔴" if margin < 0 else "")
        profit_part = (f"прибыль <b><code>{b['profit']:.2f}</code></b> ₽"
                       + (" 🔴" if b["profit"] < 0 else ""))
        lines.append(
            f"• <code>{vol}</code> шт — "
            f"закупка <code>{b['cost']:.2f}</code> ₽ · "
            f"продажа <code>{b['price']:.2f}</code> ₽ · "
            f"комиссия <code>{b['fee']:.2f}</code> ₽ · "
            f"{profit_part} · {margin_part}")
    lines.append("")
    lines.append("Чистая прибыль = продажа − комиссия FunPay − закупка. "
                 "Маржа = прибыль / продажа. 🔴 — продажа ниже себестоимости с комиссией.")
    return "\n".join(lines)


def _prices_detail_kb(s: dict, svc: str) -> "K":
    kb = K(row_width=2)
    kb.row(B("🔄 Обновить цены", callback_data=f"{CBT_PRICES_SVC_DETAIL}:{svc}"),
           B("🎯 В услугу", callback_data=f"{CBT_SERVICE_DETAIL}:{svc}"))
    kb.row(B("◀️ К услугам", callback_data=CBT_SERVICES),
           B("◀️ Цены", callback_data=CBT_PRICES))
    return kb


def _advanced_text() -> str:
    s = _load_settings()
    steamsmm = next((p for p in s.get("providers", [])
                     if _provider_style(p) == "rest"), None)
    key_state = ("задан" if (steamsmm or {}).get("api_key")
                 else "не задан")
    return (
        f"<b>⚙️ Настройки</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔑 API-ключ steamsmm.ru: <code>{key_state}</code>\n"
        f"🗺 Привязок: <code>{len(s.get('lot_mappings', []))}</code> · "
        f"👥 Авторегов: <code>{sum(1 for a in _load_accounts() if not a.get('sold'))}</code>\n"
        f"\n"
        f"Разделы:\n"
        f"🏷 Цены и наценка — прибыль-гейт, авто-цены, наценка\n"
        f"💰 Баланс и пауза — контроль баланса, автопауза\n"
        f"📡 Заказы и статусы — повторы, таймауты, опрос\n"
        f"⚙️ Расширенные — шаблоны, домены, ч/список, бэкап, логи"
    )


def _advanced_kb() -> "K":
    kb = K(row_width=1)
    kb.row(B("🔑 API-ключ steamsmm.ru", callback_data=CBT_PROVIDERS),
           B("🗺 Привязки", callback_data=CBT_MAPPINGS))
    kb.row(B("👤 Аккаунты (автореги)", callback_data=CBT_ACCOUNTS))
    kb.row(B("🗑 Удалить/деактивировать отслеживаемые лоты", callback_data=CBT_DELETE_ALL_LOTS))
    kb.row(B("🏷 Цены и наценка", callback_data=CBT_ADV_PRICES))
    kb.row(B("💰 Баланс и пауза", callback_data=CBT_ADV_BALANCE))
    kb.row(B("📡 Заказы и статусы", callback_data=CBT_ADV_ORDERS))
    kb.row(B("⚙️ Расширенные", callback_data=CBT_ADV_EXTRA))
    kb.row(B("◀️ Назад", callback_data=CBT_HOME))
    return kb


def _adv_prices_text() -> str:
    s = _load_settings()
    return (
        f"<b>🏷 Цены и наценка</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🛡 Прибыль-гейт: {_onoff(s.get('profit_guard'))}\n"
        f"💰 Мин. прибыль на заказ: <code>{float(s.get('min_profit', 0)):.2f}</code> ₽\n"
        f"💸 Комиссия FunPay: <code>{float(s.get('funpay_fee_percent', 7.5)):.1f}</code>%\n"
        f"🔁 Авто-цены (наценка): {_onoff(s.get('auto_lots_enabled'))}\n"
        f"🏷 Наценка на цену: <code>{float(s.get('auto_lots_markup_percent', 30)):.0f}</code>%\n"
        f"🌐 Подкатегория авто-лотов: <code>{int(s.get('auto_lot_node_id') or 0)}</code> — "
        f"куда «авто» создаёт новые лоты (0 — базовая 1009)\n"
        f"⏱ Интервал авто-цен: <code>{int(s.get('auto_lots_interval_min', 60))}</code> мин"
    )


def _adv_prices_kb() -> "K":
    s = _load_settings()
    kb = K(row_width=2)
    kb.row(B(("🛡 Прибыль-гейт: 🟢" if s.get("profit_guard") else "🛡 Прибыль-гейт: 🔴"), callback_data=CBT_TOGGLE_PROFIT),
           B("💰 Мин. прибыль", callback_data=CBT_EDIT_MINPROFIT))
    kb.row(B("💸 Комиссия FunPay", callback_data=CBT_EDIT_FEE),
           B(("🔁 Авто-цены: 🟢" if s.get("auto_lots_enabled") else "🔁 Авто-цены: 🔴"), callback_data=CBT_TOGGLE_AUTOLOTS))
    kb.row(B("🏷 Наценка на цену", callback_data=CBT_EDIT_MARKUP),
           B("⏱ Интервал авто-цен", callback_data=CBT_EDIT_AUTOLOTS_INT))
    kb.row(B(f"🌐 Подкатегория авто-лотов: {int(s.get('auto_lot_node_id') or 0)}",
             callback_data=CBT_EDIT_LOT_NODE))
    kb.row(B("◀️ Назад", callback_data=CBT_ADVANCED))
    return kb


def _adv_balance_text() -> str:
    s = _load_settings()
    return (
        f"<b>💰 Баланс и пауза</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⚪ Гейт баланса (гаш. лотов): {_onoff(s.get('balance_gate'))}\n"
        f"⚠️ Контроль баланса: {_onoff(s.get('balance_alert_enabled'))}\n"
        f"📉 Порог баланса: <code>{float(s.get('balance_alert_threshold', 50)):.2f}</code> ₽\n"
        f"⏱ Проверка баланса: <code>{int(s.get('balance_check_interval_min', 10))}</code> мин — "
        f"алерт при падении ниже порога и автопауза\n"
        f"⏸️ Автопауза при низком балансе: {_onoff(s.get('auto_pause_low_balance', True))} — "
        f"остановит продажи и запустит их снова при пополнении\n"
        f"⏳ Период «торгуем остатком»: <code>{float(s.get('auto_pause_grace_hours', 24) or 0):g}</code> ч — "
        f"после ручного запуска при низком балансе пауза не сработает этот срок"
    )


def _adv_balance_kb() -> "K":
    s = _load_settings()
    kb = K(row_width=2)
    kb.row(B(("⚪ Гейт баланса: 🟢" if s.get("balance_gate") else "⚪ Гейт баланса: 🔴"), callback_data=CBT_TOGGLE_BALGATE))
    kb.row(B(("⚠️ Контроль баланса: 🟢" if s.get("balance_alert_enabled") else "⚠️ Контроль баланса: 🔴"), callback_data=CBT_TOGGLE_BALALERT),
           B("📉 Порог баланса", callback_data=CBT_EDIT_BALTHRESH))
    kb.row(B("⏱ Интервал проверки", callback_data=CBT_EDIT_BALINT),
           B(("⏸️ Автопауза: 🟢" if s.get("auto_pause_low_balance", True) else "⏸️ Автопауза: 🔴"), callback_data=CBT_TOGGLE_AUTOPAUSE))
    kb.row(B("⏳ Период остатка", callback_data=CBT_EDIT_GRACE))
    kb.row(B("◀️ Назад", callback_data=CBT_ADVANCED))
    return kb


def _adv_orders_text() -> str:
    s = _load_settings()
    return (
        f"<b>📡 Заказы и статусы</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔄 Повторов заказа: <code>{int(s.get('add_retries', 3))}</code>\n"
        f"🔎 Проверка цены перед заказом: {_onoff(s.get('price_check_enabled'))}\n"
        f"🔔 Уведомления о заказах: {_onoff(s.get('new_order_notifications'))}\n"
        f"🎛 Команды !чек/рефилл: {_onoff(s.get('operator_commands'))} — статус и долив по ID поставщика "
        f"(долив тратит средства — включайте осознанно)\n"
        f"⏱ Ожидание ссылки: <code>{_fmt_duration(s.get('link_wait_timeout_sec', 86400))}</code>\n"
        f"🔄 Опрос статусов: <code>{_fmt_duration(s.get('status_poll_interval_sec', 120))}</code>\n"
        f"🚫 Автоотмена зависшего: {_onoff(s.get('auto_cancel_stuck_enabled', True))} — "
        f"<code>{_fmt_duration(s.get('auto_cancel_stuck_sec', 2700))}</code> (45 мин)"
    )


def _adv_orders_kb() -> "K":
    s = _load_settings()
    kb = K(row_width=2)
    kb.row(B("🔄 Повторов заказа", callback_data=CBT_EDIT_RETRIES),
           B(("🔎 Проверка цены: 🟢" if s.get("price_check_enabled") else "🔎 Проверка цены: 🔴"), callback_data=CBT_TOGGLE_PRICE))
    kb.row(B(("🔔 Уведомления: 🟢" if s.get("new_order_notifications") else "🔔 Уведомления: 🔴"), callback_data=CBT_TOGGLE_NEWORDER))
    kb.row(B(("🎛 !чек/рефилл: 🟢" if s.get("operator_commands") else "🎛 !чек/рефилл: 🔴"), callback_data=CBT_TOGGLE_OPCMDS))
    kb.row(B("⏱ Таймаут ссылки", callback_data=CBT_EDIT_TIMEOUT),
           B("🔄 Интервал опроса", callback_data=CBT_EDIT_POLL))
    kb.row(B("◀️ Назад", callback_data=CBT_ADVANCED))
    return kb


def _adv_extra_text() -> str:
    s = _load_settings()
    return (
        f"<b>⚙️ Расширенные</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⬆️ Авто-поднятие лотов: {_onoff(s.get('auto_raise_enabled'))}\n"
        f"⏱ Интервал поднятия: <code>{int(s.get('auto_raise_interval_min', 60))}</code> мин\n"
        f"📏 Макс. длина описания для матча: <code>{int(s.get('lot_desc_match_limit', 200))}</code> симв.\n"
        f"🌐 Разрешённых доменов: <code>{len(s.get('allowed_link_domains', []))}</code>\n"
        f"🚫 В ч/списке: <code>{len(s.get('blacklist', []))}</code>\n"
        f"🔗 Ссылок в меню: <code>{len(s.get('links_menu', []))}</code>"
    )


def _adv_extra_kb() -> "K":
    s = _load_settings()
    kb = K(row_width=2)
    kb.row(B(("⬆️ Поднятие: 🟢" if s.get("auto_raise_enabled") else "⬆️ Поднятие: 🔴"), callback_data=CBT_TOGGLE_RAISE),
           B("⏱ Интервал поднятия", callback_data=CBT_EDIT_RAISE_INT))
    kb.row(B("📏 Лимит описания", callback_data=CBT_EDIT_DESC_LIMIT),
           B("🚫 Чёрный список", callback_data=CBT_BLACKLIST))
    kb.row(B("📜 Шаблоны сообщений", callback_data=CBT_MSGS),
           B("🌐 Домены ссылок", callback_data=CBT_DOMAINS))
    kb.row(B("🔗 Полезные ссылки", callback_data=CBT_LINKS),
           B("📜 Логи плагина", callback_data=CBT_LOGS))
    kb.row(B("📤 Бэкап", callback_data=CBT_BACKUP_EXPORT),
           B("📥 Восстановить", callback_data=CBT_BACKUP_IMPORT))
    kb.row(B("◀️ Назад", callback_data=CBT_ADVANCED))
    return kb


# разделы «⚙️ Настройки»: имя -> (текст, клавиатура) и имя -> callback раздела
_ADV_SECTION_RENDER: dict[str, tuple] = {
    "prices": (_adv_prices_text, _adv_prices_kb),
    "balance": (_adv_balance_text, _adv_balance_kb),
    "orders": (_adv_orders_text, _adv_orders_kb),
    "extra": (_adv_extra_text, _adv_extra_kb),
}
_ADV_SECTION_CBT = {
    "prices": CBT_ADV_PRICES,
    "balance": CBT_ADV_BALANCE,
    "orders": CBT_ADV_ORDERS,
    "extra": CBT_ADV_EXTRA,
}


def _api_key_screen_text(s: dict) -> str:
    """Текст экрана «🔑 API-ключ steamsmm.ru» (с балансом из кеша, если есть)."""
    steamsmm = next((p for p in s.get("providers", [])
                     if _provider_style(p) == "rest"), None)
    lines = ["<b>🔑 API-ключ steamsmm.ru</b>", ""]
    if not steamsmm:
        lines += [
            "Плагин работает только через steamsmm.ru — нужен только API-ключ.",
            "",
            "Где взять: <b>steamsmm.ru → Личный кабинет → API-ключи → «Создать ключ»</b>.",
            "",
            "Ключ не задан.",
        ]
        return "\n".join(lines)
    lines += [
        f"Ключ: <code>{_mask_secret(steamsmm.get('api_key'))}</code>",
        f"URL: <code>{_html_escape(steamsmm.get('api_url'))}</code>",
    ]
    snap = _balance_snapshot(steamsmm)
    bal = snap.get("amount") if not snap.get("error") else None
    currency = _currency(snap.get("currency"))
    if bal is not None and currency:
        lines.append(f"💰 Баланс: <code>{float(bal):.2f}</code> {_html_escape(currency)}")
    else:
        lines.append("💰 Баланс: — (кнопка «💰 Баланс» ниже)")
    return "\n".join(lines)


def _api_key_screen_kb(s: dict) -> "K":
    steamsmm = next((p for p in s.get("providers", [])
                     if _provider_style(p) == "rest"), None)
    kb = K(row_width=1)
    if not steamsmm:
        kb.add(B("➕ Ввести API-ключ", callback_data=CBT_PROVIDER_ADD))
    else:
        pid = steamsmm.get("id")
        kb.row(B("🔑 Изменить ключ", callback_data=CBT_PROVIDER_ADD),
               B("💰 Баланс", callback_data=f"{CBT_PROVIDER_BAL}:{pid}"))
        kb.row(B("📡 Услуги", callback_data=f"{CBT_PROVIDER_SERVICES}:{pid}:0"),
               B("🗑 Удалить ключ", callback_data=f"{CBT_PROVIDER_DEL}:{pid}"))
    kb.add(B("🔄 Обновить", callback_data=CBT_PROVIDERS))
    kb.add(B("◀️ Назад", callback_data=CBT_ADVANCED))
    return kb


def _help_text() -> str:
    return (
        "<b>❓ Как настроить Steam SMM за 4 шага</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "1️⃣ <b>API-ключ.</b> «⚙️ Настройки → 🔑 API-ключ steamsmm.ru» → «➕ Ввести "
        "API-ключ» — плагин работает только через steamsmm.ru, нужен только ключ.\n"
        "   Где взять: сайт steamsmm.ru → Личный кабинет → API-ключи → "
        "«Создать ключ». Кнопка «💰 Баланс» проверит, что ключ рабочий.\n\n"
        "2️⃣ <b>Привязка лота.</b> «⚙️ Настройки → 🗺 Привязки» → «➕ Добавить». "
        "Введите <b>название лота точно как на FunPay</b> (или его часть), выберите тип:\n"
        "   • <b>услуга</b> — service_id из панели, множитель количества, "
        "себестоимость за единицу (для прибыль-гейта);\n"
        "   • <b>авторег</b> — тег пула с логинами/паролями из «👤 Аккаунты».\n\n"
        "3️⃣ <b>Проверка.</b> Купите свой лот с другого аккаунта — бот попросит ссылку, "
        "создаст заказ у поставщика и сам отследит статус.\n\n"
        "4️⃣ <b>По желанию (⚙️ Настройки → разделы):</b> «🏷 Цены и наценка» (прибыль-гейт, "
        "наценка), «💰 Баланс и пауза», «📡 Заказы и статусы», «⚙️ Расширенные» "
        "(тексты, домены, ч/список, бэкап, логи).\n\n"
        "🗺 <b>Куда что добавлять:</b>\n"
        "   • API-ключ steamsmm.ru → «⚙️ Настройки → 🔑 API-ключ steamsmm.ru»;\n"
        "   • связка «лот FunPay ↔ услуга» → «⚙️ Настройки → 🗺 Привязки»;\n"
        "   • логины/пароли Steam-авторегов → «⚙️ Настройки → 👤 Аккаунты»;\n"
        "   • создание лотов и активация услуг → «🎯 Услуги» на главном.\n\n"
        "💡 Покупатель отслеживает заказ командой <code>!статус</code>."
    )


def _stats_text() -> str:
    orders = _load_orders()
    now = time.time()
    day = _aggregate_profit(orders, now - 86400)
    week = _aggregate_profit(orders, now - 7 * 86400)
    month = _aggregate_profit(orders, now - 30 * 86400)
    def fmt(a):
        return f"{a['count']} шт · доход {a['revenue']} · профит {a['profit']}"
    def fmt_fees(a):
        return f"{a['count']} шт · комиссия {a['fees']}"
    return (
        f"<b>📊 Статистика Steam SMM</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📅 День: {fmt(day)}\n"
        f"🗓 Неделя: {fmt(week)}\n"
        f"📆 Месяц: {fmt(month)}\n"
        f"💸 Комиссия FunPay (всего):\n"
        f"   📅 день: {fmt_fees(day)}\n"
        f"   🗓 неделя: {fmt_fees(week)}\n"
        f"   📆 месяц: {fmt_fees(month)}"
    )


def _reg_step(bot, msg, fn) -> None:
    """Регистрация следующего шага с очисткой «зависших» хендлеров чата.

    telebot хранит next-step хендлеры списком на чат и при новом сообщении
    запускает ВСЕ накопленные. Если пользователь нажал кнопку (например,
    редактор числа) и не ответил, а потом открыл другой визард — на одно
    сообщение сработали бы оба хендлера. Поэтому перед регистрацией
    очищаем список хендлеров чата.
    """
    try:
        bot.clear_step_handler_by_chat_id(msg.chat.id)
    except Exception:
        pass
    bot.register_next_step_handler(msg, fn)


def init(cardinal: "Cardinal", *args) -> None:
    if not getattr(cardinal, "telegram", None):
        return
    tg = cardinal.telegram
    bot = tg.bot

    # 💛 Донат-баннер (защита реквизитов автора)
    global _donation_cardinal
    _donation_cardinal = cardinal
    try:
        tg = getattr(cardinal, "telegram", None)
        if tg:
            tg.cbq_handler(
                _donation_on_cb,
                lambda c: (c.data or "").startswith("ssm_dn:"))
            _start_donation_reminder(cardinal)
    except Exception:
        pass
    # 📦 Одноразовое приветствие с рекламой канала автора
    if DONATION_SHOW_ON_START:
        try:
            _send_startup_welcome(cardinal)
        except Exception:
            logger.debug("startup welcome send failed", exc_info=True)



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

    def _edit_or_send(call, text: str, kb) -> None:
        cid = call.message.chat.id
        try:
            bot.edit_message_text(text, cid, call.message.id, parse_mode="HTML", reply_markup=kb)
        except Exception:
            try:
                sent = bot.send_message(cid, text, parse_mode="HTML", reply_markup=kb)
                _menu_msg_ids[cid] = sent.message_id
            except Exception:
                pass

    def open_settings_cb(call) -> None:
        _persist_op(call.message.chat.id)
        _edit_or_send(call, _home_text(), _home_kb())
        _answer(call)

    def home_cb(call) -> None:
        _edit_or_send(call, _home_text(), _home_kb())
        _answer(call)

    def delete_all_lots_cb(call) -> None:
        chat_id = call.message.chat.id
        state = _delete_all_state_start(chat_id)
        count = len({_lot_target_id(m) for m in _managed_lot_mappings(_load_settings())})
        _edit_or_send(call, _delete_all_confirm_text(1, count),
                      _delete_all_confirm_kb(1, state["token"]))
        _answer(call)

    def delete_all_lots_cancel_cb(call) -> None:
        _delete_all_state_cancel(call.message.chat.id)
        _answer(call, "Удаление отменено")
        _edit_or_send(call, _advanced_text(), _advanced_kb())

    def delete_all_lots_confirm_cb(call) -> None:
        parts = (call.data or "").split(":")
        try:
            step, token = int(parts[-2]), parts[-1]
        except (ValueError, IndexError):
            _answer(call, "Подтверждение недействительно")
            return
        chat_id = call.message.chat.id
        state = _delete_all_state_advance(chat_id, token, step)
        if state is None:
            _answer(call, "Подтверждение устарело или уже использовано")
            _edit_or_send(call, _advanced_text(), _advanced_kb())
            return
        if step < 3:
            count = len({_lot_target_id(m) for m in _managed_lot_mappings(_load_settings())})
            _edit_or_send(call, _delete_all_confirm_text(step + 1, count),
                          _delete_all_confirm_kb(step + 1, token))
            _answer(call)
            return
        _delete_all_state_cancel(chat_id)
        _answer(call, "Удаление запущено")
        result = _delete_managed_lots(cardinal, _load_settings())
        lines = ["<b>🗑 Обработка лотов Steam SMM завершена</b>", "",
                 f"Отслеживалось: <code>{result['tracked']}</code>",
                 f"Подтверждён успех: <code>{result['succeeded']}</code>",
                 f"Очищено привязок: <code>{result['bindings_cleared']}</code>",
                 f"Ошибок: <code>{len(result['failed'])}</code>", "",
                 "<b>По каждому лоту:</b>"]
        for item in result["statuses"][:30]:
            mark = "✅" if item["ok"] else "❌"
            lines.append(f"{mark} <code>{item['lot_id']}</code>: "
                         f"{_html_escape(item['detail'])}")
        lines.append("\nℹ️ Неактивный лот может оставаться в «Ваших предложениях», "
                     "но он снят с активной продажи. Неудачные ID сохранены "
                     "в привязках для безопасного повтора.")
        _log_action("managed_lots_delete_all", "массовое удаление/деактивация",
                    tracked=result["tracked"], succeeded=result["succeeded"],
                    bindings_cleared=result["bindings_cleared"],
                    failed=len(result["failed"]))
        _edit_or_send(call, "\n".join(lines), _advanced_kb())

    def toggle_sales_cb(call) -> None:
        s = _load_settings()
        s["sales_enabled"] = not s.get("sales_enabled", True)
        if s["sales_enabled"]:
            was_autopaused = s.get("auto_pause_active", False)
            # ручной запуск снимает автопаузу — иначе автопауза может вернуть
            # продажи/снова остановить их вопреки решению оператора
            s["auto_pause_active"] = False
            # период «торгуем остатком»: не паузить N часов после ручного запуска
            rest = next((p for p in s.get("providers", [])
                         if _provider_style(p) == "rest"
                         and (p.get("api_key") or "").strip()), None)
            bal = _balance_cached(rest) if rest else None
            if was_autopaused:
                # оператор отменяет АКТИВНУЮ автопаузу — грейс безусловно
                # (кеш баланса мог устареть, решение не зависит от него)
                _manual_restart_grace(s, None)
            else:
                _manual_restart_grace(s, bal)
        _save_settings(s)
        _answer(call, "🛑 Продажи остановлены" if not s["sales_enabled"] else "▶️ Продажи запущены")
        _edit_or_send(call, _home_text(), _home_kb())

    def toggle_maintenance_cb(call) -> None:
        s = _load_settings()
        s["maintenance_mode"] = not s.get("maintenance_mode", False)
        _save_settings(s)
        _log_action("settings_changed", "maintenance toggled", maintenance_mode=s["maintenance_mode"])
        _answer(call, "🛠 Обслуживание включено" if s["maintenance_mode"] else "✅ Обслуживание выключено")
        _edit_or_send(call, _home_text(), _home_kb())

    def services_cb(call) -> None:
        _edit_or_send(call, _services_text(), _services_kb())
        _answer(call)

    def import_cb(call) -> None:
        _edit_or_send(call, _import_text(), _import_kb())
        _answer(call)

    def import_src_cb(call) -> None:
        key = call.data.split(":", 2)[-1]
        items = _parse_import_source(key)
        if not items:
            _answer(call, "❌ Источник не найден или пуст")
            _edit_or_send(call, _import_text(), _import_kb())
            return
        s = _load_settings()
        report = _import_items(s, items, _IMPORT_SOURCE_LABELS.get(key, key))
        # отвечаем на колбэк ДО сетевого запроса каталога FunPay, чтобы
        # Telegram не считал колбэк зависшим
        _answer(call, "📥 Импорт выполнен")
        lines = [f"<b>📥 Импорт из {_IMPORT_SOURCE_LABELS.get(key, key)}</b>", ""]
        if report["added"]:
            if report["drafts"] and report["added"] == report["drafts"]:
                lines.append(f"⚠️ Добавлено <code>{report['drafts']}</code> привязок без ID лота "
                             f"(работают по названию)")
            else:
                lines.append(f"✅ Добавлено привязок: <code>{report['added']}</code>")
                if report["drafts"]:
                    lines.append(f"⚠️ Из них без ID лота: <code>{report['drafts']}</code> "
                                 f"(работают по названию)")
        if report["skipped_dup"]:
            lines.append(f"⏭ Уже есть (пропущены): <code>{report['skipped_dup']}</code>")
        if report["skipped_unmapped"]:
            lines.append(f"❓ Услуга не распознана: <code>{report['skipped_unmapped']}</code> "
                         f"— привяжите вручную")
        if report["skipped_bad"]:
            lines.append(f"⚠️ Некорректный объём: <code>{report['skipped_bad']}</code>")
        # Шаг 2: авто-поиск ID лотов для черновиков через каталог FunPay
        if report["drafts"]:
            lots = _account_lots(cardinal)
            if lots:
                res = _resolve_draft_lot_ids(s, lots)
                if res["resolved"]:
                    lines.append(f"🔍 Авто-поиск ID лотов: найдено <code>{res['resolved']}</code>")
                # цены выставляем и для флагов с прошлых запусков (ретрай)
                if any(m.get("draft_resolved")
                       for m in s.get("lot_mappings", [])):
                    priced = _price_resolved_lots(cardinal, s)
                    if priced["priced"]:
                        lines.append(f"🏷 Цены выставлены <code>{priced['priced']}</code> "
                                     f"лотам по наценке")
                    if priced["failed"]:
                        lines.append(f"⚠️ Не удалось выставить цену: "
                                     f"<code>{priced['failed']}</code> — флаги сохранены")
                    if priced["no_account"]:
                        lines.append("🏷 Нет доступа к FunPay-аккаунту — цены не выставлены, "
                                     "повторите позже")
                if res["ambiguous"]:
                    lines.append(f"🤔 Неоднозначных названий: <code>{res['ambiguous']}</code> "
                                 f"— привяжите вручную")
                if res["not_found"]:
                    lines.append(f"❌ Не найдено в каталоге: <code>{res['not_found']}</code> "
                                 f"— создайте лот на FunPay и привяжите ID")
            else:
                lines.append("🔍 Каталог FunPay недоступен — ID лотов не искал, "
                             "привяжите вручную в «🎯 Услуги»")
        if not any(report.values()):
            lines.append("Ничего не перенесено.")
        _save_settings(s)
        # остались черновики — предложить поиск ID; иначе кнопки «в услуги/главное»
        kb = _import_kb() if _draft_mapping_count(s) else _import_done_kb()
        _edit_or_send(call, "\n".join(lines), kb)

    def import_resolve_cb(call) -> None:
        s = _load_settings()
        lots = _account_lots(cardinal)
        if not lots:
            _answer(call, "🔍 Каталог FunPay недоступен")
            _edit_or_send(call, _import_text(), _import_kb())
            return
        res = _resolve_draft_lot_ids(s, lots)
        has_flags = any(m.get("draft_resolved") for m in s.get("lot_mappings", []))
        priced = _price_resolved_lots(cardinal, s) if has_flags else None
        _save_settings(s)
        lines = ["<b>🔍 Авто-привязка ID лотов</b>", ""]
        if res["resolved"]:
            lines.append(f"✅ Найдено ID: <code>{res['resolved']}</code>")
        if priced and priced["priced"]:
            lines.append(f"🏷 Цены выставлены <code>{priced['priced']}</code> "
                         f"лотам по наценке")
        if priced and priced["failed"]:
            lines.append(f"⚠️ Не удалось выставить цену: <code>{priced['failed']}</code> — "
                         f"флаги сохранены, повторите позже")
        if priced and priced["no_account"]:
            lines.append("🏷 Нет доступа к FunPay-аккаунту — цены не выставлены")
        if res["ambiguous"]:
            lines.append(f"🤔 Неоднозначных названий: <code>{res['ambiguous']}</code> "
                         f"— привяжите вручную в карточке услуги")
        if res["not_found"]:
            lines.append(f"❌ Не найдено: <code>{res['not_found']}</code> — "
                         f"создайте лот на FunPay с таким названием и повторите")
        if not any(res.values()) and not (priced and (priced["priced"] or priced["failed"] or priced["no_account"])):
            lines.append("Черновиков без ID лота не осталось.")
        _answer(call, "🔍 Готово")
        _edit_or_send(call, "\n".join(lines), _import_kb())

    def export_cb(call) -> None:
        _answer(call, "📤 Экспорт выполнен")
        r = _export_mappings_to_autosmm()
        lines = ["<b>📤 Экспорт в autosmm_fpc</b>", ""]
        if not r.get("saved"):
            lines.append("❌ Не удалось сохранить файл — проверьте права на storage/.")
        else:
            lines.append(f"✅ Экспортировано: <code>{r['exported']}</code>")
        if r["skipped_dup"]:
            lines.append(f"⏭ Уже есть в autosmm: <code>{r['skipped_dup']}</code>")
        if r["skipped_empty"]:
            lines.append(f"⚠️ Без названия лота: <code>{r['skipped_empty']}</code> — пропущены")
        if r["non_numeric"]:
            lines.append(f"⚠️ С кодовым service_id: <code>{r['non_numeric']}</code> — в autosmm "
                         f"нужны числовые ID панели, поправьте их в autosmm")
        lines.append(f"Всего привязок в autosmm: <code>{r['existing_total']}</code>")
        lines.append(f"Файл: <code>{_html_escape(r['path'])}</code>")
        _edit_or_send(call, "\n".join(lines), _import_kb())

    def service_detail_cb(call) -> None:
        svc = call.data.split(":", 2)[-1]
        if svc not in SERVICE_PRESETS:
            _answer(call, "услуга не найдена")
            return
        s = _load_settings()
        _edit_or_send(call, _service_detail_text(s, svc), _service_detail_kb(s, svc))
        _answer(call)

    def service_del_cb(call) -> None:
        _, _, rest = call.data.partition(f"{CBT_SERVICE_DEL}:")
        svc, _, vol = rest.partition(":")
        s = _load_settings()
        s["lot_mappings"] = [m for m in s.get("lot_mappings", [])
                              if not (str(m.get("service_id") or "") == str(svc)
                                      and int(m.get("qty_multiplier", 1) or 1) == int(vol))]
        _save_settings(s)
        _answer(call, "🗑 Отвязано")
        _edit_or_send(call, _service_detail_text(s, svc), _service_detail_kb(s, svc))

    def service_lotid_cb(call) -> None:
        """🔍 Найти ID в карточке услуги: ищет лот по названию черновика.

        Если ID уже привязан, но цена не выставилась (draft_resolved остался),
        работает как повторная попытка выставления цены."""
        _, _, rest = call.data.partition(f"{CBT_SERVICE_LOTID}:")
        svc, _, vol_s = rest.partition(":")
        if svc not in SERVICE_PRESETS or not vol_s.isdigit():
            _answer(call, "услуга не найдена")
            return
        vol = int(vol_s)
        s = _load_settings()
        mapping = _mapping_for_volume(s, svc, vol)
        if mapping is None:
            _answer(call, "черновик не найден")
            _edit_or_send(call, _service_detail_text(s, svc), _service_detail_kb(s, svc))
            return
        if _lot_target_id(mapping) is not None:
            # ID уже есть — только цена не выставилась: пробуем ещё раз
            if not mapping.get("draft_resolved"):
                _answer(call, "лот уже привязан")
                _edit_or_send(call, _service_detail_text(s, svc), _service_detail_kb(s, svc))
                return
            status = _price_one_mapping(cardinal, s, mapping)
            _save_settings(s)
            note = {"ok": "цена выставлена по наценке",
                    "no_account": "нет доступа к FunPay-аккаунту — попробуйте позже",
                    "failed": "цена не выставлена — попробуйте позже"}[status]
            _answer(call, f"✅ {note}")
            _edit_or_send(call, _service_detail_text(s, svc), _service_detail_kb(s, svc))
            return
        name = str(mapping.get("lot_match") or "").strip()
        lots = _account_lots(cardinal)
        if not lots:
            _answer(call, "🔍 Каталог FunPay недоступен")
            return
        matches = _match_lots_by_name(lots, name)
        if not matches:
            _answer(call, "❌ Совпадений не найдено")
            bot.send_message(
                call.message.chat.id,
                f"🔍 По названию «<code>{_html_escape(name)}</code>» в каталоге "
                f"FunPay ничего не найдено.\n\n"
                f"Создайте лот на FunPay с таким названием и нажмите "
                f"«🔍 Найти ID {vol} шт» ещё раз, либо привяжите ID вручную: "
                f"«🗑 Отвязать» → «➕ Создать {vol} шт» → введите ID.",
                parse_mode="HTML")
            return
        if len(matches) == 1:
            lid = int(matches[0].id)
            status = _bind_draft_lot_id(cardinal, s, mapping, vol, lid)
            _save_settings(s)
            note = {"ok": "цена выставлена по наценке",
                    "no_account": "цена выставится, когда появится FunPay-аккаунт",
                    "failed": "цена не выставлена — повторите позже"}[status]
            _answer(call, f"✅ Лот {lid} привязан, {note}")
            _edit_or_send(call, _service_detail_text(s, svc), _service_detail_kb(s, svc))
            return
        # несколько совпадений — выбор кнопками
        kb = K(row_width=1)
        for lot in matches:
            title = _html_escape(_lot_title(lot) or str(lot.id))
            kb.add(B(f"🆔 {lot.id} — {title[:40]}",
                     callback_data=f"{CBT_SERVICE_LOTID_PICK}:{svc}:{vol}:{lot.id}"))
        kb.row(B("◀️ Отмена", callback_data=f"{CBT_SERVICE_DETAIL}:{svc}"))
        bot.send_message(
            call.message.chat.id,
            f"🔍 По названию «<code>{_html_escape(name)}</code>» найдено "
            f"<code>{len(matches)}</code> лотов. Выберите нужный:",
            parse_mode="HTML", reply_markup=kb)
        _answer(call)

    def service_lotid_pick_cb(call) -> None:
        """Выбор конкретного лота из списка совпадений."""
        _, _, rest = call.data.partition(f"{CBT_SERVICE_LOTID_PICK}:")
        svc, _, rest2 = rest.partition(":")
        vol_s, _, lid_s = rest2.partition(":")
        if svc not in SERVICE_PRESETS or not vol_s.isdigit() or not lid_s.isdigit():
            _answer(call, "неверные данные")
            return
        vol, lid = int(vol_s), int(lid_s)
        s = _load_settings()
        mapping = _mapping_for_volume(s, svc, vol)
        if mapping is None or _lot_target_id(mapping) is not None:
            _answer(call, "черновик не найден")
            _edit_or_send(call, _service_detail_text(s, svc), _service_detail_kb(s, svc))
            return
        status = _bind_draft_lot_id(cardinal, s, mapping, vol, lid)
        _save_settings(s)
        note = {"ok": "цена выставлена по наценке",
                "no_account": "цена выставится, когда появится FunPay-аккаунт",
                "failed": "цена не выставлена — повторите позже"}[status]
        _answer(call, f"✅ Лот {lid} привязан, {note}")
        _edit_or_send(call, _service_detail_text(s, svc), _service_detail_kb(s, svc))

    def service_toggle_sales_cb(call) -> None:
        svc = call.data.split(":", 2)[-1]
        if svc not in SERVICE_PRESETS:
            _answer(call, "услуга не найдена")
            return
        s = _load_settings()
        cur = _service_sales_enabled(s, svc)
        _set_service_sales(s, svc, not cur)
        _save_settings(s)
        _answer(call, "🟢 Продажа включена" if not cur else "🔴 Продажа выключена")
        _edit_or_send(call, _service_detail_text(s, svc), _service_detail_kb(s, svc))

    def _try_auto_create_and_bind(call, svc: str, vol: int) -> tuple[bool, str]:
        """Пытается создать лот на FunPay автоматически и привязать его к услуге.

        Лот создаётся в объёме, который оператор выбрал кнопкой «➕ Создать N шт»
        (цена за этот объём, множитель привязки = N, минимум в описании).
        Возвращает (True, '') при успехе, иначе (False, причина)."""
        vol = max(1, int(vol or 1))
        existing_id = _existing_lot_for_service(cardinal, _load_settings(), svc, vol)
        if existing_id is not None:
            return False, (f"дубликат не создан: эта категория и объём уже используют "
                           f"лот ID {existing_id}. Откройте и используйте существующий ID")
        _answer(call, "⏳ Создаю лот на FunPay…")
        nodes = (1009, 1351) if svc == "commend_cs2" else (None,)
        outcomes: list[tuple[int | None, int | None, str]] = []
        for node_id in nodes:
            min_out: list = []
            lot_id, reason = _auto_create_lot(
                cardinal, svc, vol, min_out=min_out, node_id_override=node_id)
            outcomes.append((node_id, lot_id, reason))
            if lot_id is None:
                continue
            min_qty = min_out[0] if min_out else None
            try:
                _bind_service_lot(cardinal, call.message.chat.id, svc, vol, int(lot_id),
                                  min_qty=min_qty, node_id=node_id)
            except Exception:
                logger.debug(f"{LOGGER_PREFIX} bind после автосоздания не удался", exc_info=True)
        successes = [(node, lot_id) for node, lot_id, _ in outcomes if lot_id is not None]
        if successes:
            lines = [f"✅ Узел {node}: лот <code>{lot_id}</code>" for node, lot_id in successes]
            lines.extend(f"❌ Узел {node}: {reason}" for node, lot_id, reason in outcomes
                         if lot_id is None)
            try:
                bot.send_message(call.message.chat.id, "\n".join(lines), parse_mode="HTML")
            except Exception:
                pass
            return True, ""
        return False, "; ".join(f"узел {node}: {reason}" for node, _, reason in outcomes)

    def _auto_create_failed(call, svc: str, vol: int, reason: str | None = None) -> None:
        """Авто-создание лота не удалось — причина и что настроить (без ручного ID).

        Ручное создание лота и ввод его ID убраны: лоты создаются только
        автоматически, оператор исправляет настройки и жмёт «➕ Создать» снова.
        """
        _answer(call)
        hint = f"\nℹ️ Причина: {reason}\n" if reason else ""
        category = SERVICE_PRESETS.get(svc, {}).get("name", svc)
        lines = [
            f"❌ <b>Авто-создание лота не удалось</b>: {category} "
            f"(<code>{svc}</code>) × <code>{vol}</code>.{hint}",
            "",
        ]
        low_reason = (reason or "").lower()
        if "funpay" in low_reason or "fields[" in low_reason or "подкатегор" in low_reason:
            lines.extend([
                "Это ошибка формы FunPay, не API-ключа поставщика.",
                "Проверьте 🌐 подкатегорию авто-лотов: в ней должен быть тип, "
                "соответствующий этой услуге.",
            ])
        else:
            lines.extend([
                "Проверьте относящуюся к причине настройку и повторите попытку.",
            ])
        try:
            bot.send_message(call.message.chat.id, "\n".join(lines), parse_mode="HTML")
        except Exception:
            pass

    def service_create_cb(call) -> None:
        _, _, rest = call.data.partition(f"{CBT_SERVICE_CREATE}:")
        svc, _, vol = rest.partition(":")
        if svc not in SERVICE_PRESETS:
            _answer(call, "услуга не найдена")
            return
        vol = int(vol)
        s = _load_settings()
        cache: dict = {}
        margin = _volume_margin(s, svc, vol, cache)
        if margin is not None and margin < 0:
            b = _volume_breakdown(s, _find_default_provider(s), svc, vol, cache)
            kb = K(row_width=2)
            kb.row(B("⚠️ Всё равно создать",
                     callback_data=f"{CBT_SERVICE_CREATE_CONFIRM}:{svc}:{vol}"),
                   B("◀️ Отмена", callback_data=f"{CBT_SERVICE_DETAIL}:{svc}"))
            lines = [
                f"<b>⚠️ Лот будет убыточным</b>",
                f"{SERVICE_PRESETS[svc]['name']} — объём <code>{vol} шт</code>:",
                "",
                f"закупка <code>{b['cost']:.2f}</code> ₽ · продажа <code>{b['price']:.2f}</code> ₽ · "
                f"комиссия <code>{b['fee']:.2f}</code> ₽ · "
                f"прибыль <b><code>{b['profit']:.2f}</code></b> ₽ · "
                f"маржа <code>{b['margin']:.1f}%</code> 🔴",
                "",
                "Продажа ниже себестоимости с учётом комиссии FunPay. "
                "Повысьте наценку в «💰 Цены» или подтвердите создание.",
            ]
            bot.send_message(call.message.chat.id, "\n".join(lines),
                             parse_mode="HTML", reply_markup=kb)
            _answer(call)
            return
        # сразу пробуем создать лот автоматически (подкатегория из настроек)
        ok, reason = _try_auto_create_and_bind(call, svc, vol)
        if ok:
            return
        _auto_create_failed(call, svc, vol, reason)

    def service_create_confirm_cb(call) -> None:
        _, _, rest = call.data.partition(f"{CBT_SERVICE_CREATE_CONFIRM}:")
        svc, _, vol = rest.partition(":")
        if svc not in SERVICE_PRESETS:
            _answer(call, "услуга не найдена")
            return
        # сразу пробуем создать лот автоматически (подкатегория из настроек)
        ok, reason = _try_auto_create_and_bind(call, svc, int(vol))
        if ok:
            return
        _auto_create_failed(call, svc, int(vol), reason)

    def prices_cb(call) -> None:
        s = _load_settings()
        _edit_or_send(call, _prices_text(), _prices_kb(s))
        _answer(call)

    def prices_recalc_cb(call) -> None:
        s = _load_settings()
        cardinal_account = getattr(cardinal, "account", None)
        if cardinal_account is not None and hasattr(cardinal_account, "get_lot_fields"):
            try:
                _auto_lots_pass(cardinal, s)
                _answer(call, "🔄 Цены пересчитаны")
            except Exception as e:
                _answer(call, f"⚠️ Ошибка пересчёта: {_short_err(e)}")
        else:
            _answer(call, "⚠️ Нет доступа к FunPay")
        _edit_or_send(call, _prices_text(), _prices_kb(s))

    def prices_svc_cb(call) -> None:
        _edit_or_send(call, _prices_svc_pick_text(), _prices_svc_pick_kb())
        _answer(call)

    def prices_margin_cb(call) -> None:
        _edit_or_send(call, _prices_margin_text(), _prices_margin_kb())
        _answer(call)

    def prices_check_cb(call) -> None:
        _edit_or_send(call, _prices_check_text(), _prices_check_kb())
        _answer(call)

    def prices_svc_detail_cb(call) -> None:
        svc = call.data.split(":", 2)[-1]
        if svc not in SERVICE_PRESETS:
            _answer(call, "услуга не найдена")
            return
        s = _load_settings()
        _edit_or_send(call, _prices_detail_text(s, svc), _prices_detail_kb(s, svc))
        _answer(call)

    def toggle_round_cb(call) -> None:
        s = _load_settings()
        s["qty_rounding"] = not s.get("qty_rounding", True)
        _save_settings(s)
        _answer(call)
        _edit_or_send(call, _prices_text(), _prices_kb(s))

    def toggle_autoprices_cb(call) -> None:
        s = _load_settings()
        s["auto_lots_enabled"] = not s.get("auto_lots_enabled", False)
        _save_settings(s)
        _answer(call)
        _edit_or_send(call, _prices_text(), _prices_kb(s))

    def backup_export_cb(call) -> None:
        blob = json.dumps(_build_backup(), ensure_ascii=False, indent=2).encode("utf-8")
        _answer(call)
        try:
            import io
            bio = io.BytesIO(blob)
            bio.name = "steam_smm_backup.json"
            bot.send_document(call.message.chat.id, bio, caption="📤 Полный бэкап Steam SMM")
        except Exception:
            bot.send_message(call.message.chat.id,
                             f"<pre>{_html_escape(blob.decode('utf-8')[:3500])}</pre>",
                             parse_mode="HTML")

    def backup_import_cb(call) -> None:
        msg = bot.send_message(call.message.chat.id,
                               "📥 Пришлите файл бэкапа <code>steam_smm_backup.json</code> "
                               "(или вставьте JSON текстом). Настройки и автореги будут заменены.",
                               parse_mode="HTML")
        _answer(call)

        def handle(m):
            try:
                if getattr(m, "document", None):
                    file_info = bot.get_file(m.document.file_id)
                    raw = bot.download_file(file_info.file_path)
                    data = json.loads(raw.decode("utf-8"))
                else:
                    data = json.loads((m.text or "").strip())
            except Exception as e:
                return bot.reply_to(m, f"❌ Не удалось разобрать JSON: {e}")
            try:
                summary = _restore_backup(data)
            except Exception as e:
                return bot.reply_to(m, f"❌ Ошибка восстановления: {e}")
            bot.reply_to(m, summary)
        _reg_step(bot, msg, handle)

    def advanced_cb(call) -> None:
        _edit_or_send(call, _advanced_text(), _advanced_kb())
        _answer(call)

    def _render_adv_section(call, section: str) -> None:
        if section in _ADV_SECTION_RENDER:
            text_fn, kb_fn = _ADV_SECTION_RENDER[section]
            _edit_or_send(call, text_fn(), kb_fn())
        else:
            _edit_or_send(call, _advanced_text(), _advanced_kb())
        _answer(call)

    def adv_prices_cb(call) -> None:
        _render_adv_section(call, "prices")

    def adv_balance_cb(call) -> None:
        _render_adv_section(call, "balance")

    def adv_orders_cb(call) -> None:
        _render_adv_section(call, "orders")

    def adv_extra_cb(call) -> None:
        _render_adv_section(call, "extra")

    def help_cb(call) -> None:
        kb = K().add(B("◀️ Назад", callback_data=CBT_HOME))
        _edit_or_send(call, _help_text(), kb)
        _answer(call)

    def stats_cb(call) -> None:
        kb = K(row_width=2)
        kb.add(B("🔄 Обновить", callback_data=CBT_STATS),
               B("◀️ Назад", callback_data=CBT_HOME))
        _edit_or_send(call, _stats_text(), kb)
        _answer(call)

    def active_cb(call) -> None:
        active = _load_active()
        kb = K(row_width=1)
        entries = [(bid, a) for bid, items in active.items() for a in items]
        if not entries:
            text = "📦 Активных заказов нет."
        else:
            lines = [f"<b>📦 Активные заказы</b> ({len(entries)})", ""]
            for bid, a in entries[:30]:
                lines.append(f"• <code>{bid}</code> → #{a.get('order_id_funpay')} (пост. {a.get('provider_order_id')})")
                kb.add(B(f"🔎 #{a.get('order_id_funpay','?')} — {bid}", callback_data=f"{CBT_ACTIVE_DETAIL}:{bid}"))
                kb.add(B(f"🧹 Очистить {bid}", callback_data=f"{CBT_ACTIVE_CLEAR_CONFIRM}:{bid}"))
            text = "\n".join(lines)
        kb.add(B("🔄 Обновить", callback_data=CBT_ACTIVE))
        kb.add(B("◀️ Назад", callback_data=CBT_HOME))
        _edit_or_send(call, text, kb)
        _answer(call)

    def history_cb(call) -> None:
        filter_status = call.data.split(":", 1)[1] if call.data.startswith(f"{CBT_HISTORY}") and ":" in call.data else ""
        parts = call.data.split(":") if call.data.startswith(f"{CBT_HISTORY}") else []
        if len(parts) == 3:
            filter_status = parts[1]
        try:
            offset = int(parts[2]) if len(parts) >= 3 else 0
        except Exception:
            offset = 0
        if filter_status not in ("", "success", "failure", "refunded", "in_progress"):
            filter_status = ""
        orders = _load_orders()
        if filter_status:
            orders = [o for o in orders if o.get("status") == filter_status]
        orders = sorted(orders, key=lambda o: float(o.get("created_at", 0) or 0), reverse=True)
        total = len(orders)
        per_page = 10
        page = orders[offset:offset + per_page]
        lines = [f"<b>📜 История заказов</b> ({total}{' · ' + filter_status if filter_status else ''})", ""]
        kb = K(row_width=3)
        if not page:
            lines.append("(пусто)")
        for o in page:
            sid = _html_escape(str(o.get("order_id", "?")))
            prov = _html_escape(str(o.get("provider_order_id", "")))
            sold = float(o.get("sold_price", 0) or 0)
            prof = float(o.get("profit", 0) or 0)
            lines.append(f"• #{sid} → {prov or '—'}: {o.get('status')} · {sold:.0f}₽ · {prof:+.1f}₽")
            kb.add(B(f"🔎 #{sid}", callback_data=f"{CBT_ORDER_DETAIL}:{o.get('order_id')}"))
        pages = max(1, (total + per_page - 1) // per_page)
        nav = []
        if offset > 0:
            nav.append(B("◀️", callback_data=f"{CBT_HISTORY}:{filter_status}:{max(0, offset - per_page)}"))
        nav.append(B(f"{offset // per_page + 1}/{pages}", callback_data=f"{CBT_HISTORY}"))
        if offset + per_page < total:
            nav.append(B("▶️", callback_data=f"{CBT_HISTORY}:{filter_status}:{offset + per_page}"))
        if nav:
            kb.row(*nav)
        if filter_status:
            kb.row(B("⚠️ Все статусы", callback_data=f"{CBT_HISTORY}"))
        kb.row(B("✅ Успешные", callback_data=f"{CBT_HISTORY}:success:0"))
        kb.row(B("❌ Фейлы", callback_data=f"{CBT_HISTORY}:failure:0"),
               B("🚫 Возвраты", callback_data=f"{CBT_HISTORY}:refunded:0"))
        kb.row(B("◀️ Назад", callback_data=CBT_HOME))
        _edit_or_send(call, "\n".join(lines), kb)
        _answer(call)

    def order_detail_cb(call) -> None:
        oid = call.data.split(":", 2)[-1]
        o = None
        for rec in _load_orders():
            if str(rec.get("order_id")) == str(oid):
                o = rec
                break
        kb = K(row_width=1)
        if not o:
            _answer(call, "не найден")
            kb.add(B("◀️ Назад", callback_data=CBT_HISTORY))
            _edit_or_send(call, "Заказ не найден.", kb)
            return
        text = (
            f"<b>🧾 Заказ #{_html_escape(str(o.get('order_id')))}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Статус: <code>{o.get('status')}</code>\n"
            f"Покупатель (id): <code>{o.get('buyer_id', '—')}</code>\n"
            f"Поставщик: <code>{o.get('provider_id', '—')}</code>\n"
            f"ID у поставщика: <code>{o.get('provider_order_id', '—')}</code>\n"
            f"Продано: <code>{float(o.get('sold_price', 0) or 0):.2f}</code> ₽\n"
            f"Себестоимость: <code>{float(o.get('provider_cost', 0) or 0):.2f}</code> ₽\n"
            f"Комиссия FunPay: <code>{float(o.get('funpay_fee', 0) or 0):.2f}</code> ₽\n"
            f"Прибыль: <code>{float(o.get('profit', 0) or 0):.2f}</code> ₽"
        )
        if o.get("refund_reason"):
            text += f"\nПричина возврата: {_html_escape(str(o.get('refund_reason')))}"
        text += f"\nСоздан: <code>{_fmt_ts(o.get('created_at'))}</code>"
        kb.add(B("◀️ Назад", callback_data=CBT_HISTORY))
        _edit_or_send(call, text, kb)
        _answer(call)

    def active_detail_cb(call) -> None:
        bid = call.data.split(":", 2)[-1]
        a = get_buyer_active_order(bid)
        kb = K(row_width=1)
        if not a:
            _answer(call, "не найден")
            kb.add(B("◀️ Назад", callback_data=CBT_ACTIVE))
            _edit_or_send(call, "Заказ не найден (возможно, уже завершён).", kb)
            return
        s = _load_settings()
        pid = a.get("provider_id")
        provider = _find_provider(s, pid) if pid else None
        poid = a.get("provider_order_id")
        data = None
        if provider and poid:
            try:
                data = _client_for_provider(provider).status(
                    poid, commend=(str(a.get("service_id") or "") == "commend_cs2"))
            except Exception as e:
                data = {"error": f"ошибка запроса: {e}"}
        text = _active_order_card(bid, a, provider, data)
        kb.add(B("🔄 Обновить статус", callback_data=f"{CBT_ACTIVE_DETAIL}:{bid}"))
        kb.add(B("🧹 Очистить", callback_data=f"{CBT_ACTIVE_CLEAR_CONFIRM}:{bid}"))
        kb.add(B("◀️ Назад", callback_data=CBT_ACTIVE))
        _edit_or_send(call, text, kb)
        _answer(call)

    def active_clear_confirm_cb(call) -> None:
        bid = call.data.split(":", 2)[-1]
        a = get_buyer_active_order(bid)
        if not a:
            _answer(call, "не найден")
            return active_cb(call)
        kb = K(row_width=2)
        kb.add(B("✅ Да, удалить", callback_data=f"{CBT_ACTIVE_CLEAR}:{bid}"),
               B("❌ Отмена", callback_data=CBT_ACTIVE))
        _edit_or_send(call, f"🧹 Точно очистить активный заказ покупателя <code>{bid}</code> (#{a.get('order_id_funpay')})?", kb)
        _answer(call)

    def active_clear_cb(call) -> None:
        bid = call.data.split(":", 2)[-1]
        remove_buyer_active_order(bid)
        _answer(call, "🧹 Очищено")
        active_cb(call)

    def links_cb(call) -> None:
        s = _load_settings()
        links = s.get("links_menu", [])
        kb = K(row_width=2)
        for i, item in enumerate(links):
            url = item.get("url", "")
            label = item.get("label", url) or url
            if _is_url_https(url):
                kb.row(B(label, url=url), B("🗑", callback_data=f"{CBT_LINK_DEL}:{i}"))
            else:
                kb.row(B(_html_escape(label), callback_data=f"{CBT_LINK_DEL}:{i}"))
        kb.add(B("➕ Добавить ссылку", callback_data=CBT_LINK_ADD))
        kb.add(B("◀️ Назад", callback_data=CBT_ADV_EXTRA))
        _edit_or_send(call, "🔗 Полезные ссылки (реф-ссылки и прочее):", kb)
        _answer(call)

    def link_add_cb(call) -> None:
        msg = bot.send_message(call.message.chat.id, "➕ Введите ссылку в формате: Название | https://...")
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
        _reg_step(bot, msg, handle)

    def link_del_cb(call) -> None:
        idx = call.data.split(":", 2)[-1]
        s = _load_settings()
        links = s.get("links_menu", [])
        try:
            i = int(idx)
        except Exception:
            i = -1
        label = links[i].get("label", "?") if 0 <= i < len(links) else "?"
        kb = K(row_width=2)
        kb.add(B("✅ Да", callback_data=f"{CBT_LINK_DEL_CONFIRM}:{i}"),
               B("❌ Отмена", callback_data=CBT_LINKS))
        _edit_or_send(call, f"🗑 Удалить ссылку «<code>{_html_escape(label)}</code>»?", kb)
        _answer(call)

    def link_del_confirm_cb(call) -> None:
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

    def logs_cb(call) -> None:
        text = "<b>📜 Логи плагина Steam SMM</b>\n<pre>" \
               + _html_escape(_read_actions_log()[-3500:]) + "</pre>"
        kb = K(row_width=2)
        kb.add(B("📤 Выгрузить файл", callback_data=CBT_LOGS_DOWNLOAD),
               B("🧹 Очистить", callback_data=CBT_LOGS_CLEAR))
        kb.row(B("◀️ Назад", callback_data=CBT_ADV_EXTRA))
        _edit_or_send(call, text, kb)
        _answer(call)

    def logs_download_cb(call) -> None:
        """Присылает файл actions.log документом (полный лог, включая ротацию)."""
        _answer(call, "📤")
        chunks = _read_actions_log_chunks()
        if not chunks:
            try:
                bot.send_message(call.message.chat.id, "📭 Лог пуст.")
            except Exception:
                pass
            return
        try:
            import io
            blob = io.BytesIO("\n".join(chunks).encode("utf-8"))
            blob.name = "steam_smm_actions.log"
            bot.send_document(call.message.chat.id, blob,
                              caption=f"📜 Логи плагина Steam SMM ({len(chunks)} строк)")
        except Exception as e:
            try:
                bot.send_message(call.message.chat.id,
                                 f"❌ Не удалось выгрузить лог: {_short_err(e)}")
            except Exception:
                pass

    def logs_clear_cb(call) -> None:
        _clear_actions_log()
        _answer(call, "🧹 Логи очищены")
        logs_cb(call)

    def toggle_confirm_cb(call) -> None:
        s = _load_settings()
        s["confirm_link"] = not s.get("confirm_link", True)
        _save_settings(s)
        _answer(call, "🔁 " + ("вкл" if s["confirm_link"] else "выкл"))
        home_cb(call)

    def toggle_refund_cb(call) -> None:
        s = _load_settings()
        s["auto_refund"] = not s.get("auto_refund", True)
        _save_settings(s)
        _answer(call, "💸 " + ("вкл" if s["auto_refund"] else "выкл"))
        home_cb(call)

    def toggle_profit_cb(call) -> None:
        s = _load_settings()
        s["profit_guard"] = not s.get("profit_guard", True)
        _save_settings(s)
        _answer(call, "🛡 " + ("вкл" if s["profit_guard"] else "выкл"))
        _render_adv_section(call, "prices")

    def toggle_balalert_cb(call) -> None:
        s = _load_settings()
        s["balance_alert_enabled"] = not s.get("balance_alert_enabled", True)
        _save_settings(s)
        _answer(call, "⚠️ " + ("вкл" if s["balance_alert_enabled"] else "выкл"))
        _render_adv_section(call, "balance")

    def toggle_neworder_cb(call) -> None:
        s = _load_settings()
        s["new_order_notifications"] = not s.get("new_order_notifications", False)
        _save_settings(s)
        _answer(call, "🔔 " + ("вкл" if s["new_order_notifications"] else "выкл"))
        _render_adv_section(call, "orders")

    def toggle_autolots_cb(call) -> None:
        s = _load_settings()
        s["auto_lots_enabled"] = not s.get("auto_lots_enabled", False)
        _save_settings(s)
        _answer(call, "🔁 " + ("вкл" if s["auto_lots_enabled"] else "выкл"))
        _render_adv_section(call, "prices")

    def toggle_balgate_cb(call) -> None:
        s = _load_settings()
        s["balance_gate"] = not s.get("balance_gate", True)
        _save_settings(s)
        _answer(call, "⚪ " + ("вкл" if s["balance_gate"] else "выкл"))
        _render_adv_section(call, "balance")

    def toggle_autopause_cb(call) -> None:
        s = _load_settings()
        s["auto_pause_low_balance"] = not s.get("auto_pause_low_balance", True)
        if not s["auto_pause_low_balance"]:
            s["auto_pause_grace_until"] = 0  # автопауза выключена — грейс не нужен
        _save_settings(s)
        _answer(call, "⏸️ " + ("вкл" if s["auto_pause_low_balance"] else "выкл"))
        _render_adv_section(call, "balance")

    def toggle_raise_cb(call) -> None:
        s = _load_settings()
        s["auto_raise_enabled"] = not s.get("auto_raise_enabled", False)
        _save_settings(s)
        _answer(call, "⬆️ " + ("вкл" if s["auto_raise_enabled"] else "выкл"))
        _render_adv_section(call, "extra")

    def toggle_opcmds_cb(call) -> None:
        s = _load_settings()
        s["operator_commands"] = not s.get("operator_commands", False)
        _save_settings(s)
        _answer(call, "🎛 " + ("вкл" if s["operator_commands"] else "выкл"))
        _render_adv_section(call, "orders")

    def toggle_price_cb(call) -> None:
        s = _load_settings()
        s["price_check_enabled"] = not s.get("price_check_enabled", False)
        _save_settings(s)
        _answer(call, "🔎 " + ("вкл" if s["price_check_enabled"] else "выкл"))
        _render_adv_section(call, "orders")

    def blacklist_cb(call) -> None:
        s = _load_settings()
        lines = ["<b>🚫 Чёрный список</b>", ""]
        kb = K(row_width=1)
        bl = s.get("blacklist", [])
        for i, entry in enumerate(bl):
            if isinstance(entry, dict):
                label = entry.get("username") or entry.get("buyer_id") or "?"
                reason = entry.get("reason")
                shown = f"{label} — {reason}" if reason else str(label)
            else:
                shown = str(entry)
            lines.append(f"• <code>{_html_escape(shown)}</code>")
            kb.add(B(f"❌ {_html_escape(str(shown))}", callback_data=f"{CBT_BLACKLIST_DEL}:{i}"))
        if not bl:
            lines.append("(пусто)")
        kb.row(B("➕ Добавить покупателя", callback_data=CBT_BLACKLIST_ADD),
               B("📥 Импорт", callback_data=CBT_BLACKLIST_IMPORT))
        kb.row(B("📤 Экспорт", callback_data=CBT_BLACKLIST_EXPORT))
        kb.add(B("◀️ Назад", callback_data=CBT_ADV_EXTRA))
        _edit_or_send(call, "\n".join(lines), kb)
        _answer(call)

    def blacklist_add_cb(call) -> None:
        try:
            bot.clear_step_handler_by_chat_id(call.message.chat.id)
        except Exception:
            pass
        msg = bot.send_message(call.message.chat.id,
                               "🚫 <b>Добавить в чёрный список</b>\n"
                               "Введите числовой ID покупателя или username:",
                               parse_mode="HTML")
        _answer(call)
        _reg_step(bot, msg, handle_blacklist_add)

    def handle_blacklist_add(m) -> None:
        value = (m.text or "").strip().lstrip("@")
        if not value:
            bot.reply_to(m, "❌ Пустое значение.")
            return
        s = _load_settings()
        added = _add_blacklist(s, buyer_id=value if value.isdigit() else None,
                               username=None if value.isdigit() else value)
        if not added:
            bot.reply_to(m, "⚠️ Уже в списке.")
            return
        _save_settings(s)
        bot.reply_to(m, f"🚫 Добавлен: <code>{_html_escape(value)}</code>", parse_mode="HTML")

    def blacklist_del_cb(call) -> None:
        idx = call.data.split(":", 2)[-1]
        try:
            i = int(idx)
        except Exception:
            return _answer(call, "ошибка")
        s = _load_settings()
        bl = s.get("blacklist", [])
        if 0 <= i < len(bl):
            entry = bl[i]
            if isinstance(entry, dict):
                removed = _remove_blacklist(s, buyer_id=entry.get("buyer_id"),
                                            username=entry.get("username"))
                shown = entry.get("username") or entry.get("buyer_id") or "?"
            else:
                shown = str(entry)
                removed = _remove_blacklist(s, buyer_id=shown if shown.isdigit() else None,
                                            username=None if shown.isdigit() else shown)
            if removed:
                _save_settings(s)
                _answer(call, f"✅ Удалён: {shown}")
            else:
                _answer(call, "не найден")
        else:
            _answer(call, "не найден")
        blacklist_cb(call)

    def blacklist_export_cb(call) -> None:
        s = _load_settings()
        blob = json.dumps(s.get("blacklist", []), ensure_ascii=False, indent=2).encode("utf-8")
        _answer(call)
        try:
            import io
            bio = io.BytesIO(blob)
            bio.name = "steam_smm_blacklist.json"
            bot.send_document(call.message.chat.id, bio, caption="🚫 Чёрный список (JSON)")
        except Exception:
            bot.send_message(call.message.chat.id, f"<pre>{_html_escape(blob.decode('utf-8'))}</pre>", parse_mode="HTML")

    def blacklist_import_cb(call) -> None:
        msg = bot.send_message(call.message.chat.id,
                               "📥 Пришлите JSON-документ со списком имён (заменит текущий):")
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
                return bot.reply_to(m, f"❌ Не удалось разобрать JSON: {e}. Прежний список сохранён.")
            s = _load_settings()
            s["blacklist"] = []
            for item in data:
                if isinstance(item, dict):
                    _add_blacklist(s, buyer_id=item.get("buyer_id"),
                                   username=item.get("username"),
                                   reason=item.get("reason") or "импорт")
                else:
                    value = str(item).strip().lstrip("@")
                    if value:
                        _add_blacklist(s, buyer_id=value if value.isdigit() else None,
                                       username=None if value.isdigit() else value,
                                       reason="импорт legacy")
            _save_settings(s)
            bot.reply_to(m, f"✅ Импортировано записей: {len(s['blacklist'])}")
        _reg_step(bot, msg, handle)

    def _make_float_editor(key: str, lo: float, hi: float, label: str, presets: list | None = None,
                           section: str = ""):
        def cb(call) -> None:
            back = _ADV_SECTION_CBT.get(section, CBT_ADVANCED)
            kb = K(row_width=2)
            if presets:
                for p in presets:
                    kb.add(B(f"{p:g}", callback_data=f"{CBT_SETF}:{key}:{p}:{section}"))
            kb.add(B("◀️ Назад", callback_data=back))
            msg = bot.send_message(call.message.chat.id,
                                   f"{label}\nВыберите готовое значение или введите число от {lo} до {hi}:",
                                   reply_markup=kb)
            _answer(call)

            def handle(m) -> None:
                try:
                    v = float((m.text or "").strip().replace(",", "."))
                    if not lo <= v <= hi:
                        raise ValueError
                except Exception:
                    bot.reply_to(m, f"❌ Вне диапазона ({lo}–{hi}). Прежнее значение сохранено.")
                    return
                s = _load_settings()
                s[key] = round(v, 2)
                _save_settings(s)
                bot.reply_to(m, f"✅ Обновлено: <code>{v:g}</code>", parse_mode="HTML")
            _reg_step(bot, msg, handle)
        return cb

    def _make_int_editor(key: str, lo: int, hi: int, label: str, presets: list | None = None,
                         section: str = ""):
        def cb(call) -> None:
            back = _ADV_SECTION_CBT.get(section, CBT_ADVANCED)
            kb = K(row_width=2)
            if presets:
                for p in presets:
                    kb.add(B(f"{p}", callback_data=f"{CBT_SETI}:{key}:{p}:{section}"))
            kb.add(B("◀️ Назад", callback_data=back))
            msg = bot.send_message(call.message.chat.id,
                                   f"{label}\nВыберите готовое значение или введите число от {lo} до {hi}:",
                                   reply_markup=kb)
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
            _reg_step(bot, msg, handle)
        return cb

    def _split_set_section(val: str) -> tuple[str, str]:
        """Отделяет суффикс раздела от значения пресета (val[:section])."""
        for name in _ADV_SECTION_CBT:
            if val.endswith(f":{name}"):
                return val[: -(len(name) + 1)], name
        return val, ""

    def set_float_cb(call) -> None:
        _, _, rest = call.data.partition(f"{CBT_SETF}:")
        key, _, val = rest.partition(":")
        val, section = _split_set_section(val)
        try:
            v = float(val.replace(",", "."))
        except Exception:
            return _answer(call, "ошибка")
        try:
            bot.clear_step_handler_by_chat_id(call.message.chat.id)
        except Exception:
            pass
        s = _load_settings()
        s[key] = round(v, 2)
        _save_settings(s)
        _answer(call, f"✅ {v:g}")
        _render_adv_section(call, section)

    def set_int_cb(call) -> None:
        _, _, rest = call.data.partition(f"{CBT_SETI}:")
        key, _, val = rest.partition(":")
        val, section = _split_set_section(val)
        try:
            v = int(float(val))
        except Exception:
            return _answer(call, "ошибка")
        try:
            bot.clear_step_handler_by_chat_id(call.message.chat.id)
        except Exception:
            pass
        s = _load_settings()
        s[key] = v
        _save_settings(s)
        _answer(call, f"✅ {v}")
        _render_adv_section(call, section)

    # ---------- API-ключ steamsmm.ru ----------
    # Плагин работает только через steamsmm.ru, поэтому вместо управления
    # поставщиками — простой экран ввода/замены API-ключа.
    def providers_cb(call) -> None:
        _edit_or_send(call, _api_key_screen_text(_load_settings()),
                      _api_key_screen_kb(_load_settings()))
        _answer(call)

    def _refresh_key_screen(bot, chat_id: int) -> None:
        """Перерисовывает экран API-ключа (правка последнего меню или новое сообщение)."""
        s = _load_settings()
        text = _api_key_screen_text(s)
        kb = _api_key_screen_kb(s)
        mid = _menu_msg_ids.get(chat_id)
        if mid is not None:
            try:
                bot.edit_message_text(text, chat_id, mid, parse_mode="HTML",
                                      reply_markup=kb)
                return
            except Exception:
                pass
        try:
            sent = bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)
            _menu_msg_ids[chat_id] = sent.message_id
        except Exception:
            pass

    def _verify_key_and_refresh(bot, chat_id: int, provider: dict) -> None:
        """Фоновая проверка ключа после ввода: обновляет кеш и экран ключа."""
        ok, msg = _check_provider_balance(provider)
        if not ok:
            try:
                bot.send_message(chat_id, f"❌ API-ключ не работает: {msg}\n"
                                          "Проверьте ключ в ЛК steamsmm.ru.")
            except Exception:
                pass
            return
        try:
            with _balance_cache_lock:
                _balance_cache[str(provider.get("id"))] = (float(msg), time.time())
        except Exception:
            pass
        _refresh_key_screen(bot, chat_id)

    def provider_view_cb(call) -> None:
        pid = call.data.split(":", 2)[-1]
        s = _load_settings()
        p = _find_provider(s, pid)
        if not p:
            _answer(call, "не найден")
            return providers_cb(call)
        snap = p.get("balance_snapshot") or {}
        cur = p.get("expected_currency") or "—"
        amount = snap.get("amount")
        state = (p.get("balance_hold") or {}).get("reason") or "ok"
        text = (f"<b>🔑 {_html_escape(p.get('name'))}</b>\n"
                f"Ключ: <code>{_mask_secret(p.get('api_key'))}</code>\n"
                f"Мин. баланс: <code>{float(p.get('min_balance', 0) or 0):.2f}</code> {_html_escape(cur)}\n"
                f"Баланс: <code>{amount if amount is not None else '—'}</code> {_html_escape(snap.get('currency') or cur)}\n"
                f"Гейт: {_onoff(p.get('low_balance_pause_enabled', True))} · состояние: <code>{state}</code>")
        kb = K(row_width=2)
        kb.row(B("🔑 Изменить ключ", callback_data=f"{CBT_PROVIDER_EDIT_KEY}:{pid}"),
               B("💰 Баланс", callback_data=f"{CBT_PROVIDER_BAL}:{pid}"))
        kb.row(B("📡 Услуги", callback_data=f"{CBT_PROVIDER_SERVICES}:{pid}:0"),
               B("🗑 Удалить", callback_data=f"{CBT_PROVIDER_DEL}:{pid}"))
        kb.row(B("📉 Мин. баланс", callback_data=f"{CBT_PROVIDER_EDIT_MINBAL}:{pid}"),
               B("💱 Валюта", callback_data=f"{CBT_PROVIDER_EDIT_CURRENCY}:{pid}"))
        kb.row(B(("⏸️ Гейт: 🟢" if p.get("low_balance_pause_enabled", True) else "⏸️ Гейт: 🔴"),
                 callback_data=f"{CBT_PROVIDER_TOGGLE_LOWBAL}:{pid}"))
        kb.row(B("◀️ Назад", callback_data=CBT_PROVIDERS))
        _edit_or_send(call, text, kb)
        _answer(call)

    def provider_add_cb(call) -> None:
        msg = bot.send_message(
            call.message.chat.id,
            "🔑 <b>API-ключ steamsmm.ru</b>\n\n"
            "Введите <b>API-ключ</b>.\n\n"
            "Где взять: steamsmm.ru → <b>Личный кабинет → API-ключи → «Создать ключ»</b>, "
            "скопируйте строку и пришлите сюда:",
            parse_mode="HTML")
        _answer(call)

        def step_key(m):
            key = (m.text or "").strip()
            if not key:
                return bot.reply_to(m, "❌ Пусто. Отменено.")
            s = _load_settings()
            existing = next((p for p in s["providers"]
                             if _provider_style(p) == "rest"), None)
            if existing:
                existing["api_key"] = key
                _save_settings(s)
                bot.reply_to(m, "✅ API-ключ steamsmm.ru обновлён. Проверяю баланс…")
                provider = existing
            else:
                preset = _steamsmm_preset(key)
                preset["id"] = _new_id("p", s["providers"])
                s["providers"].append(preset)
                _save_settings(s)
                bot.reply_to(m, "✅ API-ключ steamsmm.ru сохранён. Проверяю баланс…")
                _hint_autoreg_catalog(bot, call.message.chat.id, preset)
                provider = preset
            threading.Thread(target=_verify_key_and_refresh,
                             args=(bot, call.message.chat.id, provider),
                             name="steamsmm-key-check", daemon=True).start()
        _reg_step(bot, msg, step_key)

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
                bot.reply_to(m, f"✅ Обновлено: <code>{_html_escape(shown)}</code>", parse_mode="HTML")
            _reg_step(bot, msg, handle)
        return cb


    def provider_min_balance_cb(call) -> None:
        pid = call.data.split(":", 2)[-1]
        msg = bot.send_message(call.message.chat.id, "Введите минимальный баланс поставщика:")
        _answer(call)
        def handle(m):
            try: value = float((m.text or "").replace(",", "."))
            except Exception: return bot.reply_to(m, "❌ Неверное число.")
            s = _load_settings(); p = _find_provider(s, pid)
            if not p: return bot.reply_to(m, "❌ Поставщик не найден.")
            p["min_balance"] = max(0.0, value); _save_settings(s)
            bot.reply_to(m, "✅ Минимальный баланс сохранён.")
        _reg_step(bot, msg, handle)

    def provider_currency_cb(call) -> None:
        pid = call.data.split(":", 2)[-1]
        msg = bot.send_message(call.message.chat.id, "Введите ожидаемую валюту (например RUB, USD, EUR):")
        _answer(call)
        def handle(m):
            value = _currency(m.text)
            if not value or len(value) > 8: return bot.reply_to(m, "❌ Неверная валюта.")
            s = _load_settings(); p = _find_provider(s, pid)
            if not p: return bot.reply_to(m, "❌ Поставщик не найден.")
            p["expected_currency"] = value; _save_settings(s)
            bot.reply_to(m, "✅ Валюта сохранена.")
        _reg_step(bot, msg, handle)

    def provider_low_balance_toggle_cb(call) -> None:
        pid = call.data.split(":", 2)[-1]
        s = _load_settings(); p = _find_provider(s, pid)
        if p:
            p["low_balance_pause_enabled"] = not p.get("low_balance_pause_enabled", True)
            _save_settings(s)
        provider_view_cb(call)

    def provider_del_cb(call) -> None:
        pid = call.data.split(":", 2)[-1]
        s = _load_settings()
        p = _find_provider(s, pid)
        name = _html_escape((p or {}).get("name", "?"))
        kb = K(row_width=2)
        kb.add(B("✅ Да", callback_data=f"{CBT_PROVIDER_DEL_CONFIRM}:{pid}"),
               B("❌ Отмена", callback_data=CBT_PROVIDERS))
        _edit_or_send(call, f"🗑 Удалить поставщика <b>{name}</b> вместе с API-ключом?", kb)
        _answer(call)

    def provider_del_confirm_cb(call) -> None:
        pid = call.data.split(":", 2)[-1]
        s = _load_settings()
        s["providers"] = [p for p in s.get("providers", []) if p.get("id") != pid]
        _save_settings(s)
        _answer(call, "🗑 Удалён")
        providers_cb(call)

    def provider_style_cb(call) -> None:
        pid = call.data.split(":", 2)[-1]
        p = _find_provider(_load_settings(), pid)
        kb = K(row_width=2)
        kb.row(B("🧩 smmpanel", callback_data=f"{CBT_SETS}:{pid}:smmpanel"),
               B("🟦 steamsmm (REST)", callback_data=f"{CBT_SETS}:{pid}:rest"))
        kb.row(B("◀️ Назад", callback_data=f"{CBT_PROVIDER_VIEW}:{pid}"))
        _edit_or_send(call,
                      f"🧩 Тип API для <code>{_html_escape((p or {}).get('name', pid))}</code>:",
                      kb)
        _answer(call)

    def set_style_cb(call) -> None:
        _, _, rest = call.data.partition(f"{CBT_SETS}:")
        pid, _, style = rest.partition(":")
        s = _load_settings()
        p = _find_provider(s, pid)
        if p and style in ("smmpanel", "rest"):
            p["style"] = style
            _save_settings(s)
        _answer(call, "✅ " + style.upper())
        provider_view_cb(call)

    def provider_balance_cb(call) -> None:
        pid = call.data.split(":", 2)[-1]
        s = _load_settings()
        provider = _find_provider(s, pid)
        _answer(call, "⏳")
        if not provider:
            bot.send_message(call.message.chat.id, "❌ Поставщик не найден.")
            return
        ok, msg = _check_provider_balance(provider)
        if not ok:
            bot.send_message(call.message.chat.id,
                             f"❌ Ошибка получения баланса: {msg}")
            return
        with _balance_cache_lock:
            _balance_cache[str(provider.get("id"))] = (float(msg), time.time())
        bot.send_message(call.message.chat.id,
                         f"💰 Баланс {provider.get('name')}: <code>{msg}</code> ₽",
                         parse_mode="HTML")
        _refresh_key_screen(bot, call.message.chat.id)

    def provider_services_cb(call) -> None:
        parts = call.data.split(":", 2)
        pid = parts[1] if len(parts) > 1 else ""
        try:
            offset = int(parts[2]) if len(parts) > 2 and parts[2] else 0
        except Exception:
            offset = 0
        s = _load_settings()
        provider = _find_provider(s, pid)
        if not provider:
            _answer(call, "не найден")
            return providers_cb(call)
        _answer(call, "⏳")
        try:
            services = list(_client_for_provider(provider).services())
        except Exception as e:
            bot.send_message(call.message.chat.id, f"❌ Ошибка получения услуг: {e}")
            return
        per_page = 15
        total = len(services)
        page = services[offset:offset + per_page]
        lines = [f"<b>📡 Услуги «{_html_escape(provider.get('name'))}»</b> ({total})", ""]
        kb = K(row_width=3)
        if not page:
            lines.append("(пусто)")
        is_rest = _provider_style(provider) == "rest"
        for svc in page:
            sid = svc.get("service", svc.get("id", svc.get("service_id", "?")))
            name = svc.get("name", svc.get("title", "—"))
            lines.append(f"· <code>{sid}</code> — {_html_escape(str(name))}")
            if is_rest and str(sid).startswith("autoreg:"):
                cat = str(sid).split(":", 1)[-1]
                if cat.isdigit():
                    kb.row(B(f"🎯 Привязать авторег {cat}", callback_data=f"{CBT_AUTOREG_BIND}:{pid}:{cat}"))
        pages = max(1, (total + per_page - 1) // per_page)
        nav = []
        if offset > 0:
            nav.append(B("◀️", callback_data=f"{CBT_PROVIDER_SERVICES}:{pid}:{max(0, offset - per_page)}"))
        nav.append(B(f"{offset // per_page + 1}/{pages}", callback_data=f"{CBT_PROVIDER_VIEW}:{pid}"))
        if offset + per_page < total:
            nav.append(B("▶️", callback_data=f"{CBT_PROVIDER_SERVICES}:{pid}:{offset + per_page}"))
        if nav:
            kb.row(*nav)
        kb.row(B("◀️ Назад", callback_data=f"{CBT_PROVIDER_VIEW}:{pid}"))
        try:
            bot.edit_message_text("\n".join(lines), call.message.chat.id, call.message.message_id,
                                  parse_mode="HTML", reply_markup=kb)
        except Exception:
            bot.send_message(call.message.chat.id, "\n".join(lines), parse_mode="HTML", reply_markup=kb)

    def autoreg_bind_cb(call) -> None:
        parts = call.data.split(":")
        pid = parts[1] if len(parts) > 1 else ""
        cat = parts[2] if len(parts) > 2 else ""
        provider = _find_provider(_load_settings(), pid)
        if not provider or _provider_style(provider) != "rest" or not cat.isdigit():
            _answer(call, "неверные данные")
            return
        _answer(call)
        msg = bot.send_message(
            call.message.chat.id,
            f"🎯 Привязка авторега <code>{cat}</code> "
            f"({_html_escape(provider.get('name', pid))})\n"
            "Введите <b>название лота</b> — точно как на FunPay (или его часть):",
            parse_mode="HTML")

        def step_match(m):
            match = (m.text or "").strip()
            if not match:
                return bot.reply_to(m, "❌ Пусто. Отменено.")
            msg2 = bot.reply_to(m, "💸 Себестоимость за 1 аккаунт (₽, для прибыли; можно пропустить — Enter):")

            def step_cost(m2):
                cost = None
                raw = (m2.text or "").strip()
                if raw:
                    try:
                        cost = float(raw.replace(",", "."))
                        if cost < 0:
                            raise ValueError
                    except Exception:
                        return bot.reply_to(m2, "❌ Некорректная стоимость. Отменено.")
                s = _load_settings()
                for existing in s.get("lot_mappings", []):
                    if existing.get("mode") == "account" \
                            and existing.get("provider_id") == pid \
                            and str(existing.get("autoreg_category_id") or "") == str(int(cat)):
                        return bot.reply_to(
                            m2,
                            f"⚠️ Уже есть привязка для авторега {cat} — «{existing.get('lot_match')}». Отменено.")
                mapping = _account_purchase_mapping(pid, cat, match, cost)
                mapping["id"] = _new_id("m", s["lot_mappings"])
                s["lot_mappings"].append(mapping)
                _save_settings(s)
                pname = (_find_provider(s, pid) or {}).get("name", pid)
                bot.reply_to(m2,
                             f"✅ Привязка добавлена: «{match}» → покупка авторегов у {pname} (category {cat}).")
            _reg_step(bot, msg2, step_cost)

        _reg_step(bot, msg, step_match)

    def svc_catalog_cb(call) -> None:
        parts = call.data.split(":")
        pid = parts[1] if len(parts) > 1 else ""
        try:
            offset = int(parts[2]) if len(parts) > 2 and parts[2] else 0
        except Exception:
            offset = 0
        s = _load_settings()
        provider = _find_provider(s, pid)
        if not provider or _provider_style(provider) != "rest":
            _answer(call, "только для REST-провайдеров")
            return
        _answer(call, "⏳")
        try:
            # автореги из каталога НЕ выводим кнопками услуг — для них есть «🎯 Привязать»
            services = [s for s in _client_for_provider(provider).services()
                        if not str(s.get("service") or "").startswith("autoreg:")]
        except Exception as e:
            try:
                bot.send_message(call.message.chat.id, f"❌ Ошибка получения каталога: {e}")
            except Exception:
                pass
            return
        per_page = 6
        total = len(services)
        items_text, buttons = _catalog_page(services, offset, per_page)
        lines = [f"<b>🎯 Каталог услуг «{_html_escape(provider.get('name'))}»</b> "
                 f"({total}, стр. {offset // per_page + 1})", "", items_text]
        kb = K(row_width=1)
        for b in buttons:
            kb.add(B(f"• <code>{_html_escape(str(b['sid']))}</code> — {_html_escape(str(b['name']))}",
                     callback_data=f"{CBT_SVC_PICK}:{pid}:{b['sid']}"))
        pages = max(1, (total + per_page - 1) // per_page)
        nav = []
        if offset > 0:
            nav.append(B("◀️", callback_data=f"{CBT_SVC_CATALOG}:{pid}:{max(0, offset - per_page)}"))
        nav.append(B(f"{offset // per_page + 1}/{pages}", callback_data=f"{CBT_SVC_CATALOG}:{pid}:0"))
        if offset + per_page < total:
            nav.append(B("▶️", callback_data=f"{CBT_SVC_CATALOG}:{pid}:{offset + per_page}"))
        if nav:
            kb.row(*nav)
        kb.add(B("✏️ Ввод вручную", callback_data=f"{CBT_SVC_CATALOG_BACK}:{pid}"))
        try:
            bot.edit_message_text("\n".join(lines), call.message.chat.id, call.message.message_id,
                                  parse_mode="HTML", reply_markup=kb)
        except Exception:
            try:
                bot.send_message(call.message.chat.id, "\n".join(lines), parse_mode="HTML", reply_markup=kb)
            except Exception:
                pass

    def svc_catalog_back_cb(call) -> None:
        pid = call.data.split(":", 2)[-1]
        s = _load_settings()
        provider = _find_provider(s, pid)
        pname = (provider or {}).get("name", pid)
        kb = K(row_width=1)
        kb.add(B("🎯 Каталог услуг", callback_data=f"{CBT_SVC_CATALOG}:{pid}:0"))
        _edit_or_send(
            call,
            f"🏷 Поставщик: {pname}\n🔢 Введите код услуги (action_type) или выберите из каталога кнопкой:\n"
            f"например: comment, like, subscribe, commend_cs2…",
            kb)
        _answer(call)

    def svc_pick_cb(call) -> None:
        pid, svc = _parse_svc_pick(call.data)
        _answer(call, f"✅ {svc}")
        try:
            bot.edit_message_text(f"✅ Выбрана услуга <code>{_html_escape(svc)}</code>. Продолжаем настройку…",
                                  call.message.chat.id, call.message.message_id, parse_mode="HTML")
        except Exception:
            pass
        done = _wizard_svc_continue.get(call.message.chat.id)
        if done is None:
            try:
                bot.send_message(call.message.chat.id,
                                 "⚠️ Визард привязки неактивен — начните заново: 🗺 Привязки → ➕ Добавить.")
            except Exception:
                pass
            return
        done(svc)
        _wizard_svc_continue.pop(call.message.chat.id, None)

    def balances_cb(call) -> None:
        s = _load_settings()
        _answer(call, "⏳")
        _invalidate_balance_cache()
        items = _provider_balance(s)
        if not items:
            _edit_or_send(call, "<b>💰 Нет настроенных поставщиков.</b>",
                          K().add(B("◀️ Назад", callback_data=CBT_ADVANCED)))
            return
        lines = ["<b>💰 Балансы поставщиков</b>", ""]
        for it in items:
            p = it.get("provider", {})
            if it.get("error"):
                lines.append(f"• {_html_escape(p.get('name', '?'))}: ❌ {it['error']}")
            else:
                bal = it.get("data", {}).get("balance", "?")
                lines.append(f"• {_html_escape(p.get('name', '?'))}: <code>{bal}</code>")
        kb = K(row_width=2)
        kb.add(B("🔄 Обновить", callback_data=CBT_BALANCES),
               B("◀️ Назад", callback_data=CBT_ADVANCED))
        _edit_or_send(call, "\n".join(lines), kb)

    # ---------- привязки лотов ----------
    def mappings_cb(call) -> None:
        s = _load_settings()
        lines = ["<b>🗺 Лоты ↔ услуги / автореги</b>", ""]
        kb = K(row_width=1)
        for m in s.get("lot_mappings", []):
            mode = m.get("mode", "service")
            if mode == "account":
                target = f"пул <code>{_html_escape(m.get('pool_tag'))}</code>"
            else:
                pname = (_find_provider(s, m.get("provider_id")) or {}).get("name", m.get("provider_id"))
                target = f"{pname}/svc {m.get('service_id')} ×{m.get('qty_multiplier', 1)}"
                if str(m.get("service_id") or "") == "commend_cs2":
                    cp = _commend_params(s, m)
                    target += (f" 🎖️ {cp['commend_friendly']}/"
                               f"{cp['commend_teacher']}/{cp['commend_leader']}")
                    if m.get("commend_random"):
                        target += " 🎲"
            lines.append(f"• <code>{_html_escape(m.get('lot_match'))}</code> → {target}")
            kb.add(B(f"🗑 Удалить {_html_escape(m.get('lot_match'))}", callback_data=f"{CBT_MAPPING_DEL}:{m.get('id')}"))
            if mode != "account":
                lot_tag = m.get("target_lot_id")
                lbl = f"🎯 Лот: {lot_tag}" if lot_tag else "🎯 Задать лот"
                kb.add(B(f"{lbl} — {_html_escape(m.get('lot_match'))[:20]}",
                         callback_data=f"{CBT_MAPPING_LOT}:{m.get('id')}"))
                if lot_tag:
                    kb.add(B(f"✖️ Снять лот {lot_tag}", callback_data=f"{CBT_MAPPING_LOTCLEAR}:{m.get('id')}"))
                if str(m.get("service_id") or "") == "commend_cs2":
                    cp = _commend_params(s, m)
                    kb.add(B(f"🎖️ Похвала {cp['commend_friendly']}/"
                             f"{cp['commend_teacher']}/{cp['commend_leader']} (изменить)",
                             callback_data=f"{CBT_MAPPING_COMMEND}:{m.get('id')}"))
                    rnd = bool(m.get("commend_random"))
                    kb.add(B(f"🎲 Случайный пакет: {'🟢' if rnd else '🔴'}",
                             callback_data=f"{CBT_MAPPING_COMMEND_RANDOM}:{m.get('id')}"))
        if not s.get("lot_mappings"):
            lines.append("(пусто)")
        kb.add(B("➕ Добавить привязку", callback_data=CBT_MAPPING_ADD))
        kb.add(B("🔄 Обновить", callback_data=CBT_MAPPINGS))
        kb.row(B("📤 Экспорт JSON", callback_data=CBT_MAPPING_EXPORT),
               B("📥 Импорт JSON", callback_data=CBT_MAPPING_IMPORT))
        kb.add(B("◀️ Назад", callback_data=CBT_ADVANCED))
        _edit_or_send(call, "\n".join(lines), kb)
        _answer(call)

    def mapping_add_cb(call) -> None:
        # сбрасываем продолжение от прошлого визарда, чтобы старые кнопки каталога не сработали
        _wizard_svc_continue.pop(call.message.chat.id, None)
        msg = bot.send_message(
            call.message.chat.id,
            "➕ Введите <b>название лота</b> — точно как на FunPay (или его часть),\n"
            "либо числовой ID лота:",
            parse_mode="HTML")
        _answer(call)

        def step_match(m):
            match = (m.text or "").strip()
            if not match:
                return bot.reply_to(m, "❌ Пусто. Отменено.")
            msg2 = bot.reply_to(m, "📦 Тип привязки:\n<code>service</code> — услуга панели\n<code>account</code> — автовыдача авторега\n\nВведите тип:", parse_mode="HTML")

            def step_mode(m2):
                mode = (m2.text or "").strip().lower()
                if mode not in ("service", "account"):
                    return bot.reply_to(m2, "❌ Тип должен быть service или account. Отменено.")
                if mode == "account":
                    msg_a = bot.reply_to(
                        m2,
                        "📦 Источник авторегов:\n<code>пул</code> — из вашего инвентаря (👥 Автореги)\n"
                        "<code>покупка</code> — купить у REST-провайдера (steamsmm autoreg/create)\n\n"
                        "Введите источник:",
                        parse_mode="HTML")

                    def step_src(m3):
                        src = (m3.text or "").strip().lower()
                        if src in ("покупка", "buy", "buying", "provider", "2"):
                            s0 = _load_settings()
                            rest_providers = [p for p in s0.get("providers", []) if _provider_style(p) == "rest"]
                            if not rest_providers:
                                return bot.reply_to(
                                    m3,
                                    "❌ Нет REST-провайдеров (тип API steamsmm). "
                                    "Добавьте поставщика и смените ему тип на steamsmm. Отменено.")
                            hint = "\n".join(
                                f"• <code>{p.get('id')}</code> — {_html_escape(p.get('name'))}" for p in rest_providers)
                            msg_b = bot.reply_to(m3, f"🏷 Введите id REST-провайдера:\n{hint}", parse_mode="HTML")

                            def step_prov2(m4):
                                pid2 = (m4.text or "").strip()
                                if not _find_provider(_load_settings(), pid2):
                                    return bot.reply_to(m4, "❌ Такого поставщика нет. Отменено.")
                                msg_c = bot.reply_to(
                                    m4,
                                    "🔢 Введите category_id авторегов (см. «📡 Услуги» поставщика — пункты «Авторег:...»):")

                                def step_cat(m5):
                                    raw_cat = (m5.text or "").strip()
                                    try:
                                        cat = int(raw_cat)
                                    except Exception:
                                        return bot.reply_to(m5, "❌ Не число. Отменено.")
                                    s2 = _load_settings()
                                    mapping = _account_purchase_mapping(pid2, cat, match)
                                    mapping["id"] = _new_id("m", s2["lot_mappings"])
                                    s2["lot_mappings"].append(mapping)
                                    _save_settings(s2)
                                    pn = (_find_provider(s2, pid2) or {}).get("name", pid2)
                                    bot.reply_to(
                                        m5,
                                        f"✅ Привязка добавлена: «{match}» → покупка авторегов у {pn} (category {cat}).")

                                _reg_step(bot, msg_c, step_cat)

                            _reg_step(bot, msg_b, step_prov2)
                            return
                        tag = (m3.text or "").strip()[:64]
                        if not tag:
                            return bot.reply_to(m3, "❌ Пусто. Отменено.")
                        s = _load_settings()
                        s["lot_mappings"].append({
                            "id": _new_id("m", s["lot_mappings"]),
                            "lot_match": match, "mode": "account", "pool_tag": tag,
                        })
                        _save_settings(s)
                        bot.reply_to(m3, f"✅ Привязка добавлена: «{match}» → пул <code>{tag}</code>",
                                     parse_mode="HTML")
                    _reg_step(bot, msg_a, step_src)
                    return
                s = _load_settings()
                providers = s.get("providers", [])
                if not providers:
                    return bot.reply_to(m2, "❌ Сначала добавьте хотя бы одного поставщика.")
                prov_hint = "\n".join(f"• <code>{p.get('id')}</code> — {_html_escape(p.get('name'))}" for p in providers)
                msg3 = bot.reply_to(m2, f"🏷 Введите id поставщика:\n{prov_hint}", parse_mode="HTML")

                def step_prov(m3):
                    pid = (m3.text or "").strip()
                    if not _find_provider(_load_settings(), pid):
                        return bot.reply_to(m3, "❌ Такого поставщика нет. Отменено.")
                    pname = _find_provider(_load_settings(), pid).get("name", pid)
                    p_style = _provider_style(_find_provider(_load_settings(), pid) or {})
                    if p_style == "rest":
                        cat_kb = K(row_width=1)
                        cat_kb.add(B("🎯 Каталог услуг", callback_data=f"{CBT_SVC_CATALOG}:{pid}:0"))
                        msg4 = bot.reply_to(
                            m3,
                            f"🏷 Поставщик: {pname}\n🔢 Введите код услуги (action_type) или выберите из каталога кнопкой:\n"
                            f"например: comment, like, subscribe, commend_cs2…",
                            parse_mode="HTML", reply_markup=cat_kb)
                    else:
                        msg4 = bot.reply_to(m3, f"🏷 Поставщик: {pname}\n🔢 Введите service_id (число):")

                    def continue_svc(svc, commend_counts):
                        msg5 = bot.reply_to(msg4, "✖️ Введите множитель количества (например 1 или 1000):")

                        def step_mult(m5):
                            try:
                                mult = float((m5.text or "").strip().replace(",", "."))
                                if mult <= 0:
                                    raise ValueError
                            except Exception:
                                return bot.reply_to(m5, "❌ Некорректный множитель. Отменено.")
                            msg6 = bot.reply_to(m5, "💸 Себестоимость за 1 ед. у поставщика (₽, для прибыль-гейта; можно пропустить — Enter):")

                            def step_cost(m6):
                                cost = None
                                raw = (m6.text or "").strip()
                                if raw:
                                    try:
                                        cost = float(raw.replace(",", "."))
                                        if cost < 0:
                                            raise ValueError
                                    except Exception:
                                        return bot.reply_to(m6, "❌ Некорректная стоимость. Отменено.")
                                providers2 = _load_settings().get("providers", [])
                                others = [p for p in providers2 if p.get("id") != pid]
                                if not others:
                                    s2 = _load_settings()
                                    s2["lot_mappings"].append({
                                        "id": _new_id("m", s2["lot_mappings"]),
                                        "lot_match": match, "provider_id": pid,
                                        "service_id": svc, "qty_multiplier": mult,
                                        "cost_per_unit": cost, "mode": "service",
                                        **commend_counts,
                                    })
                                    _save_settings(s2)
                                    pname2 = (_find_provider(s2, pid) or {}).get("name", pid)
                                    bot.reply_to(m6, f"✅ Привязка добавлена: «{match}» → {pname2}/svc {svc} ×{mult}",
                                                 parse_mode="HTML")
                                    return
                                fb_hint = "\n".join(f"• <code>{p.get('id')}</code> — {_html_escape(p.get('name'))}" for p in others)
                                msg7 = bot.reply_to(
                                    m6,
                                    "🔄 Резервный поставщик на случай недоступности основного.\n"
                                    f"(можно пропустить — Enter)\n{fb_hint}",
                                    parse_mode="HTML")

                                def step_fb(m7):
                                    fbid = (m7.text or "").strip()
                                    s3 = _load_settings()
                                    if fbid and not _find_provider(s3, fbid):
                                        return bot.reply_to(m7, "❌ Такого поставщика нет. Отменено.")
                                    s3["lot_mappings"].append({
                                        "id": _new_id("m", s3["lot_mappings"]),
                                        "lot_match": match, "provider_id": pid,
                                        "service_id": svc, "qty_multiplier": mult,
                                        "cost_per_unit": cost, "mode": "service",
                                        **commend_counts,
                                        **({"fallback_provider_id": fbid} if fbid else {}),
                                    })
                                    _save_settings(s3)
                                    pname2 = (_find_provider(s3, pid) or {}).get("name", pid)
                                    line = f"✅ Привязка добавлена: «{match}» → {pname2}/svc {svc} ×{mult}"
                                    if fbid:
                                        line += f" (резерв: {fbid})"
                                    bot.reply_to(m7, line)

                                _reg_step(bot, msg7, step_fb)

                            _reg_step(bot, msg6, step_cost)

                        _reg_step(bot, msg5, step_mult)

                    def done_svc(svc):
                        if p_style == "rest" and str(svc) == "commend_cs2":
                            msg_c = bot.reply_to(
                                msg4,
                                "🏆 Похвала CS2. Введите пропорцию пакета через пробел: friendly teacher leader\n"
                                "(например: 15 5 10; максимум из значений — от 15 до 5000;\n"
                                "количество на кассе = похвалы одному профилю, пропорция масштабируется):")

                            def step_commend(m_c):
                                parts = (m_c.text or "").split()
                                if len(parts) != 3:
                                    return bot.reply_to(m_c, "❌ Нужно 3 числа. Отменено.")
                                try:
                                    f, t, l = (int(x) for x in parts)
                                except Exception:
                                    return bot.reply_to(m_c, "❌ Не числа. Отменено.")
                                if max(f, t, l) <= 0:
                                    return bot.reply_to(m_c, "❌ Хотя бы одно значение должно быть > 0. Отменено.")
                                if not (15 <= max(f, t, l) <= 5000):
                                    return bot.reply_to(m_c, "❌ Максимум из значений должен быть от 15 до 5000. Отменено.")
                                continue_svc(svc, {
                                    "commend_friendly": f,
                                    "commend_teacher": t,
                                    "commend_leader": l,
                                })

                            _reg_step(bot, msg_c, step_commend)
                            return
                        continue_svc(svc, {})

                    # продолжение визарда после выбора service_id кнопкой «🎯 Каталог услуг»
                    _wizard_svc_continue[msg4.chat.id] = done_svc

                    def step_svc(m4):
                        raw = (m4.text or "").strip()
                        if not raw:
                            return bot.reply_to(m4, "❌ Пусто. Отменено.")
                        if p_style == "rest":
                            svc = raw
                        else:
                            try:
                                svc = int(raw)
                            except Exception:
                                return bot.reply_to(m4, "❌ service_id должен быть числом. Отменено.")
                        done_svc(svc)
                        _wizard_svc_continue.pop(m4.chat.id, None)

                    _reg_step(bot, msg4, step_svc)

                _reg_step(bot, msg3, step_prov)

            _reg_step(bot, msg2, step_mode)

        _reg_step(bot, msg, step_match)

    def mapping_del_cb(call) -> None:
        mid = call.data.split(":", 2)[-1]
        s = _load_settings()
        m = next((x for x in s.get("lot_mappings", []) if x.get("id") == mid), None)
        label = _html_escape((m or {}).get("lot_match", "?"))
        kb = K(row_width=2)
        kb.add(B("✅ Да", callback_data=f"{CBT_MAPPING_DEL_CONFIRM}:{mid}"),
               B("❌ Отмена", callback_data=CBT_MAPPINGS))
        _edit_or_send(call, f"🗑 Удалить привязку «<code>{label}</code>»?", kb)
        _answer(call)

    def mapping_del_confirm_cb(call) -> None:
        mid = call.data.split(":", 2)[-1]
        s = _load_settings()
        s["lot_mappings"] = [m for m in s.get("lot_mappings", []) if m.get("id") != mid]
        _save_settings(s)
        _answer(call, "🗑 Удалено")
        mappings_cb(call)

    def mapping_lot_cb(call) -> None:
        mid = call.data.split(":", 2)[-1]
        msg = bot.send_message(call.message.chat.id,
                               "🎯 Введите ID вашего лота FunPay (число), цену которого "
                               "авто-обновлять по наценке (или 0 — снять):")
        _answer(call)

        def handle(m):
            raw = (m.text or "").strip()
            s = _load_settings()
            target = None
            if raw and raw != "0":
                try:
                    target = int(raw)
                except ValueError:
                    return bot.reply_to(m, "❌ ID должен быть числом (или 0 чтобы снять).")
            for mapping in s.get("lot_mappings", []):
                if mapping.get("id") == mid:
                    if target is None:
                        mapping.pop("target_lot_id", None)
                    else:
                        mapping["target_lot_id"] = target
                    _save_settings(s)
                    bot.reply_to(m, f"✅ Целевой лот задан: <code>{target}</code>" if target
                                 else "✅ Целевой лот снят.")
                    return
            bot.reply_to(m, "❌ Привязка не найдена.")
        _reg_step(bot, msg, handle)

    def mapping_lotclear_cb(call) -> None:
        mid = call.data.split(":", 2)[-1]
        s = _load_settings()
        for mapping in s.get("lot_mappings", []):
            if mapping.get("id") == mid:
                mapping.pop("target_lot_id", None)
                _save_settings(s)
                break
        _answer(call, "✖️ Снято")
        mappings_cb(call)

    def mapping_commend_cb(call) -> None:
        """Редактирование пакета похвалы CS2 (friendly/teacher/leader) у привязки."""
        mid = call.data.split(":", 2)[-1]
        s = _load_settings()
        m = next((x for x in s.get("lot_mappings", []) if x.get("id") == mid), None)
        if m is None or str(m.get("service_id") or "") != "commend_cs2":
            _answer(call, "привязка не найдена")
            return
        cp = _commend_params(s, m)
        msg = bot.send_message(
            call.message.chat.id,
            f"🎖️ Пакет похвалы CS2 для «{_html_escape(m.get('lot_match'))}».\n"
            f"Текущий: friendly <b>{cp['commend_friendly']}</b>, teacher "
            f"<b>{cp['commend_teacher']}</b>, leader <b>{cp['commend_leader']}</b>.\n\n"
            "Введите пропорцию пакета через пробел: friendly teacher leader\n"
            "(например: 15 5 10; максимум из значений — от 15 до 5000;\n"
            "количество на кассе = похвалы одному профилю, пропорция масштабируется):",
            parse_mode="HTML")
        _answer(call)

        def handle(m_in):
            parts = (m_in.text or "").split()
            if len(parts) != 3:
                return bot.reply_to(m_in, "❌ Нужно 3 числа. Отменено.")
            try:
                f, t, l = (int(x) for x in parts)
            except Exception:
                return bot.reply_to(m_in, "❌ Не числа. Отменено.")
            if max(f, t, l) <= 0:
                return bot.reply_to(m_in, "❌ Хотя бы одно значение должно быть > 0. Отменено.")
            if not (15 <= max(f, t, l) <= 5000):
                return bot.reply_to(m_in, "❌ Максимум из значений должен быть от 15 до 5000. Отменено.")
            s2 = _load_settings()
            for mapping in s2.get("lot_mappings", []):
                if mapping.get("id") == mid:
                    mapping["commend_friendly"] = f
                    mapping["commend_teacher"] = t
                    mapping["commend_leader"] = l
                    _save_settings(s2)
                    bot.reply_to(m_in, f"✅ Пакет похвалы: friendly {f}, teacher {t}, leader {l}.")
                    mappings_cb(call)
                    return
            bot.reply_to(m_in, "❌ Привязка не найдена.")
        _reg_step(bot, msg, handle)

    def mapping_commend_random_cb(call) -> None:
        """Тумблер «🎲 Случайный пакет» у привязки похвалы CS2."""
        mid = call.data.split(":", 2)[-1]
        s = _load_settings()
        for mapping in s.get("lot_mappings", []):
            if mapping.get("id") == mid:
                cur = bool(mapping.get("commend_random"))
                mapping["commend_random"] = not cur
                _save_settings(s)
                _answer(call, "🎲 Случайный пакет включён" if not cur
                        else "🎲 Случайный пакет выключен")
                mappings_cb(call)
                return
        _answer(call, "привязка не найдена")

    def mapping_export_cb(call) -> None:
        s = _load_settings()
        blob = json.dumps(s.get("lot_mappings", []), ensure_ascii=False, indent=2).encode("utf-8")
        _answer(call)
        try:
            import io
            bio = io.BytesIO(blob)
            bio.name = "steam_smm_mappings.json"
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
        _reg_step(bot, msg, handle)

    # ---------- автореги ----------
    def accounts_cb(call) -> None:
        s = _load_settings()
        accounts = _load_accounts()
        pools: dict[str, list] = {}
        for a in accounts:
            pools.setdefault((a.get("pool") or "").strip().lower(), []).append(a)
        lines = ["<b>👥 Автореги (инвентарь)</b>", ""]
        kb = K(row_width=1)
        if not pools:
            lines.append("(пусто)")
        for tag, items in pools.items():
            free = sum(1 for a in items if not a.get("sold"))
            lines.append(f"• пул <code>{_html_escape(tag)}</code>: <code>{free}/{len(items)}</code> свободно")
            kb.add(B(f"📂 {_html_escape(tag)} — {free}/{len(items)}", callback_data=f"{CBT_ACCOUNT_POOL}:{tag}:0"))
        kb.row(B("➕ Добавить вручную", callback_data=CBT_ACCOUNT_ADD),
               B("📥 Импорт JSON", callback_data=CBT_ACCOUNT_IMPORT))
        kb.row(B("📤 Экспорт JSON", callback_data=CBT_ACCOUNT_EXPORT))
        kb.add(B("🔄 Обновить", callback_data=CBT_ACCOUNTS))
        kb.add(B("◀️ Назад", callback_data=CBT_ADVANCED))
        _edit_or_send(call, "\n".join(lines), kb)
        _answer(call)

    def account_pool_cb(call) -> None:
        _, _, rest = call.data.partition(f"{CBT_ACCOUNT_POOL}:")
        tag, _, off = rest.rpartition(":")
        try:
            offset = int(off)
        except Exception:
            offset = 0
        tag_l = tag.strip().lower()
        accounts = [a for a in _load_accounts() if (a.get("pool") or "").strip().lower() == tag_l]
        per_page = 8
        total = len(accounts)
        page = accounts[offset:offset + per_page]
        lines = [f"<b>📂 Пул «{_html_escape(tag)}»</b> ({total})", ""]
        if not page:
            lines.append("(пусто)")
        for a in page:
            st = "✅ продан" if a.get("sold") else "🟢 свободен"
            login = _html_escape(a.get("login", "") or "")
            lines.append(f"• <code>{login}</code> — {st}")
        kb = K(row_width=3)
        pages = max(1, (total + per_page - 1) // per_page)
        nav = []
        if offset > 0:
            nav.append(B("◀️", callback_data=f"{CBT_ACCOUNT_POOL}:{tag}:{max(0, offset - per_page)}"))
        nav.append(B(f"{offset // per_page + 1}/{pages}", callback_data=CBT_ACCOUNTS))
        if offset + per_page < total:
            nav.append(B("▶️", callback_data=f"{CBT_ACCOUNT_POOL}:{tag}:{offset + per_page}"))
        if nav:
            kb.row(*nav)
        kb.add(B("◀️ Назад", callback_data=CBT_ACCOUNTS))
        _edit_or_send(call, "\n".join(lines), kb)
        _answer(call)

    def account_add_cb(call) -> None:
        msg = bot.send_message(call.message.chat.id,
                               "➕ Введите авторег в формате:\n<code>тег_пула | логин | пароль</code>\n"
                               "можно добавить несколько строк (по одной в строке).",
                               parse_mode="HTML")
        _answer(call)

        def handle(m):
            text = (m.text or "").strip()
            if not text:
                return bot.reply_to(m, "❌ Пусто. Отменено.")
            accounts = _load_accounts()
            added = 0
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = [x.strip() for x in line.split("|")]
                if len(parts) < 2:
                    bot.reply_to(m, f"⚠️ Строка пропущена (нужен формат тег|логин|пароль): {line}")
                    continue
                tag = parts[0]
                login = parts[1]
                password = parts[2] if len(parts) > 2 else ""
                accounts.append({
                    "id": _new_id("a", accounts),
                    "pool": tag, "login": login, "password": password,
                    "note": "", "sold": False,
                })
                added += 1
            _save_accounts(accounts)
            bot.reply_to(m, f"✅ Добавлено авторегов: {added}")
        _reg_step(bot, msg, handle)

    def account_import_cb(call) -> None:
        msg = bot.send_message(call.message.chat.id,
                               "📥 Пришлите JSON-документ со списком авторегов (дополнит текущие):")
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
                return bot.reply_to(m, f"❌ Не удалось разобрать JSON: {e}. Прежний список сохранён.")
            accounts = _load_accounts()
            for a in data:
                if not isinstance(a, dict) or not (a.get("login") or "").strip():
                    continue
                accounts.append({
                    "id": _new_id("a", accounts),
                    "pool": (a.get("pool") or "").strip(),
                    "login": (a.get("login") or "").strip(),
                    "password": str(a.get("password") or ""),
                    "note": str(a.get("note") or ""),
                    "sold": bool(a.get("sold")),
                })
            _save_accounts(accounts)
            bot.reply_to(m, f"✅ Импортировано авторегов: {len(data)}")
        _reg_step(bot, msg, handle)

    def account_export_cb(call) -> None:
        accounts = _load_accounts()
        blob = json.dumps(accounts, ensure_ascii=False, indent=2).encode("utf-8")
        _answer(call)
        try:
            import io
            bio = io.BytesIO(blob)
            bio.name = "steam_smm_accounts.json"
            bot.send_document(call.message.chat.id, bio, caption="👥 Автореги (JSON)")
        except Exception:
            bot.send_message(call.message.chat.id, f"<pre>{_html_escape(blob.decode('utf-8'))}</pre>", parse_mode="HTML")

    # ---------- шаблоны сообщений ----------
    def msgs_cb(call) -> None:
        kb = K(row_width=1)
        for slot, label in _SLOT_LABELS.items():
            kb.add(B(f"📜 {label}", callback_data=f"{CBT_MSG_SLOT}:{slot}"))
        kb.add(B("◀️ Назад", callback_data=CBT_ADV_EXTRA))
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
            if len(variants) > 1:
                kb.add(B(f"🗑 Удалить #{i + 1}", callback_data=f"{CBT_MSG_DEL}:{slot}:{i}"))
        kb.add(B("👁 Превью", callback_data=f"{CBT_MSG_PREVIEW}:{slot}"))
        kb.add(B("➕ Добавить вариант", callback_data=f"{CBT_MSG_ADD}:{slot}"))
        kb.add(B("◀️ Назад", callback_data=CBT_MSGS))
        _edit_or_send(call, "\n".join(lines), kb)
        _answer(call)

    def msg_preview_cb(call) -> None:
        slot = call.data.split(":", 2)[-1]
        s = _load_settings()
        variants = s["messages"].get(slot, [])
        sample = _pick_variant(variants) or "(нет вариантов)"
        try:
            rendered = sample.format(provider_order_id="000001", order_id="TEST-123")
        except Exception:
            rendered = sample
        try:
            bot.send_message(call.message.chat.id,
                             f"👁 Превью «{_SLOT_LABELS.get(slot, slot)}»:\n\n{rendered}")
        except Exception:
            pass
        _answer(call, "👁")

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
        _reg_step(bot, msg, handle)

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
        preview = _html_escape(variants[i][:40]) if 0 <= i < len(variants) else ""
        kb = K(row_width=2)
        kb.add(B("✅ Да", callback_data=f"{CBT_MSG_DEL_CONFIRM}:{slot}:{i}"),
               B("❌ Отмена", callback_data=f"{CBT_MSG_SLOT}:{slot}"))
        _edit_or_send(call, f"🗑 Удалить вариант #{i + 1}? «{preview}»", kb)
        _answer(call)

    def msg_del_confirm_cb(call) -> None:
        _, _, rest = call.data.partition(f"{CBT_MSG_DEL_CONFIRM}:")
        slot, _, idx = rest.partition(":")
        s = _load_settings()
        variants = s["messages"].get(slot, [])
        try:
            i = int(idx)
        except Exception:
            i = -1
        if 0 <= i < len(variants):
            variants.pop(i)
            _save_settings(s)
        _answer(call, "🗑 Удалено")
        call.data = f"{CBT_MSG_SLOT}:{slot}"
        msg_slot_cb(call)

    # ---------- домены ----------
    def domains_cb(call) -> None:
        s = _load_settings()
        text = "🌐 Разрешённые домены:\n" + "\n".join(f"• {d}" for d in s.get("allowed_link_domains", []))
        text += "\n\nПосле открытия редактора отправьте «+домен» или «-домен»."
        kb = K(row_width=2)
        kb.add(B("✏️ Изменить", callback_data=CBT_DOMAINS_EDIT),
               B("↩️ Сбросить", callback_data=CBT_DOMAINS_RESET))
        kb.add(B("◀️ Назад", callback_data=CBT_ADV_EXTRA))
        _edit_or_send(call, text, kb)
        _answer(call)

    def domains_edit_cb(call) -> None:
        msg = bot.send_message(call.message.chat.id, "Введите «+домен» / «-домен» (или /cancel):")
        _answer(call)

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
        _reg_step(bot, msg, handle)

    def domains_reset_cb(call) -> None:
        s = _load_settings()
        s["allowed_link_domains"] = list(DEFAULT_LINK_DOMAINS)
        _save_settings(s)
        _answer(call, "↩️ Сброшено")
        domains_cb(call)

    # --- регистрация ---
    tg.cbq_handler(open_settings_cb, lambda c: f"{CBT.PLUGIN_SETTINGS}:{UUID}" in (c.data or ""))
    tg.cbq_handler(delete_all_lots_cb, lambda c: c.data == CBT_DELETE_ALL_LOTS)
    tg.cbq_handler(delete_all_lots_cancel_cb, lambda c: (c.data or "").startswith(CBT_DELETE_ALL_LOTS_CANCEL + ":"))
    tg.cbq_handler(delete_all_lots_confirm_cb, lambda c: (c.data or "").startswith(CBT_DELETE_ALL_LOTS_CONFIRM + ":"))
    tg.cbq_handler(home_cb, lambda c: c.data == CBT_HOME)
    tg.cbq_handler(toggle_sales_cb, lambda c: c.data == CBT_TOGGLE_SALES)
    tg.cbq_handler(toggle_maintenance_cb, lambda c: c.data == CBT_TOGGLE_MAINTENANCE)
    tg.cbq_handler(services_cb, lambda c: c.data == CBT_SERVICES)
    tg.cbq_handler(import_cb, lambda c: c.data == CBT_IMPORT)
    tg.cbq_handler(import_src_cb,
                   lambda c: (c.data or "").startswith(f"{CBT_IMPORT_SRC}:"))
    tg.cbq_handler(import_resolve_cb, lambda c: c.data == CBT_IMPORT_RESOLVE)
    tg.cbq_handler(export_cb, lambda c: c.data == CBT_EXPORT)
    tg.cbq_handler(service_detail_cb, lambda c: (c.data or "").startswith(f"{CBT_SERVICE_DETAIL}:"))
    tg.cbq_handler(service_create_cb,
                   lambda c: (c.data or "").startswith(f"{CBT_SERVICE_CREATE}:"))
    tg.cbq_handler(service_create_confirm_cb,
                   lambda c: (c.data or "").startswith(f"{CBT_SERVICE_CREATE_CONFIRM}:"))
    tg.cbq_handler(service_del_cb, lambda c: (c.data or "").startswith(f"{CBT_SERVICE_DEL}:"))
    tg.cbq_handler(service_lotid_cb,
                   lambda c: (c.data or "").startswith(f"{CBT_SERVICE_LOTID}:"))
    tg.cbq_handler(service_lotid_pick_cb,
                   lambda c: (c.data or "").startswith(f"{CBT_SERVICE_LOTID_PICK}:"))
    tg.cbq_handler(service_toggle_sales_cb,
                   lambda c: (c.data or "").startswith(f"{CBT_SERVICE_TOGGLE_SALES}:"))
    tg.cbq_handler(prices_cb, lambda c: c.data == CBT_PRICES)
    tg.cbq_handler(prices_recalc_cb, lambda c: c.data == CBT_PRICES_RECALC)
    tg.cbq_handler(prices_check_cb, lambda c: c.data == CBT_PRICES_CHECK)
    tg.cbq_handler(prices_svc_cb, lambda c: c.data == CBT_PRICES_SVC)
    tg.cbq_handler(prices_svc_detail_cb,
                   lambda c: (c.data or "").startswith(f"{CBT_PRICES_SVC_DETAIL}:"))
    tg.cbq_handler(prices_margin_cb, lambda c: c.data == CBT_PRICES_MARGIN)
    tg.cbq_handler(toggle_round_cb, lambda c: c.data == CBT_TOGGLE_ROUND)
    tg.cbq_handler(toggle_autoprices_cb, lambda c: c.data == CBT_TOGGLE_AUTOPRICES)
    tg.cbq_handler(_make_int_editor("prices_recalc_interval_min", 5, 43200,
                                    "🔄 Пересчёт цен каждые N минут.",
                                    presets=[30, 60, 120, 360]),
                   lambda c: c.data == CBT_EDIT_RECALC_INT)
    tg.cbq_handler(advanced_cb, lambda c: c.data == CBT_ADVANCED)
    tg.cbq_handler(adv_prices_cb, lambda c: c.data == CBT_ADV_PRICES)
    tg.cbq_handler(adv_balance_cb, lambda c: c.data == CBT_ADV_BALANCE)
    tg.cbq_handler(adv_orders_cb, lambda c: c.data == CBT_ADV_ORDERS)
    tg.cbq_handler(adv_extra_cb, lambda c: c.data == CBT_ADV_EXTRA)
    tg.cbq_handler(help_cb, lambda c: c.data == CBT_HELP)
    tg.cbq_handler(stats_cb, lambda c: c.data == CBT_STATS)
    tg.cbq_handler(active_cb, lambda c: c.data == CBT_ACTIVE)
    tg.cbq_handler(history_cb, lambda c: (c.data or "").startswith(f"{CBT_HISTORY}"))
    tg.cbq_handler(order_detail_cb, lambda c: (c.data or "").startswith(f"{CBT_ORDER_DETAIL}:"))
    tg.cbq_handler(backup_export_cb, lambda c: c.data == CBT_BACKUP_EXPORT)
    tg.cbq_handler(backup_import_cb, lambda c: c.data == CBT_BACKUP_IMPORT)
    tg.cbq_handler(active_clear_cb, lambda c: (c.data or "").startswith(f"{CBT_ACTIVE_CLEAR}:"))
    tg.cbq_handler(links_cb, lambda c: c.data == CBT_LINKS)
    tg.cbq_handler(link_add_cb, lambda c: c.data == CBT_LINK_ADD)
    tg.cbq_handler(link_del_cb, lambda c: (c.data or "").startswith(f"{CBT_LINK_DEL}:"))
    tg.cbq_handler(logs_cb, lambda c: c.data == CBT_LOGS)
    tg.cbq_handler(logs_download_cb, lambda c: c.data == CBT_LOGS_DOWNLOAD)
    tg.cbq_handler(logs_clear_cb, lambda c: c.data == CBT_LOGS_CLEAR)
    tg.cbq_handler(provider_min_balance_cb, lambda c: (c.data or "").startswith(f"{CBT_PROVIDER_EDIT_MINBAL}:"))
    tg.cbq_handler(provider_currency_cb, lambda c: (c.data or "").startswith(f"{CBT_PROVIDER_EDIT_CURRENCY}:"))
    tg.cbq_handler(provider_low_balance_toggle_cb, lambda c: (c.data or "").startswith(f"{CBT_PROVIDER_TOGGLE_LOWBAL}:"))
    tg.cbq_handler(toggle_confirm_cb, lambda c: c.data == CBT_TOGGLE_CONFIRM)
    tg.cbq_handler(toggle_refund_cb, lambda c: c.data == CBT_TOGGLE_REFUND)
    tg.cbq_handler(toggle_profit_cb, lambda c: c.data == CBT_TOGGLE_PROFIT)
    tg.cbq_handler(toggle_balalert_cb, lambda c: c.data == CBT_TOGGLE_BALALERT)
    tg.cbq_handler(toggle_neworder_cb, lambda c: c.data == CBT_TOGGLE_NEWORDER)
    tg.cbq_handler(toggle_autolots_cb, lambda c: c.data == CBT_TOGGLE_AUTOLOTS)
    tg.cbq_handler(toggle_balgate_cb, lambda c: c.data == CBT_TOGGLE_BALGATE)
    tg.cbq_handler(toggle_autopause_cb, lambda c: c.data == CBT_TOGGLE_AUTOPAUSE)
    tg.cbq_handler(_make_float_editor("auto_pause_grace_hours", 0, 720,
                                      "⏳ Период «торгуем остатком» (часы) — после ручного "
                                      "запуска при низком балансе автопауза не сработает этот срок. "
                                      "0 — отключить (пауза сработает сразу).",
                                      presets=[0, 6, 12, 24, 48, 72], section="balance"),
                   lambda c: c.data == CBT_EDIT_GRACE)
    tg.cbq_handler(_make_int_editor("balance_check_interval_min", 1, 1440,
                                    "⏱ Интервал проверки баланса (мин) — как часто опрашивать "
                                    "баланс steamsmm.ru для алерта и автопаузы.",
                                    presets=[5, 10, 15, 30, 60], section="balance"),
                   lambda c: c.data == CBT_EDIT_BALINT)
    tg.cbq_handler(toggle_raise_cb, lambda c: c.data == CBT_TOGGLE_RAISE)
    tg.cbq_handler(toggle_opcmds_cb, lambda c: c.data == CBT_TOGGLE_OPCMDS)
    tg.cbq_handler(blacklist_cb, lambda c: c.data == CBT_BLACKLIST)
    tg.cbq_handler(blacklist_add_cb, lambda c: c.data == CBT_BLACKLIST_ADD)
    tg.cbq_handler(blacklist_del_cb, lambda c: (c.data or "").startswith(f"{CBT_BLACKLIST_DEL}:"))
    tg.cbq_handler(blacklist_export_cb, lambda c: c.data == CBT_BLACKLIST_EXPORT)
    tg.cbq_handler(blacklist_import_cb, lambda c: c.data == CBT_BLACKLIST_IMPORT)
    tg.cbq_handler(_make_float_editor("min_profit", *RANGE_MIN_PROFIT, "💰 Мин. прибыль на заказ (₽).",
                                      presets=[0, 10, 50, 100], section="prices"),
                   lambda c: c.data == CBT_EDIT_MINPROFIT)
    tg.cbq_handler(_make_float_editor("funpay_fee_percent", 0, 20, "💸 Комиссия FunPay (%).",
                                      presets=[0, 5, 6.5, 7.5, 10], section="prices"),
                       lambda c: c.data == CBT_EDIT_FEE)
    tg.cbq_handler(_make_float_editor("auto_lots_markup_percent", 0, 10000, "🏷 Наценка на цену (%).",
                                      presets=[20, 30, 50, 100], section="prices"),
                   lambda c: c.data == CBT_EDIT_MARKUP)
    tg.cbq_handler(_make_int_editor("auto_lot_node_id", 0, 10 ** 9,
                                    "🌐 ID подкатегории FunPay для автосоздания лотов "
                                    "(0 — базовая 1009). Число из URL подкатегории (…/lots/<ID>/).",
                                    presets=[0], section="prices"),
                   lambda c: c.data == CBT_EDIT_LOT_NODE)
    tg.cbq_handler(_make_int_editor("auto_lots_interval_min", 5, 86400, "⏱ Интервал авто-цен (мин).",
                                    presets=[10, 30, 60, 120], section="prices"),
                   lambda c: c.data == CBT_EDIT_AUTOLOTS_INT)
    tg.cbq_handler(_make_int_editor("auto_raise_interval_min", 5, 86400, "⬆️ Интервал поднятия лотов (мин).",
                                    presets=[30, 60, 120, 360], section="extra"),
                   lambda c: c.data == CBT_EDIT_RAISE_INT)
    tg.cbq_handler(_make_float_editor("balance_alert_threshold", *RANGE_BALANCE_ALERT, "📉 Порог баланса поставщика (₽).",
                                      presets=[50, 100, 200, 500], section="balance"),
                   lambda c: c.data == CBT_EDIT_BALTHRESH)
    tg.cbq_handler(_make_int_editor("link_wait_timeout_sec", *RANGE_LINK_TIMEOUT, "⏱ Таймаут ожидания ссылки (сек).",
                                    presets=[3600, 21600, 86400, 172800], section="orders"),
                   lambda c: c.data == CBT_EDIT_TIMEOUT)
    tg.cbq_handler(_make_int_editor("status_poll_interval_sec", *RANGE_POLL_INTERVAL, "🔄 Интервал опроса статусов (сек).",
                                    presets=[30, 60, 120, 300], section="orders"),
                   lambda c: c.data == CBT_EDIT_POLL)
    tg.cbq_handler(_make_int_editor("add_retries", 0, 20, "🔄 Повторов заказа при сбое.",
                                    presets=[0, 1, 3, 5], section="orders"),
                       lambda c: c.data == CBT_EDIT_RETRIES)
    tg.cbq_handler(_make_int_editor("lot_desc_match_limit", 20, 500,
                                    "📏 Макс. длина описания лота для подбора привязки (символов).",
                                    presets=[80, 120, 200, 500], section="extra"),
                       lambda c: c.data == CBT_EDIT_DESC_LIMIT)
    tg.cbq_handler(providers_cb, lambda c: c.data == CBT_PROVIDERS)
    tg.cbq_handler(provider_view_cb, lambda c: (c.data or "").startswith(f"{CBT_PROVIDER_VIEW}:"))
    # старые колбэки (пресет / ручной ввод) ведут на тот же ввод API-ключа
    tg.cbq_handler(provider_add_cb, lambda c: c.data in (
        CBT_PROVIDER_ADD, CBT_PROVIDER_PRESET, CBT_PROVIDER_ADD_MANUAL))
    tg.cbq_handler(provider_del_cb, lambda c: (c.data or "").startswith(f"{CBT_PROVIDER_DEL}:"))
    tg.cbq_handler(_provider_field_editor("name", "✏️ Введите новое имя:", 64),
                   lambda c: (c.data or "").startswith(f"{CBT_PROVIDER_EDIT_NAME}:"))
    tg.cbq_handler(_provider_field_editor("api_url", "🔗 Введите новый API URL:", 255),
                   lambda c: (c.data or "").startswith(f"{CBT_PROVIDER_EDIT_URL}:"))
    tg.cbq_handler(_provider_field_editor("api_key", "🔑 Введите новый API-ключ:", 255),
                   lambda c: (c.data or "").startswith(f"{CBT_PROVIDER_EDIT_KEY}:"))
    tg.cbq_handler(provider_balance_cb, lambda c: (c.data or "").startswith(f"{CBT_PROVIDER_BAL}:"))
    tg.cbq_handler(provider_services_cb, lambda c: (c.data or "").startswith(f"{CBT_PROVIDER_SERVICES}:"))
    tg.cbq_handler(autoreg_bind_cb, lambda c: (c.data or "").startswith(f"{CBT_AUTOREG_BIND}:"))
    tg.cbq_handler(mappings_cb, lambda c: c.data == CBT_MAPPINGS)
    tg.cbq_handler(mapping_add_cb, lambda c: c.data == CBT_MAPPING_ADD)
    tg.cbq_handler(mapping_del_cb, lambda c: (c.data or "").startswith(f"{CBT_MAPPING_DEL}:"))
    tg.cbq_handler(mapping_lot_cb, lambda c: (c.data or "").startswith(f"{CBT_MAPPING_LOT}:"))
    tg.cbq_handler(mapping_lotclear_cb, lambda c: (c.data or "").startswith(f"{CBT_MAPPING_LOTCLEAR}:"))
    tg.cbq_handler(mapping_commend_cb, lambda c: (c.data or "").startswith(f"{CBT_MAPPING_COMMEND}:"))
    tg.cbq_handler(mapping_commend_random_cb, lambda c: (c.data or "").startswith(f"{CBT_MAPPING_COMMEND_RANDOM}:"))
    tg.cbq_handler(mapping_export_cb, lambda c: c.data == CBT_MAPPING_EXPORT)
    tg.cbq_handler(mapping_import_cb, lambda c: c.data == CBT_MAPPING_IMPORT)
    tg.cbq_handler(svc_catalog_cb, lambda c: (c.data or "").startswith(f"{CBT_SVC_CATALOG}:"))
    tg.cbq_handler(svc_pick_cb, lambda c: (c.data or "").startswith(f"{CBT_SVC_PICK}:"))
    tg.cbq_handler(svc_catalog_back_cb, lambda c: (c.data or "").startswith(f"{CBT_SVC_CATALOG_BACK}:"))
    tg.cbq_handler(accounts_cb, lambda c: c.data == CBT_ACCOUNTS)
    tg.cbq_handler(account_add_cb, lambda c: c.data == CBT_ACCOUNT_ADD)
    tg.cbq_handler(account_import_cb, lambda c: c.data == CBT_ACCOUNT_IMPORT)
    tg.cbq_handler(account_export_cb, lambda c: c.data == CBT_ACCOUNT_EXPORT)
    tg.cbq_handler(msgs_cb, lambda c: c.data == CBT_MSGS)
    tg.cbq_handler(msg_slot_cb, lambda c: (c.data or "").startswith(f"{CBT_MSG_SLOT}:"))
    tg.cbq_handler(msg_add_cb, lambda c: (c.data or "").startswith(f"{CBT_MSG_ADD}:"))
    tg.cbq_handler(msg_del_cb, lambda c: (c.data or "").startswith(f"{CBT_MSG_DEL}:"))
    tg.cbq_handler(domains_cb, lambda c: c.data == CBT_DOMAINS)
    # --- улучшения меню ---
    tg.cbq_handler(domains_edit_cb, lambda c: c.data == CBT_DOMAINS_EDIT)
    tg.cbq_handler(domains_reset_cb, lambda c: c.data == CBT_DOMAINS_RESET)
    tg.cbq_handler(balances_cb, lambda c: c.data == CBT_BALANCES)
    tg.cbq_handler(active_detail_cb, lambda c: (c.data or "").startswith(f"{CBT_ACTIVE_DETAIL}:"))
    tg.cbq_handler(active_clear_confirm_cb, lambda c: (c.data or "").startswith(f"{CBT_ACTIVE_CLEAR_CONFIRM}:"))
    tg.cbq_handler(provider_del_confirm_cb, lambda c: (c.data or "").startswith(f"{CBT_PROVIDER_DEL_CONFIRM}:"))
    tg.cbq_handler(mapping_del_confirm_cb, lambda c: (c.data or "").startswith(f"{CBT_MAPPING_DEL_CONFIRM}:"))
    tg.cbq_handler(msg_del_confirm_cb, lambda c: (c.data or "").startswith(f"{CBT_MSG_DEL_CONFIRM}:"))
    tg.cbq_handler(link_del_confirm_cb, lambda c: (c.data or "").startswith(f"{CBT_LINK_DEL_CONFIRM}:"))
    tg.cbq_handler(account_pool_cb, lambda c: (c.data or "").startswith(f"{CBT_ACCOUNT_POOL}:"))
    tg.cbq_handler(msg_preview_cb, lambda c: (c.data or "").startswith(f"{CBT_MSG_PREVIEW}:"))
    tg.cbq_handler(set_float_cb, lambda c: (c.data or "").startswith(f"{CBT_SETF}:"))
    tg.cbq_handler(set_int_cb, lambda c: (c.data or "").startswith(f"{CBT_SETI}:"))
    tg.cbq_handler(toggle_price_cb, lambda c: c.data == CBT_TOGGLE_PRICE)
    tg.cbq_handler(provider_style_cb, lambda c: (c.data or "").startswith(f"{CBT_PROVIDER_EDIT_STYLE}:"))
    tg.cbq_handler(set_style_cb, lambda c: (c.data or "").startswith(f"{CBT_SETS}:"))

    def cmd_open(m) -> None:
        _persist_op(m.chat.id)
        text = _home_text()
        kb = _home_kb()
        cid = m.chat.id
        mid = _menu_msg_ids.get(cid)
        if mid is not None:
            try:
                bot.edit_message_text(text, cid, mid, parse_mode="HTML", reply_markup=kb)
                return
            except Exception:
                _menu_msg_ids.pop(cid, None)
        try:
            sent = bot.send_message(cid, text, reply_markup=kb, parse_mode="HTML")
            _menu_msg_ids[cid] = sent.message_id
        except Exception:
            logger.exception(f"{LOGGER_PREFIX} cmd_open failed")
    tg.msg_handler(cmd_open, commands=["steamsmm"])
    try:
        cardinal.add_telegram_commands(UUID, [
            ("steamsmm", "Steam SMM: открыть меню", True),
        ])
    except Exception:
        logger.exception("add_telegram_commands failed")

    _ensure_scheduler(cardinal)
    _reconcile_waiting(cardinal)
    _balance_alert_state.clear()

    logger.info(f"{LOGGER_PREFIX} v{VERSION} запущен")


def _on_delete(cardinal: "Cardinal", *args) -> None:
    _scheduler_stop.set()


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


BIND_TO_PRE_INIT = [init]
BIND_TO_NEW_ORDER = [_on_new_order]
BIND_TO_NEW_MESSAGE = [_on_new_message]
BIND_TO_DELETE = _on_delete


# ------------------------------------------------------------------------------
# Внутренние данные донат-баннера (закодированы + подпись):
# если реквизиты подменят на свои, подпись не сойдётся и баннер не отправится.
# ------------------------------------------------------------------------------
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

