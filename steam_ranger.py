"""
Steam Region Ranger — плагин для FunPay Cardinal.

Автоматическая смена региона Steam через покупку самой дешёвой игры в
магазине целевого региона. Управление через Telegram.

Фазы (см. extracted/plan.md):
  • Фаза 1 (1.0.0) — каркас и UI                            [готово]
  • Фаза 2 (1.1.0) — Fernet + unlock + CRUD карт           [готово]
  • Фаза 3 (1.2.0) — прокси-пул                            [готово]
  • Фаза 4 (1.3.0) — Steam-логин с Guard через чат         [готово]
  • Фаза 5 (1.4.0) — поиск дешёвой игры                    [готово]
  • Фаза 6 (1.5.0) — покупка + 3DS                         [готово]
  • Фаза 7 (1.6.0) — автоцикл                              [готово]
  • Фаза 8 (1.7.0) — ручной список игр (per-region)        [этот коммит]

Phase 8 (manual games):
  Кроме автопоиска по Steam Store можно вручную добавлять appid в свой
  список (с тэгом региона). Если для текущего региона есть хоть одна
  ручная игра — pipeline берёт самую дешёвую из неё, иначе fallback
  на автопоиск. Имя и цена подгружаются из /api/appdetails при добавлении
  и по кнопке «🔄 Обновить цены». Контролируется флагом
  `prefer_manual_list` (default True) в config.json.

Безопасность Фазы 2:
  Все чувствительные файлы (`cards.enc` в этой фазе, в следующих —
  `proxies.enc`, `session.enc`) шифруются Fernet. Ключ выводится из
  мастер-парольной фразы через PBKDF2-HMAC-SHA256 (200 000 итераций,
  32 байта, salt в meta.json). Мастер-фраза вводится один раз за процесс
  командой /sranger_unlock. Ключ держится в памяти процесса. После
  рестарта Cardinal — нужно ввести фразу заново.

  CVV не хранится. В Фазе 2 поля карты — всё кроме CVV.
"""
from __future__ import annotations

import base64
import csv
import importlib
import json
import logging
import os
import pickle
import random
import re
import secrets
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING, Any, Optional

# ── Авто-установка зависимостей ─────────────────────────────────────────────
# `cryptography` обычно уже стоит (зависимость FPC), но если нет — ставим
# в тот же интерпретатор. Стиль повторяет minecraft_donate._ensure_dependency.
_BOOT_LOGGER = logging.getLogger("FPC.steam_ranger")


def _ensure_dependency(pip_name: str, import_name: Optional[str] = None) -> bool:
    mod_name = import_name or pip_name
    try:
        importlib.import_module(mod_name)
        return True
    except ImportError:
        pass
    if os.environ.get("SRR_NO_AUTOINSTALL") == "1":
        _BOOT_LOGGER.warning(
            "steam_ranger: модуль %r не найден, авто-установка отключена "
            "(SRR_NO_AUTOINSTALL=1).", mod_name)
        return False
    _BOOT_LOGGER.warning(
        "steam_ranger: модуль %r не найден, ставлю %r через pip...",
        mod_name, pip_name)
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install",
             "--disable-pip-version-check", "--quiet", pip_name],
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    except Exception as exc:
        _BOOT_LOGGER.error(
            "steam_ranger: не удалось установить %r автоматически: %s. "
            "Поставь вручную: %s -m pip install %s",
            pip_name, exc, sys.executable, pip_name)
        return False
    importlib.invalidate_caches()
    try:
        importlib.import_module(mod_name)
        return True
    except ImportError as exc:
        _BOOT_LOGGER.error(
            "steam_ranger: %r поставился, но импорт всё равно падает: %s",
            pip_name, exc)
        return False


_ensure_dependency("cryptography")
# PySocks нужен для socks5/socks4 прокси через requests. requests сам по
# себе поддерживает http/https, но socks-схемы делегирует PySocks (импорт
# `socks`), и без него падает на стадии резолва прокси.
_ensure_dependency("PySocks", "socks")

import requests
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.padding import PKCS1v15
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from telebot.types import (
    CallbackQuery,
    InlineKeyboardButton as B,
    InlineKeyboardMarkup as K,
    Message,
)

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
DONATION_CALLBACK_PREFIX = "srr_dn"    # префикс колбэков кнопок баннера
DONATION_PLUGIN_NAME = "Steam Region Ranger"  # имя плагина в шапке баннера
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

# ---------- мета ----------
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


NAME = "Steam Region Ranger"
VERSION = "1.11.0"
DESCRIPTION = (
    "Автоматическая смена региона Steam через покупку самой дешёвой игры "
    "в магазине целевого региона. Управление через Telegram-меню (карты, "
    "прокси, Steam-логин с Guard-кодом из чата). "
    "v1.9.0: шифрование локального хранилища убрано (данные в открытом виде), "
    "мастер-пароль больше не требуется."
    " v1.10.0: покупательский поток на FunPay — покупатель присылает логин/пароль "
    "в чат, бот меняет регион его аккаунта операторской картой (по умолчанию ВЫКЛ)."
)
CREDITS = "@drakelovc"
UUID = "3e7f9c1d-2a85-4b6e-9f02-c5d1a8b73e4f"
SETTINGS_PAGE = True

logger = logging.getLogger("FPC.plugin.steam_ranger")

# ---------- пути ----------
PLUGIN_DIR = os.path.join("storage", "plugins", "steam_ranger")
CONFIG_PATH = os.path.join(PLUGIN_DIR, "config.json")
META_PATH = os.path.join(PLUGIN_DIR, "meta.json")
CARDS_PATH = os.path.join(PLUGIN_DIR, "cards.enc")
PROXIES_PATH = os.path.join(PLUGIN_DIR, "proxies.enc")
SESSION_PATH = os.path.join(PLUGIN_DIR, "session.enc")
MANUAL_GAMES_PATH = os.path.join(PLUGIN_DIR, "manual_games.enc")
LOG_DIR = os.path.join(PLUGIN_DIR, "logs")
APPDETAILS_URL = "https://store.steampowered.com/api/appdetails/"

# ---------- network ----------
IP_API_URL = "http://ip-api.com/json/?fields=status,country,countryCode,query"
PROXY_CHECK_TIMEOUT = 10  # секунд
STEAM_LOGIN_TIMEOUT = 25  # секунд на любую отдельную ручку Steam в логине
STEAM_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
STEAM_SEARCH_URL = "https://store.steampowered.com/search/"
STEAM_SEARCH_TIMEOUT = 20  # секунд
SEARCH_CACHE_TTL = 3600    # 1 час

# ---------- crypto ----------
PBKDF2_ITERATIONS = 200_000
SALT_BYTES = 16

# ---------- callback prefixes ----------
CBT_PREFIX = "SRR"
CBT_OPEN = f"{CBT_PREFIX}:O"
CBT_TOGGLE_RUN = f"{CBT_PREFIX}:RUN"
CBT_REGION = f"{CBT_PREFIX}:REG"
# Отдельный префикс (не "SRR:REG:..."), иначе через `_cb` будет
# конфликтовать с CBT_REGION. Формат: CBT_REGION_PICK:KZ — записать регион.
CBT_REGION_PICK = f"{CBT_PREFIX}:RPK"
CBT_LOGIN = f"{CBT_PREFIX}:LOGIN"
CBT_LOGOUT = f"{CBT_PREFIX}:LOGOUT"
CBT_CARDS = f"{CBT_PREFIX}:CARDS"
CBT_CARD_ADD = f"{CBT_PREFIX}:CADD"
CBT_CARD_DETAIL = f"{CBT_PREFIX}:CDET"     # CBT_CARD_DETAIL:<id>
CBT_CARD_DEL = f"{CBT_PREFIX}:CDEL"        # CBT_CARD_DEL:<id>
CBT_CARD_MAIN = f"{CBT_PREFIX}:CMAIN"      # CBT_CARD_MAIN:<id>
CBT_CARD_ADD_MAIN_YES = f"{CBT_PREFIX}:CAMY"
CBT_CARD_ADD_MAIN_NO = f"{CBT_PREFIX}:CAMN"
CBT_PROXY = f"{CBT_PREFIX}:PROXY"
CBT_PROXY_ADD = f"{CBT_PREFIX}:P:ADD"
CBT_PROXY_RECHECK = f"{CBT_PREFIX}:P:RC"
CBT_PROXY_DEL = f"{CBT_PREFIX}:P:DEL"
CBT_PURCHASE_ONCE = f"{CBT_PREFIX}:BUY1"
# Отдельный префикс, иначе через `_cb` будет матчить и CBT_PURCHASE_ONCE
# (та же ловушка, что у CBT_REGION/CBT_REGION_PICK).
CBT_PURCHASE_REFRESH = f"{CBT_PREFIX}:BR1"
# Phase 6: callback-кнопки покупки.
CBT_PURCHASE_BUY = f"{CBT_PREFIX}:BBUY"     # «💳 Купить эту»
CBT_DRY_RUN_TOGGLE = f"{CBT_PREFIX}:DRY"    # «🧪 Dry-run»
CBT_3DS_DONE = f"{CBT_PREFIX}:3DSY"         # «✅ Подтвердил 3DS»
CBT_3DS_CANCEL = f"{CBT_PREFIX}:3DSN"       # «❌ Отменить 3DS»
# v1.10.0: 3DS-подтверждение для покупательского заказа (по order_id)
CBT_BUYER_3DS = f"{CBT_PREFIX}:B3DSY"
CBT_BUYER_3DS_CANCEL = f"{CBT_PREFIX}:B3DSN"
# v1.10.0: меню покупательского потока
CBT_BUYER_MENU = f"{CBT_PREFIX}:BMENU"
CBT_BUYER_TOGGLE = f"{CBT_PREFIX}:BTGL"
CBT_BUYER_CVV = f"{CBT_PREFIX}:BCVV"
CBT_BUYER_MAP_ADD = f"{CBT_PREFIX}:BMADD"
CBT_BUYER_MAP_PICK = f"{CBT_PREFIX}:BMPICK"   # отдельный префикс (как REG/RPK)
CBT_BUYER_MAP_RM = f"{CBT_PREFIX}:BMRM"
# Phase 8: ручной список игр (per-region).
CBT_MANUAL_GAMES = f"{CBT_PREFIX}:MGAMES"   # экран списка
CBT_MGAME_ADD = f"{CBT_PREFIX}:MGADD"       # «➕ Добавить appid»
CBT_MGAME_DEL = f"{CBT_PREFIX}:MGDEL"       # CBT_MGAME_DEL:<game_id>
CBT_MGAME_REFRESH = f"{CBT_PREFIX}:MGREF"   # «🔄 Обновить цены»
CBT_MGAME_TOGGLE_PREFER = f"{CBT_PREFIX}:MGTGL"  # toggle prefer_manual_list
CBT_STATUS = f"{CBT_PREFIX}:STATUS"
CBT_UNLOCK = f"{CBT_PREFIX}:UNLOCK"
# v1.8.0
CBT_PREFLIGHT = f"{CBT_PREFIX}:PRE"
CBT_TOGGLE_REGION_NOTIFY = f"{CBT_PREFIX}:RNT"
CBT_EDIT_REGION_INTERVAL = f"{CBT_PREFIX}:RIV"
CBT_MORE = f"{CBT_PREFIX}:MORE"
CBT_GUIDE = f"{CBT_PREFIX}:GUIDE"

# ---------- telegram input states ----------
ST_ASK_PASSPHRASE = f"{CBT_PREFIX}:S_PASS"
ST_ASK_PASSPHRASE_CONFIRM = f"{CBT_PREFIX}:S_PASS2"  # подтверждение при первом unlock
ST_LOGIN_CREDS = f"{CBT_PREFIX}:S_LCRED"
ST_LOGIN_CODE = f"{CBT_PREFIX}:S_LCODE"
ST_CARD_NUMBER = f"{CBT_PREFIX}:S_CNUM"
ST_CARD_EXPIRY = f"{CBT_PREFIX}:S_CEXP"
ST_CARD_NAME = f"{CBT_PREFIX}:S_CNAME"
ST_CARD_PHONE = f"{CBT_PREFIX}:S_CPHONE"
ST_CARD_COUNTRY = f"{CBT_PREFIX}:S_CCOUNTRY"
ST_CARD_CITY = f"{CBT_PREFIX}:S_CCITY"
ST_CARD_STREET = f"{CBT_PREFIX}:S_CSTREET"
ST_CARD_ZIP = f"{CBT_PREFIX}:S_CZIP"
ST_ADD_PROXY = f"{CBT_PREFIX}:S_PROXY"
ST_ASK_CVV = f"{CBT_PREFIX}:S_CVV"
ST_ASK_CVV_AUTO = f"{CBT_PREFIX}:S_CVVA"
ST_WAIT_3DS_CONFIRM = f"{CBT_PREFIX}:S_3DS"
# Phase 8
ST_ADD_APPID = f"{CBT_PREFIX}:S_APPID"
# v1.10.0 — покупательский поток
ST_BUYER_CVV = f"{CBT_PREFIX}:S_BCVV"
ST_BUYER_MAP_LOT = f"{CBT_PREFIX}:S_BMLOT"
# v1.8.0
ST_ASK_REGION_INTERVAL = f"{CBT_PREFIX}:S_RIV"

# ---------- список регионов для меню ----------
REGIONS: list[tuple[str, str]] = [
    ("KZ", "🇰🇿 Казахстан"),
    ("UA", "🇺🇦 Украина"),
    ("TR", "🇹🇷 Турция"),
    ("AR", "🇦🇷 Аргентина"),
    ("BR", "🇧🇷 Бразилия"),
    ("IN", "🇮🇳 Индия"),
    ("RU", "🇷🇺 Россия"),
    ("US", "🇺🇸 США"),
    ("PL", "🇵🇱 Польша"),
    ("CN", "🇨🇳 Китай"),
]
REGION_LABELS: dict[str, str] = {code: lbl for code, lbl in REGIONS}
SUPPORTED_REGION_CODES: set[str] = {code for code, _ in REGIONS}


def _resolve_order_region(lot_id: Any, lot_region_map: dict) -> Optional[str]:
    """Вернуть целевой регион (CC) для лота из маппинга `lot_id -> CC`, либо None.

    Лот считается «региональным» только если он есть в маппинге и сопоставлен
    поддерживаемому региону. Ключи маппинга сравниваются как строки.
    """
    if not lot_id or not isinstance(lot_region_map, dict):
        return None
    cc = lot_region_map.get(str(lot_id))
    if cc is None:
        return None
    cc = str(cc).strip().upper()
    return cc if cc in SUPPORTED_REGION_CODES else None


def _resolve_region_by_name(order_text: str, lot_region_map: dict) -> Optional[str]:
    """Фоллбэк-связка по названию (как в steam_rental._match_lot).

    Если в `lot_region_map` ключ НЕ числовой — он трактуется как ключевое слово
    (часть названия лота). Возвращает регион первого ключа, который входит
    подстрокой в `order_text` (название/описание заказа). Самые длинные ключи
    проверяются первыми — точное совпадение приоритетнее короткого.
    """
    if not order_text or not isinstance(lot_region_map, dict):
        return None
    text_low = order_text.lower()
    # длинные ключи первыми (точнее), числовые ключи (lot_id) тут не участвуют
    name_keys = [k for k in lot_region_map if not str(k).strip().isdigit()]
    for key in sorted(name_keys, key=lambda k: -len(str(k))):
        kw = str(key).strip().lower()
        if kw and kw in text_low:
            cc = str(lot_region_map.get(key)).strip().upper()
            if cc in SUPPORTED_REGION_CODES:
                return cc
    return None


def _buyer_map_add(cfg: dict, lot_id: Any, region: str) -> tuple[bool, str]:
    """Добавить/обновить запись lot→регион в конфиге. Ключ — либо lot_id (число),
    либо ключевое слово из названия лота. Возвращает (ok, message)."""
    region = str(region).strip().upper()
    if region not in SUPPORTED_REGION_CODES:
        return False, f"Регион {region} не поддерживается ({', '.join(sorted(SUPPORTED_REGION_CODES))})"
    lid = str(lot_id).strip()
    if not lid:
        return False, "Пустой ключ (lot_id или название)"
    cfg.setdefault("lot_region_map", {})[lid] = region
    kind = "лот" if lid.isdigit() else "по названию"
    return True, f"{kind} «{lid}» → {region}"


def _buyer_map_remove(cfg: dict, lot_id: Any) -> bool:
    """Удалить запись lot_id из маппинга. True если запись была."""
    m = cfg.setdefault("lot_region_map", {})
    return m.pop(str(lot_id).strip(), None) is not None


def _buyer_menu_text() -> str:
    cfg = _load_config()
    on = "🟢 ВКЛ" if cfg.get("buyer_flow_enabled") else "🔴 ВЫКЛ"
    have_cvv = "да" if (_autocycle_cvv or _cvv_in_memory) else "нет"
    dry = "да" if cfg.get("dry_run_purchases", True) else "НЕТ (боевой)"
    m = cfg.get("lot_region_map") or {}
    lines = [
        f"<b>🛒 Покупательский поток (FunPay)</b>: {on}",
        f"CVV в памяти: <b>{have_cvv}</b> · dry-run: <b>{dry}</b>",
        "",
        "Покупатель оплачивает лот → присылает логин/пароль в чат → "
        "бот меняет регион его аккаунта операторской картой.",
        "",
        "<b>Лот / название → регион:</b>",
    ]
    lines += [f"  • <code>{lid}</code> → {cc}" for lid, cc in m.items()] or ["  (пусто)"]
    return "\n".join(lines)


def _buyer_menu_kb() -> K:
    cfg = _load_config()
    m = cfg.get("lot_region_map") or {}
    kb = K()
    on = bool(cfg.get("buyer_flow_enabled"))
    kb.add(B("🔴 Выключить поток" if on else "🟢 Включить поток",
             callback_data=CBT_BUYER_TOGGLE))
    kb.add(B("🔐 Задать CVV операторской карты", callback_data=CBT_BUYER_CVV))
    kb.add(B("➕ Привязать лот к региону", callback_data=CBT_BUYER_MAP_ADD))
    for lid, cc in list(m.items())[:20]:
        kb.add(B(f"❌ {lid} → {cc}", callback_data=f"{CBT_BUYER_MAP_RM}:{lid}"))
    kb.add(B("◀️ Назад в меню", callback_data=CBT_OPEN))
    return kb

# ---------- значения по умолчанию ----------
DEFAULT_CONFIG: dict[str, Any] = {
    "region": "KZ",
    "running": False,
    "bought_today": 0,
    "successes": 0,
    "attempts": 0,
    "last_error": "",
    # Phase 6: dry-run для покупочного pipeline. True = логируем что бы
    # отправили, но реальные /checkout/inittransaction и /finalizetransaction
    # пропускаются. Юзер должен явно выставить в False для боевого режима
    # (после прогона в DRY_RUN и просмотра логов).
    "dry_run_purchases": True,
    # Phase 8: если True (default) и для текущего региона есть хотя бы
    # одна игра в manual_games.enc — берём её (cheapest first), иначе
    # fallback на _search_cheap_games. Если False — всегда автопоиск.
    "prefer_manual_list": True,
    # v1.8.0: уведомление о смене региона аккаунта + периодическая проверка.
    "region_change_notify": True,
    "region_check_interval_sec": 600,   # 0 = периодическая проверка выключена
    "last_known_region": "",            # "" = ещё не зафиксировано
    "operator_chat_id": None,           # захватывается при открытии меню
    # v1.10.0: покупательский поток смены региона на FunPay (по умолчанию ВЫКЛ).
    "buyer_flow_enabled": False,        # вкл/выкл реакцию на заказы FunPay
    "lot_region_map": {},               # {lot_id(str) | ключевое слово: CC} — какой лот/название в какой регион
    "region_card_id": "",               # id операторской карты для оплаты ("" = первая)
    "buyer_step_timeout_sec": 600,      # таймаут ожидания ответа покупателя на шаге
    "buyer_cred_attempts": 3,           # попытки ввода логина/пароля
    "buyer_code_attempts": 3,           # попытки ввода Guard-кода
    "auto_refund_on_fail": False,       # авто-возврат на FunPay при фейле (по умолч. нет)
}

DEFAULT_META: dict[str, Any] = {
    "salt": None,         # base64 строка после первого unlock
    "version": 1,         # версия формата meta.json (для миграций)
}


# ============================================================================
# I/O — JSON
# ============================================================================
def _ensure_dir() -> None:
    os.makedirs(PLUGIN_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)


def _load_json(path: str, default: Any) -> Any:
    _ensure_dir()
    if not os.path.exists(path):
        return json.loads(json.dumps(default))
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        logger.exception(
            "steam_ranger: не удалось прочитать %s, fallback на default", path)
        return json.loads(json.dumps(default))
    if isinstance(default, dict) and isinstance(data, dict):
        for k, v in default.items():
            data.setdefault(k, v)
    return data


def _save_json(path: str, data: Any) -> None:
    _ensure_dir()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _load_config() -> dict[str, Any]:
    return _load_json(CONFIG_PATH, DEFAULT_CONFIG)


def _save_config(cfg: dict[str, Any]) -> None:
    _save_json(CONFIG_PATH, cfg)


def _load_meta() -> dict[str, Any]:
    return _load_json(META_PATH, DEFAULT_META)


def _save_meta(meta: dict[str, Any]) -> None:
    _save_json(META_PATH, meta)


# ============================================================================
# Storage codec (v1.9.0: PLAINTEXT — encryption removed per operator request)
# ============================================================================
# ВНИМАНИЕ: с v1.9.0 локальное хранилище (карты, прокси, Steam-сессия,
# manual_games) НЕ шифруется — данные лежат на диске в открытом виде (JSON).
# Мастер-пароль и Fernet убраны. Эти функции оставлены как тонкие обёртки
# над JSON, чтобы не менять все места чтения/записи. Ключ игнорируется.
def _derive_key(passphrase: str, salt: bytes) -> bytes:
    """Deprecated: encryption removed. Returns a constant placeholder."""
    return b"plaintext"


def _encrypt_bytes(key: bytes, data: bytes) -> bytes:
    return data


def _decrypt_bytes(key: bytes, blob: bytes) -> Optional[bytes]:
    return blob


def _encrypt_json(key: bytes, data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def _decrypt_json(key: bytes, blob: bytes) -> Any:
    try:
        return json.loads(blob.decode("utf-8"))
    except Exception:
        # Старый зашифрованный файл или мусор — трактуем как пусто.
        logger.debug("steam_ranger: storage blob is not valid JSON, treating as empty")
        return None


# ============================================================================
# Состояние процесса (не сериализуется)
# ============================================================================
# v1.9.0: encryption removed — storage is always accessible. The sentinel
# keeps the historical `if _master_key is None` guards working (always unlocked).
_master_key: Optional[bytes] = b"plaintext"   # not a real key; storage is plaintext
_steam_logged_in: bool = False
_cvv_in_memory: Optional[str] = None
_run_lock = threading.Lock()
# Временный буфер незавершённых добавлений карт: {user_id: {field: value}}
_card_drafts: dict[int, dict[str, str]] = {}
# v1.10.0: незавершённая привязка лота к региону через меню {user_id: lot_id}
_buyer_map_pending: dict[int, str] = {}
# Временный буфер для двухшагового unlock (первая фраза при пустом meta)
_pending_first_passphrase: dict[int, str] = {}
# Активные SteamInteractiveSession в процессе диалога (Фаза 4):
# {tg_user_id: SteamInteractiveSession}
_pending_logins: dict[int, "SteamInteractiveSession"] = {}
# Phase 6: pending purchases per tg user_id, между ASK_CVV/3DS-confirm.
# Хранит engine, transid, game, card_id, dry_run для последующего finalize.
_pending_purchases: dict[int, dict] = {}
_purchase_lock = threading.Lock()
# Phase 7: autocycle thread + stop event + CVV в RAM. Тред живёт от
# Start до Stop (или auto-stop при совпадении региона / 5 неудач подряд).
_autocycle_thread: Optional[threading.Thread] = None
_autocycle_stop = threading.Event()
_autocycle_cvv: Optional[str] = None
AUTOCYCLE_PAUSE_MIN = 60
AUTOCYCLE_PAUSE_MAX = 180
AUTOCYCLE_MAX_FAILURES = 5
# Текущая активная Steam-сессия (после успешного логина или загрузки
# session.enc на unlock). None = не залогинен.
_steam_session: Optional[requests.Session] = None
_steam_login_name: Optional[str] = None


def _is_unlocked() -> bool:
    return _master_key is not None


def _is_steam_logged_in() -> bool:
    """True если в памяти есть валидная Steam-сессия (cookies загружены).
    Реальная проверка через Steam (валидны ли куки) происходит ленивее —
    при первом использовании сессии для покупки. Здесь — только наличие.
    """
    return _steam_session is not None


# ============================================================================
# v1.10.0 — покупательский поток FunPay: OrderSession + чистые переходы
# ============================================================================
STEP_AWAIT_CREDS = "await_creds"
STEP_AWAIT_GUARD = "await_guard"
STEP_BUSY = "busy"
STEP_DONE = "done"


class OrderSession:
    """Изолированное состояние одного заказа смены региона.

    Учётные данные покупателя (login/password/guard_code) живут ТОЛЬКО здесь, в
    памяти, и обнуляются в _close_session по завершении заказа.
    """

    def __init__(self, order_id: Any, chat_id: Any, buyer: str, region: str,
                 now: Optional[float] = None) -> None:
        self.order_id = str(order_id)
        self.chat_id = chat_id
        self.buyer = buyer or ""
        self.region = region
        self.step = STEP_AWAIT_CREDS
        self.login: Optional[str] = None
        self.password: Optional[str] = None
        self.guard_code: Optional[str] = None
        self.steam: Optional["SteamInteractiveSession"] = None
        self.proxy: Optional[str] = None
        self.cred_attempts = 0
        self.code_attempts = 0
        self.purchase_engine: Any = None      # для resume 3DS
        self.purchase_transid: Optional[str] = None
        self.created_at = time.time() if now is None else now
        self.deadline = 0.0


_order_sessions: dict[str, OrderSession] = {}
_sessions_lock = threading.Lock()


def _create_session(order_id: Any, chat_id: Any, buyer: str, region: str,
                    timeout_sec: int, now: Optional[float] = None
                    ) -> tuple[OrderSession, bool]:
    """Создать сессию заказа идемпотентно. Возвращает (session, created).
    Если сессия по этому order_id уже есть — возвращает её с created=False."""
    now = time.time() if now is None else now
    with _sessions_lock:
        existing = _order_sessions.get(str(order_id))
        if existing is not None:
            return existing, False
        s = OrderSession(order_id, chat_id, buyer, region, now=now)
        s.deadline = now + max(1, int(timeout_sec))
        _order_sessions[s.order_id] = s
        return s, True


def _get_session_for_chat(chat_id: Any, buyer: Optional[str] = None
                          ) -> Optional[OrderSession]:
    """Найти активную (не завершённую) сессию по чату; при коллизии — по покупателю."""
    with _sessions_lock:
        for s in _order_sessions.values():
            if s.step != STEP_DONE and str(s.chat_id) == str(chat_id):
                return s
        if buyer:
            for s in _order_sessions.values():
                if s.step != STEP_DONE and s.buyer and s.buyer == buyer:
                    return s
    return None


def _close_session(order_id: Any, reason: str = "") -> None:
    """Завершить заказ: убрать из реестра и затереть секреты/Steam-сессию."""
    with _sessions_lock:
        s = _order_sessions.pop(str(order_id), None)
    if s is not None:
        s.login = None
        s.password = None
        s.guard_code = None
        s.steam = None
        s.purchase_engine = None
        s.step = STEP_DONE
        if reason:
            logger.info("steam_ranger: order %s closed (%s)", s.order_id, reason)


def _evict_expired_sessions(now: Optional[float] = None) -> list[OrderSession]:
    """Снять с реестра истёкшие по дедлайну активные сессии. Возвращает их список
    (для уведомления оператора вызывающей стороной). Секреты затираются."""
    now = time.time() if now is None else now
    expired: list[OrderSession] = []
    with _sessions_lock:
        for oid, s in list(_order_sessions.items()):
            if s.step != STEP_DONE and _is_expired(now, s.deadline):
                expired.append(s)
                _order_sessions.pop(oid, None)
    for s in expired:
        s.login = None
        s.password = None
        s.guard_code = None
        s.steam = None
        s.step = STEP_DONE
    return expired


# --- чистые функции переходов стейт-машины (без сайд-эффектов) ---

def _step_after_creds(parsed_ok: bool, attempts: int, max_attempts: int) -> str:
    """'login' при успешном разборе кред; иначе 'retry' пока attempts<max, 'fail' по лимиту."""
    if parsed_ok:
        return "login"
    return "retry" if attempts < max_attempts else "fail"


def _step_after_login(result: str, attempts: int, max_attempts: int) -> str:
    """result ∈ {ok, need_code, bad_creds, error}."""
    if result == "ok":
        return "purchase"
    if result == "need_code":
        return "await_guard"
    if result == "bad_creds":
        return "retry_creds" if attempts < max_attempts else "fail"
    return "fail"


def _step_after_code(result: str, attempts: int, max_attempts: int) -> str:
    """result ∈ {ok, bad_code, error}."""
    if result == "ok":
        return "purchase"
    if result == "bad_code":
        return "retry" if attempts < max_attempts else "fail"
    return "fail"


def _is_expired(now: float, deadline: float) -> bool:
    return float(now) > float(deadline)


# ============================================================================
# Card storage (Fernet)
# ============================================================================
def _load_cards() -> list[dict]:
    """Возвращает список карт. Если plugin заблокирован или файл отсутствует — []."""
    if _master_key is None:
        return []
    if not os.path.exists(CARDS_PATH):
        return []
    try:
        with open(CARDS_PATH, "rb") as f:
            blob = f.read()
    except Exception:
        logger.exception("steam_ranger: read cards.enc failed")
        return []
    data = _decrypt_json(_master_key, blob)
    if not isinstance(data, list):
        logger.error("steam_ranger: cards.enc decrypted to %s, expected list",
                     type(data).__name__)
        return []
    return data


def _save_cards(cards: list[dict]) -> None:
    if _master_key is None:
        raise RuntimeError("plugin is locked, refusing to save cards")
    _ensure_dir()
    blob = _encrypt_json(_master_key, cards)
    tmp = CARDS_PATH + ".tmp"
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, CARDS_PATH)


def _cards_count() -> int:
    if not _is_unlocked():
        return 0
    return len(_load_cards())


def _proxies_count() -> tuple[int, int]:
    """(всего, живых)."""
    if not _is_unlocked():
        return 0, 0
    pool = _load_proxies()
    alive = sum(1 for p in pool if p.get("alive"))
    return len(pool), alive


def _onoff(flag: Any) -> str:
    return "🟢 вкл" if flag else "🔴 выкл"


def _mask_pan(number: str) -> str:
    """Маскирует номер карты: '4242424242424242' -> '**** 4242'."""
    digits = "".join(ch for ch in number if ch.isdigit())
    if len(digits) < 4:
        return "****"
    return f"**** {digits[-4:]}"


def _detect_brand(number: str) -> str:
    """Грубое определение бренда по первой цифре/префиксу."""
    digits = "".join(ch for ch in number if ch.isdigit())
    if not digits:
        return "?"
    if digits[0] == "4":
        return "Visa"
    if digits[:2] in ("51", "52", "53", "54", "55") or (
            len(digits) >= 4 and 2221 <= int(digits[:4]) <= 2720):
        return "MC"
    if digits[:2] in ("34", "37"):
        return "Amex"
    if digits[:4] in ("6011",) or digits[:2] == "65":
        return "Discover"
    if digits[:2] in ("35",) or digits[0] == "3":
        return "JCB"
    return "Card"


def _new_card_id() -> str:
    """8-символьный hex для callback_data."""
    return secrets.token_hex(4)


# ============================================================================
# Proxy storage (Fernet) + ip-api validation
# ============================================================================
# Schema:
#   {
#       "id": "<hex>",
#       "url": "socks5://user:pass@host:port",
#       "country": "Kazakhstan" | None,
#       "country_code": "KZ" | None,        # ISO-2, как в Steam cc-параметре
#       "alive": True/False,
#       "last_check": <epoch int>,
#       "external_ip": "1.2.3.4" | None,    # IP, который видит ip-api
#   }
def _load_proxies() -> list[dict]:
    if _master_key is None:
        return []
    if not os.path.exists(PROXIES_PATH):
        return []
    try:
        with open(PROXIES_PATH, "rb") as f:
            blob = f.read()
    except Exception:
        logger.exception("steam_ranger: read proxies.enc failed")
        return []
    data = _decrypt_json(_master_key, blob)
    if not isinstance(data, list):
        logger.error("steam_ranger: proxies.enc decrypted to %s, expected list",
                     type(data).__name__)
        return []
    return data


def _save_proxies(pool: list[dict]) -> None:
    if _master_key is None:
        raise RuntimeError("plugin is locked, refusing to save proxies")
    _ensure_dir()
    blob = _encrypt_json(_master_key, pool)
    tmp = PROXIES_PATH + ".tmp"
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, PROXIES_PATH)


def _new_proxy_id() -> str:
    return secrets.token_hex(4)


# Поддерживаемые схемы. socks5h недоступен в plain `requests` — нужно
# socks5h:// для DNS через прокси, и это поддерживается PySocks. Включаем оба.
_PROXY_RE = re.compile(
    r"^(?P<scheme>socks5|socks5h|socks4|http|https)://"
    r"(?:(?P<user>[^:@/]+):(?P<pwd>[^@/]+)@)?"
    r"(?P<host>[\w.\-]+):(?P<port>\d{1,5})$"
)


def _validate_proxy_url(s: str) -> Optional[str]:
    """Возвращает нормализованный URL или None."""
    s = (s or "").strip()
    m = _PROXY_RE.match(s)
    if not m:
        return None
    port = int(m.group("port"))
    if not (1 <= port <= 65535):
        return None
    return s


def _mask_proxy_url(url: str) -> str:
    """`socks5://user:pass@1.2.3.4:1080` -> `socks5://***@1.2.3.4:1080`."""
    if "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    if "@" in rest:
        _creds, host = rest.rsplit("@", 1)
        return f"{scheme}://***@{host}"
    return url


def _check_proxy(url: str, timeout: int = PROXY_CHECK_TIMEOUT
                 ) -> tuple[bool, Optional[str], Optional[str], Optional[str]]:
    """Возвращает (alive, country_code, country, external_ip).

    На любую сетевую/парсерскую ошибку — alive=False, остальные None.
    Сами не ретраим: вызывающая сторона решает, перепроверять ли.
    """
    proxies = {"http": url, "https": url}
    try:
        r = requests.get(IP_API_URL, proxies=proxies, timeout=timeout,
                         headers={"User-Agent": "steam-ranger/0.1"})
    except Exception as exc:
        logger.debug("steam_ranger: proxy %s check failed at request: %s",
                     _mask_proxy_url(url), exc)
        return False, None, None, None
    if r.status_code != 200:
        logger.debug("steam_ranger: proxy %s ip-api status %s",
                     _mask_proxy_url(url), r.status_code)
        return False, None, None, None
    try:
        data = r.json()
    except Exception:
        logger.debug("steam_ranger: proxy %s ip-api invalid JSON",
                     _mask_proxy_url(url))
        return False, None, None, None
    if data.get("status") != "success":
        return False, None, None, None
    return (
        True,
        data.get("countryCode"),
        data.get("country"),
        data.get("query"),
    )


def _pick_proxy_for_region(region: str) -> Optional[str]:
    """Случайный живой прокси с country_code == region.
    Если таких нет — fallback на любой живой (с предупреждением в лог).
    """
    pool = _load_proxies()
    matching = [p for p in pool
                if p.get("alive") and p.get("country_code") == region]
    if matching:
        return random.choice(matching)["url"]
    alive = [p for p in pool if p.get("alive")]
    if alive:
        logger.warning(
            "steam_ranger: нет прокси под регион %s (всего живых: %d) — "
            "fallback на случайный живой; страна выходного IP не совпадёт",
            region, len(alive))
        return random.choice(alive)["url"]
    return None


def _new_card_id_kept_for_compat() -> None:
    """Оставлено пустым: см. _new_card_id выше; если что-то импортирует
    `_new_card_id_kept_for_compat`, ему вернётся None."""
    return None


# ============================================================================
# Steam interactive login (Phase 4)
# ============================================================================
# Используем legacy endpoint /login/dologin/ (JSON, ответы в JSON).
# Новый endpoint /IAuthenticationService/BeginAuthSessionViaCredentials/
# требует protobuf-сериализации, что значительно усложняет реализацию
# (нужны .proto-файлы Steam'а или ручная реализация wire-формата).
#
# Известное ограничение: legacy endpoint Valve постепенно сворачивает.
# На дату написания (2026-06) он всё ещё работает у большинства аккаунтов,
# но для свежесозданных аккаунтов или после изменений безопасности может
# вернуть `success=false` без явной причины. В таком случае:
#   1) залогиниться через браузер один раз (Steam запомнит "доверенное"
#      устройство для этого аккаунта),
#   2) повторить логин через плагин.
# Если и после этого endpoint не работает — нужен полноценный protobuf-flow.
class SteamLoginError(Exception):
    """Сетевая или протокольная ошибка при логине в Steam."""


class SteamInteractiveSession:
    """Двухстадийный логин Steam с Guard-кодом из Telegram-чата.

    Использование:
        sess = SteamInteractiveSession(login, password, proxy="socks5://...")
        result = sess.begin()
        if result == "need_code":
            # пользователь вводит код в Telegram
            result = sess.submit_code(code)
        if result == "ok":
            cookies = sess.session.cookies   # это RequestsCookieJar
        else:
            error = sess.last_message
    """

    BASE = "https://store.steampowered.com"
    LOGIN_GET_RSA = BASE + "/login/getrsakey/"
    LOGIN_DOLOGIN = BASE + "/login/dologin/"

    def __init__(self, login: str, password: str,
                 proxy: Optional[str] = None,
                 timeout: int = STEAM_LOGIN_TIMEOUT):
        self.login = login
        self._password = password
        self.proxy = proxy
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": STEAM_USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": self.BASE,
            "Referer": self.BASE + "/login/",
        })
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}
        # стейт между begin() и submit_code():
        self._password_encrypted: Optional[str] = None
        self._rsatimestamp: Optional[str] = None
        self._code_type: Optional[str] = None  # "email" | "mobile"
        self._email_steamid: Optional[str] = None
        self._email_domain: Optional[str] = None
        self.last_message: str = ""

    # ---- internals ----
    def _rsa_encrypt(self, mod_hex: str, exp_hex: str) -> str:
        n = int(mod_hex, 16)
        e = int(exp_hex, 16)
        pub = RSAPublicNumbers(e=e, n=n).public_key()
        ciphertext = pub.encrypt(self._password.encode("utf-8"), PKCS1v15())
        return base64.b64encode(ciphertext).decode("ascii")

    def _do_get_rsa(self) -> dict:
        try:
            r = self.session.post(
                self.LOGIN_GET_RSA,
                data={"username": self.login,
                      "donotcache": str(int(time.time() * 1000))},
                timeout=self.timeout)
        except Exception as exc:
            raise SteamLoginError(f"getrsakey: сетевая ошибка ({exc})") from exc
        try:
            return r.json()
        except Exception as exc:
            raise SteamLoginError(
                f"getrsakey: не-JSON (status={r.status_code}, "
                f"body={r.text[:120]!r})") from exc

    def _post_dologin(self, twofactor: str = "", emailauth: str = "") -> dict:
        if self._password_encrypted is None or self._rsatimestamp is None:
            raise SteamLoginError("RSA-стейт не инициализирован — call begin()")
        data = {
            "username": self.login,
            "password": self._password_encrypted,
            "rsatimestamp": self._rsatimestamp,
            "twofactorcode": twofactor,
            "emailauth": emailauth,
            "emailsteamid": self._email_steamid or "",
            "remember_login": "false",
            "captcha_text": "",
            "captchagid": "-1",
            "loginfriendlyname": "",
            "donotcache": str(int(time.time() * 1000)),
        }
        try:
            r = self.session.post(self.LOGIN_DOLOGIN, data=data,
                                   timeout=self.timeout)
        except Exception as exc:
            raise SteamLoginError(f"dologin: сетевая ошибка ({exc})") from exc
        try:
            return r.json()
        except Exception as exc:
            raise SteamLoginError(
                f"dologin: не-JSON (status={r.status_code}, "
                f"body={r.text[:120]!r})") from exc

    def _do_transfers(self, resp: dict) -> None:
        """Распространяем cookies на help/community домены через
        login/transfer URLs из ответа dologin."""
        urls = resp.get("transfer_urls") or []
        params = resp.get("transfer_parameters") or {}
        for url in urls:
            try:
                self.session.post(url, data=params, timeout=self.timeout)
            except Exception:
                logger.debug("steam_ranger: transfer to %s failed", url,
                             exc_info=True)

    def _dispatch(self, resp: dict) -> str:
        """Превращает ответ /dologin в один из 'ok' / 'need_code' / 'error'."""
        if resp.get("success"):
            self._do_transfers(resp)
            self.last_message = "OK"
            return "ok"

        if resp.get("emailauth_needed"):
            self._code_type = "email"
            self._email_steamid = resp.get("emailsteamid") or ""
            self._email_domain = resp.get("emaildomain") or "?"
            self.last_message = (
                f"email-Guard: код выслан на @{self._email_domain}")
            return "need_code"

        if resp.get("requires_twofactor"):
            self._code_type = "mobile"
            self.last_message = "Mobile Authenticator (2FA): открой приложение"
            return "need_code"

        if resp.get("captcha_needed"):
            self.last_message = (
                "Steam требует капчу — плагин это не поддерживает.\n"
                "Залогинься через браузер один раз и попробуй снова.")
            return "error"

        self.last_message = (
            resp.get("message") or "Login отклонён без явного сообщения")
        return "error"

    # ---- public ----
    def begin(self) -> str:
        try:
            rsa = self._do_get_rsa()
        except SteamLoginError as exc:
            self.last_message = str(exc)
            return "error"
        if not rsa.get("success"):
            self.last_message = (
                rsa.get("message")
                or "Steam getrsakey: success=false (часто — неверный логин)")
            return "error"
        try:
            self._password_encrypted = self._rsa_encrypt(
                rsa["publickey_mod"], rsa["publickey_exp"])
        except Exception as exc:
            self.last_message = f"RSA-шифрование пароля упало: {exc}"
            return "error"
        self._rsatimestamp = rsa["timestamp"]
        try:
            return self._dispatch(self._post_dologin())
        except SteamLoginError as exc:
            self.last_message = str(exc)
            return "error"

    def submit_code(self, code: str) -> str:
        if self._code_type is None:
            self.last_message = "begin() не вызван или код не запрошен"
            return "error"
        try:
            if self._code_type == "email":
                resp = self._post_dologin(emailauth=code)
            else:
                resp = self._post_dologin(twofactor=code)
            return self._dispatch(resp)
        except SteamLoginError as exc:
            self.last_message = str(exc)
            return "error"


# ============================================================================
# Steam session storage (session.enc)
# ============================================================================
# Storage schema (внутри Fernet):
#   {
#       "login": "username",
#       "cookies_b64": "<base64(pickle(cookiejar))>",
#       "saved_at": <epoch>,
#   }
def _save_steam_session(login: str, session: requests.Session) -> None:
    if _master_key is None:
        raise RuntimeError("plugin is locked, refusing to save session")
    _ensure_dir()
    cookies_pickle = pickle.dumps(session.cookies)
    payload = {
        "login": login,
        "cookies_b64": base64.b64encode(cookies_pickle).decode("ascii"),
        "saved_at": int(time.time()),
    }
    blob = _encrypt_json(_master_key, payload)
    tmp = SESSION_PATH + ".tmp"
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, SESSION_PATH)


def _load_steam_session() -> tuple[Optional[requests.Session], Optional[str]]:
    """Возвращает (session, login). (None, None) если файла нет/не расшифровался."""
    if _master_key is None:
        return None, None
    if not os.path.exists(SESSION_PATH):
        return None, None
    try:
        with open(SESSION_PATH, "rb") as f:
            blob = f.read()
    except Exception:
        logger.exception("steam_ranger: read session.enc failed")
        return None, None
    data = _decrypt_json(_master_key, blob)
    if not isinstance(data, dict):
        return None, None
    try:
        cookies = pickle.loads(base64.b64decode(data["cookies_b64"]))
    except Exception:
        logger.exception("steam_ranger: cookies unpickle failed")
        return None, None
    session = requests.Session()
    session.headers.update({
        "User-Agent": STEAM_USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
    })
    session.cookies = cookies
    return session, data.get("login")


def _delete_steam_session() -> None:
    try:
        if os.path.exists(SESSION_PATH):
            os.remove(SESSION_PATH)
    except Exception:
        logger.exception("steam_ranger: remove session.enc failed")


_LOGIN_LABELS = ("логин", "username", "account", "login", "user", "акк")
_PASS_LABELS = ("пароль", "password", "пасс", "pwd", "pass")
# плоский набор слов-меток (для отсева в fallback-разборе)
_CRED_LABEL_WORDS = {w.lower() for w in (_LOGIN_LABELS + _PASS_LABELS)}


def _login_ok(login: str) -> bool:
    return bool(re.match(r"^[A-Za-z0-9_\-.]{2,64}$", login or ""))


def _extract_labeled(labels: tuple[str, ...], text: str) -> Optional[str]:
    """Ищет «<label> [:=- ] <value>» (без учёта регистра, через перенос строк).
    value — первый «не-пробельный» токен после метки."""
    for lab in labels:
        m = re.search(
            r"(?<![A-Za-zА-Яа-я])" + re.escape(lab) + r"(?:\s*[:=\-]\s*|\s+)(\S+)",
            text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


def _validate_login_password(text: str) -> Optional[tuple[str, str]]:
    """Терпимый парсер логина/пароля из одного сообщения покупателя/оператора.

    Принимает, среди прочего:
      • ``Логин: user Пароль: pass`` / ``Login: user Password: pass``
      • многострочный вариант (логин на одной строке, пароль на другой)
      • ``user pass`` (без меток, через пробел)
      • ``user:pass`` (через двоеточие, без пробелов)
    Логин — 2-64 символа [A-Za-z0-9_.-]; пароль — что угодно без пробелов.
    """
    text = (text or "").strip()
    if not text:
        return None

    # 1) Явные метки (логин/пароль, login/password, в любом регистре, в т.ч. многострочно)
    login = _extract_labeled(_LOGIN_LABELS, text)
    password = _extract_labeled(_PASS_LABELS, text)
    if login and password and _login_ok(login):
        return login, password

    # 2) Fallback: режем по пробелам/двоеточиям, выкидываем слова-метки,
    #    берём первые два значимых токена (логин, затем пароль).
    tokens = [t for t in re.split(r"[\s:=]+", text) if t]
    tokens = [t for t in tokens if t.lower().strip(":=-") not in _CRED_LABEL_WORDS]
    if len(tokens) >= 2 and _login_ok(tokens[0]):
        return tokens[0], tokens[1]
    return None


def _validate_guard_code(text: str) -> Optional[str]:
    """Email-код — 5 ascii букв; mobile — 5 ascii букв/цифр."""
    s = (text or "").strip().upper()
    if not (4 <= len(s) <= 6):
        return None
    if not re.match(r"^[A-Z0-9]+$", s):
        return None
    return s


# ============================================================================
# Steam Store search (Phase 5)
# ============================================================================
# Ищем самые дешёвые игры в магазине целевого региона. Steam отдаёт
# /search/ как HTML с элементами `<a class="search_result_row" ...>`,
# каждый из которых имеет атрибуты:
#   data-ds-appid="<id>"        — appid (для бандлов — список через запятую)
#   data-price-final="<minor>"  — цена в minor-единицах локальной валюты
# Мы парсим их регуляркой, чтобы не тащить BeautifulSoup.
#
# cc-параметр + cookie steamCountry заставляют Steam отдать прайс
# нужного региона (по правилу — оба, чтобы не ловить случайные косяки).

_SEARCH_ROW_RE = re.compile(
    r'<a[^>]*class="search_result_row[^"]*"[^>]*?'
    r'data-ds-appid="(?P<appid>[\d,]+)"[^>]*?'
    r'data-price-final="(?P<price>\d+)"[^>]*?>'
    r'.*?<span class="title">(?P<name>[^<]+)</span>',
    re.DOTALL,
)

_search_cache: dict[str, tuple[float, list[dict]]] = {}
_search_cache_lock = threading.Lock()


def _set_steam_region_cookie(session: requests.Session, region: str) -> None:
    """`steamCountry=<CC>|0` (Steam URL-encode'ит сам). Используется
    при поиске и при покупке, чтобы магазин принял нужный регион."""
    session.cookies.set("steamCountry", f"{region}|0",
                        domain="store.steampowered.com", path="/")


def _decode_html_entities(s: str) -> str:
    """Декодируем основные HTML-entities из названия игры. Полноценный
    html.unescape не подключаем — Steam использует ограниченный набор."""
    return (s.replace("&amp;", "&")
            .replace("&#39;", "'").replace("&quot;", '"')
            .replace("&lt;", "<").replace("&gt;", ">")
            .replace("&nbsp;", " "))


def _search_cheap_games(region: str,
                        proxy: Optional[str] = None,
                        max_results: int = 10,
                        force_refresh: bool = False
                        ) -> list[dict]:
    """Топ-N самых дешёвых платных игр в магазине `region`.
    Кэш в RAM на SEARCH_CACHE_TTL секунд per-region.

    Возвращает list of dict:
      {appid: int, name: str, price_minor: int, cc: str}
    Бандлы (data-ds-appid содержит запятую) и бесплатные (price_minor=0)
    отфильтровываются.
    """
    now = time.time()
    if not force_refresh:
        with _search_cache_lock:
            if region in _search_cache:
                ts, results = _search_cache[region]
                if now - ts < SEARCH_CACHE_TTL:
                    logger.debug("steam_ranger: search cache hit for %s "
                                 "(%d games, age %.0fs)",
                                 region, len(results), now - ts)
                    return results[:max_results]

    sess = requests.Session()
    sess.headers.update({
        "User-Agent": STEAM_USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
    })
    if proxy:
        sess.proxies = {"http": proxy, "https": proxy}
    _set_steam_region_cookie(sess, region)

    params = {
        "category1": "998",         # paid Games (исключает demos/DLC/free)
        "supportedlang": "english",
        "sort_by": "Price_ASC",
        "cc": region,
        "l": "english",
    }
    try:
        r = sess.get(STEAM_SEARCH_URL, params=params,
                     timeout=STEAM_SEARCH_TIMEOUT)
    except Exception as exc:
        logger.warning("steam_ranger: search failed: %s", exc)
        return []
    if r.status_code != 200:
        logger.warning("steam_ranger: search status=%d body=%r",
                       r.status_code, r.text[:200])
        return []

    games: list[dict] = []
    for match in _SEARCH_ROW_RE.finditer(r.text):
        appid_raw = match.group("appid")
        if "," in appid_raw:
            continue
        try:
            appid = int(appid_raw)
            price_minor = int(match.group("price"))
        except ValueError:
            continue
        if price_minor <= 0:
            continue
        name = _decode_html_entities(match.group("name").strip())
        games.append({
            "appid": appid,
            "name": name,
            "price_minor": price_minor,
            "cc": region,
        })

    with _search_cache_lock:
        _search_cache[region] = (now, games)

    logger.info("steam_ranger: search %s -> %d games (cheapest=%s)",
                region, len(games),
                games[0]["name"] if games else "—")
    return games[:max_results]


def _format_price_minor(price_minor: int, region: str) -> str:
    """Форматирует цену из minor-units в человекочитаемое.
    Точное название валюты не определяем (нужна таблица CC->currency),
    показываем число и регион."""
    return f"{price_minor / 100:.2f} ({region})"


def _log_csv(filename: str, header: list[str], row: list[Any]) -> None:
    """Аппендит строку в CSV-лог в LOG_DIR. Создаёт файл с заголовком,
    если его ещё нет."""
    _ensure_dir()
    path = os.path.join(LOG_DIR, filename)
    is_new = not os.path.exists(path)
    try:
        with open(path, "a", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            if is_new:
                w.writerow(header)
            w.writerow(row)
    except Exception:
        logger.exception("steam_ranger: write csv %s failed", filename)


def _log_found_game(game: dict) -> None:
    _log_csv(
        "found_games.csv",
        ["timestamp", "region", "appid", "name", "price_minor"],
        [int(time.time()), game["cc"], game["appid"],
         game["name"], game["price_minor"]],
    )


def _log_purchase(game: dict, card: dict, region: str, dry_run: bool,
                  ok: bool, message: str,
                  transid: Optional[str] = None) -> None:
    _log_csv(
        "purchases.csv",
        ["timestamp", "region", "appid", "name", "price_minor",
         "card_last4", "dry_run", "ok", "transid", "message"],
        [int(time.time()), region, game.get("appid"), game.get("name"),
         game.get("price_minor"),
         _mask_pan(card.get("number", ""))[-4:],
         "1" if dry_run else "0",
         "1" if ok else "0",
         transid or "",
         message[:300]],
    )


# ============================================================================
# Steam web purchase pipeline (Phase 6)
# ============================================================================
# !!! ВАЖНО !!!
# Endpoints /cart/addtocart/, /checkout/inittransaction/, /checkout/
# finalizetransaction/ — НЕ задокументированы Valve. Реализация основана
# на reverse-engineering web-flow и известных опубликованных полях форм.
# Steam регулярно меняет анти-фрод и формат полей, поэтому код будет
# нуждаться в подкрутке после первого боевого прогона.
#
# Безопасность:
#   * Всегда логируем что бы отправили (с маскированной картой/CVV).
#   * Дефолтом dry_run_purchases=True в config.json — реальные init и
#     finalize пропускаются. Юзер должен явно переключить в False.
#   * CVV никогда не сохраняется на диск — только in-memory параметр.
#
# Известные purchaseresultdetail коды (по reverse-engineering, не
# эксклюзивный список):
PURCHASE_RESULT_DETAIL = {
    0: "Успех",
    1: "Карта отклонена",
    2: "Steam internal error",
    3: "Платёж отклонён банком",
    4: "Карта отклонена",
    5: "Лимит превышен / нужна верификация",
    6: "Невозможно списать",
    8: "AVS mismatch (адрес карты не совпал)",
    14: "CVV неверный",
    16: "3DS требуется",
    20: "Транзакция отменена пользователем",
    22: "Pending (нужен poll)",
    36: "Country/region недоступен для этого товара",
    53: "Дубликат транзакции",
    77: "Регион аккаунта другой / cart price = 0",
}


class SteamPurchaseResult:
    """Результат одной фазы pipeline'а (addtocart / init / finalize)."""

    def __init__(self, ok: bool, message: str,
                 transid: Optional[str] = None,
                 confirmation_url: Optional[str] = None,
                 raw: Optional[dict] = None):
        self.ok = ok
        self.message = message
        self.transid = transid
        self.confirmation_url = confirmation_url
        self.raw = raw or {}

    def __repr__(self) -> str:
        return (f"<SteamPurchaseResult ok={self.ok} "
                f"transid={self.transid!r} 3ds={bool(self.confirmation_url)} "
                f"msg={self.message!r}>")


class SteamPurchaseEngine:
    """Покупочный pipeline. Один engine на одну попытку покупки.

    Использование:
        eng = SteamPurchaseEngine(session, region="KZ", proxy="socks5://...",
                                   dry_run=True)
        subid = eng.get_subid_for_appid(2280)
        cart_gid = eng.add_to_cart(subid)
        r = eng.init_transaction(cart_gid, card_dict, cvv="123")
        if r.confirmation_url:
            # Открыть в браузере, подтвердить, потом:
            r = eng.finalize(r.transid)

    DRY_RUN: при dry_run=True ВСЕ методы, кроме get_subid и add_to_cart,
    будут только логировать и возвращать SteamPurchaseResult с маркером
    'DRY_RUN'. add_to_cart выполняется реально (cart-операции безопасны
    и нужны для получения cart_gid).
    """

    BASE = "https://store.steampowered.com"

    def __init__(self, session: requests.Session, region: str,
                 proxy: Optional[str] = None, dry_run: bool = True,
                 timeout: int = 30):
        self.session = session
        self.region = region.upper()
        self.proxy = proxy
        self.dry_run = dry_run
        self.timeout = timeout
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}
        _set_steam_region_cookie(self.session, self.region)
        # Steam ожидает referer/origin со store.steampowered.com.
        self.session.headers.update({
            "Origin": self.BASE,
            "Referer": self.BASE + "/",
        })

    def _ensure_sessionid(self) -> Optional[str]:
        """Steam требует cookie `sessionid` для всех POST'ов формы.
        Если его нет — делаем GET корня магазина, чтобы Steam его поставил.
        """
        sid = self.session.cookies.get(
            "sessionid", domain="store.steampowered.com")
        if sid:
            return sid
        try:
            self.session.get(self.BASE + "/", timeout=self.timeout)
        except Exception as exc:
            logger.warning("steam_ranger: ensure_sessionid GET failed: %s", exc)
        return self.session.cookies.get(
            "sessionid", domain="store.steampowered.com")

    def get_subid_for_appid(self, appid: int) -> Optional[int]:
        """Возвращает первый package id (subid) для appid через
        /api/appdetails/?filters=packages."""
        url = self.BASE + "/api/appdetails/"
        try:
            r = self.session.get(url, params={
                "appids": str(appid),
                "cc": self.region,
                "filters": "packages,price_overview",
            }, timeout=self.timeout)
        except Exception as exc:
            logger.warning("steam_ranger: appdetails failed: %s", exc)
            return None
        if r.status_code != 200:
            logger.warning("steam_ranger: appdetails status=%d", r.status_code)
            return None
        try:
            data = r.json()
        except Exception:
            logger.warning("steam_ranger: appdetails non-JSON: %r",
                           r.text[:200])
            return None
        appdata = (data.get(str(appid)) or {}).get("data") or {}
        # Path 1: packages — список subid'ов
        packages = appdata.get("packages") or []
        if packages:
            return int(packages[0])
        # Path 2: package_groups[0].subs[0].packageid
        groups = appdata.get("package_groups") or []
        if groups:
            subs = groups[0].get("subs") or []
            if subs and "packageid" in subs[0]:
                return int(subs[0]["packageid"])
        logger.warning("steam_ranger: no packages found in appdetails for %d",
                       appid)
        return None

    def add_to_cart(self, subid: int) -> Optional[str]:
        """Добавляет subid в корзину, возвращает shoppingCartGID или None."""
        sid = self._ensure_sessionid()
        if not sid:
            logger.error("steam_ranger: addtocart: no sessionid cookie")
            return None
        url = self.BASE + "/cart/addtocart/"
        try:
            r = self.session.post(url, data={
                "subid": str(subid),
                "sessionid": sid,
                "action": "add_to_cart",
                "originating_snr": "1_5_9__1",
            }, timeout=self.timeout, allow_redirects=True)
        except Exception as exc:
            logger.warning("steam_ranger: addtocart failed: %s", exc)
            return None
        if r.status_code not in (200, 302):
            logger.warning("steam_ranger: addtocart status=%d", r.status_code)
            return None
        gid = self.session.cookies.get(
            "shoppingCartGID", domain="store.steampowered.com")
        if not gid or gid == "-1":
            logger.warning("steam_ranger: addtocart: no shoppingCartGID cookie "
                           "(got %r) — Steam, возможно, отказал по региону или "
                           "anti-fraud", gid)
            return None
        return gid

    def init_transaction(self, gid_cart: str, card: dict,
                         cvv: str) -> SteamPurchaseResult:
        """POST /checkout/inittransaction/ с карточными данными и адресом."""
        sid = self._ensure_sessionid() or ""

        expiry_mm, expiry_yy = card["expiry"].split("/")
        full_year = ("20" + expiry_yy if len(expiry_yy) == 2 else expiry_yy)
        billing_country = (card.get("country_code") or self.region).upper()
        if not (len(billing_country) == 2 and billing_country.isalpha()):
            billing_country = self.region

        form = {
            "gidShoppingCart": gid_cart,
            "gidReplayOfTransID": "-1",
            "PaymentMethod": "creditcard",
            "abortPendingTransactions": "0",
            "bHasCardInfo": "1",
            "CardNumber": card["number"],
            "CardExpirationYear": full_year,
            "CardExpirationMonth": expiry_mm.zfill(2),
            "CardCVV2": cvv,
            "CardHolderName": card["name"],
            "BillingAddress1": card.get("street", ""),
            "BillingAddress2": "",
            "BillingCity": card.get("city", ""),
            "BillingState": "",
            "BillingPostalCode": card.get("zip", ""),
            "BillingCountry": billing_country,
            "ShippingAddress1": card.get("street", ""),
            "ShippingAddress2": "",
            "ShippingCity": card.get("city", ""),
            "ShippingState": "",
            "ShippingPostalCode": card.get("zip", ""),
            "ShippingCountry": billing_country,
            "bUseRememberedAddress": "0",
            "bIsGift": "0",
            "GifteeAccountID": "0",
            "GifteeEmail": "",
            "GifteeName": "",
            "GiftMessage": "",
            "Sentiment": "",
            "Signature": "",
            "ScheduledSendOnDate": "0",
            "Phone": card.get("phone", ""),
            "sessionid": sid,
        }
        # Маскируем чувствительное в логе:
        masked = dict(form)
        masked["CardNumber"] = _mask_pan(form["CardNumber"])
        masked["CardCVV2"] = "***"
        logger.info("steam_ranger: inittransaction (masked form): %s", masked)

        if self.dry_run:
            logger.warning(
                "steam_ranger: DRY_RUN — пропускаю реальный POST "
                "/checkout/inittransaction/. Чтобы запустить боевой режим, "
                "выстави dry_run_purchases=False в config.json.")
            return SteamPurchaseResult(
                ok=False,
                message="DRY_RUN: init не отправлен (см. config.json)",
                transid="DRY-RUN")

        url = self.BASE + "/checkout/inittransaction/"
        try:
            r = self.session.post(url, data=form, timeout=self.timeout)
        except Exception as exc:
            logger.exception("steam_ranger: inittransaction network error")
            return SteamPurchaseResult(False, f"inittransaction: {exc}")
        try:
            data = r.json()
        except Exception:
            return SteamPurchaseResult(
                False,
                f"inittransaction: не-JSON (status={r.status_code}, "
                f"body={r.text[:200]!r})")
        logger.info("steam_ranger: inittransaction response: %s", data)

        success = data.get("success", 0)
        purchase_detail = data.get("purchaseresultdetail")
        transid = data.get("transid") or data.get("transactionid")
        confirm_url = data.get("confirmationUrl") or data.get("authorizationURL")

        if confirm_url:
            return SteamPurchaseResult(
                ok=False,
                message="3DS требуется — открой URL в браузере",
                transid=transid,
                confirmation_url=confirm_url,
                raw=data)
        if success == 1:
            return SteamPurchaseResult(
                True, "init OK", transid=transid, raw=data)

        msg = (
            PURCHASE_RESULT_DETAIL.get(purchase_detail)
            or data.get("error")
            or f"success={success}, purchaseresultdetail={purchase_detail}"
        )
        return SteamPurchaseResult(False, f"init отказ: {msg}", raw=data)

    def finalize(self, transid: str) -> SteamPurchaseResult:
        """POST /checkout/finalizetransaction/."""
        if self.dry_run:
            logger.warning(
                "steam_ranger: DRY_RUN — пропускаю /checkout/finalizetransaction/ "
                "для transid=%s", transid)
            return SteamPurchaseResult(
                False, "DRY_RUN: finalize пропущен", transid=transid)
        sid = self._ensure_sessionid() or ""
        url = self.BASE + "/checkout/finalizetransaction/"
        browser_info = json.dumps({
            "language": "en-US",
            "javaEnabled": "false",
            "colorDepth": 24,
            "screenHeight": 1080,
            "screenWidth": 1920,
            "timeZone": -0,
            "userAgent": STEAM_USER_AGENT,
        })
        try:
            r = self.session.post(url, data={
                "transid": transid,
                "sessionid": sid,
                "browserInfo": browser_info,
            }, timeout=self.timeout)
        except Exception as exc:
            logger.exception("steam_ranger: finalize network error")
            return SteamPurchaseResult(False, f"finalize: {exc}")
        try:
            data = r.json()
        except Exception:
            return SteamPurchaseResult(
                False,
                f"finalize: не-JSON (status={r.status_code}, "
                f"body={r.text[:200]!r})")
        logger.info("steam_ranger: finalize response: %s", data)
        success = data.get("success", 0)
        if success == 1:
            return SteamPurchaseResult(
                True, "finalize OK", transid=transid, raw=data)
        if success == 22:
            return SteamPurchaseResult(
                False, "finalize pending (нужен polling)",
                transid=transid, raw=data)
        msg = (PURCHASE_RESULT_DETAIL.get(data.get("purchaseresultdetail"))
               or f"success={success}")
        return SteamPurchaseResult(False, f"finalize отказ: {msg}", raw=data)

    def transaction_status(self, transid: str) -> SteamPurchaseResult:
        url = self.BASE + "/checkout/transactionstatus/"
        try:
            r = self.session.post(url, data={"transid": transid, "count": 1},
                                   timeout=self.timeout)
        except Exception as exc:
            return SteamPurchaseResult(False, f"status: {exc}")
        try:
            return SteamPurchaseResult(
                True, "status",
                transid=transid, raw=r.json())
        except Exception:
            return SteamPurchaseResult(False, "status: не-JSON")


def _validate_cvv(s: str, brand_hint: str = "") -> Optional[str]:
    """3 цифры (Visa/MC/Discover/JCB) или 4 (Amex)."""
    s = (s or "").strip()
    if not s.isdigit():
        return None
    if brand_hint == "Amex":
        return s if len(s) == 4 else None
    return s if len(s) in (3, 4) else None


# ============================================================================
# Manual games (Phase 8) — ручной список игр для покупки, per-region
# ============================================================================
# Schema (Fernet, JSON):
#   {
#       "id": "<hex>",
#       "appid": 2280,
#       "region": "KZ",
#       "name": "Half-Life 2" | None,
#       "price_minor": 9999 | None,
#       "currency": "USD" | None,
#       "last_price_check": <epoch> | None,
#       "added_at": <epoch>,
#   }
#
# Идея: пользователь добавляет appid'ы, которые он сам нашёл/проверил
# для нужных регионов. Если для current region есть хоть одна ручная
# игра — _run_one_cycle_sync берёт её (cheapest first) вместо автопоиска.
# Это надёжнее, потому что Steam-search regex может сломаться, и
# пользователь сам контролирует, какие игры покупаются.
def _load_manual_games() -> list[dict]:
    if _master_key is None:
        return []
    if not os.path.exists(MANUAL_GAMES_PATH):
        return []
    try:
        with open(MANUAL_GAMES_PATH, "rb") as f:
            blob = f.read()
    except Exception:
        logger.exception("steam_ranger: read manual_games.enc failed")
        return []
    data = _decrypt_json(_master_key, blob)
    if not isinstance(data, list):
        logger.error("steam_ranger: manual_games.enc decrypted to %s, "
                     "expected list", type(data).__name__)
        return []
    return data


def _save_manual_games(games: list[dict]) -> None:
    if _master_key is None:
        raise RuntimeError("plugin is locked, refusing to save manual_games")
    _ensure_dir()
    blob = _encrypt_json(_master_key, games)
    tmp = MANUAL_GAMES_PATH + ".tmp"
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, MANUAL_GAMES_PATH)


def _new_manual_game_id() -> str:
    return secrets.token_hex(4)


def _validate_appid(s: str) -> Optional[int]:
    """Принимает либо число (`2280`), либо Steam URL вида
    `https://store.steampowered.com/app/2280/...`. Возвращает int или None.
    """
    s = (s or "").strip()
    if not s:
        return None
    # Plain integer
    if s.isdigit():
        n = int(s)
        return n if 1 <= n <= 99_999_999 else None
    # URL form
    m = re.search(r"store\.steampowered\.com/app/(\d+)", s)
    if m:
        n = int(m.group(1))
        return n if 1 <= n <= 99_999_999 else None
    return None


def _fetch_app_info(appid: int, region: str,
                    proxy: Optional[str] = None,
                    timeout: int = 20) -> Optional[dict]:
    """Возвращает {name, price_minor, currency} или None если игра не
    отдалась (sold out, недоступна в регионе, и т.д.).

    `price_minor=0` — игра бесплатная или не имеет публичной цены
    (например, requires-bundle). Для покупки такая не подойдёт.
    """
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": STEAM_USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
    })
    if proxy:
        sess.proxies = {"http": proxy, "https": proxy}
    try:
        r = sess.get(APPDETAILS_URL, params={
            "appids": str(appid),
            "cc": region,
            "filters": "basic,price_overview",
        }, timeout=timeout)
    except Exception as exc:
        logger.warning("steam_ranger: appdetails(%d, %s) network: %s",
                       appid, region, exc)
        return None
    if r.status_code != 200:
        logger.warning("steam_ranger: appdetails(%d, %s) status=%d",
                       appid, region, r.status_code)
        return None
    try:
        data = r.json()
    except Exception:
        logger.warning("steam_ranger: appdetails(%d, %s) non-JSON",
                       appid, region)
        return None
    entry = data.get(str(appid)) or {}
    if not entry.get("success"):
        # Игра не доступна в этом регионе (или вообще снята с продажи)
        return None
    appdata = entry.get("data") or {}
    name = appdata.get("name", f"appid:{appid}")
    price = appdata.get("price_overview")
    if not price:
        # Бесплатные/без цены — для покупки не годятся, но возвращаем
        # с price_minor=0, чтобы UI мог это показать и пользователь решил.
        return {"name": name, "price_minor": 0, "currency": None}
    return {
        "name": name,
        "price_minor": int(price.get("final", 0)),
        "currency": price.get("currency"),
    }


def _refresh_manual_game_prices(proxy: Optional[str] = None
                                 ) -> tuple[int, int]:
    """Обновляет name/price_minor/last_price_check у всех ручных игр.
    Возвращает (updated, failed). Безопасна на пустом списке.
    """
    games = _load_manual_games()
    if not games:
        return 0, 0
    updated = 0
    failed = 0
    for g in games:
        info = _fetch_app_info(g["appid"], g["region"], proxy=proxy)
        if info is None:
            failed += 1
            g["last_price_check"] = int(time.time())
            continue
        g["name"] = info["name"]
        g["price_minor"] = info["price_minor"]
        g["currency"] = info["currency"]
        g["last_price_check"] = int(time.time())
        updated += 1
    _save_manual_games(games)
    return updated, failed


def _pick_manual_game_for_region(region: str) -> Optional[dict]:
    """Самая дешёвая (по price_minor) ручная игра для региона.
    Игнорирует записи с price_minor=0 (бесплатные/без цены).
    """
    games = _load_manual_games()
    candidates = [
        g for g in games
        if g.get("region") == region
        and (g.get("price_minor") or 0) > 0
    ]
    if not candidates:
        # Если все цены ещё не подгружены (None), берём первую с
        # известной region — пусть pipeline сам грохнется или подтянет
        # цену через get_subid_for_appid.
        unpriced = [g for g in games if g.get("region") == region]
        if unpriced:
            return {
                "appid": unpriced[0]["appid"],
                "name": unpriced[0].get("name") or f"appid:{unpriced[0]['appid']}",
                "price_minor": unpriced[0].get("price_minor") or 0,
                "cc": region,
            }
        return None
    candidates.sort(key=lambda x: x.get("price_minor") or 10**9)
    pick = candidates[0]
    return {
        "appid": pick["appid"],
        "name": pick.get("name") or f"appid:{pick['appid']}",
        "price_minor": pick.get("price_minor") or 0,
        "cc": region,
    }


# ============================================================================
# Account region detection (Phase 7)
# ============================================================================
# Используется автоциклом для условия остановки: если регион аккаунта
# Steam совпал с целевым → стоп. Лучшее, что можно сделать без API-ключа —
# parse store.steampowered.com/account/?l=english + cookie steamCountry.
_ACCOUNT_COUNTRY_RE = re.compile(
    r'<a[^>]*href="[^"]*setcountry[^"]*"[^>]*>([^<]+)</a>',
    re.IGNORECASE,
)
# Минимальная map для регионов из REGIONS списка. Steam пишет
# страны на английском (даже если ?l=english не сработает на всех
# страницах, для /account/ — надёжно).
_COUNTRY_NAME_TO_CC = {
    "Kazakhstan": "KZ",
    "Ukraine": "UA",
    "Turkey": "TR",
    "Türkiye": "TR",
    "Argentina": "AR",
    "Brazil": "BR",
    "India": "IN",
    "Russian Federation": "RU",
    "Russia": "RU",
    "United States": "US",
    "Poland": "PL",
    "China": "CN",
}


# ============================================================================
# v1.8.0 — чистое ядро (preflight + смена региона) + shell
# ============================================================================
PF_PASS, PF_FAIL, PF_SKIP, PF_INFO = "pass", "fail", "skip", "info"


def _build_preflight_report(facts: dict) -> dict:
    """Чистая функция: из собранных фактов строит {items, blockers}."""
    items: list[dict] = []
    blockers: list[str] = []

    def add(key, status, detail):
        items.append({"key": key, "status": status, "detail": detail})
        if status == PF_FAIL:
            blockers.append(detail)

    target = facts.get("target_region", "?")
    unlocked = bool(facts.get("unlocked"))
    add("unlocked", PF_PASS if unlocked else PF_FAIL,
        "Плагин разблокирован" if unlocked else "🔒 Плагин заблокирован (нужен /sranger_unlock)")

    if not unlocked:
        for k, label in (("steam_session", "Steam-сессия"),
                         ("proxy", "Прокси под регион"),
                         ("main_card", "Основная карта"),
                         ("candidate_game", "Игра-кандидат")):
            add(k, PF_SKIP, f"{label}: пропущено (плагин заблокирован)")
        return {"items": items, "blockers": blockers}

    logged = bool(facts.get("steam_logged_in"))
    add("steam_session", PF_PASS if logged else PF_FAIL,
        "Steam-сессия активна" if logged else "Нет активной Steam-сессии (войди в Steam)")

    if facts.get("proxy_match"):
        add("proxy", PF_PASS, f"Есть живой прокси под регион {target}")
    elif facts.get("proxy_fallback"):
        add("proxy", PF_INFO, f"Нет прокси под {target} — только запасной (страна не совпадёт)")
    else:
        add("proxy", PF_FAIL, f"Нет живого прокси под регион {target}")

    add("main_card", PF_PASS if facts.get("has_main_card") else PF_FAIL,
        "Основная карта задана" if facts.get("has_main_card") else "Нет основной карты")

    acc_region = facts.get("account_region")
    if acc_region and acc_region == target:
        add("account_region", PF_INFO, f"Регион аккаунта уже {acc_region} — смена не нужна")
    elif acc_region:
        add("account_region", PF_INFO, f"Текущий регион аккаунта: {acc_region} → цель {target}")
    else:
        add("account_region", PF_INFO, "Регион аккаунта не определён")

    cand = facts.get("candidate")
    if cand:
        src = facts.get("candidate_source") or "?"
        add("candidate_game", PF_PASS,
            f"Игра-кандидат: {cand.get('name', '?')} ({cand.get('price_str', '?')}) [{src}]")
    else:
        add("candidate_game", PF_FAIL, f"Не удалось определить игру-кандидата для {target}")

    return {"items": items, "blockers": blockers}


def _region_change_decision(prev: str, cur: str, notify_enabled: bool, target: str) -> dict:
    """Чистая функция: prev='' = ещё не зафиксировано, cur — определённый (непустой)."""
    changed = bool(cur) and cur != prev
    return {
        "changed": changed,
        "persist": cur,
        "notify": changed and bool(notify_enabled) and prev != "",
        "target_reached": bool(cur) and cur == target,
    }


def _gather_preflight_facts() -> dict:
    """Read-only сбор фактов. Никогда не создаёт SteamPurchaseEngine и не трогает корзину."""
    cfg = _load_config()
    target = cfg.get("region", "KZ")
    facts: dict[str, Any] = {
        "target_region": target,
        "unlocked": _is_unlocked(),
        "steam_logged_in": _is_steam_logged_in(),
        "proxy_match": False, "proxy_fallback": False,
        "has_main_card": False, "account_region": None,
        "candidate": None, "candidate_source": None,
    }
    if not facts["unlocked"]:
        return facts
    try:
        pool = _load_proxies()
        alive = [p for p in pool if p.get("alive")]
        facts["proxy_match"] = any(p.get("country_code") == target for p in alive)
        facts["proxy_fallback"] = bool(alive) and not facts["proxy_match"]
    except Exception:
        logger.debug("preflight: proxy check failed", exc_info=True)
    try:
        facts["has_main_card"] = any(c.get("is_main") for c in _load_cards())
    except Exception:
        logger.debug("preflight: card check failed", exc_info=True)
    try:
        facts["account_region"] = _detect_account_region()
    except Exception:
        logger.debug("preflight: account region failed", exc_info=True)
    try:
        proxy = _pick_proxy_for_region(target)
        cand = None
        src = None
        if cfg.get("prefer_manual_list", True):
            cand = _pick_manual_game_for_region(target)
            if cand:
                src = "manual"
        if cand is None:
            games = _search_cheap_games(target, proxy=proxy, max_results=1)
            if games:
                cand = games[0]
                src = "search"
        if cand:
            price_minor = cand.get("price_minor")
            price_str = (cand.get("price_str")
                         or (f"{price_minor}" if price_minor is not None else "?"))
            facts["candidate"] = {"name": cand.get("name", "?"), "price_str": price_str}
            facts["candidate_source"] = src
    except Exception:
        logger.debug("preflight: candidate resolution failed", exc_info=True)
    return facts


def _resolve_operator_chat(cardinal: "Cardinal", cfg: dict):
    cid = cfg.get("operator_chat_id")
    if cid:
        return cid
    tg = getattr(cardinal, "telegram", None)
    users = getattr(tg, "authorized_users", None) if tg else None
    if users:
        try:
            return list(users)[0]
        except Exception:
            return None
    return None


def _notify_region_change(cardinal: "Cardinal", prev: str, cur: str,
                          target_reached: bool) -> None:
    cfg = _load_config()
    tg = getattr(cardinal, "telegram", None)
    bot = getattr(tg, "bot", None) if tg else None
    if bot is None:
        return
    chat_id = _resolve_operator_chat(cardinal, cfg)
    if not chat_id:
        return
    prev_lbl = prev or "—"
    text = (f"🌍 <b>Steam Region Ranger</b>: регион аккаунта изменился\n"
            f"<b>{prev_lbl} → {cur}</b>")
    if target_reached:
        text += "\n🎯 целевой регион достигнут"
    try:
        bot.send_message(chat_id, text, parse_mode="HTML")
    except Exception as ex:
        logger.error("steam_ranger: region-change notify failed: %s", ex)


def _observe_account_region(cardinal: "Cardinal", *, cur: "str | None" = None) -> None:
    """Единый путь детекта+уведомления (периодический чекер и автоцикл)."""
    cfg = _load_config()
    if cur is None:
        cur = _detect_account_region()
    if not cur:
        return
    prev = cfg.get("last_known_region", "")
    d = _region_change_decision(prev, cur, bool(cfg.get("region_change_notify", True)),
                                cfg.get("region", "KZ"))
    if prev != d["persist"]:
        cfg["last_known_region"] = d["persist"]
        _save_config(cfg)
    if d["notify"]:
        _notify_region_change(cardinal, prev, cur, d["target_reached"])


_region_watch_stop = threading.Event()
_region_watch_thread: "threading.Thread | None" = None


def _region_watch_loop(cardinal: "Cardinal") -> None:
    logger.info("steam_ranger: region-watch loop started")
    while not _region_watch_stop.is_set():
        try:
            cfg = _load_config()
            interval = int(cfg.get("region_check_interval_sec", 0) or 0)
            if interval > 0 and _is_unlocked() and _is_steam_logged_in():
                _observe_account_region(cardinal)
        except Exception:
            logger.debug("steam_ranger: region-watch tick failed", exc_info=True)
        wait = max(int(_load_config().get("region_check_interval_sec", 0) or 0), 60)
        _region_watch_stop.wait(wait)


def _detect_account_region(session: Any = None) -> Optional[str]:
    """Возвращает CC региона аккаунта Steam или None.
    1) Сначала пробует cookie `steamCountry` (быстро, но мы её сами выставляем).
    2) Если cookie противоречивая — GET /account/?l=english и regex.

    ``session`` — requests.Session (по умолчанию глобальная операторская
    `_steam_session`); для покупательских заказов передаётся сессия покупателя.
    """
    sess = session if session is not None else _steam_session
    if sess is None:
        return None
    # Cookie path
    sc = sess.cookies.get(
        "steamCountry", domain="store.steampowered.com")
    cookie_cc: Optional[str] = None
    if sc:
        sc = sc.replace("%7C", "|")
        cc = sc.split("|", 1)[0].upper()
        if len(cc) == 2 and cc.isalpha():
            cookie_cc = cc
    # /account/ path — авторитетный, но дороже
    try:
        r = sess.get(
            "https://store.steampowered.com/account/?l=english",
            timeout=15, allow_redirects=False)
        if r.status_code == 200:
            mt = _ACCOUNT_COUNTRY_RE.search(r.text)
            if mt:
                country = mt.group(1).strip()
                cc = _COUNTRY_NAME_TO_CC.get(country)
                if cc:
                    return cc
                logger.debug("steam_ranger: account country %r не в "
                             "_COUNTRY_NAME_TO_CC; cookie fallback=%s",
                             country, cookie_cc)
    except Exception:
        logger.debug("steam_ranger: GET /account/ failed", exc_info=True)
    return cookie_cc


# ============================================================================
# Sync purchase pipeline (Phase 7 reuses, Phase 6 has its own async UI version)
# ============================================================================
def _purchase_with_session(session: Any, region: str, cvv: str, card: dict, *,
                           dry_run: bool, prefer_manual: bool,
                           proxy: Optional[str] = None
                           ) -> tuple[bool, str, dict, Optional[str], Any, Optional[str]]:
    """Покупка самой дешёвой игры на переданной requests.Session (магазин региона).

    Возвращает (ok, message, game, confirmation_url, engine, transid).
    confirmation_url != None → требуется 3DS (finalize по engine/transid делает
    вызывающий после подтверждения). Используется и операторским автоциклом
    (на `_steam_session`), и покупательским заказом (на сессии покупателя).
    """
    if session is None:
        return False, "не залогинен в Steam", {}, None, None, None
    if not card:
        return False, "нет карты для оплаты", {}, None, None, None
    if proxy is None:
        proxy = _pick_proxy_for_region(region)

    game: Optional[dict] = None
    if prefer_manual:
        manual_pick = _pick_manual_game_for_region(region)
        if manual_pick is not None:
            game = manual_pick
            logger.info("steam_ranger: ручная игра для %s — %s (appid=%s)",
                        region, game["name"], game["appid"])
    if game is None:
        games = _search_cheap_games(region, proxy=proxy, max_results=3)
        if not games:
            return False, "search returned [] и список ручных игр пуст", {}, None, None, None
        game = games[0]

    engine = SteamPurchaseEngine(session, region, proxy=proxy, dry_run=dry_run)
    subid = engine.get_subid_for_appid(game["appid"])
    if subid is None:
        _log_purchase(game, card, region, dry_run, False, "no subid")
        return False, "no subid (appdetails не отдал packages)", game, None, engine, None
    cart_gid = engine.add_to_cart(subid)
    if not cart_gid:
        _log_purchase(game, card, region, dry_run, False, "addtocart returned None")
        return False, "addtocart returned None (Steam отказал)", game, None, engine, None

    init = engine.init_transaction(cart_gid, card, cvv)
    if init.confirmation_url:
        return False, "3DS требуется", game, init.confirmation_url, engine, init.transid
    if not init.ok:
        _log_purchase(game, card, region, dry_run, False, init.message)
        return False, init.message, game, None, engine, init.transid

    final = engine.finalize(init.transid)
    _log_purchase(game, card, region, dry_run, final.ok, final.message,
                  transid=init.transid)
    return final.ok, final.message, game, None, engine, init.transid


def _run_one_cycle_sync(cvv: str, region: str) -> tuple[bool, str, dict]:
    """Один проход pipeline без TG-обновлений. Возвращает (ok, message, game).

    Используется автоциклом. 3DS в автоцикле НЕ обрабатывается — считается
    неудачей. Тонкая обёртка над `_purchase_with_session` (операторская сессия,
    основная карта).
    """
    cards = _load_cards()
    card = next((c for c in cards if c.get("is_main")), None)
    if not card:
        return False, "нет основной карты", {}
    cfg = _load_config()
    dry_run = bool(cfg.get("dry_run_purchases", True))
    ok, msg, game, conf_url, _engine, _transid = _purchase_with_session(
        _steam_session, region, cvv, card,
        dry_run=dry_run, prefer_manual=bool(cfg.get("prefer_manual_list", True)))
    if conf_url:
        _log_purchase(game, card, region, dry_run, False,
                      "3DS required (autocycle can't handle)")
        return False, "3DS требуется (автоцикл не умеет, ручной режим)", game
    return ok, msg, game


# ============================================================================
# Validators (cards already use them; proxies/login above)
# ============================================================================
def _validate_pan(s: str) -> Optional[str]:
    """Возвращает нормализованный PAN (только цифры) или None при ошибке."""
    digits = "".join(ch for ch in (s or "") if ch.isdigit())
    if not (13 <= len(digits) <= 19):
        return None
    return digits


def _validate_expiry(s: str) -> Optional[str]:
    """`MM/YY` или `MM/YYYY`, нормализуется в `MM/YY`."""
    s = (s or "").strip().replace(" ", "")
    if "/" not in s:
        return None
    mm, yy = s.split("/", 1)
    if not (mm.isdigit() and yy.isdigit()):
        return None
    if not (1 <= int(mm) <= 12):
        return None
    if len(yy) == 4:
        yy = yy[-2:]
    if len(yy) != 2:
        return None
    return f"{int(mm):02d}/{yy}"


def _validate_phone(s: str) -> Optional[str]:
    s = (s or "").strip().replace(" ", "").replace("-", "")
    if not s.startswith("+"):
        return None
    digits = s[1:]
    if not digits.isdigit():
        return None
    if not (7 <= len(digits) <= 15):
        return None
    return "+" + digits


def _validate_nonempty(s: str, max_len: int = 100) -> Optional[str]:
    s = (s or "").strip()
    if not s:
        return None
    if len(s) > max_len:
        return None
    return s


# ============================================================================
# Plugin init
# ============================================================================
def _init(cardinal: "Cardinal", *_: Any) -> None:
    _ensure_dir()
    cfg = _load_config()
    # Phase 7: при рестарте Cardinal CVV в RAM пропадает, поэтому running
    # после рестарта НЕ авто-возобновляется. Сбрасываем флаг.
    if cfg.get("running"):
        logger.warning(
            "steam_ranger: cfg.running=true on init — CVV в RAM "
            "пропал на рестарте, сбрасываю running в false. Запусти "
            "автоцикл заново через 🟢 Start.")
        cfg["running"] = False
    _save_config(cfg)
    meta = _load_meta()
    _save_meta(meta)

    # v1.8.0: фоновый чекер смены региона аккаунта
    global _region_watch_thread
    _region_watch_stop.clear()
    if _region_watch_thread is None or not _region_watch_thread.is_alive():
        _region_watch_thread = threading.Thread(
            target=_region_watch_loop, args=(cardinal,), daemon=True,
            name="srr-region-watch")
        _region_watch_thread.start()

    tg = getattr(cardinal, "telegram", None)
    if tg is None:
        logger.warning("steam_ranger: Telegram отключён, плагин запустится "
                       "без UI. Управление будет недоступно до включения TG.")
        return

    bot = tg.bot

    # ---------- helpers ----------
    def _cb(prefix: str):
        return lambda c: c.data == prefix or c.data.startswith(prefix + ":")

    def _render(c: CallbackQuery, text: str, kb: K) -> None:
        try:
            bot.edit_message_text(
                text, c.message.chat.id, c.message.id,
                reply_markup=kb, parse_mode="HTML")
        except Exception:
            bot.send_message(
                c.message.chat.id, text,
                reply_markup=kb, parse_mode="HTML")

    def _set_state(chat_id: int, msg_id: int, user_id: int, state: str,
                   data: Optional[dict] = None) -> None:
        try:
            tg.set_state(chat_id, msg_id, user_id, state, data or {})
        except TypeError:
            tg.set_state(chat_id, msg_id, user_id, state)

    def _clear_state(m: Message) -> None:
        try:
            tg.clear_state(m.chat.id, m.from_user.id, True)
        except TypeError:
            try:
                tg.clear_state(m.chat.id, m.from_user.id)
            except Exception:
                pass
        except Exception:
            pass

    def _state_eq(m: Message, state: str) -> bool:
        try:
            st = tg.get_state(m.chat.id, m.from_user.id)
        except Exception:
            return False
        return bool(st and st.get("state") == state)

    def _ack(c: CallbackQuery, text: str = "") -> None:
        try:
            bot.answer_callback_query(c.id, text=text or None,
                                      show_alert=bool(text))
        except Exception:
            pass

    def _stub(phase: int, what: str) -> str:
        return (
            f"🚧 <b>{what}</b>\n\n"
            f"Реальная реализация — в Фазе {phase}.\n"
            f"См. <code>extracted/plan.md</code>."
        )

    def _kb_back() -> K:
        kb = K()
        kb.add(B("◀️ Назад в меню", callback_data=CBT_OPEN))
        return kb

    # ---------- main menu ----------
    def _status_short() -> dict[str, str]:
        cfg2 = _load_config()
        cards_n = _cards_count()
        prx_total, prx_alive = _proxies_count()
        return {
            "running": "🟢 запущен" if cfg2["running"] else "🔴 остановлен",
            "region": REGION_LABELS.get(cfg2["region"], cfg2["region"]),
            "region_code": cfg2["region"],
            "steam": (
                f"✅ {_steam_login_name}"
                if _is_steam_logged_in() and _steam_login_name
                else "❌ не залогинен"
            ),
            "cards": f"{cards_n}",
            "proxies": f"{prx_total} (живых: {prx_alive})",
            "lock": "🔒 заблокирован" if not _is_unlocked() else "🔓 разблокирован",
        }

    def _main_text() -> str:
        s = _status_short()
        cfg2 = _load_config()
        last_err = cfg2.get("last_error") or "—"
        # дешёвая подсказка готовности (без сетевых проверок)
        hints = []
        if not _is_unlocked():
            hints.append("• разблокируйте плагин (/sranger_unlock)")
        else:
            if not _is_steam_logged_in():
                hints.append("• войдите в Steam (🔑 Войти)")
            cards = _load_cards()
            if len(cards) <= 0:
                hints.append("• добавьте карту (💳 Карты)")
            elif not any(c.get("is_main") for c in cards):
                hints.append("• отметьте основную карту (💳 Карты)")
            if _proxies_count()[1] <= 0:
                hints.append("• добавьте живой прокси (🌐 Прокси)")
            if not cfg2.get("running"):
                hints.append("• нажмите 🟢 Start")
        hint_block = ("\n\n<b>⚠️ Чтобы заработало:</b>\n" + "\n".join(hints)) if hints \
            else "\n\n✅ Готово к запуску."
        return (
            f"<b>🎮 Steam Region Ranger</b>\n"
            f"<i>v{VERSION}</i>\n\n"
            f"Статус: <b>{s['running']}</b>\n"
            f"Доступ: <b>{s['lock']}</b>\n"
            f"Регион (целевой): <b>{s['region']}</b>\n"
            f"Steam: <b>{s['steam']}</b>\n"
            f"Карт: <b>{s['cards']}</b>\n"
            f"Прокси: <b>{s['proxies']}</b>\n"
            f"Попыток: <b>{cfg2.get('attempts', 0)}</b> · "
            f"успехов: <b>{cfg2.get('successes', 0)}</b>\n"
            f"Последняя ошибка: <i>{last_err}</i>"
            f"{hint_block}"
        )

    def _kb_main() -> K:
        cfg2 = _load_config()
        kb = K()

        if not _is_unlocked():
            kb.add(B("🔓 Разблокировать", callback_data=CBT_UNLOCK))
            return kb

        run_lbl = "🔴 Stop" if cfg2["running"] else "🟢 Start"
        kb.add(B(run_lbl, callback_data=CBT_TOGGLE_RUN))
        kb.add(B(f"🌍 Регион: {cfg2['region']}",
                 callback_data=CBT_REGION))
        if _is_steam_logged_in():
            kb.add(B("👤 Выход из Steam", callback_data=CBT_LOGOUT))
        else:
            kb.add(B("🔑 Войти в Steam", callback_data=CBT_LOGIN))
        kb.row(
            B(f"💳 Карты ({_cards_count()})",
              callback_data=CBT_CARDS),
            B(f"🌐 Прокси ({_proxies_count()[0]})",
              callback_data=CBT_PROXY),
        )
        # Phase 8: ручной список игр для текущего региона.
        manual_n = sum(
            1 for g in _load_manual_games()
            if g.get("region") == cfg2.get("region"))
        kb.add(B(f"📋 Свой список игр для {cfg2.get('region', '?')} "
                 f"({manual_n})",
                 callback_data=CBT_MANUAL_GAMES))
        kb.add(B("🔄 Разовая покупка",
                 callback_data=CBT_PURCHASE_ONCE))
        kb.row(
            B("⚙️ Ещё", callback_data=CBT_MORE),
            B("❓ Как настроить", callback_data=CBT_GUIDE),
        )
        kb.row(B("💛 Донат", callback_data=f"{DONATION_CALLBACK_PREFIX}:donate"))
        return kb

    def _text_more() -> str:
        cfg2 = _load_config()
        iv = int(cfg2.get("region_check_interval_sec", 0) or 0)
        iv_lbl = f"{iv // 60} мин" if iv > 0 else "выкл"
        dry = bool(cfg2.get("dry_run_purchases", True))
        return (
            "<b>⚙️ Ещё — дополнительно</b>\n\n"
            f"🧪 Режим покупок: <b>"
            f"{'Dry-run (без реальных покупок)' if dry else 'БОЕВОЙ'}</b>\n"
            f"🔔 Уведомления о смене региона: "
            f"<b>{_onoff(cfg2.get('region_change_notify', True))}</b>\n"
            f"⏱ Интервал проверки региона: <b>{iv_lbl}</b>\n"
            f"🛒 Покупательский поток (FunPay): "
            f"<b>{_onoff(cfg2.get('buyer_flow_enabled'))}</b>"
        )

    def _kb_more() -> K:
        cfg2 = _load_config()
        kb = K()
        dry = bool(cfg2.get("dry_run_purchases", True))
        dry_lbl = ("🧪 Dry-run покупок: ВКЛ"
                   if dry else "🔥 БОЕВОЙ режим покупок: ВКЛ")
        kb.add(B(dry_lbl, callback_data=CBT_DRY_RUN_TOGGLE))
        kb.add(B("🧪 Сухой прогон (проверка готовности)",
                 callback_data=CBT_PREFLIGHT))
        iv = int(cfg2.get("region_check_interval_sec", 0) or 0)
        iv_lbl = f"{iv // 60} мин" if iv > 0 else "выкл"
        rnt = _onoff(cfg2.get("region_change_notify", True))
        kb.row(
            B(f"🔔 Уведом. о смене региона: {rnt}",
              callback_data=CBT_TOGGLE_REGION_NOTIFY),
            B(f"⏱ Интервал: {iv_lbl}", callback_data=CBT_EDIT_REGION_INTERVAL),
        )
        kb.add(B("📊 Статус", callback_data=CBT_STATUS))
        bf = _onoff(cfg2.get("buyer_flow_enabled"))
        kb.add(B(f"🛒 Покупательский поток (FunPay): {bf}",
                 callback_data=CBT_BUYER_MENU))
        kb.add(B("◀️ Назад", callback_data=CBT_OPEN))
        return kb

    def open_main(c: CallbackQuery) -> None:
        try:
            cfg_cap = _load_config()
            if cfg_cap.get("operator_chat_id") != c.message.chat.id:
                cfg_cap["operator_chat_id"] = c.message.chat.id
                _save_config(cfg_cap)
        except Exception:
            pass
        _render(c, _main_text(), _kb_main())
        _ack(c)

    def open_more(c: CallbackQuery) -> None:
        _render(c, _text_more(), _kb_more())
        _ack(c)

    def open_guide(c: CallbackQuery) -> None:
        kb = K()
        kb.add(B("◀️ Назад", callback_data=CBT_OPEN))
        _render(c, _GUIDE_TEXT, kb)
        _ack(c)

    # ---------- v1.8.0: preflight / region-notify ----------
    def preflight_handler(c: CallbackQuery) -> None:
        _ack(c, "Проверяю готовность…")
        chat_id = c.message.chat.id

        def _worker():
            facts = _gather_preflight_facts()
            report = _build_preflight_report(facts)
            marks = {PF_PASS: "✅", PF_FAIL: "❌", PF_SKIP: "⏭", PF_INFO: "ℹ️"}
            lines = ["<b>🧪 Сухой прогон — готовность к смене региона</b>\n"]
            for it in report["items"]:
                lines.append(f"{marks.get(it['status'], '•')} {it['detail']}")
            if report["blockers"]:
                lines.append("\n<b>⛔ Блокеры:</b>")
                for b in report["blockers"]:
                    lines.append(f"  • {b}")
            else:
                lines.append("\n✅ Блокеров нет — можно запускать.")
            lines.append("\n<i>Это read-only проверка: реальная покупка не выполнялась.</i>")
            try:
                bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML")
            except Exception:
                logger.exception("steam_ranger: preflight send failed")

        threading.Thread(target=_worker, daemon=True,
                         name="srr-preflight").start()

    def toggle_region_notify(c: CallbackQuery) -> None:
        cfg2 = _load_config()
        cfg2["region_change_notify"] = not cfg2.get("region_change_notify", True)
        _save_config(cfg2)
        open_more(c)

    def ask_region_interval(c: CallbackQuery) -> None:
        m = bot.send_message(
            c.message.chat.id,
            "Введи интервал проверки региона в <b>минутах</b> "
            "(0 = выключить периодическую проверку):",
            parse_mode="HTML")
        _set_state(m.chat.id, m.id, c.from_user.id, ST_ASK_REGION_INTERVAL)
        _ack(c)

    def on_region_interval(m: Message) -> None:
        _clear_state(m)
        try:
            minutes = int((m.text or "").strip())
            if minutes < 0:
                raise ValueError
        except ValueError:
            bot.send_message(m.chat.id, "❌ Нужно целое число минут ≥ 0.")
            return
        cfg2 = _load_config()
        cfg2["region_check_interval_sec"] = minutes * 60
        _save_config(cfg2)
        bot.send_message(
            m.chat.id,
            f"✅ Интервал проверки региона: <b>{minutes} мин</b>"
            + (" (выключено)" if minutes == 0 else ""),
            parse_mode="HTML")

    # ---------- toggle running (Phase 7: реальный старт/стоп автоцикла) ----------
    def _autocycle_loop(chat_id: int, user_id: int) -> None:
        """Тред автоцикла. Останавливается:
          • если совпал регион аккаунта → success-stop;
          • после AUTOCYCLE_MAX_FAILURES неудач подряд → failure-stop;
          • при clear/set _autocycle_stop (кнопкой Stop или _on_delete);
          • если CVV пропал из RAM (не должно случаться, но на всякий).
        """
        global _autocycle_cvv, _autocycle_thread
        consec_failures = 0
        try:
            while not _autocycle_stop.is_set():
                cfg2 = _load_config()
                if not cfg2.get("running"):
                    logger.info("autocycle: running=false in config, exiting")
                    break
                target = cfg2.get("region", "KZ")
                cur = _detect_account_region()
                try:
                    _observe_account_region(cardinal, cur=cur)
                except Exception:
                    logger.debug("autocycle: observe region failed", exc_info=True)
                if cur and cur == target:
                    try:
                        bot.send_message(
                            chat_id,
                            f"✅ Регион аккаунта Steam теперь "
                            f"<b>{cur}</b>. Автоцикл остановлен "
                            f"(куплено: {cfg2.get('successes', 0)}).",
                            parse_mode="HTML")
                    except Exception:
                        pass
                    cfg2["running"] = False
                    _save_config(cfg2)
                    break
                cvv = _autocycle_cvv
                if cvv is None:
                    try:
                        bot.send_message(chat_id,
                                         "🛑 CVV пропал из памяти — "
                                         "автоцикл остановлен.")
                    except Exception:
                        pass
                    break
                ok, message, game = _run_one_cycle_sync(cvv, target)
                cfg3 = _load_config()
                cfg3["attempts"] = cfg3.get("attempts", 0) + 1
                if ok:
                    cfg3["successes"] = cfg3.get("successes", 0) + 1
                    cfg3["bought_today"] = cfg3.get("bought_today", 0) + 1
                    cfg3["last_error"] = ""
                    consec_failures = 0
                    try:
                        bot.send_message(
                            chat_id,
                            f"✅ Куплено: <b>{game.get('name', '?')}</b> "
                            f"(попытка #{cfg3['attempts']}, "
                            f"всего успехов {cfg3['successes']}).",
                            parse_mode="HTML")
                    except Exception:
                        pass
                else:
                    cfg3["last_error"] = message[:200]
                    consec_failures += 1
                    try:
                        bot.send_message(
                            chat_id,
                            f"❌ Попытка #{cfg3['attempts']}: "
                            f"<code>{message[:200]}</code>",
                            parse_mode="HTML")
                    except Exception:
                        pass
                _save_config(cfg3)
                if consec_failures >= AUTOCYCLE_MAX_FAILURES:
                    cfg4 = _load_config()
                    cfg4["running"] = False
                    _save_config(cfg4)
                    try:
                        bot.send_message(
                            chat_id,
                            f"🛑 {AUTOCYCLE_MAX_FAILURES} неудач подряд — "
                            "автоцикл остановлен. Проверь логи и "
                            "запусти заново после исправления.")
                    except Exception:
                        pass
                    break
                pause = random.randint(AUTOCYCLE_PAUSE_MIN,
                                       AUTOCYCLE_PAUSE_MAX)
                logger.info("autocycle: пауза %ds перед следующей попыткой",
                            pause)
                if _autocycle_stop.wait(pause):
                    break
        finally:
            _autocycle_cvv = None
            _autocycle_thread = None
            cfg5 = _load_config()
            if cfg5.get("running"):
                cfg5["running"] = False
                _save_config(cfg5)
            logger.info("autocycle: тред остановлен")

    def toggle_running(c: CallbackQuery) -> None:
        global _autocycle_cvv, _autocycle_thread
        if not _is_unlocked():
            _ack(c, "🔒 Сначала разблокируй плагин")
            return
        with _run_lock:
            cfg2 = _load_config()
        if cfg2.get("running"):
            _autocycle_stop.set()
            cfg2["running"] = False
            _save_config(cfg2)
            _autocycle_cvv = None
            _ack(c, "🛑 Останавливаю автоцикл…")
            try:
                bot.send_message(
                    c.message.chat.id,
                    "🔴 Автоцикл остановлен. CVV очищен из RAM.")
            except Exception:
                pass
            open_main(c)
            return
        # Запуск: предусловия
        if _steam_session is None:
            _ack(c, "❌ Сначала залогинься в Steam")
            return
        cards_list = _load_cards()
        main_card = next((x for x in cards_list if x.get("is_main")), None)
        if main_card is None:
            _ack(c, "❌ Нет основной карты — добавь и/или назначь")
            return
        pool = _load_proxies()
        target = cfg2.get("region", "KZ")
        has_region_proxy = any(
            p.get("alive") and p.get("country_code") == target
            for p in pool)
        if not has_region_proxy:
            try:
                bot.send_message(
                    c.message.chat.id,
                    f"⚠️ Нет живых прокси под регион <b>{target}</b>. "
                    "Будет fallback, но Steam, скорее всего, откажет "
                    "(IP не из региона = anti-fraud).",
                    parse_mode="HTML")
            except Exception:
                pass
        brand = _detect_brand(main_card["number"])
        cvv_len = 4 if brand == "Amex" else 3
        dry_run = bool(cfg2.get("dry_run_purchases", True))
        warn = ("🧪 DRY_RUN включён — реальных списаний не будет."
                if dry_run else
                "🔥 БОЕВОЙ режим — карта будет реально списана!")
        m = bot.send_message(
            c.message.chat.id,
            f"<b>🟢 Запуск автоцикла</b>\n\n"
            f"Регион: <b>{target}</b>\n"
            f"Карта: <b>{_mask_pan(main_card['number'])}</b> ({brand})\n"
            f"Пауза: {AUTOCYCLE_PAUSE_MIN}-{AUTOCYCLE_PAUSE_MAX}с\n"
            f"Стоп: совпадение региона или {AUTOCYCLE_MAX_FAILURES} "
            f"неудач подряд\n\n"
            f"{warn}\n\n"
            f"Введи CVV ({cvv_len} цифр) — сохранится только в RAM. "
            "/cancel — отмена.",
            parse_mode="HTML")
        _set_state(m.chat.id, m.message_id, c.from_user.id, ST_ASK_CVV_AUTO)
        _ack(c)

    def on_cvv_auto(m: Message) -> None:
        global _autocycle_cvv, _autocycle_thread
        if (m.text or "").strip().lower() in ("/cancel", "отмена", "cancel"):
            _clear_state(m)
            try:
                bot.delete_message(m.chat.id, m.message_id)
            except Exception:
                pass
            bot.send_message(m.chat.id, "🚫 Отменено.")
            return
        cards_list = _load_cards()
        main_card = next(
            (x for x in cards_list if x.get("is_main")), None)
        if main_card is None:
            _clear_state(m)
            bot.send_message(m.chat.id, "❌ Карта пропала.")
            return
        brand = _detect_brand(main_card["number"])
        cvv = _validate_cvv(m.text or "", brand_hint=brand)
        try:
            bot.delete_message(m.chat.id, m.message_id)
        except Exception:
            pass
        if cvv is None:
            cvv_len = 4 if brand == "Amex" else 3
            bot.send_message(
                m.chat.id,
                f"❌ CVV должен быть {cvv_len} цифр. /cancel или ещё раз.")
            return
        _clear_state(m)
        _autocycle_cvv = cvv
        _autocycle_stop.clear()
        cfg2 = _load_config()
        cfg2["running"] = True
        cfg2["last_error"] = ""
        _save_config(cfg2)
        _autocycle_thread = threading.Thread(
            target=_autocycle_loop,
            args=(m.chat.id, m.from_user.id),
            daemon=True, name="srr-autocycle")
        _autocycle_thread.start()
        bot.send_message(
            m.chat.id,
            "🟢 <b>Автоцикл запущен</b>.\n\n"
            "Останавливается:\n"
            "• автоматически когда регион аккаунта совпадёт с целевым;\n"
            f"• после {AUTOCYCLE_MAX_FAILURES} неудач подряд;\n"
            "• кнопкой <b>🔴 Stop</b> в /sranger.\n\n"
            "Промежуточные результаты приходят в этот чат.",
            parse_mode="HTML")

    # ---------- регион ----------
    def open_region(c: CallbackQuery) -> None:
        if not _is_unlocked():
            _ack(c, "🔒 Сначала разблокируй плагин")
            return
        cfg2 = _load_config()
        text = (
            "<b>🌍 Целевой регион</b>\n\n"
            f"Сейчас: <b>{REGION_LABELS.get(cfg2['region'], cfg2['region'])}</b>\n\n"
            "Выбери регион, в который хочешь переключить Steam-аккаунт. "
            "Плагин будет искать самые дешёвые игры именно в этом магазине."
        )
        kb = K()
        for code, lbl in REGIONS:
            mark = "✅ " if code == cfg2["region"] else ""
            kb.add(B(f"{mark}{lbl}",
                     callback_data=f"{CBT_REGION_PICK}:{code}"))
        kb.add(B("◀️ Назад в меню", callback_data=CBT_OPEN))
        _render(c, text, kb)
        _ack(c)

    def pick_region(c: CallbackQuery) -> None:
        if not _is_unlocked():
            _ack(c, "🔒 Сначала разблокируй плагин")
            return
        try:
            code = c.data.split(":", 2)[2].upper()
        except IndexError:
            _ack(c, "Битый callback")
            return
        if code not in REGION_LABELS:
            _ack(c, f"Неизвестный регион: {code}")
            return
        cfg2 = _load_config()
        cfg2["region"] = code
        _save_config(cfg2)
        _ack(c, f"✅ Регион: {code}")
        open_main(c)

    # ---------- логин / логаут (Phase 4) ----------
    def open_login(c: CallbackQuery) -> None:
        if not _is_unlocked():
            _ack(c, "🔒 Сначала разблокируй плагин")
            return
        if _is_steam_logged_in():
            _ack(c, "Уже залогинен — нажми 👤 Выход для смены аккаунта")
            return
        cfg2 = _load_config()
        target_region = cfg2.get("region", "?")
        proxy_url = _pick_proxy_for_region(target_region)
        proxy_note = (
            f"Прокси для логина: <code>{_mask_proxy_url(proxy_url)}</code>"
            if proxy_url else
            "⚠️ <b>Прокси нет</b> — логин пойдёт с твоего IP. "
            "Если регион аккаунта Steam отличается, это может вызвать защиту "
            "(письмо «новый вход с устройства» и т.п.). "
            "Лучше сначала добавить прокси.")
        m = bot.send_message(
            c.message.chat.id,
            "<b>🔑 Войти в Steam</b>\n\n"
            f"{proxy_note}\n\n"
            "Введи логин и пароль через пробел:\n"
            "<code>username password</code>\n\n"
            "/cancel — отмена.",
            parse_mode="HTML")
        _set_state(m.chat.id, m.message_id, c.from_user.id, ST_LOGIN_CREDS)
        _ack(c)

    def on_login_creds(m: Message) -> None:
        if (m.text or "").strip().lower() in ("/cancel", "отмена", "cancel"):
            _clear_state(m)
            bot.send_message(m.chat.id, "🚫 Отменено.")
            return
        creds = _validate_login_password(m.text or "")
        # Лучшая попытка — удалить сообщение пользователя с паролем.
        try:
            bot.delete_message(m.chat.id, m.message_id)
        except Exception:
            pass
        if creds is None:
            bot.send_message(
                m.chat.id,
                "❌ Не разобрал логин и пароль.\n\n"
                "Пришлите в любом из форматов:\n"
                "• <code>Логин: username Пароль: pass</code>\n"
                "• <code>Login: username Password: pass</code>\n"
                "• <code>username pass</code> или <code>username:pass</code>\n"
                "(логин 2-64 символа A-Z/0-9/._-). /cancel — отмена.",
                parse_mode="HTML")
            return
        login, password = creds
        _clear_state(m)
        cfg2 = _load_config()
        proxy_url = _pick_proxy_for_region(cfg2.get("region", ""))

        bot.send_message(m.chat.id, "⏳ Логин в Steam…")

        def _worker():
            sess = SteamInteractiveSession(login, password, proxy=proxy_url)
            result = sess.begin()
            if result == "ok":
                _finish_login_success(m, sess, login)
                return
            if result == "need_code":
                _pending_logins[m.from_user.id] = sess
                m2 = bot.send_message(
                    m.chat.id,
                    f"📩 <b>Steam Guard</b>\n\n"
                    f"{sess.last_message}\n\n"
                    "Введи код (5-6 символов, A-Z и 0-9). /cancel — отмена.",
                    parse_mode="HTML")
                _set_state(m2.chat.id, m2.message_id, m.from_user.id,
                           ST_LOGIN_CODE)
                return
            # error
            bot.send_message(
                m.chat.id,
                f"❌ Логин не прошёл:\n<code>{sess.last_message}</code>",
                parse_mode="HTML")

        threading.Thread(target=_worker, daemon=True,
                         name="srr-login").start()

    def on_login_code(m: Message) -> None:
        if (m.text or "").strip().lower() in ("/cancel", "отмена", "cancel"):
            _clear_state(m)
            _pending_logins.pop(m.from_user.id, None)
            bot.send_message(m.chat.id, "🚫 Отменено.")
            return
        code = _validate_guard_code(m.text or "")
        if code is None:
            bot.send_message(
                m.chat.id,
                "❌ Код 4-6 символов A-Z/0-9. Попробуй ещё или /cancel.")
            return
        _clear_state(m)
        sess = _pending_logins.pop(m.from_user.id, None)
        if sess is None:
            bot.send_message(m.chat.id,
                             "Сессия логина потерялась. /sranger → 🔑 Войти.")
            return
        bot.send_message(m.chat.id, "⏳ Подтверждение Guard-кода…")

        def _worker():
            result = sess.submit_code(code)
            if result == "ok":
                _finish_login_success(m, sess, sess.login)
            elif result == "need_code":
                _pending_logins[m.from_user.id] = sess
                m2 = bot.send_message(
                    m.chat.id,
                    f"❌ Код не подошёл, нужен ещё:\n<code>{sess.last_message}</code>",
                    parse_mode="HTML")
                _set_state(m2.chat.id, m2.message_id, m.from_user.id,
                           ST_LOGIN_CODE)
            else:
                bot.send_message(
                    m.chat.id,
                    f"❌ Логин не прошёл:\n<code>{sess.last_message}</code>",
                    parse_mode="HTML")

        threading.Thread(target=_worker, daemon=True,
                         name="srr-login-code").start()

    def _finish_login_success(m: Message,
                              sess: "SteamInteractiveSession",
                              login: str) -> None:
        global _steam_session, _steam_login_name
        try:
            _save_steam_session(login, sess.session)
        except Exception:
            logger.exception("steam_ranger: save session failed")
            bot.send_message(m.chat.id,
                             "⚠️ Логин прошёл, но сохранить session.enc не "
                             "удалось — после рестарта Cardinal придётся "
                             "залогиниться заново.")
        _steam_session = sess.session
        _steam_login_name = login
        bot.send_message(
            m.chat.id,
            f"✅ Залогинен в Steam как <b>{login}</b>.",
            parse_mode="HTML")

    def do_logout(c: CallbackQuery) -> None:
        global _steam_session, _steam_login_name
        if not _is_unlocked():
            _ack(c, "🔒 Сначала разблокируй плагин")
            return
        _steam_session = None
        _steam_login_name = None
        _delete_steam_session()
        _ack(c, "👋 Logout")
        open_main(c)

    # ========================================================================
    # Прокси (Phase 3)
    # ========================================================================
    def _proxy_list_text() -> str:
        pool = _load_proxies()
        total = len(pool)
        alive = sum(1 for p in pool if p.get("alive"))
        lines = [f"<b>🌐 Прокси: {total} (живых: {alive})</b>\n"]
        if not pool:
            lines.append("Пусто. Добавь первый ↓")
        else:
            for i, p in enumerate(pool, 1):
                mark = "✅" if p.get("alive") else "❌"
                cc = p.get("country_code") or "?"
                lines.append(f"{i}) {mark} <code>{_mask_proxy_url(p['url'])}</code>"
                             f" — <b>{cc}</b>")
        cfg2 = _load_config()
        target = cfg2.get("region", "?")
        in_target = sum(1 for p in pool
                        if p.get("alive") and p.get("country_code") == target)
        lines.append("")
        lines.append(f"В целевом регионе <b>{target}</b>: <b>{in_target}</b>")
        return "\n".join(lines)

    def _proxy_kb() -> K:
        pool = _load_proxies()
        kb = K()
        # ряды кнопок удаления (по одному на прокси)
        for p in pool:
            kb.add(B(f"🗑 {_mask_proxy_url(p['url'])}",
                     callback_data=f"{CBT_PROXY_DEL}:{p['id']}"))
        kb.row(
            B("➕ Добавить", callback_data=CBT_PROXY_ADD),
            B("♻️ Перепроверить все", callback_data=CBT_PROXY_RECHECK),
        )
        kb.add(B("◀️ Назад в меню", callback_data=CBT_OPEN))
        return kb

    def open_proxy(c: CallbackQuery) -> None:
        if not _is_unlocked():
            _ack(c, "🔒 Сначала разблокируй плагин")
            return
        _render(c, _proxy_list_text(), _proxy_kb())
        _ack(c)

    def proxy_add_start(c: CallbackQuery) -> None:
        if not _is_unlocked():
            _ack(c, "🔒 Сначала разблокируй плагин")
            return
        m = bot.send_message(
            c.message.chat.id,
            "<b>➕ Добавить прокси</b>\n\n"
            "Введи URL в формате:\n"
            "<code>socks5://user:pass@host:port</code>\n"
            "<code>http://host:port</code>\n"
            "<code>https://user:pass@host:port</code>\n\n"
            "Поддерживаются <b>socks5/socks5h/socks4/http/https</b>.\n"
            "После сохранения прокси будет проверен через "
            "<code>ip-api.com/json</code> для определения страны.\n\n"
            "/cancel — отмена.",
            parse_mode="HTML")
        _set_state(m.chat.id, m.message_id, c.from_user.id, ST_ADD_PROXY)
        _ack(c)

    def on_add_proxy(m: Message) -> None:
        if (m.text or "").strip().lower() in ("/cancel", "отмена", "cancel"):
            _clear_state(m)
            bot.send_message(m.chat.id, "🚫 Отменено.")
            return
        url = _validate_proxy_url(m.text or "")
        if url is None:
            bot.send_message(
                m.chat.id,
                "❌ Неверный формат. Пример: "
                "<code>socks5://user:pass@1.2.3.4:1080</code>\n"
                "Попробуй ещё или /cancel.",
                parse_mode="HTML")
            return
        _clear_state(m)
        bot.send_message(m.chat.id,
                         "⏳ Проверяю прокси через ip-api.com…")

        def _worker():
            alive, cc, country, ext_ip = _check_proxy(url)
            entry = {
                "id": _new_proxy_id(),
                "url": url,
                "country": country,
                "country_code": cc,
                "alive": alive,
                "last_check": int(time.time()),
                "external_ip": ext_ip,
            }
            try:
                pool = _load_proxies()
                pool.append(entry)
                _save_proxies(pool)
            except Exception:
                logger.exception("steam_ranger: save_proxies failed in add")
                bot.send_message(m.chat.id, "❌ Ошибка сохранения прокси.")
                return
            if alive:
                bot.send_message(
                    m.chat.id,
                    f"✅ Прокси добавлен.\n"
                    f"Страна: <b>{country}</b> ({cc})\n"
                    f"Внешний IP: <code>{ext_ip}</code>",
                    parse_mode="HTML")
            else:
                bot.send_message(
                    m.chat.id,
                    "⚠️ Прокси сохранён, но проверка не прошла "
                    "(нет ответа от ip-api или нерабочий). Помечен ❌.\n"
                    "Можно перепроверить кнопкой ♻️ позже.",
                    parse_mode="HTML")

        threading.Thread(target=_worker, daemon=True,
                         name="srr-proxy-add").start()

    def proxy_delete(c: CallbackQuery) -> None:
        if not _is_unlocked():
            _ack(c, "🔒 Сначала разблокируй плагин")
            return
        try:
            pid = c.data.split(":", 2)[2]
        except IndexError:
            _ack(c, "Битый callback")
            return
        pool = _load_proxies()
        new = [p for p in pool if p.get("id") != pid]
        if len(new) == len(pool):
            _ack(c, "Прокси не найден")
            return
        _save_proxies(new)
        _ack(c, "🗑 Удалён")
        open_proxy(c)

    def proxy_recheck_all(c: CallbackQuery) -> None:
        if not _is_unlocked():
            _ack(c, "🔒 Сначала разблокируй плагин")
            return
        pool = _load_proxies()
        if not pool:
            _ack(c, "Пул пуст")
            return
        chat_id = c.message.chat.id
        _ack(c, f"♻️ Проверяю {len(pool)} прокси…")
        try:
            bot.send_message(chat_id,
                             f"⏳ Проверяю {len(pool)} прокси через ip-api.com, "
                             "это займёт до {:d} сек…".format(
                                 PROXY_CHECK_TIMEOUT * len(pool) // 2 + 5))
        except Exception:
            pass

        def _worker():
            updated = 0
            for p in pool:
                alive, cc, country, ext_ip = _check_proxy(p["url"])
                p["alive"] = alive
                p["last_check"] = int(time.time())
                if alive:
                    p["country_code"] = cc
                    p["country"] = country
                    p["external_ip"] = ext_ip
                updated += 1
            try:
                _save_proxies(pool)
            except Exception:
                logger.exception("steam_ranger: save_proxies in recheck failed")
                bot.send_message(chat_id, "❌ Ошибка сохранения после перепроверки.")
                return
            alive_n = sum(1 for p in pool if p.get("alive"))
            bot.send_message(
                chat_id,
                f"✅ Перепроверено {updated} прокси. "
                f"Живых: <b>{alive_n}</b> / {updated}.",
                parse_mode="HTML")

        threading.Thread(target=_worker, daemon=True,
                         name="srr-proxy-recheck").start()

    # ---------- разовая покупка (Phase 5: пока только поиск) ----------
    def open_purchase_once(c: CallbackQuery) -> None:
        if not _is_unlocked():
            _ack(c, "🔒 Сначала разблокируй плагин")
            return
        cfg2 = _load_config()
        region = cfg2.get("region", "KZ")
        proxy = _pick_proxy_for_region(region)
        chat_id = c.message.chat.id

        proxy_note = (
            f"Прокси: <code>{_mask_proxy_url(proxy)}</code>"
            if proxy else
            "⚠️ Без прокси — Steam отдаст прайс региона твоего IP")

        try:
            bot.send_message(
                chat_id,
                f"⏳ Ищу самые дешёвые игры в магазине <b>{region}</b>…\n"
                f"{proxy_note}",
                parse_mode="HTML")
        except Exception:
            pass
        _ack(c)

        def _worker():
            games = _search_cheap_games(region, proxy=proxy, max_results=5)
            if not games:
                bot.send_message(
                    chat_id,
                    f"❌ Ничего не найдено в {region}.\n"
                    "Возможные причины:\n"
                    "• Прокси нерабочий или не в регионе\n"
                    "• Steam отдал капчу (редко, но бывает)\n"
                    "• Парсер регулярки не подошёл (Steam меняет вёрстку)\n\n"
                    "Проверь логи Cardinal на строки 'steam_ranger: search'.",
                    parse_mode="HTML")
                return
            for g in games:
                _log_found_game(g)

            top = games[0]
            lines = [
                f"<b>🔍 Найдено в {region}</b> "
                f"(показаны топ-{min(5, len(games))} по цене):\n"
            ]
            for i, g in enumerate(games[:5], 1):
                price = _format_price_minor(g["price_minor"], region)
                lines.append(
                    f"{i}) <b>{g['name']}</b> — {price} "
                    f"(appid <code>{g['appid']}</code>)")
            lines.append("")
            lines.append(
                "🎯 <b>Цель для покупки:</b>\n"
                f"  <b>{top['name']}</b>\n"
                f"  Цена: {_format_price_minor(top['price_minor'], region)}\n"
                f"  appid: <code>{top['appid']}</code>")
            lines.append("")
            cfg3 = _load_config()
            if cfg3.get("dry_run_purchases", True):
                lines.append(
                    "🧪 <b>Сейчас включён DRY_RUN</b> — pipeline покупки "
                    "пройдёт все шаги (addtocart → init → finalize), но "
                    "init и finalize только залогируются, реально не "
                    "отправятся. Карта не будет списана."
                )
            else:
                lines.append(
                    "🔥 <b>БОЕВОЙ РЕЖИМ</b> — следующее нажатие реально "
                    "спишет деньги с основной карты!"
                )
            kb = K()
            cards = _load_cards()
            main_card = next((c for c in cards if c.get("is_main")), None)
            if _steam_session is None:
                lines.append("\n❌ Не залогинен в Steam — login first.")
            elif main_card is None:
                lines.append("\n❌ Нет основной карты — добавь и/или назначь.")
            else:
                kb.add(B(
                    f"💳 Купить «{top['name'][:20]}» "
                    f"картой ****{_mask_pan(main_card['number'])[-4:]}",
                    callback_data=f"{CBT_PURCHASE_BUY}:{top['appid']}"))
            kb.add(B("🔄 Найти заново (clear cache)",
                     callback_data=CBT_PURCHASE_REFRESH))
            kb.add(B("◀️ Назад в меню", callback_data=CBT_OPEN))
            bot.send_message(chat_id, "\n".join(lines),
                             parse_mode="HTML", reply_markup=kb)

        threading.Thread(target=_worker, daemon=True,
                         name="srr-search").start()

    def refresh_purchase_search(c: CallbackQuery) -> None:
        """Сбрасываем кэш поиска и зовём open_purchase_once заново."""
        if not _is_unlocked():
            _ack(c, "🔒 Сначала разблокируй плагин")
            return
        cfg2 = _load_config()
        region = cfg2.get("region", "KZ")
        with _search_cache_lock:
            _search_cache.pop(region, None)
        _ack(c, "Кэш сброшен, ищу заново…")
        open_purchase_once(c)

    # ========================================================================
    # Phase 6: Buy this game flow
    # ========================================================================
    def toggle_dry_run(c: CallbackQuery) -> None:
        if not _is_unlocked():
            _ack(c, "🔒 Сначала разблокируй плагин")
            return
        cfg2 = _load_config()
        new_val = not bool(cfg2.get("dry_run_purchases", True))
        cfg2["dry_run_purchases"] = new_val
        _save_config(cfg2)
        if new_val:
            _ack(c, "🧪 Dry-run ВКЛ — карта не будет списана")
        else:
            _ack(c, "🔥 БОЕВОЙ режим ВКЛ — следующая покупка реальная!",
                 )
        open_more(c)

    def start_purchase(c: CallbackQuery) -> None:
        """Кнопка '💳 Купить эту' → ST_ASK_CVV (если предусловия выполнены)."""
        if not _is_unlocked():
            _ack(c, "🔒 Сначала разблокируй плагин")
            return
        try:
            appid = int(c.data.split(":", 2)[2])
        except (IndexError, ValueError):
            _ack(c, "Битый callback")
            return
        if _steam_session is None:
            _ack(c, "❌ Не залогинен в Steam")
            return
        cards = _load_cards()
        card = next((x for x in cards if x.get("is_main")), None)
        if card is None:
            _ack(c, "❌ Нет основной карты")
            return
        cfg2 = _load_config()
        dry_run = bool(cfg2.get("dry_run_purchases", True))
        # Сохраняем контекст покупки до получения CVV.
        with _purchase_lock:
            _pending_purchases[c.from_user.id] = {
                "appid": appid,
                "card_id": card["id"],
                "region": cfg2.get("region", "KZ"),
                "dry_run": dry_run,
                "started_at": int(time.time()),
            }
        brand = _detect_brand(card["number"])
        cvv_len = 4 if brand == "Amex" else 3
        warn = (
            "🧪 DRY_RUN — карта не будет реально списана."
            if dry_run else
            "🔥 БОЕВОЙ — следующее сообщение запустит реальную покупку!"
        )
        m = bot.send_message(
            c.message.chat.id,
            f"<b>💳 Подтверждение покупки</b>\n\n"
            f"Игра appid: <code>{appid}</code>\n"
            f"Карта: <b>{_mask_pan(card['number'])}</b> ({brand})\n"
            f"Регион: <b>{cfg2.get('region', 'KZ')}</b>\n\n"
            f"{warn}\n\n"
            f"Введи CVV ({cvv_len} цифр) или /cancel:",
            parse_mode="HTML")
        _set_state(m.chat.id, m.message_id, c.from_user.id, ST_ASK_CVV)
        _ack(c)

    def on_cvv(m: Message) -> None:
        if (m.text or "").strip().lower() in ("/cancel", "отмена", "cancel"):
            _clear_state(m)
            with _purchase_lock:
                _pending_purchases.pop(m.from_user.id, None)
            try:
                bot.delete_message(m.chat.id, m.message_id)
            except Exception:
                pass
            bot.send_message(m.chat.id, "🚫 Отменено.")
            return
        ctx = _pending_purchases.get(m.from_user.id)
        if ctx is None:
            _clear_state(m)
            bot.send_message(m.chat.id,
                             "Контекст покупки потерялся. /sranger → 🔄.")
            return
        cards = _load_cards()
        card = next((x for x in cards if x.get("id") == ctx["card_id"]), None)
        if card is None:
            _clear_state(m)
            with _purchase_lock:
                _pending_purchases.pop(m.from_user.id, None)
            bot.send_message(m.chat.id, "❌ Карта пропала. Покупка отменена.")
            return
        brand = _detect_brand(card["number"])
        cvv = _validate_cvv(m.text or "", brand_hint=brand)
        # ВАЖНО: удалить сообщение пользователя с CVV, иначе оно остаётся
        # в чате.
        try:
            bot.delete_message(m.chat.id, m.message_id)
        except Exception:
            pass
        if cvv is None:
            cvv_len = 4 if brand == "Amex" else 3
            bot.send_message(
                m.chat.id,
                f"❌ CVV должен быть {cvv_len} цифр для {brand}. "
                "Попробуй ещё или /cancel.")
            return
        _clear_state(m)
        chat_id = m.chat.id
        user_id = m.from_user.id
        # Запускаем pipeline в worker'е, чтобы не блокировать тг-поток.
        threading.Thread(
            target=_purchase_pipeline,
            args=(user_id, chat_id, ctx["appid"], card, cvv,
                  ctx["region"], ctx["dry_run"]),
            daemon=True, name="srr-purchase").start()

    def _purchase_pipeline(user_id: int, chat_id: int, appid: int,
                            card: dict, cvv: str, region: str,
                            dry_run: bool) -> None:
        """Полный pipeline в worker-треде: addtocart → init → [3DS] → finalize.
        Шлёт промежуточные сообщения в chat_id."""
        proxy = _pick_proxy_for_region(region)

        try:
            bot.send_message(
                chat_id,
                f"⏳ <b>1/4</b> добавляю в корзину "
                f"(appid <code>{appid}</code>)…",
                parse_mode="HTML")
        except Exception:
            pass

        if _steam_session is None:
            bot.send_message(chat_id, "❌ Steam-сессия пропала. Перелогинься.")
            return
        engine = SteamPurchaseEngine(
            _steam_session, region, proxy=proxy, dry_run=dry_run)
        subid = engine.get_subid_for_appid(appid)
        if subid is None:
            bot.send_message(
                chat_id,
                f"❌ Не нашёл package id у appid={appid} "
                "(/api/appdetails не отдал packages).")
            return
        cart_gid = engine.add_to_cart(subid)
        if not cart_gid:
            bot.send_message(
                chat_id,
                "❌ addtocart не дал shoppingCartGID — обычно это значит, "
                "что Steam отказал по региону, anti-fraud или капча.")
            return

        try:
            bot.send_message(
                chat_id,
                f"💳 <b>2/4</b> init transaction "
                f"(<code>{cart_gid}</code>)…",
                parse_mode="HTML")
        except Exception:
            pass
        # Цена для логов (грубо — дёрнем _search_cheap_games кэш)
        game_for_log = {"appid": appid, "name": f"appid:{appid}",
                        "price_minor": 0, "cc": region}
        try:
            cached = _search_cache.get(region, (0, []))[1]
            for g in cached:
                if g["appid"] == appid:
                    game_for_log = g
                    break
        except Exception:
            pass

        result = engine.init_transaction(cart_gid, card, cvv)
        if not result.ok and result.confirmation_url:
            # 3DS
            with _purchase_lock:
                _pending_purchases[user_id] = {
                    **(_pending_purchases.get(user_id) or {}),
                    "engine": engine,
                    "transid": result.transid,
                    "game": game_for_log,
                    "card": card,
                    "region": region,
                    "dry_run": dry_run,
                }
            kb = K()
            kb.add(B("✅ Подтвердил в банке",
                     callback_data=f"{CBT_3DS_DONE}:{user_id}"))
            kb.add(B("❌ Отменить", callback_data=f"{CBT_3DS_CANCEL}:{user_id}"))
            bot.send_message(
                chat_id,
                f"🔐 <b>3DS-подтверждение</b>\n\n"
                f"Открой ссылку, подтверди в банке "
                f"(SMS/push/код — что попросит банк):\n"
                f"<a href='{result.confirmation_url}'>"
                f"{result.confirmation_url}</a>\n\n"
                f"Затем нажми ✅ ниже. Если банк отказал — ❌.",
                parse_mode="HTML", reply_markup=kb,
                disable_web_page_preview=True)
            _set_state(chat_id, 0, user_id, ST_WAIT_3DS_CONFIRM)
            return

        if not result.ok:
            _log_purchase(game_for_log, card, region, dry_run, False,
                          result.message)
            cfg2 = _load_config()
            cfg2["last_error"] = result.message[:200]
            cfg2["attempts"] = cfg2.get("attempts", 0) + 1
            _save_config(cfg2)
            bot.send_message(
                chat_id, f"❌ init не прошёл:\n<code>{result.message}</code>",
                parse_mode="HTML")
            with _purchase_lock:
                _pending_purchases.pop(user_id, None)
            return

        # init OK без 3DS — finalize
        _do_finalize(user_id, chat_id, engine, result.transid,
                     game_for_log, card, region, dry_run)

    def _do_finalize(user_id: int, chat_id: int,
                     engine: "SteamPurchaseEngine", transid: str,
                     game: dict, card: dict, region: str,
                     dry_run: bool) -> None:
        try:
            bot.send_message(
                chat_id,
                f"🏁 <b>3/4</b> finalize "
                f"(<code>transid={transid}</code>)…",
                parse_mode="HTML")
        except Exception:
            pass
        result2 = engine.finalize(transid)
        cfg2 = _load_config()
        cfg2["attempts"] = cfg2.get("attempts", 0) + 1
        if result2.ok:
            cfg2["successes"] = cfg2.get("successes", 0) + 1
            cfg2["bought_today"] = cfg2.get("bought_today", 0) + 1
            cfg2["last_error"] = ""
            _save_config(cfg2)
            _log_purchase(game, card, region, dry_run, True,
                          result2.message, transid=transid)
            bot.send_message(
                chat_id,
                f"✅ <b>4/4 Готово</b>\n\n"
                f"<b>{game.get('name', '?')}</b> куплена в {region}!\n"
                f"transid: <code>{transid}</code>\n"
                f"{'(DRY_RUN — на самом деле нет)' if dry_run else ''}",
                parse_mode="HTML")
        else:
            cfg2["last_error"] = result2.message[:200]
            _save_config(cfg2)
            _log_purchase(game, card, region, dry_run, False,
                          result2.message, transid=transid)
            bot.send_message(
                chat_id,
                f"❌ finalize не прошёл:\n"
                f"<code>{result2.message}</code>",
                parse_mode="HTML")
        with _purchase_lock:
            _pending_purchases.pop(user_id, None)

    def confirm_3ds(c: CallbackQuery) -> None:
        if not _is_unlocked():
            _ack(c, "🔒 Сначала разблокируй плагин")
            return
        with _purchase_lock:
            ctx = _pending_purchases.get(c.from_user.id)
        if not ctx or not ctx.get("engine"):
            _ack(c, "Контекст 3DS потерялся")
            return
        _ack(c, "🏁 Финализирую…")
        threading.Thread(
            target=_do_finalize,
            args=(c.from_user.id, c.message.chat.id, ctx["engine"],
                  ctx["transid"], ctx["game"], ctx["card"], ctx["region"],
                  ctx["dry_run"]),
            daemon=True, name="srr-finalize").start()

    def cancel_3ds(c: CallbackQuery) -> None:
        with _purchase_lock:
            _pending_purchases.pop(c.from_user.id, None)
        _ack(c, "❌ Отменено")
        bot.send_message(c.message.chat.id,
                         "🚫 3DS отменён. Покупка не завершена.")

    # ========================================================================
    # Phase 8: ручной список игр (per-region)
    # ========================================================================
    def _format_manual_price(g: dict) -> str:
        pm = g.get("price_minor")
        cur = g.get("currency") or g.get("region", "?")
        if pm is None:
            return "<i>цена не запрошена</i>"
        if pm <= 0:
            return f"<i>0 (бесплатная/без цены)</i>"
        return f"{pm/100:.2f} {cur}"

    def open_manual_games(c: CallbackQuery) -> None:
        if not _is_unlocked():
            _ack(c, "🔒 Сначала разблокируй плагин")
            return
        cfg2 = _load_config()
        region = cfg2.get("region", "KZ")
        all_games = _load_manual_games()
        games = [g for g in all_games if g.get("region") == region]
        prefer = bool(cfg2.get("prefer_manual_list", True))

        lines = [
            f"<b>📋 Свой список игр — регион {region}</b>\n",
            f"Записей всего: <b>{len(all_games)}</b>, "
            f"для {region}: <b>{len(games)}</b>",
            "",
        ]
        if not games:
            lines.append("Пусто. Добавь хотя бы одну игру ↓")
        else:
            # сортируем по price_minor ascending для UX
            games_sorted = sorted(
                games,
                key=lambda x: (x.get("price_minor") or 10**9, x.get("appid")))
            for i, g in enumerate(games_sorted, 1):
                price = _format_manual_price(g)
                last = g.get("last_price_check")
                age = ""
                if last:
                    h = (int(time.time()) - last) // 3600
                    age = f" · обновлено {h}ч назад" if h > 0 else " · недавно"
                name = g.get("name") or f"appid:{g['appid']}"
                lines.append(
                    f"{i}) <b>{name}</b> — {price}{age}\n"
                    f"   appid <code>{g['appid']}</code>")
        lines.append("")
        if prefer:
            lines.append(
                "🎯 Активный режим выбора: <b>сначала из списка</b>, "
                "fallback на автопоиск (если для региона нет ручных игр)."
            )
        else:
            lines.append(
                "🎯 Активный режим выбора: <b>только автопоиск</b> — "
                "ручной список игнорируется."
            )

        kb = K()
        for g in games:
            name = (g.get("name") or f"appid:{g['appid']}")[:25]
            price = _format_manual_price(g)
            # Простая текстовая кнопка: "🗑 NAME 0.99 KZ"
            kb.add(B(f"🗑 {name} ({price})",
                     callback_data=f"{CBT_MGAME_DEL}:{g['id']}"))
        kb.row(
            B("➕ Добавить appid", callback_data=CBT_MGAME_ADD),
            B("🔄 Обновить цены", callback_data=CBT_MGAME_REFRESH),
        )
        toggle_lbl = ("🔁 Использовать только автопоиск"
                      if prefer else "🔁 Сначала из списка")
        kb.add(B(toggle_lbl, callback_data=CBT_MGAME_TOGGLE_PREFER))
        kb.add(B("◀️ Назад в меню", callback_data=CBT_OPEN))
        _render(c, "\n".join(lines), kb)
        _ack(c)

    def manual_game_add_start(c: CallbackQuery) -> None:
        if not _is_unlocked():
            _ack(c, "🔒 Сначала разблокируй плагин")
            return
        cfg2 = _load_config()
        region = cfg2.get("region", "KZ")
        m = bot.send_message(
            c.message.chat.id,
            f"<b>➕ Добавить игру в свой список</b>\n\n"
            f"Регион: <b>{region}</b> "
            "(берётся из текущей настройки в /sranger).\n\n"
            "Введи <b>appid</b> (число) или Steam URL:\n"
            "<code>2280</code>\n"
            "<code>https://store.steampowered.com/app/2280/Half_Life/</code>\n\n"
            "После сохранения цена и название подтянутся через "
            "<code>/api/appdetails</code>.\n"
            "/cancel — отмена.",
            parse_mode="HTML")
        _set_state(m.chat.id, m.message_id, c.from_user.id, ST_ADD_APPID)
        _ack(c)

    def on_appid(m: Message) -> None:
        if (m.text or "").strip().lower() in ("/cancel", "отмена", "cancel"):
            _clear_state(m)
            bot.send_message(m.chat.id, "🚫 Отменено.")
            return
        appid = _validate_appid(m.text or "")
        if appid is None:
            bot.send_message(
                m.chat.id,
                "❌ Не похоже на appid. Нужно число или URL вида "
                "<code>store.steampowered.com/app/&lt;N&gt;/...</code>. "
                "/cancel — отмена.",
                parse_mode="HTML")
            return
        _clear_state(m)
        cfg2 = _load_config()
        region = cfg2.get("region", "KZ")
        existing = _load_manual_games()
        # Дубликаты по (appid, region)
        if any(g.get("appid") == appid and g.get("region") == region
               for g in existing):
            bot.send_message(
                m.chat.id,
                f"⚠️ Игра appid {appid} уже в списке для региона {region}.")
            return
        bot.send_message(
            m.chat.id,
            f"⏳ Запрашиваю детали appid <code>{appid}</code> для {region}…",
            parse_mode="HTML")

        def _worker():
            proxy = _pick_proxy_for_region(region)
            info = _fetch_app_info(appid, region, proxy=proxy)
            entry = {
                "id": _new_manual_game_id(),
                "appid": appid,
                "region": region,
                "name": (info or {}).get("name"),
                "price_minor": (info or {}).get("price_minor"),
                "currency": (info or {}).get("currency"),
                "last_price_check": int(time.time()) if info else None,
                "added_at": int(time.time()),
            }
            try:
                games = _load_manual_games()
                games.append(entry)
                _save_manual_games(games)
            except Exception:
                logger.exception("steam_ranger: save_manual_games failed")
                bot.send_message(m.chat.id, "❌ Ошибка сохранения.")
                return
            if info is None:
                bot.send_message(
                    m.chat.id,
                    f"⚠️ Сохранил appid <code>{appid}</code>, но "
                    "<code>/api/appdetails</code> вернул success=false.\n"
                    "Возможные причины: игра не доступна в регионе "
                    f"{region}, снята с продажи или DLC без базовой игры.\n"
                    "При покупке pipeline всё равно попробует — но шансы "
                    "низкие. Проверь appid руками.",
                    parse_mode="HTML")
                return
            if (info.get("price_minor") or 0) <= 0:
                bot.send_message(
                    m.chat.id,
                    f"⚠️ <b>{info['name']}</b> добавлена, но "
                    "цена 0/нет (бесплатная или без публичной цены). "
                    "Для смены региона не подойдёт.",
                    parse_mode="HTML")
                return
            bot.send_message(
                m.chat.id,
                f"✅ Добавлено: <b>{info['name']}</b>\n"
                f"  appid: <code>{appid}</code>\n"
                f"  цена: {info['price_minor']/100:.2f} "
                f"{info.get('currency') or region}\n"
                f"  регион: {region}",
                parse_mode="HTML")

        threading.Thread(target=_worker, daemon=True,
                         name="srr-mgame-add").start()

    def manual_game_delete(c: CallbackQuery) -> None:
        if not _is_unlocked():
            _ack(c, "🔒 Сначала разблокируй плагин")
            return
        try:
            gid = c.data.split(":", 2)[2]
        except IndexError:
            _ack(c, "Битый callback")
            return
        games = _load_manual_games()
        new = [g for g in games if g.get("id") != gid]
        if len(new) == len(games):
            _ack(c, "Запись не найдена")
            return
        _save_manual_games(new)
        _ack(c, "🗑 Удалено")
        open_manual_games(c)

    def manual_game_refresh(c: CallbackQuery) -> None:
        if not _is_unlocked():
            _ack(c, "🔒 Сначала разблокируй плагин")
            return
        games = _load_manual_games()
        if not games:
            _ack(c, "Список пуст")
            return
        chat_id = c.message.chat.id
        cfg2 = _load_config()
        proxy = _pick_proxy_for_region(cfg2.get("region", "KZ"))
        _ack(c, f"🔄 Обновляю {len(games)} цен…")

        def _worker():
            updated, failed = _refresh_manual_game_prices(proxy=proxy)
            try:
                bot.send_message(
                    chat_id,
                    f"✅ Цены обновлены: {updated}/{len(games)} "
                    f"(не удалось: {failed}).")
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True,
                         name="srr-mgame-refresh").start()

    def manual_toggle_prefer(c: CallbackQuery) -> None:
        if not _is_unlocked():
            _ack(c, "🔒 Сначала разблокируй плагин")
            return
        cfg2 = _load_config()
        new_val = not bool(cfg2.get("prefer_manual_list", True))
        cfg2["prefer_manual_list"] = new_val
        _save_config(cfg2)
        _ack(c, f"prefer_manual_list = {new_val}")
        open_manual_games(c)

    # ---------- статус ----------
    def open_status(c: CallbackQuery) -> None:
        if not _is_unlocked():
            _ack(c, "🔒 Сначала разблокируй плагин")
            return
        cfg2 = _load_config()
        s = _status_short()
        last_err = cfg2.get("last_error") or "—"
        text = (
            f"<b>📊 Статус</b>\n\n"
            f"Регион (целевой): <b>{s['region']}</b>\n"
            f"Регион аккаунта: <b>?</b> "
            f"<i>(определится в Фазе 4 после логина)</i>\n"
            f"Steam: <b>{s['steam']}</b>\n"
            f"Прокси: <b>{s['proxies']}</b>\n"
            f"Карт: <b>{s['cards']}</b>\n"
            f"Запуск: <b>{s['running']}</b>\n"
            f"Куплено сегодня: <b>{cfg2.get('bought_today', 0)}</b>\n"
            f"Попыток: <b>{cfg2.get('attempts', 0)}</b> · "
            f"успехов: <b>{cfg2.get('successes', 0)}</b>\n"
            f"Последняя ошибка: <i>{last_err}</i>"
        )
        _render(c, text, _kb_back())
        _ack(c)

    # ========================================================================
    # Unlock flow
    # ========================================================================
    def _is_first_unlock() -> bool:
        return _load_meta().get("salt") is None

    def open_unlock(c: CallbackQuery) -> None:
        # Через кнопку: создаём сообщение и переводим в стейт ASK_PASSPHRASE.
        if _is_unlocked():
            _ack(c, "Уже разблокирован")
            open_main(c)
            return
        first = _is_first_unlock()
        text = (
            "🔓 <b>Разблокировка</b>\n\n"
            + ("Это первый запуск — придумай мастер-парольную фразу. "
               "Она будет нужна при каждом старте Cardinal для расшифровки "
               "локального хранилища (карты, прокси, Steam-сессия).\n\n"
               "<b>Запиши её</b>: восстановить нельзя, забыл — потеряешь "
               "все сохранённые данные.\n\n"
               "Введи фразу (минимум 8 символов):"
               if first else
               "Введи мастер-парольную фразу для расшифровки хранилища:")
        )
        try:
            m = bot.send_message(c.message.chat.id, text, parse_mode="HTML")
        except Exception:
            logger.exception("steam_ranger: send_message in open_unlock failed")
            _ack(c, "Ошибка")
            return
        _set_state(m.chat.id, m.message_id, c.from_user.id, ST_ASK_PASSPHRASE)
        _ack(c)

    def cmd_unlock(m: Message) -> None:
        # Через команду /sranger_unlock — то же самое, без callback.
        if _is_unlocked():
            bot.send_message(m.chat.id, "✅ Уже разблокирован.")
            return
        first = _is_first_unlock()
        text = (
            "🔓 <b>Разблокировка</b>\n\n"
            + ("Первый запуск. Придумай мастер-парольную фразу "
               "(минимум 8 символов):"
               if first else
               "Введи мастер-парольную фразу:")
        )
        m2 = bot.send_message(m.chat.id, text, parse_mode="HTML")
        _set_state(m2.chat.id, m2.message_id, m.from_user.id, ST_ASK_PASSPHRASE)

    def on_passphrase(m: Message) -> None:
        global _master_key
        passphrase = (m.text or "").strip()
        _clear_state(m)
        # сразу удалим сообщение пользователя с фразой, чтобы оно не висело
        # в истории чата (best-effort).
        try:
            bot.delete_message(m.chat.id, m.message_id)
        except Exception:
            pass

        if len(passphrase) < 8:
            bot.send_message(m.chat.id, "❌ Минимум 8 символов. /sranger_unlock — попробуй ещё.")
            return

        meta = _load_meta()
        if meta.get("salt") is None:
            # первый unlock: запросим повтор
            _pending_first_passphrase[m.from_user.id] = passphrase
            m2 = bot.send_message(
                m.chat.id,
                "🔁 Повтори ту же фразу для подтверждения:",
                parse_mode="HTML")
            _set_state(m2.chat.id, m2.message_id, m.from_user.id,
                       ST_ASK_PASSPHRASE_CONFIRM)
            return

        # повторный unlock: выводим ключ и пробуем расшифровать cards.enc как пруф
        salt = base64.urlsafe_b64decode(meta["salt"].encode("ascii"))
        key = _derive_key(passphrase, salt)

        if os.path.exists(CARDS_PATH):
            with open(CARDS_PATH, "rb") as f:
                blob = f.read()
            if _decrypt_bytes(key, blob) is None:
                bot.send_message(
                    m.chat.id,
                    "❌ Неверная фраза (cards.enc не расшифровался).\n"
                    "/sranger_unlock — попробуй ещё.")
                return

        _master_key = key
        _try_restore_steam_session_async(m.chat.id)
        bot.send_message(m.chat.id, "✅ Разблокировано. /sranger — меню.")
        logger.info("steam_ranger: unlocked OK (returning user)")

    def on_passphrase_confirm(m: Message) -> None:
        global _master_key
        confirm = (m.text or "").strip()
        _clear_state(m)
        try:
            bot.delete_message(m.chat.id, m.message_id)
        except Exception:
            pass

        original = _pending_first_passphrase.pop(m.from_user.id, None)
        if original is None:
            bot.send_message(m.chat.id,
                             "Сессия unlock потерялась. /sranger_unlock — заново.")
            return

        if confirm != original:
            bot.send_message(
                m.chat.id,
                "❌ Фразы не совпали. /sranger_unlock — заново.")
            return

        # Создаём salt, выводим ключ, пишем meta, кладём пустой cards.enc.
        salt = secrets.token_bytes(SALT_BYTES)
        meta = _load_meta()
        meta["salt"] = base64.urlsafe_b64encode(salt).decode("ascii")
        _save_meta(meta)

        _master_key = _derive_key(original, salt)
        # Сразу запишем пустой cards.enc, чтобы повторный unlock мог использовать
        # его как proof-of-key.
        if not os.path.exists(CARDS_PATH):
            _save_cards([])

        bot.send_message(
            m.chat.id,
            "✅ Хранилище создано и разблокировано.\n"
            "Ключ выведен из твоей фразы. Сама фраза нигде не сохранена — "
            "нужно вводить при каждом перезапуске Cardinal.\n\n"
            "/sranger — меню.")
        logger.info("steam_ranger: first unlock completed, salt generated")

    def _try_restore_steam_session_async(chat_id: int) -> None:
        """В фоне пробует загрузить session.enc после unlock. Не блокирует
        ответ пользователю; на ошибку — просто оставляет _steam_session=None."""
        global _steam_session, _steam_login_name

        def _worker():
            global _steam_session, _steam_login_name
            try:
                sess, login = _load_steam_session()
                if sess is not None:
                    _steam_session = sess
                    _steam_login_name = login
                    logger.info(
                        "steam_ranger: session.enc loaded (login=%s)", login)
                    try:
                        bot.send_message(
                            chat_id,
                            f"🔑 Steam-сессия восстановлена: <b>{login}</b>",
                            parse_mode="HTML")
                    except Exception:
                        pass
            except Exception:
                logger.exception(
                    "steam_ranger: restore session in async worker failed")

        threading.Thread(target=_worker, daemon=True,
                         name="srr-restore-session").start()

    # ========================================================================
    # Cards CRUD
    # ========================================================================
    def open_cards(c: CallbackQuery) -> None:
        if not _is_unlocked():
            _ack(c, "🔒 Сначала разблокируй плагин")
            return
        cards = _load_cards()
        lines = ["<b>💳 Сохранённые карты</b>\n"]
        if not cards:
            lines.append("Пусто. Добавь первую ↓")
        else:
            for i, card in enumerate(cards, 1):
                main_mark = " · 🟢 основная" if card.get("is_main") else ""
                country = card.get("country", "?")
                brand = _detect_brand(card.get("number", ""))
                lines.append(
                    f"{i}) {_mask_pan(card.get('number', ''))} "
                    f"({brand}, {country}){main_mark}"
                )
        kb = K()
        for card in cards:
            cid = card["id"]
            kb.add(B(f"⚙️ {_mask_pan(card.get('number', ''))} "
                     f"({card.get('country', '?')})",
                     callback_data=f"{CBT_CARD_DETAIL}:{cid}"))
        kb.add(B("➕ Добавить карту", callback_data=CBT_CARD_ADD))
        kb.add(B("◀️ Назад в меню", callback_data=CBT_OPEN))
        _render(c, "\n".join(lines), kb)
        _ack(c)

    def open_card_detail(c: CallbackQuery) -> None:
        if not _is_unlocked():
            _ack(c, "🔒 Сначала разблокируй плагин")
            return
        try:
            cid = c.data.split(":", 2)[2]
        except IndexError:
            _ack(c, "Битый callback")
            return
        cards = _load_cards()
        card = next((x for x in cards if x.get("id") == cid), None)
        if card is None:
            _ack(c, "Карта не найдена")
            return
        text = (
            f"<b>💳 Карта {_mask_pan(card.get('number', ''))}</b>\n\n"
            f"Бренд: <b>{_detect_brand(card.get('number', ''))}</b>\n"
            f"Срок: <code>{card.get('expiry', '?')}</code>\n"
            f"Имя: <code>{card.get('name', '?')}</code>\n"
            f"Телефон: <code>{card.get('phone', '?')}</code>\n"
            f"Адрес: <code>{card.get('country', '?')}, "
            f"{card.get('city', '?')}, {card.get('street', '?')}, "
            f"{card.get('zip', '?')}</code>\n"
            f"Основная: <b>{'🟢 да' if card.get('is_main') else 'нет'}</b>\n\n"
            f"<i>CVV не хранится — будет запрошен при покупке.</i>"
        )
        kb = K()
        if not card.get("is_main"):
            kb.add(B("🟢 Сделать основной",
                     callback_data=f"{CBT_CARD_MAIN}:{cid}"))
        kb.add(B("🗑 Удалить",
                 callback_data=f"{CBT_CARD_DEL}:{cid}"))
        kb.add(B("◀️ К списку", callback_data=CBT_CARDS))
        _render(c, text, kb)
        _ack(c)

    def card_set_main(c: CallbackQuery) -> None:
        if not _is_unlocked():
            _ack(c, "🔒 Сначала разблокируй плагин")
            return
        try:
            cid = c.data.split(":", 2)[2]
        except IndexError:
            _ack(c, "Битый callback")
            return
        cards = _load_cards()
        if not any(x.get("id") == cid for x in cards):
            _ack(c, "Карта не найдена")
            return
        for x in cards:
            x["is_main"] = (x.get("id") == cid)
        _save_cards(cards)
        _ack(c, "🟢 Назначена основной")
        open_cards(c)

    def card_delete(c: CallbackQuery) -> None:
        if not _is_unlocked():
            _ack(c, "🔒 Сначала разблокируй плагин")
            return
        try:
            cid = c.data.split(":", 2)[2]
        except IndexError:
            _ack(c, "Битый callback")
            return
        cards = _load_cards()
        new = [x for x in cards if x.get("id") != cid]
        if len(new) == len(cards):
            _ack(c, "Карта не найдена")
            return
        # Если удалили основную и осталось хотя бы одно — сделаем первую основной.
        had_main = any(x.get("is_main") for x in cards if x.get("id") == cid)
        if had_main and new and not any(x.get("is_main") for x in new):
            new[0]["is_main"] = True
        _save_cards(new)
        _ack(c, "🗑 Удалено")
        open_cards(c)

    # ---------- Add card: 8-step state machine ----------
    def card_add_start(c: CallbackQuery) -> None:
        if not _is_unlocked():
            _ack(c, "🔒 Сначала разблокируй плагин")
            return
        _card_drafts[c.from_user.id] = {}
        m = bot.send_message(
            c.message.chat.id,
            "<b>➕ Добавить карту (1/8)</b>\n\n"
            "Введи номер карты (13-19 цифр, можно с пробелами):",
            parse_mode="HTML")
        _set_state(m.chat.id, m.message_id, c.from_user.id, ST_CARD_NUMBER)
        _ack(c)

    def _next_step(m: Message, prompt: str, state: str, step: int) -> None:
        m2 = bot.send_message(
            m.chat.id,
            f"<b>➕ Добавить карту ({step}/8)</b>\n\n{prompt}",
            parse_mode="HTML")
        _set_state(m2.chat.id, m2.message_id, m.from_user.id, state)

    def _bad(m: Message, msg: str) -> None:
        bot.send_message(m.chat.id, f"❌ {msg}\nПопробуй ещё или /cancel.")

    def _cancel_card_add(m: Message) -> bool:
        if (m.text or "").strip().lower() in ("/cancel", "отмена", "cancel"):
            _clear_state(m)
            _card_drafts.pop(m.from_user.id, None)
            bot.send_message(m.chat.id, "🚫 Добавление отменено.")
            return True
        return False

    def on_card_number(m: Message) -> None:
        if _cancel_card_add(m):
            return
        v = _validate_pan(m.text or "")
        if v is None:
            _bad(m, "Нужно 13-19 цифр.")
            return
        _card_drafts.setdefault(m.from_user.id, {})["number"] = v
        _clear_state(m)
        _next_step(m, "Срок (формат <code>ММ/ГГ</code>, например <code>03/28</code>):",
                   ST_CARD_EXPIRY, 2)

    def on_card_expiry(m: Message) -> None:
        if _cancel_card_add(m):
            return
        v = _validate_expiry(m.text or "")
        if v is None:
            _bad(m, "Формат ММ/ГГ, ММ от 01 до 12.")
            return
        _card_drafts.setdefault(m.from_user.id, {})["expiry"] = v
        _clear_state(m)
        _next_step(m, "Имя держателя (латиница, как на карте):",
                   ST_CARD_NAME, 3)

    def on_card_name(m: Message) -> None:
        if _cancel_card_add(m):
            return
        v = _validate_nonempty(m.text or "", max_len=64)
        if v is None:
            _bad(m, "Имя пустое или слишком длинное (>64).")
            return
        _card_drafts.setdefault(m.from_user.id, {})["name"] = v.upper()
        _clear_state(m)
        _next_step(m, "Телефон в международном формате, "
                   "например <code>+77001234567</code>:",
                   ST_CARD_PHONE, 4)

    def on_card_phone(m: Message) -> None:
        if _cancel_card_add(m):
            return
        v = _validate_phone(m.text or "")
        if v is None:
            _bad(m, "Должен начинаться с + и содержать 7-15 цифр.")
            return
        _card_drafts.setdefault(m.from_user.id, {})["phone"] = v
        _clear_state(m)
        _next_step(m, "Страна (например <code>Kazakhstan</code>):",
                   ST_CARD_COUNTRY, 5)

    def on_card_country(m: Message) -> None:
        if _cancel_card_add(m):
            return
        v = _validate_nonempty(m.text or "", max_len=64)
        if v is None:
            _bad(m, "Страна пустая или слишком длинная.")
            return
        _card_drafts.setdefault(m.from_user.id, {})["country"] = v
        _clear_state(m)
        _next_step(m, "Город:", ST_CARD_CITY, 6)

    def on_card_city(m: Message) -> None:
        if _cancel_card_add(m):
            return
        v = _validate_nonempty(m.text or "", max_len=64)
        if v is None:
            _bad(m, "Город пустой или слишком длинный.")
            return
        _card_drafts.setdefault(m.from_user.id, {})["city"] = v
        _clear_state(m)
        _next_step(m, "Улица + дом:", ST_CARD_STREET, 7)

    def on_card_street(m: Message) -> None:
        if _cancel_card_add(m):
            return
        v = _validate_nonempty(m.text or "", max_len=128)
        if v is None:
            _bad(m, "Адрес пустой или слишком длинный.")
            return
        _card_drafts.setdefault(m.from_user.id, {})["street"] = v
        _clear_state(m)
        _next_step(m, "Почтовый индекс:", ST_CARD_ZIP, 8)

    def on_card_zip(m: Message) -> None:
        if _cancel_card_add(m):
            return
        v = _validate_nonempty(m.text or "", max_len=16)
        if v is None:
            _bad(m, "Индекс пустой или слишком длинный.")
            return
        _card_drafts.setdefault(m.from_user.id, {})["zip"] = v
        _clear_state(m)
        # Финальный шаг: спрашиваем, делать ли основной.
        kb = K()
        kb.row(
            B("✅ Сделать основной", callback_data=CBT_CARD_ADD_MAIN_YES),
            B("❌ Не основной", callback_data=CBT_CARD_ADD_MAIN_NO),
        )
        bot.send_message(
            m.chat.id,
            "<b>Готово! 🎉</b>\nСделать эту карту основной (используется по умолчанию для покупок)?",
            parse_mode="HTML",
            reply_markup=kb)

    def _finalize_card(c: CallbackQuery, is_main: bool) -> None:
        if not _is_unlocked():
            _ack(c, "🔒 Сначала разблокируй плагин")
            return
        draft = _card_drafts.pop(c.from_user.id, None)
        if not draft:
            _ack(c, "Драфт потерялся, начни сначала")
            open_cards(c)
            return
        cards = _load_cards()
        new_card = {
            "id": _new_card_id(),
            "number": draft["number"],
            "expiry": draft["expiry"],
            "name": draft["name"],
            "phone": draft["phone"],
            "country": draft["country"],
            "city": draft["city"],
            "street": draft["street"],
            "zip": draft["zip"],
            "is_main": False,
        }
        if is_main:
            for x in cards:
                x["is_main"] = False
            new_card["is_main"] = True
        elif not cards:
            # первая карта — авто-main, иначе нечего использовать для _one_cycle
            new_card["is_main"] = True
        cards.append(new_card)
        _save_cards(cards)
        _ack(c, "✅ Карта добавлена")
        open_cards(c)

    def card_add_main_yes(c: CallbackQuery) -> None:
        _finalize_card(c, True)

    def card_add_main_no(c: CallbackQuery) -> None:
        _finalize_card(c, False)

    # ========================================================================
    # Регистрация хендлеров
    # ========================================================================
    # ---------- покупательский поток (меню) ----------
    def open_buyer(c: CallbackQuery) -> None:
        _render(c, _buyer_menu_text(), _buyer_menu_kb())
        _ack(c)

    def buyer_toggle(c: CallbackQuery) -> None:
        cfg2 = _load_config()
        cfg2["buyer_flow_enabled"] = not cfg2.get("buyer_flow_enabled")
        _save_config(cfg2)
        _render(c, _buyer_menu_text(), _buyer_menu_kb())
        _ack(c, "ок")

    def buyer_cvv(c: CallbackQuery) -> None:
        _set_state(c.message.chat.id, c.message.id, c.from_user.id, ST_BUYER_CVV)
        bot.send_message(c.message.chat.id,
                         "Пришли CVV операторской карты (3-4 цифры). Сообщение удалю.")
        _ack(c)

    def buyer_map_add(c: CallbackQuery) -> None:
        _set_state(c.message.chat.id, c.message.id, c.from_user.id, ST_BUYER_MAP_LOT)
        bot.send_message(c.message.chat.id,
                         "Пришли <b>lot_id</b> FunPay (число из ссылки offers/&lt;N&gt;) "
                         "<b>или ключевое слово</b> из названия лота (например "
                         "<code>turkey</code> или <code>аккаунт TR</code>):",
                         parse_mode="HTML")
        _ack(c)

    def buyer_map_pick(c: CallbackQuery) -> None:
        cc = (c.data or "").split(":")[-1]
        lid = _buyer_map_pending.pop(c.from_user.id, None)
        if not lid:
            _ack(c, "Сессия привязки потеряна")
            return
        cfg2 = _load_config()
        ok, msg = _buyer_map_add(cfg2, lid, cc)
        if ok:
            _save_config(cfg2)
        _render(c, _buyer_menu_text(), _buyer_menu_kb())
        _ack(c, msg)

    def buyer_map_rm(c: CallbackQuery) -> None:
        lid = (c.data or "")[len(CBT_BUYER_MAP_RM) + 1:]
        cfg2 = _load_config()
        _buyer_map_remove(cfg2, lid)
        _save_config(cfg2)
        _render(c, _buyer_menu_text(), _buyer_menu_kb())
        _ack(c, "удалено")

    def on_buyer_cvv(m: Message) -> None:
        global _cvv_in_memory
        code = (m.text or "").strip()
        _clear_state(m)
        if code.isdigit() and 3 <= len(code) <= 4:
            _cvv_in_memory = code
            try:
                bot.delete_message(m.chat.id, m.message_id)
            except Exception:
                pass
            bot.send_message(m.chat.id, "✅ CVV сохранён в памяти (не на диске).",
                             reply_markup=_kb_back())
        else:
            bot.send_message(m.chat.id, "❌ CVV — 3-4 цифры.", reply_markup=_kb_back())

    def on_buyer_map_lot(m: Message) -> None:
        lid = (m.text or "").strip()
        _clear_state(m)
        if not lid:
            bot.send_message(m.chat.id, "Пустой ключ (lot_id или название).", reply_markup=_kb_back())
            return
        _buyer_map_pending[m.from_user.id] = lid
        kb = K()
        for code, lbl in REGIONS:
            kb.add(B(lbl, callback_data=f"{CBT_BUYER_MAP_PICK}:{code}"))
        kb.add(B("◀️ Отмена", callback_data=CBT_BUYER_MENU))
        kind = "Лот" if lid.isdigit() else "Ключевое слово"
        bot.send_message(m.chat.id, f"{kind} <code>{lid}</code> — выбери целевой регион:",
                         reply_markup=kb, parse_mode="HTML")

    tg.cbq_handler(open_main, _cb(CBT_OPEN))
    tg.cbq_handler(toggle_running, _cb(CBT_TOGGLE_RUN))
    tg.cbq_handler(pick_region, _cb(CBT_REGION_PICK))
    tg.cbq_handler(open_region, _cb(CBT_REGION))
    tg.cbq_handler(open_buyer, _cb(CBT_BUYER_MENU))
    tg.cbq_handler(buyer_toggle, _cb(CBT_BUYER_TOGGLE))
    tg.cbq_handler(buyer_cvv, _cb(CBT_BUYER_CVV))
    tg.cbq_handler(buyer_map_add, _cb(CBT_BUYER_MAP_ADD))
    tg.cbq_handler(buyer_map_pick, _cb(CBT_BUYER_MAP_PICK))
    tg.cbq_handler(buyer_map_rm, _cb(CBT_BUYER_MAP_RM))
    tg.msg_handler(on_buyer_cvv, func=lambda m: _state_eq(m, ST_BUYER_CVV))
    tg.msg_handler(on_buyer_map_lot, func=lambda m: _state_eq(m, ST_BUYER_MAP_LOT))
    tg.cbq_handler(open_login, _cb(CBT_LOGIN))
    tg.cbq_handler(do_logout, _cb(CBT_LOGOUT))
    # порядок важен: сначала более специфичные префиксы, потом CBT_CARDS
    tg.cbq_handler(card_add_main_yes, _cb(CBT_CARD_ADD_MAIN_YES))
    tg.cbq_handler(card_add_main_no, _cb(CBT_CARD_ADD_MAIN_NO))
    tg.cbq_handler(card_add_start, _cb(CBT_CARD_ADD))
    tg.cbq_handler(card_set_main, _cb(CBT_CARD_MAIN))
    tg.cbq_handler(card_delete, _cb(CBT_CARD_DEL))
    tg.cbq_handler(open_card_detail, _cb(CBT_CARD_DETAIL))
    tg.cbq_handler(open_cards, _cb(CBT_CARDS))
    # Phase 3: proxy callbacks. Порядок: специфичные префиксы до CBT_PROXY.
    tg.cbq_handler(proxy_add_start, _cb(CBT_PROXY_ADD))
    tg.cbq_handler(proxy_recheck_all, _cb(CBT_PROXY_RECHECK))
    tg.cbq_handler(proxy_delete, _cb(CBT_PROXY_DEL))
    tg.cbq_handler(open_proxy, _cb(CBT_PROXY))
    tg.cbq_handler(open_status, _cb(CBT_STATUS))
    # Phase 5: refresh-кнопка имеет отдельный префикс CBT_PURCHASE_REFRESH
    tg.cbq_handler(refresh_purchase_search, _cb(CBT_PURCHASE_REFRESH))
    # Phase 6: «купить» / dry-run / 3DS
    tg.cbq_handler(start_purchase, _cb(CBT_PURCHASE_BUY))
    tg.cbq_handler(toggle_dry_run, _cb(CBT_DRY_RUN_TOGGLE))
    tg.cbq_handler(confirm_3ds, _cb(CBT_3DS_DONE))
    tg.cbq_handler(cancel_3ds, _cb(CBT_3DS_CANCEL))
    tg.cbq_handler(lambda c: _confirm_buyer_3ds(cardinal, c), _cb(CBT_BUYER_3DS))
    tg.cbq_handler(lambda c: _cancel_buyer_3ds(cardinal, c), _cb(CBT_BUYER_3DS_CANCEL))
    # Phase 8: ручной список игр (callbacks register order: специфичные первыми)
    tg.cbq_handler(manual_game_add_start, _cb(CBT_MGAME_ADD))
    tg.cbq_handler(manual_game_refresh, _cb(CBT_MGAME_REFRESH))
    tg.cbq_handler(manual_game_delete, _cb(CBT_MGAME_DEL))
    tg.cbq_handler(manual_toggle_prefer, _cb(CBT_MGAME_TOGGLE_PREFER))
    tg.cbq_handler(open_manual_games, _cb(CBT_MANUAL_GAMES))
    tg.cbq_handler(open_purchase_once, _cb(CBT_PURCHASE_ONCE))
    tg.cbq_handler(open_unlock, _cb(CBT_UNLOCK))
    tg.cbq_handler(open_more, _cb(CBT_MORE))
    tg.cbq_handler(open_guide, _cb(CBT_GUIDE))
    # v1.8.0
    tg.cbq_handler(preflight_handler, _cb(CBT_PREFLIGHT))
    tg.cbq_handler(toggle_region_notify, _cb(CBT_TOGGLE_REGION_NOTIFY))
    tg.cbq_handler(ask_region_interval, _cb(CBT_EDIT_REGION_INTERVAL))

    # state-driven message handlers
    tg.msg_handler(on_passphrase,         func=lambda m: _state_eq(m, ST_ASK_PASSPHRASE))
    tg.msg_handler(on_passphrase_confirm, func=lambda m: _state_eq(m, ST_ASK_PASSPHRASE_CONFIRM))
    tg.msg_handler(on_card_number,        func=lambda m: _state_eq(m, ST_CARD_NUMBER))
    tg.msg_handler(on_card_expiry,        func=lambda m: _state_eq(m, ST_CARD_EXPIRY))
    tg.msg_handler(on_card_name,          func=lambda m: _state_eq(m, ST_CARD_NAME))
    tg.msg_handler(on_card_phone,         func=lambda m: _state_eq(m, ST_CARD_PHONE))
    tg.msg_handler(on_card_country,       func=lambda m: _state_eq(m, ST_CARD_COUNTRY))
    tg.msg_handler(on_card_city,          func=lambda m: _state_eq(m, ST_CARD_CITY))
    tg.msg_handler(on_card_street,        func=lambda m: _state_eq(m, ST_CARD_STREET))
    tg.msg_handler(on_region_interval,    func=lambda m: _state_eq(m, ST_ASK_REGION_INTERVAL))
    tg.msg_handler(on_card_zip,           func=lambda m: _state_eq(m, ST_CARD_ZIP))
    # Phase 3
    tg.msg_handler(on_add_proxy,          func=lambda m: _state_eq(m, ST_ADD_PROXY))
    # Phase 4: Steam login
    tg.msg_handler(on_login_creds,        func=lambda m: _state_eq(m, ST_LOGIN_CREDS))
    tg.msg_handler(on_login_code,         func=lambda m: _state_eq(m, ST_LOGIN_CODE))
    # Phase 6: CVV перед покупкой
    tg.msg_handler(on_cvv,                func=lambda m: _state_eq(m, ST_ASK_CVV))
    # Phase 7: CVV для автоцикла
    tg.msg_handler(on_cvv_auto,           func=lambda m: _state_eq(m, ST_ASK_CVV_AUTO))
    # Phase 8: appid для ручного списка
    tg.msg_handler(on_appid,              func=lambda m: _state_eq(m, ST_ADD_APPID))

    # ---------- /sranger ----------
    def cmd_open(m: Message) -> None:
        bot.send_message(m.chat.id, _main_text(),
                         reply_markup=_kb_main(), parse_mode="HTML")

    tg.msg_handler(cmd_open, commands=["sranger"])
    tg.msg_handler(cmd_unlock, commands=["sranger_unlock"])

    # ---------- /sranger_buyer — покупательский поток: статус/маппинг/тоггл ----
    def cmd_buyer(m: Message) -> None:
        global _cvv_in_memory
        cfg = _load_config()
        parts = (m.text or "").split()
        args = parts[1:]
        if args and args[0].lower() in ("on", "off"):
            cfg["buyer_flow_enabled"] = (args[0].lower() == "on")
            _save_config(cfg)
        elif args and args[0].lower() == "cvv" and len(args) >= 2:
            code = args[1].strip()
            if code.isdigit() and 3 <= len(code) <= 4:
                _cvv_in_memory = code
                try:
                    bot.delete_message(m.chat.id, m.message_id)
                except Exception:
                    pass
                bot.send_message(m.chat.id, "✅ CVV сохранён в памяти (не на диске).")
            else:
                bot.send_message(m.chat.id, "❌ CVV — 3-4 цифры.")
            return
        elif args and args[0].lower() in ("del", "rm", "-") and len(args) >= 2:
            removed = _buyer_map_remove(cfg, args[1])
            _save_config(cfg)
            bot.send_message(m.chat.id, ("🗑 Удалено." if removed else "Не найдено."))
            return
        elif len(args) >= 2:
            ok, msg = _buyer_map_add(cfg, args[0], args[1])
            if ok:
                _save_config(cfg)
            bot.send_message(m.chat.id, ("✅ " if ok else "❌ ") + msg)
            return
        # статус
        m_map = cfg.get("lot_region_map") or {}
        on = "🟢 ВКЛ" if cfg.get("buyer_flow_enabled") else "🔴 ВЫКЛ"
        cards_n = len(_load_cards())
        lines = [
            f"🛒 <b>Покупательский поток</b>: {on}",
            f"Карт оператора: {cards_n} · CVV в памяти: {'да' if (_autocycle_cvv or _cvv_in_memory) else 'нет'}",
            f"dry-run покупок: {'да' if cfg.get('dry_run_purchases', True) else 'НЕТ (боевой)'}",
            "",
            "<b>Лот / название → регион:</b>",
        ]
        lines += [f"  • <code>{lid}</code> → {cc}" for lid, cc in m_map.items()] or ["  (пусто)"]
        lines += [
            "",
            "Команды:",
            "  /sranger_buyer on | off",
            "  /sranger_buyer cvv &lt;код&gt;  — CVV операторской карты в память",
            "  /sranger_buyer &lt;lot_id&gt; &lt;CC&gt;  — добавить",
            "  /sranger_buyer del &lt;lot_id&gt;  — удалить",
        ]
        bot.send_message(m.chat.id, "\n".join(lines), parse_mode="HTML")

    tg.msg_handler(cmd_buyer, commands=["sranger_buyer"])

    # ---------- /sranger_guide ----------
    _GUIDE_TEXT = (
        "<b>📖 Steam Region Ranger — гайд</b>\n\n"
        "<b>Что это:</b> плагин для смены региона Steam через покупку самой "
        "дешёвой игры в магазине целевого региона.\n\n"
        f"<b>Текущая версия:</b> v{VERSION}.\n\n"
        "<b>Команды:</b>\n"
        "  /sranger — открыть меню\n"
        "  /sranger_guide — этот гайд\n\n"
        "<b>Первый запуск:</b>\n"
        "1) /sranger → 💳 Карты → ➕ Добавить — заполни 8 шагов.\n"
        "2) Прокси → Steam-логин → 🔄 Разовая покупка.\n\n"
        "<b>Хранение данных (v1.9.0):</b>\n"
        "• Шифрование убрано — карты/прокси/Steam-сессия лежат на диске "
        "в <b>открытом виде</b> (JSON в <code>storage/plugins/steam_ranger/</code>). "
        "Мастер-пароль больше не нужен, ничего не блокируется после рестарта.\n"
        "• <b>CVV нигде не хранится</b> — будет запрашиваться перед "
        "каждой покупкой.\n"
        "• ⚠️ Обеспечь доступ к серверу/бэкапам сам — файлы с картами не защищены.\n\n"
        "<b>Что готово (этот релиз):</b>\n"
        "  ✅ Меню, регион, статус\n"
        "  ✅ CRUD карт (без CVV)\n"
        "  ✅ Прокси-пул с автодетектом страны через ip-api\n"
        "  ✅ Steam-логин с Guard-кодом из чата (email/mobile)\n"
        "  ✅ Поиск самой дешёвой игры через Steam Store\n"
        "  ✅ Покупочный pipeline (addtocart→init→3DS→finalize)\n"
        "  ✅ Автоцикл (Start/Stop, авто-стоп по совпадению региона)\n"
        "  ✅ Ручной список игр per-region (📋 Свой список)\n\n"
        "<b>⚠️ DRY_RUN включён по умолчанию.</b> Pipeline проходит, но "
        "реальные init/finalize пропускаются. Чтобы запустить боевой "
        "режим — нажми кнопку <code>🧪 Dry-run покупок: ВКЛ</code> в "
        "главном меню или выстави <code>dry_run_purchases: false</code> "
        "в <code>storage/plugins/steam_ranger/config.json</code>.\n\n"
        "<b>Steam web-purchase — fragile:</b>\n"
        "Endpoints /cart/addtocart/, /checkout/inittransaction/, "
        "/finalizetransaction/ не задокументированы Valve. Поведение "
        "может меняться без уведомления. После первого боевого прогона "
        "смотри логи Cardinal на 'inittransaction response' / "
        "'finalize response' — там будут реальные ответы Steam.\n\n"
        "<b>Автоцикл</b> (Phase 7):\n"
        "• Запуск: 🟢 Start → ввод CVV (только в RAM).\n"
        "• Останов: при совпадении региона аккаунта с целевым ИЛИ "
        f"{AUTOCYCLE_PAUSE_MIN}-{AUTOCYCLE_PAUSE_MAX}с пауза × "
        f"{AUTOCYCLE_MAX_FAILURES} неудач подряд ИЛИ кнопка 🔴 Stop.\n"
        "• 3DS в автоцикле НЕ обрабатывается — для 3DS используй "
        "ручную «Разовая покупка».\n\n"
        "<b>Свой список игр</b> (Phase 8):\n"
        "• <code>📋 Свой список игр для &lt;region&gt;</code> в главном меню.\n"
        "• Добавь appid (число или Steam URL) — цена/имя подтянутся "
        "из <code>/api/appdetails</code>.\n"
        "• Если для текущего региона есть хоть одна ручная игра — "
        "pipeline берёт её (cheapest first), иначе fallback на автопоиск.\n"
        "• Тогл «🔁 Использовать только автопоиск» отключает приоритет "
        "ручного списка (флаг <code>prefer_manual_list</code> в config.json)."
    )

    def cmd_guide(m: Message) -> None:
        try:
            bot.send_message(m.chat.id, _GUIDE_TEXT, parse_mode="HTML")
        except Exception:
            logger.exception("steam_ranger: cmd_guide failed")

    tg.msg_handler(cmd_guide, commands=["sranger_guide"])

    try:
        cardinal.add_telegram_commands(UUID, [
            ("sranger", "Steam Region Ranger: меню", True),
            ("sranger_buyer", "Steam Region Ranger: покупательский поток", True),
            ("sranger_guide", "Steam Region Ranger: гайд", True),
        ])
    except Exception:
        logger.exception("steam_ranger: не удалось зарегистрировать команды")

    # 💛 Донат-баннер (защита реквизитов автора)
    global _donation_cardinal
    _donation_cardinal = cardinal
    try:
        tg.cbq_handler(
            _donation_on_cb,
            lambda c: (c.data or "").startswith(DONATION_CALLBACK_PREFIX + ":"))
        _start_donation_reminder(cardinal)
    except Exception:
        logger.debug("donation banner register failed", exc_info=True)
    # 📦 Одноразовое приветствие с рекламой канала автора
    if DONATION_SHOW_ON_START:
        try:
            _send_startup_welcome(cardinal)
        except Exception:
            logger.debug("startup welcome send failed", exc_info=True)


    logger.info("steam_ranger: инициализирован (v%s).", VERSION)


# ---------- FPC settings page ----------
def _open_settings_page(cardinal: "Cardinal", msg: Any) -> None:
    tg = getattr(cardinal, "telegram", None)
    if tg is None:
        return
    tg.bot.send_message(
        msg.chat.id,
        f"<b>Steam Region Ranger</b> v{VERSION}\n\n"
        "Команды:\n"
        "  /sranger — меню\n"
        "  /sranger_guide — гайд",
        parse_mode="HTML")


# ---------- выгрузка плагина ----------
def _on_delete(cardinal: "Cardinal", *_: Any) -> None:
    global _master_key, _steam_session, _steam_login_name, _autocycle_cvv, _autocycle_thread
    # Останавливаем автоцикл, если жив.
    _autocycle_stop.set()
    _autocycle_cvv = None
    _autocycle_thread = None
    _region_watch_stop.set()
    _master_key = None
    _steam_session = None
    _steam_login_name = None
    _card_drafts.clear()
    _pending_first_passphrase.clear()
    _pending_logins.clear()
    with _purchase_lock:
        _pending_purchases.clear()
    # Сбросим running в config (при следующем загрузке — чисто).
    try:
        cfg = _load_config()
        if cfg.get("running"):
            cfg["running"] = False
            _save_config(cfg)
    except Exception:
        pass
    logger.info("steam_ranger: выгружен, master-key/сессия/автоцикл очищены.")


BIND_TO_PRE_INIT = [_init]
BIND_TO_DELETE = _on_delete


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
BIND_TO_SETTINGS_PAGE = _open_settings_page


# ============================================================================
# v1.10.0 — покупательский поток FunPay: шаблоны, хендлеры, воркеры
# ============================================================================

BUYER_TEMPLATES: dict[str, str] = {
    "tpl_ask_creds": (
        "Для смены региона Steam пришлите в этот чат логин и пароль одним сообщением:\n"
        "Логин: ваш_логин\nПароль: ваш_пароль\n\n"
        "⚠️ Условия: на аккаунте должен быть доступен Steam Guard (код с почты или "
        "мобильного) и не должно быть активного лимита Steam на смену региона — иначе "
        "заказ вернём."
    ),
    "tpl_creds_format_error": (
        "Не разобрал логин и пароль. Пришлите так:\nЛогин: ваш_логин\nПароль: ваш_пароль\n"
        "(можно и одной строкой: логин пароль)"
    ),
    "tpl_ask_guard": "Steam запросил код подтверждения. Пришлите код Steam Guard одним сообщением.",
    "tpl_bad_creds": "Steam не принял логин/пароль. Проверьте и пришлите ещё раз: Логин: … Пароль: …",
    "tpl_bad_code": "Код не подошёл. Пришлите код Steam Guard ещё раз.",
    "tpl_attempts_exhausted": "Не удалось продолжить. Передаю заказ продавцу — он свяжется с вами.",
    "tpl_processing": "Принято, выполняю смену региона. Это займёт пару минут…",
    "tpl_success": "✅ Готово! Регион аккаунта изменён на {region}.",
    "tpl_fail_generic": "Не удалось выполнить смену региона. Продавец уведомлён и свяжется с вами.",
    "tpl_no_proxy_delay": "Сейчас не получается обработать заказ (нет канала под регион). Продавец уведомлён, ожидайте.",
    "tpl_dry_run": "🧪 Тестовый прогон выполнен (без реальной оплаты). Регион не менялся.",
}


def _btpl(cfg: dict, key: str, **kw: Any) -> str:
    txt = cfg.get(key) or BUYER_TEMPLATES.get(key, key)
    try:
        return txt.format(**kw)
    except Exception:
        return txt


def _buyer_send(cardinal: "Cardinal", sess: "OrderSession", key: str, **kw: Any) -> None:
    cfg = _load_config()
    try:
        cardinal.send_message(sess.chat_id, _btpl(cfg, key, region=sess.region, **kw), sess.buyer)
    except Exception:
        logger.warning("steam_ranger: buyer send failed (order %s)", sess.order_id, exc_info=True)


def _notify_operator(cardinal: "Cardinal", text: str) -> None:
    cfg = _load_config()
    tg = getattr(cardinal, "telegram", None)
    bot = getattr(tg, "bot", None) if tg else None
    chat_id = _resolve_operator_chat(cardinal, cfg)
    if bot is None or not chat_id:
        return
    try:
        bot.send_message(chat_id, text, parse_mode="HTML")
    except Exception:
        logger.debug("steam_ranger: operator notify failed", exc_info=True)


def _order_ids(cardinal: "Cardinal", order: Any) -> list[str]:
    """Кандидаты-идентификаторы лота из заказа: lot_id / subcategory_id, в т.ч. из
    полного заказа (`cardinal.get_order_from_object`). Оператор маппит любой из них.
    (Сверено с рабочим steam_rental: event.order часто без lot_id.)"""
    full = None
    try:
        getf = getattr(cardinal, "get_order_from_object", None)
        if getf is not None:
            full = getf(order)
    except Exception:
        full = None
    out: list[str] = []
    seen: set[str] = set()
    for src in (order, full):
        if src is None:
            continue
        for attr in ("lot_id", "subcategory_id"):
            v = getattr(src, attr, None)
            if v and str(v) not in seen:
                seen.add(str(v))
                out.append(str(v))
    return out


def _order_text(cardinal: "Cardinal", order: Any) -> str:
    """Собирает текст заказа (название + описание) из event.order и полного
    заказа (`cardinal.get_order_from_object`) — для связки лота по названию."""
    full = None
    try:
        getf = getattr(cardinal, "get_order_from_object", None)
        if getf is not None:
            full = getf(order)
    except Exception:
        full = None
    parts: list[str] = []
    for src in (order, full):
        if src is None:
            continue
        for attr in ("title", "description", "full_description"):
            v = getattr(src, attr, None)
            if v:
                parts.append(str(v))
    return " ".join(parts)


def _select_operator_card(cfg: dict) -> Optional[dict]:
    cards = _load_cards()
    if not cards:
        return None
    cid = cfg.get("region_card_id")
    if cid:
        c = next((c for c in cards if str(c.get("id")) == str(cid)), None)
        if c:
            return c
    return next((c for c in cards if c.get("is_main")), None) or cards[0]


def _login_result_kind(result: str) -> str:
    """begin()/submit_code() → {ok, need_code, bad_creds}. 'error' трактуем как
    bad_creds (даём покупателю повтор; на капче попытки просто исчерпаются)."""
    if result == "ok":
        return "ok"
    if result == "need_code":
        return "need_code"
    return "bad_creds"


def on_new_order(cardinal: "Cardinal", event: Any, *args: Any) -> None:
    """BIND_TO_NEW_ORDER: запуск покупательского потока по «региональному» лоту."""
    cfg = _load_config()
    if not cfg.get("buyer_flow_enabled"):
        return
    order = getattr(event, "order", None) or event
    # фильтр статуса как в рабочем steam_rental (в тестах FunPayAPI нет → пропускаем)
    try:
        from FunPayAPI.common.enums import OrderStatuses
        if getattr(order, "status", None) not in (OrderStatuses.PAID,):
            return
    except Exception:
        pass
    lot_map = cfg.get("lot_region_map") or {}
    region = None
    for cid in _order_ids(cardinal, order):
        region = _resolve_order_region(cid, lot_map)
        if region:
            break
    # фоллбэк: связка по названию лота (как steam_rental._match_lot)
    if not region:
        region = _resolve_region_by_name(_order_text(cardinal, order), lot_map)
    if not region:
        return
    order_id = getattr(order, "id", None)
    chat_id = getattr(order, "chat_id", None)
    buyer = getattr(order, "buyer_username", "") or ""
    if order_id is None or chat_id is None:
        return
    sess, created = _create_session(order_id, chat_id, buyer, region,
                                    int(cfg.get("buyer_step_timeout_sec", 600)))
    if not created:
        return
    logger.info("steam_ranger: buyer order %s → регион %s", sess.order_id, region)
    _buyer_send(cardinal, sess, "tpl_ask_creds")


def on_buyer_message(cardinal: "Cardinal", event: Any, *args: Any) -> None:
    """BIND_TO_NEW_MESSAGE: приём логина/пароля и Guard-кода от покупателя."""
    cfg = _load_config()
    if not cfg.get("buyer_flow_enabled"):
        return
    msg = getattr(event, "message", None) or event
    acc = getattr(cardinal, "account", None)
    if acc is not None and getattr(msg, "author_id", None) == getattr(acc, "id", None):
        return  # своё сообщение продавца — игнор
    chat_id = getattr(msg, "chat_id", None) or getattr(msg, "node_id", None)
    text = getattr(msg, "text", None) or ""
    author = getattr(msg, "author", None)
    if chat_id is None or not text.strip():
        return
    sess = _get_session_for_chat(chat_id, author)
    if sess is None:
        return
    timeout = int(cfg.get("buyer_step_timeout_sec", 600))
    if _is_expired(time.time(), sess.deadline):
        _buyer_send(cardinal, sess, "tpl_attempts_exhausted")
        _notify_operator(cardinal, f"⌛ Заказ <code>{sess.order_id}</code>: таймаут ожидания покупателя.")
        _close_session(sess.order_id, "timeout")
        return

    if sess.step == STEP_AWAIT_CREDS:
        creds = _validate_login_password(text)
        if creds is None:
            sess.cred_attempts += 1
            if _step_after_creds(False, sess.cred_attempts,
                                 int(cfg.get("buyer_cred_attempts", 3))) == "retry":
                sess.deadline = time.time() + timeout
                _buyer_send(cardinal, sess, "tpl_creds_format_error")
            else:
                _buyer_send(cardinal, sess, "tpl_attempts_exhausted")
                _notify_operator(cardinal, f"❌ Заказ <code>{sess.order_id}</code>: исчерпаны попытки ввода логина/пароля.")
                _close_session(sess.order_id, "creds attempts")
            return
        sess.login, sess.password = creds
        sess.step = STEP_BUSY
        threading.Thread(target=_process_login, args=(cardinal, sess), daemon=True,
                         name="srr-buyer-login").start()
        return

    if sess.step == STEP_AWAIT_GUARD:
        code = _validate_guard_code(text)
        if code is None:
            sess.code_attempts += 1
            if _step_after_code("bad_code", sess.code_attempts,
                                int(cfg.get("buyer_code_attempts", 3))) == "retry":
                _buyer_send(cardinal, sess, "tpl_bad_code")
            else:
                _buyer_send(cardinal, sess, "tpl_attempts_exhausted")
                _notify_operator(cardinal, f"❌ Заказ <code>{sess.order_id}</code>: исчерпаны попытки Guard-кода.")
                _close_session(sess.order_id, "code attempts")
            return
        sess.step = STEP_BUSY
        threading.Thread(target=_process_guard, args=(cardinal, sess, code), daemon=True,
                         name="srr-buyer-guard").start()
        return
    # STEP_BUSY — идёт обработка, входящее игнорируем


def _process_login(cardinal: "Cardinal", sess: "OrderSession") -> None:
    cfg = _load_config()
    timeout = int(cfg.get("buyer_step_timeout_sec", 600))
    proxy = _pick_proxy_for_region(sess.region)
    if not proxy:
        _buyer_send(cardinal, sess, "tpl_no_proxy_delay")
        _notify_operator(cardinal, f"❌ Заказ <code>{sess.order_id}</code>: нет живого прокси под {sess.region}.")
        _close_session(sess.order_id, "no proxy")
        return
    sess.proxy = proxy
    try:
        sess.steam = SteamInteractiveSession(sess.login, sess.password, proxy=proxy)
        result = sess.steam.begin()
    except Exception:
        logger.exception("steam_ranger: buyer login failed (order %s)", sess.order_id)
        result = "error"
    kind = _login_result_kind(result)
    if kind == "ok":
        _process_purchase(cardinal, sess)
        return
    if kind == "need_code":
        sess.step = STEP_AWAIT_GUARD
        sess.deadline = time.time() + timeout
        _buyer_send(cardinal, sess, "tpl_ask_guard")
        return
    # bad_creds
    sess.cred_attempts += 1
    if _step_after_login("bad_creds", sess.cred_attempts,
                         int(cfg.get("buyer_cred_attempts", 3))) == "retry_creds":
        sess.login = None
        sess.password = None
        sess.steam = None
        sess.step = STEP_AWAIT_CREDS
        sess.deadline = time.time() + timeout
        _buyer_send(cardinal, sess, "tpl_bad_creds")
    else:
        _buyer_send(cardinal, sess, "tpl_attempts_exhausted")
        _notify_operator(cardinal, f"❌ Заказ <code>{sess.order_id}</code>: вход не удался (лимит попыток).")
        _close_session(sess.order_id, "login failed")


def _process_guard(cardinal: "Cardinal", sess: "OrderSession", code: str) -> None:
    cfg = _load_config()
    timeout = int(cfg.get("buyer_step_timeout_sec", 600))
    try:
        result = sess.steam.submit_code(code) if sess.steam else "error"
    except Exception:
        logger.exception("steam_ranger: buyer guard failed (order %s)", sess.order_id)
        result = "error"
    if result == "ok":
        _process_purchase(cardinal, sess)
        return
    sess.code_attempts += 1
    if _step_after_code("bad_code", sess.code_attempts,
                        int(cfg.get("buyer_code_attempts", 3))) == "retry":
        sess.step = STEP_AWAIT_GUARD
        sess.deadline = time.time() + timeout
        _buyer_send(cardinal, sess, "tpl_bad_code")
    else:
        _buyer_send(cardinal, sess, "tpl_attempts_exhausted")
        _notify_operator(cardinal, f"❌ Заказ <code>{sess.order_id}</code>: Guard-код не принят (лимит).")
        _close_session(sess.order_id, "guard failed")


def _process_purchase(cardinal: "Cardinal", sess: "OrderSession") -> None:
    cfg = _load_config()
    _buyer_send(cardinal, sess, "tpl_processing")
    card = _select_operator_card(cfg)
    if not card:
        _buyer_send(cardinal, sess, "tpl_fail_generic")
        _notify_operator(cardinal, f"❌ Заказ <code>{sess.order_id}</code>: нет операторской карты для оплаты.")
        _close_session(sess.order_id, "no card")
        return
    cvv = _autocycle_cvv or _cvv_in_memory
    if not cvv:
        _buyer_send(cardinal, sess, "tpl_fail_generic")
        _notify_operator(cardinal, f"❌ Заказ <code>{sess.order_id}</code>: нет CVV в памяти (задайте через автоцикл/меню).")
        _close_session(sess.order_id, "no cvv")
        return
    dry_run = bool(cfg.get("dry_run_purchases", True))
    session = sess.steam.session if sess.steam else None
    try:
        ok, msg, game, conf_url, engine, transid = _purchase_with_session(
            session, sess.region, cvv, card,
            dry_run=dry_run, prefer_manual=bool(cfg.get("prefer_manual_list", True)),
            proxy=sess.proxy)
    except Exception:
        logger.exception("steam_ranger: buyer purchase failed (order %s)", sess.order_id)
        ok, msg, game, conf_url, engine, transid = False, "exception", {}, None, None, None

    if conf_url:
        # 3DS: оператор подтверждает в банке по ссылке, затем кнопкой → finalize.
        sess.purchase_engine = engine
        sess.purchase_transid = transid
        sess.step = STEP_BUSY
        sess.deadline = time.time() + 3600  # даём оператору час на подтверждение
        _buyer_send(cardinal, sess, "tpl_processing")
        kb = K()
        kb.add(B("✅ Подтвердил 3DS", callback_data=f"{CBT_BUYER_3DS}:{sess.order_id}"))
        kb.add(B("❌ Отменить", callback_data=f"{CBT_BUYER_3DS_CANCEL}:{sess.order_id}"))
        _notify_operator_kb(
            cardinal,
            f"🔐 Заказ <code>{sess.order_id}</code> ({sess.region}): требуется 3DS.\n"
            f"Подтверди в банке по ссылке, затем нажми ✅:\n"
            f"<a href='{conf_url}'>{conf_url}</a>",
            kb)
        return
    if dry_run:
        _buyer_send(cardinal, sess, "tpl_dry_run")
        _notify_operator(cardinal, f"🧪 Заказ <code>{sess.order_id}</code>: dry-run покупка ({msg}).")
        _close_session(sess.order_id, "dry-run")
        return
    if not ok:
        _buyer_send(cardinal, sess, "tpl_fail_generic")
        _notify_operator(cardinal, f"❌ Заказ <code>{sess.order_id}</code> ({sess.region}): покупка не удалась — {msg}.")
        _close_session(sess.order_id, f"purchase fail: {msg}")
        return
    _finish_buyer_purchase(cardinal, sess)


def _notify_operator_kb(cardinal: "Cardinal", text: str, kb) -> None:
    cfg = _load_config()
    tg = getattr(cardinal, "telegram", None)
    bot = getattr(tg, "bot", None) if tg else None
    chat_id = _resolve_operator_chat(cardinal, cfg)
    if bot is None or not chat_id:
        return
    try:
        bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb,
                         disable_web_page_preview=True)
    except Exception:
        logger.debug("steam_ranger: operator kb notify failed", exc_info=True)


def _finish_buyer_purchase(cardinal: "Cardinal", sess: "OrderSession") -> None:
    """Боевой успех покупки → проверяем регион и отвечаем покупателю."""
    session = sess.steam.session if sess.steam else None
    region_now = _detect_account_region(session)
    if region_now == sess.region:
        _buyer_send(cardinal, sess, "tpl_success")
        _notify_operator(cardinal, f"✅ Заказ <code>{sess.order_id}</code>: регион сменён на {sess.region}.")
        _close_session(sess.order_id, "success")
    else:
        _buyer_send(cardinal, sess, "tpl_fail_generic")
        _notify_operator(cardinal, f"⚠️ Заказ <code>{sess.order_id}</code>: покупка ок, но регион={region_now}≠{sess.region}.")
        _close_session(sess.order_id, "region mismatch")


def _finalize_buyer_3ds(cardinal: "Cardinal", sess: "OrderSession") -> None:
    """Завершить покупку после подтверждения 3DS оператором."""
    engine = sess.purchase_engine
    transid = sess.purchase_transid
    if engine is None or not transid:
        _notify_operator(cardinal, f"❌ Заказ <code>{sess.order_id}</code>: контекст 3DS потерян.")
        _close_session(sess.order_id, "3ds context lost")
        return
    try:
        final = engine.finalize(transid)
    except Exception:
        logger.exception("steam_ranger: buyer 3ds finalize failed (order %s)", sess.order_id)
        final = None
    if final is None or not getattr(final, "ok", False):
        msg = getattr(final, "message", "ошибка finalize") if final else "ошибка finalize"
        _buyer_send(cardinal, sess, "tpl_fail_generic")
        _notify_operator(cardinal, f"❌ Заказ <code>{sess.order_id}</code>: 3DS finalize не удался — {msg}.")
        _close_session(sess.order_id, "3ds finalize fail")
        return
    _finish_buyer_purchase(cardinal, sess)


def _confirm_buyer_3ds(cardinal: "Cardinal", call: Any) -> None:
    tg = getattr(cardinal, "telegram", None)
    bot = getattr(tg, "bot", None) if tg else None
    order_id = (call.data or "").split(":")[-1]
    sess = _order_sessions.get(str(order_id))
    if sess is None or sess.purchase_engine is None:
        if bot:
            try:
                bot.answer_callback_query(call.id, "Контекст 3DS потерян.")
            except Exception:
                pass
        return
    if bot:
        try:
            bot.answer_callback_query(call.id, "🏁 Финализирую…")
        except Exception:
            pass
    threading.Thread(target=_finalize_buyer_3ds, args=(cardinal, sess), daemon=True,
                     name="srr-buyer-3ds").start()


def _cancel_buyer_3ds(cardinal: "Cardinal", call: Any) -> None:
    tg = getattr(cardinal, "telegram", None)
    bot = getattr(tg, "bot", None) if tg else None
    order_id = (call.data or "").split(":")[-1]
    sess = _order_sessions.get(str(order_id))
    if sess is not None:
        _buyer_send(cardinal, sess, "tpl_fail_generic")
        _close_session(sess.order_id, "3ds cancelled by operator")
    if bot:
        try:
            bot.answer_callback_query(call.id, "❌ Отменено")
        except Exception:
            pass


BIND_TO_NEW_ORDER = [on_new_order]
BIND_TO_NEW_MESSAGE = [on_buyer_message]


# ----------------------------------------------------------------------------
# Auto-crash logging (как в minecraft_donate.py).
# ----------------------------------------------------------------------------
def _autolog_install():
    import functools as _functools
    import logging as _logging

    _plugin_name = __name__.rsplit(".", 1)[-1]
    _log = None
    for _name in ("logger", "LOGGER", "_LOGGER", "log"):
        _candidate = globals().get(_name)
        if _candidate is not None and hasattr(_candidate, "exception"):
            _log = _candidate
            break
    if _log is None:
        _log = _logging.getLogger(_plugin_name)

    def _wrap(_fn):
        if _fn is None or not callable(_fn):
            return _fn
        if getattr(_fn, "_autolog_wrapped", False):
            return _fn

        @_functools.wraps(_fn)
        def _w(*args, **kwargs):
            try:
                return _fn(*args, **kwargs)
            except Exception:
                _log.exception(
                    "[%s] Unhandled exception in %s",
                    _plugin_name,
                    getattr(_fn, "__qualname__", repr(_fn)),
                )
                raise

        _w._autolog_wrapped = True
        return _w

    g = globals()
    for _k in list(g.keys()):
        if not _k.startswith("BIND_TO_"):
            continue
        _v = g[_k]
        if _v is None:
            continue
        if callable(_v):
            g[_k] = _wrap(_v)
        elif isinstance(_v, list):
            g[_k] = [_wrap(_h) for _h in _v]
    _init_fn = g.get("init")
    if callable(_init_fn):
        g["init"] = _wrap(_init_fn)


try:
    _autolog_install()
except Exception:
    import logging as _l
    _l.getLogger(__name__).exception("Auto-crash logging install failed")
