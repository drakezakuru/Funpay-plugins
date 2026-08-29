from __future__ import annotations

import configparser
import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cardinal import Cardinal

import requests
import telebot

from Utils import cardinal_tools
from tg_bot import keyboards, CBT
from tg_bot.utils import NotificationTypes


NAME = "AI Chat Plugin"
VERSION = "2.1.0"
DESCRIPTION = "AI-powered auto-responder using OpenRouter API"
CREDITS = "@drakelovc"
UUID = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
SETTINGS_PAGE = True
BIND_TO_DELETE = None


# ══════════════════════════════════════════════════════════════════════════════
# 💛 DONATION BANNER — защита реквизитов автора.
# Реквизиты закодированы (base64 + SHA-256 подпись) и лежат ВНИЗУ файла в
# _donation_details(): если их подменить на свои, подпись не сойдётся и
# баннер НЕ отправится. True = 1 (вкл), False = 0 (выкл).
# ══════════════════════════════════════════════════════════════════════════════
DONATION_ENABLED = True                # True = 1 (показывать баннер), False = 0
DONATION_SHOW_ON_START = True          # True = 1 (слать при старте плагина)
DONATION_DAILY_ENABLED = True          # True = 1 (напоминание раз в сутки)
DONATION_DAILY_HOUR = 16               # час напоминания (0-23, МСК)
DONATION_CALLBACK_PREFIX = "aichat_dn"  # префикс колбэков кнопок баннера
DONATION_PLUGIN_NAME = "AI Chat Plugin"  # имя плагина в шапке баннера
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
            logger.debug(
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
                    logger.debug(
                        "donation reminder failed for uid=%s",
                        uid, exc_info=True)
        except Exception:
            logger.debug("donation reminder error", exc_info=True)
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


def _welcome_startup_text() -> str:
    """Текст приветственного сообщения при первом запуске плагина."""
    return (
        f"✨ <b>{DONATION_PLUGIN_NAME}</b> v{VERSION} запущен!\n\n"
        "Спасибо что выбрал этот плагин. 🎉\n\n"
        f"📦 <b>Другие плагины автора</b> и обновления — "
        f"в канале {AUTHOR_CHANNEL_USERNAME}:\n"
        f"<a href=\"{AUTHOR_CHANNEL_URL}\">{AUTHOR_CHANNEL_URL}</a>\n\n"
        "Подписывайся, чтобы не пропустить новые плагины и фичи. "
        "Если есть идеи/баги — пиши в канал 🙌"
    )


def _welcome_startup_kb():
    """Кнопка-ссылка на канал автора для приветственного сообщения."""
    from telebot import types as tbtypes  # type: ignore
    kb = tbtypes.InlineKeyboardMarkup(row_width=1)
    kb.add(
        tbtypes.InlineKeyboardButton(
            f"📦 Открыть канал {AUTHOR_CHANNEL_USERNAME}",
            url=AUTHOR_CHANNEL_URL),
    )
    return kb


def _send_startup_welcome(cardinal) -> bool:
    """Одноразово шлёт приветственное сообщение операторам при старте плагина.

    Общий для всех плагинов файл-замок (storage/plugins/_donation_mail/)
    создаётся атомарно через O_CREAT|O_EXCL: первый плагин, добежавший
    до приветствия, создаёт welcome_sent.lock и шлёт; остальные видят,
    что файл уже есть, и пропускают. Каждый плагин остаётся автономным,
    но оператор получает только одно сообщение на все плагины автора.
    """
    if not DONATION_ENABLED:
        return False
    if _donation_tampered():
        return False
    tg = getattr(cardinal, "telegram", None)
    if not tg or not getattr(tg, "bot", None):
        return False
    _dir = os.path.join("storage", "plugins", "_donation_mail")
    try:
        os.makedirs(_dir, exist_ok=True)
    except Exception:
        pass
    _lock = os.path.join(_dir, "welcome_sent.lock")
    try:
        _fd = os.open(_lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(_fd, "w") as _f:
            _f.write(__name__)
    except FileExistsError:
        return False
    except Exception:
        return False
    text = _welcome_startup_text()
    kb = None
    try:
        kb = _welcome_startup_kb()
    except Exception:
        kb = None
    targets = list(getattr(tg, "authorized_users", []) or [])
    for uid in targets:
        try:
            tg.bot.send_message(uid, text, parse_mode="HTML",
                                reply_markup=kb,
                                disable_web_page_preview=True)
        except Exception:
            logger.debug("welcome startup failed for uid=%s", uid,
                         exc_info=True)
    return True


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


# --- Internationalization (i18n) ---

LANG: dict[str, dict[str, str]] = {
    "ru": {
        "settings_title": "AI Chat Plugin v{version} - Настройки",
        "plugin_enabled": "Плагин включен",
        "auto_delivery_info": "Инфо авто-выдачи",
        "forward_questions": "Пересылка вопросов",
        "list_products": "Показ товаров при уточнении",
        "anti_spam": "Анти-спам",
        "working_hours": "Рабочие часы",
        "blacklist": "Чёрный список",
        "stop_words": "Стоп-слова",
        "upsell": "Допродажа",
        "promos": "Промо-коды",
        "language_detect": "Определение языка",
        "templates": "Шаблоны",
        "statistics": "Статистика",
        "model_label": "Модель",
        "api_key_label": "API ключ",
        "system_prompt_label": "Системный промпт",
        "holding_msg_label": "Сообщение ожидания",
        "max_history_label": "Макс. история",
        "timeout_label": "Таймаут",
        "manage_blacklist": "Управление чёрным списком",
        "manage_templates": "Управление шаблонами",
        "manage_stopwords": "Управление стоп-словами",
        "manage_promos": "Управление промо",
        "upsell_prompt_label": "Промпт допродажи",
        "view_stats": "Статистика",
        "current_settings": "Текущие настройки",
        "setting_changed": "Настройка изменена!",
        "back": "Назад",
        "add_user": "Добавить пользователя",
        "add_template": "Добавить шаблон",
        "add_stopword": "Добавить стоп-слово",
        "add_complaint_stopwords": "Стоп-слова жалоб",
        "complaint_stopwords_added": "Добавлено стоп-слов жалоб: {n}. Фильтр включён — такие диалоги теперь уходят продавцу.",
        "add_promo": "Добавить промо",
        "enter_model": "Введите название модели (напр. <code>openai/gpt-3.5-turbo</code>):",
        "enter_api_key": "Введите новый API ключ OpenRouter:",
        "enter_system_prompt": "Введите новый системный промпт:",
        "enter_holding_msg": "Введите сообщение ожидания (отправляется покупателю при пересылке):",
        "enter_max_history": "Введите максимальное количество сообщений истории (число):",
        "enter_timeout": "Введите таймаут API в секундах (число):",
        "enter_spam_limit": "Введите макс. сообщений в минуту (число):",
        "enter_spam_reply": "Введите сообщение при спаме:",
        "enter_wh_start": "Введите час начала (0-23):",
        "enter_wh_end": "Введите час окончания (0-23):",
        "enter_offline_msg": "Введите сообщение вне рабочих часов:",
        "enter_blacklist_user": "Введите имя пользователя для добавления в чёрный список:",
        "enter_template_name": "Введите название шаблона (короткий идентификатор, напр. 'warranty', 'refund'):",
        "enter_template_text": "Имя шаблона: <b>{name}</b>\n\nТеперь введите текст шаблона:",
        "enter_stopword": "Введите стоп-слово для добавления:",
        "enter_promo_code": "Введите промо-код (напр. SAVE10):",
        "enter_promo_desc": "Промо-код: <b>{code}</b>\n\nТеперь введите описание промо:",
        "enter_upsell_prompt": "Текущий промпт допродажи:\n<i>{current}</i>\n\nВведите новый текст промпта допродажи:",
        # Product catalog
        "product_catalog": "📦 Каталог товаров",
        "product_catalog_mgmt": "Управление каталогом товаров\n\nЗдесь вы можете добавить товары которые AI будет предлагать покупателям.",
        "product_added": "Товар <b>{name}</b> добавлен!",
        "product_removed": "Товар <b>{name}</b> удалён.",
        "enter_product_name": "Введите название товара:",
        "enter_product_price": "Товар: <b>{name}</b>\n\nВведите цену (число или текст, напр. '100 руб.' или 'от 500'): ",
        "enter_product_desc": "Товар: <b>{name}</b>, Цена: <b>{price}</b>\n\nВведите описание товара (кратко о чём товар, условия, особенности): ",
        "product_empty": "Каталог пуст. Добавьте товары чтобы AI мог отвечать покупателям.",
        "add_product": "➕ Добавить товар",
        "sync_products": "🔄 Загрузить с FunPay",
        "syncing_products": "Загружаю товары с FunPay...",
        "sync_products_done": "Загружено {n} лотов с FunPay",
        "sync_products_empty": "Cardinal не вернул лотов. Проверьте подключение.",
        "value_empty": "Значение не может быть пустым.",
        "invalid_number": "Введите корректное число.",
        "unknown_field": "Неизвестное поле.",
        "setting_updated": "Настройка <b>{field}</b> обновлена!",
        "stats_reset": "Статистика сброшена.",
        "reset_stats": "Сбросить статистику",
        "removed": "Удалено: {item}",
        "added_to_blacklist": "Пользователь <b>{user}</b> добавлен в чёрный список!",
        "template_added": "Шаблон <b>{name}</b> добавлен!",
        "stopword_added": "Стоп-слово <b>{word}</b> добавлено!",
        "promo_added": "Промо <b>{code}</b> добавлено!",
        "upsell_updated": "Промпт допродажи обновлён!",
        "back_to_settings": "Назад к настройкам",
        "back_to_blacklist": "Назад к чёрному списку",
        "back_to_templates": "Назад к шаблонам",
        "back_to_stopwords": "Назад к стоп-словам",
        "back_to_promos": "Назад к промо",
        "blacklist_mgmt": "Управление чёрным списком\n\nНажмите на пользователя для удаления или добавьте нового.",
        "templates_mgmt": "Управление шаблонами\n\nНажмите на шаблон для удаления или добавьте новый.",
        "stopwords_mgmt": "Управление стоп-словами\n\nНажмите на слово для удаления или добавьте новое.",
        "promos_mgmt": "Управление промо-кодами\n\nНажмите на код для удаления или добавьте новый.",
        "stats_title": "AI Chat Plugin - Статистика",
        "stats_today": "Сегодня",
        "stats_week": "За неделю",
        "messages_processed": "Сообщений обработано",
        "forwarded_to_seller": "Переслано продавцу",
        "tokens_used": "Токенов использовано",
        "guide_title": "AI Chat Plugin - Гайд",
        "guide_body": (
            "<b>Что делает:</b>\n"
            "Автоматически отвечает покупателям в чатах FunPay используя AI. "
            "Поддерживает несколько провайдеров: OpenRouter, OpenAI, Gemini, DeepSeek, Anthropic. "
            "Может классифицировать вопросы: стандартные — отвечает сам, нестандартные — "
            "пересылает продавцу в Telegram.\n\n"
            "<b>Быстрый старт:</b>\n"
            "1. Выберите провайдера: /aichat → ⚙️ Core → Provider\n"
            "2. Получите API ключ у выбранного провайдера\n"
            "3. /aichat → \U0001f511 API key → вставьте ключ\n"
            "4. /aichat → ⚙️ Core → \u2705 Plugin enabled\n"
            "5. Выберите пресет промпта или напишите свой System prompt\n\n"
            "<b>Готовые пресеты промптов:</b>\n"
            "• Продавец FunPay — общий магазин\n"
            "• Аренда Steam — заточен под аренду аккаунтов\n"
            "• Игровые товары — донат, ключи, скины\n"
            "• Свой промпт — полностью кастомный\n\n"
            "<b>Логика работы:</b>\n"
            "• Покупатель пишет → AI получает контекст (история, товары, инфо)\n"
            "• Стандартный вопрос → AI отвечает сам\n"
            "• Стоп-слово / нестандарт → пересылка продавцу в TG, "
            "покупателю — holding-сообщение\n"
            "• Anti-spam → если покупатель спамит, AI не отвечает\n"
            "• Working hours → вне часов отправляется offline-сообщение\n\n"
            "<b>Функции:</b>\n"
            "\u2022 Multi-provider — OpenRouter / OpenAI / Gemini / DeepSeek / Anthropic\n"
            "\u2022 Auto-learning — AI запоминает удачные ответы из ваших шаблонов "
            "и использует их в похожих вопросах\n"
            "\u2022 Anti-spam — лимит сообщений в минуту от одного покупателя\n"
            "\u2022 Working hours — AI отвечает только в заданные часы\n"
            "\u2022 Blacklist — игнор конкретных покупателей\n"
            "\u2022 Stop-words — пересылка продавцу при детекте ключевых слов\n"
            "\u2022 Templates — шаблоны быстрых ответов\n"
            "\u2022 Upsell — допродажа сопутствующих товаров\n"
            "\u2022 Promos — промо-коды в контексте AI\n"
            "\u2022 Language detect — ответ на языке покупателя (RU/EN/UA/...)\n"
            "\u2022 Statistics — обработанные сообщения, токены, пересылки\n"
            "\u2022 Multilang UI — интерфейс плагина на RU/EN\n\n"
            "<b>v2.1.0:</b>\n"
            "\u2022 Память с TTL — контекст диалога истекает по таймауту\n"
            "\u2022 Фильтр тем — запрещённые темы получают ответ-заглушку\n"
            "\u2022 Резервные модели — автопереключение при сбое основной\n"
            "\u2022 Эскалация — уведомление оператора по ключевым словам\n"
            "\u2022 Лог Q&A — запись вопросов/ответов для разбора\n"
            "\u2022 FAQ-кэш — частые вопросы без вызова модели\n"
            "\u2022 Пауза оператора — ИИ молчит, пока отвечает оператор\n"
            "\u2022 Мастер-переключатель и дневной бюджет токенов\n\n"
            "<b>Команды:</b>\n"
            "/aichat — открыть меню настроек\n"
            "/aichat_guide — этот гайд\n"
            "/aichat_test — тест подключения к AI-провайдеру"
        ),
        "test_testing": "Тестирую подключение к OpenRouter API...",
        "test_passed": "Тест пройден!",
        "test_failed": "Тест не пройден!",
        "test_no_key": "Тест не пройден: API ключ не настроен.\nОткройте /aichat - API key и введите ключ OpenRouter.",
        "language_label": "Язык: {lang}",
        "language_ru": "Русский",
        "language_en": "English",
        # Settings display
        "core_section": "Основное",
        "status_label": "Статус",
        "enabled_text": "Включен",
        "disabled_text": "Выключен",
        "prompt_section": "Промпт",
        "auto_delivery_section": "Авто-выдача",
        "info_in_context": "Инфо в контексте",
        "forwarding_section": "Пересылка",
        "forward_label": "Пересылка",
        "message_label": "Сообщение",
        "features_section": "Функции",
        "not_set": "не задан",
        # Provider & preset
        "provider_label": "Провайдер",
        "prompt_preset_label": "Пресет промпта",
        "provider_openrouter": "OpenRouter",
        "provider_openai": "OpenAI",
        "provider_gemini": "Gemini",
        "provider_deepseek": "DeepSeek",
        "provider_anthropic": "Anthropic",
        "preset_funpay_seller": "Продавец FunPay",
        "preset_steam_rental": "Аренда Steam",
        "preset_game_items": "Игровые товары",
        "preset_custom": "Свой промпт",
        "select_provider": "Выберите провайдера AI:",
        "select_preset": "Выберите пресет системного промпта:",
        "provider_changed_to": "Провайдер изменён на",
        "provider_set_model_hint": "Не забудьте выбрать модель для этого провайдера (кнопка 🤖 Модель — там есть пресеты).",
        "provider_examples": "Примеры",
        "unknown_provider": "Неизвестный провайдер",
        # Model presets
        "select_model": "Выберите модель:",
        "model_manual": "Ввести вручную",
        "model_testing": "Модель {model} сохранена. Тестирую подключение...",
        # Custom providers
        "cprovider_add": "Добавить провайдера",
        "cprovider_manage": "Управление провайдерами",
        "cprovider_delete_hint": "Нажмите на провайдера для удаления. ⭐ — активный.",
        "cprovider_note": "Кастомных провайдеров: {count}",
        "no_custom_providers": "Кастомных провайдеров пока нет. Нажмите ➕ чтобы добавить свой (любой OpenAI-совместимый API).",
        "cprovider_removed": "Провайдер {name} удалён",
        "enter_cprovider_name": "Новый провайдер — шаг 1/4.\n\nНазвание (латиница/цифры/_, 2-20 символов):",
        "invalid_cprovider_name": "Название должно содержать только латиницу, цифры и _ (2-20 символов).",
        "cprovider_reserved": "Это название занято встроенным провайдером.",
        "cprovider_exists": "Провайдер с таким названием уже существует.",
        "enter_cprovider_url": "Шаг 2/4.\n\nBase URL провайдера (например https://api.together.xyz/v1):",
        "invalid_cprovider_url": "URL должен начинаться с http:// или https://",
        "enter_cprovider_models": "Шаг 3/4.\n\nМодели через запятую (сохранятся как пресеты в меню модели). Отправьте '-', чтобы пропустить:",
        "enter_cprovider_key": "Шаг 4/4.\n\nAPI ключ этого провайдера (или '-', чтобы использовать общий ключ):",
        "cprovider_added": "Провайдер {name} добавлен!",
        "cprovider_models_count": "Моделей сохранено: {count}",
        "cprovider_make_active": "Сделать активным провайдером",
        # Setting descriptions
        "desc_plugin_enabled": "Включает/выключает AI-ответы на сообщения покупателей",
        "desc_auto_delivery_info": "AI сообщает покупателю об автовыдаче после оплаты",
        "desc_forward_questions": "Пересылать нестандартные вопросы продавцу в Telegram",
        "desc_notify_standard": "Уведомлять продавца когда AI ответил покупателю сам",
        "notify_standard_label": "Уведом при ответе AI",
        "desc_list_products": "Показывать список товаров когда покупатель уточняет",
        "desc_anti_spam": "Ограничение количества сообщений от покупателя в минуту",
        "desc_working_hours": "AI отвечает только в заданные часы, вне часов - отправляет offline-сообщение",
        "desc_blacklist": "Игнорирование сообщений от указанных покупателей",
        "desc_stop_words": "При обнаружении ключевых слов - пересылка продавцу вместо AI-ответа",
        "desc_upsell": "AI предлагает сопутствующие товары в ответах",
        "desc_promos": "AI упоминает промо-коды в контексте ответов",
        "desc_language_detect": "Определяет язык покупателя и отвечает на нём",
        "desc_templates": "Шаблоны быстрых ответов для типовых вопросов",
        "desc_statistics": "Сбор статистики: обработанные сообщения, токены, пересылки",
        "desc_model": "Название AI-модели (зависит от выбранного провайдера)",
        "desc_api_key": "API ключ для доступа к выбранному AI-провайдеру",
        "desc_system_prompt": "Системная инструкция - определяет поведение и стиль AI",
        "desc_holding_msg": "Сообщение покупателю пока вопрос пересылается продавцу",
        "desc_max_history": "Сколько сообщений хранить для контекста разговора",
        "desc_timeout": "Таймаут ожидания ответа от AI API (секунды)",
        # Notification texts
        "ai_manual_attention": "AI Chat Plugin - Требуется внимание",
        "ai_stopword_detected": "AI Chat Plugin - Обнаружено стоп-слово",
        "buyer_context": "Контекст покупателя",
        "auto_learning": "Авто-обучение",
        "manage_learned": "Обученные ответы",
        "learned_empty": "Нет обученных ответов",
        "learned_deleted": "Ответ удален",
        # v2.1.0 enhancement labels
        "memory_ttl_label": "Память TTL (сек)",
        "fallback_models_label": "Резервные модели",
        "budget": "Бюджет токенов",
        "budget_limit_label": "Дневной лимит",
        "budget_unit_label": "Единица бюджета",
        "budget_alert_label": "Порог оповещения",
        "test_model": "🧪 Тест модели",
        "topic_filter": "Фильтр тем",
        "manage_topic_filter": "Запрещённые темы",
        "topic_canned_label": "Ответ-заглушка",
        "add_topic": "Добавить тему",
        "escalation": "Эскалация",
        "manage_escalation": "Слова эскалации",
        "add_escalation": "Добавить слово",
        "escalation_pause": "Пауза при эскалации",
        "operator_pause": "Пауза оператора",
        "pause_timeout_label": "Таймаут паузы (сек)",
        "faq": "FAQ-кэш",
        "manage_faq": "Управление FAQ",
        "add_faq": "Добавить FAQ",
        "qa_log": "Лог Q&A",
        "view_logs": "📋 Логи",
        "logs_title": "Последние логи плагина:",
        "logs_empty": "Логи пусты.",
        "enter_memory_ttl": "Введите TTL памяти в секундах (целое > 0):",
        "enter_fallback_models": "Введите резервные модели через запятую (пусто — очистить):",
        "enter_budget_limit": "Введите дневной лимит (целое > 0):",
        "enter_budget_unit": "Введите единицу бюджета: tokens или requests:",
        "enter_budget_alert": "Введите порог оповещения (целое > 0):",
        "enter_pause_timeout": "Введите таймаут паузы оператора в секундах (целое > 0):",
        "enter_topic_canned": "Введите ответ-заглушку для запрещённых тем:",
        "enter_topic": "Введите запрещённую тему / ключевое слово:",
        "enter_escalation": "Введите ключевое слово эскалации:",
        "enter_faq_patterns": "Введите шаблоны FAQ через запятую (ключевые слова):",
        "enter_faq_answer": "Шаблоны: <b>{patterns}</b>\n\nТеперь введите ответ FAQ:",
        "topic_added": "Тема <b>{item}</b> добавлена!",
        "escalation_added": "Слово <b>{item}</b> добавлено!",
        "faq_added": "FAQ-запись добавлена!",
        "plugin_on": "Плагин включён",
        "plugin_off": "Плагин выключен",
        "topic_mgmt": "Запрещённые темы\n\nНажмите на запись для удаления или добавьте новую.",
        "escalation_mgmt": "Слова эскалации\n\nНажмите на запись для удаления или добавьте новую.",
        "faq_mgmt": "FAQ-записи\n\nНажмите на запись для удаления или добавьте новую.",
        "back_to_topic": "Назад к фильтру тем",
        "back_to_escalation": "Назад к эскалации",
        "back_to_faq": "Назад к FAQ",
        "page_label": "Стр. {page}/{total}",
    },
    "en": {
        "settings_title": "AI Chat Plugin v{version} - Settings",
        "plugin_enabled": "Plugin enabled",
        "auto_delivery_info": "Auto-delivery info",
        "forward_questions": "Forward questions",
        "list_products": "List products on clarify",
        "anti_spam": "Anti-spam",
        "working_hours": "Working hours",
        "blacklist": "Blacklist",
        "stop_words": "Stop-words",
        "upsell": "Upsell",
        "promos": "Promos",
        "language_detect": "Language detect",
        "templates": "Templates",
        "statistics": "Statistics",
        "model_label": "Model",
        "api_key_label": "API key",
        "system_prompt_label": "System prompt",
        "holding_msg_label": "Holding message",
        "max_history_label": "Max history",
        "timeout_label": "Timeout",
        "manage_blacklist": "Manage Blacklist",
        "manage_templates": "Manage Templates",
        "manage_stopwords": "Manage Stop-words",
        "manage_promos": "Manage Promos",
        "upsell_prompt_label": "Upsell prompt",
        "view_stats": "View Stats",
        "current_settings": "Current settings",
        "setting_changed": "Setting changed!",
        "back": "Back",
        "add_user": "Add user",
        "add_template": "Add template",
        "add_stopword": "Add stop-word",
        "add_complaint_stopwords": "Complaint stop-words",
        "complaint_stopwords_added": "Added complaint stop-words: {n}. Filter enabled \u2014 such chats are now forwarded to the seller.",
        "add_promo": "Add promo",
        "enter_model": "Enter model name (e.g. <code>openai/gpt-3.5-turbo</code>):",
        "enter_api_key": "Enter new OpenRouter API key:",
        "enter_system_prompt": "Enter new system prompt:",
        "enter_holding_msg": "Enter new holding message (sent to buyer when forwarding):",
        "enter_max_history": "Enter max number of history messages (integer):",
        "enter_timeout": "Enter API timeout in seconds (integer):",
        "enter_spam_limit": "Enter max messages per minute (integer):",
        "enter_spam_reply": "Enter the message to send when buyer is spamming:",
        "enter_wh_start": "Enter start hour (0-23):",
        "enter_wh_end": "Enter end hour (0-23):",
        "enter_offline_msg": "Enter the offline message (sent outside working hours):",
        "enter_blacklist_user": "Enter the username to add to the blacklist:",
        "enter_template_name": "Enter the template name (short identifier, e.g. 'warranty', 'refund'):",
        "enter_template_text": "Template name: <b>{name}</b>\n\nNow enter the template text:",
        "enter_stopword": "Enter the stop-word to add:",
        "enter_promo_code": "Enter the promo code (e.g. SAVE10):",
        "enter_promo_desc": "Promo code: <b>{code}</b>\n\nNow enter a description for this promo:",
        "enter_upsell_prompt": "Current upsell prompt:\n<i>{current}</i>\n\nEnter the new upsell prompt addon text:",
        # Product catalog
        "product_catalog": "📦 Product Catalog",
        "product_catalog_mgmt": "Product Catalog Management\n\nAdd products that the AI will offer to buyers.",
        "product_added": "Product <b>{name}</b> added!",
        "product_removed": "Product <b>{name}</b> removed.",
        "enter_product_name": "Enter the product name:",
        "enter_product_price": "Product: <b>{name}</b>\n\nEnter the price (number or text, e.g. '100 rub.' or 'from 500'): ",
        "enter_product_desc": "Product: <b>{name}</b>, Price: <b>{price}</b>\n\nEnter a brief product description: ",
        "product_empty": "Catalog is empty. Add products so the AI can answer buyers.",
        "add_product": "➕ Add product",
        "sync_products": "🔄 Sync from FunPay",
        "syncing_products": "Loading products from FunPay...",
        "sync_products_done": "Loaded {n} lots from FunPay",
        "sync_products_empty": "Cardinal returned no lots. Check connection.",
        "value_empty": "Value cannot be empty.",
        "invalid_number": "Please enter a valid number.",
        "unknown_field": "Unknown field.",
        "setting_updated": "Setting <b>{field}</b> updated!",
        "stats_reset": "Statistics have been reset.",
        "reset_stats": "Reset stats",
        "removed": "Removed: {item}",
        "added_to_blacklist": "User <b>{user}</b> added to blacklist!",
        "template_added": "Template <b>{name}</b> added!",
        "stopword_added": "Stop-word <b>{word}</b> added!",
        "promo_added": "Promo <b>{code}</b> added!",
        "upsell_updated": "Upsell prompt updated!",
        "back_to_settings": "Back to settings",
        "back_to_blacklist": "Back to blacklist",
        "back_to_templates": "Back to templates",
        "back_to_stopwords": "Back to stop-words",
        "back_to_promos": "Back to promos",
        "blacklist_mgmt": "Blacklist Management\n\nClick a user to remove, or add a new one.",
        "templates_mgmt": "Templates Management\n\nClick a template to remove, or add a new one.",
        "stopwords_mgmt": "Stop-words Management\n\nClick a word to remove, or add a new one.",
        "promos_mgmt": "Promo Codes Management\n\nClick a code to remove, or add a new one.",
        "stats_title": "AI Chat Plugin - Statistics",
        "stats_today": "Today",
        "stats_week": "This week",
        "messages_processed": "Messages processed",
        "forwarded_to_seller": "Forwarded to seller",
        "tokens_used": "Tokens used",
        "guide_title": "AI Chat Plugin - Guide",
        "guide_body": (
            "<b>What it does:</b>\n"
            "Automatically replies to buyers in FunPay chats using AI. "
            "Supports multiple providers: OpenRouter, OpenAI, Gemini, DeepSeek, Anthropic. "
            "Can classify questions: standard ones are answered by AI, non-standard ones are "
            "forwarded to the seller in Telegram.\n\n"
            "<b>Quick start:</b>\n"
            "1. Pick a provider: /aichat → ⚙️ Core → Provider\n"
            "2. Get an API key from your chosen provider\n"
            "3. /aichat → \U0001f511 API key → paste the key\n"
            "4. /aichat → ⚙️ Core → \u2705 Plugin enabled\n"
            "5. Choose a prompt preset or write a custom System prompt\n\n"
            "<b>Built-in prompt presets:</b>\n"
            "• FunPay Seller — generic store\n"
            "• Steam Rental — tuned for account rental\n"
            "• Game Items — donate, keys, skins\n"
            "• Custom prompt — fully your own\n\n"
            "<b>How it works:</b>\n"
            "• Buyer writes → AI gets context (history, products, info)\n"
            "• Standard question → AI replies on its own\n"
            "• Stop-word / non-standard → forward to seller in TG, "
            "buyer gets a holding message\n"
            "• Anti-spam → if buyer spams, AI stops replying\n"
            "• Working hours → outside hours, offline message is sent\n\n"
            "<b>Features:</b>\n"
            "\u2022 Multi-provider — OpenRouter / OpenAI / Gemini / DeepSeek / Anthropic\n"
            "\u2022 Auto-learning — AI remembers good answers from your templates "
            "and reuses them on similar questions\n"
            "\u2022 Anti-spam — message rate limit per buyer per minute\n"
            "\u2022 Working hours — AI replies only during configured hours\n"
            "\u2022 Blacklist — ignore specific buyers\n"
            "\u2022 Stop-words — forward to seller on keyword detection\n"
            "\u2022 Templates — quick-reply templates\n"
            "\u2022 Upsell — cross-sell related products\n"
            "\u2022 Promos — promo codes in AI context\n"
            "\u2022 Language detect — reply in buyer's language (RU/EN/UA/...)\n"
            "\u2022 Statistics — messages processed, tokens, forwards\n"
            "\u2022 Multilang UI — plugin interface in RU/EN\n\n"
            "<b>v2.1.0:</b>\n"
            "\u2022 Memory TTL — conversation context expires on inactivity\n"
            "\u2022 Topic filter — denied topics get a canned reply\n"
            "\u2022 Fallback models — auto-switch when the primary fails\n"
            "\u2022 Escalation — operator notified on trigger keywords\n"
            "\u2022 Q&A log — record exchanges for review\n"
            "\u2022 FAQ cache — common questions without a model call\n"
            "\u2022 Operator pause — AI stays silent while operator replies\n"
            "\u2022 Master toggle and daily token budget\n\n"
            "<b>Commands:</b>\n"
            "/aichat — open settings menu\n"
            "/aichat_guide — this guide\n"
            "/aichat_test — test connection to the AI provider"
        ),
        "test_testing": "Testing connection to OpenRouter API...",
        "test_passed": "Test passed!",
        "test_failed": "Test failed!",
        "test_no_key": "Test failed: API key not configured.\nOpen /aichat - API key and enter your OpenRouter key.",
        "language_label": "Language: {lang}",
        "language_ru": "Russian",
        "language_en": "English",
        # Settings display
        "core_section": "Core",
        "status_label": "Status",
        "enabled_text": "Enabled",
        "disabled_text": "Disabled",
        "prompt_section": "Prompt",
        "auto_delivery_section": "Auto-delivery",
        "info_in_context": "Info in context",
        "forwarding_section": "Forwarding",
        "forward_label": "Forward",
        "message_label": "Message",
        "features_section": "Features",
        "not_set": "not set",
        # Provider & preset
        "provider_label": "Provider",
        "prompt_preset_label": "Prompt preset",
        "provider_openrouter": "OpenRouter",
        "provider_openai": "OpenAI",
        "provider_gemini": "Gemini",
        "provider_deepseek": "DeepSeek",
        "provider_anthropic": "Anthropic",
        "preset_funpay_seller": "FunPay Seller",
        "preset_steam_rental": "Steam Rental",
        "preset_game_items": "Game Items",
        "preset_custom": "Custom prompt",
        "select_provider": "Select AI provider:",
        "select_preset": "Select system prompt preset:",
        "provider_changed_to": "Provider changed to",
        "provider_set_model_hint": "Don't forget to pick a model for this provider (🤖 Model button has presets).",
        "provider_examples": "Examples",
        "unknown_provider": "Unknown provider",
        # Model presets
        "select_model": "Select a model:",
        "model_manual": "Enter manually",
        "model_testing": "Model {model} saved. Testing connection...",
        # Custom providers
        "cprovider_add": "Add provider",
        "cprovider_manage": "Manage providers",
        "cprovider_delete_hint": "Tap a provider to delete it. ⭐ = active.",
        "cprovider_note": "Custom providers: {count}",
        "no_custom_providers": "No custom providers yet. Tap ➕ to add your own (any OpenAI-compatible API).",
        "cprovider_removed": "Provider {name} removed",
        "enter_cprovider_name": "New provider — step 1/4.\n\nName (latin/digits/_, 2-20 chars):",
        "invalid_cprovider_name": "Name must contain only latin letters, digits and _ (2-20 chars).",
        "cprovider_reserved": "This name is reserved by a built-in provider.",
        "cprovider_exists": "A provider with this name already exists.",
        "enter_cprovider_url": "Step 2/4.\n\nProvider base URL (e.g. https://api.together.xyz/v1):",
        "invalid_cprovider_url": "URL must start with http:// or https://",
        "enter_cprovider_models": "Step 3/4.\n\nModels, comma-separated (saved as presets in the model menu). Send '-' to skip:",
        "enter_cprovider_key": "Step 4/4.\n\nAPI key for this provider (or '-' to use the general key):",
        "cprovider_added": "Provider {name} added!",
        "cprovider_models_count": "Models saved: {count}",
        "cprovider_make_active": "Make active provider",
        # Setting descriptions
        "desc_plugin_enabled": "Enables/disables AI responses to buyer messages",
        "desc_auto_delivery_info": "AI informs buyer about auto-delivery after payment",
        "desc_forward_questions": "Forward non-standard questions to seller in Telegram",
        "desc_notify_standard": "Notify seller when AI answers directly",
        "notify_standard_label": "Notify on AI reply",
        "desc_list_products": "Show product list when buyer asks for clarification",
        "desc_anti_spam": "Limit number of messages per buyer per minute",
        "desc_working_hours": "AI responds only during set hours, sends offline message otherwise",
        "desc_blacklist": "Ignore messages from specified buyers",
        "desc_stop_words": "When keywords detected - forward to seller instead of AI response",
        "desc_upsell": "AI suggests related products in responses",
        "desc_promos": "AI mentions promo codes in response context",
        "desc_language_detect": "Detects buyer language and responds in it",
        "desc_templates": "Quick reply templates for common questions",
        "desc_statistics": "Collect stats: processed messages, tokens, forwards",
        "desc_model": "AI model name (depends on selected provider)",
        "desc_api_key": "API key for accessing the selected AI provider",
        "desc_system_prompt": "System instruction - defines AI behavior and style",
        "desc_holding_msg": "Message to buyer while question is forwarded to seller",
        "desc_max_history": "How many messages to keep for conversation context",
        "desc_timeout": "Timeout waiting for AI API response (seconds)",
        # Notification texts
        "ai_manual_attention": "AI Chat Plugin - Manual attention needed",
        "ai_stopword_detected": "AI Chat Plugin - Stop-word detected",
        "buyer_context": "Buyer context",
        "auto_learning": "Auto-learning",
        "manage_learned": "Learned responses",
        "learned_empty": "No learned responses",
        "learned_deleted": "Response deleted",
        # v2.1.0 enhancement labels
        "memory_ttl_label": "Memory TTL (s)",
        "fallback_models_label": "Fallback models",
        "budget": "Token budget",
        "budget_limit_label": "Daily limit",
        "budget_unit_label": "Budget unit",
        "budget_alert_label": "Alert threshold",
        "test_model": "🧪 Test model",
        "topic_filter": "Topic filter",
        "manage_topic_filter": "Denied topics",
        "topic_canned_label": "Canned reply",
        "add_topic": "Add topic",
        "escalation": "Escalation",
        "manage_escalation": "Escalation keywords",
        "add_escalation": "Add keyword",
        "escalation_pause": "Pause on escalation",
        "operator_pause": "Operator pause",
        "pause_timeout_label": "Pause timeout (s)",
        "faq": "FAQ cache",
        "manage_faq": "Manage FAQ",
        "add_faq": "Add FAQ",
        "qa_log": "Q&A log",
        "view_logs": "📋 Logs",
        "logs_title": "Recent plugin logs:",
        "logs_empty": "No logs yet.",
        "enter_memory_ttl": "Enter memory TTL in seconds (integer > 0):",
        "enter_fallback_models": "Enter fallback models, comma-separated (empty to clear):",
        "enter_budget_limit": "Enter daily limit (integer > 0):",
        "enter_budget_unit": "Enter budget unit: tokens or requests:",
        "enter_budget_alert": "Enter alert threshold (integer > 0):",
        "enter_pause_timeout": "Enter operator-pause timeout in seconds (integer > 0):",
        "enter_topic_canned": "Enter the canned reply for denied topics:",
        "enter_topic": "Enter the denied topic / keyword:",
        "enter_escalation": "Enter the escalation keyword:",
        "enter_faq_patterns": "Enter FAQ patterns, comma-separated (keywords):",
        "enter_faq_answer": "Patterns: <b>{patterns}</b>\n\nNow enter the FAQ answer:",
        "topic_added": "Topic <b>{item}</b> added!",
        "escalation_added": "Keyword <b>{item}</b> added!",
        "faq_added": "FAQ entry added!",
        "plugin_on": "Plugin enabled",
        "plugin_off": "Plugin disabled",
        "topic_mgmt": "Denied Topics\n\nClick an entry to remove, or add a new one.",
        "escalation_mgmt": "Escalation Keywords\n\nClick an entry to remove, or add a new one.",
        "faq_mgmt": "FAQ Entries\n\nClick an entry to remove, or add a new one.",
        "back_to_topic": "Back to topic filter",
        "back_to_escalation": "Back to escalation",
        "back_to_faq": "Back to FAQ",
        "page_label": "Page {page}/{total}",
    },
}


def _t(key: str, **kwargs) -> str:
    """Get translated string for current language."""
    lang = _current_lang
    if lang not in LANG:
        lang = "ru"
    text = LANG[lang].get(key) or LANG["ru"].get(key) or key
    if kwargs:
        text = text.format(**kwargs)
    return text


def _load_language_from_config() -> None:
    """Load language setting from config into _current_lang cache. Called once at init."""
    global _current_lang
    config = load_config()
    _current_lang = config.get("General", "language", fallback="ru")
    if _current_lang not in LANG:
        _current_lang = "ru"


# --- Recommended complaint/refund stop-words (one-tap add) ---

# Слова-маркеры жалоб/возвратов/претензий. При срабатывании диалог
# пересылается продавцу, а не обрабатывается авто-ответом ИИ.
RECOMMENDED_COMPLAINT_STOPWORDS: list[str] = [
    "возврат", "верните деньги", "верни деньги", "верните", "верни",
    "обман", "обманул", "развод", "разводил", "кидала", "кидалово",
    "скам", "scam", "мошенник", "мошенничество", "афера",
    "не работает", "не пришло", "не пришёл", "не получил", "не выдал",
    "не выдали", "не активируется", "не подходит", "брак", "бракованный",
    "жалоба", "жалоб", "претензи", "арбитраж", "спор", "диспут",
    "чарджбэк", "chargeback", "refund", "плохой товар",
    "обманули", "ворует", "украли", "верните средства",
]


# --- Telegram Settings UI callback constants ---

class AIChatCBT:
    """Callback type constants for AI Chat Plugin Telegram UI."""
    SETTINGS_MENU = "aichat:settings"
    # Category navigation
    CATEGORY_CORE = "aichat:cat:core"
    CATEGORY_RESPONDER = "aichat:cat:responder"
    CATEGORY_MODERATION = "aichat:cat:moderation"
    CATEGORY_SALES = "aichat:cat:sales"
    CATEGORY_STATS = "aichat:cat:stats"
    BACK_TO_MAIN = "aichat:back:main"
    TOGGLE_ENABLED = "aichat:toggle:enabled"
    TOGGLE_NOTIFY_AD = "aichat:toggle:notify_ad"
    TOGGLE_FORWARD = "aichat:toggle:forward"
    TOGGLE_NOTIFY_STANDARD = "aichat:toggle:notify_standard"
    TOGGLE_LIST_PRODUCTS = "aichat:toggle:list_products"
    EDIT_MODEL = "aichat:edit:model"
    EDIT_SYSTEM_PROMPT = "aichat:edit:system_prompt"
    EDIT_HOLDING_MSG = "aichat:edit:holding_msg"
    EDIT_API_KEY = "aichat:edit:api_key"
    EDIT_MAX_HISTORY = "aichat:edit:max_history"
    EDIT_TIMEOUT = "aichat:edit:timeout"
    VIEW_SETTINGS = "aichat:view"
    # Anti-spam
    TOGGLE_ANTISPAM = "aichat:toggle:antispam"
    EDIT_SPAM_LIMIT = "aichat:edit:spam_limit"
    EDIT_SPAM_REPLY = "aichat:edit:spam_reply"
    # Working hours
    TOGGLE_WORKING_HOURS = "aichat:toggle:working_hours"
    EDIT_WORKING_HOURS_START = "aichat:edit:wh_start"
    EDIT_WORKING_HOURS_END = "aichat:edit:wh_end"
    EDIT_OFFLINE_MSG = "aichat:edit:offline_msg"
    # Blacklist
    TOGGLE_BLACKLIST = "aichat:toggle:blacklist"
    MANAGE_BLACKLIST = "aichat:manage:blacklist"
    ADD_BLACKLIST = "aichat:add:blacklist"
    REMOVE_BLACKLIST_PREFIX = "aichat:blacklist:rm:"
    # Templates
    TOGGLE_TEMPLATES = "aichat:toggle:templates"
    MANAGE_TEMPLATES = "aichat:manage:templates"
    ADD_TEMPLATE = "aichat:add:template"
    REMOVE_TEMPLATE_PREFIX = "aichat:tpl:rm:"
    # Product Catalog
    MANAGE_PRODUCTS = "aichat:manage:products"
    ADD_PRODUCT = "aichat:add:product"
    REMOVE_PRODUCT_PREFIX = "aichat:product:rm:"
    SYNC_PRODUCTS = "aichat:sync:products"
    # Stop-words
    TOGGLE_STOPWORDS = "aichat:toggle:stopwords"
    MANAGE_STOPWORDS = "aichat:manage:stopwords"
    ADD_STOPWORD = "aichat:add:stopword"
    ADD_COMPLAINT_STOPWORDS = "aichat:add:complaint_sw"
    REMOVE_STOPWORD_PREFIX = "aichat:sw:rm:"
    # Upsell
    TOGGLE_UPSELL = "aichat:toggle:upsell"
    MANAGE_UPSELL = "aichat:manage:upsell"
    EDIT_UPSELL_PROMPT = "aichat:edit:upsell_prompt"
    # Promos
    TOGGLE_PROMOS = "aichat:toggle:promos"
    MANAGE_PROMOS = "aichat:manage:promos"
    ADD_PROMO = "aichat:add:promo"
    REMOVE_PROMO_PREFIX = "aichat:promo:rm:"
    # Language detection
    TOGGLE_LANGUAGE_DETECT = "aichat:toggle:lang_detect"
    # Language (i18n)
    TOGGLE_LANGUAGE = "aichat:toggle:language"
    # Provider & preset
    EDIT_PROVIDER = "aichat:edit:provider"
    EDIT_PROMPT_PRESET = "aichat:edit:prompt_preset"
    SELECT_PROVIDER = "aichat:sel:provider"
    SELECT_PRESET = "aichat:sel:preset"
    DOWNLOAD_PROMPT = "aichat:download:prompt"
    # Model presets & custom providers
    SELECT_MODEL_PRESET = "aichat:sel:model"
    MODEL_MANUAL = "aichat:model:manual"
    MANAGE_CPROVIDERS = "aichat:cprov:manage"
    ADD_CPROVIDER = "aichat:cprov:add"
    REMOVE_CPROVIDER_PREFIX = "aichat:cprov:rm:"
    # Preset management
    MANAGE_PRESETS = "aichat:presets:manage"
    CREATE_PRESET = "aichat:presets:create"
    EDIT_PRESET_PREFIX = "aichat:presets:edit:"
    DELETE_PRESET_PREFIX = "aichat:presets:del:"
    CONFIRM_DELETE_PREFIX = "aichat:presets:cdel:"
    # Statistics
    TOGGLE_STATISTICS = "aichat:toggle:statistics"
    VIEW_STATS = "aichat:view:stats"
    RESET_STATS = "aichat:reset:stats"
    # Buyer context & Auto-learning
    TOGGLE_BUYER_CONTEXT = "aichat:toggle:buyer_context"
    TOGGLE_AUTO_LEARNING = "aichat:toggle:auto_learning"
    MANAGE_LEARNED = "aichat:manage:learned"
    DELETE_LEARNED_PREFIX = "aichat:learned:del:"
    # --- v2.1.0 enhancements ---
    # Core
    EDIT_MEMORY_TTL = "aichat:edit:memory_ttl"
    EDIT_FALLBACK_MODELS = "aichat:edit:fallback_models"
    # Budget (Core)
    TOGGLE_BUDGET = "aichat:toggle:budget"
    EDIT_BUDGET_LIMIT = "aichat:edit:budget_limit"
    EDIT_BUDGET_UNIT = "aichat:edit:budget_unit"
    EDIT_BUDGET_ALERT = "aichat:edit:budget_alert"
    TEST_MODEL = "aichat:test_model"
    # Topic filter (Moderation)
    TOGGLE_TOPIC_FILTER = "aichat:toggle:topic_filter"
    MANAGE_TOPIC_FILTER = "aichat:manage:topic_filter"
    ADD_TOPIC = "aichat:add:topic"
    REMOVE_TOPIC_PREFIX = "aichat:topic:rm:"
    EDIT_TOPIC_CANNED_REPLY = "aichat:edit:topic_canned"
    # Escalation (Moderation)
    TOGGLE_ESCALATION = "aichat:toggle:escalation"
    MANAGE_ESCALATION = "aichat:manage:escalation"
    ADD_ESCALATION = "aichat:add:escalation"
    REMOVE_ESCALATION_PREFIX = "aichat:esc:rm:"
    TOGGLE_ESCALATION_PAUSE = "aichat:toggle:escalation_pause"
    # Operator pause (Moderation)
    TOGGLE_OPERATOR_PAUSE = "aichat:toggle:operator_pause"
    EDIT_PAUSE_TIMEOUT = "aichat:edit:pause_timeout"
    # FAQ (Sales)
    TOGGLE_FAQ = "aichat:toggle:faq"
    MANAGE_FAQ = "aichat:manage:faq"
    ADD_FAQ = "aichat:add:faq"
    REMOVE_FAQ_PREFIX = "aichat:faq:rm:"
    # Q&A logging (Stats)
    TOGGLE_QA_LOG = "aichat:toggle:qa_log"
    VIEW_LOGS = "aichat:view:logs"


# State tracking for text input from users
# {telegram_user_id: (field_name_or_tuple, timestamp)}
_pending_input: dict[int, tuple[str | tuple, float]] = {}

# TTL for pending input entries (5 minutes)
_PENDING_INPUT_TTL = 300.0


def _set_pending_input(user_id: int, field: str | tuple) -> None:
    """Set a pending input entry with timestamp. Also cleans up expired entries."""
    now = time.time()
    # Clean up expired entries
    expired = [uid for uid, (_, ts) in _pending_input.items() if now - ts > _PENDING_INPUT_TTL]
    for uid in expired:
        del _pending_input[uid]
    _pending_input[user_id] = (field, now)


def _get_pending_input(user_id: int) -> str | tuple | None:
    """Get and remove a pending input entry, returning None if expired or missing."""
    entry = _pending_input.pop(user_id, None)
    if entry is None:
        return None
    field, ts = entry
    if time.time() - ts > _PENDING_INPUT_TTL:
        return None
    return field


def _has_pending_input(user_id: int) -> bool:
    """Check if user has a non-expired pending input entry."""
    entry = _pending_input.get(user_id)
    if entry is None:
        return False
    _, ts = entry
    if time.time() - ts > _PENDING_INPUT_TTL:
        del _pending_input[user_id]
        return False
    return True


logger = logging.getLogger("FPC.ai_chat_plugin")
LOGGER_PREFIX = "[AICHAT]"

# --- In-memory log buffer for Telegram log viewer ---
_log_buffer: list[str] = []
_log_buffer_lock = threading.Lock()
_LOG_BUFFER_MAX = 100


class _TelegramLogHandler(logging.Handler):
    """Captures [AICHAT] log lines into an in-memory ring buffer."""
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            with _log_buffer_lock:
                _log_buffer.append(msg)
                if len(_log_buffer) > _LOG_BUFFER_MAX:
                    del _log_buffer[: len(_log_buffer) - _LOG_BUFFER_MAX]
        except Exception:
            pass


try:
    _tg_handler = _TelegramLogHandler()
    _tg_handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(_tg_handler)
except Exception:
    pass


def _get_log_entries(n: int = 30) -> list[str]:
    """Return the last *n* log lines from the buffer."""
    with _log_buffer_lock:
        return list(_log_buffer[-n:])


CONFIG_PATH = "configs/ai_chat_plugin.cfg"
STATS_PATH = "configs/ai_chat_plugin_stats.json"
PRESETS_PATH = "configs/ai_chat_plugin_presets.json"
BUYER_CONTEXT_PATH = "configs/ai_chat_plugin_buyer_context.json"
LEARNED_RESPONSES_PATH = "configs/ai_chat_plugin_learned.json"
# v2.1.0 enhancement sidecars (see ai-chat-plugin-enhancements design)
FAQ_PATH = "configs/ai_chat_plugin_faq.json"
QA_LOG_PATH = "configs/ai_chat_plugin_qa_log.json"
BUDGET_PATH = "configs/ai_chat_plugin_budget.json"
PAUSE_PATH = "configs/ai_chat_plugin_pause.json"
PROVIDERS_PATH = "configs/ai_chat_plugin_providers.json"
QA_LOG_MAX_RECORDS = 500
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"

# --- Multi-provider support ---

PROVIDERS: dict[str, str] = {
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
    "openai": "https://api.openai.com/v1/chat/completions",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
    "deepseek": "https://api.deepseek.com/v1/chat/completions",
    "anthropic": "https://api.anthropic.com/v1/messages",
}

# --- Пресеты моделей OpenRouter (бесплатные, проверенные) ---
# Список собран по живому /api/v1/models: id -> подпись в меню.

OPENROUTER_MODEL_PRESETS: list[tuple[str, str]] = [
    ("openrouter/free", "🆓 Free Router (авто-выбор free)"),
    ("thinkingmachines/inkling:free", "Inkling 975B — лучший для чата"),
    ("minimax/minimax-m3:free", "MiniMax M3 — быстрый и точный"),
    ("nvidia/nemotron-3-ultra-550b-a55b:free", "Nemotron 3 Ultra (NVIDIA)"),
    ("google/gemma-4-31b-it:free", "Gemma 4 31B (Google)"),
    ("nvidia/nemotron-3-super-120b-a12b:free", "Nemotron 3 Super (NVIDIA)"),
    ("minimax/minimax-m2.7:free", "MiniMax M2.7 — компактный"),
]

# Пресеты моделей для встроенных провайдеров (кроме OpenRouter)
BUILTIN_MODEL_PRESETS: dict[str, list[tuple[str, str]]] = {
    "openai": [
        ("gpt-4o-mini", "GPT-4o mini"),
        ("gpt-4o", "GPT-4o"),
        ("gpt-3.5-turbo", "GPT-3.5 Turbo"),
    ],
    "gemini": [
        ("gemini-1.5-flash", "Gemini 1.5 Flash"),
        ("gemini-1.5-pro", "Gemini 1.5 Pro"),
    ],
    "deepseek": [
        ("deepseek-chat", "DeepSeek Chat"),
        ("deepseek-coder", "DeepSeek Coder"),
    ],
    "anthropic": [
        ("claude-3-haiku-20240307", "Claude 3 Haiku"),
        ("claude-3-sonnet-20240229", "Claude 3 Sonnet"),
    ],
}

# --- Кастомные провайдеры (несколько, хранятся в JSON вместе с моделями) ---

_custom_providers_lock = threading.Lock()


def _load_custom_providers() -> dict[str, dict]:
    """Load user-defined providers: {name: {base_url, models: [..], api_key}}."""
    if not os.path.exists(PROVIDERS_PATH):
        return {}
    try:
        with open(PROVIDERS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        logger.warning(f"{LOGGER_PREFIX} не удалось прочитать {PROVIDERS_PATH}", exc_info=True)
        return {}


def _save_custom_providers(providers: dict[str, dict]) -> None:
    """Save user-defined providers to JSON file."""
    with _custom_providers_lock:
        try:
            with open(PROVIDERS_PATH, "w", encoding="utf-8") as f:
                json.dump(providers, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.error(f"{LOGGER_PREFIX} не удалось сохранить {PROVIDERS_PATH}", exc_info=True)


def _normalize_custom_url(base_url: str) -> str:
    """Turn a user-entered base_url into a chat/completions endpoint."""
    url = (base_url or "").strip().rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    return url + "/chat/completions"


def get_custom_provider_url(name: str) -> str | None:
    """Return normalized chat/completions URL for a custom provider, else None."""
    entry = _load_custom_providers().get(name)
    if not entry or not entry.get("base_url"):
        return None
    return _normalize_custom_url(entry["base_url"])


def get_custom_provider_models(name: str) -> list[str]:
    """Return saved model list for a custom provider."""
    entry = _load_custom_providers().get(name)
    if not entry:
        return []
    return [m for m in entry.get("models", []) if m]


def get_custom_provider_key(name: str) -> str:
    """Return the per-provider API key (may be empty → use the general key)."""
    entry = _load_custom_providers().get(name)
    return (entry or {}).get("api_key", "") or ""


def get_api_key_for_provider(config: configparser.ConfigParser, provider: str) -> str:
    """API key for a provider: custom provider key if set, otherwise the general key."""
    if provider not in PROVIDERS:
        key = get_custom_provider_key(provider)
        if key:
            return key
    return get_api_key(config)


def _model_menu_entries(provider: str) -> list[tuple[str, str]]:
    """Deterministic (model, label) list shown in the model preset menu."""
    if provider == "openrouter":
        return list(OPENROUTER_MODEL_PRESETS)
    if provider in PROVIDERS:
        return list(BUILTIN_MODEL_PRESETS.get(provider, []))
    return [(m, m) for m in get_custom_provider_models(provider)]

# --- Preset system prompts (Russian) ---

PRESET_PROMPTS: dict[str, str | None] = {
    "funpay_seller": (
        "Ты — бот-консультант ЭТОГО МАГАЗИНА на FunPay. Ты продаёшь товары этого продавца. "
        "Твоя главная задача — продавать именно ЭТИ товары, а не давать общую информацию про FunPay. "
        "ВАЖНО: НИКОГДА не отвечай общими фразами про FunPay (типа 'На FunPay представлены товары...' или 'Вы можете найти здесь...'). "
        "Всегда отвечай ТОЛЬКО про товары этого магазина из каталога ниже. "
        "Если покупатель спрашивает о цене КОНКРЕТНОГО товара — покажи цену ТОЛЬКО ЭТОГО товара. "
        "НЕ СКЛАДЫВАЙ цены разных товаров. Один вопрос = один товар. "
        "Найди в каталоге товар, который соответствует вопросу покупателя, и покажи ЕГО цену. "
        "Если подходящих товаров несколько — перечисли каждый ОТДЕЛЬНО с ценой. "
        "Если покупатель спрашивает о наличии — проверь каталог и скажи есть ли товар. "
        "Если товара нет в каталоге — скажи что данного товара сейчас нет и предложи написать продавцу. "
        "Отвечай кратко, по делу, на языке покупателя. "
        "Если покупатель недоволен или хочет возврат — вежливо попроси описать проблему и передай вопрос продавцу. "
        "При вопросах о гарантии — объясни что все товары проверены перед продажей. "
        "Не обсуждай конкурентов и не давай ссылки на сторонние ресурсы."
    ),
    "steam_rental": (
        "Ты - ассистент сервиса аренды Steam-аккаунтов. Ты помогаешь покупателям с вопросами "
        "об аренде игр, продлении подписки и решении технических проблем. "
        "Основные команды которые должен знать покупатель: "
        "!rck - или похожие дает код от Social Club, "
        "!код - получить код для входа в аккаунт (Steam Guard код), "
        "!продлить - продлить срок аренды аккаунта, "
        "!статус - проверить оставшееся время аренды. "
        "При первом обращении объясни покупателю как пользоваться арендой: "
        "1) После оплаты придут данные для входа в Steam аккаунт, "
        "2) Для входа нужно ввести логин и пароль, затем запросить код через !код, "
        "3) Играть можно в течение оплаченного периода, "
        "4) Нельзя менять пароль, email или данные аккаунта, "
        "5) Нельзя покупать игры или совершать транзакции на аккаунте. "
        "Если покупатель сообщает о проблемах со входом, попробуй помочь сам в течении "
        "5 минут - логин и пароль которые даны пользователю всегда верные, он их не верно "
        "ввел, но не груби. Если проблема не решается, передай вопрос продавцу. "
        "Всегда напоминай что изменение данных аккаунта запрещено и приведет к блокировке доступа без возврата."
    ),
    "game_items": (
        "Ты - консультант магазина игровых товаров на FunPay. Ты продаешь игровые ключи, "
        "аккаунты, внутриигровые предметы и валюту. Отвечай покупателям быстро и информативно. "
        "При вопросах о ключах: объясни что ключ приходит мгновенно после оплаты через авто-выдачу, "
        "ключ активируется в соответствующем сервисе (Steam, Epic, GOG и т.д.), "
        "ключ одноразовый и после активации привязывается к аккаунту покупателя навсегда. "
        "При вопросах об аккаунтах: уточни какой именно аккаунт интересует, "
        "объясни что аккаунт передается с полным доступом и покупатель сможет сменить данные. "
        "При вопросах о внутриигровых предметах: уточни сервер и никнейм персонажа, "
        "объясни способ и сроки передачи предмета. "
        "Гарантия: все ключи рабочие и проверены перед продажей, если ключ не работает - "
        "предоставляем замену. Аккаунты передаются с гарантией на первые 24 часа. "
        "Если покупатель просит скидку, предложи посмотреть текущие промо-акции. "
        "Если вопрос сложный или нестандартный, передай продавцу."
    ),
    "custom": None,
}

# --- English versions of built-in preset prompts ---

PRESET_PROMPTS_EN: dict[str, str | None] = {
    "funpay_seller": (
        "You are a sales bot for THIS SPECIFIC STORE on FunPay. You sell products from this seller. "
        "Your main job is to sell THESE products, not to give generic information about FunPay. "
        "IMPORTANT: NEVER reply with generic FunPay info (like 'FunPay has various products...' or 'You can find...'). "
        "ALWAYS reply ONLY about this store's products from the catalog below. "
        "If the buyer asks about prices — show PRICES FROM THE CATALOG, not general FunPay ranges. "
        "If the buyer asks about availability — check the catalog and say if the product exists. "
        "If the product is not in the catalog — say it's currently unavailable and offer to contact the seller. "
        "Reply briefly, in the buyer's language. "
        "If the buyer is unhappy or wants a refund — politely ask them to describe the problem and forward to the seller. "
        "For warranty questions — explain all products are verified before sale. "
        "Do not discuss competitors or provide links to external resources."
    ),
    "steam_rental": (
        "You are an assistant for a Steam account rental service. You help buyers with questions "
        "about game rentals, subscription extensions, and technical issues. "
        "Key commands the buyer should know: "
        "!rck - or similar gives a Social Club code, "
        "!code - get the login code (Steam Guard code), "
        "!extend - extend the rental period, "
        "!status - check remaining rental time. "
        "On first contact, explain how to use the rental: "
        "1) After payment, login credentials for the Steam account will be provided, "
        "2) To log in, enter the username and password, then request a code via !code, "
        "3) You can play during the paid period, "
        "4) You cannot change the password, email, or account data, "
        "5) You cannot buy games or make transactions on the account. "
        "If the buyer reports login issues, try to help within 5 minutes - the login and password "
        "provided are always correct, they just entered them incorrectly, but do not be rude. "
        "If the problem persists, forward the question to the seller. "
        "Always remind that changing account data is prohibited and will result in access being blocked without a refund."
    ),
    "game_items": (
        "You are a consultant for a game items store on FunPay. You sell game keys, "
        "accounts, in-game items, and currency. Reply to buyers quickly and informatively. "
        "For key questions: explain that the key arrives instantly after payment via auto-delivery, "
        "the key is activated in the corresponding service (Steam, Epic, GOG, etc.), "
        "the key is single-use and once activated is permanently linked to the buyer's account. "
        "For account questions: clarify which account they are interested in, "
        "explain that the account is transferred with full access and the buyer can change the data. "
        "For in-game item questions: clarify the server and character nickname, "
        "explain the method and timeframe for item delivery. "
        "Warranty: all keys are working and verified before sale, if a key doesn't work - "
        "we provide a replacement. Accounts are transferred with a 24-hour warranty. "
        "If the buyer asks for a discount, suggest checking current promotions. "
        "If the question is complex or non-standard, forward it to the seller."
    ),
    "custom": None,
}

# --- Custom presets storage ---

_custom_presets_lock = threading.Lock()


def _load_custom_presets() -> dict[str, str]:
    """Load user-defined presets from JSON file."""
    if not os.path.exists(PRESETS_PATH):
        return {}
    try:
        with open(PRESETS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_custom_presets(presets: dict[str, str]) -> None:
    """Save user-defined presets to JSON file."""
    with _custom_presets_lock:
        try:
            with open(PRESETS_PATH, "w", encoding="utf-8") as f:
                json.dump(presets, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.error("Failed to save custom presets", exc_info=True)


# --- Buyer context storage ---

_buyer_context_lock = threading.Lock()


def _load_buyer_context() -> dict:
    """Load buyer context data from JSON file."""
    if not os.path.exists(BUYER_CONTEXT_PATH):
        return {}
    try:
        with open(BUYER_CONTEXT_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_buyer_context(data: dict) -> None:
    """Save buyer context data to JSON file."""
    with _buyer_context_lock:
        try:
            with open(BUYER_CONTEXT_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.error("Failed to save buyer context", exc_info=True)


def _update_buyer_context(buyer_username: str) -> None:
    """Increment contact count and update last_contact timestamp for a buyer."""
    with _buyer_context_lock:
        data = _load_buyer_context()
        if buyer_username not in data:
            data[buyer_username] = {"contact_count": 0, "last_contact": ""}
        data[buyer_username]["contact_count"] += 1
        data[buyer_username]["last_contact"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        try:
            with open(BUYER_CONTEXT_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.error("Failed to save buyer context", exc_info=True)


def _get_buyer_context_text(buyer_username: str) -> str:
    """Return formatted buyer context text for inclusion in system prompt."""
    data = _load_buyer_context()
    entry = data.get(buyer_username)
    if not entry or entry.get("contact_count", 0) <= 1:
        return ""
    count = entry["contact_count"]
    last = entry.get("last_contact", "unknown")
    return (
        f"\n\nBuyer context: This buyer has contacted {count} times. "
        f"Last contact: {last}. They are a returning customer."
    )


# --- Auto-learning storage ---

_learned_lock = threading.Lock()


def _load_learned_responses() -> list:
    """Load learned Q&A responses from JSON file."""
    if not os.path.exists(LEARNED_RESPONSES_PATH):
        return []
    try:
        with open(LEARNED_RESPONSES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_learned_responses(data: list) -> None:
    """Save learned Q&A responses to JSON file."""
    with _learned_lock:
        try:
            with open(LEARNED_RESPONSES_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.error("Failed to save learned responses", exc_info=True)


def _add_learned_response(question: str, answer: str) -> None:
    """Add a learned Q&A pair, trimming to max 50 entries.
    Skips entries where the seller answer is shorter than 10 characters."""
    if len(answer) < 10:
        return
    with _learned_lock:
        data = _load_learned_responses()
        data.append({
            "buyer_question": question,
            "seller_answer": answer,
            "timestamp": time.time(),
        })
        # Keep only the last 50
        if len(data) > 50:
            data = data[-50:]
        try:
            with open(LEARNED_RESPONSES_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.error("Failed to save learned responses", exc_info=True)


def _get_learned_examples_text(max_items: int = 5) -> str:
    """Return formatted text with recent learned Q&A pairs for inclusion in prompt."""
    data = _load_learned_responses()
    if not data:
        return ""
    recent = data[-max_items:]
    lines = ["\n\nExample Q&A from previous interactions (use as reference for similar questions):"]
    for item in recent:
        q = item.get("buyer_question", "")[:200]
        a = item.get("seller_answer", "")[:200]
        lines.append(f"  Q: {q}")
        lines.append(f"  A: {a}")
        lines.append("")
    return "\n".join(lines)


# --- Pending forwards for auto-learning ---

_pending_forwards: dict[str, dict] = {}
_pending_forwards_lock = threading.Lock()

# --- Duplicate-event gate: one buyer message can arrive via BOTH Cardinal
# events (msg_new_msg + last_chat_message_changed); claim (chat_id, text)
# for a short TTL so handle_message runs only once per exact-duplicate message.
# NOTE: Tier 2 soft dedup was removed — both events now get processed.
# If Cardinal sends two events for the same real message, only the
# text-exact duplicate is suppressed.

_recently_claimed: dict[str, float] = {}
_recently_claimed_lock = threading.Lock()
_CLAIM_TTL_SECONDS = 10.0


def _claim_message_for_processing(chat_id: str, message_text: str) -> bool:
    """Atomically claim (chat_id, normalized text) for the TTL window.

    Returns True for the first caller, False only if the exact same text
    arrives again within the TTL (true duplicate from Cardinal).
    """
    now = time.time()
    text_key = f"{chat_id}|{(message_text or '').strip().lower()}"
    with _recently_claimed_lock:
        # Evict expired entries
        for k in [k for k, ts in _recently_claimed.items() if now - ts > _CLAIM_TTL_SECONDS]:
            del _recently_claimed[k]

        # Exact text match only — both Cardinal events now pass through
        if text_key in _recently_claimed:
            return False

        _recently_claimed[text_key] = now
        return True


# --- Language detection ---

def _detect_language(text: str) -> str:
    """Simple heuristic language detection. Returns 'en' if >80% ASCII latin, else 'ru'.
    Requires at least 10 alphabetic characters for reliable detection."""
    alpha_chars = [ch for ch in text if ch.isalpha()]
    if len(alpha_chars) < 10:
        return "ru"
    ascii_latin = sum(1 for ch in alpha_chars if ord(ch) < 128)
    ratio = ascii_latin / len(alpha_chars)
    return "en" if ratio > 0.8 else "ru"


# --- Multi-language preset helper ---

def _get_preset_text(preset_data, lang: str = "ru") -> str:
    """Get preset text respecting language. Handles both str and dict formats."""
    if isinstance(preset_data, dict):
        if lang == "en" and "text_en" in preset_data:
            return preset_data["text_en"]
        return preset_data.get("text", "")
    return preset_data if isinstance(preset_data, str) else ""


# --- Thread-safe conversation history ---

# Lock protecting all mutations to conversation_history
_history_lock = threading.Lock()

# Conversation history: {chat_id: [{"role": "...", "content": "..."}, ...]}
conversation_history: dict[str, list[dict[str, str]]] = {}

# Timestamps of last activity per chat for TTL eviction: {chat_id: float}
_history_last_access: dict[str, float] = {}

# Default TTL for inactive chats in seconds (30 minutes)
_HISTORY_TTL_SECONDS = 1800

# --- v2.1.0 enhancement shared state and locks ---
# Operator-pause: {chat_id: expiry_epoch}; budget counter dict; file-write locks.
_operator_pause: dict[str, float] = {}
_pause_lock = threading.Lock()
_budget_state: dict = {}
_budget_lock = threading.Lock()
_faq_lock = threading.Lock()
_qa_log_lock = threading.Lock()

# --- Cached config and stock data ---

_config_cache: configparser.ConfigParser | None = None
_config_cache_time: float = 0.0
_CONFIG_CACHE_TTL = 5.0  # seconds

# Cached language value (updated on language toggle, initialized during _pre_init)
_current_lang: str = "ru"

_stock_cache: dict[str, int] = {}
_stock_cache_time: float = 0.0
_STOCK_CACHE_TTL = 30.0  # seconds

# --- Cached lot details data ---

_lot_details_cache: list[dict[str, str]] = []
_lot_details_cache_time: float = 0.0
_LOT_DETAILS_CACHE_TTL = 60.0  # seconds

# Cache for full lot descriptions (from get_lot_page)
_lot_pages_cache: dict[int, dict[str, str]] = {}  # {lot_id: {description, price, ...}}
_lot_pages_cache_time: float = 0.0
_LOT_PAGES_CACHE_TTL = 300.0  # 5 minutes — full descriptions change rarely

# --- Anti-spam tracking ---

_spam_tracker: dict[str, list[float]] = {}  # {buyer_username: [timestamps]}
_spam_lock = threading.Lock()

# --- Statistics ---

_stats: dict[str, int | str] = {
    "messages_today": 0,
    "messages_week": 0,
    "forwarded_today": 0,
    "forwarded_week": 0,
    "tokens_today": 0,
    "tokens_week": 0,
    "last_reset_day": "",
    "last_reset_week": "",
}
_stats_lock = threading.Lock()


def _load_stats_from_file() -> None:
    """Load statistics from JSON file on disk."""
    global _stats
    try:
        with open(STATS_PATH, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        # Only update keys that exist in our schema
        for key in _stats:
            if key in loaded:
                _stats[key] = loaded[key]
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass


def _save_stats_to_file() -> None:
    """Save current statistics to JSON file on disk."""
    try:
        with open(STATS_PATH, "w", encoding="utf-8") as f:
            json.dump(_stats, f, ensure_ascii=False)
    except OSError as e:
        logger.debug(f"Could not save stats to file: {e}")


# Load stats from file on module init
_load_stats_from_file()


# --- Helper functions for new features ---

def _check_spam(author: str, config: configparser.ConfigParser) -> bool:
    """Check if a buyer is sending messages too fast. Returns True if spamming."""
    if not config.getboolean("AntiSpam", "enabled", fallback=False):
        return False
    max_per_minute = config.getint("AntiSpam", "max_messages_per_minute", fallback=5)
    now = time.time()
    with _spam_lock:
        timestamps = _spam_tracker.get(author, [])
        # Remove timestamps older than 60 seconds
        timestamps = [t for t in timestamps if now - t < 60.0]
        timestamps.append(now)
        _spam_tracker[author] = timestamps

        # Periodic sweep: remove keys with empty timestamp lists to prevent memory leak
        stale_keys = [k for k, v in _spam_tracker.items() if not v]
        for k in stale_keys:
            del _spam_tracker[k]

        return len(timestamps) > max_per_minute


def _is_working_hours(config: configparser.ConfigParser) -> bool:
    """Check if current time is within working hours. Returns True if within hours."""
    if not config.getboolean("WorkingHours", "enabled", fallback=False):
        return True
    start_hour = config.getint("WorkingHours", "start_hour", fallback=10)
    end_hour = config.getint("WorkingHours", "end_hour", fallback=22)
    timezone_offset = config.getint("WorkingHours", "timezone_offset", fallback=0)
    current_hour = (datetime.utcnow().hour + timezone_offset) % 24
    if start_hour <= end_hour:
        return start_hour <= current_hour < end_hour
    else:
        # Overnight range (e.g., 22 to 6)
        return current_hour >= start_hour or current_hour < end_hour


def _is_blacklisted(author: str, config: configparser.ConfigParser) -> bool:
    """Check if a buyer is blacklisted."""
    if not config.getboolean("Blacklist", "enabled", fallback=False):
        return False
    users_str = config.get("Blacklist", "users", fallback="")
    if not users_str.strip():
        return False
    users = [u.strip().lower() for u in users_str.split(",") if u.strip()]
    return author.lower() in users


def _parse_csv_list(raw: str) -> list[str]:
    """Parse a comma-separated config value into a clean list (strip, drop empties)."""
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _match_keyword(message: str, words) -> str | None:
    """Return the first keyword matching ``message`` using word-boundary,
    case-insensitive comparison (the same rule as the stop-word matcher), else None.

    Shared by the Topic_Filter, Escalation_Manager and FAQ_Cache so all three use
    identical matching semantics.
    """
    if not message or not words:
        return None
    message_lower = message.lower()
    for word in words:
        word = (word or "").strip()
        if not word:
            continue
        pattern = r'\b' + re.escape(word.lower()) + r'\b'
        if re.search(pattern, message_lower):
            return word
    return None


def _check_stop_words(message: str, config: configparser.ConfigParser) -> bool:
    """Check if message contains any stop-words. Returns True if found."""
    if not config.getboolean("StopWords", "enabled", fallback=False):
        return False
    words = _parse_csv_list(config.get("StopWords", "words", fallback=""))
    if not words:
        return False
    return _match_keyword(message, words) is not None


def _get_templates(config: configparser.ConfigParser) -> dict[str, str]:
    """Get quick reply templates from config."""
    if not config.getboolean("Templates", "enabled", fallback=False):
        return {}
    templates = {}
    if config.has_section("Templates"):
        for key, value in config.items("Templates"):
            if key.startswith("template_"):
                name = key[len("template_"):]
                templates[name] = value
    return templates


# --- Product Catalog (manual product entries for AI context) ---

def _get_product_catalog(config: configparser.ConfigParser) -> list[dict[str, str]]:
    """Get manually added products from config. Format:
    [ProductCatalog]
    product_0_name = FC Mobile Буст
    product_0_price = от 500 руб.
    product_0_desc = Профессиональный буст в FC Mobile
    product_1_name = ...
    """
    products = []
    if not config.has_section("ProductCatalog"):
        return products
    # Find all product indices
    indices = set()
    for key, _ in config.items("ProductCatalog"):
        if key.startswith("product_") and "_" in key[8:]:
            parts = key[8:].split("_", 1)
            try:
                indices.add(int(parts[0]))
            except ValueError:
                pass
    for idx in sorted(indices):
        name = config.get("ProductCatalog", f"product_{idx}_name", fallback="")
        price = config.get("ProductCatalog", f"product_{idx}_price", fallback="")
        desc = config.get("ProductCatalog", f"product_{idx}_desc", fallback="")
        if name:
            products.append({"title": name, "price": price, "description": desc})
    return products


def _add_product_to_catalog(name: str, price: str, desc: str) -> None:
    """Add a product to the ProductCatalog config section."""
    config = load_config()
    if not config.has_section("ProductCatalog"):
        config.add_section("ProductCatalog")
    # Find next index
    max_idx = -1
    for key, _ in config.items("ProductCatalog"):
        if key.startswith("product_") and "_name" in key:
            try:
                idx = int(key[8:].split("_")[0])
                max_idx = max(max_idx, idx)
            except ValueError:
                pass
    new_idx = max_idx + 1
    config.set("ProductCatalog", f"product_{new_idx}_name", name)
    config.set("ProductCatalog", f"product_{new_idx}_price", price)
    config.set("ProductCatalog", f"product_{new_idx}_desc", desc)
    save_config(config)


def _remove_product_from_catalog(name: str) -> bool:
    """Remove a product by name from the catalog. Returns True if found."""
    config = load_config()
    if not config.has_section("ProductCatalog"):
        return False
    found = False
    to_remove = []
    for key, value in config.items("ProductCatalog"):
        if key.endswith("_name") and value == name:
            prefix = key[:-5]  # e.g. product_0
            to_remove.extend([f"{prefix}_name", f"{prefix}_price", f"{prefix}_desc"])
            found = True
    for key in to_remove:
        config.remove_option("ProductCatalog", key)
    if found:
        save_config(config)
    return found


def _get_promo_codes(config: configparser.ConfigParser) -> list[dict[str, str]]:
    """Get promo codes from config. Format: CODE:description, one per line or pipe-separated."""
    if not config.getboolean("Promos", "enabled", fallback=False):
        return []
    codes_str = config.get("Promos", "codes", fallback="")
    if not codes_str.strip():
        return []
    promos = []
    # Primary delimiter: newline. Fallback: pipe '|' if single line with no newlines.
    lines = codes_str.strip().split("\n")
    if len(lines) == 1 and "|" in lines[0]:
        entries = lines[0].split("|")
    else:
        entries = lines
    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue
        if ":" in entry:
            code, desc = entry.split(":", 1)
            promos.append({"code": code.strip(), "description": desc.strip()})
        elif entry:
            promos.append({"code": entry, "description": ""})
    return promos


def _update_stats(forwarded: bool, tokens: int) -> None:
    """Update statistics counters. Thread-safe. Persists to file."""
    with _stats_lock:
        today = datetime.now().strftime("%Y-%m-%d")
        week_num = datetime.now().strftime("%Y-W%W")

        # Reset daily if day changed
        if _stats["last_reset_day"] != today:
            _stats["messages_today"] = 0
            _stats["forwarded_today"] = 0
            _stats["tokens_today"] = 0
            _stats["last_reset_day"] = today

        # Reset weekly if week changed
        if _stats["last_reset_week"] != week_num:
            _stats["messages_week"] = 0
            _stats["forwarded_week"] = 0
            _stats["tokens_week"] = 0
            _stats["last_reset_week"] = week_num

        _stats["messages_today"] += 1
        _stats["messages_week"] += 1
        _stats["tokens_today"] += tokens
        _stats["tokens_week"] += tokens

        if forwarded:
            _stats["forwarded_today"] += 1
            _stats["forwarded_week"] += 1

        _save_stats_to_file()


def _get_stats_text() -> str:
    """Get formatted statistics text for display."""
    with _stats_lock:
        return (
            f"<b>\U0001f4c8 {_t('stats_title')}</b>\n\n"
            f"<b>{_t('stats_today')}:</b>\n"
            f"  {_t('messages_processed')}: {_stats['messages_today']}\n"
            f"  {_t('forwarded_to_seller')}: {_stats['forwarded_today']}\n"
            f"  {_t('tokens_used')}: {_stats['tokens_today']}\n\n"
            f"<b>{_t('stats_week')}:</b>\n"
            f"  {_t('messages_processed')}: {_stats['messages_week']}\n"
            f"  {_t('forwarded_to_seller')}: {_stats['forwarded_week']}\n"
            f"  {_t('tokens_used')}: {_stats['tokens_week']}"
        )


def _reset_stats() -> None:
    """Reset all statistics counters and persist to file."""
    with _stats_lock:
        _stats["messages_today"] = 0
        _stats["messages_week"] = 0
        _stats["forwarded_today"] = 0
        _stats["forwarded_week"] = 0
        _stats["tokens_today"] = 0
        _stats["tokens_week"] = 0
        _stats["last_reset_day"] = ""
        _stats["last_reset_week"] = ""
        _save_stats_to_file()


def _estimate_tokens(text: str) -> int:
    """Estimate token count for a text string (approximate: 1 token per 4 chars)."""
    return len(text) // 4


def load_config() -> configparser.ConfigParser:
    """Load plugin configuration from INI file, with short-lived cache."""
    global _config_cache, _config_cache_time

    now = time.time()
    if _config_cache is not None and (now - _config_cache_time) < _CONFIG_CACHE_TTL:
        return _config_cache

    config = configparser.ConfigParser()
    config.read(CONFIG_PATH, encoding="utf-8")
    _config_cache = config
    _config_cache_time = now
    return config


def is_enabled(config: configparser.ConfigParser) -> bool:
    """Check if the plugin is enabled in config."""
    return config.getboolean("General", "enabled", fallback=False)


def get_api_key(config: configparser.ConfigParser) -> str:
    """Get OpenRouter API key from config or environment variable."""
    env_key = os.environ.get("OPENROUTER_API_KEY")
    if env_key:
        return env_key
    return config.get("General", "openrouter_api_key", fallback="")


def get_model(config: configparser.ConfigParser) -> str:
    """Get the AI model name from config."""
    return config.get("General", "model", fallback="openai/gpt-3.5-turbo")


def get_max_history(config: configparser.ConfigParser) -> int:
    """Get max number of history messages to keep per chat."""
    return config.getint("General", "max_history_messages", fallback=10)


def get_response_timeout(config: configparser.ConfigParser) -> int:
    """Get API response timeout in seconds."""
    return config.getint("General", "response_timeout", fallback=30)


def get_system_prompt(config: configparser.ConfigParser) -> str:
    """Get custom system prompt from config."""
    return config.get(
        "General",
        "system_prompt",
        fallback=(
            "Ты - вежливый и компетентный продавец-консультант на FunPay. "
            "Помогай покупателям с вопросами о товарах, ценах, наличии и доставке. "
            "Отвечай кратко, по-русски и только на основе данных каталога."
        ),
    )


def get_provider(config: configparser.ConfigParser) -> str:
    """Get the configured AI provider name."""
    return config.get("General", "provider", fallback="openrouter")


def get_prompt_presets(config: configparser.ConfigParser) -> list[str]:
    """Get list of active prompt preset names."""
    raw = config.get("General", "prompt_preset", fallback="custom")
    presets = [p.strip() for p in raw.split(",") if p.strip()]
    return presets if presets else ["custom"]


def get_prompt_preset(config: configparser.ConfigParser) -> str:
    """Get the configured prompt preset name (first one for display, backward compat)."""
    presets = get_prompt_presets(config)
    return presets[0] if presets else "custom"


PRESET_SEPARATOR = "\n\n---\n\n"


def _get_effective_prompt(config: configparser.ConfigParser, lang: str = "ru") -> str:
    """Get the combined system prompt from all active presets."""
    presets = get_prompt_presets(config)
    custom_presets = _load_custom_presets()
    parts = []
    for preset in presets:
        if preset in custom_presets:
            preset_data = custom_presets[preset]
            parts.append(_get_preset_text(preset_data, lang))
        elif preset != "custom" and preset in PRESET_PROMPTS and PRESET_PROMPTS[preset] is not None:
            if lang == "en" and preset in PRESET_PROMPTS_EN and PRESET_PROMPTS_EN[preset] is not None:
                parts.append(PRESET_PROMPTS_EN[preset])
            else:
                parts.append(PRESET_PROMPTS[preset])
        elif preset == "custom":
            custom_text = get_system_prompt(config)
            if custom_text:
                parts.append(custom_text)
    if not parts:
        return get_system_prompt(config)
    return PRESET_SEPARATOR.join(parts)


def get_holding_message(config: configparser.ConfigParser) -> str:
    """Get configurable holding message for FORWARD cases."""
    return config.get(
        "Forwarding",
        "holding_message",
        fallback="Ваш вопрос передан продавцу. Пожалуйста, ожидайте ответа.",
    )


def reset_holding_message_to_default(config_path) -> None:
    """One-shot helper: rewrite the user's holding_message to the RU default.

    The previous English default was stored in users' ai_chat_plugin.cfg on
    first save. Call this from a maintenance entry-point (e.g. plugin
    upgrade hook) to normalize it without forcing the user to re-edit the
    config manually.
    """
    import configparser as _cp

    if not config_path or not Path(config_path).exists():
        return
    cfg = _cp.ConfigParser()
    cfg.read(config_path, encoding="utf-8")
    if not cfg.has_section("Forwarding"):
        return
    current = cfg.get("Forwarding", "holding_message", fallback="").strip()
    english_defaults = {
        "Your question has been forwarded to the seller. Please wait for a response.",
        "Forwarding",
    }
    if current in english_defaults:
        cfg.set(
            "Forwarding",
            "holding_message",
            "Ваш вопрос передан продавцу. Пожалуйста, ожидайте ответа.",
        )
        with open(config_path, "w", encoding="utf-8") as f:
            cfg.write(f)


def should_notify_auto_delivery(config: configparser.ConfigParser) -> bool:
    """Check if auto-delivery info should be included in AI context."""
    return config.getboolean("AutoDelivery", "notify_auto_delivery", fallback=True)


def should_forward_non_standard(config: configparser.ConfigParser) -> bool:
    """Check if non-standard questions should be forwarded to seller."""
    return config.getboolean("Forwarding", "forward_non_standard", fallback=True)


def should_list_products_on_clarify(config: configparser.ConfigParser) -> bool:
    """Check if the AI should list product names when asking buyer to clarify."""
    return config.getboolean("General", "list_products_on_clarify", fallback=False)


# === v2.1.0 enhancement config accessors (all new flags default to disabled) ===

def get_memory_ttl(config: configparser.ConfigParser) -> int:
    """Memory TTL in seconds for per-buyer history eviction (default 1800)."""
    try:
        ttl = config.getint("General", "memory_ttl_seconds", fallback=1800)
    except ValueError:
        return 1800
    return ttl if ttl > 0 else 1800


def get_fallback_models(config: configparser.ConfigParser) -> list[str]:
    """Ordered list of backup models tried after the primary model fails."""
    return _parse_csv_list(config.get("General", "fallback_models", fallback=""))


def topic_filter_enabled(config: configparser.ConfigParser) -> bool:
    return config.getboolean("TopicFilter", "enabled", fallback=False)


def get_deny_list(config: configparser.ConfigParser) -> list[str]:
    return _parse_csv_list(config.get("TopicFilter", "deny_list", fallback=""))


def escalation_enabled(config: configparser.ConfigParser) -> bool:
    return config.getboolean("Escalation", "enabled", fallback=False)


def get_escalation_keywords(config: configparser.ConfigParser) -> list[str]:
    return _parse_csv_list(config.get("Escalation", "keywords", fallback=""))


def escalation_pause_enabled(config: configparser.ConfigParser) -> bool:
    return config.getboolean("Escalation", "pause_on_escalation", fallback=False)


def qa_log_enabled(config: configparser.ConfigParser) -> bool:
    return config.getboolean("QALog", "enabled", fallback=False)


def faq_enabled(config: configparser.ConfigParser) -> bool:
    return config.getboolean("FAQ", "enabled", fallback=False)


def operator_pause_enabled(config: configparser.ConfigParser) -> bool:
    return config.getboolean("OperatorPause", "enabled", fallback=False)


def get_operator_pause_timeout(config: configparser.ConfigParser) -> int:
    """Operator-pause timeout in seconds (default 300)."""
    try:
        timeout = config.getint("OperatorPause", "timeout_seconds", fallback=300)
    except ValueError:
        return 300
    return timeout if timeout > 0 else 300


def budget_enabled(config: configparser.ConfigParser) -> bool:
    return config.getboolean("Budget", "enabled", fallback=False)


def get_budget_limit(config: configparser.ConfigParser) -> int:
    try:
        return max(0, config.getint("Budget", "daily_limit", fallback=0))
    except ValueError:
        return 0


def get_budget_unit(config: configparser.ConfigParser) -> str:
    unit = config.get("Budget", "unit", fallback="tokens").strip().lower()
    return unit if unit in ("tokens", "requests") else "tokens"


def get_budget_alert_threshold(config: configparser.ConfigParser) -> int:
    try:
        return max(0, config.getint("Budget", "alert_threshold", fallback=0))
    except ValueError:
        return 0


# Default canned replies per buyer language (used when none configured).
_DEFAULT_CANNED_REPLY = {
    "ru": "По этому вопросу вам ответит продавец, пожалуйста, подождите.",
    "en": "The seller will get back to you on this question, please wait.",
}


def get_topic_canned_reply(config: configparser.ConfigParser, buyer_lang: str = "ru") -> str:
    """Canned reply for the Topic_Filter; falls back to a default per buyer language."""
    text = config.get("TopicFilter", "canned_reply", fallback="").strip()
    if text:
        return text
    return _DEFAULT_CANNED_REPLY.get(buyer_lang, _DEFAULT_CANNED_REPLY["ru"])


def get_budget_canned_reply(config: configparser.ConfigParser, buyer_lang: str = "ru") -> str:
    """Canned reply sent when the daily budget is exhausted."""
    text = config.get("Budget", "canned_reply", fallback="").strip()
    if text:
        return text
    return _DEFAULT_CANNED_REPLY.get(buyer_lang, _DEFAULT_CANNED_REPLY["ru"])


def _parse_positive_int(raw: str) -> int | None:
    """Parse free-text into a positive integer, or None if invalid.

    Used by the Settings_UI numeric fields (memory TTL, pause timeout, budget
    limit/alert): accept iff the value is a strictly positive integer.
    """
    if raw is None:
        return None
    raw = raw.strip()
    if not raw or not raw.lstrip("+").isdigit():
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _sanitize_lot_name(lot_name: str) -> str:
    """Sanitize a lot name to prevent prompt injection.

    Removes control-like patterns and limits length.
    """
    # Strip common instruction-injection patterns
    sanitized = re.sub(r"[\[\]\{\}<>]", "", lot_name)
    # Collapse whitespace
    sanitized = re.sub(r"\s+", " ", sanitized).strip()
    # Limit length to prevent overly long lot names from dominating the prompt
    return sanitized[:100]


def _get_stock_counts(c: Cardinal) -> dict[str, int]:
    """Get stock counts for all lots, with short-lived cache."""
    global _stock_cache, _stock_cache_time

    now = time.time()
    if _stock_cache and (now - _stock_cache_time) < _STOCK_CACHE_TTL:
        return _stock_cache

    counts: dict[str, int] = {}
    if hasattr(c, "AD_CFG") and c.AD_CFG:
        for lot_name in c.AD_CFG.sections():
            products_file = c.AD_CFG.get(lot_name, "productsFileName", fallback=None)
            if products_file:
                file_path = f"storage/products/{products_file}"
                try:
                    counts[lot_name] = cardinal_tools.count_products(file_path)
                except Exception:
                    counts[lot_name] = -1  # unknown
            else:
                counts[lot_name] = 0

    _stock_cache = counts
    _stock_cache_time = now
    return counts


def _get_lot_details(c: Cardinal) -> list[dict[str, str]]:
    """Gather comprehensive lot details from Cardinal, with cache."""
    global _lot_details_cache, _lot_details_cache_time

    now = time.time()
    if _lot_details_cache and (now - _lot_details_cache_time) < _LOT_DETAILS_CACHE_TTL:
        return _lot_details_cache

    details: list[dict[str, str]] = []

    try:
        lots = None
        # --- Primary: c.profile (UserProfile with get_lots()) ---
        # Cardinal stores lots in c.profile, NOT in c.account!
        profile = getattr(c, "profile", None)
        if profile is not None and hasattr(profile, "get_lots"):
            try:
                lots = profile.get_lots()
                if lots:
                    logger.info(
                        f"{LOGGER_PREFIX} лоты загружены из c.profile.get_lots(): {len(lots)} шт.")
            except Exception as e:
                logger.debug(f"c.profile.get_lots() failed: {e}")
        # --- Fallback: c.account.get_lots() ---
        if not lots:
            acc = getattr(c, "account", None)
            if acc is not None and hasattr(acc, "get_lots"):
                try:
                    lots = acc.get_lots()
                    if lots:
                        logger.info(
                            f"{LOGGER_PREFIX} лоты загружены из c.account.get_lots(): {len(lots)} шт.")
                except Exception as e:
                    logger.debug(f"c.account.get_lots() failed: {e}")
        # --- Fallback: c.account.lots attribute ---
        if not lots:
            acc = getattr(c, "account", None)
            if acc is not None and hasattr(acc, "lots"):
                try:
                    raw = acc.lots
                    if raw:
                        lots = raw
                except Exception:
                    pass

        if lots:
            if isinstance(lots, dict):
                lot_items = list(lots.values())
            elif isinstance(lots, (list, tuple)):
                lot_items = list(lots)
            else:
                lot_items = []
            # Debug: dump first lot's attributes to understand the data structure
            if lot_items and not _lot_details_cache:
                first = lot_items[0]
                attrs = {k: type(getattr(first, k, None)).__name__ for k in dir(first) if not k.startswith('_')}
                logger.info(
                    f"{LOGGER_PREFIX} account.lots: {len(lot_items)} лотов, "
                    f"тип: {type(lots).__name__}, "
                    f"атрибуты 1-го лота: {attrs}")
            for lot in lot_items:
                lot_info: dict[str, str] = {}

                title = None
                if hasattr(lot, "title"):
                    title = lot.title
                elif hasattr(lot, "description"):
                    title = lot.description
                elif hasattr(lot, "name"):
                    title = lot.name
                if title:
                    lot_info["title"] = str(title)

                if hasattr(lot, "description") and hasattr(lot, "title"):
                    lot_info["description"] = str(lot.description)
                elif hasattr(lot, "short_description"):
                    lot_info["description"] = str(lot.short_description)

                if hasattr(lot, "price"):
                    price = lot.price
                    if price is not None:
                        lot_info["price"] = str(price)

                if hasattr(lot, "category_name"):
                    lot_info["category"] = str(lot.category_name)
                elif hasattr(lot, "category"):
                    cat = lot.category
                    if hasattr(cat, "name"):
                        lot_info["category"] = str(cat.name)
                    elif isinstance(cat, str):
                        lot_info["category"] = cat

                if hasattr(lot, "subcategory_name"):
                    lot_info["subcategory"] = str(lot.subcategory_name)
                elif hasattr(lot, "subcategory"):
                    subcat = lot.subcategory
                    if hasattr(subcat, "name"):
                        lot_info["subcategory"] = str(subcat.name)
                    elif isinstance(subcat, str):
                        lot_info["subcategory"] = subcat

                if hasattr(lot, "active"):
                    lot_info["active"] = str(lot.active)

                if hasattr(lot, "id"):
                    lot_info["id"] = str(lot.id)

                if lot_info:
                    details.append(lot_info)
    except Exception as e:
        logger.debug(f"Could not gather lot details from account.lots: {e}")

    try:
        if hasattr(c, "MAIN_CFG") and c.MAIN_CFG:
            for section in c.MAIN_CFG.sections():
                if c.MAIN_CFG.has_option(section, "lot_description"):
                    desc = c.MAIN_CFG.get(section, "lot_description", fallback="")
                    already_exists = any(
                        d.get("title") == section or d.get("description") == desc
                        for d in details
                    )
                    if not already_exists and desc:
                        lot_info = {"title": section, "description": desc}
                        if c.MAIN_CFG.has_option(section, "price"):
                            lot_info["price"] = c.MAIN_CFG.get(section, "price")
                        details.append(lot_info)
    except Exception as e:
        logger.debug(f"Could not gather lot details from MAIN_CFG: {e}")

    # --- Enrich with full descriptions from LotPage (get_lot_page API) ---
    global _lot_pages_cache, _lot_pages_cache_time
    try:
        now_pages = time.time()
        pages_cache_fresh = (
            _lot_pages_cache and (now_pages - _lot_pages_cache_time) < _LOT_PAGES_CACHE_TTL
        )
        # Only fetch if we have lots with IDs and cache is stale
        lots_with_ids = [d for d in details if d.get("id")]
        if lots_with_ids and not pages_cache_fresh and hasattr(c, "account") and c.account:
            fetched = 0
            for d in lots_with_ids:
                lot_id = int(d["id"])
                if lot_id in _lot_pages_cache:
                    # Use cached page
                    page_data = _lot_pages_cache[lot_id]
                    if page_data.get("full_description"):
                        d["full_description"] = page_data["full_description"]
                    if page_data.get("price") and not d.get("price"):
                        d["price"] = page_data["price"]
                    continue
                try:
                    if hasattr(c.account, "get_lot_page"):
                        page = c.account.get_lot_page(lot_id)
                        if page and hasattr(page, "full_description") and page.full_description:
                            d["full_description"] = str(page.full_description)
                            _lot_pages_cache[lot_id] = {
                                "full_description": str(page.full_description),
                                "price": str(page.price) if hasattr(page, "price") else "",
                            }
                            fetched += 1
                            time.sleep(0.3)  # Rate limit: 1 request per 300ms
                except Exception as e:
                    logger.debug(f"{LOGGER_PREFIX} get_lot_page({lot_id}) failed: {e}")
            if fetched > 0:
                _lot_pages_cache_time = now_pages
                logger.info(
                    f"{LOGGER_PREFIX} загружено {fetched} полных описаний лотов (LotPage)")
    except Exception as e:
        logger.debug(f"Could not enrich with LotPage data: {e}")

    # --- Manual product catalog (added by user in /aichat → Продажи → Каталог) ---
    try:
        config = load_config()
        manual_products = _get_product_catalog(config)
        for mp in manual_products:
            # Don't duplicate if already from account.lots or MAIN_CFG
            already = any(
                d.get("title", "").lower() == mp["title"].lower()
                for d in details
            )
            if not already:
                details.append(mp)
    except Exception as e:
        logger.debug(f"Could not load manual product catalog: {e}")

    _lot_details_cache = details
    _lot_details_cache_time = now
    if details:
        logger.info(
            f"{LOGGER_PREFIX} каталог товаров загружен: {len(details)} лотов "
            f"({', '.join(d.get('title', '?')[:30] for d in details[:5])})")
    else:
        logger.warning(
            f"{LOGGER_PREFIX} каталог товаров ПУСТ — AI не знает о ваших товарах! "
            f"Проверьте что Cardinal загрузил лоты (c.account.lots).")
    return details


def _find_relevant_lot(
    chat_name: str,
    lot_details: list[dict[str, str]],
    stock_counts: dict[str, int],
) -> dict[str, str] | None:
    """Try to find the lot most relevant to the current chat."""
    if not chat_name:
        return None

    chat_name_lower = chat_name.lower().strip()

    for lot in lot_details:
        title = lot.get("title", "")
        if title and title.lower() in chat_name_lower:
            return lot
        if title and chat_name_lower in title.lower():
            return lot

    for lot_name in stock_counts:
        if lot_name.lower() in chat_name_lower:
            return {"title": lot_name, "_from_stock": "true"}
        if chat_name_lower in lot_name.lower():
            return {"title": lot_name, "_from_stock": "true"}

    return None


def build_system_prompt(c: Cardinal, config: configparser.ConfigParser, chat_name: str = "", buyer_lang: str = "ru") -> str:
    """Build the full system prompt including product details and auto-delivery context."""
    # Build base prompt from all active presets
    base_prompt = _get_effective_prompt(config, lang=buyer_lang)
    parts = [base_prompt]

    # Gather comprehensive lot details
    lot_details = _get_lot_details(c)
    stock_counts = _get_stock_counts(c) if should_notify_auto_delivery(config) else {}

    # Add full product catalog information
    if lot_details:
        catalog_lines = []
        for lot in lot_details:
            title = lot.get("title", "Unknown")
            safe_title = _sanitize_lot_name(title)
            line_parts = [f"  Товар: {safe_title}"]

            # Prefer full_description (from LotPage) over short description
            desc_text = lot.get("full_description") or lot.get("description")
            if desc_text:
                # Use more of the full description (500 chars for detailed info)
                max_len = 500 if lot.get("full_description") else 200
                line_parts.append(f"  Описание: {desc_text[:max_len]}")
            if "price" in lot:
                line_parts.append(f"  Цена: {lot['price']} руб.")
            if "category" in lot:
                line_parts.append(f"  Категория: {lot['category']}")
            if "subcategory" in lot:
                line_parts.append(f"  Подкатегория: {lot['subcategory']}")
            if "active" in lot:
                status = "активен" if lot["active"].lower() in ("true", "1") else "неактивен"
                line_parts.append(f"  Статус: {status}")

            stock = stock_counts.get(title)
            if stock is not None:
                if stock > 0:
                    line_parts.append(f"  Автовыдача: есть ({stock} в наличии)")
                elif stock == 0:
                    line_parts.append("  Автовыдача: нет в наличии")
                elif stock == -1:
                    line_parts.append("  Автовыдача: настроена, остаток неизвестен")

            catalog_lines.append("\n".join(line_parts))

        parts.append(
            "\n\nКАТАЛОГ ТОВАРОВ ПРОДАВЦА (ОБЯЗАТЕЛЬНО используй эту информацию для ответов):\n"
            + "\n---\n".join(catalog_lines)
        )

    # Add auto-delivery lots that are not already in the catalog
    if should_notify_auto_delivery(config) and stock_counts:
        catalog_titles = {lot.get("title", "").lower() for lot in lot_details}
        extra_ad_lines = []
        for lot_name, stock_count in stock_counts.items():
            if lot_name.lower() not in catalog_titles:
                safe_name = _sanitize_lot_name(lot_name)
                if stock_count > 0:
                    extra_ad_lines.append(
                        f"- Lot '{safe_name}': auto-delivery available, {stock_count} in stock"
                    )
                elif stock_count == 0:
                    extra_ad_lines.append(
                        f"- Lot '{safe_name}': no auto-delivery"
                    )
                elif stock_count == -1:
                    extra_ad_lines.append(
                        f"- Lot '{safe_name}': auto-delivery configured, stock unknown"
                    )

        if extra_ad_lines:
            parts.append(
                "\n\nAdditional lots with delivery status:\n"
                + "\n".join(extra_ad_lines)
            )

    # Add delivery instructions
    if stock_counts:
        parts.append(
            "\nЕсли товар есть в наличии с автовыдачей, скажи покупателю: "
            "\"Вы можете купить сейчас и получить товар мгновенно через автовыдачу.\""
        )
        parts.append(
            "Если товара нет в наличии или нет автовыдачи, скажи покупателю: "
            "\"Пожалуйста, подождите — продавец обработает ваш заказ вручную.\""
        )

    # Try to detect which specific product the buyer is asking about
    relevant_lot = None
    if chat_name:
        relevant_lot = _find_relevant_lot(chat_name, lot_details, stock_counts)

    # If no match found but only one lot exists, auto-select it
    if relevant_lot is None and len(lot_details) == 1:
        relevant_lot = lot_details[0]

    if relevant_lot:
        context_lines = [
            "\n\nПокупатель спрашивает об этом конкретном товаре:"
        ]
        title = relevant_lot.get("title", "")
        if title:
            context_lines.append(f"  Товар: {_sanitize_lot_name(title)}")
        if "description" in relevant_lot:
            context_lines.append(f"  Описание: {relevant_lot['description'][:200]}")
        if "price" in relevant_lot:
            context_lines.append(f"  Цена: {relevant_lot['price']} руб.")
        if "category" in relevant_lot:
            context_lines.append(f"  Категория: {relevant_lot['category']}")
        if "subcategory" in relevant_lot:
            context_lines.append(f"  Подкатегория: {relevant_lot['subcategory']}")

        stock = stock_counts.get(title)
        if stock is not None:
            if stock > 0:
                context_lines.append(f"  Автовыдача: есть ({stock} в наличии)")
            else:
                context_lines.append("  Автовыдача: нет в наличии")

        context_lines.append(
            "Отвечай на основе информации о товаре выше. "
            "Если покупатель спрашивает что получит — опиши этот товар."
        )
        parts.append("\n".join(context_lines))
    elif relevant_lot is None and len(lot_details) > 1:
        list_products = should_list_products_on_clarify(config)
        if list_products:
            product_names = [
                _sanitize_lot_name(lot.get("title", "Unknown"))
                for lot in lot_details
                if lot.get("title")
            ]
            disambiguation_lines = [
                "\n\nYou could not determine which product the buyer is asking about. "
                "If their question is about a specific product (price, availability, "
                "delivery, description), ask them to clarify which product they mean. "
                "List the available product names so they can choose. "
                "If the question is general (not product-specific), answer it normally.",
                "\nAvailable products:",
            ]
            for name in product_names:
                disambiguation_lines.append(f"  - {name}")
            parts.append("\n".join(disambiguation_lines))
        else:
            parts.append(
                "\n\nYou could not determine which product the buyer is asking about. "
                "If their question is about a specific product (price, availability, "
                "delivery, description), ask them to clarify which product they mean. "
                "Do NOT list product names; just ask which product they are interested in. "
                "If the question is general (not product-specific), answer it normally."
            )

    # --- New feature prompt additions ---

    # Templates context
    templates = _get_templates(config)
    if templates:
        tpl_lines = [
            "\n\nYou have these pre-written response templates. "
            "Use them when the topic matches instead of generating from scratch:"
        ]
        for name, text in templates.items():
            tpl_lines.append(f"  [{name}]: {text}")
        parts.append("\n".join(tpl_lines))

    # Upsell prompt addon
    if config.getboolean("Upsell", "enabled", fallback=False):
        upsell_addon = config.get(
            "Upsell", "prompt_addon",
            fallback="Based on the product catalog, suggest one related product "
                     "the buyer might also like. Keep it brief and natural."
        )
        if upsell_addon.strip():
            parts.append(f"\n\n{upsell_addon}")

    # Promo codes info
    promos = _get_promo_codes(config)
    if promos:
        promo_lines = [
            "\n\nCurrent promotions and promo codes (mention relevant promotions naturally when appropriate):"
        ]
        for p in promos:
            if p["description"]:
                promo_lines.append(f"  Code: {p['code']} - {p['description']}")
            else:
                promo_lines.append(f"  Code: {p['code']}")
        parts.append("\n".join(promo_lines))

    # Language rule
    if config.getboolean("LanguageDetect", "enabled", fallback=False):
        parts.append(
            "\n\nIMPORTANT: Detect the language of the buyer message and ALWAYS reply "
            "in that same language. If the buyer writes in Russian, reply in Russian. "
            "If in English, reply in English. Match their language exactly."
        )
    else:
        # Default policy: Russian-first. Switch to English only when the buyer
        # writes in English. Never reply in English to a Russian message.
        parts.append(
            "\n\nIMPORTANT: By default ALWAYS reply in Russian, even if the buyer's "
            "message contains transliteration, brand names or occasional English words. "
            "Switch to English ONLY if the buyer's message is clearly written in English."
        )

    # Buyer context
    if config.getboolean("BuyerContext", "enabled", fallback=False) and chat_name:
        buyer_ctx = _get_buyer_context_text(chat_name)
        if buyer_ctx:
            parts.append(buyer_ctx)

    # Auto-learning examples
    if config.getboolean("AutoLearning", "enabled", fallback=False):
        learned_text = _get_learned_examples_text(max_items=5)
        if learned_text:
            parts.append(learned_text)

    # Final language reminder (models like Nemotron sometimes ignore earlier instructions)
    if config.getboolean("LanguageDetect", "enabled", fallback=False):
        parts.append(
            "\n\nREMINDER: Reply in the SAME language as the buyer. "
            "Russian message → Russian reply. English message → English reply. "
            "Do NOT mix languages."
        )
    else:
        parts.append(
            "\n\nREMINDER: By default reply in RUSSIAN. "
            "NEVER reply in English to a Russian message."
        )

    # Classification instructions
    parts.append(
        "\n\nIMPORTANT: You MUST prefix every response with exactly one of these tags:\n"
        "[STANDARD] - if you can fully answer the question yourself\n"
        "[FORWARD] - if the question requires the seller's personal attention "
        "(e.g., custom requests, complaints, refund requests, questions you cannot answer)\n"
        "\nExample: [STANDARD] Yes, this product is available for instant delivery!"
    )

    return "\n".join(parts)


def _evict_stale_history(now: float | None = None, ttl: int | None = None) -> None:
    """Remove conversation history entries older than the TTL.

    Must be called while holding _history_lock. ``now`` and ``ttl`` are injectable
    for deterministic testing; by default the wall clock and the config-driven TTL
    are used.
    """
    if now is None:
        now = time.time()
    if ttl is None:
        ttl = get_memory_ttl(load_config())
    stale_ids = [
        cid for cid, ts in _history_last_access.items()
        if (now - ts) > ttl
    ]
    for cid in stale_ids:
        conversation_history.pop(cid, None)
        _history_last_access.pop(cid, None)


def add_to_history(
    chat_id: str, role: str, content: str, max_messages: int,
    now: float | None = None,
) -> None:
    """Add a message to conversation history, trimming if needed. Thread-safe.

    ``now`` is injectable for deterministic testing of the last-activity timestamp.
    """
    if now is None:
        now = time.time()
    with _history_lock:
        _evict_stale_history(now=now)

        if chat_id not in conversation_history:
            conversation_history[chat_id] = []

        conversation_history[chat_id].append({"role": role, "content": content})

        if len(conversation_history[chat_id]) > max_messages:
            conversation_history[chat_id] = conversation_history[chat_id][-max_messages:]

        _history_last_access[chat_id] = now


def _enforce_memory_bounds(
    chat_id: str,
    system_prompt: str,
    message_text: str,
    config: configparser.ConfigParser,
) -> list[dict[str, str]]:
    """Trim a chat's stored history to the configured count and context-token budget.

    Removes only the oldest entries, preserving the most-recent suffix. Returns the
    bounded history. Thread-safe.
    """
    max_messages = get_max_history(config)
    max_context_tokens = config.getint("General", "max_context_tokens", fallback=6000)
    with _history_lock:
        hist = list(conversation_history.get(chat_id, []))
        if len(hist) > max_messages:
            hist = hist[-max_messages:]
        while hist and _estimate_tokens(
            system_prompt + " ".join(m["content"] for m in hist) + message_text
        ) > max_context_tokens:
            hist.pop(0)
        conversation_history[chat_id] = hist
        return hist


def get_history(chat_id: str) -> list[dict[str, str]]:
    """Get a copy of the conversation history for a chat. Thread-safe."""
    with _history_lock:
        _history_last_access[chat_id] = time.time()
        return list(conversation_history.get(chat_id, []))


def call_ai_api(
    api_key: str,
    model: str,
    system_prompt: str,
    chat_id: str,
    user_message: str,
    timeout: int,
    provider: str = "openrouter",
) -> tuple[str | None, int, str | None]:
    """Call the AI API for the specified provider and return (response_text, total_tokens, error_msg).

    For openai/deepseek/gemini/openrouter/custom: uses OpenAI-compatible format.
    For anthropic: uses Anthropic Messages API format.

    Returns (None, 0, error_msg) on error. Retries once on transient failure.
    error_msg is a human-readable string explaining the failure.
    """
    if provider in PROVIDERS:
        api_url = PROVIDERS[provider]
    else:
        custom_url = get_custom_provider_url(provider)
        if custom_url:
            api_url = custom_url
        else:
            logger.warning(
                f"{LOGGER_PREFIX} неизвестный провайдер {provider!r}, откат на openrouter")
            provider = "openrouter"
            api_url = PROVIDERS[provider]

    messages = []
    history = get_history(chat_id)

    if provider == "anthropic":
        # Anthropic format: system is top-level, messages without system role
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        messages.extend(history)
        messages.append({"role": "user", "content": user_message})

        payload = {
            "model": model,
            "system": system_prompt,
            "messages": messages,
            "max_tokens": 4096,
        }
    else:
        # OpenAI-compatible format (openai, deepseek, gemini, openrouter)
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        messages.append({"role": "system", "content": system_prompt})
        messages.extend(history)
        messages.append({"role": "user", "content": user_message})

        payload = {
            "model": model,
            "messages": messages,
        }

    max_attempts = 2
    for attempt in range(max_attempts):
        try:
            response = requests.post(
                api_url,
                headers=headers,
                json=payload,
                timeout=timeout,
            )
            response.raise_for_status()
            data = response.json()

            # Check for API-level error in response body
            if isinstance(data, dict) and data.get("error"):
                err_detail = data["error"]
                if isinstance(err_detail, dict):
                    err_msg_body = err_detail.get("message") or str(err_detail)
                else:
                    err_msg_body = str(err_detail)
                return None, 0, f"API ошибка от {provider}: {err_msg_body[:200]}"

            if provider == "anthropic":
                # Anthropic response format
                text = data["content"][0]["text"]
                total_tokens = 0
                if "usage" in data:
                    input_tokens = data["usage"].get("input_tokens", 0)
                    output_tokens = data["usage"].get("output_tokens", 0)
                    total_tokens = input_tokens + output_tokens
            else:
                # OpenAI-compatible response format
                try:
                    text = data["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError) as parse_err:
                    # Model may return an unexpected structure (e.g. no choices)
                    raw = str(data)[:300]
                    return None, 0, f"Неожиданный формат ответа от {provider} (модель {model}): {parse_err}. Тело: {raw}"
                total_tokens = 0
                if "usage" in data and "total_tokens" in data["usage"]:
                    total_tokens = int(data["usage"]["total_tokens"])

            return text, total_tokens, None
        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code if e.response is not None else 0
            err_body = ""
            try:
                err_body = (e.response.text or "")[:200]
            except Exception:
                pass
            if status_code == 429:
                err_msg = f"HTTP 429 — лимит запросов OpenRouter (модель {model}). Попробуйте позже."
                if attempt < max_attempts - 1:
                    logger.info(f"{LOGGER_PREFIX} {provider} HTTP 429 (модель {model}), повтор...")
                    time.sleep(2)
                    continue
                logger.warning(f"{LOGGER_PREFIX} {provider} HTTP 429 (модель {model}): {err_body}")
                return None, 0, err_msg
            if status_code >= 500:
                err_msg = f"HTTP {status_code} — сервер {provider} временно недоступен (модель {model})."
                if attempt < max_attempts - 1:
                    logger.info(f"{LOGGER_PREFIX} {provider} HTTP {status_code} (модель {model}), повтор...")
                    time.sleep(1)
                    continue
                logger.warning(f"{LOGGER_PREFIX} {provider} HTTP {status_code} (модель {model}): {err_body}")
                return None, 0, err_msg
            err_msg = f"HTTP {status_code} — ошибка API {provider} (модель {model})."
            logger.warning(f"{LOGGER_PREFIX} {provider} HTTP ошибка (модель {model}): {e}")
            return None, 0, err_msg
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            err_msg = f"Ошибка соединения с {provider} (модель {model}): {type(e).__name__}."
            if attempt < max_attempts - 1:
                logger.info(f"{LOGGER_PREFIX} {provider} проблема соединения (модель {model}), повтор: {e}")
                time.sleep(1)
                continue
            logger.warning(f"{LOGGER_PREFIX} {provider} ошибка соединения (модель {model}): {e}")
            return None, 0, err_msg
        except Exception as e:
            err_msg = f"Ошибка API {provider} (модель {model}): {e}"
            logger.warning(f"{LOGGER_PREFIX} {provider} ошибка API (модель {model}): {e}")
            return None, 0, err_msg

    return None, 0, "Неизвестная ошибка"


def call_openrouter_api(
    api_key: str,
    model: str,
    system_prompt: str,
    chat_id: str,
    user_message: str,
    timeout: int,
) -> tuple[str | None, int, str | None]:
    """Call the OpenRouter API and return (response_text, total_tokens, error_msg).

    Returns (None, 0, error_msg) on error. Retries once on transient failure.
    Backward-compatible wrapper around call_ai_api.
    """
    config = load_config()
    provider = get_provider(config)
    return call_ai_api(api_key, model, system_prompt, chat_id, user_message, timeout, provider)


def run_model_test(
    model: str, provider: str, timeout: int = 15,
) -> tuple[bool, str | None, int, str | None]:
    """Quick connectivity test for a provider/model combo.

    Returns ``(ok, reply, tokens, error_msg)``. ``ok`` is False when the API key is missing
    or the request fails. Uses a scratch chat_id so buyer history is untouched.
    ``error_msg`` is a human-readable explanation of the failure.
    """
    config = load_config()
    api_key = get_api_key_for_provider(config, provider)
    if not api_key or api_key == "YOUR_API_KEY_HERE":
        logger.warning(f"{LOGGER_PREFIX} тест модели {model}: API ключ не настроен")
        return False, None, 0, "API ключ не настроен. Откройте /aichat → 🗝 API key."
    reply, tokens, err = call_ai_api(
        api_key, model,
        "Ты тестовый ассистент. Ответь ровно: TEST_OK",
        "__test__",
        "Привет, это работает?",
        timeout,
        provider,
    )
    ok = reply is not None
    if ok:
        logger.info(f"{LOGGER_PREFIX} тест модели {model} ({provider}) пройден, токенов: {tokens}")
    else:
        logger.warning(f"{LOGGER_PREFIX} тест модели {model} ({provider}) НЕ пройден: {err}")
    return ok, reply, tokens, err


def _model_test_text(model: str, provider: str, ok: bool, reply: str | None, tokens: int, error_msg: str | None = None) -> str:
    """Format the auto-test result message shown after saving a model."""
    if not api_key_present_for(provider):
        return (
            f"\u26a0\ufe0f Модель сохранена: <code>{model}</code>\n"
            f"\u274c Тест не выполнен: API ключ не настроен.\n"
            f"Откройте /aichat \u2192 \U0001f511 API key."
        )
    if ok:
        return (
            f"\u2705 Модель сохранена и протестирована: <code>{model}</code>\n"
            f"\U0001f4ca Токенов: {tokens}\n"
            f"\U0001f4ac Ответ: <i>{(reply or '')[:150]}</i>"
        )
    err_line = error_msg or "Проверьте название модели и API ключ."
    return (
        f"\u26a0\ufe0f Модель сохранена: <code>{model}</code>\n"
        f"\u274c Тест НЕ пройден:"
    ) + f"\n{err_line}"


def api_key_present_for(provider: str) -> bool:
    """True if an API key is configured for the provider (general or per-provider)."""
    key = get_api_key_for_provider(load_config(), provider)
    return bool(key) and key != "YOUR_API_KEY_HERE"


# ======================================================================
# v2.1.0 enhancement components (Topic_Filter, Model_Router, redaction,
# QA_Logger, FAQ_Cache, Operator_Pause, Budget_Controller, Escalation)
# ======================================================================

# --- Topic_Filter (Req 2) ---

def topic_filter_check(
    message: str, config: configparser.ConfigParser, buyer_lang: str = "ru"
) -> str | None:
    """Return the Canned_Reply text if an enabled deny-list entry matches ``message``,
    else None. Never calls a model.
    """
    if not topic_filter_enabled(config):
        return None
    deny_list = get_deny_list(config)
    if not deny_list:
        return None
    if _match_keyword(message, deny_list) is None:
        return None
    return get_topic_canned_reply(config, buyer_lang)


# --- Model_Router (Req 3) ---

def route_completion(
    api_key: str,
    system_prompt: str,
    chat_id: str,
    user_message: str,
    timeout: int,
    provider: str,
    primary_model: str,
    fallback_models,
) -> tuple[str | None, int, str | None]:
    """Try the primary model then each fallback (in order) via ``call_ai_api``.

    Returns ``(text, tokens, responding_model)`` on the first success, or
    ``(None, 0, None)`` when every model fails. An empty fallback list yields exactly
    the legacy single-model behaviour.
    """
    models = [primary_model] + [m for m in (fallback_models or []) if m]
    last_err = None
    for model in models:
        text, tokens, err = call_ai_api(
            api_key, model, system_prompt, chat_id, user_message, timeout, provider
        )
        if text is not None:
            return text, tokens, model
        last_err = err
    return None, 0, None


# --- Secret/PII redaction (Req 10) ---

def _collect_secrets(config: configparser.ConfigParser) -> list[str]:
    """Collect configured secret values (API key) that must never appear in logs."""
    secrets = []
    try:
        key = config.get("General", "openrouter_api_key", fallback="")
        if key and key != "YOUR_API_KEY_HERE":
            secrets.append(key)
    except Exception:
        pass
    env_key = os.environ.get("OPENROUTER_API_KEY")
    if env_key:
        secrets.append(env_key)
    return [s for s in secrets if s]


def _redact_secrets(text: str, config: configparser.ConfigParser) -> str:
    """Replace any configured API key / provider secret in ``text`` with ``***``."""
    if not text:
        return text
    result = text
    for secret in _collect_secrets(config):
        if secret and secret in result:
            result = result.replace(secret, "***")
    return result


# --- QA_Logger (Req 5) ---

def _build_qa_record(
    chat_id: str, prompt: str, model: str, answer: str,
    latency_ms: int, tokens: int, ts: float | None = None,
) -> dict:
    """Construct a QA_Record dict. ``model`` may be a responding model id or one of
    the markers ``topic_filter`` / ``faq_cache``."""
    return {
        "ts": time.time() if ts is None else ts,
        "chat_id": str(chat_id),
        "prompt": prompt,
        "model": model,
        "answer": answer,
        "latency_ms": int(latency_ms),
        "tokens": int(tokens) if tokens else 0,
    }


def _load_qa_log() -> dict:
    """Load the QA log sidecar, tolerating missing/invalid files."""
    try:
        with open(QA_LOG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("records"), list):
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {"version": 1, "records": []}


def qa_log(record: dict, config: configparser.ConfigParser) -> None:
    """Append a QA_Record to the capped JSON sidecar if QA logging is enabled.

    Secrets are redacted from string fields before write. Write failures are logged
    and swallowed.
    """
    if not qa_log_enabled(config):
        return
    safe = dict(record)
    for field in ("prompt", "answer", "model"):
        if isinstance(safe.get(field), str):
            safe[field] = _redact_secrets(safe[field], config)
    with _qa_log_lock:
        data = _load_qa_log()
        data["records"].append(safe)
        if len(data["records"]) > QA_LOG_MAX_RECORDS:
            data["records"] = data["records"][-QA_LOG_MAX_RECORDS:]
        try:
            with open(QA_LOG_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except OSError as e:
            logger.warning(f"Could not write QA log: {e}")


# --- FAQ_Cache (Req 6) ---

def _load_faq_store() -> dict:
    """Load the FAQ sidecar, tolerating missing/invalid files."""
    try:
        with open(FAQ_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data.setdefault("version", 1)
            data.setdefault("enabled", False)
            if not isinstance(data.get("entries"), list):
                data["entries"] = []
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {"version": 1, "enabled": False, "entries": []}


def _save_faq_store(store: dict) -> None:
    """Persist the FAQ sidecar under the FAQ lock. Write failures are swallowed."""
    with _faq_lock:
        try:
            with open(FAQ_PATH, "w", encoding="utf-8") as f:
                json.dump(store, f, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.warning(f"Could not write FAQ store: {e}")


def faq_lookup(message: str, faq_store: dict) -> str | None:
    """Return the answer of the first FAQ_Entry whose patterns match ``message``
    while the cache is enabled, else None. Never calls a model.
    """
    if not isinstance(faq_store, dict) or not faq_store.get("enabled"):
        return None
    for entry in faq_store.get("entries", []):
        if not isinstance(entry, dict):
            continue
        patterns = entry.get("patterns") or []
        if _match_keyword(message, patterns) is not None:
            return entry.get("answer", "") or None
    return None


# --- Operator_Presence_Monitor / Operator_Pause (Req 7) ---

def _save_pause_state() -> None:
    """Mirror the in-memory pause map to its sidecar (best-effort). Caller holds lock."""
    try:
        payload = {
            "version": 1,
            "paused": {cid: {"until": until} for cid, until in _operator_pause.items()},
        }
        with open(PAUSE_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except OSError as e:
        logger.debug(f"Could not write pause state: {e}")


def set_operator_pause(
    chat_id: str, config: configparser.ConfigParser, now: float | None = None
) -> None:
    """Set an Operator_Pause for ``chat_id`` expiring after the configured timeout."""
    if now is None:
        now = time.time()
    timeout = get_operator_pause_timeout(config)
    with _pause_lock:
        _operator_pause[str(chat_id)] = now + timeout
        _save_pause_state()


def operator_pause_active(
    chat_id: str, config: configparser.ConfigParser, now: float | None = None
) -> bool:
    """Return True while an Operator_Pause is active for ``chat_id``.

    Expiry is checked inline; an expired pause is cleared. When the monitor is
    disabled this always returns False (no-op).
    """
    if not operator_pause_enabled(config):
        return False
    if now is None:
        now = time.time()
    chat_id = str(chat_id)
    with _pause_lock:
        until = _operator_pause.get(chat_id)
        if until is None:
            return False
        if now > until:
            _operator_pause.pop(chat_id, None)
            _save_pause_state()
            return False
        return True


# --- Budget_Controller (Req 9) ---

def _today_str(today=None) -> str:
    from datetime import date
    return (today or date.today()).isoformat()


def _load_budget_state() -> dict:
    """Load the budget counter sidecar, tolerating missing/invalid files."""
    try:
        with open(BUDGET_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {"version": 1, "date": "", "tokens": 0, "requests": 0, "alert_sent": False}


def _save_budget_state(state: dict) -> None:
    """Persist the budget counter (best-effort). Caller holds _budget_lock."""
    try:
        with open(BUDGET_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except OSError as e:
        logger.debug(f"Could not write budget state: {e}")


def _budget_reset_if_new_day(state: dict, today_iso: str) -> dict:
    """Reset the daily counters when the stored date differs from ``today_iso``."""
    if state.get("date") != today_iso:
        state["date"] = today_iso
        state["tokens"] = 0
        state["requests"] = 0
        state["alert_sent"] = False
    return state


def budget_blocked(config: configparser.ConfigParser, today=None) -> bool:
    """Return True if the budget is enabled and today's usage already reached the limit."""
    if not budget_enabled(config):
        return False
    limit = get_budget_limit(config)
    if limit <= 0:
        return False
    unit = get_budget_unit(config)
    today_iso = _today_str(today)
    with _budget_lock:
        state = _load_budget_state()
        state = _budget_reset_if_new_day(state, today_iso)
        return int(state.get(unit, 0)) >= limit


def budget_accumulate(c, config: configparser.ConfigParser, tokens: int, today=None) -> None:
    """Accumulate today's usage (per configured unit), resetting on a new day and
    sending a Russian operator alert exactly once when the alert threshold is crossed.
    """
    if not budget_enabled(config):
        return
    unit = get_budget_unit(config)
    threshold = get_budget_alert_threshold(config)
    today_iso = _today_str(today)
    fire_alert = False
    usage_now = 0
    with _budget_lock:
        state = _load_budget_state()
        state = _budget_reset_if_new_day(state, today_iso)
        state["tokens"] = int(state.get("tokens", 0)) + int(tokens or 0)
        state["requests"] = int(state.get("requests", 0)) + 1
        usage_now = int(state.get(unit, 0))
        if threshold > 0 and usage_now >= threshold and not state.get("alert_sent"):
            state["alert_sent"] = True
            fire_alert = True
        _save_budget_state(state)
    if fire_alert and c is not None:
        try:
            text = (
                f"<b>AI Chat Plugin — лимит расхода</b>\n\n"
                f"Дневное использование достигло порога оповещения: "
                f"{usage_now} {unit} (порог {threshold})."
            )
            c.telegram.send_notification(text, None, NotificationTypes.new_message)
        except Exception as e:
            logger.error(f"Failed to send budget alert: {e}")


# --- Escalation_Manager (Req 4) ---

def escalation_check(
    c, chat_id: str, chat_name: str, author: str, message: str,
    config: configparser.ConfigParser, now: float | None = None,
) -> bool:
    """If enabled and an Escalation_Keyword matches: notify the operator (RU),
    optionally set an Operator_Pause, and return True so the caller skips the AI.
    Otherwise return False.
    """
    if not escalation_enabled(config):
        return False
    keywords = get_escalation_keywords(config)
    if not keywords:
        return False
    if _match_keyword(message, keywords) is None:
        return False
    try:
        notification_text = (
            f"<b>AI Chat Plugin — эскалация</b>\n\n"
            f"Чат: <b>{chat_name}</b> (ID {chat_id})\n"
            f"Покупатель: <b>{author}</b>\n\n"
            f"Сообщение: {message}"
        )
        keyboard = keyboards.reply(str(chat_id), chat_name)
        c.telegram.send_notification(
            notification_text, keyboard, NotificationTypes.new_message
        )
    except Exception as e:
        logger.error(f"Failed to send escalation notification: {e}")
    if escalation_pause_enabled(config):
        set_operator_pause(str(chat_id), config, now=now)
    return True


_RESPONSE_TAG_RE = re.compile(r"\[?\s*(STANDARD|FORWARD)\s*\]?\s*:?", re.IGNORECASE)


def parse_response(response_text: str) -> tuple[str, str]:
    """Parse AI response into (classification, text).

    Returns:
        Tuple of (classification, cleaned_text) where classification is
        'STANDARD' or 'FORWARD'. Defaults to FORWARD when the model
        omits the tag entirely.

    Толерантный разбор: тег распознаётся без учёта регистра, с/без скобок,
    сквозь обрамляющий markdown (**, `, *) и небольшой ведущий текст —
    чтобы готовый ответ не уходил в FORWARD из-за мелкого расхождения
    форматирования.
    """
    text = (response_text or "").strip()
    if not text:
        return "FORWARD", ""

    # 1) Тег в самом начале (возможно за markdown-обрамлением).
    stripped = text.lstrip("*`_>~ \t\r\n")
    m = _RESPONSE_TAG_RE.match(stripped)
    if m:
        tag = m.group(1).upper()
        cleaned = stripped[m.end():].strip(" *`_:>~-\t\r\n")
        return tag, cleaned

    # 2) Тег где-то в начале сообщения (первые ~40 символов).
    head = text[:40]
    m2 = _RESPONSE_TAG_RE.search(head)
    if m2:
        tag = m2.group(1).upper()
        cleaned = (text[:m2.start()] + text[m2.end():]).strip(" *`_:>~-\t\r\n")
        return tag, cleaned

    # 3) Тега нет — безопасный дефолт: пересылаем продавцу.
    return "FORWARD", text


def is_auto_response_command(c: Cardinal, message_text: str) -> bool:
    """Check if the message matches an auto-response command."""
    if not hasattr(c, "AR_CFG") or c.AR_CFG is None:
        return False
    return message_text.strip() in c.AR_CFG.sections()


# --- Bot / system message filter ---
# Messages from internal bots (steam_rental, steam_ranger, etc.) that should
# NEVER be forwarded to the AI. Patterns cover both RU and EN templates.
_BOT_MESSAGE_PATTERNS: list[str] = [
    # Steam Guard codes
    "Steam Guard",
    " steam guard ",
    # Order/delivery notifications from internal bots
    "Осталось кодов:",
    "codes remaining:",
    "Код действителен",
    "code is valid",
    "Подождите ",
    "please wait",
]


def _is_bot_system_message(message_text: str) -> bool:
    """Return True if the message looks like an internal bot/system message
    (Steam Guard codes, order status updates, bot commands, etc.) that
    should not be processed by the AI chat plugin."""
    text = (message_text or "").strip()
    if not text:
        return False
    lower = text.lower()
    for pattern in _BOT_MESSAGE_PATTERNS:
        if pattern.lower() in lower:
            return True
    # Bot commands: !2фа, !код, !продлить, !статус, !помощь, !rck, !code, etc.
    if text.startswith("!"):
        return True
    return False


def handle_message(
    c: Cardinal,
    chat_id: str,
    chat_name: str,
    message_text: str,
    author: str,
) -> None:
    """Process a buyer message and generate an AI response.

    This function runs in a separate thread to avoid blocking.
    """
    # --- Duplicate-event gate: the same buyer message can arrive via BOTH
    # msg_new_msg and last_chat_message_changed; drop the second copy fast.
    if not _claim_message_for_processing(chat_id, message_text):
        logger.info(
            f"{LOGGER_PREFIX} дубликат события для чата {chat_id} "
            f"({chat_name!r}) — пропуск (двойная доставка Cardinal)")
        return

    # --- Bot/system message filter: skip Steam Guard codes, order updates, etc.
    if _is_bot_system_message(message_text):
        logger.debug(
            f"{LOGGER_PREFIX} системное/бот-сообщение в чате {chat_id} — пропуск")
        return

    config = load_config()

    if not is_enabled(config):
        return

    # --- Blacklist check ---
    if _is_blacklisted(author, config):
        logger.debug(f"Ignoring message from blacklisted user: {author}")
        return

    # --- Operator-pause gate (Req 7): skip AI while an operator is engaged ---
    if operator_pause_active(chat_id, config):
        logger.debug(f"Operator pause active for chat {chat_id}; skipping AI reply.")
        return

    # --- Working hours check ---
    if not _is_working_hours(config):
        offline_msg = config.get(
            "WorkingHours", "offline_message",
            fallback="Seller is offline now, will respond in the morning."
        )
        try:
            c.send_message(chat_id, offline_msg, chat_name)
            logger.info(f"Sent offline message to chat {chat_id} ({chat_name}).")
        except Exception as e:
            logger.error(f"Failed to send offline message to chat {chat_id}: {e}")
        return

    # --- Anti-spam check ---
    if _check_spam(author, config):
        spam_reply = config.get(
            "AntiSpam", "spam_reply",
            fallback="Please wait, you are sending messages too fast."
        )
        try:
            c.send_message(chat_id, spam_reply, chat_name)
            logger.info(f"Sent spam warning to chat {chat_id} ({chat_name}).")
        except Exception as e:
            logger.error(f"Failed to send spam reply to chat {chat_id}: {e}")
        return

    # Buyer language (used by canned replies and the system prompt below).
    buyer_lang = "en" if _detect_language(message_text) == "en" else "ru"

    # --- Topic_Filter deny-list (Req 2): canned reply, no model call ---
    canned = topic_filter_check(message_text, config, buyer_lang)
    if canned is not None:
        try:
            c.send_message(chat_id, canned, chat_name)
            logger.info(f"Topic filter matched in chat {chat_id}; sent canned reply.")
        except Exception as e:
            logger.error(f"Failed to send topic-filter reply to chat {chat_id}: {e}")
        qa_log(_build_qa_record(chat_id, message_text, "topic_filter", canned, 0, 0), config)
        return

    # --- Stop-words check ---
    if _check_stop_words(message_text, config):
        # Forward to seller immediately, skip AI
        try:
            notification_text = (
                f"<b>{_t('ai_stopword_detected')}</b>\n\n"
                f"Chat: <b>{chat_name}</b>\n"
                f"Buyer: <b>{author}</b>\n\n"
                f"Message: {message_text}"
            )
            keyboard = keyboards.reply(chat_id, chat_name)
            c.telegram.send_notification(
                notification_text, keyboard, NotificationTypes.new_message
            )
            logger.info(
                f"Stop-word detected in message from {chat_name}. Forwarded to seller."
            )
        except Exception as e:
            logger.error(f"Failed to forward stop-word message to Telegram: {e}")

        # Update stats if enabled
        if config.getboolean("Statistics", "enabled", fallback=False):
            _update_stats(forwarded=True, tokens=0)
        return

    # --- Escalation (Req 4): notify operator, optionally pause, skip AI ---
    if escalation_check(c, chat_id, chat_name, author, message_text, config):
        logger.info(f"Escalation keyword matched in chat {chat_id}; escalated to operator.")
        if config.getboolean("Statistics", "enabled", fallback=False):
            _update_stats(forwarded=True, tokens=0)
        return

    # --- FAQ_Cache (Req 6): stored answer, no model call ---
    if faq_enabled(config):
        faq_answer = faq_lookup(message_text, _load_faq_store())
        if faq_answer:
            try:
                c.send_message(chat_id, faq_answer, chat_name)
                logger.info(f"FAQ cache hit in chat {chat_id}; sent stored answer.")
            except Exception as e:
                logger.error(f"Failed to send FAQ reply to chat {chat_id}: {e}")
            qa_log(_build_qa_record(chat_id, message_text, "faq_cache", faq_answer, 0, 0), config)
            return

    # --- Budget gate (Req 9): stop paid calls once the daily budget is exhausted ---
    if budget_blocked(config):
        budget_reply = get_budget_canned_reply(config, buyer_lang)
        try:
            c.send_message(chat_id, budget_reply, chat_name)
            logger.info(f"Daily budget exhausted; sent canned reply to chat {chat_id}.")
        except Exception as e:
            logger.error(f"Failed to send budget reply to chat {chat_id}: {e}")
        return

    provider = get_provider(config)
    api_key = get_api_key_for_provider(config, provider)
    if not api_key or api_key == "YOUR_API_KEY_HERE":
        logger.warning(f"{LOGGER_PREFIX} API ключ не настроен (провайдер {provider}).")
        return

    model = get_model(config)
    max_history = get_max_history(config)
    timeout = get_response_timeout(config)

    # Build system prompt with dynamic context
    system_prompt = build_system_prompt(c, config, chat_name, buyer_lang=buyer_lang)

    # Prompt size guard: check approximate token count before calling API
    max_context_tokens = config.getint("General", "max_context_tokens", fallback=6000)
    history = get_history(chat_id)
    history_text = " ".join(m["content"] for m in history)
    total_estimated_tokens = _estimate_tokens(system_prompt + history_text + message_text)
    if total_estimated_tokens > max_context_tokens:
        logger.warning(
            f"Estimated context size ({total_estimated_tokens} tokens) exceeds "
            f"max_context_tokens ({max_context_tokens}). Truncating history."
        )
        # Truncate history by removing oldest messages until within budget
        while history and _estimate_tokens(
            system_prompt + " ".join(m["content"] for m in history) + message_text
        ) > max_context_tokens:
            history.pop(0)
        # Update the stored history to the truncated version
        with _history_lock:
            conversation_history[chat_id] = history

    # Call the API via the Model_Router (primary + fallbacks)
    fallback_models = get_fallback_models(config)
    _t_start = time.time()
    response_text, tokens_used, responding_model = route_completion(
        api_key, system_prompt, chat_id, message_text, timeout,
        provider, model, fallback_models,
    )
    latency_ms = int((time.time() - _t_start) * 1000)

    if response_text is None:
        logger.warning(
            f"{LOGGER_PREFIX} нет ответа от AI для чата {chat_id} "
            f"(провайдер {provider}, модели: {[model] + fallback_models}). Auto-reply пропущен."
        )
        return

    logger.info(
        f"{LOGGER_PREFIX} AI ответ получен: чат {chat_id}, модель {responding_model or model}, "
        f"токенов {tokens_used}, {latency_ms} мс"
    )

    # Parse the classification
    classification, reply_text = parse_response(response_text)

    if not reply_text:
        logger.warning("AI returned empty reply text. Skipping.")
        return

    # Update conversation history (thread-safe).
    # ВАЖНО: ассистентский ход добавляем НИЖЕ — только то, что покупатель
    # реально увидел. Иначе при FORWARD непосланный AI-ответ засоряет контекст.
    add_to_history(chat_id, "user", message_text, max_history)

    # Update statistics
    forwarded = classification == "FORWARD"
    if config.getboolean("Statistics", "enabled", fallback=False):
        _update_stats(forwarded=forwarded, tokens=tokens_used)

    if classification == "STANDARD":
        # Send reply directly to the buyer
        try:
            c.send_message(chat_id, reply_text, chat_name)
            logger.info(
                f"{LOGGER_PREFIX} AI-ответ отправлен в чат {chat_id} ({chat_name}), "
                f"модель {responding_model or model}."
            )
            # Notify seller that AI answered (optional)
            if config.getboolean("Forwarding", "notify_on_standard", fallback=False):
                try:
                    used_model = responding_model or model
                    # Get improvement suggestion from AI (non-blocking)
                    suggestion = ""
                    try:
                        suggest_prompt = (
                            "Ты — ревьюер ответов продавца. Вот вопрос покупателя и ответ продавца. "
                            "Предложи ОДНУ краткую идею как улучшить ответ (1-2 предложения). "
                            "Если ответ хороший — просто скажи '✅ Ответ хороший'. "
                            "Отвечай ТОЛЬКО на русском."
                        )
                        suggest_reply, _ = call_ai_api(
                            api_key, model, suggest_prompt, "__suggest__",
                            f"Вопрос: {message_text}\nОтвет продавца: {reply_text}",
                            10, provider,
                        )
                        if suggest_reply:
                            suggestion = suggest_reply.strip()
                    except Exception:
                        pass
                    notify_text = (
                        f"<b>🤖 AI ответил покупателю</b>\n\n"
                        f"Chat: <b>{chat_name}</b>\n"
                        f"Buyer: <b>{author}</b>\n\n"
                        f"<b>Вопрос:</b> {message_text}\n\n"
                        f"<b>🤖 Модель:</b> <code>{used_model}</code>\n\n"
                        f"<b>Ответ AI:</b>\n{reply_text}"
                    )
                    if suggestion:
                        notify_text += f"\n\n<b>💡 Как улучшить:</b>\n<i>{suggestion}</i>"
                    c.telegram.send_notification(
                        notify_text, None, NotificationTypes.new_message
                    )
                except Exception as e:
                    logger.debug(f"Failed to send standard notification: {e}")
            # В историю кладём именно отправленный ответ.
            add_to_history(chat_id, "assistant", reply_text, max_history)
        except Exception as e:
            logger.error(f"Failed to send message to chat {chat_id}: {e}")

    elif classification == "FORWARD":
        forward_enabled = should_forward_non_standard(config)

        if not forward_enabled:
            # Forwarding disabled: AI answers directly even for non-standard questions
            try:
                c.send_message(chat_id, reply_text, chat_name)
                logger.info(
                    f"{LOGGER_PREFIX} AI-ответ (FORWARD→STANDARD, пересылка выключена) "
                    f"отправлен в чат {chat_id} ({chat_name}), модель {responding_model or model}."
                )
                add_to_history(chat_id, "assistant", reply_text, max_history)
            except Exception as e:
                logger.error(f"Failed to send message to chat {chat_id}: {e}")
            # Still notify seller if notify_on_standard is enabled
            if config.getboolean("Forwarding", "notify_on_standard", fallback=False):
                try:
                    used_model = responding_model or model
                    notify_text = (
                        f"<b>🤖 AI ответил (без пересылки)</b>\n\n"
                        f"Chat: <b>{chat_name}</b>\n"
                        f"Buyer: <b>{author}</b>\n\n"
                        f"<b>Вопрос:</b> {message_text}\n\n"
                        f"<b>🤖 Модель:</b> <code>{used_model}</code>\n\n"
                        f"<b>Ответ AI:</b>\n{reply_text}"
                    )
                    c.telegram.send_notification(
                        notify_text, None, NotificationTypes.new_message
                    )
                except Exception as e:
                    logger.debug(f"Failed to send standard notification: {e}")
        else:
            # Forwarding enabled: send holding message + forward to seller
            holding_message = get_holding_message(config)
            try:
                c.send_message(chat_id, holding_message, chat_name)
                logger.info(
                    f"Sent holding message to chat {chat_id} ({chat_name})."
                )
                add_to_history(chat_id, "assistant", holding_message, max_history)
            except Exception as e:
                logger.error(f"Failed to send holding message to chat {chat_id}: {e}")

        # Forward to seller via Telegram if enabled
        if forward_enabled:
            try:
                used_model = responding_model or model
                notification_text = (
                    f"<b>{_t('ai_manual_attention')}</b>\n\n"
                    f"Chat: <b>{chat_name}</b>\n"
                    f"Buyer: <b>{author}</b>\n\n"
                    f"<b>Вопрос покупателя:</b>\n{message_text}\n\n"
                    f"<b>🤖 Модель:</b> <code>{used_model}</code>\n\n"
                    f"<b>Ответ модели:</b>\n{reply_text}"
                )
                keyboard = keyboards.reply(chat_id, chat_name)
                c.telegram.send_notification(
                    notification_text, keyboard, NotificationTypes.new_message
                )
                logger.info(
                    f"Forwarded message from {chat_name} to Telegram."
                )
            except Exception as e:
                logger.error(f"Failed to forward to Telegram: {e}")

        # Track forwarded question for auto-learning
        if config.getboolean("AutoLearning", "enabled", fallback=False):
            with _pending_forwards_lock:
                # TTL eviction: remove entries older than 3600 seconds
                now_ts = time.time()
                expired_keys = [k for k, v in _pending_forwards.items()
                                if now_ts - v.get("timestamp", 0) > 3600]
                for k in expired_keys:
                    del _pending_forwards[k]
                _pending_forwards[chat_id] = {"question": message_text, "timestamp": now_ts}

    # --- Budget accumulate + Q&A logging (Req 9, 5) ---
    budget_accumulate(c, config, tokens_used)
    qa_log(
        _build_qa_record(
            chat_id, message_text, responding_model or model,
            reply_text, latency_ms, tokens_used,
        ),
        config,
    )

    # Update buyer context
    if config.getboolean("BuyerContext", "enabled", fallback=False):
        _update_buyer_context(author)


def new_message_handler(c: Cardinal, e) -> None:
    """Handler for new message events (new mode)."""
    config = load_config()

    if not is_enabled(config):
        return

    msg = e.message

    # Auto-learning: detect seller replies after FORWARD
    if msg.author_id == c.account.id:
        chat_id = str(msg.chat_id)
        # Operator-presence (Req 7): a live operator message pauses AI for that chat.
        try:
            set_operator_pause(chat_id, config)
        except Exception as e:
            logger.debug(f"Failed to set operator pause for chat {chat_id}: {e}")
        with _pending_forwards_lock:
            if chat_id in _pending_forwards:
                entry = _pending_forwards[chat_id]
                age = time.time() - entry.get("timestamp", 0)
                if age < 3600:
                    seller_text = msg.text
                    if seller_text and config.getboolean("AutoLearning", "enabled", fallback=False):
                        _add_learned_response(entry["question"], seller_text)
                del _pending_forwards[chat_id]
        return

    message_text = msg.text
    if not message_text:
        return

    # Skip if message matches an auto-response command
    if is_auto_response_command(c, message_text):
        return

    chat_id = str(msg.chat_id)
    chat_name = msg.chat_name or ""
    author = msg.author or ""

    # Run in a separate thread to avoid blocking
    thread = threading.Thread(
        target=handle_message,
        args=(c, chat_id, chat_name, message_text, author),
        daemon=True,
    )
    thread.start()


def last_chat_message_changed_handler(c: Cardinal, e) -> None:
    """Handler for last chat message changed events (old mode)."""
    config = load_config()

    if not is_enabled(config):
        return

    chat = e.chat

    if not chat.unread:
        return

    if hasattr(chat, "type"):
        from FunPayAPI.types import MessageTypes
        if chat.type != MessageTypes.NON_SYSTEM:
            return

    message_text = str(chat).strip()
    if not message_text:
        return

    if is_auto_response_command(c, message_text):
        return

    chat_id = str(chat.id)
    chat_name = chat.name or ""
    author = chat_name

    thread = threading.Thread(
        target=handle_message,
        args=(c, chat_id, chat_name, message_text, author),
        daemon=True,
    )
    thread.start()


BIND_TO_NEW_MESSAGE = [new_message_handler]
BIND_TO_LAST_CHAT_MESSAGE_CHANGED = [last_chat_message_changed_handler]


# --- Config saving ---

def save_config(config: configparser.ConfigParser) -> None:
    """Write current config state back to the INI file."""
    global _config_cache, _config_cache_time
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        config.write(f)
    # Invalidate cache
    _config_cache = None
    _config_cache_time = 0.0


# --- Telegram Settings Interface ---

def _settings_keyboard() -> telebot.types.InlineKeyboardMarkup:
    """Build the MAIN settings menu - category navigation only."""
    config = load_config()
    enabled = config.getboolean("General", "enabled", fallback=False)
    enabled_icon = "\u2705" if enabled else "\u274c"

    # Current UI language
    ui_lang = config.get("General", "language", fallback="ru")
    lang_display = _t("language_ru") if ui_lang == "ru" else _t("language_en")

    kb = telebot.types.InlineKeyboardMarkup()
    kb.row(telebot.types.InlineKeyboardButton(
        f"{enabled_icon} {_t('plugin_enabled')}", callback_data=AIChatCBT.TOGGLE_ENABLED))
    kb.row(
        telebot.types.InlineKeyboardButton("\u2699\ufe0f \u041e\u0441\u043d\u043e\u0432\u043d\u044b\u0435", callback_data=AIChatCBT.CATEGORY_CORE),
        telebot.types.InlineKeyboardButton("\U0001f4ac \u0410\u0432\u0442\u043e\u043e\u0442\u0432\u0435\u0442\u0447\u0438\u043a", callback_data=AIChatCBT.CATEGORY_RESPONDER),
    )
    kb.row(
        telebot.types.InlineKeyboardButton("\U0001f6e1 \u041c\u043e\u0434\u0435\u0440\u0430\u0446\u0438\u044f", callback_data=AIChatCBT.CATEGORY_MODERATION),
        telebot.types.InlineKeyboardButton("\U0001f4b0 \u041f\u0440\u043e\u0434\u0430\u0436\u0438", callback_data=AIChatCBT.CATEGORY_SALES),
    )
    kb.row(telebot.types.InlineKeyboardButton("\U0001f4ca \u0421\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u043a\u0430", callback_data=AIChatCBT.CATEGORY_STATS))
    kb.row(telebot.types.InlineKeyboardButton(
        f"{_t('view_logs')}", callback_data=AIChatCBT.VIEW_LOGS))
    # Language toggle
    kb.row(
        telebot.types.InlineKeyboardButton(
            f"\U0001f310 {_t('language_label', lang=lang_display)}",
            callback_data=AIChatCBT.TOGGLE_LANGUAGE,
        )
    )
    return kb


def _core_keyboard() -> telebot.types.InlineKeyboardMarkup:
    """Core settings sub-menu: model, API key, max history, timeout, provider."""
    kb = telebot.types.InlineKeyboardMarkup()
    kb.row(telebot.types.InlineKeyboardButton(
               f"\U0001f916 {_t('model_label')}", callback_data=AIChatCBT.EDIT_MODEL),
           telebot.types.InlineKeyboardButton(
               f"\U0001f511 {_t('api_key_label')}", callback_data=AIChatCBT.EDIT_API_KEY))
    kb.row(telebot.types.InlineKeyboardButton(
               f"\U0001f4ca {_t('max_history_label')}", callback_data=AIChatCBT.EDIT_MAX_HISTORY),
           telebot.types.InlineKeyboardButton(
               f"\u23f1 {_t('timeout_label')}", callback_data=AIChatCBT.EDIT_TIMEOUT))
    kb.row(telebot.types.InlineKeyboardButton(
        f"\U0001f310 {_t('provider_label')}", callback_data=AIChatCBT.EDIT_PROVIDER),
        telebot.types.InlineKeyboardButton(
        f"\U0001f9ea {_t('test_model')}", callback_data=AIChatCBT.TEST_MODEL))
    config = load_config()
    budget_on = budget_enabled(config)
    kb.row(telebot.types.InlineKeyboardButton(
        f"\U0001f9e0 {_t('memory_ttl_label')}", callback_data=AIChatCBT.EDIT_MEMORY_TTL),
        telebot.types.InlineKeyboardButton(
        f"\U0001f501 {_t('fallback_models_label')}", callback_data=AIChatCBT.EDIT_FALLBACK_MODELS))
    kb.row(telebot.types.InlineKeyboardButton(
        f"{'✅' if budget_on else '❌'} {_t('budget')}", callback_data=AIChatCBT.TOGGLE_BUDGET))
    kb.row(telebot.types.InlineKeyboardButton(
        f"\U0001f4b5 {_t('budget_limit_label')}", callback_data=AIChatCBT.EDIT_BUDGET_LIMIT),
        telebot.types.InlineKeyboardButton(
        f"\U0001f4cf {_t('budget_unit_label')}", callback_data=AIChatCBT.EDIT_BUDGET_UNIT))
    kb.row(telebot.types.InlineKeyboardButton(
        f"\U0001f514 {_t('budget_alert_label')}", callback_data=AIChatCBT.EDIT_BUDGET_ALERT))
    kb.row(telebot.types.InlineKeyboardButton(
        f"\u25c0\ufe0f {_t('back')}", callback_data=AIChatCBT.BACK_TO_MAIN))
    return kb


def _responder_keyboard() -> telebot.types.InlineKeyboardMarkup:
    """Auto-responder sub-menu."""
    config = load_config()
    notify_ad = config.getboolean("AutoDelivery", "notify_auto_delivery", fallback=True)
    forward = config.getboolean("Forwarding", "forward_non_standard", fallback=True)
    list_products = config.getboolean("General", "list_products_on_clarify", fallback=False)
    lang_detect = config.getboolean("LanguageDetect", "enabled", fallback=False)
    buyer_context = config.getboolean("BuyerContext", "enabled", fallback=False)
    auto_learning = config.getboolean("AutoLearning", "enabled", fallback=False)

    kb = telebot.types.InlineKeyboardMarkup()
    kb.row(telebot.types.InlineKeyboardButton(
        f"{'✅' if notify_ad else '❌'} {_t('auto_delivery_info')}",
        callback_data=AIChatCBT.TOGGLE_NOTIFY_AD))
    kb.row(telebot.types.InlineKeyboardButton(
        f"{'✅' if forward else '❌'} {_t('forward_questions')}",
        callback_data=AIChatCBT.TOGGLE_FORWARD))
    notify_std = config.getboolean("Forwarding", "notify_on_standard", fallback=False)
    kb.row(telebot.types.InlineKeyboardButton(
        f"{'✅' if notify_std else '❌'} {_t('notify_standard_label')}",
        callback_data=AIChatCBT.TOGGLE_NOTIFY_STANDARD))
    kb.row(telebot.types.InlineKeyboardButton(
        f"{'✅' if list_products else '❌'} {_t('list_products')}",
        callback_data=AIChatCBT.TOGGLE_LIST_PRODUCTS))
    kb.row(telebot.types.InlineKeyboardButton(
        f"{'✅' if lang_detect else '❌'} {_t('language_detect')}",
        callback_data=AIChatCBT.TOGGLE_LANGUAGE_DETECT))
    kb.row(telebot.types.InlineKeyboardButton(
        f"{'✅' if buyer_context else '❌'} {_t('buyer_context')}",
        callback_data=AIChatCBT.TOGGLE_BUYER_CONTEXT))
    kb.row(telebot.types.InlineKeyboardButton(
        f"{'✅' if auto_learning else '❌'} {_t('auto_learning')}",
        callback_data=AIChatCBT.TOGGLE_AUTO_LEARNING))
    kb.row(telebot.types.InlineKeyboardButton(
        f"\U0001f4da {_t('manage_learned')}", callback_data=AIChatCBT.MANAGE_LEARNED))
    kb.row(telebot.types.InlineKeyboardButton(
        f"\U0001f4dd {_t('system_prompt_label')}", callback_data=AIChatCBT.EDIT_SYSTEM_PROMPT))
    kb.row(telebot.types.InlineKeyboardButton(
        f"\U0001f4dc {_t('prompt_preset_label')}", callback_data=AIChatCBT.EDIT_PROMPT_PRESET))
    kb.row(telebot.types.InlineKeyboardButton(
        "\U0001f4e5 Скачать промпт", callback_data=AIChatCBT.DOWNLOAD_PROMPT))
    kb.row(telebot.types.InlineKeyboardButton(
        "\U0001f4cb Управление пресетами", callback_data=AIChatCBT.MANAGE_PRESETS))
    kb.row(telebot.types.InlineKeyboardButton(
        f"\U0001f4ac {_t('holding_msg_label')}", callback_data=AIChatCBT.EDIT_HOLDING_MSG))
    kb.row(telebot.types.InlineKeyboardButton(
        f"\u25c0\ufe0f {_t('back')}", callback_data=AIChatCBT.BACK_TO_MAIN))
    return kb


def _moderation_keyboard() -> telebot.types.InlineKeyboardMarkup:
    """Moderation sub-menu: anti-spam, blacklist, stop-words, working hours."""
    config = load_config()
    antispam = config.getboolean("AntiSpam", "enabled", fallback=False)
    working_hours = config.getboolean("WorkingHours", "enabled", fallback=False)
    blacklist = config.getboolean("Blacklist", "enabled", fallback=False)
    stopwords = config.getboolean("StopWords", "enabled", fallback=False)

    kb = telebot.types.InlineKeyboardMarkup()
    kb.row(telebot.types.InlineKeyboardButton(
        f"{'✅' if antispam else '❌'} {_t('anti_spam')}",
        callback_data=AIChatCBT.TOGGLE_ANTISPAM))
    kb.row(telebot.types.InlineKeyboardButton(
        f"{'✅' if working_hours else '❌'} {_t('working_hours')}",
        callback_data=AIChatCBT.TOGGLE_WORKING_HOURS))
    kb.row(telebot.types.InlineKeyboardButton(
        f"{'✅' if blacklist else '❌'} {_t('blacklist')}",
        callback_data=AIChatCBT.TOGGLE_BLACKLIST))
    kb.row(telebot.types.InlineKeyboardButton(
        f"{'✅' if stopwords else '❌'} {_t('stop_words')}",
        callback_data=AIChatCBT.TOGGLE_STOPWORDS))
    kb.row(
        telebot.types.InlineKeyboardButton(
            f"\U0001f6ab {_t('manage_blacklist')}", callback_data=AIChatCBT.MANAGE_BLACKLIST),
        telebot.types.InlineKeyboardButton(
            f"\u26d4 {_t('manage_stopwords')}", callback_data=AIChatCBT.MANAGE_STOPWORDS))
    kb.row(telebot.types.InlineKeyboardButton(
        f"\U0001f552 {_t('enter_wh_start')[:20]}", callback_data=AIChatCBT.EDIT_WORKING_HOURS_START))
    kb.row(telebot.types.InlineKeyboardButton(
        f"\U0001f552 {_t('enter_wh_end')[:20]}", callback_data=AIChatCBT.EDIT_WORKING_HOURS_END))
    kb.row(telebot.types.InlineKeyboardButton(
        f"\U0001f4ac Offline msg", callback_data=AIChatCBT.EDIT_OFFLINE_MSG))
    # v2.1.0: topic filter, escalation, operator pause
    topic_on = topic_filter_enabled(config)
    esc_on = escalation_enabled(config)
    esc_pause = escalation_pause_enabled(config)
    oppause_on = operator_pause_enabled(config)
    kb.row(telebot.types.InlineKeyboardButton(
        f"{'✅' if topic_on else '❌'} {_t('topic_filter')}",
        callback_data=AIChatCBT.TOGGLE_TOPIC_FILTER),
        telebot.types.InlineKeyboardButton(
        f"\U0001f4dd {_t('manage_topic_filter')}", callback_data=AIChatCBT.MANAGE_TOPIC_FILTER))
    kb.row(telebot.types.InlineKeyboardButton(
        f"\U0001f4ac {_t('topic_canned_label')}", callback_data=AIChatCBT.EDIT_TOPIC_CANNED_REPLY))
    kb.row(telebot.types.InlineKeyboardButton(
        f"{'✅' if esc_on else '❌'} {_t('escalation')}",
        callback_data=AIChatCBT.TOGGLE_ESCALATION),
        telebot.types.InlineKeyboardButton(
        f"\U0001f4dd {_t('manage_escalation')}", callback_data=AIChatCBT.MANAGE_ESCALATION))
    kb.row(telebot.types.InlineKeyboardButton(
        f"{'✅' if esc_pause else '❌'} {_t('escalation_pause')}",
        callback_data=AIChatCBT.TOGGLE_ESCALATION_PAUSE))
    kb.row(telebot.types.InlineKeyboardButton(
        f"{'✅' if oppause_on else '❌'} {_t('operator_pause')}",
        callback_data=AIChatCBT.TOGGLE_OPERATOR_PAUSE),
        telebot.types.InlineKeyboardButton(
        f"\u23f1 {_t('pause_timeout_label')}", callback_data=AIChatCBT.EDIT_PAUSE_TIMEOUT))
    kb.row(telebot.types.InlineKeyboardButton(
        f"\u25c0\ufe0f {_t('back')}", callback_data=AIChatCBT.BACK_TO_MAIN))
    return kb


def _sales_keyboard() -> telebot.types.InlineKeyboardMarkup:
    """Sales sub-menu: upsell, promos, templates."""
    config = load_config()
    upsell = config.getboolean("Upsell", "enabled", fallback=False)
    promos = config.getboolean("Promos", "enabled", fallback=False)
    templates = config.getboolean("Templates", "enabled", fallback=False)

    kb = telebot.types.InlineKeyboardMarkup()
    kb.row(telebot.types.InlineKeyboardButton(
        f"{'✅' if upsell else '❌'} {_t('upsell')}",
        callback_data=AIChatCBT.TOGGLE_UPSELL))
    kb.row(telebot.types.InlineKeyboardButton(
        f"{'✅' if promos else '❌'} {_t('promos')}",
        callback_data=AIChatCBT.TOGGLE_PROMOS))
    kb.row(telebot.types.InlineKeyboardButton(
        f"{'✅' if templates else '❌'} {_t('templates')}",
        callback_data=AIChatCBT.TOGGLE_TEMPLATES))
    kb.row(
        telebot.types.InlineKeyboardButton(
            f"\U0001f4b0 {_t('upsell_prompt_label')}", callback_data=AIChatCBT.MANAGE_UPSELL),
        telebot.types.InlineKeyboardButton(
            f"\U0001f381 {_t('manage_promos')}", callback_data=AIChatCBT.MANAGE_PROMOS))
    kb.row(telebot.types.InlineKeyboardButton(
        f"\U0001f4cb {_t('manage_templates')}", callback_data=AIChatCBT.MANAGE_TEMPLATES))
    faq_on = faq_enabled(config)
    kb.row(telebot.types.InlineKeyboardButton(
        f"{'✅' if faq_on else '❌'} {_t('faq')}", callback_data=AIChatCBT.TOGGLE_FAQ),
        telebot.types.InlineKeyboardButton(
        f"\U0001f4d6 {_t('manage_faq')}", callback_data=AIChatCBT.MANAGE_FAQ))
    kb.row(telebot.types.InlineKeyboardButton(
        f"\U0001f4e6 {_t('product_catalog')}", callback_data=AIChatCBT.MANAGE_PRODUCTS))
    kb.row(telebot.types.InlineKeyboardButton(
        f"\u25c0\ufe0f {_t('back')}", callback_data=AIChatCBT.BACK_TO_MAIN))
    return kb


def _stats_keyboard() -> telebot.types.InlineKeyboardMarkup:
    """Statistics sub-menu."""
    config = load_config()
    statistics = config.getboolean("Statistics", "enabled", fallback=False)
    qa_on = qa_log_enabled(config)

    kb = telebot.types.InlineKeyboardMarkup()
    kb.row(telebot.types.InlineKeyboardButton(
        f"{'✅' if statistics else '❌'} {_t('statistics')}",
        callback_data=AIChatCBT.TOGGLE_STATISTICS))
    kb.row(telebot.types.InlineKeyboardButton(
        f"{'✅' if qa_on else '❌'} {_t('qa_log')}", callback_data=AIChatCBT.TOGGLE_QA_LOG))
    kb.row(telebot.types.InlineKeyboardButton(
        f"\U0001f4c8 {_t('view_stats')}", callback_data=AIChatCBT.VIEW_STATS))
    kb.row(telebot.types.InlineKeyboardButton(
        f"\U0001f5d1 {_t('reset_stats')}", callback_data=AIChatCBT.RESET_STATS))
    kb.row(telebot.types.InlineKeyboardButton(
        f"\u25c0\ufe0f {_t('back')}", callback_data=AIChatCBT.BACK_TO_MAIN))
    return kb


def _format_current_settings() -> str:
    """Format current settings - simplified main view."""
    config = load_config()
    enabled = config.getboolean("General", "enabled", fallback=False)
    api_key = get_api_key(config)
    model = get_model(config)
    provider = get_provider(config)

    if api_key and api_key != "YOUR_API_KEY_HERE":
        masked_key = api_key[:8] + "..." + api_key[-4:] if len(api_key) > 12 else "***"
    else:
        masked_key = _t("not_set")

    icon = lambda v: "\u2705" if v else "\u274c"

    provider_display = _t(f"provider_{provider}") if f"provider_{provider}" in LANG.get(_current_lang, {}) else provider
    presets = get_prompt_presets(config)
    preset_names = []
    for p in presets:
        name = _t(f"preset_{p}") if f"preset_{p}" in LANG.get(_current_lang, {}) else p
        preset_names.append(name)
    preset_display = ", ".join(preset_names)

    # Count active features
    features = []
    if config.getboolean("AntiSpam", "enabled", fallback=False): features.append(_t("anti_spam"))
    if config.getboolean("WorkingHours", "enabled", fallback=False): features.append(_t("working_hours"))
    if config.getboolean("Blacklist", "enabled", fallback=False): features.append(_t("blacklist"))
    if config.getboolean("StopWords", "enabled", fallback=False): features.append(_t("stop_words"))
    if config.getboolean("Upsell", "enabled", fallback=False): features.append(_t("upsell"))
    if config.getboolean("Promos", "enabled", fallback=False): features.append(_t("promos"))
    if config.getboolean("Templates", "enabled", fallback=False): features.append(_t("templates"))
    if config.getboolean("LanguageDetect", "enabled", fallback=False): features.append(_t("language_detect"))

    features_text = ", ".join(features) if features else _t("not_set")

    return (
        f"<b>\U0001f916 AI Chat Plugin v{VERSION}</b>\n\n"
        f"{_t('status_label')}: {icon(enabled)} {_t('enabled_text') if enabled else _t('disabled_text')}\n"
        f"  \u2139\ufe0f {_t('desc_plugin_enabled')}\n"
        f"{_t('model_label')}: <code>{model}</code>\n"
        f"  \u2139\ufe0f {_t('desc_model')}\n"
        f"{_t('provider_label')}: <code>{provider_display}</code>\n"
        f"{_t('prompt_preset_label')}: <code>{preset_display}</code>\n"
        f"  \U0001f4dd <i>{_get_effective_prompt(config)[:100]}{'...' if len(_get_effective_prompt(config)) > 100 else ''}</i>\n"
        f"API: <code>{masked_key}</code>\n"
        f"  \u2139\ufe0f {_t('desc_api_key')}\n\n"
        f"{_t('features_section')}: {features_text}\n\n"
        f"\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u043a\u0430\u0442\u0435\u0433\u043e\u0440\u0438\u044e \u0434\u043b\u044f \u043d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0438:"
    )


def _format_core_settings() -> str:
    """Format core settings sub-menu header."""
    config = load_config()
    model = get_model(config)
    api_key = get_api_key(config)
    max_history = get_max_history(config)
    timeout = get_response_timeout(config)
    provider = get_provider(config)
    provider_display = _t(f"provider_{provider}") if f"provider_{provider}" in LANG.get(_current_lang, {}) else provider
    if api_key and api_key != "YOUR_API_KEY_HERE":
        masked_key = api_key[:8] + "..." + api_key[-4:] if len(api_key) > 12 else "***"
    else:
        masked_key = _t("not_set")
    return (
        f"<b>\u2699\ufe0f {_t('core_section')}</b>\n\n"
        f"{_t('model_label')}: <code>{model}</code>\n"
        f"{_t('provider_label')}: <code>{provider_display}</code>\n"
        f"{_t('api_key_label')}: <code>{masked_key}</code>\n"
        f"{_t('max_history_label')}: <code>{max_history}</code>\n"
        f"{_t('timeout_label')}: <code>{timeout}s</code>"
    )


def _format_responder_settings() -> str:
    """Format responder settings sub-menu header."""
    config = load_config()
    presets = get_prompt_presets(config)
    effective_prompt = _get_effective_prompt(config)
    prompt_display = effective_prompt[:150] + "..." if len(effective_prompt) > 150 else effective_prompt

    preset_names = []
    for p in presets:
        name = _t(f"preset_{p}") if f"preset_{p}" in LANG.get(_current_lang, {}) else p
        preset_names.append(name)
    preset_display = ", ".join(preset_names)

    return (
        f"<b>\U0001f4ac {_t('forward_questions')}</b>\n\n"
        f"{_t('prompt_preset_label')}: <b>{preset_display}</b>\n\n"
        f"\U0001f4dd {_t('system_prompt_label')}:\n<i>{prompt_display}</i>"
    )


def _format_moderation_settings() -> str:
    """Format moderation settings sub-menu header."""
    lines = [f"<b>\U0001f6e1 {_t('features_section')}</b>\n"]
    lines.append(f"\u2022 {_t('anti_spam')}: <i>{_t('desc_anti_spam')}</i>")
    lines.append(f"\u2022 {_t('working_hours')}: <i>{_t('desc_working_hours')}</i>")
    lines.append(f"\u2022 {_t('blacklist')}: <i>{_t('desc_blacklist')}</i>")
    lines.append(f"\u2022 {_t('stop_words')}: <i>{_t('desc_stop_words')}</i>")
    return "\n".join(lines)


def _format_sales_settings() -> str:
    """Format sales settings sub-menu header."""
    lines = [f"<b>\U0001f4b0 {_t('upsell')}</b>\n"]
    lines.append(f"\u2022 {_t('upsell')}: <i>{_t('desc_upsell')}</i>")
    lines.append(f"\u2022 {_t('promos')}: <i>{_t('desc_promos')}</i>")
    lines.append(f"\u2022 {_t('templates')}: <i>{_t('desc_templates')}</i>")
    return "\n".join(lines)


def _format_stats_settings() -> str:
    """Format stats settings sub-menu header."""
    return f"<b>\U0001f4ca {_t('statistics')}</b>\n\n\u0423\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0438\u0435 \u0441\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u043a\u043e\u0439 \u043f\u043b\u0430\u0433\u0438\u043d\u0430:"


def _toggle_setting(section: str, key: str) -> None:
    """Toggle a boolean setting in config and save."""
    config = load_config()
    if not config.has_section(section):
        config.add_section(section)
    current = config.getboolean(section, key, fallback=False)
    config.set(section, key, str(not current).lower())
    save_config(config)


def _set_setting(section: str, key: str, value: str) -> None:
    """Set a string setting in config and save."""
    config = load_config()
    if not config.has_section(section):
        config.add_section(section)
    config.set(section, key, value)
    save_config(config)


# --- v2.1.0 list-menu rendering (paginated, within Telegram limits) ---

TG_MSG_LIMIT = 4096          # Telegram message text hard limit
TG_MAX_KB_ROWS = 100         # Telegram inline-keyboard button/row limit
LIST_PAGE_SIZE = 8           # entries shown per page in list menus


def _paginate(items: list, page: int, page_size: int = LIST_PAGE_SIZE):
    """Return (page_items, clamped_page, total_pages, start_index)."""
    items = list(items)
    total_pages = max(1, (len(items) + page_size - 1) // page_size)
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 0
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    return items[start:start + page_size], page, total_pages, start


def _list_menu_text(title: str, items: list, page: int = 0,
                    page_size: int = LIST_PAGE_SIZE) -> str:
    """Render a list-menu header within the Telegram message length limit."""
    page_items, page, total_pages, start = _paginate(items, page, page_size)
    lines = [f"<b>{title}</b>", ""]
    if not items:
        lines.append("<i>—</i>")
    else:
        for offset, it in enumerate(page_items):
            lines.append(f"{start + offset + 1}. {str(it)[:60]}")
    lines.append("")
    lines.append(_t("page_label", page=page + 1, total=total_pages))
    return "\n".join(lines)[:TG_MSG_LIMIT]


def _list_menu_keyboard(items: list, remove_prefix: str, add_cb: str, add_label: str,
                        back_cb: str, page_cb_prefix: str, page: int = 0,
                        page_size: int = LIST_PAGE_SIZE) -> telebot.types.InlineKeyboardMarkup:
    """Build a paginated list-management keyboard within Telegram row limits.

    Entries are removed by absolute index (callback ``remove_prefix + index``); page
    navigation uses ``page_cb_prefix + page``.
    """
    page_items, page, total_pages, start = _paginate(items, page, page_size)
    kb = telebot.types.InlineKeyboardMarkup()
    for offset, it in enumerate(page_items):
        idx = start + offset
        kb.row(telebot.types.InlineKeyboardButton(
            f"\u274c {str(it)[:40]}", callback_data=f"{remove_prefix}{idx}"))
    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(telebot.types.InlineKeyboardButton(
                "\u25c0", callback_data=f"{page_cb_prefix}{page - 1}"))
        if page < total_pages - 1:
            nav.append(telebot.types.InlineKeyboardButton(
                "\u25b6", callback_data=f"{page_cb_prefix}{page + 1}"))
        if nav:
            kb.row(*nav)
    kb.row(telebot.types.InlineKeyboardButton(
        f"\u2795 {add_label}", callback_data=add_cb))
    kb.row(telebot.types.InlineKeyboardButton(
        f"\u25c0\ufe0f {_t('back')}", callback_data=back_cb))
    return kb


def _blacklist_keyboard() -> telebot.types.InlineKeyboardMarkup:
    """Build the blacklist management sub-menu keyboard."""
    config = load_config()
    users_str = config.get("Blacklist", "users", fallback="")
    users = [u.strip() for u in users_str.split(",") if u.strip()]

    kb = telebot.types.InlineKeyboardMarkup()
    for user in users:
        kb.row(
            telebot.types.InlineKeyboardButton(
                f"\u274c {user}",
                callback_data=AIChatCBT.REMOVE_BLACKLIST_PREFIX + user,
            )
        )
    kb.row(
        telebot.types.InlineKeyboardButton(
            f"\u2795 {_t('add_user')}", callback_data=AIChatCBT.ADD_BLACKLIST
        )
    )
    kb.row(
        telebot.types.InlineKeyboardButton(
            f"\u2b05\ufe0f {_t('back')}", callback_data=AIChatCBT.SETTINGS_MENU
        )
    )
    return kb


def _templates_keyboard() -> telebot.types.InlineKeyboardMarkup:
    """Build the templates management sub-menu keyboard."""
    config = load_config()
    templates = _get_templates(config)

    kb = telebot.types.InlineKeyboardMarkup()
    for name in templates:
        kb.row(
            telebot.types.InlineKeyboardButton(
                f"\u274c {name}",
                callback_data=AIChatCBT.REMOVE_TEMPLATE_PREFIX + name,
            )
        )
    kb.row(
        telebot.types.InlineKeyboardButton(
            f"\u2795 {_t('add_template')}", callback_data=AIChatCBT.ADD_TEMPLATE
        )
    )
    kb.row(
        telebot.types.InlineKeyboardButton(
            f"\u2b05\ufe0f {_t('back')}", callback_data=AIChatCBT.SETTINGS_MENU
        )
    )
    return kb


def _stopwords_keyboard() -> telebot.types.InlineKeyboardMarkup:
    """Build the stop-words management sub-menu keyboard."""
    config = load_config()
    words_str = config.get("StopWords", "words", fallback="")
    words = [w.strip() for w in words_str.split(",") if w.strip()]

    kb = telebot.types.InlineKeyboardMarkup()
    for word in words:
        kb.row(
            telebot.types.InlineKeyboardButton(
                f"\u274c {word}",
                callback_data=AIChatCBT.REMOVE_STOPWORD_PREFIX + word,
            )
        )
    kb.row(
        telebot.types.InlineKeyboardButton(
            f"\u2795 {_t('add_stopword')}", callback_data=AIChatCBT.ADD_STOPWORD
        )
    )
    kb.row(
        telebot.types.InlineKeyboardButton(
            f"\U0001f6e1 {_t('add_complaint_stopwords')}",
            callback_data=AIChatCBT.ADD_COMPLAINT_STOPWORDS,
        )
    )
    kb.row(
        telebot.types.InlineKeyboardButton(
            f"\u2b05\ufe0f {_t('back')}", callback_data=AIChatCBT.SETTINGS_MENU
        )
    )
    return kb


def _promos_keyboard() -> telebot.types.InlineKeyboardMarkup:
    """Build the promos management sub-menu keyboard."""
    promos = _get_promo_codes(load_config())

    kb = telebot.types.InlineKeyboardMarkup()
    for p in promos:
        label = p["code"]
        if p["description"]:
            label += f": {p['description'][:20]}"
        kb.row(
            telebot.types.InlineKeyboardButton(
                f"\u274c {label}",
                callback_data=AIChatCBT.REMOVE_PROMO_PREFIX + p["code"],
            )
        )
    kb.row(
        telebot.types.InlineKeyboardButton(
            f"\u2795 {_t('add_promo')}", callback_data=AIChatCBT.ADD_PROMO
        )
    )
    kb.row(
        telebot.types.InlineKeyboardButton(
            f"\u2b05\ufe0f {_t('back')}", callback_data=AIChatCBT.SETTINGS_MENU
        )
    )
    return kb


# --- Telegram Handler Registration ---

_safe_edit_bot = None
_cardinal_ref = None  # Set in _pre_init for sync operations

def _safe_edit_message_text(*args, **kwargs):
    """Wrapper around bot.edit_message_text that swallows the harmless
    Telegram 'message is not modified' error.

    Telegram answers 400 'message is not modified' whenever the new
    text and reply_markup are byte-for-byte equal to the current
    ones. For idempotent settings menus (where the same handler can
    re-render after a no-op toggle) this is a normal situation, not
    a bug — we DEBUG-log it and return None.

    Any other exception is re-raised so callers and the global
    Telegram handler still see real failures.
    """
    try:
        return _safe_edit_bot.edit_message_text(*args, **kwargs)
    except Exception as _edit_ex:
        if "not modified" in str(_edit_ex).lower():
            logger.debug(
                "ai_chat_plugin: edit_message_text noop "
                "(message not modified)")
            return None
        raise

def init(c: Cardinal) -> None:
    """Initialize Telegram bot handlers for the settings interface."""
    global _safe_edit_bot
    bot = c.telegram.bot
    _safe_edit_bot = bot

    # --- One-shot config migration: rewrite stale English holding_message
    # saved by older plugin versions to the new RU default. Safe to run on
    # every startup — only touches the file if the value matches the old EN.
    try:
        cfg_path = Path("configs") / "ai_chat_plugin.cfg"
        if not cfg_path.exists():
            cfg_path = Path("storage") / "ai_chat_plugin.cfg"
        if cfg_path.exists():
            reset_holding_message_to_default(cfg_path)
    except Exception as e:
        logger.debug(f"{LOGGER_PREFIX} holding_message migration skipped: {e}")

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.SETTINGS_MENU)
    def _open_settings_menu(call: telebot.types.CallbackQuery) -> None:
        """Open the main settings menu."""
        _safe_edit_message_text(
            _format_current_settings(),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=_settings_keyboard(),
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.TOGGLE_ENABLED)
    def _toggle_enabled(call: telebot.types.CallbackQuery) -> None:
        _toggle_setting("General", "enabled")
        now_on = load_config().getboolean("General", "enabled", fallback=False)
        _safe_edit_message_text(
            _format_current_settings(),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=_settings_keyboard(),
            parse_mode="HTML",
        )
        bot.answer_callback_query(
            call.id, _t("plugin_on") if now_on else _t("plugin_off"))

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.TOGGLE_NOTIFY_AD)
    def _toggle_notify_ad(call: telebot.types.CallbackQuery) -> None:
        _toggle_setting("AutoDelivery", "notify_auto_delivery")
        _safe_edit_message_text(
            _format_responder_settings(),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=_responder_keyboard(),
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id, _t("setting_changed"))

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.TOGGLE_FORWARD)
    def _toggle_forward(call: telebot.types.CallbackQuery) -> None:
        _toggle_setting("Forwarding", "forward_non_standard")
        _safe_edit_message_text(
            _format_responder_settings(),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=_responder_keyboard(),
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id, _t("setting_changed"))

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.TOGGLE_NOTIFY_STANDARD)
    def _toggle_notify_standard(call: telebot.types.CallbackQuery) -> None:
        _toggle_setting("Forwarding", "notify_on_standard")
        _safe_edit_message_text(
            _format_responder_settings(),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=_responder_keyboard(),
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id, _t("setting_changed"))

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.TOGGLE_LIST_PRODUCTS)
    def _toggle_list_products(call: telebot.types.CallbackQuery) -> None:
        _toggle_setting("General", "list_products_on_clarify")
        _safe_edit_message_text(
            _format_responder_settings(),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=_responder_keyboard(),
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id, _t("setting_changed"))

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.TOGGLE_ANTISPAM)
    def _toggle_antispam(call: telebot.types.CallbackQuery) -> None:
        _toggle_setting("AntiSpam", "enabled")
        _safe_edit_message_text(
            _format_moderation_settings(),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=_moderation_keyboard(),
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id, _t("setting_changed"))

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.TOGGLE_WORKING_HOURS)
    def _toggle_working_hours(call: telebot.types.CallbackQuery) -> None:
        _toggle_setting("WorkingHours", "enabled")
        _safe_edit_message_text(
            _format_moderation_settings(),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=_moderation_keyboard(),
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id, _t("setting_changed"))

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.TOGGLE_BLACKLIST)
    def _toggle_blacklist(call: telebot.types.CallbackQuery) -> None:
        _toggle_setting("Blacklist", "enabled")
        _safe_edit_message_text(
            _format_moderation_settings(),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=_moderation_keyboard(),
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id, _t("setting_changed"))

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.TOGGLE_STOPWORDS)
    def _toggle_stopwords(call: telebot.types.CallbackQuery) -> None:
        _toggle_setting("StopWords", "enabled")
        _safe_edit_message_text(
            _format_moderation_settings(),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=_moderation_keyboard(),
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id, _t("setting_changed"))

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.TOGGLE_UPSELL)
    def _toggle_upsell(call: telebot.types.CallbackQuery) -> None:
        _toggle_setting("Upsell", "enabled")
        _safe_edit_message_text(
            _format_sales_settings(),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=_sales_keyboard(),
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id, _t("setting_changed"))

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.TOGGLE_PROMOS)
    def _toggle_promos(call: telebot.types.CallbackQuery) -> None:
        _toggle_setting("Promos", "enabled")
        _safe_edit_message_text(
            _format_sales_settings(),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=_sales_keyboard(),
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id, _t("setting_changed"))

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.TOGGLE_LANGUAGE_DETECT)
    def _toggle_language_detect(call: telebot.types.CallbackQuery) -> None:
        _toggle_setting("LanguageDetect", "enabled")
        _safe_edit_message_text(
            _format_responder_settings(),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=_responder_keyboard(),
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id, _t("setting_changed"))

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.TOGGLE_BUYER_CONTEXT)
    def _toggle_buyer_context(call: telebot.types.CallbackQuery) -> None:
        _toggle_setting("BuyerContext", "enabled")
        _safe_edit_message_text(
            _format_responder_settings(),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=_responder_keyboard(),
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id, _t("setting_changed"))

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.TOGGLE_AUTO_LEARNING)
    def _toggle_auto_learning(call: telebot.types.CallbackQuery) -> None:
        _toggle_setting("AutoLearning", "enabled")
        _safe_edit_message_text(
            _format_responder_settings(),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=_responder_keyboard(),
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id, _t("setting_changed"))

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.MANAGE_LEARNED)
    def _manage_learned(call: telebot.types.CallbackQuery) -> None:
        data = _load_learned_responses()
        if not data:
            bot.answer_callback_query(call.id, _t("learned_empty"), show_alert=True)
            return
        kb = telebot.types.InlineKeyboardMarkup()
        for i, item in enumerate(data[-10:]):
            q = item.get("buyer_question", "")[:40]
            kb.row(telebot.types.InlineKeyboardButton(
                f"\u274c {q}...",
                callback_data=f"{AIChatCBT.DELETE_LEARNED_PREFIX}{i}",
            ))
        kb.row(telebot.types.InlineKeyboardButton(
            f"\u25c0\ufe0f {_t('back')}", callback_data=AIChatCBT.CATEGORY_RESPONDER))
        _safe_edit_message_text(
            f"<b>\U0001f4da {_t('manage_learned')}</b>\n\n"
            f"Total: {len(data)} entries",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=kb,
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(
        func=lambda call: call.data.startswith(AIChatCBT.DELETE_LEARNED_PREFIX)
    )
    def _delete_learned(call: telebot.types.CallbackQuery) -> None:
        idx_str = call.data[len(AIChatCBT.DELETE_LEARNED_PREFIX):]
        try:
            idx = int(idx_str)
        except ValueError:
            bot.answer_callback_query(call.id)
            return
        data = _load_learned_responses()
        # Index is relative to last 10 shown
        actual_start = max(0, len(data) - 10)
        actual_idx = actual_start + idx
        if 0 <= actual_idx < len(data):
            data.pop(actual_idx)
            _save_learned_responses(data)
        bot.answer_callback_query(call.id, _t("learned_deleted"))
        # Refresh the list
        if not data:
            _safe_edit_message_text(
                f"<b>\U0001f4da {_t('manage_learned')}</b>\n\n{_t('learned_empty')}",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=telebot.types.InlineKeyboardMarkup().row(
                    telebot.types.InlineKeyboardButton(
                        f"\u25c0\ufe0f {_t('back')}", callback_data=AIChatCBT.CATEGORY_RESPONDER)
                ),
                parse_mode="HTML",
            )
        else:
            kb = telebot.types.InlineKeyboardMarkup()
            for i, item in enumerate(data[-10:]):
                q = item.get("buyer_question", "")[:40]
                kb.row(telebot.types.InlineKeyboardButton(
                    f"\u274c {q}...",
                    callback_data=f"{AIChatCBT.DELETE_LEARNED_PREFIX}{i}",
                ))
            kb.row(telebot.types.InlineKeyboardButton(
                f"\u25c0\ufe0f {_t('back')}", callback_data=AIChatCBT.CATEGORY_RESPONDER))
            _safe_edit_message_text(
                f"<b>\U0001f4da {_t('manage_learned')}</b>\n\n"
                f"Total: {len(data)} entries",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=kb,
                parse_mode="HTML",
            )

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.TOGGLE_TEMPLATES)
    def _toggle_templates(call: telebot.types.CallbackQuery) -> None:
        _toggle_setting("Templates", "enabled")
        _safe_edit_message_text(
            _format_sales_settings(),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=_sales_keyboard(),
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id, _t("setting_changed"))

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.TOGGLE_STATISTICS)
    def _toggle_statistics(call: telebot.types.CallbackQuery) -> None:
        _toggle_setting("Statistics", "enabled")
        _safe_edit_message_text(
            _format_stats_settings(),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=_stats_keyboard(),
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id, _t("setting_changed"))

    # --- Language toggle handler ---

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.TOGGLE_LANGUAGE)
    def _toggle_language(call: telebot.types.CallbackQuery) -> None:
        global _current_lang
        config = load_config()
        current_lang = config.get("General", "language", fallback="ru")
        new_lang = "en" if current_lang == "ru" else "ru"
        _set_setting("General", "language", new_lang)
        _current_lang = new_lang
        _safe_edit_message_text(
            _format_current_settings(),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=_settings_keyboard(),
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id, _t("setting_changed"))

    # --- Provider selection handlers ---

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.EDIT_PROVIDER)
    def _edit_provider(call: telebot.types.CallbackQuery) -> None:
        """Show provider selection keyboard (built-in + custom providers)."""
        config = load_config()
        current_provider = get_provider(config)
        custom_providers = _load_custom_providers()
        kb = telebot.types.InlineKeyboardMarkup()
        for prov_key in PROVIDERS:
            icon = "\u2705 " if prov_key == current_provider else ""
            label = _t(f"provider_{prov_key}")
            kb.row(telebot.types.InlineKeyboardButton(
                f"{icon}{label}",
                callback_data=f"{AIChatCBT.SELECT_PROVIDER}:{prov_key}",
            ))
        for prov_key in custom_providers:
            icon = "\u2705 " if prov_key == current_provider else ""
            kb.row(telebot.types.InlineKeyboardButton(
                f"{icon}\U0001f527 {prov_key}",
                callback_data=f"{AIChatCBT.SELECT_PROVIDER}:{prov_key}",
            ))
        kb.row(telebot.types.InlineKeyboardButton(
            f"\u2795 {_t('cprovider_add')}", callback_data=AIChatCBT.ADD_CPROVIDER))
        kb.row(telebot.types.InlineKeyboardButton(
            f"\U0001f5d1 {_t('cprovider_manage')}", callback_data=AIChatCBT.MANAGE_CPROVIDERS))
        kb.row(telebot.types.InlineKeyboardButton(
            f"\u25c0\ufe0f {_t('back')}", callback_data=AIChatCBT.CATEGORY_CORE))
        custom_note = (
            f"\n\n\U0001f527 {_t('cprovider_note', count=len(custom_providers))}"
            if custom_providers else ""
        )
        _safe_edit_message_text(
            f"<b>{_t('select_provider')}</b>{custom_note}",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=kb,
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(
        func=lambda call: call.data.startswith(f"{AIChatCBT.SELECT_PROVIDER}:")
    )
    def _select_provider(call: telebot.types.CallbackQuery) -> None:
        """Save selected provider to config."""
        provider_name = call.data[len(f"{AIChatCBT.SELECT_PROVIDER}:"):]
        if provider_name in PROVIDERS or provider_name in _load_custom_providers():
            _set_setting("General", "provider", provider_name)
            logger.info(f"{LOGGER_PREFIX} провайдер изменён на {provider_name}")
        else:
            bot.answer_callback_query(call.id, _t("unknown_provider"), show_alert=True)
            return
        _safe_edit_message_text(
            _format_core_settings(),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=_core_keyboard(),
            parse_mode="HTML",
        )
        model_hints = {
            "openrouter": "openai/gpt-3.5-turbo, z-ai/glm-5.2:free",
            "openai": "gpt-4o, gpt-4o-mini, gpt-3.5-turbo",
            "gemini": "gemini-1.5-flash, gemini-1.5-pro",
            "deepseek": "deepseek-chat, deepseek-coder",
            "anthropic": "claude-3-sonnet-20240229, claude-3-haiku-20240307",
        }
        hint = model_hints.get(provider_name, "")
        if not hint:
            saved_models = get_custom_provider_models(provider_name)
            hint = ", ".join(saved_models[:3]) if saved_models else "-"
        alert_text = (
            f"{_t('provider_changed_to')} {provider_name}.\n"
            f"{_t('provider_set_model_hint')}\n"
            f"{_t('provider_examples')}: {hint}"
        )
        bot.answer_callback_query(call.id, alert_text, show_alert=True)

    # --- Custom providers management ---

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.MANAGE_CPROVIDERS)
    def _manage_cproviders(call: telebot.types.CallbackQuery) -> None:
        """List custom providers with delete buttons and an add button."""
        custom_providers = _load_custom_providers()
        current_provider = get_provider(load_config())
        kb = telebot.types.InlineKeyboardMarkup()
        for prov_key in custom_providers:
            models_n = len(get_custom_provider_models(prov_key))
            mark = " \u2b50" if prov_key == current_provider else ""
            kb.row(telebot.types.InlineKeyboardButton(
                f"\u274c {prov_key} ({models_n}){mark}",
                callback_data=f"{AIChatCBT.REMOVE_CPROVIDER_PREFIX}{prov_key}",
            ))
        kb.row(telebot.types.InlineKeyboardButton(
            f"\u2795 {_t('cprovider_add')}", callback_data=AIChatCBT.ADD_CPROVIDER))
        kb.row(telebot.types.InlineKeyboardButton(
            f"\u25c0\ufe0f {_t('back')}", callback_data=AIChatCBT.EDIT_PROVIDER))
        text = (
            f"<b>{_t('cprovider_manage')}</b>\n\n"
            + (_t("cprovider_delete_hint") if custom_providers else _t("no_custom_providers"))
        )
        _safe_edit_message_text(
            text, call.message.chat.id, call.message.message_id,
            reply_markup=kb, parse_mode="HTML",
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(
        func=lambda call: call.data.startswith(AIChatCBT.REMOVE_CPROVIDER_PREFIX)
    )
    def _remove_cprovider(call: telebot.types.CallbackQuery) -> None:
        """Delete a custom provider; reset to openrouter if it was active."""
        prov_key = call.data[len(AIChatCBT.REMOVE_CPROVIDER_PREFIX):]
        providers = _load_custom_providers()
        if prov_key in providers:
            del providers[prov_key]
            _save_custom_providers(providers)
            logger.info(f"{LOGGER_PREFIX} кастомный провайдер удалён: {prov_key}")
            if get_provider(load_config()) == prov_key:
                _set_setting("General", "provider", "openrouter")
                logger.info(f"{LOGGER_PREFIX} активный провайдер сброшен на openrouter")
            bot.answer_callback_query(call.id, _t("cprovider_removed", name=prov_key))
        else:
            bot.answer_callback_query(call.id)
        _manage_cproviders(call)

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.ADD_CPROVIDER)
    def _add_cprovider(call: telebot.types.CallbackQuery) -> None:
        """Start the add-custom-provider wizard: step 1 — name."""
        _set_pending_input(call.from_user.id, ("add_cprovider_name",))
        bot.send_message(
            call.message.chat.id,
            _t("enter_cprovider_name"),
            parse_mode="HTML",
            reply_markup=telebot.types.ForceReply(selective=True),
        )
        bot.answer_callback_query(call.id)

    # --- Prompt preset selection handlers ---

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.EDIT_PROMPT_PRESET)
    def _edit_prompt_preset(call: telebot.types.CallbackQuery) -> None:
        """Show prompt preset selection keyboard with checkboxes."""
        config = load_config()
        active_presets = get_prompt_presets(config)
        kb = telebot.types.InlineKeyboardMarkup()
        for preset_key in PRESET_PROMPTS:
            icon = "\u2705" if preset_key in active_presets else "\u2b1c"
            label = _t(f"preset_{preset_key}")
            kb.row(telebot.types.InlineKeyboardButton(
                f"{icon} {label}",
                callback_data=f"{AIChatCBT.SELECT_PRESET}:{preset_key}",
            ))
        # Add custom presets
        custom_presets = _load_custom_presets()
        for preset_key in custom_presets:
            if preset_key not in PRESET_PROMPTS:
                icon = "\u2705" if preset_key in active_presets else "\u2b1c"
                kb.row(telebot.types.InlineKeyboardButton(
                    f"{icon} \U0001f464 {preset_key}",
                    callback_data=f"{AIChatCBT.SELECT_PRESET}:{preset_key}",
                ))
        kb.row(telebot.types.InlineKeyboardButton(
            f"\u25c0\ufe0f {_t('back')}", callback_data=AIChatCBT.CATEGORY_RESPONDER))

        active_text = ", ".join(active_presets) if active_presets else "\u043d\u0435\u0442"
        _safe_edit_message_text(
            f"<b>{_t('select_preset')}</b>\n\n"
            f"\u0410\u043a\u0442\u0438\u0432\u043d\u044b\u0435: <code>{active_text}</code>\n"
            f"\u0412\u044b\u0431\u0440\u0430\u043d\u043e: {len(active_presets)}\n\n"
            f"\u041d\u0430\u0436\u043c\u0438\u0442\u0435 \u0434\u043b\u044f \u0432\u043a\u043b\u044e\u0447\u0435\u043d\u0438\u044f/\u043e\u0442\u043a\u043b\u044e\u0447\u0435\u043d\u0438\u044f:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=kb,
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(
        func=lambda call: call.data.startswith(f"{AIChatCBT.SELECT_PRESET}:")
    )
    def _select_preset(call: telebot.types.CallbackQuery) -> None:
        """Toggle a preset on/off in the active list."""
        preset_name = call.data[len(f"{AIChatCBT.SELECT_PRESET}:"):]
        custom_presets = _load_custom_presets()
        # Validate preset exists
        if preset_name not in PRESET_PROMPTS and preset_name not in custom_presets:
            bot.answer_callback_query(call.id, "\u041f\u0440\u0435\u0441\u0435\u0442 \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d.", show_alert=True)
            return

        config = load_config()
        active_presets = get_prompt_presets(config)

        if preset_name in active_presets:
            # Remove it (but keep at least one or allow empty -> falls back to custom prompt)
            active_presets.remove(preset_name)
            if not active_presets:
                active_presets = ["custom"]
        else:
            # Add it, remove "custom" if it was the only one and we're adding a real preset
            if active_presets == ["custom"] and preset_name != "custom":
                active_presets = [preset_name]
            else:
                if "custom" in active_presets and preset_name != "custom":
                    active_presets.remove("custom")
                active_presets.append(preset_name)

        # Save as comma-separated
        _set_setting("General", "prompt_preset", ",".join(active_presets))

        # Re-render the selection menu
        _edit_prompt_preset(call)

    # --- Download prompt as file handler ---

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.DOWNLOAD_PROMPT)
    def _download_prompt(call: telebot.types.CallbackQuery) -> None:
        """Send the full effective system prompt as a .txt file."""
        config = load_config()
        prompt_text = _get_effective_prompt(config)
        presets = get_prompt_presets(config)

        if not prompt_text or prompt_text.strip() == "":
            bot.answer_callback_query(call.id, "\u041f\u0440\u043e\u043c\u043f\u0442 \u043f\u0443\u0441\u0442.", show_alert=True)
            return

        import io
        file_content = io.BytesIO(prompt_text.encode("utf-8"))
        file_content.name = f"system_prompt_{'_'.join(presets)}.txt"

        try:
            bot.send_document(
                call.message.chat.id,
                file_content,
                caption=f"\U0001f4dd \u0421\u0438\u0441\u0442\u0435\u043c\u043d\u044b\u0439 \u043f\u0440\u043e\u043c\u043f\u0442 ({len(presets)} \u043f\u0440\u0435\u0441\u0435\u0442(\u043e\u0432): {', '.join(presets)})\n\u0414\u043b\u0438\u043d\u0430: {len(prompt_text)} \u0441\u0438\u043c\u0432\u043e\u043b\u043e\u0432",
            )
        except Exception as e:
            bot.answer_callback_query(call.id, f"\u041e\u0448\u0438\u0431\u043a\u0430: {e}", show_alert=True)
            return
        bot.answer_callback_query(call.id)

    # --- Preset management handlers ---

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.MANAGE_PRESETS)
    def _manage_presets(call: telebot.types.CallbackQuery) -> None:
        """Show preset management menu."""
        custom_presets = _load_custom_presets()
        builtin_keys = [k for k in PRESET_PROMPTS if k != "custom"]

        lines = ["<b>\U0001f4cb \u0423\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0438\u0435 \u043f\u0440\u0435\u0441\u0435\u0442\u0430\u043c\u0438</b>\n"]
        lines.append("<b>\u0412\u0441\u0442\u0440\u043e\u0435\u043d\u043d\u044b\u0435:</b>")
        for k in builtin_keys:
            label = _t(f"preset_{k}")
            lines.append(f"  \u2022 {label}")
        if custom_presets:
            lines.append("\n<b>\u041f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044c\u0441\u043a\u0438\u0435:</b>")
            for k in custom_presets:
                lines.append(f"  \u2022 {k} ({len(custom_presets[k])} \u0441\u0438\u043c\u0432.)")
        else:
            lines.append("\n<i>\u041f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044c\u0441\u043a\u0438\u0445 \u043f\u0440\u0435\u0441\u0435\u0442\u043e\u0432 \u043d\u0435\u0442.</i>")

        kb = telebot.types.InlineKeyboardMarkup()
        # Edit buttons for built-in presets
        for k in builtin_keys:
            label = _t(f"preset_{k}")
            kb.row(telebot.types.InlineKeyboardButton(
                f"\u270f\ufe0f {label}",
                callback_data=f"{AIChatCBT.EDIT_PRESET_PREFIX}{k}"))
        # Edit + delete buttons for custom presets
        for k in custom_presets:
            kb.row(
                telebot.types.InlineKeyboardButton(
                    f"\u270f\ufe0f {k}", callback_data=f"{AIChatCBT.EDIT_PRESET_PREFIX}{k}"),
                telebot.types.InlineKeyboardButton(
                    "\U0001f5d1", callback_data=f"{AIChatCBT.DELETE_PRESET_PREFIX}{k}"),
            )
        kb.row(telebot.types.InlineKeyboardButton(
            "\u2795 \u0421\u043e\u0437\u0434\u0430\u0442\u044c \u043f\u0440\u0435\u0441\u0435\u0442", callback_data=AIChatCBT.CREATE_PRESET))
        kb.row(telebot.types.InlineKeyboardButton(
            f"\u25c0\ufe0f {_t('back')}", callback_data=AIChatCBT.CATEGORY_RESPONDER))

        _safe_edit_message_text(
            "\n".join(lines),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=kb,
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.CREATE_PRESET)
    def _create_preset(call: telebot.types.CallbackQuery) -> None:
        """Ask user for new preset name."""
        _set_pending_input(call.from_user.id, ("create_preset_name",))
        bot.send_message(
            call.message.chat.id,
            "\u2795 <b>\u0421\u043e\u0437\u0434\u0430\u043d\u0438\u0435 \u043d\u043e\u0432\u043e\u0433\u043e \u043f\u0440\u0435\u0441\u0435\u0442\u0430</b>\n\n"
            "\u0412\u0432\u0435\u0434\u0438\u0442\u0435 \u043d\u0430\u0437\u0432\u0430\u043d\u0438\u0435 \u043f\u0440\u0435\u0441\u0435\u0442\u0430 (\u043b\u0430\u0442\u0438\u043d\u0438\u0446\u0430, \u0446\u0438\u0444\u0440\u044b, \u043f\u043e\u0434\u0447\u0451\u0440\u043a\u0438\u0432\u0430\u043d\u0438\u0435).\n"
            "\u041d\u0430\u043f\u0440\u0438\u043c\u0435\u0440: <code>my_shop</code>, <code>rental_v2</code>",
            parse_mode="HTML",
            reply_markup=telebot.types.ForceReply(selective=True),
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(
        func=lambda call: call.data.startswith(AIChatCBT.EDIT_PRESET_PREFIX))
    def _edit_preset(call: telebot.types.CallbackQuery) -> None:
        """Ask user for new text for a preset."""
        preset_key = call.data[len(AIChatCBT.EDIT_PRESET_PREFIX):]
        _set_pending_input(call.from_user.id, ("edit_preset_text", preset_key))

        # Show current text
        current_text = ""
        custom_presets = _load_custom_presets()
        if preset_key in custom_presets:
            current_text = custom_presets[preset_key]
        elif preset_key in PRESET_PROMPTS and PRESET_PROMPTS[preset_key] is not None:
            current_text = PRESET_PROMPTS[preset_key]

        preview = current_text[:200] + "..." if len(current_text) > 200 else current_text

        bot.send_message(
            call.message.chat.id,
            f"\u270f\ufe0f <b>\u0420\u0435\u0434\u0430\u043a\u0442\u0438\u0440\u043e\u0432\u0430\u043d\u0438\u0435 \u043f\u0440\u0435\u0441\u0435\u0442\u0430: {preset_key}</b>\n\n"
            f"\u0422\u0435\u043a\u0443\u0449\u0438\u0439 \u0442\u0435\u043a\u0441\u0442 ({len(current_text)} \u0441\u0438\u043c\u0432.):\n"
            f"<i>{preview}</i>\n\n"
            "\u041e\u0442\u043f\u0440\u0430\u0432\u044c\u0442\u0435 \u043d\u043e\u0432\u044b\u0439 \u0442\u0435\u043a\u0441\u0442 \u043f\u0440\u043e\u043c\u043f\u0442\u0430 \u0441\u043b\u0435\u0434\u0443\u044e\u0449\u0438\u043c \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0435\u043c.\n"
            "\u0414\u043b\u044f \u043e\u0442\u043c\u0435\u043d\u044b \u043e\u0442\u043f\u0440\u0430\u0432\u044c\u0442\u0435 <code>\u043e\u0442\u043c\u0435\u043d\u0430</code>.",
            parse_mode="HTML",
            reply_markup=telebot.types.ForceReply(selective=True),
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(
        func=lambda call: call.data.startswith(AIChatCBT.DELETE_PRESET_PREFIX))
    def _delete_preset(call: telebot.types.CallbackQuery) -> None:
        """Confirm deletion of a custom preset."""
        preset_key = call.data[len(AIChatCBT.DELETE_PRESET_PREFIX):]
        kb = telebot.types.InlineKeyboardMarkup()
        kb.row(
            telebot.types.InlineKeyboardButton(
                "\u2705 \u0414\u0430, \u0443\u0434\u0430\u043b\u0438\u0442\u044c", callback_data=f"{AIChatCBT.CONFIRM_DELETE_PREFIX}{preset_key}"),
            telebot.types.InlineKeyboardButton(
                "\u274c \u041e\u0442\u043c\u0435\u043d\u0430", callback_data=AIChatCBT.MANAGE_PRESETS),
        )
        _safe_edit_message_text(
            f"\U0001f5d1 \u0423\u0434\u0430\u043b\u0438\u0442\u044c \u043f\u0440\u0435\u0441\u0435\u0442 <b>{preset_key}</b>?",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=kb,
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(
        func=lambda call: call.data.startswith(AIChatCBT.CONFIRM_DELETE_PREFIX))
    def _confirm_delete_preset(call: telebot.types.CallbackQuery) -> None:
        """Actually delete a custom preset."""
        preset_key = call.data[len(AIChatCBT.CONFIRM_DELETE_PREFIX):]
        custom_presets = _load_custom_presets()
        if preset_key in custom_presets:
            del custom_presets[preset_key]
            _save_custom_presets(custom_presets)
            # If this was the active preset, switch to custom
            config = load_config()
            if get_prompt_preset(config) == preset_key:
                _set_setting("General", "prompt_preset", "custom")
            bot.answer_callback_query(call.id, f"\u041f\u0440\u0435\u0441\u0435\u0442 '{preset_key}' \u0443\u0434\u0430\u043b\u0451\u043d.", show_alert=True)
        else:
            bot.answer_callback_query(call.id, "\u041f\u0440\u0435\u0441\u0435\u0442 \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d.", show_alert=True)
        # Return to manage presets view
        _manage_presets(call)

    # --- Category navigation handlers ---

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.CATEGORY_CORE)
    def _open_core(call: telebot.types.CallbackQuery) -> None:
        _safe_edit_message_text(
            _format_core_settings(),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=_core_keyboard(),
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.CATEGORY_RESPONDER)
    def _open_responder(call: telebot.types.CallbackQuery) -> None:
        _safe_edit_message_text(
            _format_responder_settings(),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=_responder_keyboard(),
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.CATEGORY_MODERATION)
    def _open_moderation(call: telebot.types.CallbackQuery) -> None:
        _safe_edit_message_text(
            _format_moderation_settings(),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=_moderation_keyboard(),
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.CATEGORY_SALES)
    def _open_sales(call: telebot.types.CallbackQuery) -> None:
        _safe_edit_message_text(
            _format_sales_settings(),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=_sales_keyboard(),
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.CATEGORY_STATS)
    def _open_stats(call: telebot.types.CallbackQuery) -> None:
        _safe_edit_message_text(
            _format_stats_settings(),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=_stats_keyboard(),
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.BACK_TO_MAIN)
    def _back_to_main(call: telebot.types.CallbackQuery) -> None:
        _safe_edit_message_text(
            _format_current_settings(),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=_settings_keyboard(),
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id)

    # --- Management sub-menu handlers ---

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.MANAGE_BLACKLIST)
    def _manage_blacklist(call: telebot.types.CallbackQuery) -> None:
        _safe_edit_message_text(
            f"<b>{_t('blacklist_mgmt')}</b>",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=_blacklist_keyboard(),
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.ADD_BLACKLIST)
    def _add_blacklist(call: telebot.types.CallbackQuery) -> None:
        _set_pending_input(call.from_user.id, "add_blacklist_user")
        bot.send_message(
            call.message.chat.id,
            _t("enter_blacklist_user"),
            reply_markup=telebot.types.ForceReply(selective=True),
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(
        func=lambda call: call.data.startswith(AIChatCBT.REMOVE_BLACKLIST_PREFIX)
    )
    def _remove_blacklist(call: telebot.types.CallbackQuery) -> None:
        user = call.data[len(AIChatCBT.REMOVE_BLACKLIST_PREFIX):]
        config = load_config()
        users_str = config.get("Blacklist", "users", fallback="")
        users = [u.strip() for u in users_str.split(",") if u.strip()]
        users = [u for u in users if u.lower() != user.lower()]
        _set_setting("Blacklist", "users", ", ".join(users))
        _safe_edit_message_text(
            f"<b>{_t('blacklist_mgmt')}</b>",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=_blacklist_keyboard(),
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id, _t("removed", item=user))

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.MANAGE_TEMPLATES)
    def _manage_templates(call: telebot.types.CallbackQuery) -> None:
        _safe_edit_message_text(
            f"<b>{_t('templates_mgmt')}</b>",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=_templates_keyboard(),
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.ADD_TEMPLATE)
    def _add_template(call: telebot.types.CallbackQuery) -> None:
        _set_pending_input(call.from_user.id, "add_template_name")
        bot.send_message(
            call.message.chat.id,
            _t("enter_template_name"),
            reply_markup=telebot.types.ForceReply(selective=True),
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(
        func=lambda call: call.data.startswith(AIChatCBT.REMOVE_TEMPLATE_PREFIX)
    )
    def _remove_template(call: telebot.types.CallbackQuery) -> None:
        name = call.data[len(AIChatCBT.REMOVE_TEMPLATE_PREFIX):]
        config = load_config()
        key = f"template_{name}"
        if config.has_option("Templates", key):
            config.remove_option("Templates", key)
            save_config(config)
        _safe_edit_message_text(
            f"<b>{_t('templates_mgmt')}</b>",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=_templates_keyboard(),
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id, _t("removed", item=name))

    # --- Product Catalog handlers ---

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.MANAGE_PRODUCTS)
    def _manage_products(call: telebot.types.CallbackQuery) -> None:
        config = load_config()
        products = _get_product_catalog(config)
        kb = telebot.types.InlineKeyboardMarkup()
        kb.row(telebot.types.InlineKeyboardButton(
            f"🔄 {_t('sync_products')}", callback_data=AIChatCBT.SYNC_PRODUCTS))
        for p in products:
            kb.row(telebot.types.InlineKeyboardButton(
                f"❌ {p['title'][:40]}",
                callback_data=f"{AIChatCBT.REMOVE_PRODUCT_PREFIX}{p['title']}"))
        kb.row(telebot.types.InlineKeyboardButton(
            f"➕ {_t('add_product')}", callback_data=AIChatCBT.ADD_PRODUCT))
        kb.row(telebot.types.InlineKeyboardButton(
            f"\u25c0\ufe0f {_t('back')}", callback_data=AIChatCBT.CATEGORY_SALES))
        # Show auto-loaded lot count
        auto_count = len(_lot_details_cache) if _lot_details_cache else 0
        text = f"<b>{_t('product_catalog_mgmt')}</b>\n\n"
        if auto_count > 0:
            text += f"📥 Авто-загрузка: <b>{auto_count}</b> лотов с FunPay\n"
        if products:
            text += f"\n📦 <b>Ручные товары ({len(products)}):</b>\n"
            for p in products:
                price_str = f" — {p['price']}" if p.get('price') else ""
                text += f"  • <b>{p['title']}</b>{price_str}\n"
                if p.get('description'):
                    text += f"    <i>{p['description'][:80]}</i>\n"
        else:
            text += f"\n{_t('product_empty')}"
        _safe_edit_message_text(text, call.message.chat.id,
            call.message.message_id, reply_markup=kb, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.ADD_PRODUCT)
    def _add_product(call: telebot.types.CallbackQuery) -> None:
        _set_pending_input(call.from_user.id, "add_product_name")
        bot.send_message(
            call.message.chat.id,
            _t("enter_product_name"),
            reply_markup=telebot.types.ForceReply(selective=True),
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(
        func=lambda call: call.data.startswith(AIChatCBT.REMOVE_PRODUCT_PREFIX)
    )
    def _remove_product(call: telebot.types.CallbackQuery) -> None:
        name = call.data[len(AIChatCBT.REMOVE_PRODUCT_PREFIX):]
        _remove_product_from_catalog(name)
        _manage_products(call)
        bot.answer_callback_query(call.id, _t("product_removed", name=name))

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.SYNC_PRODUCTS)
    def _sync_products(call: telebot.types.CallbackQuery) -> None:
        """Manually sync products from Cardinal (force refresh lot cache)."""
        global _lot_details_cache, _lot_details_cache_time
        bot.answer_callback_query(call.id, _t("syncing_products"))
        # Clear cache to force reload
        _lot_details_cache = []
        _lot_details_cache_time = 0.0
        _lot_pages_cache.clear()
        _lot_pages_cache_time = 0.0
        if _cardinal_ref is not None:
            # Diagnostic: dump what account/profile has
            diag_lines = []
            prof = getattr(_cardinal_ref, "profile", None)
            diag_lines.append(f"profile: {type(prof).__name__ if prof else 'None'}")
            if prof and hasattr(prof, "get_lots"):
                try:
                    p_lots = prof.get_lots()
                    diag_lines.append(f"  profile.get_lots(): {len(p_lots) if p_lots else 0} шт.")
                    if p_lots:
                        first = p_lots[0]
                        diag_lines.append(f"  first lot: {type(first).__name__} title={getattr(first, 'title', '?')[:30]} price={getattr(first, 'price', '?')}")
                except Exception as e:
                    diag_lines.append(f"  profile.get_lots() ERROR: {e}")
            acc = getattr(_cardinal_ref, "account", None)
            diag_lines.append(f"account: {type(acc).__name__ if acc else 'None'}")
            if acc:
                diag_lines.append(f"  has get_lots: {hasattr(acc, 'get_lots')}")
                diag_lines.append(f"  has lots attr: {hasattr(acc, 'lots')}")
                if hasattr(acc, "get_lots"):
                    try:
                        test_lots = acc.get_lots()
                        diag_lines.append(f"  account.get_lots(): {len(test_lots) if test_lots else 0} шт.")
                    except Exception as e:
                        diag_lines.append(f"  account.get_lots() ERROR: {e}")
            try:
                details = _get_lot_details(_cardinal_ref)
                diag_lines.append(f"\nresult: {len(details)} lots")
                if details:
                    for d in details[:5]:
                        diag_lines.append(f"  • {d.get('title', '?')[:50]} | {d.get('price', '?')}")
                result_text = "\n".join(diag_lines)
                if details:
                    _safe_edit_message_text(
                        f"✅ {_t('sync_products_done', n=len(details))}\n\n<pre>{result_text}</pre>",
                        call.message.chat.id, call.message.message_id,
                        reply_markup=telebot.types.InlineKeyboardMarkup().add(
                            telebot.types.InlineKeyboardButton(
                                f"\u25c0\ufe0f {_t('back')}",
                                callback_data=AIChatCBT.CATEGORY_SALES)),
                        parse_mode="HTML",
                    )
                else:
                    _safe_edit_message_text(
                        f"⚠️ {_t('sync_products_empty')}\n\n<pre>{result_text}</pre>",
                        call.message.chat.id, call.message.message_id,
                        reply_markup=telebot.types.InlineKeyboardMarkup().add(
                            telebot.types.InlineKeyboardButton(
                                f"\u25c0\ufe0f {_t('back')}",
                                callback_data=AIChatCBT.CATEGORY_SALES)),
                        parse_mode="HTML",
                    )
            except Exception as e:
                logger.error(f"{LOGGER_PREFIX} sync products failed: {e}", exc_info=True)
                diag_lines.append(f"\nEXCEPTION: {e}")
                _safe_edit_message_text(
                    f"❌ Ошибка:\n<pre>{'\n'.join(diag_lines)}</pre>",
                    call.message.chat.id, call.message.message_id,
                    parse_mode="HTML",
                )
        else:
            _safe_edit_message_text(
                "⚠️ Cardinal не инициализирован. Перезапустите бота.",
                call.message.chat.id, call.message.message_id,
                parse_mode="HTML",
            )

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.MANAGE_STOPWORDS)
    def _manage_stopwords(call: telebot.types.CallbackQuery) -> None:
        _safe_edit_message_text(
            f"<b>{_t('stopwords_mgmt')}</b>",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=_stopwords_keyboard(),
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.ADD_STOPWORD)
    def _add_stopword(call: telebot.types.CallbackQuery) -> None:
        _set_pending_input(call.from_user.id, "add_stopword")
        bot.send_message(
            call.message.chat.id,
            _t("enter_stopword"),
            reply_markup=telebot.types.ForceReply(selective=True),
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(
        func=lambda call: call.data == AIChatCBT.ADD_COMPLAINT_STOPWORDS
    )
    def _add_complaint_stopwords(call: telebot.types.CallbackQuery) -> None:
        """Одной кнопкой добавляет рекомендованный набор стоп-слов жалоб
        и включает фильтр StopWords, чтобы такие диалоги уходили продавцу."""
        config = load_config()
        words_str = config.get("StopWords", "words", fallback="")
        existing = [w.strip() for w in words_str.split(",") if w.strip()]
        existing_lower = {w.lower() for w in existing}
        added = 0
        for w in RECOMMENDED_COMPLAINT_STOPWORDS:
            if w.lower() not in existing_lower:
                existing.append(w)
                existing_lower.add(w.lower())
                added += 1
        _set_setting("StopWords", "words", ",".join(existing))
        # Включаем фильтр, если он был выключен — иначе слова не работают.
        if not config.getboolean("StopWords", "enabled", fallback=False):
            _set_setting("StopWords", "enabled", "true")
        _safe_edit_message_text(
            f"<b>{_t('stopwords_mgmt')}</b>",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=_stopwords_keyboard(),
            parse_mode="HTML",
        )
        bot.answer_callback_query(
            call.id, _t("complaint_stopwords_added", n=added), show_alert=True)

    @bot.callback_query_handler(
        func=lambda call: call.data.startswith(AIChatCBT.REMOVE_STOPWORD_PREFIX)
    )
    def _remove_stopword(call: telebot.types.CallbackQuery) -> None:
        word = call.data[len(AIChatCBT.REMOVE_STOPWORD_PREFIX):]
        config = load_config()
        words_str = config.get("StopWords", "words", fallback="")
        words = [w.strip() for w in words_str.split(",") if w.strip()]
        words = [w for w in words if w.lower() != word.lower()]
        _set_setting("StopWords", "words", ",".join(words))
        _safe_edit_message_text(
            f"<b>{_t('stopwords_mgmt')}</b>",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=_stopwords_keyboard(),
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id, _t("removed", item=word))

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.MANAGE_PROMOS)
    def _manage_promos(call: telebot.types.CallbackQuery) -> None:
        _safe_edit_message_text(
            f"<b>{_t('promos_mgmt')}</b>",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=_promos_keyboard(),
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.ADD_PROMO)
    def _add_promo(call: telebot.types.CallbackQuery) -> None:
        _set_pending_input(call.from_user.id, "add_promo_code")
        bot.send_message(
            call.message.chat.id,
            _t("enter_promo_code"),
            reply_markup=telebot.types.ForceReply(selective=True),
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(
        func=lambda call: call.data.startswith(AIChatCBT.REMOVE_PROMO_PREFIX)
    )
    def _remove_promo(call: telebot.types.CallbackQuery) -> None:
        code = call.data[len(AIChatCBT.REMOVE_PROMO_PREFIX):]
        config = load_config()
        codes_str = config.get("Promos", "codes", fallback="")
        lines = [e.strip() for e in codes_str.split("\n") if e.strip()]
        lines = [e for e in lines if not e.startswith(code + ":") and e != code]
        _set_setting("Promos", "codes", "\n".join(lines))
        _safe_edit_message_text(
            f"<b>{_t('promos_mgmt')}</b>",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=_promos_keyboard(),
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id, _t("removed", item=code))

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.MANAGE_UPSELL)
    def _manage_upsell(call: telebot.types.CallbackQuery) -> None:
        _set_pending_input(call.from_user.id, "upsell_prompt")
        config = load_config()
        current = config.get("Upsell", "prompt_addon", fallback="")
        bot.send_message(
            call.message.chat.id,
            _t("enter_upsell_prompt", current=current[:200]),
            parse_mode="HTML",
            reply_markup=telebot.types.ForceReply(selective=True),
        )
        bot.answer_callback_query(call.id)

    # --- Stats handlers ---

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.VIEW_STATS)
    def _view_stats(call: telebot.types.CallbackQuery) -> None:
        kb = telebot.types.InlineKeyboardMarkup()
        kb.row(
            telebot.types.InlineKeyboardButton(
                f"\U0001f5d1 {_t('reset_stats')}", callback_data=AIChatCBT.RESET_STATS
            )
        )
        kb.row(
            telebot.types.InlineKeyboardButton(
                f"\u2b05\ufe0f {_t('back')}", callback_data=AIChatCBT.SETTINGS_MENU
            )
        )
        _safe_edit_message_text(
            _get_stats_text(),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=kb,
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.RESET_STATS)
    def _reset_stats_handler(call: telebot.types.CallbackQuery) -> None:
        _reset_stats()
        kb = telebot.types.InlineKeyboardMarkup()
        kb.row(
            telebot.types.InlineKeyboardButton(
                f"\u2b05\ufe0f {_t('back')}", callback_data=AIChatCBT.SETTINGS_MENU
            )
        )
        _safe_edit_message_text(
            f"<b>{_t('stats_reset')}</b>",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=kb,
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id, _t("stats_reset"))

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.VIEW_LOGS)
    def _view_logs(call: telebot.types.CallbackQuery) -> None:
        entries = _get_log_entries(30)
        if not entries:
            text = f"<b>{_t('logs_title')}</b>\n\n{_t('logs_empty')}"
        else:
            text = f"<b>{_t('logs_title')}</b>\n\n" + "\n".join(entries)
        kb = telebot.types.InlineKeyboardMarkup()
        kb.row(telebot.types.InlineKeyboardButton(
            f"\U0001f504 {_t('view_logs')}", callback_data=AIChatCBT.VIEW_LOGS))
        kb.row(telebot.types.InlineKeyboardButton(
            f"\u2b05\ufe0f {_t('back')}", callback_data=AIChatCBT.SETTINGS_MENU))
        _safe_edit_message_text(
            text, call.message.chat.id, call.message.message_id,
            reply_markup=kb, parse_mode="HTML",
        )
        bot.answer_callback_query(call.id)

    # --- Edit buttons for existing settings ---

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.VIEW_SETTINGS)
    def _view_settings(call: telebot.types.CallbackQuery) -> None:
        _safe_edit_message_text(
            _format_current_settings(),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=_settings_keyboard(),
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.EDIT_MODEL)
    def _edit_model(call: telebot.types.CallbackQuery) -> None:
        """Show model preset menu for the active provider (+ manual input)."""
        config = load_config()
        provider = get_provider(config)
        current_model = get_model(config)
        entries = _model_menu_entries(provider)
        kb = telebot.types.InlineKeyboardMarkup()
        for idx, (_, label) in enumerate(entries):
            icon = "\u2705 " if entries[idx][0] == current_model else ""
            kb.row(telebot.types.InlineKeyboardButton(
                f"{icon}{label}", callback_data=f"{AIChatCBT.SELECT_MODEL_PRESET}:{idx}"))
        kb.row(telebot.types.InlineKeyboardButton(
            f"\u270d\ufe0f {_t('model_manual')}", callback_data=AIChatCBT.MODEL_MANUAL))
        kb.row(telebot.types.InlineKeyboardButton(
            f"\u25c0\ufe0f {_t('back')}", callback_data=AIChatCBT.CATEGORY_CORE))
        header = f"<b>{_t('select_model')}</b>\n\U0001f916 {_t('model_label')}: <code>{current_model}</code>"
        _safe_edit_message_text(
            header,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=kb,
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(
        func=lambda call: call.data.startswith(f"{AIChatCBT.SELECT_MODEL_PRESET}:")
    )
    def _select_model_preset(call: telebot.types.CallbackQuery) -> None:
        """Save a preset model, then auto-test it."""
        provider = get_provider(load_config())
        entries = _model_menu_entries(provider)
        try:
            idx = int(call.data[len(f"{AIChatCBT.SELECT_MODEL_PRESET}:"):])
        except ValueError:
            bot.answer_callback_query(call.id)
            return
        if idx < 0 or idx >= len(entries):
            bot.answer_callback_query(call.id)
            return
        model = entries[idx][0]
        _set_setting("General", "model", model)
        logger.info(f"{LOGGER_PREFIX} модель изменена на {model} (провайдер {provider})")
        _safe_edit_message_text(
            f"\U0001f504 {_t('model_testing', model=model)}",
            call.message.chat.id,
            call.message.message_id,
            parse_mode="HTML",
        )
        ok, reply, tokens, err_msg = run_model_test(model, provider)
        result_kb = telebot.types.InlineKeyboardMarkup()
        result_kb.row(telebot.types.InlineKeyboardButton(
            f"\U0001f916 {_t('model_label')}", callback_data=AIChatCBT.EDIT_MODEL))
        result_kb.row(telebot.types.InlineKeyboardButton(
            f"\u25c0\ufe0f {_t('back')}", callback_data=AIChatCBT.CATEGORY_CORE))
        _safe_edit_message_text(
            _model_test_text(model, provider, ok, reply, tokens, err_msg),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=result_kb,
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.MODEL_MANUAL)
    def _model_manual_input(call: telebot.types.CallbackQuery) -> None:
        """Ask the user to type a model name manually."""
        _set_pending_input(call.from_user.id, "model")
        bot.send_message(
            call.message.chat.id,
            _t("enter_model"),
            parse_mode="HTML",
            reply_markup=telebot.types.ForceReply(selective=True),
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.TEST_MODEL)
    def _test_model_button(call: telebot.types.CallbackQuery) -> None:
        """Run model connectivity test from a dedicated button."""
        config = load_config()
        provider = get_provider(config)
        model = get_model(config)
        if not model:
            bot.send_message(
                call.message.chat.id,
                _t('test_no_key'),
                parse_mode="HTML",
            )
            bot.answer_callback_query(call.id)
            return
        bot.send_message(
            call.message.chat.id,
            f"{_t('test_testing')} ({provider}, {model})",
            parse_mode="HTML",
        )
        ok, reply, tokens, err_msg = run_model_test(model, provider)
        result_kb = telebot.types.InlineKeyboardMarkup()
        result_kb.row(telebot.types.InlineKeyboardButton(
            f"\U0001f9ea {_t('test_model')}", callback_data=AIChatCBT.TEST_MODEL))
        result_kb.row(telebot.types.InlineKeyboardButton(
            f"\U0001f916 {_t('model_label')}", callback_data=AIChatCBT.EDIT_MODEL))
        result_kb.row(telebot.types.InlineKeyboardButton(
            f"\u25c0\ufe0f {_t('back')}", callback_data=AIChatCBT.CATEGORY_CORE))
        _safe_edit_message_text(
            _model_test_text(model, provider, ok, reply, tokens, err_msg),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=result_kb,
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.EDIT_API_KEY)
    def _edit_api_key(call: telebot.types.CallbackQuery) -> None:
        _set_pending_input(call.from_user.id, "api_key")
        bot.send_message(
            call.message.chat.id,
            _t("enter_api_key"),
            reply_markup=telebot.types.ForceReply(selective=True),
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.EDIT_SYSTEM_PROMPT)
    def _edit_system_prompt(call: telebot.types.CallbackQuery) -> None:
        _set_pending_input(call.from_user.id, "system_prompt")
        bot.send_message(
            call.message.chat.id,
            _t("enter_system_prompt"),
            reply_markup=telebot.types.ForceReply(selective=True),
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.EDIT_HOLDING_MSG)
    def _edit_holding_msg(call: telebot.types.CallbackQuery) -> None:
        _set_pending_input(call.from_user.id, "holding_message")
        bot.send_message(
            call.message.chat.id,
            _t("enter_holding_msg"),
            reply_markup=telebot.types.ForceReply(selective=True),
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.EDIT_MAX_HISTORY)
    def _edit_max_history(call: telebot.types.CallbackQuery) -> None:
        _set_pending_input(call.from_user.id, "max_history_messages")
        bot.send_message(
            call.message.chat.id,
            _t("enter_max_history"),
            reply_markup=telebot.types.ForceReply(selective=True),
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.EDIT_TIMEOUT)
    def _edit_timeout(call: telebot.types.CallbackQuery) -> None:
        _set_pending_input(call.from_user.id, "response_timeout")
        bot.send_message(
            call.message.chat.id,
            _t("enter_timeout"),
            reply_markup=telebot.types.ForceReply(selective=True),
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.EDIT_SPAM_LIMIT)
    def _edit_spam_limit(call: telebot.types.CallbackQuery) -> None:
        _set_pending_input(call.from_user.id, "spam_limit")
        bot.send_message(
            call.message.chat.id,
            _t("enter_spam_limit"),
            reply_markup=telebot.types.ForceReply(selective=True),
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.EDIT_SPAM_REPLY)
    def _edit_spam_reply(call: telebot.types.CallbackQuery) -> None:
        _set_pending_input(call.from_user.id, "spam_reply")
        bot.send_message(
            call.message.chat.id,
            _t("enter_spam_reply"),
            reply_markup=telebot.types.ForceReply(selective=True),
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.EDIT_WORKING_HOURS_START)
    def _edit_wh_start(call: telebot.types.CallbackQuery) -> None:
        _set_pending_input(call.from_user.id, "working_hours_start")
        bot.send_message(
            call.message.chat.id,
            _t("enter_wh_start"),
            reply_markup=telebot.types.ForceReply(selective=True),
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.EDIT_WORKING_HOURS_END)
    def _edit_wh_end(call: telebot.types.CallbackQuery) -> None:
        _set_pending_input(call.from_user.id, "working_hours_end")
        bot.send_message(
            call.message.chat.id,
            _t("enter_wh_end"),
            reply_markup=telebot.types.ForceReply(selective=True),
        )
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.EDIT_OFFLINE_MSG)
    def _edit_offline_msg(call: telebot.types.CallbackQuery) -> None:
        _set_pending_input(call.from_user.id, "offline_message")
        bot.send_message(
            call.message.chat.id,
            _t("enter_offline_msg"),
            reply_markup=telebot.types.ForceReply(selective=True),
        )
        bot.answer_callback_query(call.id)

    # === v2.1.0 enhancement handlers ===

    def _ask_input(call, field, prompt_key, **fmt):
        _set_pending_input(call.from_user.id, field)
        bot.send_message(
            call.message.chat.id, _t(prompt_key, **fmt), parse_mode="HTML",
            reply_markup=telebot.types.ForceReply(selective=True))
        bot.answer_callback_query(call.id)

    def _confirm_v21(msg, field):
        kb = telebot.types.InlineKeyboardMarkup()
        kb.row(telebot.types.InlineKeyboardButton(
            f"\u2b05\ufe0f {_t('back_to_settings')}", callback_data=AIChatCBT.SETTINGS_MENU))
        bot.reply_to(msg, f"\u2705 {_t('setting_updated', field=field)}",
                     parse_mode="HTML", reply_markup=kb)

    def _render_core(call):
        _safe_edit_message_text(_format_core_settings(), call.message.chat.id,
            call.message.message_id, reply_markup=_core_keyboard(), parse_mode="HTML")

    def _render_moderation(call):
        _safe_edit_message_text(_format_moderation_settings(), call.message.chat.id,
            call.message.message_id, reply_markup=_moderation_keyboard(), parse_mode="HTML")

    def _render_sales(call):
        _safe_edit_message_text(_format_sales_settings(), call.message.chat.id,
            call.message.message_id, reply_markup=_sales_keyboard(), parse_mode="HTML")

    def _render_stats(call):
        _safe_edit_message_text(_format_stats_settings(), call.message.chat.id,
            call.message.message_id, reply_markup=_stats_keyboard(), parse_mode="HTML")

    def _page_from(call, base):
        rest = call.data[len(base):]
        if rest.startswith(":"):
            try:
                return int(rest[1:])
            except ValueError:
                return 0
        return 0

    # --- toggles ---
    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.TOGGLE_BUDGET)
    def _toggle_budget(call):
        _toggle_setting("Budget", "enabled")
        _render_core(call)
        bot.answer_callback_query(call.id, _t("setting_changed"))

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.TOGGLE_TOPIC_FILTER)
    def _toggle_topic_filter(call):
        _toggle_setting("TopicFilter", "enabled")
        _render_moderation(call)
        bot.answer_callback_query(call.id, _t("setting_changed"))

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.TOGGLE_ESCALATION)
    def _toggle_escalation(call):
        _toggle_setting("Escalation", "enabled")
        _render_moderation(call)
        bot.answer_callback_query(call.id, _t("setting_changed"))

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.TOGGLE_ESCALATION_PAUSE)
    def _toggle_escalation_pause(call):
        _toggle_setting("Escalation", "pause_on_escalation")
        _render_moderation(call)
        bot.answer_callback_query(call.id, _t("setting_changed"))

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.TOGGLE_OPERATOR_PAUSE)
    def _toggle_operator_pause(call):
        _toggle_setting("OperatorPause", "enabled")
        _render_moderation(call)
        bot.answer_callback_query(call.id, _t("setting_changed"))

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.TOGGLE_QA_LOG)
    def _toggle_qa_log(call):
        _toggle_setting("QALog", "enabled")
        _render_stats(call)
        bot.answer_callback_query(call.id, _t("setting_changed"))

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.TOGGLE_FAQ)
    def _toggle_faq(call):
        _toggle_setting("FAQ", "enabled")
        # keep sidecar 'enabled' in sync with the config flag (design: config mirrors sidecar)
        new_state = load_config().getboolean("FAQ", "enabled", fallback=False)
        store = _load_faq_store()
        store["enabled"] = new_state
        _save_faq_store(store)
        _render_sales(call)
        bot.answer_callback_query(call.id, _t("setting_changed"))

    # --- numeric / text edits ---
    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.EDIT_MEMORY_TTL)
    def _edit_memory_ttl(call):
        _ask_input(call, "memory_ttl", "enter_memory_ttl")

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.EDIT_FALLBACK_MODELS)
    def _edit_fallback_models(call):
        _ask_input(call, "fallback_models", "enter_fallback_models")

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.EDIT_BUDGET_LIMIT)
    def _edit_budget_limit(call):
        _ask_input(call, "budget_limit", "enter_budget_limit")

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.EDIT_BUDGET_UNIT)
    def _edit_budget_unit(call):
        _ask_input(call, "budget_unit", "enter_budget_unit")

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.EDIT_BUDGET_ALERT)
    def _edit_budget_alert(call):
        _ask_input(call, "budget_alert", "enter_budget_alert")

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.EDIT_PAUSE_TIMEOUT)
    def _edit_pause_timeout(call):
        _ask_input(call, "pause_timeout", "enter_pause_timeout")

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.EDIT_TOPIC_CANNED_REPLY)
    def _edit_topic_canned(call):
        _ask_input(call, "topic_canned", "enter_topic_canned")

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.ADD_TOPIC)
    def _add_topic(call):
        _ask_input(call, "add_topic", "enter_topic")

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.ADD_ESCALATION)
    def _add_escalation(call):
        _ask_input(call, "add_escalation", "enter_escalation")

    @bot.callback_query_handler(func=lambda call: call.data == AIChatCBT.ADD_FAQ)
    def _add_faq(call):
        _ask_input(call, "add_faq_patterns", "enter_faq_patterns")

    # --- paginated list-management menus ---
    @bot.callback_query_handler(
        func=lambda call: call.data == AIChatCBT.MANAGE_TOPIC_FILTER
        or call.data.startswith(AIChatCBT.MANAGE_TOPIC_FILTER + ":"))
    def _manage_topic_filter(call):
        page = _page_from(call, AIChatCBT.MANAGE_TOPIC_FILTER)
        items = get_deny_list(load_config())
        _safe_edit_message_text(
            _list_menu_text(_t("topic_mgmt"), items, page),
            call.message.chat.id, call.message.message_id,
            reply_markup=_list_menu_keyboard(
                items, AIChatCBT.REMOVE_TOPIC_PREFIX, AIChatCBT.ADD_TOPIC,
                _t("add_topic"), AIChatCBT.CATEGORY_MODERATION,
                AIChatCBT.MANAGE_TOPIC_FILTER + ":", page),
            parse_mode="HTML")
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(
        func=lambda call: call.data == AIChatCBT.MANAGE_ESCALATION
        or call.data.startswith(AIChatCBT.MANAGE_ESCALATION + ":"))
    def _manage_escalation(call):
        page = _page_from(call, AIChatCBT.MANAGE_ESCALATION)
        items = get_escalation_keywords(load_config())
        _safe_edit_message_text(
            _list_menu_text(_t("escalation_mgmt"), items, page),
            call.message.chat.id, call.message.message_id,
            reply_markup=_list_menu_keyboard(
                items, AIChatCBT.REMOVE_ESCALATION_PREFIX, AIChatCBT.ADD_ESCALATION,
                _t("add_escalation"), AIChatCBT.CATEGORY_MODERATION,
                AIChatCBT.MANAGE_ESCALATION + ":", page),
            parse_mode="HTML")
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(
        func=lambda call: call.data == AIChatCBT.MANAGE_FAQ
        or call.data.startswith(AIChatCBT.MANAGE_FAQ + ":"))
    def _manage_faq(call):
        page = _page_from(call, AIChatCBT.MANAGE_FAQ)
        store = _load_faq_store()
        labels = [", ".join(e.get("patterns", [])) or e.get("answer", "")
                  for e in store.get("entries", [])]
        _safe_edit_message_text(
            _list_menu_text(_t("faq_mgmt"), labels, page),
            call.message.chat.id, call.message.message_id,
            reply_markup=_list_menu_keyboard(
                labels, AIChatCBT.REMOVE_FAQ_PREFIX, AIChatCBT.ADD_FAQ,
                _t("add_faq"), AIChatCBT.CATEGORY_SALES,
                AIChatCBT.MANAGE_FAQ + ":", page),
            parse_mode="HTML")
        bot.answer_callback_query(call.id)

    # --- list removals (by absolute index) ---
    @bot.callback_query_handler(
        func=lambda call: call.data.startswith(AIChatCBT.REMOVE_TOPIC_PREFIX))
    def _remove_topic(call):
        items = get_deny_list(load_config())
        try:
            idx = int(call.data[len(AIChatCBT.REMOVE_TOPIC_PREFIX):])
        except ValueError:
            idx = -1
        if 0 <= idx < len(items):
            removed = items.pop(idx)
            _set_setting("TopicFilter", "deny_list", ", ".join(items))
            bot.answer_callback_query(call.id, _t("removed", item=removed))
        else:
            bot.answer_callback_query(call.id)
        _manage_topic_filter(call)

    @bot.callback_query_handler(
        func=lambda call: call.data.startswith(AIChatCBT.REMOVE_ESCALATION_PREFIX))
    def _remove_escalation(call):
        items = get_escalation_keywords(load_config())
        try:
            idx = int(call.data[len(AIChatCBT.REMOVE_ESCALATION_PREFIX):])
        except ValueError:
            idx = -1
        if 0 <= idx < len(items):
            removed = items.pop(idx)
            _set_setting("Escalation", "keywords", ", ".join(items))
            bot.answer_callback_query(call.id, _t("removed", item=removed))
        else:
            bot.answer_callback_query(call.id)
        _manage_escalation(call)

    @bot.callback_query_handler(
        func=lambda call: call.data.startswith(AIChatCBT.REMOVE_FAQ_PREFIX))
    def _remove_faq(call):
        store = _load_faq_store()
        entries = store.get("entries", [])
        try:
            idx = int(call.data[len(AIChatCBT.REMOVE_FAQ_PREFIX):])
        except ValueError:
            idx = -1
        if 0 <= idx < len(entries):
            entries.pop(idx)
            store["entries"] = entries
            _save_faq_store(store)
            bot.answer_callback_query(call.id, _t("removed", item=str(idx + 1)))
        else:
            bot.answer_callback_query(call.id)
        _manage_faq(call)

    # --- Text input handler ---

    @bot.message_handler(
        func=lambda msg: _has_pending_input(msg.from_user.id) and msg.reply_to_message is not None
    )
    def _handle_text_input(msg: telebot.types.Message) -> None:
        """Handle text input for settings that require typed values."""
        user_id = msg.from_user.id
        field = _get_pending_input(user_id)
        if field is None:
            return

        value = msg.text.strip()
        if not value:
            bot.reply_to(msg, f"\u274c {_t('value_empty')}")
            return

        # Handle two-step template input
        if field == "add_template_name":
            # Store the name and ask for template text
            _set_pending_input(user_id, ("add_template_text", value))
            bot.send_message(
                msg.chat.id,
                _t("enter_template_text", name=value),
                parse_mode="HTML",
                reply_markup=telebot.types.ForceReply(selective=True),
            )
            return

        if isinstance(field, tuple) and field[0] == "add_template_text":
            template_name = field[1]
            _set_setting("Templates", f"template_{template_name}", value)
            kb = telebot.types.InlineKeyboardMarkup()
            kb.row(
                telebot.types.InlineKeyboardButton(
                    f"\u2b05\ufe0f {_t('back_to_templates')}",
                    callback_data=AIChatCBT.MANAGE_TEMPLATES,
                )
            )
            bot.reply_to(
                msg,
                f"\u2705 {_t('template_added', name=template_name)}",
                parse_mode="HTML",
                reply_markup=kb,
            )
            return

        # --- Product catalog: 3-step input (name → price → desc) ---
        if field == "add_product_name":
            _set_pending_input(user_id, ("add_product_price", value))
            bot.send_message(
                msg.chat.id,
                _t("enter_product_price", name=value),
                parse_mode="HTML",
                reply_markup=telebot.types.ForceReply(selective=True),
            )
            return

        if isinstance(field, tuple) and field[0] == "add_product_price":
            product_name = field[1]
            _set_pending_input(user_id, ("add_product_desc", product_name, value))
            bot.send_message(
                msg.chat.id,
                _t("enter_product_desc", name=product_name, price=value),
                parse_mode="HTML",
                reply_markup=telebot.types.ForceReply(selective=True),
            )
            return

        if isinstance(field, tuple) and field[0] == "add_product_desc":
            product_name = field[1]
            product_price = field[2]
            _add_product_to_catalog(product_name, product_price, value)
            # Clear cache so new product loads immediately
            global _lot_details_cache, _lot_details_cache_time
            _lot_details_cache = []
            _lot_details_cache_time = 0.0
            kb = telebot.types.InlineKeyboardMarkup()
            kb.row(telebot.types.InlineKeyboardButton(
                f"\u2b05\ufe0f {_t('back_to_settings')}",
                callback_data=AIChatCBT.CATEGORY_SALES))
            bot.reply_to(
                msg,
                f"\u2705 {_t('product_added', name=product_name)}",
                parse_mode="HTML",
                reply_markup=kb,
            )
            return

        # Handle two-step promo input
        if field == "add_promo_code":
            _set_pending_input(user_id, ("add_promo_desc", value))
            bot.send_message(
                msg.chat.id,
                _t("enter_promo_desc", code=value),
                parse_mode="HTML",
                reply_markup=telebot.types.ForceReply(selective=True),
            )
            return

        if isinstance(field, tuple) and field[0] == "add_promo_desc":
            promo_code = field[1]
            config = load_config()
            codes_str = config.get("Promos", "codes", fallback="")
            new_entry = f"{promo_code}:{value}"
            if codes_str.strip():
                codes_str = codes_str.strip() + "\n" + new_entry
            else:
                codes_str = new_entry
            _set_setting("Promos", "codes", codes_str)
            kb = telebot.types.InlineKeyboardMarkup()
            kb.row(
                telebot.types.InlineKeyboardButton(
                    f"\u2b05\ufe0f {_t('back_to_promos')}",
                    callback_data=AIChatCBT.MANAGE_PROMOS,
                )
            )
            bot.reply_to(
                msg,
                f"\u2705 {_t('promo_added', code=promo_code)}",
                parse_mode="HTML",
                reply_markup=kb,
            )
            return

        # Handle blacklist add
        if field == "add_blacklist_user":
            config = load_config()
            users_str = config.get("Blacklist", "users", fallback="")
            users = [u.strip() for u in users_str.split(",") if u.strip()]
            if value.lower() not in [u.lower() for u in users]:
                users.append(value)
            _set_setting("Blacklist", "users", ", ".join(users))
            kb = telebot.types.InlineKeyboardMarkup()
            kb.row(
                telebot.types.InlineKeyboardButton(
                    f"\u2b05\ufe0f {_t('back_to_blacklist')}",
                    callback_data=AIChatCBT.MANAGE_BLACKLIST,
                )
            )
            bot.reply_to(
                msg,
                f"\u2705 {_t('added_to_blacklist', user=value)}",
                parse_mode="HTML",
                reply_markup=kb,
            )
            return

        # Handle custom provider wizard - step 1: name
        if isinstance(field, tuple) and field[0] == "add_cprovider_name":
            name = value.strip().lower()
            if not re.match(r'^[a-z0-9_]{2,20}$', name):
                bot.reply_to(msg, f"\u274c {_t('invalid_cprovider_name')}")
                return
            if name in PROVIDERS:
                bot.reply_to(msg, f"\u274c {_t('cprovider_reserved')}")
                return
            if name in _load_custom_providers():
                bot.reply_to(msg, f"\u274c {_t('cprovider_exists')}")
                return
            _set_pending_input(user_id, ("add_cprovider_url", name))
            bot.send_message(
                msg.chat.id,
                _t("enter_cprovider_url", name=name),
                parse_mode="HTML",
                reply_markup=telebot.types.ForceReply(selective=True),
            )
            return

        # Handle custom provider wizard - step 2: base URL
        if isinstance(field, tuple) and field[0] == "add_cprovider_url":
            name = field[1]
            if not value.startswith(("http://", "https://")):
                bot.reply_to(msg, f"\u274c {_t('invalid_cprovider_url')}")
                return
            _set_pending_input(user_id, ("add_cprovider_models", name, value))
            bot.send_message(
                msg.chat.id,
                _t("enter_cprovider_models"),
                parse_mode="HTML",
                reply_markup=telebot.types.ForceReply(selective=True),
            )
            return

        # Handle custom provider wizard - step 3: models (optional)
        if isinstance(field, tuple) and field[0] == "add_cprovider_models":
            name, base_url = field[1], field[2]
            models = [] if value == "-" else [
                m.strip() for m in re.split(r"[,\n]", value) if m.strip()]
            _set_pending_input(user_id, ("add_cprovider_key", name, base_url, models))
            bot.send_message(
                msg.chat.id,
                _t("enter_cprovider_key"),
                parse_mode="HTML",
                reply_markup=telebot.types.ForceReply(selective=True),
            )
            return

        # Handle custom provider wizard - step 4: API key (optional) → save
        if isinstance(field, tuple) and field[0] == "add_cprovider_key":
            name, base_url, models = field[1], field[2], field[3]
            api_key_value = "" if value == "-" else value
            providers = _load_custom_providers()
            providers[name] = {
                "base_url": base_url,
                "models": models,
                "api_key": api_key_value,
            }
            _save_custom_providers(providers)
            logger.info(
                f"{LOGGER_PREFIX} добавлен кастомный провайдер {name} "
                f"({base_url}), моделей: {len(models)}, "
                f"ключ: {'есть' if api_key_value else 'общий'}")
            kb = telebot.types.InlineKeyboardMarkup()
            kb.row(telebot.types.InlineKeyboardButton(
                f"\u2705 {_t('cprovider_make_active')}",
                callback_data=f"{AIChatCBT.SELECT_PROVIDER}:{name}"))
            kb.row(telebot.types.InlineKeyboardButton(
                f"\U0001f310 {_t('provider_label')}", callback_data=AIChatCBT.EDIT_PROVIDER))
            bot.reply_to(
                msg,
                f"\u2705 {_t('cprovider_added', name=name)}\n"
                f"\U0001f4e1 {base_url}\n"
                f"\U0001f916 {_t('cprovider_models_count', count=len(models))}",
                parse_mode="HTML",
                reply_markup=kb,
            )
            return

        # Handle stopword add
        if field == "add_stopword":
            config = load_config()
            words_str = config.get("StopWords", "words", fallback="")
            words = [w.strip() for w in words_str.split(",") if w.strip()]
            if value.lower() not in [w.lower() for w in words]:
                words.append(value.lower())
            _set_setting("StopWords", "words", ",".join(words))
            kb = telebot.types.InlineKeyboardMarkup()
            kb.row(
                telebot.types.InlineKeyboardButton(
                    f"\u2b05\ufe0f {_t('back_to_stopwords')}",
                    callback_data=AIChatCBT.MANAGE_STOPWORDS,
                )
            )
            bot.reply_to(
                msg,
                f"\u2705 {_t('stopword_added', word=value)}",
                parse_mode="HTML",
                reply_markup=kb,
            )
            return

        # Handle create preset - step 1: name
        if isinstance(field, tuple) and field == ("create_preset_name",):
            name = value.strip().lower()
            if not re.match(r'^[a-z0-9_]{2,30}$', name):
                bot.reply_to(msg,
                    "\u274c \u041d\u0430\u0437\u0432\u0430\u043d\u0438\u0435 \u0434\u043e\u043b\u0436\u043d\u043e \u0441\u043e\u0434\u0435\u0440\u0436\u0430\u0442\u044c \u0442\u043e\u043b\u044c\u043a\u043e \u043b\u0430\u0442\u0438\u043d\u0438\u0446\u0443, \u0446\u0438\u0444\u0440\u044b \u0438 _ (2-30 \u0441\u0438\u043c\u0432\u043e\u043b\u043e\u0432).")
                return
            if name in PRESET_PROMPTS:
                bot.reply_to(msg,
                    "\u274c \u042d\u0442\u043e \u043d\u0430\u0437\u0432\u0430\u043d\u0438\u0435 \u0437\u0430\u0440\u0435\u0437\u0435\u0440\u0432\u0438\u0440\u043e\u0432\u0430\u043d\u043e \u0432\u0441\u0442\u0440\u043e\u0435\u043d\u043d\u044b\u043c \u043f\u0440\u0435\u0441\u0435\u0442\u043e\u043c.")
                return
            _set_pending_input(msg.from_user.id, ("create_preset_text", name))
            bot.send_message(msg.chat.id,
                f"\u2705 \u041d\u0430\u0437\u0432\u0430\u043d\u0438\u0435: <b>{name}</b>\n\n\u0422\u0435\u043f\u0435\u0440\u044c \u043e\u0442\u043f\u0440\u0430\u0432\u044c\u0442\u0435 \u0442\u0435\u043a\u0441\u0442 \u043f\u0440\u043e\u043c\u043f\u0442\u0430:",
                parse_mode="HTML",
                reply_markup=telebot.types.ForceReply(selective=True))
            return

        # Handle create preset - step 2: text
        if isinstance(field, tuple) and field[0] == "create_preset_text":
            preset_name = field[1]
            custom_presets = _load_custom_presets()
            custom_presets[preset_name] = value
            _save_custom_presets(custom_presets)
            bot.reply_to(msg,
                f"\u2705 \u041f\u0440\u0435\u0441\u0435\u0442 <b>{preset_name}</b> \u0441\u043e\u0437\u0434\u0430\u043d ({len(value)} \u0441\u0438\u043c\u0432.).\n"
                f"\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u0435\u0433\u043e \u0447\u0435\u0440\u0435\u0437 \u043c\u0435\u043d\u044e \u043f\u0440\u0435\u0441\u0435\u0442\u043e\u0432.",
                parse_mode="HTML")
            return

        # Handle edit preset text
        if isinstance(field, tuple) and field[0] == "edit_preset_text":
            preset_key = field[1]
            if value.strip().lower() in ("\u043e\u0442\u043c\u0435\u043d\u0430", "cancel"):
                bot.reply_to(msg, "\u041e\u0442\u043c\u0435\u043d\u0435\u043d\u043e.")
                return
            # Save: for built-in presets, store override in custom presets file
            custom_presets = _load_custom_presets()
            custom_presets[preset_key] = value
            _save_custom_presets(custom_presets)
            # If built-in was overridden, also update PRESET_PROMPTS in memory
            if preset_key in PRESET_PROMPTS:
                PRESET_PROMPTS[preset_key] = value
            bot.reply_to(msg,
                f"\u2705 \u041f\u0440\u0435\u0441\u0435\u0442 <b>{preset_key}</b> \u043e\u0431\u043d\u043e\u0432\u043b\u0451\u043d ({len(value)} \u0441\u0438\u043c\u0432.).",
                parse_mode="HTML")
            return

        # Handle upsell prompt
        if field == "upsell_prompt":
            _set_setting("Upsell", "prompt_addon", value)
            kb = telebot.types.InlineKeyboardMarkup()
            kb.row(
                telebot.types.InlineKeyboardButton(
                    f"\u2b05\ufe0f {_t('back_to_settings')}",
                    callback_data=AIChatCBT.SETTINGS_MENU,
                )
            )
            bot.reply_to(
                msg,
                f"\u2705 {_t('upsell_updated')}",
                parse_mode="HTML",
                reply_markup=kb,
            )
            return

        # --- v2.1.0: positive-integer fields ---
        if field in ("memory_ttl", "pause_timeout", "budget_limit", "budget_alert"):
            n = _parse_positive_int(value)
            if n is None:
                bot.reply_to(msg, f"\u274c {_t('invalid_number')}")
                return
            section_key = {
                "memory_ttl": ("General", "memory_ttl_seconds"),
                "pause_timeout": ("OperatorPause", "timeout_seconds"),
                "budget_limit": ("Budget", "daily_limit"),
                "budget_alert": ("Budget", "alert_threshold"),
            }[field]
            _set_setting(section_key[0], section_key[1], str(n))
            _confirm_v21(msg, field)
            return

        # --- v2.1.0: budget unit ---
        if field == "budget_unit":
            unit = value.strip().lower()
            if unit not in ("tokens", "requests"):
                bot.reply_to(msg, f"\u274c {_t('invalid_number')}")
                return
            _set_setting("Budget", "unit", unit)
            _confirm_v21(msg, field)
            return

        # --- v2.1.0: fallback models / topic canned reply (free text) ---
        if field == "fallback_models":
            _set_setting("General", "fallback_models", value)
            _confirm_v21(msg, field)
            return
        if field == "topic_canned":
            _set_setting("TopicFilter", "canned_reply", value)
            _confirm_v21(msg, field)
            return

        # --- v2.1.0: deny-list / escalation list additions ---
        if field in ("add_topic", "add_escalation"):
            section, key, back_cb = (
                ("TopicFilter", "deny_list", AIChatCBT.MANAGE_TOPIC_FILTER)
                if field == "add_topic"
                else ("Escalation", "keywords", AIChatCBT.MANAGE_ESCALATION)
            )
            config = load_config()
            items = _parse_csv_list(config.get(section, key, fallback=""))
            if value.lower() not in [i.lower() for i in items]:
                items.append(value)
            _set_setting(section, key, ", ".join(items))
            kb = telebot.types.InlineKeyboardMarkup()
            kb.row(telebot.types.InlineKeyboardButton(
                f"\u2b05\ufe0f {_t('back')}", callback_data=back_cb))
            label = "topic_added" if field == "add_topic" else "escalation_added"
            bot.reply_to(msg, f"\u2705 {_t(label, item=value)}",
                         parse_mode="HTML", reply_markup=kb)
            return

        # --- v2.1.0: FAQ two-step add ---
        if field == "add_faq_patterns":
            pats = _parse_csv_list(value)
            if not pats:
                bot.reply_to(msg, f"\u274c {_t('value_empty')}")
                return
            _set_pending_input(user_id, ("add_faq_answer", value))
            bot.send_message(
                msg.chat.id, _t("enter_faq_answer", patterns=", ".join(pats)),
                parse_mode="HTML",
                reply_markup=telebot.types.ForceReply(selective=True))
            return
        if isinstance(field, tuple) and field[0] == "add_faq_answer":
            patterns = _parse_csv_list(field[1])
            store = _load_faq_store()
            store.setdefault("entries", [])
            store["entries"].append({
                "id": f"faq_{int(time.time())}",
                "patterns": patterns,
                "answer": value,
            })
            _save_faq_store(store)
            kb = telebot.types.InlineKeyboardMarkup()
            kb.row(telebot.types.InlineKeyboardButton(
                f"\u2b05\ufe0f {_t('back')}", callback_data=AIChatCBT.MANAGE_FAQ))
            bot.reply_to(msg, f"\u2705 {_t('faq_added')}",
                         parse_mode="HTML", reply_markup=kb)
            return

        # Validate numeric fields
        if field in ("max_history_messages", "response_timeout", "spam_limit",
                     "working_hours_start", "working_hours_end"):
            try:
                int_val = int(value)
                if int_val < 0:
                    raise ValueError()
                if field in ("working_hours_start", "working_hours_end") and int_val > 23:
                    raise ValueError()
            except ValueError:
                bot.reply_to(msg, f"\u274c {_t('invalid_number')}")
                return

        # Model input: save, then run the connection test automatically
        if field == "model":
            _set_setting("General", "model", value)
            provider = get_provider(load_config())
            logger.info(f"{LOGGER_PREFIX} модель изменена на {value} (провайдер {provider})")
            bot.reply_to(msg, f"\U0001f504 {_t('model_testing', model=value)}", parse_mode="HTML")
            ok, reply, tokens, err_msg = run_model_test(value, provider)
            kb = telebot.types.InlineKeyboardMarkup()
            kb.row(telebot.types.InlineKeyboardButton(
                f"\U0001f916 {_t('model_label')}", callback_data=AIChatCBT.EDIT_MODEL))
            kb.row(telebot.types.InlineKeyboardButton(
                f"\u25c0\ufe0f {_t('back_to_settings')}", callback_data=AIChatCBT.SETTINGS_MENU))
            bot.reply_to(
                msg,
                _model_test_text(value, provider, ok, reply, tokens, err_msg),
                parse_mode="HTML",
                reply_markup=kb,
            )
            return

        # API key input: save without echoing the value back
        if field == "api_key":
            _set_setting("General", "openrouter_api_key", value)
            logger.info(
                f"{LOGGER_PREFIX} API ключ обновлён "
                f"(провайдер {get_provider(load_config())}, длина {len(value)})")
            kb = telebot.types.InlineKeyboardMarkup()
            kb.row(
                telebot.types.InlineKeyboardButton(
                    f"\u2b05\ufe0f {_t('back_to_settings')}", callback_data=AIChatCBT.SETTINGS_MENU
                )
            )
            bot.reply_to(
                msg,
                f"\u2705 {_t('setting_updated', field=field)}",
                parse_mode="HTML",
                reply_markup=kb,
            )
            return

        # Map field names to config sections and keys
        # (model и api_key обрабатываются выше отдельными ветками)
        field_map = {
            "system_prompt": ("General", "system_prompt"),
            "holding_message": ("Forwarding", "holding_message"),
            "max_history_messages": ("General", "max_history_messages"),
            "response_timeout": ("General", "response_timeout"),
            "spam_limit": ("AntiSpam", "max_messages_per_minute"),
            "spam_reply": ("AntiSpam", "spam_reply"),
            "working_hours_start": ("WorkingHours", "start_hour"),
            "working_hours_end": ("WorkingHours", "end_hour"),
            "offline_message": ("WorkingHours", "offline_message"),
        }

        section, key = field_map.get(field, (None, None))
        if section is None:
            bot.reply_to(msg, f"\u274c {_t('unknown_field')}")
            return

        _set_setting(section, key, value)

        # Show confirmation with a button to return to settings
        kb = telebot.types.InlineKeyboardMarkup()
        kb.row(
            telebot.types.InlineKeyboardButton(
                f"\u2b05\ufe0f {_t('back_to_settings')}", callback_data=AIChatCBT.SETTINGS_MENU
            )
        )
        bot.reply_to(
            msg,
            f"\u2705 {_t('setting_updated', field=field)}",
            parse_mode="HTML",
            reply_markup=kb,
        )


def open_settings(c: Cardinal, msg: telebot.types.Message) -> None:
    """Open the settings page. Called by Cardinal when user clicks plugin settings."""
    bot = c.telegram.bot
    bot.send_message(
        msg.chat.id,
        _format_current_settings(),
        reply_markup=_settings_keyboard(),
        parse_mode="HTML",
    )


def _pre_init(c: Cardinal) -> None:
    """Pre-init: register TG handlers, /aichat command, settings page callback, guide, test."""
    global _cardinal_ref
    _cardinal_ref = c
    _load_language_from_config()
    init(c)
    tg = getattr(c, "telegram", None)
    if tg is None:
        return
    bot = tg.bot

    # Handle settings page callback from FPC plugin card (47:{UUID}:offset)
    @bot.callback_query_handler(func=lambda call: call.data.startswith(f"47:{UUID}"))
    def _settings_page_callback(call: telebot.types.CallbackQuery) -> None:
        _safe_edit_message_text(
            _format_current_settings(),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=_settings_keyboard(),
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id)

    # /aichat command to open settings
    @bot.message_handler(commands=["aichat"])
    def _cmd_aichat(msg: telebot.types.Message) -> None:
        bot.send_message(
            msg.chat.id,
            _format_current_settings(),
            reply_markup=_settings_keyboard(),
            parse_mode="HTML",
        )

    # /aichat_guide — guide
    @bot.message_handler(commands=["aichat_guide"])
    def _cmd_guide(msg: telebot.types.Message) -> None:
        try:
            bot.send_message(
                msg.chat.id,
                f"<b>\U0001f4d6 {_t('guide_title')}</b>\n\n{_t('guide_body')}",
                parse_mode="HTML",
            )
        except Exception as e:
            try:
                bot.send_message(
                    msg.chat.id,
                    f"\u26a0 Не удалось отправить гайд: <code>{e}</code>",
                    parse_mode="HTML")
            except Exception:
                logger.error("ai_chat: cmd_guide failed", exc_info=True)

    # /aichat_test — test with fake data
    @bot.message_handler(commands=["aichat_test"])
    def _cmd_test(msg: telebot.types.Message) -> None:
        config = load_config()
        provider = get_provider(config)
        model = get_model(config)

        if not api_key_present_for(provider):
            bot.send_message(
                msg.chat.id,
                f"\u274c <b>{_t('test_no_key')}</b>",
                parse_mode="HTML",
            )
            return

        bot.send_message(
            msg.chat.id,
            f"\U0001f504 {_t('test_testing')} ({provider}, {model})")

        ok, test_response, tokens, err_msg = run_model_test(model, provider)

        if ok:
            result = (
                f"\u2705 <b>{_t('test_passed')}</b>\n\n"
                f"\U0001f310 Provider: <code>{provider}</code>\n"
                f"\U0001f916 {_t('model_label')}: <code>{model}</code>\n"
                f"\U0001f4ca {_t('tokens_used')}: {tokens}\n"
                f"\U0001f4ac AI: <i>{(test_response or '')[:200]}</i>"
            )
        else:
            result = (
                f"\u274c <b>{_t('test_failed')}</b>\n\n"
                f"\U0001f310 Provider: <code>{provider}</code>\n"
                f"\U0001f916 {_t('model_label')}: <code>{model}</code>\n"
                f"\u26a0\ufe0f {err_msg or 'Неизвестная ошибка'}"
            )

        bot.send_message(msg.chat.id, result, parse_mode="HTML")

    try:
        c.add_telegram_commands(UUID, [
            ("aichat", "AI Chat: настройки", True),
            ("aichat_guide", "AI Chat: гайд", True),
            ("aichat_test", "AI Chat: тест", True),
        ])
    except Exception:
        logger.debug("Не удалось зарегистрировать команды AI Chat Plugin")

    # 💛 Донат-баннер (защита реквизитов автора)
    global _donation_cardinal
    _donation_cardinal = c
    try:
        tg.cbq_handler(
            _donation_on_cb,
            lambda c: (c.data or "").startswith(DONATION_CALLBACK_PREFIX + ":"))
        _start_donation_reminder(c)
    except Exception:
        logger.debug("donation banner register failed", exc_info=True)

    # 📦 Одноразовое приветствие с рекламой канала автора
    if DONATION_SHOW_ON_START:
        try:
            _send_startup_welcome(c)
        except Exception:
            logger.debug("startup welcome send failed", exc_info=True)


BIND_TO_PRE_INIT = [_pre_init]
BIND_TO_SETTINGS_PAGE = open_settings



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
