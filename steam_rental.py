"""
Steam Rental — плагин для FunPay Cardinal.

Автоматическая аренда Steam-аккаунтов:
  * Покупатель платит за лот -> бот выдаёт логин/пароль в чат FunPay.
  * Команды в чате FunPay: !код, !продлить, !статус, !помощь, !пин,
    !статусrp, !помощьrp.
  * По истечении срока аренды плагин отзывает все Steam-сессии и (опционально)
    меняет пароль через wizard-recovery + mobile confirm.
  * Напоминания перед истечением аренды.
  * Бонус за 5★ отзыв — автоматическое продление аренды.
  * Продление через оплату (extension-лоты).
  * Авто-деактивация лотов FunPay когда нет свободных аккаунтов
    (через FunPayAPI get_lot_fields → save_lot, с защитой amount=0).
  * Remote Play аренда (Steam Link PIN).
  * Заморозка аккаунтов — замороженные не выдаются.
  * VAC / Trade ban scan (раз в час) — автоматический freeze при бане.
  * Расширенная система шаблонов с плейсхолдерами.
  * actions.log — человекочитаемый журнал действий с ротацией.
  * PC-клуб с AI photo-review + ручная фото-проверка лотов.
  * Blacklist покупателей (auto после refund/cancel).
  * Daily summary в Telegram, Prometheus /metrics.

Управление — Telegram-команды в ПУ FunPay Cardinal: /srental

Автор: @drakelovc.
Лицензия: MIT.
"""
from __future__ import annotations

import csv
import datetime
import hashlib
import importlib
import io
import json
import logging
import os
import random
import re
import secrets as pysecrets
import string
import subprocess
import sys
import threading
import time
import traceback
import uuid
import zipfile
import base64
from base64 import b64encode
from typing import TYPE_CHECKING, Any, Callable

import requests


# ── Авто-установка внешних зависимостей ──────────────────────────────────────
# Плагин использует библиотеку steampy (TOTP-генерация Steam Guard, mobile
# confirmations). Если её нет в окружении FPC — ставим автоматически в тот же
# Python, который запустил Cardinal, чтобы пользователю не пришлось делать
# pip install руками.
_BOOT_LOGGER = logging.getLogger("FPC.steam_rental")


def _ensure_dependency(pip_name: str, import_name: str | None = None) -> bool:
    """Гарантирует наличие пакета. Возвращает True если модуль доступен."""
    mod_name = import_name or pip_name
    try:
        importlib.import_module(mod_name)
        return True
    except ImportError:
        pass
    _BOOT_LOGGER.warning(
        "steam_rental: модуль %r не найден, ставлю %r через pip...",
        mod_name, pip_name)
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
             "--quiet", pip_name],
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    except Exception as exc:
        _BOOT_LOGGER.error(
            "steam_rental: не удалось установить %r автоматически: %s. "
            "Поставь вручную: %s -m pip install %s",
            pip_name, exc, sys.executable, pip_name)
        return False
    importlib.invalidate_caches()
    try:
        importlib.import_module(mod_name)
        _BOOT_LOGGER.info("steam_rental: %r успешно установлен.", pip_name)
        return True
    except ImportError as exc:
        _BOOT_LOGGER.error(
            "steam_rental: %r поставился, но импорт всё равно падает: %s",
            pip_name, exc)
        return False


_ensure_dependency("steampy")


if TYPE_CHECKING:
    from cardinal import Cardinal
    from FunPayAPI.updater.events import (
        NewMessageEvent,
        NewOrderEvent,
        OrderStatusChangedEvent,
    )


# ══════════════════════════════════════════════════════════════════════════════
# 💛 DONATION BANNER — защита реквизитов автора.
# Реквизиты закодированы (base64 + SHA-256 подпись) и лежат ВНИЗУ файла в
# _donation_details(): если их подменить на свои, подпись не сойдётся и
# баннер НЕ отправится. True = 1 (вкл), False = 0 (выкл).
# ══════════════════════════════════════════════════════════════════════════════

DONATION_ENABLED = True                # True = 1 (показывать баннер), False = 0
DONATION_SHOW_ON_START = False         # True = 1 (слать при старте плагина)
DONATION_DAILY_ENABLED = True          # True = 1 (напоминание раз в сутки)
DONATION_DAILY_HOUR = 16               # час напоминания (0-23, МСК)
DONATION_CALLBACK_PREFIX = "srl_dn"    # префикс колбэков кнопок баннера
DONATION_PLUGIN_NAME = "Steam Rental"  # имя плагина в шапке баннера

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


# ── Плагин-метаданные (FPC читает эти константы из файла) ────────────────────
NAME = "Steam Rental"
VERSION = "2.23.3"
DESCRIPTION = (
    "Авто-аренда Steam-аккаунтов на FunPay: выдача логин/пароль после оплаты, "
    "Remote Play (Steam Link PIN) аренда, "
    "команды !код/!продлить/!статус/!помощь/!пин/!статусrp/!помощьrp, "
    "напоминания, бонус за отзыв, авто-продление, статистика, история + CSV, "
    "проверка аккаунтов, авто-заморозка, массовые действия."
)
CREDITS = "@drakelovc"
UUID = "1274b5e1-4b95-477d-b0a7-98066addbf36"
SETTINGS_PAGE = True
BIND_TO_DELETE = None

# ── Логи/конфиг/хранилище ────────────────────────────────────────────────────
LOGGER = logging.getLogger("FPC.steam_rental")

STORAGE_DIR = os.path.join("storage", "plugins", "steam_rental")

ACCOUNTS_FILE = os.path.join(STORAGE_DIR, "accounts.json")
LOTS_FILE = os.path.join(STORAGE_DIR, "lots.json")
GAMES_FILE = os.path.join(STORAGE_DIR, "games.json")
CONFIG_FILE = os.path.join(STORAGE_DIR, "config.json")
HISTORY_FILE = os.path.join(STORAGE_DIR, "history.json")
EVENTS_FILE = os.path.join(STORAGE_DIR, "events.json")
CLUBS_FILE = os.path.join(STORAGE_DIR, "clubs.json")
BLACKLIST_FILE = os.path.join(STORAGE_DIR, "blacklist.json")
METRICS_FILE = os.path.join(STORAGE_DIR, "metrics.json")
LOT_STATE_FILE = os.path.join(STORAGE_DIR, "lot_activation.json")
# Бронирование конкретного аккаунта по логину (Irent <login>).
# Покупатель резервирует ИМЕННО ЭТОТ акк → бот выдаёт ссылку на оплату →
# после оплаты выдаётся именно зарезервированный аккаунт. Если бронь
# истекает или акк становится занят до оплаты — случайный аккаунт НЕ выдаётся.
RESERVATIONS_FILE = os.path.join(STORAGE_DIR, "reservations.json")
# Одноразовый приоритет на конкретный аккаунт (!priority/!приоритет <login>).
# В отличие от RESERVATIONS — НЕ блокирует акк за покупателем. Просто
# запоминает предпочтение: при оплате, если этот acc свободен — выдадим его,
# иначе случайный из пула. После выдачи приоритет сбрасывается.
PRIORITIES_FILE = os.path.join(STORAGE_DIR, "priorities.json")
# Waitlist на конкретный аккаунт (!notify/!жду <login>). При освобождении
# аккаунта (end_rental) уведомляем top-N ожидающих покупателей.
WAITLIST_FILE = os.path.join(STORAGE_DIR, "waitlist.json")
# v2.22: шаблоны сообщений вынесены в отдельные JSON-файлы (RU/EN).
# Канонический источник правды — файлы; admin может редактировать
# через TG-меню «📝 Шаблоны» (с переключателем 🇷🇺/🇬🇧) или прямо в файле.
TEMPLATES_RU_FILE = os.path.join(STORAGE_DIR, "templates_ru.json")
TEMPLATES_EN_FILE = os.path.join(STORAGE_DIR, "templates_en.json")
# v2.22: язык конкретного покупателя ({str(buyer_id): "ru"|"en"}).
# Переключается командами !engrent / !rusrent в чате FunPay.
BUYER_LANG_FILE = os.path.join(STORAGE_DIR, "buyer_lang.json")
# Человекочитаемый лог действий плагина (активация лотов, заморозка,
# выдача аренды и т.п.). Ротация по размеру.
ACTIONS_LOG_FILE = os.path.join(STORAGE_DIR, "actions.log")
ACTIONS_LOG_MAX_BYTES = 2 * 1024 * 1024   # 2 MiB
ACTIONS_LOG_BACKUPS = 5                    # actions.log.1 … actions.log.5
# v6: ручная фото-проверка (manual photo-review) — отдельно от AI-club, чтобы
# не пересекалось. Хранит pending-проверки {order_id: {...}}.
MANUAL_REVIEW_FILE = os.path.join(STORAGE_DIR, "manual_review.json")
# Remote Play (RP) storage
SESSIONS_FILE = os.path.join(STORAGE_DIR, "sessions.json")
QUEUE_FILE = os.path.join(STORAGE_DIR, "queue.json")
SCREENSHOTS_DIR = os.path.join(STORAGE_DIR, "screenshots")

# ── Шаблоны по умолчанию ─────────────────────────────────────────────────────
_DEFAULT_TEMPLATES: dict[str, str] = {
    "issue": (
        "🟩 АККАУНТ ВЫДАН!\n"
        "🎮 Игра: {game}\n\n"
        "🔑 Логин: {login}\n"
        "🔒 Пароль: {password}\n"
        "⏰ Срок: {duration}\n\n"
        "💬 Команды: !код {login} | !продлить\n"
        "⭐ +1 час за отзыв 5 звёзд!\n"
        "🔄 Пароль сменится после аренды\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🌐 If you prefer English: type !engrent"
    ),
    # По умолчанию пусто: post-delivery сообщение опционально и не должно
    # содержать встроенный Rockstar/Social-Club-текст «по умолчанию» — это
    # релевантно только конкретным сборкам аккаунтов. Чтобы включить:
    # 1) ⚙ Настройки → 🔔 Уведомления → «Сообщение после выдачи» = ✅
    # 2) задать текст шаблона post_delivery (глобально) или добавить
    #    кастомный текст в acc["post_delivery"] / lot["post_delivery"]
    #    (per-account / per-lot override — выигрывает в этом порядке).
    "post_delivery": "",
    "extend": (
        "🟥 ПРОДЛЕНИЕ АРЕНДЫ\n\n"
        "⚠ Чтобы продлить аренду:\n\n"
        "1. Перейдите по ссылке ниже\n"
        "2. Оплатите нужное количество часов\n"
        "3. Аренда продлится автоматически!\n\n"
        "🔗 Ссылка на продление:\n"
        "{link}\n\n"
        "⏰ Лот активен {ttl_minutes} минут — "
        "если не успеете, напишите !продлить ещё раз."
    ),
    "extended": (
        "🟥 АРЕНДА ПРОДЛЕНА!\n"
        "🟩 Добавлено времени: {hours} ч.\n"
        "🔵 Новый срок: до {new_expires}\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🎮 Приятной игры! 🟥"
    ),
    "reminder": (
        "НАПОМИНАНИЕ!\n\n"
        "⚠ Аренда заканчивается через {minutes} минут!\n\n"
        "👤 Аккаунт: {login}\n"
        "🎮 Игра: {game}\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "⭐ После окончания:\n"
        "• Пароль будет изменён\n"
        "• Доступ закроется\n\n"
        "💡 Хотите продлить?\n"
        "Напишите !продлить — я пришлю ссылку на лот продления.\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📝 Не забудьте написать отзыв и спасибо за аренду!"
    ),
    "reminder_2": (
        "🔔 ПОСЛЕДНЕЕ НАПОМИНАНИЕ!\n\n"
        "⚠ Аренда заканчивается через {minutes} минут!\n\n"
        "👤 Аккаунт: {login}\n"
        "🎮 Игра: {game}\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "⏳ Времени мало — сохрани прогресс.\n"
        "💡 Хочешь продолжить? Напиши !продлить — я пришлю ссылку.\n\n"
        "📝 Не забудь оставить отзыв 5★!"
    ),
    "expired": (
        "🟥 АРЕНДА ЗАВЕРШЕНА\n\n"
        "👤 Аккаунт: {login}\n"
        "🎮 Игра: {game}\n"
        "⏰ Время аренды: {hours} ч.\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🔄 Пароль был автоматически изменён\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "💖 Спасибо за аренду! Ждём вас снова 🟥"
    ),
    "guard_code": (
        "🟥 Код Steam Guard для {login}: {code}\n"
        "(действителен ~30 секунд)"
    ),
    "guard_error": (
        "⚠ ОШИБКА\n\n"
        "✖ Аккаунт не найден\n\n"
        "Возможные причины:\n"
        "• Неверный логин аккаунта\n"
        "• У вас нет активной аренды\n"
        "• Аренда уже завершена\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "💡 Попробуйте:\n"
        "• !код без логина — автоопределение"
    ),
    "guard_error_no_secret": (
        "⚠ ОШИБКА\n\n"
        "✖ Steam Guard недоступен\n\n"
        "Для этого аккаунта не настроен\n"
        "мобильный аутентификатор.\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📧 Обратитесь к продавцу для\n"
        "получения кода вручную"
    ),
    "no_accounts": (
        "✖ К сожалению, все аккаунты заняты\n\n"
        "🎮 Игра: {game}\n"
        "⏰ Ближайший аккаунт освободится через: {next_free_in}\n"
        "📅 Освободится в: {next_free} (МСК)\n\n"
        "💡 Хотите встать в очередь?\n"
        "Напишите !очередь и мы уведомим вас, когда аккаунт освободится"
    ),
    # ── Queue templates ───────────────────────────────────────────────────
    "queue_joined": (
        "✅ Вы добавлены в очередь!\n\n"
        "🎮 Игра: {game}\n"
        "📍 Позиция: {position}\n\n"
        "Мы уведомим вас когда аккаунт освободится."
    ),
    "queue_notified": (
        "🟢 {notify_template}\n\n"
        "🎮 Игра: {game}\n"
        "⏰ У вас есть 15 минут."
    ),
    "queue_full": "❌ Очередь заполнена. Попробуйте позже.",
    "queue_already": (
        "ℹ Вы уже в очереди!\n"
        "📍 Позиция: {position}"
    ),
    "help": (
        "🟥 ПОМОЩЬ ПО АРЕНДЕ 🟥\n\n"
        "💬 Доступные команды:\n\n"
        "🔑 !код [логин]\n"
        "↳ Получить Steam Guard код\n"
        "↳ Без логина — для вашего аккаунта\n\n"
        "🔄 !продлить\n"
        "↳ Инструкция по продлению\n\n"
        "📊 !статус\n"
        "↳ Информация об аренде\n\n"
        "❓ !помощь\n"
        "↳ Это сообщение\n\n"
        "⚡ Команды работают только в этом чате"
    ),
    "status": (
        "📊 СТАТУС АРЕНДЫ\n\n"
        "👤 Аккаунт: {login}\n"
        "🎮 Игра: {game}\n"
        "⏰ Осталось: {minutes} мин.\n"
        "📅 До: {new_expires}\n\n"
        "💡 Для продления напишите !продлить"
    ),
    "welcome": (
        "👋 Добро пожаловать!\n\n"
        "Доступные команды:\n"
        "• !код — получить Steam Guard код\n"
        "• !продлить — продлить аренду\n"
        "• !статус — информация об аренде\n"
        "• !помощь — все команды"
    ),
    "review_reward": (
        "🟧 БОНУС ЗА ОТЗЫВ\n\n"
        "⭐ Спасибо за отзыв 5 звёзд!\n\n"
        "🟩 Аренда продлена на {hours} час.\n\n"
        "🎮 Новый срок:\n"
        "↳ До {new_expires}\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "💖 Спасибо за доверие!"
    ),
    "review_deleted": (
        "🚨 ОТЗЫВ УДАЛЁН\n\n"
        "⚠ Вы удалили отзыв!\n\n"
        "🟥 Аренда сокращена на {hours} час.\n"
        "⏰ Новый срок: до {new_expires}\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "⛔ ПРЕДУПРЕЖДЕНИЕ: при повторном удалении\n"
        "отзыва вы будете добавлены в чёрный список\n"
        "и не сможете арендовать аккаунты!"
    ),
    "order_received": (
        "🟩 ЗАКАЗ ПОЛУЧЕН!\n\n"
        "⚡ Спасибо за покупку!\n"
        "🔄 Ваш заказ обрабатывается...\n\n"
        "✍ Я выдам аккаунт в ближайшее время\n"
        "  (обычно 1-5 минут)\n\n"
        "🎮 Пожалуйста, оставайтесь на связи!"
    ),
    # ── v6: ручная фото-проверка (manual review) ─────────────────────────
    "mr_request_photo": (
        "📷 Для оформления заказа пришлите фото-подтверждение того, "
        "что вы находитесь в ПК-клубе.\n\n"
        "Просто отправьте одно фото в этот чат."
    ),
    "mr_photo_received": (
        "✅ Фото успешно отправлено и находится на проверке.\n\n"
        "Как только продавец одобрит — аккаунт будет выдан автоматически."
    ),
    "mr_declined": (
        "❌ К сожалению, фото-подтверждение не было одобрено.\n\n"
        "Заказ автоматически отменён, деньги возвращены на ваш счёт FunPay."
    ),
    # ── Remote Play templates ─────────────────────────────────────────────
    "issue_remoteplay": (
        "🟩 REMOTE PLAY АРЕНДА!\n"
        "🎮 Игра: {game}\n\n"
        "🔗 Подключение через Steam Link:\n"
        "📱 PIN-код: {pin}\n\n"
        "📋 Инструкция:\n"
        "1. Откройте Steam → Настройки → Remote Play\n"
        "2. Нажмите «Связать Steam Link»\n"
        "3. Введите PIN: {pin}\n\n"
        "⏰ Срок аренды: {duration}\n"
        "⚠ PIN действителен 5 минут!\n\n"
        "💬 Команды:\n"
        "   !пин — получить новый PIN (если старый истёк)\n"
        "   !статусrp — статус сессии\n"
        "   !помощьrp — список команд"
    ),
    "pin_generated": (
        "🔗 НОВЫЙ PIN ДЛЯ REMOTE PLAY\n\n"
        "📱 PIN-код: {pin}\n\n"
        "Введите его в настройках Remote Play → Связать Steam Link.\n"
        "⚠ Действителен 5 минут!\n\n"
        "⏰ Осталось аренды: {time_left}"
    ),
    "pin_error": (
        "⚠ ОШИБКА\n\n"
        "✖ Не удалось сгенерировать PIN.\n"
        "Возможные причины:\n"
        "• Remote Play не включён на аккаунте\n"
        "• Ошибка авторизации\n\n"
        "📧 Обратитесь к продавцу."
    ),
    "session_expired": (
        "🟥 АРЕНДА ЧЕРЕЗ REMOTE PLAY ЗАВЕРШЕНА\n\n"
        "🎮 Игра: {game}\n"
        "⏰ Время аренды: {hours} ч.\n\n"
        "🔄 Сессия Remote Play отключена.\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "💖 Спасибо за аренду! Ждём вас снова!"
    ),
    "no_active_session": (
        "⚠ У вас нет активной Remote Play сессии.\n\n"
        "Возможные причины:\n"
        "• Аренда ещё не оформлена\n"
        "• Аренда уже завершена\n\n"
        "💡 Попробуйте купить аренду заново."
    ),
    "status_rp": (
        "📊 СТАТУС REMOTE PLAY\n\n"
        "🎮 Игра: {game}\n"
        "⏰ Осталось: {time_left}\n"
        "📅 До: {expires_at}\n"
        "🔗 Статус подключения: {connection_status}\n\n"
        "💡 Для нового PIN — !пин"
    ),
    "reminder_rp": (
        "⚠ НАПОМИНАНИЕ\n\n"
        "Remote Play аренда заканчивается через {minutes} минут!\n\n"
        "🎮 Игра: {game}\n\n"
        "После окончания сессия будет отключена."
    ),
    "help_rp": (
        "🟥 ПОМОЩЬ (Remote Play) 🟥\n\n"
        "💬 Доступные команды:\n\n"
        "  !пин — получить новый PIN для подключения\n"
        "  !статусrp — информация о сессии\n"
        "  !помощьrp — это сообщение\n\n"
        "⚡ Вы играете через Remote Play.\n"
        "Задержка зависит от интернет-соединения."
    ),
    "cheat_detected": (
        "🚨 ПОДОЗРЕНИЕ НА ЧИТЫ\n\n"
        "🎮 Аккаунт: {alias}\n"
        "👤 Покупатель: {buyer}\n"
        "📅 Время: {timestamp}\n\n"
        "AI-анализ скриншота выявил подозрительную активность.\n"
        "Confidence: {confidence}%\n"
        "Детали: {reasoning}"
    ),
    "no_accounts_rp": (
        "✖ Все Remote Play аккаунты заняты.\n\n"
        "🎮 Игра: {game}\n"
        "⏰ Ближайший аккаунт освободится через: {next_free_in}\n"
        "📅 Освободится в: {next_free} (МСК)\n\n"
        "📧 Попробуйте позже или напишите продавцу."
    ),
    "accounts_list": (
        "📋 Доступные аккаунты\n\n"
        "{lots}\n\n"
        "💬 Чтобы арендовать — оплатите лот на FunPay."
    ),
    "accounts_list_empty": (
        "📋 Доступные аккаунты\n\n"
        "✖ К сожалению, сейчас нет свободных аккаунтов.\n"
        "Напишите продавцу — он добавит."
    ),
    "accounts_list_lot_line": (
        "🎮 {game} ({free} шт.)\n"
        "   {logins}"
    ),
    # v2.23.0: блок «🔴 Сейчас в аренде» (показываем занятые аккаунты
    # с оставшимся временем, чтобы покупатель видел, что свободно
    # сейчас и когда что-то освободится).
    "accounts_list_busy_header": (
        "🔴 Сейчас в аренде:"
    ),
    "accounts_list_busy_line": (
        "⏰ {game} — {login} (осталось {remaining})"
    ),
    # ── Бронирование конкретного аккаунта (Irent <login>) ─────────────────
    "reserve_ok": (
        "✅ ВЫБРАН КОНКРЕТНЫЙ АККАУНТ / EXACT ACCOUNT SELECTED\n\n"
        "👤 Логин / Login: {login}\n"
        "🎮 Игра / Game: {game}\n\n"
        "💳 Оплатите лот аренды сейчас / Buy the rental lot now.\n\n"
        "🔗 Ссылка на аренду / Rental link:\n"
        "{link}\n\n"
        "⚠ Важно / Important: бронь действует {minutes} мин. "
        "Если аккаунт станет занят до оплаты — случайный аккаунт выдан НЕ будет."
    ),
    "reserve_busy": (
        "❌ Аккаунт {login} сейчас занят (в аренде или заморожен).\n\n"
        "Попробуйте другой логин или напишите !аккаунты — "
        "покажу свободные."
    ),
    "reserve_unknown": (
        "❌ Аккаунт {login} не найден.\n\n"
        "Проверьте правильность логина или напишите !аккаунты — "
        "покажу доступные."
    ),
    "reserve_no_lot": (
        "❌ Аккаунт {login} найден, но не привязан к лоту аренды.\n"
        "Обратитесь к продавцу."
    ),
    "reserve_already_held": (
        "ℹ У вас уже есть активная бронь на аккаунт {login}.\n"
        "🔗 Ссылка: {link}\n"
        "⏰ Действует до {expires}."
    ),
    "reserve_taken_by_other": (
        "❌ Аккаунт {login} уже забронирован другим покупателем.\n"
        "Попробуйте позже или выберите другой логин."
    ),
    "reserve_help": (
        "💬 Бронирование конкретного аккаунта\n\n"
        "Используйте: irent <логин>\n"
        "Пример: irent {example_login}\n\n"
        "Бот зарезервирует именно этот аккаунт и пришлёт ссылку на оплату."
    ),
    "reserve_expired_at_pay": (
        "⚠ Оплата принята, но активная бронь не найдена / "
        "Payment received, but no active rental was found.\n\n"
        "👤 Аккаунт: {login}\n\n"
        "Бронь истекла или аккаунт стал занят до оплаты. "
        "Случайный аккаунт мы не выдаём — пожалуйста, обратитесь к продавцу "
        "для возврата средств."
    ),
    # ── Одноразовый приоритет (!priority/!приоритет <login>) ──────────────
    "priority_set": (
        "✅ ПРИОРИТЕТ УСТАНОВЛЕН\n\n"
        "👤 Логин: {login}\n"
        "🎮 Игра: {game}\n\n"
        "При следующей покупке постараюсь выдать ИМЕННО этот аккаунт.\n"
        "Если он будет занят — получите случайный из пула.\n"
        "Приоритет одноразовый — сбросится после выдачи."
    ),
    "priority_unknown": (
        "❌ Аккаунт {login} не найден.\n"
        "Проверьте правильность логина."
    ),
    "priority_no_lot": (
        "❌ Аккаунт {login} есть, но не привязан к лоту аренды.\n"
        "Обратитесь к продавцу."
    ),
    "priority_help": (
        "💬 Одноразовый приоритет на аккаунт\n\n"
        "Используйте: !priority <логин>\n"
        "Пример: !priority {example_login}\n\n"
        "При следующей покупке бот попробует выдать именно этот акк. "
        "Если будет занят — выдаст случайный, и приоритет всё равно "
        "сбросится."
    ),
    "priority_replaced": (
        "ℹ Прежний приоритет ({old_login}) заменён на {login}."
    ),
    # ── Waitlist (!notify/!жду <login>) ───────────────────────────────────
    "waitlist_added": (
        "🟢 ВЫ В ЛИСТЕ ОЖИДАНИЯ\n\n"
        "👤 Логин: {login}\n"
        "📍 Позиция: {position}\n\n"
        "Когда аккаунт освободится, я сразу уведомлю первых "
        "{notify_top} человек."
    ),
    "waitlist_already": (
        "ℹ Вы уже в листе ожидания на {login}.\n"
        "📍 Позиция: {position}"
    ),
    "waitlist_full": (
        "❌ Лист ожидания на {login} переполнен.\n"
        "Попробуйте позже."
    ),
    "waitlist_unknown": (
        "❌ Аккаунт {login} не найден.\n"
        "Проверьте правильность логина."
    ),
    "waitlist_no_lot": (
        "❌ Аккаунт {login} не привязан к лоту аренды.\n"
        "Лист ожидания недоступен — обратитесь к продавцу."
    ),
    "waitlist_help": (
        "💬 Лист ожидания на аккаунт\n\n"
        "Используйте: !notify <логин>  или  !жду <логин>\n"
        "Пример: !жду {example_login}\n\n"
        "Когда указанный аккаунт освободится, я уведомлю первых "
        "{notify_top} человек из очереди."
    ),
    "waitlist_notified": (
        "🟢 АККАУНТ {login} ОСВОБОДИЛСЯ!\n\n"
        "🎮 Игра: {game}\n"
        "🔗 {link}\n\n"
        "⚡ Поторопитесь — оплачивайте лот, кто первый из {notify_top} "
        "ожидающих, тот и заберёт.\n"
        "💡 Совет: ДО оплаты напишите команду irent {login} — "
        "забронируете именно этот акк за собой на короткое время."
    ),
}

# v2.22: English translations of all 57 templates above. Used when buyer's
# lang is "en" (set via !engrent in FunPay chat). Names match RU keys 1:1.
# Canonical source of truth at runtime is the JSON file
# `storage/plugins/steam_rental/templates_en.json` — this dict only seeds it
# on first run.
_DEFAULT_TEMPLATES_EN: dict[str, str] = {
    "issue": (
        "🟩 ACCOUNT DELIVERED!\n"
        "🎮 Game: {game}\n\n"
        "🔑 Login: {login}\n"
        "🔒 Password: {password}\n"
        "⏰ Duration: {duration}\n\n"
        "💬 Commands: !code {login} | !extend\n"
        "⭐ +1 hour for a 5★ review!\n"
        "🔄 Password will be changed after rental"
    ),
    "post_delivery": "",
    "extend": (
        "🟥 RENTAL EXTENSION\n\n"
        "⚠ To extend your rental:\n\n"
        "1. Open the link below\n"
        "2. Pay for the desired number of hours\n"
        "3. Rental will be extended automatically!\n\n"
        "🔗 Extension link:\n"
        "{link}\n\n"
        "⏰ The lot is active for {ttl_minutes} minutes — "
        "if you don't make it, just type !extend again."
    ),
    "extended": (
        "🟥 RENTAL EXTENDED!\n"
        "🟩 Time added: {hours} h.\n"
        "🔵 New deadline: until {new_expires}\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🎮 Have fun! 🟥"
    ),
    "reminder": (
        "REMINDER!\n\n"
        "⚠ Your rental ends in {minutes} minutes!\n\n"
        "👤 Account: {login}\n"
        "🎮 Game: {game}\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "⭐ When the time runs out:\n"
        "• Password will be changed\n"
        "• Access will be revoked\n\n"
        "💡 Want to extend?\n"
        "Type !extend — I'll send you the extension link.\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📝 Don't forget to leave a review, thanks for renting!"
    ),
    "reminder_2": (
        "🔔 LAST REMINDER!\n\n"
        "⚠ Your rental ends in {minutes} minutes!\n\n"
        "👤 Account: {login}\n"
        "🎮 Game: {game}\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "⏳ Time is short — save your progress.\n"
        "💡 Want to keep playing? Type !extend — I'll send you the link.\n\n"
        "📝 Don't forget the 5★ review!"
    ),
    "expired": (
        "🟥 RENTAL ENDED\n\n"
        "👤 Account: {login}\n"
        "🎮 Game: {game}\n"
        "⏰ Rental time: {hours} h.\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🔄 Password has been changed automatically\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "💖 Thanks for renting! Come back any time 🟥"
    ),
    "guard_code": (
        "🟥 Steam Guard code for {login}: {code}\n"
        "(valid ~30 seconds)"
    ),
    "guard_error": (
        "⚠ ERROR\n\n"
        "✖ Account not found\n\n"
        "Possible reasons:\n"
        "• Wrong account login\n"
        "• You don't have an active rental\n"
        "• Rental already ended\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "💡 Try:\n"
        "• !code without login — auto-detect"
    ),
    "guard_error_no_secret": (
        "⚠ ERROR\n\n"
        "✖ Steam Guard unavailable\n\n"
        "This account doesn't have\n"
        "the mobile authenticator set up.\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📧 Contact the seller for a manual code"
    ),
    "no_accounts": (
        "✖ Sorry, all accounts are currently rented\n\n"
        "🎮 Game: {game}\n"
        "⏰ Next free in: {next_free_in}\n"
        "📅 Free at: {next_free} (MSK)\n\n"
        "💡 Want to join the queue?\n"
        "Type !queue and we'll notify you when an account is free"
    ),
    "queue_joined": (
        "✅ Added to the queue!\n\n"
        "🎮 Game: {game}\n"
        "📍 Position: {position}\n\n"
        "We'll notify you when an account becomes available."
    ),
    "queue_notified": (
        "🟢 {notify_template}\n\n"
        "🎮 Game: {game}\n"
        "⏰ You have 15 minutes."
    ),
    "queue_full": "❌ Queue is full. Try later.",
    "queue_already": (
        "ℹ You are already in the queue!\n"
        "📍 Position: {position}"
    ),
    "help": (
        "🟥 RENTAL HELP 🟥\n\n"
        "💬 Available commands:\n\n"
        "🔑 !code [login]\n"
        "↳ Get a Steam Guard code\n"
        "↳ Without login — for your account\n\n"
        "🔄 !extend\n"
        "↳ Extension instructions\n\n"
        "📊 !status\n"
        "↳ Rental info\n\n"
        "❓ !help\n"
        "↳ This message\n\n"
        "🌐 !rusrent — switch chat to Russian\n\n"
        "⚡ Commands work only in this chat"
    ),
    "status": (
        "📊 RENTAL STATUS\n\n"
        "👤 Account: {login}\n"
        "🎮 Game: {game}\n"
        "⏰ Time left: {minutes} min.\n"
        "📅 Until: {new_expires}\n\n"
        "💡 To extend, type !extend"
    ),
    "welcome": (
        "👋 Welcome!\n\n"
        "Available commands:\n"
        "• !code — get a Steam Guard code\n"
        "• !extend — extend the rental\n"
        "• !status — rental info\n"
        "• !help — all commands"
    ),
    "review_reward": (
        "🟧 REVIEW BONUS\n\n"
        "⭐ Thanks for the 5★ review!\n\n"
        "🟩 Rental extended by {hours} h.\n\n"
        "🎮 New deadline:\n"
        "↳ Until {new_expires}\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "💖 Thanks for your trust!"
    ),
    "review_deleted": (
        "🚨 REVIEW DELETED\n\n"
        "⚠ You deleted your review!\n\n"
        "🟥 Rental shortened by {hours} h.\n"
        "⏰ New deadline: until {new_expires}\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "⛔ WARNING: deleting another review\n"
        "will get you blacklisted and\n"
        "you won't be able to rent accounts!"
    ),
    "order_received": (
        "🟩 ORDER RECEIVED!\n\n"
        "⚡ Thanks for the purchase!\n"
        "🔄 Your order is being processed...\n\n"
        "✍ I'll deliver the account shortly\n"
        "  (usually 1-5 minutes)\n\n"
        "🎮 Please stay tuned!"
    ),
    "mr_request_photo": (
        "📷 To complete the order, please send a photo confirming "
        "you are at the PC club.\n\n"
        "Just send one photo to this chat."
    ),
    "mr_photo_received": (
        "✅ Photo successfully sent and is under review.\n\n"
        "Once the seller approves, the account will be delivered "
        "automatically."
    ),
    "mr_declined": (
        "❌ Sorry, the photo confirmation was not approved.\n\n"
        "The order has been cancelled and refunded to your FunPay balance."
    ),
    "issue_remoteplay": (
        "🟩 REMOTE PLAY RENTAL!\n"
        "🎮 Game: {game}\n\n"
        "🔗 Connect via Steam Link:\n"
        "📱 PIN code: {pin}\n\n"
        "📋 Instructions:\n"
        "1. Open Steam → Settings → Remote Play\n"
        "2. Click \"Pair Steam Link\"\n"
        "3. Enter PIN: {pin}\n\n"
        "⏰ Rental duration: {duration}\n"
        "⚠ PIN is valid for 5 minutes!\n\n"
        "💬 Commands:\n"
        "   !pin — get a new PIN (if the old one expired)\n"
        "   !statusrp — session status\n"
        "   !helprp — list of commands"
    ),
    "pin_generated": (
        "🔗 NEW PIN FOR REMOTE PLAY\n\n"
        "📱 PIN code: {pin}\n\n"
        "Enter it in Remote Play settings → Pair Steam Link.\n"
        "⚠ Valid for 5 minutes!\n\n"
        "⏰ Rental remaining: {time_left}"
    ),
    "pin_error": (
        "⚠ ERROR\n\n"
        "✖ Could not generate a PIN.\n"
        "Possible reasons:\n"
        "• Remote Play is not enabled on the account\n"
        "• Authentication error\n\n"
        "📧 Contact the seller."
    ),
    "session_expired": (
        "🟥 REMOTE PLAY RENTAL ENDED\n\n"
        "🎮 Game: {game}\n"
        "⏰ Rental time: {hours} h.\n\n"
        "🔄 Remote Play session disconnected.\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "💖 Thanks for renting! Come back any time!"
    ),
    "no_active_session": (
        "⚠ You don't have an active Remote Play session.\n\n"
        "Possible reasons:\n"
        "• Rental hasn't been ordered yet\n"
        "• Rental already ended\n\n"
        "💡 Try buying a rental again."
    ),
    "status_rp": (
        "📊 REMOTE PLAY STATUS\n\n"
        "🎮 Game: {game}\n"
        "⏰ Time left: {time_left}\n"
        "📅 Until: {expires_at}\n"
        "🔗 Connection status: {connection_status}\n\n"
        "💡 For a new PIN, use !pin"
    ),
    "reminder_rp": (
        "⚠ REMINDER\n\n"
        "Remote Play rental ends in {minutes} minutes!\n\n"
        "🎮 Game: {game}\n\n"
        "After it ends, the session will be disconnected."
    ),
    "help_rp": (
        "🟥 HELP (Remote Play) 🟥\n\n"
        "💬 Available commands:\n\n"
        "  !pin — get a new connection PIN\n"
        "  !statusrp — session info\n"
        "  !helprp — this message\n\n"
        "⚡ You're playing via Remote Play.\n"
        "Latency depends on your internet connection."
    ),
    "cheat_detected": (
        "🚨 CHEATING SUSPECTED\n\n"
        "🎮 Account: {alias}\n"
        "👤 Buyer: {buyer}\n"
        "📅 Time: {timestamp}\n\n"
        "AI screenshot analysis flagged suspicious activity.\n"
        "Confidence: {confidence}%\n"
        "Details: {reasoning}"
    ),
    "no_accounts_rp": (
        "✖ All Remote Play accounts are taken.\n\n"
        "🎮 Game: {game}\n"
        "⏰ Next free in: {next_free_in}\n"
        "📅 Free at: {next_free} (MSK)\n\n"
        "📧 Try later or message the seller."
    ),
    "accounts_list": (
        "📋 Available accounts\n\n"
        "{lots}\n\n"
        "💬 To rent — pay the lot on FunPay."
    ),
    "accounts_list_empty": (
        "📋 Available accounts\n\n"
        "✖ Sorry, no free accounts right now.\n"
        "Message the seller — they'll add some."
    ),
    "accounts_list_lot_line": (
        "🎮 {game} ({free} pcs.)\n"
        "   {logins}"
    ),
    "accounts_list_busy_header": (
        "🔴 Currently rented:"
    ),
    "accounts_list_busy_line": (
        "⏰ {game} — {login} ({remaining} left)"
    ),
    "reserve_ok": (
        "✅ EXACT ACCOUNT SELECTED\n\n"
        "👤 Login: {login}\n"
        "🎮 Game: {game}\n\n"
        "💳 Buy the rental lot now.\n\n"
        "🔗 Rental link:\n"
        "{link}\n\n"
        "⚠ Important: the reservation is valid for {minutes} min. "
        "If the account becomes busy before you pay — no random "
        "account will be issued."
    ),
    "reserve_busy": (
        "❌ Account {login} is currently busy (rented or frozen).\n\n"
        "Try a different login or type !accounts to see what's free."
    ),
    "reserve_unknown": (
        "❌ Account {login} not found.\n\n"
        "Check the login spelling or type !accounts to see "
        "available ones."
    ),
    "reserve_no_lot": (
        "❌ Account {login} found, but is not linked to a rental lot.\n"
        "Contact the seller."
    ),
    "reserve_already_held": (
        "ℹ You already have an active reservation for {login}.\n"
        "🔗 Link: {link}\n"
        "⏰ Valid until {expires}."
    ),
    "reserve_taken_by_other": (
        "❌ Account {login} is already reserved by another buyer.\n"
        "Try later or pick a different login."
    ),
    "reserve_help": (
        "💬 Reserve a specific account\n\n"
        "Use: irent <login>\n"
        "Example: irent {example_login}\n\n"
        "I'll reserve exactly this account and send you a payment link."
    ),
    "reserve_expired_at_pay": (
        "⚠ Payment received, but no active reservation was found.\n\n"
        "👤 Account: {login}\n\n"
        "The reservation expired or the account became busy before payment. "
        "We don't issue a random account in this case — please contact "
        "the seller for a refund."
    ),
    "priority_set": (
        "✅ PRIORITY SET\n\n"
        "👤 Login: {login}\n"
        "🎮 Game: {game}\n\n"
        "On your next purchase I'll try to deliver this exact account.\n"
        "If it's busy — you'll get a random one from the pool.\n"
        "Priority is one-shot — it resets after delivery."
    ),
    "priority_unknown": (
        "❌ Account {login} not found.\n"
        "Check the login spelling."
    ),
    "priority_no_lot": (
        "❌ Account {login} exists, but is not linked to a rental lot.\n"
        "Contact the seller."
    ),
    "priority_help": (
        "💬 One-shot priority on an account\n\n"
        "Use: !priority <login>\n"
        "Example: !priority {example_login}\n\n"
        "On your next purchase the bot will try to deliver this exact "
        "account. If it's busy — a random one is delivered, and the "
        "priority resets anyway."
    ),
    "priority_replaced": (
        "ℹ Previous priority ({old_login}) replaced with {login}."
    ),
    "waitlist_added": (
        "🟢 ADDED TO WAITLIST\n\n"
        "👤 Login: {login}\n"
        "📍 Position: {position}\n\n"
        "When the account is free I'll notify the first {notify_top} "
        "people in line."
    ),
    "waitlist_already": (
        "ℹ You are already on the waitlist for {login}.\n"
        "📍 Position: {position}"
    ),
    "waitlist_full": (
        "❌ Waitlist for {login} is full.\n"
        "Try later."
    ),
    "waitlist_unknown": (
        "❌ Account {login} not found.\n"
        "Check the login spelling."
    ),
    "waitlist_no_lot": (
        "❌ Account {login} is not linked to a rental lot.\n"
        "Waitlist is unavailable — contact the seller."
    ),
    "waitlist_help": (
        "💬 Waitlist for an account\n\n"
        "Use: !notify <login>\n"
        "Example: !notify {example_login}\n\n"
        "When that account is free I'll notify the first {notify_top} "
        "people in line."
    ),
    "waitlist_notified": (
        "🟢 ACCOUNT {login} IS FREE!\n\n"
        "🎮 Game: {game}\n"
        "🔗 {link}\n\n"
        "⚡ Hurry — pay the lot, the first of the {notify_top} "
        "waitlisted gets it.\n"
        "💡 Tip: BEFORE paying, type irent {login} to lock this exact "
        "account for a few minutes."
    ),
}


_DEFAULT_CONFIG: dict[str, Any] = {
    "change_password_on_expire": True,
    # ⚠ По умолчанию ВЫКЛ. До v2.13 этот флаг лежал True, но по факту
    # auto-revoke сессий в end_rental был ХАРДКОДОМ выключен (флаг ничего
    # не делал). Теперь флаг работает: при истечении/завершении аренды
    # бот сделает revoke other sessions ровно если он = True. Включать
    # имеет смысл, когда стабильно проходит change_password (Error 24
    # починен). Включить можно в ⚙ Настройки → 🔒 Безопасность.
    "revoke_sessions_on_expire": False,
    "tg_notify": True,
    "guardik_command": "!код",
    "reminder_minutes": 30,
    "reminder_minutes_2": 10,
    "review_bonus_enabled": True,
    "review_bonus_hours": 1,
    # Минимальное число звёзд для бонуса. Стандарт — 5★. Понижение
    # допустимо, но осмысленно только если у вас reliable parser:
    # FunPay JSON-API часто отдаёт rating=null даже на видимых 5★.
    "review_bonus_min_stars": 5,
    # Оптимистичный режим: если review.text НЕ ПУСТОЙ, а stars=0/None
    # (FunPay не вернул рейтинг) — считаем отзыв как «квалифицирующий»
    # и начисляем бонус. Триггер: 1) Review.stars недоступен в твоей
    # версии FunPayAPI; 2) автор писал «звёзды никак не запарсить».
    # Минус: 1★ отзыв с текстом тоже даст бонус. По умолчанию ВКЛ.
    "review_bonus_optimistic_unknown": True,
    "review_delete_penalty_enabled": True,
    "review_delete_penalty_hours": 1,
    "review_delete_blacklist": True,
    "auto_deactivate_lots": True,
    "auto_deliver": True,
    # «📧 Доп. информация» (post-delivery message) по умолчанию ВЫКЛ —
    # из коробки шлём только основной шаблон выдачи. Включить можно
    # глобально (⚙ Настройки → 🔔 Уведомления) или точечно — кастомным
    # текстом per-account / per-lot через кнопку «📧 Доп. инфо».
    "post_delivery_message_enabled": False,
    "post_delivery_delay_seconds": 3,
    "min_rental_hours": 1,
    "max_rental_hours": 1668,
    "auto_extend_enabled": True,
    # v2.22: язык по умолчанию для новых покупателей. Покупатель может
    # переключить себе командой !engrent (en) / !rusrent (ru) в чате FunPay.
    "default_language": "ru",
    # v2.21: дефолт длительности extension-лота, если в описании лота нет
    # тэга #Hours: / #Time: и у самого лота нет legacy-значения duration_min.
    "extension_default_minutes": 60,
    # v2.16.1: TTL extension-лота, активированного по команде !продлить.
    # Если за это время покупатель не оплатил — лот деактивируется обратно
    # (раньше он висел включённым на FunPay бесконечно). 0 — без таймаута.
    "extension_active_ttl_minutes": 10,
    # v2.15: опциональный fallback для extension-покупок. Срабатывает,
    # если плагин получил новый заказ, но не смог распознать lot_id ни
    # как extension-лот (через is_extension/extension_lot_ids), ни как
    # main-лот (через _match_lot_by_game / _match_lot). Например, FunPay
    # отдал заказ без id (lot_id=None), а в title/desc есть слово
    # «ПРОДЛЕНИЕ»/«extend» — это явно покупка лота-продления, и у
    # покупателя есть активная аренда → продлеваем её, выбрав
    # extension-лот по игре аренды (через _find_extension_lot_for_alias).
    # Защита: если в desc упомянута игра, отличная от игры активной
    # аренды — отказ (чтобы не продлить чужую игру).
    # v2.19: ВКЛЮЧЕН по умолчанию. Раньше OFF из осторожности, но в
    # проде это вело к молчаливым провалам (FunPay часто отдаёт
    # extension-заказы без lot_id → плагин писал «лот None НЕ настроен»,
    # деньги получены, аренда не продлена). Защиты от ложного матча
    # уже встроены — fallback требует совпадения по игре.
    "extension_buyer_fallback_enabled": True,
    "check_accounts_on_start": True,
    "templates": dict(_DEFAULT_TEMPLATES),
    # VAC/Trade ban scan
    "steam_api_key": "",
    "vac_scan_enabled": False,
    "vac_scan_interval_min": 60,
    # PC-club mode (AI verification)
    "ai_provider": "openrouter",  # openrouter|openai|anthropic|google
    "openrouter_api_key": "",
    "openrouter_model": "google/gemini-2.0-flash-exp:free",
    "openai_api_key": "",
    "openai_model": "gpt-4o-mini",
    "anthropic_api_key": "",
    "anthropic_model": "claude-3-5-haiku-20241022",
    "google_ai_api_key": "",
    "google_ai_model": "gemini-1.5-flash",
    "club_mode_global_enabled": False,
    "club_auto_approve_threshold": 80,
    "club_auto_decline_threshold": 30,
    "club_request_ttl_hours": 24,
    "seller_funpay_nickname": "",
    "pcclub_command": "/pcclub",
    # ── AI-fake / deepfake детектор ───────────────────────────────────────
    # Перед обычной верификацией фото плагин делает отдельный вызов AI,
    # который оценивает признаки AI-генерации (Midjourney/SD/DALL-E/Sora/
    # Photoshop/composite). Если score >= decline → авто-отказ заявки.
    # Если score >= manual → серая зона, отправка на ручную проверку.
    "ai_fake_detector_enabled": True,
    "ai_fake_decline_threshold": 70,
    "ai_fake_manual_threshold": 40,
    # При включённом флаге детектор работает и для manual_review-фото
    # (не только для PC-club).
    "ai_fake_detector_in_manual_review": True,
    # v5: blacklist покупателей. Срабатывает на NEW_ORDER + auto-add при refund.
    "blacklist_enabled": True,
    "auto_blacklist_on_refund": True,
    # v5: Prometheus /metrics endpoint
    "metrics_enabled": False,
    "metrics_port": 9101,
    "metrics_bind": "0.0.0.0",
    # v5: Daily summary в Telegram. Час задаётся в МСК (00 — полночь МСК).
    "daily_summary_enabled": True,
    "daily_summary_hour_msk": 0,
    # v5: explicit recovery после краха VM
    "recovery_on_start": True,
    # ── Remote Play settings ──────────────────────────────────────────────
    "pin_ttl_seconds": 300,
    "monitoring_enabled": False,
    "monitoring_interval_seconds": 300,
    "anticheat_ai_enabled": False,
    "anticheat_confidence_threshold": 70,
    "auto_disconnect_on_cheat": False,
    # ── Queue settings ────────────────────────────────────────────────────
    "queue_enabled": True,
    "queue_ttl_hours": 24,
    "queue_notify_template": "Аккаунт освободился! Оплатите лот в течение 15 минут для гарантированного доступа.",
    "queue_max_per_lot": 10,
    # ── Бронирование конкретного аккаунта (Irent <login>) ─────────────────
    # Если включено — покупатель может командой `irent <логин>` (или
    # `!арендую <логин>` / `!reserve <логин>`) зарезервировать конкретный
    # Steam-аккаунт на TTL минут. После оплаты лота выдаётся ИМЕННО этот
    # аккаунт. Если бронь протухла / аккаунт стал занят к моменту оплаты —
    # случайный аккаунт НЕ выдаётся, оператор получает уведомление в TG.
    "reservations_enabled": True,
    "reservations_ttl_minutes": 20,
    # Дополнительные алиасы команды (через запятую). Префиксы !/(пусто)
    # допустимы — нормализуем при разборе.
    "reservations_commands": "irent,!арендую,!reserve,!забронировать",
    # ── Одноразовый приоритет (!priority/!приоритет <login>) ──────────────
    # При оплате бот посмотрит, есть ли у buyer_id записанный приоритет
    # на свободный alias из этого лота — и предпочтёт его. Если нет/занят —
    # обычный _pick_free_alias. После выдачи приоритет сбрасывается.
    "priority_enabled": True,
    "priority_ttl_hours": 24,
    "priority_commands": "!priority,!приоритет",
    # ── Waitlist (!notify/!жду <login>) ───────────────────────────────────
    # Лист ожидания на КОНКРЕТНЫЙ аккаунт. При end_rental(alias) уведомляем
    # top-N ожидающих в этом списке.
    "waitlist_enabled": True,
    "waitlist_max_per_alias": 10,
    "waitlist_notify_top": 3,
    "waitlist_ttl_hours": 24,
    "waitlist_commands": "!notify,!жду",
}

# ── Бронирование конкретного аккаунта (reservations) ────────────────────────
# Хранилище: {"items": {alias: {alias, buyer_id, buyer_username, chat_id,
#                                lot_key, created_ts, expires_ts}}}
# Один alias = одна активная бронь. Один buyer_id может держать максимум
# одну бронь — повторный irent <login> переоткрывает её на новый акк.

def _load_reservations() -> dict[str, Any]:
    return _load_json(RESERVATIONS_FILE, {"items": {}})


def _save_reservations(data: dict[str, Any]) -> None:
    _save_json(RESERVATIONS_FILE, data)


def _purge_expired_reservations() -> int:
    """Удаляет протухшие брони. Возвращает кол-во удалённых."""
    now = _now()
    removed = 0
    with _lock:
        data = _load_reservations()
        items = data.get("items", {})
        for alias in list(items.keys()):
            r = items.get(alias) or {}
            if int(r.get("expires_ts") or 0) <= now:
                items.pop(alias, None)
                removed += 1
        if removed:
            # `items` уже та же ссылка что в data["items"], переприсваивать
            # не нужно — просто сохраняем data на диск.
            _save_reservations(data)
    return removed


def _find_reservation_for_buyer(buyer_id: int,
                                lot_key: str | None = None
                                ) -> tuple[str, dict[str, Any]] | None:
    """Возвращает (alias, reservation) — активную бронь покупателя.
    Если задан lot_key — фильтруем по нему."""
    _purge_expired_reservations()
    data = _load_reservations()
    for alias, r in data.get("items", {}).items():
        if int(r.get("buyer_id", -1)) != int(buyer_id):
            continue
        if lot_key and str(r.get("lot_key")) != str(lot_key):
            continue
        return alias, r
    return None


def _find_lot_by_alias(alias: str) -> tuple[str, dict[str, Any]] | None:
    """Возвращает (lot_key, lot) лота, в пуле которого есть данный alias.
    Приоритет: rental-лоты с числовым ключом (валидная offer-ссылка),
    затем любые остальные."""
    lots = list_lots()
    candidates: list[tuple[str, dict[str, Any]]] = []
    for key, val in lots.items():
        if alias in val.get("aliases", []):
            candidates.append((key, val))
    if not candidates:
        return None
    # Сначала числовые ключи (FunPay offer-id), исключая extension-лоты.
    for key, val in candidates:
        if key.isdigit() and not val.get("is_extension"):
            return key, val
    return candidates[0]


def _try_create_reservation_atomic(
        alias: str, buyer_id: int, buyer_username: str, chat_id: Any,
        lot_key: str, ttl_minutes: int) -> tuple[str, dict[str, Any] | None]:
    """Атомарная проверка-и-создание брони под одним `_lock`.
    Возвращает (status, entry):
      - ("ok", entry)             — бронь создана/обновлена
      - ("already_self", entry)   — у buyer_id уже своя бронь на этот alias
      - ("taken_by_other", None)  — alias забронирован другим
    """
    now = _now()
    with _lock:
        data = _load_reservations()
        items = data.setdefault("items", {})
        # Сначала чистим протухшее (inline, без отдельной функции — мы
        # уже под локом, рекурсия по _purge_expired_reservations поломает).
        for a in list(items.keys()):
            if int((items.get(a) or {}).get("expires_ts") or 0) <= now:
                items.pop(a, None)

        cur = items.get(alias)
        if cur:
            cur_buyer = int(cur.get("buyer_id", -1))
            if cur_buyer != int(buyer_id):
                return ("taken_by_other", None)
            # своя — обновим TTL и ссылочные поля
        # Снимаем прежние брони этого покупателя на ДРУГИЕ alias.
        for prev_alias in list(items.keys()):
            r = items[prev_alias]
            if int(r.get("buyer_id", -1)) == int(buyer_id) \
                    and prev_alias != alias:
                items.pop(prev_alias, None)

        entry = {
            "alias": alias,
            "buyer_id": int(buyer_id),
            "buyer_username": str(buyer_username or ""),
            "chat_id": chat_id,
            "lot_key": str(lot_key),
            "created_ts": now,
            "expires_ts": now + max(1, ttl_minutes) * 60,
        }
        was_self = bool(cur)
        items[alias] = entry
        _save_reservations(data)
        return ("already_self" if was_self else "ok", entry)


def _create_reservation(alias: str, buyer_id: int, buyer_username: str,
                        chat_id: Any, lot_key: str,
                        ttl_minutes: int) -> dict[str, Any]:
    """Создаёт/обновляет бронь. Если у buyer_id уже есть бронь на ДРУГОЙ
    alias — снимаем её (один buyer = одна бронь)."""
    now = _now()
    with _lock:
        data = _load_reservations()
        items = data.setdefault("items", {})
        # Снимаем прежние брони этого покупателя.
        for prev_alias in list(items.keys()):
            r = items[prev_alias]
            if int(r.get("buyer_id", -1)) == int(buyer_id) \
                    and prev_alias != alias:
                items.pop(prev_alias, None)
        entry = {
            "alias": alias,
            "buyer_id": int(buyer_id),
            "buyer_username": str(buyer_username or ""),
            "chat_id": chat_id,
            "lot_key": str(lot_key),
            "created_ts": now,
            "expires_ts": now + max(1, ttl_minutes) * 60,
        }
        items[alias] = entry
        _save_reservations(data)
        return entry


def _release_reservation(alias: str) -> bool:
    """Снимает бронь по alias. Возвращает True если удалили запись."""
    with _lock:
        data = _load_reservations()
        items = data.setdefault("items", {})
        if alias in items:
            items.pop(alias, None)
            _save_reservations(data)
            return True
    return False


def _release_reservation_after_delivery(alias: str,
                                        delivered_to_buyer_id: int | None
                                        ) -> None:
    """Снимает бронь после успешной выдачи + логирует случай, когда
    выдача ушла НЕ владельцу брони (alias забрал случайный заказ от
    другого покупателя). Это семантический сигнал, что бронь не
    защитила alias — оператору полезно видеть в actions.log."""
    with _lock:
        data = _load_reservations()
        items = data.setdefault("items", {})
        prev = items.get(alias)
        if prev is None:
            return
        prev_buyer = int(prev.get("buyer_id", -1))
        items.pop(alias, None)
        _save_reservations(data)
    try:
        if (delivered_to_buyer_id is not None
                and prev_buyer != int(delivered_to_buyer_id)):
            _log_action(
                "reservation_lost_on_other_delivery",
                f"Бронь {alias} затёрта выдачей другому покупателю",
                alias=alias,
                reservation_buyer_id=prev_buyer,
                delivered_to_buyer_id=int(delivered_to_buyer_id))
    except Exception:
        LOGGER.debug("steam_rental: log reservation_lost failed",
                     exc_info=True)


def _is_alias_rentable(acc: dict[str, Any] | None) -> bool:
    """True если аккаунт сейчас можно выдать в обычную (non-RP) аренду."""
    if not acc:
        return False
    if acc.get("frozen"):
        return False
    if acc.get("rental"):
        return False
    if _account_pool(acc) == "remoteplay":
        return False
    if find_active_rp_session_by_alias(acc.get("alias", "")):
        return False
    return True


# ── Одноразовый приоритет на конкретный аккаунт ─────────────────────────────
# Хранилище: {"items": {str(buyer_id): {alias, login, lot_key, created_ts,
#                                        expires_ts}}}
def _load_priorities() -> dict[str, Any]:
    return _load_json(PRIORITIES_FILE, {"items": {}})


def _save_priorities(data: dict[str, Any]) -> None:
    _save_json(PRIORITIES_FILE, data)


def _purge_expired_priorities() -> int:
    now = _now()
    removed = 0
    with _lock:
        data = _load_priorities()
        items = data.get("items", {})
        for k in list(items.keys()):
            if int((items.get(k) or {}).get("expires_ts") or 0) <= now:
                items.pop(k, None)
                removed += 1
        if removed:
            # `items` — та же ссылка, переприсваивать не нужно.
            _save_priorities(data)
    return removed


def _set_priority(buyer_id: int, alias: str, login: str, lot_key: str,
                  ttl_hours: int) -> dict[str, Any]:
    """Записывает/перезаписывает приоритет покупателя. Возвращает прежнюю
    запись (или {}) — удобно для сообщения «было заменено»."""
    now = _now()
    with _lock:
        data = _load_priorities()
        items = data.setdefault("items", {})
        prev = items.get(str(buyer_id), {}) or {}
        items[str(buyer_id)] = {
            "alias": alias,
            "login": login,
            "lot_key": str(lot_key),
            "created_ts": now,
            "expires_ts": now + max(1, ttl_hours) * 3600,
        }
        _save_priorities(data)
        return prev


def _get_priority(buyer_id: int) -> dict[str, Any] | None:
    with _lock:
        # Inline-purge под локом, чтобы не было гонки с _set_priority/
        # _consume_priority между чисткой и чтением.
        now = _now()
        data = _load_priorities()
        items = data.setdefault("items", {})
        changed = False
        for k in list(items.keys()):
            if int((items.get(k) or {}).get("expires_ts") or 0) <= now:
                items.pop(k, None)
                changed = True
        if changed:
            _save_priorities(data)
        return items.get(str(buyer_id))


def _consume_priority(buyer_id: int) -> bool:
    with _lock:
        data = _load_priorities()
        items = data.setdefault("items", {})
        if str(buyer_id) in items:
            items.pop(str(buyer_id), None)
            _save_priorities(data)
            return True
    return False


# ── Waitlist на конкретный аккаунт ──────────────────────────────────────────
# Хранилище: {"items": {alias: [{buyer_id, buyer_username, chat_id,
#                                 queued_at, notified_ts, lot_key}]}}
def _load_waitlist() -> dict[str, Any]:
    return _load_json(WAITLIST_FILE, {"items": {}})


def _save_waitlist(data: dict[str, Any]) -> None:
    _save_json(WAITLIST_FILE, data)


def _waitlist_add(alias: str, buyer_id: int, buyer_username: str,
                  chat_id: Any, lot_key: str) -> dict[str, Any]:
    """Добавляет в лист ожидания. Возвращает {ok, position?, reason?}."""
    cfg = get_config()
    max_per = int(cfg.get("waitlist_max_per_alias", 10) or 10)
    with _lock:
        data = _load_waitlist()
        items = data.setdefault("items", {})
        lst = items.setdefault(alias, [])
        # Уже в листе?
        for i, e in enumerate(lst):
            if int(e.get("buyer_id", 0)) == int(buyer_id):
                return {"ok": False, "reason": "already",
                        "position": i + 1}
        if len(lst) >= max_per:
            return {"ok": False, "reason": "full"}
        lst.append({
            "buyer_id": int(buyer_id),
            "buyer_username": str(buyer_username or ""),
            "chat_id": chat_id,
            "lot_key": str(lot_key),
            "queued_at": _now(),
            "notified_ts": 0,
        })
        items[alias] = lst
        _save_waitlist(data)
        return {"ok": True, "position": len(lst)}


def _waitlist_cleanup_alias(alias: str) -> None:
    """Удаляет протухшие записи у конкретного alias."""
    cfg = get_config()
    ttl_h = int(cfg.get("waitlist_ttl_hours", 24) or 24)
    cutoff = _now() - ttl_h * 3600
    with _lock:
        data = _load_waitlist()
        items = data.setdefault("items", {})
        lst = items.get(alias, [])
        new_lst = [e for e in lst if int(e.get("queued_at", 0)) > cutoff]
        if len(new_lst) != len(lst):
            if new_lst:
                items[alias] = new_lst
            else:
                items.pop(alias, None)
            _save_waitlist(data)


def _waitlist_notify_top(cardinal: "Cardinal | None", alias: str,
                         lot_key: str, login: str, game: str) -> int:
    """Уведомляет top-N ожидающих, что alias освободился. Возвращает кол-во
    отправленных уведомлений. Уведомлённые помечаются и удаляются после."""
    cfg = get_config()
    if not cfg.get("waitlist_enabled", True):
        return 0
    if cardinal is None:
        return 0
    top_n = max(1, int(cfg.get("waitlist_notify_top", 3) or 3))
    _waitlist_cleanup_alias(alias)
    with _lock:
        data = _load_waitlist()
        items = data.setdefault("items", {})
        lst = list(items.get(alias, []))
        if not lst:
            return 0
        targets = lst[:top_n]
    if not targets:
        return 0

    link = (f"https://funpay.com/lots/offer?id={lot_key}"
            if str(lot_key).isdigit() else "—")
    sent = 0
    notified_buyer_ids: set[int] = set()
    for e in targets:
        try:
            # v2.22: рендерим шаблон отдельно для каждого получателя —
            # у разных покупателей в waitlist может быть разный язык чата.
            text = _render_template(
                "waitlist_notified",
                buyer_id=e.get("buyer_id"),
                login=login,
                game=game or "—",
                link=link,
                notify_top=str(top_n))
            cardinal.send_message(
                e.get("chat_id"),
                _strip_html(text),
                chat_name=e.get("buyer_username"),
                interlocutor_id=e.get("buyer_id"),
                watermark=False)
            notified_buyer_ids.add(int(e.get("buyer_id", 0)))
            sent += 1
        except Exception:
            LOGGER.debug("steam_rental: waitlist notify send failed",
                         exc_info=True)

    # Уведомлённых удаляем; параллельно добавленных за время отправки —
    # сохраняем (перечитываем lst внутри лока и фильтруем по buyer_id).
    if notified_buyer_ids:
        with _lock:
            data = _load_waitlist()
            items = data.setdefault("items", {})
            cur_lst = items.get(alias, [])
            new_lst = [e for e in cur_lst
                       if int(e.get("buyer_id", 0)) not in notified_buyer_ids]
            if new_lst:
                items[alias] = new_lst
            else:
                items.pop(alias, None)
            _save_waitlist(data)
    return sent


# ── Защита от двойного клика по operator_stop ───────────────────────────────
# Set алиасов, для которых сейчас выполняется end_rental в фоне.
# Любой повторный клик «🛑 Прервать» по тому же alias увидит «уже
# останавливается». Гард установлен и в самом end_rental — это покрывает
# все пути (TG, шедулер, recovery_expired). UI-чек ниже — best-effort
# для приятного UX, чтобы не плодить тредов вхолостую.
_stopping_aliases_lock = threading.Lock()
_stopping_aliases: set[str] = set()


def _try_mark_stopping(alias: str) -> bool:
    """Атомарно помечает alias как 'останавливается'. Возвращает True если
    мы первые (можно стартовать end_rental); False если уже идёт."""
    if not alias:
        return False
    with _stopping_aliases_lock:
        if alias in _stopping_aliases:
            return False
        _stopping_aliases.add(alias)
        return True


def _is_stopping(alias: str) -> bool:
    """Best-effort peek без мутации — для UX в TG-кнопках."""
    if not alias:
        return False
    with _stopping_aliases_lock:
        return alias in _stopping_aliases


def _unmark_stopping(alias: str) -> None:
    if not alias:
        return
    with _stopping_aliases_lock:
        _stopping_aliases.discard(alias)


def _end_rental_guarded(cardinal: "Cardinal | None", alias: str,
                        *, reason: str = "expire") -> dict[str, Any]:
    """Backward-compat: end_rental теперь сам ставит/снимает гард,
    эта функция остаётся как тонкая обёртка для существующих вызовов."""
    return end_rental(cardinal, alias, reason=reason)


# ── Ивенты (события/расписание) ──────────────────────────────────────────────
def _load_events() -> dict[str, Any]:
    return _load_json(EVENTS_FILE, {
        "unclosed_notify": {
            "enabled": True,
            "interval_hours": 24,
            "last_run": 0,
            "next_run": 0,
        }
    })


def _save_events(events: dict[str, Any]) -> None:
    _save_json(EVENTS_FILE, events)


# ── PC-club / whitelist хранилище ───────────────────────────────────────────
def _load_clubs() -> dict[str, Any]:
    return _load_json(CLUBS_FILE, {
        "whitelist": {},   # {funpay_user_id_str: {username, approved_ts, attempts, last_order}}
        "requests": {},    # {order_id_str: {buyer_id, buyer_username, chat_id, code,
                           #   lot_key, duration_min, status, ai_verdict, photo_url,
                           #   created_ts, decided_ts, decided_by}}
        "stats": {"ai_calls": 0, "ai_approves": 0, "ai_declines": 0,
                  "manual_approves": 0, "manual_declines": 0},
    })


def _save_clubs(data: dict[str, Any]) -> None:
    _save_json(CLUBS_FILE, data)


def _club_in_whitelist(funpay_user_id: int | str) -> bool:
    return str(funpay_user_id) in _load_clubs().get("whitelist", {})


def _club_add_to_whitelist(funpay_user_id: int | str,
                           username: str = "",
                           order_id: str = "") -> None:
    with _lock:
        data = _load_clubs()
        wl = data.setdefault("whitelist", {})
        key = str(funpay_user_id)
        entry = wl.get(key, {})
        entry.update({
            "username": username or entry.get("username", ""),
            "approved_ts": _now(),
            "attempts": entry.get("attempts", 0) + 1,
            "last_order": order_id or entry.get("last_order", ""),
        })
        wl[key] = entry
        _save_clubs(data)


def _club_remove_from_whitelist(funpay_user_id: int | str) -> bool:
    with _lock:
        data = _load_clubs()
        wl = data.setdefault("whitelist", {})
        key = str(funpay_user_id)
        if key not in wl:
            return False
        del wl[key]
        _save_clubs(data)
        return True


def _club_list_whitelist() -> list[dict[str, Any]]:
    wl = _load_clubs().get("whitelist", {})
    out = []
    for k, v in wl.items():
        item = dict(v)
        item["funpay_user_id"] = k
        out.append(item)
    out.sort(key=lambda x: x.get("approved_ts", 0), reverse=True)
    return out


def _club_gen_code() -> str:
    """Уникальный код верификации формата VRF-XXXX."""
    chars = string.ascii_uppercase + string.digits
    chars = chars.replace("0", "").replace("O", "")
    chars = chars.replace("1", "").replace("I", "").replace("L", "")
    return "VRF-" + "".join(pysecrets.choice(chars) for _ in range(4))


def _club_create_request(order_id: str, buyer_id: int, buyer_username: str,
                         chat_id: int | str, lot_key: str,
                         duration_min: int) -> dict[str, Any]:
    with _lock:
        data = _load_clubs()
        reqs = data.setdefault("requests", {})
        code = _club_gen_code()
        # Гарантируем уникальность кода среди активных заявок.
        active = {r.get("code") for r in reqs.values()
                  if r.get("status") in ("awaiting_photo", "verifying", "manual")}
        while code in active:
            code = _club_gen_code()
        entry = {
            "buyer_id": int(buyer_id),
            "buyer_username": str(buyer_username),
            "chat_id": chat_id,
            "code": code,
            "lot_key": lot_key,
            "duration_min": int(duration_min),
            "status": "awaiting_command",
            "ai_verdict": None,
            "photo_url": None,
            "photo_local": None,
            "created_ts": _now(),
            "decided_ts": 0,
            "decided_by": "",
            "alias_issued": "",
        }
        reqs[str(order_id)] = entry
        _save_clubs(data)
        return entry


def _club_update_request(order_id: str, **fields: Any) -> dict[str, Any] | None:
    with _lock:
        data = _load_clubs()
        reqs = data.setdefault("requests", {})
        if str(order_id) not in reqs:
            return None
        reqs[str(order_id)].update(fields)
        _save_clubs(data)
        return reqs[str(order_id)]


def _club_get_request(order_id: str) -> dict[str, Any] | None:
    return _load_clubs().get("requests", {}).get(str(order_id))


def _club_find_request_by_buyer(buyer_id: int) -> tuple[str, dict[str, Any]] | None:
    """Ищет ACTIVE заявку покупателя (awaiting_command / awaiting_photo /
    verifying / manual)."""
    data = _load_clubs()
    for order_id, r in data.get("requests", {}).items():
        if r.get("status") not in ("awaiting_command", "awaiting_photo",
                                     "verifying", "manual"):
            continue
        if int(r.get("buyer_id", -1)) == int(buyer_id):
            return order_id, r
    return None


def _club_pending_requests() -> list[dict[str, Any]]:
    """Все НЕ-финальные заявки + те, что требуют ручного решения, отсортированы по времени."""
    data = _load_clubs()
    out = []
    for order_id, r in data.get("requests", {}).items():
        if r.get("status") in ("awaiting_command", "awaiting_photo",
                                  "verifying", "manual"):
            item = dict(r)
            item["order_id"] = order_id
            out.append(item)
    out.sort(key=lambda x: x.get("created_ts", 0))
    return out


def _club_cleanup_expired() -> int:
    """Помечает просроченные заявки как expired."""
    cfg = get_config()
    ttl_h = int(cfg.get("club_request_ttl_hours", 24))
    if ttl_h <= 0:
        return 0
    now = _now()
    cnt = 0
    with _lock:
        data = _load_clubs()
        for order_id, r in data.get("requests", {}).items():
            if r.get("status") not in ("awaiting_command", "awaiting_photo",
                                          "verifying", "manual"):
                continue
            age_h = (now - r.get("created_ts", now)) / 3600
            if age_h >= ttl_h:
                r["status"] = "expired"
                r["decided_ts"] = now
                r["decided_by"] = "auto"
                cnt += 1
        if cnt:
            _save_clubs(data)
    return cnt


def _club_stat_inc(key: str, n: int = 1) -> None:
    with _lock:
        data = _load_clubs()
        s = data.setdefault("stats", {})
        s[key] = int(s.get(key, 0)) + n
        _save_clubs(data)


# ── Ручная фото-проверка для PC-club тарифа (manual review) ─────────────────
# Поведение: после NEW_ORDER на лот с флагом `manual_review` бот просит
# покупателя прислать фото-подтверждение пребывания в PC-клубе. Когда фото
# приходит — пересылаем владельцу в Telegram c кнопками «✅ Одобрить» /
# «❌ Отклонить». Approve → стандартная выдача. Decline → cardinal.account.refund
# (возврат денег) + уведомление покупателю.
def _load_manual_review() -> dict[str, Any]:
    return _load_json(MANUAL_REVIEW_FILE, {
        # {order_id_str: {buyer_id, buyer_username, chat_id, lot_key,
        #                 duration_min, status, photo_url, created_ts,
        #                 decided_ts, decided_by}}
        "requests": {},
    })


def _save_manual_review(data: dict[str, Any]) -> None:
    _save_json(MANUAL_REVIEW_FILE, data)


def _mr_create(order_id: str, buyer_id: int, buyer_username: str,
               chat_id: int | str, lot_key: str,
               duration_min: int) -> dict[str, Any]:
    with _lock:
        data = _load_manual_review()
        reqs = data.setdefault("requests", {})
        entry = {
            "buyer_id": int(buyer_id),
            "buyer_username": str(buyer_username or ""),
            "chat_id": chat_id,
            "lot_key": str(lot_key or ""),
            "duration_min": int(duration_min),
            "status": "awaiting_photo",
            "photo_url": None,
            "created_ts": _now(),
            "decided_ts": 0,
            "decided_by": "",
        }
        reqs[str(order_id)] = entry
        _save_manual_review(data)
        return entry


def _mr_update(order_id: str, **fields: Any) -> dict[str, Any] | None:
    with _lock:
        data = _load_manual_review()
        reqs = data.setdefault("requests", {})
        if str(order_id) not in reqs:
            return None
        reqs[str(order_id)].update(fields)
        _save_manual_review(data)
        return reqs[str(order_id)]


def _mr_get(order_id: str) -> dict[str, Any] | None:
    return _load_manual_review().get("requests", {}).get(str(order_id))


def _mr_find_active_by_buyer(buyer_id: int) -> tuple[str, dict[str, Any]] | None:
    """Активная заявка покупателя в статусе awaiting_photo / pending_review."""
    data = _load_manual_review()
    for order_id, r in data.get("requests", {}).items():
        if r.get("status") not in ("awaiting_photo", "pending_review"):
            continue
        if int(r.get("buyer_id", -1)) == int(buyer_id):
            return order_id, r
    return None


def _mr_pending() -> list[dict[str, Any]]:
    out = []
    for order_id, r in _load_manual_review().get("requests", {}).items():
        if r.get("status") in ("awaiting_photo", "pending_review"):
            item = dict(r)
            item["order_id"] = order_id
            out.append(item)
    out.sort(key=lambda x: x.get("created_ts", 0))
    return out


# ── AI-провайдеры (vision verification) ─────────────────────────────────────
_AI_PROVIDERS = ("openrouter", "openai", "anthropic", "google")

_AI_PROVIDER_LABELS = {
    "openrouter": "OpenRouter",
    "openai": "OpenAI (ChatGPT)",
    "anthropic": "Anthropic (Claude)",
    "google": "Google Gemini",
}

_AI_PROVIDER_PRESETS = {
    "openrouter": [
        "google/gemini-2.0-flash-exp:free",
        "meta-llama/llama-3.2-11b-vision-instruct:free",
        "mistralai/pixtral-12b:free",
        "openai/gpt-4o-mini",
        "anthropic/claude-3-5-haiku",
    ],
    "openai": [
        "gpt-4o-mini",
        "gpt-4o",
    ],
    "anthropic": [
        "claude-3-5-haiku-20241022",
        "claude-3-5-sonnet-20241022",
    ],
    "google": [
        "gemini-1.5-flash",
        "gemini-1.5-flash-8b",
        "gemini-2.0-flash-exp",
    ],
}


def _ai_provider_keys(provider: str) -> tuple[str, str]:
    """Возвращает (config_key_api, config_key_model) для провайдера."""
    mapping = {
        "openrouter": ("openrouter_api_key", "openrouter_model"),
        "openai": ("openai_api_key", "openai_model"),
        "anthropic": ("anthropic_api_key", "anthropic_model"),
        "google": ("google_ai_api_key", "google_ai_model"),
    }
    return mapping.get(provider, mapping["openrouter"])


def _ai_get_active() -> tuple[str, str, str]:
    """Возвращает (provider, api_key, model) активного AI."""
    cfg = get_config()
    provider = cfg.get("ai_provider") or "openrouter"
    if provider not in _AI_PROVIDERS:
        provider = "openrouter"
    k_api, k_model = _ai_provider_keys(provider)
    return provider, cfg.get(k_api, "") or "", cfg.get(k_model, "") or ""


def _ai_validate_key(provider: str, api_key: str) -> tuple[bool, str]:
    """Делает мин. тестовый запрос. Возвращает (ok, error_msg)."""
    if not api_key.strip():
        return False, "Пустой ключ"
    try:
        if provider == "openrouter":
            r = requests.get(
                "https://openrouter.ai/api/v1/auth/key",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=15)
            if r.status_code == 200:
                return True, ""
            return False, f"HTTP {r.status_code}: {r.text[:200]}"
        if provider == "openai":
            r = requests.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=15)
            if r.status_code == 200:
                return True, ""
            return False, f"HTTP {r.status_code}: {r.text[:200]}"
        if provider == "anthropic":
            # У Anthropic нет дешёвого endpoint для проверки ключа,
            # делаем минимальный messages-запрос.
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"},
                json={"model": "claude-3-5-haiku-20241022",
                      "max_tokens": 1,
                      "messages": [{"role": "user", "content": "hi"}]},
                timeout=20)
            if r.status_code in (200, 400):
                # 400 = invalid_request (но ключ валиден)
                txt = r.text.lower()
                if "authentication_error" in txt or "invalid_api_key" in txt:
                    return False, f"HTTP {r.status_code}: {r.text[:200]}"
                return True, ""
            return False, f"HTTP {r.status_code}: {r.text[:200]}"
        if provider == "google":
            r = requests.get(
                f"https://generativelanguage.googleapis.com/v1beta/models"
                f"?key={api_key}",
                timeout=15)
            if r.status_code == 200:
                return True, ""
            return False, f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as exc:
        return False, f"Сетевая ошибка: {exc}"
    return False, "Неизвестный провайдер"


def _download_image_as_b64(url: str, max_bytes: int = 8 * 1024 * 1024) -> tuple[bytes, str]:
    """Скачивает картинку и возвращает (bytes, mime). Ограничение по размеру."""
    r = requests.get(url, timeout=30, stream=True)
    r.raise_for_status()
    mime = r.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
    if not mime.startswith("image/"):
        mime = "image/jpeg"
    data = b""
    for chunk in r.iter_content(chunk_size=16384):
        data += chunk
        if len(data) > max_bytes:
            raise ValueError(f"image too large (> {max_bytes // 1024 // 1024} MB)")
    return data, mime


def _ai_verify_club_photo(image_url: str, code: str, seller_nick: str) -> dict[str, Any]:
    """Запрашивает у активного AI-провайдера верификацию фото PC-клуба.

    Возвращает dict:
      {
        "ok": bool,
        "is_pc_club": bool,
        "funpay_chat_visible": bool,
        "seller_nickname_visible": bool,
        "code_visible": bool,
        "confidence": int (0..100),
        "reasoning": str,
        "error": str,
        "provider": str,
        "model": str,
        "raw": str,
      }
    """
    provider, api_key, model = _ai_get_active()
    result: dict[str, Any] = {
        "ok": False, "is_pc_club": False, "funpay_chat_visible": False,
        "seller_nickname_visible": False, "code_visible": False,
        "confidence": 0, "reasoning": "", "error": "",
        "provider": provider, "model": model, "raw": "",
    }
    if not api_key:
        result["error"] = f"Не настроен ключ для {_AI_PROVIDER_LABELS.get(provider, provider)}"
        return result
    if not model:
        result["error"] = f"Не выбрана модель для {_AI_PROVIDER_LABELS.get(provider, provider)}"
        return result

    try:
        img_bytes, mime = _download_image_as_b64(image_url)
    except Exception as exc:
        result["error"] = f"download: {exc}"
        return result
    b64 = base64.b64encode(img_bytes).decode("ascii")

    # ── 1. Сначала прогоняем fake-детектор (если включён). ──────────────
    # Скамеры генерят «фото из ПК-клуба» в Midjourney/SD с нужным кодом и
    # подсовывают боту. Без отдельной проверки обычная verify их пропустит,
    # потому что формально на картинке есть и клуб, и чат, и код. Fake-
    # детектор смотрит на артефакты генерации и режет такие фото в корне.
    cfg = get_config()
    fake_verdict: dict[str, Any] = {}
    if cfg.get("ai_fake_detector_enabled", True):
        try:
            fake_verdict = _ai_detect_fake_from_b64(b64, mime)
        except Exception as exc:
            LOGGER.warning(
                "steam_rental: fake-detector crashed: %s", exc, exc_info=True)
            fake_verdict = {"ok": False, "error": f"crash: {exc}"}
        result["fake"] = fake_verdict
        decision = _fake_verdict_classify(fake_verdict)
        result["fake_decision"] = decision
        if decision == "decline":
            # Фото — почти точно AI-генерация. Выкидываем заявку, не
            # делая второй платный AI-вызов.
            result.update({
                "ok": True,
                "is_pc_club": False,
                "funpay_chat_visible": False,
                "seller_nickname_visible": False,
                "code_visible": False,
                "confidence": 0,
                "reasoning": (
                    "Фото похоже на AI-генерацию "
                    f"({fake_verdict.get('ai_generated_score', 0)}%). "
                    + (fake_verdict.get("reasoning") or "")[:300]
                ),
            })
            _club_stat_inc("ai_calls")
            return result

    prompt = (
        "Ты — модератор сервиса аренды Steam-аккаунтов. Тебе пришла фотография "
        "от покупателя, который претендует на льготный тариф для PC-клубов.\n\n"
        "На фотографии должны быть одновременно видны:\n"
        f"1) интерьер PC-клуба (несколько ПК подряд / вывеска клуба / "
        f"характерная компьютерная зала),\n"
        f"2) открытый чат FunPay с продавцом (никнейм продавца: "
        f"\"{seller_nick}\"),\n"
        f"3) код верификации \"{code}\" (написан на бумажке, на экране, "
        f"или в последнем сообщении покупателя в этом чате).\n\n"
        "Проанализируй фото и ответь СТРОГО в формате JSON, без обрамления:\n"
        "{\n"
        '  "is_pc_club": true/false,\n'
        '  "funpay_chat_visible": true/false,\n'
        '  "seller_nickname_visible": true/false,\n'
        '  "code_visible": true/false,\n'
        '  "confidence": 0..100,\n'
        '  "reasoning": "краткое объяснение что видно на фото"\n'
        "}\n\n"
        "confidence — твоя суммарная уверенность, что покупатель находится в "
        "реальном PC-клубе ИМЕННО СЕЙЧАС и фото свежее (а не reuse из "
        "интернета). Если код невидим — confidence не выше 30. Если все 4 "
        "признака true — confidence 80..100."
    )

    try:
        text = _ai_chat_with_image(provider, api_key, model, prompt, b64, mime)
    except Exception as exc:
        result["error"] = f"AI call: {exc}"
        return result

    result["raw"] = text[:2000]
    parsed = _ai_parse_json_verdict(text)
    if not parsed:
        result["error"] = "AI ответ не распознан как JSON"
        return result

    result.update({
        "ok": True,
        "is_pc_club": bool(parsed.get("is_pc_club")),
        "funpay_chat_visible": bool(parsed.get("funpay_chat_visible")),
        "seller_nickname_visible": bool(parsed.get("seller_nickname_visible")),
        "code_visible": bool(parsed.get("code_visible")),
        "confidence": int(max(0, min(100, parsed.get("confidence", 0)))),
        "reasoning": str(parsed.get("reasoning", ""))[:500],
    })
    _club_stat_inc("ai_calls")
    return result


def _ai_parse_json_verdict(text: str) -> dict[str, Any] | None:
    """Достаёт JSON из ответа AI (может содержать ```json или просто текст)."""
    if not text:
        return None
    # Попытка прямого парса
    try:
        return json.loads(text)
    except Exception:
        pass
    # Попытка найти JSON-блок
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return None


def _ai_chat_with_image(provider: str, api_key: str, model: str,
                       prompt: str, image_b64: str, mime: str) -> str:
    """Универсальный вызов AI с фото. Возвращает текст ответа."""
    if provider in ("openrouter", "openai"):
        if provider == "openrouter":
            url = "https://openrouter.ai/api/v1/chat/completions"
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://funpay.com",
                "X-Title": "FunPay Steam Rental",
            }
        else:
            url = "https://api.openai.com/v1/chat/completions"
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
        payload = {
            "model": model,
            "max_tokens": 600,
            "temperature": 0.0,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url",
                     "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
                ],
            }],
        }
        r = requests.post(url, headers=headers, json=payload, timeout=90)
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"]

    if provider == "anthropic":
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": model,
            "max_tokens": 600,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image",
                     "source": {"type": "base64", "media_type": mime,
                                "data": image_b64}},
                    {"type": "text", "text": prompt},
                ],
            }],
        }
        r = requests.post(url, headers=headers, json=payload, timeout=90)
        r.raise_for_status()
        data = r.json()
        return data["content"][0]["text"]

    if provider == "google":
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{model}:generateContent?key={api_key}")
        payload = {
            "contents": [{
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": mime, "data": image_b64}},
                ]
            }],
            "generationConfig": {"maxOutputTokens": 600, "temperature": 0.0},
        }
        r = requests.post(url, json=payload, timeout=90)
        r.raise_for_status()
        data = r.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]

    raise ValueError(f"unknown AI provider: {provider}")


# ── AI-fake / deepfake detector ─────────────────────────────────────────────
# Отдельный AI-вызов с узким промптом: оцениваем признаки того, что фото
# было сгенерировано AI (Midjourney / Stable Diffusion / DALL-E / Sora) или
# собрано в фоторедакторе. Скамеры всё чаще генерят «фото из ПК-клуба» с
# подсунутым кодом верификации, обычная verify-функция их пропускает (потому
# что формально на картинке всё есть). Этот детектор смотрит именно на
# артефакты генерации: нерегулярные пальцы/буквы, plastic-skin, нелогичную
# геометрию монитора/кабелей, муаровые отражения, EXIF-странности и т.д.

_FAKE_DETECTOR_PROMPT = (
    "Ты — судебный фото-эксперт. Твоя задача: оценить, является ли "
    "присланное фото реальной фотографией с камеры телефона/фотоаппарата, "
    "ИЛИ оно было сгенерировано/обработано искусственным интеллектом "
    "(Midjourney, Stable Diffusion, DALL-E, Sora, Flux, Nano Banana) либо "
    "грубо собрано в фоторедакторе.\n\n"
    "Обращай внимание на типичные артефакты генерации:\n"
    "• руки/пальцы неправильной формы или количества;\n"
    "• текст, цифры и логотипы на фоне «плывут», нечитаемы или абсурдны;\n"
    "• кабели/провода соединяются в никуда, мониторы без задней панели;\n"
    "• симметрия глаз/ушей/зубов нарушена, plastic-skin, переразмытый фон;\n"
    "• нелогичные тени, отсутствие теней под предметами;\n"
    "• «вшитый» в картинку текст с подозрительно идеальным шрифтом;\n"
    "• признаки коллажа: разные источники света, обрезанные края, "
    "  не совпадающие пропорции;\n"
    "• чрезмерно «киношная» эстетика, фон слишком чистый и постановочный.\n\n"
    "Учитывай: реальные фото из ПК-клубов обычно содержат шум камеры, "
    "случайный мусор, неровные кабели, неидеальные стулья. Если фото "
    "выглядит «слишком красиво» или «слишком чисто» — это подозрительно.\n\n"
    "Ответь СТРОГО в формате JSON, без обрамления:\n"
    "{\n"
    '  "ai_generated_score": 0..100,\n'
    '  "is_likely_ai": true/false,\n'
    '  "is_likely_edited": true/false,\n'
    '  "artifacts": ["короткий список замеченных признаков"],\n'
    '  "reasoning": "1-2 предложения объяснения"\n'
    "}\n\n"
    "ai_generated_score — суммарная вероятность (в %), что фото "
    "сгенерировано/смонтировано ИИ. 0 = точно реальное фото с камеры, "
    "100 = точно сгенерировано. Не стесняйся ставить высокие значения, "
    "если видишь явные артефакты."
)


def _ai_detect_fake_from_b64(image_b64: str, mime: str) -> dict[str, Any]:
    """Запускает fake-детектор на уже загруженной картинке.

    Возвращает dict:
      {
        "ok": bool,
        "ai_generated_score": int (0..100),
        "is_likely_ai": bool,
        "is_likely_edited": bool,
        "artifacts": list[str],
        "reasoning": str,
        "error": str,
        "provider": str,
        "model": str,
        "raw": str,
      }
    """
    provider, api_key, model = _ai_get_active()
    result: dict[str, Any] = {
        "ok": False,
        "ai_generated_score": 0,
        "is_likely_ai": False,
        "is_likely_edited": False,
        "artifacts": [],
        "reasoning": "",
        "error": "",
        "provider": provider,
        "model": model,
        "raw": "",
    }
    if not api_key:
        result["error"] = (
            f"Не настроен ключ для "
            f"{_AI_PROVIDER_LABELS.get(provider, provider)}")
        return result
    if not model:
        result["error"] = (
            f"Не выбрана модель для "
            f"{_AI_PROVIDER_LABELS.get(provider, provider)}")
        return result

    try:
        text = _ai_chat_with_image(
            provider, api_key, model, _FAKE_DETECTOR_PROMPT, image_b64, mime)
    except Exception as exc:
        result["error"] = f"AI call: {exc}"
        return result

    result["raw"] = text[:2000]
    parsed = _ai_parse_json_verdict(text)
    if not parsed:
        result["error"] = "AI ответ fake-детектора не распознан как JSON"
        return result

    artifacts = parsed.get("artifacts") or []
    if not isinstance(artifacts, list):
        artifacts = [str(artifacts)]
    artifacts = [str(a)[:120] for a in artifacts][:10]

    result.update({
        "ok": True,
        "ai_generated_score": int(max(0, min(100,
            int(parsed.get("ai_generated_score", 0))))),
        "is_likely_ai": bool(parsed.get("is_likely_ai")),
        "is_likely_edited": bool(parsed.get("is_likely_edited")),
        "artifacts": artifacts,
        "reasoning": str(parsed.get("reasoning", ""))[:500],
    })
    return result


def _ai_detect_fake_image(image_url: str) -> dict[str, Any]:
    """Скачивает картинку по URL и запускает fake-детектор."""
    provider, api_key, model = _ai_get_active()
    result: dict[str, Any] = {
        "ok": False, "ai_generated_score": 0,
        "is_likely_ai": False, "is_likely_edited": False,
        "artifacts": [], "reasoning": "", "error": "",
        "provider": provider, "model": model, "raw": "",
    }
    try:
        img_bytes, mime = _download_image_as_b64(image_url)
    except Exception as exc:
        result["error"] = f"download: {exc}"
        return result
    b64 = base64.b64encode(img_bytes).decode("ascii")
    return _ai_detect_fake_from_b64(b64, mime)


def _fake_verdict_classify(verdict: dict[str, Any]) -> str:
    """Возвращает 'decline' / 'manual' / 'pass' по конфигу.

    pass    — фото выглядит реальным (или детектор отключён / упал);
    manual  — серая зона, нужна ручная проверка админом;
    decline — фото с высокой вероятностью AI-генерация, авто-отказ.
    """
    cfg = get_config()
    if not cfg.get("ai_fake_detector_enabled", True):
        return "pass"
    if not verdict or not verdict.get("ok"):
        # Не валим заявку из-за падения детектора — в crast возвращаем pass.
        return "pass"
    score = int(verdict.get("ai_generated_score", 0))
    decl = int(cfg.get("ai_fake_decline_threshold", 70))
    manu = int(cfg.get("ai_fake_manual_threshold", 40))
    if score >= decl:
        return "decline"
    if score >= manu:
        return "manual"
    return "pass"


# ── VAC / Trade ban scan ─────────────────────────────────────────────────────
_last_vac_scan_ts: int = 0


def _steam_get_player_bans(steamids: list[str], api_key: str) -> dict[str, dict[str, Any]]:
    """Дёргает ISteamUser/GetPlayerBans v1. Возвращает {steamid: ban_info}."""
    if not steamids or not api_key:
        return {}
    out: dict[str, dict[str, Any]] = {}
    # Steam API ограничивает ~100 SteamID на запрос.
    for i in range(0, len(steamids), 100):
        chunk = steamids[i:i + 100]
        try:
            r = requests.get(
                "https://api.steampowered.com/ISteamUser/GetPlayerBans/v1/",
                params={"key": api_key, "steamids": ",".join(chunk)},
                timeout=30)
            r.raise_for_status()
            data = r.json()
            for p in data.get("players", []):
                sid = str(p.get("SteamId"))
                out[sid] = p
        except Exception:
            LOGGER.warning("steam_rental: GetPlayerBans chunk failed",
                           exc_info=True)
    return out


def _has_any_ban(info: dict[str, Any]) -> tuple[bool, str]:
    """Возвращает (banned, описание_причины)."""
    if not info:
        return False, ""
    reasons = []
    if info.get("VACBanned"):
        reasons.append(f"VAC ban (×{info.get('NumberOfVACBans', 1)})")
    if int(info.get("NumberOfGameBans", 0) or 0) > 0:
        reasons.append(f"Game ban (×{info.get('NumberOfGameBans')})")
    econ = info.get("EconomyBan") or "none"
    if econ and econ != "none":
        reasons.append(f"Trade/Economy ban: {econ}")
    if info.get("CommunityBanned"):
        reasons.append("Community ban")
    if reasons:
        return True, "; ".join(reasons)
    return False, ""


def _vac_scan_iter(cardinal: "Cardinal") -> dict[str, Any]:
    """Один прогон VAC-скана. Возвращает summary dict."""
    global _last_vac_scan_ts
    cfg = get_config()
    api_key = (cfg.get("steam_api_key") or "").strip()
    summary = {"checked": 0, "banned": 0, "ended": [], "errors": []}
    if not cfg.get("vac_scan_enabled"):
        summary["errors"].append("disabled in config")
        return summary
    if not api_key:
        summary["errors"].append("no steam_api_key")
        return summary

    # Собираем SteamID активных аренд (по которым ещё не было решения).
    targets: list[tuple[str, str]] = []  # [(steamid, alias)]
    for acc in list_accounts():
        if not acc.get("rental"):
            continue
        sid = acc.get("steamid")
        if sid:
            targets.append((str(sid), acc["alias"]))
    if not targets:
        _last_vac_scan_ts = _now()
        return summary

    steamids = [t[0] for t in targets]
    bans = _steam_get_player_bans(steamids, api_key)
    summary["checked"] = len(steamids)

    for sid, alias in targets:
        info = bans.get(sid)
        banned, reason = _has_any_ban(info)
        if not banned:
            continue
        summary["banned"] += 1
        try:
            with _lock:
                acc = find_account(alias)
                if acc:
                    acc["last_vac_check"] = _now()
                    acc["last_vac_reason"] = reason
                    upsert_account(acc)
            res = end_rental(cardinal, alias, reason="vac_ban")
            _log_action("acc_vac_ban",
                        f"VAC/ban на {alias}: {reason}",
                        alias=alias, reason=reason,
                        revoked=res.get("revoked"),
                        changed=res.get("changed"))
            with _lock:
                acc = find_account(alias)
                if acc and not acc.get("frozen"):
                    acc["frozen"] = True
                    acc["freeze_reason"] = f"VAC/ban: {reason}"
                    acc["freeze_ts"] = _now()
                    upsert_account(acc)
                    _log_action("acc_freeze",
                                f"Заморозка {alias} из-за VAC/ban",
                                alias=alias, reason=f"VAC: {reason}",
                                mode="auto")
            try:
                _notify_tg(cardinal,
                           f"🚨 <b>Steam Rental — VAC SCAN</b>\n"
                           f"Аккаунт <code>{alias}</code> получил бан: "
                           f"<b>{_esc(reason)}</b>\n"
                           f"Аренда авто-закрыта, акк заморожен.\n"
                           f"end_rental: changed={res.get('changed')}, "
                           f"revoked={res.get('revoked')}.")
                _update_lot_activation(cardinal)
            except Exception:
                LOGGER.debug("steam_rental: VAC scan tg-alert failed",
                             exc_info=True)
            summary["ended"].append(f"{alias} ({reason})")
        except Exception as exc:
            summary["errors"].append(f"{alias}: {exc}")
            LOGGER.error("steam_rental: VAC scan end_rental crash for %s",
                         alias, exc_info=True)

    _last_vac_scan_ts = _now()
    return summary


def _get_unclosed_rentals() -> list[dict[str, Any]]:
    """Находит аренды, которые истекли, но заказ не подтверждён."""
    accs = list_accounts()
    result = []
    now = _now()
    for a in accs:
        r = a.get("rental")
        if not r:
            continue
        expires = r.get("expires_at", 0)
        if expires and expires < now:
            result.append({
                "alias": a["alias"],
                "buyer_username": r.get("buyer_username", "?"),
                "order_id": r.get("order_id", ""),
                "expired_at": _fmt_ts(expires),
                "overdue_min": (now - expires) // 60,
            })
    return result


# Парсинг длительности из названия лота (запасной вариант).
# Supported formats: '2ч', '2 ч', '30мин', '1h', '175m', '3d',
# '1 hour', '2 day', '1 week', etc.
_DURATION_PATTERNS = [
    (re.compile(r"(\d+)\s*мин", re.IGNORECASE), 1),
    (re.compile(r"(\d+)\s*ч(?:ас|\.)?", re.IGNORECASE), 60),
    (re.compile(r"(\d+)\s*д(?:ен|н)", re.IGNORECASE), 60 * 24),
    (re.compile(r"(\d+)\s*нед", re.IGNORECASE), 60 * 24 * 7),
    (re.compile(r"(\d+)\s*мес", re.IGNORECASE), 60 * 24 * 30),
    # Short Latin single-letter suffixes (standalone).
    (re.compile(r"(\d+)\s*h\b", re.IGNORECASE), 60),
    (re.compile(r"(\d+)\s*m\b", re.IGNORECASE), 1),
    (re.compile(r"(\d+)\s*d\b", re.IGNORECASE), 60 * 24),
    (re.compile(r"(\d+)\s*w\b", re.IGNORECASE), 60 * 24 * 7),
    # Long English words.
    (re.compile(r"(\d+)\s*hour", re.IGNORECASE), 60),
    (re.compile(r"(\d+)\s*day", re.IGNORECASE), 60 * 24),
    (re.compile(r"(\d+)\s*week", re.IGNORECASE), 60 * 24 * 7),
]


def _ensure_storage() -> None:
    os.makedirs(STORAGE_DIR, exist_ok=True)
    os.makedirs(SCREENSHOTS_DIR, exist_ok=True)


def _load_json(path: str, default: Any) -> Any:
    _ensure_storage()
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        LOGGER.warning("steam_rental: не удалось прочитать %s", path, exc_info=True)
        return default


def _save_json(path: str, data: Any) -> None:
    _ensure_storage()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _now() -> int:
    return int(time.time())


# Все пользовательские отображения времени — в Москве (UTC+3, без перехода
# на летнее время). FunPay аудитория русскоязычная, времена UTC сбивали
# покупателей и оператора.
_MSK_TZ = datetime.timezone(datetime.timedelta(hours=3), "МСК")


def _fmt_ts(ts: int) -> str:
    return datetime.datetime.fromtimestamp(int(ts), tz=_MSK_TZ).strftime(
        "%Y-%m-%d %H:%M:%S")


def _gen_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        pw = "".join(pysecrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in pw)
                and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw)):
            return pw


def _parse_duration_minutes(text: str) -> int | None:
    if not text:
        return None
    for pattern, mul in _DURATION_PATTERNS:
        m = pattern.search(text)
        if m:
            try:
                return int(m.group(1)) * mul
            except ValueError:
                continue
    return None


def _hashtag_suffix_to_multiplier(suffix: str) -> int | None:
    """Return multiplier (to minutes) for a duration suffix, or None if unrecognized."""
    s = suffix.lower().strip()
    if not s:
        return 1  # no suffix means minutes
    if s in ("h", "\u0447", "\u0447\u0430\u0441", "\u0447\u0430\u0441\u0430", "\u0447\u0430\u0441\u043e\u0432"):
        return 60
    if s in ("d", "\u0434", "\u0434\u043d", "\u0434\u0435\u043d\u044c", "\u0434\u043d\u044f", "\u0434\u043d\u0435\u0439"):
        return 1440
    if s in ("m", "\u043c\u0438\u043d", "min", "\u043c\u0438\u043d\u0443\u0442"):
        return 1
    if s in ("w", "\u043d\u0435\u0434", "\u043d\u0435\u0434\u0435\u043b\u044f", "\u043d\u0435\u0434\u0435\u043b\u044c"):
        return 10080
    return None  # unrecognized suffix


def _parse_hashtag_time(text: str) -> int | None:
    """Parse #Time: <number><suffix> from text. Returns minutes or None."""
    if not text:
        return None
    # Regex handles: '#Time: 2 ч', '#Time: 2ч', '#Time:2h', '#Time: 175m'
    # The \s* between groups allows both space and no-space before the suffix.
    m = re.search(r'#Time:\s*(\d+)\s*(\S*)', text, re.IGNORECASE)
    if not m:
        return None
    value = int(m.group(1))
    suffix = m.group(2)
    mult = _hashtag_suffix_to_multiplier(suffix)
    if mult is None:
        return None
    return value * mult


def _parse_hashtag_hours(text: str) -> int | None:
    """Parse #Hours: <number> from text. Returns minutes (hours*60) or None.

    Suffix is ignored — value is always treated as hours.
    """
    if not text:
        return None
    m = re.search(r'#Hours:\s*(\d+)', text, re.IGNORECASE)
    if not m:
        return None
    try:
        hours = int(m.group(1))
    except (TypeError, ValueError):
        return None
    if hours <= 0:
        return None
    return hours * 60


def _parse_hashtag_review(text: str) -> int | None:
    """Parse #Review: <number><suffix> from text. Returns minutes or None."""
    if not text:
        return None
    # Regex handles: '#Review: 1h', '#Review: 60', '#Review:2 д', '#Review: 30m'
    # The \s* between groups allows both space and no-space before the suffix.
    m = re.search(r'#Review:\s*(\d+)\s*(\S*)', text, re.IGNORECASE)
    if not m:
        return None
    value = int(m.group(1))
    suffix = m.group(2)
    mult = _hashtag_suffix_to_multiplier(suffix)
    if mult is None:
        return None
    return value * mult


# ── Очередь ожидания (waiting queue) ─────────────────────────────────────────
_queue_lock = threading.Lock()


def _load_queue() -> dict:
    """Load queue data: {lot_key: [{buyer_id, buyer_username, chat_id, queued_at, notified}]}"""
    return _load_json(QUEUE_FILE, {})


def _save_queue(data: dict) -> None:
    _save_json(QUEUE_FILE, data)


def _add_to_queue(lot_key: str, buyer_id: int, buyer_username: str, chat_id: Any) -> dict:
    """Add buyer to queue. Returns {"ok": True, "position": N} or {"ok": False, "reason": "..."}"""
    cfg = get_config()
    max_per_lot = int(cfg.get("queue_max_per_lot", 10))
    with _queue_lock:
        queue = _load_queue()
        lot_queue = queue.get(lot_key, [])

        # Check if already in queue
        for entry in lot_queue:
            if int(entry.get("buyer_id", 0)) == int(buyer_id):
                position = lot_queue.index(entry) + 1
                return {"ok": False, "reason": "already", "position": position}

        if len(lot_queue) >= max_per_lot:
            return {"ok": False, "reason": "full"}

        lot_queue.append({
            "buyer_id": int(buyer_id),
            "buyer_username": str(buyer_username),
            "chat_id": chat_id,
            "queued_at": _now(),
            "notified": False,
        })
        queue[lot_key] = lot_queue
        _save_queue(queue)
        return {"ok": True, "position": len(lot_queue)}


def _remove_from_queue(lot_key: str, buyer_id: int) -> None:
    with _queue_lock:
        queue = _load_queue()
        lot_queue = queue.get(lot_key, [])
        queue[lot_key] = [e for e in lot_queue if int(e.get("buyer_id", 0)) != int(buyer_id)]
        if not queue[lot_key]:
            del queue[lot_key]
        _save_queue(queue)


def _cleanup_expired_queue() -> None:
    """Remove queue entries older than TTL."""
    cfg = get_config()
    ttl_hours = int(cfg.get("queue_ttl_hours", 24))
    cutoff = _now() - ttl_hours * 3600
    with _queue_lock:
        queue = _load_queue()
        changed = False
        for lot_key in list(queue.keys()):
            before = len(queue[lot_key])
            queue[lot_key] = [e for e in queue[lot_key] if e.get("queued_at", 0) > cutoff]
            if len(queue[lot_key]) != before:
                changed = True
            if not queue[lot_key]:
                del queue[lot_key]
                changed = True
        if changed:
            _save_queue(queue)


def _notify_next_in_queue(cardinal: "Cardinal", lot_key: str) -> None:
    """Notify the first non-notified buyer in queue that an account is free."""
    cfg = get_config()
    if not cfg.get("queue_enabled", True):
        return
    with _queue_lock:
        queue = _load_queue()
        lot_queue = queue.get(lot_key, [])
        if not lot_queue:
            return

        # Find first non-notified entry
        for entry in lot_queue:
            if not entry.get("notified"):
                game = ""
                # Try to get game from lot
                lots = _load_json(LOTS_FILE, {})
                lot_data = lots.get(lot_key, {})
                game = lot_data.get("game", "")

                notify_template = cfg.get("queue_notify_template",
                    "Аккаунт освободился! Оплатите лот в течение 15 минут.")
                text = _render_template("queue_notified",
                    buyer_id=entry.get("buyer_id"),
                    notify_template=notify_template, game=game or "")
                try:
                    cardinal.send_message(
                        entry["chat_id"], text,
                        chat_name=entry.get("buyer_username"),
                        interlocutor_id=entry.get("buyer_id"),
                        watermark=False)
                except Exception:
                    LOGGER.debug("steam_rental: queue notify failed", exc_info=True)

                entry["notified"] = True
                _save_queue(queue)
                break


# ── Конфиг ───────────────────────────────────────────────────────────────────
def get_config() -> dict[str, Any]:
    cfg = _load_json(CONFIG_FILE, dict(_DEFAULT_CONFIG))
    updated = False
    # Миграция v2.10 -> v2.11: время отображается в МСК. Старый ключ
    # daily_summary_hour_utc переводим в daily_summary_hour_msk
    # (UTC + 3 mod 24). Сам старый ключ оставляем в файле, чтобы
    # не делать destructive-миграцию; он просто перестаёт читаться.
    if ("daily_summary_hour_msk" not in cfg
            and "daily_summary_hour_utc" in cfg):
        try:
            cfg["daily_summary_hour_msk"] = (
                int(cfg["daily_summary_hour_utc"]) + 3) % 24
        except (TypeError, ValueError):
            cfg["daily_summary_hour_msk"] = 0
        updated = True
    # v2.19: одноразово включаем extension_buyer_fallback_enabled. Раньше
    # дефолт был False, что вело к молчаливым провалам extension-покупок,
    # когда FunPay отдаёт заказ без lot_id. Защиты от ложных срабатываний
    # уже встроены (matching по игре активной аренды). Метка
    # _ext_fallback_enabled_v2_19 не повторяется.
    if not cfg.get("_ext_fallback_enabled_v2_19"):
        cfg["extension_buyer_fallback_enabled"] = True
        cfg["_ext_fallback_enabled_v2_19"] = True
        updated = True
    # v2.22: одноразовая миграция cfg["templates"] → templates_ru.json
    # + создание templates_*.json из встроенных дефолтов.
    try:
        _ensure_templates_files()
    except Exception:
        LOGGER.debug("steam_rental: ensure templates files failed",
                     exc_info=True)
    if _migrate_legacy_templates_into_files(cfg):
        updated = True
    # v2.22.1: сразу за legacy-миграцией — апгрейд устаревших дефолтов
    # в templates_ru.json (например, добавили строку «If you prefer
    # English» к шаблону issue в v2.22.1). Не трогает кастомизированные
    # значения. Идемпотентно.
    try:
        _migrate_outdated_template_defaults()
    except Exception:
        LOGGER.debug(
            "steam_rental: migrate_outdated_template_defaults failed",
            exc_info=True)
    for k, v in _DEFAULT_CONFIG.items():
        if k not in cfg:
            cfg[k] = v
            updated = True
    if "templates" not in cfg or not isinstance(cfg.get("templates"), dict):
        cfg["templates"] = {} if cfg.get("_templates_externalized_v2_22") \
            else dict(_DEFAULT_TEMPLATES)
        updated = True
    else:
        # После миграции v2.22 cfg["templates"] намеренно пуст
        # (источник правды — templates_*.json). Не пере-заполняем его
        # дефолтами, иначе бакфилл «воскресит» legacy-override и
        # сломает приоритет файла над cfg.
        if not cfg.get("_templates_externalized_v2_22"):
            for tk, tv in _DEFAULT_TEMPLATES.items():
                if tk not in cfg["templates"]:
                    cfg["templates"][tk] = tv
                    updated = True
    # Миграция v2.12.0 -> v2.12.1: убрать Rockstar/Social-Club-текст
    # «по умолчанию» из шаблона post_delivery. Если у пользователя в
    # cfg["templates"]["post_delivery"] до сих пор записан старый
    # встроенный дефолт (он был стандартом до 2.12.1) — заменяем на ""
    # и одновременно гасим глобальный флаг, чтобы это сообщение
    # перестало улетать автоматически. Кастомные тексты, заданные
    # продавцом, не трогаем.
    _OLD_POST_DELIVERY_DEFAULT = (
        "📧 ДОПОЛНИТЕЛЬНАЯ ИНФОРМАЦИЯ\n\n"
        "🎮 Для получения кода Rockstar Social Club:\n"
        "Напишите команду: !код {login}\n\n"
        "Если нужен код с почты — обратитесь к продавцу.\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "💡 Приятной игры!"
    )
    try:
        cur_pd = cfg.get("templates", {}).get("post_delivery")
    except AttributeError:
        cur_pd = None
    if cur_pd == _OLD_POST_DELIVERY_DEFAULT:
        cfg["templates"]["post_delivery"] = ""
        # Опционально гасим глобальный флаг: если он был включён,
        # значит покупатели получали именно этот Rockstar-текст —
        # его больше нет, и продолжать слать пустоту смысла нет.
        if cfg.get("post_delivery_message_enabled"):
            cfg["post_delivery_message_enabled"] = False
        updated = True
    # Миграция v2.12.x -> v2.13.0: revoke_sessions_on_expire был ВСЕГДА
    # True по дефолту, но фактический revoke в end_rental был хардкодом
    # выключен. Юзер не мог осознанно «включить» эту опцию — её просто
    # никогда не было видно как опции. Теперь она реально работает и
    # дефолт = False. Если в файле до сих пор лежит легаси-True —
    # принудительно выключаем, чтобы при апгрейде у людей внезапно
    # не начало срабатывать revoke сессий. Кто хочет — включит ручкой
    # в ⚙ Настройки → 🔒 Безопасность.
    if cfg.get("revoke_sessions_on_expire") is True \
            and "_revoke_sessions_migrated_v2_13" not in cfg:
        cfg["revoke_sessions_on_expire"] = False
        cfg["_revoke_sessions_migrated_v2_13"] = True
        updated = True
    # Миграция v2.13.x -> v2.14.0: расширяем шаблоны no_accounts /
    # no_accounts_rp полями {next_free_in} и {next_free}, чтобы
    # покупатель видел, через сколько освободится ближайший аккаунт
    # (раньше при гонке двух одновременных оплат на пул из одного
    # аккаунта второй покупатель вообще не получал ни этого текста,
    # ни оценки времени ожидания). Заменяем шаблон ТОЛЬКО если в
    # config.json лежит старый дефолт; кастомные тексты не трогаем —
    # их можно вручную дописать новыми плейсхолдерами через ПУ.
    _OLD_NO_ACCOUNTS_DEFAULT = (
        "✖ К сожалению, все аккаунты заняты\n\n"
        "🎮 Игра: {game}\n\n"
        "💡 Хотите встать в очередь?\n"
        "Напишите !очередь и мы уведомим вас, когда аккаунт освободится"
    )
    _OLD_NO_ACCOUNTS_RP_DEFAULT = (
        "✖ Все Remote Play аккаунты заняты.\n\n"
        "🎮 Игра: {game}\n\n"
        "📧 Попробуйте позже или напишите продавцу."
    )
    if "_no_accounts_next_free_migrated_v2_14" not in cfg:
        try:
            tpls = cfg.get("templates") or {}
            if tpls.get("no_accounts") == _OLD_NO_ACCOUNTS_DEFAULT:
                tpls["no_accounts"] = _DEFAULT_TEMPLATES["no_accounts"]
                updated = True
            if tpls.get("no_accounts_rp") == _OLD_NO_ACCOUNTS_RP_DEFAULT:
                tpls["no_accounts_rp"] = _DEFAULT_TEMPLATES["no_accounts_rp"]
                updated = True
        except Exception:
            pass
        cfg["_no_accounts_next_free_migrated_v2_14"] = True
        updated = True
    if updated:
        _save_json(CONFIG_FILE, cfg)
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    _save_json(CONFIG_FILE, cfg)


def _render_template(name: str, *, buyer_id: Any = None,
                     lang: str | None = None, **kwargs: Any) -> str:
    """Рендерит шаблон по имени с плейсхолдерами {key}.

    Источник правды (в порядке убывания приоритета):
      1) cfg["templates_<lang>"][name] — admin-override через TG (legacy).
      2) Файл templates_ru.json / templates_en.json — то, что показывает
         и пишет TG-меню «📝 Шаблоны» (с переключателем 🇷🇺/🇬🇧).
      3) cfg["templates"][name] — legacy-override до v2.22 (только RU).
      4) _DEFAULT_TEMPLATES / _DEFAULT_TEMPLATES_EN — встроенные дефолты.
      5) RU как последний fallback, если EN-ключ отсутствует.

    Язык:
      - Если задан явно `lang` — берём его.
      - Иначе если задан `buyer_id` — читаем из buyer_lang.json
        (по умолчанию cfg.default_language).
      - Иначе RU.
    """
    cfg = get_config()
    if lang is None:
        lang = _get_buyer_lang(buyer_id) if buyer_id else \
            (cfg.get("default_language", "ru") or "ru")
    if lang not in ("ru", "en"):
        lang = "ru"

    # 1) cfg legacy override (RU only — для обратной совместимости).
    legacy_overrides = cfg.get("templates") or {}
    file_overrides = _load_templates_file(lang)
    tpl = ""
    if lang == "ru":
        tpl = file_overrides.get(name) or legacy_overrides.get(name) \
            or _DEFAULT_TEMPLATES.get(name, "")
    else:
        tpl = file_overrides.get(name) \
            or _DEFAULT_TEMPLATES_EN.get(name, "") \
            or _DEFAULT_TEMPLATES.get(name, "")
    for k, v in kwargs.items():
        tpl = tpl.replace("{" + k + "}", str(v))
    return tpl


# ── i18n: per-buyer language + JSON-файлы шаблонов (v2.22) ───────────────────
_buyer_lang_lock = threading.Lock()
_buyer_lang_cache: dict[str, str] | None = None
_templates_cache: dict[str, tuple[float, dict[str, str]]] = {}
_templates_cache_lock = threading.Lock()


def _load_buyer_lang() -> dict[str, str]:
    global _buyer_lang_cache
    if _buyer_lang_cache is not None:
        return _buyer_lang_cache
    with _buyer_lang_lock:
        if _buyer_lang_cache is None:
            _buyer_lang_cache = _load_json(BUYER_LANG_FILE, {})
    return _buyer_lang_cache


def _save_buyer_lang(data: dict[str, str]) -> None:
    global _buyer_lang_cache
    with _buyer_lang_lock:
        _buyer_lang_cache = data
        _save_json(BUYER_LANG_FILE, data)


def _get_buyer_lang(buyer_id: Any) -> str:
    if buyer_id is None:
        return get_config().get("default_language", "ru") or "ru"
    data = _load_buyer_lang()
    lang = data.get(str(buyer_id))
    if lang in ("ru", "en"):
        return lang
    return get_config().get("default_language", "ru") or "ru"


def _set_buyer_lang(buyer_id: Any, lang: str) -> None:
    if buyer_id is None or lang not in ("ru", "en"):
        return
    data = dict(_load_buyer_lang())
    data[str(buyer_id)] = lang
    _save_buyer_lang(data)


def _templates_file_for(lang: str) -> str:
    return TEMPLATES_EN_FILE if lang == "en" else TEMPLATES_RU_FILE


def _load_templates_file(lang: str) -> dict[str, str]:
    """Читает шаблоны из JSON-файла. Кэширует с проверкой mtime."""
    path = _templates_file_for(lang)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    with _templates_cache_lock:
        cached = _templates_cache.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            data = _load_json(path, {}) or {}
        except Exception:
            data = {}
        _templates_cache[path] = (mtime, data)
        return data


def _save_templates_file(lang: str, data: dict[str, str]) -> None:
    """Атомарно пишет шаблоны языка lang в JSON-файл и сбрасывает кэш."""
    path = _templates_file_for(lang)
    _save_json(path, data)
    with _templates_cache_lock:
        _templates_cache.pop(path, None)


def _ensure_templates_files() -> None:
    """Создаёт templates_*.json из встроенных дефолтов, если файлов нет.
    Идемпотентно: повторные вызовы не перезаписывают существующие файлы.
    """
    if not os.path.exists(TEMPLATES_RU_FILE):
        _save_templates_file("ru", dict(_DEFAULT_TEMPLATES))
    if not os.path.exists(TEMPLATES_EN_FILE):
        _save_templates_file("en", dict(_DEFAULT_TEMPLATES_EN))


# v2.22.1: словарь старых дефолтов RU-шаблонов, которые поменялись в этом
# релизе. Используется для бесшовной миграции: если в templates_ru.json
# лежит ровно один из этих old-default-вариантов (т.е. seller не правил
# его руками), он автоматически апгрейдится до текущего дефолта. Если
# в файле кастомный текст — НЕ трогаем.
_OLD_DEFAULT_TEMPLATES_RU: dict[str, list[str]] = {
    # v2.22.0 → v2.22.1: добавлена строка подсказки про английский
    "issue": [
        (
            "🟩 АККАУНТ ВЫДАН!\n"
            "🎮 Игра: {game}\n\n"
            "🔑 Логин: {login}\n"
            "🔒 Пароль: {password}\n"
            "⏰ Срок: {duration}\n\n"
            "💬 Команды: !код {login} | !продлить\n"
            "⭐ +1 час за отзыв 5 звёзд!\n"
            "🔄 Пароль сменится после аренды"
        ),
    ],
    # v2.22.x → v2.23.0: в строке игры теперь выводятся Steam-логины
    # свободных аккаунтов (плейсхолдер {logins}).
    # v2.23.0 → v2.23.1: убраны HTML-теги <b>/<code> (FunPay-чат их не
    # рендерит, показывал буквально «<b>1</b>»).
    "accounts_list_lot_line": [
        "🎮 {game}: <b>{free}</b> шт.",
        "🎮 {game} (<b>{free}</b> шт.)\n   {logins}",
    ],
    "accounts_list": [
        (
            "📋 <b>Доступные аккаунты</b>\n\n"
            "{lots}\n\n"
            "💬 Чтобы арендовать — оплатите лот на FunPay."
        ),
    ],
    "accounts_list_empty": [
        (
            "📋 <b>Доступные аккаунты</b>\n\n"
            "✖ К сожалению, сейчас нет свободных аккаунтов.\n"
            "Напишите продавцу — он добавит."
        ),
    ],
    "accounts_list_busy_header": [
        "🔴 <b>Сейчас в аренде:</b>",
    ],
    "accounts_list_busy_line": [
        "⏰ {game} — <code>{login}</code> (осталось {remaining})",
    ],
}

# v2.23.0: аналогичная карта для EN (раньше для EN миграции не было).
_OLD_DEFAULT_TEMPLATES_EN: dict[str, list[str]] = {
    "accounts_list_lot_line": [
        "🎮 {game}: <b>{free}</b> pcs.",
        "🎮 {game} (<b>{free}</b> pcs.)\n   {logins}",
    ],
    "accounts_list": [
        (
            "📋 <b>Available accounts</b>\n\n"
            "{lots}\n\n"
            "💬 To rent — pay the lot on FunPay."
        ),
    ],
    "accounts_list_empty": [
        (
            "📋 <b>Available accounts</b>\n\n"
            "✖ Sorry, no free accounts right now.\n"
            "Message the seller — they'll add some."
        ),
    ],
    "accounts_list_busy_header": [
        "🔴 <b>Currently rented:</b>",
    ],
    "accounts_list_busy_line": [
        "⏰ {game} — <code>{login}</code> ({remaining} left)",
    ],
}


def _migrate_outdated_template_defaults() -> bool:
    """Заменяет в templates_ru.json/templates_en.json те ключи, чьи значения
    точно совпадают со старыми дефолтами (т.е. seller их не редактировал)
    — на свежие дефолты. Возвращает True если что-то поменялось.

    Идемпотентно: повторный вызов после миграции — no-op (значения уже
    совпадают с текущими дефолтами, не со старыми).

    v2.23.0: миграция теперь работает и для EN (раньше — только RU).
    """
    changed_any = False
    for lang, old_map, defaults in (
        ("ru", _OLD_DEFAULT_TEMPLATES_RU, _DEFAULT_TEMPLATES),
        ("en", _OLD_DEFAULT_TEMPLATES_EN, _DEFAULT_TEMPLATES_EN),
    ):
        try:
            cur_file = _load_templates_file(lang) or {}
            new_file = dict(cur_file)
            for key, old_values in old_map.items():
                cur = new_file.get(key)
                if cur in old_values:
                    new_default = defaults.get(key, "")
                    if new_default and new_default != cur:
                        new_file[key] = new_default
                        LOGGER.info(
                            "steam_rental: миграция шаблона %s %r — "
                            "старый дефолт заменён на новый",
                            lang.upper(), key)
            if new_file != cur_file:
                _save_templates_file(lang, new_file)
                changed_any = True
        except Exception:
            LOGGER.debug(
                "steam_rental: outdated-defaults migration (%s) failed",
                lang, exc_info=True)
    return changed_any


def _migrate_legacy_templates_into_files(cfg: dict[str, Any]) -> bool:
    """Одноразовая миграция: переносит cfg["templates"] (старый legacy
    override до v2.22) в templates_ru.json. Помечает результат флагом
    `_templates_externalized_v2_22` чтобы не повторять. Возвращает True,
    если что-то изменилось в cfg (нужно пересохранить).
    """
    if cfg.get("_templates_externalized_v2_22"):
        return False
    legacy = cfg.get("templates") or {}
    # Сохраняем только те ключи, которые отличаются от встроенных дефолтов
    # (чтобы не плодить мусор в файле).
    changed_overrides = {
        k: v for k, v in legacy.items()
        if v and v != _DEFAULT_TEMPLATES.get(k)
    }
    if changed_overrides:
        _ensure_templates_files()
        current = _load_templates_file("ru") or {}
        merged = {**current, **changed_overrides}
        _save_templates_file("ru", merged)
    cfg["templates"] = {}  # очищаем legacy
    cfg["_templates_externalized_v2_22"] = True
    return True


# ── История аренд / статистика ───────────────────────────────────────────────
# ── actions.log: человекочитаемый журнал действий плагина ──────────────────
_actions_log_lock = threading.Lock()
_actions_logger: logging.Logger | None = None


def _get_actions_logger() -> logging.Logger | None:
    """Возвращает (и лениво создаёт) Logger, пишущий в storage/.../actions.log
    с ротацией по размеру. Никогда не пробрасывает в корневой logger
    (propagate=False), чтобы actions не дублировались в cardinal.log.
    """
    global _actions_logger
    if _actions_logger is not None:
        return _actions_logger
    with _actions_log_lock:
        if _actions_logger is not None:
            return _actions_logger
        try:
            os.makedirs(STORAGE_DIR, exist_ok=True)
            from logging.handlers import RotatingFileHandler
            lg = logging.getLogger("FPC.steam_rental.actions")
            lg.setLevel(logging.INFO)
            lg.propagate = False  # не лить в cardinal.log
            # На всякий случай: если хэндлер уже стоит (повторная
            # инициализация) — не плодим дубли.
            already = any(getattr(h, "_steam_rental_actions", False)
                          for h in lg.handlers)
            if not already:
                handler = RotatingFileHandler(
                    ACTIONS_LOG_FILE,
                    maxBytes=ACTIONS_LOG_MAX_BYTES,
                    backupCount=ACTIONS_LOG_BACKUPS,
                    encoding="utf-8",
                )
                handler.setFormatter(logging.Formatter(
                    "%(asctime)s %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                ))
                handler._steam_rental_actions = True  # type: ignore[attr-defined]
                lg.addHandler(handler)
            _actions_logger = lg
            return lg
        except Exception:
            LOGGER.debug("steam_rental: actions logger init failed",
                         exc_info=True)
            return None


# Иконки/префиксы для разных типов действий (для читаемости actions.log).
_ACTION_ICONS = {
    "lot_activated":   "✅ ЛОТ ВКЛ ",
    "lot_deactivated": "⛔ ЛОТ ВЫКЛ",
    "lot_skip_ext":    "⏭ ЛОТ EXT ",
    "lot_save_failed": "⚠ ЛОТ FAIL",
    "rental_start":    "📦 ВЫДАЧА  ",
    "rental_end":      "🏁 КОНЕЦ   ",
    "rental_extend":   "⏳ ПРОДЛ   ",
    "rental_refund":   "💸 ВОЗВРАТ ",
    "acc_freeze":      "❄️ ЗАМОР   ",
    "acc_unfreeze":    "🔥 РАЗМОР  ",
    "acc_vac_ban":     "🚨 VAC BAN ",
    "acc_login_ok":    "🔑 ЛОГИН OK",
    "acc_login_fail":  "❌ ЛОГИН FA",
    "review_bonus":    "⭐ БОНУС   ",
    "review_penalty":  "👎 ШТРАФ   ",
    "reactivation":    "🔁 ПЕРЕАКТ ",
}


def _log_action(action: str, summary: str = "", **extra: Any) -> None:
    """Записывает действие плагина в actions.log.

    :param action: ключ из _ACTION_ICONS (или произвольный).
    :param summary: основная человекочитаемая строка.
    :param extra: дополнительные поля (alias, lot_id, order_id, …) —
        выводятся через ' | k=v'.
    """
    lg = _get_actions_logger()
    if lg is None:
        return
    icon = _ACTION_ICONS.get(action, f"• {action:10}")
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
        LOGGER.debug("steam_rental: write actions.log failed", exc_info=True)


def _log_rental_event(event_type: str, alias: str, **extra: Any) -> None:
    entry: dict[str, Any] = {
        "ts": _now(),
        "event": event_type,
        "alias": alias,
    }
    for k, v in extra.items():
        if v is not None:
            entry[k] = v
    try:
        history = _load_json(HISTORY_FILE, [])
        history.append(entry)
        if len(history) > 10000:
            history = history[-10000:]
        _save_json(HISTORY_FILE, history)
    except Exception:
        LOGGER.debug("steam_rental: failed to log rental event", exc_info=True)


def list_history() -> list[dict[str, Any]]:
    return _load_json(HISTORY_FILE, [])


def _apply_refund_to_stats(order_id: Any, buyer_username: str | None,
                           buyer_id: int | None) -> tuple[str | None, float]:
    """Учитывает возврат денег по заказу: вычитает выручку из per-account
    статистики и записывает в history событие 'refund' с отрицательной суммой.

    Логика:
      1. Находит в history последнее событие 'start' с тем же order_id.
      2. Если уже refunded — повтор не делает (идемпотентно).
      3. Помечает start.refunded=True, refund_ts=now.
      4. Добавляет новое событие 'refund' с amount=-original_amount,
         чтобы все формулы выручки (sum по period) автоматически вычитали
         его. duration/alias/order_id переносятся для трассировки.
      5. Декрементит acc.stats.total_revenue и инкрементит refunded_count.

    Возвращает (alias, amount) если matching start найден и refund применён;
    (alias, amount) если refund уже был применён ранее; (None, 0.0) — если
    в history нет start с таким order_id (например, заказ из старой версии,
    либо это extension-лот, у которого revenue уходит в `start` уже учтённой
    аренды — без отдельного `start` для extension'а).
    """
    if not order_id:
        return None, 0.0
    try:
        history = _load_json(HISTORY_FILE, [])
    except Exception:
        return None, 0.0
    target = None
    for h in reversed(history):
        if (h.get("event") == "start"
                and str(h.get("order_id") or "") == str(order_id)):
            target = h
            break
    if target is None:
        return None, 0.0
    amount = float(target.get("amount", 0) or 0)
    alias = target.get("alias") or None
    if target.get("refunded"):
        LOGGER.info(
            "steam_rental: refund для заказа %s уже учтён, пропускаю",
            order_id)
        return alias, amount
    target["refunded"] = True
    target["refund_ts"] = _now()
    history.append({
        "ts": _now(),
        "event": "refund",
        "alias": alias or "",
        "order_id": str(order_id),
        "buyer_username": buyer_username or target.get("buyer_username") or "",
        "buyer_id": buyer_id if buyer_id is not None else target.get("buyer_id"),
        "amount": -amount,
        "duration_min": int(target.get("duration_min", 0) or 0),
    })
    try:
        _save_json(HISTORY_FILE, history)
    except Exception:
        LOGGER.error(
            "steam_rental: не удалось сохранить history после refund %s",
            order_id, exc_info=True)
    if alias and amount > 0:
        try:
            _bump_acc_stat(
                alias,
                inc_refunded_count=1,
                add_total_revenue=-amount,
            )
        except Exception:
            LOGGER.error(
                "steam_rental: bump_acc_stat refund(%s, %s) failed",
                alias, amount, exc_info=True)
    _log_action(
        "rental_refund",
        f"Возврат #{order_id} → {alias or '?'}: -{amount:.2f}",
        alias=alias, order_id=order_id,
        buyer=buyer_username, buyer_id=buyer_id,
        amount=-amount)
    return alias, amount


def export_history_csv() -> bytes:
    history = list_history()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["timestamp", "event", "alias", "buyer_username",
                     "buyer_id", "order_id", "game", "duration_min", "amount"])
    for entry in history:
        writer.writerow([
            _fmt_ts(entry.get("ts", 0)),
            entry.get("event", ""),
            entry.get("alias", ""),
            entry.get("buyer_username", ""),
            entry.get("buyer_id", ""),
            entry.get("order_id", ""),
            entry.get("game", ""),
            entry.get("duration_min", ""),
            entry.get("amount", ""),
        ])
    return output.getvalue().encode("utf-8-sig")


def _format_acc_stats_compact(alias: str) -> str:
    """Компактная per-account сводка (для /srental_acc_stats и кнопки)."""
    acc = find_account(alias)
    if not acc:
        return f"Аккаунт <code>{alias}</code> не найден."
    st = acc.get("stats") or {}
    sales = int(st.get("rentals_count", 0) or 0)
    revenue = float(st.get("total_revenue", 0) or 0)
    reviews = int(st.get("reviews_count", 0) or 0)
    extends = int(st.get("ext_count", 0) or 0)
    refunded_count = int(st.get("refunded_count", 0) or 0)
    cost = float(acc.get("cost", 0.0) or 0.0)
    profit = revenue - cost
    roi_str = "—"
    if cost > 0:
        roi_str = f"{(profit / cost) * 100:+.0f}%"
    game = acc.get("game") or _get_game_for_alias(alias) or "—"

    # v2.22.4: считаем суммарные refund'ы по этому аккаунту, чтобы
    # показать «💸 Возвратов: N (-NNN₽)» рядом с другой статистикой.
    refund_amount = 0.0
    if refunded_count:
        for h in list_history():
            if h.get("event") == "refund" and h.get("alias") == alias:
                refund_amount += float(h.get("amount", 0) or 0)
    # refund_amount уже отрицательный (мы пишем -original в history)

    # Статус
    if acc.get("frozen"):
        status = "❄️ Заморожен"
    elif acc.get("rental"):
        r = acc["rental"]
        remain = max(0, int(r.get("expires_at", 0)) - _now())
        remain_h = remain // 3600
        remain_m = (remain % 3600) // 60
        status = f"🔴 В аренде (осталось {remain_h}ч {remain_m}м)"
    else:
        status = "🟢 Свободен"

    # Периоды
    fp = _calc_finance_periods(alias)

    last_used = int(st.get("last_used_at", 0) or 0)
    last_str = _fmt_ts(last_used) + " МСК" if last_used else "—"

    return (
        f"<b>📊 {_esc(alias)}</b> — {status}\n"
        f"🎮 {_esc(game)}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💵 <b>Расход:</b> {cost:.0f}₽\n"
        f"💰 <b>Выручка:</b> {revenue:.0f}₽\n"
        f"📈 <b>Прибыль:</b> {profit:+.0f}₽  (ROI {roi_str})\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🛒 Продаж: <b>{sales}</b>  •  "
        f"🔄 Продлений: <b>{extends}</b>  •  "
        f"⭐ Отзывов: <b>{reviews}</b>"
        + (
            f"\n💸 Возвратов: <b>{refunded_count}</b>  "
            f"(<b>{refund_amount:+.0f}₽</b>)"
            if refunded_count else ""
        )
        + "\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📅 <b>Выручка по периодам</b> (с учётом возвратов)\n"
        f"  День:    <b>{fp['day']:.0f}₽</b>  "
        f"({fp['count_day']} прод.)\n"
        f"  Неделя:  <b>{fp['week']:.0f}₽</b>  "
        f"({fp['count_week']} прод.)\n"
        f"  Месяц:   <b>{fp['month']:.0f}₽</b>  "
        f"({fp['count_month']} прод.)\n"
        f"  Всего:   <b>{fp['total']:.0f}₽</b>  "
        f"({fp['count_total']} прод.)\n"
        f"\n🕒 Последняя активность: {last_str}"
    )


def _calc_finance_periods(alias: str | None = None) -> dict[str, float]:
    """Считает финансы за периоды (day/week/month/total) по history.

    Если alias задан — только для этого аккаунта; иначе глобально.
    Возвращает {"day": ..., "week": ..., "month": ..., "total": ...,
                "count_day": ..., "count_week": ..., "count_month": ...,
                "count_total": ...}.
    """
    history = list_history()
    now = _now()
    day_ago = now - 86400
    week_ago = now - 7 * 86400
    month_ago = now - 30 * 86400
    out = {
        "day": 0.0, "week": 0.0, "month": 0.0, "total": 0.0,
        "count_day": 0, "count_week": 0,
        "count_month": 0, "count_total": 0,
    }
    # v2.22.4: учитываем 'start' (положительная сумма) И 'refund'
    # (отрицательная) — refund-события автоматически вычитаются из
    # дневной/недельной/месячной/общей выручки. Раньше per-account
    # «Выручка по периодам» считалась ТОЛЬКО по 'start' и поэтому
    # не показывала, что покупатель вернул деньги. Счётчик «прод.»
    # по-прежнему считаем только по 'start' — он отражает количество
    # выданных аренд, не нетто-сделки.
    for h in history:
        ev = h.get("event")
        if ev not in ("start", "refund"):
            continue
        if alias is not None and h.get("alias") != alias:
            continue
        ts = int(h.get("ts", 0) or 0)
        amt = float(h.get("amount", 0) or 0)  # refund.amount уже отрицательный
        is_start = (ev == "start")
        out["total"] += amt
        if is_start:
            out["count_total"] += 1
        if ts >= day_ago:
            out["day"] += amt
            if is_start:
                out["count_day"] += 1
        if ts >= week_ago:
            out["week"] += amt
            if is_start:
                out["count_week"] += 1
        if ts >= month_ago:
            out["month"] += amt
            if is_start:
                out["count_month"] += 1
    return out


def _calc_stats() -> dict[str, Any]:
    history = list_history()
    accs = list_accounts()
    now = _now()
    day_ago = now - 86400
    week_ago = now - 7 * 86400
    month_ago = now - 30 * 86400

    starts = [h for h in history if h.get("event") == "start"]
    starts_day = [h for h in starts if h.get("ts", 0) >= day_ago]
    starts_week = [h for h in starts if h.get("ts", 0) >= week_ago]
    starts_month = [h for h in starts if h.get("ts", 0) >= month_ago]

    durations = [h.get("duration_min", 0) for h in starts if h.get("duration_min")]
    avg_duration = sum(durations) / len(durations) if durations else 0

    # Выручка считается по событиям 'start' (положительная) И 'refund'
    # (отрицательная): возврат денег покупателю автоматически уменьшает
    # выручку в общей сводке и в дневной/недельной/месячной разбивке.
    money = [h for h in history if h.get("event") in ("start", "refund")]
    money_day = [h for h in money if h.get("ts", 0) >= day_ago]
    money_week = [h for h in money if h.get("ts", 0) >= week_ago]
    money_month = [h for h in money if h.get("ts", 0) >= month_ago]
    amounts = [float(h.get("amount", 0) or 0) for h in money]
    total_rev = sum(amounts)
    rev_day = sum(float(h.get("amount", 0) or 0) for h in money_day)
    rev_week = sum(float(h.get("amount", 0) or 0) for h in money_week)
    rev_month = sum(float(h.get("amount", 0) or 0) for h in money_month)
    avg_check = (sum(float(h.get("amount", 0) or 0) for h in starts) / len(starts)) \
        if starts else 0

    games: dict[str, int] = {}
    for h in starts:
        g = h.get("game", "").strip()
        if g and g != "—":
            games[g] = games.get(g, 0) + 1
    top_games = sorted(games.items(), key=lambda x: x[1], reverse=True)[:5]

    n_total = len(accs)
    n_frozen = sum(1 for a in accs if a.get("frozen"))
    n_rented = sum(1 for a in accs if a.get("rental"))
    n_free = n_total - n_frozen - n_rented
    n_problem = sum(
        1 for a in accs
        if a.get("login_failures", 0) or a.get("chpwd_failures", 0))
    total_login_fails = sum(int(a.get("login_failures", 0)) for a in accs)
    total_chpwd_fails = sum(int(a.get("chpwd_failures", 0)) for a in accs)
    utilization = (n_rented / max(1, n_total - n_frozen)) * 100

    return {
        "total": len(starts),
        "day": len(starts_day),
        "week": len(starts_week),
        "month": len(starts_month),
        "avg_duration": avg_duration,
        "avg_check": avg_check,
        "total_revenue": total_rev,
        "rev_day": rev_day,
        "rev_week": rev_week,
        "rev_month": rev_month,
        "top_games": top_games,
        "extensions": len([h for h in history if h.get("event") == "extend"]),
        "review_bonuses": len([h for h in history if h.get("event") == "review_bonus"]),
        "acc_total": n_total,
        "acc_free": n_free,
        "acc_rented": n_rented,
        "acc_frozen": n_frozen,
        "acc_problem": n_problem,
        "login_failures": total_login_fails,
        "chpwd_failures": total_chpwd_fails,
        "utilization_pct": utilization,
    }


# ─── v5: Buyer blacklist ─────────────────────────────────────────────────────
def list_blacklist() -> list[dict[str, Any]]:
    """Список заблокированных покупателей. Структура:
    [{'buyer_id': int|None, 'username': str|None, 'reason': str, 'ts': int}, ...]"""
    return _load_json(BLACKLIST_FILE, [])


def _save_blacklist(items: list[dict[str, Any]]) -> None:
    _save_json(BLACKLIST_FILE, items)


def _normalize_bl_key(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().lower()


def is_blacklisted(buyer_id: Any = None, username: Any = None) -> bool:
    """Совпадение по buyer_id ИЛИ username (case-insensitive)."""
    bid = _normalize_bl_key(buyer_id)
    uname = _normalize_bl_key(username)
    if not bid and not uname:
        return False
    for entry in list_blacklist():
        if bid and _normalize_bl_key(entry.get("buyer_id")) == bid:
            return True
        if uname and _normalize_bl_key(entry.get("username")) == uname:
            return True
    return False


def add_to_blacklist(buyer_id: Any = None, username: Any = None,
                     reason: str = "manual") -> bool:
    """Возвращает True если запись добавлена (или уже существовала)."""
    bid = _normalize_bl_key(buyer_id)
    uname = _normalize_bl_key(username)
    if not bid and not uname:
        return False
    with _lock:
        items = list_blacklist()
        for e in items:
            if (bid and _normalize_bl_key(e.get("buyer_id")) == bid) \
                    or (uname and _normalize_bl_key(e.get("username")) == uname):
                return True  # уже есть
        items.append({
            "buyer_id": int(buyer_id) if str(buyer_id or "").isdigit() else None,
            "username": str(username) if username else None,
            "reason": reason,
            "ts": _now(),
        })
        _save_blacklist(items)
    return True


def remove_from_blacklist(buyer_id: Any = None, username: Any = None) -> bool:
    bid = _normalize_bl_key(buyer_id)
    uname = _normalize_bl_key(username)
    if not bid and not uname:
        return False
    with _lock:
        items = list_blacklist()
        keep = []
        removed = False
        for e in items:
            matches = (
                (bid and _normalize_bl_key(e.get("buyer_id")) == bid)
                or (uname and _normalize_bl_key(e.get("username")) == uname)
            )
            if matches:
                removed = True
            else:
                keep.append(e)
        if removed:
            _save_blacklist(keep)
        return removed


# ─── v5: Метрики Prometheus ──────────────────────────────────────────────────
def _load_metrics() -> dict[str, Any]:
    return _load_json(METRICS_FILE, {
        "rentals_delivered_total": 0,
        "rentals_expired_total": 0,
        "rentals_extended_total": 0,
        "blocked_blacklist_total": 0,
        "operator_stop_total": 0,
        "operator_switch_total": 0,
        "blacklist_auto_refund_total": 0,
    })


def _save_metrics(m: dict[str, Any]) -> None:
    _save_json(METRICS_FILE, m)


def _metric_inc(key: str, delta: int = 1) -> None:
    with _lock:
        m = _load_metrics()
        m[key] = int(m.get(key, 0)) + delta
        _save_metrics(m)


def _metric_snapshot() -> dict[str, float]:
    m = _load_metrics()
    accs = list_accounts()
    snap: dict[str, float] = {
        "asr_assignments_active": sum(1 for a in accs if a.get("rental")),
        "asr_accounts_total": len(accs),
        "asr_accounts_frozen": sum(1 for a in accs if a.get("frozen")),
        "asr_accounts_free": sum(
            1 for a in accs
            if not a.get("frozen") and not a.get("rental")),
        "asr_blacklist_size": len(list_blacklist()),
    }
    for k in ("rentals_delivered_total", "rentals_expired_total",
              "rentals_extended_total", "blocked_blacklist_total",
              "operator_stop_total", "operator_switch_total",
              "blacklist_auto_refund_total"):
        snap[f"asr_{k}"] = float(m.get(k, 0))
    return snap


# ─── v5: Per-account аналитика ───────────────────────────────────────────────
def _bump_acc_stat(alias: str, **fields: Any) -> None:
    """Обновляет блок acc['stats']:
    - increments: rentals_count, delivered_count, expired_count, ext_count
    - sums: total_minutes, total_revenue
    - timestamps: first_used_at (один раз), last_used_at, last_expired_at,
      last_delivered_at
    """
    if not alias:
        return
    with _lock:
        acc = find_account(alias)
        if not acc:
            return
        st = acc.get("stats") or {}
        now = _now()
        if not st.get("first_used_at"):
            st["first_used_at"] = now
        st["last_used_at"] = now
        for k, v in fields.items():
            if k.startswith("inc_"):
                key = k[4:]
                st[key] = int(st.get(key, 0)) + int(v)
            elif k.startswith("add_"):
                key = k[4:]
                st[key] = float(st.get(key, 0)) + float(v)
            elif k.startswith("set_"):
                key = k[4:]
                st[key] = v
            else:
                st[key] = v
        acc["stats"] = st
        upsert_account(acc)


# ── Отслеживание логинов / авто-заморозка ────────────────────────────────────
# Пороги авто-заморозки (можно вынести в config при желании).
_LOGIN_FAILURE_THRESHOLD = 3
_CHANGE_PW_FAILURE_THRESHOLD = 2

# Глобальная ссылка на Cardinal для отправки алёртов из тред-сейф-функций.
_CARDINAL_REF: "Cardinal | None" = None

# Кэш последнего известного состояния активации лотов на FunPay:
# {lot_key: {"active": True/False, "ts": int, "result": "ok"/"fail"}}
_LOT_ACTIVATION_CACHE: dict[str, dict[str, Any]] = {}
_lot_state_loaded = False
_lot_state_lock = threading.Lock()

# v2.16.1: таймеры авто-деактивации extension-лотов, активированных по команде
# !продлить. Если покупатель не оплатил за config.extension_active_ttl_minutes
# минут — таймер выключит лот обратно. На успешной оплате
# _handle_extension_purchase отменяет таймер и сам деактивирует лот.
# Ключ — str(lot_id), значение — threading.Timer.
_EXT_LOT_TIMERS: dict[str, threading.Timer] = {}
_ext_lot_timers_lock = threading.Lock()


def _set_cardinal_ref(c: "Cardinal | None") -> None:
    global _CARDINAL_REF
    _CARDINAL_REF = c


def _ensure_lot_state_loaded() -> None:
    """Lazy-load кэша активации с диска (один раз за процесс)."""
    global _lot_state_loaded
    if _lot_state_loaded:
        return
    with _lot_state_lock:
        if _lot_state_loaded:
            return
        try:
            data = _load_json(LOT_STATE_FILE, {})
            if isinstance(data, dict):
                _LOT_ACTIVATION_CACHE.update(data)
        except Exception:
            LOGGER.debug("steam_rental: lot_state load failed",
                         exc_info=True)
        _lot_state_loaded = True


def _save_lot_state() -> None:
    """Сохраняет кэш активации на диск (тихо)."""
    try:
        _save_json(LOT_STATE_FILE, _LOT_ACTIVATION_CACHE)
    except Exception:
        LOGGER.debug("steam_rental: lot_state save failed", exc_info=True)


def _get_lot_active_cached(lot_key: str) -> dict[str, Any] | None:
    """Возвращает последнее известное состояние активации лота или None."""
    _ensure_lot_state_loaded()
    return _LOT_ACTIVATION_CACHE.get(str(lot_key))


def _alert_auto_freeze(alias: str, reason: str, count: int) -> None:
    """Шлёт TG-алёрт об авто-заморозке аккаунта."""
    _log_action("acc_freeze",
                f"Авто-заморозка аккаунта {alias}",
                alias=alias, reason=reason, fail_count=count, mode="auto")
    c = _CARDINAL_REF
    if c is None:
        return
    try:
        _notify_tg(c,
                   f"❄️ <b>Steam Rental</b>: авто-заморозка <code>{alias}</code>\n"
                   f"Причина: {reason} ({count} подряд)\n"
                   f"Лоты переактивируются. Размораживай вручную через "
                   f"<code>/srental → 📋 → {alias} → 🔥 Разморозить</code>.")
        _update_lot_activation(c)
    except Exception:
        LOGGER.debug("steam_rental: alert auto-freeze failed", exc_info=True)


def _track_login_result(alias: str, success: bool) -> None:
    triggered = False
    fail_count = 0
    with _lock:
        acc = find_account(alias)
        if not acc:
            return
        if success:
            acc["login_failures"] = 0
        else:
            acc["login_failures"] = acc.get("login_failures", 0) + 1
            fail_count = acc["login_failures"]
            if (acc["login_failures"] >= _LOGIN_FAILURE_THRESHOLD
                    and not acc.get("frozen")):
                acc["frozen"] = True
                acc["freeze_reason"] = (
                    f"auto: {fail_count} login failures")
                acc["freeze_ts"] = _now()
                triggered = True
                LOGGER.warning(
                    "steam_rental: авто-заморозка %s после %d неудач логина",
                    alias, fail_count)
        upsert_account(acc)
    if triggered:
        _alert_auto_freeze(alias, "неудачные логины", fail_count)


def _track_change_pw_result(alias: str, success: bool,
                            error_msg: str = "") -> None:
    """Считает подряд идущие Error 24 / прочие падения change_password."""
    triggered = False
    fail_count = 0
    with _lock:
        acc = find_account(alias)
        if not acc:
            return
        if success:
            acc["chpwd_failures"] = 0
        else:
            acc["chpwd_failures"] = acc.get("chpwd_failures", 0) + 1
            fail_count = acc["chpwd_failures"]
            acc["chpwd_last_error"] = (error_msg or "")[:300]
            if (acc["chpwd_failures"] >= _CHANGE_PW_FAILURE_THRESHOLD
                    and not acc.get("frozen")):
                acc["frozen"] = True
                acc["freeze_reason"] = (
                    f"auto: {fail_count} change_password failures")
                acc["freeze_ts"] = _now()
                triggered = True
                LOGGER.warning(
                    "steam_rental: авто-заморозка %s после %d ошибок change_password",
                    alias, fail_count)
        upsert_account(acc)
    if triggered:
        _alert_auto_freeze(alias, "ошибки смены пароля (Error 24 и т.п.)",
                            fail_count)


def _push_previous_password(alias: str, old_pw: str,
                            limit: int = 5) -> None:
    """Сохраняет предыдущий пароль в acc['previous_passwords'] (max=limit)."""
    if not old_pw:
        return
    with _lock:
        acc = find_account(alias)
        if not acc:
            return
        history = acc.get("previous_passwords") or []
        if not isinstance(history, list):
            history = []
        history.append({"pw": old_pw, "ts": _now()})
        if len(history) > limit:
            history = history[-limit:]
        acc["previous_passwords"] = history
        upsert_account(acc)


# ── Steam API клиент (на базе bot.py пользователя) ──────────────────────────
_API = "https://api.steampowered.com"
_COMMUNITY = "https://steamcommunity.com"
_STORE = "https://store.steampowered.com"
_LOGIN_HOST = "https://login.steampowered.com"
_HELP = "https://help.steampowered.com"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36"
)


class SteamError(RuntimeError):
    pass


class SteamSession:
    """
    Авторизованная сессия Steam. Логин — через IAuthenticationService +
    shared_secret/identity_secret из .maFile.
    """

    def __init__(self, account_name: str, password: str,
                 shared_secret: str, identity_secret: str,
                 steamid: str | None = None):
        from steampy import guard as steam_guard

        self.account_name = account_name
        self.password = password
        self.shared_secret = shared_secret
        self.identity_secret = identity_secret
        self.steamid: str | None = steamid
        self.sess = requests.Session()
        self.sess.headers.update({"User-Agent": _USER_AGENT})
        self._guard = steam_guard

    def generate_2fa_code(self) -> str:
        return self._guard.generate_one_time_code(self.shared_secret)

    def get_guard_code(self) -> str:
        return self.generate_2fa_code()

    def login(self) -> None:
        from rsa import PublicKey, encrypt as rsa_encrypt

        rsa_resp = self.sess.get(
            f"{_API}/IAuthenticationService/GetPasswordRSAPublicKey/v1/",
            params={"account_name": self.account_name}, timeout=15)
        rsa_data = rsa_resp.json().get("response", {}) or {}
        if "publickey_mod" not in rsa_data:
            raise SteamError("Не удалось получить RSA-ключ от Steam")
        rsa_key = PublicKey(int(rsa_data["publickey_mod"], 16),
                            int(rsa_data["publickey_exp"], 16))
        enc_pw = b64encode(rsa_encrypt(self.password.encode(), rsa_key)).decode()

        begin_data = {
            "account_name": self.account_name,
            "encrypted_password": enc_pw,
            "encryption_timestamp": rsa_data["timestamp"],
            "persistence": "1",
        }
        client_id = None
        request_id = None
        steam_id = None
        for _attempt in range(3):
            resp = self.sess.post(
                f"{_API}/IAuthenticationService/BeginAuthSessionViaCredentials/v1/",
                data=begin_data, timeout=15)
            rd = resp.json().get("response", {}) or {}
            client_id = rd.get("client_id")
            request_id = rd.get("request_id")
            steam_id = rd.get("steamid")
            if client_id:
                break
            time.sleep(1)
        if not client_id:
            raise SteamError("Steam не вернул client_id (возможно, неверный пароль)")
        steam_id = str(steam_id)

        code = self.generate_2fa_code()
        self.sess.post(
            f"{_API}/IAuthenticationService/UpdateAuthSessionWithSteamGuardCode/v1/",
            data={"client_id": client_id, "steamid": steam_id,
                  "code_type": 3, "code": code}, timeout=15)

        refresh_token = None
        for _ in range(10):
            poll = self.sess.post(
                f"{_API}/IAuthenticationService/PollAuthSessionStatus/v1/",
                data={"client_id": client_id, "request_id": request_id},
                timeout=15)
            refresh_token = poll.json().get("response", {}).get("refresh_token")
            if refresh_token:
                break
            time.sleep(2)
        if not refresh_token:
            raise SteamError("Не удалось получить refresh_token (Steam Guard fail)")

        self.sess.get(_COMMUNITY, timeout=15)
        sessionid = self.sess.cookies.get("sessionid", "")
        fin = self.sess.post(
            f"{_LOGIN_HOST}/jwt/finalizelogin",
            data={"nonce": refresh_token, "sessionid": sessionid,
                  "redir": f"{_COMMUNITY}/login/home/?goto="},
            timeout=15)
        fin_json = fin.json()
        for ti in fin_json.get("transfer_info", []):
            ti["params"]["steamID"] = fin_json.get("steamID", steam_id)
            try:
                self.sess.post(ti["url"], ti["params"], timeout=10)
            except Exception:
                pass

        self.steamid = str(fin_json.get("steamID") or steam_id)
        LOGGER.info("steam_rental: login OK для %s (steamid=%s)",
                    self.account_name, self.steamid)

    def sessionid_for(self, host: str) -> str:
        try:
            self.sess.get(host, timeout=15)
        except Exception:
            pass
        for cookie in self.sess.cookies:
            if cookie.name == "sessionid":
                if cookie.domain.lstrip(".") in host:
                    return cookie.value
        return self.sess.cookies.get("sessionid", "") or ""

    def revoke_all_other_sessions(self) -> bool:
        sessionid = self.sessionid_for(_STORE)
        if not sessionid:
            raise SteamError("Нет sessionid для store.steampowered.com")
        endpoints = [
            (f"{_STORE}/twofactor/manage_action",
             {"action": "deauthorize", "sessionid": sessionid}),
            (f"{_COMMUNITY}/profiles/{self.steamid}/edit/info",
             {"sessionID": sessionid, "type": "deauthorize"}),
        ]
        ok = False
        for url, data in endpoints:
            try:
                r = self.sess.post(url, data=data, timeout=15,
                                    headers={"Referer": f"{_STORE}/account/"})
                if r.status_code < 400:
                    ok = True
            except Exception:
                LOGGER.debug("steam_rental: revoke endpoint %s failed", url,
                             exc_info=True)
        return ok

    def change_password(self, new_password: str) -> None:
        from rsa import PublicKey, encrypt as rsa_encrypt
        from urllib.parse import urlparse, parse_qs

        sid_help = self.sessionid_for(_HELP)
        if not sid_help:
            raise SteamError("Нет sessionid для help.steampowered.com (логин истёк?)")

        r1 = self.sess.get(
            f"{_HELP}/wizard/HelpChangePassword?redir=store/account/",
            headers={"User-Agent": _USER_AGENT,
                     "Referer": f"{_STORE}/", "Accept": "text/html"},
            allow_redirects=True, timeout=15)
        final_url = r1.url
        qs = parse_qs(urlparse(final_url).query)
        params = {k: qs.get(k, [""])[0] for k in
                  ("s", "account", "reset", "lost", "issueid")}
        if not params["s"]:
            raise SteamError(
                "Не удалось получить параметры wizard-recovery (нужен валидный логин в Steam)")

        self.sess.get(
            f"{_HELP}/en/wizard/HelpWithLoginInfoEnterCode",
            params={**params, "sessionid": sid_help,
                    "wizard_ajax": 1, "gamepad": 0},
            headers={"User-Agent": _USER_AGENT,
                     "X-Requested-With": "XMLHttpRequest"}, timeout=15)

        r3 = self.sess.post(
            f"{_HELP}/en/wizard/AjaxSendAccountRecoveryCode",
            data={"sessionid": sid_help, "wizard_ajax": "1", "gamepad": "0",
                  "s": params["s"], "method": "8", "link": "", "n": "1"},
            headers={"User-Agent": _USER_AGENT,
                     "X-Requested-With": "XMLHttpRequest",
                     "Origin": _HELP,
                     "Referer": f"{_HELP}/en/wizard/HelpWithLoginInfoEnterCode"},
            timeout=15)
        r3_json = self._safe_json(r3)
        if r3_json.get("errorMsg"):
            raise SteamError(f"AjaxSendAccountRecoveryCode: {r3_json['errorMsg']}")

        self._mobile_confirm_recovery(params["s"])

        self.sess.post(
            f"{_HELP}/en/wizard/AjaxPollAccountRecoveryConfirmation",
            data={"sessionid": sid_help, "wizard_ajax": 1,
                  "s": params["s"], "reset": params["reset"],
                  "lost": params["lost"], "method": 8,
                  "issueid": params["issueid"], "gamepad": 0},
            headers={"User-Agent": _USER_AGENT,
                     "X-Requested-With": "XMLHttpRequest",
                     "Origin": _HELP}, timeout=15)

        self.sess.get(
            f"{_HELP}/en/wizard/AjaxVerifyAccountRecoveryCode",
            params={"code": "", "s": params["s"], "reset": params["reset"],
                    "lost": params["lost"], "method": 8,
                    "issueid": params["issueid"], "sessionid": sid_help,
                    "wizard_ajax": 1, "gamepad": 0},
            headers={"User-Agent": _USER_AGENT,
                     "X-Requested-With": "XMLHttpRequest"}, timeout=15)

        self.sess.post(
            f"{_HELP}/en/wizard/AjaxAccountRecoveryGetNextStep",
            data={"sessionid": sid_help, "wizard_ajax": 1, "s": params["s"],
                  "account": params["account"], "reset": params["reset"],
                  "issueid": params["issueid"], "lost": 2},
            headers={"User-Agent": _USER_AGENT,
                     "X-Requested-With": "XMLHttpRequest",
                     "Origin": _HELP}, timeout=15)

        def _fetch_rsa() -> tuple["PublicKey", str]:
            rsa_r = self.sess.post(
                f"{_HELP}/en/login/getrsakey/",
                data={"sessionid": sid_help, "username": self.account_name},
                headers={"User-Agent": _USER_AGENT,
                         "X-Requested-With": "XMLHttpRequest",
                         "Origin": _HELP}, timeout=15)
            rsa_json = self._safe_json(rsa_r)
            if "publickey_mod" not in rsa_json:
                raise SteamError("Не удалось получить RSA-ключ Steam")
            return (PublicKey(int(rsa_json["publickey_mod"], 16),
                              int(rsa_json["publickey_exp"], 16)),
                    rsa_json["timestamp"])

        # ── VerifyPassword: ТЕКУЩИЙ пароль (proof of ownership) ────────────
        rsa_key_old, ts_old = _fetch_rsa()
        enc_old = b64encode(rsa_encrypt(self.password.encode("ascii"),
                                         rsa_key_old)).decode()
        vp = self.sess.post(
            f"{_HELP}/en/wizard/AjaxAccountRecoveryVerifyPassword/",
            data={"sessionid": sid_help, "s": params["s"], "lost": 2,
                  "reset": 1, "password": enc_old, "rsatimestamp": ts_old},
            headers={"User-Agent": _USER_AGENT,
                     "X-Requested-With": "XMLHttpRequest",
                     "Origin": _HELP}, timeout=15)
        vp_json = self._safe_json(vp)
        if vp_json.get("errorMsg"):
            raise SteamError(f"AjaxAccountRecoveryVerifyPassword: {vp_json['errorMsg']}")

        # ── CheckPasswordAvailable: новый пароль (plaintext) ───────────────
        chk = self.sess.post(
            f"{_HELP}/en/wizard/AjaxCheckPasswordAvailable/",
            data={"sessionid": sid_help, "wizard_ajax": 1,
                  "password": new_password},
            headers={"User-Agent": _USER_AGENT, "Origin": _HELP}, timeout=15)
        chk_json = self._safe_json(chk)
        if not chk_json.get("available", True):
            raise SteamError("Steam: новый пароль недоступен (слишком простой/похожий)")

        # ── ChangePassword: НОВЫЙ пароль (со свежим RSA timestamp) ─────────
        rsa_key_new, ts_new = _fetch_rsa()
        enc_new = b64encode(rsa_encrypt(new_password.encode("ascii"),
                                         rsa_key_new)).decode()
        ch = self.sess.post(
            f"{_HELP}/en/wizard/AjaxAccountRecoveryChangePassword/",
            data={"sessionid": sid_help, "wizard_ajax": 1, "s": params["s"],
                  "account": params["account"], "password": enc_new,
                  "rsatimestamp": ts_new},
            headers={"User-Agent": _USER_AGENT,
                     "X-Requested-With": "XMLHttpRequest",
                     "Origin": _HELP}, timeout=15)
        ch_json = self._safe_json(ch)
        if ch_json.get("errorMsg"):
            raise SteamError(f"AjaxAccountRecoveryChangePassword: {ch_json['errorMsg']}")

        self.password = new_password
        LOGGER.info("steam_rental: пароль успешно изменён для %s", self.account_name)

    @staticmethod
    def _safe_json(r: "requests.Response") -> dict[str, Any]:
        try:
            return r.json() or {}
        except Exception:
            return {}

    def _mobile_confirm_recovery(self, s_id: str) -> None:
        from steampy.confirmation import ConfirmationExecutor

        if not self.steamid:
            raise SteamError("Нет steamid — нужно сначала залогиниться")

        ce = ConfirmationExecutor(self.identity_secret, self.steamid, self.sess)
        last_exc: Exception | None = None
        for _try in range(6):
            try:
                confs = ce._get_confirmations()
            except Exception as exc:
                last_exc = exc
                time.sleep(2)
                continue
            target = None
            for c in confs:
                cid = getattr(c, "data_accept", None) or getattr(c, "creator_id", None) \
                    or getattr(c, "creator", None)
                if cid and str(cid) == str(s_id):
                    target = c
                    break
            if target is None and confs:
                target = confs[-1]
            if target is not None:
                try:
                    ce._send_confirmation(target)
                    return
                except Exception as exc:
                    last_exc = exc
                    time.sleep(2)
            else:
                time.sleep(2)
        raise SteamError(
            f"Не удалось подтвердить запрос смены пароля через mobile "
            f"({type(last_exc).__name__ if last_exc else 'нет confirmation'}: "
            f"{last_exc or ''})")


# ── Управление аккаунтами / лотами / арендой ────────────────────────────────
_lock = threading.RLock()


def list_accounts() -> list[dict[str, Any]]:
    return _load_json(ACCOUNTS_FILE, [])


def save_accounts(accs: list[dict[str, Any]]) -> None:
    _save_json(ACCOUNTS_FILE, accs)


def _list_active_rentals() -> list[dict[str, Any]]:
    """Список активных аренд (rental'ы хранятся внутри accounts.json)."""
    out: list[dict[str, Any]] = []
    now = _now()
    for a in list_accounts():
        r = a.get("rental")
        if not r:
            continue
        exp = int(r.get("expires_at", 0) or 0)
        remaining_sec = max(0, exp - now)
        if remaining_sec <= 0:
            continue
        out.append({
            "id": str(r.get("order_id") or a.get("alias", "")),
            "alias": a.get("alias", ""),
            "account": a.get("account_name") or a.get("alias", "?"),
            "buyer": r.get("buyer_username", "?"),
            "buyer_id": r.get("buyer_id"),
            "chat_id": r.get("chat_id"),
            "order_id": r.get("order_id"),
            "started_at": int(r.get("started_at", 0) or 0),
            "expires_at": exp,
            "duration_min": int(r.get("duration_min", 0) or 0),
            "remaining_sec": remaining_sec,
            "remaining_str": _human_minutes(remaining_sec // 60),
        })
    out.sort(key=lambda x: x["expires_at"])
    return out


def _resolve_rental_by_sid(sid: str) -> dict[str, Any] | None:
    for r in _list_active_rentals():
        if _sid(r["id"]) == sid:
            return r
    return None


def find_active_rental(alias: str) -> dict[str, Any] | None:
    a = find_account(alias)
    if not a:
        return None
    r = a.get("rental")
    if not r:
        return None
    if int(r.get("expires_at", 0) or 0) <= _now():
        return None
    return r


def find_account(alias: str) -> dict[str, Any] | None:
    for a in list_accounts():
        if a.get("alias", "").lower() == alias.lower():
            return a
    return None


def find_account_by_login(login: str) -> dict[str, Any] | None:
    for a in list_accounts():
        if a.get("account_name", "").lower() == login.lower():
            return a
    return None


def upsert_account(acc: dict[str, Any]) -> None:
    with _lock:
        accs = list_accounts()
        for i, a in enumerate(accs):
            if a.get("alias", "").lower() == acc["alias"].lower():
                accs[i] = acc
                break
        else:
            accs.append(acc)
        save_accounts(accs)


def delete_account(alias: str) -> bool:
    with _lock:
        accs = list_accounts()
        new = [a for a in accs if a.get("alias", "").lower() != alias.lower()]
        if len(new) == len(accs):
            return False
        save_accounts(new)
        return True


def rename_account(old_alias: str, new_alias: str) -> tuple[bool, str]:
    """Переименовывает алиас аккаунта. Обновляет пулы лотов автоматически.

    Возвращает (ok, message). На ошибку — (False, причина)."""
    new_alias = (new_alias or "").strip()
    if not new_alias:
        return False, "Алиас не может быть пустым."
    if len(new_alias) > 32:
        return False, "Алиас слишком длинный (max 32 символа)."
    # Разрешённые символы: латиница, цифры, _ - . (без пробелов и спецсимволов)
    if not re.match(r"^[A-Za-z0-9_.\-]+$", new_alias):
        return False, ("В алиасе разрешены только латиница, цифры, "
                       "точка, дефис и подчёркивание.")
    if old_alias.lower() == new_alias.lower():
        return False, "Новый алиас совпадает со старым."
    with _lock:
        accs = list_accounts()
        target_idx = -1
        for i, a in enumerate(accs):
            if a.get("alias", "").lower() == old_alias.lower():
                target_idx = i
            elif a.get("alias", "").lower() == new_alias.lower():
                return False, "Такой алиас уже занят."
        if target_idx < 0:
            return False, "Аккаунт не найден."
        accs[target_idx]["alias"] = new_alias
        save_accounts(accs)

        # Обновляем пулы лотов: каждый алиас, совпадающий со старым,
        # заменяем на новый (case-insensitive).
        lots = list_lots()
        lots_changed = False
        for key, lot in lots.items():
            pool = lot.get("aliases") or []
            new_pool = []
            replaced = False
            for a in pool:
                if a.lower() == old_alias.lower():
                    new_pool.append(new_alias)
                    replaced = True
                else:
                    new_pool.append(a)
            if replaced:
                lots[key]["aliases"] = new_pool
                lots_changed = True
        if lots_changed:
            save_lots(lots)
        return True, "ok"


def list_lots() -> dict[str, dict[str, Any]]:
    return _load_json(LOTS_FILE, {})


def save_lots(lots: dict[str, dict[str, Any]]) -> None:
    _save_json(LOTS_FILE, lots)


def set_lot(lot_id_or_keyword: str, duration_min: int = 0,
            aliases: list[str] | None = None,
            *, game: str = "", extension_lot_ids: list[str] | None = None,
            extension_games: list[str] | None = None,
            club_mode: bool | None = None,
            manual_review: bool | None = None,
            lot_type: str | None = None,
            is_extension: bool | None = None,
            game_key: str | None = None,
            kind: str | None = None) -> list[str]:
    """Создаёт или обновляет лот.

    Если duration_min == 0 — длительность будет браться из описания лота
    (#Hours: / #Time: / парсинг текста) при выдаче.

    is_extension=True — лот-продление: на FunPay по умолчанию неактивен,
    активируется только когда покупатель напишет !продлить.

    game_key — slug игры из games.json (новое). kind ∈ {main, ext} — тип лота
    внутри игры (новое). Старые `game` (строка-имя) и `is_extension` сохранены
    для обратной совместимости.
    """
    warnings: list[str] = []
    if aliases is None:
        aliases = []
    with _lock:
        lots = list_lots()
        existing = lots.get(str(lot_id_or_keyword), {})

        # kind: 'ext' если is_extension=True, иначе 'main'. Если kind передан явно — он побеждает.
        if kind is None:
            effective_kind = "ext" if is_extension else (
                "ext" if existing.get("is_extension") else "main"
            )
        else:
            effective_kind = str(kind)

        lots[str(lot_id_or_keyword)] = {
            "duration_min": int(duration_min or 0),
            "aliases": aliases,
            "game": game or existing.get("game", ""),
            "game_key": (game_key
                         if game_key is not None
                         else existing.get("game_key", "")),
            "kind": effective_kind,
            "extension_lot_ids": extension_lot_ids
                or existing.get("extension_lot_ids", []),
            "extension_games": (extension_games
                                if extension_games is not None
                                else existing.get("extension_games", [])),
            "club_mode": (existing.get("club_mode", False)
                          if club_mode is None else bool(club_mode)),
            "manual_review": (existing.get("manual_review", False)
                              if manual_review is None
                              else bool(manual_review)),
            "type": lot_type or existing.get("type", "rental"),
            "is_extension": (effective_kind == "ext"),
        }
        save_lots(lots)

        # Синхронизируем games.json: добавим/уберём lot_id в нужный список игры
        gk = lots[str(lot_id_or_keyword)].get("game_key") or ""
        if gk:
            games = list_games()
            g = games.get(gk)
            if g:
                key = "ext_lot_ids" if effective_kind == "ext" else "lot_ids"
                lst = [x for x in (g.get(key) or [])
                       if str(x) != str(lot_id_or_keyword)]
                lst.append(str(lot_id_or_keyword))
                g[key] = lst
                # Уберём из другого списка, если там был
                other_key = "lot_ids" if effective_kind == "ext" else "ext_lot_ids"
                g[other_key] = [x for x in (g.get(other_key) or [])
                                 if str(x) != str(lot_id_or_keyword)]
                games[gk] = g
                save_games(games)
    # Warn if RP lot has accounts with pool='rental'
    effective_type = lot_type or existing.get("type", "rental")
    if effective_type == "remoteplay":
        for a in aliases:
            acc = find_account(a)
            if acc and _account_pool(acc) == "rental":
                msg = (f"RP lot {lot_id_or_keyword} contains alias "
                       f"{a} with pool='rental'")
                LOGGER.warning("steam_rental: %s", msg)
                warnings.append(msg)
    return warnings


# ── Games (иерархия game → lots) ─────────────────────────────────────────
# Формат games.json:
#   { game_key: {name, subcategory_id, category_id, lot_ids (list[str]),
#                ext_lot_ids (list[str]), notes} }
# game_key — короткий slug (используется как ключ в lots[i].game_key)
def list_games() -> dict[str, dict[str, Any]]:
    return _load_json(GAMES_FILE, {})


def save_games(games: dict[str, dict[str, Any]]) -> None:
    _save_json(GAMES_FILE, games)


def get_game(game_key: str) -> dict[str, Any] | None:
    if not game_key:
        return None
    return list_games().get(str(game_key))


def _slugify_game(name: str) -> str:
    """Нормализует название игры в короткий key для games.json.
    Берёт первое слово, транслитерит в ascii, оставляет a-z0-9."""
    import re as _re
    if not name:
        return ""
    s = name.strip().lower()
    # Кириллица → латиница (минимальный маппинг для популярных игр).
    cyr2lat = {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
        "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
        "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
        "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    }
    out = []
    for ch in s:
        if ch in cyr2lat:
            out.append(cyr2lat[ch])
        elif ch.isalnum():
            out.append(ch)
        elif ch in (" ", "-", "_"):
            out.append("_")
    slug = "".join(out).strip("_")
    slug = _re.sub(r"_+", "_", slug)
    return slug[:32] or "game"


def set_game(game_key: str, name: str, *,
            subcategory_id: int | None = None,
            category_id: int | None = None,
            notes: str = "") -> str:
    """Создаёт или обновляет игру. Возвращает реально использованный key."""
    key = (game_key or "").strip() or _slugify_game(name)
    key = _slugify_game(key) or "game"
    with _lock:
        games = list_games()
        existing = games.get(key, {})
        games[key] = {
            "name": name or existing.get("name", key),
            "subcategory_id": (int(subcategory_id)
                              if subcategory_id is not None
                              else existing.get("subcategory_id")),
            "category_id": (int(category_id)
                            if category_id is not None
                            else existing.get("category_id")),
            "lot_ids": existing.get("lot_ids", []),
            "ext_lot_ids": existing.get("ext_lot_ids", []),
            "notes": notes or existing.get("notes", ""),
            "ts": _now(),
        }
        save_games(games)
    return key


def delete_game(game_key: str) -> bool:
    """Удаляет игру и снимает привязку с её лотов (лоты остаются)."""
    with _lock:
        games = list_games()
        if str(game_key) not in games:
            return False
        del games[str(game_key)]
        save_games(games)
        # Снимаем game_key с лотов этой игры
        lots = list_lots()
        for lid, lot in list(lots.items()):
            if lot.get("game_key") == str(game_key):
                lot["game_key"] = ""
        save_lots(lots)
    return True


def add_lot_to_game(game_key: str, lot_id: str, kind: str = "main") -> bool:
    """Привязывает лот к игре (kind: 'main' или 'ext')."""
    with _lock:
        games = list_games()
        g = games.get(str(game_key))
        if not g:
            return False
        key = "lot_ids" if kind == "main" else "ext_lot_ids"
        lst = list(g.get(key) or [])
        if str(lot_id) not in lst:
            lst.append(str(lot_id))
        g[key] = lst
        games[str(game_key)] = g
        save_games(games)
    return True


def remove_lot_from_game(game_key: str, lot_id: str, kind: str = "main") -> bool:
    with _lock:
        games = list_games()
        g = games.get(str(game_key))
        if not g:
            return False
        key = "lot_ids" if kind == "main" else "ext_lot_ids"
        lst = [x for x in (g.get(key) or []) if str(x) != str(lot_id)]
        g[key] = lst
        games[str(game_key)] = g
        save_games(games)
    return True


def delete_lot(lot_id_or_keyword: str,
               cardinal: "Cardinal | None" = None) -> dict[str, Any]:
    """Удаляет лот из конфигурации плагина.

    Помимо удаления записи из ``lots.json`` и сброса кэша активации,
    функция:
      1. Пытается деактивировать листинг на FunPay (``save_lot active=False``)
         — best-effort, чтобы лот реально перестал «включаться/выключаться»
         на сайте после удаления. Используется переданный ``cardinal`` или
         глобальный ``_CARDINAL_REF``.
      2. Чистит ссылки на удалённый лот в ``games.json`` (поля
         ``lot_ids`` и ``ext_lot_ids`` всех игр).
      3. Чистит ссылки на удалённый лот в ``extension_lot_ids`` остальных
         лотов (чтобы «материнские» лоты не указывали на несуществующий
         extension).

    Returns:
        dict с ключами:
          ``ok``           — bool, удалена ли запись из ``lots.json``;
          ``funpay_off``   — bool, деактивирован ли листинг на FunPay;
          ``funpay_tried`` — bool, пытались ли вообще трогать FunPay
                            (numeric ID + cardinal доступны);
          ``games_cleaned``    — int, у скольких игр почистили ссылки;
          ``parents_cleaned``  — int, у скольких других лотов почистили
                                 ``extension_lot_ids``.
    """
    key = str(lot_id_or_keyword)
    res: dict[str, Any] = {
        "ok": False,
        "funpay_off": False,
        "funpay_tried": False,
        "games_cleaned": 0,
        "parents_cleaned": 0,
    }

    # 1) Деактивация на FunPay (best-effort, ДО удаления из lots.json,
    #    чтобы листинг гарантированно остановился, даже если позже
    #    что-то сорвётся при чистке ссылок).
    c = cardinal if cardinal is not None else _CARDINAL_REF
    if c is not None and key.isdigit():
        res["funpay_tried"] = True
        try:
            res["funpay_off"] = bool(_set_funpay_lot_active(c, key, False))
        except Exception:
            LOGGER.debug("steam_rental: delete_lot funpay deactivate failed",
                         exc_info=True)
            res["funpay_off"] = False

    # 2) Удаление из lots.json + чистка ссылок в других лотах.
    with _lock:
        lots = list_lots()
        was_in_lots = key in lots
        if was_in_lots:
            del lots[key]
            # Чистим extension_lot_ids у всех остальных лотов.
            for other_key, other_val in lots.items():
                ext_ids = list(other_val.get("extension_lot_ids") or [])
                new_ext = [x for x in ext_ids if str(x) != key]
                if len(new_ext) != len(ext_ids):
                    other_val["extension_lot_ids"] = new_ext
                    res["parents_cleaned"] += 1
            save_lots(lots)
        else:
            # Сирота: записи в lots.json нет, но extension_lot_ids
            # других лотов всё равно могут на неё ссылаться.
            parents_changed = False
            for other_key, other_val in lots.items():
                ext_ids = list(other_val.get("extension_lot_ids") or [])
                new_ext = [x for x in ext_ids if str(x) != key]
                if len(new_ext) != len(ext_ids):
                    other_val["extension_lot_ids"] = new_ext
                    res["parents_cleaned"] += 1
                    parents_changed = True
            if parents_changed:
                save_lots(lots)

        # 3) Чистка ссылок в games.json (всегда, даже для сирот).
        games = list_games()
        games_changed = False
        for gkey, gval in games.items():
            changed_here = False
            for fld in ("lot_ids", "ext_lot_ids"):
                ids = list(gval.get(fld) or [])
                new_ids = [x for x in ids if str(x) != key]
                if len(new_ids) != len(ids):
                    gval[fld] = new_ids
                    changed_here = True
            if changed_here:
                games_changed = True
                res["games_cleaned"] += 1
        if games_changed:
            save_games(games)

    # 4) Сброс кэша активации (если был).
    if key in _LOT_ACTIVATION_CACHE:
        _LOT_ACTIVATION_CACHE.pop(key, None)
        _save_lot_state()

    # ok=True если удалили запись или почистили хоть какие-то ссылки/FunPay.
    res["ok"] = bool(
        was_in_lots
        or res["games_cleaned"]
        or res["parents_cleaned"]
        or (res["funpay_tried"] and res["funpay_off"])
    )
    return res


def _match_lot(order_desc: str, lot_id: str | None) -> dict[str, Any] | None:
    """Legacy-фоллбэк: совпадение по lot_id или по ключевому слову."""
    lots = list_lots()
    if lot_id and str(lot_id) in lots:
        return {"key": str(lot_id), **lots[str(lot_id)]}
    desc_low = (order_desc or "").lower()
    for key, val in lots.items():
        if not key.isdigit() and key.lower() in desc_low:
            return {"key": key, **val}
    return None


def _match_lot_by_game(order_title: str) -> dict[str, Any] | None:
    """Матчит заказ по `Order.title` (полное название) к играм в games.json.
    Стратегия:
      1. Найти игру, у которой name входит в order_title.
      2. Из её main-лотов выбрать тот, у которого title/aliases
         подходит, и есть свободные аккаунты.
      3. Если ни один main-лот игры не подходит — фоллбэк на _match_lot.
    """
    if not order_title:
        return None
    title_low = order_title.lower()
    games = list_games()
    if not games:
        return None
    lots = list_lots()

    # Соберём кандидатов: (игра, длина совпадения имени)
    cands: list[tuple[dict, dict, int]] = []
    for gkey, g in games.items():
        gname = (g.get("name") or "").strip()
        if not gname:
            continue
        gn_low = gname.lower()
        if gn_low in title_low:
            cands.append((g, lots, len(gn_low)))
        else:
            # Подстрочный в обратную сторону: "cs 2" в "Counter-Strike 2"
            for token in re.findall(r"[\w\-]{3,}", gn_low):
                if token in title_low:
                    cands.append((g, lots, len(token)))
                    break
    if not cands:
        return None

    # Сортируем по длине совпадения (самые длинные — самые точные)
    cands.sort(key=lambda x: -x[2])

    # Берём первую игру и ищем подходящий main-лот
    for g, _, _ in cands:
        for lot_id in g.get("lot_ids") or []:
            lot = lots.get(str(lot_id))
            if not lot:
                continue
            # lot должен быть main, не ext
            if (lot.get("kind") or "main") != "main":
                continue
            # Есть ли свободные аккаунты (учитывая гибридный пул)?
            pool = _combined_lot_pool(lot)
            if _count_free_accounts(pool) <= 0:
                continue
            return {"key": str(lot_id), **lot}

    # v2.23.1: если все main-лоты матченной игры существуют, но
    # свободных аккаунтов нет — всё равно возвращаем ПЕРВЫЙ main-лот.
    # Это нужно, чтобы _handler_new_order дошёл до блока `no_accounts`
    # и отправил покупателю шаблон «все аккаунты заняты, ближайший
    # освободится через ...» вместо «лот None НЕ настроен в плагине».
    # Без этого: два покупателя покупают два лота одной игры одновременно,
    # первый получает последний аккаунт, второй получает «лот не настроен»
    # потому что _match_lot_by_game возвращала None при пустом пуле.
    for g, _, _ in cands:
        for lot_id in g.get("lot_ids") or []:
            lot = lots.get(str(lot_id))
            if not lot:
                continue
            if (lot.get("kind") or "main") != "main":
                continue
            return {"key": str(lot_id), **lot}

    return None


def _combined_lot_pool(lot: dict[str, Any]) -> list[str]:
    """Гибридный пул алиасов лота:
       1) per-game `global_aliases` из games.json (ручной список на уровне игры)
       2) все аккаунты, у которых `acc.game_key == lot.game_key` (привязка
          аккаунт↔игра — добавляется через ⚙ TG-меню «🎮 Игра»)
       3) `aliases` самого лота (legacy: ручной список на уровне лота)
    Дедупликация сохраняет порядок (приоритет game-pool над lot-pool).
    """
    seen: set[str] = set()
    out: list[str] = []
    gkey = (lot.get("game_key") or "").strip()
    if gkey:
        # 1) global_aliases на уровне игры (старое поле, ручной список)
        g = get_game(gkey)
        if g:
            for a in g.get("global_aliases") or []:
                al = str(a).lower()
                if al not in seen:
                    seen.add(al)
                    out.append(str(a))
        # 2) все аккаунты с тем же game_key — это и есть «привязка
        # аккаунта к игре». Один раз поставил game_key на акк — он
        # автоматически доступен ВО ВСЕХ лотах этой игры.
        try:
            for a in list_accounts():
                if (a.get("game_key") or "").strip().lower() == gkey.lower():
                    al_v = str(a.get("alias") or "")
                    if not al_v:
                        continue
                    al_lc = al_v.lower()
                    if al_lc not in seen:
                        seen.add(al_lc)
                        out.append(al_v)
        except Exception:
            LOGGER.debug(
                "steam_rental: combined_lot_pool game_key scan failed",
                exc_info=True)
    # 3) per-lot aliases (legacy / точечная привязка)
    for a in lot.get("aliases") or []:
        al = str(a).lower()
        if al not in seen:
            seen.add(al)
            out.append(str(a))
    return out


def _is_extension_lot(lot_id: str | None) -> dict[str, Any] | None:
    """Проверяет, является ли lot_id extension-лотом.

    Считается extension'ом, если:
      1. Сам лот помечен флагом is_extension=True (новый wizard-механизм).
      2. lot_id присутствует в extension_lot_ids какого-то «материнского»
         лота (старый механизм).
    """
    if not lot_id:
        return None
    lots = list_lots()
    # 1) Новый механизм: is_extension у самого лота
    direct = lots.get(str(lot_id))
    if direct is not None and direct.get("is_extension"):
        return {"key": str(lot_id), **direct}
    # 2) Старый механизм: lot_id в extension_lot_ids чужого лота
    for key, val in lots.items():
        ext_ids = val.get("extension_lot_ids") or []
        if str(lot_id) in [str(x) for x in ext_ids]:
            return {"key": key, **val}
    return None


def _extension_target_games(parent_lot: dict[str, Any]) -> list[str]:
    """Список игр, на которые распространяется продление через parent_lot.

    Если у лота задано поле extension_games — возвращаем его (нормализованным).
    Иначе — возвращаем [game] родительского лота как fallback.
    Пустой список означает «продлевать любую активную аренду покупателя».
    """
    games = parent_lot.get("extension_games") or []
    norm = [(g or "").strip() for g in games if (g or "").strip()]
    if norm:
        return norm
    parent_game = (parent_lot.get("game") or "").strip()
    if parent_game:
        return [parent_game]
    return []


def _find_extension_lot_for_alias(alias: str) -> str | None:
    """Подбирает наиболее подходящий extension-лот для аренды аккаунта alias.

    Логика (от строгого к нестрогому):
      1. Жёсткая привязка legacy: alias в `parent.aliases` И ext_key
         в `parent.extension_lot_ids` (старый wizard, до game_key).
      2. NEW: совпадение по game_key — `acc.game_key` (или game_key
         любого лота, в чьём combined-пуле находится alias) совпадает с
         `ext_lot.game_key`. Двойная проверка через
         `games.json[gk].ext_lot_ids` — на случай если ext-лот добавлен
         через карточку игры, но у самого лота `game_key` ещё не
         подхватился (legacy записи).
      3. По имени игры (legacy): `ext.extension_games` пересекается с
         `acc.game` ИЛИ `ext.game` равен `acc.game`.
      4. Единственный extension-лот в системе → возвращаем его (нет
         неоднозначности, behave like single-game setup).
      5. None — иначе. Прямой fallback на «любой ext-лот» убран
         специально: при многолотовой настройке он отдавал ссылку на
         чужую игру (см. v2.12.2 changelog).
    """
    acc = find_account(alias) or {}
    acc_game = (_get_game_for_alias(alias) or "").strip().lower()
    acc_gkey = (acc.get("game_key") or "").strip()
    lots = list_lots()
    # Если у самого аккаунта game_key не выставлен — выводим из лота,
    # в чьём combined-пуле находится alias. Это покрывает случай
    # «аккаунт привязан к игре только через game_key лота, но у самого
    # acc поле пустое» (старые аккаунты до game_key-flow).
    if not acc_gkey:
        for _k, _v in lots.items():
            try:
                pool = _combined_lot_pool(_v)
            except Exception:
                pool = list(_v.get("aliases") or [])
            if alias in pool:
                gk = (_v.get("game_key") or "").strip()
                if gk:
                    acc_gkey = gk
                    break
    acc_gkey_lc = acc_gkey.lower()

    # Кандидаты — только числовые ID и is_extension=True
    candidates: list[tuple[str, dict[str, Any]]] = [
        (k, v) for k, v in lots.items()
        if k.isdigit() and v.get("is_extension")
    ]
    if not candidates:
        return None

    # 1. Жёсткая привязка legacy: alias в каком-либо «материнском» лоте,
    #    у которого этот extension-ID указан в extension_lot_ids.
    for ext_key, ext_val in candidates:
        for parent_key, parent_val in lots.items():
            ext_ids = [
                str(x) for x in (parent_val.get("extension_lot_ids") or [])
            ]
            if ext_key in ext_ids and alias in (parent_val.get("aliases") or []):
                return ext_key

    # 2. NEW: совпадение по game_key.
    if acc_gkey_lc:
        # 2a. Сначала ext-лоты с тем же game_key, что и аккаунт.
        for ext_key, ext_val in candidates:
            if (ext_val.get("game_key") or "").strip().lower() == acc_gkey_lc:
                return ext_key
        # 2b. Подстраховка: список ext-лотов из games.json[acc_gkey].
        #     Покрывает случай, когда сам ext-лот по каким-то причинам
        #     потерял game_key, но в games.json он всё ещё прописан.
        g = get_game(acc_gkey)
        if g:
            game_ext_ids = {str(x) for x in (g.get("ext_lot_ids") or [])}
            for ext_key, _ext_val in candidates:
                if ext_key in game_ext_ids:
                    return ext_key

    # 3. По имени игры (legacy ext.extension_games + lot.game).
    if acc_game:
        for ext_key, ext_val in candidates:
            ext_games = [
                (g or "").strip().lower()
                for g in (ext_val.get("extension_games") or [])
            ]
            if acc_game in ext_games:
                return ext_key
        for ext_key, ext_val in candidates:
            if (ext_val.get("game") or "").strip().lower() == acc_game:
                return ext_key

    # 4. Единственный ext-лот → можно отдать его (single-game setup).
    if len(candidates) == 1:
        return candidates[0][0]

    # 5. Многолотовая настройка без явной привязки → отказываемся гадать.
    LOGGER.warning(
        "steam_rental: !продлить — не нашёл подходящий extension-лот для "
        "alias=%s (game=%r, game_key=%r); ext-лотов в системе: %d, ни один "
        "не привязан к этой игре. Раньше плагин отдавал случайный ext-лот, "
        "что приводило к ссылке на чужую игру (фикс v2.12.2).",
        alias, acc_game, acc_gkey, len(candidates))
    return None


# ── Встроенная либа lot-activation (вместо внешнего lot_activation_common) ─
def _common_lib():
    """Возвращает «фейковый» модуль с теми же функциями, что были у внешней
    либы lot_activation_common. Используется чтобы остальной код не менять.
    Если рядом всё-таки лежит реальный lot_activation_common.py — берём его
    (на случай если кто-то его уже использует).
    """
    # Попробуем сначала реальную либу — если она есть.
    try:
        import lot_activation_common  # type: ignore
        return lot_activation_common
    except Exception:
        pass

    # Иначе — синтезируем shim.
    class _Shim:
        @staticmethod
        def get_funpay_account(cardinal):
            return _get_funpay_account_impl(cardinal)

        @staticmethod
        def apply_lot_active(cardinal, lot_id, active):
            return _apply_lot_active_impl(cardinal, int(lot_id), bool(active))

        @staticmethod
        def detect_category_id(cardinal, lot_id):
            acc = _get_funpay_account_impl(cardinal)
            if acc is None or not hasattr(acc, "get_lot_fields"):
                return None
            try:
                fields = acc.get_lot_fields(int(lot_id))
            except Exception:
                return None
            cat = getattr(getattr(fields, "subcategory", None),
                           "category", None)
            cid = getattr(cat, "id", None)
            return int(cid) if cid is not None else None

        @staticmethod
        def make_actions_logger(plugin_name, storage_dir):
            return _make_actions_logger_impl(plugin_name, storage_dir)

        @staticmethod
        def log_action(actions_logger, action, summary="", **extra):
            _log_action_impl(actions_logger, action, summary, **extra)

    return _Shim()


def _get_funpay_account_impl(cardinal):
    """Реализация get_funpay_account — берёт acc у cardinal."""
    if cardinal is None:
        return None
    acc = getattr(cardinal, "account", None)
    if acc is not None and (hasattr(acc, "save_lot")
                            or hasattr(acc, "save_offer")):
        return acc
    if hasattr(cardinal, "save_lot") or hasattr(cardinal, "save_offer"):
        return cardinal
    return None


def _apply_lot_active_impl(cardinal, lot_id: int, active: bool) -> bool:
    """Правильный путь FunPayAPI:
      1) get_lot_fields(lot_id)
      2) fields.active = bool (через свойство, НЕ через словарь!)
      3) save_lot(fields)
    + защита от amount=0.
    """
    acc = _get_funpay_account_impl(cardinal)
    if acc is None:
        raise RuntimeError(
            "FunPay API недоступен: ни cardinal.account, ни cardinal "
            "не предоставляют save_lot/save_offer")
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


# ── actions.log (встроенная версия) ─────────────────────────────────────────
_ACTIONS_LOGGERS_LOCAL: dict[str, logging.Logger] = {}
_ACTIONS_ICONS_LOCAL = {
    "lot_activated":   "✅ ЛОТ ВКЛ ",
    "lot_deactivated": "⛔ ЛОТ ВЫКЛ",
    "lot_skip_ext":    "⏭ ЛОТ EXT ",
    "lot_save_failed": "⚠ ЛОТ FAIL",
    "rental_start":    "📦 ВЫДАЧА  ",
    "rental_end":      "🏁 КОНЕЦ   ",
    "rental_extend":   "⏳ ПРОДЛ   ",
    "rental_refund":   "💸 ВОЗВРАТ ",
    "acc_freeze":      "❄️ ЗАМОР   ",
    "acc_unfreeze":    "🔥 РАЗМОР  ",
    "acc_vac_ban":     "🚨 VAC BAN ",
    "acc_login_ok":    "🔑 ЛОГИН OK",
    "acc_login_fail":  "❌ ЛОГИН FA",
    "acc_balance_low": "💰 БАЛАНС  ",
    "review_bonus":    "⭐ БОНУС   ",
    "review_penalty":  "👎 ШТРАФ   ",
    "reactivation":    "🔁 ПЕРЕАКТ ",
    "rule_applied":    "📐 ПРАВИЛО ",
    "sync_prices":     "💱 ЦЕНЫ    ",
    "stock_sync":      "📦 СКЛАД   ",
    "delivery":        "📨 ВЫДАЧА  ",
}


def _make_actions_logger_impl(plugin_name: str, storage_dir: str):
    """Создаёт RotatingFileHandler в storage_dir/actions.log. propagate=False."""
    if plugin_name in _ACTIONS_LOGGERS_LOCAL:
        return _ACTIONS_LOGGERS_LOCAL[plugin_name]
    try:
        os.makedirs(storage_dir, exist_ok=True)
        from logging.handlers import RotatingFileHandler
        log_path = os.path.join(storage_dir, "actions.log")
        lg = logging.getLogger(f"FPC.{plugin_name}.actions")
        lg.setLevel(logging.INFO)
        lg.propagate = False
        already = any(getattr(h, "_lot_actions_handler", False)
                       for h in lg.handlers)
        if not already:
            handler = RotatingFileHandler(
                log_path, maxBytes=2 * 1024 * 1024,
                backupCount=5, encoding="utf-8")
            handler.setFormatter(logging.Formatter(
                "%(asctime)s %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S"))
            handler._lot_actions_handler = True  # type: ignore[attr-defined]
            lg.addHandler(handler)
        _ACTIONS_LOGGERS_LOCAL[plugin_name] = lg
        return lg
    except Exception:
        LOGGER.debug("actions logger init failed for %s", plugin_name,
                     exc_info=True)
        return None


def _log_action_impl(actions_logger, action: str, summary: str = "",
                       **extra) -> None:
    if actions_logger is None:
        return
    icon = _ACTIONS_ICONS_LOCAL.get(action, f"• {action:10}")
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
        actions_logger.info(line)
    except Exception:
        LOGGER.debug("write actions.log failed", exc_info=True)


def _get_funpay_account(cardinal: "Cardinal"):
    """Возвращает объект FunPayAPI Account, на котором лежат методы лотов.

    В FunPayCardinal (sidor0912/FunPayCardinal) методы управления лотами
    (``get_lot_fields`` / ``save_lot``) находятся НЕ на самом ``cardinal``,
    а на ``cardinal.account`` — экземпляре ``FunPayAPI.Account``.
    Старые форки/обёртки иногда прокидывают эти методы прямо на ``cardinal``,
    поэтому пробуем оба варианта.

    Тонкая обёртка над lot_activation_common.get_funpay_account (если она
    доступна), иначе — legacy-реализация.
    """
    lib = _common_lib()
    if lib is not None:
        return lib.get_funpay_account(cardinal)
    if cardinal is None:
        return None
    acc = getattr(cardinal, "account", None)
    if acc is not None and (hasattr(acc, "save_lot")
                            or hasattr(acc, "save_offer")):
        return acc
    # Fallback: некоторые форки имеют методы прямо на cardinal.
    if hasattr(cardinal, "save_lot") or hasattr(cardinal, "save_offer"):
        return cardinal
    return None


def _apply_lot_active_flag(cardinal: "Cardinal", lot_id: int,
                            active: bool) -> bool:
    """Меняет флаг active у лота FunPay через get_lot_fields/save_lot.

    Тонкая обёртка над lot_activation_common.apply_lot_active. Если общей
    либы нет — выполняет ту же логику локально (legacy).
    """
    lib = _common_lib()
    if lib is not None:
        return lib.apply_lot_active(cardinal, int(lot_id), bool(active))

    # Legacy-фолбэк (точная копия общей либы).
    acc = _get_funpay_account(cardinal)
    if acc is None:
        raise RuntimeError(
            "FunPay API недоступен: ни cardinal.account, ни cardinal "
            "не предоставляют save_lot/save_offer")
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


def _set_funpay_lot_active(cardinal: "Cardinal", lot_id: str,
                            active: bool) -> bool:
    """Активирует/деактивирует лот на FunPay. Возвращает True при успехе."""
    if not str(lot_id).isdigit():
        return False
    try:
        _apply_lot_active_flag(cardinal, int(lot_id), bool(active))
        LOGGER.info("steam_rental: lot %s set_active=%s", lot_id, active)
        _LOT_ACTIVATION_CACHE[str(lot_id)] = {
            "active": bool(active),
            "ts": _now(),
            "result": "ok",
        }
        _save_lot_state()
        return True
    except Exception:
        LOGGER.warning(
            "steam_rental: set_lot_active(%s, %s) failed", lot_id, active,
            exc_info=True)
        _LOT_ACTIVATION_CACHE[str(lot_id)] = {
            "active": None,
            "ts": _now(),
            "result": "fail",
        }
        _save_lot_state()
        return False


def _cancel_ext_lot_deactivation(lot_id: Any) -> bool:
    """Отменяет (если есть) запланированную авто-деактивацию ext-лота.
    Вызывается после успешной оплаты extension-покупки и из самого
    шедулера перед заменой таймера. Возвращает True если таймер был
    отменён."""
    key = str(lot_id)
    with _ext_lot_timers_lock:
        t = _EXT_LOT_TIMERS.pop(key, None)
    if t is None:
        return False
    try:
        t.cancel()
    except Exception:
        pass
    return True


def _schedule_ext_lot_deactivation(cardinal: "Cardinal", lot_id: Any,
                                    ttl_minutes: int) -> None:
    """Через ttl_minutes минут выключает ext-лот на FunPay, если он всё
    ещё помечен как extension и не был выключен раньше (через оплату или
    повторный !продлить). Если для этого lot_id уже стоит таймер — он
    заменяется (повторный !продлить продлевает окно).
    """
    if ttl_minutes <= 0:
        return
    key = str(lot_id)

    def _fire() -> None:
        # Снимем себя из реестра (если ещё там) и проверим, что лот всё ещё
        # extension и не выключен другими путями.
        with _ext_lot_timers_lock:
            _EXT_LOT_TIMERS.pop(key, None)
        try:
            lots = list_lots()
            lot = lots.get(key)
            if not lot:
                LOGGER.info(
                    "steam_rental: ext-lot %s удалён из конфига до "
                    "истечения TTL — деактивация не нужна", key)
                return
            if not lot.get("is_extension"):
                LOGGER.info(
                    "steam_rental: lot %s больше не extension — TTL-"
                    "деактивация пропущена", key)
                return
            cache = _LOT_ACTIVATION_CACHE.get(key) or {}
            if cache.get("result") == "ok" and cache.get("active") is False:
                LOGGER.info(
                    "steam_rental: ext-lot %s уже деактивирован — TTL "
                    "пропущен", key)
                return
            if _set_funpay_lot_active(cardinal, key, False):
                LOGGER.info(
                    "steam_rental: ext-lot %s авто-деактивирован "
                    "после TTL %d мин (не оплатили)", key, ttl_minutes)
                _log_action(
                    "lot_skip_ext",
                    f"Авто-деактивация ext-лота {key}: не оплатили "
                    f"за {ttl_minutes} мин",
                    lot_id=key, ttl_minutes=ttl_minutes)
                _notify_tg(
                    cardinal,
                    f"⏰ <b>Steam Rental</b>: ext-лот "
                    f"<code>{key}</code> авто-деактивирован — "
                    f"покупатель не оплатил за {ttl_minutes} мин.")
            else:
                LOGGER.warning(
                    "steam_rental: TTL-деактивация ext-лота %s "
                    "не удалась (FunPay API)", key)
        except Exception:
            LOGGER.error(
                "steam_rental: ext-lot TTL-deactivation crashed for %s",
                key, exc_info=True)

    # Заменяем предыдущий таймер, если он был — повторный !продлить
    # «обновляет» окно.
    _cancel_ext_lot_deactivation(key)
    timer = threading.Timer(ttl_minutes * 60, _fire)
    timer.daemon = True
    with _ext_lot_timers_lock:
        _EXT_LOT_TIMERS[key] = timer
    timer.start()
    LOGGER.info(
        "steam_rental: ext-lot %s запланирован к авто-деактивации "
        "через %d мин", key, ttl_minutes)


def _account_pool(acc: dict) -> str:
    """Returns the account's pool value: 'rental', 'remoteplay', or 'both'.
    Defaults to 'both' for backward compatibility with existing accounts."""
    pool = acc.get("pool", "both")
    if pool not in ("rental", "remoteplay", "both"):
        return "both"
    return pool


def _pick_free_alias(
        aliases: list[str],
        exclude_other_reserved_for_buyer: int | None = None) -> str | None:
    """Возвращает alias первого свободного (без аренды и не замороженного)
    аккаунта.

    Если задан `exclude_other_reserved_for_buyer` — исключаем aliases,
    на которые есть активная (непротухшая) бронь ДРУГОГО покупателя:
    шаблон `reserve_ok` обещает «случайный аккаунт выдан НЕ будет», и
    эта эксклюзивность защищает обещание для конкурентных заказов.
    Брони этого же покупателя пропускаем — он сам их и оплачивает.
    """
    reserved_other: set[str] = set()
    if exclude_other_reserved_for_buyer is not None:
        try:
            _purge_expired_reservations()
            data = _load_reservations()
            now_ts = _now()
            for a, r in (data.get("items") or {}).items():
                if int(r.get("expires_ts") or 0) <= now_ts:
                    continue
                if int(r.get("buyer_id", -1)) != int(
                        exclude_other_reserved_for_buyer):
                    reserved_other.add(a)
        except Exception:
            LOGGER.debug("steam_rental: pick_free_alias reservation filter "
                         "failed", exc_info=True)

    for alias in aliases:
        if alias in reserved_other:
            continue
        acc = find_account(alias)
        if not acc:
            continue
        if acc.get("frozen"):
            continue
        if acc.get("rental"):
            continue
        if _account_pool(acc) == "remoteplay":
            continue
        if find_active_rp_session_by_alias(alias):
            continue
        return alias
    return None


def _count_free_accounts(aliases: list[str]) -> int:
    """Считает свободные (не frozen, без аренды) аккаунты в пуле."""
    count = 0
    for alias in aliases:
        acc = find_account(alias)
        if not acc:
            continue
        if acc.get("frozen"):
            continue
        if acc.get("rental"):
            continue
        if _account_pool(acc) == "remoteplay":
            continue
        if find_active_rp_session_by_alias(alias):
            continue
        count += 1
    return count


def _next_free_at_for_pool(aliases: list[str]) -> int | None:
    """Возвращает минимальный expires_at среди активных аренд в пуле.

    Используется в шаблоне `no_accounts`, чтобы покупатель видел, через
    сколько освободится ближайший аккаунт (важно при гонке двух
    одновременных оплат на пул из одного аккаунта). Учитываются ТОЛЬКО
    аккаунты, которые сами по себе освободятся по таймеру:
      * frozen — НЕ учитываем (не освободятся без вмешательства);
      * без `rental` — пропускаем (если они есть, текст «нет свободных»
        вообще не вызывается);
      * Remote Play (sessions.json) — берём session.expires_at.
    Возвращает None, если в пуле некого ждать."""
    now = _now()
    candidates: list[int] = []
    for alias in aliases:
        acc = find_account(alias)
        if not acc:
            continue
        if acc.get("frozen"):
            continue
        # Обычная аренда (deliver_account)
        rental = acc.get("rental") or {}
        exp = int(rental.get("expires_at") or 0)
        if exp > now:
            candidates.append(exp)
            continue
        # Remote Play сессия (deliver_remoteplay)
        try:
            sess = find_active_rp_session_by_alias(alias)
        except Exception:
            sess = None
        if sess:
            sess_exp = int((sess or {}).get("expires_at") or 0)
            if sess_exp > now:
                candidates.append(sess_exp)
    if not candidates:
        return None
    return min(candidates)


def _get_game_for_alias(alias: str) -> str:
    """Определяет название игры для аккаунта.

    Приоритет:
      1. acc.game (display string), если задан;
      2. games.json[acc.game_key].name;
      3. lot.game (по любому лоту, в чьём пуле есть alias);
      4. games.json[lot.game_key].name (по любому такому лоту);
      5. "" если ничего не нашли.
    """
    acc = find_account(alias)
    if acc:
        if acc.get("game"):
            return acc["game"]
        ag_key = (acc.get("game_key") or "").strip()
        if ag_key:
            g = get_game(ag_key)
            if g and g.get("name"):
                return g["name"]
    lots = list_lots()
    for _key, val in lots.items():
        if alias in val.get("aliases", []):
            if val.get("game"):
                return val["game"]
            lk = (val.get("game_key") or "").strip()
            if lk:
                g = get_game(lk)
                if g and g.get("name"):
                    return g["name"]
    return ""


def _resolve_post_delivery_template(alias: str) -> tuple[str, str]:
    """Возвращает (template_text, source) для post_delivery c приоритетом:
        1. acc["post_delivery"]            → source="account"
        2. lot["post_delivery"] (по alias) → source="lot"
        3. глобальный шаблон               → source="global"
    Если все пустые — ("", "none").
    Пустую строку из аккаунта/лота интерпретируем как «не слать»
    (явное отключение для этого скоупа), но только если это явно
    установленная пустая строка — отсутствующий ключ означает
    «использовать fallback».
    """
    # 1) per-account
    acc = find_account(alias)
    if acc is not None and "post_delivery" in acc:
        tpl = acc.get("post_delivery")
        if tpl is None:
            tpl = ""
        return (str(tpl), "account")

    # 2) per-lot — берём первый лот, содержащий этот alias
    lots = list_lots()
    for _key, val in lots.items():
        if alias in (val.get("aliases") or []):
            if "post_delivery" in val:
                tpl = val.get("post_delivery")
                if tpl is None:
                    tpl = ""
                return (str(tpl), "lot")

    # 3) глобальный шаблон
    cfg = get_config()
    templates = cfg.get("templates") or {}
    tpl = templates.get("post_delivery")
    if tpl is None:
        tpl = _DEFAULT_TEMPLATES.get("post_delivery", "")
    return (str(tpl or ""), "global")


def _find_buyer_active_rental(buyer_id: int, game: str | None = None) -> dict[str, Any] | None:
    """Находит активную аренду покупателя (опционально по game)."""
    for acc in list_accounts():
        rental = acc.get("rental")
        if not rental:
            continue
        if int(rental.get("buyer_id", -1)) != int(buyer_id):
            continue
        if rental.get("expires_at", 0) <= _now():
            continue
        if game and _get_game_for_alias(acc["alias"]) != game:
            continue
        return acc
    return None


def _find_buyer_last_rental(buyer_id: int) -> dict[str, Any] | None:
    """Находит последнюю (даже истёкшую) аренду покупателя."""
    best = None
    best_ts = 0
    for acc in list_accounts():
        rental = acc.get("rental")
        if not rental:
            continue
        if int(rental.get("buyer_id", -1)) != int(buyer_id):
            continue
        started = rental.get("started_at", 0)
        if started > best_ts:
            best = acc
            best_ts = started
    return best


def _extend_rental(alias: str, extra_minutes: int,
                    reason: str = "manual") -> int:
    with _lock:
        acc = find_account(alias)
        if not acc or not acc.get("rental"):
            return 0
        rental = acc["rental"]
        old_expires = rental["expires_at"]
        new_expires = max(old_expires, _now()) + extra_minutes * 60
        rental["expires_at"] = new_expires
        rental["reminded"] = False
        rental["reminded_2"] = False
        upsert_account(acc)
    _log_rental_event("extend", alias,
                      buyer_username=rental.get("buyer_username"),
                      buyer_id=rental.get("buyer_id"),
                      duration_min=extra_minutes, reason=reason)
    _log_action("rental_extend",
                f"Продление {alias} на {extra_minutes} мин ({reason})",
                alias=alias,
                buyer=rental.get("buyer_username"),
                extra_minutes=extra_minutes, reason=reason)
    _bump_acc_stat(alias, inc_ext_count=1,
                   add_total_minutes=float(extra_minutes),
                   set_last_extended_at=_now())
    _metric_inc("rentals_extended_total")
    return new_expires


def _rent_account_to_buyer(acc: dict[str, Any], *, buyer_id: int,
                             buyer_username: str, chat_id: int | str | None,
                             order_id: str,
                             duration_min: int) -> None:
    """Записать активную аренду на аккаунт (без парсинга/отправки в FunPay)."""
    with _lock:
        started = _now()
        expires = started + duration_min * 60
        acc["rental"] = {
            "buyer_id": int(buyer_id),
            "buyer_username": str(buyer_username),
            "order_id": str(order_id),
            "chat_id": chat_id,
            "started_at": started,
            "expires_at": expires,
            "duration_min": duration_min,
            "reminded": False,
            "reminded_2": False,
            "review_bonus_minutes": 0,
        }
        upsert_account(acc)
    _log_action("rental_start",
                f"Ручная выдача {acc.get('alias', '?')}",
                alias=acc.get("alias"), buyer=buyer_username,
                buyer_id=buyer_id, order_id=order_id,
                manual=True)


def _finish_rental(acc: dict[str, Any], *, reason: str = "manual_finish",
                   send_message: bool = True) -> None:
    """Сменить пароль и очистить rental (как при естественном завершении).

    Тяжёлая операция — меняет пароль через Steam. Если send_message=False
    (ручной режим), НЕ отправляет уведомление в FunPay чат покупателя.
    """
    alias = acc.get("alias", "?")
    r = acc.get("rental") or {}
    try:
        cfg = get_config()
        if cfg.get("change_password_on_expire", True):
            sess = SteamSession(
                account_name=acc.get("account_name", ""),
                password=acc.get("password", ""),
                shared_secret=acc.get("shared_secret"),
                identity_secret=acc.get("identity_secret"),
                steamid=acc.get("steamid"),
            )
            try:
                sess.login()
                new_pw = _gen_password()
                sess.change_password(new_pw)
                with _lock:
                    acc["password"] = new_pw
            except Exception as exc:
                LOGGER.warning(
                    "steam_rental: _finish_rental: change_password failed "
                    "for %s: %s", alias, exc)
    except Exception:
        LOGGER.debug("steam_rental: _finish_rental: change_password skipped",
                     exc_info=True)
    with _lock:
        acc.pop("rental", None)
        acc["last_finished_at"] = _now()
        upsert_account(acc)
    _log_action("rental_end",
                f"Ручное завершение {alias} ({reason})",
                alias=alias, buyer=r.get("buyer_username"),
                buyer_id=r.get("buyer_id"), manual=True)


def _cancel_rental(acc: dict[str, Any], *, reason: str = "manual_cancel",
                   send_message: bool = True) -> None:
    """Просто очистить rental (без смены пароля)."""
    alias = acc.get("alias", "?")
    r = acc.get("rental") or {}
    with _lock:
        acc.pop("rental", None)
        acc["last_cancelled_at"] = _now()
        upsert_account(acc)
    _log_action("rental_cancel",
                f"Ручная отмена {alias} ({reason})",
                alias=alias, buyer=r.get("buyer_username"),
                buyer_id=r.get("buyer_id"), manual=True)


# ── Авто-деактивация лотов FunPay ────────────────────────────────────────────
def _update_lot_activation(cardinal: "Cardinal",
                            *, force: bool = False,
                            verbose: bool = False) -> dict[str, Any]:
    """Деактивирует лоты без свободных аккаунтов, активирует с наличием.

    Args:
        cardinal: объект FunPay Cardinal.
        force: игнорировать auto_deactivate_lots (запуск по кнопке).
        verbose: логировать каждый лот на уровне INFO.

    Returns:
        {
          "activated": N, "deactivated": N, "skipped": N, "failed": N,
          "total_lots": N, "numeric_lots": N, "ext_lots": N,
          "stopped_reason": str | None,  # причина раннего выхода
          "api_method": "save_lot" | "save_offer" | None,
          "failures": [{"lot": "...", "error": "..."}, ...],  # детали ошибок
        }
    """
    counters: dict[str, Any] = {
        "activated": 0, "deactivated": 0, "skipped": 0, "failed": 0,
        "total_lots": 0, "numeric_lots": 0, "ext_lots": 0,
        "stopped_reason": None,
        "api_method": None,
        "failures": [],
    }
    cfg = get_config()
    if not force and not cfg.get("auto_deactivate_lots"):
        LOGGER.debug(
            "steam_rental: _update_lot_activation skipped "
            "(auto_deactivate_lots=False)")
        counters["stopped_reason"] = "auto_deactivate_lots выключен в настройках"
        return counters

    if cardinal is None:
        LOGGER.warning("steam_rental: _update_lot_activation: cardinal=None")
        counters["stopped_reason"] = "cardinal=None (плагин не подцеплен к боту)"
        return counters

    # Определяем какой именно объект и метод доступны для управления лотами.
    # В FunPayCardinal методы лежат на cardinal.account (FunPayAPI Account),
    # а не на самом cardinal. Старый код искал их на cardinal и всегда падал.
    api_obj = _get_funpay_account(cardinal)
    api_method = None
    if api_obj is not None:
        if hasattr(api_obj, "save_lot"):
            api_method = "save_lot"
        elif hasattr(api_obj, "save_offer"):
            api_method = "save_offer"
    counters["api_method"] = api_method

    if api_obj is None or api_method is None:
        msg = ("cardinal.account не предоставляет save_lot/save_offer — "
               "FunPay API недоступен из плагина "
               "(проверь версию FunPayCardinal/FunPayAPI)")
        LOGGER.warning("steam_rental: %s", msg)
        counters["stopped_reason"] = msg
        return counters

    if not hasattr(api_obj, "get_lot_fields"):
        msg = ("cardinal.account.get_lot_fields отсутствует — "
               "нельзя получить поля лота для смены флага active")
        LOGGER.warning("steam_rental: %s", msg)
        counters["stopped_reason"] = msg
        return counters

    lots = list_lots()
    counters["total_lots"] = len(lots)
    counters["numeric_lots"] = sum(1 for k in lots if k.isdigit())
    counters["ext_lots"] = sum(1 for v in lots.values()
                                if v.get("is_extension"))
    if not lots:
        counters["stopped_reason"] = "лотов в базе нет"
        return counters

    now_ts = _now()
    for key, val in lots.items():
        if not key.isdigit():
            counters["skipped"] += 1
            continue
        # Extension-лоты не управляются авто-активацией: они активируются
        # вручную при команде покупателя !продлить и деактивируются после
        # успешной покупки extension.
        if val.get("is_extension"):
            counters["skipped"] += 1
            continue
        lot_id = int(key)
        # Учитываем как per-lot aliases, так и глобальный пул игры
        # (game-level: global_aliases + accounts.where(game_key=lot.game_key)).
        aliases = _combined_lot_pool(val)
        free = _count_free_accounts(aliases)
        want_active = free > 0

        # ── ОПТИМИЗАЦИЯ: пропускаем лот если его состояние уже совпадает
        # с желаемым. Раньше плагин каждые 30с дёргал save_lot для каждого
        # лота — нагружал FunPay (HTTP-запросы + риск 429) и спамил
        # actions.log. Теперь зовём API только при реальной смене состояния.
        # При force=True (кнопка «Переактивация») всё равно прогоняем.
        if not force:
            prev = _LOT_ACTIVATION_CACHE.get(str(key)) or {}
            if (prev.get("result") == "ok"
                    and prev.get("active") == bool(want_active)):
                counters["skipped"] += 1
                continue

        try:
            # Корректный путь FunPayAPI:
            #   1) acc.get_lot_fields(lot_id) — тянет поля формы + csrf_token
            #   2) меняем СВОЙСТВО fields.active (НЕ fields.fields["active"]!)
            #      потому что renew_fields() в save_lot() пере-записывает
            #      ключ "active" из self.active.
            #   3) acc.save_lot(fields) — POST lots/offerSave
            # save_lot ничего не возвращает: успех = отсутствие исключения.
            #
            # Важно: при amount == 0 LotFields принудительно ставит
            # active=False (внутренняя «защита»). У rental-лотов наличие
            # часто = 0 (управляется плагином), поэтому перед активацией
            # ставим amount=1, если он не auto_delivery.
            fields = api_obj.get_lot_fields(int(lot_id))
            if want_active:
                if (getattr(fields, "amount", None) in (None, 0)
                        and not getattr(fields, "auto_delivery", False)):
                    try:
                        fields.amount = 1
                    except Exception:
                        pass
                fields.active = True
            else:
                fields.active = False
            if api_method == "save_lot":
                api_obj.save_lot(fields)
            else:
                api_obj.save_offer(fields)
            ok = True
            prev_active = (_LOT_ACTIVATION_CACHE.get(str(key)) or {}).get(
                "active")
            # Обновляем кэш состояния (для отображения в /srental → 🎯 Лоты)
            _LOT_ACTIVATION_CACHE[str(key)] = {
                "active": bool(want_active),
                "ts": now_ts,
                "result": "ok",
            }
            # Пишем в actions.log только при ФАКТИЧЕСКОМ изменении состояния
            # (или при force — ручной переактивации). Раньше каждый тик
            # фонового чекера спамил «активирован» одни и те же лоты.
            state_changed = (prev_active != bool(want_active))
            if want_active:
                counters["activated"] += 1
                if state_changed or force:
                    _log_action("lot_activated",
                                f"Лот {key} активирован",
                                lot_id=key, free=free)
                if verbose:
                    LOGGER.info(
                        "steam_rental: лот %s активирован (%d свободных)",
                        key, free)
                else:
                    LOGGER.debug(
                        "steam_rental: лот %s активирован (%d свободных)",
                        key, free)
            else:
                counters["deactivated"] += 1
                if state_changed or force:
                    _log_action("lot_deactivated",
                                f"Лот {key} деактивирован — нет свободных",
                                lot_id=key, free=free)
                LOGGER.info(
                    "steam_rental: лот %s деактивирован (нет свободных)",
                    key)
        except Exception as e:
            counters["failed"] += 1
            counters["failures"].append({
                "lot": str(key),
                "error": f"{type(e).__name__}: {str(e)[:150]}",
            })
            _LOT_ACTIVATION_CACHE[str(key)] = {
                "active": None,
                "ts": now_ts,
                "result": "fail",
            }
            _log_action("lot_save_failed",
                        f"Не удалось сохранить лот {key}",
                        lot_id=key, want_active=want_active,
                        error=f"{type(e).__name__}: {str(e)[:120]}")
            LOGGER.warning(
                "steam_rental: save_lot(%s, active=%s) failed: %s",
                key, want_active, str(e)[:200], exc_info=True)
    _save_lot_state()
    return counters


# ── Доставка / снятие аренды ────────────────────────────────────────────────
def deliver_account(cardinal: "Cardinal", *, alias: str, duration_min: int,
                     order_id: str, buyer_username: str, buyer_id: int,
                     chat_id: int | str,
                     review_bonus_minutes: int | None = None) -> bool:
    cfg = get_config()
    with _lock:
        acc = find_account(alias)
        if not acc:
            return False
        if acc.get("rental"):
            return False
        if acc.get("frozen"):
            return False
        started = _now()
        expires = started + duration_min * 60
        acc["rental"] = {
            "buyer_id": int(buyer_id),
            "buyer_username": str(buyer_username),
            "order_id": str(order_id),
            "chat_id": int(chat_id) if isinstance(chat_id, str) and chat_id.isdigit()
                       else chat_id,
            "started_at": started,
            "expires_at": expires,
            "duration_min": duration_min,
            "reminded": False,
            "reminded_2": False,
            "review_bonus_minutes": review_bonus_minutes,
        }
        upsert_account(acc)

    game = _get_game_for_alias(alias)
    hours = duration_min / 60
    duration_str = _human_minutes(duration_min)

    text = _render_template(
        "issue",
        buyer_id=buyer_id,
        login=acc["account_name"],
        password=acc["password"],
        game=game or "—",
        duration=duration_str,
        hours=f"{hours:.0f}" if hours == int(hours) else f"{hours:.1f}",
        minutes=str(duration_min),
        new_expires=_fmt_ts(expires),
    )

    try:
        cardinal.send_message(chat_id, text, chat_name=buyer_username,
                              interlocutor_id=buyer_id, watermark=False)
        # ── Post-delivery message ─────────────────────────────────────
        # v2.22.3: разделяем гейтинг по источнику текста.
        # • Per-account / per-lot override (acc["post_delivery"] /
        #   lot["post_delivery"]) — это ЯВНОЕ намерение продавца, шлём
        #   всегда, когда текст не пустой. Пустая строка ("") у
        #   override = «выключено для этого акка/лота» (тоже явное).
        # • Глобальный шаблон — гейтится опцией
        #   post_delivery_message_enabled (как и раньше). Это нужно,
        #   чтобы по дефолту никто не получал хардкод-Rockstar-текст.
        # Раньше глобальный флаг гейтил ВСЁ — потому per-account
        # override молча игнорился, и продавцы зря настраивали.
        pd_tpl, pd_source = _resolve_post_delivery_template(alias)
        pd_should_send = False
        if pd_source in ("account", "lot"):
            pd_should_send = bool(pd_tpl and pd_tpl.strip())
        elif pd_source == "global":
            pd_should_send = (
                cfg.get("post_delivery_message_enabled", False)
                and bool(pd_tpl and pd_tpl.strip())
            )
        if pd_should_send:
            delay = cfg.get("post_delivery_delay_seconds", 3)
            if delay > 0:
                time.sleep(delay)
            # Подставляем плейсхолдеры в найденный шаблон
            post_text = pd_tpl
            for _k, _v in {
                "login": acc["account_name"],
                "password": acc["password"],
                "game": game or "\u2014",
                "duration": duration_str,
                "hours": f"{hours:.0f}" if hours == int(hours) else f"{hours:.1f}",
                "minutes": str(duration_min),
            }.items():
                post_text = post_text.replace("{" + _k + "}", str(_v))
            try:
                cardinal.send_message(chat_id, post_text, chat_name=buyer_username,
                                      interlocutor_id=buyer_id, watermark=False)
                LOGGER.info(
                    "steam_rental: post_delivery sent for %s (source=%s)",
                    alias, pd_source)
            except Exception:
                LOGGER.debug("steam_rental: post_delivery message failed", exc_info=True)
        else:
            LOGGER.info(
                "steam_rental: post_delivery skipped for %s "
                "(source=%s, empty=%s, global_flag=%s)",
                alias, pd_source, not bool(pd_tpl and pd_tpl.strip()),
                cfg.get("post_delivery_message_enabled", False))
        LOGGER.info("steam_rental: выдан аккаунт %s для %s (order=%s) на %d мин",
                    alias, buyer_username, order_id, duration_min)
        # v5: операторские кнопки под уведомлением + per-account stats
        _last_price_v = getattr(cardinal, "_sr_last_price", None)
        _bump_acc_stat(
            alias,
            inc_rentals_count=1,
            inc_delivered_count=1,
            add_total_minutes=float(duration_min),
            add_total_revenue=float(_last_price_v or 0),
            set_last_delivered_at=_now(),
            set_last_buyer_id=int(buyer_id) if buyer_id else None,
            set_last_buyer_username=str(buyer_username or ""),
            set_last_order_id=str(order_id or ""))
        _metric_inc("rentals_delivered_total")
        _notify_tg(cardinal,
                   f"📦 <b>Steam Rental</b>: выдан аккаунт <code>{alias}</code> "
                   f"покупателю <b>{buyer_username}</b> (заказ #{order_id}) "
                   f"на {duration_str}. До {_fmt_ts(expires)} МСК."
                   + (" 🧪 <b>ТЕСТ</b> (выдача-проверка, Steam не задействован)"
                      if acc.get("test") else ""),
                   op_alias=alias)
        _update_lot_activation(cardinal)
        _log_rental_event("start", alias,
                          buyer_username=buyer_username, buyer_id=buyer_id,
                          order_id=order_id, game=game or "",
                          duration_min=duration_min,
                          amount=_last_price_v)
        _log_action("rental_start",
                    f"Выдан {alias} → {buyer_username} на {duration_str}",
                    alias=alias, buyer=buyer_username, buyer_id=buyer_id,
                    order_id=order_id, game=game or "",
                    duration_min=duration_min, amount=_last_price_v)
        return True
    except Exception:
        LOGGER.error("steam_rental: не удалось отправить креды в чат %s",
                     chat_id, exc_info=True)
        return False


def end_rental(cardinal: "Cardinal | None", alias: str, *,
               reason: str = "expire") -> dict[str, Any]:
    cfg = get_config()
    result: dict[str, Any] = {"alias": alias, "revoked": None, "changed": None,
                              "errors": []}
    # ── Защита от параллельных end_rental по одному alias ─────────────
    # Покрывает все пути (TG-кнопки, шедулер expire, recovery_expired,
    # operator_switch). Если уже идёт — выходим без действий, отдаём
    # маркер в errors чтобы вызывающая сторона не считала это ошибкой.
    if not _try_mark_stopping(alias):
        result["errors"].append("already_stopping")
        LOGGER.info(
            "steam_rental: end_rental(%s, %s) пропущен — уже идёт "
            "параллельный end_rental",
            alias, reason)
        return result
    try:
        return _end_rental_impl(cardinal, alias, reason, cfg, result)
    finally:
        _unmark_stopping(alias)


def _end_rental_impl(cardinal: "Cardinal | None", alias: str, reason: str,
                     cfg: dict[str, Any],
                     result: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        acc = find_account(alias)
        if not acc:
            result["errors"].append("account not found")
            return result
        rental = acc.get("rental")

    # Тестовый аккаунт: в Steam не ходим (нет данных) — просто снимаем аренду.
    if acc.get("test"):
        with _lock:
            acc = find_account(alias) or acc
            if acc.get("rental"):
                acc.pop("rental", None)
                upsert_account(acc)
        result["changed"] = None
        result["errors"].append("test account: steam ops skipped")
        if cardinal is not None:
            _update_lot_activation(cardinal)
        if rental:
            _log_rental_event("end", alias,
                              buyer_username=rental.get("buyer_username"),
                              buyer_id=rental.get("buyer_id"),
                              order_id=rental.get("order_id"),
                              game=_get_game_for_alias(alias) or "",
                              duration_min=rental.get("duration_min", 0),
                              reason=reason)
        return result

    try:
        sess = SteamSession(
            account_name=acc["account_name"],
            password=acc["password"],
            shared_secret=acc["shared_secret"],
            identity_secret=acc["identity_secret"],
            steamid=acc.get("steamid"),
        )
        sess.login()
        _track_login_result(alias, True)
        with _lock:
            acc = find_account(alias) or acc
            acc["steamid"] = sess.steamid
            upsert_account(acc)
    except Exception as exc:
        _track_login_result(alias, False)
        result["errors"].append(f"login: {exc}")
        LOGGER.error("steam_rental: end_rental login failed for %s", alias,
                     exc_info=True)
        sess = None

    # ── Revoke сессий ──────────────────────────────────────────────────
    # Управляется флагом `revoke_sessions_on_expire` (⚙ Настройки →
    # 🔒 Безопасность). По умолчанию ВЫКЛ — раньше блок был хардкодом
    # отключён, теперь это явная опция. Также есть отдельная ручная
    # кнопка `📤 Отозвать сессии` на карточке аккаунта — она работает
    # независимо от этого флага.
    if sess is not None and cfg.get("revoke_sessions_on_expire", False):
        try:
            ok_rv = sess.revoke_all_other_sessions()
            result["revoked"] = bool(ok_rv)
            LOGGER.info(
                "steam_rental: end_rental revoke_sessions for %s → %s",
                alias, ok_rv)
        except Exception as exc:
            result["revoked"] = False
            result["errors"].append(f"revoke_sessions: {exc}")
            LOGGER.warning(
                "steam_rental: revoke_sessions failed for %s: %s",
                alias, exc)

    if sess is not None and cfg.get("change_password_on_expire", True):
        new_pw = _gen_password()
        try:
            old_pw = acc.get("password", "")
            sess.change_password(new_pw)
            _push_previous_password(alias, old_pw)
            with _lock:
                acc = find_account(alias) or acc
                acc["password"] = new_pw
                upsert_account(acc)
            _track_change_pw_result(alias, True)
            result["changed"] = True
        except Exception as exc:
            result["changed"] = False
            result["errors"].append(f"change_password: {exc}")
            _track_change_pw_result(alias, False, error_msg=str(exc))
            LOGGER.error("steam_rental: change_password failed for %s", alias,
                         exc_info=True)

    with _lock:
        acc = find_account(alias) or acc
        if acc.get("rental"):
            acc.pop("rental", None)
            upsert_account(acc)

    game = _get_game_for_alias(alias)
    duration_min = rental.get("duration_min", 0) if rental else 0
    hours = duration_min / 60

    if rental:
        _log_rental_event("end", alias,
                          buyer_username=rental.get("buyer_username"),
                          buyer_id=rental.get("buyer_id"),
                          order_id=rental.get("order_id"),
                          game=game or "", duration_min=duration_min,
                          reason=reason)
        _log_action("rental_end",
                    f"Аренда {alias} завершена ({reason})",
                    alias=alias,
                    buyer=rental.get("buyer_username"),
                    order_id=rental.get("order_id"),
                    game=game or "",
                    duration_min=duration_min, reason=reason)
        # v5: per-account stats + Prometheus counter
        _bump_acc_stat(
            alias,
            inc_expired_count=1 if reason in ("expire", "recovery_expired",
                                              "operator_stop") else 0,
            set_last_expired_at=_now(),
            set_last_end_reason=reason)
        if reason in ("expire", "recovery_expired"):
            _metric_inc("rentals_expired_total")
        elif reason == "operator_stop":
            _metric_inc("operator_stop_total")

    if rental and cardinal is not None:
        try:
            text = _render_template(
                "expired",
                buyer_id=rental.get("buyer_id"),
                login=acc["account_name"],
                game=game or "—",
                hours=f"{hours:.0f}" if hours == int(hours) else f"{hours:.1f}",
                duration=_human_minutes(duration_min),
            )
            cardinal.send_message(
                rental["chat_id"], text,
                chat_name=rental.get("buyer_username"),
                interlocutor_id=rental.get("buyer_id"),
                watermark=False)
        except Exception:
            LOGGER.debug("steam_rental: failed to send expire message",
                         exc_info=True)

    summary = (f"alias={alias} reason={reason} "
               f"revoked={result['revoked']} changed={result['changed']} "
               f"errors={result['errors'] or '—'}")
    LOGGER.info("steam_rental: end_rental %s", summary)
    if cardinal is not None:
        _notify_tg(cardinal,
                   f"⏰ <b>Steam Rental</b>: аренда <code>{alias}</code> закрыта "
                   f"({reason}).\nrevoked={result['revoked']}, "
                   f"changed={result['changed']}\n"
                   + (f"❗ Ошибки: {result['errors']}" if result['errors'] else ""))
        _update_lot_activation(cardinal)
    # Notify queue
    try:
        lots = _load_json(LOTS_FILE, {})
        for lk, ld in lots.items():
            if alias in ld.get("aliases", []):
                _notify_next_in_queue(cardinal, lk)
                break
    except Exception:
        LOGGER.debug("steam_rental: queue notify after end_rental failed", exc_info=True)
    # ── Waitlist top-N notify (per-alias) ────────────────────────────────
    try:
        cfg2 = get_config()
        if cfg2.get("waitlist_enabled", True) and cardinal is not None:
            login_w = (acc.get("account_name") or "") if acc else ""
            game_w = _get_game_for_alias(alias) or ""
            lot_key_w = ""
            try:
                lots_w = _load_json(LOTS_FILE, {})
                for lk, ld in lots_w.items():
                    if alias in ld.get("aliases", []) and str(lk).isdigit():
                        lot_key_w = str(lk)
                        break
            except Exception:
                pass
            sent = _waitlist_notify_top(cardinal, alias, lot_key_w,
                                        login_w, game_w)
            if sent:
                _log_action(
                    "waitlist_notified",
                    f"Waitlist {alias}: уведомлено {sent}",
                    alias=alias, login=login_w, count=sent)
    except Exception:
        LOGGER.debug("steam_rental: waitlist notify after end_rental failed",
                     exc_info=True)
    return result


# ══════════════════════════════════════════════════════════════════════════════
# ── REMOTE PLAY: sessions, API, delivery, monitoring ─────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

_STEAM_API_RP = "https://api.steampowered.com"


def _ensure_rp_storage() -> None:
    os.makedirs(SCREENSHOTS_DIR, exist_ok=True)


# ── Remote Play sessions ─────────────────────────────────────────────────────
def list_rp_sessions() -> dict[str, dict[str, Any]]:
    """Returns {session_id: session_data}."""
    return _load_json(SESSIONS_FILE, {})


def save_rp_sessions(sessions: dict[str, dict[str, Any]]) -> None:
    _save_json(SESSIONS_FILE, sessions)


def find_rp_session(session_id: str) -> dict[str, Any] | None:
    return list_rp_sessions().get(session_id)


def find_active_rp_session_by_alias(alias: str) -> dict[str, Any] | None:
    if not alias:
        return None
    for s in list_rp_sessions().values():
        if s.get("status") != "active":
            continue
        if str(s.get("alias", "")).lower() == alias.lower():
            return s
    return None


def find_active_rp_session_by_buyer(buyer_id: int) -> dict[str, Any] | None:
    for s in list_rp_sessions().values():
        if s.get("status") != "active":
            continue
        try:
            if int(s.get("buyer_id", -1)) == int(buyer_id):
                return s
        except (TypeError, ValueError):
            continue
    return None


def _new_rp_session_id() -> str:
    return uuid.uuid4().hex[:12]


# ── Steam Remote Play API ────────────────────────────────────────────────────

def _generate_steam_link_pin(session: "SteamSession") -> dict[str, Any]:
    """Generates a Steam Link PIN via Steam Remote Play API."""
    try:
        sessionid = session.sessionid_for("https://store.steampowered.com")

        pin_url = "https://store.steampowered.com/remoteplay/ajaxgeneratepin"
        resp = session.sess.post(
            pin_url,
            data={"sessionid": sessionid},
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 Chrome/118.0.0.0 Safari/537.36",
                "Referer": "https://store.steampowered.com/remoteplay",
                "Origin": "https://store.steampowered.com",
            },
            timeout=30,
        )

        if resp.status_code == 200:
            data = resp.json()
            pin = data.get("pin") or data.get("code") or data.get("link_code")
            if pin:
                return {"ok": True, "pin": str(pin)}

        fallback_url = (
            f"{_STEAM_API_RP}/IRemoteClientService/"
            f"AllocateSteamLinkCode/v1/"
        )
        resp2 = session.sess.post(
            fallback_url,
            data={
                "steamid": session.steamid,
                "device_name": "FunPay Remote Play",
            },
            headers={"User-Agent": "Steam Client"},
            timeout=30,
        )
        if resp2.status_code == 200:
            data2 = resp2.json()
            response = data2.get("response", {})
            pin2 = response.get("pin") or response.get("code")
            if pin2:
                return {"ok": True, "pin": str(pin2)}

        pair_url = (
            f"{_STEAM_API_RP}/IRemoteClientService/"
            f"StartPairing/v1/"
        )
        resp3 = session.sess.post(
            pair_url,
            data={
                "steamid": session.steamid,
                "device_type": 1,
            },
            timeout=30,
        )
        if resp3.status_code == 200:
            data3 = resp3.json()
            response3 = data3.get("response", {})
            pin3 = response3.get("pin") or response3.get("pair_code")
            if pin3:
                return {"ok": True, "pin": str(pin3)}

        return {
            "ok": False,
            "error": f"Steam did not return PIN (HTTP {resp.status_code}). "
                     f"Ensure Remote Play is enabled on the account."
        }
    except Exception as exc:
        return {"ok": False, "error": f"PIN generation error: {exc}"}


def _disconnect_remote_play(session: "SteamSession", device_id: str = "") -> bool:
    """Disconnects a Remote Play session."""
    try:
        sessionid = session.sessionid_for("https://store.steampowered.com")

        url = "https://store.steampowered.com/remoteplay/ajaxdeauthorize"
        resp = session.sess.post(
            url,
            data={
                "sessionid": sessionid,
                "device_id": device_id or "all",
            },
            headers={
                "Referer": "https://store.steampowered.com/remoteplay",
                "Origin": "https://store.steampowered.com",
            },
            timeout=30,
        )
        if resp.status_code == 200:
            return True

        deauth_url = (
            f"{_STEAM_API_RP}/IRemoteClientService/"
            f"UnpairDevice/v1/"
        )
        resp2 = session.sess.post(
            deauth_url,
            data={"steamid": session.steamid, "device_id": device_id or "0"},
            timeout=30,
        )
        return resp2.status_code == 200
    except Exception:
        LOGGER.error("steam_rental: RP disconnect failed", exc_info=True)
        return False


def _enable_remote_play(session: "SteamSession") -> bool:
    """Enables Remote Play on the account."""
    try:
        sessionid = session.sessionid_for("https://store.steampowered.com")
        url = "https://store.steampowered.com/remoteplay/ajaxenableremoteplay"
        resp = session.sess.post(
            url,
            data={"sessionid": sessionid, "enable": "1"},
            headers={
                "Referer": "https://store.steampowered.com/remoteplay",
                "Origin": "https://store.steampowered.com",
            },
            timeout=30,
        )
        return resp.status_code == 200
    except Exception:
        LOGGER.warning("steam_rental: enable remote play failed",
                       exc_info=True)
        return False


# ── Remote Play delivery ─────────────────────────────────────────────────────

def deliver_remoteplay(cardinal: "Cardinal", *, alias: str,
                       duration_min: int, order_id: str,
                       buyer_username: str, buyer_id: int,
                       chat_id: int | str) -> dict[str, Any] | None:
    """Delivers a Remote Play session: login, enable RP, generate PIN,
    create session, notify buyer."""
    cfg = get_config()

    with _lock:
        acc = find_account(alias)
        if not acc:
            LOGGER.warning("steam_rental: RP account %s not found", alias)
            return None
        if acc.get("frozen"):
            LOGGER.warning("steam_rental: RP account %s is frozen", alias)
            return None
        existing = find_active_rp_session_by_alias(alias)
        if existing:
            LOGGER.warning("steam_rental: RP account %s already in session", alias)
            return None
        if acc.get("rental"):
            LOGGER.warning("steam_rental: RP account %s has active regular rental", alias)
            return None
        if _account_pool(acc) == "rental":
            LOGGER.warning("steam_rental: RP account %s pool is 'rental', cannot deliver RP", alias)
            return None

    # Login to Steam
    try:
        sess = SteamSession(
            account_name=acc["account_name"],
            password=acc["password"],
            shared_secret=acc["shared_secret"],
            identity_secret=acc["identity_secret"],
            steamid=acc.get("steamid"),
        )
        sess.login()
        with _lock:
            acc = find_account(alias) or acc
            acc["steamid"] = sess.steamid
            upsert_account(acc)
    except Exception as exc:
        LOGGER.error("steam_rental: RP login failed for %s: %s",
                     alias, exc, exc_info=True)
        return None

    # Enable Remote Play
    _enable_remote_play(sess)

    # Generate PIN
    pin_result = _generate_steam_link_pin(sess)
    if not pin_result.get("ok"):
        LOGGER.error("steam_rental: RP PIN generation failed for %s: %s",
                     alias, pin_result.get("error"))
        try:
            cardinal.send_message(
                chat_id,
                _render_template("pin_error", buyer_id=buyer_id),
                chat_name=buyer_username,
                interlocutor_id=buyer_id,
                watermark=False,
            )
        except Exception:
            pass
        return None

    pin = pin_result["pin"]
    started = _now()
    expires = started + duration_min * 60

    # Create session
    session_id = _new_rp_session_id()
    session_data: dict[str, Any] = {
        "id": session_id,
        "alias": alias,
        "account_name": acc["account_name"],
        "buyer_id": int(buyer_id),
        "buyer_username": str(buyer_username),
        "chat_id": chat_id,
        "order_id": str(order_id),
        "pin": pin,
        "pin_generated_at": started,
        "started_at": started,
        "expires_at": expires,
        "duration_min": duration_min,
        "status": "active",
        "connection_status": "awaiting_pin_entry",
        "reminded": False,
        "reminded_2": False,
        "screenshots": [],
        "cheat_alerts": [],
    }

    with _lock:
        sessions = list_rp_sessions()
        sessions[session_id] = session_data
        save_rp_sessions(sessions)

    # Send PIN to buyer
    game = acc.get("game") or _get_game_for_alias(alias) or ""
    duration_str = _human_minutes(duration_min)

    text = _render_template(
        "issue_remoteplay",
        buyer_id=buyer_id,
        game=game or "",
        pin=pin,
        duration=duration_str,
    )

    try:
        cardinal.send_message(
            chat_id, text,
            chat_name=buyer_username,
            interlocutor_id=buyer_id,
            watermark=False,
        )
    except Exception:
        LOGGER.error("steam_rental: RP could not send PIN to chat %s",
                     chat_id, exc_info=True)
        return None

    _log_rental_event("rp_start", alias=alias, buyer_username=buyer_username,
               buyer_id=buyer_id, order_id=order_id,
               duration_min=duration_min, game=game)

    _notify_tg(
        cardinal,
        f"🔗 <b>Steam Remote Play</b>: session <code>{alias}</code>\n"
        f"👤 Buyer: <b>{buyer_username}</b> (order #{order_id})\n"
        f"📱 PIN: <code>{pin}</code>\n"
        f"⏰ Duration: {duration_str}, until {_fmt_ts(expires)} МСК",
    )

    LOGGER.info(
        "steam_rental: RP session %s (alias=%s) for %s, PIN=%s, %d min",
        session_id, alias, buyer_username, pin, duration_min)

    return session_data


# ── Remote Play end session ──────────────────────────────────────────────────

def end_rp_session(cardinal: "Cardinal | None", session_id: str, *,
                   reason: str = "expire") -> bool:
    """Terminates a Remote Play session: disconnect, notify buyer."""
    with _lock:
        sessions = list_rp_sessions()
        session = sessions.get(session_id)
        if not session:
            return False
        if session.get("status") != "active":
            return False
        session["status"] = "ended"
        session["ended_at"] = _now()
        session["end_reason"] = reason
        save_rp_sessions(sessions)

    alias = session.get("alias", "")
    acc = find_account(alias)

    # Disconnect Remote Play
    if acc:
        try:
            sess = SteamSession(
                account_name=acc["account_name"],
                password=acc["password"],
                shared_secret=acc["shared_secret"],
                identity_secret=acc["identity_secret"],
                steamid=acc.get("steamid"),
            )
            sess.login()
            _disconnect_remote_play(sess)
        except Exception:
            LOGGER.warning("steam_rental: RP disconnect on end failed for %s",
                           alias, exc_info=True)

    # Notify buyer
    if cardinal is not None:
        game = acc.get("game", "") if acc else ""
        game = game or _get_game_for_alias(alias) or ""
        hours = session.get("duration_min", 0) / 60

        try:
            cardinal.send_message(
                session["chat_id"],
                _render_template(
                    "session_expired",
                    buyer_id=session.get("buyer_id"),
                    game=game or "",
                    hours=f"{hours:.0f}" if hours == int(hours) else f"{hours:.1f}",
                ),
                chat_name=session.get("buyer_username"),
                interlocutor_id=session.get("buyer_id"),
                watermark=False,
            )
        except Exception:
            LOGGER.debug("steam_rental: RP expire msg failed", exc_info=True)

    _log_rental_event("rp_end", alias=alias,
               buyer_username=session.get("buyer_username"),
               buyer_id=session.get("buyer_id"),
               order_id=session.get("order_id"),
               reason=reason)

    if cardinal is not None:
        _notify_tg(
            cardinal,
            f"⏰ <b>Steam Remote Play</b>: session <code>{alias}</code> "
            f"ended ({reason}).\n"
            f"👤 {session.get('buyer_username')}",
        )

    LOGGER.info("steam_rental: RP session %s ended (alias=%s, reason=%s)",
                session_id, alias, reason)
    # Notify queue for RP sessions
    if cardinal is not None:
        try:
            lots = _load_json(LOTS_FILE, {})
            for lk, ld in lots.items():
                if alias in ld.get("aliases", []):
                    _notify_next_in_queue(cardinal, lk)
                    break
        except Exception:
            LOGGER.debug("steam_rental: queue notify after end_rp_session failed", exc_info=True)
    return True


# ── Remote Play PIN regeneration ─────────────────────────────────────────────

def regenerate_rp_pin(cardinal: "Cardinal", session_id: str) -> dict[str, Any]:
    """Generates a new PIN for an active RP session."""
    with _lock:
        sessions = list_rp_sessions()
        session = sessions.get(session_id)
        if not session or session.get("status") != "active":
            return {"ok": False, "error": "Session not found or inactive."}

    alias = session.get("alias", "")
    acc = find_account(alias)
    if not acc:
        return {"ok": False, "error": "Account not found."}

    try:
        sess = SteamSession(
            account_name=acc["account_name"],
            password=acc["password"],
            shared_secret=acc["shared_secret"],
            identity_secret=acc["identity_secret"],
            steamid=acc.get("steamid"),
        )
        sess.login()
    except Exception as exc:
        return {"ok": False, "error": f"Login error: {exc}"}

    pin_result = _generate_steam_link_pin(sess)
    if not pin_result.get("ok"):
        return pin_result

    with _lock:
        sessions = list_rp_sessions()
        if session_id in sessions:
            sessions[session_id]["pin"] = pin_result["pin"]
            sessions[session_id]["pin_generated_at"] = _now()
            save_rp_sessions(sessions)

    return {"ok": True, "pin": pin_result["pin"]}


# ── Remote Play anti-cheat monitoring ────────────────────────────────────────

def _rp_take_screenshot(session_id: str) -> dict[str, Any]:
    """Takes a screenshot of an active Remote Play session."""
    _ensure_rp_storage()
    sessions = list_rp_sessions()
    session = sessions.get(session_id)
    if not session or session.get("status") != "active":
        return {"ok": False, "error": "Session not active."}

    alias = session.get("alias", "")
    acc = find_account(alias)
    if not acc:
        return {"ok": False, "error": "Account not found."}

    try:
        sess = SteamSession(
            account_name=acc["account_name"],
            password=acc["password"],
            shared_secret=acc["shared_secret"],
            identity_secret=acc["identity_secret"],
            steamid=acc.get("steamid"),
        )
        sess.login()

        screenshot_url = (
            f"{_STEAM_API_RP}/IRemoteClientService/"
            f"GetStreamScreenshot/v1/"
        )
        resp = sess.sess.post(
            screenshot_url,
            data={
                "steamid": sess.steamid,
                "session_id": session_id,
            },
            timeout=30,
        )

        if resp.status_code == 200:
            content_type = resp.headers.get("Content-Type", "")
            if "image" in content_type:
                ts = _now()
                filename = f"{session_id}_{ts}.jpg"
                filepath = os.path.join(SCREENSHOTS_DIR, filename)
                with open(filepath, "wb") as f:
                    f.write(resp.content)

                with _lock:
                    sessions = list_rp_sessions()
                    if session_id in sessions:
                        sessions[session_id].setdefault("screenshots", []).append({
                            "path": filepath,
                            "timestamp": ts,
                        })
                        save_rp_sessions(sessions)

                return {"ok": True, "path": filepath, "timestamp": ts}

            try:
                data = resp.json()
                img_url = data.get("response", {}).get("screenshot_url")
                if img_url:
                    img_resp = requests.get(img_url, timeout=30)
                    if img_resp.status_code == 200:
                        ts = _now()
                        filename = f"{session_id}_{ts}.jpg"
                        filepath = os.path.join(SCREENSHOTS_DIR, filename)
                        with open(filepath, "wb") as f:
                            f.write(img_resp.content)
                        with _lock:
                            sessions = list_rp_sessions()
                            if session_id in sessions:
                                sessions[session_id].setdefault(
                                    "screenshots", []).append({
                                    "path": filepath,
                                    "timestamp": ts,
                                })
                                save_rp_sessions(sessions)
                        return {"ok": True, "path": filepath, "timestamp": ts}
            except Exception:
                pass

        return {"ok": False, "error": f"HTTP {resp.status_code}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _rp_analyze_screenshot_for_cheats(filepath: str, alias: str,
                                      session_id: str) -> dict[str, Any]:
    """Analyzes a screenshot for cheats using AI."""
    try:
        provider, api_key, model = _ai_get_active()
    except Exception:
        return {"is_cheat": False, "confidence": 0,
                "reasoning": "AI module unavailable."}

    if not api_key or not model:
        return {"is_cheat": False, "confidence": 0,
                "reasoning": "AI not configured."}

    try:
        with open(filepath, "rb") as f:
            img_bytes = f.read()
        b64 = base64.b64encode(img_bytes).decode("ascii")
        mime = "image/jpeg"
    except Exception as exc:
        return {"is_cheat": False, "confidence": 0,
                "reasoning": f"File read error: {exc}"}

    prompt = (
        "You are a moderator for a Steam account rental service. "
        "The screenshot shows the current screen of a player connected via Remote Play.\n\n"
        "Analyze the screenshot for SIGNS OF CHEATS:\n"
        "- Cheat overlay menus (ImGui, external injectors)\n"
        "- ESP/wallhack (player outlines through walls)\n"
        "- Aimbot indicators\n"
        "- Cheat consoles, injectors (Cheat Engine, etc.)\n"
        "- Suspicious program windows on desktop\n\n"
        "Reply STRICTLY in JSON format:\n"
        "{\n"
        '  "is_cheat": true/false,\n'
        '  "confidence": 0..100,\n'
        '  "reasoning": "what is suspicious"\n'
        "}\n\n"
        "If the screenshot shows normal gameplay without suspicious elements, "
        "is_cheat=false, confidence=0-20."
    )

    try:
        text = _ai_chat_with_image(provider, api_key, model, prompt, b64, mime)
    except Exception as exc:
        return {"is_cheat": False, "confidence": 0,
                "reasoning": f"AI error: {exc}"}

    try:
        parsed = _ai_parse_json_verdict(text)
        if parsed:
            return {
                "is_cheat": bool(parsed.get("is_cheat")),
                "confidence": int(max(0, min(100, parsed.get("confidence", 0)))),
                "reasoning": str(parsed.get("reasoning", ""))[:500],
            }
    except Exception:
        pass

    return {"is_cheat": False, "confidence": 0,
            "reasoning": "Could not parse AI response."}


def _rp_run_monitoring_cycle(cardinal: "Cardinal") -> None:
    """Takes screenshots of all active RP sessions and analyzes for cheats."""
    cfg = get_config()
    if not cfg.get("anticheat_ai_enabled"):
        return

    threshold = int(cfg.get("anticheat_confidence_threshold", 70))

    for sid, session in list(list_rp_sessions().items()):
        if session.get("status") != "active":
            continue

        result = _rp_take_screenshot(sid)
        if not result.get("ok"):
            LOGGER.debug("steam_rental: RP screenshot failed for %s: %s",
                         sid, result.get("error"))
            continue

        alias = session.get("alias", "")
        analysis = _rp_analyze_screenshot_for_cheats(
            result["path"], alias, sid)

        if analysis.get("is_cheat") and analysis.get("confidence", 0) >= threshold:
            _rp_handle_cheat_detected(cardinal, sid, session, analysis)


def _rp_handle_cheat_detected(cardinal: "Cardinal", session_id: str,
                              session: dict[str, Any],
                              analysis: dict[str, Any]) -> None:
    """Handles cheat detection in RP session."""
    cfg = get_config()
    alias = session.get("alias", "")
    buyer = session.get("buyer_username", "?")

    with _lock:
        sessions = list_rp_sessions()
        if session_id in sessions:
            sessions[session_id].setdefault("cheat_alerts", []).append({
                "timestamp": _now(),
                "confidence": analysis.get("confidence", 0),
                "reasoning": analysis.get("reasoning", ""),
            })
            save_rp_sessions(sessions)

    alert_text = _render_template(
        "cheat_detected",
        alias=alias,
        buyer=buyer,
        timestamp=_fmt_ts(_now()),
        confidence=str(analysis.get("confidence", 0)),
        reasoning=analysis.get("reasoning", "?"),
    )
    _notify_tg(cardinal, f"🚨 " + alert_text)

    _log_rental_event("rp_cheat_detected", alias=alias,
               buyer_username=buyer,
               confidence=analysis.get("confidence"),
               reasoning=analysis.get("reasoning"))

    if cfg.get("auto_disconnect_on_cheat"):
        end_rp_session(cardinal, session_id, reason="cheat_detected")
        _notify_tg(
            cardinal,
            f"🛑 <b>Steam Remote Play</b>: session <code>{alias}</code> "
            f"AUTO-DISCONNECTED due to cheat detection!\n"
            f"👤 Buyer: <b>{buyer}</b>",
        )


# ── Remote Play buyer commands ───────────────────────────────────────────────

def _rp_cmd_pin(cardinal: "Cardinal", msg: Any) -> None:
    """Command !pin - regenerate PIN for Remote Play."""
    buyer_id = getattr(msg, "author_id", None)
    if not buyer_id:
        return

    session = find_active_rp_session_by_buyer(int(buyer_id))
    if not session:
        try:
            cardinal.send_message(
                msg.chat_id,
                _render_template("no_active_session", buyer_id=buyer_id),
                chat_name=getattr(msg, "chat_name", ""),
                interlocutor_id=buyer_id,
                watermark=False,
            )
        except Exception:
            pass
        return

    result = regenerate_rp_pin(cardinal, session["id"])
    if result.get("ok"):
        time_left_sec = max(0, session.get("expires_at", 0) - _now())
        time_left_str = _human_minutes(time_left_sec // 60)
        text = _render_template(
            "pin_generated", buyer_id=buyer_id,
            pin=result["pin"],
            time_left=time_left_str,
        )
    else:
        text = _render_template("pin_error", buyer_id=buyer_id)

    try:
        cardinal.send_message(
            msg.chat_id, text,
            chat_name=getattr(msg, "chat_name", ""),
            interlocutor_id=buyer_id,
            watermark=False,
        )
    except Exception:
        pass


def _rp_cmd_status(cardinal: "Cardinal", msg: Any) -> None:
    """Command !statusrp - Remote Play session status."""
    buyer_id = getattr(msg, "author_id", None)
    if not buyer_id:
        return

    session = find_active_rp_session_by_buyer(int(buyer_id))
    if not session:
        try:
            cardinal.send_message(
                msg.chat_id,
                _render_template("no_active_session", buyer_id=buyer_id),
                chat_name=getattr(msg, "chat_name", ""),
                interlocutor_id=buyer_id,
                watermark=False,
            )
        except Exception:
            pass
        return

    alias = session.get("alias", "")
    game = _get_game_for_alias(alias) or ""
    time_left_sec = max(0, session.get("expires_at", 0) - _now())
    time_left_str = _human_minutes(time_left_sec // 60)

    text = _render_template(
        "status_rp", buyer_id=buyer_id,
        game=game or "",
        time_left=time_left_str,
        expires_at=_fmt_ts(session.get("expires_at", 0)),
        connection_status=session.get("connection_status", "unknown"),
    )

    try:
        cardinal.send_message(
            msg.chat_id, text,
            chat_name=getattr(msg, "chat_name", ""),
            interlocutor_id=buyer_id,
            watermark=False,
        )
    except Exception:
        pass


def _rp_cmd_help(cardinal: "Cardinal", msg: Any) -> None:
    """Command !helprp."""
    try:
        cardinal.send_message(
            msg.chat_id,
            _render_template("help_rp", buyer_id=buyer_id),
            chat_name=getattr(msg, "chat_name", ""),
            interlocutor_id=getattr(msg, "author_id", None),
            watermark=False,
        )
    except Exception:
        pass


# ── Команда очереди ──────────────────────────────────────────────────────────
def _cmd_queue(cardinal: "Cardinal", msg: Any) -> None:
    """Command !очередь / !queue - join waiting queue."""
    cfg = get_config()
    if not cfg.get("queue_enabled", True):
        return

    buyer_id = getattr(msg, "author_id", None)
    buyer_username = getattr(msg, "chat_name", "") or getattr(msg, "author", "") or ""
    chat_id = getattr(msg, "chat_id", None)

    if not buyer_id or not chat_id:
        return

    lots = _load_json(LOTS_FILE, {})
    target_lot_key = None
    target_game = ""

    # First, try to find the buyer's relevant lot from active/expired rental
    buyer_acc = _find_buyer_active_rental(int(buyer_id))
    if not buyer_acc:
        buyer_acc = _find_buyer_last_rental(int(buyer_id))
    if buyer_acc:
        alias = buyer_acc.get("alias", "")
        for lot_key, lot_data in lots.items():
            if alias in _combined_lot_pool(lot_data):
                # Only queue if this lot has no free accounts
                aliases = _combined_lot_pool(lot_data)
                free_count = _count_free_accounts(aliases)
                if free_count == 0:
                    target_lot_key = lot_key
                    target_game = lot_data.get("game", "")
                break

    # Fallback: find the first lot with no free accounts
    if not target_lot_key:
        for lot_key, lot_data in lots.items():
            aliases = _combined_lot_pool(lot_data)
            if not aliases:
                continue
            free_count = _count_free_accounts(aliases)
            if free_count == 0:
                target_lot_key = lot_key
                target_game = lot_data.get("game", "")
                break

    if not target_lot_key:
        # All lots have free accounts - no need to queue
        try:
            cardinal.send_message(
                chat_id,
                "✅ Аккаунты доступны! Оплатите лот для получения доступа.",
                chat_name=buyer_username,
                interlocutor_id=buyer_id,
                watermark=False)
        except Exception:
            pass
        return

    result = _add_to_queue(target_lot_key, buyer_id, buyer_username, chat_id)

    if result["ok"]:
        text = _render_template("queue_joined",
            buyer_id=buyer_id,
            game=target_game, position=str(result["position"]))
    elif result.get("reason") == "already":
        text = _render_template("queue_already",
            buyer_id=buyer_id,
            position=str(result.get("position", "?")))
    elif result.get("reason") == "full":
        text = _render_template("queue_full", buyer_id=buyer_id)
    else:
        return

    try:
        cardinal.send_message(
            chat_id, text,
            chat_name=buyer_username,
            interlocutor_id=buyer_id,
            watermark=False)
    except Exception:
        LOGGER.debug("steam_rental: queue cmd response failed", exc_info=True)


# ── Фоновый чекер экспирации + напоминания ──────────────────────────────────
_checker_thread: threading.Thread | None = None
_stop_event = threading.Event()

_last_rp_monitor_ts: int = 0
_rp_monitor_thread: threading.Thread | None = None
_rp_monitor_active = threading.Event()


def _rp_checker_tick(cardinal: "Cardinal") -> None:
    """Called each tick of the main checker loop to handle RP expirations and monitoring."""
    global _last_rp_monitor_ts
    now = _now()
    cfg = get_config()
    reminder_min = int(cfg.get("reminder_minutes", 30) or 0)
    reminder_min_2 = int(cfg.get("reminder_minutes_2", 10) or 0)

    for sid, session in list(list_rp_sessions().items()):
        if session.get("status") != "active":
            continue
        expires_at = session.get("expires_at", 0)

        if expires_at <= now:
            try:
                end_rp_session(cardinal, sid, reason="expire")
            except Exception:
                LOGGER.error("steam_rental: RP end_session crash for %s",
                             sid, exc_info=True)
            continue
        time_left = expires_at - now
        # Сначала второе (более позднее по времени) напоминание
        if (reminder_min_2 > 0
                and not session.get("reminded_2", False)
                and time_left <= reminder_min_2 * 60):
            _rp_send_reminder(cardinal, sid, session, time_left,
                              second=True)
        elif (reminder_min > 0
                and not session.get("reminded", False)
                and time_left <= reminder_min * 60):
            _rp_send_reminder(cardinal, sid, session, time_left)

    # Anti-cheat monitoring (fallback - primary monitoring in _rp_monitoring_loop thread)
    if cfg.get("monitoring_enabled") and not _rp_monitor_active.is_set():
        interval = int(cfg.get("monitoring_interval_seconds", 300))
        if now - _last_rp_monitor_ts >= interval:
            _last_rp_monitor_ts = now
            _rp_run_monitoring_cycle(cardinal)


def _rp_send_reminder(cardinal: "Cardinal", session_id: str,
                      session: dict[str, Any], time_left: int,
                      second: bool = False) -> None:
    """Sends a reminder about RP session expiring soon.

    If second=True — uses the second-reminder template if available;
    falls back to the regular one otherwise.
    """
    minutes_left = max(1, time_left // 60)
    alias = session.get("alias", "")
    game = _get_game_for_alias(alias) or ""

    # Для RP отдельного 2-го шаблона нет — используем reminder_rp с пометкой.
    tpl_key = "reminder_rp"
    try:
        cardinal.send_message(
            session["chat_id"],
            _render_template(
                tpl_key,
                buyer_id=session.get("buyer_id"),
                minutes=str(minutes_left),
                game=game or "",
            ),
            chat_name=session.get("buyer_username"),
            interlocutor_id=session.get("buyer_id"),
            watermark=False,
        )
    except Exception:
        LOGGER.debug("steam_rental: RP reminder failed for %s",
                     session_id, exc_info=True)

    with _lock:
        sessions = list_rp_sessions()
        if session_id in sessions:
            if second:
                sessions[session_id]["reminded_2"] = True
                # Страхуем reminded — если 1-е не сработало
                sessions[session_id]["reminded"] = True
            else:
                sessions[session_id]["reminded"] = True
            save_rp_sessions(sessions)


def _rp_monitoring_loop(cardinal: "Cardinal") -> None:
    """Dedicated RP monitoring thread - takes screenshots at configured interval."""
    _rp_monitor_active.set()
    LOGGER.info("steam_rental: RP monitoring thread started")
    while not _stop_event.is_set():
        try:
            cfg = get_config()
            if cfg.get("monitoring_enabled"):
                _rp_run_monitoring_cycle(cardinal)
            interval = int(cfg.get("monitoring_interval_seconds", 300))
            _stop_event.wait(interval)
        except Exception:
            LOGGER.error("steam_rental: RP monitoring loop error", exc_info=True)
            _stop_event.wait(60)
    LOGGER.info("steam_rental: RP monitoring thread stopped")


def _checker_loop(cardinal: "Cardinal") -> None:
    LOGGER.info("steam_rental: background checker started (expiration + reminders)")
    while not _stop_event.is_set():
        try:
            now = _now()
            cfg = get_config()
            reminder_min = int(cfg.get("reminder_minutes", 30) or 0)
            reminder_min_2 = int(cfg.get("reminder_minutes_2", 10) or 0)

            expired: list[str] = []
            remind: list[tuple[str, dict[str, Any]]] = []
            remind2: list[tuple[str, dict[str, Any]]] = []

            for acc in list_accounts():
                rental = acc.get("rental")
                if not rental:
                    continue
                expires_at = rental.get("expires_at", 0)

                if expires_at <= now:
                    expired.append(acc["alias"])
                    continue
                time_left = expires_at - now
                # Второе напоминание (более позднее, меньше минут до конца)
                if (reminder_min_2 > 0
                        and not rental.get("reminded_2", False)
                        and time_left <= reminder_min_2 * 60):
                    remind2.append((acc["alias"], rental))
                # Первое напоминание (раньше, больше минут до конца)
                elif (reminder_min > 0
                        and not rental.get("reminded", False)
                        and time_left <= reminder_min * 60):
                    remind.append((acc["alias"], rental))

            for alias in expired:
                try:
                    end_rental(cardinal, alias, reason="expire")
                except Exception:
                    LOGGER.error("steam_rental: end_rental crash for %s", alias,
                                 exc_info=True)

            for alias, rental in remind:
                try:
                    acc = find_account(alias)
                    if not acc:
                        continue
                    time_left = rental.get("expires_at", 0) - _now()
                    minutes_left = max(1, time_left // 60)
                    game = _get_game_for_alias(alias)

                    text = _render_template(
                        "reminder",
                        buyer_id=rental.get("buyer_id"),
                        login=acc["account_name"],
                        game=game or "—",
                        minutes=str(minutes_left),
                        new_expires=_fmt_ts(rental["expires_at"]),
                    )
                    cardinal.send_message(
                        rental["chat_id"], text,
                        chat_name=rental.get("buyer_username"),
                        interlocutor_id=rental.get("buyer_id"),
                        watermark=False)

                    with _lock:
                        acc = find_account(alias) or acc
                        if acc.get("rental"):
                            acc["rental"]["reminded"] = True
                            upsert_account(acc)

                    LOGGER.info("steam_rental: reminder sent for %s (%d мин осталось)",
                                alias, minutes_left)
                except Exception:
                    LOGGER.error("steam_rental: reminder failed for %s", alias,
                                 exc_info=True)

            for alias, rental in remind2:
                try:
                    acc = find_account(alias)
                    if not acc:
                        continue
                    time_left = rental.get("expires_at", 0) - _now()
                    minutes_left = max(1, time_left // 60)
                    game = _get_game_for_alias(alias)

                    text = _render_template(
                        "reminder_2",
                        buyer_id=rental.get("buyer_id"),
                        login=acc["account_name"],
                        game=game or "—",
                        minutes=str(minutes_left),
                        new_expires=_fmt_ts(rental["expires_at"]),
                    )
                    cardinal.send_message(
                        rental["chat_id"], text,
                        chat_name=rental.get("buyer_username"),
                        interlocutor_id=rental.get("buyer_id"),
                        watermark=False)

                    with _lock:
                        acc = find_account(alias) or acc
                        if acc.get("rental"):
                            acc["rental"]["reminded_2"] = True
                            # Также страхуем reminded (если 1-е напоминание
                            # вдруг было пропущено — например при коротких арендах)
                            acc["rental"]["reminded"] = True
                            upsert_account(acc)

                    LOGGER.info("steam_rental: reminder_2 sent for %s (%d мин осталось)",
                                alias, minutes_left)
                except Exception:
                    LOGGER.error("steam_rental: reminder_2 failed for %s", alias,
                                 exc_info=True)

            try:
                _update_lot_activation(cardinal)
            except Exception:
                LOGGER.debug("steam_rental: lot activation update failed", exc_info=True)

            # Проверяем ивенты (уведомление о незакрытых заказах)
            try:
                events = _load_events()
                ev = events.get("unclosed_notify", {})
                _next_run = ev.get("next_run", 0)
                # next_run==0 (ещё ни разу не запускался) трактуем как «пора»:
                # иначе авто-уведомление никогда не стартует само.
                if ev.get("enabled", True) and (not _next_run or _next_run <= now):
                    unclosed = _get_unclosed_rentals()
                    ev["last_run"] = now
                    interval = ev.get("interval_hours", 24)
                    ev["next_run"] = now + interval * 3600
                    _save_events(events)
                    if unclosed:
                        lines = [f"⚠ Незакрытых заказов на аренду: "
                                 f"{len(unclosed)}"]
                        for u in unclosed[:5]:
                            lines.append(
                                f"  • {u['alias']} — {u['buyer_username']} "
                                f"(просрочка {u['overdue_min']} мин.)")
                        if len(unclosed) > 5:
                            lines.append(f"  ... и ещё {len(unclosed) - 5}")
                        _notify_tg(cardinal, "\n".join(lines))
            except Exception:
                LOGGER.debug("steam_rental: events check failed", exc_info=True)

            # ── VAC/Trade ban scan ──
            try:
                cfg2 = get_config()
                if cfg2.get("vac_scan_enabled") \
                        and (cfg2.get("steam_api_key") or "").strip():
                    interval_s = max(5, int(cfg2.get("vac_scan_interval_min",
                                                      60))) * 60
                    if (_last_vac_scan_ts == 0
                            or now - _last_vac_scan_ts >= interval_s):
                        try:
                            _vac_scan_iter(cardinal)
                        except Exception:
                            LOGGER.error(
                                "steam_rental: VAC scan iter crash",
                                exc_info=True)
            except Exception:
                LOGGER.debug("steam_rental: VAC scan gate failed",
                             exc_info=True)

            # ── PC-club: чистка просроченных заявок ──
            try:
                expired = _club_cleanup_expired()
                if expired:
                    LOGGER.info(
                        "steam_rental: club: %d просроч. заявок помечено",
                        expired)
            except Exception:
                LOGGER.debug("steam_rental: club cleanup failed",
                             exc_info=True)

            # v5: периодический sqlite-снимок (не блокирует тик)
            try:
                _sqlite_dump_now()
            except Exception:
                LOGGER.debug("steam_rental: sqlite tick failed",
                              exc_info=True)

            # Queue cleanup
            try:
                _cleanup_expired_queue()
            except Exception:
                LOGGER.debug("steam_rental: queue cleanup failed", exc_info=True)

            # ── Remote Play: session expiration + monitoring ──
            try:
                _rp_checker_tick(cardinal)
            except Exception:
                LOGGER.debug("steam_rental: RP checker tick failed",
                             exc_info=True)
        except Exception:
            LOGGER.error("steam_rental: checker loop error", exc_info=True)
        _stop_event.wait(30)
    LOGGER.info("steam_rental: background checker stopped")


# ── Telegram-уведомления ────────────────────────────────────────────────────
def _notify_tg(cardinal: "Cardinal", text: str,
               *, op_alias: str | None = None) -> None:
    """v5: при op_alias=<alias> под уведомлением появится inline-клавиатура
    с быстрыми действиями оператора (➕15м / 🛑 Прервать / 🔁 Сменить)."""
    cfg = get_config()
    if not cfg.get("tg_notify", True):
        return
    try:
        if cardinal.telegram is None:
            return
    except Exception:
        return
    reply_markup = None
    if op_alias:
        try:
            from telebot import types as tbtypes  # type: ignore
            sid = _sid(op_alias)
            reply_markup = tbtypes.InlineKeyboardMarkup(row_width=3)
            reply_markup.add(
                tbtypes.InlineKeyboardButton(
                    "➕15м", callback_data=f"sr:ext:{sid}:15"),
                tbtypes.InlineKeyboardButton(
                    "➕30м", callback_data=f"sr:ext:{sid}:30"),
                tbtypes.InlineKeyboardButton(
                    "➕60м", callback_data=f"sr:ext:{sid}:60"),
            )
            reply_markup.add(
                tbtypes.InlineKeyboardButton(
                    "🛑 Прервать", callback_data=f"sr:stop:{sid}"),
                tbtypes.InlineKeyboardButton(
                    "🔁 Сменить", callback_data=f"sr:switch:{sid}"),
            )
            reply_markup.add(
                tbtypes.InlineKeyboardButton(
                    "🚫 В blacklist", callback_data=f"sr:opbl:{sid}"))
        except Exception:
            reply_markup = None
    try:
        if reply_markup is not None:
            for chat_id in cardinal.telegram.authorized_users:
                try:
                    cardinal.telegram.bot.send_message(
                        chat_id, text, parse_mode="HTML",
                        reply_markup=reply_markup)
                except Exception:
                    LOGGER.debug(
                        "steam_rental: TG op-notify failed for %s",
                        chat_id, exc_info=True)
            return
        from tg_bot import utils as tg_utils
        notif_type = getattr(tg_utils.NotificationTypes, "other_plugins_loaded",
                             getattr(tg_utils.NotificationTypes, "critical", "other"))
        threading.Thread(
            target=cardinal.telegram.send_notification,
            args=(text,),
            kwargs={"notification_type": notif_type}, daemon=True).start()
    except Exception:
        try:
            for chat_id in cardinal.telegram.authorized_users:
                cardinal.telegram.bot.send_message(chat_id, text,
                                                   parse_mode="HTML")
        except Exception:
            LOGGER.debug("steam_rental: TG notify failed", exc_info=True)


def _human_minutes(m: int) -> str:
    if m >= 60 * 24 * 30:
        mo, rem = divmod(m, 60 * 24 * 30)
        rd = rem // (60 * 24)
        return f"{mo}мес {rd}д" if rd else f"{mo}мес"
    if m >= 60 * 24:
        d, rem = divmod(m, 60 * 24)
        return f"{d}д {rem // 60}ч" if rem else f"{d}д"
    if m >= 60:
        h, rem = divmod(m, 60)
        return f"{h}ч {rem}м" if rem else f"{h}ч"
    return f"{m}м"


def _human_minutes_en(m: int) -> str:
    """v2.23.0: английский аналог `_human_minutes` для покупательских
    сообщений (чтобы «{remaining}» в шаблонах EN не остались на русских
    единицах). Used at least в `accounts_list_busy_line`.
    """
    if m >= 60 * 24 * 30:
        mo, rem = divmod(m, 60 * 24 * 30)
        rd = rem // (60 * 24)
        return f"{mo}mo {rd}d" if rd else f"{mo}mo"
    if m >= 60 * 24:
        d, rem = divmod(m, 60 * 24)
        return f"{d}d {rem // 60}h" if rem else f"{d}d"
    if m >= 60:
        h, rem = divmod(m, 60)
        return f"{h}h {rem}m" if rem else f"{h}h"
    return f"{m}m"


def _human_minutes_lang(m: int, lang: str) -> str:
    return _human_minutes_en(m) if lang == "en" else _human_minutes(m)


_DURATION_UNITS = {
    "m": 1, "min": 1, "мин": 1, "м": 1,
    "h": 60, "hr": 60, "час": 60, "ч": 60,
    "d": 60 * 24, "day": 60 * 24, "д": 60 * 24, "дн": 60 * 24,
    "день": 60 * 24, "дня": 60 * 24, "дней": 60 * 24,
    "w": 60 * 24 * 7, "week": 60 * 24 * 7, "нед": 60 * 24 * 7,
    "неделя": 60 * 24 * 7, "недель": 60 * 24 * 7,
    "mo": 60 * 24 * 30, "month": 60 * 24 * 30, "мес": 60 * 24 * 30,
    "месяц": 60 * 24 * 30, "месяцев": 60 * 24 * 30,
}


def _parse_duration(text: str) -> int:
    """Парсит '60', '2h', '1d', '1w', '1mo' → возвращает минуты.

    Бросает ValueError при невалидном вводе."""
    s = (text or "").strip().lower().replace(",", ".")
    if not s:
        raise ValueError("Пустой ввод.")
    # Голое число → минуты
    try:
        n = int(s)
        if n <= 0:
            raise ValueError("Длительность должна быть положительной.")
        return n
    except ValueError:
        pass
    # Формат: число + суффикс
    m = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*([a-zа-я]+)$", s)
    if not m:
        raise ValueError(
            "Неверный формат. Примеры: 60, 60m, 2h, 1d, 1w, 1mo.")
    val = float(m.group(1))
    unit = m.group(2)
    if unit not in _DURATION_UNITS:
        raise ValueError(
            f"Неизвестная единица '{unit}'. Доступно: m/h/d/w/mo, "
            f"мин/ч/д/нед/мес.")
    minutes = int(round(val * _DURATION_UNITS[unit]))
    if minutes <= 0:
        raise ValueError("Длительность должна быть положительной.")
    return minutes


# ── Проверка аккаунтов при запуске ────────────────────────────────────────────
def _check_accounts_thread(cardinal: "Cardinal") -> None:
    cfg = get_config()
    if not cfg.get("check_accounts_on_start"):
        return
    accs = list_accounts()
    if not accs:
        return
    LOGGER.info("steam_rental: проверка %d аккаунтов при запуске...", len(accs))
    ok_list: list[str] = []
    fail_list: list[tuple[str, str]] = []
    for acc in accs:
        if acc.get("frozen"):
            continue
        if acc.get("test"):
            continue
        if not acc.get("shared_secret"):
            continue
        try:
            s = SteamSession(
                acc["account_name"], acc["password"],
                acc["shared_secret"], acc["identity_secret"],
                acc.get("steamid"))
            s.login()
            _track_login_result(acc["alias"], True)
            with _lock:
                a = find_account(acc["alias"]) or acc
                a["steamid"] = s.steamid
                upsert_account(a)
            ok_list.append(acc["alias"])
        except Exception as exc:
            _track_login_result(acc["alias"], False)
            fail_list.append((acc["alias"], str(exc)[:80]))
        time.sleep(3)

    frozen_count = sum(1 for a, _ in fail_list
                       if (find_account(a) or {}).get("frozen"))
    summary = f"OK: {len(ok_list)}, Ошибки: {len(fail_list)}"
    if fail_list:
        fail_lines = "\n".join(f"  • {a}: {e}" for a, e in fail_list[:10])
        summary += f"\n\n<b>Неудачные:</b>\n<code>{fail_lines}</code>"
        if frozen_count:
            summary += f"\n\n❄️ Авто-заморожено: {frozen_count}"

    _notify_tg(cardinal, f"🔍 <b>Steam Rental</b>: проверка аккаунтов\n{summary}")
    LOGGER.info("steam_rental: проверка завершена — ok=%d, fail=%d, frozen=%d",
                len(ok_list), len(fail_list), frozen_count)
    try:
        _update_lot_activation(cardinal)
    except Exception:
        pass


# ── Хэндлеры событий FunPay ──────────────────────────────────────────────────
def _handler_pre_init(cardinal: "Cardinal") -> None:
    # 💛 Донат-баннер (защита реквизитов автора)
    global _donation_cardinal
    _donation_cardinal = cardinal
    try:
        tg = getattr(cardinal, "telegram", None)
        if tg:
            tg.cbq_handler(
                _donation_on_cb,
                lambda c: (c.data or "").startswith("srl_dn:"))
            _start_donation_reminder(cardinal)
    except Exception:
        pass

    _ensure_storage()
    get_config()
    list_accounts()
    list_lots()
    LOGGER.info("steam_rental: storage initialised at %s", STORAGE_DIR)
    # license check removed
    _register_tg_commands(cardinal)
    # v5: SQLite sidecar — не падаем, если модуль/sqlite3 недоступны
    try:
        from steam_sqlite import autotune  # type: ignore
        autotune(STORAGE_DIR, "steam_rental")
    except Exception:
        LOGGER.debug("steam_rental: sqlite sidecar disabled",
                      exc_info=True)


_SQLITE_DUMP_DISABLED = False  # one-shot flag: модуль не нашли — больше не пробуем


def _sqlite_dump_now() -> bool:
    """v5: дамп текущего состояния в SQLite (sidecar).

    Если модуль steam_sqlite не установлен — кэшируем флаг и больше не
    пытаемся импортировать, чтобы не засирать лог тысячами трейсбеков.
    """
    global _SQLITE_DUMP_DISABLED
    if _SQLITE_DUMP_DISABLED:
        return False
    try:
        from steam_sqlite import dump_rental  # type: ignore
        return dump_rental(
            list_accounts(), {}, list_history(), list_blacklist())
    except ModuleNotFoundError:
        _SQLITE_DUMP_DISABLED = True
        LOGGER.info(
            "steam_rental: steam_sqlite.py не найден рядом с плагином — "
            "sqlite-дамп отключён (это не влияет на работу плагина).")
        return False
    except Exception:
        LOGGER.debug("steam_rental: sqlite dump failed", exc_info=True)
        return False


def _recover_on_start(cardinal: "Cardinal") -> None:
    """v5: при старте бота — explicit recovery просроченных аренд.
    Без него background-чекер тоже их подберёт, но recovery даёт мгновенный
    сигнал админу в Telegram, а с пометкой `recovered=True` видно, что аренда
    «дотянула» через крах VM."""
    cfg = get_config()
    if not cfg.get("recovery_on_start", True):
        return
    now = _now()
    expired_aliases: list[str] = []
    for acc in list_accounts():
        rental = acc.get("rental")
        if not rental:
            continue
        if int(rental.get("expires_at", 0) or 0) <= now:
            expired_aliases.append(acc["alias"])
    if not expired_aliases:
        return
    LOGGER.info("steam_rental: recovery: closing %d expired rentals after restart",
                len(expired_aliases))
    _notify_tg(cardinal,
               f"♻ <b>Steam Rental</b>: recovery после рестарта — закрываю "
               f"{len(expired_aliases)} просроченных аренд: "
               f"<code>{_esc(', '.join(expired_aliases[:10]))}</code>"
               + (f" (+{len(expired_aliases) - 10})"
                  if len(expired_aliases) > 10 else ""))
    for alias in expired_aliases:
        try:
            with _lock:
                acc = find_account(alias)
                if acc and acc.get("rental"):
                    acc["rental"]["recovered"] = True
                    upsert_account(acc)
            end_rental(cardinal, alias, reason="recovery_expired")
        except Exception:
            LOGGER.error("steam_rental: recovery end_rental crash for %s",
                          alias, exc_info=True)


_metrics_http_server: Any = None
_metrics_http_thread: threading.Thread | None = None


def _metrics_render() -> str:
    snap = _metric_snapshot()
    lines: list[str] = []
    for key, val in sorted(snap.items()):
        is_counter = key.endswith("_total")
        mtype = "counter" if is_counter else "gauge"
        lines.append(f"# HELP steam_rental_{key} {key}")
        lines.append(f"# TYPE steam_rental_{key} {mtype}")
        lines.append(f"steam_rental_{key} {float(val)}")
    return "\n".join(lines) + "\n"


def _start_metrics_server(cardinal: "Cardinal") -> None:
    global _metrics_http_server, _metrics_http_thread
    cfg = get_config()
    if not cfg.get("metrics_enabled"):
        return
    if _metrics_http_thread and _metrics_http_thread.is_alive():
        return
    try:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path != "/metrics":
                    self.send_response(404)
                    self.end_headers()
                    return
                try:
                    body = _metrics_render().encode("utf-8")
                except Exception:
                    self.send_response(500)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    "text/plain; version=0.0.4; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_a: Any, **_kw: Any) -> None:
                return

        bind = cfg.get("metrics_bind") or "0.0.0.0"
        port = int(cfg.get("metrics_port", 9101))
        srv = ThreadingHTTPServer((bind, port), _Handler)
        _metrics_http_server = srv

        def _run() -> None:
            try:
                srv.serve_forever()
            except Exception:
                LOGGER.error("steam_rental: metrics server crashed",
                              exc_info=True)

        t = threading.Thread(target=_run, daemon=True,
                             name="steam_rental-metrics")
        t.start()
        _metrics_http_thread = t
        LOGGER.info("steam_rental: Prometheus metrics on %s:%d/metrics",
                    bind, port)
    except Exception:
        LOGGER.error("steam_rental: failed to start metrics server",
                      exc_info=True)


def _stop_metrics_server() -> None:
    global _metrics_http_server, _metrics_http_thread
    srv = _metrics_http_server
    if srv is not None:
        try:
            srv.shutdown()
        except Exception:
            pass
        _metrics_http_server = None
    _metrics_http_thread = None


def _daily_summary_text() -> str:
    history = list_history()
    accs = list_accounts()
    now = _now()
    day_ago = now - 86400
    starts = [h for h in history
              if h.get("event") == "start" and h.get("ts", 0) >= day_ago]
    ends = [h for h in history
            if h.get("event") == "end" and h.get("ts", 0) >= day_ago]
    extends = [h for h in history
               if h.get("event") == "extend" and h.get("ts", 0) >= day_ago]
    refunds = [h for h in history
               if h.get("event") == "refund" and h.get("ts", 0) >= day_ago]
    # Выручка = sum(start.amount) - sum(|refund.amount|). refund.amount уже
    # отрицательный, поэтому sum по обоим типам сразу даёт нетто.
    revenue = sum(float(h.get("amount", 0) or 0) for h in (starts + refunds))
    refund_total = -sum(float(h.get("amount", 0) or 0) for h in refunds)
    active = [a for a in accs if a.get("rental")]
    frozen = [a for a in accs if a.get("frozen")]
    free = [a for a in accs
            if not a.get("rental") and not a.get("frozen")]
    return (
        "📊 <b>Steam Rental — сводка за сутки</b>\n\n"
        f"🆕 Выдано аренд: <b>{len(starts)}</b>\n"
        f"⏰ Завершено: <b>{len(ends)}</b>\n"
        f"➕ Продлений: <b>{len(extends)}</b>\n"
        f"� Возвратов: <b>{len(refunds)}</b>"
        + (f" (-{refund_total:.2f})" if refund_total > 0 else "")
        + "\n"
        f"�💰 Выручка: <b>{revenue:.2f}</b>\n\n"
        f"📦 Пул: всего <b>{len(accs)}</b>, "
        f"свободно <b>{len(free)}</b>, "
        f"в аренде <b>{len(active)}</b>, "
        f"заморожено <b>{len(frozen)}</b>"
    )


_daily_summary_thread: threading.Thread | None = None


def _daily_summary_loop(cardinal: "Cardinal") -> None:
    """Шлёт сводку 1 раз в сутки в указанный час МСК (по умолч. 00:00 МСК)."""
    last_sent_day = -1
    while not _stop_event.is_set():
        try:
            cfg = get_config()
            if not cfg.get("daily_summary_enabled", True):
                _stop_event.wait(300)
                continue
            target_hour = int(cfg.get("daily_summary_hour_msk", 0)) % 24
            now_msk = datetime.datetime.now(tz=_MSK_TZ)
            if (now_msk.hour == target_hour
                    and now_msk.toordinal() != last_sent_day):
                try:
                    _notify_tg(cardinal, _daily_summary_text())
                    last_sent_day = now_msk.toordinal()
                except Exception:
                    LOGGER.error("steam_rental: daily summary failed",
                                  exc_info=True)
        except Exception:
            LOGGER.debug("steam_rental: daily summary tick failed",
                          exc_info=True)
        _stop_event.wait(60)


def _handler_post_start(cardinal: "Cardinal") -> None:
    global _checker_thread, _daily_summary_thread, _rp_monitor_thread
    _set_cardinal_ref(cardinal)
    # ── v6: миграция → games.json (фоном) ──
    # Пробегаемся по текущим лотам и создаём игры по полю `game`.
    # Если cardinal/account доступны — подтягиваем title/subcategory_id
    # через get_lot_fields. Идемпотентно: не перезаписывает ручные правки.
    try:
        threading.Thread(
            target=lambda c=cardinal: _migrate_lots_to_games_v6(c),
            daemon=True, name="steam_rental-migrate-games-v6").start()
    except Exception:
        LOGGER.debug("steam_rental: games migration thread failed to start",
                     exc_info=True)
    if _checker_thread and _checker_thread.is_alive():
        return
    _stop_event.clear()
    _checker_thread = threading.Thread(
        target=_checker_loop, args=(cardinal,), daemon=True,
        name="steam_rental-checker")
    _checker_thread.start()
    threading.Thread(
        target=_check_accounts_thread, args=(cardinal,), daemon=True,
        name="steam_rental-startup-check").start()
    # v5: recovery, метрики, daily summary
    try:
        _recover_on_start(cardinal)
    except Exception:
        LOGGER.error("steam_rental: recovery crash", exc_info=True)
    try:
        _start_metrics_server(cardinal)
    except Exception:
        LOGGER.error("steam_rental: metrics server boot crash", exc_info=True)
    if not (_daily_summary_thread and _daily_summary_thread.is_alive()):
        _daily_summary_thread = threading.Thread(
            target=_daily_summary_loop, args=(cardinal,), daemon=True,
            name="steam_rental-daily-summary")
        _daily_summary_thread.start()
    # ── RP monitoring dedicated thread ──
    cfg = get_config()
    if cfg.get("monitoring_enabled") and not _rp_monitor_active.is_set():
        _rp_monitor_thread = threading.Thread(
            target=_rp_monitoring_loop, args=(cardinal,), daemon=True,
            name="steam_rental-rp-monitor")
        _rp_monitor_thread.start()


def _migrate_lots_to_games_v6(cardinal: "Cardinal") -> None:
    """v6: миграция в games.json.

    Идемпотентно: если game_key уже проставлен у лота — пропускаем.
    Для каждого лота с пустым game_key:
      - берём game (старое поле с именем игры),
      - берём subcategory_id/category_id из FunPay через get_lot_fields,
      - создаём/обновляем запись в games.json,
      - проставляем lot.game_key и kind.
    """
    try:
        lots = list_lots()
        games = list_games()
        if not lots:
            return
        # Если у всех лотов уже проставлен game_key — миграция не нужна
        if all(lot.get("game_key") for lot in lots.values()):
            return
        account_obj = getattr(cardinal, "account", None) if cardinal else None
        for lot_id, lot in lots.items():
            try:
                if lot.get("game_key"):
                    continue
                game_name = (lot.get("game") or "").strip()
                if not game_name:
                    # Нет имени игры — пропускаем. Заполнится вручную
                    # через /srental → 🎮 Игры → Добавить игру.
                    continue
                sub_id = None
                cat_id = None
                if account_obj is not None and str(lot_id).isdigit():
                    try:
                        lf = account_obj.get_lot_fields(int(lot_id))
                        sub_id = (lf.subcategory.id
                                  if getattr(lf, "subcategory", None) else None)
                        cat_id = (lf.subcategory.category.id
                                  if getattr(lf.subcategory, "category", None)
                                  else None)
                    except Exception:
                        LOGGER.debug(
                            "steam_rental: migrate get_lot_fields(%s) failed",
                            lot_id, exc_info=True)
                gkey = set_game(_slugify_game(game_name), game_name,
                                subcategory_id=sub_id, category_id=cat_id)
                with _lock:
                    lots = list_lots()
                    if lot_id in lots:
                        lots[lot_id]["game_key"] = gkey
                        lots[lot_id]["kind"] = lots[lot_id].get("kind") or (
                            "ext" if lots[lot_id].get("is_extension") else "main"
                        )
                    save_lots(lots)
                    # Добавим в lot_ids/ext_lot_ids игры
                add_lot_to_game(gkey, str(lot_id),
                                kind=lots.get(str(lot_id), {}).get("kind", "main"))
                LOGGER.info(
                    "steam_rental: migrate: lot %s → game '%s' (key=%s)",
                    lot_id, game_name, gkey)
            except Exception:
                LOGGER.warning(
                    "steam_rental: migrate: failed for lot %s", lot_id,
                    exc_info=True)
    except Exception:
        LOGGER.error("steam_rental: games migration crashed", exc_info=True)


def _handler_pre_stop(cardinal: "Cardinal") -> None:
    _stop_event.set()
    _set_cardinal_ref(None)
    _stop_metrics_server()
    # v2.16.1: отменяем все pending TTL-таймеры extension-лотов, чтобы они
    # не сработали после остановки плагина (они держат ссылку на cardinal
    # и могут попытаться дёрнуть API уже после shutdown).
    try:
        with _ext_lot_timers_lock:
            timers = list(_EXT_LOT_TIMERS.values())
            _EXT_LOT_TIMERS.clear()
        for t in timers:
            try:
                t.cancel()
            except Exception:
                pass
    except Exception:
        LOGGER.debug(
            "steam_rental: ext-lot timers cleanup failed", exc_info=True)


# v2.15: ключевые слова для fallback'а распознавания extension-покупки по
# тексту заказа. Срабатывает только если включён
# extension_buyer_fallback_enabled. Намеренно консервативный набор —
# покрывает «ПРОДЛЕНИЕ»/«продли»/«продлеваем»/«продлить»/«extend(ed/sion)»,
# без широких токенов вроде «час», которые встречаются в каждом
# названии аренды.
_EXTENSION_KEYWORDS_RE = re.compile(
    r"продл|extend|extension",
    re.IGNORECASE,
)


def _try_extension_buyer_fallback(
        order: Any, full_order: Any, lot_id: str | None,
        desc: str) -> dict[str, Any] | None:
    """Эвристический fallback: распознать покупку как extension по
    активной аренде покупателя, когда явного матча по lot_id нет.

    Включается флагом ``extension_buyer_fallback_enabled``
    (⚙ Настройки → 🔒 Безопасность). Сценарий, ради которого фича
    появилась: FunPay/FPC отдали заказ без ``subcategory_id`` /
    ``lot_id`` (lot_id=None) — и плагин раньше молча выкидывал
    «лот None НЕ настроен», хотя клиент явно купил лот-продление и у
    него уже есть активная аренда.

    Условия срабатывания (все одновременно):
      1. ``extension_buyer_fallback_enabled`` = True.
      2. В заголовке/описании заказа есть слово из
         ``_EXTENSION_KEYWORDS_RE`` (ПРОДЛЕНИЕ / extend / extension).
      3. У ``order.buyer_id`` есть активная (не истёкшая) аренда.
      4. В тексте заказа НЕ упомянута игра, отличная от игры активной
         аренды (защита от случайного продления чужой игры — если
         клиент одновременно играет в две игры или текст лота
         неоднозначен, лучше не угадывать).
      5. Для аренды нашёлся подходящий extension-лот через
         ``_find_extension_lot_for_alias`` (по game_key /
         extension_games / legacy aliases).

    Возвращает dict ``parent_lot`` той же формы, что и
    ``_is_extension_lot`` (``{"key": ext_id, ...lot_data}``) — этот
    объект потом передаётся в ``_handle_extension_purchase`` 1:1.
    Возвращает None, если хотя бы одно условие не выполнено.
    """
    buyer_id = getattr(order, "buyer_id", None)
    if not buyer_id:
        return None

    title = ""
    if full_order is not None:
        title = (getattr(full_order, "title", None) or "") or ""
    text = (str(title) + " " + str(desc or "")).strip()
    if not text or not _EXTENSION_KEYWORDS_RE.search(text):
        return None

    try:
        active_acc = _find_buyer_active_rental(int(buyer_id))
    except Exception:
        LOGGER.debug(
            "steam_rental: extension fallback — find_buyer_active_rental "
            "raised", exc_info=True)
        return None
    if not active_acc:
        return None
    alias = active_acc.get("alias") or ""
    if not alias:
        return None

    rental_game = (_get_game_for_alias(alias) or "").strip()
    if rental_game:
        text_low = text.lower()
        rental_game_low = rental_game.lower()
        # Если в тексте заказа фигурирует ИМЯ ДРУГОЙ игры из games.json —
        # отказываемся: с большой вероятностью клиент купил extension
        # для другой аренды (которой у него сейчас нет), и продлевать
        # эту — неверно.
        try:
            for _gkey, g in list_games().items():
                other_name = (g.get("name") or "").strip()
                if not other_name:
                    continue
                other_low = other_name.lower()
                if other_low == rental_game_low:
                    continue
                # match по полному имени игры в тексте заказа
                if other_low in text_low:
                    LOGGER.info(
                        "steam_rental: extension fallback skip — текст "
                        "заказа упоминает игру %r, активная аренда у %s "
                        "по игре %r", other_name, alias, rental_game)
                    return None
        except Exception:
            LOGGER.debug(
                "steam_rental: extension fallback — list_games scan failed",
                exc_info=True)

    try:
        ext_key = _find_extension_lot_for_alias(alias)
    except Exception:
        LOGGER.debug(
            "steam_rental: extension fallback — _find_extension_lot_for_alias "
            "raised for alias=%s", alias, exc_info=True)
        return None
    if not ext_key:
        LOGGER.info(
            "steam_rental: extension fallback skip — нет extension-лота "
            "для аренды alias=%s (buyer=%s, game=%r)",
            alias, buyer_id, rental_game)
        return None

    ext_lot = list_lots().get(str(ext_key))
    if not ext_lot:
        return None

    LOGGER.info(
        "steam_rental: extension fallback HIT — заказ #%s buyer=%s alias=%s "
        "ext_lot=%s (lot_id из заказа был %r)",
        getattr(order, "id", "?"), buyer_id, alias, ext_key, lot_id)
    return {"key": str(ext_key), **ext_lot}


def _handler_new_order(cardinal: "Cardinal", event: "NewOrderEvent") -> None:
    try:
        from FunPayAPI.common.enums import OrderStatuses
    except Exception:
        OrderStatuses = None  # type: ignore[assignment]

    try:
        order = event.order
        if OrderStatuses is not None and getattr(order, "status", None) \
                not in (OrderStatuses.PAID,):
            return
        # Лог факта получения нового заказа — для диагностики «купили, но
        # ничего не произошло». Если такой строки нет в actions.log —
        # значит событие до плагина даже не дошло (другой плагин съел
        # / FPC не передал).
        try:
            _log_action("delivery",
                        f"Получен заказ #{getattr(order, 'id', '?')} "
                        f"от {getattr(order, 'buyer_username', '?')}",
                        order_id=getattr(order, "id", None),
                        buyer=getattr(order, "buyer_username", None),
                        buyer_id=getattr(order, "buyer_id", None),
                        desc=getattr(order, "description", "")[:100])
        except Exception:
            pass

        full_order = None
        try:
            full_order = cardinal.get_order_from_object(order)
        except Exception:
            pass

        lot_id = None
        for attr in ("subcategory_id", "lot_id"):
            if full_order is not None:
                lot_id = getattr(full_order, attr, None) or lot_id
        desc = getattr(order, "description", "") or ""

        # Полный текст описания лота для парсинга тегов (#Hours:/#Time:/#Review:)
        # и срока. order.description — это лишь короткое ИМЯ лота, тегов в нём
        # НЕТ; сами теги живут в полном описании лота на странице заказа
        # (Order.full_description). Поэтому раньше #Hours: из описания лота не
        # читался и срок брался из duration_min лота. Собираем всё доступное.
        def _attr(obj, name):
            return (getattr(obj, name, "") or "") if obj is not None else ""
        desc_full = " ".join(s for s in (
            desc,
            _attr(order, "title"),
            _attr(order, "full_description"),
            _attr(full_order, "full_description"),
            _attr(full_order, "description"),
            _attr(full_order, "title"),
        ) if s).strip()

        if lot_id is None and full_order is not None:
            for attr in ("html", "raw_html"):
                html = getattr(full_order, attr, "") or ""
                m = re.search(r"offers/(\d+)", html)
                if m:
                    lot_id = m.group(1)
                    break

        cfg = get_config()

        # v5: blacklist-чек. Срабатывает ДО extension-лотов и ДО match_lot,
        # чтобы заблокированный покупатель ничего не получил.
        # v2.22.2: упрочнили выборку buyer_id/username — пробуем сначала
        # event.order, потом full_order. FunPay/FPC иногда отдаёт
        # buyer_id=None в event.order, и тогда раньше is_blacklisted
        # запускалось с пустыми ключами и тихо возвращало False —
        # покупатель из ЧС спокойно покупал ещё раз.
        buyer_id_v = (getattr(order, "buyer_id", None)
                      or getattr(full_order, "buyer_id", None))
        buyer_un_v = (getattr(order, "buyer_username", None)
                      or getattr(full_order, "buyer_username", None))
        bl_hit = is_blacklisted(buyer_id_v, buyer_un_v) \
            if (buyer_id_v or buyer_un_v) else False
        bl_enabled = bool(cfg.get("blacklist_enabled", True))
        # Подробный лог для диагностики «купил несмотря на blacklist».
        LOGGER.info(
            "steam_rental: blacklist-check order=%s buyer=%s id=%s "
            "blacklist_enabled=%s is_blacklisted=%s bl_size=%d",
            getattr(order, "id", "?"), buyer_un_v, buyer_id_v,
            bl_enabled, bl_hit, len(list_blacklist()))
        if bl_hit and bl_enabled:
            _metric_inc("blocked_blacklist_total")
            LOGGER.info(
                "steam_rental: blacklist hit — заказ %s от %s (id=%s) "
                "проигнорирован", order.id, buyer_un_v, buyer_id_v)
            _notify_tg(cardinal,
                       f"🚫 <b>Steam Rental</b>: заказ #{order.id} "
                       f"от <b>{buyer_un_v}</b> "
                       f"(id <code>{buyer_id_v}</code>) "
                       f"проигнорирован — покупатель в blacklist.")
            return
        if bl_hit and not bl_enabled:
            # Покупатель в ЧС, но опция «Блокировка на NEW_ORDER» выключена.
            # Не блокируем (поведение по настройке), но громко
            # уведомляем оператора, чтобы баг «не работает blacklist» был
            # очевиден сразу.
            _notify_tg(cardinal,
                       f"⚠️ <b>Steam Rental</b>: заказ #{order.id} от "
                       f"<b>{buyer_un_v}</b> "
                       f"(id <code>{buyer_id_v}</code>) — "
                       f"покупатель в blacklist, НО опция "
                       f"«Блокировка на NEW_ORDER» выключена в настройках, "
                       f"поэтому заказ выдаётся. Включи в "
                       f"<code>/srental → ⚙ Настройки → 🚫 Blacklist</code>.")
            LOGGER.warning(
                "steam_rental: blacklist hit для %s, но blacklist_enabled=False"
                " — заказ %s НЕ блокируется", buyer_un_v, order.id)

        # Проверяем: это extension-лот?
        parent_lot = _is_extension_lot(lot_id)
        if parent_lot:
            _handle_extension_purchase(
                cardinal, order, parent_lot, cfg)
            return

        # v2.15: опциональный fallback — если lot_id не распознан как
        # extension (часто потому, что FunPay/FPC отдали заказ без
        # lot_id), но в title/desc есть «ПРОДЛЕНИЕ»/«extend» и у
        # покупателя есть активная аренда — продлить её через
        # extension-лот, привязанный к её игре. По умолчанию ВЫКЛ.
        if cfg.get("extension_buyer_fallback_enabled", False):
            fb_parent = _try_extension_buyer_fallback(
                order, full_order, lot_id, desc)
            if fb_parent:
                _handle_extension_purchase(cardinal, order, fb_parent, cfg)
                return

        # v6: матчинг в 2 уровня —
        # 1) по игре (Order.title → games.json.name → main-лот игры)
        # 2) legacy фоллбэк (lot_id / keyword)
        order_title = None
        if full_order is not None:
            order_title = getattr(full_order, "title", None) or None
        if not order_title:
            order_title = desc
        lot = _match_lot_by_game(order_title) or _match_lot(desc, lot_id)
        if not lot:
            LOGGER.warning(
                "steam_rental: НЕ НАЙДЕН ЛОТ для заказа #%s (lot_id=%s, "
                "desc=%r). Возможно, этого лота нет в БД плагина — "
                "добавь его через /srental → 🎯 Лоты.",
                getattr(order, "id", "?"), lot_id, desc[:200])
            _log_action("lot_save_failed",
                        f"Заказ #{getattr(order, 'id', '?')} — лот не "
                        f"найден в БД плагина",
                        order_id=getattr(order, "id", None),
                        lot_id=lot_id,
                        buyer=getattr(order, "buyer_username", None),
                        desc=desc[:120])
            _notify_tg(cardinal,
                       f"⚠️ <b>Steam Rental</b>: заказ "
                       f"<code>#{getattr(order, 'id', '?')}</code> "
                       f"от <b>{getattr(order, 'buyer_username', '?')}</b>"
                       f" — лот <code>{lot_id}</code> НЕ настроен в плагине.\n"
                       f"Добавь лот через <code>/srental → 🎯 Лоты</code> "
                       f"или верни деньги.\n\n"
                       f"Описание: <code>{_esc(desc[:200])}</code>")
            return

        # auto_deliver: если автовыдача выключена в настройках — не выдаём,
        # а оставляем заказ на ручную обработку (с уведомлением оператора).
        if not cfg.get("auto_deliver", True):
            LOGGER.info("steam_rental: auto_deliver выключен, заказ %s "
                        "оставлен на ручную выдачу", order.id)
            _log_action("lot_save_failed",
                        f"Заказ #{order.id} — auto_deliver выключен",
                        order_id=getattr(order, "id", None),
                        lot_id=lot.get("key"),
                        buyer=getattr(order, "buyer_username", None))
            _notify_tg(cardinal,
                       f"📦 <b>Steam Rental</b>: оплачен лот "
                       f"<code>{lot.get('key')}</code> "
                       f"(заказ #{order.id}, покупатель "
                       f"{getattr(order, 'buyer_username', '?')}). "
                       f"Автовыдача выключена — выдай вручную.")
            return

        # ── Длительность аренды: ТОЛЬКО из тэгов в описании лота ────
        # Единый детерминированный источник: либо #Hours:, либо #Time:
        # в описании лота на FunPay. Никаких эвристик «2 часа» из
        # названия и никаких UI-фолбэков из конфига плагина — это
        # ровно то, что просил пользователь, чтобы исключить путаницу
        # «лот «24 часа» выдан как 1 час» и подобные.
        # Для extension-лотов длительность по-прежнему берётся из
        # их `duration_min` (там это значение продления, заданное в
        # отдельном extension-визарде) — но эта ветка обрабатывает
        # main-заказ и до этой строки extension-кейс уже отработан
        # выше через _handle_extension_purchase.
        duration_min = 0
        _ht_hours = _parse_hashtag_hours(desc_full)
        if _ht_hours is not None and _ht_hours > 0:
            duration_min = _ht_hours
            LOGGER.debug("steam_rental: order %s — duration из #Hours: %d мин",
                         getattr(order, "id", "?"), _ht_hours)
        else:
            _ht_time = _parse_hashtag_time(desc_full)
            if _ht_time is not None and _ht_time > 0:
                duration_min = _ht_time
                LOGGER.debug(
                    "steam_rental: order %s — duration из #Time: %d мин",
                    getattr(order, "id", "?"), _ht_time)
        # #Review: бонус (минут за 5★ отзыв) — параллельный канал
        _ht_review = _parse_hashtag_review(desc_full)
        if duration_min <= 0:
            LOGGER.warning(
                "steam_rental: для заказа %s не найдено #Hours:/#Time: в "
                "описании лота (desc=%r)",
                getattr(order, "id", "?"), desc_full[:200])
            _log_action("lot_save_failed",
                        f"Заказ #{order.id} — нет #Hours:/#Time: в описании",
                        order_id=getattr(order, "id", None),
                        lot_id=lot.get("key"),
                        buyer=getattr(order, "buyer_username", None),
                        desc=desc_full[:120])
            _notify_tg(cardinal,
                       f"⚠️ <b>Steam Rental</b>: заказ "
                       f"<code>#{order.id}</code> от "
                       f"<b>{order.buyer_username}</b> — в описании лота "
                       f"<code>{lot.get('key')}</code> на FunPay не найден "
                       f"тэг <code>#Hours: 24</code> или "
                       f"<code>#Time: 2ч</code>. Без него бот не знает "
                       f"срок аренды. Добавь тэг в описание лота на "
                       f"FunPay и нажми «обновить лоты», или выдай "
                       f"вручную.")
            return

        # ── PC-club mode: задерживаем выдачу до подтверждения фото ──
        club_mode = bool(lot.get("club_mode"))
        if club_mode and cfg.get("club_mode_global_enabled", False):
            if not _club_in_whitelist(order.buyer_id):
                try:
                    _start_club_verification(
                        cardinal, order, lot, duration_min)
                except Exception:
                    LOGGER.error(
                        "steam_rental: club verification start crash",
                        exc_info=True)
                return  # выдача отложена до прохождения проверки
            else:
                LOGGER.info(
                    "steam_rental: club whitelist hit для %s, выдаём сразу",
                    order.buyer_username)

        # ── v6: Manual photo-review (PC-club, ручное одобрение) ─────
        # Если у лота включён `manual_review` — просим у покупателя фото,
        # пересылаем владельцу в TG и ждём решения. Approve → выдача через
        # обычный поток. Decline → cardinal.account.refund + уведомление.
        if bool(lot.get("manual_review")):
            try:
                _mr_start(cardinal, order, lot, duration_min)
            except Exception:
                LOGGER.error(
                    "steam_rental: manual_review start crash",
                    exc_info=True)
            return  # выдача отложена

        amount = int(getattr(order, "amount", 1) or 1)
        if amount < 1:
            amount = 1
        # При покупке N штук одного лота: выдаём ОДИН аккаунт с длительностью
        # duration_min * N (накопительная аренда), а не N разных аккаунтов.
        total_duration = duration_min * amount

        # Применяем лимиты длительности из настроек (в часах → минуты).
        _min_h = int(cfg.get("min_rental_hours", 0) or 0)
        _max_h = int(cfg.get("max_rental_hours", 0) or 0)
        if _min_h > 0 and total_duration < _min_h * 60:
            LOGGER.info("steam_rental: заказ %s — длительность %dм поднята "
                        "до min_rental_hours=%dч", order.id, total_duration, _min_h)
            total_duration = _min_h * 60
        if _max_h > 0 and total_duration > _max_h * 60:
            LOGGER.info("steam_rental: заказ %s — длительность %dм урезана "
                        "до max_rental_hours=%dч", order.id, total_duration, _max_h)
            total_duration = _max_h * 60

        # Запоминаем цену заказа для статистики выручки/ROI (deliver_account
        # читает cardinal._sr_last_price). Раньше оно нигде не присваивалось.
        try:
            _price_v = None
            if full_order is not None:
                _sum_obj = getattr(full_order, "sum", None)
                if _sum_obj is not None:
                    _price_v = getattr(_sum_obj, "value", None)
                if _price_v is None:
                    _price_v = getattr(full_order, "price", None)
            if _price_v is None:
                _price_v = getattr(order, "price", None)
            cardinal._sr_last_price = float(_price_v) if _price_v is not None else None
        except Exception:
            cardinal._sr_last_price = None

        # ── Бронь конкретного аккаунта (Irent <login>) ──
        # Если у покупателя есть активная бронь под ЭТОТ лот — выдаём
        # именно зарезервированный alias. Если бронь протухла или акк
        # стал занят к моменту оплаты — НЕ выдаём случайный, шлём
        # покупателю предупреждение и зовём оператора в TG.
        alias: str | None = None
        reserved_alias_failed = False
        if cfg.get("reservations_enabled", True):
            try:
                _purge_expired_reservations()
                buyer_id_r = getattr(order, "buyer_id", None)
                if buyer_id_r is not None:
                    order_lot_key = str(lot.get("key") or "")
                    res = _find_reservation_for_buyer(
                        int(buyer_id_r), lot_key=order_lot_key)
                    # lot_key-фильтр выше может отбросить бронь, если
                    # acc живёт сразу в нескольких лотах одной игры
                    # (lot_key в бронe ≠ lot_key матченного ордера).
                    # Делаем явный peek без фильтра — для DEBUG-лога,
                    # чтобы такие расхождения были видны в cardinal.log.
                    if res is None:
                        any_res = _find_reservation_for_buyer(
                            int(buyer_id_r))
                        if any_res is not None:
                            r_alias, r_entry = any_res
                            r_lot_key = str(r_entry.get("lot_key") or "")
                            if r_alias in lot.get("aliases", []) \
                                    and r_lot_key != order_lot_key:
                                LOGGER.debug(
                                    "steam_rental: order %s — у покупателя"
                                    " есть бронь %s, но lot_key брони (%s)"
                                    " ≠ lot_key ордера (%s); бронь не"
                                    " применена",
                                    order.id, r_alias, r_lot_key,
                                    order_lot_key)
                    if res is not None:
                        cand_alias, _entry = res
                        if cand_alias in lot.get("aliases", []):
                            cand_acc = find_account(cand_alias)
                            if _is_alias_rentable(cand_acc):
                                alias = cand_alias
                                LOGGER.info(
                                    "steam_rental: order %s — использую "
                                    "забронированный alias %s",
                                    order.id, cand_alias)
                                _log_action(
                                    "reservation_consumed",
                                    f"Бронь {cand_alias} применена к "
                                    f"заказу #{order.id}",
                                    order_id=order.id,
                                    alias=cand_alias,
                                    buyer=getattr(order, "buyer_username",
                                                  None))
                            else:
                                reserved_alias_failed = True
                                login_lost = (cand_acc or {}).get(
                                    "account_name") or cand_alias
                                _release_reservation(cand_alias)
                                LOGGER.warning(
                                    "steam_rental: order %s — забронированный"
                                    " alias %s стал недоступен",
                                    order.id, cand_alias)
                                try:
                                    cardinal.send_message(
                                        order.chat_id,
                                        _render_template(
                                            "reserve_expired_at_pay",
                                            buyer_id=order.buyer_id,
                                            login=login_lost),
                                        chat_name=order.buyer_username,
                                        interlocutor_id=order.buyer_id,
                                        watermark=False)
                                except Exception:
                                    LOGGER.debug(
                                        "steam_rental: reserve_expired_at_pay"
                                        " send failed", exc_info=True)
                                _notify_tg(
                                    cardinal,
                                    f"⚠️ <b>Steam Rental</b>: бронь "
                                    f"<code>{_esc(cand_alias)}</code> "
                                    f"(<code>{_esc(login_lost)}</code>) "
                                    f"покупателя <b>"
                                    f"{_esc(str(order.buyer_username))}"
                                    f"</b> ПОТЕРЯНА к оплате заказа "
                                    f"#{order.id}. Случайный акк не выдаём — "
                                    f"верни деньги или выдай вручную.")
                                _log_action(
                                    "reservation_lost",
                                    f"Бронь {cand_alias} утеряна на оплате",
                                    order_id=order.id,
                                    alias=cand_alias,
                                    buyer=getattr(order, "buyer_username",
                                                  None))
            except Exception:
                LOGGER.error("steam_rental: reservation pre-pick crashed",
                             exc_info=True)

        if reserved_alias_failed:
            # Жёсткая политика как на скрине: «случайный аккаунт выдан
            # не будет» — выходим без выдачи.
            return

        # ── Одноразовый приоритет (!priority <login>) ─────────────────
        # В отличие от reservation: НЕ блокирует выдачу. Если приоритетный
        # alias свободен — выдадим его; иначе оставим alias=None и выдадим
        # случайный. priority_consume_buyer_id ставим ТОЛЬКО при успешном
        # применении (фикс ревью: «одноразовый» = «одна удачная выдача»).
        priority_consume_buyer_id: int | None = None
        if alias is None and cfg.get("priority_enabled", True):
            try:
                buyer_id_p = getattr(order, "buyer_id", None)
                if buyer_id_p is not None:
                    pri = _get_priority(int(buyer_id_p))
                    if pri:
                        cand_alias = pri.get("alias")
                        if cand_alias and cand_alias in lot.get("aliases", []):
                            cand_acc = find_account(cand_alias)
                            if _is_alias_rentable(cand_acc):
                                alias = cand_alias
                                priority_consume_buyer_id = int(buyer_id_p)
                                LOGGER.info(
                                    "steam_rental: order %s — приоритет "
                                    "%s применён", order.id, cand_alias)
                                _log_action(
                                    "priority_consumed",
                                    f"Приоритет {cand_alias} применён к "
                                    f"заказу #{order.id}",
                                    order_id=order.id, alias=cand_alias,
                                    buyer=getattr(order, "buyer_username",
                                                  None))
                            else:
                                LOGGER.info(
                                    "steam_rental: order %s — приоритет "
                                    "%s занят, выдам случайный (приоритет"
                                    " остаётся до следующей покупки)",
                                    order.id, cand_alias)
            except Exception:
                LOGGER.error("steam_rental: priority pre-pick crashed",
                             exc_info=True)

        if alias is None:
            alias = _pick_free_alias(
                _combined_lot_pool(lot),
                exclude_other_reserved_for_buyer=getattr(order, "buyer_id",
                                                        None))
        delivered = 0
        delivered_alias: str | None = None
        # Race-condition: _pick_free_alias читает accounts.json без `_lock`,
        # поэтому два одновременных заказа на один и тот же лот могут
        # выбрать ОДИН и тот же alias. Под `_lock` побеждает первый
        # deliver_*(...), второй увидит acc.rental != None и вернёт False.
        # До v2.14 handler в этом случае молча выходил — покупатель не
        # получал НИ выдачи, НИ сообщения «нет свободных». Делаем
        # retry-loop с пере-выбором alias, а если пул реально исчерпан —
        # падаем в no_accounts-ветку с указанием, через сколько
        # освободится ближайший аккаунт.
        pool_size = max(1, len(_combined_lot_pool(lot)))
        max_attempts = min(8, pool_size + 1)
        attempts = 0
        while alias and attempts < max_attempts:
            attempts += 1
            if lot.get("type") == "remoteplay":
                rp_result = deliver_remoteplay(
                    cardinal, alias=alias,
                    duration_min=total_duration,
                    order_id=str(order.id),
                    buyer_username=str(order.buyer_username),
                    buyer_id=int(order.buyer_id),
                    chat_id=order.chat_id,
                )
                if rp_result:
                    delivered = 1
                    delivered_alias = alias
                    break
            else:
                ok = deliver_account(cardinal, alias=alias,
                                     duration_min=total_duration,
                                     order_id=order.id,
                                     buyer_username=order.buyer_username,
                                     buyer_id=order.buyer_id,
                                     chat_id=order.chat_id,
                                     review_bonus_minutes=_ht_review)
                if ok:
                    delivered = 1
                    delivered_alias = alias
                    break
            # Race lost: alias стал занят между _pick_free_alias и deliver_*.
            LOGGER.info(
                "steam_rental: order %s — alias %s занят (race "
                "condition), пробую другой (попытка %d/%d)",
                order.id, alias, attempts, max_attempts)
            # Если raced был именно приоритетный alias — приоритет НЕ
            # считаем израсходованным (та же логика, что и «priority alias
            # был занят с самого начала»). Сбрасываем consume-флаг, чтобы
            # фоллбэк-выдача случайного акка не списала приоритет.
            if priority_consume_buyer_id is not None:
                priority_consume_buyer_id = None
            alias = _pick_free_alias(
                _combined_lot_pool(lot),
                exclude_other_reserved_for_buyer=getattr(order, "buyer_id",
                                                        None))

        if not delivered:
            LOGGER.warning(
                "steam_rental: нет свободных аккаунтов для лота %s "
                "(пул=%s)", lot.get("key"), _combined_lot_pool(lot))
            _log_action("lot_save_failed",
                        f"Нет свободных аккаунтов для лота "
                        f"{lot.get('key')} (заказ #{order.id})",
                        order_id=getattr(order, "id", None),
                        lot_id=lot.get("key"),
                        buyer=getattr(order, "buyer_username", None),
                        aliases=",".join(lot.get("aliases", [])[:5]))
            game = lot.get("game", "")
            # Подсказка покупателю: через сколько освободится ближайший
            # аккаунт в пуле. None → в пуле некого ждать (все frozen или
            # пул пуст) — отдадим прочерк.
            pool = _combined_lot_pool(lot)
            next_free_at = _next_free_at_for_pool(pool)
            if next_free_at:
                next_free_str = _fmt_ts(next_free_at)
                next_free_min = max(
                    0, (next_free_at - _now() + 59) // 60)
                next_free_in_str = _human_minutes(int(next_free_min))
            else:
                next_free_str = "—"
                next_free_in_str = "—"
            no_acc_tpl = ("no_accounts_rp" if lot.get("type") == "remoteplay"
                          else "no_accounts")
            text = _render_template(
                no_acc_tpl,
                buyer_id=order.buyer_id,
                game=game or "",
                next_free=next_free_str,
                next_free_in=next_free_in_str)
            try:
                cardinal.send_message(
                    order.chat_id, text,
                    chat_name=order.buyer_username,
                    interlocutor_id=order.buyer_id, watermark=False)
            except Exception:
                LOGGER.debug("steam_rental: no_account msg failed",
                             exc_info=True)
            _notify_tg(
                cardinal,
                f"⚠️ <b>Steam Rental</b>: нет свободных аккаунтов для "
                f"лота <code>{lot.get('key')}</code> "
                f"(заказ #{order.id}, покупатель {order.buyer_username})."
                + (f" Ближайший освободится через {next_free_in_str} "
                   f"({next_free_str} МСК)."
                   if next_free_at else "")
                + " Выдай вручную или верни деньги.")

        LOGGER.info("steam_rental: order %s — qty=%d, выдан %s "
                    "(длительность=%dм)",
                    order.id, amount, delivered_alias or "—", total_duration)
        # Снимаем бронь после выдачи (если была) — alias теперь занят арендой,
        # но явное освобождение чище чем ждать пока бронь протухнет по TTL.
        # Если выдача ушла НЕ владельцу брони — логируем как сигнал
        # «бронь не сработала» (см. ревью).
        if delivered and delivered_alias:
            try:
                _release_reservation_after_delivery(
                    delivered_alias,
                    delivered_to_buyer_id=getattr(order, "buyer_id", None))
            except Exception:
                LOGGER.debug("steam_rental: release reservation failed",
                             exc_info=True)
        # Одноразовый приоритет — сбрасываем после ЛЮБОЙ выдачи (даже если
        # выпал случайный, т.к. приоритет одноразовый по ТЗ).
        if delivered and priority_consume_buyer_id is not None:
            try:
                _consume_priority(priority_consume_buyer_id)
            except Exception:
                LOGGER.debug("steam_rental: consume priority failed",
                             exc_info=True)
    except Exception:
        LOGGER.error("steam_rental: handler_new_order crashed", exc_info=True)


def _handle_extension_purchase(cardinal: "Cardinal", order: Any,
                                parent_lot: dict[str, Any],
                                cfg: dict[str, Any]) -> None:
    if not cfg.get("auto_extend_enabled", True):
        return
    buyer_id = getattr(order, "buyer_id", None)
    buyer_username = getattr(order, "buyer_username", "")
    chat_id = getattr(order, "chat_id", None)

    if not buyer_id or not chat_id:
        LOGGER.warning("steam_rental: extension purchase — no buyer_id/chat_id")
        return

    # v2.21: срок продления резолвим в порядке (от самого приоритетного):
    #   1) #Hours: или #Time: в описании лота (как у main-лотов),
    #   2) lot.duration_min — персональный дефолт лота из старого
    #      newext_duration-визарда (бесшовная миграция),
    #   3) extension_default_minutes из конфига (по умолчанию 60).
    # Это устраняет различие между main и ext по семантике источника
    # срока: оба приоритетно читают теги из описания на FunPay.
    def _attr(o, name):
        return (getattr(o, name, "") or "") if o is not None else ""
    full_order = None
    try:
        full_order = cardinal.get_order_from_object(order)
    except Exception:
        pass
    desc_full = " ".join(s for s in (
        _attr(order, "description"),
        _attr(order, "title"),
        _attr(order, "full_description"),
        _attr(full_order, "full_description"),
        _attr(full_order, "description"),
        _attr(full_order, "title"),
    ) if s).strip()

    duration_min = 0
    _ht_hours = _parse_hashtag_hours(desc_full)
    if _ht_hours is not None and _ht_hours > 0:
        duration_min = _ht_hours
        LOGGER.debug(
            "steam_rental: ext order %s — duration из #Hours: %d мин",
            getattr(order, "id", "?"), _ht_hours)
    else:
        _ht_time = _parse_hashtag_time(desc_full)
        if _ht_time is not None and _ht_time > 0:
            duration_min = _ht_time
            LOGGER.debug(
                "steam_rental: ext order %s — duration из #Time: %d мин",
                getattr(order, "id", "?"), _ht_time)
    if duration_min <= 0:
        # Per-lot fallback: значение из старого newext_duration-визарда.
        _per_lot = int(parent_lot.get("duration_min") or 0)
        if _per_lot > 0:
            duration_min = _per_lot
            LOGGER.info(
                "steam_rental: ext order %s — нет тэга в описании, "
                "беру lot.duration_min=%d мин (legacy)",
                getattr(order, "id", "?"), _per_lot)
    if duration_min <= 0:
        # Глобальный дефолт.
        duration_min = int(cfg.get("extension_default_minutes", 60) or 60)
        LOGGER.info(
            "steam_rental: ext order %s — нет ни тэга, ни lot.duration_min, "
            "использую extension_default_minutes=%d мин",
            getattr(order, "id", "?"), duration_min)
    # При покупке N штук extension-лота — продлеваем на duration_min * N.
    amount = int(getattr(order, "amount", 1) or 1)
    if amount < 1:
        amount = 1
    duration_min *= amount
    game = parent_lot.get("game", "")
    target_games = _extension_target_games(parent_lot)

    # Сначала ищем активную аренду с одной из целевых игр.
    acc = None
    for g in target_games:
        acc = _find_buyer_active_rental(int(buyer_id), g)
        if acc:
            break
    # Fallback: любая активная аренда покупателя.
    if not acc:
        acc = _find_buyer_active_rental(int(buyer_id))

    if not acc:
        LOGGER.warning("steam_rental: extension — нет активной аренды для buyer %s",
                        buyer_id)
        try:
            cardinal.send_message(
                chat_id,
                "У вас нет активной аренды для продления. "
                "Обратитесь к продавцу.",
                chat_name=buyer_username,
                interlocutor_id=buyer_id, watermark=False)
        except Exception:
            pass
        return

    alias = acc["alias"]
    new_expires = _extend_rental(alias, duration_min, reason="extension_purchase")
    hours = duration_min / 60

    text = _render_template(
        "extended",
        buyer_id=buyer_id,
        hours=f"{hours:.0f}" if hours == int(hours) else f"{hours:.1f}",
        new_expires=_fmt_ts(new_expires),
        login=acc["account_name"],
        game=game or _get_game_for_alias(alias) or "—",
    )

    try:
        cardinal.send_message(chat_id, text, chat_name=buyer_username,
                              interlocutor_id=buyer_id, watermark=False)
    except Exception:
        LOGGER.debug("steam_rental: extension msg failed", exc_info=True)

    LOGGER.info("steam_rental: аренда %s продлена на %d мин для %s (extension purchase)",
                alias, duration_min, buyer_username)
    _notify_tg(cardinal,
               f"➕ <b>Steam Rental</b>: аренда <code>{alias}</code> "
               f"продлена на {_human_minutes(duration_min)} "
               f"(покупка extension-лота, покупатель {buyer_username}).")

    # Если купленный лот помечен как is_extension — деактивируем его обратно
    # на FunPay (он активируется снова только по команде !продлить).
    parent_key = parent_lot.get("key")
    if parent_key and parent_lot.get("is_extension"):
        # v2.16.1: оплата пришла — отменяем pending TTL-таймер, чтобы он
        # не пытался выключить уже выключаемый нами лот.
        try:
            _cancel_ext_lot_deactivation(parent_key)
        except Exception:
            LOGGER.debug(
                "steam_rental: cancel ext TTL timer failed for %s",
                parent_key, exc_info=True)
        if _set_funpay_lot_active(cardinal, parent_key, False):
            LOGGER.info(
                "steam_rental: extension lot %s деактивирован после покупки",
                parent_key)


# Регекс fallback для системных сообщений FunPay про отзыв
# (используется, только если MessageTypes недоступен в этой версии FPC).
# Ловит:
#   «Покупатель X написал отзыв к заказу #BRZQ5QKX.»
#   «Покупатель X изменил отзыв к заказу #BRZQ5QKX.»
#   «Покупатель X удалил отзыв к заказу #GZ1T687T.»  (v2.19)
#   «Продавец Y ответил на отзыв к заказу #BRZQ5QKX.»
_REVIEW_TEXT_RE = re.compile(
    r"(?:написал|изменил|удалил|ответил на)\s+отзыв.*?#\s*([A-Z0-9]{6,12})",
    re.IGNORECASE)

# v2.19: отдельный регекс именно для УДАЛЕНИЯ отзыва — нужен только
# для пенальти-пути, чтобы не путать с «написал/изменил».
_REVIEW_DELETED_RE = re.compile(
    r"удалил\s+отзыв.*?#\s*([A-Z0-9]{6,12})",
    re.IGNORECASE)


def _extract_review_deleted_order_id(msg: Any) -> str | None:
    """Если это системное сообщение FunPay про УДАЛЕНИЕ отзыва — вернёт
    order_id, иначе None. Самый надёжный сигнал «отзыв реально удалили»;
    предпочтителен перед эвристикой stars==0 в OrderStatusChangedEvent
    (та срабатывает ложно на extension-покупках).
    """
    text = (getattr(msg, "text", "") or "")
    if not text:
        return None
    m = _REVIEW_DELETED_RE.search(text)
    if not m:
        return None
    return m.group(1).strip() or None


def _is_review_system_message(msg: Any) -> bool:
    """Системное сообщение FunPay про отзыв?

    Сначала FPC-нативный путь через msg.type / Message.get_message_type()
    (см. FunPayAPI.types.Message.get_message_type — детектит
    NEW_FEEDBACK / FEEDBACK_CHANGED / NEW_FEEDBACK_ANSWER через
    встроенные регексы FunPayAPI). Если недоступно — fallback на
    regex по тексту.
    """
    try:
        from FunPayAPI.common.enums import MessageTypes  # type: ignore
        feedback_types = {
            getattr(MessageTypes, "NEW_FEEDBACK", None),
            getattr(MessageTypes, "FEEDBACK_CHANGED", None),
            getattr(MessageTypes, "NEW_FEEDBACK_ANSWER", None),
        }
        feedback_types.discard(None)
        msg_type = getattr(msg, "type", None)
        if msg_type is None:
            getter = getattr(msg, "get_message_type", None)
            if callable(getter):
                try:
                    msg_type = getter()
                except Exception:
                    msg_type = None
        if msg_type is not None and msg_type in feedback_types:
            return True
    except Exception:
        pass

    # Fallback: regex по тексту (для совместимости со старыми FPC).
    text = (getattr(msg, "text", "") or "")
    if not text:
        return False
    if _REVIEW_TEXT_RE.search(text):
        return True
    return False


def _trigger_review_bonus_from_msg(cardinal: "Cardinal", msg: Any) -> None:
    """Backup-путь для бонуса за отзыв через системное сообщение чата.

    Через 3 сек (время на пропагацию ревью в JSON API FunPay) получаем
    свежий Order. Предпочтительно — через cardinal.get_order_from_object(msg)
    (FPC-нативный helper из cardinal.py: 3 попытки + кэширование на
    самом msg-объекте через _order_attempt_made / _order). Fallback
    на cardinal.account.get_order(order_id) если helper недоступен.
    Затем собираем шим-event и вызываем _handler_order_status_changed().
    Идемпотентность через rental.review_bonus_applied.
    """
    text = (getattr(msg, "text", "") or "")
    # Извлекаем order_id (для логов и fallback-пути).
    order_id = ""
    m = _REVIEW_TEXT_RE.search(text)
    if m:
        order_id = m.group(1).strip()

    LOGGER.info(
        "steam_rental: chat-trigger review bonus для order=%s "
        "(тип сообщения: %s, текст: '%s…')",
        order_id or "?",
        getattr(msg, "type", None),
        text[:60].replace("\n", " "))

    def _run():
        try:
            time.sleep(3.0)
            fresh_order = None
            # 1) FPC-нативный helper — кэширует и retry'ит.
            getter = getattr(cardinal, "get_order_from_object", None)
            if callable(getter):
                try:
                    fresh_order = getter(msg)
                except Exception:
                    LOGGER.debug(
                        "steam_rental: cardinal.get_order_from_object failed",
                        exc_info=True)
            # 2) Fallback: явный get_order(order_id).
            if fresh_order is None and order_id:
                acc_obj = getattr(cardinal, "account", None)
                if acc_obj is not None and hasattr(acc_obj, "get_order"):
                    try:
                        fresh_order = acc_obj.get_order(order_id)
                    except Exception:
                        LOGGER.debug(
                            "steam_rental: chat-trigger get_order(%s) failed",
                            order_id, exc_info=True)
            if fresh_order is None:
                LOGGER.info(
                    "steam_rental: chat-trigger — не удалось получить "
                    "Order для order=%s", order_id or "?")
                return

            class _ShimEvent:
                __slots__ = ("order",)

                def __init__(self, order):
                    self.order = order

            try:
                _handler_order_status_changed(
                    cardinal, _ShimEvent(fresh_order))  # type: ignore[arg-type]
            except Exception:
                LOGGER.exception(
                    "steam_rental: chat-trigger handler call failed for "
                    "order=%s", order_id or "?")
        except Exception:
            LOGGER.debug(
                "steam_rental: chat-trigger background thread failed",
                exc_info=True)

    threading.Thread(
        target=_run, daemon=True,
        name=f"sr-review-trigger-{order_id or 'unknown'}").start()


def _handler_order_status_changed(cardinal: "Cardinal",
                                   event: "OrderStatusChangedEvent") -> None:
    """Обработка изменения статуса заказа.

    v5: ловим REFUND/CANCEL → авто-добавление buyer в blacklist.
    Старое поведение (бонус за 5★ отзыв) сохранено."""
    cfg = get_config()

    try:
        order = event.order
        buyer_id_v = getattr(order, "buyer_id", None)
        buyer_un_v = getattr(order, "buyer_username", None)
        order_id_v = getattr(order, "id", None)

        # ── REFUND / CANCEL detection (общее для refund-stats и blacklist) ─
        try:
            from FunPayAPI.common.enums import OrderStatuses as _OS
        except Exception:
            _OS = None  # type: ignore[assignment]
        status_v = getattr(order, "status", None)
        is_refund = False
        if _OS is not None:
            for attr_name in ("REFUNDED", "REFUND", "CANCELED",
                              "CANCELLED", "REVERSED"):
                target = getattr(_OS, attr_name, None)
                if target is not None and status_v == target:
                    is_refund = True
                    break
        else:
            status_str = str(status_v).lower() if status_v else ""
            is_refund = any(
                tok in status_str
                for tok in ("refund", "cancel", "revers"))

        if is_refund:
            # ── списываем деньги с прибыли за аренду ───────────────
            # На refund/cancel: вычитаем выручку из per-account stats
            # и пишем в history событие 'refund' с отрицательной
            # суммой, чтобы дневная/недельная/месячная выручка тоже
            # уменьшилась. Идемпотентно — повторный refund по тому же
            # order_id не дублирует списание. Всегда работает, не
            # зависит от настройки auto_blacklist_on_refund.
            try:
                refund_alias, refund_amount = _apply_refund_to_stats(
                    order_id_v, buyer_un_v, buyer_id_v)
            except Exception:
                LOGGER.error(
                    "steam_rental: _apply_refund_to_stats(%s) crash",
                    order_id_v, exc_info=True)
                refund_alias, refund_amount = None, 0.0
            if refund_alias and refund_amount > 0:
                _notify_tg(cardinal,
                           f"💸 <b>Steam Rental</b>: возврат по заказу "
                           f"#{order_id_v} — выручка "
                           f"<b>−{refund_amount:.2f}</b>₽ списана с "
                           f"аккаунта <code>{refund_alias}</code>.")

        # ── v5: REFUND / CANCEL → blacklist ───────────────────────────
        # v2.23.2: добавляем в blacklist ТОЛЬКО если это НАШ заказ (т.е.
        # _apply_refund_to_stats нашёл start-event в нашем history).
        # Раньше blacklist-add срабатывал для ВСЕХ refund-ов, включая
        # заказы других плагинов — покупатель попадал в ЧС даже если
        # аккаунт ему не выдавался этим плагином.
        if cfg.get("auto_blacklist_on_refund", True) and refund_alias:
            if is_refund and (buyer_id_v or buyer_un_v):
                if add_to_blacklist(
                        buyer_id_v, buyer_un_v,
                        reason=f"refund order={order_id_v}"):
                    _metric_inc("blacklist_auto_refund_total")
                    _notify_tg(cardinal,
                               f"🚫 <b>Steam Rental</b>: покупатель "
                               f"<b>{buyer_un_v}</b> "
                               f"(id <code>{buyer_id_v}</code>) "
                               f"добавлен в blacklist после refund/cancel "
                               f"заказа #{order_id_v}.")

        if not cfg.get("review_bonus_enabled"):
            return

        # Утилита: вытащить stars из Review-объекта/строки/числа.
        def _extract_stars(rev: Any) -> int:
            if rev is None:
                return 0
            for star_attr in ("stars", "rating", "score"):
                star_val = getattr(rev, star_attr, None)
                if star_val is not None:
                    try:
                        return int(star_val)
                    except (ValueError, TypeError):
                        pass
            try:
                return int(rev)
            except (ValueError, TypeError):
                if isinstance(rev, str) and "5" in rev:
                    return 5
            return 0

        review = None
        for attr in ("review", "stars", "rating"):
            review = getattr(order, attr, None)
            if review is not None:
                break

        # Определяем stars (число)
        # FunPayAPI Review — dataclass с полем .stars; кроме того, некоторые
        # обёртки кладут int напрямую. Поддерживаем оба варианта плюс
        # ситуацию, когда review = None (отзыва ещё нет / был удалён).
        stars = _extract_stars(review)

        # ── Re-fetch при stars=0 ──────────────────────────────────────────
        # FunPayCardinal иногда дёргает OrderStatusChangedEvent ДО того как
        # парсер заполнил Review-объект (видели на «Изменён отзыв на заказ
        # #...»: event.order.review = None, хотя в чате уже виден 5★ отзыв).
        # Чтобы бонус не прошёл мимо, при stars=0 и наличии order_id
        # делаем свежий get_order(order_id) — берём результат оттуда.
        refetched = False
        if stars == 0 and order_id_v:
            try:
                acc_obj = getattr(cardinal, "account", None)
                if acc_obj is not None and hasattr(acc_obj, "get_order"):
                    fresh_order = acc_obj.get_order(str(order_id_v))
                    if fresh_order is not None:
                        fresh_review = None
                        for attr in ("review", "stars", "rating"):
                            fresh_review = getattr(fresh_order, attr, None)
                            if fresh_review is not None:
                                break
                        fresh_stars = _extract_stars(fresh_review)
                        if fresh_stars > 0:
                            LOGGER.info(
                                "steam_rental: review re-fetch order=%s "
                                "buyer=%s — stars %s→%s (event был стейл)",
                                order_id_v, buyer_id_v, stars, fresh_stars)
                            review = fresh_review
                            stars = fresh_stars
                            refetched = True
            except Exception:
                LOGGER.debug(
                    "steam_rental: re-fetch order=%s for review failed",
                    order_id_v, exc_info=True)

        LOGGER.info(
            "steam_rental: review handler order=%s buyer=%s stars=%s "
            "review_type=%s refetched=%s",
            order_id_v, buyer_id_v, stars,
            type(review).__name__ if review is not None else "None",
            refetched)

        # ── Optimistic-fallback ──────────────────────────────────────────
        # FunPay JSON-API нередко отдаёт rating=null даже когда отзыв
        # видим в чате (см. чат от 09.06.26: «звёзды никак не запарсить»).
        # Если stars=0, но review.text непустой — это положительный
        # сигнал: покупатель потратил время написать отзыв. Считаем его
        # квалифицирующим, чтобы бонус не уходил в трубу из-за бага
        # парсера.
        review_text = ""
        if review is not None:
            try:
                review_text = (getattr(review, "text", "") or "").strip()
            except Exception:
                review_text = ""

        min_stars = int(cfg.get("review_bonus_min_stars", 5) or 5)
        optimistic_unknown = bool(
            cfg.get("review_bonus_optimistic_unknown", True))
        optimistic = False
        if stars == 0 and review_text and optimistic_unknown:
            optimistic = True
            LOGGER.info(
                "steam_rental: review bonus — stars=0 (FunPay не вернул "
                "rating), но review.text непустой ('%s…') и "
                "review_bonus_optimistic_unknown=True → засчитываем как "
                "положительный отзыв",
                review_text[:40].replace("\n", " "))

        # ── Обнаружение удаления отзыва → штраф ──────────────────────────
        # Срабатывает только когда нет ни stars, ни текста (отзыв удалён).
        # v2.19: дополнительная защита — пенальти только если order_id из
        # события совпадает с rental.order_id. Иначе ложно срабатывает на
        # extension-покупках того же покупателя: extension-заказ только что
        # оплачен → отзыва нет → бот думает что удалили старый отзыв за
        # активную аренду.
        if stars == 0 and not optimistic \
                and cfg.get("review_delete_penalty_enabled", True):
            buyer_id_rd = getattr(order, "buyer_id", None)
            buyer_username_rd = getattr(order, "buyer_username", "")
            chat_id_rd = getattr(order, "chat_id", None)

            if buyer_id_rd:
                acc_rd = _find_buyer_active_rental(int(buyer_id_rd))
                if not acc_rd:
                    acc_rd = _find_buyer_last_rental(int(buyer_id_rd))

                # v2.19: order_id события должен совпадать с order_id
                # аренды. Иначе это просто другой заказ (extension /
                # параллельная покупка) без отзыва — не повод штрафовать.
                rental_order_id = ""
                if acc_rd and acc_rd.get("rental"):
                    rental_order_id = str(
                        acc_rd["rental"].get("order_id") or "")
                event_order_id = str(order_id_v or "")
                if (rental_order_id and event_order_id
                        and rental_order_id != event_order_id):
                    LOGGER.info(
                        "steam_rental: review_deleted skip — order=%s "
                        "не совпадает с rental.order_id=%s "
                        "(вероятно, extension/параллельный заказ "
                        "покупателя %s) — штраф не применяем",
                        event_order_id, rental_order_id,
                        buyer_username_rd)
                    return

                if acc_rd and acc_rd.get("rental") \
                        and acc_rd["rental"].get("review_bonus_applied"):
                    alias_rd = acc_rd["alias"]
                    penalty_hours = int(cfg.get("review_delete_penalty_hours", 1))
                    penalty_min = penalty_hours * 60

                    # Вычитаем время (отрицательное продление)
                    new_exp = 0
                    with _lock:
                        _acc_rd2 = find_account(alias_rd)
                        if _acc_rd2 and _acc_rd2.get("rental"):
                            old_exp = _acc_rd2["rental"]["expires_at"]
                            new_exp = max(_now(), old_exp - penalty_min * 60)
                            _acc_rd2["rental"]["expires_at"] = new_exp
                            _acc_rd2["rental"]["review_bonus_applied"] = False
                            _acc_rd2["rental"]["review_deleted_ts"] = _now()
                            upsert_account(_acc_rd2)

                    _log_rental_event("review_deleted_penalty", alias_rd,
                                      buyer_username=buyer_username_rd,
                                      buyer_id=buyer_id_rd,
                                      hours=penalty_hours)

                    # Отправляем предупреждение покупателю
                    _penalty_display = str(penalty_hours)
                    text_rd = _render_template(
                        "review_deleted",
                        buyer_id=buyer_id_rd,
                        hours=_penalty_display,
                        new_expires=_fmt_ts(new_exp) if new_exp else "—",
                    )
                    if chat_id_rd:
                        try:
                            cardinal.send_message(
                                chat_id_rd, text_rd,
                                chat_name=buyer_username_rd,
                                interlocutor_id=buyer_id_rd,
                                watermark=False)
                        except Exception:
                            LOGGER.debug("steam_rental: review_deleted msg failed",
                                         exc_info=True)

                    # Добавляем в чёрный список если включено
                    if cfg.get("review_delete_blacklist", True):
                        add_to_blacklist(
                            buyer_id_rd, buyer_username_rd,
                            reason=f"review_deleted order={order_id_v}")
                        _notify_tg(
                            cardinal,
                            f"🚫 <b>Steam Rental</b>: покупатель "
                            f"<b>{buyer_username_rd}</b> "
                            f"(id <code>{buyer_id_rd}</code>) удалил отзыв — "
                            f"штраф {_penalty_display}ч + добавлен в blacklist.")
                    else:
                        _notify_tg(
                            cardinal,
                            f"⚠ <b>Steam Rental</b>: покупатель "
                            f"<b>{buyer_username_rd}</b> "
                            f"(id <code>{buyer_id_rd}</code>) удалил отзыв — "
                            f"штраф {_penalty_display}ч к аренде "
                            f"<code>{alias_rd}</code>.")

                    LOGGER.info(
                        "steam_rental: штраф за удаление отзыва — %s "
                        "сокращён на %sч для %s",
                        alias_rd, _penalty_display, buyer_username_rd)
            return

        # ── Бонусная ветка ───────────────────────────────────────────────
        # Квалифицирует:
        #   stars >= min_stars (нормальный путь)  ИЛИ
        #   optimistic=True (stars=0, но review.text непустой)
        if not optimistic and stars < min_stars:
            if stars > 0:
                LOGGER.info(
                    "steam_rental: review bonus skipped — stars=%s (мин %s) "
                    "для order=%s buyer=%s",
                    stars, min_stars, order_id_v, buyer_id_v)
            else:
                LOGGER.info(
                    "steam_rental: review bonus skipped — stars=0 и "
                    "review.text пустой (или review=None) для order=%s "
                    "buyer=%s. Если у тебя FunPayAPI всегда отдаёт "
                    "rating=null — включи review_bonus_optimistic_unknown.",
                    order_id_v, buyer_id_v)
            return

        buyer_id = getattr(order, "buyer_id", None)
        buyer_username = getattr(order, "buyer_username", "")
        chat_id = getattr(order, "chat_id", None)

        if not buyer_id:
            LOGGER.info(
                "steam_rental: review bonus skipped — нет buyer_id "
                "у order=%s", order_id_v)
            return

        acc = _find_buyer_active_rental(int(buyer_id))
        if not acc:
            acc = _find_buyer_last_rental(int(buyer_id))

        if not acc or not acc.get("rental"):
            LOGGER.info(
                "steam_rental: review bonus skipped — нет активной/"
                "недавней аренды для buyer=%s (order=%s). Возможно "
                "rental уже был завершён и rental-словарь очищен.",
                buyer_id, order_id_v)
            return

        rental_data = acc.get("rental") or {}
        # Идемпотентность: если бонус уже начислялся за эту аренду —
        # повторный 5★ event'а (правка отзыва, ответ продавца, refresh
        # парсера и т.д.) не должен давать бонус ещё раз.
        if rental_data.get("review_bonus_applied"):
            LOGGER.info(
                "steam_rental: review bonus skipped — уже начислен ранее "
                "для %s (buyer=%s order=%s).",
                acc.get("alias"), buyer_id, order_id_v)
            return

        bonus_hours = int(cfg.get("review_bonus_hours", 1))
        _stored_review_bonus = rental_data.get("review_bonus_minutes")
        if _stored_review_bonus is not None:
            bonus_min = int(_stored_review_bonus)
        else:
            bonus_min = bonus_hours * 60
        if bonus_min <= 0:
            return
        alias = acc["alias"]
        new_expires = _extend_rental(alias, bonus_min, reason="review_bonus")

        # Помечаем, что бонус за отзыв был начислен (для отслеживания удаления)
        with _lock:
            _acc2 = find_account(alias)
            if _acc2 and _acc2.get("rental"):
                _acc2["rental"]["review_bonus_applied"] = True
                _acc2["rental"]["review_bonus_applied_ts"] = _now()
                upsert_account(_acc2)

        # Инкрементируем счётчик отзывов аккаунта (для статистики)
        try:
            _bump_acc_stat(alias, inc_reviews_count=1,
                           set_last_review_ts=_now())
        except Exception:
            LOGGER.debug("steam_rental: bump reviews_count failed",
                         exc_info=True)

        _log_rental_event("review_bonus", alias,
                          buyer_username=buyer_username, buyer_id=buyer_id,
                          hours=bonus_min / 60)

        if new_expires <= 0:
            return

        game = _get_game_for_alias(alias)
        _bonus_display = f"{bonus_min / 60:.0f}" if bonus_min % 60 == 0 else f"{bonus_min / 60:.1f}"
        text = _render_template(
            "review_reward",
            buyer_id=buyer_id,
            hours=_bonus_display,
            new_expires=_fmt_ts(new_expires),
            login=acc["account_name"],
            game=game or "\u2014",
        )

        if chat_id:
            try:
                cardinal.send_message(
                    chat_id, text,
                    chat_name=buyer_username,
                    interlocutor_id=buyer_id,
                    watermark=False)
            except Exception:
                LOGGER.debug("steam_rental: review_reward msg failed", exc_info=True)

        LOGGER.info("steam_rental: бонус за отзыв 5★ — %s продлён на %sч для %s",
                    alias, _bonus_display, buyer_username)
        _notify_tg(cardinal,
                   f"⭐ <b>Steam Rental</b>: бонус за 5★ отзыв — "
                   f"аренда <code>{alias}</code> продлена на {_bonus_display}ч "
                   f"(покупатель {buyer_username}).")
    except Exception:
        LOGGER.error("steam_rental: handler_order_status_changed crashed",
                     exc_info=True)


def _cmd_accounts_list(cardinal: "Cardinal", msg: Any) -> None:
    """Показывает покупателю список лотов со свободными аккаунтами.

    v2.23.0:
      * Дедупликация лотов по `game_key` (или по `game.lower()` для
        legacy-лотов без game_key). Если у одной игры несколько лотов
        с общим пулом — выводим её одной строкой, а не N одинаковых.
      * В строке игры показываем реальные Steam-логины свободных
        аккаунтов (`acc.account_name`). Лимит — 10 логинов; больше —
        «… и ещё N».
      * Дополнительный блок «🔴 Сейчас в аренде» с занятыми аккаунтами
        и оставшимся временем — покупатель видит, когда что-то
        освободится.
    """
    try:
        chat_id = msg.chat_id
        chat_name = getattr(msg, "chat_name", None)
        author_id = getattr(msg, "author_id", None)
        lang = _get_buyer_lang(author_id) if author_id else "ru"

        lots = _load_json(LOTS_FILE, {})
        # ── Группируем лоты по игре ──────────────────────────────────
        groups: dict[str, dict[str, Any]] = {}
        for key, val in lots.items():
            gkey = (val.get("game_key") or "").strip().lower()
            game_name = (val.get("game") or "").strip()
            group_key = gkey or game_name.lower() or str(key).lower()
            grp = groups.setdefault(group_key, {
                "game_name": game_name or str(key),
                "lots": [],
            })
            if not grp["game_name"] and game_name:
                grp["game_name"] = game_name
            grp["lots"].append(val)

        # ── Свободные строки ─────────────────────────────────────────
        lines: list[str] = []
        # Чтобы один и тот же арендованный аккаунт не появлялся в нескольких
        # группах (если он привязан к нескольким играм через legacy-лоты) —
        # дедуплицируем сами.
        busy_seen: set[str] = set()
        busy_lines: list[str] = []
        now_ts = _now()

        for group_key, grp in groups.items():
            seen_low: set[str] = set()
            combined: list[str] = []
            for lot in grp["lots"]:
                for a in _combined_lot_pool(lot):
                    al = str(a).lower()
                    if al not in seen_low:
                        seen_low.add(al)
                        combined.append(str(a))
            if not combined:
                continue
            free_aliases: list[str] = []
            busy_in_group: list[tuple[str, str, int]] = []  # (alias, login, remaining_min)
            for a in combined:
                acc = find_account(a)
                if not acc:
                    continue
                if acc.get("frozen") or acc.get("status") == "frozen":
                    continue
                rental = acc.get("rental")
                if rental:
                    al_low = str(a).lower()
                    if al_low in busy_seen:
                        continue
                    busy_seen.add(al_low)
                    login = (acc.get("account_name") or a)
                    exp = int(rental.get("expires_at", 0) or 0)
                    rem_sec = max(0, exp - now_ts)
                    rem_min = rem_sec // 60 + (1 if rem_sec % 60 else 0)
                    busy_in_group.append((a, login, rem_min))
                else:
                    free_aliases.append(a)

            # ── Свободные: одна строка на игру ──────────────────────
            if free_aliases:
                max_logins = 10
                shown = free_aliases[:max_logins]
                login_strs: list[str] = []
                for a in shown:
                    acc = find_account(a)
                    login = (acc.get("account_name") if acc else "") or a
                    login_strs.append(login)
                extra = len(free_aliases) - len(shown)
                logins_text = ", ".join(login_strs)
                if extra > 0:
                    more = (f" … и ещё {extra}" if lang == "ru"
                            else f" … and {extra} more")
                    logins_text += more
                lines.append(_render_template(
                    "accounts_list_lot_line",
                    buyer_id=author_id,
                    game=grp["game_name"] or "—",
                    free=str(len(free_aliases)),
                    logins=logins_text,
                ))

            # ── Занятые: одна строка на каждую аренду ───────────────
            for _alias, login, rem_min in busy_in_group:
                busy_lines.append(_render_template(
                    "accounts_list_busy_line",
                    buyer_id=author_id,
                    game=grp["game_name"] or "—",
                    login=login,
                    remaining=(_human_minutes_lang(rem_min, lang)
                               if rem_min > 0 else "—"),
                ))

        # ── Финальный рендер ─────────────────────────────────────────
        if not lines and not busy_lines:
            text = _render_template("accounts_list_empty",
                                    buyer_id=author_id)
        else:
            blocks: list[str] = []
            if lines:
                blocks.append("\n\n".join(lines))
            if busy_lines:
                header = _render_template("accounts_list_busy_header",
                                          buyer_id=author_id)
                blocks.append(header + "\n" + "\n".join(busy_lines))
            text = _render_template("accounts_list",
                                    buyer_id=author_id,
                                    lots="\n\n".join(blocks))

        cardinal.send_message(
            chat_id, text,
            chat_name=chat_name,
            interlocutor_id=author_id, watermark=False)
    except Exception:
        LOGGER.error("steam_rental: _cmd_accounts_list crashed", exc_info=True)


# ── Команда брони конкретного аккаунта (Irent <login>) ─────────────────────
def _parse_reserve_command(text: str, cfg: dict[str, Any]) -> str | None:
    """Возвращает аргумент (логин) если text — это команда бронирования.
    Иначе None. Тонкая обёртка над _parse_kw_command, чтобы держать
    backward-совместимость по имени и читать csv именно из reservations_commands."""
    return _parse_kw_command(
        text,
        cfg.get("reservations_commands") or "",
        "irent,!арендую,!reserve,!забронировать")


def _cmd_reserve(cardinal: "Cardinal", msg: Any, login_arg: str) -> None:
    """`irent <login>` — резервирует конкретный Steam-аккаунт за покупателем
    и шлёт ссылку на оплату лота. Случайный аккаунт после оплаты НЕ выдаём."""
    cfg = get_config()
    chat_id = msg.chat_id
    chat_name = getattr(msg, "chat_name", None)
    author_id = getattr(msg, "author_id", None)

    def _send(text: str) -> None:
        try:
            cardinal.send_message(
                chat_id, _strip_html(text),
                chat_name=chat_name,
                interlocutor_id=author_id, watermark=False)
        except Exception:
            LOGGER.debug("steam_rental: reserve send failed", exc_info=True)

    def _t(name: str, **kw: Any) -> str:
        # Локальный хелпер: всегда передаём buyer_id=author_id
        # чтобы шаблоны рендерились на языке покупателя.
        return _render_template(name, buyer_id=author_id, **kw)

    if not cfg.get("reservations_enabled", True):
        return

    login = (login_arg or "").strip().strip(":,;.").split()
    login = login[0] if login else ""
    if not login:
        # Помощь — берём первый попавшийся свободный логин как пример.
        example = "<логин>"
        try:
            for a in list_accounts():
                if _is_alias_rentable(a):
                    example = a.get("account_name") or example
                    break
        except Exception:
            pass
        _send(_t("reserve_help", example_login=example))
        return

    if author_id is None:
        return

    # 1) Ищем аккаунт по логину.
    acc = find_account_by_login(login)
    if not acc:
        _send(_t("reserve_unknown", login=login))
        return

    alias = acc.get("alias") or ""
    if not alias:
        _send(_t("reserve_unknown", login=login))
        return

    # 2) Ищем лот, в пуле которого этот alias.
    lot_pair = _find_lot_by_alias(alias)
    if not lot_pair:
        _send(_t("reserve_no_lot", login=login))
        return
    lot_key, lot = lot_pair

    # Только обычная аренда (non-remoteplay) поддерживается этой командой.
    if lot.get("type") == "remoteplay":
        _send(_t("reserve_no_lot", login=login))
        return

    # 3) Aлиас не должен быть занят/заморожен.
    if not _is_alias_rentable(acc):
        _send(_t("reserve_busy", login=login))
        return

    # Лот должен быть числовым ключом FunPay (offer-id).
    if not str(lot_key).isdigit():
        _send(_t("reserve_no_lot", login=login))
        return

    # 4) Атомарно создаём/обновляем бронь. Все TOCTOU-проверки внутри.
    ttl = int(cfg.get("reservations_ttl_minutes", 20) or 20)
    status, entry = _try_create_reservation_atomic(
        alias=alias,
        buyer_id=int(author_id),
        buyer_username=str(chat_name or ""),
        chat_id=chat_id,
        lot_key=str(lot_key),
        ttl_minutes=ttl)

    if status == "taken_by_other":
        _send(_t("reserve_taken_by_other", login=login))
        return

    link = f"https://funpay.com/lots/offer?id={lot_key}"

    if status == "already_self":
        _send(_t(
            "reserve_already_held",
            login=login,
            link=link,
            expires=_fmt_ts(int((entry or {}).get("expires_ts") or 0))))
        return

    # 5) Бронь создана — теперь можно активировать лот на FunPay (best-effort).
    try:
        _set_funpay_lot_active(cardinal, str(lot_key), True)
    except Exception:
        LOGGER.debug("steam_rental: reserve lot activate failed",
                     exc_info=True)

    game = lot.get("game") or _get_game_for_alias(alias) or "—"
    _send(_t(
        "reserve_ok",
        login=login,
        game=game,
        link=link,
        minutes=str(ttl)))

    _log_action(
        "reservation_created",
        f"Бронь {alias} → buyer {chat_name or author_id} (TTL {ttl}m)",
        alias=alias,
        buyer=chat_name,
        buyer_id=author_id,
        lot_id=lot_key,
        expires=_fmt_ts(int((entry or {}).get("expires_ts") or 0)))

    _notify_tg(
        cardinal,
        f"📌 <b>Steam Rental</b>: бронь акка <code>{_esc(alias)}</code> "
        f"(<code>{_esc(login)}</code>) для <b>{_esc(str(chat_name))}</b> "
        f"на {ttl} мин. Лот <code>{lot_key}</code>.")


# ── Команда одноразового приоритета (!priority/!приоритет <login>) ─────────
def _parse_kw_command(text: str, csv_cfg: str,
                      default_csv: str) -> str | None:
    """Универсальный парсер команд через csv-список префиксов.
    Возвращает аргумент (или '' если без аргумента) если команда
    распознана, иначе None."""
    raw = (text or "").strip()
    if not raw:
        return None
    cmds_raw = csv_cfg or default_csv
    cmds = [c.strip().lower() for c in cmds_raw.split(",") if c.strip()]
    low = raw.lower()
    for cmd in cmds:
        if low == cmd:
            return ""
        if low.startswith(cmd + " "):
            return raw[len(cmd):].strip()
    return None


def _cmd_priority(cardinal: "Cardinal", msg: Any, login_arg: str) -> None:
    """!priority <login> — одноразовый приоритет на конкретный аккаунт.
    При следующей оплате выдадим именно этот acc, если свободен."""
    cfg = get_config()
    chat_id = msg.chat_id
    chat_name = getattr(msg, "chat_name", None)
    author_id = getattr(msg, "author_id", None)

    def _send(text: str) -> None:
        try:
            cardinal.send_message(
                chat_id, _strip_html(text),
                chat_name=chat_name,
                interlocutor_id=author_id, watermark=False)
        except Exception:
            LOGGER.debug("steam_rental: priority send failed", exc_info=True)

    def _t(name: str, **kw: Any) -> str:
        return _render_template(name, buyer_id=author_id, **kw)

    if not cfg.get("priority_enabled", True):
        return

    login = (login_arg or "").strip().strip(":,;.").split()
    login = login[0] if login else ""
    if not login:
        example = "<логин>"
        try:
            for a in list_accounts():
                if _is_alias_rentable(a):
                    example = a.get("account_name") or example
                    break
        except Exception:
            pass
        _send(_t("priority_help", example_login=example))
        return
    if author_id is None:
        return

    acc = find_account_by_login(login)
    if not acc:
        _send(_t("priority_unknown", login=login))
        return
    alias = acc.get("alias") or ""
    if not alias:
        _send(_t("priority_unknown", login=login))
        return

    lot_pair = _find_lot_by_alias(alias)
    if not lot_pair:
        _send(_t("priority_no_lot", login=login))
        return
    lot_key, lot = lot_pair
    if lot.get("type") == "remoteplay":
        _send(_t("priority_no_lot", login=login))
        return
    if not str(lot_key).isdigit():
        _send(_t("priority_no_lot", login=login))
        return

    ttl_h = int(cfg.get("priority_ttl_hours", 24) or 24)
    prev = _set_priority(int(author_id), alias, login, str(lot_key), ttl_h)

    game = lot.get("game") or _get_game_for_alias(alias) or "—"
    if prev and prev.get("login") and prev.get("login") != login:
        _send(_t("priority_replaced",
                               old_login=prev.get("login"), login=login))
    _send(_t("priority_set", login=login, game=game))

    _log_action(
        "priority_set",
        f"Приоритет {alias} → buyer {chat_name or author_id}",
        alias=alias,
        login=login,
        buyer=chat_name,
        buyer_id=author_id,
        lot_id=lot_key,
        ttl_hours=ttl_h)


# ── Команда waitlist (!notify/!жду <login>) ────────────────────────────────
def _cmd_waitlist(cardinal: "Cardinal", msg: Any, login_arg: str) -> None:
    """!notify <login> / !жду <login> — встать в очередь ожидания
    конкретного аккаунта. При освобождении уведомим первых N человек."""
    cfg = get_config()
    chat_id = msg.chat_id
    chat_name = getattr(msg, "chat_name", None)
    author_id = getattr(msg, "author_id", None)
    notify_top = int(cfg.get("waitlist_notify_top", 3) or 3)

    def _send(text: str) -> None:
        try:
            cardinal.send_message(
                chat_id, _strip_html(text),
                chat_name=chat_name,
                interlocutor_id=author_id, watermark=False)
        except Exception:
            LOGGER.debug("steam_rental: waitlist send failed", exc_info=True)

    def _t(name: str, **kw: Any) -> str:
        return _render_template(name, buyer_id=author_id, **kw)

    if not cfg.get("waitlist_enabled", True):
        return

    login = (login_arg or "").strip().strip(":,;.").split()
    login = login[0] if login else ""
    if not login:
        example = "<логин>"
        try:
            for a in list_accounts():
                if a.get("account_name"):
                    example = a.get("account_name")
                    break
        except Exception:
            pass
        _send(_t("waitlist_help",
                               example_login=example,
                               notify_top=str(notify_top)))
        return
    if author_id is None:
        return

    acc = find_account_by_login(login)
    if not acc:
        _send(_t("waitlist_unknown", login=login))
        return
    alias = acc.get("alias") or ""
    if not alias:
        _send(_t("waitlist_unknown", login=login))
        return

    lot_pair = _find_lot_by_alias(alias)
    if not lot_pair:
        _send(_t("waitlist_no_lot", login=login))
        return
    lot_key = lot_pair[0]

    res = _waitlist_add(
        alias=alias,
        buyer_id=int(author_id),
        buyer_username=str(chat_name or ""),
        chat_id=chat_id,
        lot_key=str(lot_key))

    if res.get("ok"):
        _send(_t("waitlist_added",
                               login=login,
                               position=str(res.get("position", "?")),
                               notify_top=str(notify_top)))
        _log_action(
            "waitlist_added",
            f"Waitlist {alias}: +{chat_name or author_id} #{res.get('position')}",
            alias=alias, login=login, buyer=chat_name, buyer_id=author_id,
            position=res.get("position"))
    elif res.get("reason") == "already":
        _send(_t("waitlist_already",
                               login=login,
                               position=str(res.get("position", "?"))))
    elif res.get("reason") == "full":
        _send(_t("waitlist_full", login=login))


def _handler_new_message(cardinal: "Cardinal", event: "NewMessageEvent") -> None:
    """Обрабатывает команды покупателя: !код, !продлить, !статус, !помощь.
    Дополнительно — приём фото для PC-club верификации."""
    try:
        msg = event.message
        text = (getattr(msg, "text", "") or "").strip()
        image_link = getattr(msg, "image_link", None) or None

        if getattr(msg, "author_id", None) == cardinal.account.id:
            return

        # ── Backup-триггер для бонуса за отзыв ──────────────────────────────
        # FPC помечает системные сообщения по типам (handlers.py
        # process_review_handler ловит NEW_FEEDBACK / FEEDBACK_CHANGED).
        # Это надёжнее регекса и работает на всех локалях.
        # Если OrderStatusChangedEvent не дошёл / event.order был стейл
        # / Review.stars пришёл null — этот путь ловит ситуацию.
        # Идемпотентность через rental.review_bonus_applied.
        try:
            if _is_review_system_message(msg):
                _trigger_review_bonus_from_msg(cardinal, msg)
                # НЕ return — служебное сообщение, не должно ломать
                # остальную логику чата.
        except Exception:
            LOGGER.debug(
                "steam_rental: review-bonus trigger from chat failed",
                exc_info=True)

        # ── v6: Manual photo-review — самый ранний приоритет ──
        # Если у покупателя есть активная заявка manual_review → принимаем
        # фото или (если он шлёт текст) напоминаем, что ждём фото.
        author_id_mr = getattr(msg, "author_id", None)
        if author_id_mr is not None:
            try:
                mr_found = _mr_find_active_by_buyer(int(author_id_mr))
            except Exception:
                mr_found = None
            if mr_found is not None:
                mr_oid, mr_req = mr_found
                if image_link and mr_req.get("status") == "awaiting_photo":
                    _mr_on_photo(cardinal, msg, mr_oid, image_link)
                    return
                if mr_req.get("status") == "pending_review":
                    # Фото уже на проверке — не реагируем на доп.
                    # сообщения, чтобы не флудить, но не выходим из
                    # обработчика, пусть нижняя логика игнорирует.
                    pass

        # ── PC-club: входящие сообщения от покупателя ──
        author_id = getattr(msg, "author_id", None)
        if author_id is not None:
            try:
                found = _club_find_request_by_buyer(int(author_id))
            except Exception:
                found = None

            cfg2 = get_config()
            pcclub_cmd = (cfg2.get("pcclub_command")
                          or "/pcclub").strip().lower()
            text_lower_early = (text or "").strip().lower()
            is_pcclub_cmd = (
                pcclub_cmd
                and (text_lower_early == pcclub_cmd
                     or text_lower_early.startswith(pcclub_cmd + " ")))

            # 1) Команда /pcclub — переводим в awaiting_photo и шлём код
            if is_pcclub_cmd:
                if found is not None:
                    order_id, req = found
                    if req.get("status") in ("awaiting_command",
                                              "awaiting_photo"):
                        _club_update_request(order_id,
                                              status="awaiting_photo")
                        _send_club_photo_prompt(cardinal, msg, req)
                    elif req.get("status") in ("verifying", "manual"):
                        try:
                            cardinal.send_message(
                                msg.chat_id,
                                "Фото уже на проверке, жди решение.",
                                chat_name=msg.chat_name,
                                interlocutor_id=msg.author_id,
                                watermark=False)
                        except Exception:
                            pass
                else:
                    try:
                        cardinal.send_message(
                            msg.chat_id,
                            "У вас нет активного PC-club заказа.",
                            chat_name=msg.chat_name,
                            interlocutor_id=msg.author_id,
                            watermark=False)
                    except Exception:
                        pass
                return

            # 2) Фото — берём ТОЛЬКО если была команда раньше
            if found is not None and image_link:
                order_id, req = found
                if req.get("status") == "awaiting_photo":
                    _on_club_photo_received(cardinal, msg, order_id, image_link)
                    return

        if not text:
            return

        text_lower = text.lower()

        # ── Бронирование конкретного аккаунта (Irent <login>) ──
        # Отрабатывает раньше прочих, т.к. команды могут начинаться без '!'.
        try:
            cfg_r = get_config()
            if cfg_r.get("reservations_enabled", True):
                arg = _parse_reserve_command(text, cfg_r)
                if arg is not None:
                    _cmd_reserve(cardinal, msg, arg)
                    return
            # ── Одноразовый приоритет (!priority <login>) ──
            if cfg_r.get("priority_enabled", True):
                arg = _parse_kw_command(
                    text,
                    cfg_r.get("priority_commands", "") or "",
                    "!priority,!приоритет")
                if arg is not None:
                    _cmd_priority(cardinal, msg, arg)
                    return
            # ── Waitlist (!notify <login> / !жду <login>) ──
            if cfg_r.get("waitlist_enabled", True):
                arg = _parse_kw_command(
                    text,
                    cfg_r.get("waitlist_commands", "") or "",
                    "!notify,!жду")
                if arg is not None:
                    _cmd_waitlist(cardinal, msg, arg)
                    return
        except Exception:
            LOGGER.debug("steam_rental: reserve dispatch failed",
                         exc_info=True)

        if text_lower.startswith("!код") or text_lower.startswith("!guardik") or text_lower.startswith("!code"):
            _cmd_guard_code(cardinal, msg, text)
        elif text_lower.startswith("!продлить") or text_lower.startswith("!extend"):
            _cmd_extend(cardinal, msg)
        elif text_lower.startswith("!статус") or text_lower.startswith("!status"):
            _cmd_status(cardinal, msg)
        elif text_lower.startswith("!помощь") or text_lower.startswith("!help"):
            _cmd_help(cardinal, msg)
        # ── Remote Play buyer commands ──
        elif text_lower.startswith("!пин") or text_lower.startswith("!pin"):
            _rp_cmd_pin(cardinal, msg)
        elif text_lower.startswith("!статусrp") or text_lower.startswith("!statusrp"):
            _rp_cmd_status(cardinal, msg)
        elif text_lower.startswith("!помощьrp") or text_lower.startswith("!helprp"):
            _rp_cmd_help(cardinal, msg)
        elif text_lower.startswith("!аккаунты") or text_lower.startswith("!accounts"):
            _cmd_accounts_list(cardinal, msg)
        elif text_lower.startswith("!очередь") or text_lower.startswith("!queue"):
            _cmd_queue(cardinal, msg)
        # v2.22: переключение языка чата покупателя
        elif text_lower.startswith("!engrent") or text_lower.startswith("!english"):
            _cmd_set_lang(cardinal, msg, "en")
        elif text_lower.startswith("!rusrent") or text_lower.startswith("!russian"):
            _cmd_set_lang(cardinal, msg, "ru")

    except Exception:
        LOGGER.error("steam_rental: handler_new_message crashed", exc_info=True)


# ── PC-club: проверка фото и выдача ────────────────────────────────────────
def _start_club_verification(cardinal: "Cardinal", order: Any,
                              lot: dict[str, Any], duration_min: int) -> None:
    """Создаёт заявку и отправляет инструкцию покупателю."""
    cfg = get_config()
    req = _club_create_request(
        order_id=str(order.id),
        buyer_id=int(order.buyer_id),
        buyer_username=str(order.buyer_username or ""),
        chat_id=order.chat_id,
        lot_key=str(lot.get("key") or ""),
        duration_min=int(duration_min),
    )
    pcclub_cmd = (cfg.get("pcclub_command") or "/pcclub").strip() or "/pcclub"
    text = (
        f"👋 Привет, {order.buyer_username}!\n\n"
        f"Это <b>PC-клубный тариф</b> — он требует одноразовой проверки.\n\n"
        f"🔑 Когда будешь в PC-клубе — отправь в этот чат команду:\n"
        f"<b><code>{_esc(pcclub_cmd)}</code></b>\n\n"
        f"Я пришлю тебе код верификации и подробную инструкцию "
        f"что должно быть на фото. До этого фото присылать НЕ нужно.\n\n"
        f"После проверки бот автоматически выдаст аккаунт.\n"
        f"Если не сможем подтвердить — деньги вернутся."
    )
    try:
        cardinal.send_message(
            order.chat_id, _strip_html(text),
            chat_name=order.buyer_username,
            interlocutor_id=order.buyer_id, watermark=False)
    except Exception:
        LOGGER.warning("steam_rental: club start msg failed", exc_info=True)
    _notify_tg(cardinal,
               f"🏠 <b>PC-club</b>: новый заказ #{order.id}, "
               f"покупатель <b>{_esc(str(order.buyer_username))}</b>. "
               f"Код: <code>{req['code']}</code>. "
               f"Ждём команду <code>{_esc(pcclub_cmd)}</code>.")


def _send_club_photo_prompt(cardinal: "Cardinal", msg: Any,
                              req: dict[str, Any]) -> None:
    """Отправляет покупателю инструкцию по фото после команды /pcclub."""
    cfg = get_config()
    seller_nick = (cfg.get("seller_funpay_nickname") or "").strip() \
        or "продавцом"
    code = req.get("code") or ""
    text = (
        f"📷 Пришли в этот чат одно фото, на котором видно:\n"
        f"1) интерьер PC-клуба (компы / вывеска / зала);\n"
        f"2) открытый чат FunPay со мной "
        f"(никнейм продавца: <b>{_esc(seller_nick)}</b>);\n"
        f"3) код верификации: <b><code>{code}</code></b> "
        f"(на бумажке, на экране или последним сообщением в чате).\n\n"
        f"Проверка занимает 10–60 секунд."
    )
    try:
        cardinal.send_message(
            msg.chat_id, _strip_html(text),
            chat_name=msg.chat_name,
            interlocutor_id=msg.author_id, watermark=False)
    except Exception:
        LOGGER.warning("steam_rental: club photo prompt failed",
                       exc_info=True)


def _strip_html(s: str) -> str:
    """Убирает HTML-теги (для FunPay-чата, который html не поддерживает)."""
    return re.sub(r"</?[a-zA-Z][^>]*>", "", s)


def _on_club_photo_received(cardinal: "Cardinal", msg: Any,
                             order_id: str, image_link: str) -> None:
    """Покупатель прислал фото — запускаем верификацию."""
    req = _club_update_request(order_id,
                                photo_url=image_link,
                                status="verifying")
    if not req:
        return
    cfg = get_config()
    # Ack — даём покупателю обратную связь, чтоб не думал «ничего не происходит».
    try:
        cardinal.send_message(
            msg.chat_id,
            "Принял фото, запускаю проверку. Это занимает 10-60 секунд.",
            chat_name=msg.chat_name,
            interlocutor_id=msg.author_id, watermark=False)
    except Exception:
        pass

    seller_nick = (cfg.get("seller_funpay_nickname") or "").strip() \
        or "продавец"

    def _bg():
        try:
            verdict = _ai_verify_club_photo(image_link, req["code"], seller_nick)
            _club_update_request(order_id, ai_verdict=verdict)
            _process_club_verdict(cardinal, order_id, verdict)
        except Exception as exc:
            LOGGER.error("steam_rental: club AI verify crash %s", order_id,
                         exc_info=True)
            _club_update_request(order_id,
                                  ai_verdict={"error": str(exc), "ok": False},
                                  status="manual")
            _notify_tg(cardinal,
                       f"🏠 <b>PC-club</b>: ошибка AI для заказа "
                       f"#{order_id}: <code>{_esc(str(exc))[:200]}</code>\n"
                       f"Зайди в <code>/srental → 🏠 PC-клубы</code>, "
                       f"разрули вручную.")

    threading.Thread(target=_bg, daemon=True,
                      name=f"sr-club-{order_id}").start()


def _process_club_verdict(cardinal: "Cardinal", order_id: str,
                           verdict: dict[str, Any]) -> None:
    """Применяет AI-вердикт: auto-approve / decline / в ручную очередь."""
    cfg = get_config()
    appr = int(cfg.get("club_auto_approve_threshold", 80))
    decl = int(cfg.get("club_auto_decline_threshold", 30))
    req = _club_get_request(order_id)
    if not req:
        return

    if not verdict.get("ok"):
        _club_update_request(order_id, status="manual")
        _notify_club_to_admin(cardinal, order_id, verdict, mode="ai_error")
        return

    confidence = int(verdict.get("confidence", 0))
    all_true = all(verdict.get(k) for k in (
        "is_pc_club", "funpay_chat_visible",
        "seller_nickname_visible", "code_visible"))

    # ── Fake-детектор: «decline» ставит низкий confidence в самом верификаторе,
    # но дополнительно даём явную причину отказа покупателю. «manual» —
    # никогда не апруваем автоматически, что бы основной верификатор ни
    # вернул, отправляем заявку на ручное решение админа.
    fake = verdict.get("fake") or {}
    fake_decision = verdict.get("fake_decision") or "pass"
    if fake_decision == "decline":
        reason = (
            f"AI-фото-детектор: вероятность подделки "
            f"{int(fake.get('ai_generated_score', 0))}%. "
            + (fake.get("reasoning") or "")[:300]
        )
        _decline_club_request(cardinal, order_id, by="ai", reason=reason)
        return
    if fake_decision == "manual":
        _club_update_request(order_id, status="manual")
        _notify_club_to_admin(cardinal, order_id, verdict, mode="manual")
        return

    if all_true and confidence >= appr:
        _approve_club_request(cardinal, order_id, by="ai")
        return
    if confidence <= decl:
        _decline_club_request(cardinal, order_id, by="ai",
                               reason=verdict.get("reasoning", ""))
        return

    # Серая зона — на ручное решение админа.
    _club_update_request(order_id, status="manual")
    _notify_club_to_admin(cardinal, order_id, verdict, mode="manual")


def _approve_club_request(cardinal: "Cardinal", order_id: str, *,
                           by: str = "manual",
                           admin_uid: int | None = None) -> bool:
    req = _club_get_request(order_id)
    if not req:
        return False
    if req.get("status") not in ("verifying", "manual", "awaiting_photo"):
        return False
    lots = list_lots()
    lot = lots.get(req.get("lot_key") or "")
    if not lot:
        _notify_tg(cardinal,
                   f"🏠 <b>PC-club</b>: лот <code>"
                   f"{_esc(req.get('lot_key') or '?')}</code> не найден, "
                   f"не могу выдать акк для заказа #{order_id}.")
        return False
    alias = _pick_free_alias(lot.get("aliases", []))
    if not alias:
        _notify_tg(cardinal,
                   f"🏠 <b>PC-club</b>: для заказа #{order_id} нет свободных "
                   f"аккаунтов в лоте <code>{lot.get('key')}</code>. "
                   f"Дозаливай и нажми «Approve» ещё раз.")
        return False
    ok = deliver_account(cardinal,
                          alias=alias,
                          duration_min=int(req.get("duration_min") or 0),
                          order_id=str(order_id),
                          buyer_username=str(req.get("buyer_username") or ""),
                          buyer_id=int(req.get("buyer_id") or 0),
                          chat_id=req.get("chat_id"))
    if not ok:
        return False

    _club_update_request(order_id, status="approved",
                          decided_ts=_now(),
                          decided_by=by + (f":{admin_uid}" if admin_uid else ""),
                          alias_issued=alias)
    _club_add_to_whitelist(req.get("buyer_id"),
                           username=str(req.get("buyer_username") or ""),
                           order_id=str(order_id))
    _club_stat_inc(f"{by}_approves")
    _notify_tg(cardinal,
               f"🏠 <b>PC-club</b>: заявка #{order_id} одобрена "
               f"<b>{by.upper()}</b>. Выдан акк <code>{alias}</code>, "
               f"покупатель <b>{_esc(str(req.get('buyer_username')))}</b> "
               f"добавлен в whitelist.")
    return True


def _decline_club_request(cardinal: "Cardinal", order_id: str, *,
                           by: str = "manual",
                           admin_uid: int | None = None,
                           reason: str = "") -> bool:
    req = _club_get_request(order_id)
    if not req:
        return False
    if req.get("status") not in ("verifying", "manual", "awaiting_photo"):
        return False
    _club_update_request(order_id, status="declined",
                          decided_ts=_now(),
                          decided_by=by + (f":{admin_uid}" if admin_uid else ""),
                          decline_reason=reason)
    try:
        cardinal.send_message(
            req.get("chat_id"),
            "К сожалению, мы не смогли подтвердить, что вы находитесь в "
            "PC-клубе. Если это ошибка — пришлите более чёткое фото "
            "(вывеска клуба + чат FunPay со мной + код верификации) или "
            "напишите нам в чат, разрулим вручную и вернём деньги.",
            chat_name=req.get("buyer_username"),
            interlocutor_id=req.get("buyer_id"), watermark=False)
    except Exception:
        pass
    _club_stat_inc(f"{by}_declines")
    _notify_tg(cardinal,
               f"🏠 <b>PC-club</b>: заявка #{order_id} отклонена "
               f"<b>{by.upper()}</b>. Причина: <i>{_esc(reason)[:200]}</i>\n"
               f"Проверь нужно ли вернуть деньги покупателю "
               f"<b>{_esc(str(req.get('buyer_username')))}</b>.")
    return True


def _notify_club_to_admin(cardinal: "Cardinal", order_id: str,
                           verdict: dict[str, Any],
                           mode: str = "manual") -> None:
    """Шлёт в TG карточку с фото + вердикт + кнопки."""
    req = _club_get_request(order_id)
    if not req:
        return
    tg = getattr(cardinal, "telegram", None)
    if tg is None:
        return
    cfg = get_config()
    if not cfg.get("tg_notify", True):
        return

    header = ("🏠 <b>PC-club: серая зона, нужно решение</b>"
              if mode == "manual"
              else "🏠 <b>PC-club: ошибка AI, ручная проверка</b>")
    lines = [
        header, "",
        f"Заказ: <code>#{order_id}</code>",
        f"Покупатель: <b>{_esc(str(req.get('buyer_username')))}</b> "
        f"(id <code>{req.get('buyer_id')}</code>)",
        f"Код верификации: <code>{req.get('code')}</code>",
    ]
    if verdict.get("ok"):
        lines.append(
            f"AI ({_esc(verdict.get('provider', '?'))}/"
            f"{_esc(verdict.get('model', '?'))}): "
            f"confidence <b>{verdict.get('confidence')}%</b>, "
            f"club={verdict.get('is_pc_club')}, "
            f"chat={verdict.get('funpay_chat_visible')}, "
            f"nick={verdict.get('seller_nickname_visible')}, "
            f"code={verdict.get('code_visible')}")
        rea = verdict.get("reasoning") or ""
        if rea:
            lines.append(f"<i>{_esc(rea[:300])}</i>")
    else:
        lines.append(
            f"<i>AI error: {_esc(verdict.get('error', ''))[:300]}</i>")

    # ── Fake-детектор: отдельной строкой, чтобы было видно с первого взгляда.
    fake = verdict.get("fake") or {}
    if fake:
        if fake.get("ok"):
            score = int(fake.get("ai_generated_score", 0))
            decision = verdict.get("fake_decision", "pass")
            badge = {
                "decline": "🚨 AI-генерация",
                "manual": "⚠️ Подозрение на AI",
                "pass": "✅ Реальное фото",
            }.get(decision, "❔ ?")
            lines.append(
                f"\n🔬 Fake-детектор: <b>{badge}</b> "
                f"({score}%)")
            arts = fake.get("artifacts") or []
            if arts:
                lines.append(
                    "Артефакты: <code>"
                    f"{_esc(', '.join(arts)[:200])}</code>")
            rea_f = fake.get("reasoning") or ""
            if rea_f:
                lines.append(f"<i>{_esc(rea_f[:200])}</i>")
        else:
            err_f = fake.get("error") or ""
            if err_f:
                lines.append(
                    f"\n🔬 Fake-детектор: <i>ошибка — "
                    f"{_esc(err_f[:200])}</i>")

    kb = None
    try:
        from telebot import types as _tbt
        kb = _tbt.InlineKeyboardMarkup()
        kb.add(
            _tbt.InlineKeyboardButton(
                "✅ Одобрить",
                callback_data=f"sr:clbapr:{order_id}"),
            _tbt.InlineKeyboardButton(
                "❌ Отказать",
                callback_data=f"sr:clbdec:{order_id}"))
        kb.add(_tbt.InlineKeyboardButton(
            "🔁 Запросить ещё фото",
            callback_data=f"sr:clbret:{order_id}"))
        kb.add(_tbt.InlineKeyboardButton(
            "↪️ К списку",
            callback_data="sr:clbs"))
    except Exception:
        kb = None

    photo_url = req.get("photo_url")

    def _send():
        try:
            for admin_id in getattr(tg, "authorized_users", []) or []:
                try:
                    if photo_url:
                        tg.bot.send_photo(
                            admin_id, photo_url,
                            caption="\n".join(lines)[:1024],
                            parse_mode="HTML", reply_markup=kb)
                    else:
                        tg.bot.send_message(
                            admin_id, "\n".join(lines),
                            parse_mode="HTML", reply_markup=kb,
                            disable_web_page_preview=True)
                except Exception:
                    LOGGER.debug("steam_rental: club admin notify failed",
                                 exc_info=True)
        except Exception:
            LOGGER.debug("steam_rental: club admin notify outer fail",
                         exc_info=True)

    threading.Thread(target=_send, daemon=True).start()


# ── v6: Manual photo-review flow (ручное одобрение фото из ПК-клуба) ────
def _mr_start(cardinal: "Cardinal", order: Any, lot: dict[str, Any],
              duration_min: int) -> None:
    """Создаёт заявку и шлёт покупателю запрос фото. Уведомляет админа в TG."""
    _mr_create(
        order_id=str(order.id),
        buyer_id=int(order.buyer_id),
        buyer_username=str(order.buyer_username or ""),
        chat_id=order.chat_id,
        lot_key=str(lot.get("key") or ""),
        duration_min=int(duration_min),
    )
    text = _render_template("mr_request_photo", buyer_id=order.buyer_id)
    try:
        cardinal.send_message(
            order.chat_id, _strip_html(text),
            chat_name=order.buyer_username,
            interlocutor_id=order.buyer_id, watermark=False)
    except Exception:
        LOGGER.warning(
            "steam_rental: manual_review request_photo send failed",
            exc_info=True)
    _notify_tg(cardinal,
               f"📷 <b>Manual review</b>: новый заказ "
               f"#{order.id} от <b>{_esc(str(order.buyer_username))}</b>. "
               f"Жду фото-подтверждение от покупателя в чате FunPay.")


def _mr_on_photo(cardinal: "Cardinal", msg: Any, order_id: str,
                 image_link: str) -> None:
    """Покупатель прислал фото — переводим в pending_review и пересылаем
    админу в TG с кнопками Approve/Decline."""
    req = _mr_update(order_id, photo_url=image_link, status="pending_review")
    if not req:
        return
    # Ack покупателю.
    try:
        ack = _render_template("mr_photo_received", buyer_id=msg.author_id)
        cardinal.send_message(
            msg.chat_id, _strip_html(ack),
            chat_name=msg.chat_name,
            interlocutor_id=msg.author_id, watermark=False)
    except Exception:
        LOGGER.debug("steam_rental: mr ack failed", exc_info=True)

    # ── Fake-детектор для manual_review (если включён). ──────────────────
    # Если фото — AI-генерация, авто-возврат денег покупателю и алёрт
    # админу. В серой зоне просто помечаем вердикт, дальше admin решает.
    cfg = get_config()
    if (cfg.get("ai_fake_detector_enabled", True)
            and cfg.get("ai_fake_detector_in_manual_review", True)):
        def _bg_fake():
            try:
                fv = _ai_detect_fake_image(image_link)
                _mr_update(order_id, fake_verdict=fv)
                decision = _fake_verdict_classify(fv)
                if decision == "decline":
                    score = int(fv.get("ai_generated_score", 0))
                    rea = (fv.get("reasoning") or "")[:200]
                    _notify_tg(cardinal,
                               f"🚨 <b>Manual review</b>: фото для заказа "
                               f"#{order_id} распознано как AI-генерация "
                               f"({score}%). Авто-возврат денег.\n"
                               f"<i>{_esc(rea)}</i>")
                    _mr_decline(cardinal, order_id, admin_uid=None)
                    return
                # pass / manual / detector failed — отправляем админу
                # карточку с вердиктом fake-детектора.
                _mr_notify_admin(cardinal, order_id)
            except Exception:
                LOGGER.warning(
                    "steam_rental: mr fake-detector crash %s", order_id,
                    exc_info=True)
                _mr_notify_admin(cardinal, order_id)

        threading.Thread(target=_bg_fake, daemon=True,
                          name=f"sr-mr-fake-{order_id}").start()
        return

    _mr_notify_admin(cardinal, order_id)


def _mr_notify_admin(cardinal: "Cardinal", order_id: str) -> None:
    """Шлёт фото + кнопки «Одобрить / Отклонить» владельцу в Telegram."""
    req = _mr_get(order_id)
    if not req:
        return
    tg = getattr(cardinal, "telegram", None)
    if tg is None:
        return
    cfg = get_config()
    if not cfg.get("tg_notify", True):
        return

    lines = [
        "📷 <b>Manual review — фото на проверке</b>", "",
        f"Заказ: <code>#{_esc(str(order_id))}</code>",
        f"Покупатель: <b>{_esc(str(req.get('buyer_username')))}</b> "
        f"(id <code>{req.get('buyer_id')}</code>)",
        f"Лот: <code>{_esc(str(req.get('lot_key')))}</code>",
        f"Длительность: <b>"
        f"{_human_minutes(int(req.get('duration_min') or 0))}</b>",
    ]
    # ── Fake-детектор результат: предупреждение для админа. ──────────────
    fake = req.get("fake_verdict") or {}
    if fake:
        if fake.get("ok"):
            score = int(fake.get("ai_generated_score", 0))
            decision = _fake_verdict_classify(fake)
            badge = {
                "decline": "🚨 AI-генерация",
                "manual": "⚠️ Подозрение на AI",
                "pass": "✅ Реальное фото",
            }.get(decision, "❔ ?")
            lines.append(f"\n🔬 Fake-детектор: <b>{badge}</b> ({score}%)")
            arts = fake.get("artifacts") or []
            if arts:
                lines.append(
                    "Артефакты: <code>"
                    f"{_esc(', '.join(arts)[:180])}</code>")
            rea_f = fake.get("reasoning") or ""
            if rea_f:
                lines.append(f"<i>{_esc(rea_f[:180])}</i>")
        else:
            err_f = fake.get("error") or ""
            if err_f:
                lines.append(
                    f"\n🔬 Fake-детектор: <i>ошибка — "
                    f"{_esc(err_f[:180])}</i>")
    kb = None
    try:
        from telebot import types as _tbt
        kb = _tbt.InlineKeyboardMarkup(row_width=2)
        kb.add(
            _tbt.InlineKeyboardButton(
                "✅ Одобрить", callback_data=f"sr:mrapr:{order_id}"),
            _tbt.InlineKeyboardButton(
                "❌ Отклонить", callback_data=f"sr:mrdec:{order_id}"),
        )
    except Exception:
        kb = None

    photo_url = req.get("photo_url")

    def _send():
        try:
            for admin_id in getattr(tg, "authorized_users", []) or []:
                try:
                    if photo_url:
                        tg.bot.send_photo(
                            admin_id, photo_url,
                            caption="\n".join(lines)[:1024],
                            parse_mode="HTML", reply_markup=kb)
                    else:
                        tg.bot.send_message(
                            admin_id, "\n".join(lines),
                            parse_mode="HTML", reply_markup=kb,
                            disable_web_page_preview=True)
                except Exception:
                    LOGGER.debug(
                        "steam_rental: mr admin notify failed",
                        exc_info=True)
        except Exception:
            LOGGER.debug("steam_rental: mr admin notify outer fail",
                         exc_info=True)

    threading.Thread(target=_send, daemon=True).start()


def _mr_approve(cardinal: "Cardinal", order_id: str, *,
                admin_uid: int | None = None) -> tuple[bool, str]:
    """Одобряет заявку и запускает обычную выдачу. Возвращает (ok, msg)."""
    req = _mr_get(order_id)
    if not req:
        return False, "Заявка не найдена."
    if req.get("status") not in ("awaiting_photo", "pending_review"):
        return False, f"Уже {req.get('status')}, ничего не делаю."
    lots = list_lots()
    lot = lots.get(req.get("lot_key") or "")
    if not lot:
        return False, ("Лот <code>"
                        f"{_esc(req.get('lot_key') or '?')}</code> не найден.")
    duration_min = int(req.get("duration_min") or lot.get("duration_min") or 0)
    if duration_min <= 0:
        return False, "Длительность не задана."
    alias = _pick_free_alias(lot.get("aliases", []) or [])
    if not alias:
        return False, "Нет свободных аккаунтов в пуле лота."
    ok = deliver_account(cardinal,
                          alias=alias,
                          duration_min=duration_min,
                          order_id=str(order_id),
                          buyer_username=str(req.get("buyer_username") or ""),
                          buyer_id=int(req.get("buyer_id") or 0),
                          chat_id=req.get("chat_id"))
    if not ok:
        return False, f"deliver_account вернул False для {alias}."
    _mr_update(order_id, status="approved",
                decided_ts=_now(),
                decided_by=f"manual:{admin_uid or '?'}",
                alias_issued=alias)
    _notify_tg(cardinal,
               f"✅ <b>Manual review</b>: заявка #{order_id} одобрена. "
               f"Выдан акк <code>{alias}</code> покупателю "
               f"<b>{_esc(str(req.get('buyer_username')))}</b>.")
    return True, f"Одобрено, выдан {alias}."


def _mr_decline(cardinal: "Cardinal", order_id: str, *,
                admin_uid: int | None = None) -> tuple[bool, str]:
    """Отклоняет заявку: refund заказа через FunPay API + уведомление
    покупателя. Возвращает (ok, msg)."""
    req = _mr_get(order_id)
    if not req:
        return False, "Заявка не найдена."
    if req.get("status") not in ("awaiting_photo", "pending_review"):
        return False, f"Уже {req.get('status')}, ничего не делаю."

    refund_ok = False
    refund_err = ""
    try:
        # cardinal.account.refund(order_id) — POST orders/refund.
        # Возвращает None при успехе, бросает RefundError при ошибке.
        cardinal.account.refund(str(order_id))
        refund_ok = True
    except Exception as exc:
        refund_err = str(exc)
        LOGGER.error(
            "steam_rental: cardinal.account.refund(%s) crash", order_id,
            exc_info=True)

    _mr_update(order_id, status="declined",
                decided_ts=_now(),
                decided_by=f"manual:{admin_uid or '?'}",
                refund_ok=refund_ok,
                refund_err=refund_err[:300] if refund_err else "")
    # Уведомить покупателя в чате FunPay.
    try:
        text = _render_template("mr_declined", buyer_id=req.get("buyer_id"))
        cardinal.send_message(
            req.get("chat_id"), _strip_html(text),
            chat_name=req.get("buyer_username"),
            interlocutor_id=req.get("buyer_id"), watermark=False)
    except Exception:
        LOGGER.debug("steam_rental: mr decline notify buyer failed",
                     exc_info=True)
    if refund_ok:
        _notify_tg(cardinal,
                   f"❌ <b>Manual review</b>: заявка #{order_id} отклонена. "
                   f"Возврат средств выполнен через FunPay API.")
        return True, "Отклонено и возврат прошёл."
    _notify_tg(cardinal,
               f"⚠ <b>Manual review</b>: заявка #{order_id} отклонена, "
               f"но <b>возврат не прошёл</b>: <code>"
               f"{_esc(refund_err)[:300]}</code>\n"
               f"Сделай возврат вручную в кабинете FunPay.")
    return False, f"Отклонено, но refund: {refund_err[:200]}"


def _cmd_guard_code(cardinal: "Cardinal", msg: Any, text: str) -> None:
    """!код [логин] — Steam Guard код."""
    cfg = get_config()
    author_id = msg.author_id
    chat_id = msg.chat_id

    parts = text.split(None, 1)
    requested_login = parts[1].strip() if len(parts) > 1 else None

    if requested_login:
        acc = find_account(requested_login) or find_account_by_login(requested_login)
    else:
        acc_data = _find_buyer_active_rental(int(author_id))
        acc = acc_data if acc_data else None

    if not acc:
        text_resp = _render_template("guard_error", buyer_id=author_id)
        cardinal.send_message(
            chat_id, text_resp,
            chat_name=msg.chat_name,
            interlocutor_id=author_id, watermark=False)
        return

    rental = acc.get("rental") or {}
    if not rental or int(rental.get("buyer_id", -1)) != int(author_id):
        text_resp = _render_template("guard_error", buyer_id=author_id)
        cardinal.send_message(
            chat_id, text_resp,
            chat_name=msg.chat_name,
            interlocutor_id=author_id, watermark=False)
        return

    if rental.get("expires_at", 0) <= _now():
        cardinal.send_message(
            chat_id,
            "Срок аренды истёк, доступ закрыт.",
            chat_name=msg.chat_name,
            interlocutor_id=author_id, watermark=False)
        return

    # Тестовый аккаунт: Steam-данных нет — возвращаем фейковый код, чтобы
    # проверить сквозной поток команды !код без обращения к Steam.
    if acc.get("test"):
        code = "".join(random.choice("BCDFGHJKMNPQRTVWXY23456789") for _ in range(5))
        text_resp = _render_template(
            "guard_code",
            buyer_id=author_id,
            login=acc["account_name"],
            code=code,
        )
        cardinal.send_message(
            chat_id, text_resp,
            chat_name=msg.chat_name,
            interlocutor_id=author_id, watermark=False)
        return

    if not acc.get("shared_secret"):
        text_resp = _render_template("guard_error_no_secret",
                                     buyer_id=author_id)
        cardinal.send_message(
            chat_id, text_resp,
            chat_name=msg.chat_name,
            interlocutor_id=author_id, watermark=False)
        return

    try:
        sess = SteamSession(
            account_name=acc["account_name"],
            password=acc["password"],
            shared_secret=acc["shared_secret"],
            identity_secret=acc["identity_secret"],
            steamid=acc.get("steamid"),
        )
        code = sess.generate_2fa_code()
    except Exception as exc:
        LOGGER.error("steam_rental: guard code gen failed for %s", acc["alias"],
                     exc_info=True)
        cardinal.send_message(
            chat_id, f"Ошибка генерации кода: {exc}",
            chat_name=msg.chat_name,
            interlocutor_id=author_id, watermark=False)
        return

    text_resp = _render_template(
        "guard_code",
        buyer_id=author_id,
        login=acc["account_name"],
        code=code,
    )
    cardinal.send_message(
        chat_id, text_resp,
        chat_name=msg.chat_name,
        interlocutor_id=author_id, watermark=False)


def _cmd_extend(cardinal: "Cardinal", msg: Any) -> None:
    """!продлить — активирует extension-лот и шлёт ссылку покупателю."""
    author_id = msg.author_id
    chat_id = msg.chat_id

    acc = _find_buyer_active_rental(int(author_id))
    if not acc:
        cardinal.send_message(
            chat_id,
            "У вас нет активной аренды для продления.",
            chat_name=msg.chat_name,
            interlocutor_id=author_id, watermark=False)
        return

    alias = acc["alias"]
    game = _get_game_for_alias(alias)

    ext_link = ""
    ext_lot_key: str | None = None

    # 1. Приоритет: подходящий extension-лот (is_extension=True).
    ext_lot_key = _find_extension_lot_for_alias(alias)
    if ext_lot_key:
        # Активируем его на FunPay перед отправкой ссылки.
        if _set_funpay_lot_active(cardinal, ext_lot_key, True):
            ext_link = f"https://funpay.com/lots/offer?id={ext_lot_key}"
            LOGGER.info(
                "steam_rental: extension lot %s активирован по !продлить "
                "для buyer %s (alias %s)",
                ext_lot_key, author_id, alias)
            # v2.16.1: если за TTL минут не оплатят — лот выключится сам.
            try:
                _ttl_min = int(get_config().get(
                    "extension_active_ttl_minutes", 10) or 0)
            except Exception:
                _ttl_min = 10
            if _ttl_min > 0:
                _schedule_ext_lot_deactivation(
                    cardinal, ext_lot_key, _ttl_min)
        else:
            # Активировать не удалось — всё равно даём ссылку,
            # но логируем предупреждение.
            ext_link = f"https://funpay.com/lots/offer?id={ext_lot_key}"
            LOGGER.warning(
                "steam_rental: не удалось активировать extension lot %s "
                "на FunPay, отправляю ссылку как есть", ext_lot_key)

    # 2. Fallback: старая логика — extension_lot_ids у «материнского» лота.
    if not ext_link:
        lots = list_lots()
        for key, val in lots.items():
            ext_ids = val.get("extension_lot_ids") or []
            if alias in val.get("aliases", []) and ext_ids:
                ext_link = f"https://funpay.com/lots/offer?id={ext_ids[0]}"
                break

    # 3. Совсем fallback: ссылка на main-лот ИЗ ТОЙ ЖЕ ИГРЫ, в пуле
    #    которого есть этот аккаунт. Используем _combined_lot_pool, чтобы
    #    учесть привязку аккаунт↔игра по game_key (у некоторых аккаунтов
    #    alias не лежит в lot.aliases напрямую, а попадает в пул через
    #    `acc.game_key == lot.game_key`). Дополнительно отсеиваем
    #    extension-лоты — на этом шаге мы хотим main-лот.
    if not ext_link:
        lots = list_lots()
        acc_obj = find_account(alias) or {}
        acc_gkey = (acc_obj.get("game_key") or "").strip().lower()
        acc_game_name = (game or "").strip().lower()
        same_game_main: str | None = None
        any_main: str | None = None
        for key, val in lots.items():
            if not key.isdigit():
                continue
            if val.get("is_extension"):
                continue
            try:
                pool = _combined_lot_pool(val)
            except Exception:
                pool = list(val.get("aliases") or [])
            if alias not in pool:
                continue
            # совпадение по game_key или по имени игры
            lot_gkey = (val.get("game_key") or "").strip().lower()
            lot_game = (val.get("game") or "").strip().lower()
            same = (
                (acc_gkey and lot_gkey == acc_gkey) or
                (acc_game_name and lot_game == acc_game_name)
            )
            if same and same_game_main is None:
                same_game_main = key
                break
            if any_main is None:
                any_main = key
        chosen = same_game_main or any_main
        if chosen:
            ext_link = f"https://funpay.com/lots/offer?id={chosen}"

    text = _render_template(
        "extend",
        buyer_id=author_id,
        link=ext_link or "(ссылка недоступна)",
        login=acc["account_name"],
        game=game or "—",
        ttl_minutes=int(get_config().get(
            "extension_active_ttl_minutes", 10) or 0),
    )
    cardinal.send_message(
        chat_id, text,
        chat_name=msg.chat_name,
        interlocutor_id=author_id, watermark=False)


def _cmd_status(cardinal: "Cardinal", msg: Any) -> None:
    """!статус — информация об аренде."""
    author_id = msg.author_id
    chat_id = msg.chat_id

    acc = _find_buyer_active_rental(int(author_id))
    if not acc:
        # Сообщение «нет активной аренды» — короткое, локализуем по
        # языку покупателя.
        no_rental = (
            "You don't have an active rental."
            if _get_buyer_lang(author_id) == "en"
            else "У вас нет активной аренды."
        )
        cardinal.send_message(
            chat_id, no_rental,
            chat_name=msg.chat_name,
            interlocutor_id=author_id, watermark=False)
        return

    rental = acc["rental"]
    remain = max(0, rental["expires_at"] - _now())
    game = _get_game_for_alias(acc["alias"])

    text = _render_template(
        "status",
        buyer_id=author_id,
        login=acc["account_name"],
        game=game or "—",
        minutes=str(remain // 60),
        new_expires=_fmt_ts(rental["expires_at"]),
    )
    cardinal.send_message(
        chat_id, text,
        chat_name=msg.chat_name,
        interlocutor_id=author_id, watermark=False)


def _cmd_help(cardinal: "Cardinal", msg: Any) -> None:
    """!помощь — список команд (на языке покупателя)."""
    text = _render_template("help", buyer_id=msg.author_id)
    cardinal.send_message(
        msg.chat_id, text,
        chat_name=msg.chat_name,
        interlocutor_id=msg.author_id, watermark=False)


def _cmd_set_lang(cardinal: "Cardinal", msg: Any, lang: str) -> None:
    """!engrent / !rusrent — переключение языка диалога с ботом для
    конкретного покупателя. Меняем сразу и шлём подтверждение на
    выбранном языке."""
    if lang not in ("ru", "en"):
        return
    try:
        _set_buyer_lang(getattr(msg, "author_id", None), lang)
    except Exception:
        LOGGER.warning("steam_rental: set_buyer_lang failed", exc_info=True)
    if lang == "en":
        text = (
            "✅ Language switched to English.\n\n"
            "All bot messages in this chat will now be in English.\n"
            "Type !rusrent to switch back to Russian."
        )
    else:
        text = (
            "✅ Язык переключён на русский.\n\n"
            "Все сообщения бота в этом чате теперь будут на русском.\n"
            "Напиши !engrent чтобы переключить обратно на английский."
        )
    try:
        cardinal.send_message(
            msg.chat_id, text,
            chat_name=msg.chat_name,
            interlocutor_id=msg.author_id, watermark=False)
    except Exception:
        LOGGER.warning(
            "steam_rental: _cmd_set_lang send_message failed",
            exc_info=True)


# ── Telegram UI (inline-menu) ───────────────────────────────────────────────
_pending_state: dict[int, dict[str, Any]] = {}

# Глобальный фильтр списка аккаунтов (общий для всех админов).
_acc_filter: dict[str, Any] = {
    "mode": "all",      # all|free|rented|problem|frozen
    "search": "",        # подстрока по алиасу/логину/игре
    "sort": "alias",     # alias|expires|fails
}


def _filtered_accounts() -> list[dict[str, Any]]:
    """Возвращает список акков с учётом фильтра/сортировки/поиска."""
    accs = list_accounts()
    mode = _acc_filter.get("mode", "all")
    q = (_acc_filter.get("search") or "").strip().lower()
    sort = _acc_filter.get("sort", "alias")

    def _is_problem(a: dict[str, Any]) -> bool:
        return bool(a.get("login_failures", 0)
                    or a.get("chpwd_failures", 0)
                    or a.get("frozen"))

    if mode == "free":
        accs = [a for a in accs if not a.get("rental") and not a.get("frozen")]
    elif mode == "rented":
        accs = [a for a in accs if a.get("rental")]
    elif mode == "frozen":
        accs = [a for a in accs if a.get("frozen")]
    elif mode == "problem":
        accs = [a for a in accs if _is_problem(a)]

    if q:
        def _hit(a: dict[str, Any]) -> bool:
            blob = (
                (a.get("alias", "") + " "
                 + a.get("account_name", "") + " "
                 + (a.get("game") or "")).lower())
            return q in blob
        accs = [a for a in accs if _hit(a)]

    if sort == "expires":
        def _ex_key(a: dict[str, Any]) -> tuple[int, int, str]:
            r = a.get("rental")
            if a.get("frozen"):
                return (2, 0, a.get("alias", ""))
            if r:
                return (0, int(r.get("expires_at", 0)), a.get("alias", ""))
            return (1, 0, a.get("alias", ""))
        accs.sort(key=_ex_key)
    elif sort == "fails":
        accs.sort(key=lambda a: (
            -(int(a.get("login_failures", 0))
              + int(a.get("chpwd_failures", 0))
              + (5 if a.get("frozen") else 0)),
            a.get("alias", "")))
    else:
        accs.sort(key=lambda a: a.get("alias", "").lower())
    return accs


_FILTER_LABELS = {
    "all": "Все",
    "free": "🟢 Свободные",
    "rented": "🔴 В аренде",
    "problem": "⚠️ Проблемные",
    "frozen": "❄️ Замороженные",
}

_SORT_LABELS = {
    "alias": "По алиасу",
    "expires": "По таймеру",
    "fails": "По ошибкам",
}


def _sid(s: str) -> str:
    return hashlib.md5(str(s).encode("utf-8")).hexdigest()[:8]


def _resolve_alias(sid: str) -> str | None:
    for a in list_accounts():
        if _sid(a["alias"]) == sid:
            return a["alias"]
    return None


def _resolve_lot(sid: str) -> str | None:
    for key in list_lots().keys():
        if _sid(key) == sid:
            return key
    return None


def _resolve_orphan_lot(sid: str) -> str | None:
    """Ищет «сиротский» lot_id — есть в ссылках (games.json,
    extension_lot_ids других лотов), но отсутствует в lots.json.

    Возвращает строку-ID или None.
    """
    lots = list_lots()
    candidates: set[str] = set()
    for g in list_games().values():
        for fld in ("lot_ids", "ext_lot_ids"):
            for lid in (g.get(fld) or []):
                candidates.add(str(lid))
    for lot in lots.values():
        for lid in (lot.get("extension_lot_ids") or []):
            candidates.add(str(lid))
    for cand in candidates:
        if cand in lots:
            continue
        if _sid(cand) == sid:
            return cand
    return None


def _resolve_game(sid: str) -> str | None:
    for key in list_games().keys():
        if _sid(key) == sid:
            return key
    return None


def _esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _register_tg_commands(cardinal: "Cardinal") -> None:
    tg = getattr(cardinal, "telegram", None)
    if tg is None:
        return

    from telebot import types as tbtypes  # type: ignore

    def _is_admin_user(uid: int) -> bool:
        try:
            return uid in tg.authorized_users
        except Exception:
            return False

    # ───── Рендер меню ───────────────────────────────────────────────────
    def _kb_main() -> tbtypes.InlineKeyboardMarkup:
        accs = list_accounts()
        lots = list_lots()
        games = list_games()
        active = sum(1 for a in accs if a.get("rental"))
        frozen = sum(1 for a in accs if a.get("frozen"))
        kb = tbtypes.InlineKeyboardMarkup(row_width=2)
        kb.add(
            tbtypes.InlineKeyboardButton(
                f"📋 Аккаунты ({len(accs)})", callback_data="sr:accs:0"),
            tbtypes.InlineKeyboardButton(
                f"🎮 Игры ({len(games)})", callback_data="sr:games"),
        )
        kb.add(
            tbtypes.InlineKeyboardButton(
                f"🎮 Аренда ({active})", callback_data="sr:rental"),
            tbtypes.InlineKeyboardButton(
                "⚙ Настройки", callback_data="sr:settings"),
        )
        kb.add(
            tbtypes.InlineKeyboardButton(
                "📝 Шаблоны", callback_data="sr:templates"),
            tbtypes.InlineKeyboardButton(
                "📊 Статистика", callback_data="sr:stats"),
        )
        kb.add(
            tbtypes.InlineKeyboardButton(
                "🔧 Инструменты", callback_data="sr:tools"),
        )
        kb.add(
            tbtypes.InlineKeyboardButton(
                "❌ Закрыть", callback_data="sr:close"),
        )
        return kb

    def _text_main() -> str:
        accs = list_accounts()
        lots = list_lots()
        games = list_games()
        active = [a for a in accs if a.get("rental")]
        frozen = [a for a in accs if a.get("frozen")]
        free = [a for a in accs if not a.get("rental") and not a.get("frozen")]
        return (
            f"<b>🎮 Steam Rental v{VERSION}</b>\n\n"
            f"Аккаунтов: <b>{len(accs)}</b> "
            f"(свободно: {len(free)}, занято: {len(active)}, "
            f"заморожено: {len(frozen)})\n"
            f"Игр: <b>{len(games)}</b> • "
            f"Лотов: <b>{len(lots)}</b>\n"
            f"Активных аренд: <b>{len(active)}</b>\n\n"
            "Выбери раздел:"
        )

    def _text_tools() -> str:
        return "<b>🔧 Инструменты</b>\n\nВыберите раздел:"

    def _kb_tools() -> tbtypes.InlineKeyboardMarkup:
        kb = tbtypes.InlineKeyboardMarkup(row_width=2)
        kb.add(
            tbtypes.InlineKeyboardButton(
                "📜 История", callback_data="sr:history"),
            tbtypes.InlineKeyboardButton(
                "🔧 Массовые", callback_data="sr:bulk"),
        )
        kb.add(
            tbtypes.InlineKeyboardButton(
                "🚩 Ивенты", callback_data="sr:events"),
            tbtypes.InlineKeyboardButton(
                "📝 Инструкция", callback_data="sr:instructions"),
        )
        kb.add(
            tbtypes.InlineKeyboardButton(
                "🎮 Remote Play", callback_data="sr:rp:main"),
            tbtypes.InlineKeyboardButton(
                "📋 Очередь", callback_data="sr:queue:view"),
        )
        kb.add(
            tbtypes.InlineKeyboardButton(
                "❓ Помощь", callback_data="sr:help"),
        )
        kb.add(
            tbtypes.InlineKeyboardButton(
                "◀️ Назад", callback_data="sr:main"),
        )
        return kb

    def _kb_accs(page: int = 0) -> tbtypes.InlineKeyboardMarkup:
        accs = _filtered_accounts()
        per_page = 8
        kb = tbtypes.InlineKeyboardMarkup(row_width=1)
        start = page * per_page
        for a in accs[start:start + per_page]:
            r = a.get("rental")
            f = a.get("frozen")
            if f:
                marker = "❄️"
            elif r:
                marker = "🔴"
            else:
                marker = "🟢"
            label = f"{marker} {a['alias']}"
            if a.get("test"):
                label += " 🧪"
            if a.get("game"):
                label += f" • {a['game'][:10]}"
            if r:
                label += f" — {r.get('buyer_username', '?')[:12]}"
            warn_marks = []
            if a.get("login_failures", 0):
                warn_marks.append(f"⚠️L{a['login_failures']}")
            if a.get("chpwd_failures", 0):
                warn_marks.append(f"⚠️P{a['chpwd_failures']}")
            if warn_marks:
                label += " " + "".join(warn_marks)
            kb.add(tbtypes.InlineKeyboardButton(
                label, callback_data=f"sr:acc:{_sid(a['alias'])}"))
        nav = []
        if page > 0:
            nav.append(tbtypes.InlineKeyboardButton(
                "◀", callback_data=f"sr:accs:{page - 1}"))
        if start + per_page < len(accs):
            nav.append(tbtypes.InlineKeyboardButton(
                "▶", callback_data=f"sr:accs:{page + 1}"))
        if nav:
            kb.row(*nav)
        kb.add(
            tbtypes.InlineKeyboardButton(
                f"🔍 Фильтр: {_FILTER_LABELS.get(_acc_filter['mode'], '?')}",
                callback_data="sr:flt"),
            tbtypes.InlineKeyboardButton(
                f"↕ {_SORT_LABELS.get(_acc_filter['sort'], '?')}",
                callback_data="sr:srt"),
        )
        kb.add(
            tbtypes.InlineKeyboardButton(
                "➕ Добавить", callback_data="sr:add"),
            tbtypes.InlineKeyboardButton(
                "📥 Импорт .maFile", callback_data="sr:bulkimport"),
        )
        kb.add(tbtypes.InlineKeyboardButton(
            "🧪 Тестовый аккаунт", callback_data="sr:addtest"))
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ Назад", callback_data="sr:main"))
        return kb

    def _text_accs() -> str:
        accs_all = list_accounts()
        accs = _filtered_accounts()
        if not accs_all:
            return ("<b>📋 Аккаунты</b>\n\n"
                    "Пока пусто. Нажми «➕ Добавить» или «📥 Импорт».")
        lines = [f"<b>📋 Аккаунты: {len(accs)} из {len(accs_all)}</b>"]
        mode = _acc_filter.get("mode", "all")
        if mode != "all":
            lines.append(f"🔍 Фильтр: <code>{_esc(_FILTER_LABELS.get(mode, mode))}</code>")
        if _acc_filter.get("search"):
            lines.append(f"🔤 Поиск: <code>{_esc(_acc_filter['search'])}</code>")
        if _acc_filter.get("sort", "alias") != "alias":
            lines.append(
                f"↕ Сорт: <code>{_esc(_SORT_LABELS.get(_acc_filter['sort'], '?'))}</code>")
        lines.append("\nВыбери аккаунт:")
        return "\n".join(lines)

    def _kb_acc_filter() -> tbtypes.InlineKeyboardMarkup:
        kb = tbtypes.InlineKeyboardMarkup(row_width=2)
        cur = _acc_filter.get("mode", "all")
        for key, lbl in _FILTER_LABELS.items():
            mark = "✅ " if key == cur else ""
            kb.add(tbtypes.InlineKeyboardButton(
                mark + lbl, callback_data=f"sr:fltm:{key}"))
        kb.add(
            tbtypes.InlineKeyboardButton(
                "🔤 Поиск", callback_data="sr:fltq"),
            tbtypes.InlineKeyboardButton(
                "♻️ Сброс", callback_data="sr:fltrst"),
        )
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ К списку", callback_data="sr:accs:0"))
        return kb

    def _text_acc_filter() -> str:
        cur = _acc_filter.get("mode", "all")
        q = _acc_filter.get("search", "")
        return (
            "<b>🔍 Фильтр аккаунтов</b>\n\n"
            f"Текущий режим: <b>{_esc(_FILTER_LABELS.get(cur, cur))}</b>\n"
            f"Поиск: <code>{_esc(q) if q else '—'}</code>\n\n"
            "Поиск работает по алиасу, логину Steam и названию игры.\n"
            "«Проблемные» = есть неудачи логина или смены пароля."
        )

    def _kb_acc_sort() -> tbtypes.InlineKeyboardMarkup:
        kb = tbtypes.InlineKeyboardMarkup(row_width=1)
        cur = _acc_filter.get("sort", "alias")
        for key, lbl in _SORT_LABELS.items():
            mark = "✅ " if key == cur else ""
            kb.add(tbtypes.InlineKeyboardButton(
                mark + lbl, callback_data=f"sr:srts:{key}"))
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ К списку", callback_data="sr:accs:0"))
        return kb

    def _text_acc_sort() -> str:
        cur = _acc_filter.get("sort", "alias")
        return (
            "<b>↕ Сортировка</b>\n\n"
            f"Сейчас: <b>{_esc(_SORT_LABELS.get(cur, cur))}</b>\n\n"
            "«По таймеру» = сначала аренды с ближайшим концом.\n"
            "«По ошибкам» = проблемные сверху."
        )

    def _kb_acc(alias: str) -> tbtypes.InlineKeyboardMarkup:
        sid = _sid(alias)
        acc = find_account(alias)
        is_frozen = acc.get("frozen", False) if acc else False
        has_history = bool(acc and (acc.get("previous_passwords") or []))
        kb = tbtypes.InlineKeyboardMarkup(row_width=2)
        kb.add(
            tbtypes.InlineKeyboardButton(
                "🔑 Показать пароль", callback_data=f"sr:show:{sid}"),
            tbtypes.InlineKeyboardButton(
                "🛡 Steam Guard", callback_data=f"sr:guard:{sid}"),
        )
        kb.add(
            tbtypes.InlineKeyboardButton(
                "🔁 Сменить пароль", callback_data=f"sr:chpwd:{sid}"),
            tbtypes.InlineKeyboardButton(
                "📤 Отозвать сессии", callback_data=f"sr:revoke:{sid}"),
        )
        freeze_label = "🔥 Разморозить" if is_frozen else "❄️ Заморозить"
        kb.add(
            tbtypes.InlineKeyboardButton(
                freeze_label, callback_data=f"sr:freeze:{sid}"),
            tbtypes.InlineKeyboardButton(
                "🎮 Игра", callback_data=f"sr:setgame:{sid}"),
        )
        kb.add(
            tbtypes.InlineKeyboardButton(
                "✏️ Алиас", callback_data=f"sr:ralias:{sid}"),
            tbtypes.InlineKeyboardButton(
                "🔓 Освободить", callback_data=f"sr:free:{sid}"),
        )
        cost_val = float((acc or {}).get("cost", 0.0) or 0.0)
        cost_lbl = f"💰 Стоимость: {cost_val:.0f}₽" if cost_val > 0 \
            else "💰 Стоимость"
        kb.add(
            tbtypes.InlineKeyboardButton(
                cost_lbl, callback_data=f"sr:setcost:{sid}"),
            tbtypes.InlineKeyboardButton(
                "📊 Статистика", callback_data=f"sr:accstats:{sid}"),
        )
        # Per-account post_delivery override
        _pd_has = bool((acc or {}).get("post_delivery") is not None
                       and (acc or {}).get("post_delivery") != ""
                       ) if acc else False
        _pd_off = bool(acc and "post_delivery" in acc
                       and (acc.get("post_delivery") or "").strip() == "")
        if _pd_off:
            _pd_lbl = "📧 Доп. инфо: ⛔ выкл"
        elif _pd_has:
            _pd_lbl = "📧 Доп. инфо: ✏️ кастом"
        else:
            _pd_lbl = "📧 Доп. инфо"
        kb.add(tbtypes.InlineKeyboardButton(
            _pd_lbl, callback_data=f"sr:setpd_acc:{sid}"))
        kb.add(tbtypes.InlineKeyboardButton(
            "🗑 Удалить", callback_data=f"sr:del:{sid}"))
        if has_history:
            kb.add(tbtypes.InlineKeyboardButton(
                "📜 История паролей",
                callback_data=f"sr:pwhist:{sid}"))
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ К списку", callback_data="sr:accs:0"))
        return kb

    def _text_acc(alias: str, *, show_pw: bool = False,
                  show_history: bool = False) -> str:
        acc = find_account(alias)
        if not acc:
            return "Аккаунт не найден."
        r = acc.get("rental")
        frozen = acc.get("frozen", False)
        freeze_reason = acc.get("freeze_reason", "")
        game = acc.get("game") or _get_game_for_alias(alias) or "—"

        status_line = ""
        if frozen:
            status_line = "❄️ <b>Заморожен</b> (не выдаётся)\n"
            if freeze_reason:
                status_line += f"  Причина: <i>{_esc(freeze_reason)}</i>\n"
        elif r:
            remain = max(0, r["expires_at"] - _now())
            status_line = (
                f"🔴 <b>В аренде</b>\n"
                f"  Покупатель: <code>{_esc(r.get('buyer_username', '?'))}</code> "
                f"(id {r.get('buyer_id', '?')})\n"
                f"  Заказ: <code>#{_esc(r.get('order_id', '?'))}</code>\n"
                f"  До: <code>{_fmt_ts(r['expires_at'])} МСК</code>\n"
                f"  Осталось: <b>{_human_minutes(remain // 60 + (1 if remain % 60 else 0))}</b>\n")
        else:
            status_line = "🟢 <b>Свободен</b>\n"

        pw = acc.get("password", "")
        pw_view = pw if show_pw else ("•" * min(len(pw), 12) if pw else "—")
        fails = acc.get("login_failures", 0)
        chpwd_fails = acc.get("chpwd_failures", 0)
        warn_lines = []
        if fails:
            warn_lines.append(f"⚠️ Неудач логина: {fails}")
        if chpwd_fails:
            warn_lines.append(
                f"⚠️ Ошибок смены пароля: {chpwd_fails}")
            if acc.get("chpwd_last_error"):
                warn_lines.append(
                    f"  Последняя: <i>{_esc(acc['chpwd_last_error'][:120])}</i>")
        warn_block = ("\n".join(warn_lines) + "\n") if warn_lines else ""

        history = acc.get("previous_passwords") or []
        history_block = ""
        if history:
            history_block = (
                f"\n📜 Старые пароли: <b>{len(history)}</b> "
                f"(лимит 5, см. <code>sr:pwhist</code>)\n")
            if show_history:
                history_block += "\n<b>История паролей:</b>\n"
                for i, entry in enumerate(reversed(history), 1):
                    ts = _fmt_ts(entry.get("ts", 0))
                    pw_h = entry.get("pw", "")
                    history_block += (
                        f"  {i}. <code>{_fmt_ts(entry.get('ts', 0))}</code>: "
                        f"<code>{_esc(pw_h)}</code>\n")

        # ── 💵 Финансы по аккаунту ─────────────────────────────────
        st = acc.get("stats") or {}
        total_rentals = int(st.get("rentals_count", 0) or 0)
        total_revenue = float(st.get("total_revenue", 0) or 0)
        total_reviews = int(st.get("reviews_count", 0) or 0)
        refunded_count = int(st.get("refunded_count", 0) or 0)
        cost = float(acc.get("cost", 0.0) or 0.0)
        profit = total_revenue - cost
        roi_str = ""
        if cost > 0:
            roi_str = f" • ROI {(profit / cost) * 100:+.0f}%"
        # v2.22.4: refund-блок — видно сразу, был ли возврат и сколько.
        refund_part = ""
        if refunded_count:
            refund_part = f" • 💸 Возвратов: <b>{refunded_count}</b>"
        finance_block = (
            f"\n💵 <b>Финансы:</b> "
            f"{total_revenue:.0f}₽ − {cost:.0f}₽ = "
            f"<b>{profit:+.0f}₽</b>{roi_str}\n"
            f"🛒 Продаж: <b>{total_rentals}</b> • "
            f"⭐ Отзывов: <b>{total_reviews}</b>"
            f"{refund_part} • "
            f"<i>(подробнее — 📊 Статистика)</i>\n"
        )

        return (
            f"<b>🎮 {_esc(alias)}</b>\n\n"
            f"Логин: <code>{_esc(acc.get('account_name', ''))}</code>\n"
            f"Пароль: <code>{_esc(pw_view)}</code>\n"
            f"SteamID: <code>{_esc(acc.get('steamid') or '—')}</code>\n"
            f"Игра: <b>{_esc(game)}</b>\n\n"
            f"{warn_block}"
            f"{status_line}"
            f"{finance_block}"
            f"{history_block}"
        )

    def _lot_status_icon(key: str, val: dict[str, Any], free: int) -> str:
        """Иконка статуса активации лота на FunPay.

        ✅ — активен; ⛔ — выключен; ❓ — состояние неизвестно (ещё ни
        разу не пытались синхронизировать); ⚠ — последняя попытка
        синхронизации не удалась.
        """
        cache = _get_lot_active_cached(key)
        if cache is None:
            # Ещё не синхронизировали. Делаем «ожидание» на основе
            # бизнес-логики плагина.
            if val.get("is_extension"):
                return "⛔"  # extension-лот по умолчанию выключен
            return "✅" if free > 0 else "⛔"
        if cache.get("result") == "fail":
            return "⚠"
        return "✅" if cache.get("active") else "⛔"

    def _kb_lots() -> tbtypes.InlineKeyboardMarkup:
        lots = list_lots()
        kb = tbtypes.InlineKeyboardMarkup(row_width=1)
        for key, val in lots.items():
            game = val.get("game", "")
            free = _count_free_accounts(_combined_lot_pool(val))
            # Маркер extension-лота: привязан к чему-то для продления.
            is_ext = bool(val.get("is_extension")) \
                or bool(_is_extension_lot(key)) \
                or bool(val.get("extension_games"))
            type_icon = "🔄" if is_ext else "🎯"
            status_icon = _lot_status_icon(key, val, free)
            label = f"{status_icon}{type_icon} {key[:14]}"
            if game:
                label += f" • {game[:10]}"
            label += f" — {_human_minutes(val['duration_min'])} — {free} св."
            kb.add(tbtypes.InlineKeyboardButton(
                label, callback_data=f"sr:lot:{_sid(key)}"))
        kb.add(
            tbtypes.InlineKeyboardButton(
                "➕ Добавить лот", callback_data="sr:newlot"),
            tbtypes.InlineKeyboardButton(
                "📥 Массово", callback_data="sr:newlots"),
        )
        kb.add(tbtypes.InlineKeyboardButton(
            "🔄 Добавить лот для продления",
            callback_data="sr:newextlot"))
        kb.add(tbtypes.InlineKeyboardButton(
            "🔁 Переактивировать лоты",
            callback_data="sr:reacttlots"))
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ Назад", callback_data="sr:main"))
        return kb

    def _text_lots() -> str:
        lots = list_lots()
        if not lots:
            return ("<b>🎯 Лоты</b>\n\n"
                    "Пока пусто. Нажми «➕ Добавить лот».")

        # Подсчёт статусов
        n_on = n_off = n_unknown = n_fail = 0
        for key, val in lots.items():
            free = _count_free_accounts(_combined_lot_pool(val))
            cache = _get_lot_active_cached(key)
            if cache is None:
                # Прогноз по бизнес-логике
                if val.get("is_extension"):
                    n_off += 1
                elif free > 0:
                    n_unknown += 1
                else:
                    n_off += 1
            elif cache.get("result") == "fail":
                n_fail += 1
            elif cache.get("active"):
                n_on += 1
            else:
                n_off += 1

        return (
            f"<b>🎯 Лоты ({len(lots)})</b>\n"
            f"✅ Включено: <b>{n_on}</b>  •  "
            f"⛔ Выключено: <b>{n_off}</b>"
            + (f"  •  ❓ Не синхронизировано: <b>{n_unknown}</b>"
               if n_unknown else "")
            + (f"  •  ⚠ С ошибкой: <b>{n_fail}</b>" if n_fail else "")
            + "\n\n"
            "<i>Иконки слева: ✅ — лот включён на FunPay, ⛔ — выключен, "
            "⚠ — последняя синхронизация не удалась, ❓ — состояние "
            "неизвестно (ещё не синхронизировано). "
            "🎯 — обычный лот, 🔄 — лот для продления.</i>\n\n"
            "Выбери лот:")

    # ──── v6: Игры (game → lots) ────
    def _kb_games() -> tbtypes.InlineKeyboardMarkup:
        games = list_games()
        lots = list_lots()
        kb = tbtypes.InlineKeyboardMarkup(row_width=1)
        for gkey, g in games.items():
            main_ids = g.get("lot_ids") or []
            ext_ids = g.get("ext_lot_ids") or []
            # Считаем сколько всего аккаунтов и сколько свободно
            all_aliases: set[str] = set()
            for lid in main_ids + ext_ids:
                lot = lots.get(str(lid), {})
                all_aliases.update(_combined_lot_pool(lot))
            free = _count_free_accounts(list(all_aliases))
            # Активные/неактивные лоты (по кэшу FunPay)
            on = off = unk = 0
            for lid in main_ids:
                cache = _get_lot_active_cached(str(lid))
                if cache is None:
                    unk += 1
                elif cache.get("result") == "fail":
                    unk += 1
                elif cache.get("active"):
                    on += 1
                else:
                    off += 1
            status_lbl = f"✅{on} ⛔{off}" + (f" ❓{unk}" if unk else "")
            free_lbl = f"{free} своб." if free else "0 своб."
            label = (f"🎮 {g.get('name', gkey)} — "
                     f"{len(main_ids)} main, {len(ext_ids)} ext — "
                     f"{status_lbl} — {free_lbl}")
            kb.add(tbtypes.InlineKeyboardButton(
                label, callback_data=f"sr:game:{_sid(gkey)}"))
        kb.add(tbtypes.InlineKeyboardButton(
            "🔁 Переактивировать все лоты",
            callback_data="sr:reacttlots"))
        kb.add(tbtypes.InlineKeyboardButton(
            "➕ Добавить игру", callback_data="sr:addgame"))
        kb.add(tbtypes.InlineKeyboardButton(
            "📋 Старый список лотов (legacy)", callback_data="sr:lots"))
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ Назад", callback_data="sr:main"))
        return kb

    def _text_games() -> str:
        games = list_games()
        if not games:
            return ("<b>🎮 Игры</b>\n\n"
                    "Пока нет ни одной игры. Нажми «➕ Добавить игру».")
        # Сводка по лотам
        on = off = unk = 0
        for g in games.values():
            for lid in (g.get("lot_ids") or []):
                cache = _get_lot_active_cached(str(lid))
                if cache is None:
                    unk += 1
                elif cache.get("result") == "fail":
                    unk += 1
                elif cache.get("active"):
                    on += 1
                else:
                    off += 1
        sum_lbl = (f"✅ Включено: <b>{on}</b>  •  "
                   f"⛔ Выключено: <b>{off}</b>")
        if unk:
            sum_lbl += f"  •  ❓ Неизвестно: <b>{unk}</b>"
        return (f"<b>🎮 Игры ({len(games)})</b>\n"
                f"{sum_lbl}\n\n"
                "✅/⛔/❓ — состояние лотов на FunPay. "
                "Выбери игру:")

    def _kb_game(gkey: str) -> tbtypes.InlineKeyboardMarkup:
        g = get_game(gkey)
        if not g:
            return tbtypes.InlineKeyboardMarkup()
        lots = list_lots()
        main_ids = g.get("lot_ids") or []
        ext_ids = g.get("ext_lot_ids") or []
        kb = tbtypes.InlineKeyboardMarkup(row_width=1)
        # Кнопка "лот" с актив/неактив статусом
        def _lot_btn(lid, kind):
            lot = lots.get(str(lid), {})
            cache = _get_lot_active_cached(str(lid))
            if cache is None or cache.get("result") == "fail":
                icon = "❓"
            elif cache.get("active"):
                icon = "✅"
            else:
                icon = "⛔"
            kind_icon = "🎯" if kind == "main" else "🔄"
            l_free = _count_free_accounts(_combined_lot_pool(lot))
            label = (f"{icon}{kind_icon} <code>{_esc(str(lid))}</code> "
                     f"({l_free} св.)")
            return tbtypes.InlineKeyboardButton(
                label, callback_data=f"sr:lot:{_sid(str(lid))}")
        if main_ids:
            kb.add(tbtypes.InlineKeyboardButton(
                "── Main лоты ──", callback_data="sr:noop"))
            for lid in main_ids:
                kb.add(_lot_btn(lid, "main"))
        if ext_ids:
            kb.add(tbtypes.InlineKeyboardButton(
                "── Ext лоты (для продления) ──", callback_data="sr:noop"))
            for lid in ext_ids:
                kb.add(_lot_btn(lid, "ext"))
        kb.add(tbtypes.InlineKeyboardButton(
            "🔄 Обновить статус лотов этой игры",
            callback_data=f"sr:game_react:{_sid(gkey)}"))
        kb.add(tbtypes.InlineKeyboardButton(
            "👥 Аккаунты игры",
            callback_data=f"sr:gacc:{_sid(gkey)}"))
        kb.add(
            tbtypes.InlineKeyboardButton(
                "➕ Main лот (ID)", callback_data=f"sr:gaddmain:{_sid(gkey)}"),
            tbtypes.InlineKeyboardButton(
                "➕ Ext лот (ID)", callback_data=f"sr:gaddext:{_sid(gkey)}"),
        )
        kb.add(tbtypes.InlineKeyboardButton(
            "🗑 Удалить игру", callback_data=f"sr:gdel:{_sid(gkey)}"))
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ К играм", callback_data="sr:games"))
        return kb

    def _text_game(gkey: str) -> str:
        g = get_game(gkey)
        if not g:
            return "Игра не найдена."
        lots = list_lots()
        main_ids = g.get("lot_ids") or []
        ext_ids = g.get("ext_lot_ids") or []
        global_pool: list[str] = list(g.get("global_aliases") or [])
        # Аккаунты, привязанные к игре через acc.game_key — авто-пул.
        gkey_lc = str(gkey).lower()
        auto_pool: list[str] = []
        for a in list_accounts():
            if (a.get("game_key") or "").strip().lower() == gkey_lc:
                al = a.get("alias", "")
                if al:
                    auto_pool.append(al)
        # Считаем свободные
        all_aliases: set[str] = set()
        for lid in main_ids + ext_ids:
            all_aliases.update(_combined_lot_pool(lots.get(str(lid), {})))
        free = _count_free_accounts(list(all_aliases))
        def _lot_lbl(lid):
            lot = lots.get(str(lid), {})
            if not lot:
                return f"<code>{lid}</code> (нет в БД)"
            l_free = _count_free_accounts(_combined_lot_pool(lot))
            cache = _get_lot_active_cached(str(lid))
            if cache is None or cache.get("result") == "fail":
                icon = "❓"
            elif cache.get("active"):
                icon = "✅"
            else:
                icon = "⛔"
            return f"{icon} <code>{_esc(str(lid))}</code> ({l_free} св.)"
        main_lines = ", ".join(_lot_lbl(x) for x in main_ids) or "—"
        ext_lines = ", ".join(_lot_lbl(x) for x in ext_ids) or "—"
        auto_preview = ", ".join(auto_pool[:6])
        if len(auto_pool) > 6:
            auto_preview += f", … (+{len(auto_pool) - 6})"
        return (
            f"<b>🎮 {g.get('name', gkey)}</b>\n\n"
            f"<b>Main лоты</b>: {main_lines}\n"
            f"<b>Extension лоты</b> (продление): {ext_lines}\n\n"
            f"<b>👥 Аккаунты игры</b>: <b>{len(auto_pool)}</b>"
            + (f"  <code>{_esc(auto_preview)}</code>" if auto_pool else "")
            + "\n"
            + (f"Старый ручной пул (global_aliases): "
               f"<b>{len(global_pool)}</b> акк.\n" if global_pool else "")
            + f"Свободно сейчас: <b>{free}</b>\n\n"
            "<i>«Аккаунты игры» автоматически попадают в пул "
            "<b>всех лотов</b> этой игры — добавлять в каждый лот "
            "отдельно не нужно. Управление — кнопкой "
            "«👥 Аккаунты игры» ниже.</i>\n"
            "<i>✅/⛔/❓ — состояние лота на FunPay.</i>"
        )

    # ──── v6: Аренда (выдать/активные/завершить/продлить/отменить) ────
    def _kb_rental() -> tbtypes.InlineKeyboardMarkup:
        kb = tbtypes.InlineKeyboardMarkup(row_width=2)
        kb.add(
            tbtypes.InlineKeyboardButton(
                "🆕 Выдать", callback_data="sr:rissue"),
            tbtypes.InlineKeyboardButton(
                "📋 Активные", callback_data="sr:ractive"),
        )
        kb.add(
            tbtypes.InlineKeyboardButton(
                "✅ Завершить", callback_data="sr:rfinish"),
            tbtypes.InlineKeyboardButton(
                "🔁 Продлить", callback_data="sr:rextend"),
        )
        kb.add(
            tbtypes.InlineKeyboardButton(
                "❌ Отменить", callback_data="sr:rcancel"),
        )
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ Назад", callback_data="sr:main"))
        return kb

    def _text_rental() -> str:
        return ("<b>🎮 Аренда</b>\n\n"
                "Доступные действия: выдача, завершение, продление и отмена аренды.\n"
                "Используйте кнопки ниже, чтобы управлять текущими сессиями.")

    def _kb_rental_active(page: int = 0) -> tbtypes.InlineKeyboardMarkup:
        rentals = _list_active_rentals()
        per_page = 8
        kb = tbtypes.InlineKeyboardMarkup(row_width=1)
        for r in rentals[page * per_page:(page + 1) * per_page]:
            sid = _sid(r["id"])
            kb.add(tbtypes.InlineKeyboardButton(
                f"🔴 {r['account']} → {r['buyer']} "
                f"({r['remaining_str']})",
                callback_data=f"sr:rasg:{sid}"))
        nav: list[tbtypes.InlineKeyboardButton] = []
        if page > 0:
            nav.append(tbtypes.InlineKeyboardButton(
                "◀️", callback_data=f"sr:ractive:{page - 1}"))
        if (page + 1) * per_page < len(rentals):
            nav.append(tbtypes.InlineKeyboardButton(
                "▶️", callback_data=f"sr:ractive:{page + 1}"))
        if nav:
            kb.row(*nav)
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ Назад", callback_data="sr:rental"))
        return kb

    def _text_rental_active() -> str:
        rentals = _list_active_rentals()
        if not rentals:
            return "<b>📋 Активные аренды</b>\n\nНет активных аренд."
        return f"<b>📋 Активные аренды ({len(rentals)})</b>"

    def _kb_rental_pick_account(action: str) -> tbtypes.InlineKeyboardMarkup:
        """Кнопки выбора аккаунта для выдачи/завершения/продления/отмены."""
        accs = list_accounts()
        free = [a for a in accs
                if not a.get("frozen") and not find_active_rental(a.get("alias"))]
        if action == "issue":
            accs = free
        kb = tbtypes.InlineKeyboardMarkup(row_width=2)
        for a in accs[:24]:
            sid = _sid(a.get("alias", ""))
            label = f"🟢 {a.get('alias', '?')}"
            if a.get("game"):
                label += f" • {a['game'][:8]}"
            kb.add(tbtypes.InlineKeyboardButton(
                label, callback_data=f"sr:r{action[:3]}_acc:{sid}"))
        if not accs:
            kb.add(tbtypes.InlineKeyboardButton(
                "Нет подходящих аккаунтов", callback_data="sr:rental"))
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ Назад", callback_data="sr:rental"))
        return kb

    def _kb_lot(key: str) -> tbtypes.InlineKeyboardMarkup:
        sid = _sid(key)
        lots = list_lots()
        val = lots.get(key, {})
        club_on = "✅" if val.get("club_mode") else "❌"
        mr_on = "✅" if val.get("manual_review") else "❌"
        kb = tbtypes.InlineKeyboardMarkup(row_width=2)
        kb.add(
            tbtypes.InlineKeyboardButton(
                "✏️ Длительность", callback_data=f"sr:edur:{sid}"),
            tbtypes.InlineKeyboardButton(
                "👥 Пул аккаунтов", callback_data=f"sr:eali:{sid}"),
        )
        kb.add(
            tbtypes.InlineKeyboardButton(
                "🎮 Игра", callback_data=f"sr:lgame:{sid}"),
            tbtypes.InlineKeyboardButton(
                "🔄 Лоты продления", callback_data=f"sr:lext:{sid}"),
        )
        kb.add(tbtypes.InlineKeyboardButton(
            "🎮 Extension: игры", callback_data=f"sr:lextg:{sid}"))
        kb.add(tbtypes.InlineKeyboardButton(
            f"{club_on} 🏠 PC-club (AI)",
            callback_data=f"sr:clubmode:{sid}"))
        kb.add(tbtypes.InlineKeyboardButton(
            f"{mr_on} 📷 Ручная фото-проверка",
            callback_data=f"sr:mrmode:{sid}"))
        # Per-lot post_delivery override
        _pd_off = ("post_delivery" in val
                   and (val.get("post_delivery") or "").strip() == "")
        _pd_has = bool(val.get("post_delivery"))
        if _pd_off:
            _pd_lbl = "📧 Доп. инфо: ⛔ выкл"
        elif _pd_has:
            _pd_lbl = "📧 Доп. инфо: ✏️ кастом"
        else:
            _pd_lbl = "📧 Доп. инфо"
        kb.add(tbtypes.InlineKeyboardButton(
            _pd_lbl, callback_data=f"sr:setpd_lot:{sid}"))
        kb.add(tbtypes.InlineKeyboardButton(
            "🗑 Удалить", callback_data=f"sr:dlot:{sid}"))
        # «◀️ Назад» — контекстная: если у лота есть game_key и игра
        # существует в games.json, возвращаемся на карточку этой игры,
        # иначе — на общий список лотов (legacy-меню).
        gkey_back = (val.get("game_key") or "").strip()
        if gkey_back and get_game(gkey_back):
            kb.add(tbtypes.InlineKeyboardButton(
                "◀️ К игре", callback_data=f"sr:game:{_sid(gkey_back)}"))
        else:
            kb.add(tbtypes.InlineKeyboardButton(
                "◀️ К списку", callback_data="sr:lots"))
        return kb

    def _text_lot(key: str) -> str:
        lots = list_lots()
        val = lots.get(key)
        if not val:
            return "Лот не найден."
        kind = "ID лота" if key.isdigit() else "ключевое слово"
        game = val.get("game", "—") or "—"
        ext_ids = val.get("extension_lot_ids") or []
        ext_games = val.get("extension_games") or []
        is_ext_lot = bool(val.get("is_extension"))
        free = _count_free_accounts(val.get("aliases", []))
        club_line = ""
        if val.get("club_mode"):
            cfg = get_config()
            mark = ("активен"
                    if cfg.get("club_mode_global_enabled")
                    else "режим лота включён, но <b>глобальный</b> "
                    "переключатель в настройках выключен")
            club_line = (f"🏠 <b>PC-club режим (AI)</b>: {mark}\n")
        mr_line = ""
        if val.get("manual_review"):
            mr_pending_n = sum(
                1 for r in _load_manual_review().get("requests", {}).values()
                if r.get("lot_key") == key
                and r.get("status") in ("awaiting_photo", "pending_review"))
            mr_line = (f"📷 <b>Ручная фото-проверка</b>: ВКЛ "
                       f"(заявок ждут: <b>{mr_pending_n}</b>)\n")
        ext_marker = ""
        if is_ext_lot:
            ext_marker = (
                "\n🔄 <b>Лот для продления</b>\n"
                "<i>На FunPay по умолчанию выключен. Активируется при "
                "<code>!продлить</code> от покупателя и выключается обратно "
                "после покупки.</i>\n"
            )

        # Статус на FunPay
        cache = _get_lot_active_cached(key)
        if cache is None:
            if is_ext_lot:
                status_line = "FunPay: ⛔ выключен (ожидание команды !продлить)"
            elif free > 0:
                status_line = ("FunPay: ❓ не синхронизировано "
                               "(нажми «🔁 Переактивировать лоты»)")
            else:
                status_line = "FunPay: ⛔ выключен (нет свободных аккаунтов)"
        elif cache.get("result") == "fail":
            ts_str = _fmt_ts(int(cache.get("ts", 0)))
            status_line = (f"FunPay: ⚠ ошибка синхронизации ({ts_str} МСК) "
                           "— смотри логи")
        else:
            ts_str = _fmt_ts(int(cache.get("ts", 0)))
            if cache.get("active"):
                status_line = (f"FunPay: ✅ включён "
                               f"<i>(синхр. {ts_str} МСК)</i>")
            else:
                status_line = (f"FunPay: ⛔ выключен "
                               f"<i>(синхр. {ts_str} МСК)</i>")

        return (
            f"<b>🎯 Лот: <code>{_esc(key)}</code></b>\n\n"
            f"Тип: {kind}\n"
            f"{status_line}\n"
            f"{ext_marker}"
            f"Игра: <b>{_esc(game)}</b>\n"
            f"Длительность: <b>{_human_minutes(val['duration_min'])}</b>\n"
            f"{club_line}"
            f"{mr_line}"
            f"Пул ({len(val.get('aliases', []))}): "
            f"<code>{_esc(', '.join(val.get('aliases', [])) or '—')}</code>\n"
            f"Свободно: <b>{free}</b>\n"
            f"🔄 Лоты продления: <code>{_esc(', '.join(ext_ids)) if ext_ids else 'не настроены'}</code>\n"
            f"🎮 Extension (игры): <code>"
            f"{_esc(', '.join(ext_games)) if ext_games else 'не настроены'}</code>"
        )

    def _kb_status() -> tbtypes.InlineKeyboardMarkup:
        kb = tbtypes.InlineKeyboardMarkup()
        accs = list_accounts()
        active = [a for a in accs if a.get("rental")]
        for a in active[:20]:  # лимит чтобы клавиатура не разорвалась
            alias = a["alias"]
            sid = _sid(alias)
            kb.add(tbtypes.InlineKeyboardButton(
                f"⚙ {alias}", callback_data=f"sr:op:{sid}"))
        kb.add(tbtypes.InlineKeyboardButton(
            "🔄 Обновить", callback_data="sr:status"))
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ Назад", callback_data="sr:main"))
        return kb

    def _text_status() -> str:
        accs = list_accounts()
        active = [a for a in accs if a.get("rental")]
        if not active:
            return "<b>📊 Активные аренды</b>\n\nНикто ничего не арендует."
        lines = ["<b>📊 Активные аренды</b>",
                 "<i>Жми на ⚙ под списком — открывается панель оператора "
                 "(➕продлить / 🛑прервать / 🔁сменить).</i>", ""]
        for a in active:
            r = a["rental"]
            remain = max(0, r["expires_at"] - _now())
            game = a.get("game") or _get_game_for_alias(a["alias"]) or ""
            game_str = f" • {_esc(game)}" if game else ""
            lines.append(
                f"<b>{_esc(a['alias'])}</b>{game_str} → "
                f"{_esc(r.get('buyer_username', '?'))} "
                f"(заказ #{_esc(r.get('order_id', '?'))})\n"
                f"  Осталось: {_human_minutes(remain // 60 + (1 if remain % 60 else 0))} "
                f"(до {_fmt_ts(r['expires_at'])} МСК)\n")
        return "\n".join(lines)

    def _kb_op_panel(alias: str) -> tbtypes.InlineKeyboardMarkup:
        sid = _sid(alias)
        kb = tbtypes.InlineKeyboardMarkup(row_width=3)
        kb.add(
            tbtypes.InlineKeyboardButton(
                "➕15м", callback_data=f"sr:ext:{sid}:15"),
            tbtypes.InlineKeyboardButton(
                "➕30м", callback_data=f"sr:ext:{sid}:30"),
            tbtypes.InlineKeyboardButton(
                "➕60м", callback_data=f"sr:ext:{sid}:60"),
        )
        kb.add(
            tbtypes.InlineKeyboardButton(
                "🛑 Прервать", callback_data=f"sr:stop:{sid}"),
            tbtypes.InlineKeyboardButton(
                "🔁 Сменить", callback_data=f"sr:switch:{sid}"),
        )
        kb.add(tbtypes.InlineKeyboardButton(
            "🚫 Buyer → blacklist", callback_data=f"sr:opbl:{sid}"))
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ К списку", callback_data="sr:status"))
        return kb

    def _text_op_panel(alias: str) -> str:
        acc = find_account(alias)
        if not acc or not acc.get("rental"):
            return f"<b>⚙ {_esc(alias)}</b>\n\nАренда уже не активна."
        r = acc["rental"]
        remain = max(0, int(r.get("expires_at", 0)) - _now())
        game = acc.get("game") or _get_game_for_alias(alias) or "—"
        stats = acc.get("stats") or {}
        total_rentals = int(stats.get("rentals_count", 0))
        total_revenue = float(stats.get("total_revenue", 0))
        total_reviews = int(stats.get("reviews_count", 0))
        cost = float(acc.get("cost", 0.0) or 0.0)
        profit = total_revenue - cost
        roi_line = ""
        if cost > 0:
            roi = (profit / cost) * 100
            roi_line = f"  ▸ ROI: <b>{roi:+.0f}%</b>\n"
        return (
            f"<b>⚙ Панель оператора — {_esc(alias)}</b>\n\n"
            f"🎮 Игра: <b>{_esc(game)}</b>\n"
            f"👤 Покупатель: <b>{_esc(r.get('buyer_username', '?'))}</b> "
            f"(id <code>{_esc(str(r.get('buyer_id', '?')))}</code>)\n"
            f"📦 Заказ: <code>#{_esc(str(r.get('order_id', '?')))}</code>\n"
            f"⏰ Осталось: <b>{_human_minutes(remain // 60 + (1 if remain % 60 else 0))}</b>\n"
            f"📅 До: <b>{_fmt_ts(int(r.get('expires_at', 0)))}</b> МСК\n\n"
            f"💵 <b>Финансы по аккаунту</b>\n"
            f"  ▸ Продаж: <b>{total_rentals}</b>\n"
            f"  ▸ Сумма: <b>{total_revenue:.0f}₽</b>\n"
            f"  ▸ Отзывов: <b>{total_reviews}</b>\n"
            f"  ▸ Расход: <b>{cost:.0f}₽</b>\n"
            f"  ▸ Прибыль: <b>{profit:+.0f}₽</b>\n"
            f"{roi_line}"
        )

    def _kb_blacklist() -> tbtypes.InlineKeyboardMarkup:
        kb = tbtypes.InlineKeyboardMarkup(row_width=1)
        cfg = get_config()
        on1 = "✅" if cfg.get("blacklist_enabled", True) else "❌"
        on2 = "✅" if cfg.get("auto_blacklist_on_refund", True) else "❌"
        kb.add(tbtypes.InlineKeyboardButton(
            f"{on1} Блокировка на NEW_ORDER",
            callback_data="sr:tgl:blacklist_enabled"))
        kb.add(tbtypes.InlineKeyboardButton(
            f"{on2} Авто-добавление при refund",
            callback_data="sr:tgl:auto_blacklist_on_refund"))
        for entry in list_blacklist()[:20]:
            label = (entry.get("username")
                     or f"id:{entry.get('buyer_id')}" or "?")
            kb.add(tbtypes.InlineKeyboardButton(
                f"❌ {label}",
                callback_data=f"sr:blrm:{_sid(str(label))}"))
        kb.add(tbtypes.InlineKeyboardButton(
            "➕ Добавить", callback_data="sr:bladd"))
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ Назад", callback_data="sr:settings"))
        return kb

    def _text_blacklist() -> str:
        items = list_blacklist()
        if not items:
            body = "Пусто. Покупатель попадёт сюда автоматически после refund " \
                   "(если включено) или вручную."
        else:
            lines = []
            for e in items[:50]:
                un = e.get("username") or "—"
                bid = e.get("buyer_id") or "—"
                reason = e.get("reason") or "—"
                ts = _fmt_ts(int(e.get("ts", 0)))
                lines.append(
                    f"• <code>{_esc(str(un))}</code> "
                    f"(id <code>{_esc(str(bid))}</code>) — "
                    f"<i>{_esc(reason)}</i> · {ts}")
            extra = (f"\n… и ещё {len(items) - 50}"
                     if len(items) > 50 else "")
            body = "\n".join(lines) + extra
        return f"<b>🚫 Blacklist покупателей</b>\n\n{body}"

    def _toggle_btn(label: str, key: str,
                    cfg: dict | None = None) -> tbtypes.InlineKeyboardButton:
        if cfg is None:
            cfg = get_config()
        on = "✅" if cfg.get(key) else "❌"
        return tbtypes.InlineKeyboardButton(
            f"{on} {label}", callback_data=f"sr:tgl:{key}")

    def _kb_settings() -> tbtypes.InlineKeyboardMarkup:
        cfg = get_config()
        kb = tbtypes.InlineKeyboardMarkup(row_width=1)
        # Подразделы
        kb.add(tbtypes.InlineKeyboardButton(
            "🔔 Уведомления", callback_data="sr:setnotify"))
        kb.add(tbtypes.InlineKeyboardButton(
            "⭐ Отзывы и бонусы", callback_data="sr:setreview"))
        kb.add(tbtypes.InlineKeyboardButton(
            "⏱ Лимиты аренды", callback_data="sr:setlimits"))
        kb.add(tbtypes.InlineKeyboardButton(
            "🔒 Безопасность", callback_data="sr:setsec"))
        kb.add(tbtypes.InlineKeyboardButton(
            "🏠 PC-клуб + AI", callback_data="sr:clbset"))
        kb.add(tbtypes.InlineKeyboardButton(
            "🚨 VAC / Trade ban scan", callback_data="sr:vacset"))
        bl_n = len(list_blacklist())
        kb.add(tbtypes.InlineKeyboardButton(
            f"🚫 Blacklist ({bl_n})", callback_data="sr:blist"))
        m_on = "✅" if cfg.get("metrics_enabled") else "❌"
        kb.add(tbtypes.InlineKeyboardButton(
            f"{m_on} Prometheus /metrics", callback_data="sr:metset"))
        s_on = "✅" if cfg.get("daily_summary_enabled", True) else "❌"
        kb.add(tbtypes.InlineKeyboardButton(
            f"{s_on} Daily summary", callback_data="sr:dsumset"))
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ Назад", callback_data="sr:main"))
        return kb

    # ── Подраздел: Уведомления ─────────────────────────────────────────
    def _kb_notify_set() -> tbtypes.InlineKeyboardMarkup:
        cfg = get_config()
        kb = tbtypes.InlineKeyboardMarkup(row_width=1)
        kb.add(_toggle_btn("Уведомления в Telegram", "tg_notify", cfg))
        kb.add(_toggle_btn("Сообщение после выдачи",
                           "post_delivery_message_enabled", cfg))
        kb.add(tbtypes.InlineKeyboardButton(
            f"⏱ Задержка после выдачи: "
            f"{cfg.get('post_delivery_delay_seconds', 3)} сек",
            callback_data="sr:edset:post_delivery_delay_seconds"))
        rem1 = int(cfg.get('reminder_minutes', 30) or 0)
        rem1_lbl = f"{rem1} мин" if rem1 > 0 else "выкл"
        kb.add(tbtypes.InlineKeyboardButton(
            f"⏰ 1-е напоминание: {rem1_lbl}",
            callback_data="sr:edset:reminder_minutes"))
        rem2 = int(cfg.get('reminder_minutes_2', 10) or 0)
        rem2_lbl = f"{rem2} мин" if rem2 > 0 else "выкл"
        kb.add(tbtypes.InlineKeyboardButton(
            f"⏰ 2-е напоминание: {rem2_lbl}",
            callback_data="sr:edset:reminder_minutes_2"))
        kb.add(tbtypes.InlineKeyboardButton(
            "✏️ Команда !код",
            callback_data="sr:edset:guardik_command"))
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ Назад", callback_data="sr:settings"))
        return kb

    def _text_notify_set() -> str:
        cfg = get_config()
        tg_on = '✅' if cfg.get('tg_notify') else '❌'
        post_on = '✅' if cfg.get('post_delivery_message_enabled', False) else '❌'
        rem1 = int(cfg.get('reminder_minutes', 30) or 0)
        rem2 = int(cfg.get('reminder_minutes_2', 10) or 0)
        rem1_lbl = f"{rem1} мин" if rem1 > 0 else "выкл"
        rem2_lbl = f"{rem2} мин" if rem2 > 0 else "выкл"
        return (
            "<b>🔔 Уведомления</b>\n\n"
            f"Уведомления в Telegram: {tg_on}\n"
            f"Сообщение после выдачи: {post_on}\n"
            f"Задержка после выдачи: <b>{cfg.get('post_delivery_delay_seconds', 3)}</b> сек\n"
            f"1-е напоминание: <b>{rem1_lbl}</b> до окончания\n"
            f"2-е напоминание: <b>{rem2_lbl}</b> до окончания\n"
            f"Команда: <code>{_esc(cfg.get('guardik_command', '!код'))}</code>"
        )

    # ── Подраздел: Отзывы и бонусы ─────────────────────────────────────
    def _kb_review_set() -> tbtypes.InlineKeyboardMarkup:
        cfg = get_config()
        kb = tbtypes.InlineKeyboardMarkup(row_width=1)
        kb.add(_toggle_btn("Бонус за 5★ отзыв",
                           "review_bonus_enabled", cfg))
        kb.add(tbtypes.InlineKeyboardButton(
            f"⭐ Бонус за отзыв: {cfg.get('review_bonus_hours', 1)} ч",
            callback_data="sr:edset:review_bonus_hours"))
        kb.add(_toggle_btn("Штраф за удаление отзыва",
                           "review_delete_penalty_enabled", cfg))
        kb.add(tbtypes.InlineKeyboardButton(
            f"🚨 Штраф: {cfg.get('review_delete_penalty_hours', 1)} ч",
            callback_data="sr:edset:review_delete_penalty_hours"))
        kb.add(_toggle_btn("ЧС при удалении отзыва",
                           "review_delete_blacklist", cfg))
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ Назад", callback_data="sr:settings"))
        return kb

    def _text_review_set() -> str:
        cfg = get_config()
        bonus_on = '✅' if cfg.get('review_bonus_enabled', True) else '❌'
        penalty_on = '✅' if cfg.get('review_delete_penalty_enabled', True) else '❌'
        bl_on = '✅' if cfg.get('review_delete_blacklist', True) else '❌'
        return (
            "<b>⭐ Отзывы и бонусы</b>\n\n"
            f"Бонус за 5★ отзыв: {bonus_on} "
            f"(<b>{cfg.get('review_bonus_hours', 1)}</b> ч)\n"
            f"Штраф за удаление отзыва: {penalty_on} "
            f"(<b>{cfg.get('review_delete_penalty_hours', 1)}</b> ч)\n"
            f"ЧС при удалении отзыва: {bl_on}"
        )

    # ── Подраздел: Лимиты аренды ───────────────────────────────────────
    def _kb_limits_set() -> tbtypes.InlineKeyboardMarkup:
        cfg = get_config()
        kb = tbtypes.InlineKeyboardMarkup(row_width=1)
        kb.add(tbtypes.InlineKeyboardButton(
            f"⏱ Мин. аренда: {cfg.get('min_rental_hours', 1)} ч",
            callback_data="sr:edset:min_rental_hours"))
        kb.add(tbtypes.InlineKeyboardButton(
            f"⏱ Макс. аренда: {cfg.get('max_rental_hours', 1668)} ч",
            callback_data="sr:edset:max_rental_hours"))
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ Назад", callback_data="sr:settings"))
        return kb

    def _text_limits_set() -> str:
        cfg = get_config()
        return (
            "<b>⏱ Лимиты аренды</b>\n\n"
            f"Мин. аренда: <b>{cfg.get('min_rental_hours', 1)}</b> ч\n"
            f"Макс. аренда: <b>{cfg.get('max_rental_hours', 1668)}</b> ч"
        )

    # ── Подраздел: Безопасность ───────────────────────────────────────
    def _kb_sec_set() -> tbtypes.InlineKeyboardMarkup:
        cfg = get_config()
        kb = tbtypes.InlineKeyboardMarkup(row_width=1)
        kb.add(_toggle_btn("Автовыдача аккаунта", "auto_deliver", cfg))
        kb.add(_toggle_btn("Менять пароль по истечении",
                           "change_password_on_expire", cfg))
        kb.add(_toggle_btn("Отзывать сессии по истечении",
                           "revoke_sessions_on_expire", cfg))
        kb.add(_toggle_btn("Проверка при запуске",
                           "check_accounts_on_start", cfg))
        kb.add(_toggle_btn("Авто-деактивация лотов",
                           "auto_deactivate_lots", cfg))
        kb.add(_toggle_btn("Авто-продление (extension)",
                           "auto_extend_enabled", cfg))
        kb.add(_toggle_btn("Fallback продления по тексту заказа",
                           "extension_buyer_fallback_enabled", cfg))
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ Назад", callback_data="sr:settings"))
        return kb

    def _text_sec_set() -> str:
        cfg = get_config()
        def _b(k, d=True):
            return '✅' if cfg.get(k, d) else '❌'
        return (
            "<b>🔒 Безопасность</b>\n\n"
            f"Автовыдача аккаунта: {_b('auto_deliver')}\n"
            f"Менять пароль по истечении: {_b('change_password_on_expire')}\n"
            f"Отзывать сессии по истечении: {_b('revoke_sessions_on_expire')}\n"
            f"Проверка при запуске: {_b('check_accounts_on_start')}\n"
            f"Авто-деактивация лотов: {_b('auto_deactivate_lots')}\n"
            f"Авто-продление (extension): {_b('auto_extend_enabled')}\n"
            f"Fallback продления по тексту заказа: "
            f"{_b('extension_buyer_fallback_enabled', False)}\n"
            "\n<i>Авто-поднятие лотов FunPay делает Cardinal сам "
            "(autoRaise в _main.cfg). Плагин в это не вмешивается. "
            "«Подождите N часов» в логах — естественный rate-limit "
            "FunPay, а не ошибка.)</i>\n\n"
            "<i>Fallback продления — на случай, если FunPay отдал "
            "заказ без lot_id и плагин не смог распознать "
            "extension-лот. По ключевым словам "
            "(«ПРОДЛЕНИЕ»/«extend») и активной аренде покупателя "
            "плагин подберёт extension-лот сам. Защита: чужая игра в "
            "названии заказа = пропуск.</i>"
        )

    # Реестр view'ов для роутинга после тогла
    _TGL_RETURN_VIEW: dict[str, str] = {
        # PC-club + AI
        "club_mode_global_enabled": "clbset",
        "ai_fake_detector_enabled": "clbset",
        "ai_fake_detector_in_manual_review": "clbset",
        # Уведомления
        "tg_notify": "setnotify",
        "post_delivery_message_enabled": "setnotify",
        # Отзывы и бонусы
        "review_bonus_enabled": "setreview",
        "review_delete_penalty_enabled": "setreview",
        "review_delete_blacklist": "setreview",
        # Безопасность
        "auto_deliver": "setsec",
        "change_password_on_expire": "setsec",
        "revoke_sessions_on_expire": "setsec",
        "check_accounts_on_start": "setsec",
        "auto_deactivate_lots": "setsec",
        "auto_extend_enabled": "setsec",
        "extension_buyer_fallback_enabled": "setsec",
        # Метрики и сводка — оставались в подразделах метрик
        "metrics_enabled": "metset",
        "daily_summary_enabled": "dsumset",
    }

    def _kb_metset() -> tbtypes.InlineKeyboardMarkup:
        cfg = get_config()
        kb = tbtypes.InlineKeyboardMarkup(row_width=1)
        on = "✅" if cfg.get("metrics_enabled") else "❌"
        kb.add(tbtypes.InlineKeyboardButton(
            f"{on} Включить /metrics",
            callback_data="sr:tgl:metrics_enabled"))
        kb.add(tbtypes.InlineKeyboardButton(
            f"🔌 Порт: {cfg.get('metrics_port', 9101)}",
            callback_data="sr:edset:metrics_port"))
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ Назад", callback_data="sr:settings"))
        return kb

    def _text_metset() -> str:
        cfg = get_config()
        running = "✅" if (_metrics_http_thread is not None
                          and _metrics_http_thread.is_alive()) else "❌"
        return (
            "<b>📈 Prometheus /metrics</b>\n\n"
            f"Состояние сервера: {running}\n"
            f"Порт: <b>{cfg.get('metrics_port', 9101)}</b>\n"
            f"Bind: <code>{_esc(cfg.get('metrics_bind') or '0.0.0.0')}</code>\n\n"
            "Включи — после рестарта FPC HTTP-сервер откроется на "
            "<code>http://&lt;host&gt;:&lt;port&gt;/metrics</code>.\n"
            "Метрики: <code>steam_rental_asr_*</code>."
        )

    def _kb_dsumset() -> tbtypes.InlineKeyboardMarkup:
        cfg = get_config()
        kb = tbtypes.InlineKeyboardMarkup(row_width=1)
        on = "✅" if cfg.get("daily_summary_enabled", True) else "❌"
        kb.add(tbtypes.InlineKeyboardButton(
            f"{on} Включить ежедневную сводку",
            callback_data="sr:tgl:daily_summary_enabled"))
        kb.add(tbtypes.InlineKeyboardButton(
            f"🕐 Час МСК: {cfg.get('daily_summary_hour_msk', 0)}",
            callback_data="sr:edset:daily_summary_hour_msk"))
        kb.add(tbtypes.InlineKeyboardButton(
            "📤 Прислать сейчас", callback_data="sr:dsumnow"))
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ Назад", callback_data="sr:settings"))
        return kb

    def _text_dsumset() -> str:
        cfg = get_config()
        return (
            "<b>📊 Daily summary</b>\n\n"
            "Раз в сутки бот шлёт сводку: аренд / завершено / продлено / "
            "выручка / состояние пула.\n\n"
            f"Включено: "
            f"<b>{'да' if cfg.get('daily_summary_enabled', True) else 'нет'}</b>\n"
            f"Час отправки: "
            f"<b>{cfg.get('daily_summary_hour_msk', 0)}:00 МСК</b> "
            "(0 МСК = полночь по Москве)."
        )

    def _text_settings() -> str:
        cfg = get_config()
        tg_on = '✅' if cfg.get('tg_notify') else '❌'
        post_on = '✅' if cfg.get('post_delivery_message_enabled', False) else '❌'
        rem1 = int(cfg.get('reminder_minutes', 30) or 0)
        rem2 = int(cfg.get('reminder_minutes_2', 10) or 0)
        rem_cnt = (1 if rem1 > 0 else 0) + (1 if rem2 > 0 else 0)
        bonus_on = '✅' if cfg.get('review_bonus_enabled', True) else '❌'
        penalty_on = '✅' if cfg.get('review_delete_penalty_enabled', True) else '❌'
        sec_on_cnt = sum(1 for k in (
            "auto_deliver",
            "change_password_on_expire",
            "revoke_sessions_on_expire",
            "check_accounts_on_start",
            "auto_deactivate_lots",
            "auto_extend_enabled",
        ) if cfg.get(k, True))
        # extension_buyer_fallback_enabled по умолчанию False — отдельным
        # слагаемым, чтобы дефолт-значения остальных не «затирались».
        if cfg.get("extension_buyer_fallback_enabled", False):
            sec_on_cnt += 1
        return (
            "<b>⚙ Настройки</b>\n\n"
            f"🔔 <b>Уведомления:</b> {tg_on} TG, выдача {post_on}, "
            f"напомин. {rem_cnt}/2\n"
            f"⭐ <b>Отзывы:</b> бонус {bonus_on} "
            f"({cfg.get('review_bonus_hours', 1)} ч), штраф {penalty_on}\n"
            f"⏱ <b>Лимиты:</b> {cfg.get('min_rental_hours', 1)}..."
            f"{cfg.get('max_rental_hours', 1668)} ч\n"
            f"🔒 <b>Безопасность:</b> {sec_on_cnt}/7 включено\n"
            f"Команда: <code>{_esc(cfg.get('guardik_command', '!код'))}</code>\n\n"
            "Выбери раздел для настройки:"
        )

    # ── VAC scan settings ───────────────────────────────────────────────
    def _kb_vacset() -> tbtypes.InlineKeyboardMarkup:
        cfg = get_config()
        kb = tbtypes.InlineKeyboardMarkup(row_width=1)
        on = "✅" if cfg.get("vac_scan_enabled") else "❌"
        kb.add(tbtypes.InlineKeyboardButton(
            f"{on} Включить фоновый VAC/ban scan",
            callback_data="sr:tgl:vac_scan_enabled"))
        key = (cfg.get("steam_api_key") or "").strip()
        key_disp = ("✅ установлен (" + key[:6] + "…)" if key
                    else "❌ не установлен")
        kb.add(tbtypes.InlineKeyboardButton(
            f"🔑 Steam Web API key: {key_disp}",
            callback_data="sr:edset:steam_api_key"))
        kb.add(tbtypes.InlineKeyboardButton(
            f"⏱ Интервал: {cfg.get('vac_scan_interval_min', 60)} мин",
            callback_data="sr:edset:vac_scan_interval_min"))
        kb.add(tbtypes.InlineKeyboardButton(
            "🚀 Запустить сейчас", callback_data="sr:vacrun"))
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ Назад", callback_data="sr:settings"))
        return kb

    def _text_vacset() -> str:
        cfg = get_config()
        key_set = bool((cfg.get("steam_api_key") or "").strip())
        en = bool(cfg.get("vac_scan_enabled"))
        last = _last_vac_scan_ts
        last_str = (_fmt_ts(last) + " МСК") if last else "ещё не запускался"
        return (
            "<b>🚨 VAC / Trade ban scan</b>\n\n"
            f"Статус: {'<b>включён</b>' if en else 'выключен'}\n"
            f"API key: {'есть' if key_set else '<b>нет</b>'}\n"
            f"Интервал: <b>{cfg.get('vac_scan_interval_min', 60)}</b> мин\n"
            f"Последний прогон: <code>{_esc(last_str)}</code>\n\n"
            "Раз в N минут сканирует SteamID активных арендованных аккаунтов "
            "через <code>ISteamUser/GetPlayerBans</code>. При обнаружении "
            "VAC/Game/Trade/Community-бана: авто-завершение аренды + заморозка "
            "аккаунта + TG-алёрт.\n\n"
            "Ключ бесплатный, получить: "
            "https://steamcommunity.com/dev/apikey"
        )

    # ── PC-club / AI settings ───────────────────────────────────────────
    def _kb_clbset() -> tbtypes.InlineKeyboardMarkup:
        cfg = get_config()
        kb = tbtypes.InlineKeyboardMarkup(row_width=1)
        gl_on = "✅" if cfg.get("club_mode_global_enabled") else "❌"
        kb.add(tbtypes.InlineKeyboardButton(
            f"{gl_on} Глобально включить PC-club режим",
            callback_data="sr:tgl:club_mode_global_enabled"))

        prov = cfg.get("ai_provider", "openrouter")
        kb.add(tbtypes.InlineKeyboardButton(
            f"🤖 Провайдер: {_AI_PROVIDER_LABELS.get(prov, prov)}",
            callback_data="sr:aiprov"))

        k_api, k_model = _ai_provider_keys(prov)
        key = (cfg.get(k_api) or "").strip()
        key_disp = ("✅ установлен" if key else "❌ не установлен")
        kb.add(tbtypes.InlineKeyboardButton(
            f"🔑 API key ({_AI_PROVIDER_LABELS.get(prov, prov)}): {key_disp}",
            callback_data=f"sr:edset:{k_api}"))
        kb.add(tbtypes.InlineKeyboardButton(
            f"🧠 Модель: {(cfg.get(k_model) or '—')[:30]}",
            callback_data="sr:aimodel"))
        kb.add(tbtypes.InlineKeyboardButton(
            "🧪 Проверить ключ", callback_data="sr:aitest"))

        kb.add(tbtypes.InlineKeyboardButton(
            f"📛 FunPay-ник: "
            f"{_esc(cfg.get('seller_funpay_nickname') or '—')[:20]}",
            callback_data="sr:edset:seller_funpay_nickname"))
        kb.add(tbtypes.InlineKeyboardButton(
            f"✅ Auto-approve ≥ {cfg.get('club_auto_approve_threshold', 80)}%",
            callback_data="sr:edset:club_auto_approve_threshold"))
        kb.add(tbtypes.InlineKeyboardButton(
            f"❌ Auto-decline ≤ {cfg.get('club_auto_decline_threshold', 30)}%",
            callback_data="sr:edset:club_auto_decline_threshold"))
        kb.add(tbtypes.InlineKeyboardButton(
            f"⏳ TTL заявки: {cfg.get('club_request_ttl_hours', 24)} ч",
            callback_data="sr:edset:club_request_ttl_hours"))
        kb.add(tbtypes.InlineKeyboardButton(
            f"💬 Команда вызова: "
            f"{cfg.get('pcclub_command') or '/pcclub'}",
            callback_data="sr:edset:pcclub_command"))
        # ── Fake-детектор (anti-AI-photo) ────────────────────────────────
        fake_on = "✅" if cfg.get("ai_fake_detector_enabled", True) else "❌"
        kb.add(tbtypes.InlineKeyboardButton(
            f"{fake_on} 🔬 Анти-AI-фото детектор",
            callback_data="sr:tgl:ai_fake_detector_enabled"))
        fake_mr = "✅" if cfg.get(
            "ai_fake_detector_in_manual_review", True) else "❌"
        kb.add(tbtypes.InlineKeyboardButton(
            f"{fake_mr} применять и в manual_review",
            callback_data="sr:tgl:ai_fake_detector_in_manual_review"))
        kb.add(tbtypes.InlineKeyboardButton(
            f"🚨 Авто-отказ ≥ "
            f"{cfg.get('ai_fake_decline_threshold', 70)}%",
            callback_data="sr:edset:ai_fake_decline_threshold"))
        kb.add(tbtypes.InlineKeyboardButton(
            f"⚠️ В ручную ≥ "
            f"{cfg.get('ai_fake_manual_threshold', 40)}%",
            callback_data="sr:edset:ai_fake_manual_threshold"))
        kb.add(tbtypes.InlineKeyboardButton(
            "🏠 Очередь / whitelist", callback_data="sr:clbs"))
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ Назад", callback_data="sr:settings"))
        return kb

    def _text_clbset() -> str:
        cfg = get_config()
        prov = cfg.get("ai_provider", "openrouter")
        k_api, k_model = _ai_provider_keys(prov)
        st = _load_clubs().get("stats", {})
        pending = sum(1 for r in _load_clubs().get("requests", {}).values()
                      if r.get("status") in ("awaiting_photo", "verifying", "manual"))
        wl = len(_load_clubs().get("whitelist", {}))
        return (
            "<b>🏠 PC-club + AI верификация</b>\n\n"
            f"Глобально: <b>"
            f"{'ВКЛ' if cfg.get('club_mode_global_enabled') else 'ВЫКЛ'}</b>\n"
            f"Провайдер: <b>{_AI_PROVIDER_LABELS.get(prov, prov)}</b>\n"
            f"Модель: <code>{_esc(cfg.get(k_model) or '—')}</code>\n"
            f"API key: "
            f"{'есть' if (cfg.get(k_api) or '').strip() else '<b>нет</b>'}\n"
            f"Никнейм FunPay: "
            f"<code>{_esc(cfg.get('seller_funpay_nickname') or '—')}</code>\n\n"
            f"Заявок в очереди: <b>{pending}</b>\n"
            f"В whitelist клубов: <b>{wl}</b>\n"
            f"AI-вызовов: <b>{st.get('ai_calls', 0)}</b> "
            f"(auto-ok: {st.get('ai_approves', 0)}, "
            f"auto-decl: {st.get('ai_declines', 0)}, "
            f"manual-ok: {st.get('manual_approves', 0)}, "
            f"manual-decl: {st.get('manual_declines', 0)})\n\n"
            f"🔬 Анти-AI-фото: <b>"
            f"{'ВКЛ' if cfg.get('ai_fake_detector_enabled', True) else 'ВЫКЛ'}"
            f"</b> (decline ≥ "
            f"{cfg.get('ai_fake_decline_threshold', 70)}%, "
            f"manual ≥ {cfg.get('ai_fake_manual_threshold', 40)}%)\n"
            f"   Manual-review: <b>"
            f"{'ВКЛ' if cfg.get('ai_fake_detector_in_manual_review', True) else 'ВЫКЛ'}"
            f"</b>\n\n"
            f"Не забудь у конкретного лота включить «🏠 PC-club режим» — "
            f"глобальный тогл выше — это общий рубильник."
        )

    def _kb_aiprov() -> tbtypes.InlineKeyboardMarkup:
        cfg = get_config()
        cur = cfg.get("ai_provider", "openrouter")
        kb = tbtypes.InlineKeyboardMarkup(row_width=1)
        for p in _AI_PROVIDERS:
            mark = "✅ " if p == cur else ""
            kb.add(tbtypes.InlineKeyboardButton(
                mark + _AI_PROVIDER_LABELS.get(p, p),
                callback_data=f"sr:aiprovset:{p}"))
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ Назад", callback_data="sr:clbset"))
        return kb

    def _text_aiprov() -> str:
        cfg = get_config()
        cur = cfg.get("ai_provider", "openrouter")
        return (
            "<b>🤖 Выбор AI-провайдера</b>\n\n"
            f"Сейчас: <b>{_AI_PROVIDER_LABELS.get(cur, cur)}</b>\n\n"
            "У каждого провайдера свой API key и своя модель — "
            "хранятся отдельно, переключайся без перевводов."
        )

    def _kb_aimodel() -> tbtypes.InlineKeyboardMarkup:
        cfg = get_config()
        prov = cfg.get("ai_provider", "openrouter")
        k_api, k_model = _ai_provider_keys(prov)
        cur = cfg.get(k_model) or ""
        kb = tbtypes.InlineKeyboardMarkup(row_width=1)
        for idx, preset in enumerate(_AI_PROVIDER_PRESETS.get(prov, [])):
            mark = "✅ " if preset == cur else ""
            kb.add(tbtypes.InlineKeyboardButton(
                mark + preset[:55],
                callback_data=f"sr:aimodelset:{idx}"))
        kb.add(tbtypes.InlineKeyboardButton(
            "✏️ Ввести модель вручную",
            callback_data=f"sr:edset:{k_model}"))
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ Назад", callback_data="sr:clbset"))
        return kb

    def _text_aimodel() -> str:
        cfg = get_config()
        prov = cfg.get("ai_provider", "openrouter")
        _, k_model = _ai_provider_keys(prov)
        return (
            "<b>🧠 Модель AI</b>\n\n"
            f"Провайдер: <b>{_AI_PROVIDER_LABELS.get(prov, prov)}</b>\n"
            f"Сейчас: <code>{_esc(cfg.get(k_model) or '—')}</code>\n\n"
            "Выбери пресет или введи вручную. Должна поддерживать vision "
            "(анализ изображений), иначе AI не сможет проверять фото."
        )

    # ── PC-club: очередь + whitelist ────────────────────────────────────
    def _kb_clbs() -> tbtypes.InlineKeyboardMarkup:
        kb = tbtypes.InlineKeyboardMarkup(row_width=1)
        pending = _club_pending_requests()
        for r in pending[:10]:
            label = (f"{_status_emoji(r.get('status'))} "
                     f"#{r.get('order_id')} "
                     f"• {(r.get('buyer_username') or '?')[:12]}")
            kb.add(tbtypes.InlineKeyboardButton(
                label, callback_data=f"sr:clbreq:{r.get('order_id')}"))
        kb.add(tbtypes.InlineKeyboardButton(
            f"👥 Whitelist ({len(_load_clubs().get('whitelist', {}))})",
            callback_data="sr:clbwl"))
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ Назад", callback_data="sr:clbset"))
        return kb

    def _text_clbs() -> str:
        pending = _club_pending_requests()
        if not pending:
            return ("<b>🏠 PC-club — очередь</b>\n\n"
                    "Пока пусто. Заявки появятся, как только покупатели "
                    "начнут оплачивать PC-club тариф.")
        lines = ["<b>🏠 PC-club — очередь</b>", ""]
        for r in pending[:20]:
            verdict = r.get("ai_verdict") or {}
            conf = verdict.get("confidence") if verdict.get("ok") else None
            v_str = (f"AI {conf}%" if conf is not None
                     else f"AI ⚠ {verdict.get('error', '')[:50]}"
                     if verdict and verdict.get("error")
                     else "ждём фото" if r.get("status") == "awaiting_photo"
                     else "—")
            lines.append(
                f"{_status_emoji(r.get('status'))} <code>#{r.get('order_id')}"
                f"</code> • {_esc(str(r.get('buyer_username')))[:20]} • "
                f"{v_str}")
        return "\n".join(lines)

    def _status_emoji(s: str | None) -> str:
        return {
            "awaiting_photo": "📷",
            "verifying": "🔄",
            "manual": "❓",
            "approved": "✅",
            "declined": "❌",
            "expired": "⏰",
        }.get(s or "", "•")

    def _kb_clbreq(order_id: str) -> tbtypes.InlineKeyboardMarkup:
        kb = tbtypes.InlineKeyboardMarkup(row_width=2)
        req = _club_get_request(order_id) or {}
        if req.get("status") in ("awaiting_photo", "verifying", "manual"):
            kb.add(
                tbtypes.InlineKeyboardButton(
                    "✅ Одобрить", callback_data=f"sr:clbapr:{order_id}"),
                tbtypes.InlineKeyboardButton(
                    "❌ Отказать", callback_data=f"sr:clbdec:{order_id}"),
            )
            kb.add(tbtypes.InlineKeyboardButton(
                "🔁 Запросить ещё фото",
                callback_data=f"sr:clbret:{order_id}"))
        if req.get("photo_url"):
            kb.add(tbtypes.InlineKeyboardButton(
                "🖼 Фото", url=req["photo_url"]))
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ К списку", callback_data="sr:clbs"))
        return kb

    def _text_clbreq(order_id: str) -> str:
        req = _club_get_request(order_id)
        if not req:
            return "Заявка не найдена."
        v = req.get("ai_verdict") or {}
        lines = [
            f"<b>🏠 Заявка #{_esc(order_id)}</b>", "",
            f"Покупатель: <b>{_esc(str(req.get('buyer_username')))}</b> "
            f"(id <code>{req.get('buyer_id')}</code>)",
            f"Лот: <code>{_esc(str(req.get('lot_key')))}</code>",
            f"Длительность: <b>"
            f"{_human_minutes(int(req.get('duration_min') or 0))}</b>",
            f"Код: <code>{_esc(str(req.get('code')))}</code>",
            f"Статус: <b>{_esc(str(req.get('status')))}</b>",
            f"Создана: <code>{_fmt_ts(req.get('created_ts', 0))}</code>",
        ]
        if v:
            if v.get("ok"):
                lines.append(
                    f"\nAI ({_esc(v.get('provider', '?'))}/"
                    f"{_esc(v.get('model', '?'))}): "
                    f"confidence <b>{v.get('confidence')}%</b>")
                lines.append(
                    f"  club={v.get('is_pc_club')}, "
                    f"chat={v.get('funpay_chat_visible')}, "
                    f"nick={v.get('seller_nickname_visible')}, "
                    f"code={v.get('code_visible')}")
                if v.get("reasoning"):
                    lines.append(f"  <i>{_esc(v['reasoning'][:300])}</i>")
            else:
                lines.append(f"\nAI error: <i>"
                             f"{_esc(v.get('error', ''))[:300]}</i>")
        if req.get("alias_issued"):
            lines.append(f"\nВыдан акк: <code>"
                         f"{_esc(req['alias_issued'])}</code>")
        return "\n".join(lines)

    def _kb_clbwl(page: int = 0) -> tbtypes.InlineKeyboardMarkup:
        kb = tbtypes.InlineKeyboardMarkup(row_width=1)
        wl = _club_list_whitelist()
        per = 10
        start = page * per
        for w in wl[start:start + per]:
            label = (f"{(w.get('username') or w.get('funpay_user_id'))[:25]} "
                     f"• {w.get('attempts', 1)}×")
            kb.add(tbtypes.InlineKeyboardButton(
                label,
                callback_data=f"sr:clbwldel:{w.get('funpay_user_id')}"))
        nav = []
        if start > 0:
            nav.append(tbtypes.InlineKeyboardButton(
                "◀️", callback_data=f"sr:clbwlp:{page - 1}"))
        if start + per < len(wl):
            nav.append(tbtypes.InlineKeyboardButton(
                "▶️", callback_data=f"sr:clbwlp:{page + 1}"))
        if nav:
            kb.row(*nav)
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ Назад", callback_data="sr:clbs"))
        return kb

    def _text_clbwl() -> str:
        wl = _club_list_whitelist()
        if not wl:
            return ("<b>👥 PC-club whitelist</b>\n\n"
                    "Пока пусто. Покупатели попадают сюда после "
                    "успешной верификации (AI или ручной).\n\n"
                    "Тап на запись = удалить из whitelist.")
        return (f"<b>👥 PC-club whitelist ({len(wl)})</b>\n\n"
                "Тап на запись = удалить из whitelist.")

    # ───── Шаблоны ──────────────────────────────────────────────────────
    # v2.22: язык, выбранный администратором для редактирования (RU/EN).
    # Хранится in-memory per admin uid; меняется кнопкой 🇷🇺/🇬🇧.
    _admin_tpl_lang: dict[int, str] = {}

    def _get_admin_tpl_lang(uid: int) -> str:
        return _admin_tpl_lang.get(uid, "ru")

    def _kb_templates(uid: int = 0) -> tbtypes.InlineKeyboardMarkup:
        kb = tbtypes.InlineKeyboardMarkup(row_width=2)
        cur_lang = _get_admin_tpl_lang(uid)
        # Переключатель языка
        kb.add(
            tbtypes.InlineKeyboardButton(
                ("🇷🇺 RU ✓" if cur_lang == "ru" else "🇷🇺 RU"),
                callback_data="sr:tplang:ru"),
            tbtypes.InlineKeyboardButton(
                ("🇬🇧 EN ✓" if cur_lang == "en" else "🇬🇧 EN"),
                callback_data="sr:tplang:en"),
        )
        template_names = [
            ("issue", "📦 Выдача"),
            ("post_delivery", "📨 После выдачи"),
            ("extend", "🔗 Продление"),
            ("extended", "✅ Продлено"),
            ("reminder", "⏰ Напоминание"),
            ("expired", "⏳ Истечение"),
            ("guard_code", "🛡 Guard код"),
            ("guard_error", "⚠️ Guard ошибка"),
            ("guard_error_no_secret", "⚠️ Нет Guard"),
            ("no_accounts", "🚫 Нет аккаунтов"),
            ("help", "❓ Помощь"),
            ("status", "📊 Статус"),
            ("welcome", "👋 Приветствие"),
            ("review_reward", "⭐ Бонус отзыв"),
            ("order_received", "🛒 Заказ получен"),
        ]
        for tpl_key, tpl_label in template_names:
            kb.add(tbtypes.InlineKeyboardButton(
                tpl_label, callback_data=f"sr:edtpl:{tpl_key}"))
        kb.add(tbtypes.InlineKeyboardButton(
            "🔄 Сбросить все", callback_data="sr:rstpl"))
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ Назад", callback_data="sr:main"))
        return kb

    def _text_templates(uid: int = 0) -> str:
        cur_lang = _get_admin_tpl_lang(uid)
        lang_label = "🇷🇺 русский" if cur_lang == "ru" else "🇬🇧 English"
        return (
            "<b>📝 Шаблоны сообщений</b>\n\n"
            f"Текущий язык: <b>{lang_label}</b>\n"
            "Выбери шаблон для редактирования. Кнопка 🇷🇺/🇬🇧 "
            "переключает язык, который сейчас редактируется.\n\n"
            "<b>Плейсхолдеры:</b>\n"
            "<code>{login}</code> — логин аккаунта\n"
            "<code>{password}</code> — пароль\n"
            "<code>{game}</code> — игра\n"
            "<code>{duration}</code> — длительность (текст)\n"
            "<code>{hours}</code> — часы\n"
            "<code>{minutes}</code> — минуты\n"
            "<code>{new_expires}</code> — новый срок\n"
            "<code>{code}</code> — Steam Guard код\n"
            "<code>{link}</code> — ссылка на продление\n\n"
            "<i>Покупатели на английском получают шаблоны из набора 🇬🇧, "
            "переключают командой <code>!engrent</code> в чате FunPay.</i>"
        )

    # ───── Статистика ──────────────────────────────────────────────────
    def _kb_stats() -> tbtypes.InlineKeyboardMarkup:
        kb = tbtypes.InlineKeyboardMarkup(row_width=1)
        kb.add(tbtypes.InlineKeyboardButton(
            "📊 Статистика по аккаунтам",
            callback_data="sr:accstatslist:0"))
        kb.add(tbtypes.InlineKeyboardButton(
            "🔄 Обновить", callback_data="sr:stats"))
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ Назад", callback_data="sr:main"))
        return kb

    def _text_stats() -> str:
        stats = _calc_stats()
        lines = [
            "<b>📊 Статистика аренд</b>",
            "",
            f"<b>Аренды:</b>",
            f"  За день: <b>{stats['day']}</b>",
            f"  За неделю: <b>{stats['week']}</b>",
            f"  За месяц: <b>{stats['month']}</b>",
            f"  Всего: <b>{stats['total']}</b>",
            "",
            f"<b>Средняя длительность:</b> "
            f"{_human_minutes(int(stats['avg_duration']))}",
            f"<b>Продлений:</b> {stats['extensions']}",
            f"<b>Бонусов за отзыв:</b> {stats['review_bonuses']}",
            "",
            "<b>Аккаунты:</b>",
            f"  Всего: <b>{stats['acc_total']}</b>",
            f"  🟢 Свободных: <b>{stats['acc_free']}</b>",
            f"  🔴 В аренде: <b>{stats['acc_rented']}</b>",
            f"  ❄️ Заморожено: <b>{stats['acc_frozen']}</b>",
            f"  ⚠️ С ошибками: <b>{stats['acc_problem']}</b>",
            f"  📊 Утилизация: "
            f"<b>{stats['utilization_pct']:.0f}%</b>",
        ]
        if stats["login_failures"] or stats["chpwd_failures"]:
            lines += [
                "",
                "<b>Ошибки (накоплено):</b>",
                f"  Неудачных логинов: {stats['login_failures']}",
                f"  Ошибок смены пароля: {stats['chpwd_failures']}",
            ]
        if stats["total_revenue"]:
            lines += [
                "",
                "<b>Доход:</b>",
                f"  За день: {stats['rev_day']:.0f}",
                f"  За неделю: {stats['rev_week']:.0f}",
                f"  За месяц: {stats['rev_month']:.0f}",
                f"  Всего: {stats['total_revenue']:.0f}",
                f"  Средний чек: {stats['avg_check']:.0f}",
            ]
        if stats["top_games"]:
            lines += ["", "<b>Популярные игры:</b>"]
            for game_name, count in stats["top_games"]:
                lines.append(f"  • {_esc(game_name)}: {count}")

        # ── 💰 Финансы (калькулятор + периоды + топ-3) ────────────────
        accs_all = list_accounts()
        total_cost = sum(float(a.get("cost", 0) or 0) for a in accs_all)
        total_revenue_all = sum(
            float((a.get("stats") or {}).get("total_revenue", 0) or 0)
            for a in accs_all)
        total_reviews_all = sum(
            int((a.get("stats") or {}).get("reviews_count", 0) or 0)
            for a in accs_all)
        total_profit = total_revenue_all - total_cost
        n_accs = max(1, len(accs_all))
        roi_str = "—"
        if total_cost > 0:
            roi_str = f"{(total_profit / total_cost) * 100:+.0f}%"
        avg_profit_per_acc = total_profit / n_accs

        # Финансы по периодам (из истории)
        fp = _calc_finance_periods(None)

        lines += [
            "",
            "━━━━━━━━━━━━━━━━━━",
            "<b>💰 Финансы (итог)</b>",
            f"  💵 Расход:    <b>{total_cost:.0f}₽</b>",
            f"  💰 Выручка:   <b>{total_revenue_all:.0f}₽</b>",
            f"  📈 Прибыль:   <b>{total_profit:+.0f}₽</b>  "
            f"(ROI {roi_str})",
            f"  ⭐ Отзывов:   <b>{total_reviews_all}</b>",
            f"  📊 Ср. прибыль/акк: <b>{avg_profit_per_acc:+.0f}₽</b>",
            "",
            "<b>📅 Выручка по периодам</b>",
            f"  День:    <b>{fp['day']:.0f}₽</b>  "
            f"({fp['count_day']} прод.)",
            f"  Неделя:  <b>{fp['week']:.0f}₽</b>  "
            f"({fp['count_week']} прод.)",
            f"  Месяц:   <b>{fp['month']:.0f}₽</b>  "
            f"({fp['count_month']} прод.)",
        ]

        # ── 🏆 Топ-3 самых прибыльных аккаунта ─────────────────────────
        scored = []
        for a in accs_all:
            alias = a.get("alias", "")
            if not alias:
                continue
            rev = float((a.get("stats") or {}).get("total_revenue", 0) or 0)
            cst = float(a.get("cost", 0) or 0)
            pft = rev - cst
            sales = int((a.get("stats") or {}).get("rentals_count", 0) or 0)
            if sales == 0 and rev == 0 and cst == 0:
                continue  # пустые аккаунты не показываем
            scored.append((pft, alias, rev, cst, sales))
        scored.sort(reverse=True, key=lambda x: x[0])
        top3 = scored[:3]
        if top3:
            lines += ["", "<b>🏆 Топ-3 прибыли</b>"]
            medals = ["🥇", "🥈", "🥉"]
            for i, (pft, alias, rev, cst, sales) in enumerate(top3):
                roi_s = "—"
                if cst > 0:
                    roi_s = f"{(pft / cst) * 100:+.0f}%"
                lines.append(
                    f"  {medals[i]} <code>{_esc(alias)}</code>: "
                    f"<b>{pft:+.0f}₽</b> "
                    f"(выручка {rev:.0f}₽, ROI {roi_s}, {sales} прод.)")

        return "\n".join(lines)

    # ───── История ───────────────────────────────────────────────────────
    def _kb_history() -> tbtypes.InlineKeyboardMarkup:
        kb = tbtypes.InlineKeyboardMarkup(row_width=1)
        kb.add(tbtypes.InlineKeyboardButton(
            "📥 Скачать CSV", callback_data="sr:hcsv"))
        kb.add(tbtypes.InlineKeyboardButton(
            "📄 Действия (последние 30)", callback_data="sr:actions_tail"))
        kb.add(tbtypes.InlineKeyboardButton(
            "📎 Скачать actions.log", callback_data="sr:actions_file"))
        kb.add(tbtypes.InlineKeyboardButton(
            "🗑 Очистить историю", callback_data="sr:hclear"))
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ Назад", callback_data="sr:main"))
        return kb

    def _text_history() -> str:
        history = list_history()
        if not history:
            return "<b>📜 История аренд</b>\n\nПока пусто."
        last_10 = history[-10:]
        last_10.reverse()
        lines = [f"<b>📜 История аренд</b> (всего: {len(history)})", ""]
        event_icons = {
            "start": "📦", "end": "⏳", "extend": "➕",
            "review_bonus": "⭐",
        }
        for h in last_10:
            icon = event_icons.get(h.get("event", ""), "•")
            ts = _fmt_ts(h.get("ts", 0))
            alias = h.get("alias", "?")
            buyer = h.get("buyer_username", "")
            evt = h.get("event", "?")
            line = f"{icon} <code>{ts}</code> {_esc(evt)} <b>{_esc(alias)}</b>"
            if buyer:
                line += f" ({_esc(buyer)})"
            lines.append(line)
        lines.append("\nПоследние 10 записей. Используй CSV для полной выгрузки.")
        return "\n".join(lines)

    # ───── Ивенты (события) ─────────────────────────────────────
    def _kb_events() -> tbtypes.InlineKeyboardMarkup:
        events = _load_events()
        unclosed = events.get("unclosed_notify", {})
        kb = tbtypes.InlineKeyboardMarkup(row_width=1)
        enabled = unclosed.get("enabled", True)
        kb.add(tbtypes.InlineKeyboardButton(
            f"{'✅' if enabled else '❌'} Уведомление незакрытых заказов",
            callback_data="sr:ev_toggle"))
        kb.add(tbtypes.InlineKeyboardButton(
            "⚠ Уведомить незакрытые заказы",
            callback_data="sr:ev_run"))
        kb.add(tbtypes.InlineKeyboardButton(
            "⏰ Интервал (часы)",
            callback_data="sr:ev_interval"))
        kb.add(
            tbtypes.InlineKeyboardButton(
                "🔄 Обновить", callback_data="sr:events"),
        )
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ Назад", callback_data="sr:main"))
        return kb

    def _text_events() -> str:
        events = _load_events()
        unclosed = events.get("unclosed_notify", {})
        enabled = unclosed.get("enabled", True)
        interval = unclosed.get("interval_hours", 24)
        last_run = unclosed.get("last_run", 0)
        next_run = unclosed.get("next_run", 0)
        unclosed_list = _get_unclosed_rentals()
        last_str = _fmt_ts(last_run) if last_run else "никогда"
        next_str = _fmt_ts(next_run) if next_run else "не запланировано"
        return (
            "<b>🚩 Ивенты steam_rental</b>\n\n"
            f"⚠ <b>Уведомление незакрытых заказов:</b>\n"
            f"  Статус: {'✅ вкл' if enabled else '❌ выкл'}\n"
            f"  Интервал: {interval} ч.\n"
            f"  · Последнее: {last_str}\n"
            f"  · Следующее: {next_str}\n\n"
            f"Незакрытых заказов сейчас: <b>{len(unclosed_list)}</b>"
        )

    def _run_unclosed_notify() -> str:
        unclosed = _get_unclosed_rentals()
        events = _load_events()
        ev = events.setdefault("unclosed_notify", {})
        ev["last_run"] = _now()
        interval = ev.get("interval_hours", 24)
        ev["next_run"] = _now() + interval * 3600
        _save_events(events)
        if not unclosed:
            return "Незакрытых заказов нет."
        lines = [f"⚠ <b>Незакрытых заказов: {len(unclosed)}</b>\n"]
        for u in unclosed:
            lines.append(
                f"• <b>{_esc(u['alias'])}</b> \u2014 "
                f"{_esc(u['buyer_username'])} "
                f"(истёк {u['expired_at']}, "
                f"просрочка {u['overdue_min']} мин.)")
        return "\n".join(lines)

    # ───── Инструкция ─────────────────────────────────────────
    # Полное руководство: добавление игры, лота, аккаунта. Время аренды
    # задаётся ТОЛЬКО тэгами в описании лота на FunPay (#Hours: / #Time:),
    # никаких UI-полей и эвристик — это намеренно, чтобы не было багов вида
    # «лот «24 часа» выдан как 1 час». Намеренно держим текст в пределах
    # 4096 символов (лимит Telegram editMessageText/sendMessage).
    INSTRUCTIONS_TEXT = (
        "<b>📝 Инструкция Steam Rental</b>\n\n"
        "<b>Порядок настройки:</b> игра → аккаунт → лот.\n\n"
        "<b>1️⃣ Добавь игру</b>\n"
        "<code>/srental → 🎮 Игры → ➕ Добавить</code>. "
        "Введи имя как на FunPay (например, "
        "<code>Counter Strike 2</code>). К игре потом привязываются "
        "и аккаунты, и лоты — это позволяет выдавать любой свободный "
        "аккаунт игры под любой её лот.\n\n"
        "<b>2️⃣ Добавь аккаунт</b>\n"
        "<code>/srental → 📋 Аккаунты → ➕ Добавить</code>:\n"
        "  • <b>alias</b> — короткое имя для тебя (например, "
        "<code>cs1</code>);\n"
        "  • <b>.maFile</b> — пришли документом из Steam Desktop "
        "Authenticator;\n"
        "  • <b>пароль</b> — текущий пароль Steam-аккаунта;\n"
        "  • <b>игра</b> — выбери из списка.\n\n"
        "<b>3️⃣ Добавь лот FunPay</b>\n"
        "<code>/srental → 🎯 Лоты → ➕ Добавить</code>. Шаги:\n"
        "  • <b>ID лота</b> — число из URL "
        "<code>funpay.com/lots/offer?id=12345678</code>;\n"
        "  • <b>пул аккаунтов</b> — выбери кнопками;\n"
        "  • <b>игра</b> — та же, к которой привязан аккаунт.\n\n"
        "<b>⚠️ Срок аренды задаётся ТОЛЬКО в описании лота на FunPay</b>\n"
        "В описании лота на FunPay добавь одну из строк:\n"
        "  • <code>#Hours: 24</code> — 24 часа\n"
        "  • <code>#Hours: 1</code> — 1 час\n"
        "  • <code>#Time: 2ч</code> — 2 часа\n"
        "  • <code>#Time: 30m</code> — 30 минут\n"
        "  • <code>#Time: 1d</code> — 1 день\n"
        "Без этого тэга бот не знает срок и НЕ выдаст аккаунт — "
        "пришлёт тебе уведомление, что нужно добавить тэг.\n"
        "<b>Бонус за 5★ отзыв</b> — отдельный тэг "
        "<code>#Review: 1h</code> в том же описании "
        "(добавляет 1 час к аренде, можно <code>30m</code>, "
        "<code>2ч</code> и т.п.).\n\n"
        "<b>4️⃣ Лот для продления (опционально)</b>\n"
        "<code>/srental → 🎯 Лоты → ➕ Лот-продление</code>. "
        "При покупке такого лота активная аренда покупателя "
        "автоматически продлевается. Срок продления, как и у обычных "
        "лотов, читается из описания лота на FunPay (<code>#Hours: 1</code> "
        "/ <code>#Time: 30m</code>); если тэга нет — дефолт 1 час. "
        "По команде <code>!продлить</code> в чате FunPay лот включается "
        "на 10 минут, потом сам выключается, если не оплатили.\n\n"
        "<b>5️⃣ Что делает покупатель</b>\n"
        "Оплачивает лот → получает логин/пароль. В чате FunPay: "
        "<code>!код</code>, <code>!продлить</code>, <code>!статус</code>, "
        "<code>!помощь</code>. По истечении бот сменит пароль.\n\n"
        "<b>Полезно:</b> ❄️ заморозка аккаунта, ⭐ бонус за 5★ отзыв, "
        "📊 статистика + CSV, 🔧 массовые действия, 🚨 VAC-скан, "
        "🏠 PC-club + AI-проверка фото."
    )

    def _kb_instructions() -> tbtypes.InlineKeyboardMarkup:
        kb = tbtypes.InlineKeyboardMarkup()
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ Назад", callback_data="sr:main"))
        return kb

    # ───── Массовые действия ─────────────────────────────────────────────
    def _kb_bulk() -> tbtypes.InlineKeyboardMarkup:
        kb = tbtypes.InlineKeyboardMarkup(row_width=1)
        kb.add(
            tbtypes.InlineKeyboardButton(
                "❄️ Заморозить все", callback_data="sr:bfreeze"),
            tbtypes.InlineKeyboardButton(
                "🔥 Разморозить все", callback_data="sr:bunfreeze"),
            tbtypes.InlineKeyboardButton(
                "🔁 Сменить все пароли", callback_data="sr:bchpwd"),
            tbtypes.InlineKeyboardButton(
                "🔍 Проверить все аккаунты", callback_data="sr:bcheck"),
        )
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ Назад", callback_data="sr:main"))
        return kb

    def _text_bulk() -> str:
        accs = list_accounts()
        frozen = sum(1 for a in accs if a.get("frozen"))
        active = sum(1 for a in accs if a.get("rental"))
        free = len(accs) - frozen - active
        return (
            "<b>🔧 Массовые действия</b>\n\n"
            f"Всего аккаунтов: <b>{len(accs)}</b>\n"
            f"Свободно: {free} | Занято: {active} | Заморожено: {frozen}\n\n"
            "⚠️ Будь осторожен — действия необратимы."
        )

    def _bulk_freeze_all():
        count = 0
        with _lock:
            accs = list_accounts()
            for a in accs:
                if not a.get("frozen") and not a.get("rental"):
                    a["frozen"] = True
                    count += 1
            save_accounts(accs)
        return count

    def _bulk_unfreeze_all():
        count = 0
        with _lock:
            accs = list_accounts()
            for a in accs:
                if a.get("frozen"):
                    a["frozen"] = False
                    a["login_failures"] = 0
                    count += 1
            save_accounts(accs)
        return count

    def _bulk_chpwd_thread(chat_id, msg_id):
        accs = list_accounts()
        targets = [a for a in accs if not a.get("rental") and not a.get("frozen")]
        if not targets:
            kb = tbtypes.InlineKeyboardMarkup()
            kb.add(tbtypes.InlineKeyboardButton(
                "◀️ Назад", callback_data="sr:bulk"))
            _edit_menu(chat_id, msg_id,
                       "Нет подходящих аккаунтов для смены паролей.", kb)
            return
        ok = 0
        fail = 0
        for acc in targets:
            try:
                s = SteamSession(acc["account_name"], acc["password"],
                                  acc["shared_secret"], acc["identity_secret"],
                                  acc.get("steamid"))
                s.login()
                _track_login_result(acc["alias"], True)
                new_pw = _gen_password()
                s.change_password(new_pw)
                with _lock:
                    a = find_account(acc["alias"]) or acc
                    a["password"] = new_pw
                    a["steamid"] = s.steamid
                    upsert_account(a)
                ok += 1
            except Exception:
                _track_login_result(acc["alias"], False)
                fail += 1
            time.sleep(3)
        kb = tbtypes.InlineKeyboardMarkup()
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ Назад", callback_data="sr:bulk"))
        _edit_menu(chat_id, msg_id,
                   f"<b>🔁 Массовая смена паролей</b>\n\n"
                   f"✅ Успешно: {ok}\n❌ Ошибки: {fail}", kb)

    def _bulk_check_thread(chat_id, msg_id):
        accs = list_accounts()
        targets = [a for a in accs if not a.get("frozen") and a.get("shared_secret")]
        if not targets:
            kb = tbtypes.InlineKeyboardMarkup()
            kb.add(tbtypes.InlineKeyboardButton(
                "◀️ Назад", callback_data="sr:bulk"))
            _edit_menu(chat_id, msg_id, "Нет аккаунтов для проверки.", kb)
            return
        ok_list: list[str] = []
        fail_list: list[str] = []
        for acc in targets:
            try:
                s = SteamSession(acc["account_name"], acc["password"],
                                  acc["shared_secret"], acc["identity_secret"],
                                  acc.get("steamid"))
                s.login()
                _track_login_result(acc["alias"], True)
                with _lock:
                    a = find_account(acc["alias"]) or acc
                    a["steamid"] = s.steamid
                    upsert_account(a)
                ok_list.append(acc["alias"])
            except Exception:
                _track_login_result(acc["alias"], False)
                fail_list.append(acc["alias"])
            time.sleep(3)
        frozen_count = sum(1 for a in fail_list
                           if (find_account(a) or {}).get("frozen"))
        kb = tbtypes.InlineKeyboardMarkup()
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ Назад", callback_data="sr:bulk"))
        txt = (f"<b>🔍 Проверка аккаунтов</b>\n\n"
               f"✅ OK: {len(ok_list)}\n❌ Ошибки: {len(fail_list)}")
        if frozen_count:
            txt += f"\n❄️ Авто-заморожено: {frozen_count}"
        if fail_list:
            txt += f"\n\nНеудачные: <code>{', '.join(fail_list[:20])}</code>"
        _edit_menu(chat_id, msg_id, txt, kb)
        try:
            _update_lot_activation(cardinal)
        except Exception:
            pass

    HELP_TEXT = (
        f"<b>❓ Steam Rental v{VERSION}</b>\n\n"
        "<b>Быстрый старт:</b>\n"
        "1. <b>📋 Аккаунты → ➕ Добавить</b> — alias, .maFile, пароль.\n"
        "2. <b>🎯 Лоты → ➕ Добавить</b> — ID лота, длительность, пул.\n"
        "3. Покупатель оплачивает → плагин выдаёт креды.\n"
        "4. Покупатель пишет в чате FunPay:\n"
        "   • <code>!код [логин]</code> — Steam Guard код\n"
        "   • <code>!продлить</code> — инструкция продления\n"
        "   • <code>!статус</code> — инфо об аренде\n"
        "   • <code>!помощь</code> — список команд\n"
        "5. По истечении — отзыв сессий + смена пароля.\n\n"
        "� <b>Полный changelog</b> всех версий и других плагинов — "
        "<code>extracted/UPDATES.md</code> в репозитории."
    )

    def _kb_help() -> tbtypes.InlineKeyboardMarkup:
        kb = tbtypes.InlineKeyboardMarkup()
        kb.add(tbtypes.InlineKeyboardButton("◀️ Назад", callback_data="sr:main"))
        return kb

    # ───── Helpers ──────────────────────────────────────────────────────
    def _split_text(text: str, limit: int = 4000) -> list[str]:
        """Режет длинный текст на куски <= limit символов по границам строк,
        чтобы не упереться в лимит Telegram (4096) при edit/send. Разрыв идёт
        только между целыми строками, поэтому HTML-теги (каждый в своей
        строке) не рвутся. Сверхдлинную одиночную строку режем жёстко."""
        parts: list[str] = []
        cur = ""
        for line in (text or "").split("\n"):
            while len(line) > limit:
                if cur:
                    parts.append(cur)
                    cur = ""
                parts.append(line[:limit])
                line = line[limit:]
            if cur and len(cur) + len(line) + 1 > limit:
                parts.append(cur)
                cur = line
            else:
                cur = (cur + "\n" + line) if cur else line
        if cur:
            parts.append(cur)
        return parts or [""]

    def _edit_menu(chat_id: int, message_id: int, text: str,
                   kb: tbtypes.InlineKeyboardMarkup) -> None:
        # Telegram ограничивает edit/sendMessage 4096 символами. Длинные
        # тексты (например, HELP с полным changelog ~8k символов) иначе роняют
        # edit_message_text, ошибка молча проглатывается ниже, и кнопка в меню
        # «не работает» (visually no-op). Поэтому длинный текст шлём кусками:
        # первый — правкой исходного сообщения, остальные — новыми; клавиатуру
        # вешаем на последний кусок, чтобы кнопка «◀️ Назад» осталась рабочей.
        chunks = _split_text(text, 4000)
        if len(chunks) > 1:
            _empty_kb = tbtypes.InlineKeyboardMarkup()
            for i, ch in enumerate(chunks):
                _kb = kb if i == len(chunks) - 1 else _empty_kb
                try:
                    if i == 0:
                        tg.bot.edit_message_text(
                            ch, chat_id=chat_id, message_id=message_id,
                            reply_markup=_kb, parse_mode="HTML",
                            disable_web_page_preview=True)
                    else:
                        tg.bot.send_message(
                            chat_id, ch, reply_markup=_kb,
                            parse_mode="HTML",
                            disable_web_page_preview=True)
                except Exception:
                    LOGGER.warning(
                        "steam_rental: edit_menu chunk %d/%d failed "
                        "(chat=%s)", i + 1, len(chunks), chat_id,
                        exc_info=True)
            return
        try:
            tg.bot.edit_message_text(text, chat_id=chat_id,
                                     message_id=message_id,
                                     reply_markup=kb, parse_mode="HTML",
                                     disable_web_page_preview=True)
        except Exception as _edit_ex:
            # "message is not modified" — обычная идемпотентная правка
            # (та же кнопка/тот же текст), не шумим.
            _msg = str(_edit_ex).lower()
            if "not modified" in _msg:
                LOGGER.debug(
                    "steam_rental: edit_menu noop (message not modified)")
                return
            LOGGER.warning(
                "steam_rental: edit_menu failed (chat=%s msg=%s "
                "text_len=%s): %s",
                chat_id, message_id, len(text or ""), _edit_ex,
                exc_info=True)

    def _send_menu(chat_id: int) -> "tbtypes.Message":
        return tg.bot.send_message(chat_id, _text_main(),
                                    reply_markup=_kb_main(),
                                    parse_mode="HTML",
                                    disable_web_page_preview=True)

    # ───── Remote Play sub-panel (sr:rp:*) ──────────────────────────────
    def _render_rp_account_detail(acc: dict, alias: str) -> tuple:
        """Build text and keyboard for the RP account detail view."""
        pool = _account_pool(acc)
        pool_icons = {"remoteplay": "🎮", "rental": "📋", "both": "🔄"}
        icon = pool_icons.get(pool, "🔄")
        rp_session = find_active_rp_session_by_alias(alias)
        status_lines = []
        if acc.get("frozen"):
            status_lines.append("❄️ Заморожен")
        if acc.get("rental"):
            status_lines.append("📋 В аренде")
        if rp_session:
            status_lines.append("▶️ RP сессия активна")
        status_str = ", ".join(status_lines) if status_lines else "Свободен"
        text = (
            f"<b>📦 Аккаунт: {_esc(alias)}</b>\n\n"
            f"👤 Login: <code>{_esc(acc.get('account_name', '?'))}</code>\n"
            f"{icon} Pool: <b>{pool}</b>\n"
            f"📊 Статус: {status_str}\n"
        )
        kb = tbtypes.InlineKeyboardMarkup(row_width=1)
        kb.add(tbtypes.InlineKeyboardButton(
            f"🔄 Сменить Pool [{pool}]",
            callback_data=f"sr:rp:pool:{_sid(alias)}"))
        kb.add(tbtypes.InlineKeyboardButton(
            "« Назад", callback_data="sr:rp:accs"))
        return text, kb

    def _handle_rp_callback(chat_id: int, msg_id: int, arg: str, call) -> None:
        """Handle sr:rp:<arg> callbacks from the main rental menu."""
        if not _is_admin_user(call.from_user.id):
            return
        if arg == "main":
            sessions = list_rp_sessions()
            active_count = sum(1 for s in sessions.values()
                               if s.get("status") == "active")
            accs = list_accounts()
            lots = list_lots()
            rp_lots = [k for k, v in lots.items()
                       if v.get("type") == "remoteplay"]
            rp_accs = [a for a in accs
                       if a.get("remoteplay") or a.get("rp_enabled")
                       or _account_pool(a) in ("remoteplay", "both")]

            text = (
                "<b>🎮 Remote Play</b>\n\n"
                f"▶️ Активных сессий: <b>{active_count}</b>\n"
                f"📦 RP аккаунтов: <b>{len(rp_accs)}</b>\n"
                f"🎯 RP лотов: <b>{len(rp_lots)}</b>\n\n"
                "Выбери раздел:"
            )
            kb = tbtypes.InlineKeyboardMarkup(row_width=2)
            kb.add(
                tbtypes.InlineKeyboardButton(
                    "▶️ Сессии RP", callback_data="sr:rp:sessions"),
                tbtypes.InlineKeyboardButton(
                    "➕ Аккаунт RP", callback_data="sr:rp:add_acc"),
            )
            kb.add(
                tbtypes.InlineKeyboardButton(
                    "📦 Аккаунты RP", callback_data="sr:rp:accs"),
                tbtypes.InlineKeyboardButton(
                    "🎯 Лоты RP", callback_data="sr:rp:lots"),
            )
            kb.add(
                tbtypes.InlineKeyboardButton(
                    "📖 Гайд RP", callback_data="sr:rp:guide"),
            )
            kb.add(
                tbtypes.InlineKeyboardButton(
                    "« Назад", callback_data="sr:rp:back"),
            )
            _edit_menu(chat_id, msg_id, text, kb)

        elif arg == "sessions":
            sessions = list_rp_sessions()
            active = {k: v for k, v in sessions.items()
                      if v.get("status") == "active"}
            if not active:
                text = "▶️ <b>Сессии Remote Play</b>\n\nНет активных сессий."
            else:
                lines = ["▶️ <b>Сессии Remote Play</b>\n"]
                for sid, s in active.items():
                    time_left = max(0, s.get("expires_at", 0) - _now())
                    lines.append(
                        f"  🔗 <code>{_esc(s.get('alias', '?'))}</code> - "
                        f"{_esc(s.get('buyer_username', '?'))}\n"
                        f"     ⏰ Осталось: {_human_minutes(time_left // 60)}"
                    )
                text = "\n".join(lines)
            kb = tbtypes.InlineKeyboardMarkup(row_width=1)
            kb.add(
                tbtypes.InlineKeyboardButton(
                    "« Назад", callback_data="sr:rp:main"),
            )
            _edit_menu(chat_id, msg_id, text, kb)

        elif arg == "add_acc":
            text = (
                "<b>➕ Добавить RP аккаунт</b>\n\n"
                "Используй команду:\n"
                "<code>/srp_add alias login password shared identity [steamid]</code>\n\n"
                "<b>Параметры:</b>\n"
                "• <code>alias</code> - короткое имя аккаунта\n"
                "• <code>login</code> - логин Steam\n"
                "• <code>password</code> - пароль Steam\n"
                "• <code>shared</code> - shared_secret (Steam Guard)\n"
                "• <code>identity</code> - identity_secret\n"
                "• <code>[steamid]</code> - SteamID64 (опционально)"
            )
            kb = tbtypes.InlineKeyboardMarkup(row_width=1)
            kb.add(
                tbtypes.InlineKeyboardButton(
                    "« Назад", callback_data="sr:rp:main"),
            )
            _edit_menu(chat_id, msg_id, text, kb)

        elif arg == "lots":
            lots = list_lots()
            rp_lots = {k: v for k, v in lots.items()
                       if v.get("type") == "remoteplay"}
            if not rp_lots:
                text = "🎯 <b>Лоты Remote Play</b>\n\nНет RP лотов."
            else:
                lines = ["🎯 <b>Лоты Remote Play</b>\n"]
                for key, lot in rp_lots.items():
                    active_flag = "✅" if lot.get("active") else "❌"
                    lines.append(
                        f"  {active_flag} <code>{_esc(key)}</code> - "
                        f"{_esc(lot.get('game', '?'))} "
                        f"({lot.get('duration_min', '?')} мин)"
                    )
                text = "\n".join(lines)
            kb = tbtypes.InlineKeyboardMarkup(row_width=1)
            kb.add(
                tbtypes.InlineKeyboardButton(
                    "« Назад", callback_data="sr:rp:main"),
            )
            _edit_menu(chat_id, msg_id, text, kb)

        elif arg == "guide":
            try:
                tg.bot.answer_callback_query(call.id)
            except Exception:
                pass
            text = (
                "<b>📖 Steam Remote Play — Гайд</b>\n\n"
                "<b>Что это:</b>\n"
                "Сдача Steam-аккаунтов через Remote Play. Покупатель получает "
                "PIN Steam Link (4 цифры), подключает свой клиент к аккаунту "
                "и играет удалённо. По истечении аренды — авто-дисконнект.\n\n"
                "<b>Настройка:</b>\n"
                "1. /sremoteplay → меню\n"
                "2. <code>/srp_add alias login password shared identity</code>\n"
                "3. <code>/srp_lot keyword duration_min alias1,alias2 game</code>\n"
                "4. На Steam-аккаунте: Settings → Remote Play → ON\n"
                "5. В пуле аккаунта поставить «remoteplay» или «both»\n\n"
                "<b>Как работает:</b>\n"
                "• Покупатель оплачивает → бот логинится в Steam\n"
                "• Генерируется PIN (4 цифры)\n"
                "• Покупатель вводит PIN → подключается\n"
                "• По таймеру → сессия отключается\n\n"
                "<b>📨 Команды покупателя:</b>\n"
                "• <code>!пин</code> / <code>!pin</code> — новый PIN\n"
                "• <code>!статусrp</code> / <code>!statusrp</code> — статус\n"
                "• <code>!помощьrp</code> / <code>!helprp</code> — помощь\n\n"
                "<b>🔖 Хэштеги в описании лота:</b>\n"
                "<code>#Hours: 2</code> — длительность в часах (приоритетный)\n"
                "<code>#Time: 2ч</code> — длительность аренды\n"
                "<code>#Review: 30m</code> — бонус за отзыв\n"
                "(суффиксы: m/мин, h/ч/час, d/д/дн, w/нед)\n\n"
                "<b>Анти-чит:</b>\n"
                "• Скриншоты сессии + AI-анализ\n"
                "• Авто-дисконнект при детекте читов\n\n"
                "Полный гайд: /srp_guide"
            )
            kb = tbtypes.InlineKeyboardMarkup(row_width=1)
            kb.add(
                tbtypes.InlineKeyboardButton(
                    "« Назад", callback_data="sr:rp:main"),
            )
            _edit_menu(chat_id, msg_id, text, kb)

        elif arg == "back":
            _edit_menu(chat_id, msg_id, _text_main(), _kb_main())

        elif arg == "accs":
            # Show RP accounts list with pool status
            accs = list_accounts()
            rp_accs = [a for a in accs
                       if _account_pool(a) in ("remoteplay", "both")]
            if not rp_accs:
                text = "📦 <b>RP Аккаунты</b>\n\nНет аккаунтов для Remote Play."
            else:
                lines = ["📦 <b>RP Аккаунты</b>\n"]
                pool_icons = {"remoteplay": "🎮", "rental": "📋", "both": "🔄"}
                for a in rp_accs:
                    pool = _account_pool(a)
                    icon = pool_icons.get(pool, "🔄")
                    frozen_flag = "❄️" if a.get("frozen") else ""
                    rp_active = "▶️" if find_active_rp_session_by_alias(a["alias"]) else ""
                    rental_flag = "📋" if a.get("rental") else ""
                    lines.append(
                        f"  {icon} <code>{_esc(a['alias'])}</code> "
                        f"[{pool}] {frozen_flag}{rp_active}{rental_flag}"
                    )
                text = "\n".join(lines)
            kb = tbtypes.InlineKeyboardMarkup(row_width=2)
            for a in rp_accs[:20]:
                kb.add(tbtypes.InlineKeyboardButton(
                    f"{_esc(a['alias'])} [{_account_pool(a)}]",
                    callback_data=f"sr:rp:detail:{_sid(a['alias'])}"))
            kb.add(tbtypes.InlineKeyboardButton(
                "« Назад", callback_data="sr:rp:main"))
            _edit_menu(chat_id, msg_id, text, kb)

        elif arg.startswith("detail:"):
            # Show individual RP account detail with pool toggle button
            sid_val = arg[len("detail:"):]
            alias = _resolve_alias(sid_val)
            if not alias:
                _edit_menu(chat_id, msg_id,
                           "❌ Аккаунт не найден.",
                           tbtypes.InlineKeyboardMarkup().add(
                               tbtypes.InlineKeyboardButton(
                                   "« Назад", callback_data="sr:rp:accs")))
                return
            acc = find_account(alias)
            if not acc:
                _edit_menu(chat_id, msg_id,
                           "❌ Аккаунт не найден.",
                           tbtypes.InlineKeyboardMarkup().add(
                               tbtypes.InlineKeyboardButton(
                                   "« Назад", callback_data="sr:rp:accs")))
                return
            text, kb = _render_rp_account_detail(acc, alias)
            _edit_menu(chat_id, msg_id, text, kb)

        elif arg.startswith("pool:"):
            # Cycle pool: remoteplay -> rental -> both -> remoteplay
            sid_val = arg[len("pool:"):]
            alias = _resolve_alias(sid_val)
            if not alias:
                tg.bot.answer_callback_query(call.id, "Аккаунт не найден.")
                return
            acc = find_account(alias)
            if not acc:
                tg.bot.answer_callback_query(call.id, "Аккаунт не найден.")
                return
            current_pool = _account_pool(acc)
            cycle = {"remoteplay": "rental", "rental": "both", "both": "remoteplay"}
            new_pool = cycle.get(current_pool, "both")
            # Guard: prevent cycling to rental if RP session is active
            if new_pool == "rental":
                rp_session = find_active_rp_session_by_alias(alias)
                if rp_session:
                    tg.bot.answer_callback_query(
                        call.id,
                        "Cannot change pool: account has active RP session",
                        show_alert=True)
                    return
            # Guard: prevent cycling to remoteplay if regular rental is active
            if new_pool == "remoteplay":
                if acc.get("rental"):
                    tg.bot.answer_callback_query(
                        call.id,
                        "Cannot change pool: account has active rental",
                        show_alert=True)
                    return
            acc["pool"] = new_pool
            upsert_account(acc)
            tg.bot.answer_callback_query(
                call.id, f"Pool: {current_pool} -> {new_pool}")
            # Re-render detail view
            text, kb = _render_rp_account_detail(acc, alias)
            _edit_menu(chat_id, msg_id, text, kb)

    # ───── /srental ─────────────────────────────────────────────────────
    def cmd_srental(message):
        if not _is_admin_user(message.from_user.id):
            return
        _send_menu(message.chat.id)

    def cmd_cancel(message):
        if not _is_admin_user(message.from_user.id):
            return
        if _pending_state.pop(message.from_user.id, None):
            tg.bot.send_message(message.chat.id, "Отменено.")
        else:
            tg.bot.send_message(message.chat.id, "Нет активного ввода.")

    def cmd_stats(message):
        """Глобальная статистика: продажи / отзывы / финансы.

        По умолчанию — общая сводка. С аргументом alias — детально
        по конкретному аккаунту (пример: /srental_stats cs1).
        """
        if not _is_admin_user(message.from_user.id):
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) > 1:
            alias = parts[1].strip()
            if not find_account(alias):
                tg.bot.send_message(message.chat.id,
                    f"Аккаунт <code>{_esc(alias)}</code> не найден.",
                    parse_mode="HTML")
                return
            try:
                text = _format_acc_stats_compact(alias)
            except Exception as e:
                LOGGER.error(
                    "steam_rental: cmd_stats per-account failed",
                    exc_info=True)
                text = f"⚠ Ошибка: <code>{_esc(str(e))}</code>"
            tg.bot.send_message(message.chat.id, text,
                                parse_mode="HTML",
                                disable_web_page_preview=True)
            return
        # Общая сводка
        try:
            text = _text_stats()
            tg.bot.send_message(message.chat.id, text,
                                parse_mode="HTML",
                                disable_web_page_preview=True)
        except Exception as e:
            tg.bot.send_message(message.chat.id,
                f"⚠ Ошибка построения статистики: <code>{_esc(str(e))}</code>",
                parse_mode="HTML")
            LOGGER.error("steam_rental: cmd_stats failed", exc_info=True)

    def cmd_acc_stats(message):
        """Статистика по конкретному аккаунту или меню выбора.

        /srental_acc_stats — список всех аккаунтов с inline-кнопками.
        /srental_acc_stats <alias> — сразу сводка по этому аккаунту.
        """
        if not _is_admin_user(message.from_user.id):
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) > 1:
            alias = parts[1].strip()
            if not find_account(alias):
                tg.bot.send_message(message.chat.id,
                    f"Аккаунт <code>{_esc(alias)}</code> не найден.",
                    parse_mode="HTML")
                return
            try:
                text = _format_acc_stats_compact(alias)
            except Exception as e:
                LOGGER.error(
                    "steam_rental: cmd_acc_stats failed", exc_info=True)
                text = f"⚠ Ошибка: <code>{_esc(str(e))}</code>"
            tg.bot.send_message(message.chat.id, text,
                                parse_mode="HTML",
                                disable_web_page_preview=True)
            return
        # Меню выбора аккаунта
        accs = sorted(list_accounts(), key=lambda a: a.get("alias", ""))
        if not accs:
            tg.bot.send_message(message.chat.id,
                "Аккаунтов нет. Добавь через /srental → 📋 Аккаунты.")
            return
        kb = tbtypes.InlineKeyboardMarkup(row_width=2)
        for a in accs[:40]:
            alias = a.get("alias", "")
            if not alias:
                continue
            rev = float((a.get("stats") or {}).get("total_revenue", 0) or 0)
            cst = float(a.get("cost", 0) or 0)
            pft = rev - cst
            label = f"{alias} ({pft:+.0f}₽)"
            kb.add(tbtypes.InlineKeyboardButton(
                label, callback_data=f"sr:accstats:{_sid(alias)}"))
        tg.bot.send_message(message.chat.id,
            "<b>📊 Статистика по аккаунтам</b>\n\n"
            "Выбери аккаунт для подробной сводки:",
            reply_markup=kb, parse_mode="HTML")

    # ───── Callback router ───────────────────────────────────────────────
    def on_cb(call):
        uid = call.from_user.id
        if not _is_admin_user(uid):
            tg.bot.answer_callback_query(call.id, "Нет доступа.")
            return
        data = (call.data or "")
        if not data.startswith("sr:"):
            return
        parts = data.split(":", 2)
        action = parts[1] if len(parts) > 1 else ""
        arg = parts[2] if len(parts) > 2 else ""
        chat_id = call.message.chat.id
        msg_id = call.message.message_id
        # Trace на каждый клик, чтобы видеть в cardinal.log что callback
        # вообще доходит до плагина (помогает отличить «handler упал»
        # от «click не доехал до steam_rental»).
        LOGGER.info(
            "steam_rental: cb action=%s arg=%s user=%s chat=%s",
            action, arg, uid, chat_id)

        try:
            if action == "main":
                _edit_menu(chat_id, msg_id, _text_main(), _kb_main())
            elif action == "close":
                try:
                    tg.bot.delete_message(chat_id, msg_id)
                except Exception:
                    pass
            elif action == "accs":
                page = int(arg) if arg.isdigit() else 0
                _edit_menu(chat_id, msg_id, _text_accs(), _kb_accs(page))
            elif action == "acc":
                alias = _resolve_alias(arg)
                if not alias:
                    tg.bot.answer_callback_query(call.id, "Аккаунт не найден.")
                    _edit_menu(chat_id, msg_id, _text_accs(), _kb_accs(0))
                    return
                _edit_menu(chat_id, msg_id, _text_acc(alias), _kb_acc(alias))
            elif action == "show":
                alias = _resolve_alias(arg)
                if alias:
                    _edit_menu(chat_id, msg_id,
                               _text_acc(alias, show_pw=True), _kb_acc(alias))
            elif action == "guard":
                alias = _resolve_alias(arg)
                if alias:
                    _send_guard_code(chat_id, alias)
            elif action == "chpwd":
                alias = _resolve_alias(arg)
                if alias:
                    _start_chpwd(chat_id, msg_id, alias, call.id)
            elif action == "revoke":
                alias = _resolve_alias(arg)
                if alias:
                    _start_revoke(chat_id, msg_id, alias, call.id)
            elif action == "freeze":
                alias = _resolve_alias(arg)
                if alias:
                    state = "не найден"
                    with _lock:
                        acc = find_account(alias)
                        if acc:
                            acc["frozen"] = not acc.get("frozen", False)
                            if not acc["frozen"]:
                                acc["login_failures"] = 0
                            upsert_account(acc)
                            state = "заморожен ❄️" if acc["frozen"] else "разморожен 🔥"
                            if acc["frozen"]:
                                _log_action("acc_freeze",
                                            f"Ручная заморозка {alias} из TG",
                                            alias=alias, mode="manual",
                                            user_id=uid)
                            else:
                                _log_action("acc_unfreeze",
                                            f"Ручная разморозка {alias} из TG",
                                            alias=alias, mode="manual",
                                            user_id=uid)
                    tg.bot.answer_callback_query(call.id, f"Аккаунт {state}.")
                    _edit_menu(chat_id, msg_id, _text_acc(alias), _kb_acc(alias))
                    _update_lot_activation(cardinal)
            elif action == "setgame":
                alias = _resolve_alias(arg)
                if alias:
                    _start_set_game_acc(uid, chat_id, msg_id, alias, call.id)
            elif action == "setcost":
                alias = _resolve_alias(arg)
                if alias:
                    _start_set_cost(uid, chat_id, msg_id, alias, call.id)
            elif action == "setpd_acc":
                alias = _resolve_alias(arg)
                if alias:
                    _start_set_post_delivery_acc(uid, chat_id, msg_id, alias, call.id)
            elif action == "setpd_lot":
                key = _resolve_lot(arg)
                if key:
                    _start_set_post_delivery_lot(uid, chat_id, msg_id, key, call.id)
            elif action == "accstats":
                try:
                    tg.bot.answer_callback_query(call.id)
                except Exception:
                    pass
                alias = _resolve_alias(arg)
                if not alias:
                    return
                try:
                    text = _format_acc_stats_compact(alias)
                except Exception as e:
                    LOGGER.error(
                        "steam_rental: accstats callback failed",
                        exc_info=True)
                    text = f"⚠ Ошибка: <code>{_esc(str(e))}</code>"
                # Кнопка «◀️ К списку аккаунтов»
                kb_back = tbtypes.InlineKeyboardMarkup()
                kb_back.add(tbtypes.InlineKeyboardButton(
                    f"◀️ К аккаунту {alias}",
                    callback_data=f"sr:acc:{_sid(alias)}"))
                kb_back.add(tbtypes.InlineKeyboardButton(
                    "📊 Все аккаунты",
                    callback_data="sr:accstatslist:0"))
                _edit_menu(chat_id, msg_id, text, kb_back)
            elif action == "accstatslist":
                try:
                    tg.bot.answer_callback_query(call.id)
                except Exception:
                    pass
                page = int(arg) if arg.isdigit() else 0
                per_page = 20
                accs = sorted(list_accounts(),
                              key=lambda a: a.get("alias", ""))
                total_pages = max(1, (len(accs) + per_page - 1) // per_page)
                page = max(0, min(page, total_pages - 1))
                chunk = accs[page * per_page:(page + 1) * per_page]
                kb_list = tbtypes.InlineKeyboardMarkup(row_width=2)
                for a in chunk:
                    alias = a.get("alias", "")
                    if not alias:
                        continue
                    rev = float(
                        (a.get("stats") or {}).get("total_revenue", 0) or 0)
                    cst = float(a.get("cost", 0) or 0)
                    pft = rev - cst
                    label = f"{alias} ({pft:+.0f}₽)"
                    kb_list.add(tbtypes.InlineKeyboardButton(
                        label, callback_data=f"sr:accstats:{_sid(alias)}"))
                # Пагинация
                nav = []
                if page > 0:
                    nav.append(tbtypes.InlineKeyboardButton(
                        "◀ Стр.",
                        callback_data=f"sr:accstatslist:{page-1}"))
                if page + 1 < total_pages:
                    nav.append(tbtypes.InlineKeyboardButton(
                        "Стр. ▶",
                        callback_data=f"sr:accstatslist:{page+1}"))
                if nav:
                    kb_list.row(*nav)
                kb_list.add(tbtypes.InlineKeyboardButton(
                    "◀️ К статистике", callback_data="sr:stats"))
                _edit_menu(chat_id, msg_id,
                    f"<b>📊 Статистика по аккаунтам</b>\n\n"
                    f"Страница {page + 1}/{total_pages}. "
                    f"Аккаунтов: {len(accs)}.\n\n"
                    f"Выбери аккаунт для подробной сводки "
                    f"(в скобках — прибыль):",
                    kb_list)
            elif action == "ralias":
                alias = _resolve_alias(arg)
                if alias:
                    _start_rename_alias(uid, chat_id, msg_id, alias, call.id)
            elif action == "free":
                alias = _resolve_alias(arg)
                if alias:
                    with _lock:
                        acc = find_account(alias)
                        if acc:
                            acc.pop("rental", None)
                            upsert_account(acc)
                    tg.bot.answer_callback_query(call.id, "Освобождён.")
                    _edit_menu(chat_id, msg_id, _text_acc(alias), _kb_acc(alias))
                    _update_lot_activation(cardinal)
            elif action == "del":
                alias = _resolve_alias(arg)
                if alias:
                    _show_confirm_delete_acc(chat_id, msg_id, alias)
            elif action == "cdel":
                alias = _resolve_alias(arg)
                if alias:
                    delete_account(alias)
                    tg.bot.answer_callback_query(call.id, "Удалён.")
                    _edit_menu(chat_id, msg_id, _text_accs(), _kb_accs(0))
            elif action == "add":
                _start_add(uid, chat_id, msg_id, call.id)
            elif action == "addtest":
                _create_test_account(uid, chat_id, msg_id, call.id)
            elif action == "bulkimport":
                _start_bulk_import(uid, chat_id, msg_id, call.id)
            elif action == "flt":
                _edit_menu(chat_id, msg_id, _text_acc_filter(), _kb_acc_filter())
            elif action == "fltm":
                if arg in _FILTER_LABELS:
                    _acc_filter["mode"] = arg
                tg.bot.answer_callback_query(call.id,
                    f"Фильтр: {_FILTER_LABELS.get(arg, arg)}")
                _edit_menu(chat_id, msg_id, _text_accs(), _kb_accs(0))
            elif action == "fltrst":
                _acc_filter["mode"] = "all"
                _acc_filter["search"] = ""
                _acc_filter["sort"] = "alias"
                tg.bot.answer_callback_query(call.id, "Фильтр сброшен.")
                _edit_menu(chat_id, msg_id, _text_accs(), _kb_accs(0))
            elif action == "fltq":
                _start_filter_search(uid, chat_id, msg_id, call.id)
            elif action == "srt":
                _edit_menu(chat_id, msg_id, _text_acc_sort(), _kb_acc_sort())
            elif action == "srts":
                if arg in _SORT_LABELS:
                    _acc_filter["sort"] = arg
                tg.bot.answer_callback_query(call.id,
                    f"Сортировка: {_SORT_LABELS.get(arg, arg)}")
                _edit_menu(chat_id, msg_id, _text_accs(), _kb_accs(0))
            elif action == "pwhist":
                alias = _resolve_alias(arg)
                if alias:
                    _edit_menu(chat_id, msg_id,
                               _text_acc(alias, show_pw=True, show_history=True),
                               _kb_acc(alias))
            elif action == "lots":
                _edit_menu(chat_id, msg_id, _text_lots(), _kb_lots())
            elif action == "games":
                _edit_menu(chat_id, msg_id, _text_games(), _kb_games())
            elif action == "game":
                gkey = _resolve_game(arg)
                if not gkey:
                    tg.bot.answer_callback_query(call.id, "Не найдено.")
                    _edit_menu(chat_id, msg_id, _text_games(), _kb_games())
                    return
                _edit_menu(chat_id, msg_id, _text_game(gkey), _kb_game(gkey))
            elif action == "addgame":
                _start_add_game(uid, chat_id, msg_id, call.id)
            elif action == "gaddmain":
                gkey = _resolve_game(arg)
                if gkey:
                    _start_add_game_lot(uid, chat_id, msg_id, call.id,
                                        gkey, "main")
            elif action == "gaddext":
                gkey = _resolve_game(arg)
                if gkey:
                    _start_add_game_lot(uid, chat_id, msg_id, call.id,
                                        gkey, "ext")
            elif action == "gdel":
                gkey = _resolve_game(arg)
                if gkey:
                    _pending_state[uid] = {
                        "step": "confirm_del_game",
                        "ctx": gkey,
                        "chat_id": chat_id,
                        "main_msg_id": msg_id,
                    }
                    kb = tbtypes.InlineKeyboardMarkup(row_width=2)
                    kb.add(
                        tbtypes.InlineKeyboardButton(
                            "✅ Да, удалить", callback_data=f"sr:gdel_yes:{arg}"),
                        tbtypes.InlineKeyboardButton(
                            "❌ Отмена", callback_data=f"sr:game:{arg}"),
                    )
                    try:
                        _edit_menu(chat_id, msg_id,
                                   f"Удалить игру и отвязать её лоты?\n\n"
                                   f"Лоты <b>останутся</b> в lots.json, "
                                   f"но game_key сбросится.",
                                   kb)
                    except Exception:
                        pass
            elif action == "gdel_yes":
                gkey = _resolve_game(arg)
                if gkey and delete_game(gkey):
                    tg.bot.answer_callback_query(call.id, "Удалено.")
                    _edit_menu(chat_id, msg_id, _text_games(), _kb_games())
            elif action == "game_react":
                gkey = _resolve_game(arg)
                if gkey:
                    try:
                        tg.bot.answer_callback_query(
                            call.id, "Обновляю статус лотов...")
                    except Exception:
                        pass
                    try:
                        counters = _update_lot_activation(
                            cardinal, force=True, verbose=False)
                        _log_action("reactivation",
                                    f"Ручная переактивация лотов игры {gkey}",
                                    game=gkey,
                                    activated=counters.get("activated", 0),
                                    deactivated=counters.get("deactivated", 0),
                                    skipped=counters.get("skipped", 0),
                                    failed=counters.get("failed", 0),
                                    user_id=uid)
                        try:
                            tg.bot.send_message(
                                chat_id,
                                f"✅ Статусы лотов обновлены для "
                                f"<b>{_esc(gkey)}</b>:\n"
                                f"  Активировано: "
                                f"<b>{counters.get('activated', 0)}</b>\n"
                                f"  Деактивировано: "
                                f"<b>{counters.get('deactivated', 0)}</b>\n"
                                f"  Пропущено: "
                                f"<b>{counters.get('skipped', 0)}</b>\n"
                                f"  Ошибок: "
                                f"<b>{counters.get('failed', 0)}</b>",
                                parse_mode="HTML")
                        except Exception:
                            pass
                    except Exception as e:
                        LOGGER.warning(
                            "steam_rental: game_react failed for %s: %s",
                            gkey, e, exc_info=True)
                    _edit_menu(chat_id, msg_id,
                               _text_game(gkey), _kb_game(gkey))
            elif action == "gacc":
                gkey = _resolve_game(arg)
                if not gkey:
                    tg.bot.answer_callback_query(call.id, "Не найдено.")
                    _edit_menu(chat_id, msg_id, _text_games(), _kb_games())
                    return
                _start_edit_game_accs(uid, chat_id, msg_id, gkey, call.id)
            elif action == "lot":
                key = _resolve_lot(arg)
                if not key:
                    # Может быть «сирота» — есть ссылка из games.json /
                    # extension_lot_ids, но самой записи в lots.json уже нет.
                    orphan = _resolve_orphan_lot(arg)
                    if orphan is not None:
                        kb_o = tbtypes.InlineKeyboardMarkup(row_width=1)
                        kb_o.add(tbtypes.InlineKeyboardButton(
                            "🗑 Очистить ссылку и выключить на FunPay",
                            callback_data=f"sr:cdorph:{arg}"))
                        kb_o.add(tbtypes.InlineKeyboardButton(
                            "◀️ К играм", callback_data="sr:games"))
                        _edit_menu(
                            chat_id, msg_id,
                            f"<b>⚠️ Лот <code>{_esc(orphan)}</code> — "
                            f"сирота</b>\n\n"
                            "Этот ID есть в ссылках игр (<code>games.json</code>) "
                            "и/или в <code>extension_lot_ids</code> другого лота, "
                            "но самой записи в <code>lots.json</code> нет. "
                            "Плагин им не управляет, но листинг на FunPay "
                            "может продолжать существовать.\n\n"
                            "Нажми кнопку ниже, чтобы выключить лот на FunPay "
                            "и убрать висящие ссылки.",
                            kb_o)
                        return
                    tg.bot.answer_callback_query(call.id, "Не найден.")
                    _edit_menu(chat_id, msg_id, _text_lots(), _kb_lots())
                    return
                _edit_menu(chat_id, msg_id, _text_lot(key), _kb_lot(key))
            elif action == "newlot":
                _start_newlot(uid, chat_id, msg_id, call.id)
            elif action == "newlots":
                _start_bulk_lots(uid, chat_id, msg_id, call.id)
            elif action == "newextlot":
                _start_new_ext_lot(uid, chat_id, msg_id, call.id)
            elif action == "reacttlots":
                try:
                    tg.bot.answer_callback_query(
                        call.id, "Переактивирую лоты на FunPay...")
                except Exception:
                    pass
                try:
                    counters = _update_lot_activation(
                        cardinal, force=True, verbose=True)
                except Exception as e:
                    LOGGER.error(
                        "steam_rental: reacttlots failed", exc_info=True)
                    counters = None
                    err_text = str(e)[:200]
                else:
                    err_text = ""
                if counters is not None:
                    _log_action("reactivation",
                                "Ручная переактивация лотов из TG",
                                activated=counters.get("activated", 0),
                                deactivated=counters.get("deactivated", 0),
                                skipped=counters.get("skipped", 0),
                                failed=counters.get("failed", 0),
                                user_id=uid)

                if counters is None:
                    text = (
                        f"<b>🔁 Переактивация лотов</b>\n\n"
                        f"❌ Ошибка: <code>{_esc(err_text)}</code>")
                else:
                    api_label = counters.get("api_method") or "—"
                    stopped = counters.get("stopped_reason")
                    diag = (
                        f"<b>🔁 Переактивация лотов</b>\n\n"
                        f"📊 <b>Результат</b>\n"
                        f"  ✅ Активировано: <b>{counters['activated']}</b>\n"
                        f"  ⛔ Деактивировано: "
                        f"<b>{counters['deactivated']}</b>\n"
                        f"  ⏭ Пропущено: <b>{counters['skipped']}</b>\n"
                        f"  ⚠ Ошибок: <b>{counters['failed']}</b>\n\n"
                        f"🔧 <b>Диагностика</b>\n"
                        f"  • Всего лотов в базе: "
                        f"<b>{counters.get('total_lots', 0)}</b>\n"
                        f"  • С числовым ID (управляемые): "
                        f"<b>{counters.get('numeric_lots', 0)}</b>\n"
                        f"  • Extension-лотов (пропускаются): "
                        f"<b>{counters.get('ext_lots', 0)}</b>\n"
                        f"  • API метод: <code>{_esc(api_label)}</code>\n"
                    )
                    if stopped:
                        diag += (
                            f"\n⚠ <b>Раннее завершение:</b> "
                            f"<i>{_esc(stopped)}</i>\n"
                        )
                    failures = counters.get("failures") or []
                    if failures:
                        diag += "\n<b>Детали ошибок (первые 5):</b>\n"
                        for f in failures[:5]:
                            diag += (
                                f"  • <code>{_esc(str(f.get('lot')))}</code>: "
                                f"<i>{_esc(str(f.get('error')))}</i>\n"
                            )
                    if (counters.get('total_lots', 0) > 0
                            and counters.get('numeric_lots', 0) == 0):
                        diag += (
                            "\n💡 <b>Подсказка:</b> в базе нет лотов с "
                            "числовыми ID. Бот не может управлять лотами "
                            "по ключевым словам — только по реальным "
                            "FunPay ID. Открой <code>/srental → 🎯 Лоты</code> "
                            "и пересоздай лоты с реальными ID (число из URL "
                            "<code>?id=...</code>).\n"
                        )
                    elif (counters.get('numeric_lots', 0) > 0
                            and counters['activated'] == 0
                            and counters['deactivated'] == 0
                            and counters['failed'] == 0):
                        diag += (
                            "\n💡 <b>Странно:</b> числовые лоты есть, но "
                            "ни одной операции не выполнено. Возможно "
                            "все они — extension-лоты, либо что-то не "
                            "так с итерацией. Смотри логи.\n"
                        )
                    text = diag
                kb_back = tbtypes.InlineKeyboardMarkup()
                kb_back.add(tbtypes.InlineKeyboardButton(
                    "◀️ К лотам", callback_data="sr:lots"))
                _edit_menu(chat_id, msg_id, text, kb_back)
            elif action == "blotclub":
                # колбэк из шага 3/4 массового добавления
                st_ = _pending_state.get(uid)
                if not st_ or st_.get("step") != "blot_club":
                    tg.bot.answer_callback_query(
                        call.id, "Сессия ввода истекла, начни заново.")
                    return
                st_["club_mode"] = (arg == "1")
                st_["step"] = "blot_keys"
                tg.bot.answer_callback_query(
                    call.id,
                    "PC-club: " + ("ВКЛ" if st_["club_mode"] else "ВЫКЛ"))
                _prompt(chat_id, msg_id,
                        "<b>Шаг 4/4.</b> Пришли <b>список ID лотов</b> (или "
                        "ключевых слов) — <b>каждый на отдельной строке</b>.\n\n"
                        "Принимаются также через запятую или ;. Все будут "
                        "созданы с одинаковыми параметрами, которые ты ввёл "
                        "выше.\n\n"
                        "<i>Длительность аренды каждого лота определяется по "
                        "его описанию (<code>#Hours: 2</code> или "
                        "<code>#Time: 2ч</code>).</i>")
            elif action == "edur":
                key = _resolve_lot(arg)
                if key:
                    _start_edit_duration(uid, chat_id, msg_id, key, call.id)
            elif action == "eali":
                key = _resolve_lot(arg)
                if key:
                    _start_edit_aliases(uid, chat_id, msg_id, key, call.id)
            elif action == "noop":
                tg.bot.answer_callback_query(call.id)
            elif action == "apick":
                st_ = _pending_state.get(uid)
                if not st_ or "picker_sel" not in st_:
                    tg.bot.answer_callback_query(
                        call.id, "Сессия ввода истекла.")
                    return
                alias = arg
                sel = list(st_.get("picker_sel") or [])
                low = alias.lower()
                if any(s.lower() == low for s in sel):
                    sel = [s for s in sel if s.lower() != low]
                    tg.bot.answer_callback_query(call.id, f"➖ {alias}")
                else:
                    if not find_account(alias):
                        tg.bot.answer_callback_query(
                            call.id, "Аккаунт не найден.")
                        return
                    sel.append(alias)
                    tg.bot.answer_callback_query(call.id, f"➕ {alias}")
                st_["picker_sel"] = sel
                _show_alias_picker(chat_id, msg_id, st_)
            elif action == "appg":
                st_ = _pending_state.get(uid)
                if not st_ or "picker_sel" not in st_:
                    tg.bot.answer_callback_query(
                        call.id, "Сессия ввода истекла.")
                    return
                try:
                    st_["picker_page"] = int(arg)
                except ValueError:
                    st_["picker_page"] = 0
                tg.bot.answer_callback_query(call.id)
                _show_alias_picker(chat_id, msg_id, st_)
            elif action == "apall":
                st_ = _pending_state.get(uid)
                if not st_ or "picker_sel" not in st_:
                    tg.bot.answer_callback_query(
                        call.id, "Сессия ввода истекла.")
                    return
                st_["picker_sel"] = [a["alias"] for a in list_accounts()
                                     if a.get("alias")]
                tg.bot.answer_callback_query(
                    call.id, f"Выбрано: {len(st_['picker_sel'])}")
                _show_alias_picker(chat_id, msg_id, st_)
            elif action == "apclr":
                st_ = _pending_state.get(uid)
                if not st_ or "picker_sel" not in st_:
                    tg.bot.answer_callback_query(
                        call.id, "Сессия ввода истекла.")
                    return
                st_["picker_sel"] = []
                tg.bot.answer_callback_query(call.id, "Очищено.")
                _show_alias_picker(chat_id, msg_id, st_)
            elif action == "apman":
                st_ = _pending_state.get(uid)
                if not st_ or "picker_sel" not in st_:
                    tg.bot.answer_callback_query(
                        call.id, "Сессия ввода истекла.")
                    return
                tg.bot.answer_callback_query(call.id)
                cur = ", ".join(st_.get("picker_sel") or [])
                _prompt(chat_id, msg_id,
                        "<b>Ручной ввод пула</b>\n\n"
                        "Отправь список alias'ов <b>через запятую</b>.\n\n"
                        f"Текущий выбор: <code>{_esc(cur or '—')}</code>\n\n"
                        "Например: <code>cs1, cs2, cs3</code>\n"
                        "Или отправь <code>-</code> для пустого пула.")
            elif action == "apdone":
                st_ = _pending_state.get(uid)
                if not st_ or "picker_sel" not in st_:
                    tg.bot.answer_callback_query(
                        call.id, "Сессия ввода истекла.")
                    return
                aliases = list(st_.get("picker_sel") or [])
                mode = st_.get("picker_mode")
                tg.bot.answer_callback_query(
                    call.id, f"Готово: {len(aliases)} акк.")
                if mode == "editlot":
                    key = st_.get("ctx", "")
                    with _lock:
                        lots = list_lots()
                        if key not in lots:
                            tg.bot.send_message(chat_id, "Лот не найден.")
                            _pending_state.pop(uid, None)
                            return
                        lots[key]["aliases"] = aliases
                        save_lots(lots)
                    _pending_state.pop(uid, None)
                    tg.bot.send_message(chat_id,
                        f"✅ {_esc(key)}: пул → "
                        f"<code>{_esc(', '.join(aliases) or '—')}</code>",
                        parse_mode="HTML")
                    if msg_id:
                        _edit_menu(chat_id, msg_id,
                                   _text_lot(key), _kb_lot(key))
                elif mode == "newlot":
                    if not aliases:
                        tg.bot.send_message(chat_id,
                            "⚠ Выбери хотя бы один аккаунт или нажми «Ввести "
                            "вручную» → <code>-</code> (не рекомендуется).",
                            parse_mode="HTML")
                        return
                    st_["aliases"] = aliases
                    st_["step"] = "newlot_game"
                    st_.pop("picker_mode", None)
                    st_.pop("picker_sel", None)
                    st_.pop("picker_page", None)
                    _prompt(chat_id, msg_id,
                            "<b>Шаг 3/3.</b> Введи <b>название игры</b> "
                            "(например: <code>Counter Strike 2</code>)\n"
                            "или <code>-</code> чтобы пропустить.\n\n"
                            "<i>⏱ Длительность аренды бот возьмёт из описания "
                            "лота на FunPay по тэгу <code>#Hours: 24</code> "
                            "или <code>#Time: 2ч</code>.</i>")
                elif mode == "blot":
                    st_["aliases"] = aliases
                    st_["step"] = "blot_game"
                    st_.pop("picker_mode", None)
                    st_.pop("picker_sel", None)
                    st_.pop("picker_page", None)
                    _prompt(chat_id, msg_id,
                            "<b>Шаг 2/4.</b> Отправь <b>название игры</b> "
                            "(одна для всех лотов).\n\n"
                            "Или <code>-</code> чтобы пропустить.")
                elif mode == "gameacc":
                    gkey = st_.get("ctx", "")
                    g = get_game(gkey)
                    if not g:
                        tg.bot.send_message(chat_id, "Игра не найдена.")
                        _pending_state.pop(uid, None)
                        return
                    game_name = g.get("name", gkey) or gkey
                    target_lc = {a.lower() for a in aliases}
                    gkey_lc = str(gkey).lower()
                    attached: list[str] = []
                    detached: list[str] = []
                    unknown: list[str] = []
                    moved: list[str] = []
                    with _lock:
                        accs = list_accounts()
                        # Валидация: все ли выбранные алиасы существуют?
                        accs_by_alias = {
                            (a.get("alias") or "").lower(): a for a in accs}
                        for al in aliases:
                            if al.lower() not in accs_by_alias:
                                unknown.append(al)
                        if unknown:
                            tg.bot.send_message(chat_id,
                                "⚠ Не найдены аккаунты: "
                                f"<code>{_esc(', '.join(unknown))}</code>. "
                                "Отмена операции — изменения не сохранены.",
                                parse_mode="HTML")
                            return
                        for acc in accs:
                            alias = acc.get("alias", "")
                            if not alias:
                                continue
                            cur_gk = (acc.get("game_key") or "").strip().lower()
                            if alias.lower() in target_lc:
                                if cur_gk == gkey_lc:
                                    # Уже привязан к этой игре — синхронизируем
                                    # отображаемое имя (могли переименовать игру).
                                    if (acc.get("game") or "") != game_name:
                                        acc["game"] = game_name
                                else:
                                    if cur_gk:
                                        moved.append(alias)
                                    acc["game_key"] = gkey
                                    acc["game"] = game_name
                                    attached.append(alias)
                            else:
                                if cur_gk == gkey_lc:
                                    acc["game_key"] = ""
                                    acc["game"] = ""
                                    detached.append(alias)
                        save_accounts(accs)
                    _pending_state.pop(uid, None)
                    parts = [f"✅ <b>{_esc(game_name)}</b>:"]
                    if attached:
                        parts.append(
                            f"➕ привязано: <b>{len(attached)}</b> "
                            f"(<code>{_esc(', '.join(attached))}</code>)")
                    if detached:
                        parts.append(
                            f"➖ отвязано: <b>{len(detached)}</b> "
                            f"(<code>{_esc(', '.join(detached))}</code>)")
                    if moved:
                        parts.append(
                            f"🔁 перенесено из других игр: "
                            f"<b>{len(moved)}</b> "
                            f"(<code>{_esc(', '.join(moved))}</code>)")
                    if not attached and not detached:
                        parts.append("<i>Изменений нет.</i>")
                    parts.append(
                        "<i>Привязанные аккаунты автоматически попадают "
                        "в пул всех лотов этой игры.</i>")
                    tg.bot.send_message(chat_id, "\n".join(parts),
                                        parse_mode="HTML")
                    if msg_id:
                        _edit_menu(chat_id, msg_id,
                                   _text_game(gkey), _kb_game(gkey))
            elif action == "lgame":
                key = _resolve_lot(arg)
                if key:
                    _start_set_game_lot(uid, chat_id, msg_id, key, call.id)
            elif action == "lext":
                key = _resolve_lot(arg)
                if key:
                    _start_edit_ext(uid, chat_id, msg_id, key, call.id)
            elif action == "lextg":
                key = _resolve_lot(arg)
                if key:
                    _start_edit_ext_games(uid, chat_id, msg_id, key, call.id)
            elif action == "dlot":
                key = _resolve_lot(arg)
                if key:
                    _show_confirm_delete_lot(chat_id, msg_id, key)
            elif action == "cdlot":
                key = _resolve_lot(arg)
                if key:
                    res = delete_lot(key, cardinal=cardinal)
                    if res.get("funpay_tried"):
                        if res.get("funpay_off"):
                            cb_msg = "Удалён + выключен на FunPay."
                        else:
                            cb_msg = "Удалён, но FunPay не отключился (см. логи)."
                    else:
                        cb_msg = "Удалён."
                    tg.bot.answer_callback_query(call.id, cb_msg)
                    _edit_menu(chat_id, msg_id, _text_lots(), _kb_lots())
            elif action == "cdorph":
                # Очистка «сиротской» ссылки: записи в lots.json нет, но
                # она засветилась в games.json / extension_lot_ids и/или
                # как живой листинг на FunPay.
                orphan = _resolve_orphan_lot(arg)
                if orphan is None:
                    tg.bot.answer_callback_query(
                        call.id, "Уже очищено.")
                    _edit_menu(chat_id, msg_id,
                               _text_games(), _kb_games())
                    return
                res = delete_lot(orphan, cardinal=cardinal)
                parts = []
                if res.get("funpay_tried"):
                    parts.append("FunPay: "
                                 + ("выключен" if res.get("funpay_off")
                                    else "ошибка"))
                if res.get("games_cleaned"):
                    parts.append(f"games: {res['games_cleaned']}")
                if res.get("parents_cleaned"):
                    parts.append(f"parents: {res['parents_cleaned']}")
                cb_msg = ("Сирота очищена. " + ", ".join(parts)
                          if parts else "Сирота очищена.")
                tg.bot.answer_callback_query(call.id, cb_msg[:200])
                _edit_menu(chat_id, msg_id, _text_games(), _kb_games())
            # ── v6: Аренда (выдать/активные/завершить/продлить/отменить) ──
            elif action == "rental":
                _edit_menu(chat_id, msg_id, _text_rental(), _kb_rental())
            elif action == "ractive":
                page = int(arg) if arg.isdigit() else 0
                _edit_menu(chat_id, msg_id,
                           _text_rental_active(),
                           _kb_rental_active(page))
            elif action == "rasg":
                rid = _resolve_rental_by_sid(arg)
                if not rid:
                    tg.bot.answer_callback_query(call.id, "Не найдено.")
                    _edit_menu(chat_id, msg_id,
                               _text_rental_active(), _kb_rental_active(0))
                    return
                _show_rental_actions(chat_id, msg_id, rid)
            elif action == "rissue":
                _edit_menu(chat_id, msg_id,
                           "<b>🆕 Выдать аренду</b>\n\n"
                           "Выбери аккаунт, который хочешь выдать:",
                           _kb_rental_pick_account("issue"))
            elif action == "rfinish":
                _edit_menu(chat_id, msg_id,
                           "<b>✅ Завершить аренду</b>\n\n"
                           "Выбери аккаунт (с активной арендой):",
                           _kb_rental_pick_account("finish"))
            elif action == "rextend":
                _edit_menu(chat_id, msg_id,
                           "<b>🔁 Продлить аренду</b>\n\n"
                           "Выбери аккаунт (с активной арендой):",
                           _kb_rental_pick_account("extend"))
            elif action == "rcancel":
                _edit_menu(chat_id, msg_id,
                           "<b>❌ Отменить аренду</b>\n\n"
                           "Выбери аккаунт (с активной арендой):",
                           _kb_rental_pick_account("cancel"))
            elif action == "ris_acc":
                # issue (выдать)
                alias = _resolve_alias(arg)
                if alias:
                    _pending_state[uid] = {
                        "step": "manual_issue_buyer",
                        "ctx": alias,
                        "chat_id": chat_id, "main_msg_id": msg_id,
                    }
                    _prompt(chat_id, msg_id,
                            f"<b>🆕 Выдать {alias}</b>\n\n"
                            f"Отправь <b>buyer_id</b> (число) и "
                            f"<b>buyer_username</b> через пробел.\n"
                            f"Например: <code>12345678 Stevenz123</code>\n\n"
                            f"Или отправь <code>-</code> чтобы выдать "
                            f"без buyer_id (имя покупателя обязательно).",
                            "no_cancel_btn")
                    _add_back_to_rental(msg_id, chat_id)
            elif action == "rfin_acc":
                alias = _resolve_alias(arg)
                if alias:
                    _pending_state[uid] = {
                        "step": "manual_finish",
                        "ctx": alias,
                        "chat_id": chat_id, "main_msg_id": msg_id,
                    }
                    _show_rental_actions(chat_id, msg_id,
                                          {"id": "",
                                           "alias": alias,
                                           "account": alias,
                                           "buyer": "—",
                                           "order_id": ""})
                    _add_back_to_rental(msg_id, chat_id)
            elif action == "rext_acc":
                alias = _resolve_alias(arg)
                if alias:
                    _pending_state[uid] = {
                        "step": "manual_extend_hours",
                        "ctx": alias,
                        "chat_id": chat_id, "main_msg_id": msg_id,
                    }
                    _prompt(chat_id, msg_id,
                            f"<b>🔁 Продлить {alias}</b>\n\n"
                            f"На сколько <b>часов</b> продлить? "
                            f"Можно дробно (<code>1.5</code>).",
                            "no_cancel_btn")
                    _add_back_to_rental(msg_id, chat_id)
            elif action == "rcan_acc":
                alias = _resolve_alias(arg)
                if alias:
                    _pending_state[uid] = {
                        "step": "confirm_cancel",
                        "ctx": alias,
                        "chat_id": chat_id, "main_msg_id": msg_id,
                    }
                    _show_rental_actions(chat_id, msg_id,
                                          {"id": "",
                                           "alias": alias,
                                           "account": alias,
                                           "buyer": "—",
                                           "order_id": ""})
                    _add_back_to_rental(msg_id, chat_id)

            # Direct-actions: вызывается прямо с карточки конкретной аренды,
            # сохраняя контекст (sid → rental → alias). Без выбора аккаунта.
            elif action == "rext_dir":
                rd = _resolve_rental_by_sid(arg)
                if not rd:
                    tg.bot.answer_callback_query(call.id, "Аренда не найдена.")
                    _edit_menu(chat_id, msg_id,
                               _text_rental_active(),
                               _kb_rental_active(0))
                    return
                _pending_state[uid] = {
                    "step": "manual_extend_hours",
                    "ctx": rd["alias"],
                    "chat_id": chat_id, "main_msg_id": msg_id,
                }
                tg.bot.answer_callback_query(call.id)
                _prompt(chat_id, msg_id,
                        f"<b>🔁 Продлить {_esc(rd['alias'])}</b>\n\n"
                        f"Покупатель: <code>{_esc(rd.get('buyer', '?'))}</code>\n"
                        f"Осталось: <b>{rd.get('remaining_str', '?')}</b>\n\n"
                        f"На сколько <b>часов</b> продлить? "
                        f"Можно дробно (<code>1.5</code>).")
            elif action == "rfin_dir":
                rd = _resolve_rental_by_sid(arg)
                if not rd:
                    tg.bot.answer_callback_query(call.id, "Аренда не найдена.")
                    _edit_menu(chat_id, msg_id,
                               _text_rental_active(),
                               _kb_rental_active(0))
                    return
                _pending_state[uid] = {
                    "step": "manual_finish",
                    "ctx": rd["alias"],
                    "chat_id": chat_id, "main_msg_id": msg_id,
                }
                tg.bot.answer_callback_query(call.id)
                _prompt(chat_id, msg_id,
                        f"<b>✅ Завершить аренду {_esc(rd['alias'])}</b>\n\n"
                        f"Покупатель: <code>{_esc(rd.get('buyer', '?'))}</code>\n\n"
                        f"Подтверди: отправь <code>да</code> чтобы завершить "
                        f"немедленно (Steam-сессии будут отозваны, пароль "
                        f"сменён, аккаунт освобождён).\n"
                        f"Любой другой ответ — отмена.")
            elif action == "rcan_dir":
                rd = _resolve_rental_by_sid(arg)
                if not rd:
                    tg.bot.answer_callback_query(call.id, "Аренда не найдена.")
                    _edit_menu(chat_id, msg_id,
                               _text_rental_active(),
                               _kb_rental_active(0))
                    return
                _pending_state[uid] = {
                    "step": "confirm_cancel",
                    "ctx": rd["alias"],
                    "chat_id": chat_id, "main_msg_id": msg_id,
                }
                tg.bot.answer_callback_query(call.id)
                _prompt(chat_id, msg_id,
                        f"<b>❌ Отменить аренду {_esc(rd['alias'])}</b>\n\n"
                        f"Покупатель: <code>{_esc(rd.get('buyer', '?'))}</code>\n"
                        f"Заказ: <code>#{_esc(str(rd.get('order_id') or ''))}</code>\n\n"
                        f"Подтверди: отправь <code>да</code> для отмены "
                        f"(аренда будет завершена, заказ refund'нется через "
                        f"Cardinal). Любой другой ответ — отмена действия.")

            elif action == "status":
                _edit_menu(chat_id, msg_id, _text_status(), _kb_status())
            elif action == "settings":
                _edit_menu(chat_id, msg_id, _text_settings(), _kb_settings())
            elif action == "setnotify":
                _edit_menu(chat_id, msg_id,
                           _text_notify_set(), _kb_notify_set())
            elif action == "setreview":
                _edit_menu(chat_id, msg_id,
                           _text_review_set(), _kb_review_set())
            elif action == "setlimits":
                _edit_menu(chat_id, msg_id,
                           _text_limits_set(), _kb_limits_set())
            elif action == "setsec":
                _edit_menu(chat_id, msg_id,
                           _text_sec_set(), _kb_sec_set())
            elif action == "tgl":
                cfg = get_config()
                if arg in cfg and isinstance(cfg[arg], bool):
                    cfg[arg] = not cfg[arg]
                    save_config(cfg)
                # Возврат пользователя в тот подраздел, где был тогл.
                view = _TGL_RETURN_VIEW.get(arg, "settings")
                if view == "clbset":
                    _edit_menu(chat_id, msg_id, _text_clbset(), _kb_clbset())
                elif view == "setnotify":
                    _edit_menu(chat_id, msg_id,
                               _text_notify_set(), _kb_notify_set())
                elif view == "setreview":
                    _edit_menu(chat_id, msg_id,
                               _text_review_set(), _kb_review_set())
                elif view == "setlimits":
                    _edit_menu(chat_id, msg_id,
                               _text_limits_set(), _kb_limits_set())
                elif view == "setsec":
                    _edit_menu(chat_id, msg_id,
                               _text_sec_set(), _kb_sec_set())
                elif view == "metset":
                    _edit_menu(chat_id, msg_id,
                               _text_metset(), _kb_metset())
                elif view == "dsumset":
                    _edit_menu(chat_id, msg_id,
                               _text_dsumset(), _kb_dsumset())
                else:
                    _edit_menu(chat_id, msg_id,
                               _text_settings(), _kb_settings())
            elif action == "edset":
                _start_edit_setting(uid, chat_id, msg_id, arg, call.id)
            elif action == "templates":
                _edit_menu(chat_id, msg_id,
                           _text_templates(uid), _kb_templates(uid))
            elif action == "tplang":
                if arg in ("ru", "en"):
                    _admin_tpl_lang[uid] = arg
                _edit_menu(chat_id, msg_id,
                           _text_templates(uid), _kb_templates(uid))
            elif action == "edtpl":
                _start_edit_template(uid, chat_id, msg_id, arg, call.id)
            elif action == "rstpl":
                # v2.22: сбрасываем шаблоны выбранного админом языка в
                # дефолты — пишем в JSON-файл, чтобы поведение совпало
                # с обычным редактированием.
                cur_lang = _get_admin_tpl_lang(uid)
                defaults = (dict(_DEFAULT_TEMPLATES_EN) if cur_lang == "en"
                            else dict(_DEFAULT_TEMPLATES))
                _save_templates_file(cur_lang, defaults)
                tg.bot.answer_callback_query(
                    call.id, f"Шаблоны {cur_lang.upper()} сброшены.")
                _edit_menu(chat_id, msg_id,
                           _text_templates(uid), _kb_templates(uid))
            elif action == "stats":
                _edit_menu(chat_id, msg_id, _text_stats(), _kb_stats())
            elif action == "tools":
                _edit_menu(chat_id, msg_id, _text_tools(), _kb_tools())
            elif action == "history":
                _edit_menu(chat_id, msg_id, _text_history(), _kb_history())
            elif action == "hcsv":
                try:
                    csv_data = export_history_csv()
                    doc = io.BytesIO(csv_data)
                    doc.name = "steam_rental_history.csv"
                    tg.bot.send_document(chat_id, doc)
                    tg.bot.answer_callback_query(call.id, "CSV отправлен.")
                except Exception as exc:
                    tg.bot.answer_callback_query(
                        call.id, f"Ошибка: {str(exc)[:50]}")
            elif action == "hclear":
                _save_json(HISTORY_FILE, [])
                tg.bot.answer_callback_query(call.id, "История очищена.")
                _edit_menu(chat_id, msg_id, _text_history(), _kb_history())
            elif action == "actions_tail":
                try:
                    if not os.path.isfile(ACTIONS_LOG_FILE):
                        text = ("<b>📄 Действия</b>\n\n"
                                "Файл actions.log пока пуст.")
                    else:
                        # Читаем хвост ~64 KiB, берём последние 30 строк.
                        with open(ACTIONS_LOG_FILE, "rb") as f:
                            try:
                                f.seek(0, os.SEEK_END)
                                size = f.tell()
                                f.seek(max(0, size - 65536), os.SEEK_SET)
                                tail = f.read().decode("utf-8",
                                                       errors="replace")
                            except Exception:
                                f.seek(0)
                                tail = f.read().decode("utf-8",
                                                       errors="replace")
                        lines = [ln for ln in tail.splitlines() if ln.strip()]
                        lines = lines[-30:]
                        body = "\n".join(_esc(ln) for ln in lines) or "—"
                        text = (f"<b>📄 Действия — последние "
                                f"{len(lines)}</b>\n<pre>{body}</pre>")
                    kb = tbtypes.InlineKeyboardMarkup()
                    kb.add(tbtypes.InlineKeyboardButton(
                        "📎 Скачать целиком",
                        callback_data="sr:actions_file"))
                    kb.add(tbtypes.InlineKeyboardButton(
                        "◀️ Назад", callback_data="sr:history"))
                    _edit_menu(chat_id, msg_id, text, kb)
                    tg.bot.answer_callback_query(call.id)
                except Exception as exc:
                    tg.bot.answer_callback_query(
                        call.id, f"Ошибка: {str(exc)[:50]}")
            elif action == "actions_file":
                try:
                    if not os.path.isfile(ACTIONS_LOG_FILE):
                        tg.bot.answer_callback_query(
                            call.id, "actions.log ещё не создан.")
                    else:
                        with open(ACTIONS_LOG_FILE, "rb") as f:
                            data = f.read()
                        doc = io.BytesIO(data)
                        doc.name = "steam_rental_actions.log"
                        tg.bot.send_document(chat_id, doc)
                        tg.bot.answer_callback_query(call.id, "Лог отправлен.")
                except Exception as exc:
                    tg.bot.answer_callback_query(
                        call.id, f"Ошибка: {str(exc)[:50]}")
            elif action == "bulk":
                _edit_menu(chat_id, msg_id, _text_bulk(), _kb_bulk())
            elif action == "bfreeze":
                cnt = _bulk_freeze_all()
                _log_action("acc_freeze",
                            f"Массовая заморозка из TG: {cnt} аккаунтов",
                            count=cnt, mode="bulk", user_id=uid)
                tg.bot.answer_callback_query(
                    call.id, f"Заморожено: {cnt}")
                _edit_menu(chat_id, msg_id, _text_bulk(), _kb_bulk())
                _update_lot_activation(cardinal)
            elif action == "bunfreeze":
                cnt = _bulk_unfreeze_all()
                _log_action("acc_unfreeze",
                            f"Массовая разморозка из TG: {cnt} аккаунтов",
                            count=cnt, mode="bulk", user_id=uid)
                tg.bot.answer_callback_query(
                    call.id, f"Разморожено: {cnt}")
                _edit_menu(chat_id, msg_id, _text_bulk(), _kb_bulk())
                _update_lot_activation(cardinal)
            elif action == "bchpwd":
                tg.bot.answer_callback_query(call.id, "Запускаю...")
                kb = tbtypes.InlineKeyboardMarkup()
                kb.add(tbtypes.InlineKeyboardButton(
                    "◀️ Назад", callback_data="sr:bulk"))
                _edit_menu(chat_id, msg_id,
                           "⏳ Массовая смена паролей...\n"
                           "Это может занять несколько минут.", kb)
                threading.Thread(
                    target=_bulk_chpwd_thread,
                    args=(chat_id, msg_id), daemon=True).start()
            elif action == "bcheck":
                tg.bot.answer_callback_query(call.id, "Запускаю...")
                kb = tbtypes.InlineKeyboardMarkup()
                kb.add(tbtypes.InlineKeyboardButton(
                    "◀️ Назад", callback_data="sr:bulk"))
                _edit_menu(chat_id, msg_id,
                           "⏳ Проверка аккаунтов...\n"
                           "Это может занять несколько минут.", kb)
                threading.Thread(
                    target=_bulk_check_thread,
                    args=(chat_id, msg_id), daemon=True).start()
            elif action == "events":
                _edit_menu(chat_id, msg_id, _text_events(), _kb_events())
            elif action == "ev_toggle":
                events = _load_events()
                ev = events.setdefault("unclosed_notify", {})
                ev["enabled"] = not ev.get("enabled", True)
                if ev["enabled"] and not ev.get("next_run"):
                    ev["next_run"] = _now() + ev.get("interval_hours", 24) * 3600
                _save_events(events)
                _edit_menu(chat_id, msg_id, _text_events(), _kb_events())
            elif action == "ev_run":
                result = _run_unclosed_notify()
                tg.bot.send_message(chat_id, result, parse_mode="HTML")
                _edit_menu(chat_id, msg_id, _text_events(), _kb_events())
            elif action == "ev_interval":
                _pending_state[uid] = {
                    "step": "ev_interval",
                    "chat_id": chat_id, "main_msg_id": msg_id,
                }
                kb = tbtypes.InlineKeyboardMarkup()
                kb.add(tbtypes.InlineKeyboardButton(
                    "❌ Отмена", callback_data="sr:cancel_input"))
                events = _load_events()
                cur = events.get("unclosed_notify", {}).get("interval_hours", 24)
                tg.bot.edit_message_text(
                    f"Текущий интервал: <b>{cur}</b> ч.\n\n"
                    f"Введи новый интервал (часы, целое число):",
                    chat_id=chat_id, message_id=msg_id,
                    reply_markup=kb, parse_mode="HTML")
            elif action == "instructions":
                # Шлём НОВЫМ сообщением (не edit), чтобы кнопка гарантированно
                # давала визуальный ответ. Раньше edit мог тихо упасть (длина,
                # parse error, message_id истёк) и кнопка казалась «мёртвой».
                try:
                    tg.bot.answer_callback_query(call.id)
                except Exception:
                    pass
                try:
                    tg.bot.send_message(
                        chat_id, INSTRUCTIONS_TEXT,
                        reply_markup=_kb_instructions(),
                        parse_mode="HTML",
                        disable_web_page_preview=True)
                except Exception:
                    LOGGER.warning(
                        "steam_rental: cb 'instructions' send_message failed",
                        exc_info=True)
            # ── Queue management ─────────────────────────────────
            elif action == "queue":
                if arg == "view":
                    queue = _load_queue()
                    if not queue:
                        text = "<b>📋 Очередь</b>\n\nОчередь пуста."
                    else:
                        lines = ["<b>📋 Очередь ожидания</b>\n"]
                        for lk, entries in queue.items():
                            lots_data = _load_json(LOTS_FILE, {})
                            game = lots_data.get(lk, {}).get("game", lk)
                            lines.append(f"\n<b>{game}</b> (лот {lk}):")
                            for i, e in enumerate(entries, 1):
                                notified = " [уведомлен]" if e.get("notified") else ""
                                lines.append(
                                    f"  {i}. {e.get('buyer_username', '?')}{notified}")
                        text = "\n".join(lines)
                    kb = tbtypes.InlineKeyboardMarkup(row_width=2)
                    kb.add(tbtypes.InlineKeyboardButton(
                        "🗑 Очистить очередь", callback_data="sr:queue:clear"))
                    kb.add(tbtypes.InlineKeyboardButton(
                        "🔄 Обновить", callback_data="sr:queue:view"))
                    kb.add(tbtypes.InlineKeyboardButton(
                        "◀️ Назад", callback_data="sr:tools"))
                    _edit_menu(chat_id, msg_id, text, kb)
                elif arg == "clear":
                    _save_queue({})
                    tg.bot.answer_callback_query(call.id, "Очередь очищена.")
                    kb = tbtypes.InlineKeyboardMarkup(row_width=1)
                    kb.add(tbtypes.InlineKeyboardButton(
                        "◀️ Назад", callback_data="sr:tools"))
                    _edit_menu(chat_id, msg_id,
                               "<b>📋 Очередь</b>\n\nОчередь очищена.", kb)
            elif action == "help":
                # Аналогично «instructions» — шлём новым сообщением, чтобы
                # кнопка не могла «залипнуть» из-за edit-ошибок.
                try:
                    tg.bot.answer_callback_query(call.id)
                except Exception:
                    pass
                try:
                    tg.bot.send_message(
                        chat_id, HELP_TEXT,
                        reply_markup=_kb_help(),
                        parse_mode="HTML",
                        disable_web_page_preview=True)
                except Exception:
                    LOGGER.warning(
                        "steam_rental: cb 'help' send_message failed",
                        exc_info=True)
            elif action == "cancel_input":
                _pending_state.pop(uid, None)
                tg.bot.answer_callback_query(call.id, "Отменено.")
                _edit_menu(chat_id, msg_id, _text_main(), _kb_main())

            # ── VAC scan ─────────────────────────────────────────
            elif action == "vacset":
                _edit_menu(chat_id, msg_id, _text_vacset(), _kb_vacset())
            elif action == "vacrun":
                cfg2 = get_config()
                if not (cfg2.get("steam_api_key") or "").strip():
                    tg.bot.answer_callback_query(
                        call.id, "Нужен Steam Web API key.", show_alert=True)
                    return
                tg.bot.answer_callback_query(call.id, "Запускаю...")

                def _run_vac():
                    summary = _vac_scan_iter(cardinal)
                    msg = (
                        f"🚨 VAC scan: проверено <b>{summary['checked']}</b>, "
                        f"найдено бан-аккаунтов <b>{summary['banned']}</b>.\n"
                        + (f"Закрыто аренд: {len(summary['ended'])}\n"
                           if summary['ended'] else "")
                        + (f"Ошибки: {summary['errors']}"
                           if summary['errors'] else ""))
                    try:
                        tg.bot.send_message(chat_id, msg, parse_mode="HTML")
                    except Exception:
                        pass
                threading.Thread(target=_run_vac, daemon=True).start()
                _edit_menu(chat_id, msg_id, _text_vacset(), _kb_vacset())

            # ── PC-club settings / AI ────────────────────────────
            elif action == "clbset":
                _edit_menu(chat_id, msg_id, _text_clbset(), _kb_clbset())
            elif action == "aiprov":
                _edit_menu(chat_id, msg_id, _text_aiprov(), _kb_aiprov())
            elif action == "aiprovset":
                if arg in _AI_PROVIDERS:
                    cfg2 = get_config()
                    cfg2["ai_provider"] = arg
                    save_config(cfg2)
                    tg.bot.answer_callback_query(
                        call.id, f"Провайдер: {_AI_PROVIDER_LABELS.get(arg)}")
                _edit_menu(chat_id, msg_id, _text_clbset(), _kb_clbset())
            elif action == "aimodel":
                _edit_menu(chat_id, msg_id, _text_aimodel(), _kb_aimodel())
            elif action == "aimodelset":
                cfg2 = get_config()
                prov = cfg2.get("ai_provider", "openrouter")
                presets = _AI_PROVIDER_PRESETS.get(prov, [])
                try:
                    idx = int(arg)
                except Exception:
                    idx = -1
                if 0 <= idx < len(presets):
                    _, k_model = _ai_provider_keys(prov)
                    cfg2[k_model] = presets[idx]
                    save_config(cfg2)
                    tg.bot.answer_callback_query(
                        call.id, f"Модель: {presets[idx][:30]}")
                _edit_menu(chat_id, msg_id, _text_clbset(), _kb_clbset())
            elif action == "aitest":
                provider, api_key, model = _ai_get_active()
                if not api_key:
                    tg.bot.answer_callback_query(
                        call.id, "Ключ не задан.", show_alert=True)
                    return
                tg.bot.answer_callback_query(call.id, "Проверяю...")

                def _run_test():
                    ok, err = _ai_validate_key(provider, api_key)
                    if ok:
                        tg.bot.send_message(
                            chat_id,
                            f"✅ Ключ <b>{_AI_PROVIDER_LABELS.get(provider)}"
                            f"</b> валиден.",
                            parse_mode="HTML")
                    else:
                        tg.bot.send_message(
                            chat_id,
                            f"❌ Ключ невалиден: <code>"
                            f"{_esc(err)[:300]}</code>",
                            parse_mode="HTML")
                threading.Thread(target=_run_test, daemon=True).start()

            elif action == "clbs":
                _edit_menu(chat_id, msg_id, _text_clbs(), _kb_clbs())
            elif action == "clbreq":
                _edit_menu(chat_id, msg_id, _text_clbreq(arg),
                           _kb_clbreq(arg))
            elif action == "clbapr":
                ok = _approve_club_request(cardinal, arg, by="manual",
                                            admin_uid=uid)
                tg.bot.answer_callback_query(
                    call.id, "Одобрено." if ok else "Не удалось одобрить.")
                _edit_menu(chat_id, msg_id, _text_clbreq(arg),
                           _kb_clbreq(arg))
            elif action == "clbdec":
                ok = _decline_club_request(cardinal, arg, by="manual",
                                            admin_uid=uid,
                                            reason="manual decision")
                tg.bot.answer_callback_query(
                    call.id, "Отказано." if ok else "Не удалось отказать.")
                _edit_menu(chat_id, msg_id, _text_clbreq(arg),
                           _kb_clbreq(arg))
            # ── v6: Manual review (фото из ПК-клуба, ручное решение) ──
            elif action == "mrapr":
                ok_v, msg_v = _mr_approve(cardinal, arg, admin_uid=uid)
                tg.bot.answer_callback_query(
                    call.id, msg_v[:200], show_alert=not ok_v)
                try:
                    tg.bot.send_message(
                        chat_id,
                        f"📷 Manual review #{_esc(str(arg))}: "
                        f"{'✅' if ok_v else '⚠️'} {_esc(msg_v)}",
                        parse_mode="HTML")
                except Exception:
                    pass
            elif action == "mrdec":
                ok_v, msg_v = _mr_decline(cardinal, arg, admin_uid=uid)
                tg.bot.answer_callback_query(
                    call.id, msg_v[:200], show_alert=not ok_v)
                try:
                    tg.bot.send_message(
                        chat_id,
                        f"📷 Manual review #{_esc(str(arg))}: "
                        f"{'❌' if ok_v else '⚠️'} {_esc(msg_v)}",
                        parse_mode="HTML")
                except Exception:
                    pass
            elif action == "clbret":
                req = _club_get_request(arg)
                if req:
                    _club_update_request(arg, status="awaiting_photo",
                                          photo_url=None,
                                          ai_verdict=None)
                    try:
                        cardinal.send_message(
                            req.get("chat_id"),
                            f"Нужно ещё одно фото. На фото должно быть: "
                            f"интерьер клуба + чат FunPay со мной + "
                            f"код {req.get('code')}.",
                            chat_name=req.get("buyer_username"),
                            interlocutor_id=req.get("buyer_id"),
                            watermark=False)
                    except Exception:
                        pass
                tg.bot.answer_callback_query(call.id, "Запросил новое фото.")
                _edit_menu(chat_id, msg_id, _text_clbreq(arg),
                           _kb_clbreq(arg))
            elif action == "clbwl":
                _edit_menu(chat_id, msg_id, _text_clbwl(), _kb_clbwl(0))
            elif action == "clbwlp":
                try:
                    page = int(arg or 0)
                except Exception:
                    page = 0
                _edit_menu(chat_id, msg_id, _text_clbwl(), _kb_clbwl(page))
            elif action == "clbwldel":
                if _club_remove_from_whitelist(arg):
                    tg.bot.answer_callback_query(call.id, "Удалён.")
                _edit_menu(chat_id, msg_id, _text_clbwl(), _kb_clbwl(0))

            # ── Лот: тоггл PC-club режима ────────────────────────
            elif action == "clubmode":
                key = _resolve_lot(arg)
                if key:
                    lots = list_lots()
                    if key in lots:
                        lots[key]["club_mode"] = not bool(
                            lots[key].get("club_mode"))
                        save_lots(lots)
                        tg.bot.answer_callback_query(
                            call.id,
                            "PC-club режим: "
                            + ("ON" if lots[key]["club_mode"] else "OFF"))
                        _edit_menu(chat_id, msg_id, _text_lot(key),
                                   _kb_lot(key))
                        return
                tg.bot.answer_callback_query(call.id, "Лот не найден.")
            # ── v6: Лот: тоггл Manual review (ручное одобрение фото) ──
            elif action == "mrmode":
                key = _resolve_lot(arg)
                if key:
                    lots = list_lots()
                    if key in lots:
                        lots[key]["manual_review"] = not bool(
                            lots[key].get("manual_review"))
                        save_lots(lots)
                        tg.bot.answer_callback_query(
                            call.id,
                            "Ручная фото-проверка: "
                            + ("ON" if lots[key]["manual_review"]
                               else "OFF"))
                        _edit_menu(chat_id, msg_id, _text_lot(key),
                                   _kb_lot(key))
                        return
                tg.bot.answer_callback_query(call.id, "Лот не найден.")

            # ── v5: Operator panel / extend / stop / switch ──────
            elif action == "op":
                alias = _resolve_alias(arg)
                if not alias:
                    tg.bot.answer_callback_query(
                        call.id, "Аккаунт не найден.")
                    return
                _edit_menu(chat_id, msg_id, _text_op_panel(alias),
                           _kb_op_panel(alias))
            elif action == "ext":
                # arg = "{sid}:{minutes}"
                if ":" not in arg:
                    tg.bot.answer_callback_query(call.id, "Bad arg.")
                    return
                sid_v, _, mins_s = arg.partition(":")
                alias = _resolve_alias(sid_v)
                try:
                    add_min = int(mins_s)
                except Exception:
                    add_min = 0
                if not alias or add_min <= 0:
                    tg.bot.answer_callback_query(
                        call.id, "Не получилось.")
                    return
                new_exp = _extend_rental(
                    alias, add_min, reason="operator_extend")
                if new_exp <= 0:
                    tg.bot.answer_callback_query(
                        call.id, "Аренда уже закрыта.")
                    _edit_menu(chat_id, msg_id, _text_status(),
                               _kb_status())
                    return
                # уведомить покупателя
                acc_n = find_account(alias)
                if acc_n and acc_n.get("rental"):
                    r_n = acc_n["rental"]
                    try:
                        cardinal.send_message(
                            r_n.get("chat_id"),
                            f"➕ Аренда продлена на {add_min} мин. "
                            f"Новое окончание: {_fmt_ts(new_exp)} МСК.",
                            chat_name=r_n.get("buyer_username"),
                            interlocutor_id=r_n.get("buyer_id"),
                            watermark=False)
                    except Exception:
                        LOGGER.debug(
                            "steam_rental: op-extend notify failed",
                            exc_info=True)
                tg.bot.answer_callback_query(
                    call.id, f"➕ Продлено на {add_min} мин.")
                _edit_menu(chat_id, msg_id, _text_op_panel(alias),
                           _kb_op_panel(alias))
            elif action == "stop":
                alias = _resolve_alias(arg)
                if not alias:
                    tg.bot.answer_callback_query(
                        call.id, "Аккаунт не найден.")
                    return
                acc_n = find_account(alias)
                if not acc_n or not acc_n.get("rental"):
                    tg.bot.answer_callback_query(
                        call.id, "Аренда уже закрыта.")
                    _edit_menu(chat_id, msg_id, _text_status(),
                               _kb_status())
                    return
                # Защита от двойного клика: peek на set _stopping_aliases.
                # Сам end_rental тоже атомарно проверит — peek нужен для
                # моментального UX-ответа без запуска лишнего треда.
                if _is_stopping(alias):
                    tg.bot.answer_callback_query(
                        call.id, "⏳ Уже останавливается, подожди…",
                        show_alert=False)
                    return
                tg.bot.answer_callback_query(
                    call.id, "🛑 Прерываю...")
                threading.Thread(
                    target=end_rental,
                    args=(cardinal, alias),
                    kwargs={"reason": "operator_stop"},
                    daemon=True).start()
                _edit_menu(chat_id, msg_id, _text_status(),
                           _kb_status())
            elif action == "switch":
                alias = _resolve_alias(arg)
                if not alias:
                    tg.bot.answer_callback_query(
                        call.id, "Аккаунт не найден.")
                    return
                acc_n = find_account(alias)
                if not acc_n or not acc_n.get("rental"):
                    tg.bot.answer_callback_query(
                        call.id, "Аренда уже закрыта.")
                    return
                r_n = acc_n["rental"]
                remain_min = max(
                    1, (int(r_n.get("expires_at", 0)) - _now()) // 60)
                # Подбираем альтернативу из тех же лотов
                buyer_id_n = r_n.get("buyer_id")
                buyer_un_n = r_n.get("buyer_username", "")
                chat_id_n = r_n.get("chat_id")
                order_id_n = r_n.get("order_id", "")
                # Соберём union pool лотов, где старый alias участвует
                pool_union: list[str] = []
                for _key_l, _lot_l in list_lots().items():
                    if alias.lower() in [
                            x.lower() for x in (_lot_l.get("aliases") or [])]:
                        for _a_l in (_lot_l.get("aliases") or []):
                            if _a_l.lower() != alias.lower() \
                                    and _a_l not in pool_union:
                                pool_union.append(_a_l)
                new_alias = _pick_free_alias(pool_union)
                if not new_alias:
                    tg.bot.answer_callback_query(
                        call.id, "Нет свободных аккаунтов.",
                        show_alert=True)
                    return
                # Закрываем старую (peek для UX, гард в end_rental).
                if _is_stopping(alias):
                    tg.bot.answer_callback_query(
                        call.id, "⏳ Уже останавливается, подожди…",
                        show_alert=False)
                    return
                threading.Thread(
                    target=end_rental,
                    args=(cardinal, alias),
                    kwargs={"reason": "operator_switch"},
                    daemon=True).start()
                # Выдаём новую с оставшимся временем
                ok = deliver_account(
                    cardinal, alias=new_alias,
                    duration_min=int(remain_min),
                    order_id=str(order_id_n),
                    buyer_username=str(buyer_un_n),
                    buyer_id=int(buyer_id_n) if buyer_id_n else 0,
                    chat_id=chat_id_n)
                _metric_inc("operator_switch_total")
                tg.bot.answer_callback_query(
                    call.id,
                    f"🔁 Сменили: {alias} → {new_alias}" if ok else
                    f"Не удалось выдать {new_alias}.")
                _edit_menu(chat_id, msg_id, _text_status(),
                           _kb_status())
            elif action == "opbl":
                alias = _resolve_alias(arg)
                if not alias:
                    tg.bot.answer_callback_query(
                        call.id, "Не найден.")
                    return
                acc_n = find_account(alias)
                if not acc_n or not acc_n.get("rental"):
                    tg.bot.answer_callback_query(
                        call.id, "Аренда закрыта.")
                    return
                r_n = acc_n["rental"]
                added = add_to_blacklist(
                    r_n.get("buyer_id"),
                    r_n.get("buyer_username"),
                    reason="operator_button")
                tg.bot.answer_callback_query(
                    call.id,
                    "🚫 Добавлен в blacklist." if added else
                    "Уже в blacklist.")
            elif action == "blist":
                _edit_menu(chat_id, msg_id, _text_blacklist(),
                           _kb_blacklist())
            elif action == "blrm":
                # arg = sid от username (или строки)
                items = list_blacklist()
                removed = False
                for e in items:
                    label = (e.get("username")
                             or f"id:{e.get('buyer_id')}" or "?")
                    if _sid(str(label)) == arg:
                        removed = remove_from_blacklist(
                            e.get("buyer_id"), e.get("username"))
                        break
                tg.bot.answer_callback_query(
                    call.id,
                    "❌ Удалён." if removed else "Не найден.")
                _edit_menu(chat_id, msg_id, _text_blacklist(),
                           _kb_blacklist())
            elif action == "bladd":
                _pending_state[uid] = {
                    "step": "bl_add", "chat_id": chat_id,
                    "main_msg_id": msg_id}
                _prompt(chat_id, msg_id,
                        "<b>🚫 Добавить в blacklist</b>\n\n"
                        "Пришли <b>username</b> или <b>buyer_id</b> "
                        "одним сообщением. Можно так: "
                        "<code>username</code>, <code>12345</code>, или "
                        "<code>username, 12345</code> "
                        "(оба значения сразу).")
                tg.bot.answer_callback_query(call.id)
            elif action == "metset":
                _edit_menu(chat_id, msg_id, _text_metset(),
                           _kb_metset())
            elif action == "dsumset":
                _edit_menu(chat_id, msg_id, _text_dsumset(),
                           _kb_dsumset())
            elif action == "dsumnow":
                try:
                    _notify_tg(cardinal, _daily_summary_text())
                    tg.bot.answer_callback_query(
                        call.id, "📤 Сводка отправлена.")
                except Exception:
                    tg.bot.answer_callback_query(
                        call.id, "Ошибка, см. логи.")
                _edit_menu(chat_id, msg_id, _text_dsumset(),
                           _kb_dsumset())

            elif action == "test":
                # arg = "REAL" or "FAKE"
                if arg == "REAL":
                    tg.bot.answer_callback_query(call.id)
                    from steam_rental import list_accounts as _list_accs2
                    accs2 = _list_accs2()
                    if not accs2:
                        tg.bot.send_message(
                            chat_id,
                            "❌ <b>Тест невозможен:</b> нет аккаунтов в пуле.",
                            parse_mode="HTML",
                        )
                        return
                    acc2 = accs2[0]
                    alias2 = acc2.get("alias", "?")
                    tg.bot.send_message(
                        chat_id,
                        f"🔄 Тестирую Steam Guard для <code>{alias2}</code>...",
                        parse_mode="HTML",
                    )
                    import threading as _thr2

                    def _worker_real():
                        try:
                            sess = SteamSession(
                                account_name=acc2["account_name"],
                                password=acc2["password"],
                                shared_secret=acc2["shared_secret"],
                                identity_secret=acc2["identity_secret"],
                                steamid=acc2.get("steamid"),
                            )
                            code = sess.get_guard_code()
                            tg.bot.send_message(
                                chat_id,
                                f"✅ <b>Тест пройден!</b>\n\n"
                                f"🎮 Alias: <code>{alias2}</code>\n"
                                f"👤 Login: <code>{acc2['account_name']}</code>\n"
                                f"🔑 Guard код: <code>{code}</code>\n\n"
                                f"Steam Guard работает, плагин готов!",
                                parse_mode="HTML",
                            )
                        except Exception as ex:
                            tg.bot.send_message(
                                chat_id,
                                f"❌ <b>Тест не пройден!</b>\n\n"
                                f"🎮 Alias: <code>{alias2}</code>\n"
                                f"Ошибка: <code>{str(ex)[:200]}</code>\n\n"
                                f"Проверьте:\n"
                                f"• shared_secret / identity_secret\n"
                                f"• Пароль аккаунта\n"
                                f"• Steam Guard привязан",
                                parse_mode="HTML",
                            )

                    _thr2.Thread(target=_worker_real, daemon=True).start()
                    return
                elif arg == "FAKE":
                    tg.bot.answer_callback_query(call.id)
                    try:
                        from steam_rental import SteamSession as _SS
                        sess = _SS(
                            account_name="test_account",
                            password="test_password",
                            shared_secret="SBnfHHGS/SI4TUH5VtCGnFEI+nM=",
                            identity_secret="dGVzdF9pZGVudGl0eV9zZWNyZXQ=",
                            steamid="76561198000000000",
                        )
                        code = sess.get_guard_code()
                        tg.bot.send_message(
                            chat_id,
                            f"✅ <b>Фейковый тест пройден!</b>\n\n"
                            f"🎭 Режим: тестовый (фейковые данные)\n"
                            f"🔑 Guard код: <code>{code}</code>\n"
                            f"📝 Использован тестовый shared_secret\n\n"
                            f"Логика генерации Steam Guard работает корректно!",
                            parse_mode="HTML",
                        )
                    except Exception as ex:
                        tg.bot.send_message(
                            chat_id,
                            f"❌ <b>Фейковый тест не пройден!</b>\n"
                            f"Ошибка: <code>{str(ex)[:200]}</code>",
                            parse_mode="HTML",
                        )
                    return

            elif action == "rp":
                _handle_rp_callback(chat_id, msg_id, arg, call)

            else:
                tg.bot.answer_callback_query(call.id, "Неизвестное действие.")
                return
            tg.bot.answer_callback_query(call.id)
        except Exception:
            try:
                _user_id = getattr(call.from_user, "id", "?") \
                    if hasattr(call, "from_user") else "?"
            except Exception:
                _user_id = "?"
            LOGGER.error(
                "steam_rental: callback %r crashed "
                "(user=%s chat=%s msg=%s)",
                data, _user_id, chat_id, msg_id, exc_info=True)
            try:
                tg.bot.answer_callback_query(call.id, "Ошибка, см. логи.")
            except Exception:
                pass

    # ───── Confirm-удаление ──────────────────────────────────────────────
    def _show_confirm_delete_acc(chat_id, msg_id, alias):
        sid = _sid(alias)
        kb = tbtypes.InlineKeyboardMarkup(row_width=2)
        kb.add(
            tbtypes.InlineKeyboardButton(
                "✅ Удалить", callback_data=f"sr:cdel:{sid}"),
            tbtypes.InlineKeyboardButton(
                "❌ Отмена", callback_data=f"sr:acc:{sid}"),
        )
        _edit_menu(chat_id, msg_id,
                   f"Удалить аккаунт <b>{_esc(alias)}</b>?\n\n"
                   f"Активная аренда (если есть) будет сброшена.", kb)

    def _show_confirm_delete_lot(chat_id, msg_id, key):
        sid = _sid(key)
        kb = tbtypes.InlineKeyboardMarkup(row_width=2)
        kb.add(
            tbtypes.InlineKeyboardButton(
                "✅ Удалить", callback_data=f"sr:cdlot:{sid}"),
            tbtypes.InlineKeyboardButton(
                "❌ Отмена", callback_data=f"sr:lot:{sid}"),
        )
        _edit_menu(chat_id, msg_id,
                   f"Удалить настройку лота <code>{_esc(key)}</code>?", kb)

    # ───── Long-running ops ──────────────────────────────────────────────
    def _send_guard_code(chat_id, alias):
        acc = find_account(alias)
        if not acc:
            tg.bot.send_message(chat_id, "Не найден.")
            return
        try:
            from steampy import guard
            code = guard.generate_one_time_code(acc["shared_secret"])
            tg.bot.send_message(
                chat_id,
                f"🛡 <b>{_esc(alias)}</b>: <code>{code}</code>",
                parse_mode="HTML")
        except Exception as exc:
            tg.bot.send_message(chat_id, f"Ошибка: {exc}")

    def _start_chpwd(chat_id, msg_id, alias, cb_id):
        tg.bot.answer_callback_query(cb_id, "Запускаю...")
        sid = _sid(alias)
        kb = tbtypes.InlineKeyboardMarkup()
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ К аккаунту", callback_data=f"sr:acc:{sid}"))
        _edit_menu(chat_id, msg_id,
                   f"⏳ Меняю пароль для <b>{_esc(alias)}</b>...\n\n"
                   f"Логин → отзыв сессий → mobile confirm → новый пароль. "
                   f"Это займёт ~15–30 секунд.", kb)
        def _run():
            try:
                r = end_rental(cardinal, alias, reason="manual_chpwd")
                ok_pw = r.get("changed")
                ok_rv = r.get("revoked")
                errs = r.get("errors") or []
                status = ("✅ Готово" if ok_pw and not errs else
                          ("⚠️ С ошибками" if errs else "🆗 Завершено"))
                txt = (f"<b>{status}</b> для <b>{_esc(alias)}</b>\n\n"
                       f"Revoke sessions: <code>{ok_rv}</code>\n"
                       f"Change password: <code>{ok_pw}</code>\n")
                if errs:
                    txt += f"\n<b>Ошибки:</b>\n<code>{_esc('; '.join(map(str, errs))[:1500])}</code>"
                _edit_menu(chat_id, msg_id, txt, kb)
            except Exception as exc:
                _edit_menu(chat_id, msg_id,
                           f"❌ Ошибка: <code>{_esc(str(exc))}</code>", kb)
        threading.Thread(target=_run, daemon=True).start()

    def _start_revoke(chat_id, msg_id, alias, cb_id):
        tg.bot.answer_callback_query(cb_id, "Отзываю сессии...")
        sid = _sid(alias)
        kb = tbtypes.InlineKeyboardMarkup()
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ К аккаунту", callback_data=f"sr:acc:{sid}"))
        _edit_menu(chat_id, msg_id,
                   f"⏳ Отзываю сессии для <b>{_esc(alias)}</b>...", kb)
        def _run():
            acc = find_account(alias)
            if not acc:
                _edit_menu(chat_id, msg_id, "Аккаунт не найден.", kb)
                return
            try:
                s = SteamSession(acc["account_name"], acc["password"],
                                  acc["shared_secret"], acc["identity_secret"],
                                  acc.get("steamid"))
                s.login()
                _track_login_result(alias, True)
                ok = s.revoke_all_other_sessions()
                _edit_menu(chat_id, msg_id,
                           f"{'✅' if ok else '⚠️'} Revoke <b>{_esc(alias)}</b>: "
                           f"<code>{ok}</code>", kb)
            except Exception as exc:
                _track_login_result(alias, False)
                _edit_menu(chat_id, msg_id,
                           f"❌ Ошибка: <code>{_esc(str(exc))}</code>", kb)
        threading.Thread(target=_run, daemon=True).start()

    # ───── Wizards (state-machine) ───────────────────────────────────────
    def _prompt(chat_id, msg_id, text, mode=None):
        """Показать prompt пользователю. Гарантированно дойдёт до экрана:
        пробует edit_message_text, при провале — отправляет новым
        сообщением. Иначе при тихом провале edit'а юзер будет видеть
        старый экран, а _pending_state уже выставлен — это и есть
        классический сюрприз 'плагин ничего не показал, но команды
        принимаются'.

        mode:
          * None              — стандартный prompt с кнопкой "❌ Отмена".
          * "no_cancel_btn"   — без кнопки отмены (используется когда
                                 поверх будет добавлена кнопка "К арендам"
                                 через _add_back_to_rental).
        """
        kb = tbtypes.InlineKeyboardMarkup()
        if mode != "no_cancel_btn":
            kb.add(tbtypes.InlineKeyboardButton(
                "❌ Отмена", callback_data="sr:cancel_input"))
        edited_ok = False
        if msg_id:
            try:
                tg.bot.edit_message_text(text, chat_id=chat_id,
                                         message_id=msg_id,
                                         reply_markup=kb,
                                         parse_mode="HTML",
                                         disable_web_page_preview=True)
                edited_ok = True
            except Exception as _ex:
                if "not modified" in str(_ex).lower():
                    # Текст совпал — пользователь уже видит то что нужно,
                    # это норма.
                    edited_ok = True
                else:
                    LOGGER.warning(
                        "steam_rental: _prompt edit failed (chat=%s "
                        "msg=%s text_len=%s): %s",
                        chat_id, msg_id, len(text or ""), _ex)
        if not edited_ok:
            try:
                tg.bot.send_message(chat_id, text,
                                    reply_markup=kb,
                                    parse_mode="HTML",
                                    disable_web_page_preview=True)
            except Exception as _ex:
                LOGGER.error(
                    "steam_rental: _prompt send_message (HTML) "
                    "failed: %s", _ex, exc_info=True)
                # Последний шанс — отправить как plain text без HTML.
                try:
                    tg.bot.send_message(chat_id, text,
                                        reply_markup=kb,
                                        disable_web_page_preview=True)
                except Exception:
                    LOGGER.error(
                        "steam_rental: _prompt plain fallback failed",
                        exc_info=True)

    def _add_back_to_rental(msg_id: int | None, chat_id: int) -> None:
        """Добавляет кнопку «К арендам» к сообщению prompt (если есть msg_id)."""
        if not msg_id:
            return
        try:
            cur = tg.bot.get_message(chat_id, msg_id)
        except Exception:
            return
        kb = cur.reply_markup or tbtypes.InlineKeyboardMarkup()
        if isinstance(kb, tbtypes.InlineKeyboardMarkup):
            has_back = False
            try:
                for row in kb.keyboard:
                    for btn in row:
                        if btn.callback_data == "sr:rental":
                            has_back = True
                            break
            except Exception:
                pass
            if not has_back:
                kb.add(tbtypes.InlineKeyboardButton(
                    "◀️ К арендам", callback_data="sr:rental"))
            try:
                tg.bot.edit_message_reply_markup(
                    chat_id, msg_id, reply_markup=kb)
            except Exception:
                pass

    def _show_rental_actions(chat_id: int, msg_id: int | None,
                              rid: dict[str, Any]) -> None:
        """Показать карточку rental'а с inline-кнопками действий.
        Гарантированно показывается на экране: edit + fallback send."""
        def _show(txt: str, kb: tbtypes.InlineKeyboardMarkup) -> None:
            edited_ok = False
            if msg_id:
                try:
                    tg.bot.edit_message_text(
                        txt, chat_id=chat_id, message_id=msg_id,
                        reply_markup=kb, parse_mode="HTML",
                        disable_web_page_preview=True)
                    edited_ok = True
                except Exception as _ex:
                    if "not modified" in str(_ex).lower():
                        edited_ok = True
                    else:
                        LOGGER.warning(
                            "steam_rental: rental_actions edit failed "
                            "(chat=%s msg=%s): %s",
                            chat_id, msg_id, _ex)
            if not edited_ok:
                try:
                    tg.bot.send_message(
                        chat_id, txt, reply_markup=kb,
                        parse_mode="HTML",
                        disable_web_page_preview=True)
                except Exception:
                    LOGGER.error(
                        "steam_rental: rental_actions send failed",
                        exc_info=True)
                    try:
                        tg.bot.send_message(chat_id, txt, reply_markup=kb)
                    except Exception:
                        LOGGER.error(
                            "steam_rental: rental_actions plain failed",
                            exc_info=True)

        alias = rid.get("alias", "")
        a = find_account(alias) if alias else None
        r = (a or {}).get("rental") if a else None
        if not r:
            txt = f"<b>📭 Аренды нет</b> на <code>{_esc(alias or '?')}</code>."
            kb = tbtypes.InlineKeyboardMarkup()
            kb.add(tbtypes.InlineKeyboardButton(
                "🆕 Выдать новую", callback_data="sr:rissue"))
            kb.add(tbtypes.InlineKeyboardButton(
                "◀️ К арендам", callback_data="sr:rental"))
            _show(txt, kb)
            return
        # Текст
        buyer = r.get("buyer_username", "?")
        order = r.get("order_id", "")
        exp = int(r.get("expires_at", 0) or 0)
        rem_sec = max(0, exp - _now())
        rem_str = _human_minutes(rem_sec // 60)
        exp_str = datetime.datetime.fromtimestamp(exp, tz=_MSK_TZ).strftime(
            "%d.%m %H:%M")
        txt = (
            f"<b>📋 Аренда {_esc(alias)}</b>\n\n"
            f"Покупатель: <b>{_esc(buyer)}</b>\n"
            f"Заказ: <code>#{_esc(str(order))}</code>\n"
            f"Осталось: <b>{rem_str}</b>\n"
            f"Истекает: <code>{exp_str}</code>"
        )
        # SID конкретной аренды — чтобы кнопки действовали ТОЛЬКО на неё,
        # а не открывали выбор аккаунта заново.
        rsid = _sid(str(rid.get("id") or alias))
        kb = tbtypes.InlineKeyboardMarkup(row_width=2)
        kb.add(
            tbtypes.InlineKeyboardButton(
                "🔁 Продлить", callback_data=f"sr:rext_dir:{rsid}"),
            tbtypes.InlineKeyboardButton(
                "✅ Завершить", callback_data=f"sr:rfin_dir:{rsid}"),
        )
        kb.add(tbtypes.InlineKeyboardButton(
            "❌ Отменить", callback_data=f"sr:rcan_dir:{rsid}"))
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ К арендам", callback_data="sr:ractive"))
        _show(txt, kb)

    def _start_add(uid, chat_id, msg_id, cb_id):
        _pending_state[uid] = {
            "step": "add_alias", "chat_id": chat_id, "main_msg_id": msg_id}
        tg.bot.answer_callback_query(cb_id)
        _prompt(chat_id, msg_id,
                "<b>Шаг 1/4.</b> Отправь <b>alias</b> для аккаунта — "
                "короткое имя для команды <code>!код</code>.\n\n"
                "Только латиница, цифры, <code>_</code> и <code>-</code>, "
                "до 16 символов. Например: <code>cs1</code>, <code>dota_2</code>.")

    def _create_test_account(uid, chat_id, msg_id, cb_id):
        """Создаёт пустой ТЕСТОВЫЙ аккаунт (без maFile/секретов) одной кнопкой.

        Нужен для проверки сквозного потока выдачи: можно купить лот и
        убедиться, что креды приходят и аренда создаётся, не трогая реальные
        Steam-аккаунты. Для такого акка Steam-логин, смена пароля и Guard
        НЕ выполняются (см. _cmd_guard_code / end_rental / _check_accounts_thread).
        """
        n = 1
        while find_account(f"test{n}") is not None:
            n += 1
        alias = f"test{n}"
        acc = {
            "alias": alias,
            "account_name": f"test_login_{n}",
            "password": _gen_password(12),
            "shared_secret": "",
            "identity_secret": "",
            "steamid": None,
            "frozen": False,
            "game": "TEST",
            "login_failures": 0,
            "cost": 0.0,
            "test": True,
        }
        upsert_account(acc)
        try:
            tg.bot.answer_callback_query(cb_id, "Тестовый аккаунт создан")
        except Exception:
            pass
        kb = tbtypes.InlineKeyboardMarkup()
        kb.add(tbtypes.InlineKeyboardButton(
            "👁 К аккаунту", callback_data=f"sr:acc:{_sid(alias)}"))
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ К аккаунтам", callback_data="sr:accs:0"))
        _edit_menu(
            chat_id, msg_id,
            f"🧪 <b>Тестовый аккаунт создан: {_esc(alias)}</b>\n\n"
            f"Логин: <code>{_esc(acc['account_name'])}</code>\n"
            f"Пароль: <code>{_esc(acc['password'])}</code>\n\n"
            "Это пустышка без Steam-данных. Логин в Steam, смена пароля и "
            "Guard для него <b>не выполняются</b> — выдачу можно проверить "
            "end-to-end без риска для реальных аккаунтов.\n\n"
            "<b>Как протестировать выдачу:</b>\n"
            f"1. Открой нужный лот → 👥 Пул аккаунтов → добавь "
            f"<code>{_esc(alias)}</code>.\n"
            "2. Купи этот лот сам.\n"
            "3. Проверь, что пришли креды и аренда появилась.\n"
            "4. Команда <code>!код</code> вернёт тестовый код (без Steam).\n"
            "5. По истечении/возврату аренда снимется без обращения к Steam.\n\n"
            "Когда закончишь — просто удали этот аккаунт.",
            kb)

    def _start_bulk_import(uid, chat_id, msg_id, cb_id):
        _pending_state[uid] = {
            "step": "bulk_import_zip", "chat_id": chat_id,
            "main_msg_id": msg_id}
        tg.bot.answer_callback_query(cb_id)
        _prompt(chat_id, msg_id,
                "<b>📥 Массовый импорт аккаунтов</b>\n\n"
                "Отправь <b>.zip</b>-архив, в котором лежат:\n"
                "• один или несколько <code>.maFile</code> (любое расширение — .maFile / .json)\n"
                "• файл <code>passwords.txt</code> в формате:\n"
                "<code>login:password</code> (по строке на аккаунт).\n\n"
                "• Опционально — <code>aliases.txt</code> формата "
                "<code>login:alias</code>, иначе alias будет равен логину "
                "(обрезанному до 16 символов).\n"
                "• Опционально — <code>costs.txt</code> формата "
                "<code>login:cost</code> (стоимость аккаунта в ₽).\n\n"
                "Импорт проверит логин каждого аккаунта и покажет отчёт.")

    def _start_filter_search(uid, chat_id, msg_id, cb_id):
        _pending_state[uid] = {
            "step": "acc_search", "chat_id": chat_id,
            "main_msg_id": msg_id}
        tg.bot.answer_callback_query(cb_id)
        cur = _acc_filter.get("search", "")
        _prompt(chat_id, msg_id,
                "<b>🔤 Поиск по аккаунтам</b>\n\n"
                f"Сейчас: <code>{_esc(cur) if cur else '—'}</code>\n\n"
                "Отправь подстроку (поиск по алиасу, логину Steam и названию игры).\n"
                "Отправь <code>-</code> чтобы очистить поиск.")

    def _start_bulk_lots(uid, chat_id, msg_id, cb_id):
        # Длительность теперь берётся из описания лота (#Hours: / #Time: /
        # «N часов»). Сразу переходим к выбору пула алиасов через picker.
        _pending_state[uid] = {
            "step": "blot_aliases",
            "chat_id": chat_id,
            "main_msg_id": msg_id,
            "picker_mode": "blot",
            "picker_sel": [],
            "picker_page": 0,
        }
        tg.bot.answer_callback_query(cb_id)
        if msg_id:
            _show_alias_picker(chat_id, msg_id, _pending_state[uid])
        else:
            _prompt(chat_id, msg_id,
                    "<b>📥 Массовое добавление лотов</b>\n\n"
                    "<b>Шаг 1/4.</b> Отправь <b>общий пул алиасов</b> "
                    "через запятую (используется для ВСЕХ создаваемых лотов).\n\n"
                    "Например: <code>cs1, cs2, cs3</code>\n"
                    "Или <code>-</code> если хочешь создать лоты с пустым "
                    "пулом (привяжешь акки позже).\n\n"
                    "<i>Длительность аренды берётся из описания лота "
                    "(<code>#Hours: 2</code> или <code>#Time: 2ч</code>).</i>")

    def _start_newlot(uid, chat_id, msg_id, cb_id):
        _pending_state[uid] = {
            "step": "newlot_key", "chat_id": chat_id, "main_msg_id": msg_id}
        tg.bot.answer_callback_query(cb_id)
        _prompt(chat_id, msg_id,
                "<b>Шаг 1/3.</b> Отправь <b>ID лота</b> FunPay (число из URL "
                "<code>?id=12345678</code>) <i>или</i> <b>ключевое слово</b> "
                "из названия лота.\n\n"
                "<i>Длительность аренды бот возьмёт из описания лота "
                "на FunPay по тэгу <code>#Hours: 24</code> "
                "или <code>#Time: 2ч</code>. Без тэга бот выдачу не "
                "произведёт.</i>")

    def _start_new_ext_lot(uid, chat_id, msg_id, cb_id):
        """Wizard для создания лота-продления (extension)."""
        _pending_state[uid] = {
            "step": "newext_key", "chat_id": chat_id, "main_msg_id": msg_id}
        tg.bot.answer_callback_query(cb_id)
        _prompt(chat_id, msg_id,
                "<b>🔄 Создание лота для продления</b>\n\n"
                "<b>Шаг 1/2.</b> Отправь <b>ID лота</b> FunPay "
                "(число из URL <code>?id=12345678</code>) <i>или</i> "
                "<b>ключевое слово</b> из названия лота.\n\n"
                "<i>Это будет «extension»-лот: при его покупке активная "
                "аренда покупателя автоматически продлевается. Срок "
                "продления берётся из описания лота на FunPay по тэгу "
                "<code>#Hours: 1</code> или <code>#Time: 30m</code>; "
                "если тэга нет — используется дефолт "
                "<code>extension_default_minutes</code> (1 час).</i>")

    def _start_add_game(uid, chat_id, msg_id, cb_id):
        """Wizard: добавить игру. На шаге 1 ждём имя, на 2 — main-лоты,
        на 3 — extension-лоты."""
        _pending_state[uid] = {
            "step": "addgame_name",
            "chat_id": chat_id, "main_msg_id": msg_id,
        }
        try:
            tg.bot.answer_callback_query(cb_id)
        except Exception:
            pass
        _prompt(chat_id, msg_id,
                "<b>🎮 Добавление игры</b>\n\n"
                "<b>Шаг 1/3.</b> Отправь <b>название игры</b> "
                "(например, <code>GTA 5</code> или <code>Counter-Strike 2</code>).\n\n"
                "<i>Название должно встречаться в названиях лотов FunPay "
                "для авто-матчинга.</i>")

    def _start_add_game_lot(uid, chat_id, msg_id, cb_id, gkey: str,
                            kind: str):
        """Wizard: добавить main/ext лот к существующей игре."""
        _pending_state[uid] = {
            "step": f"addgame_{kind}_lot",
            "ctx": gkey,
            "chat_id": chat_id, "main_msg_id": msg_id,
        }
        try:
            tg.bot.answer_callback_query(cb_id)
        except Exception:
            pass
        kind_label = "Main" if kind == "main" else "Extension"
        _prompt(chat_id, msg_id,
                f"<b>🎮 {gkey}: добавить {kind_label} лот</b>\n\n"
                f"Отправь <b>ID лота</b> FunPay "
                f"(число из URL <code>?id=12345678</code>).\n\n"
                f"<i>Бот попробует подтянуть название/subcategory/category "
                f"через <code>get_lot_fields</code>.</i>")

    def _start_edit_duration(uid, chat_id, msg_id, key, cb_id):
        _pending_state[uid] = {
            "step": "editlot_duration", "ctx": key,
            "chat_id": chat_id, "main_msg_id": msg_id}
        tg.bot.answer_callback_query(cb_id)
        _prompt(chat_id, msg_id,
                f"Отправь новую <b>длительность</b> аренды для лота "
                f"<code>{_esc(key)}</code>.\n\n"
                f"Форматы: <code>60</code> (минуты), <code>2h</code>, "
                f"<code>1d</code>, <code>1w</code>, <code>1mo</code>.\n"
                f"Можно с кириллицей: <code>2ч</code>, <code>1д</code>, "
                f"<code>1мес</code>.")

    # ── Интерактивный выбор алиасов (пул аккаунтов лота) ────────────
    _ALIASES_PER_PAGE = 20

    def _alias_picker_text(st: dict) -> str:
        mode = st.get("picker_mode", "editlot")
        if mode == "editlot":
            key = st.get("ctx", "")
            title = (f"<b>📋 Пул лота</b> <code>{_esc(key)}</code>\n\n"
                     "Отметь аккаунты, которые войдут в пул.")
        elif mode == "newlot":
            title = ("<b>Шаг 2/3.</b> <b>Выбор пула аккаунтов</b>\n\n"
                     "Отметь аккаунты для нового лота.\n\n"
                     "<i>Длительность берётся из описания лота "
                     "(<code>#Hours: 2</code> или <code>#Time: 2ч</code>).</i>")
        elif mode == "blot":
            title = ("<b>Шаг 1/4.</b> <b>Общий пул для лотов</b>\n\n"
                     "Отметь аккаунты, которые войдут во все создаваемые лоты "
                     "(можно оставить пустым).\n\n"
                     "<i>Длительность аренды берётся из описания лота "
                     "(<code>#Hours: 2</code> / <code>#Time: 2ч</code>).</i>")
        elif mode == "gameacc":
            gkey = st.get("ctx", "")
            g = get_game(gkey) or {}
            gname = g.get("name", gkey) or gkey
            title = (f"<b>👥 Аккаунты игры</b> "
                     f"<b>{_esc(gname)}</b>\n\n"
                     "Отметь аккаунты, которые принадлежат этой игре. "
                     "Они автоматически попадут в пул "
                     "<b>всех лотов</b> этой игры — добавлять "
                     "в каждый лот отдельно не нужно.\n\n"
                     "<i>Снятая галочка = аккаунт отвязан от игры.</i>")
        else:
            title = "<b>Выбор аккаунтов</b>"
        sel = st.get("picker_sel") or []
        n_total = len([a for a in list_accounts() if a.get("alias")])
        return (f"{title}\n\n"
                f"Выбрано: <b>{len(sel)}</b> из {n_total}\n"
                f"<code>{_esc(', '.join(sel) or '—')}</code>")

    def _alias_picker_kb(st: dict) -> "tbtypes.InlineKeyboardMarkup":
        sel_lower = {s.lower() for s in (st.get("picker_sel") or [])}
        page = int(st.get("picker_page", 0))
        accs = sorted(
            [a for a in list_accounts() if a.get("alias")],
            key=lambda a: a.get("alias", "").lower())
        total = len(accs)
        pages = max(1, (total + _ALIASES_PER_PAGE - 1) // _ALIASES_PER_PAGE)
        page = max(0, min(page, pages - 1))
        st["picker_page"] = page
        start = page * _ALIASES_PER_PAGE
        chunk = accs[start:start + _ALIASES_PER_PAGE]

        kb = tbtypes.InlineKeyboardMarkup(row_width=2)
        for acc in chunk:
            alias = acc.get("alias", "")
            mark = "✅" if alias.lower() in sel_lower else "⬜"
            frozen = " ❄️" if acc.get("frozen") else ""
            rent = " 🔒" if acc.get("rental") else ""
            label = f"{mark} {alias}{frozen}{rent}"
            kb.add(tbtypes.InlineKeyboardButton(
                label[:60], callback_data=f"sr:apick:{alias}"))
        if pages > 1:
            nav = []
            if page > 0:
                nav.append(tbtypes.InlineKeyboardButton(
                    "◀️", callback_data=f"sr:appg:{page - 1}"))
            nav.append(tbtypes.InlineKeyboardButton(
                f"{page + 1}/{pages}", callback_data="sr:noop"))
            if page < pages - 1:
                nav.append(tbtypes.InlineKeyboardButton(
                    "▶️", callback_data=f"sr:appg:{page + 1}"))
            kb.row(*nav)
        kb.row(
            tbtypes.InlineKeyboardButton(
                "✅ Все", callback_data="sr:apall"),
            tbtypes.InlineKeyboardButton(
                "⬜ Очистить", callback_data="sr:apclr"),
        )
        kb.row(
            tbtypes.InlineKeyboardButton(
                "✏️ Ввести вручную", callback_data="sr:apman"),
        )
        kb.row(
            tbtypes.InlineKeyboardButton(
                "💾 Готово", callback_data="sr:apdone"),
            tbtypes.InlineKeyboardButton(
                "❌ Отмена", callback_data="sr:cancel_input"),
        )
        return kb

    def _show_alias_picker(chat_id, msg_id, st):
        _edit_menu(chat_id, msg_id,
                   _alias_picker_text(st), _alias_picker_kb(st))

    def _start_edit_aliases(uid, chat_id, msg_id, key, cb_id):
        cur = list(list_lots().get(key, {}).get("aliases", []))
        _pending_state[uid] = {
            "step": "editlot_aliases", "ctx": key,
            "chat_id": chat_id, "main_msg_id": msg_id,
            "picker_mode": "editlot",
            "picker_sel": cur,
            "picker_page": 0,
        }
        tg.bot.answer_callback_query(cb_id)
        _show_alias_picker(chat_id, msg_id, _pending_state[uid])

    def _start_edit_game_accs(uid, chat_id, msg_id, gkey, cb_id):
        """Picker: какие аккаунты привязаны к игре (по `account.game_key`).

        Изначальный выбор = все акки с `acc.game_key == gkey`.
        На «💾 Готово»:
          • выбранным алиасам ставим `game_key=gkey` и `game=<имя игры>`;
          • тем, кто был в выборке (game_key=gkey), но был снят — чистим
            `game_key`/`game`.
        Привязка автоматически расширяет пул всех лотов этой игры
        (см. `_combined_lot_pool`)."""
        g = get_game(gkey)
        if not g:
            try:
                tg.bot.answer_callback_query(cb_id, "Игра не найдена.")
            except Exception:
                pass
            return
        cur = []
        gkey_lc = str(gkey).lower()
        for a in list_accounts():
            if (a.get("game_key") or "").strip().lower() == gkey_lc:
                al = a.get("alias", "")
                if al:
                    cur.append(al)
        _pending_state[uid] = {
            "step": "game_acc_pool", "ctx": gkey,
            "chat_id": chat_id, "main_msg_id": msg_id,
            "picker_mode": "gameacc",
            "picker_sel": cur,
            "picker_page": 0,
        }
        try:
            tg.bot.answer_callback_query(cb_id)
        except Exception:
            pass
        _show_alias_picker(chat_id, msg_id, _pending_state[uid])

    def _start_set_game_lot(uid, chat_id, msg_id, key, cb_id):
        _pending_state[uid] = {
            "step": "editlot_game", "ctx": key,
            "chat_id": chat_id, "main_msg_id": msg_id}
        tg.bot.answer_callback_query(cb_id)
        cur = list_lots().get(key, {}).get("game", "")
        _prompt(chat_id, msg_id,
                f"Отправь название <b>игры</b> для лота "
                f"<code>{_esc(key)}</code>.\n\n"
                f"Сейчас: <code>{_esc(cur or '—')}</code>\n\n"
                f"Используется в шаблонах как <code>{{game}}</code>.")

    def _start_edit_ext(uid, chat_id, msg_id, key, cb_id):
        _pending_state[uid] = {
            "step": "editlot_ext", "ctx": key,
            "chat_id": chat_id, "main_msg_id": msg_id}
        tg.bot.answer_callback_query(cb_id)
        cur = ", ".join(list_lots().get(key, {}).get("extension_lot_ids", []))
        _prompt(chat_id, msg_id,
                f"Отправь <b>ID лотов продления</b> через запятую.\n\n"
                f"Это лоты FunPay, при покупке которых активная аренда\n"
                f"на этот аккаунт будет автоматически продлена.\n\n"
                f"Сейчас: <code>{_esc(cur or '—')}</code>\n\n"
                f"Пример: <code>12345, 67890</code>\n"
                f"Для очистки отправь: <code>-</code>")

    def _start_edit_ext_games(uid, chat_id, msg_id, key, cb_id):
        _pending_state[uid] = {
            "step": "editlot_ext_games", "ctx": key,
            "chat_id": chat_id, "main_msg_id": msg_id}
        tg.bot.answer_callback_query(cb_id)
        cur = ", ".join(list_lots().get(key, {}).get("extension_games", []))
        _prompt(chat_id, msg_id,
                "Отправь <b>список игр для extension</b> через запятую.\n\n"
                "Когда покупатель купит этот extension-лот, бот найдёт "
                "его активную аренду <b>с одной из перечисленных игр</b> "
                "и продлит её.\n\n"
                f"Сейчас: <code>{_esc(cur or '—')}</code>\n\n"
                "Пример: <code>GTA 5, Red Dead Redemption 2</code>\n"
                "Для очистки отправь: <code>-</code>")

    def _start_set_game_acc(uid, chat_id, msg_id, alias, cb_id):
        _pending_state[uid] = {
            "step": "setgame_acc", "ctx": alias,
            "chat_id": chat_id, "main_msg_id": msg_id}
        tg.bot.answer_callback_query(cb_id)
        acc = find_account(alias)
        cur = acc.get("game", "") if acc else ""
        cur_key = (acc.get("game_key") if acc else "") or ""
        # Список существующих игр — подсказка чтобы пользователь
        # выбрал ту же самую (тогда game_key совпадёт и аккаунт
        # автоматически попадёт в пул всех её лотов).
        existing = list_games() or {}
        if existing:
            ex_lines: list[str] = []
            for k, g in list(existing.items())[:20]:
                gn = (g.get("name") or k).strip()
                ex_lines.append(f"  • <b>{_esc(gn)}</b> (<code>{_esc(k)}</code>)")
            ex_block = "\n\n📋 Уже есть игры:\n" + "\n".join(ex_lines)
        else:
            ex_block = ""
        cur_block = (
            f"Сейчас: <code>{_esc(cur or '—')}</code>"
            + (f" (key: <code>{_esc(cur_key)}</code>)" if cur_key else "")
        )
        _prompt(chat_id, msg_id,
                f"Отправь название <b>игры</b> для аккаунта "
                f"<b>{_esc(alias)}</b>.\n\n"
                f"{cur_block}\n\n"
                f"💡 Аккаунт будет привязан к игре "
                f"<i>(не к каждому лоту отдельно)</i> — "
                f"тогда любой лот этой игры сможет его выдать.\n"
                f"Если такая игра уже есть в списке — напиши её "
                f"название точно (или ключ через <code>:</code>, например "
                f"<code>DayZ:dayz</code>).{ex_block}\n\n"
                f"• <code>-</code> — очистить (без игры)")

    def _start_set_cost(uid, chat_id, msg_id, alias, cb_id):
        _pending_state[uid] = {
            "step": "set_cost", "ctx": alias,
            "chat_id": chat_id, "main_msg_id": msg_id}
        tg.bot.answer_callback_query(cb_id)
        acc = find_account(alias)
        cur = float((acc or {}).get("cost", 0.0) or 0.0)
        cur_lbl = f"{cur:.2f}".rstrip("0").rstrip(".") + "₽" if cur > 0 else "—"
        _prompt(chat_id, msg_id,
                f"Введи <b>стоимость аккаунта</b> в ₽ для "
                f"<b>{_esc(alias)}</b>.\n\n"
                f"Сейчас: <code>{cur_lbl}</code>\n\n"
                "Можно дробное (<code>1500.50</code>) или целое.\n"
                "Отправь <code>0</code> или <code>-</code> чтобы обнулить.")

    def _start_set_post_delivery_acc(uid, chat_id, msg_id, alias, cb_id):
        _pending_state[uid] = {
            "step": "setpd_acc", "ctx": alias,
            "chat_id": chat_id, "main_msg_id": msg_id}
        tg.bot.answer_callback_query(cb_id)
        acc = find_account(alias)
        cur = (acc or {}).get("post_delivery")
        if cur is None:
            cur_lbl = "— (используется глобальный шаблон или fallback с лота)"
        elif (cur or "").strip() == "":
            cur_lbl = "⛔ ВЫКЛЮЧЕНО (для этого аккаунта пост-сообщение не шлётся)"
        else:
            cur_lbl = _esc(cur)[:1500]
        _prompt(chat_id, msg_id,
                f"<b>📧 Доп. инфо для аккаунта <code>{_esc(alias)}</code></b>\n\n"
                f"Текущее значение:\n<code>{cur_lbl}</code>\n\n"
                f"Отправь новый текст. Доступные плейсхолдеры:\n"
                f"<code>{{login}}</code>, <code>{{password}}</code>, "
                f"<code>{{game}}</code>, <code>{{duration}}</code>, "
                f"<code>{{hours}}</code>, <code>{{minutes}}</code>.\n\n"
                f"• <code>-</code> — сбросить (использовать fallback с лота / глобальный)\n"
                f"• <code>off</code> или одна точка <code>.</code> — выключить пост-сообщение для этого аккаунта")

    def _start_set_post_delivery_lot(uid, chat_id, msg_id, key, cb_id):
        _pending_state[uid] = {
            "step": "setpd_lot", "ctx": key,
            "chat_id": chat_id, "main_msg_id": msg_id}
        tg.bot.answer_callback_query(cb_id)
        lot = (list_lots() or {}).get(key, {})
        cur = lot.get("post_delivery")
        if cur is None:
            cur_lbl = "— (используется глобальный шаблон)"
        elif (cur or "").strip() == "":
            cur_lbl = "⛔ ВЫКЛЮЧЕНО (для этого лота пост-сообщение не шлётся)"
        else:
            cur_lbl = _esc(cur)[:1500]
        _prompt(chat_id, msg_id,
                f"<b>📧 Доп. инфо для лота <code>{_esc(key)}</code></b>\n\n"
                f"Текущее значение:\n<code>{cur_lbl}</code>\n\n"
                f"Этот текст применится ко <b>всем аккаунтам</b> в этом лоте, "
                f"если у конкретного аккаунта нет своего шаблона.\n\n"
                f"Доступные плейсхолдеры: "
                f"<code>{{login}}</code>, <code>{{password}}</code>, "
                f"<code>{{game}}</code>, <code>{{duration}}</code>, "
                f"<code>{{hours}}</code>, <code>{{minutes}}</code>.\n\n"
                f"• <code>-</code> — сбросить (использовать глобальный)\n"
                f"• <code>off</code> или одна точка <code>.</code> — выключить для этого лота")

    def _start_rename_alias(uid, chat_id, msg_id, alias, cb_id):
        _pending_state[uid] = {
            "step": "rename_alias", "ctx": alias,
            "chat_id": chat_id, "main_msg_id": msg_id}
        tg.bot.answer_callback_query(cb_id)
        _prompt(chat_id, msg_id,
                f"Отправь <b>новый алиас</b> для аккаунта "
                f"<b>{_esc(alias)}</b>.\n\n"
                f"Допустимы: латиница, цифры, точка, дефис, подчёркивание "
                f"(до 32 символов).\n"
                f"Привязки в лотах обновятся автоматически.")

    def _start_edit_setting(uid, chat_id, msg_id, setting_key, cb_id):
        cfg = get_config()
        if setting_key not in cfg:
            tg.bot.answer_callback_query(cb_id, "Неизвестная настройка.")
            return
        _pending_state[uid] = {
            "step": "edit_setting", "ctx": setting_key,
            "chat_id": chat_id, "main_msg_id": msg_id}
        tg.bot.answer_callback_query(cb_id)
        cur = cfg.get(setting_key, "")
        _prompt(chat_id, msg_id,
                f"Отправь новое значение для <code>{_esc(setting_key)}</code>.\n\n"
                f"Сейчас: <code>{_esc(str(cur))[:500]}</code>")

    def _start_edit_template(uid, chat_id, msg_id, tpl_key, cb_id):
        # v2.22: читаем из JSON-файла выбранного админом языка
        cur_lang = _get_admin_tpl_lang(uid)
        file_tpls = _load_templates_file(cur_lang)
        defaults = (_DEFAULT_TEMPLATES_EN if cur_lang == "en"
                    else _DEFAULT_TEMPLATES)
        cur = file_tpls.get(tpl_key) or defaults.get(tpl_key, "")
        _pending_state[uid] = {
            "step": "edit_template", "ctx": tpl_key,
            "lang": cur_lang,
            "chat_id": chat_id, "main_msg_id": msg_id}
        tg.bot.answer_callback_query(cb_id)
        _prompt(chat_id, msg_id,
                f"<b>Редактирование шаблона: <code>{_esc(tpl_key)}</code></b>\n\n"
                f"Текущий текст:\n<code>{_esc(cur)[:1500]}</code>\n\n"
                f"Отправь новый текст шаблона. "
                f"Доступные плейсхолдеры: "
                f"<code>{{login}}</code>, <code>{{password}}</code>, "
                f"<code>{{game}}</code>, <code>{{duration}}</code>, "
                f"<code>{{hours}}</code>, <code>{{minutes}}</code>, "
                f"<code>{{new_expires}}</code>, <code>{{code}}</code>, "
                f"<code>{{link}}</code>.")

    # ───── Обработчик ввода (текст) ──────────────────────────────────────
    _valid_steps_text = {
        "add_alias", "add_password", "add_cost", "newlot_key",
        "newlot_aliases", "newlot_game",
        "editlot_duration", "editlot_aliases", "editlot_game", "editlot_ext",
        "editlot_ext_games",
        "edit_setting", "edit_template", "setgame_acc", "set_cost",
        "setpd_acc", "setpd_lot",
        "acc_search", "rename_alias",
        "blot_aliases", "blot_game",
        "blot_club", "blot_keys", "blot_extparent",
        "bl_add",
        "newext_key", "newext_games",
        "addgame_name", "addgame_main_lots", "addgame_ext_lots",
        "addgame_main_lot", "addgame_ext_lot",
        "game_acc_pool",
        "manual_issue_buyer", "manual_finish", "manual_extend_hours",
        "confirm_cancel",
    }

    def _is_pending_text(m):
        uid = getattr(m.from_user, "id", None)
        if uid not in _pending_state:
            return False
        st = _pending_state[uid].get("step")
        if st not in _valid_steps_text:
            return False
        text = (m.text or "")
        if not text.strip() or text.startswith("/"):
            return False
        # Для шагов добавления лотов к игре — id лота должен быть числом
        if st in ("addgame_main_lot", "addgame_ext_lot"):
            return text.strip().isdigit()
        return True

    def _is_pending_doc(m):
        uid = getattr(m.from_user, "id", None)
        if uid not in _pending_state:
            return False
        return _pending_state[uid].get("step") in ("add_mafile",
                                                    "bulk_import_zip")

    _alias_re = re.compile(r"^[A-Za-z0-9_-]{1,16}$")

    def _refresh_main(chat_id, msg_id):
        _edit_menu(chat_id, msg_id, _text_main(), _kb_main())

    def _handle_pending_text(message):
        uid = message.from_user.id
        st = _pending_state.get(uid)
        if not st:
            return
        text = (message.text or "").strip()
        chat_id = st["chat_id"]
        msg_id = st.get("main_msg_id")
        step = st["step"]

        if step == "add_alias":
            if not _alias_re.match(text):
                tg.bot.send_message(chat_id,
                    "Alias должен быть [A-Za-z0-9_-]{1,16}. Попробуй ещё раз "
                    "или нажми «Отмена».")
                return
            if find_account(text):
                tg.bot.send_message(chat_id, f"Alias {text} уже занят.")
                return
            st["alias"] = text
            st["step"] = "add_mafile"
            _prompt(chat_id, msg_id,
                    f"<b>Шаг 2/4.</b> Alias: <code>{_esc(text)}</code>\n\n"
                    f"Теперь отправь <b>файл .maFile</b> (как документ).")

        elif step == "add_password":
            st["password"] = text
            try:
                tg.bot.delete_message(message.chat.id, message.message_id)
            except Exception:
                pass
            st["step"] = "add_cost"
            _prompt(chat_id, msg_id,
                    f"<b>Шаг 4/4.</b> Введи <b>стоимость аккаунта</b> в ₽ "
                    "для калькулятора прибыли.\n\n"
                    "Целое или дробное число, например: <code>1500</code> "
                    "или <code>2399.50</code>.\n"
                    "Отправь <code>-</code>, чтобы пропустить (можно "
                    "указать позже через карточку аккаунта).")

        elif step == "add_cost":
            t = text.strip()
            if t == "-":
                st["cost"] = 0.0
            else:
                try:
                    val = float(t.replace(",", "."))
                    if val < 0:
                        raise ValueError
                except ValueError:
                    tg.bot.send_message(chat_id,
                        "⚠ Нужно число ≥ 0 или <code>-</code>.",
                        parse_mode="HTML")
                    return
                st["cost"] = val
            _finalize_add(uid)

        # ── v6: ручные операции с арендами (выдать/завершить/продлить/отменить) ──
        elif step == "manual_issue_buyer":
            alias = _pending_state[uid].get("ctx", "")
            parts = text.strip().split(maxsplit=1)
            if not parts:
                tg.bot.send_message(chat_id,
                    "Нужно <b>buyer_id</b> и <b>buyer_username</b> через пробел.")
                return
            try:
                buyer_id = int(parts[0])
            except ValueError:
                tg.bot.send_message(chat_id,
                    "⚠ buyer_id должен быть числом.")
                return
            buyer_username = parts[1] if len(parts) > 1 else "?"
            acc = find_account(alias)
            if not acc:
                tg.bot.send_message(chat_id, "Аккаунт не найден.")
                _pending_state.pop(uid, None)
                return
            try:
                _rent_account_to_buyer(
                    acc, buyer_id=buyer_id, buyer_username=buyer_username,
                    chat_id=None, order_id=f"MANUAL-{int(time.time())}",
                    duration_min=60)
                tg.bot.send_message(chat_id,
                    f"✅ Аккаунт <code>{_esc(alias)}</code> выдан покупателю "
                    f"<b>{_esc(buyer_username)}</b> (id <code>{buyer_id}</code>), "
                    f"срок 60 мин.",
                    parse_mode="HTML")
                _log_action("rental_start",
                            f"Ручная выдача {alias}",
                            alias=alias, buyer=buyer_username,
                            buyer_id=buyer_id, manual=True)
            except Exception as e:
                tg.bot.send_message(chat_id, f"❌ Ошибка: {e}")
            _pending_state.pop(uid, None)
            return
        elif step == "manual_finish":
            alias = _pending_state[uid].get("ctx", "")
            if text.strip().lower() not in ("да", "yes", "y", "+"):
                tg.bot.send_message(chat_id,
                    f"Отменено. Аренда <code>{_esc(alias)}</code> не тронута.",
                    parse_mode="HTML")
                _pending_state.pop(uid, None)
                return
            acc = find_account(alias)
            if not acc:
                tg.bot.send_message(chat_id, "Аккаунт не найден.")
                _pending_state.pop(uid, None)
                return
            try:
                _finish_rental(acc, reason="manual_finish",
                               send_message=False)
                tg.bot.send_message(chat_id,
                    f"✅ Аренда <code>{_esc(alias)}</code> завершена.",
                    parse_mode="HTML")
                _log_action("rental_end",
                            f"Ручное завершение {alias}",
                            alias=alias, manual=True)
            except Exception as e:
                LOGGER.error(
                    "steam_rental: manual_finish failed for %s",
                    alias, exc_info=True)
                tg.bot.send_message(chat_id, f"❌ Ошибка: {e}")
            _pending_state.pop(uid, None)
            return
        elif step == "manual_extend_hours":
            alias = _pending_state[uid].get("ctx", "")
            try:
                hours = float(text.strip().replace(",", "."))
            except ValueError:
                tg.bot.send_message(chat_id, "⚠ Нужно число часов.")
                return
            if hours <= 0:
                tg.bot.send_message(chat_id, "⚠ Часы должны быть > 0.")
                return
            try:
                # Снимаем rental ДО _extend_rental, чтобы взять buyer/chat,
                # а new_expires возвращает сама _extend_rental.
                acc_pre = find_account(alias)
                rental_pre = (acc_pre or {}).get("rental") or {}
                buyer_chat_id = rental_pre.get("chat_id")
                buyer_id_v = rental_pre.get("buyer_id")
                buyer_username_v = rental_pre.get("buyer_username") or ""

                extra_minutes = int(hours * 60)
                new_expires = _extend_rental(
                    alias, extra_minutes, reason="manual_extend")
                tg.bot.send_message(chat_id,
                    f"✅ Аренда <code>{_esc(alias)}</code> продлена на "
                    f"<b>{hours}</b> ч.",
                    parse_mode="HTML")
                _log_action("rental_extend",
                            f"Ручное продление {alias} на {hours}ч",
                            alias=alias, hours=hours, manual=True)

                # Уведомление покупателю в чат FunPay — тот же шаблон,
                # что и при автоматическом продлении через extension-лот.
                if new_expires and buyer_chat_id and acc_pre:
                    try:
                        game = (_get_game_for_alias(alias) or "—")
                        hours_display = (f"{hours:.0f}"
                                         if hours == int(hours)
                                         else f"{hours:.1f}")
                        text_buyer = _render_template(
                            "extended",
                            buyer_id=buyer_id_v,
                            hours=hours_display,
                            new_expires=_fmt_ts(new_expires),
                            login=acc_pre.get("account_name", ""),
                            game=game,
                        )
                        cardinal.send_message(
                            buyer_chat_id, text_buyer,
                            chat_name=buyer_username_v,
                            interlocutor_id=buyer_id_v,
                            watermark=False)
                    except Exception:
                        LOGGER.warning(
                            "steam_rental: manual_extend buyer-notify "
                            "failed for alias=%s buyer=%s",
                            alias, buyer_username_v, exc_info=True)
            except Exception as e:
                LOGGER.error(
                    "steam_rental: manual_extend_hours failed "
                    "for %s (hours=%s)", alias, hours, exc_info=True)
                tg.bot.send_message(chat_id, f"❌ Ошибка: {e}")
            _pending_state.pop(uid, None)
            return
        elif step == "confirm_cancel":
            alias = _pending_state[uid].get("ctx", "")
            if text.strip().lower() not in ("да", "yes", "y", "+"):
                tg.bot.send_message(chat_id,
                    f"Отменено. Аренда <code>{_esc(alias)}</code> не тронута.",
                    parse_mode="HTML")
                _pending_state.pop(uid, None)
                return
            acc = find_account(alias)
            if not acc:
                tg.bot.send_message(chat_id, "Аккаунт не найден.")
                _pending_state.pop(uid, None)
                return
            try:
                _cancel_rental(acc, reason="manual_cancel",
                               send_message=False)
                tg.bot.send_message(chat_id,
                    f"❌ Аренда <code>{_esc(alias)}</code> отменена.",
                    parse_mode="HTML")
                _log_action("rental_cancel",
                            f"Ручная отмена {alias}",
                            alias=alias, manual=True)
            except Exception as e:
                LOGGER.error(
                    "steam_rental: manual_cancel failed for %s",
                    alias, exc_info=True)
                tg.bot.send_message(chat_id, f"❌ Ошибка: {e}")
            _pending_state.pop(uid, None)
            return

        elif step == "newlot_key":
            st["key"] = text
            st["step"] = "newlot_aliases"
            st["picker_mode"] = "newlot"
            st["picker_sel"] = []
            st["picker_page"] = 0
            tg.bot.send_message(chat_id,
                f"OK. Ключ лота: <code>{_esc(text)}</code>",
                parse_mode="HTML")
            if msg_id:
                _show_alias_picker(chat_id, msg_id, st)
            else:
                _prompt(chat_id, msg_id,
                        "<b>Шаг 2/3.</b> Отправь <b>список alias'ов</b> для "
                        "пула через запятую (например: "
                        "<code>cs1, cs2, cs3</code>).")

        elif step == "newlot_aliases":
            if text.strip() == "-":
                aliases: list[str] = []
            else:
                aliases = [a.strip() for a in text.split(",") if a.strip()]
                unknown = [a for a in aliases if not find_account(a)]
                if unknown:
                    tg.bot.send_message(chat_id,
                        f"Эти алиасы не найдены: {', '.join(unknown)}.\n"
                        f"Введи список ещё раз или /srental_cancel.")
                    return
            st["aliases"] = aliases
            st["step"] = "newlot_game"
            for k in ("picker_mode", "picker_sel", "picker_page"):
                st.pop(k, None)
            _prompt(chat_id, msg_id,
                    "<b>Шаг 3/3.</b> Введи <b>название игры</b> "
                    "(например: <code>Counter Strike 2</code>)\n"
                    "или <code>-</code> чтобы пропустить.")

        elif step == "newlot_game":
            game = "" if text == "-" else text
            # Длительность всегда 0 — реальное время аренды бот читает
            # из описания лота на FunPay по тэгам #Hours: / #Time: при
            # каждой покупке. Это единый источник истины: один тэг в
            # описании лота → одинаково корректная выдача всегда.
            set_lot(st["key"], 0, st["aliases"], game=game)
            _pending_state.pop(uid, None)
            tg.bot.send_message(chat_id,
                f"✅ Лот <code>{_esc(st['key'])}</code> настроен.\n"
                f"🎮 Пул: <code>{_esc(', '.join(st['aliases']) or '—')}</code>"
                + (f"\n🎯 Игра: <b>{_esc(game)}</b>" if game else "")
                + "\n\n<i>⏱ Не забудь добавить в описание лота на FunPay "
                "тэг <code>#Hours: 24</code> (или <code>#Time: 2ч</code>) "
                "— иначе бот не поймёт срок аренды и не выдаст аккаунт.</i>",
                parse_mode="HTML")
            if msg_id:
                _edit_menu(chat_id, msg_id, _text_lots(), _kb_lots())

        # ── Массовое добавление лотов ────────────────────────────────
        elif step == "blot_aliases":
            if text.strip() == "-":
                aliases: list[str] = []
            else:
                aliases = [a.strip() for a in text.split(",") if a.strip()]
                unknown = [a for a in aliases if not find_account(a)]
                if unknown:
                    tg.bot.send_message(chat_id,
                        f"Эти алиасы не найдены: {', '.join(unknown)}.\n"
                        f"Введи список ещё раз или /srental_cancel.")
                    return
            st["aliases"] = aliases
            st["step"] = "blot_game"
            for k in ("picker_mode", "picker_sel", "picker_page"):
                st.pop(k, None)
            _prompt(chat_id, msg_id,
                    "<b>Шаг 2/4.</b> Отправь <b>название игры</b> "
                    "(одна для всех лотов).\n\n"
                    "Или <code>-</code> чтобы пропустить.")

        elif step == "blot_game":
            st["game"] = "" if text.strip() == "-" else text.strip()
            st["step"] = "blot_club"
            kb = tbtypes.InlineKeyboardMarkup(row_width=2)
            kb.add(
                tbtypes.InlineKeyboardButton(
                    "🏠 PC-club (ON)", callback_data="sr:blotclub:1"),
                tbtypes.InlineKeyboardButton(
                    "Обычный (OFF)", callback_data="sr:blotclub:0"),
            )
            kb.add(tbtypes.InlineKeyboardButton(
                "❌ Отмена", callback_data="sr:cancel_input"))
            tg.bot.send_message(chat_id,
                "<b>Шаг 3/4.</b> Включить <b>PC-club</b> режим для всех лотов?\n\n"
                "Если включишь — покупатели должны будут пройти "
                "AI-верификацию фото перед выдачей.",
                reply_markup=kb, parse_mode="HTML")

        elif step == "blot_keys":
            raw = text.replace(",", "\n").replace(";", "\n")
            keys = [k.strip() for k in raw.splitlines() if k.strip()]
            if not keys:
                tg.bot.send_message(chat_id,
                    "Не нашёл ни одного ключа. Пришли список ещё раз или "
                    "/srental_cancel.")
                return
            existing = list_lots()
            added: list[str] = []
            skipped: list[str] = []
            for key in keys:
                if key in existing:
                    skipped.append(key)
                    continue
                try:
                    set_lot(key, 0, list(st["aliases"]),
                            game=st["game"],
                            club_mode=bool(st.get("club_mode")))
                    added.append(key)
                except Exception as e:
                    skipped.append(f"{key} ({e})")
            st["added"] = added
            st["skipped"] = skipped
            club_tag = "🏠 " if st.get("club_mode") else ""
            report = (
                f"✅ <b>Массовое добавление лотов</b>\n\n"
                f"Параметры: {club_tag}длительность из описания, "
                f"пул: <code>{_esc(', '.join(st['aliases']) or '—')}</code>"
                f"{', игра: <b>' + _esc(st['game']) + '</b>' if st['game'] else ''}\n\n"
                f"➕ Добавлено: <b>{len(added)}</b>\n"
                f"↷ Уже было / пропущено: <b>{len(skipped)}</b>\n\n")
            if added:
                report += ("Добавлены:\n<code>"
                           + _esc("\n".join(added[:30])) + "</code>")
                if len(added) > 30:
                    report += f"\n… и ещё {len(added) - 30}"
            tg.bot.send_message(chat_id, report, parse_mode="HTML")

            # Опциональный шаг 5/5: привязать как extension к родительскому лоту
            if added:
                st["step"] = "blot_extparent"
                _prompt(chat_id, msg_id,
                        "<b>Шаг 5/5 (опционально).</b> Привязать эти "
                        f"{len(added)} лотов как <b>extension</b> к "
                        "существующему лоту?\n\n"
                        "Пришли <b>ID или ключевое слово</b> родительского лота — "
                        "созданные ID добавятся в его список extension-лотов.\n\n"
                        "Или отправь <code>-</code> чтобы оставить как обычные.")
            else:
                _pending_state.pop(uid, None)
                if msg_id:
                    _edit_menu(chat_id, msg_id, _text_lots(), _kb_lots())

        elif step == "blot_extparent":
            added = st.get("added") or []
            if text.strip() == "-" or not added:
                _pending_state.pop(uid, None)
                if msg_id:
                    _edit_menu(chat_id, msg_id, _text_lots(), _kb_lots())
                return
            parent_key = text.strip()
            lots = list_lots()
            # Поиск по точному ключу или по подстроке keyword'а
            if parent_key not in lots:
                matches = [k for k in lots
                           if not k.isdigit() and parent_key.lower() in k.lower()]
                if len(matches) == 1:
                    parent_key = matches[0]
            if parent_key not in lots:
                tg.bot.send_message(chat_id,
                    f"⚠ Лот <code>{_esc(parent_key)}</code> не найден. "
                    f"Введи ID/keyword ещё раз или <code>-</code> чтобы "
                    f"пропустить.", parse_mode="HTML")
                return
            with _lock:
                lots = list_lots()
                cur_ext = list(lots[parent_key].get("extension_lot_ids", []))
                added_to_ext = []
                for a in added:
                    if a not in cur_ext:
                        cur_ext.append(a)
                        added_to_ext.append(a)
                lots[parent_key]["extension_lot_ids"] = cur_ext
                save_lots(lots)
            _pending_state.pop(uid, None)
            tg.bot.send_message(chat_id,
                f"✅ {len(added_to_ext)} лотов привязаны как extension к "
                f"<code>{_esc(parent_key)}</code>.\n\n"
                f"Теперь покупатели этого лота смогут продлевать аренду через них.",
                parse_mode="HTML")
            if msg_id:
                _edit_menu(chat_id, msg_id,
                           _text_lot(parent_key), _kb_lot(parent_key))

        # ── v6 Wizard: добавить игру / лоты к игре ─────────────────────
        elif step == "addgame_name":
            name = text.strip()
            if not name:
                tg.bot.send_message(chat_id,
                    "Название не может быть пустым. Введи название игры:")
                return
            _pending_state[uid]["ctx"] = name
            _pending_state[uid]["step"] = "addgame_main_lots"
            tg.bot.send_message(chat_id,
                f"✅ Игра: <b>{_esc(name)}</b>\n\n"
                f"<b>Шаг 2/3.</b> Отправь ID <b>Main лотов</b> "
                f"через запятую (числа из URL <code>?id=...</code>).\n\n"
                f"Можно пусто — тогда отправь <code>-</code>.\n\n"
                f"<i>Main-лоты активируются автоматически и выдают "
                f"аккаунты при покупке.</i>",
                parse_mode="HTML")
            return
        elif step == "addgame_main_lots":
            txt = text.strip()
            main_ids: list[str] = []
            if txt and txt != "-":
                for raw in txt.replace(";", ",").split(","):
                    raw = raw.strip()
                    if raw.isdigit():
                        main_ids.append(raw)
                    else:
                        tg.bot.send_message(chat_id,
                            f"⚠ Игнорирую «<code>{_esc(raw)}</code>» — "
                            f"не похоже на числовой ID.")
            _pending_state[uid]["main_lots"] = main_ids
            _pending_state[uid]["step"] = "addgame_ext_lots"
            tg.bot.send_message(chat_id,
                f"✅ Main лоты: <b>{len(main_ids)}</b>\n\n"
                f"<b>Шаг 3/3.</b> Отправь ID <b>Extension лотов</b> "
                f"(для продления аренды). Можно пусто — <code>-</code>.",
                parse_mode="HTML")
            return
        elif step == "addgame_ext_lots":
            name = _pending_state[uid].get("ctx", "")
            main_ids = _pending_state[uid].get("main_lots", [])
            ext_ids: list[str] = []
            txt = text.strip()
            if txt and txt != "-":
                for raw in txt.replace(";", ",").split(","):
                    raw = raw.strip()
                    if raw.isdigit():
                        ext_ids.append(raw)
            # Создаём игру
            gkey = set_game(_slugify_game(name), name)
            # Создаём лоты с game_key и kind
            for lid in main_ids:
                set_lot(lid, duration_min=0, aliases=[],
                        game=name, is_extension=False,
                        game_key=gkey, kind="main")
            for lid in ext_ids:
                set_lot(lid, duration_min=0, aliases=[],
                        game=name, is_extension=True,
                        game_key=gkey, kind="ext")
            _pending_state.pop(uid, None)
            tg.bot.send_message(chat_id,
                f"✅ Игра создана: <b>{_esc(name)}</b> "
                f"(<code>{gkey}</code>)\n"
                f"  • Main лотов: <b>{len(main_ids)}</b>\n"
                f"  • Extension лотов: <b>{len(ext_ids)}</b>\n\n"
                f"Теперь привяжи аккаунты к лотам через "
                f"📋 Аккаунты → конкретный акк → ➕ Lot",
                parse_mode="HTML")
            if msg_id:
                _edit_menu(chat_id, msg_id, _text_game(gkey), _kb_game(gkey))
            return
        elif step == "addgame_main_lot":
            gkey = _pending_state[uid].get("ctx", "")
            lid = text.strip()
            if not lid.isdigit():
                tg.bot.send_message(chat_id,
                    "⚠ ID лота должен быть числом. Введи ID:")
                return
            g = get_game(gkey)
            if not g:
                _pending_state.pop(uid, None)
                tg.bot.send_message(chat_id, "Игра не найдена. /srental → 🎮 Игры")
                return
            set_lot(lid, duration_min=0, aliases=[],
                    game=g.get("name", ""), is_extension=False,
                    game_key=gkey, kind="main")
            _pending_state.pop(uid, None)
            tg.bot.send_message(chat_id,
                f"✅ Main лот <code>{_esc(lid)}</code> добавлен в игру "
                f"<b>{_esc(gkey)}</b>.",
                parse_mode="HTML")
            if msg_id:
                _edit_menu(chat_id, msg_id, _text_game(gkey), _kb_game(gkey))
            return
        elif step == "addgame_ext_lot":
            gkey = _pending_state[uid].get("ctx", "")
            lid = text.strip()
            if not lid.isdigit():
                tg.bot.send_message(chat_id,
                    "⚠ ID лота должен быть числом. Введи ID:")
                return
            g = get_game(gkey)
            if not g:
                _pending_state.pop(uid, None)
                tg.bot.send_message(chat_id, "Игра не найдена. /srental → 🎮 Игры")
                return
            set_lot(lid, duration_min=0, aliases=[],
                    game=g.get("name", ""), is_extension=True,
                    game_key=gkey, kind="ext")
            _pending_state.pop(uid, None)
            tg.bot.send_message(chat_id,
                f"✅ Ext лот <code>{_esc(lid)}</code> добавлен в игру "
                f"<b>{_esc(gkey)}</b>.",
                parse_mode="HTML")
            if msg_id:
                _edit_menu(chat_id, msg_id, _text_game(gkey), _kb_game(gkey))
            return

        # ── Wizard: лот для продления (extension-лот) ─────────────────
        elif step == "newext_key":
            key = text.strip()
            if not key:
                tg.bot.send_message(chat_id,
                    "Ключ не может быть пустым. Введи ID или keyword:")
                return
            if key in list_lots():
                tg.bot.send_message(chat_id,
                    f"⚠ Лот <code>{_esc(key)}</code> уже существует. "
                    f"Введи другой ID/keyword или /srental_cancel.",
                    parse_mode="HTML")
                return
            st["key"] = key
            st["step"] = "newext_games"
            _prompt(chat_id, msg_id,
                    f"<b>Шаг 2/2.</b> Ключ лота: <code>{_esc(key)}</code>\n\n"
                    "Введи <b>список игр</b> через запятую — этот лот "
                    "будет продлевать аренду аккаунта <i>с любой из "
                    "перечисленных игр</i>.\n\n"
                    "Пример: <code>GTA 5, Red Dead Redemption 2</code>\n\n"
                    "Или отправь <code>-</code> чтобы лот продлевал "
                    "любую активную аренду покупателя без проверки игры.")

        elif step == "newext_games":
            t = text.strip()
            if t == "-" or not t:
                ext_games: list[str] = []
            else:
                ext_games = [g.strip() for g in t.split(",") if g.strip()]
            key = st["key"]
            try:
                # duration_min=0 — реальный срок продления плагин читает
                # из описания лота на FunPay (#Hours:/#Time:) при каждой
                # покупке; если тэга нет — берётся
                # cfg.extension_default_minutes (60 мин).
                set_lot(key, 0, [],
                        extension_games=ext_games,
                        is_extension=True)
            except Exception as e:
                tg.bot.send_message(chat_id,
                    f"⚠ Ошибка создания лота: <code>{_esc(str(e))}</code>",
                    parse_mode="HTML")
                _pending_state.pop(uid, None)
                return
            # Сразу деактивируем лот на FunPay — будет активирован только
            # по команде покупателя !продлить.
            try:
                cardinal_ref = _CARDINAL_REF
                if cardinal_ref is not None and key.isdigit():
                    _set_funpay_lot_active(cardinal_ref, key, False)
            except Exception:
                LOGGER.debug("steam_rental: deactivate extension lot on create "
                             "failed", exc_info=True)
            _pending_state.pop(uid, None)
            games_label = ", ".join(ext_games) if ext_games else "—"
            default_min = int(get_config().get(
                "extension_default_minutes", 60) or 60)
            tg.bot.send_message(chat_id,
                f"✅ <b>Лот для продления создан</b>\n\n"
                f"ID/keyword: <code>{_esc(key)}</code>\n"
                f"Игры (extension): <code>{_esc(games_label)}</code>\n\n"
                f"🔻 Лот <b>деактивирован</b> на FunPay. Он будет включаться "
                f"автоматически, когда покупатель напишет "
                f"<code>!продлить</code>, и выключаться обратно после "
                f"покупки.\n\n"
                f"<i>⏱ Срок продления: добавь в описание лота на FunPay "
                f"тэг <code>#Hours: 1</code> или <code>#Time: 30m</code>. "
                f"Без тэга бот продлит на дефолт "
                f"<b>{_human_minutes(default_min)}</b>.</i>",
                parse_mode="HTML")
            if msg_id:
                _edit_menu(chat_id, msg_id, _text_lot(key), _kb_lot(key))

        elif step == "editlot_duration":
            key = st["ctx"]
            try:
                dur = _parse_duration(text)
            except ValueError as e:
                tg.bot.send_message(chat_id, f"⚠ {str(e)}")
                return
            with _lock:
                lots = list_lots()
                if key not in lots:
                    tg.bot.send_message(chat_id, "Лот не найден.")
                    _pending_state.pop(uid, None)
                    return
                lots[key]["duration_min"] = dur
                save_lots(lots)
            _pending_state.pop(uid, None)
            tg.bot.send_message(chat_id,
                f"✅ {_esc(key)}: длительность → {_human_minutes(dur)}",
                parse_mode="HTML")
            if msg_id:
                _edit_menu(chat_id, msg_id, _text_lot(key), _kb_lot(key))

        elif step == "editlot_aliases":
            key = st["ctx"]
            aliases = [a.strip() for a in text.split(",") if a.strip()]
            unknown = [a for a in aliases if not find_account(a)]
            if unknown:
                tg.bot.send_message(chat_id,
                    f"Эти alias'ы не найдены: {', '.join(unknown)}.")
                return
            with _lock:
                lots = list_lots()
                if key not in lots:
                    tg.bot.send_message(chat_id, "Лот не найден.")
                    _pending_state.pop(uid, None)
                    return
                lots[key]["aliases"] = aliases
                save_lots(lots)
            _pending_state.pop(uid, None)
            tg.bot.send_message(chat_id,
                f"✅ {_esc(key)}: пул → <code>{_esc(', '.join(aliases))}</code>",
                parse_mode="HTML")
            if msg_id:
                _edit_menu(chat_id, msg_id, _text_lot(key), _kb_lot(key))

        elif step == "game_acc_pool":
            gkey = st["ctx"]
            g = get_game(gkey)
            if not g:
                tg.bot.send_message(chat_id, "Игра не найдена.")
                _pending_state.pop(uid, None)
                return
            raw = (text or "").strip()
            if raw == "-":
                aliases: list[str] = []
            else:
                aliases = [a.strip() for a in raw.split(",") if a.strip()]
            unknown = [a for a in aliases if not find_account(a)]
            if unknown:
                tg.bot.send_message(chat_id,
                    f"Эти alias'ы не найдены: <code>"
                    f"{_esc(', '.join(unknown))}</code>. Изменения не "
                    "сохранены — попробуй ещё раз или нажми «❌ Отмена».",
                    parse_mode="HTML")
                return
            game_name = g.get("name", gkey) or gkey
            target_lc = {a.lower() for a in aliases}
            gkey_lc = str(gkey).lower()
            attached: list[str] = []
            detached: list[str] = []
            moved: list[str] = []
            with _lock:
                accs = list_accounts()
                for acc in accs:
                    alias = acc.get("alias", "")
                    if not alias:
                        continue
                    cur_gk = (acc.get("game_key") or "").strip().lower()
                    if alias.lower() in target_lc:
                        if cur_gk == gkey_lc:
                            if (acc.get("game") or "") != game_name:
                                acc["game"] = game_name
                        else:
                            if cur_gk:
                                moved.append(alias)
                            acc["game_key"] = gkey
                            acc["game"] = game_name
                            attached.append(alias)
                    else:
                        if cur_gk == gkey_lc:
                            acc["game_key"] = ""
                            acc["game"] = ""
                            detached.append(alias)
                save_accounts(accs)
            _pending_state.pop(uid, None)
            parts = [f"✅ <b>{_esc(game_name)}</b>:"]
            if attached:
                parts.append(
                    f"➕ привязано: <b>{len(attached)}</b> "
                    f"(<code>{_esc(', '.join(attached))}</code>)")
            if detached:
                parts.append(
                    f"➖ отвязано: <b>{len(detached)}</b> "
                    f"(<code>{_esc(', '.join(detached))}</code>)")
            if moved:
                parts.append(
                    f"🔁 перенесено: <b>{len(moved)}</b> "
                    f"(<code>{_esc(', '.join(moved))}</code>)")
            if not attached and not detached:
                parts.append("<i>Изменений нет.</i>")
            tg.bot.send_message(chat_id, "\n".join(parts),
                                parse_mode="HTML")
            if msg_id:
                _edit_menu(chat_id, msg_id,
                           _text_game(gkey), _kb_game(gkey))

        elif step == "editlot_game":
            key = st["ctx"]
            game = "" if text == "-" else text
            with _lock:
                lots = list_lots()
                if key not in lots:
                    tg.bot.send_message(chat_id, "Лот не найден.")
                    _pending_state.pop(uid, None)
                    return
                lots[key]["game"] = game
                save_lots(lots)
            _pending_state.pop(uid, None)
            tg.bot.send_message(chat_id,
                f"✅ {_esc(key)}: игра → <b>{_esc(game or '—')}</b>",
                parse_mode="HTML")
            if msg_id:
                _edit_menu(chat_id, msg_id, _text_lot(key), _kb_lot(key))

        elif step == "editlot_ext":
            key = st["ctx"]
            if text.strip() == "-":
                ext_ids = []
            else:
                ext_ids = [x.strip() for x in text.split(",") if x.strip()]
            with _lock:
                lots = list_lots()
                if key not in lots:
                    tg.bot.send_message(chat_id, "Лот не найден.")
                    _pending_state.pop(uid, None)
                    return
                lots[key]["extension_lot_ids"] = ext_ids
                save_lots(lots)
            _pending_state.pop(uid, None)
            tg.bot.send_message(chat_id,
                f"✅ {_esc(key)}: лоты продления → "
                f"<code>{_esc(', '.join(ext_ids) or '—')}</code>",
                parse_mode="HTML")
            if msg_id:
                _edit_menu(chat_id, msg_id, _text_lot(key), _kb_lot(key))

        elif step == "editlot_ext_games":
            key = st["ctx"]
            if text.strip() == "-":
                ext_games: list[str] = []
            else:
                ext_games = [x.strip() for x in text.split(",") if x.strip()]
            with _lock:
                lots = list_lots()
                if key not in lots:
                    tg.bot.send_message(chat_id, "Лот не найден.")
                    _pending_state.pop(uid, None)
                    return
                lots[key]["extension_games"] = ext_games
                save_lots(lots)
            _pending_state.pop(uid, None)
            tg.bot.send_message(chat_id,
                f"✅ {_esc(key)}: extension-игры → "
                f"<code>{_esc(', '.join(ext_games) or '—')}</code>",
                parse_mode="HTML")
            if msg_id:
                _edit_menu(chat_id, msg_id, _text_lot(key), _kb_lot(key))

        elif step == "setgame_acc":
            alias = st["ctx"]
            raw = (text or "").strip()
            cleared = (raw == "-")
            game_name = "" if cleared else raw
            game_key = ""
            # Поддержка явного ключа: "DayZ:dayz" → name="DayZ", key="dayz".
            # Без двоеточия — slugify сами.
            if not cleared and game_name:
                if ":" in game_name:
                    nm, _, kk = game_name.partition(":")
                    game_name = nm.strip() or kk.strip()
                    game_key = _slugify_game(kk.strip()) or _slugify_game(game_name)
                else:
                    # Если такая игра уже есть по name — берём её key.
                    existing = list_games() or {}
                    matched_key = ""
                    for k, g in existing.items():
                        if (g.get("name") or "").strip().lower() \
                                == game_name.lower():
                            matched_key = k
                            break
                    game_key = matched_key or _slugify_game(game_name)

            with _lock:
                acc = find_account(alias)
                if not acc:
                    tg.bot.send_message(chat_id, "Аккаунт не найден.")
                    _pending_state.pop(uid, None)
                    return
                if cleared:
                    acc["game"] = ""
                    acc["game_key"] = ""
                else:
                    acc["game"] = game_name
                    acc["game_key"] = game_key
                upsert_account(acc)
            # Если игра — новая (key не существовал), автосоздаём запись
            # в games.json. Существующие записи не трогаем (идемпотентно).
            game_created = False
            if not cleared and game_key:
                existing_after = list_games() or {}
                if game_key not in existing_after:
                    try:
                        set_game(game_key, game_name)
                        game_created = True
                    except Exception:
                        LOGGER.debug(
                            "steam_rental: auto-create game on setgame_acc "
                            "failed", exc_info=True)
            _pending_state.pop(uid, None)
            if cleared:
                summary = "♻️ очищено"
            else:
                key_lbl = f" (key: <code>{_esc(game_key)}</code>)" if game_key else ""
                created_lbl = " ✨ <i>новая игра создана</i>" if game_created else ""
                summary = f"<b>{_esc(game_name)}</b>{key_lbl}{created_lbl}"
            tg.bot.send_message(chat_id,
                f"✅ {_esc(alias)}: игра → {summary}\n"
                f"<i>Аккаунт автоматически попадёт в пул всех лотов "
                f"этой игры.</i>",
                parse_mode="HTML")
            if msg_id:
                _edit_menu(chat_id, msg_id, _text_acc(alias), _kb_acc(alias))

        elif step == "set_cost":
            alias = st["ctx"]
            t = (text or "").strip()
            if t == "-" or t == "":
                cost_val = 0.0
            else:
                try:
                    cost_val = float(t.replace(",", "."))
                    if cost_val < 0:
                        raise ValueError
                except ValueError:
                    tg.bot.send_message(chat_id,
                        "⚠ Нужно число ≥ 0 или <code>-</code>.",
                        parse_mode="HTML")
                    return
            with _lock:
                acc = find_account(alias)
                if not acc:
                    tg.bot.send_message(chat_id, "Аккаунт не найден.")
                    _pending_state.pop(uid, None)
                    return
                acc["cost"] = cost_val
                upsert_account(acc)
            _pending_state.pop(uid, None)
            tg.bot.send_message(chat_id,
                f"✅ {_esc(alias)}: стоимость → <b>{cost_val:.2f}₽</b>",
                parse_mode="HTML")
            if msg_id:
                _edit_menu(chat_id, msg_id, _text_acc(alias), _kb_acc(alias))

        elif step == "setpd_acc":
            alias = st["ctx"]
            t = text or ""
            t_stripped = t.strip()
            with _lock:
                acc = find_account(alias)
                if not acc:
                    tg.bot.send_message(chat_id, "Аккаунт не найден.")
                    _pending_state.pop(uid, None)
                    return
                if t_stripped == "-":
                    acc.pop("post_delivery", None)
                    summary = "♻️ сброшено (используется лот/глобальный шаблон)"
                elif t_stripped.lower() == "off" or t_stripped == ".":
                    acc["post_delivery"] = ""
                    summary = "⛔ выключено для этого аккаунта"
                else:
                    acc["post_delivery"] = t
                    preview = _esc(t)[:300]
                    summary = f"✏️ кастом установлен:\n<code>{preview}</code>"
                upsert_account(acc)
            _pending_state.pop(uid, None)
            tg.bot.send_message(chat_id,
                f"✅ {_esc(alias)}: 📧 доп. инфо → {summary}",
                parse_mode="HTML")
            if msg_id:
                _edit_menu(chat_id, msg_id, _text_acc(alias), _kb_acc(alias))

        elif step == "setpd_lot":
            key = st["ctx"]
            t = text or ""
            t_stripped = t.strip()
            with _lock:
                lots = list_lots()
                if key not in lots:
                    tg.bot.send_message(chat_id, "Лот не найден.")
                    _pending_state.pop(uid, None)
                    return
                if t_stripped == "-":
                    lots[key].pop("post_delivery", None)
                    summary = "♻️ сброшено (используется глобальный шаблон)"
                elif t_stripped.lower() == "off" or t_stripped == ".":
                    lots[key]["post_delivery"] = ""
                    summary = "⛔ выключено для этого лота"
                else:
                    lots[key]["post_delivery"] = t
                    preview = _esc(t)[:300]
                    summary = f"✏️ кастом установлен:\n<code>{preview}</code>"
                save_lots(lots)
            _pending_state.pop(uid, None)
            tg.bot.send_message(chat_id,
                f"✅ Лот {_esc(key)}: 📧 доп. инфо → {summary}",
                parse_mode="HTML")
            if msg_id:
                _edit_menu(chat_id, msg_id, _text_lot(key), _kb_lot(key))

        elif step == "rename_alias":
            old_alias = st["ctx"]
            new_alias = (text or "").strip()
            ok, reason = rename_account(old_alias, new_alias)
            if not ok:
                tg.bot.send_message(chat_id,
                    f"⚠ {_esc(reason)}\n\nПопробуй ещё раз или /srental_cancel.")
                return
            _pending_state.pop(uid, None)
            tg.bot.send_message(chat_id,
                f"✅ Алиас изменён: <code>{_esc(old_alias)}</code> "
                f"→ <code>{_esc(new_alias)}</code>",
                parse_mode="HTML")
            if msg_id:
                _edit_menu(chat_id, msg_id,
                           _text_acc(new_alias), _kb_acc(new_alias))

        elif step == "edit_setting":
            setting_key = st["ctx"]
            cfg = get_config()
            if setting_key not in cfg:
                tg.bot.send_message(chat_id, "Неизвестная настройка.")
                _pending_state.pop(uid, None)
                return
            old_val = cfg[setting_key]
            if isinstance(old_val, int):
                try:
                    cfg[setting_key] = int(text)
                except ValueError:
                    tg.bot.send_message(chat_id, "Нужно число.")
                    return
            elif isinstance(old_val, float):
                try:
                    cfg[setting_key] = float(text)
                except ValueError:
                    tg.bot.send_message(chat_id, "Нужно число.")
                    return
            else:
                cfg[setting_key] = text.strip()
            save_config(cfg)
            _pending_state.pop(uid, None)

            # ── Доп.валидация API-ключей AI-провайдеров ──
            api_provider_map = {
                "openrouter_api_key": "openrouter",
                "openai_api_key": "openai",
                "anthropic_api_key": "anthropic",
                "google_ai_api_key": "google",
            }
            back_to_clb = setting_key in api_provider_map or setting_key in (
                "openrouter_model", "openai_model", "anthropic_model",
                "google_ai_model", "seller_funpay_nickname",
                "club_auto_approve_threshold",
                "club_auto_decline_threshold",
                "club_request_ttl_hours",
                "pcclub_command",
                "ai_fake_decline_threshold",
                "ai_fake_manual_threshold",
            )
            back_to_vac = setting_key in ("steam_api_key",
                                            "vac_scan_interval_min")

            if setting_key in api_provider_map and text.strip():
                tg.bot.send_message(chat_id, "🔍 Проверяю ключ...")

                def _vk(setting_key=setting_key, key=text.strip()):
                    provider = api_provider_map[setting_key]
                    ok, err = _ai_validate_key(provider, key)
                    if ok:
                        tg.bot.send_message(
                            chat_id,
                            f"✅ <code>{_esc(setting_key)}</code> сохранён "
                            f"и принят провайдером "
                            f"<b>{_AI_PROVIDER_LABELS.get(provider)}</b>.",
                            parse_mode="HTML")
                    else:
                        tg.bot.send_message(
                            chat_id,
                            f"⚠ <code>{_esc(setting_key)}</code> сохранён, "
                            f"но провайдер ответил ошибкой:\n"
                            f"<code>{_esc(err)[:400]}</code>",
                            parse_mode="HTML")
                threading.Thread(target=_vk, daemon=True).start()
            else:
                tg.bot.send_message(chat_id,
                    f"✅ <code>{_esc(setting_key)}</code> обновлено.",
                    parse_mode="HTML")

            if msg_id:
                if back_to_clb:
                    _edit_menu(chat_id, msg_id, _text_clbset(), _kb_clbset())
                elif back_to_vac:
                    _edit_menu(chat_id, msg_id, _text_vacset(), _kb_vacset())
                else:
                    _edit_menu(chat_id, msg_id,
                               _text_settings(), _kb_settings())

        elif step == "edit_template":
            tpl_key = st["ctx"]
            cur_lang = st.get("lang", "ru")
            # v2.22: пишем правку в соответствующий JSON-файл
            cur = dict(_load_templates_file(cur_lang) or {})
            cur[tpl_key] = text
            _save_templates_file(cur_lang, cur)
            _pending_state.pop(uid, None)
            lang_label = "🇬🇧 EN" if cur_lang == "en" else "🇷🇺 RU"
            tg.bot.send_message(chat_id,
                f"✅ Шаблон <code>{_esc(tpl_key)}</code> "
                f"({lang_label}) обновлён.",
                parse_mode="HTML")
            if msg_id:
                _edit_menu(chat_id, msg_id,
                           _text_templates(uid), _kb_templates(uid))
        elif step == "bl_add":
            _pending_state.pop(uid, None)
            tokens = [t.strip() for t in re.split(r"[\s,]+", text) if t.strip()]
            bid: int | None = None
            uname: str | None = None
            for t in tokens:
                if t.isdigit() and bid is None:
                    bid = int(t)
                elif uname is None:
                    uname = t
            if not bid and not uname:
                tg.bot.send_message(
                    chat_id,
                    "⚠ Не нашёл ни username, ни ID. Попробуй ещё раз "
                    "через blacklist-меню.")
                return
            added = add_to_blacklist(bid, uname, reason="manual")
            tg.bot.send_message(
                chat_id,
                f"✅ Добавлен в blacklist: "
                f"username=<code>{_esc(str(uname or '—'))}</code>, "
                f"id=<code>{_esc(str(bid or '—'))}</code>"
                if added else
                "Этот покупатель уже был в blacklist.",
                parse_mode="HTML")
            if msg_id:
                _edit_menu(chat_id, msg_id, _text_blacklist(), _kb_blacklist())
        elif step == "ev_interval":
            try:
                val = int(text)
                if val < 1:
                    val = 1
            except ValueError:
                tg.bot.send_message(chat_id, "Некорректное число.")
                return
            events = _load_events()
            ev = events.setdefault("unclosed_notify", {})
            ev["interval_hours"] = val
            if ev.get("enabled", True):
                ev["next_run"] = _now() + val * 3600
            _save_events(events)
            _pending_state.pop(uid, None)
            tg.bot.send_message(chat_id,
                f"✅ Интервал установлен: {val} ч.")
            if msg_id:
                _edit_menu(chat_id, msg_id, _text_events(), _kb_events())

        elif step == "acc_search":
            if text == "-":
                _acc_filter["search"] = ""
                note = "Поиск очищен."
            else:
                _acc_filter["search"] = text
                note = f"Поиск: <code>{_esc(text)}</code>"
            _pending_state.pop(uid, None)
            tg.bot.send_message(chat_id, "✅ " + note, parse_mode="HTML")
            if msg_id:
                _edit_menu(chat_id, msg_id, _text_accs(), _kb_accs(0))

    def _handle_pending_doc(message):
        uid = message.from_user.id
        st = _pending_state.get(uid)
        if not st:
            return
        step = st.get("step")
        if step == "add_mafile":
            _handle_add_mafile_doc(uid, message)
        elif step == "bulk_import_zip":
            _handle_bulk_import_doc(uid, message)

    def _handle_add_mafile_doc(uid, message):
        st = _pending_state.get(uid) or {}
        chat_id = st["chat_id"]
        msg_id = st.get("main_msg_id")
        try:
            file_info = tg.bot.get_file(message.document.file_id)
            blob = tg.bot.download_file(file_info.file_path)
            data = json.loads(blob.decode("utf-8"))
        except Exception as exc:
            tg.bot.send_message(chat_id,
                                f"Не удалось распарсить .maFile: {exc}")
            return
        for k in ("account_name", "shared_secret", "identity_secret"):
            if not data.get(k):
                tg.bot.send_message(chat_id,
                    f"В .maFile нет поля {k}.")
                return
        st["mafile"] = data
        st["step"] = "add_password"
        try:
            tg.bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            pass
        _prompt(chat_id, msg_id,
                f"<b>Шаг 3/4.</b> Файл принят. Теперь отправь <b>пароль</b> "
                f"от Steam-аккаунта <code>{_esc(data['account_name'])}</code>. "
                f"Сообщение с паролем будет удалено автоматически.")

    def _handle_bulk_import_doc(uid, message):
        st = _pending_state.pop(uid, None)
        if not st:
            return
        chat_id = st["chat_id"]
        msg_id = st.get("main_msg_id")
        kb_back = tbtypes.InlineKeyboardMarkup()
        kb_back.add(tbtypes.InlineKeyboardButton(
            "◀️ К списку", callback_data="sr:accs:0"))
        try:
            file_info = tg.bot.get_file(message.document.file_id)
            blob = tg.bot.download_file(file_info.file_path)
        except Exception as exc:
            _edit_menu(chat_id, msg_id,
                       f"❌ Не удалось скачать файл: <code>{_esc(str(exc))}</code>",
                       kb_back)
            return
        try:
            tg.bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            pass
        try:
            zf = zipfile.ZipFile(io.BytesIO(blob))
        except Exception as exc:
            _edit_menu(chat_id, msg_id,
                       f"❌ Не похоже на валидный zip: <code>{_esc(str(exc))}</code>",
                       kb_back)
            return

        passwords_map: dict[str, str] = {}
        aliases_map: dict[str, str] = {}
        costs_map: dict[str, float] = {}
        mafiles: list[tuple[str, dict[str, Any]]] = []

        for name in zf.namelist():
            if name.endswith("/") or name.startswith("__MACOSX"):
                continue
            base = os.path.basename(name).lower()
            try:
                raw = zf.read(name)
            except Exception:
                continue
            if base == "passwords.txt":
                for line in raw.decode("utf-8", errors="ignore").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or ":" not in line:
                        continue
                    login, _, pw = line.partition(":")
                    login = login.strip()
                    pw = pw.strip()
                    if login and pw:
                        passwords_map[login.lower()] = pw
                continue
            if base == "aliases.txt":
                for line in raw.decode("utf-8", errors="ignore").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or ":" not in line:
                        continue
                    login, _, alias = line.partition(":")
                    login = login.strip()
                    alias = alias.strip()
                    if login and alias:
                        aliases_map[login.lower()] = alias
                continue
            if base == "costs.txt":
                for line in raw.decode("utf-8", errors="ignore").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or ":" not in line:
                        continue
                    login, _, cost_s = line.partition(":")
                    login = login.strip().lower()
                    cost_s = cost_s.strip().replace(",", ".")
                    if not login or not cost_s:
                        continue
                    try:
                        cost_val = float(cost_s)
                        if cost_val < 0:
                            continue
                    except ValueError:
                        continue
                    costs_map[login] = cost_val
                continue
            try:
                data = json.loads(raw.decode("utf-8", errors="ignore"))
            except Exception:
                continue
            if isinstance(data, dict) and data.get("shared_secret"):
                mafiles.append((name, data))

        if not mafiles:
            _edit_menu(chat_id, msg_id,
                       "❌ В zip не найдено .maFile-подобных файлов "
                       "(JSON с полем shared_secret).", kb_back)
            return
        if not passwords_map:
            _edit_menu(chat_id, msg_id,
                       "❌ В zip нет <code>passwords.txt</code>.\n\n"
                       "Нужен файл с линями <code>login:password</code>.",
                       kb_back)
            return

        _edit_menu(chat_id, msg_id,
                   f"⏳ Импорт запущен: найдено {len(mafiles)} .maFile.\n"
                   f"Проверяю логины...", kb_back)

        def _bg():
            added: list[str] = []
            skipped: list[str] = []
            failed: list[str] = []
            for _path, ma in mafiles:
                login = ma.get("account_name", "").strip()
                if not login:
                    skipped.append(f"{_path}: нет account_name")
                    continue
                pw = passwords_map.get(login.lower())
                if not pw:
                    skipped.append(f"{login}: нет пароля в passwords.txt")
                    continue
                alias = aliases_map.get(login.lower()) or login[:16]
                alias = re.sub(r"[^A-Za-z0-9_-]", "", alias)[:16] or "acc"
                base_alias = alias
                n = 1
                while find_account(alias):
                    n += 1
                    alias = f"{base_alias[:14]}_{n}"[-16:]
                    if n > 50:
                        break
                acc = {
                    "alias": alias,
                    "account_name": login,
                    "password": pw,
                    "shared_secret": ma["shared_secret"],
                    "identity_secret": ma.get("identity_secret", ""),
                    "steamid": (str(ma.get("Session", {}).get("SteamID", ""))
                                or None),
                    "mafile": ma,
                    "frozen": False,
                    "game": "",
                    "login_failures": 0,
                    "cost": float(costs_map.get(login.lower(), 0.0)),
                }
                try:
                    s = SteamSession(login, pw, ma["shared_secret"],
                                      ma.get("identity_secret", ""))
                    s.login()
                    acc["steamid"] = s.steamid
                    upsert_account(acc)
                    _track_login_result(alias, True)
                    added.append(f"{alias} ({login})")
                except Exception as exc:
                    # Сохраняем, но помечаем как frozen с причиной "login failed".
                    acc["frozen"] = True
                    acc["freeze_reason"] = f"import: login failed ({str(exc)[:100]})"
                    acc["freeze_ts"] = _now()
                    upsert_account(acc)
                    failed.append(f"{alias} ({login}): {str(exc)[:80]}")
            lines = [f"<b>📥 Импорт завершён</b>\n"]
            lines.append(f"✅ Добавлено: {len(added)}")
            lines.append(f"❌ Ошибка логина (сохранено как frozen): {len(failed)}")
            lines.append(f"➖ Пропущено: {len(skipped)}")
            if added:
                lines.append("\n<b>Добавлены:</b>")
                for x in added[:30]:
                    lines.append(f"  • <code>{_esc(x)}</code>")
                if len(added) > 30:
                    lines.append(f"  … и ещё {len(added) - 30}")
            if failed:
                lines.append("\n<b>Ошибки:</b>")
                for x in failed[:10]:
                    lines.append(f"  • <code>{_esc(x)}</code>")
                if len(failed) > 10:
                    lines.append(f"  … и ещё {len(failed) - 10}")
            if skipped:
                lines.append("\n<b>Пропущены:</b>")
                for x in skipped[:10]:
                    lines.append(f"  • <code>{_esc(x)}</code>")
            _edit_menu(chat_id, msg_id, "\n".join(lines), kb_back)
            _update_lot_activation(cardinal)

        threading.Thread(target=_bg, daemon=True,
                         name="steam_rental-bulk-import").start()

    def _finalize_add(uid: int) -> None:
        st = _pending_state.pop(uid, None)
        if not st:
            return
        ma = st["mafile"]
        alias = st["alias"]
        chat_id = st["chat_id"]
        msg_id = st.get("main_msg_id")
        try:
            cost = float(st.get("cost", 0.0) or 0.0)
        except (TypeError, ValueError):
            cost = 0.0
        acc = {
            "alias": alias,
            "account_name": ma["account_name"],
            "password": st["password"],
            "shared_secret": ma["shared_secret"],
            "identity_secret": ma["identity_secret"],
            "steamid": str(ma.get("Session", {}).get("SteamID", "")) or None,
            "mafile": ma,
            "frozen": False,
            "game": "",
            "login_failures": 0,
            "cost": cost,
        }
        upsert_account(acc)
        kb = tbtypes.InlineKeyboardMarkup()
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ К аккаунту", callback_data=f"sr:acc:{_sid(alias)}"))
        _edit_menu(chat_id, msg_id,
                   f"⏳ Аккаунт <b>{_esc(alias)}</b> сохранён. "
                   f"Проверяю логин в Steam...", kb)
        def _verify():
            try:
                s = SteamSession(acc["account_name"], acc["password"],
                                  acc["shared_secret"], acc["identity_secret"])
                s.login()
                _track_login_result(alias, True)
                with _lock:
                    a = find_account(alias) or acc
                    a["steamid"] = s.steamid
                    upsert_account(a)
                _edit_menu(chat_id, msg_id,
                    f"✅ <b>{_esc(alias)}</b> добавлен и проверен.\n"
                    f"SteamID: <code>{s.steamid}</code>", kb)
            except Exception as exc:
                _track_login_result(alias, False)
                _edit_menu(chat_id, msg_id,
                    f"⚠️ <b>{_esc(alias)}</b> сохранён, но логин не "
                    f"прошёл:\n<code>{_esc(str(exc))}</code>\n\n"
                    f"Проверь логин/пароль/.maFile.", kb)
        threading.Thread(target=_verify, daemon=True).start()

    # ───── Регистрация хэндлеров в telebot ───────────────────────────────
    tg.msg_handler(cmd_srental, commands=["srental"])
    tg.msg_handler(cmd_cancel, commands=["srental_cancel"])
    tg.msg_handler(cmd_stats, commands=["srental_stats"])
    tg.msg_handler(cmd_acc_stats, commands=["srental_acc_stats"])
    tg.msg_handler(_handle_pending_text, func=_is_pending_text)
    tg.msg_handler(_handle_pending_doc, func=_is_pending_doc,
                   content_types=["document"])
    tg.cbq_handler(on_cb, lambda c: (c.data or "").startswith("sr:"))

    # Handle settings page callback from FPC plugin card (47:{UUID}:offset)
    def _settings_card_cb(call):
        if not _is_admin_user(call.from_user.id):
            tg.bot.answer_callback_query(call.id, "Нет доступа.")
            return
        chat_id = call.message.chat.id
        msg_id = call.message.message_id
        _edit_menu(chat_id, msg_id, _text_settings(), _kb_settings())
        tg.bot.answer_callback_query(call.id)

    tg.cbq_handler(_settings_card_cb, lambda c: (c.data or "").startswith(f"47:{UUID}"))

    # /srental_guide — гайд
    def cmd_guide(m) -> None:
        guide_text = (
            "<b>📖 Steam Rental — Гайд</b>\n\n"
            "<b>Что делает:</b>\n"
            "Автоматическая аренда Steam-аккаунтов через FunPay. "
            "Покупатель платит → получает логин/пароль на время → "
            "по истечении пароль меняется. Поддерживает обычный режим "
            "(логин/пароль) и Remote Play (Steam Link PIN).\n\n"
            "<b>Быстрый старт:</b>\n"
            "1. /srental → 📋 Аккаунты → ➕ Добавить\n"
            "2. Загрузите .maFile (shared_secret + identity_secret)\n"
            "3. /srental → 🎯 Лоты → ➕ Добавить — привяжите lot_id "
            "к alias и задайте длительность\n"
            "4. (опц.) Установите бонус за отзыв и стоп-минуты\n"
            "5. Готово — бот выдаёт после оплаты\n\n"
            "<b>Как работает:</b>\n"
            "• Покупатель оплачивает лот → бот выдаёт логин/пароль "
            "(или PIN для Remote Play)\n"
            "• Покупатель пишет !код — приходит Steam Guard (TOTP, 30 сек)\n"
            "• По истечении срока — пароль меняется автоматически, "
            "Remote Play — отключается\n\n"
            "<b>📨 Команды покупателя (обычная аренда):</b>\n"
            "• <code>!код [логин]</code> — Steam Guard. Без логина — "
            "для активного заказа покупателя\n"
            "• <code>!продлить</code> — ссылка на лот продления\n"
            "• <code>!статус</code> — сколько времени осталось\n"
            "• <code>!помощь</code> — список команд\n\n"
            "<b>🎮 Команды покупателя (Remote Play):</b>\n"
            "• <code>!пин</code> / <code>!pin</code> — выдать новый Steam Link PIN\n"
            "• <code>!статусrp</code> / <code>!statusrp</code> — статус сессии\n"
            "• <code>!помощьrp</code> / <code>!helprp</code> — справка\n\n"
            "<b>🔖 Хэштеги в описании лота FunPay:</b>\n"
            "Можно переопределить параметры аренды прямо в описании лота "
            "(не трогая настройки в /srental):\n"
            "• <code>#Hours: 2</code> — длительность аренды в часах (highest priority)\n"
            "• <code>#Time: 2ч</code> — длительность аренды с суффиксами\n"
            "• <code>#Review: 1h</code> — бонус за отзыв 5★ / штраф при удалении\n\n"
            "Поддерживаемые суффиксы:\n"
            "  ▫️ минуты: <code>m</code>, <code>min</code>, <code>мин</code>, "
            "<code>минут</code> (или без суффикса)\n"
            "  ▫️ часы: <code>h</code>, <code>ч</code>, <code>час</code>, "
            "<code>часа</code>, <code>часов</code>\n"
            "  ▫️ дни: <code>d</code>, <code>д</code>, <code>дн</code>, "
            "<code>день</code>, <code>дня</code>, <code>дней</code>\n"
            "  ▫️ недели: <code>w</code>, <code>нед</code>, <code>неделя</code>, "
            "<code>недель</code>\n\n"
            "Примеры: <code>#Time: 2ч</code>, <code>#Time:120m</code>, "
            "<code>#Time: 1д</code>, <code>#Time: 1w</code>, "
            "<code>#Review: 60</code> (= 60 минут).\n\n"
            "<b>Приоритет длительности:</b>\n"
            "1. <code>#Hours:</code> в описании лота (highest)\n"
            "2. <code>#Time:</code> в описании лота\n"
            "3. duration_min из настроек лота в /srental\n"
            "4. Парсинг описания лота (фразы «2 часа», «30 мин» и т.п.)\n\n"
            "<b>Основные функции:</b>\n"
            "• Автовыдача логина/пароля при оплате\n"
            "• TOTP Steam Guard через .maFile\n"
            "• Напоминания за N минут до конца аренды\n"
            "• Бонус за отзыв 5★ (+время) / штраф при удалении (−время)\n"
            "• Авто-продление через специальные extension-лоты\n"
            "• Очередь ожидания, когда все аккаунты заняты\n"
            "• Авто-смена пароля по окончании\n"
            "• Чёрный список покупателей\n\n"
            "<b>Защита и проверки:</b>\n"
            "• PC-Club режим — AI-проверка фото покупателя из ПК-клуба "
            "(распознаёт reuse картинок, проверяет видимость кода и ника)\n"
            "• Ручная фото-проверка — pending-pool ордеров на ваш ручной аппрув\n"
            "• Ban-сканер — VAC/Trade/Game/Community ban мониторинг "
            "→ авто-заморозка аккаунта\n"
            "• Авто-заморозка после N подряд ошибок логина\n"
            "• Капча/auth fail-обработка с уведомлением\n\n"
            "<b>Remote Play особенности:</b>\n"
            "• Один и тот же аккаунт может быть в пуле «rental», "
            "«remoteplay» или «both»\n"
            "• Anti-cheat скриншоты + AI-анализ подозрительной активности\n"
            "• Авто-дисконнект при детекте читов\n"
            "• Подробнее: /srental → 🎮 Remote Play → 📖 Гайд RP\n\n"
            "<b>Аналитика:</b>\n"
            "• История заказов + CSV экспорт\n"
            "• Статистика по аккаунтам и покупателям\n"
            "• Daily summary в Telegram\n"
            "• Prometheus метрики (опционально)\n"
            "• SQLite зеркало (через плагин Steam SQLite)\n\n"
            "<b>Массовые действия:</b>\n"
            "• Bulk-разморозка / заморозка\n"
            "• Bulk-проверка банов\n"
            "• Bulk-смена пароля\n\n"
            "<b>Telegram-команды:</b>\n"
            "/srental — главное меню\n"
            "/sremoteplay — меню Remote Play\n"
            "/srental_guide — этот гайд\n"
            "/srental_test — тест Steam Guard на первом аккаунте\n"
            "/srp_guide — гайд Remote Play\n"
            "/srp_test — тест RP-логина\n"
            "/srp_add — добавить RP-аккаунт командой\n"
            "/srp_lot — добавить RP-лот командой"
        )
        tg.bot.send_message(m.chat.id, guide_text, parse_mode="HTML")

    tg.msg_handler(cmd_guide, commands=["srental_guide"])

    # /srental_test — тест Steam Guard на первом аккаунте
    def cmd_test(m) -> None:
        kb = tbtypes.InlineKeyboardMarkup(row_width=2)
        kb.add(
            tbtypes.InlineKeyboardButton(
                "🌐 Реальный тест", callback_data="sr:test:REAL"),
            tbtypes.InlineKeyboardButton(
                "🎭 Фейковый тест", callback_data="sr:test:FAKE"),
        )
        tg.bot.send_message(
            m.chat.id,
            "🧪 <b>Выберите тип теста:</b>\n\n"
            "🌐 <b>Реальный</b> — подключение к Steam (первый аккаунт)\n"
            "🎭 <b>Фейковый</b> — проверка логики генерации без подключения",
            parse_mode="HTML",
            reply_markup=kb,
        )

    tg.msg_handler(cmd_test, commands=["srental_test"])

    # ── Remote Play Telegram commands ────────────────────────────────────
    def cmd_sremoteplay(message):
        if not _is_admin_user(message.from_user.id):
            return
        _srp_show_main_menu(tg.bot, message.chat.id)

    def cmd_srp_guide(message):
        if not _is_admin_user(message.from_user.id):
            return
        guide_text = (
            "<b>📖 Steam Remote Play — Гайд</b>\n\n"
            "<b>Что делает:</b>\n"
            "Аренда Steam-аккаунтов через Remote Play. Бот логинится в Steam, "
            "генерирует Steam Link PIN (4 цифры) — покупатель подключает "
            "свой Steam Link/Deck/PC к аккаунту по PIN и играет удалённо. "
            "По истечении срока сессия принудительно отключается.\n\n"
            "<b>Быстрый старт:</b>\n"
            "1. /sremoteplay → главное меню\n"
            "2. Добавить аккаунт через UI или командой:\n"
            "   <code>/srp_add alias login password shared identity [steamid]</code>\n"
            "3. Создать лот:\n"
            "   <code>/srp_lot keyword duration_min alias1,alias2 game</code>\n"
            "4. На самом Steam-аккаунте включить Remote Play "
            "(Steam → Настройки → Remote Play → Разрешить)\n"
            "5. Убедиться, что в пуле аккаунта стоит «remoteplay» или «both»\n\n"
            "<b>Как работает:</b>\n"
            "• Покупатель оплачивает лот → бот логинится в Steam\n"
            "• Генерируется Steam Link PIN (4 цифры)\n"
            "• Покупатель вводит PIN → подключается через Steam Link\n"
            "• По таймеру → сессия принудительно отключается\n\n"
            "<b>📨 Команды покупателя:</b>\n"
            "• <code>!пин</code> / <code>!pin</code> — получить новый PIN\n"
            "• <code>!статусrp</code> / <code>!statusrp</code> — статус сессии и таймер\n"
            "• <code>!помощьrp</code> / <code>!helprp</code> — справка\n\n"
            "<b>🔖 Хэштеги в описании лота:</b>\n"
            "Те же что в обычной аренде: <code>#Time: 2ч</code>, "
            "<code>#Review: 30m</code> (см. /srental_guide для всех суффиксов).\n\n"
            "<b>Защита и анти-чит:</b>\n"
            "• Периодические скриншоты Remote Play сессии\n"
            "• AI-анализ подозрительной активности (cheat-overlay, "
            "сторонние окна)\n"
            "• Авто-дисконнект при детекте читов\n"
            "• Лог всех PIN-выдач\n\n"
            "<b>Особенности пулов:</b>\n"
            "Аккаунт может быть в одном из пулов:\n"
            "  ▫️ <code>rental</code> — только обычная аренда (логин/пароль)\n"
            "  ▫️ <code>remoteplay</code> — только Remote Play\n"
            "  ▫️ <code>both</code> — оба режима (бот выберет по типу лота)\n\n"
            "<b>Telegram-команды:</b>\n"
            "/sremoteplay — главное меню Remote Play\n"
            "/srp_guide — этот гайд\n"
            "/srp_test — тест RP-логина на первом аккаунте\n"
            "/srp_add — быстрое добавление RP-аккаунта\n"
            "/srp_lot — быстрое добавление RP-лота"
        )
        tg.bot.send_message(message.chat.id, guide_text, parse_mode="HTML")

    def cmd_srp_test(message):
        if not _is_admin_user(message.from_user.id):
            return
        accs = list_accounts()
        if not accs:
            tg.bot.send_message(
                message.chat.id,
                "❌ <b>Test impossible:</b> no accounts.\n"
                "Add one via <code>/srp_add alias login pass shared identity</code>",
                parse_mode="HTML",
            )
            return

        acc = accs[0]
        al = acc.get("alias", "?")
        tg.bot.send_message(
            message.chat.id,
            f"🔄 Testing Steam login for <code>{al}</code>...",
            parse_mode="HTML",
        )

        def _worker():
            try:
                sess = SteamSession(
                    account_name=acc["account_name"],
                    password=acc["password"],
                    shared_secret=acc["shared_secret"],
                    identity_secret=acc["identity_secret"],
                    steamid=acc.get("steamid"),
                )
                sess.login()
                tg.bot.send_message(
                    message.chat.id,
                    f"✅ <b>Test passed!</b>\n\n"
                    f"🎮 Alias: <code>{al}</code>\n"
                    f"👤 Login: <code>{acc['account_name']}</code>\n"
                    f"🆔 SteamID: <code>{sess.steamid}</code>\n\n"
                    f"Steam login OK. Remote Play ready!",
                    parse_mode="HTML",
                )
            except Exception as ex:
                tg.bot.send_message(
                    message.chat.id,
                    f"❌ <b>Test failed!</b>\n\n"
                    f"🎮 Alias: <code>{al}</code>\n"
                    f"Error: <code>{str(ex)[:200]}</code>",
                    parse_mode="HTML",
                )

        threading.Thread(target=_worker, daemon=True).start()

    def cmd_srp_add(message):
        if not _is_admin_user(message.from_user.id):
            return
        _srp_tg_add_account(tg.bot, message)

    def cmd_srp_lot(message):
        if not _is_admin_user(message.from_user.id):
            return
        _srp_tg_add_lot(tg.bot, message)

    tg.msg_handler(cmd_sremoteplay, commands=["sremoteplay"])
    tg.msg_handler(cmd_srp_guide, commands=["srp_guide"])
    tg.msg_handler(cmd_srp_test, commands=["srp_test"])
    tg.msg_handler(cmd_srp_add, commands=["srp_add"])
    tg.msg_handler(cmd_srp_lot, commands=["srp_lot"])

    # srp: callback handler
    def _srp_callback_filter(call):
        return (call.data or "").startswith("srp:")

    @tg.bot.callback_query_handler(func=_srp_callback_filter)
    def _srp_callback_handler(call):
        if not _is_admin_user(call.from_user.id):
            return
        _srp_handle_callback(cardinal, tg.bot, call)

    try:
        cardinal.add_telegram_commands(UUID, [
            ("srental", "Steam Rental: открыть меню", True),
            ("srental_stats", "Steam Rental: статистика и финансы", True),
            ("srental_acc_stats", "Steam Rental: статистика по аккаунту", True),
            ("srental_cancel", "Steam Rental: отменить ввод", False),
            ("srental_guide", "Steam Rental: гайд", True),
            ("srental_test", "Steam Rental: тест", True),
            ("sremoteplay", "Steam Remote Play: меню", True),
            ("srp_guide", "Steam Remote Play: гайд", True),
            ("srp_test", "Steam Remote Play: тест", True),
            ("srp_add", "Steam Remote Play: добавить аккаунт", False),
            ("srp_lot", "Steam Remote Play: добавить лот", False),
        ])
    except Exception:
        LOGGER.debug("steam_rental: add_telegram_commands failed", exc_info=True)


# ── Remote Play TG helpers ────────────────────────────────────────────────────

def _srp_show_main_menu(bot: Any, chat_id: int) -> None:
    """Main menu for /sremoteplay."""
    from telebot import types as tbtypes  # type: ignore

    sessions = list_rp_sessions()
    active_count = sum(1 for s in sessions.values()
                       if s.get("status") == "active")
    accs = list_accounts()
    lots = list_lots()
    rp_lots = sum(1 for v in lots.values() if v.get("type") == "remoteplay")

    text = (
        "🔗 <b>Steam Remote Play - Panel</b>\n\n"
        f"📦 Accounts: <b>{len(accs)}</b>\n"
        f"🎮 RP Lots: <b>{rp_lots}</b>\n"
        f"▶️ Active RP sessions: <b>{active_count}</b>\n"
    )

    markup = tbtypes.InlineKeyboardMarkup(row_width=2)
    markup.add(
        tbtypes.InlineKeyboardButton(
            "▶️ Sessions", callback_data="srp:menu:sessions"),
        tbtypes.InlineKeyboardButton(
            "⚙️ Settings", callback_data="srp:menu:settings"),
    )
    markup.add(
        tbtypes.InlineKeyboardButton(
            "🛡️ Monitoring", callback_data="srp:menu:monitor"),
        tbtypes.InlineKeyboardButton(
            "📊 Stats", callback_data="srp:menu:stats"),
    )

    bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)


def _srp_handle_callback(cardinal: "Cardinal", bot: Any, call: Any) -> None:
    """Callback handler for srp: buttons."""
    from telebot import types as tbtypes  # type: ignore

    data = call.data
    chat_id = call.message.chat.id

    try:
        parts = data.split(":")
        if len(parts) < 3:
            bot.answer_callback_query(call.id)
            return

        action = parts[1]
        param = parts[2] if len(parts) > 2 else ""

        if action == "menu":
            if param == "sessions":
                sessions = list_rp_sessions()
                active = {k: v for k, v in sessions.items()
                          if v.get("status") == "active"}
                if not active:
                    text = "▶️ <b>Active RP Sessions</b>\n\nNo active sessions."
                else:
                    lines = ["▶️ <b>Active RP Sessions</b>\n"]
                    for sid, s in active.items():
                        time_left = max(0, s.get("expires_at", 0) - _now())
                        lines.append(
                            f"  🔗 <code>{s.get('alias')}</code> - "
                            f"{s.get('buyer_username', '?')}\n"
                            f"     ⏰ Left: {_human_minutes(time_left // 60)}"
                        )
                    text = "\n".join(lines)
                markup = tbtypes.InlineKeyboardMarkup(row_width=1)
                for sid in list(active.keys())[:5]:
                    s = active[sid]
                    markup.add(
                        tbtypes.InlineKeyboardButton(
                            f"🛑 Stop {s.get('alias')}",
                            callback_data=f"srp:stop:{sid}"),
                    )
                markup.add(
                    tbtypes.InlineKeyboardButton(
                        "« Back", callback_data="srp:menu:main"),
                )
                bot.edit_message_text(text, chat_id, call.message.message_id,
                                      parse_mode="HTML", reply_markup=markup)

            elif param == "settings":
                cfg = get_config()
                text = (
                    "⚙️ <b>Remote Play Settings</b>\n\n"
                    f"📱 PIN TTL: {cfg.get('pin_ttl_seconds', 300)} sec\n"
                    f"🛡️ Monitoring: {'✅' if cfg.get('monitoring_enabled') else '❌'}\n"
                    f"🤖 AI anti-cheat: {'✅' if cfg.get('anticheat_ai_enabled') else '❌'}\n"
                    f"⚡ Auto-disconnect on cheat: "
                    f"{'✅' if cfg.get('auto_disconnect_on_cheat') else '❌'}\n"
                )
                markup = tbtypes.InlineKeyboardMarkup(row_width=1)
                markup.add(
                    tbtypes.InlineKeyboardButton(
                        "🛡️ Toggle monitoring",
                        callback_data="srp:toggle:monitoring_enabled"),
                    tbtypes.InlineKeyboardButton(
                        "🤖 Toggle AI anti-cheat",
                        callback_data="srp:toggle:anticheat_ai_enabled"),
                    tbtypes.InlineKeyboardButton(
                        "⚡ Toggle auto-disconnect",
                        callback_data="srp:toggle:auto_disconnect_on_cheat"),
                    tbtypes.InlineKeyboardButton(
                        "« Back", callback_data="srp:menu:main"),
                )
                bot.edit_message_text(text, chat_id, call.message.message_id,
                                      parse_mode="HTML", reply_markup=markup)

            elif param == "monitor":
                cfg = get_config()
                sessions = list_rp_sessions()
                active = [s for s in sessions.values()
                          if s.get("status") == "active"]
                total_alerts = sum(
                    len(s.get("cheat_alerts", []))
                    for s in sessions.values())
                text = (
                    "🛡️ <b>Anti-cheat Monitoring</b>\n\n"
                    f"Status: {'✅ Enabled' if cfg.get('monitoring_enabled') else '❌ Disabled'}\n"
                    f"AI: {'✅' if cfg.get('anticheat_ai_enabled') else '❌'}\n"
                    f"Interval: {cfg.get('monitoring_interval_seconds', 300)} sec\n"
                    f"Threshold: {cfg.get('anticheat_confidence_threshold', 70)}%\n\n"
                    f"▶️ Active sessions: {len(active)}\n"
                    f"🚨 Total alerts: {total_alerts}\n"
                )
                markup = tbtypes.InlineKeyboardMarkup(row_width=1)
                for s in active[:5]:
                    markup.add(
                        tbtypes.InlineKeyboardButton(
                            f"📸 Check {s.get('alias')}",
                            callback_data=f"srp:screen:{s['id']}"),
                    )
                markup.add(
                    tbtypes.InlineKeyboardButton(
                        "« Back", callback_data="srp:menu:main"),
                )
                bot.edit_message_text(text, chat_id, call.message.message_id,
                                      parse_mode="HTML", reply_markup=markup)

            elif param == "stats":
                sessions = list_rp_sessions()
                total_sessions = len(sessions)
                active_sessions = sum(1 for s in sessions.values()
                                      if s.get("status") == "active")
                ended_sessions = sum(1 for s in sessions.values()
                                     if s.get("status") == "ended")
                text = (
                    "📊 <b>Remote Play Stats</b>\n\n"
                    f"📦 Total sessions: {total_sessions}\n"
                    f"▶️ Active: {active_sessions}\n"
                    f"⏹ Ended: {ended_sessions}\n"
                )
                markup = tbtypes.InlineKeyboardMarkup(row_width=1)
                markup.add(
                    tbtypes.InlineKeyboardButton(
                        "« Back", callback_data="srp:menu:main"),
                )
                bot.edit_message_text(text, chat_id, call.message.message_id,
                                      parse_mode="HTML", reply_markup=markup)

            elif param == "main":
                _srp_show_main_menu(bot, chat_id)

        elif action == "screen":
            result = _rp_take_screenshot(param)
            if result.get("ok"):
                try:
                    with open(result["path"], "rb") as f:
                        bot.send_photo(
                            chat_id, f,
                            caption=f"📸 Screenshot session {param}\n"
                                    f"🕐 {_fmt_ts(result['timestamp'])}",
                        )
                except Exception:
                    bot.send_message(
                        chat_id,
                        f"📸 Screenshot saved: <code>{result['path']}</code>",
                        parse_mode="HTML",
                    )
            else:
                bot.send_message(
                    chat_id,
                    f"❌ Screenshot failed: {result.get('error', '?')}",
                    parse_mode="HTML",
                )

        elif action == "stop":
            ok = end_rp_session(cardinal, param, reason="operator_stop")
            if ok:
                bot.send_message(chat_id, "✅ Session terminated.",
                                 parse_mode="HTML")
            else:
                bot.send_message(chat_id,
                                 "❌ Session not found or already ended.",
                                 parse_mode="HTML")

        elif action == "newpin":
            result = regenerate_rp_pin(cardinal, param)
            if result.get("ok"):
                session = find_rp_session(param)
                bot.send_message(
                    chat_id,
                    f"✅ New PIN: <code>{result['pin']}</code>",
                    parse_mode="HTML",
                )
                if session and session.get("chat_id"):
                    time_left_sec = max(0, session.get("expires_at", 0) - _now())
                    try:
                        cardinal.send_message(
                            session["chat_id"],
                            _render_template(
                                "pin_generated",
                                buyer_id=session.get("buyer_id"),
                                pin=result["pin"],
                                time_left=_human_minutes(time_left_sec // 60),
                            ),
                            chat_name=session.get("buyer_username"),
                            interlocutor_id=session.get("buyer_id"),
                            watermark=False,
                        )
                    except Exception:
                        pass
            else:
                bot.send_message(
                    chat_id,
                    f"❌ Error: {result.get('error', '?')}",
                    parse_mode="HTML",
                )

        elif action == "toggle":
            cfg = get_config()
            current = bool(cfg.get(param, False))
            cfg[param] = not current
            save_config(cfg)
            status = "✅ Enabled" if not current else "❌ Disabled"
            bot.send_message(chat_id, f"⚙️ <code>{param}</code>: {status}",
                             parse_mode="HTML")

        bot.answer_callback_query(call.id)
    except Exception:
        LOGGER.error("steam_rental: srp callback error", exc_info=True)
        try:
            bot.answer_callback_query(call.id, "❌ Error")
        except Exception:
            pass


def _srp_tg_add_account(bot: Any, message: Any) -> None:
    """Command: /srp_add alias login password shared_secret identity_secret [steamid] [pool]"""
    parts = message.text.split(maxsplit=7)
    if len(parts) < 6:
        bot.reply_to(
            message,
            "Format: /srp_add alias login password shared_secret "
            "identity_secret [steamid] [pool]",
        )
        return

    alias = parts[1]
    login = parts[2]
    password = parts[3]
    shared_secret = parts[4]
    identity_secret = parts[5]
    steamid = parts[6] if len(parts) > 6 else None
    pool = parts[7] if len(parts) > 7 else "remoteplay"
    if pool not in ("rental", "remoteplay", "both"):
        pool = "remoteplay"

    if find_account(alias):
        bot.reply_to(message, f"❌ Alias <code>{alias}</code> already exists.",
                     parse_mode="HTML")
        return

    acc = {
        "alias": alias,
        "account_name": login,
        "password": password,
        "shared_secret": shared_secret,
        "identity_secret": identity_secret,
        "steamid": steamid,
        "game": "",
        "frozen": False,
        "added_at": _now(),
        "pool": pool,
    }
    upsert_account(acc)
    bot.reply_to(
        message,
        f"✅ Account <code>{alias}</code> ({login}) added to pool.\n"
        f"Pool: <code>{pool}</code>",
        parse_mode="HTML",
    )


def _srp_tg_add_lot(bot: Any, message: Any) -> None:
    """Command: /srp_lot lot_id duration_min alias1,alias2,... [game_name]"""
    parts = message.text.split(maxsplit=4)
    if len(parts) < 4:
        bot.reply_to(
            message,
            "Format: /srp_lot lot_id duration_min alias1,alias2 [game_name]",
        )
        return

    lot_id = parts[1]
    try:
        duration_min = int(parts[2])
    except ValueError:
        bot.reply_to(message, "❌ duration_min must be a number.")
        return
    aliases = [a.strip() for a in parts[3].split(",") if a.strip()]
    game = parts[4] if len(parts) > 4 else ""

    lot_warnings = set_lot(lot_id, duration_min=duration_min, aliases=aliases,
                           game=game, lot_type="remoteplay")
    reply_text = (
        f"✅ RP Lot <code>{lot_id}</code> saved.\n"
        f"Duration: {duration_min} min\n"
        f"Accounts: {', '.join(aliases)}\n"
        f"Game: {game or '-'}\n"
        f"Type: remoteplay"
    )
    if lot_warnings:
        reply_text += "\n\n⚠️ <b>Warnings:</b>\n" + "\n".join(
            f"• {w}" for w in lot_warnings)
    bot.reply_to(message, reply_text, parse_mode="HTML")


def _open_settings_page(cardinal: "Cardinal", msg) -> None:
    """FPC settings page handler - directs user to /srental."""
    tg = getattr(cardinal, "telegram", None)
    if tg is None:
        return
    tg.bot.send_message(
        msg.chat.id,
        "<b>Steam Rental</b>\n\n"
        "Для настройки используйте команду /srental\n"
        "Для гайда: /srental_guide\n"
        "Для теста: /srental_test",
        parse_mode="HTML",
    )


# ── Экспорт хэндлеров для FPC ───────────────────────────────────────────────
BIND_TO_SETTINGS_PAGE = _open_settings_page
BIND_TO_PRE_INIT = [_handler_pre_init]
BIND_TO_POST_START = [_handler_post_start]
BIND_TO_PRE_STOP = [_handler_pre_stop]
BIND_TO_NEW_ORDER = [_handler_new_order]
BIND_TO_NEW_MESSAGE = [_handler_new_message]
BIND_TO_ORDER_STATUS_CHANGED = [_handler_order_status_changed]



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
