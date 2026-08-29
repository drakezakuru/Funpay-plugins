"""
MinecraftDonate — плагин для FunPay Cardinal.

Авто-выдача игровой валюты на пиратских Minecraft-сетках (FunTime, HolyWorld,
любых кастомных) через MC-чат-бот. Бот логинится своим аккаунтом-донором,
проходит CAPTCHA, переключается на нужный подсервер (`/server anarchyXXX`) и
шлёт `/pay {nick} {amount}`. Поддерживает двойное подтверждение перевода
(`/pay confirm`), пул доноров, мульти-анархию (anarchy120/121/122/...),
кастомные подсерверы, парсинг описания лота (`#funtime @anarchy121 money:1000`),
PNG-скриншот чата как пруф для покупателя.

Метод выдачи (RCON / chat-bot / иное) полностью настраивается шаблонами
команд в Telegram-меню. Никаких хардкод-команд под конкретный сервер — есть
пресеты FunTime/HolyWorld, которые применяются одной кнопкой.

Зависимости (ставятся автоматически при первой загрузке плагина):
  quarry, twisted, Pillow

Если авто-установка недоступна (нет pip / песочница / закрытое окружение),
плагин всё равно загрузится, но попытки выдачи будут падать с понятной
ошибкой в Telegram (`Установите: pip install quarry`). Авто-установку
можно отключить, выставив переменную окружения MCD_NO_AUTOINSTALL=1.
"""
from __future__ import annotations

import importlib
import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
import io
from typing import TYPE_CHECKING, Any, Callable, Optional

# ── Авто-установка зависимостей ─────────────────────────────────────────────
# Ставим quarry/twisted/Pillow в тот же интерпретатор, который запустил
# Cardinal — пользователю не нужно делать pip install руками. Стиль повторяет
# `steam_rental._ensure_dependency`.
_BOOT_LOGGER = logging.getLogger("FPC.minecraft_donate")


def _ensure_dependency(pip_name: str, import_name: Optional[str] = None) -> bool:
    """Гарантирует наличие пакета. Возвращает True если модуль доступен."""
    mod_name = import_name or pip_name
    try:
        importlib.import_module(mod_name)
        return True
    except ImportError:
        pass
    if os.environ.get("MCD_NO_AUTOINSTALL") == "1":
        _BOOT_LOGGER.warning(
            "minecraft_donate: модуль %r не найден, авто-установка отключена "
            "(MCD_NO_AUTOINSTALL=1).", mod_name)
        return False
    _BOOT_LOGGER.warning(
        "minecraft_donate: модуль %r не найден, ставлю %r через pip...",
        mod_name, pip_name)
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install",
             "--disable-pip-version-check", "--quiet", pip_name],
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    except Exception as exc:
        _BOOT_LOGGER.error(
            "minecraft_donate: не удалось установить %r автоматически: %s. "
            "Поставь вручную: %s -m pip install %s",
            pip_name, exc, sys.executable, pip_name)
        return False
    importlib.invalidate_caches()
    try:
        importlib.import_module(mod_name)
        _BOOT_LOGGER.info(
            "minecraft_donate: %r успешно установлен.", pip_name)
        return True
    except ImportError as exc:
        _BOOT_LOGGER.error(
            "minecraft_donate: %r поставился, но импорт всё равно падает: %s",
            pip_name, exc)
        return False


# Pillow и twisted — обязательные. quarry — основной протокольный клиент.
_ensure_dependency("Pillow", import_name="PIL")
_ensure_dependency("twisted")
_ensure_dependency("quarry")


from telebot.types import (
    CallbackQuery,
    InlineKeyboardButton as B,
    InlineKeyboardMarkup as K,
    Message,
)

if TYPE_CHECKING:
    from cardinal import Cardinal
    from FunPayAPI.updater.events import NewOrderEvent


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
DONATION_CALLBACK_PREFIX = "mcd_dn"    # префикс колбэков кнопок баннера
DONATION_PLUGIN_NAME = "MinecraftDonate"  # имя плагина в шапке баннера
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


NAME = "MinecraftDonate"
VERSION = "1.3.1"
DESCRIPTION = (
    "Авто-выдача валюты в Minecraft через чат-бот: пресеты FunTime/HolyWorld, "
    "пул доноров, мульти-анархия (anarchy120/121/...), кастомные подсерверы, "
    "CAPTCHA, /pay confirm, парсинг #funtime @anarchy121 money:N из описания "
    "лота, PNG-скрин чата как пруф. v1.3.0: клан-выдача из казны "
    "(/clan money + /clan withdraw), image-капча (BotFilter) с отправкой "
    "картинки оператору в Telegram, выдача по заходу игрока (pay-on-join). "
    "Все три новые функции по умолчанию выключены — поведение v1.2.0 "
    "сохраняется без изменений."
)
CREDITS = "@drakelovc"
UUID = "f4c9a3b7-2e15-4d8a-9c0b-7d3e8f1a4b62"
SETTINGS_PAGE = True

logger = logging.getLogger("FPC.plugin.minecraft_donate")

PLUGIN_DIR = os.path.join("storage", "plugins", "minecraft_donate")
CONFIG_PATH = os.path.join(PLUGIN_DIR, "config.json")
SERVERS_PATH = os.path.join(PLUGIN_DIR, "servers.json")
DONORS_PATH = os.path.join(PLUGIN_DIR, "donors.json")
LOTS_PATH = os.path.join(PLUGIN_DIR, "lots.json")
ORDERS_PATH = os.path.join(PLUGIN_DIR, "orders.json")
HISTORY_PATH = os.path.join(PLUGIN_DIR, "history.json")
SCREENS_DIR = os.path.join(PLUGIN_DIR, "screens")
LOG_PATH = os.path.join(PLUGIN_DIR, "log.txt")

MAX_LOG_LINES = 500
MAX_HISTORY = 200

# ---------- CBT ----------
CBT_PREFIX = "MCD"
CBT_OPEN = f"{CBT_PREFIX}:O"
CBT_START = f"{CBT_PREFIX}:STR"

CBT_TAB_SERVERS = f"{CBT_PREFIX}:T:SRV"
CBT_TAB_DONORS = f"{CBT_PREFIX}:T:DNR"
CBT_TAB_LOTS = f"{CBT_PREFIX}:T:LOT"
CBT_TAB_ORDERS = f"{CBT_PREFIX}:T:ORD"
CBT_TAB_HISTORY = f"{CBT_PREFIX}:T:HIS"
CBT_TAB_SETTINGS = f"{CBT_PREFIX}:T:SET"
CBT_TAB_LOGS = f"{CBT_PREFIX}:T:LOG"

CBT_SRV_DETAIL = f"{CBT_PREFIX}:SRV:D"
CBT_SRV_PRESET = f"{CBT_PREFIX}:SRV:P"
CBT_SRV_DEL = f"{CBT_PREFIX}:SRV:DEL"
CBT_SRV_ADD = f"{CBT_PREFIX}:SRV:ADD"
CBT_SRV_EDIT = f"{CBT_PREFIX}:SRV:E"
CBT_SRV_TEST = f"{CBT_PREFIX}:SRV:TST"
CBT_SUB_DETAIL = f"{CBT_PREFIX}:SUB:D"
CBT_SUB_ADD = f"{CBT_PREFIX}:SUB:ADD"
CBT_SUB_DEL = f"{CBT_PREFIX}:SUB:DEL"
CBT_SUB_EDIT = f"{CBT_PREFIX}:SUB:E"

# v1.3.0: панели новых функций (клан-выдача / image-капча / pay-on-join)
CBT_SRV_FEATURES = f"{CBT_PREFIX}:SRV:FT"
CBT_SRV_FTE = f"{CBT_PREFIX}:SRV:FTE"
CBT_SUB_FEATURES = f"{CBT_PREFIX}:SUB:FT"
CBT_SUB_FTE = f"{CBT_PREFIX}:SUB:FTE"

CBT_DNR_DETAIL = f"{CBT_PREFIX}:DNR:D"
CBT_DNR_ADD = f"{CBT_PREFIX}:DNR:ADD"
CBT_DNR_DEL = f"{CBT_PREFIX}:DNR:DEL"
CBT_DNR_TEST = f"{CBT_PREFIX}:DNR:TST"
CBT_DNR_BAL = f"{CBT_PREFIX}:DNR:BAL"

CBT_LOT_ADD = f"{CBT_PREFIX}:LOT:ADD"
CBT_LOT_DEL = f"{CBT_PREFIX}:LOT:DEL"

CBT_ORD_RETRY = f"{CBT_PREFIX}:ORD:R"
CBT_ORD_CANCEL = f"{CBT_PREFIX}:ORD:C"

CBT_HIS_DETAIL = f"{CBT_PREFIX}:HIS:D"
CBT_HIS_SCREEN = f"{CBT_PREFIX}:HIS:S"

CBT_SET_EDIT = f"{CBT_PREFIX}:SET:E"
CBT_SET_TOGGLE = f"{CBT_PREFIX}:SET:T"

CBT_LOGS_CLEAR = f"{CBT_PREFIX}:LOG:CLR"
CBT_DRY_RUN = f"{CBT_PREFIX}:DRY"
CBT_TEST_DELIVERY = f"{CBT_PREFIX}:TST:DLV"
CBT_HELP = f"{CBT_PREFIX}:HELP"

# states (telegram input awaiting)
ST_AWAIT_SRV = f"{CBT_PREFIX}:S_SRV"
ST_AWAIT_SUB = f"{CBT_PREFIX}:S_SUB"
ST_AWAIT_DNR = f"{CBT_PREFIX}:S_DNR"
ST_AWAIT_LOT = f"{CBT_PREFIX}:S_LOT"
ST_AWAIT_EDIT = f"{CBT_PREFIX}:S_EDT"
ST_AWAIT_TEST = f"{CBT_PREFIX}:S_TST"

# ---------- пресеты серверов ----------
BUILTIN_PRESETS: dict[str, dict[str, Any]] = {
    "funtime": {
        "label": "FunTime",
        "host": "play.funtime.su",
        "port": 25565,
        "version": "auto",  # quarry автоопределит протокол
        "auth_mode": "offline",
        "login_cmd": "/login {password}",
        "default_pay_cmd": "/pay {nick} {amount}",
        "default_switch_cmd": "/{subserver}",   # FT: прямые команды /anarchy101, /anarchy1001
        "default_balance_cmd": "/money",
        "default_balance_regex": r"баланс[^\d]*([\d\s]+)|монет[:\s]+([\d\s,.]+)",
        "default_confirm_needed": True,
        "default_confirm_trigger": r"необходимо\s+подтвер|введите\s+повторно|подтвер.*перев|/pay\s+confirm",
        "default_confirm_cmd": "/pay {nick} {amount}",  # FT: повтор той же команды
        "default_success_regex": r"успешно[!.]?\s*игроку\s+(\w+)\s+отправлено|вы\s+перевели\s+(\d[\d\s]*)",
        "default_error_regex": r"недостат|не найден|ошибк|cooldown|кулдаун|нельзя",
        # 2FA через ВК/Telegram — после /login сервер шлёт «Подтвердите вход через ВК или ТГ»
        "default_twofa_enabled": True,
        "default_twofa_trigger": r"подтвер.*(?:ВК|ТГ|Telegram|VK|Discord)|verify.*(?:VK|Telegram)",
        "default_login_success_regex": r"успешн.*авториз|вы\s+успешно\s+вошли|приятной\s+игры",
        "default_twofa_wait_sec": 300,  # 5 минут на подтверждение
        "default_captcha": {
            "enabled": True,
            "type": "chat",  # TODO: image-captcha (BotFilter) обсуждаем отдельно
            "trigger_regex": r"введите\s+(?:номер|код)[^A-Z0-9]{1,40}([A-Z0-9]{3,10})",
            "respond_via": "chat",
            "timeout_sec": 30,
        },
        # реальный список с FunTime (anarchy101–107, anarchy1001–1003 + мини-режимы)
        "suggested_subservers": [
            "anarchy101", "anarchy102", "anarchy103", "anarchy104",
            "anarchy105", "anarchy106", "anarchy107",
            "anarchy1001", "anarchy1002", "anarchy1003",
            "skyblock", "bedwars", "skywars", "survival",
            "minigames", "creative", "hunger",
        ],
        "currency_label": "монет",  # FT использует «монеты» ($)
    },
    "holyworld": {
        "label": "HolyWorld",
        "host": "mc.holyworld.ru",
        "port": 25565,
        "version": "auto",
        "auth_mode": "offline",
        "login_cmd": "/login {password}",
        "default_pay_cmd": "/pay {nick} {amount}",
        "default_switch_cmd": "/server {subserver}",
        "default_balance_cmd": "/money",
        "default_balance_regex": r"баланс[^\d]*([\d\s]+)",
        "default_confirm_needed": True,
        "default_confirm_trigger": r"подтвер|confirm|введите.*confirm",
        "default_confirm_cmd": "/pay confirm",
        "default_success_regex": r"(?:перев[её]д|отправ|переда[лн])\D*(\d[\d\s]*)\D+(\w+)|вы перевели (\d[\d\s]*)",
        "default_error_regex": r"недостат|не найден|ошибк|cooldown|кулдаун|нельзя",
        # HolyWorld тоже может требовать 2FA — детектор включён, фразы такие же
        "default_twofa_enabled": True,
        "default_twofa_trigger": r"подтвер.*(?:ВК|ТГ|Telegram|VK|Discord)|verify.*(?:VK|Telegram)",
        "default_login_success_regex": r"успешн.*авториз|вы\s+успешно\s+вошли|приятной\s+игры",
        "default_twofa_wait_sec": 300,
        "default_captcha": {
            "enabled": True,
            "type": "chat",
            "trigger_regex": r"(?:введите|кап[чт]|code|код)[^A-Z0-9]{1,40}([A-Z0-9]{3,10})",
            "respond_via": "chat",
            "timeout_sec": 30,
        },
        "suggested_subservers": [
            "anarchy14", "anarchy15", "anarchy16",
            "lite", "bedwars", "survival", "skyblock",
        ],
        "currency_label": "коинов",
    },
}

DEFAULT_CONFIG: dict[str, Any] = {
    "running": False,
    "settings": {
        "dry_run": False,              # тестовый режим: всё кроме реального /pay
        "max_per_order": 1_000_000,
        "max_per_minute": 5,           # rate-limit (anti-abuse)
        "session_idle_kick_sec": 300,  # держим сессию донора 5 минут
        "captcha_timeout_sec": 30,
        "pay_timeout_sec": 20,
        "switch_timeout_sec": 15,
        "login_post_delay_sec": 2,     # пауза после логина
        "command_jitter_min_sec": 1.5,
        "command_jitter_max_sec": 3.0,
        "retry_attempts": 3,
        "retry_backoff_sec": 30,
        "buyer_greeting": (
            "✅ Заказ #{order_id}: {amount_per_unit} {currency} × {qty} = "
            "<b>{total} {currency}</b>\n"
            "Сервер: <b>{server_label}</b> · подсервер <b>{subserver}</b>\n\n"
            "⚠️ Все переводы только на ОДИН аккаунт.\n"
            "⚠️ Изменить ник после подтверждения нельзя.\n\n"
            "Напишите ваш ник в Minecraft (3-16 символов: латиница/цифры/_)."
        ),
        "buyer_confirm": (
            "Проверьте:\n"
            "  ник     — <b>{nick}</b>\n"
            "  сервер  — <b>{server_label}</b> · <b>{subserver}</b>\n\n"
            "Подтвердите — напишите <b>ДА</b> или <b>+</b>.\n"
            "Чтобы изменить — пришлите новый ник."
        ),
        "buyer_paying": (
            "⏳ Выдаю {total} {currency} на {nick}, ожидайте 10-30 сек..."
        ),
        "buyer_success": (
            "✅ Готово. {total} {currency} отправлены на <b>{nick}</b> "
            "({server_label} · {subserver})."
        ),
        "buyer_failed": (
            "⚠️ Не удалось выдать автоматически. С вами свяжется продавец."
        ),
        "nick_regex": r"^[A-Za-z0-9_]{3,16}$",
        "confirm_words": ["да", "+", "yes", "ок", "ok", "ага", "д", "y"],
        "notify_chats": [],
        # v1.2.0:
        "nick_deny_list": [],            # антифрод: ники, которым не выдаём
        "nick_rate_limit": 0,            # макс. выдач на ник за окно (0 = выкл)
        "nick_rate_window_sec": 86400,
        "empty_command": "/money",       # тест-режим: безвредная команда вместо /pay
        "infra_alert_suppress_sec": 600, # антиспам алертов о недоступности сервера
    },
}


# ---------- v1.3.0: дефолты новых функций (клан-выдача / image-капча / pay-on-join) ----------
# FunTime/HolyWorld пишут баланс казны строкой вида
# «[⚔] Баланс клана: 1 234 567» — regex захватывает число (с пробелами-разделителями).
DEFAULT_CLAN_BALANCE_REGEX = r"Баланс\s*клана[^\d]*([\d\s]+)"

# Новые per-Server дефолты (ключи с префиксом default_, переопределяются на
# уровне подсервера ключами без префикса — тот же механизм, что и v1.2.0).
SERVER_V130_DEFAULTS: dict[str, Any] = {
    "default_clan_flow": False,
    "default_clan_balance_cmd": "/clan money",
    "default_clan_balance_regex": DEFAULT_CLAN_BALANCE_REGEX,
    "default_clan_balance_timeout_sec": 10,            # 1..60
    "default_clan_withdraw_cmd": "/clan withdraw {amount}",
    "default_pay_on_join": False,
    "default_join_timeout_sec": 120,                   # 10..600
}

# Допустимые диапазоны (валидация в Telegram-UI и резолве).
CLAN_BALANCE_TIMEOUT_RANGE = (1, 60)
CAPTCHA_TIMEOUT_RANGE = (10, 600)
JOIN_TIMEOUT_RANGE = (10, 600)
FEATURE_TEXT_LEN_RANGE = (1, 255)


def _migrate_server_v130(server: dict[str, Any]) -> dict[str, Any]:
    """Additive `setdefault`-миграция одного сервера до v1.3.0.

    НИКОГДА не меняет/не удаляет/не переупорядочивает существующие поля — только
    дописывает отсутствующие новые ключи в конец (Req 6.1, 6.2)."""
    if not isinstance(server, dict):
        return server
    for k, v in SERVER_V130_DEFAULTS.items():
        server.setdefault(k, json.loads(json.dumps(v)))
    return server


# ---------- I/O ----------
def _ensure_dir() -> None:
    os.makedirs(PLUGIN_DIR, exist_ok=True)
    os.makedirs(SCREENS_DIR, exist_ok=True)


def _load_json(path: str, default: Any) -> Any:
    _ensure_dir()
    if not os.path.exists(path):
        return json.loads(json.dumps(default))
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return json.loads(json.dumps(default))


def _save_json(path: str, data: Any) -> None:
    _ensure_dir()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _load_config() -> dict[str, Any]:
    cfg = _load_json(CONFIG_PATH, DEFAULT_CONFIG)
    s = cfg.setdefault("settings", {})
    for k, v in DEFAULT_CONFIG["settings"].items():
        s.setdefault(k, v)
    cfg.setdefault("running", False)
    return cfg


def _save_config(cfg: dict[str, Any]) -> None:
    _save_json(CONFIG_PATH, cfg)


def _load_servers() -> dict[str, Any]:
    data = _load_json(SERVERS_PATH, {})
    # Additive-миграция v1.3.0: дописываем недостающие новые ключи каждому
    # серверу через setdefault. Существующие значения/порядок не трогаем.
    if isinstance(data, dict):
        for s in data.values():
            _migrate_server_v130(s)
    return data


def _save_servers(data: dict[str, Any]) -> None:
    _save_json(SERVERS_PATH, data)


def _load_donors() -> list[dict[str, Any]]:
    return _load_json(DONORS_PATH, [])


def _save_donors(data: list[dict[str, Any]]) -> None:
    _save_json(DONORS_PATH, data)


def _load_lots() -> list[dict[str, Any]]:
    return _load_json(LOTS_PATH, [])


def _save_lots(data: list[dict[str, Any]]) -> None:
    _save_json(LOTS_PATH, data)


def _load_orders() -> list[dict[str, Any]]:
    return _load_json(ORDERS_PATH, [])


def _save_orders(data: list[dict[str, Any]]) -> None:
    _save_json(ORDERS_PATH, data)


def _load_history() -> list[dict[str, Any]]:
    return _load_json(HISTORY_PATH, [])


def _save_history(data: list[dict[str, Any]]) -> None:
    _save_json(HISTORY_PATH, data[-MAX_HISTORY:])


def _log(msg: str) -> None:
    _ensure_dir()
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {msg}"
    logger.info(msg)
    try:
        lines: list[str] = []
        if os.path.exists(LOG_PATH):
            with open(LOG_PATH, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        lines.append(line)
        lines = lines[-MAX_LOG_LINES:]
        with open(LOG_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception:
        pass


def _read_logs() -> str:
    if not os.path.exists(LOG_PATH):
        return "(пусто)"
    with open(LOG_PATH, "r", encoding="utf-8") as f:
        return f.read().strip() or "(пусто)"


# ---------- парсер описания лота ----------
_LOT_RE_SERVER = re.compile(
    r"#(?P<server>funtime|fantime|фантайм|holyworld|holyword|холик|холиворлд)",
    re.IGNORECASE,
)
_LOT_RE_SUB = re.compile(r"@(?P<sub>[A-Za-zА-Яа-я0-9_\-]+)", re.IGNORECASE)
_LOT_RE_MONEY = re.compile(
    r"(?:money|coins|coin|coins?|сумма|деньги|коинов?|monet)\s*[:=]\s*(\d+)",
    re.IGNORECASE,
)
_LOT_RE_CURRENCY = re.compile(r"currency\s*[:=]\s*([A-Za-zА-Яа-я]+)", re.IGNORECASE)

_SERVER_ALIAS_MAP = {
    "funtime": "funtime", "fantime": "funtime", "фантайм": "funtime",
    "holyworld": "holyworld", "holyword": "holyworld",
    "холик": "holyworld", "холиворлд": "holyworld",
}


def parse_lot_description(text: str) -> dict[str, Any]:
    """
    Парсит описание/название лота FunPay.
    Возвращает dict: {server_alias, subserver, amount_per_unit, currency_label}.
    Поля могут быть None.
    """
    text = text or ""
    out: dict[str, Any] = {
        "server_alias": None, "subserver": None,
        "amount_per_unit": None, "currency_label": None,
    }
    m = _LOT_RE_SERVER.search(text)
    if m:
        out["server_alias"] = _SERVER_ALIAS_MAP.get(m.group("server").lower())
    m = _LOT_RE_SUB.search(text)
    if m:
        sub = m.group("sub").lower()
        # @anarchy121 / @anarch121 / @a121 → нормализуем к "anarchyXXX" если число
        m_num = re.match(r"^(?:anarchy|anarch|анархия|анарх|a)(\d+)$", sub)
        if m_num:
            sub = f"anarchy{m_num.group(1)}"
        out["subserver"] = sub
    m = _LOT_RE_MONEY.search(text)
    if m:
        try:
            out["amount_per_unit"] = int(m.group(1))
        except ValueError:
            pass
    m = _LOT_RE_CURRENCY.search(text)
    if m:
        out["currency_label"] = m.group(1)
    return out


# ---------- утилиты сервера/подсервера ----------
def get_server(alias: str) -> Optional[dict[str, Any]]:
    servers = _load_servers()
    return servers.get(alias)


def list_subservers(server_alias: str) -> list[str]:
    s = get_server(server_alias) or {}
    return list((s.get("subservers") or {}).keys())


def add_subserver(server_alias: str, sub_name: str,
                  overrides: Optional[dict[str, Any]] = None) -> None:
    servers = _load_servers()
    s = servers.setdefault(server_alias, {})
    subs = s.setdefault("subservers", {})
    sub = subs.setdefault(sub_name, {})
    if overrides:
        sub.update(overrides)
    _save_servers(servers)
    _log(f"server={server_alias} +subserver {sub_name}")


def del_subserver(server_alias: str, sub_name: str) -> None:
    servers = _load_servers()
    s = servers.get(server_alias) or {}
    subs = s.get("subservers") or {}
    if sub_name in subs:
        del subs[sub_name]
        _save_servers(servers)
        _log(f"server={server_alias} -subserver {sub_name}")


def apply_preset(alias: str, preset_key: str) -> bool:
    """Применяет пресет FT/HW к серверу alias."""
    preset = BUILTIN_PRESETS.get(preset_key)
    if not preset:
        return False
    servers = _load_servers()
    s = servers.setdefault(alias, {})
    # сохраняем существующий пароль RCON / login_password (никогда не перетираем)
    keep_keys = {"login_password", "subservers", "donors_login_password"}
    saved = {k: s[k] for k in keep_keys if k in s}
    s.clear()
    for k, v in preset.items():
        s[k] = json.loads(json.dumps(v))  # deep copy
    s.update(saved)
    s.setdefault("subservers", {})
    # автоматически создаём подсерверы из suggested
    for sub in s.get("suggested_subservers", []):
        s["subservers"].setdefault(sub, {})
    _save_servers(servers)
    _log(f"server={alias} применён пресет {preset_key}")
    return True


def resolve_subserver_settings(server_alias: str, sub_name: str) -> dict[str, Any]:
    """
    Сводит настройки подсервера: дефолты сервера + override подсервера.
    Возвращает плоский dict с готовыми значениями.
    """
    s = get_server(server_alias) or {}
    sub = (s.get("subservers") or {}).get(sub_name) or {}

    def pick(key: str, default: Any = None) -> Any:
        v = sub.get(key)
        if v is not None and v != "":
            return v
        return s.get(f"default_{key}", s.get(key, default))

    return {
        "server_alias": server_alias,
        "server_label": s.get("label", server_alias),
        "host": s.get("host"),
        "port": int(s.get("port", 25565)),
        "version": s.get("version", "auto"),
        "auth_mode": s.get("auth_mode", "offline"),
        "subserver": sub_name,
        "switch_cmd": pick("switch_cmd", "/server {subserver}"),
        "pay_cmd": pick("pay_cmd", "/pay {nick} {amount}"),
        "balance_cmd": pick("balance_cmd", "/money"),
        "balance_regex": pick("balance_regex", r"баланс[^\d]*([\d\s]+)"),
        "confirm_needed": pick("confirm_needed", False),
        "confirm_trigger": pick("confirm_trigger", r"подтвер|confirm"),
        "confirm_cmd": pick("confirm_cmd", "/pay confirm"),
        "success_regex": pick("success_regex", r"перев[её]д.*выполн|успешно"),
        "error_regex": pick("error_regex", r"недостат|ошибк|не найден"),
        "twofa_enabled": pick("twofa_enabled", False),
        "twofa_trigger": pick("twofa_trigger",
                              r"подтвер.*(?:ВК|ТГ|VK|Telegram)|verify.*VK"),
        "login_success_regex": pick("login_success_regex",
                                    r"успешн.*авториз|вы\s+успешно\s+вошли"),
        "twofa_wait_sec": int(pick("twofa_wait_sec", 300)),
        "captcha": pick("captcha", {"enabled": False, "type": "none"}),
        "currency_label": pick("currency_label", "коинов"),
        "login_cmd": s.get("login_cmd", "/login {password}"),
        # ── v1.3.0: клан-выдача (по умолчанию выключена) ──
        "clan_flow": bool(pick("clan_flow", False)),
        "clan_balance_cmd": pick("clan_balance_cmd", "/clan money"),
        "clan_balance_regex": pick("clan_balance_regex", DEFAULT_CLAN_BALANCE_REGEX),
        "clan_balance_timeout_sec": int(pick("clan_balance_timeout_sec", 10)),
        "clan_withdraw_cmd": pick("clan_withdraw_cmd", "/clan withdraw {amount}"),
        # ── v1.3.0: выдача по заходу игрока (по умолчанию выключена) ──
        "pay_on_join": bool(pick("pay_on_join", False)),
        "join_timeout_sec": int(pick("join_timeout_sec", 120)),
    }


# ============================================================================
# v1.2.0 — чистое ядро (антифрод/команды/классификация/тест-режим)
# ============================================================================
def _norm_nick(nick: str) -> str:
    return str(nick or "").strip().lower()


def _nick_recent_count(history: list, nick: str, now: float, window_sec: int) -> int:
    if window_sec <= 0:
        return 0
    lo = now - window_sec
    n = _norm_nick(nick)
    return sum(1 for h in history
               if _norm_nick(h.get("nick")) == n
               and lo <= float(h.get("ts", 0) or 0) <= now
               and (h.get("ok") or h.get("success")))


def _nick_allowed(nick: str, regex: str, deny_list: list, history: list, now: float,
                  *, rate_limit: int, window_sec: int) -> tuple[bool, str]:
    """Возвращает (allowed, reason). reason ∈ {'', 'format', 'denied', 'rate'}."""
    try:
        if not re.match(regex or r"^[A-Za-z0-9_]{3,16}$", nick or ""):
            return False, "format"
    except re.error:
        if not nick:
            return False, "format"
    if _norm_nick(nick) in {_norm_nick(x) for x in (deny_list or [])}:
        return False, "denied"
    if rate_limit and _nick_recent_count(history, nick, now, window_sec) >= rate_limit:
        return False, "rate"
    return True, ""


_CMD_KEYS = ("switch_cmd", "pay_cmd", "confirm_cmd", "balance_cmd")


def _resolve_commands(server_defaults: dict, sub_overrides: dict, lot_overrides: dict) -> dict:
    """Команды с приоритетом Lot > Subserver > Server. Пустые значения игнорируются."""
    out = dict(server_defaults or {})
    out.update({k: v for k, v in (sub_overrides or {}).items() if v})
    out.update({k: v for k, v in (lot_overrides or {}).items() if v})
    return out


_INFRA_PATTERNS = ("login", "логин", "auth", "авториз", "connect", "подключ",
                   "timeout", "таймаут", "switch", "сервер недоступ", "captcha", "капч",
                   "disconnect", "отключ")
_PAYMENT_PATTERNS = ("недостат", "cooldown", "кулдаун", "не найден", "unknown nick",
                     "нельзя", "лимит")


def _classify_failure(reason: str) -> str:
    """'infra' (проблема инфраструктуры) или 'payment' (игровой отказ). Дефолт — payment."""
    r = (reason or "").lower()
    if any(p in r for p in _PAYMENT_PATTERNS):
        return "payment"
    if any(p in r for p in _INFRA_PATTERNS):
        return "infra"
    return "payment"


def _infra_alert_due(last_ts: float, now: float, suppress_sec: int) -> bool:
    return (now - float(last_ts or 0)) >= suppress_sec


def _effective_pay_command(test_mode: bool, empty_command: str, real_command: str) -> str:
    return empty_command if test_mode else real_command


# ============================================================================
# v1.3.0 — чистое ядро (клан-выдача / image-капча / pay-on-join / тест-режим)
# ============================================================================
def _parse_clan_balance(line: str, regex: str) -> Optional[int]:
    """Парсит баланс казны клана из строки чата.

    Возвращает целое (только цифры из захваченной группы) или None, если
    строка не сматчилась / группа пустая / не парсится."""
    try:
        m = re.search(regex or "", line or "", flags=re.IGNORECASE | re.UNICODE)
    except re.error:
        return None
    if not m:
        return None
    try:
        captured = m.group(1)
    except IndexError:
        # regex без capture-group — пробуем весь матч
        captured = m.group(0)
    digits = re.sub(r"\D", "", captured or "")
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def _clan_decide(balance: Optional[int], required: int) -> tuple[str, str]:
    """Решение по клан-балансу. Возвращает (action, reason).

    action ∈ {'pay', 'insufficient', 'parse_fail'}:
      - None баланс            → 'parse_fail'
      - balance >= required    → 'pay'
      - иначе                  → 'insufficient'
    """
    if balance is None:
        return "parse_fail", "не удалось распознать баланс казны"
    try:
        bal_i = int(balance)
        req_i = int(required)
    except (TypeError, ValueError):
        return "parse_fail", "некорректные числа"
    if bal_i < req_i:
        return "insufficient", f"в казне {bal_i}, требуется {req_i}"
    return "pay", ""


def _norm_join_nick(nick: str) -> str:
    """Нормализация ника для сравнения при заходе: трим + casefold."""
    return str(nick or "").strip().casefold()


def _nick_matches_join(joiner_display: str, target: str) -> bool:
    """True, если зашедший игрок совпадает с целевым ником (регистр/пробелы игнор)."""
    return _norm_join_nick(joiner_display) == _norm_join_nick(target)


def _effective_clan_and_pay(test_mode: bool, empty: str, withdraw: str,
                            pay: str) -> tuple[str, str]:
    """Тест-режим подменяет ОБЕ команды (withdraw и pay) на безвредную empty.

    Возвращает (effective_withdraw, effective_pay). Гарантирует, что при
    test_mode реальные `/clan withdraw` и `/pay` недостижимы (Req 5.1, 5.2)."""
    if test_mode:
        return empty, empty
    return withdraw, pay


# ---------- v1.3.0: валидация настроек новых функций (для Telegram-UI) ----------
_FEATURE_BOOL_FIELDS = {"clan_flow", "pay_on_join"}
_FEATURE_INT_RANGES = {
    "clan_balance_timeout_sec": CLAN_BALANCE_TIMEOUT_RANGE,
    "join_timeout_sec": JOIN_TIMEOUT_RANGE,
    "captcha_timeout": CAPTCHA_TIMEOUT_RANGE,
}
_FEATURE_STR_FIELDS = {
    "clan_balance_cmd", "clan_balance_regex", "clan_withdraw_cmd",
    "captcha_trigger",
}
_FEATURE_ENUMS = {
    "captcha_type": ("none", "chat", "image"),
    "captcha_respond": ("chat", "command"),
}


def _parse_bool_ru(raw: str) -> Optional[bool]:
    v = (raw or "").strip().lower()
    if v in ("true", "1", "да", "+", "yes", "on", "вкл"):
        return True
    if v in ("false", "0", "нет", "-", "no", "off", "выкл"):
        return False
    return None


def _validate_feature_value(field: str, raw: str) -> tuple[bool, Any, str]:
    """Проверяет значение настройки новой функции.

    Возвращает (ok, coerced_value, error_ru). При ok=False значение НЕ должно
    сохраняться (прежнее сохраняется), а error_ru показывается оператору."""
    raw = (raw or "").strip()
    if field in _FEATURE_BOOL_FIELDS:
        b = _parse_bool_ru(raw)
        if b is None:
            return False, None, f"Поле «{field}»: введите да/нет (вкл/выкл)."
        return True, b, ""
    if field in _FEATURE_INT_RANGES:
        lo, hi = _FEATURE_INT_RANGES[field]
        try:
            n = int(raw)
        except ValueError:
            return False, None, f"Поле «{field}»: нужно целое число в диапазоне {lo}–{hi}."
        if not (lo <= n <= hi):
            return False, None, f"Поле «{field}»: значение {n} вне диапазона {lo}–{hi}."
        return True, n, ""
    if field in _FEATURE_STR_FIELDS:
        lo, hi = FEATURE_TEXT_LEN_RANGE
        if not (lo <= len(raw) <= hi):
            return False, None, (f"Поле «{field}»: длина {len(raw)} вне диапазона "
                                 f"{lo}–{hi} символов.")
        if field.endswith("regex") or field == "captcha_trigger":
            try:
                re.compile(raw)
            except re.error as ex:
                return False, None, f"Поле «{field}»: некорректный regex ({ex})."
        return True, raw, ""
    if field in _FEATURE_ENUMS:
        allowed = _FEATURE_ENUMS[field]
        v = raw.lower()
        if v not in allowed:
            return False, None, f"Поле «{field}»: допустимо одно из: {', '.join(allowed)}."
        return True, v, ""
    return False, None, f"Неизвестное поле «{field}»."


def _feature_source_label(server: dict, sub: dict, bare_key: str) -> str:
    """RU-метка источника значения: переопределение подсервера или дефолт сервера."""
    if sub is not None:
        v = sub.get(bare_key)
        if v is not None and v != "":
            return "переопределение подсервера"
    return "дефолт сервера"


def _render_resolved_features(server_alias: str, sub_name: Optional[str] = None) -> str:
    """RU-сводка эффективных настроек новых функций с пометкой источника."""
    eff = resolve_subserver_settings(server_alias, sub_name or "")
    s = get_server(server_alias) or {}
    sub = ((s.get("subservers") or {}).get(sub_name) or {}) if sub_name else None
    cap = eff.get("captcha") or {}

    def src(bare: str) -> str:
        return _feature_source_label(s, sub, bare)

    def cap_src() -> str:
        if sub is not None and (sub.get("captcha") not in (None, "", {})):
            return "переопределение подсервера"
        return "дефолт сервера"

    lines = ["<b>Эффективные настройки (Resolved):</b>"]
    lines.append(f"🏰 Клан-выдача: <b>{'ВКЛ' if eff['clan_flow'] else 'выкл'}</b> "
                 f"({src('clan_flow')})")
    lines.append(f"  • баланс-команда: <code>{eff['clan_balance_cmd']}</code> "
                 f"({src('clan_balance_cmd')})")
    lines.append(f"  • regex баланса: <code>{eff['clan_balance_regex']}</code> "
                 f"({src('clan_balance_regex')})")
    lines.append(f"  • таймаут баланса: <b>{eff['clan_balance_timeout_sec']}с</b> "
                 f"({src('clan_balance_timeout_sec')})")
    lines.append(f"  • withdraw-команда: <code>{eff['clan_withdraw_cmd']}</code> "
                 f"({src('clan_withdraw_cmd')})")
    lines.append(f"🤖 Капча: тип <b>{cap.get('type', 'none')}</b> ({cap_src()})")
    lines.append(f"  • trigger: <code>{cap.get('trigger_regex', '')}</code>")
    lines.append(f"  • таймаут: <b>{cap.get('timeout_sec', 30)}с</b> · "
                 f"respond_via: <b>{cap.get('respond_via', 'chat')}</b>")
    lines.append(f"🎯 Выдача по заходу: <b>{'ВКЛ' if eff['pay_on_join'] else 'выкл'}</b> "
                 f"({src('pay_on_join')})")
    lines.append(f"  • таймаут захода: <b>{eff['join_timeout_sec']}с</b> "
                 f"({src('join_timeout_sec')})")
    return "\n".join(lines)


# ---------- палитра карт Minecraft (для image-капчи / BotFilter) ----------
# Базовые цвета карты (id → RGB). Каждый базовый цвет даёт 4 оттенка в палитре
# карты через множители (180, 220, 255, 135) / 255. Итоговый индекс в map-data:
#   palette_index = base_id * 4 + shade.
# Источник — каноническая таблица цветов предмета «Карта» Minecraft.
# Базовые id 0..3 (shade-варианты id 0) — прозрачные (NONE).
_MC_MAP_BASE_COLORS: list[tuple[int, int, int]] = [
    (0, 0, 0),         # 0  NONE (прозрачный)
    (0, 0, 0),         # 1  NONE
    (0, 0, 0),         # 2  NONE
    (0, 0, 0),         # 3  NONE
    (127, 178, 56),    # 4  GRASS
    (247, 233, 163),   # 5  SAND
    (199, 199, 199),   # 6  WOOL
    (255, 0, 0),       # 7  FIRE
    (160, 160, 255),   # 8  ICE
    (167, 167, 167),   # 9  METAL
    (0, 124, 0),       # 10 PLANT
    (255, 255, 255),   # 11 SNOW
    (164, 168, 184),   # 12 CLAY
    (151, 109, 77),    # 13 DIRT
    (112, 112, 112),   # 14 STONE
    (64, 64, 255),     # 15 WATER
    (143, 119, 72),    # 16 WOOD
    (255, 252, 245),   # 17 QUARTZ
    (216, 127, 51),    # 18 COLOR_ORANGE
    (178, 76, 216),    # 19 COLOR_MAGENTA
    (102, 153, 216),   # 20 COLOR_LIGHT_BLUE
    (229, 229, 51),    # 21 COLOR_YELLOW
    (127, 204, 25),    # 22 COLOR_LIGHT_GREEN
    (242, 127, 165),   # 23 COLOR_PINK
    (76, 76, 76),      # 24 COLOR_GRAY
    (153, 153, 153),   # 25 COLOR_LIGHT_GRAY
    (76, 127, 153),    # 26 COLOR_CYAN
    (127, 63, 178),    # 27 COLOR_PURPLE
    (51, 76, 178),     # 28 COLOR_BLUE
    (102, 76, 51),     # 29 COLOR_BROWN
    (102, 127, 51),    # 30 COLOR_GREEN
    (153, 51, 51),     # 31 COLOR_RED
    (25, 25, 25),      # 32 COLOR_BLACK
    (250, 238, 77),    # 33 GOLD
    (92, 219, 213),    # 34 DIAMOND
    (74, 128, 255),    # 35 LAPIS
    (0, 217, 58),      # 36 EMERALD
    (129, 86, 49),     # 37 PODZOL
    (112, 2, 0),       # 38 NETHER
    (209, 177, 161),   # 39 TERRACOTTA_WHITE
    (159, 82, 36),     # 40 TERRACOTTA_ORANGE
    (149, 87, 108),    # 41 TERRACOTTA_MAGENTA
    (112, 108, 138),   # 42 TERRACOTTA_LIGHT_BLUE
    (186, 133, 36),    # 43 TERRACOTTA_YELLOW
    (103, 117, 53),    # 44 TERRACOTTA_LIGHT_GREEN
    (160, 77, 78),     # 45 TERRACOTTA_PINK
    (57, 41, 35),      # 46 TERRACOTTA_GRAY
    (135, 107, 98),    # 47 TERRACOTTA_LIGHT_GRAY
    (87, 92, 92),      # 48 TERRACOTTA_CYAN
    (122, 73, 88),     # 49 TERRACOTTA_PURPLE
    (76, 62, 92),      # 50 TERRACOTTA_BLUE
    (76, 50, 35),      # 51 TERRACOTTA_BROWN
    (76, 82, 42),      # 52 TERRACOTTA_GREEN
    (142, 60, 46),     # 53 TERRACOTTA_RED
    (37, 22, 16),      # 54 TERRACOTTA_BLACK
    (189, 48, 49),     # 55 CRIMSON_NYLIUM
    (148, 63, 97),     # 56 CRIMSON_STEM
    (92, 25, 29),      # 57 CRIMSON_HYPHAE
    (22, 126, 134),    # 58 WARPED_NYLIUM
    (58, 142, 140),    # 59 WARPED_STEM
    (86, 44, 62),      # 60 WARPED_HYPHAE
    (20, 180, 133),    # 61 WARPED_WART_BLOCK
    (100, 100, 100),   # 62 DEEPSLATE
    (216, 175, 147),   # 63 RAW_IRON
    (127, 167, 150),   # 64 GLOW_LICHEN
]

# Множители оттенков (как в Minecraft): индекс shade → коэффициент.
_MC_MAP_SHADE_MULT = (180, 220, 255, 135)


def _build_map_palette() -> list[tuple[int, int, int]]:
    """Разворачивает базовые цвета в полную палитру карты (base*4 оттенка)."""
    palette: list[tuple[int, int, int]] = []
    for (r, g, b) in _MC_MAP_BASE_COLORS:
        for mult in _MC_MAP_SHADE_MULT:
            palette.append((r * mult // 255, g * mult // 255, b * mult // 255))
    return palette


# Полная палитра карт: 65 базовых цветов × 4 оттенка = 260 записей (>164).
_MC_MAP_PALETTE: list[tuple[int, int, int]] = _build_map_palette()

# Индексы 0..3 — прозрачные (NONE-цвет в любом оттенке).
_MC_MAP_TRANSPARENT_MAX = 4


def _stitch_map_tiles(tiles: list[dict], palette: list[tuple[int, int, int]]) -> bytes:
    """Склеивает map-тайлы (128×128 каждый) в один PNG по их (x, y, rotation).

    tiles: [{'mapId': int, 'data': bytes(индексы палитры), 'x': int, 'y': int,
             'rotation': int(0..3)}, ...]
    Холст авто-размером 128*cols × 128*rows (cols=max x+1, rows=max y+1).
    Недостающие ячейки остаются прозрачными. Возвращает PNG-байты.
    Размер холста инвариантен к перестановке списка тайлов (Req 2.3)."""
    from PIL import Image  # type: ignore

    # пустые/неполные палитры дополняем до 256 для режима "P"
    flat: list[int] = []
    for (r, g, b) in palette[:256]:
        flat.extend((int(r) & 255, int(g) & 255, int(b) & 255))
    if len(flat) < 256 * 3:
        flat.extend([0] * (256 * 3 - len(flat)))

    if not tiles:
        cols = rows = 1
    else:
        cols = max(int(t.get("x", 0)) for t in tiles) + 1
        rows = max(int(t.get("y", 0)) for t in tiles) + 1
        cols = max(cols, 1)
        rows = max(rows, 1)

    canvas = Image.new("RGBA", (128 * cols, 128 * rows), (0, 0, 0, 0))
    # таблица альфы: индексы < 4 (NONE) — прозрачны, остальные — непрозрачны
    alpha_lut = bytes([0] * _MC_MAP_TRANSPARENT_MAX
                      + [255] * (256 - _MC_MAP_TRANSPARENT_MAX))

    for t in tiles:
        data = bytes(t.get("data") or b"")
        data = data[:16384].ljust(16384, b"\x00")
        base = Image.frombytes("P", (128, 128), data)
        base.putpalette(flat)
        rgb = base.convert("RGB").convert("RGBA")
        # альфа считается из СЫРЫХ индексов (а не через палитру)
        alpha = Image.frombytes("L", (128, 128), data.translate(alpha_lut))
        rgb.putalpha(alpha)
        rotation = int(t.get("rotation", 0)) % 4
        if rotation:
            rgb = rgb.rotate(-90 * rotation)  # по часовой
        x = int(t.get("x", 0))
        y = int(t.get("y", 0))
        canvas.paste(rgb, (x * 128, y * 128), rgb)

    buf = io.BytesIO()
    canvas.save(buf, "PNG")
    return buf.getvalue()


# Антиспам алертов о недоступности сервера: {server_alias: last_alert_ts}
_infra_last_alert: dict[str, float] = {}


# ---------- доноры ----------
def find_donors(server_alias: str, subserver: str,
                min_balance: int = 0) -> list[dict[str, Any]]:
    """Доноры для (server, subserver) с подходящим статусом и балансом."""
    donors = _load_donors()
    out = []
    for d in donors:
        if d.get("server") != server_alias:
            continue
        if d.get("subserver") != subserver:
            continue
        if d.get("status") == "banned":
            continue
        if int(d.get("balance_cached") or 0) < min_balance:
            continue
        out.append(d)
    # round-robin: сортируем по last_used (давний — раньше)
    out.sort(key=lambda x: float(x.get("last_used") or 0))
    return out


def update_donor(nick: str, **fields: Any) -> None:
    donors = _load_donors()
    for d in donors:
        if d.get("nick") == nick:
            d.update(fields)
            break
    _save_donors(donors)


# ---------- thread-safe rate limiter ----------
class _RateLimiter:
    def __init__(self, max_per_minute: int):
        self.max = max_per_minute
        self.events: list[float] = []
        self.lock = threading.Lock()

    def allow(self) -> bool:
        with self.lock:
            now = time.time()
            self.events = [t for t in self.events if now - t < 60]
            if len(self.events) >= self.max:
                return False
            self.events.append(now)
            return True


_rate_limiter: Optional[_RateLimiter] = None


def _get_rate_limiter() -> _RateLimiter:
    global _rate_limiter
    cfg = _load_config()
    mpm = int(cfg["settings"].get("max_per_minute", 5))
    if _rate_limiter is None or _rate_limiter.max != mpm:
        _rate_limiter = _RateLimiter(mpm)
    return _rate_limiter



# ============================================================
# MINECRAFT-КЛИЕНТ (quarry, с graceful fallback)
# ============================================================

try:
    from quarry.net.client import ClientFactory, SpawningClientProtocol  # type: ignore
    from quarry.net.auth import OfflineProfile  # type: ignore
    from twisted.internet import reactor as _twisted_reactor  # type: ignore

    QUARRY_AVAILABLE = True
except Exception as _quarry_import_err:  # pragma: no cover
    QUARRY_AVAILABLE = False
    _twisted_reactor = None
    _QUARRY_ERR = str(_quarry_import_err)
    SpawningClientProtocol = object  # type: ignore


_reactor_thread: Optional[threading.Thread] = None
_reactor_started = threading.Event()


def _start_reactor_once() -> None:
    """Поднимает Twisted reactor в daemon-потоке (один раз на жизнь процесса)."""
    global _reactor_thread
    if not QUARRY_AVAILABLE or _reactor_started.is_set():
        return

    def _run() -> None:
        try:
            _twisted_reactor.run(installSignalHandlers=False)
        except Exception:
            logger.exception("twisted reactor crashed")

    _reactor_thread = threading.Thread(
        target=_run, daemon=True, name="MCD-Reactor",
    )
    _reactor_thread.start()
    _reactor_started.set()
    _log("Twisted reactor запущен (фоновый поток).")


def _parse_map_packet(buff: Any) -> Optional[dict]:
    """Best-effort разбор Map Data пакета в tile-dict для image-капчи.

    Формат пакета версионнозависим; парсим максимально терпимо и при любой
    неожиданности возвращаем None (вызывающий код всё обернёт в try/except).
    Возвращает {'mapId', 'data', 'x', 'y', 'rotation'} либо None, если в пакете
    нет полного блока 128×128 (частичные/иконочные обновления игнорируем).

    Раскладка тайлов по рамкам (item-frames) в самом map-пакете не приходит —
    x/y/rotation проставляются нулями; реальную сетку капчи восстанавливает
    отдельный слой (метаданные сущностей), здесь же — данные одного тайла."""
    try:
        map_id = buff.unpack_varint()
        buff.unpack("b")  # scale
    except Exception:
        return None
    # пробуем пройти необязательные булевы поля (tracking/locked) и иконки
    try:
        # 1.17+: locked (bool). 1.9-1.16: tracking position (bool) [+ locked 1.14+]
        # читаем до двух булевых, затем массив иконок
        flag1 = buff.unpack("?")
        cols = None
        # икона-массив присутствует только если tracking/has-icons; пробуем варинт-длину
        try:
            icon_count = buff.unpack_varint()
            for _ in range(icon_count):
                buff.unpack_varint()  # type
                buff.unpack("b")      # x
                buff.unpack("b")      # z
                buff.unpack("b")      # direction
                if buff.unpack("?"):
                    buff.unpack_chat()
        except Exception:
            pass
        cols = buff.unpack("B")
        rows = buff.unpack("B")
        buff.unpack("B")  # x offset
        buff.unpack("B")  # z offset
        length = buff.unpack_varint()
        data = buff.read(length)
    except Exception:
        return None
    if cols == 128 and rows == 128 and data and len(data) >= 128 * 128:
        return {"mapId": int(map_id), "data": bytes(data[:128 * 128]),
                "x": 0, "y": 0, "rotation": 0}
    return None


class MCSession:
    """
    Синхронный фасад над quarry-клиентом.
    Каждая операция (connect, send_chat, wait_for) блокирует вызывающий поток
    и общается с reactor через `callFromThread`.

    Если quarry не установлен — все методы кидают RuntimeError с понятным
    текстом для оператора.
    """

    def __init__(self, host: str, port: int, nick: str, version: Any = "auto"):
        if not QUARRY_AVAILABLE:
            raise RuntimeError(
                f"quarry не установлен ({_QUARRY_ERR}). "
                "Авто-установка не сработала — проверьте логи. "
                "Поставьте вручную: pip install quarry twisted"
            )
        _start_reactor_once()
        self.host = host
        self.port = int(port)
        self.nick = nick
        self.version = version
        self.protocol: Optional[Any] = None
        self.factory: Optional[Any] = None
        self.chat_q: "queue.Queue[str]" = queue.Queue()
        self.chat_log: list[str] = []        # последние N сообщений (для скрина)
        self.connected = threading.Event()
        self.disconnected = threading.Event()
        self.disconnect_reason: str = ""
        # ── v1.3.0: трекинг игроков (pay-on-join) и map-пакетов (image-капча) ──
        self.players: set[str] = set()           # нормализованные ники онлайн
        self._players_lock = threading.Lock()
        self._join_waiters: list[tuple[str, threading.Event]] = []
        self.map_tiles: list[dict] = []           # собранные map-тайлы капчи
        self._map_lock = threading.Lock()

    # ------- callbacks из reactor-потока -------

    def _on_chat(self, message: str) -> None:
        """Вызывается из reactor-потока при получении любого чата."""
        self.chat_log.append(message)
        if len(self.chat_log) > 200:
            self.chat_log = self.chat_log[-200:]
        try:
            self.chat_q.put_nowait(message)
        except queue.Full:
            pass

    def _on_connected(self, protocol: Any) -> None:
        self.protocol = protocol
        self.connected.set()

    def _on_disconnected(self, reason: str) -> None:
        self.disconnect_reason = str(reason)
        self.disconnected.set()
        # будим всех, кто ждёт захода игрока — сессия умерла
        with self._players_lock:
            for _, ev in self._join_waiters:
                ev.set()

    # ── v1.3.0: трекинг захода игроков (pay-on-join) ──
    def _on_player_join(self, display_name: str) -> None:
        """Вызывается из reactor-потока при появлении игрока в tab-list."""
        norm = _norm_join_nick(display_name)
        if not norm:
            return
        with self._players_lock:
            self.players.add(norm)
            for target, ev in self._join_waiters:
                if target == norm:
                    ev.set()

    def _on_player_leave(self, display_name: str) -> None:
        norm = _norm_join_nick(display_name)
        with self._players_lock:
            self.players.discard(norm)

    def get_present_players(self) -> set[str]:
        with self._players_lock:
            return set(self.players)

    def wait_for_player_join(self, target_nick: str, timeout: float) -> bool:
        """Ждёт захода игрока target_nick на подсервер (до timeout сек).

        Уже присутствующий ник — мгновенное совпадение (Req 3.2)."""
        norm = _norm_join_nick(target_nick)
        ev = threading.Event()
        with self._players_lock:
            if norm in self.players:
                return True
            if self.disconnected.is_set():
                return False
            self._join_waiters.append((norm, ev))
        try:
            ev.wait(timeout=max(0.0, float(timeout)))
        finally:
            with self._players_lock:
                try:
                    self._join_waiters.remove((norm, ev))
                except ValueError:
                    pass
        with self._players_lock:
            return norm in self.players

    # ── v1.3.0: сбор map-пакетов (image-капча / BotFilter) ──
    def _on_map_packet(self, tile: dict) -> None:
        """Вызывается из reactor-потока при получении map-пакета."""
        with self._map_lock:
            self.map_tiles.append(tile)

    def collect_map_tiles(self, timeout: float, min_tiles: int = 1) -> list[dict]:
        """Собирает map-тайлы в течение timeout сек (или пока их ≥ min_tiles)."""
        deadline = time.time() + max(0.0, float(timeout))
        while time.time() < deadline:
            with self._map_lock:
                if len(self.map_tiles) >= min_tiles:
                    break
            if self.disconnected.is_set():
                break
            time.sleep(0.2)
        with self._map_lock:
            return list(self.map_tiles)

    # ------- публичный API -------

    def connect(self, timeout: float = 30.0) -> bool:
        """Подключается к серверу. True — если успешно."""
        session = self
        protocol_version_arg = None if self.version == "auto" else int(self.version)

        class _Proto(SpawningClientProtocol):  # type: ignore
            def player_joined(inner_self) -> None:  # noqa: N805
                super().player_joined()
                session._on_connected(inner_self)

            def packet_chat_message(inner_self, buff) -> None:  # noqa: N805
                # 1.7-1.18: Clientbound Chat Message
                try:
                    raw = buff.unpack_chat()
                    text = raw.to_string() if hasattr(raw, "to_string") else str(raw)
                    buff.discard()
                    session._on_chat(text)
                except Exception:
                    buff.discard()

            def packet_system_chat_message(inner_self, buff) -> None:  # noqa: N805
                # 1.19+: System Chat Message
                try:
                    raw = buff.unpack_chat()
                    text = raw.to_string() if hasattr(raw, "to_string") else str(raw)
                    buff.discard()
                    session._on_chat(text)
                except Exception:
                    buff.discard()

            def packet_disguised_chat_message(inner_self, buff) -> None:  # noqa: N805
                # 1.19.1+: Disguised Chat Message
                try:
                    raw = buff.unpack_chat()
                    text = raw.to_string() if hasattr(raw, "to_string") else str(raw)
                    buff.discard()
                    session._on_chat(text)
                except Exception:
                    buff.discard()

            def packet_player_list_item(inner_self, buff) -> None:  # noqa: N805
                # 1.8-1.19.2: Player Info (add/remove players в tab-list).
                # Best-effort: разбираем имена для pay-on-join. На любой ошибке
                # просто отбрасываем буфер (трекинг — не критичная функция).
                try:
                    action = buff.unpack_varint()
                    count = buff.unpack_varint()
                    for _ in range(count):
                        buff.unpack_uuid()
                        if action == 0:  # add player
                            name = buff.unpack_string()
                            props = buff.unpack_varint()
                            for _ in range(props):
                                buff.unpack_string()
                                buff.unpack_string()
                                if buff.unpack("?"):
                                    buff.unpack_string()
                            buff.unpack_varint()  # gamemode
                            buff.unpack_varint()  # ping
                            if buff.unpack("?"):
                                buff.unpack_chat()
                            session._on_player_join(name)
                    buff.discard()
                except Exception:
                    buff.discard()

            def packet_map(inner_self, buff) -> None:  # noqa: N805
                # Map Data — данные капчи BotFilter. Best-effort парсинг
                # (формат версионнозависим); на ошибке отбрасываем.
                try:
                    tile = _parse_map_packet(buff)
                    buff.discard()
                    if tile is not None:
                        session._on_map_packet(tile)
                except Exception:
                    buff.discard()

            def connection_lost(inner_self, reason) -> None:  # noqa: N805
                session._on_disconnected(str(reason))
                try:
                    super().connection_lost(reason)
                except Exception:
                    pass

        try:
            profile = OfflineProfile(self.nick)
        except Exception:
            # на свежих версиях quarry конструктор иной
            profile = OfflineProfile.from_display_name(self.nick)  # type: ignore

        factory = ClientFactory(profile=profile)
        if protocol_version_arg:
            factory.protocol_version = protocol_version_arg
        factory.protocol = _Proto
        self.factory = factory

        def _do_connect() -> None:
            try:
                factory.connect(self.host, self.port)
            except Exception as ex:
                self._on_disconnected(f"connect-fail: {ex}")

        _twisted_reactor.callFromThread(_do_connect)
        ok = self.connected.wait(timeout=timeout)
        if not ok:
            self._on_disconnected("connect-timeout")
            return False
        return True

    def send_chat(self, message: str) -> None:
        if self.protocol is None:
            raise RuntimeError("Сессия не подключена")
        proto = self.protocol

        def _send() -> None:
            try:
                # quarry 1.16+: send chat via packet builder
                if hasattr(proto, "send_chat"):
                    proto.send_chat(message)
                else:
                    # fallback for older quarry
                    proto.send_packet("chat_message", proto.buff_type.pack_string(message))
            except Exception as ex:
                _log(f"send_chat error: {ex}")

        _twisted_reactor.callFromThread(_send)
        self.chat_log.append(f"<{self.nick}> {message}")

    def drain_chat(self) -> None:
        """Очищает накопившуюся очередь чата. Нужно для переиспользуемых
        сессий: иначе wait_for_chat может сматчить старое сообщение
        (например «успешно»/подтверждение) от предыдущего заказа."""
        try:
            while True:
                self.chat_q.get_nowait()
        except queue.Empty:
            pass

    def wait_for_chat(self, regex: str, timeout: float = 15.0) -> Optional[re.Match]:
        """
        Ждёт чат-сообщение, матчащееся regex (re.IGNORECASE | re.UNICODE).
        Возвращает re.Match или None по таймауту.
        """
        deadline = time.time() + timeout
        pat = re.compile(regex, re.IGNORECASE | re.UNICODE)
        while time.time() < deadline:
            if self.disconnected.is_set():
                return None
            try:
                msg = self.chat_q.get(timeout=min(1.0, deadline - time.time()))
            except queue.Empty:
                continue
            m = pat.search(msg)
            if m:
                return m
        return None

    def disconnect(self) -> None:
        if self.protocol is None:
            return

        def _close() -> None:
            try:
                self.protocol.close()
            except Exception:
                pass

        try:
            _twisted_reactor.callFromThread(_close)
        except Exception:
            pass
        self.disconnected.set()
        self.protocol = None

    def get_chat_log(self, last_n: int = 50) -> list[str]:
        return list(self.chat_log[-last_n:])


# ---------- CAPTCHA ----------
# v1.3.0: реестр ожиданий ответа оператора на image-капчу (BotFilter).
# Когда бот отправил картинку капчи оператору в Telegram, он регистрирует
# «ожидание»; TG-обработчик скармливает следующий непустой текст оператора
# через feed_operator_captcha_reply().
_image_captcha_pending: list[dict] = []
_image_captcha_lock = threading.Lock()


def feed_operator_captcha_reply(chat_id: Any, text: str) -> bool:
    """Передаёт текстовый ответ оператора ожидающей image-капче.

    Возвращает True, если ответ принят каким-то ожиданием."""
    text = (text or "").strip()
    if not text:
        return False
    now = time.time()
    with _image_captcha_lock:
        for e in list(_image_captcha_pending):
            if e["expires_at"] <= now:
                continue
            if not e["chats"] or chat_id in e["chats"]:
                e["box"]["answer"] = text
                e["event"].set()
                return True
    return False


def _has_pending_captcha(chat_id: Any) -> bool:
    now = time.time()
    with _image_captcha_lock:
        return any(
            e["expires_at"] > now and (not e["chats"] or chat_id in e["chats"])
            for e in _image_captcha_pending)


def _await_operator_answer_default(timeout: float, op_chats: Any) -> Optional[str]:
    """Дефолтное ожидание ответа оператора через общий реестр."""
    ev = threading.Event()
    box: dict[str, Any] = {"answer": None}
    entry = {"event": ev, "box": box,
             "expires_at": time.time() + max(0.0, float(timeout)),
             "chats": set(op_chats or [])}
    with _image_captcha_lock:
        _image_captcha_pending.append(entry)
    try:
        ev.wait(timeout=max(0.0, float(timeout)))
    finally:
        with _image_captcha_lock:
            try:
                _image_captcha_pending.remove(entry)
            except ValueError:
                pass
    ans = box["answer"]
    return ans if ans else None


def _send_captcha_photo_default(cardinal: Any, png: bytes, caption: str,
                                op_chats: Any) -> None:
    """Дефолтная отправка PNG капчи оператору в Telegram."""
    tg = getattr(cardinal, "telegram", None)
    if tg is None:
        raise RuntimeError("нет Telegram для отправки капчи")
    chats = list(op_chats or [])
    if not chats:
        raise RuntimeError("не задан notify_chats — некуда слать капчу")
    sent = False
    for cid in chats:
        bio = io.BytesIO(png)
        bio.name = "captcha.png"
        tg.bot.send_photo(cid, bio, caption=caption)
        sent = True
    if not sent:
        raise RuntimeError("капча не отправлена ни в один чат")


def solve_captcha(session: MCSession, captcha_cfg: dict[str, Any],
                  log_lines: list[str], *, cardinal: Any = None,
                  send_photo: Optional[Callable[[bytes, str], None]] = None,
                  await_answer: Optional[Callable[[float], Optional[str]]] = None,
                  collect_tiles: Optional[Callable[[float], list[dict]]] = None,
                  op_chats: Optional[list] = None) -> bool:
    """
    Пытается решить CAPTCHA по конфигу подсервера.
    log_lines — буфер строк для скрина (мутабельный, мы туда дописываем).
    Возвращает True, если капча решена/не требуется.

    v1.3.0: тип `image` (BotFilter) — собирает map-пакеты, склеивает PNG и
    отправляет оператору в Telegram, ждёт код и шлёт ответ на сервер. Типы
    `none`/`chat` ведут себя в точности как в v1.2.0 (Req 2.11).
    """
    if not captcha_cfg or not captcha_cfg.get("enabled"):
        return True
    ctype = captcha_cfg.get("type", "chat")
    timeout = float(captcha_cfg.get("timeout_sec", 30))

    if ctype == "none":
        return True

    if ctype == "chat":
        regex = captcha_cfg.get("trigger_regex", "")
        if not regex:
            return True
        m = session.wait_for_chat(regex, timeout=timeout)
        if not m:
            log_lines.append("[CAPTCHA] не появилась за таймаут — продолжаем")
            return True  # сервер не показал капчу — считаем что её нет
        try:
            answer = m.group(1)
        except IndexError:
            log_lines.append("[CAPTCHA] regex без capture-group — пропуск")
            return True
        log_lines.append(f"[CAPTCHA] обнаружена, отвечаю: {answer}")
        respond = captcha_cfg.get("respond_via", "chat")
        if respond == "command":
            session.send_chat(f"/captcha {answer}")
        else:
            session.send_chat(answer)
        return True

    if ctype == "image":
        return _solve_image_captcha(
            session, captcha_cfg, log_lines, timeout,
            cardinal=cardinal, send_photo=send_photo,
            await_answer=await_answer, collect_tiles=collect_tiles,
            op_chats=op_chats)

    # inventory_click и иные типы пока не поддерживаем
    log_lines.append(f"[CAPTCHA] тип '{ctype}' пока не поддержан, требуется ручная настройка")
    return False


def _solve_image_captcha(session: MCSession, captcha_cfg: dict[str, Any],
                         log_lines: list[str], timeout: float, *,
                         cardinal: Any = None,
                         send_photo: Optional[Callable[[bytes, str], None]] = None,
                         await_answer: Optional[Callable[[float], Optional[str]]] = None,
                         collect_tiles: Optional[Callable[[float], list[dict]]] = None,
                         op_chats: Optional[list] = None) -> bool:
    """Image/map (BotFilter) капча. Любой сбой → не решено (Req 2.7–2.10)."""
    # 0) чаты оператора
    if op_chats is None:
        try:
            op_chats = list(_load_config()["settings"].get("notify_chats", []))
        except Exception:
            op_chats = []

    # 1) ждём триггер BotFilter
    trigger = captcha_cfg.get("trigger_regex") or "BotFilter"
    m = session.wait_for_chat(trigger, timeout=timeout)
    if not m:
        log_lines.append("[CAPTCHA-IMG] триггер BotFilter не появился — считаем, капчи нет")
        return True  # капча не активна (Req 2.2)

    log_lines.append("[CAPTCHA-IMG] BotFilter обнаружен, собираю карту…")

    # 2) собираем map-тайлы
    try:
        collector = collect_tiles or (lambda t: session.collect_map_tiles(t))
        tiles = collector(timeout)
    except Exception as ex:
        log_lines.append(f"[CAPTCHA-IMG] ошибка сбора map-пакетов: {ex} (инфра)")
        return False
    if not tiles:
        log_lines.append("[CAPTCHA-IMG] не получено ни одного map-тайла (инфра)")
        return False

    # 3) склеиваем PNG
    try:
        png = _stitch_map_tiles(tiles, _MC_MAP_PALETTE)
    except Exception as ex:
        log_lines.append(f"[CAPTCHA-IMG] ошибка склейки картинки: {ex} (инфра)")
        return False

    # 4) отправляем оператору
    caption = "🤖 BotFilter-капча: пришлите код одним сообщением (в ответ)."
    try:
        if send_photo is not None:
            send_photo(png, caption)
        else:
            _send_captcha_photo_default(cardinal, png, caption, op_chats)
    except Exception as ex:
        log_lines.append(f"[CAPTCHA-IMG] не удалось отправить картинку оператору: {ex} (инфра)")
        return False

    log_lines.append("[CAPTCHA-IMG] картинка отправлена оператору, жду код…")

    # 5) ждём ответ оператора
    try:
        if await_answer is not None:
            answer = await_answer(timeout)
        else:
            answer = _await_operator_answer_default(timeout, op_chats)
    except Exception as ex:
        log_lines.append(f"[CAPTCHA-IMG] ошибка ожидания ответа оператора: {ex} (инфра)")
        return False
    if not answer or not str(answer).strip():
        log_lines.append("[CAPTCHA-IMG] оператор не ответил за таймаут (инфра)")
        return False
    answer = str(answer).strip()

    # 6) отправляем ответ на сервер (без ретраев — Req 2.7)
    respond = captcha_cfg.get("respond_via", "chat")
    try:
        if respond == "command":
            session.send_chat(f"/captcha {answer}")
        else:
            session.send_chat(answer)
    except Exception as ex:
        log_lines.append(f"[CAPTCHA-IMG] ошибка отправки ответа на сервер: {ex} (не решено, без ретрая)")
        return False

    log_lines.append(f"[CAPTCHA-IMG] код '{answer}' отправлен на сервер")
    return True


# ---------- чат-скрин (PIL) ----------
def render_chat_screenshot(
    title: str, subtitle: str, chat_lines: list[str], out_path: str,
) -> bool:
    """
    Рисует PNG-скрин чата в стиле Minecraft. Возвращает True/False.
    Минимальная реализация: тёмный фон, моноспейс-шрифт, цветные префиксы.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont  # type: ignore
    except Exception as ex:
        _log(f"PIL не установлен ({ex}) — скриншот пропущен")
        return False

    width = 900
    line_h = 22
    pad = 18
    title_h = 56
    height = title_h + pad * 2 + line_h * max(1, len(chat_lines)) + 20

    img = Image.new("RGB", (width, height), (24, 24, 28))
    draw = ImageDraw.Draw(img)

    # шрифт: пытаемся системные моноспейс
    font = None
    font_bold = None
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
        "/Library/Fonts/Menlo.ttc",
        "C:/Windows/Fonts/consola.ttf",
    ]
    for c in candidates:
        if os.path.exists(c):
            try:
                font = ImageFont.truetype(c, 16)
                font_bold = ImageFont.truetype(c, 18)
                break
            except Exception:
                continue
    if font is None:
        font = ImageFont.load_default()
        font_bold = font

    # заголовок
    draw.rectangle([0, 0, width, title_h], fill=(40, 40, 50))
    draw.text((pad, 8), title, fill=(255, 215, 0), font=font_bold)
    draw.text((pad, 30), subtitle, fill=(180, 180, 200), font=font)

    # чат
    y = title_h + pad
    for line in chat_lines:
        color = (220, 220, 220)
        s = line
        # подкрашиваем «теги»
        if s.startswith("[CAPTCHA]"):
            color = (255, 165, 0)
        elif s.startswith("[Сервер]") or s.startswith("[Server]"):
            color = (255, 255, 100)
        elif "✓" in s or "успешно" in s.lower() or "выполнен" in s.lower():
            color = (120, 230, 120)
        elif "ошибк" in s.lower() or "недостат" in s.lower():
            color = (255, 100, 100)
        elif s.startswith("<"):
            color = (160, 200, 255)
        # обрезаем по ширине
        max_chars = 95
        if len(s) > max_chars:
            s = s[: max_chars - 1] + "…"
        draw.text((pad, y), s, fill=color, font=font)
        y += line_h

    try:
        img.save(out_path, "PNG")
        return True
    except Exception as ex:
        _log(f"render_chat_screenshot save fail: {ex}")
        return False



# ============================================================
# БИЗНЕС-ЛОГИКА ВЫДАЧИ
# ============================================================

# Активные сессии донор-аккаунтов: nick → MCSession
_donor_sessions: dict[str, MCSession] = {}
_donor_sessions_lock = threading.Lock()

# Глобальная ссылка на cardinal — нужна, чтобы алертить оператора в TG
# из воркер-потоков (например, при детекте 2FA-запроса).
_cardinal_ref: Optional["Cardinal"] = None


def _alert_admin(text: str) -> None:
    """Тонкая обёртка над _notify_tg для использования из воркеров."""
    try:
        if _cardinal_ref is not None:
            _notify_tg(_cardinal_ref, text)
    except Exception:
        logger.exception("alert_admin failed")


def _get_or_open_session(donor: dict[str, Any], settings: dict[str, Any]) -> MCSession:
    """Берёт живую сессию донора или открывает новую."""
    nick = donor["nick"]
    with _donor_sessions_lock:
        s = _donor_sessions.get(nick)
        if s is not None and not s.disconnected.is_set():
            return s

    sub_settings = resolve_subserver_settings(donor["server"], donor["subserver"])
    s = MCSession(
        host=sub_settings["host"], port=sub_settings["port"],
        nick=nick, version=sub_settings.get("version", "auto"),
    )
    _log(f"Donor {nick}: connecting to {sub_settings['host']}:{sub_settings['port']}")
    if not s.connect(timeout=30.0):
        update_donor(nick, status="offline", last_error=s.disconnect_reason)
        raise RuntimeError(f"Не подключиться: {s.disconnect_reason}")

    # /login
    pwd = donor.get("login_pass") or ""
    if pwd:
        login_cmd = sub_settings["login_cmd"].format(password=pwd)
        s.send_chat(login_cmd)
        time.sleep(float(settings.get("login_post_delay_sec", 2)))

    # CAPTCHA после логина
    chat_log_buf: list[str] = []
    captcha_ok = solve_captcha(s, sub_settings.get("captcha") or {}, chat_log_buf,
                               cardinal=_cardinal_ref)
    if not captcha_ok:
        s.disconnect()
        raise RuntimeError("CAPTCHA не решена")

    # 2FA через ВК/Telegram (FunTime/HolyWorld после логина могут попросить
    # подтвердить вход в личных сообщениях). Бот сам подтвердить не может —
    # детектируем триггер, алертим оператора и ждём «успешной авторизации».
    if sub_settings.get("twofa_enabled"):
        twofa_re = sub_settings["twofa_trigger"]
        login_ok_re = sub_settings["login_success_regex"]
        # ждём до 10 сек: или успех (2FA не требуется), или триггер 2FA
        m = s.wait_for_chat(f"(?:{twofa_re})|(?:{login_ok_re})", timeout=10.0)
        if m and re.search(twofa_re, m.group(0), re.IGNORECASE | re.UNICODE):
            wait_sec = int(sub_settings.get("twofa_wait_sec", 300))
            _alert_admin(
                f"🚨 Донор {nick} ({sub_settings['server_label']}): "
                f"требуется 2FA через ВК/Telegram. Подтверди вход в личных "
                f"сообщениях. Жду до {wait_sec} сек.\n"
                f"Если не подтвердишь — этот ордер уйдёт в FAIL."
            )
            _log(f"Donor {nick}: 2FA pending, waiting up to {wait_sec}s")
            m2 = s.wait_for_chat(login_ok_re, timeout=wait_sec)
            if not m2:
                s.disconnect()
                _alert_admin(
                    f"❌ Донор {nick}: 2FA timeout ({wait_sec}s) — "
                    f"подтверждение через ВК/ТГ не получено."
                )
                raise RuntimeError("2FA timeout — оператор не подтвердил вход в ВК/ТГ")
            _alert_admin(f"✅ Донор {nick}: 2FA подтверждена, продолжаю.")
            _log(f"Donor {nick}: 2FA confirmed, login OK")

    # Сохраняем
    with _donor_sessions_lock:
        _donor_sessions[nick] = s
    update_donor(nick, status="online", last_used=time.time())
    return s


def _close_idle_sessions(idle_sec: int) -> None:
    now = time.time()
    with _donor_sessions_lock:
        for nick, s in list(_donor_sessions.items()):
            last = float(getattr(s, "last_action_at", now))
            if now - last > idle_sec:
                try:
                    s.disconnect()
                except Exception:
                    pass
                _donor_sessions.pop(nick, None)


def _humanize_amount(n: int | str) -> str:
    try:
        return f"{int(n):,}".replace(",", " ")
    except (TypeError, ValueError):
        return str(n)


def deliver_payment(
    server_alias: str, subserver: str, target_nick: str, total_amount: int,
    order_id: str, lot_overrides: Optional[dict] = None,
) -> tuple[bool, str, Optional[str]]:
    """
    Полный цикл выдачи: выбор донора → сессия → /server → /pay → confirm → парс.
    Возвращает (success, human_message, screenshot_path|None).
    """
    cfg = _load_config()
    settings = cfg["settings"]

    if not _get_rate_limiter().allow():
        return False, "Превышен лимит выдач/мин (anti-abuse).", None

    if total_amount > int(settings.get("max_per_order", 1_000_000)):
        return False, f"Сумма {total_amount} превышает лимит на заказ.", None

    sub_settings = resolve_subserver_settings(server_alias, subserver)
    if not sub_settings.get("host"):
        return False, f"Сервер '{server_alias}' не настроен.", None

    donors = find_donors(server_alias, subserver, min_balance=total_amount)
    if not donors:
        return False, f"Нет доступных доноров на {server_alias} · {subserver} с балансом ≥ {total_amount}", None

    last_err = ""
    for donor in donors:
        try:
            ok, msg, scr = _try_pay_via_donor(
                donor, sub_settings, target_nick, total_amount, order_id, settings,
                lot_overrides=lot_overrides,
            )
            if ok:
                return True, msg, scr
            last_err = msg
        except Exception as ex:
            last_err = str(ex)
            _log(f"Donor {donor.get('nick')} fail: {ex}")
            update_donor(donor["nick"], status="offline", last_error=str(ex))
            continue
    return False, f"Все доноры не сработали. Последняя ошибка: {last_err}", None


def _try_pay_via_donor(
    donor: dict[str, Any], sub: dict[str, Any], nick: str, amount: int,
    order_id: str, settings: dict[str, Any], lot_overrides: Optional[dict] = None,
) -> tuple[bool, str, Optional[str]]:
    """Одна попытка выдачи через конкретного донора."""
    # v1.2.0: per-lot override команд поверх server+subserver резолва.
    cmds = _resolve_commands({}, sub, lot_overrides or {})
    session = _get_or_open_session(donor, settings)
    # Сессия могла быть переиспользована (живой коннект от прошлого заказа).
    # Чистим очередь чата, иначе wait_for_chat ниже может сматчить старое
    # «успешно»/подтверждение и дать ложный результат.
    session.drain_chat()
    chat_buf: list[str] = []
    chat_buf.append(f"[Сервер] Донор {donor['nick']} вошёл")

    # 1) /server <subserver>
    switch_cmd = cmds["switch_cmd"].format(subserver=sub["subserver"])
    chat_buf.append(f"<{donor['nick']}> {switch_cmd}")
    session.send_chat(switch_cmd)
    time.sleep(_jitter(settings))

    # ждём подтверждение свича (не критично — некоторые серверы ничего не пишут)
    session.wait_for_chat(
        rf"(?:подключ|зашли|joined|server)\s*.*{re.escape(sub['subserver'])}",
        timeout=float(settings.get("switch_timeout_sec", 15)),
    )

    dry_run = bool(settings.get("dry_run"))
    empty_cmd = settings.get("empty_command") or "/money"

    # ── v1.3.0: КЛАН-ВЫДАЧА (по умолчанию выключена → поведение v1.2.0) ──
    # После свича и ПЕРЕД /pay: читаем баланс казны, при достатке снимаем
    # нужную сумму (/clan withdraw), затем продолжаем к обычному /pay.
    clan_withdraw_done = ""   # для тест-отчёта
    if sub.get("clan_flow"):
        bal_cmd = (cmds.get("clan_balance_cmd") or sub["clan_balance_cmd"]).format(
            nick=nick, amount=amount, subserver=sub["subserver"])
        clan_regex = sub["clan_balance_regex"]
        clan_to = float(sub.get("clan_balance_timeout_sec", 10))
        chat_buf.append(f"<{donor['nick']}> {bal_cmd}")
        session.send_chat(bal_cmd)
        m_bal = session.wait_for_chat(clan_regex, timeout=clan_to)
        balance = _parse_clan_balance(m_bal.string, clan_regex) if m_bal else None
        action, reason = _clan_decide(balance, int(amount))
        chat_buf.append(f"[КЛАН] баланс казны: {balance if balance is not None else '???'} "
                        f"· нужно {amount} · решение: {action}")
        if action == "parse_fail":
            _alert_admin(
                f"🏰 <b>MinecraftDonate</b>: не удалось прочитать баланс казны "
                f"(инфра-сбой, таймаут).\nСервер: <b>{sub['server_label']}</b> · "
                f"{sub['subserver']}\nЗаказ #{order_id}.")
            msg = (f"FAIL: таймаут чтения баланса казны клана "
                   f"({sub['server_label']} · {sub['subserver']})")
            _log(msg)
            return False, msg, None
        if action == "insufficient":
            _alert_admin(
                f"🏰 <b>MinecraftDonate</b>: недостаточно средств в казне клана.\n"
                f"Сервер: <b>{sub['server_label']}</b> · {sub['subserver']}\n"
                f"Требуется: <b>{amount}</b>, в казне: <b>{balance}</b>\n"
                f"Заказ #{order_id} — выдача отменена (нужен refund).")
            msg = (f"FAIL: недостаточно средств в казне клана "
                   f"(в казне {balance}, требуется {amount})")
            _log(msg)
            return False, msg, None
        # action == 'pay' → снимаем нужную сумму из казны (в тесте — empty)
        withdraw_cmd = (cmds.get("clan_withdraw_cmd") or sub["clan_withdraw_cmd"]).format(
            nick=nick, amount=amount, subserver=sub["subserver"])
        pay_real = cmds["pay_cmd"].format(nick=nick, amount=amount)
        eff_withdraw, _eff_pay = _effective_clan_and_pay(
            dry_run, empty_cmd, withdraw_cmd, pay_real)
        clan_withdraw_done = withdraw_cmd
        if dry_run:
            chat_buf.append(f"[ТЕСТ] клан-withdraw НЕ отправлен (было бы: {withdraw_cmd}), "
                            f"вместо него: {eff_withdraw}")
        else:
            chat_buf.append(f"<{donor['nick']}> {eff_withdraw}")
        try:
            session.send_chat(eff_withdraw)
            time.sleep(_jitter(settings))
        except Exception as ex:
            chat_buf.append(f"[КЛАН] ошибка отправки withdraw: {ex}")

    # ── v1.3.0: ВЫДАЧА ПО ЗАХОДУ (pay-on-join, по умолчанию выключена) ──
    if sub.get("pay_on_join"):
        join_to = float(sub.get("join_timeout_sec", 120))
        chat_buf.append(f"[Сервер] жду захода игрока {nick} (до {int(join_to)}с)…")
        joined = session.wait_for_player_join(nick, join_to)
        if not joined:
            _alert_admin(
                f"🎯 <b>MinecraftDonate</b>: игрок <b>{nick}</b> не зашёл на "
                f"<b>{sub['server_label']}</b> · {sub['subserver']} за "
                f"{int(join_to)}с.\nЗаказ #{order_id} — /pay не отправлен.")
            msg = (f"FAIL: игрок {nick} не зашёл на {sub['subserver']} "
                   f"за {int(join_to)}с (pay-on-join)")
            _log(msg)
            return False, msg, None
        chat_buf.append(f"[Сервер] игрок {nick} зашёл — продолжаю выдачу")

    # 2) /pay <nick> <amount>
    pay_cmd = cmds["pay_cmd"].format(nick=nick, amount=amount)
    chat_buf.append(f"<{donor['nick']}> {pay_cmd}")

    if dry_run:
        # v1.2.0: тест-режим — вместо реального /pay шлём безвредную команду.
        chat_buf.append("[ТЕСТ] вместо /pay отправляю безвредную команду: "
                        f"{empty_cmd}")
        try:
            session.send_chat(empty_cmd)
            time.sleep(_jitter(settings))
        except Exception as ex:
            chat_buf.append(f"[ТЕСТ] ошибка отправки тестовой команды: {ex}")
        if cmds.get("confirm_needed") or sub.get("confirm_needed"):
            chat_buf.append(f"<{donor['nick']}> {cmds.get('confirm_cmd', sub['confirm_cmd'])}")
            chat_buf.append("[ТЕСТ] /pay confirm НЕ отправлен")
        chat_buf.append("[ТЕСТ] реального перевода нет (тест-режим)")

        # рендерим скрин с пометкой ТЕСТ
        scr_path = os.path.join(SCREENS_DIR, f"{order_id}.png")
        title = f"[ТЕСТ]  {sub['server_label']}  ·  Order #{order_id}"
        subtitle = (
            f"{time.strftime('%d.%m.%Y %H:%M')}  ·  subserver: {sub['subserver']}"
            f"  ·  donor: {donor['nick']}  ·  ТЕСТ"
        )
        rendered = render_chat_screenshot(title, subtitle, chat_buf, scr_path)
        try:
            with open(scr_path.replace(".png", ".txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(chat_buf))
        except Exception:
            pass
        update_donor(donor["nick"], last_used=time.time(), status="online")
        msg = (
            f"🧪 ТЕСТ OK: команда /pay была бы '{pay_cmd}', реально НЕ выдано "
            f"(донор {donor['nick']})"
        )
        # Req 5.3: расширенный тест-отчёт для новых функций (только если включены —
        # иначе текст байт-в-байт как в v1.2.0).
        _extra: list[str] = []
        if sub.get("clan_flow"):
            _extra.append(
                f"клан-выдача: баланс-команда '{bal_cmd}', /clan withdraw было бы "
                f"'{clan_withdraw_done or '—'}' (реально не отправлено)")
        if sub.get("pay_on_join"):
            _extra.append("pay-on-join: игрок дождан до выдачи")
        if (sub.get("captcha") or {}).get("type") == "image":
            _extra.append("image-капча: пройдена на этапе логина")
        if _extra:
            msg = msg + "\n🧪 " + "; ".join(_extra)
        _log(msg)
        return True, msg, (scr_path if rendered else None)

    session.send_chat(pay_cmd)
    time.sleep(_jitter(settings))

    # 3) Условный confirm: ждём ЛЮБОГО из трёх — success / confirm-prompt / error.
    # На FunTime подтверждение нужно не всегда (часто проходит сразу), а само
    # подтверждение — это ПОВТОР той же команды /pay {nick} {amount},
    # не /pay confirm. На HolyWorld — /pay confirm. Различия задаются в пресете.
    pay_timeout = float(settings.get("pay_timeout_sec", 20))
    success_re = sub["success_regex"]
    error_re = sub["error_regex"]
    confirm_re = sub["confirm_trigger"]
    combined = f"(?P<ok>{success_re})|(?P<cf>{confirm_re})|(?P<er>{error_re})"

    m = session.wait_for_chat(combined, timeout=pay_timeout)
    if m and m.lastgroup == "cf":
        chat_buf.append("[Сервер] требуется подтверждение")
        # confirm_cmd может быть «/pay confirm» (HW) ИЛИ «/pay {nick} {amount}»
        # (FT — повтор) — оба формата поддерживаются через .format().
        try:
            confirm_cmd = sub["confirm_cmd"].format(nick=nick, amount=amount)
        except (KeyError, IndexError):
            confirm_cmd = sub["confirm_cmd"]
        chat_buf.append(f"<{donor['nick']}> {confirm_cmd}")
        session.send_chat(confirm_cmd)
        time.sleep(_jitter(settings))
        # после confirm ждём только success или error
        m = session.wait_for_chat(
            f"(?P<ok>{success_re})|(?P<er>{error_re})",
            timeout=pay_timeout,
        )

    success = bool(m and m.lastgroup == "ok")
    error = bool(m and m.lastgroup == "er")

    # дозабираем последние реплики чата для скрина
    for line in session.get_chat_log(last_n=20):
        if line not in chat_buf:
            chat_buf.append(line)

    # 5) рендерим скрин
    scr_path = os.path.join(SCREENS_DIR, f"{order_id}.png")
    title = f"{sub['server_label']}  ·  Order #{order_id}"
    subtitle = (
        f"{time.strftime('%d.%m.%Y %H:%M')}  ·  subserver: {sub['subserver']}"
        f"  ·  donor: {donor['nick']}"
    )
    rendered = render_chat_screenshot(title, subtitle, chat_buf, scr_path)
    final_scr = scr_path if rendered else None

    # 6) сохраняем текстовый лог
    try:
        with open(scr_path.replace(".png", ".txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(chat_buf))
    except Exception:
        pass

    # 7) обновляем донора
    update_donor(donor["nick"], last_used=time.time(), status="online")

    if success:
        new_balance = int(donor.get("balance_cached") or 0) - int(amount)
        update_donor(donor["nick"], balance_cached=max(0, new_balance))
        msg = f"OK: {amount} → {nick} (донор {donor['nick']})"
        _log(msg)
        _log_action_md("delivery",
                        f"Выдано {amount} → {nick}",
                        donor=donor.get("nick"),
                        new_balance=max(0, new_balance),
                        amount=int(amount), recipient=nick)
        # Триггерим пересчёт активации лотов: баланс мог обнулиться.
        try:
            if _cardinal_ref is not None:
                threading.Thread(
                    target=lambda: _update_lot_activation_md(_cardinal_ref),
                    daemon=True,
                    name="md-lotact-after-deliver").start()
        except Exception:
            pass
        return True, msg, final_scr

    if error:
        msg = f"FAIL: сервер ответил ошибкой ({m.group(0)[:100] if m else '???'})"
        _log(msg)
        return False, msg, final_scr

    msg = f"FAIL: таймаут ожидания ответа от сервера"
    _log(msg)
    return False, msg, final_scr


def _jitter(settings: dict[str, Any]) -> float:
    import random
    a = float(settings.get("command_jitter_min_sec", 1.5))
    b = float(settings.get("command_jitter_max_sec", 3.0))
    return random.uniform(min(a, b), max(a, b))


# ============================================================
# FUNPAY-ФЛОУ
# ============================================================

def _extract_lot_id(order: Any, cardinal: Any = None) -> Any:
    """Реальный id лота FunPay. `OrderShortcut` его не содержит (есть только
    `subcategory.id` — id подкатегории, а не лота!). Если передан `cardinal` —
    тянем полный заказ (`account.get_order().lot_id`); фоллбэк на html и
    `subcategory.id`."""
    order_id = getattr(order, "id", None)
    if cardinal is not None:
        try:
            getter = getattr(getattr(cardinal, "account", None), "get_order", None)
            if callable(getter) and order_id is not None:
                full = getter(str(order_id))
                lid = getattr(full, "lot_id", None)
                if lid:
                    return str(lid)
        except Exception:
            logger.debug("minecraft_donate: get_order для lot_id не удался", exc_info=True)
    html = getattr(order, "html", "") or ""
    for pat in (r'data-offer="(\d+)"', r"offer\?id=(\d+)", r"offers/(\d+)"):
        m = re.search(pat, html)
        if m:
            return m.group(1)
    sub = getattr(order, "subcategory", None)
    return getattr(sub, "id", None) if sub is not None else None


def _find_lot_settings(order: Any, cardinal: Any = None) -> Optional[dict[str, Any]]:
    """
    Ищет настройки лота: сначала по описанию (теги), потом по lot_id из lots.json.
    Возвращает плоский dict с server_alias, subserver, amount_per_unit, currency_label.
    """
    text = (
        (getattr(order, "description", "") or "")
        + " "
        + (getattr(order, "title", "") or "")
        + " "
        + (getattr(order, "full_description", "") or "")
    )
    parsed = parse_lot_description(text)

    lot_id = _extract_lot_id(order, cardinal)

    # override из lots.json
    lots = _load_lots()
    lot_cfg = None
    for lot in lots:
        if lot_id and str(lot.get("lot_id")) == str(lot_id):
            lot_cfg = lot
            break

    if lot_cfg:
        for k in ("server_alias", "subserver", "amount_per_unit", "currency_label"):
            if not parsed.get(k) and lot_cfg.get(k):
                parsed[k] = lot_cfg[k]

    if not parsed.get("server_alias") or not parsed.get("subserver"):
        return None
    if not parsed.get("amount_per_unit"):
        return None

    s = get_server(parsed["server_alias"]) or {}
    if not parsed.get("currency_label"):
        parsed["currency_label"] = s.get("currency_label", "коинов")
    return parsed


def _get_or_create_order_state(order_id: str) -> dict[str, Any]:
    orders = _load_orders()
    for o in orders:
        if str(o.get("order_id")) == str(order_id):
            return o
    return {}


def _save_order_state(order: dict[str, Any]) -> None:
    orders = _load_orders()
    found = False
    for i, o in enumerate(orders):
        if str(o.get("order_id")) == str(order["order_id"]):
            orders[i] = order
            found = True
            break
    if not found:
        orders.append(order)
    _save_orders(orders)


def _drop_order_state(order_id: str) -> None:
    orders = _load_orders()
    orders = [o for o in orders if str(o.get("order_id")) != str(order_id)]
    _save_orders(orders)


def _format_msg(template: str, **vars: Any) -> str:
    try:
        return template.format(**vars)
    except Exception:
        return template


def _on_new_order(cardinal: "Cardinal", event: "NewOrderEvent") -> None:
    cfg = _load_config()
    if not cfg["running"]:
        logger.debug(
            "minecraft_donate: плагин выключен (running=False), "
            "заказ #%s пропущен", getattr(event.order, "id", "?"))
        return
    order = event.order

    lot_settings = _find_lot_settings(order, cardinal)
    if not lot_settings:
        # Не наш лот — НОРМАЛЬНО, DEBUG (без actions.log).
        logger.debug(
            "minecraft_donate: заказ #%s — не наш лот (нет привязки в "
            "lots.json и нет тегов в описании), пропуск",
            getattr(order, "id", "?"))
        return

    # Лот наш — теперь любой выход = инцидент.
    qty = int(getattr(order, "amount", None) or getattr(order, "quantity", 1) or 1)
    amount_per_unit = int(lot_settings["amount_per_unit"])
    total = amount_per_unit * qty
    server_alias = lot_settings["server_alias"]
    subserver = lot_settings["subserver"]
    s = get_server(server_alias) or {}
    server_label = s.get("label", server_alias)
    currency = lot_settings.get("currency_label", "коинов")

    _log_action_md("delivery",
                    f"Получен заказ #{getattr(order, 'id', '?')} → "
                    f"{server_label}/{subserver} ({total} {currency})",
                    order_id=getattr(order, "id", None),
                    buyer=getattr(order, "buyer_username", None),
                    server=server_alias, subserver=subserver,
                    amount=total, currency=currency, qty=qty)

    chat_id = getattr(order, "chat_id", None) or getattr(order, "buyer_id", None)
    if chat_id is None:
        _log(f"Order #{order.id}: no chat_id → cannot ask nick")
        _log_action_md("lot_save_failed",
                        f"Заказ #{order.id} — нет chat_id",
                        order_id=order.id,
                        buyer=getattr(order, "buyer_username", None))
        _notify_tg(cardinal,
                   f"⚠️ <b>Minecraft Donate</b>: заказ "
                   f"<code>#{order.id}</code> — не найден chat_id, "
                   f"запрос ника невозможен. Выдай вручную или верни деньги.")
        return

    # сохраняем состояние ордера
    state = {
        "order_id": str(order.id),
        "chat_id": chat_id,
        "buyer": getattr(order, "buyer_username", None) or "buyer",
        "server_alias": server_alias,
        "subserver": subserver,
        "server_label": server_label,
        "currency": currency,
        "amount_per_unit": amount_per_unit,
        "qty": qty,
        "total": total,
        "stage": "wait_nick",
        "nick": None,
        "created_at": time.time(),
    }
    _save_order_state(state)

    greeting = _format_msg(
        cfg["settings"]["buyer_greeting"],
        order_id=order.id, server_label=server_label, subserver=subserver,
        amount_per_unit=_humanize_amount(amount_per_unit), qty=qty,
        total=_humanize_amount(total), currency=currency,
    )
    if cfg["settings"].get("dry_run"):
        greeting = (
            "🧪 <b>ТЕСТОВЫЙ РЕЖИМ</b> — реальная выдача отключена.\n"
            "Бот пройдёт всю цепочку, но НЕ переведёт коины.\n\n"
        ) + greeting
    try:
        cardinal.send_message(chat_id, greeting)
    except Exception:
        logger.exception("send_message greeting fail")

    _notify_tg(cardinal, f"🛒 #{order.id}: {qty}×{amount_per_unit}={total} {currency} · {server_label}/{subserver}")
    _log(f"Order #{order.id}: greeting отправлено, ждём ник")


def _on_new_message(cardinal: "Cardinal", event: Any) -> None:
    cfg = _load_config()
    if not cfg["running"]:
        return

    msg = event.message
    text = (msg.text or "").strip()
    if not text:
        return
    own_id = getattr(cardinal.account, "id", None)
    if msg.author_id == 0 or msg.author_id == own_id:
        return

    # Среди заказов этого чата, ожидающих ответа, берём САМЫЙ РАННИЙ (FIFO):
    # покупатель отвечает на запросы в том порядке, в каком их задавали,
    # поэтому при нескольких параллельных заказах в одном чате ник/подтверждение
    # не уходят в произвольный заказ.
    candidates = [
        o for o in _load_orders()
        if o.get("chat_id") == msg.chat_id
        and o.get("stage") in ("wait_nick", "wait_confirm")
    ]
    if not candidates:
        return
    state = min(candidates, key=lambda o: float(o.get("created_at", 0) or 0))

    settings = cfg["settings"]
    if state["stage"] == "wait_nick":
        nick_re = settings.get("nick_regex", r"^[A-Za-z0-9_]{3,16}$")
        allowed, reason = _nick_allowed(
            text, nick_re, settings.get("nick_deny_list", []),
            _load_history(), time.time(),
            rate_limit=int(settings.get("nick_rate_limit", 0) or 0),
            window_sec=int(settings.get("nick_rate_window_sec", 86400) or 86400))
        if not allowed:
            buyer_text = {
                "format": "❌ Ник не подходит. Нужно 3-16 символов: латиница, цифры, _.",
                "denied": "🚫 Выдача на этот ник недоступна. Свяжитесь с продавцом.",
                "rate": "🚫 Превышен лимит выдач на этот ник. Свяжитесь с продавцом.",
            }.get(reason, "❌ Ник не подходит.")
            try:
                cardinal.send_message(msg.chat_id, buyer_text)
            except Exception:
                pass
            if reason in ("denied", "rate"):
                _notify_tg(cardinal,
                           f"🚫 Антифрод: ник <b>{text}</b> отклонён ({reason}) "
                           f"по заказу #{state.get('order_id')}.")
            return
        state["nick"] = text
        state["stage"] = "wait_confirm"
        _save_order_state(state)
        confirm_msg = _format_msg(
            settings["buyer_confirm"],
            nick=text, server_label=state["server_label"],
            subserver=state["subserver"],
        )
        try:
            cardinal.send_message(msg.chat_id, confirm_msg)
        except Exception:
            pass
        return

    if state["stage"] == "wait_confirm":
        words = [w.lower() for w in settings.get("confirm_words", ["да", "+"])]
        if text.lower().strip() in words:
            # подтверждено → выдаём
            state["stage"] = "paying"
            _save_order_state(state)
            paying_msg = _format_msg(
                settings["buyer_paying"],
                nick=state["nick"], total=_humanize_amount(state["total"]),
                currency=state["currency"],
            )
            try:
                cardinal.send_message(msg.chat_id, paying_msg)
            except Exception:
                pass
            threading.Thread(
                target=_run_delivery, args=(cardinal, state),
                daemon=True, name=f"MCD-Deliver-{state['order_id']}",
            ).start()
            return
        # не подтверждение — считаем новым ником
        nick_re = settings.get("nick_regex", r"^[A-Za-z0-9_]{3,16}$")
        if re.match(nick_re, text):
            state["nick"] = text
            _save_order_state(state)
            confirm_msg = _format_msg(
                settings["buyer_confirm"],
                nick=text, server_label=state["server_label"],
                subserver=state["subserver"],
            )
            try:
                cardinal.send_message(msg.chat_id, confirm_msg)
            except Exception:
                pass
        else:
            try:
                cardinal.send_message(
                    msg.chat_id,
                    "Подтвердите — напишите <b>ДА</b> или <b>+</b>, "
                    "или пришлите новый ник.",
                )
            except Exception:
                pass


def _run_delivery(cardinal: "Cardinal", state: dict[str, Any]) -> None:
    cfg = _load_config()
    settings = cfg["settings"]
    success, msg, scr_path = deliver_payment(
        state["server_alias"], state["subserver"], state["nick"],
        int(state["total"]), str(state["order_id"]),
        lot_overrides=state.get("cmd_overrides"),
    )

    if success:
        text = _format_msg(
            settings["buyer_success"],
            nick=state["nick"], total=_humanize_amount(state["total"]),
            currency=state["currency"], server_label=state["server_label"],
            subserver=state["subserver"],
        )
        try:
            cardinal.send_message(state["chat_id"], text)
        except Exception:
            pass
        # отправляем скрин (если есть)
        if scr_path and os.path.exists(scr_path):
            try:
                cardinal.send_image(state["chat_id"], scr_path)
            except Exception:
                # fallback: некоторые версии FPC используют другой метод
                try:
                    with open(scr_path, "rb") as f:
                        cardinal.account.send_image(state["chat_id"], f)
                except Exception:
                    logger.exception("send_image fail")

        # история
        h = _load_history()
        h.append({
            "order_id": state["order_id"],
            "buyer": state.get("buyer"),
            "nick": state["nick"],
            "server": state["server_alias"],
            "subserver": state["subserver"],
            "total": state["total"],
            "currency": state["currency"],
            "screenshot": os.path.basename(scr_path) if scr_path else None,
            "ts": time.time(),
            "success": True,
            "message": msg,
        })
        _save_history(h)
        _drop_order_state(state["order_id"])
        _notify_tg(cardinal, f"✅ #{state['order_id']}: {state['total']} → {state['nick']}")
    else:
        try:
            cardinal.send_message(state["chat_id"], settings["buyer_failed"])
        except Exception:
            pass
        state["stage"] = "failed"
        state["last_error"] = msg
        _save_order_state(state)
        h = _load_history()
        h.append({
            "order_id": state["order_id"], "buyer": state.get("buyer"),
            "nick": state["nick"], "server": state["server_alias"],
            "subserver": state["subserver"], "total": state["total"],
            "currency": state["currency"],
            "screenshot": os.path.basename(scr_path) if scr_path else None,
            "ts": time.time(), "success": False, "message": msg,
        })
        _save_history(h)
        _notify_tg(cardinal, f"🚨 FAIL #{state['order_id']}: {msg}")
        # v1.2.0: алерт о недоступности сервера (инфра-сбой, не игровой отказ)
        if _classify_failure(msg) == "infra":
            now = time.time()
            srv = state["server_alias"]
            suppress = int(settings.get("infra_alert_suppress_sec", 600) or 600)
            if _infra_alert_due(_infra_last_alert.get(srv, 0.0), now, suppress):
                _infra_last_alert[srv] = now
                _notify_tg(
                    cardinal,
                    f"⚠️ <b>Minecraft Donate</b>: сервер <b>{state['server_label']}</b> "
                    f"· {state['subserver']} недоступен (инфра-сбой): {msg}")


def _notify_tg(cardinal: "Cardinal", text: str) -> None:
    cfg = _load_config()
    tg = getattr(cardinal, "telegram", None)
    if tg is None:
        return
    for cid in cfg["settings"].get("notify_chats", []):
        try:
            tg.bot.send_message(cid, text)
        except Exception:
            pass



# ============================================================
# TELEGRAM-МЕНЮ
# ============================================================

# ── Общая либа: actions.log + raise-skip + авто-(де)активация по балансу ──
# ── Встроенная либа lot-activation ─────────────────────────────────────────
_CARDINAL_REF_MD = None


def _shared_raise_state_md(cardinal):
    if cardinal is None:
        return None
    acc = getattr(cardinal, "account", None)
    if acc is None:
        return None
    st = getattr(acc, "_lot_raise_skip_state", None)
    if st is None:
        st = {"by_plugin": {}, "patched": False, "orig": None}
        try:
            acc._lot_raise_skip_state = st  # type: ignore[attr-defined]
        except Exception:
            return None
    return st


def _install_raise_skip_shared_md(cardinal) -> bool:
    st = _shared_raise_state_md(cardinal)
    if st is None or st["patched"]:
        return st is not None
    acc = cardinal.account
    if not hasattr(acc, "raise_lots"):
        return False
    orig = acc.raise_lots
    st["orig"] = orig

    def _patched(category_id, subcategories=None, exclude=None):
        try:
            cid = int(category_id)
            combined: set[int] = set()
            owner = None
            for pname, pset in st["by_plugin"].items():
                if cid in pset and owner is None:
                    owner = pname
                combined.update(pset)
            if cid in combined:
                logger.info(
                    "raise-skip: пропуск авто-поднятия категории %s "
                    "(плагин: %s)", cid, owner or "?")
                return True
        except Exception:
            logger.debug("raise-skip: check failed", exc_info=True)
        return orig(category_id, subcategories=subcategories,
                     exclude=exclude)

    _patched._lot_raise_patched = True  # type: ignore[attr-defined]
    acc.raise_lots = _patched  # type: ignore[method-assign]
    st["patched"] = True
    logger.info("md: установлен общий патч raise_lots")
    return True


def _register_skip_md(cardinal, plugin_name: str, category_ids):
    st = _shared_raise_state_md(cardinal)
    if st is None:
        return
    st["by_plugin"][plugin_name] = {int(x) for x in category_ids
                                      if x is not None}


def _get_funpay_account_md(cardinal):
    if cardinal is None:
        return None
    acc = getattr(cardinal, "account", None)
    if acc is not None and (hasattr(acc, "save_lot")
                            or hasattr(acc, "save_offer")):
        return acc
    return None


def _apply_lot_active_md(cardinal, lot_id: int, active: bool) -> bool:
    acc = _get_funpay_account_md(cardinal)
    if acc is None:
        raise RuntimeError("FunPay API недоступен")
    fields = acc.get_lot_fields(int(lot_id))
    if active:
        if (getattr(fields, "amount", None) in (None, 0)
                and not getattr(fields, "auto_delivery", False)):
            try:
                fields.amount = 1
            except Exception:
                pass
        fields.active = True
    else:
        fields.active = False
    if hasattr(acc, "save_lot"):
        acc.save_lot(fields)
    else:
        acc.save_offer(fields)
    return True


def _detect_category_id_md(cardinal, lot_id: int):
    acc = _get_funpay_account_md(cardinal)
    if acc is None or not hasattr(acc, "get_lot_fields"):
        return None
    try:
        fields = acc.get_lot_fields(int(lot_id))
    except Exception:
        return None
    cat = getattr(getattr(fields, "subcategory", None), "category", None)
    cid = getattr(cat, "id", None)
    return int(cid) if cid is not None else None


_ACTIONS_ICONS_MD = {
    "lot_activated":   "✅ ЛОТ ВКЛ ",
    "lot_deactivated": "⛔ ЛОТ ВЫКЛ",
    "lot_save_failed": "⚠ ЛОТ FAIL",
    "delivery":        "📨 ВЫДАЧА  ",
    "acc_balance_low": "💰 БАЛАНС  ",
    "raise_skipped":   "🚫 RAISE   ",
}


def _make_actions_logger_md(plugin_name: str, storage_dir: str):
    try:
        import os as _os
        _os.makedirs(storage_dir, exist_ok=True)
        from logging.handlers import RotatingFileHandler
        log_path = _os.path.join(storage_dir, "actions.log")
        lg = logging.getLogger(f"FPC.{plugin_name}.actions")
        lg.setLevel(logging.INFO)
        lg.propagate = False
        if not any(getattr(h, "_lot_actions_handler", False)
                    for h in lg.handlers):
            handler = RotatingFileHandler(
                log_path, maxBytes=2 * 1024 * 1024,
                backupCount=5, encoding="utf-8")
            handler.setFormatter(logging.Formatter(
                "%(asctime)s %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S"))
            handler._lot_actions_handler = True  # type: ignore[attr-defined]
            lg.addHandler(handler)
        return lg
    except Exception:
        return None


def _do_log_action_md(lg, action: str, summary: str = "", **extra) -> None:
    if lg is None:
        return
    icon = _ACTIONS_ICONS_MD.get(action, f"• {action:10}")
    parts: list[str] = []
    if summary:
        parts.append(summary)
    for k, v in extra.items():
        if v is None or v == "":
            continue
        sv = str(v)
        if len(sv) > 120:
            sv = sv[:117] + "…"
        parts.append(f"{k}={sv}")
    line = f"{icon} | " + " | ".join(parts) if parts else icon
    try:
        lg.info(line)
    except Exception:
        pass


def _common_lib_md():
    try:
        import lot_activation_common  # type: ignore
        return lot_activation_common
    except Exception:
        pass

    class _Shim:
        @staticmethod
        def apply_lot_active(c, lid, act):
            return _apply_lot_active_md(c, int(lid), bool(act))

        @staticmethod
        def install_raise_skip_patch(c):
            return _install_raise_skip_shared_md(c)

        @staticmethod
        def register_skip_categories(pname, ids):
            _register_skip_md(_CARDINAL_REF_MD, pname, ids)

        @staticmethod
        def detect_category_id(c, lid):
            return _detect_category_id_md(c, int(lid))

        @staticmethod
        def make_actions_logger(pname, sdir):
            return _make_actions_logger_md(pname, sdir)

        @staticmethod
        def log_action(lg, action, summary="", **extra):
            _do_log_action_md(lg, action, summary, **extra)

    return _Shim()


_actions_logger_md: "logging.Logger | None" = None
_lotact_md_thread: "threading.Thread | None" = None
_lotact_md_stop = threading.Event()


def _get_actions_logger_md():
    global _actions_logger_md
    if _actions_logger_md is not None:
        return _actions_logger_md
    lib = _common_lib_md()
    if lib is None:
        return None
    _actions_logger_md = lib.make_actions_logger(
        "minecraft_donate", PLUGIN_DIR)
    return _actions_logger_md


def _log_action_md(action: str, summary: str = "", **extra: Any) -> None:
    lib = _common_lib_md()
    if lib is None:
        return
    lib.log_action(_get_actions_logger_md(), action, summary, **extra)


def _total_balance_for(server_alias: str, subserver: str) -> int:
    """Сумма balance_cached всех не-banned доноров для (server, subserver)."""
    if not server_alias or not subserver:
        return 0
    total = 0
    for d in _load_donors():
        if d.get("server") != server_alias:
            continue
        if d.get("subserver") != subserver:
            continue
        if d.get("status") == "banned":
            continue
        try:
            total += int(d.get("balance_cached") or 0)
        except Exception:
            pass
    return total


def _update_lot_activation_md(cardinal: "Cardinal") -> dict[str, Any]:
    """Деактивирует лоты, у которых нет доноров с положительным балансом
    на нужном сервере/подсервере. Реактивирует, когда баланс появляется.

    Работает только для лотов с явным override в lots.json — без него
    плагин не знает, какие доноры нужны для этого лота (server_alias и
    subserver приходят из ТЕГОВ в описании FunPay в момент покупки).

    Возвращает счётчики {activated, deactivated, skipped, failed}.
    """
    counters = {"activated": 0, "deactivated": 0, "skipped": 0,
                "failed": 0, "stopped_reason": None}
    if cardinal is None:
        counters["stopped_reason"] = "cardinal=None"
        return counters
    lib = _common_lib_md()
    if lib is None:
        counters["stopped_reason"] = "lot_activation_common.py не найден"
        return counters
    lots = _load_lots() or []
    if not lots:
        counters["stopped_reason"] = "lots.json пуст"
        return counters

    min_balance = 1  # хоть что-то на донорах должно быть
    for lot in lots:
        try:
            lot_id = int(lot.get("lot_id"))
        except (TypeError, ValueError):
            counters["skipped"] += 1
            continue
        server_alias = lot.get("server_alias")
        subserver = lot.get("subserver")
        if not server_alias or not subserver:
            counters["skipped"] += 1
            continue
        total = _total_balance_for(server_alias, subserver)
        want_active = total >= min_balance
        try:
            lib.apply_lot_active(cardinal, lot_id, want_active)
            if want_active:
                counters["activated"] += 1
                _log_action_md("lot_activated",
                                f"Лот {lot_id} активирован "
                                f"(баланс доноров: {total})",
                                lot_id=lot_id, total_balance=total,
                                server=server_alias, subserver=subserver)
            else:
                counters["deactivated"] += 1
                _log_action_md("lot_deactivated",
                                f"Лот {lot_id} деактивирован — пустые "
                                f"балансы доноров ({server_alias}/{subserver})",
                                lot_id=lot_id, total_balance=total,
                                server=server_alias, subserver=subserver)
                _log_action_md("acc_balance_low",
                                f"Балансы исчерпаны: {server_alias}/{subserver}",
                                server=server_alias, subserver=subserver,
                                total_balance=total)
        except Exception as e:
            counters["failed"] += 1
            _log_action_md("lot_save_failed",
                            f"Не удалось сохранить лот {lot_id}",
                            lot_id=lot_id, want_active=want_active,
                            error=f"{type(e).__name__}: {str(e)[:120]}")
            logger.debug(
                "minecraft_donate: apply_lot_active(%s) failed: %s",
                lot_id, e, exc_info=True)
    return counters


def _refresh_raise_skip_md(cardinal: "Cardinal") -> None:
    """Регистрирует category_id наших лотов для пропуска авто-поднятия."""
    lib = _common_lib_md()
    if lib is None or cardinal is None:
        return
    cat_ids: set[int] = set()
    for lot in _load_lots() or []:
        try:
            lot_id = int(lot.get("lot_id"))
        except (TypeError, ValueError):
            continue
        cid = lib.detect_category_id(cardinal, lot_id)
        if cid is not None:
            cat_ids.add(int(cid))
    lib.register_skip_categories("minecraft_donate", cat_ids)
    if cat_ids:
        logger.info("minecraft_donate: raise-skip категории: %s",
                    sorted(cat_ids))


def _lotact_loop_md(cardinal: "Cardinal") -> None:
    """Фоновый поток: раз в 5 минут пересчитывает активацию лотов."""
    INTERVAL = 5 * 60
    while not _lotact_md_stop.is_set():
        try:
            res = _update_lot_activation_md(cardinal)
            if res.get("activated") or res.get("deactivated"):
                logger.info(
                    "minecraft_donate: lot activation: +%d / -%d",
                    res.get("activated", 0), res.get("deactivated", 0))
        except Exception:
            logger.debug("minecraft_donate: lotact loop iter failed",
                         exc_info=True)
        _lotact_md_stop.wait(INTERVAL)


def _init(cardinal: "Cardinal", *_: Any) -> None:
    global _cardinal_ref, _lotact_md_thread, _CARDINAL_REF_MD
    _cardinal_ref = cardinal
    _CARDINAL_REF_MD = cardinal
    _ensure_dir()
    cfg = _load_config()
    _save_config(cfg)

    # ── Авто-(де)активация + raise-skip ──
    lib = _common_lib_md()
    if lib is not None:
        try:
            lib.install_raise_skip_patch(cardinal)

            def _bootstrap_md():
                try:
                    _refresh_raise_skip_md(cardinal)
                except Exception:
                    pass
                try:
                    _update_lot_activation_md(cardinal)
                except Exception:
                    pass
            threading.Thread(
                target=_bootstrap_md, daemon=True,
                name="md-lotact-bootstrap").start()
            if not (_lotact_md_thread and _lotact_md_thread.is_alive()):
                _lotact_md_stop.clear()
                _lotact_md_thread = threading.Thread(
                    target=_lotact_loop_md, args=(cardinal,), daemon=True,
                    name="md-lotact-loop")
                _lotact_md_thread.start()
        except Exception:
            logger.debug("minecraft_donate: lot-activation setup failed",
                         exc_info=True)

    if QUARRY_AVAILABLE:
        _start_reactor_once()
    else:
        _log("⚠️ quarry недоступен (авто-установка не сработала). "
             "Поставьте вручную: pip install quarry twisted")

    tg = getattr(cardinal, "telegram", None)
    if tg is None:
        _log("Telegram отключен — управление только через JSON-конфиги.")
        return

    bot = tg.bot

    # ------- helpers -------
    def _cb(prefix: str):
        return lambda c: c.data == prefix or c.data.startswith(prefix + ":")

    def _render(c: CallbackQuery, text: str, kb: K) -> None:
        try:
            bot.edit_message_text(text, c.message.chat.id, c.message.id,
                                  reply_markup=kb, parse_mode="HTML")
        except Exception as _edit_ex:
            # "message is not modified" — пользователь уже видит то, что
            # нужно (тот же текст/кнопки). Не шлём дубль-сообщение.
            if "not modified" in str(_edit_ex).lower():
                logger.debug(
                    "minecraft_donate: _render noop (message not modified)")
                return
            logger.warning(
                "minecraft_donate: _render edit failed, falling back to "
                "send_message (chat=%s msg=%s): %s",
                c.message.chat.id, c.message.id, _edit_ex)
            bot.send_message(c.message.chat.id, text, reply_markup=kb,
                             parse_mode="HTML")

    def _set_state(chat_id: int, msg_id: int, user_id: int, state: str,
                   data: Optional[dict] = None) -> None:
        try:
            tg.set_state(chat_id, msg_id, user_id, state, data or {})
        except TypeError:
            tg.set_state(chat_id, msg_id, user_id, state)

    def _get_state(m: Message) -> dict:
        st = tg.get_state(m.chat.id, m.from_user.id)
        return st or {}

    # ------- main page -------
    def _status_text() -> str:
        c = _load_config()
        servers = _load_servers()
        donors = _load_donors()
        orders = _load_orders()
        history = _load_history()
        st = "🟢 запущен" if c["running"] else "🔴 остановлен"
        quarry = "✅" if QUARRY_AVAILABLE else "❌ не установлен"
        dry = "🧪 ТЕСТ (dry-run)" if c["settings"].get("dry_run") else "🔴"
        lots = _load_lots()
        hints = []
        if not QUARRY_AVAILABLE:
            hints.append("• установите библиотеку quarry (см. ❓ Как настроить)")
        if not servers:
            hints.append("• добавьте сервер (🖥 Серверы → пресет FunTime/HolyWorld)")
        if not donors:
            hints.append("• добавьте донора-аккаунт (👤 Доноры)")
        if not lots:
            hints.append("• привяжите лот (🛒 Лоты)")
        if not c["running"]:
            hints.append("• нажмите «▶️ Запустить»")
        hint_block = ("\n<b>⚠️ Чтобы заработало:</b>\n" + "\n".join(hints)) if hints else \
            "\n✅ Всё готово к работе."
        return (
            f"<b>MinecraftDonate</b>\n"
            f"Авто-выдача валюты в Minecraft (FunTime/HolyWorld/любой).\n\n"
            f"📊 Статус: <b>{st}</b>\n"
            f"🧪 Dry-run: <b>{dry}</b>\n"
            f"📡 quarry: {quarry}\n"
            f"🖥 Серверов: <b>{len(servers)}</b>\n"
            f"👤 Доноров: <b>{len(donors)}</b>\n"
            f"⏳ Активных ордеров: <b>{len(orders)}</b>\n"
            f"📜 История: <b>{len(history)}</b>\n"
            f"{hint_block}"
        )

    def _help_text() -> str:
        return (
            "<b>❓ Как настроить MinecraftDonate</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "1️⃣ <b>Сервер.</b> «🖥 Серверы» → выберите готовый пресет "
            "(FunTime/HolyWorld) или добавьте свой (host, порт, команда оплаты, "
            "подсерверы). Кнопкой проверки можно протестировать вход.\n\n"
            "2️⃣ <b>Донор.</b> «👤 Доноры» → добавьте игровой аккаунт-донор, "
            "с которого будет уходить валюта (ник + пароль). «💰 Баланс» проверит "
            "вход и средства.\n\n"
            "3️⃣ <b>Лот.</b> «🛒 Лоты» → привяжите лот: <b>название лота как на "
            "FunPay</b> (или теги в описании) → сервер, подсервер, сколько валюты "
            "за единицу.\n\n"
            "4️⃣ <b>Проверка.</b> Включите «🧪 Dry-run» и нажмите «🧪 Тестовая "
            "выдача» — это симуляция без реального перевода. Когда всё ок — "
            "выключите Dry-run и нажмите «▶️ Запустить».\n\n"
            "💡 На оплаченный заказ бот спросит у покупателя ник и переведёт валюту."
        )

    def _kb_main() -> K:
        c = _load_config()
        kb = K()
        run_btn = B(
            "⏹ Остановить" if c["running"] else "▶️ Запустить",
            callback_data=CBT_START,
        )
        kb.add(run_btn)
        dry_lbl = "🧪 Dry-run: ВКЛ" if c["settings"].get("dry_run") else "🧪 Dry-run: ВЫКЛ"
        kb.add(B(dry_lbl, callback_data=CBT_DRY_RUN))
        kb.add(B("🧪 Тестовая выдача (без FunPay)", callback_data=CBT_TEST_DELIVERY))
        kb.row(
            B("🖥 Серверы", callback_data=CBT_TAB_SERVERS),
            B("👤 Доноры", callback_data=CBT_TAB_DONORS),
        )
        kb.row(
            B("🛒 Лоты", callback_data=CBT_TAB_LOTS),
            B("⏳ Ордеры", callback_data=CBT_TAB_ORDERS),
        )
        kb.row(
            B("📜 История", callback_data=CBT_TAB_HISTORY),
            B("⚙️ Настройки", callback_data=CBT_TAB_SETTINGS),
        )
        kb.row(B("📋 Логи", callback_data=CBT_TAB_LOGS),
               B("❓ Как настроить", callback_data=CBT_HELP))
        kb.row(B("💛 Донат", callback_data=f"{DONATION_CALLBACK_PREFIX}:donate"))
        return kb

    def open_main(c: CallbackQuery) -> None:
        _render(c, _status_text(), _kb_main())
        bot.answer_callback_query(c.id)

    def open_help(c: CallbackQuery) -> None:
        _render(c, _help_text(), K().add(B("⬅️ Назад", callback_data=CBT_OPEN)))
        bot.answer_callback_query(c.id)

    def toggle_running(c: CallbackQuery) -> None:
        cfg2 = _load_config()
        cfg2["running"] = not cfg2["running"]
        _save_config(cfg2)
        _log(f"running = {cfg2['running']}")
        open_main(c)

    def toggle_dry_run(c: CallbackQuery) -> None:
        cfg2 = _load_config()
        cfg2["settings"]["dry_run"] = not cfg2["settings"].get("dry_run")
        _save_config(cfg2)
        _log(f"dry_run = {cfg2['settings']['dry_run']}")
        open_main(c)

    def ask_test_delivery(c: CallbackQuery) -> None:
        cfg2 = _load_config()
        warn = ""
        if not cfg2["settings"].get("dry_run"):
            warn = (
                "\n\n⚠️ <b>Dry-run выключен</b> — этот тест отправит "
                "РЕАЛЬНУЮ команду /pay донором. Чтобы только симулировать "
                "(без реального перевода), сначала включите Dry-run кнопкой выше."
            )
        m = bot.send_message(
            c.message.chat.id,
            "🧪 Тестовая выдача (без FunPay-ордера).\n\n"
            "Пришли строкой:\n"
            "<code>server|subserver|nick|amount</code>\n\n"
            "Пример:\n<code>funtime|anarchy121|TestPlayer|1</code>"
            + warn,
            parse_mode="HTML",
        )
        _set_state(m.chat.id, m.message_id, c.from_user.id, ST_AWAIT_TEST)
        bot.answer_callback_query(c.id)

    def on_test_delivery(m: Message) -> None:
        tg.clear_state(m.chat.id, m.from_user.id, True)
        parts = [p.strip() for p in (m.text or "").split("|")]
        if len(parts) < 4:
            bot.send_message(m.chat.id, "Нужно 4 поля через |")
            return
        server, sub, nick, amount_str = parts[0], parts[1], parts[2], parts[3]
        try:
            amount = int(amount_str)
        except ValueError:
            bot.send_message(m.chat.id, "amount — число")
            return
        cfg2 = _load_config()
        mode = "🧪 DRY-RUN" if cfg2["settings"].get("dry_run") else "💸 РЕАЛЬНЫЙ"
        bot.send_message(m.chat.id, f"{mode}: запускаю выдачу {amount} → {nick}...")

        def _worker() -> None:
            try:
                order_id = f"TEST-{int(time.time())}"
                ok, msg, scr = deliver_payment(
                    server, sub, nick, amount, order_id,
                )
                ico = "✅" if ok else "❌"
                bot.send_message(
                    m.chat.id,
                    f"{ico} {msg}",
                    parse_mode="HTML",
                )
                if scr and os.path.exists(scr):
                    try:
                        with open(scr, "rb") as f:
                            bot.send_photo(m.chat.id, f, caption=f"{mode}")
                    except Exception:
                        logger.exception("send photo fail")
            except Exception as ex:
                bot.send_message(m.chat.id, f"❌ Исключение: {ex}")

        threading.Thread(target=_worker, daemon=True).start()

    # ------- серверы -------
    def open_servers(c: CallbackQuery) -> None:
        servers = _load_servers()
        lines = ["<b>🖥 Серверы</b>\n"]
        if not servers:
            lines.append("Пусто. Применить пресет ↓")
        else:
            for alias, s in servers.items():
                subs = s.get("subservers") or {}
                lines.append(
                    f"<b>{s.get('label', alias)}</b> "
                    f"(<code>{alias}</code>) · {s.get('host', '?')}:{s.get('port', '?')} · "
                    f"подсерверов: {len(subs)}"
                )
        kb = K()
        for alias in servers:
            kb.add(B(f"⚙️ {servers[alias].get('label', alias)}",
                     callback_data=f"{CBT_SRV_DETAIL}:{alias}"))
        kb.row(
            B("📦 Пресет: FunTime", callback_data=f"{CBT_SRV_PRESET}:funtime"),
            B("📦 Пресет: HolyWorld", callback_data=f"{CBT_SRV_PRESET}:holyworld"),
        )
        kb.add(B("➕ Свой сервер", callback_data=CBT_SRV_ADD))
        kb.add(B("⬅️ Назад", callback_data=CBT_OPEN))
        _render(c, "\n".join(lines), kb)
        bot.answer_callback_query(c.id)

    def apply_server_preset(c: CallbackQuery) -> None:
        preset_key = c.data.split(":")[-1]
        if apply_preset(preset_key, preset_key):
            bot.answer_callback_query(c.id, f"Пресет {preset_key} применён")
        else:
            bot.answer_callback_query(c.id, "Ошибка пресета")
        open_servers(c)

    def open_server_detail(c: CallbackQuery) -> None:
        alias = c.data.split(":")[-1]
        s = get_server(alias)
        if not s:
            bot.answer_callback_query(c.id, "Не найдено")
            return
        subs = s.get("subservers") or {}
        text = (
            f"<b>🖥 {s.get('label', alias)}</b>\n"
            f"alias: <code>{alias}</code>\n"
            f"host: <code>{s.get('host', '?')}:{s.get('port', '?')}</code>\n"
            f"login_cmd: <code>{s.get('login_cmd', '/login {password}')}</code>\n"
            f"pay_cmd: <code>{s.get('default_pay_cmd', '/pay {nick} {amount}')}</code>\n"
            f"switch_cmd: <code>{s.get('default_switch_cmd', '/server {subserver}')}</code>\n"
            f"confirm_needed: <b>{s.get('default_confirm_needed', False)}</b>\n"
            f"подсерверов: <b>{len(subs)}</b>\n"
        )
        kb = K()
        for sub_name in sorted(subs.keys()):
            kb.row(
                B(f"🧩 {sub_name}", callback_data=f"{CBT_SUB_DETAIL}:{alias}:{sub_name}"),
                B("🗑", callback_data=f"{CBT_SUB_DEL}:{alias}:{sub_name}"),
            )
        kb.add(B("➕ Свой подсервер", callback_data=f"{CBT_SUB_ADD}:{alias}"))
        kb.row(
            B("✏️ host/port", callback_data=f"{CBT_SRV_EDIT}:{alias}:host"),
            B("✏️ pay_cmd", callback_data=f"{CBT_SRV_EDIT}:{alias}:default_pay_cmd"),
        )
        kb.row(
            B("✏️ switch_cmd", callback_data=f"{CBT_SRV_EDIT}:{alias}:default_switch_cmd"),
            B("✏️ login_cmd", callback_data=f"{CBT_SRV_EDIT}:{alias}:login_cmd"),
        )
        kb.row(
            B("✏️ success_regex", callback_data=f"{CBT_SRV_EDIT}:{alias}:default_success_regex"),
            B("✏️ error_regex", callback_data=f"{CBT_SRV_EDIT}:{alias}:default_error_regex"),
        )
        kb.row(
            B("✏️ confirm_trigger", callback_data=f"{CBT_SRV_EDIT}:{alias}:default_confirm_trigger"),
            B("✏️ confirm_cmd", callback_data=f"{CBT_SRV_EDIT}:{alias}:default_confirm_cmd"),
        )
        kb.row(
            B("✏️ captcha regex", callback_data=f"{CBT_SRV_EDIT}:{alias}:captcha_regex"),
            B("✏️ currency_label", callback_data=f"{CBT_SRV_EDIT}:{alias}:currency_label"),
        )
        kb.add(B("🏰🤖🎯 Клан / Капча / Заход",
                 callback_data=f"{CBT_SRV_FEATURES}:{alias}"))
        kb.add(B("🗑 Удалить сервер", callback_data=f"{CBT_SRV_DEL}:{alias}"))
        kb.add(B("⬅️ Назад", callback_data=CBT_TAB_SERVERS))
        _render(c, text, kb)
        bot.answer_callback_query(c.id)

    def add_server_ask(c: CallbackQuery) -> None:
        m = bot.send_message(
            c.message.chat.id,
            "Создать свой сервер. Пришли строкой:\n"
            "<code>alias|label|host|port</code>\n\n"
            "Пример:\n<code>mycube|MyCube|play.mycube.ru|25565</code>",
            parse_mode="HTML",
        )
        _set_state(m.chat.id, m.message_id, c.from_user.id, ST_AWAIT_SRV)
        bot.answer_callback_query(c.id)

    def on_add_server(m: Message) -> None:
        tg.clear_state(m.chat.id, m.from_user.id, True)
        parts = [p.strip() for p in (m.text or "").split("|")]
        if len(parts) < 4:
            bot.send_message(m.chat.id, "Нужно 4 поля через |")
            return
        alias, label, host, port = parts[0], parts[1], parts[2], parts[3]
        try:
            port_i = int(port)
        except ValueError:
            bot.send_message(m.chat.id, "Порт — число")
            return
        servers = _load_servers()
        s = servers.setdefault(alias, {})
        s["label"] = label
        s["host"] = host
        s["port"] = port_i
        s.setdefault("auth_mode", "offline")
        s.setdefault("login_cmd", "/login {password}")
        s.setdefault("default_pay_cmd", "/pay {nick} {amount}")
        s.setdefault("default_switch_cmd", "/server {subserver}")
        s.setdefault("default_confirm_needed", False)
        s.setdefault("default_confirm_trigger", r"подтвер|confirm")
        s.setdefault("default_confirm_cmd", "/pay confirm")
        s.setdefault("default_success_regex", r"перев[её]д.*выполн|успешно")
        s.setdefault("default_error_regex", r"недостат|ошибк|не найден")
        s.setdefault("default_captcha", {"enabled": False, "type": "none"})
        s.setdefault("currency_label", "коинов")
        s.setdefault("subservers", {})
        _save_servers(servers)
        _log(f"+server {alias}")
        bot.send_message(m.chat.id, f"✅ Сервер <b>{label}</b> создан.", parse_mode="HTML")

    def del_server(c: CallbackQuery) -> None:
        alias = c.data.split(":")[-1]
        servers = _load_servers()
        if alias in servers:
            del servers[alias]
            _save_servers(servers)
            _log(f"-server {alias}")
        open_servers(c)

    def edit_server_field(c: CallbackQuery) -> None:
        parts = c.data.split(":")
        alias = parts[-2]
        field = parts[-1]
        m = bot.send_message(
            c.message.chat.id,
            f"Сервер <b>{alias}</b>: введите новое значение поля <code>{field}</code>",
            parse_mode="HTML",
        )
        _set_state(m.chat.id, m.message_id, c.from_user.id, ST_AWAIT_EDIT,
                   {"target": "server", "alias": alias, "field": field})
        bot.answer_callback_query(c.id)

    # ------- подсерверы -------
    def add_subserver_ask(c: CallbackQuery) -> None:
        alias = c.data.split(":")[-1]
        s = get_server(alias) or {}
        suggested = s.get("suggested_subservers", [])
        suggest_text = ""
        if suggested:
            suggest_text = "\nПредложения: <code>" + ", ".join(suggested[:8]) + "</code>"
        m = bot.send_message(
            c.message.chat.id,
            f"Сервер <b>{alias}</b>. Имя нового подсервера "
            f"(например <code>anarchy123</code> или своё):{suggest_text}",
            parse_mode="HTML",
        )
        _set_state(m.chat.id, m.message_id, c.from_user.id, ST_AWAIT_SUB,
                   {"alias": alias})
        bot.answer_callback_query(c.id)

    def on_add_subserver(m: Message) -> None:
        st = _get_state(m).get("data") or {}
        alias = st.get("alias")
        tg.clear_state(m.chat.id, m.from_user.id, True)
        if not alias:
            return
        sub_name = (m.text or "").strip().lower()
        if not re.match(r"^[a-zA-Z0-9_\-]{1,40}$", sub_name):
            bot.send_message(m.chat.id, "Имя: только латиница/цифры/_/-, до 40 симв.")
            return
        add_subserver(alias, sub_name)
        bot.send_message(m.chat.id, f"✅ Подсервер <b>{sub_name}</b> добавлен.", parse_mode="HTML")

    def del_subserver_cb(c: CallbackQuery) -> None:
        parts = c.data.split(":")
        alias = parts[-2]
        sub_name = parts[-1]
        del_subserver(alias, sub_name)
        # перерисовываем сервер
        c.data = f"{CBT_SRV_DETAIL}:{alias}"
        open_server_detail(c)

    def open_sub_detail(c: CallbackQuery) -> None:
        parts = c.data.split(":")
        alias = parts[-2]
        sub_name = parts[-1]
        eff = resolve_subserver_settings(alias, sub_name)
        s = get_server(alias) or {}
        sub_raw = (s.get("subservers") or {}).get(sub_name) or {}
        text = (
            f"<b>🧩 {alias} · {sub_name}</b>\n\n"
            f"Эффективные настройки (с учётом дефолтов сервера):\n"
            f"switch_cmd: <code>{eff['switch_cmd']}</code>\n"
            f"pay_cmd: <code>{eff['pay_cmd']}</code>\n"
            f"confirm_needed: <b>{eff['confirm_needed']}</b>\n"
            f"confirm_cmd: <code>{eff['confirm_cmd']}</code>\n"
            f"currency: <b>{eff['currency_label']}</b>\n\n"
            f"Override этого подсервера: <code>{json.dumps(sub_raw, ensure_ascii=False) or '{}'}</code>"
        )
        kb = K()
        for f in ("pay_cmd", "switch_cmd", "confirm_cmd", "confirm_needed",
                  "success_regex", "error_regex", "currency_label"):
            kb.add(B(f"✏️ {f}",
                     callback_data=f"{CBT_SUB_EDIT}:{alias}:{sub_name}:{f}"))
        kb.add(B("🏰🤖🎯 Клан / Капча / Заход (override)",
                 callback_data=f"{CBT_SUB_FEATURES}:{alias}:{sub_name}"))
        kb.add(B("⬅️ Назад", callback_data=f"{CBT_SRV_DETAIL}:{alias}"))
        _render(c, text, kb)
        bot.answer_callback_query(c.id)

    def edit_sub_field(c: CallbackQuery) -> None:
        parts = c.data.split(":")
        alias = parts[-3]
        sub_name = parts[-2]
        field = parts[-1]
        m = bot.send_message(
            c.message.chat.id,
            f"<b>{alias} · {sub_name}</b>: новое значение <code>{field}</code> "
            f"(пусто = очистить override)",
            parse_mode="HTML",
        )
        _set_state(m.chat.id, m.message_id, c.from_user.id, ST_AWAIT_EDIT,
                   {"target": "subserver", "alias": alias, "sub": sub_name, "field": field})
        bot.answer_callback_query(c.id)

    # ------- v1.3.0: панели новых функций (клан-выдача / капча / pay-on-join) -------
    def _features_kb_server(alias: str) -> K:
        kb = K()
        kb.add(B("🏰 Клан-выдача (вкл/выкл)",
                 callback_data=f"{CBT_SRV_FTE}:{alias}:clan_flow"))
        kb.row(
            B("баланс-команда", callback_data=f"{CBT_SRV_FTE}:{alias}:clan_balance_cmd"),
            B("regex баланса", callback_data=f"{CBT_SRV_FTE}:{alias}:clan_balance_regex"),
        )
        kb.row(
            B("таймаут баланса", callback_data=f"{CBT_SRV_FTE}:{alias}:clan_balance_timeout_sec"),
            B("withdraw-команда", callback_data=f"{CBT_SRV_FTE}:{alias}:clan_withdraw_cmd"),
        )
        kb.row(
            B("🤖 тип капчи", callback_data=f"{CBT_SRV_FTE}:{alias}:captcha_type"),
            B("trigger капчи", callback_data=f"{CBT_SRV_FTE}:{alias}:captcha_trigger"),
        )
        kb.row(
            B("таймаут капчи", callback_data=f"{CBT_SRV_FTE}:{alias}:captcha_timeout"),
            B("respond_via", callback_data=f"{CBT_SRV_FTE}:{alias}:captcha_respond"),
        )
        kb.add(B("🎯 Выдача по заходу (вкл/выкл)",
                 callback_data=f"{CBT_SRV_FTE}:{alias}:pay_on_join"))
        kb.add(B("таймаут захода",
                 callback_data=f"{CBT_SRV_FTE}:{alias}:join_timeout_sec"))
        kb.add(B("⬅️ Назад", callback_data=f"{CBT_SRV_DETAIL}:{alias}"))
        return kb

    def _features_kb_sub(alias: str, sub_name: str) -> K:
        kb = K()
        p = f"{CBT_SUB_FTE}:{alias}:{sub_name}"
        kb.add(B("🏰 Клан-выдача (да/нет/пусто=сброс)", callback_data=f"{p}:clan_flow"))
        kb.row(
            B("баланс-команда", callback_data=f"{p}:clan_balance_cmd"),
            B("regex баланса", callback_data=f"{p}:clan_balance_regex"),
        )
        kb.row(
            B("таймаут баланса", callback_data=f"{p}:clan_balance_timeout_sec"),
            B("withdraw-команда", callback_data=f"{p}:clan_withdraw_cmd"),
        )
        kb.row(
            B("🤖 тип капчи", callback_data=f"{p}:captcha_type"),
            B("trigger капчи", callback_data=f"{p}:captcha_trigger"),
        )
        kb.row(
            B("таймаут капчи", callback_data=f"{p}:captcha_timeout"),
            B("respond_via", callback_data=f"{p}:captcha_respond"),
        )
        kb.add(B("🎯 Выдача по заходу (да/нет/пусто=сброс)", callback_data=f"{p}:pay_on_join"))
        kb.add(B("таймаут захода", callback_data=f"{p}:join_timeout_sec"))
        kb.add(B("⬅️ Назад", callback_data=f"{CBT_SUB_DETAIL}:{alias}:{sub_name}"))
        return kb

    def open_server_features(c: CallbackQuery) -> None:
        alias = c.data.split(":")[-1]
        if not get_server(alias):
            bot.answer_callback_query(c.id, "Не найдено")
            return
        text = (f"<b>🛠 {alias} — новые функции (уровень сервера)</b>\n\n"
                + _render_resolved_features(alias))
        _render(c, text, _features_kb_server(alias))
        bot.answer_callback_query(c.id)

    def open_sub_features(c: CallbackQuery) -> None:
        parts = c.data.split(":")
        alias, sub_name = parts[-2], parts[-1]
        text = (f"<b>🛠 {alias} · {sub_name} — новые функции (override)</b>\n\n"
                + _render_resolved_features(alias, sub_name))
        _render(c, text, _features_kb_sub(alias, sub_name))
        bot.answer_callback_query(c.id)

    def edit_server_feature(c: CallbackQuery) -> None:
        parts = c.data.split(":")
        alias, field = parts[-2], parts[-1]
        m = bot.send_message(
            c.message.chat.id,
            f"Сервер <b>{alias}</b> · функция <code>{field}</code>: введите значение",
            parse_mode="HTML")
        _set_state(m.chat.id, m.message_id, c.from_user.id, ST_AWAIT_EDIT,
                   {"target": "server_feature", "alias": alias, "field": field})
        bot.answer_callback_query(c.id)

    def edit_sub_feature(c: CallbackQuery) -> None:
        parts = c.data.split(":")
        alias, sub_name, field = parts[-3], parts[-2], parts[-1]
        m = bot.send_message(
            c.message.chat.id,
            f"<b>{alias} · {sub_name}</b> · функция <code>{field}</code>: "
            f"введите значение (пусто = сбросить override)",
            parse_mode="HTML")
        _set_state(m.chat.id, m.message_id, c.from_user.id, ST_AWAIT_EDIT,
                   {"target": "subserver_feature", "alias": alias,
                    "sub": sub_name, "field": field})
        bot.answer_callback_query(c.id)

    def on_edit(m: Message) -> None:
        st = _get_state(m).get("data") or {}
        target = st.get("target")
        tg.clear_state(m.chat.id, m.from_user.id, True)
        val_raw = (m.text or "").strip()

        def _coerce(value: str, field: str) -> Any:
            if value == "":
                return None
            if field.endswith("needed"):
                return value.lower() in ("true", "1", "да", "+", "yes", "on")
            if field == "host":
                # формат "host" или "host:port"
                if ":" in value:
                    h, p = value.split(":", 1)
                    return (h, int(p))
                return (value, None)
            if field == "port":
                return int(value)
            if field == "amount_per_unit" or field == "max_per_order":
                return int(value)
            return value

        if target == "server":
            alias, field = st["alias"], st["field"]
            servers = _load_servers()
            s = servers.setdefault(alias, {})
            if field == "host":
                hp = _coerce(val_raw, "host")
                if hp:
                    s["host"] = hp[0]
                    if hp[1] is not None:
                        s["port"] = hp[1]
            elif field == "captcha_regex":
                cap = s.setdefault("default_captcha", {"enabled": True, "type": "chat"})
                cap["trigger_regex"] = val_raw
                cap.setdefault("respond_via", "chat")
                cap.setdefault("timeout_sec", 30)
                cap["enabled"] = bool(val_raw)
            else:
                v = _coerce(val_raw, field)
                if v is None:
                    s.pop(field, None)
                else:
                    s[field] = v
            _save_servers(servers)
            bot.send_message(m.chat.id, "✅ Сохранено")
        elif target in ("server_feature", "subserver_feature"):
            is_sub = target == "subserver_feature"
            alias = st["alias"]
            field = st["field"]
            sub_name = st.get("sub")
            _captcha_fields = {"captcha_type", "captcha_trigger",
                               "captcha_timeout", "captcha_respond"}
            # Пустой ввод на уровне подсервера = сброс override.
            if is_sub and val_raw == "":
                servers = _load_servers()
                s = servers.setdefault(alias, {})
                sub_obj = s.setdefault("subservers", {}).setdefault(sub_name, {})
                if field in _captcha_fields:
                    cap = sub_obj.get("captcha")
                    if isinstance(cap, dict):
                        key = {"captcha_type": "type", "captcha_trigger": "trigger_regex",
                               "captcha_timeout": "timeout_sec",
                               "captcha_respond": "respond_via"}[field]
                        cap.pop(key, None)
                        if not cap:
                            sub_obj.pop("captcha", None)
                else:
                    sub_obj.pop(field, None)
                _save_servers(servers)
                bot.send_message(
                    m.chat.id, "✅ Override сброшен (берётся дефолт сервера).\n\n"
                    + _render_resolved_features(alias, sub_name), parse_mode="HTML")
            else:
                ok, value, err = _validate_feature_value(field, val_raw)
                if not ok:
                    # Req 4.5: отклоняем, прежнее значение сохраняется.
                    bot.send_message(m.chat.id, f"❌ {err} Прежнее значение сохранено.")
                else:
                    servers = _load_servers()
                    s = servers.setdefault(alias, {})
                    if is_sub:
                        target_obj = s.setdefault("subservers", {}).setdefault(sub_name, {})
                    else:
                        target_obj = s
                    if field in _captcha_fields:
                        cap_key = "captcha" if is_sub else "default_captcha"
                        cap = target_obj.setdefault(
                            cap_key, {"enabled": True, "type": "chat",
                                      "respond_via": "chat", "timeout_sec": 30,
                                      "trigger_regex": ""})
                        if not isinstance(cap, dict):
                            cap = {"enabled": True, "type": "chat"}
                            target_obj[cap_key] = cap
                        mapping = {"captcha_type": "type",
                                   "captcha_trigger": "trigger_regex",
                                   "captcha_timeout": "timeout_sec",
                                   "captcha_respond": "respond_via"}
                        cap[mapping[field]] = value
                        # тип image/chat → капча включена; none → выключена
                        if field == "captcha_type":
                            cap["enabled"] = value != "none"
                    else:
                        store_key = field if is_sub else f"default_{field}"
                        target_obj[store_key] = value
                    _save_servers(servers)
                    bot.send_message(
                        m.chat.id, "✅ Сохранено.\n\n"
                        + _render_resolved_features(alias, sub_name if is_sub else None),
                        parse_mode="HTML")
        elif target == "settings":
            field = st["field"]
            cfg2 = _load_config()
            try:
                if field in ("max_per_order", "max_per_minute", "captcha_timeout_sec",
                             "pay_timeout_sec", "switch_timeout_sec", "retry_attempts",
                             "retry_backoff_sec", "session_idle_kick_sec",
                             "login_post_delay_sec"):
                    cfg2["settings"][field] = int(val_raw)
                elif field in ("command_jitter_min_sec", "command_jitter_max_sec"):
                    cfg2["settings"][field] = float(val_raw)
                elif field == "notify_chats":
                    cfg2["settings"]["notify_chats"] = [
                        int(x) for x in val_raw.split(",") if x.strip()
                    ]
                else:
                    cfg2["settings"][field] = val_raw
                _save_config(cfg2)
                bot.send_message(m.chat.id, "✅ Сохранено")
            except Exception as ex:
                bot.send_message(m.chat.id, f"Ошибка: {ex}")
        elif target == "donor_pass":
            cfg_donors = _load_donors()
            for d in cfg_donors:
                if d.get("nick") == st["nick"]:
                    d["login_pass"] = val_raw
                    break
            _save_donors(cfg_donors)
            bot.send_message(m.chat.id, "✅ Пароль сохранён")
        elif target == "donor_balance":
            cfg_donors = _load_donors()
            for d in cfg_donors:
                if d.get("nick") == st["nick"]:
                    try:
                        d["balance_cached"] = int(val_raw)
                    except ValueError:
                        bot.send_message(m.chat.id, "Число")
                        return
                    break
            _save_donors(cfg_donors)
            bot.send_message(m.chat.id, "✅ Баланс обновлён")

    # ------- доноры -------
    def open_donors(c: CallbackQuery) -> None:
        donors = _load_donors()
        servers = _load_servers()
        lines = ["<b>👤 Доноры</b>\n"]
        if not donors:
            lines.append("Пусто.")
        else:
            for d in donors:
                lines.append(
                    f"• <b>{d.get('nick')}</b> — {d.get('server')}/{d.get('subserver')} "
                    f"· бал: {_humanize_amount(d.get('balance_cached', 0))} "
                    f"· {d.get('status', '?')}"
                )
        kb = K()
        for d in donors:
            kb.row(
                B(f"⚙️ {d.get('nick')}",
                  callback_data=f"{CBT_DNR_DETAIL}:{d.get('nick')}"),
                B("🗑", callback_data=f"{CBT_DNR_DEL}:{d.get('nick')}"),
            )
        kb.add(B("➕ Добавить донора", callback_data=CBT_DNR_ADD))
        kb.add(B("⬅️ Назад", callback_data=CBT_OPEN))
        _render(c, "\n".join(lines), kb)
        bot.answer_callback_query(c.id)

    def add_donor_ask(c: CallbackQuery) -> None:
        servers = _load_servers()
        srv_list = ", ".join(servers.keys()) or "(сначала добавьте сервер)"
        m = bot.send_message(
            c.message.chat.id,
            "Новый донор. Пришли строкой:\n"
            "<code>server|subserver|nick|login_pass|balance</code>\n\n"
            f"Известные серверы: <code>{srv_list}</code>\n"
            "Пример:\n<code>funtime|anarchy121|FT_Bot1|qwerty123|150000</code>",
            parse_mode="HTML",
        )
        _set_state(m.chat.id, m.message_id, c.from_user.id, ST_AWAIT_DNR)
        bot.answer_callback_query(c.id)

    def on_add_donor(m: Message) -> None:
        tg.clear_state(m.chat.id, m.from_user.id, True)
        parts = [p.strip() for p in (m.text or "").split("|")]
        if len(parts) < 4:
            bot.send_message(m.chat.id, "Нужно минимум 4 поля через |")
            return
        server, sub, nick, pwd = parts[0], parts[1], parts[2], parts[3]
        bal = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
        if not get_server(server):
            bot.send_message(m.chat.id, f"Сервер <code>{server}</code> не настроен.", parse_mode="HTML")
            return
        # авто-создание подсервера если ещё нет
        if sub not in list_subservers(server):
            add_subserver(server, sub)
        donors = _load_donors()
        if any(d.get("nick") == nick for d in donors):
            bot.send_message(m.chat.id, "Донор с таким ником уже есть.")
            return
        donors.append({
            "nick": nick, "server": server, "subserver": sub,
            "login_pass": pwd, "balance_cached": bal,
            "status": "offline", "added_at": time.time(), "last_used": 0,
        })
        _save_donors(donors)
        _log(f"+donor {nick}@{server}/{sub}")
        bot.send_message(m.chat.id, f"✅ Донор <b>{nick}</b> добавлен.", parse_mode="HTML")

    def open_donor_detail(c: CallbackQuery) -> None:
        # callback_data может быть:
        #   {CBT_DNR_DETAIL}:<nick>             — показать карточку
        #   {CBT_DNR_DETAIL}:edit_pass:<nick>   — спросить новый пароль
        #   {CBT_DNR_DETAIL}:edit_bal:<nick>    — спросить новый баланс
        rest = ""
        if c.data.startswith(CBT_DNR_DETAIL + ":"):
            rest = c.data[len(CBT_DNR_DETAIL) + 1:]
        if rest.startswith("edit_pass:"):
            nick = rest.split(":", 1)[1]
            mm = bot.send_message(
                c.message.chat.id,
                f"Новый пароль для <code>{nick}</code> (пустое — сбросить):",
                parse_mode="HTML")
            _set_state(mm.chat.id, mm.message_id, c.from_user.id, ST_AWAIT_EDIT,
                       {"target": "donor_pass", "nick": nick})
            bot.answer_callback_query(c.id)
            return
        if rest.startswith("edit_bal:"):
            nick = rest.split(":", 1)[1]
            mm = bot.send_message(
                c.message.chat.id,
                f"Новый баланс (число) для <code>{nick}</code>:",
                parse_mode="HTML")
            _set_state(mm.chat.id, mm.message_id, c.from_user.id, ST_AWAIT_EDIT,
                       {"target": "donor_balance", "nick": nick})
            bot.answer_callback_query(c.id)
            return

        nick = rest or c.data.split(":")[-1]
        donors = _load_donors()
        d = next((x for x in donors if x.get("nick") == nick), None)
        if not d:
            bot.answer_callback_query(c.id, "Не найден")
            return
        masked_pwd = ("*" * len(d.get("login_pass", ""))) or "(нет)"
        text = (
            f"<b>👤 {nick}</b>\n"
            f"server: <code>{d.get('server')}</code>\n"
            f"subserver: <code>{d.get('subserver')}</code>\n"
            f"login_pass: <code>{masked_pwd}</code>\n"
            f"balance: <b>{_humanize_amount(d.get('balance_cached', 0))}</b>\n"
            f"status: <b>{d.get('status', '?')}</b>\n"
            f"last_error: <code>{d.get('last_error', '')[:200]}</code>"
        )
        kb = K()
        kb.row(
            B("🧪 Тест логина", callback_data=f"{CBT_DNR_TEST}:{nick}"),
            B("💰 Спросить баланс", callback_data=f"{CBT_DNR_BAL}:{nick}"),
        )
        kb.row(
            B("✏️ Пароль", callback_data=f"{CBT_DNR_DETAIL}:edit_pass:{nick}"),
            B("✏️ Баланс", callback_data=f"{CBT_DNR_DETAIL}:edit_bal:{nick}"),
        )
        kb.add(B("🗑 Удалить", callback_data=f"{CBT_DNR_DEL}:{nick}"))
        kb.add(B("⬅️ Назад", callback_data=CBT_TAB_DONORS))
        _render(c, text, kb)
        bot.answer_callback_query(c.id)

    def del_donor(c: CallbackQuery) -> None:
        nick = c.data.split(":")[-1]
        donors = _load_donors()
        donors = [d for d in donors if d.get("nick") != nick]
        _save_donors(donors)
        _log(f"-donor {nick}")
        open_donors(c)

    def test_donor(c: CallbackQuery) -> None:
        nick = c.data.split(":")[-1]
        donors = _load_donors()
        d = next((x for x in donors if x.get("nick") == nick), None)
        if not d:
            bot.answer_callback_query(c.id, "Не найден")
            return
        bot.answer_callback_query(c.id, "Подключаюсь…")

        def _worker() -> None:
            try:
                cfg = _load_config()
                _get_or_open_session(d, cfg["settings"])
                bot.send_message(c.message.chat.id, f"✅ {nick}: подключение успешно")
            except Exception as ex:
                bot.send_message(c.message.chat.id, f"❌ {nick}: {ex}")

        threading.Thread(target=_worker, daemon=True).start()

    # ------- лоты -------
    def open_lots(c: CallbackQuery) -> None:
        lots = _load_lots()
        lines = ["<b>🛒 Лоты (override)</b>\n",
                 "Лот можно полностью описать тегами в названии/описании FunPay:\n"
                 "<code>#funtime @anarchy121 money:1000</code>\n",
                 "Здесь — только override для тех лотов, где теги не работают.\n"]
        if not lots:
            lines.append("Пусто.")
        else:
            for lot in lots:
                lines.append(
                    f"• lot_id <code>{lot.get('lot_id')}</code> → "
                    f"{lot.get('server_alias')}/{lot.get('subserver')} · "
                    f"{lot.get('amount_per_unit')} {lot.get('currency_label', 'коинов')}"
                )
        kb = K()
        for i, lot in enumerate(lots):
            kb.add(B(f"🗑 lot {lot.get('lot_id')}",
                     callback_data=f"{CBT_LOT_DEL}:{i}"))
        kb.add(B("➕ Привязать лот", callback_data=CBT_LOT_ADD))
        kb.add(B("⬅️ Назад", callback_data=CBT_OPEN))
        _render(c, "\n".join(lines), kb)
        bot.answer_callback_query(c.id)

    def add_lot_ask(c: CallbackQuery) -> None:
        m = bot.send_message(
            c.message.chat.id,
            "Привязка лота. Пришли строкой:\n"
            "<code>lot_id|server|subserver|amount_per_unit|currency</code>\n\n"
            "Пример:\n<code>12345678|funtime|anarchy121|1000|коинов</code>",
            parse_mode="HTML",
        )
        _set_state(m.chat.id, m.message_id, c.from_user.id, ST_AWAIT_LOT)
        bot.answer_callback_query(c.id)

    def on_add_lot(m: Message) -> None:
        tg.clear_state(m.chat.id, m.from_user.id, True)
        parts = [p.strip() for p in (m.text or "").split("|")]
        if len(parts) < 4:
            bot.send_message(m.chat.id, "Нужно минимум 4 поля через |")
            return
        try:
            lot_id = int(parts[0])
            amount = int(parts[3])
        except ValueError:
            bot.send_message(m.chat.id, "lot_id и amount — числа")
            return
        currency = parts[4] if len(parts) > 4 else "коинов"
        lots = _load_lots()
        lots = [l for l in lots if str(l.get("lot_id")) != str(lot_id)]
        lots.append({
            "lot_id": lot_id, "server_alias": parts[1], "subserver": parts[2],
            "amount_per_unit": amount, "currency_label": currency,
        })
        _save_lots(lots)
        _log(f"+lot {lot_id}")
        bot.send_message(m.chat.id, "✅ Лот привязан")

    def del_lot(c: CallbackQuery) -> None:
        idx = int(c.data.split(":")[-1])
        lots = _load_lots()
        if 0 <= idx < len(lots):
            lots.pop(idx)
            _save_lots(lots)
        open_lots(c)

    # ------- ордеры -------
    def open_orders(c: CallbackQuery) -> None:
        orders = _load_orders()
        if not orders:
            text = "<b>⏳ Ордеры</b>\n\nПусто."
        else:
            lines = ["<b>⏳ Активные ордеры</b>\n"]
            for o in orders[-30:]:
                stage = o.get("stage", "?")
                lines.append(
                    f"• #<code>{o.get('order_id')}</code> {o.get('buyer', '?')}: "
                    f"{stage} · {o.get('total')} {o.get('currency')} → "
                    f"{o.get('nick') or '(ждём ник)'}"
                )
            text = "\n".join(lines)
        kb = K()
        for o in orders[-15:]:
            if o.get("stage") == "failed":
                kb.row(
                    B(f"🔁 #{o.get('order_id')}",
                      callback_data=f"{CBT_ORD_RETRY}:{o.get('order_id')}"),
                    B(f"❌", callback_data=f"{CBT_ORD_CANCEL}:{o.get('order_id')}"),
                )
        kb.add(B("⬅️ Назад", callback_data=CBT_OPEN))
        _render(c, text, kb)
        bot.answer_callback_query(c.id)

    def retry_order(c: CallbackQuery) -> None:
        oid = c.data.split(":")[-1]
        orders = _load_orders()
        state = next((o for o in orders if str(o.get("order_id")) == str(oid)), None)
        if not state:
            bot.answer_callback_query(c.id, "Не найдено")
            return
        state["stage"] = "paying"
        _save_order_state(state)
        threading.Thread(target=_run_delivery, args=(cardinal, state), daemon=True).start()
        bot.answer_callback_query(c.id, "Перезапущено")
        open_orders(c)

    def cancel_order(c: CallbackQuery) -> None:
        oid = c.data.split(":")[-1]
        _drop_order_state(oid)
        open_orders(c)

    # ------- история -------
    def open_history(c: CallbackQuery) -> None:
        h = _load_history()
        if not h:
            text = "<b>📜 История</b>\n\nПусто."
        else:
            lines = ["<b>📜 История (последние 25)</b>\n"]
            for e in h[-25:]:
                ico = "✅" if e.get("success") else "❌"
                ts = time.strftime("%d.%m %H:%M", time.localtime(e.get("ts", 0)))
                lines.append(
                    f"{ico} {ts} #{e.get('order_id')} → {e.get('nick')} · "
                    f"{e.get('total')} ({e.get('server')}/{e.get('subserver')})"
                )
            text = "\n".join(lines)
        kb = K()
        for e in h[-10:]:
            if e.get("screenshot"):
                kb.add(B(f"📷 #{e.get('order_id')}",
                         callback_data=f"{CBT_HIS_SCREEN}:{e.get('order_id')}"))
        kb.add(B("⬅️ Назад", callback_data=CBT_OPEN))
        _render(c, text, kb)
        bot.answer_callback_query(c.id)

    def show_history_screen(c: CallbackQuery) -> None:
        oid = c.data.split(":")[-1]
        path = os.path.join(SCREENS_DIR, f"{oid}.png")
        if not os.path.exists(path):
            bot.answer_callback_query(c.id, "Файл не найден")
            return
        try:
            with open(path, "rb") as f:
                bot.send_photo(c.message.chat.id, f, caption=f"Order #{oid}")
        except Exception:
            bot.answer_callback_query(c.id, "Ошибка отправки")
        bot.answer_callback_query(c.id)

    # ------- настройки -------
    def open_settings_tab(c: CallbackQuery) -> None:
        cfg2 = _load_config()
        s = cfg2["settings"]
        text = (
            "<b>⚙️ Настройки</b>\n\n"
            f"max_per_order: <b>{s.get('max_per_order')}</b>\n"
            f"max_per_minute: <b>{s.get('max_per_minute')}</b>\n"
            f"pay_timeout_sec: <b>{s.get('pay_timeout_sec')}</b>\n"
            f"switch_timeout_sec: <b>{s.get('switch_timeout_sec')}</b>\n"
            f"captcha_timeout_sec: <b>{s.get('captcha_timeout_sec')}</b>\n"
            f"session_idle_kick_sec: <b>{s.get('session_idle_kick_sec')}</b>\n"
            f"command_jitter: <b>{s.get('command_jitter_min_sec')}–{s.get('command_jitter_max_sec')}</b>\n"
            f"notify_chats: <code>{s.get('notify_chats')}</code>\n"
        )
        kb = K()
        for f in ("max_per_order", "max_per_minute", "pay_timeout_sec",
                  "switch_timeout_sec", "captcha_timeout_sec",
                  "session_idle_kick_sec", "command_jitter_min_sec",
                  "command_jitter_max_sec", "notify_chats",
                  "buyer_greeting", "buyer_confirm", "buyer_paying",
                  "buyer_success", "buyer_failed"):
            kb.add(B(f"✏️ {f}", callback_data=f"{CBT_SET_EDIT}:{f}"))
        kb.add(B("⬅️ Назад", callback_data=CBT_OPEN))
        _render(c, text, kb)
        bot.answer_callback_query(c.id)

    def edit_setting(c: CallbackQuery) -> None:
        field = c.data.split(":")[-1]
        m = bot.send_message(c.message.chat.id, f"Новое значение <code>{field}</code>:",
                             parse_mode="HTML")
        _set_state(m.chat.id, m.message_id, c.from_user.id, ST_AWAIT_EDIT,
                   {"target": "settings", "field": field})
        bot.answer_callback_query(c.id)

    # ------- логи -------
    def open_logs(c: CallbackQuery) -> None:
        text = _read_logs()
        if len(text) > 3500:
            text = text[-3500:]
        kb = K()
        kb.row(B("🗑 Очистить", callback_data=CBT_LOGS_CLEAR),
               B("⬅️ Назад", callback_data=CBT_OPEN))
        _render(c, f"<b>📋 Логи</b>\n<pre>{text}</pre>", kb)
        bot.answer_callback_query(c.id)

    def clear_logs(c: CallbackQuery) -> None:
        try:
            if os.path.exists(LOG_PATH):
                os.remove(LOG_PATH)
        except Exception:
            pass
        open_logs(c)

    # ------- регистрация -------
    tg.cbq_handler(open_main, _cb(CBT_OPEN))
    tg.cbq_handler(open_help, _cb(CBT_HELP))

    def _open_from_plugin_card(c: CallbackQuery) -> None:
        try:
            bot.send_message(c.message.chat.id, _status_text(),
                             reply_markup=_kb_main(), parse_mode="HTML")
        except Exception:
            pass
        bot.answer_callback_query(c.id)

    tg.cbq_handler(_open_from_plugin_card, lambda c: c.data.startswith(f"47:{UUID}"))
    tg.cbq_handler(toggle_running, _cb(CBT_START))
    tg.cbq_handler(toggle_dry_run, _cb(CBT_DRY_RUN))
    tg.cbq_handler(ask_test_delivery, _cb(CBT_TEST_DELIVERY))

    tg.cbq_handler(open_servers, _cb(CBT_TAB_SERVERS))
    tg.cbq_handler(apply_server_preset, _cb(CBT_SRV_PRESET))
    tg.cbq_handler(open_server_detail, _cb(CBT_SRV_DETAIL))
    tg.cbq_handler(add_server_ask, _cb(CBT_SRV_ADD))
    tg.cbq_handler(del_server, _cb(CBT_SRV_DEL))
    tg.cbq_handler(edit_server_field, _cb(CBT_SRV_EDIT))

    tg.cbq_handler(add_subserver_ask, _cb(CBT_SUB_ADD))
    tg.cbq_handler(del_subserver_cb, _cb(CBT_SUB_DEL))
    tg.cbq_handler(open_sub_detail, _cb(CBT_SUB_DETAIL))
    tg.cbq_handler(edit_sub_field, _cb(CBT_SUB_EDIT))

    # v1.3.0: панели новых функций
    tg.cbq_handler(open_server_features, _cb(CBT_SRV_FEATURES))
    tg.cbq_handler(edit_server_feature, _cb(CBT_SRV_FTE))
    tg.cbq_handler(open_sub_features, _cb(CBT_SUB_FEATURES))
    tg.cbq_handler(edit_sub_feature, _cb(CBT_SUB_FTE))

    tg.cbq_handler(open_donors, _cb(CBT_TAB_DONORS))
    tg.cbq_handler(add_donor_ask, _cb(CBT_DNR_ADD))
    tg.cbq_handler(open_donor_detail, _cb(CBT_DNR_DETAIL))
    tg.cbq_handler(del_donor, _cb(CBT_DNR_DEL))
    tg.cbq_handler(test_donor, _cb(CBT_DNR_TEST))

    tg.cbq_handler(open_lots, _cb(CBT_TAB_LOTS))
    tg.cbq_handler(add_lot_ask, _cb(CBT_LOT_ADD))
    tg.cbq_handler(del_lot, _cb(CBT_LOT_DEL))

    tg.cbq_handler(open_orders, _cb(CBT_TAB_ORDERS))
    tg.cbq_handler(retry_order, _cb(CBT_ORD_RETRY))
    tg.cbq_handler(cancel_order, _cb(CBT_ORD_CANCEL))

    tg.cbq_handler(open_history, _cb(CBT_TAB_HISTORY))
    tg.cbq_handler(show_history_screen, _cb(CBT_HIS_SCREEN))

    tg.cbq_handler(open_settings_tab, _cb(CBT_TAB_SETTINGS))
    tg.cbq_handler(edit_setting, _cb(CBT_SET_EDIT))

    tg.cbq_handler(open_logs, _cb(CBT_TAB_LOGS))
    tg.cbq_handler(clear_logs, _cb(CBT_LOGS_CLEAR))

    # text inputs
    def _state_eq(m: Message, state: str) -> bool:
        st = tg.get_state(m.chat.id, m.from_user.id)
        return bool(st and st.get("state") == state)

    tg.msg_handler(on_add_server, func=lambda m: _state_eq(m, ST_AWAIT_SRV))
    tg.msg_handler(on_add_subserver, func=lambda m: _state_eq(m, ST_AWAIT_SUB))
    tg.msg_handler(on_add_donor, func=lambda m: _state_eq(m, ST_AWAIT_DNR))
    tg.msg_handler(on_add_lot, func=lambda m: _state_eq(m, ST_AWAIT_LOT))
    tg.msg_handler(on_edit, func=lambda m: _state_eq(m, ST_AWAIT_EDIT))
    tg.msg_handler(on_test_delivery, func=lambda m: _state_eq(m, ST_AWAIT_TEST))

    # v1.3.0: ответ оператора на image-капчу (когда нет активного FSM-состояния
    # и есть ожидающая капча для этого чата).
    def _captcha_reply_ready(m: Message) -> bool:
        try:
            stt = tg.get_state(m.chat.id, m.from_user.id)
            if stt and stt.get("state"):
                return False
        except Exception:
            pass
        return _has_pending_captcha(m.chat.id)

    def on_operator_captcha_reply(m: Message) -> None:
        if feed_operator_captcha_reply(m.chat.id, m.text or ""):
            try:
                bot.send_message(m.chat.id, "✅ Код капчи принят, отправляю на сервер.")
            except Exception:
                pass

    tg.msg_handler(on_operator_captcha_reply, func=_captcha_reply_ready)

    # /mcdonate
    def cmd_open(m: Message) -> None:
        bot.send_message(m.chat.id, _status_text(), reply_markup=_kb_main(), parse_mode="HTML")

    tg.msg_handler(cmd_open, commands=["mcdonate"])

    def cmd_guide(m: Message) -> None:
        try:
            bot.send_message(
                m.chat.id,
                _MCD_GUIDE_TEXT,
                parse_mode="HTML",
            )
        except Exception as e:
            try:
                bot.send_message(
                    m.chat.id,
                    f"⚠ Не удалось отправить гайд: <code>{e}</code>",
                    parse_mode="HTML")
            except Exception:
                logger.error("minecraft_donate: cmd_guide failed",
                             exc_info=True)

    _MCD_GUIDE_TEXT = (
        "<b>📖 MinecraftDonate — гайд</b>\n\n"
        "1. /mcdonate → 🖥 Серверы → 📦 Пресет: FunTime / HolyWorld\n"
        "   (или ➕ Свой сервер)\n"
        "2. 🖥 Серверы → выбрать сервер → ➕ Свой подсервер\n"
        "   (anarchy121 и т.д.)\n"
        "3. 👤 Доноры → ➕ Добавить (ник, пароль, баланс)\n"
        "4. На FunPay в названии/описании лота укажи теги:\n"
        "   <code>#funtime @anarchy121 money:1000</code>\n"
        "5. ▶️ Запустить.\n\n"
        "<b>🧪 Безопасный тест перед боем:</b>\n"
        "1) Включи <b>🧪 Dry-run</b> в главном меню\n"
        "2) Жми <b>🧪 Тестовая выдача</b> → пришли\n"
        "   <code>funtime|anarchy121|TestPlayer|1</code>\n"
        "3) Бот пройдёт всю цепочку (логин → капча → /server → /pay)\n"
        "   но <b>НЕ отправит</b> /pay — увидишь чат-скрин с пометкой DRY-RUN\n"
        "4) Если всё ок — выключи Dry-run и включи ▶️ Запустить\n\n"
        "<b>Как покупатель получает валюту:</b>\n"
        "• Оплачивает лот\n"
        "• Бот просит ник, потом ДА/+ для подтверждения\n"
        "• Бот логинится донором, шлёт /pay, делает PNG-скрин чата\n"
        "• Покупатель получает скрин + сообщение\n\n"
        "<b>Зависимости:</b>\n"
        "Ставятся автоматически при первой загрузке плагина "
        "(quarry, twisted, Pillow). Если что-то не встало — выполните:\n"
        "<code>pip install quarry twisted Pillow</code>"
    )

    tg.msg_handler(cmd_guide, commands=["mcd_guide"])

    try:
        cardinal.add_telegram_commands(UUID, [
            ("mcdonate", "MinecraftDonate: меню", True),
            ("mcd_guide", "MinecraftDonate: гайд", True),
        ])
    except Exception:
        logger.exception("Не удалось зарегистрировать команды")

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


    _log("MinecraftDonate инициализирован.")


def _open_settings_page(cardinal: "Cardinal", msg: Any) -> None:
    """FPC settings-page handler."""
    tg = getattr(cardinal, "telegram", None)
    if tg is None:
        return
    tg.bot.send_message(
        msg.chat.id,
        "<b>MinecraftDonate</b>\n\n"
        "Команда: /mcdonate — меню\n"
        "Команда: /mcd_guide — гайд",
        parse_mode="HTML",
    )


def _on_delete(cardinal: "Cardinal", *_: Any) -> None:
    with _donor_sessions_lock:
        for s in _donor_sessions.values():
            try:
                s.disconnect()
            except Exception:
                pass
        _donor_sessions.clear()
    _log("MinecraftDonate выгружен.")


BIND_TO_SETTINGS_PAGE = _open_settings_page
BIND_TO_PRE_INIT = [_init]
BIND_TO_NEW_ORDER = [_on_new_order]
BIND_TO_NEW_MESSAGE = [_on_new_message]
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



# ----------------------------------------------------------------------------
# Auto-crash logging: wrap plugin entry points (BIND_TO_* handlers and init)
# so any unhandled exception is logged with full traceback. Makes silent
# crashes visible in cardinal logs. Re-raises so cardinal can still react.
# Idempotent and self-protecting (failure to install never breaks the plugin).
# ----------------------------------------------------------------------------
def _autolog_install():
    import logging as _logging
    import functools as _functools

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
