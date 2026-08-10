"""
AIReviews plugin for FunPay Cardinal
====================================

Авто-ответ ИИ на отзывы покупателей к заказам FunPay через единую абстракцию
AI-провайдеров (OpenRouter / OpenAI / Gemini). Русское Telegram-меню.

Чистая переписка стороннего `gpt_review.py`, который был жёстко завязан на
Groq + g4f через реферальный прокси. Здесь — НЕТ g4f, НЕТ groq, НЕТ прокси,
только `requests` и единый клиент с тремя адаптерами.

Поток: событие NEW_FEEDBACK / FEEDBACK_CHANGED → `account.get_order(order_id)` →
проверка порога звёзд и кулдауна → рендер промпта → запрос к модели (primary +
fallback) → `account.send_review(order.id, rating=None, text=...)`. Опционально —
тёплое сообщение покупателю в чат.

Без бэкдоров: НЕТ БД, НЕТ удалённой «активации»/лицензий/kill-switch. Outbound
только к funpay.com, openrouter.ai, api.openai.com,
generativelanguage.googleapis.com и Telegram Bot API. Все ключи маскируются.
Конфиг — только в storage/plugins/ai_reviews/ (плюс read-only чтение ключей из
configs/ai_chat_plugin.cfg).
"""

from __future__ import annotations

import configparser
import json
import logging
import os
import re
import threading
import time
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
DONATION_SHOW_ON_START = False         # True = 1 (слать при старте плагина)
DONATION_DAILY_ENABLED = True          # True = 1 (напоминание раз в сутки)
DONATION_DAILY_HOUR = 16               # час напоминания (0-23, МСК)
DONATION_CALLBACK_PREFIX = "airev_dn"  # префикс колбэков кнопок баннера
DONATION_PLUGIN_NAME = "AI Reviews"    # имя плагина в шапке баннера

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
            tg = getattr(cardinal, "telegram", None)
            for uid in list(getattr(tg, "authorized_users", []) or []):
                try:
                    tg.bot.send_message(
                        uid,
                        "😄 Улыбнись! Тебя снимает скрытая камера 📷\n\n"
                        "А если захочешь отблагодарить за бесплатный "
                        "плагин — реквизиты в баннере выше 😉",
                        parse_mode="HTML",
                        reply_markup=_donation_banner_kb(),
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

NAME = "AIReviews"
VERSION = "1.0.1"
DESCRIPTION = (
    "Авто-ответ ИИ на отзывы покупателей через единую абстракцию провайдеров "
    "(OpenRouter / OpenAI / Gemini): primary + fallback модели, порог звёзд, "
    "кулдаун на покупателя, опц. сообщение в чат, русское Telegram-меню. "
    "Без g4f/groq, без бэкдоров и удалённой активации."
)
CREDITS = "@drakelovc"
UUID = "b9f3c1d7-2a64-4e58-8c1a-7d0e9f5b3a26"
SETTINGS_PAGE = True

logger = logging.getLogger(f"FPC.{__name__}")
LOGGER_PREFIX = "[AIREVIEWS]"


# =========================================================================
# Хранилище: пути и дефолты
# =========================================================================

PLUGIN_DIR = Path("storage/plugins/ai_reviews")
SETTINGS_PATH = PLUGIN_DIR / "settings.json"
REVIEWS_PATH = PLUGIN_DIR / "reviews.json"
STATE_PATH = PLUGIN_DIR / "state.json"

# Read-only: общий конфиг ai_chat_plugin для повторного использования ключей (Req 1.3)
AICHAT_CFG_PATH = "configs/ai_chat_plugin.cfg"

# Дефолтные base_url провайдеров
DEFAULT_PROVIDERS: dict[str, dict[str, str]] = {
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "api_key": ""},
    "openai": {"base_url": "https://api.openai.com/v1", "api_key": ""},
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta", "api_key": ""},
}

# Допустимые хосты для outbound-запросов AI (Req 8.2)
ALLOWED_AI_HOSTS = ("openrouter.ai", "api.openai.com", "generativelanguage.googleapis.com")

RANGE_MIN_STARS = (1, 5)
RANGE_COOLDOWN = (0, 86400)
MAX_FALLBACK_MODELS = 10
MAX_MODEL_LEN = 128

# Лимиты ответа FunPay / чата
REVIEW_MAX_CHARS = 800
CHAT_MAX_CHARS = 500

# Сетевые ретраи
MODEL_RETRIES = 2          # попыток на одну модель (Req 2.3)
SEND_REVIEW_RETRIES = 3    # попыток отправки отзыва на FunPay (Req 4.5)
RETRY_BACKOFF_SEC = 0.5    # базовая пауза между ретраями (тесты ставят 0)
HTTP_TIMEOUT = 30

DEFAULT_SETTINGS: dict[str, Any] = {
    "providers": json.loads(json.dumps(DEFAULT_PROVIDERS)),
    "active_provider": "openrouter",
    "primary_model": "openai/gpt-4o-mini",
    "fallback_models": ["google/gemini-2.0-flash"],
    "min_stars": 4,
    "reply_cooldown_sec": 60,
    "send_in_chat": False,
    "prompt_template": (
        "Привет! Ты — ИИ-ассистент магазина. Покупатель {name} оценил товар "
        "«{item}» на {rating}/5 и написал: «{text}». Ответь в доброжелательном "
        "тоне, поблагодари за покупку, добавь эмодзи. Без упоминания других "
        "ресурсов. До 800 символов."
    ),
    "chat_template": (
        "Покупатель {name} купил «{item}» за {cost} {currency}. Поблагодари его "
        "коротко и тепло, до 500 символов."
    ),
    "links_menu": [
        {"label": "🛡 Proxy6 (реф)", "url": "https://proxy6.net/?r=865936"},
    ],
    "operator_chat_id": None,
}

_io_lock = threading.RLock()


class AIError(Exception):
    """Ошибка обращения к AI-провайдеру (после исчерпания ретраев / постоянный сбой)."""


# =========================================================================
# Чистое ядро (pure core) — design §2
# =========================================================================

def _render_prompt(template: str, data: dict) -> str:
    """Последовательная подстановка `{k}` → str(v) (пусто для None/отсутствия).

    Validates: Requirements 3.2, 3.3
    """
    out = template if isinstance(template, str) else ""
    for k, v in (data or {}).items():
        out = out.replace("{" + str(k) + "}", str(v) if v is not None else "")
    return out


def _truncate(text: str, max_chars: int) -> str:
    """Идентичность при len <= max_chars, иначе обрезка так, что len <= max_chars
    с многоточием.

    Validates: Requirements 4.4
    """
    text = text if isinstance(text, str) else str(text or "")
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _should_reply(stars: float, min_stars: float, last_reply_ts: float,
                  now: float, cooldown: float) -> bool:
    """Конъюнктивный гейт: stars >= min_stars И (now - last_reply_ts) >= cooldown.

    Validates: Requirements 4.2, 4.6
    """
    return stars >= min_stars and (now - last_reply_ts) >= cooldown


def _fallback_iter(primary: str, fallbacks: list[str]) -> list[str]:
    """primary первым, затем уникальные fallback'и != primary (порядок сохранён).

    Validates: Requirements 2.3
    """
    out = [primary]
    seen = {primary}
    for m in (fallbacks or []):
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out


def _is_url_https(url: Any) -> bool:
    """True ровно когда url — строка и начинается с https://.

    Validates: Requirements 7.4
    """
    return isinstance(url, str) and url.startswith("https://")


def _mask_secret(s: Any, head: int = 4, tail: int = 2) -> str:
    """Маскировка секрета: первые head + … + последние tail символа.

    Validates: Requirements 8.3
    """
    if not s:
        return ""
    s = str(s)
    if len(s) <= head + tail:
        return "***"
    return s[:head] + "…" + s[-tail:]


def _html_escape(s: Any) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# =========================================================================
# Хранилище: load/save (атомарная запись tmp + os.replace, setdefault-миграция)
# =========================================================================

def _ensure_dir() -> None:
    PLUGIN_DIR.mkdir(parents=True, exist_ok=True)


def _load_json(path: Path, default):
    _ensure_dir()
    path = Path(path)
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
    path = Path(path)
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


def _clamp_int(v: Any, lo: int, hi: int, default: int) -> int:
    try:
        x = int(v)
    except Exception:
        return default
    return max(lo, min(hi, x))


def _load_settings() -> dict[str, Any]:
    """Загрузка настроек с setdefault-миграцией каждого ключа из design §1."""
    merged = json.loads(json.dumps(DEFAULT_SETTINGS))
    data = _load_json(SETTINGS_PATH, {})
    if isinstance(data, dict):
        for k, v in data.items():
            merged[k] = v

    # setdefault-миграция новых/отсутствующих ключей (старые конфиги)
    merged.setdefault("active_provider", DEFAULT_SETTINGS["active_provider"])
    merged.setdefault("primary_model", DEFAULT_SETTINGS["primary_model"])
    merged.setdefault("fallback_models", list(DEFAULT_SETTINGS["fallback_models"]))
    merged.setdefault("min_stars", DEFAULT_SETTINGS["min_stars"])
    merged.setdefault("reply_cooldown_sec", DEFAULT_SETTINGS["reply_cooldown_sec"])
    merged.setdefault("send_in_chat", DEFAULT_SETTINGS["send_in_chat"])
    merged.setdefault("prompt_template", DEFAULT_SETTINGS["prompt_template"])
    merged.setdefault("chat_template", DEFAULT_SETTINGS["chat_template"])
    merged.setdefault("links_menu", json.loads(json.dumps(DEFAULT_SETTINGS["links_menu"])))
    merged.setdefault("operator_chat_id", None)

    # провайдеры: гарантируем все три с base_url + api_key
    provs = merged.get("providers")
    if not isinstance(provs, dict):
        provs = {}
    for name, defcfg in DEFAULT_PROVIDERS.items():
        cur = provs.get(name)
        if not isinstance(cur, dict):
            cur = {}
        cur.setdefault("base_url", defcfg["base_url"])
        cur.setdefault("api_key", defcfg.get("api_key", ""))
        provs[name] = cur
    merged["providers"] = provs

    if merged.get("active_provider") not in DEFAULT_PROVIDERS:
        merged["active_provider"] = "openrouter"

    # нормализация типов / диапазонов
    merged["min_stars"] = _clamp_int(merged.get("min_stars"), *RANGE_MIN_STARS, DEFAULT_SETTINGS["min_stars"])
    merged["reply_cooldown_sec"] = _clamp_int(
        merged.get("reply_cooldown_sec"), *RANGE_COOLDOWN, DEFAULT_SETTINGS["reply_cooldown_sec"])
    merged["send_in_chat"] = bool(merged.get("send_in_chat"))
    fb = merged.get("fallback_models")
    if not isinstance(fb, list):
        fb = []
    merged["fallback_models"] = [str(m) for m in fb if m][:MAX_FALLBACK_MODELS]
    if not isinstance(merged.get("links_menu"), list):
        merged["links_menu"] = json.loads(json.dumps(DEFAULT_SETTINGS["links_menu"]))
    return merged


def _save_settings(s: dict[str, Any]) -> None:
    _save_json(SETTINGS_PATH, s)


def _load_reviews() -> list[dict]:
    data = _load_json(REVIEWS_PATH, [])
    return data if isinstance(data, list) else []


def _record_review(entry: dict) -> None:
    with _io_lock:
        items = _load_reviews()
        items.append(entry)
        if len(items) > 1000:
            items = items[-1000:]
        _save_json(REVIEWS_PATH, items)


def _load_state() -> dict:
    data = _load_json(STATE_PATH, {})
    return data if isinstance(data, dict) else {}


def _save_state(d: dict) -> None:
    _save_json(STATE_PATH, d)


def _get_last_reply_ts(buyer_key: Any) -> float:
    st = _load_state().get("last_reply", {})
    try:
        return float(st.get(str(buyer_key), 0.0))
    except Exception:
        return 0.0


def _set_last_reply_ts(buyer_key: Any, ts: float) -> None:
    with _io_lock:
        st = _load_state()
        last = st.get("last_reply")
        if not isinstance(last, dict):
            last = {}
        last[str(buyer_key)] = float(ts)
        st["last_reply"] = last
        _save_state(st)


# =========================================================================
# Повторное использование ключей из configs/ai_chat_plugin.cfg (Req 1.3)
# =========================================================================

def _read_aichat_cfg_key(provider: str) -> str:
    """Читает API-ключ для provider из общего configs/ai_chat_plugin.cfg.

    Сначала ищем секцию [Providers] с ключом-именем провайдера; для openrouter
    дополнительно поддерживаем легаси-поле [General] openrouter_api_key. Файл
    только читается, никогда не пишется этим плагином.
    """
    try:
        if not os.path.exists(AICHAT_CFG_PATH):
            return ""
        cfg = configparser.ConfigParser()
        cfg.read(AICHAT_CFG_PATH, encoding="utf-8")
        if cfg.has_section("Providers"):
            val = cfg.get("Providers", provider, fallback="")
            if val and val != "YOUR_API_KEY_HERE":
                return val
        if provider == "openrouter" and cfg.has_section("General"):
            val = cfg.get("General", "openrouter_api_key", fallback="")
            if val and val != "YOUR_API_KEY_HERE":
                return val
    except Exception:
        logger.debug(f"{LOGGER_PREFIX} ошибка чтения {AICHAT_CFG_PATH}", exc_info=True)
    return ""


def _effective_api_key(settings: dict, provider: str) -> str:
    """Ключ из собственных настроек; если пуст — из общего ai_chat_plugin.cfg."""
    own = ((settings.get("providers", {}) or {}).get(provider, {}) or {}).get("api_key", "")
    if own:
        return own
    return _read_aichat_cfg_key(provider)


# =========================================================================
# AIClient: один интерфейс, три адаптера (design §3)
# =========================================================================

class AIClient:
    """Единый клиент. `complete(model, prompt, max_tokens)` — единственный вызов.

    Провайдер определяется из active_provider: для openrouter допустима любая
    модель; для openai/gemini — нативный endpoint этого провайдера (Req 1.5).
    """

    def __init__(self, providers: dict, active_provider: str = "openrouter",
                 key_resolver=None, timeout: int = HTTP_TIMEOUT, session=None):
        self.providers = providers or {}
        self.active_provider = active_provider if active_provider in DEFAULT_PROVIDERS else "openrouter"
        self._resolve_key = key_resolver or (
            lambda p: (self.providers.get(p, {}) or {}).get("api_key", ""))
        self.timeout = timeout
        self.session = session or requests

    def _provider_for(self, model: str) -> str:
        # openrouter принимает любые модели; openai/gemini — прямой endpoint.
        if self.active_provider in ("openai", "gemini"):
            return self.active_provider
        return "openrouter"

    def _base_url(self, provider: str) -> str:
        cfg = self.providers.get(provider, {}) or {}
        return (cfg.get("base_url") or DEFAULT_PROVIDERS[provider]["base_url"]).rstrip("/")

    def complete(self, model: str, prompt: str, max_tokens: int = 800) -> str:
        provider = self._provider_for(model)
        adapters = {
            "openrouter": self._adapter_openrouter,
            "openai": self._adapter_openai,
            "gemini": self._adapter_gemini,
        }
        return adapters[provider](model, prompt, max_tokens)

    # --- HTTP с ограниченными ретраями (Req 2.3) ---

    def _post_json(self, provider: str, url: str, headers: dict, body: dict) -> dict:
        last_exc: Exception | None = None
        for attempt in range(MODEL_RETRIES):
            try:
                resp = self.session.post(url, headers=headers, json=body, timeout=self.timeout)
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                last_exc = e
                if attempt < MODEL_RETRIES - 1:
                    time.sleep(RETRY_BACKOFF_SEC * (attempt + 1))
                    continue
                raise AIError(f"{provider}: сетевая ошибка: {e}") from e
            except Exception as e:
                raise AIError(f"{provider}: ошибка запроса: {e}") from e

            code = getattr(resp, "status_code", 200)
            if code == 429 or code >= 500:
                last_exc = AIError(f"{provider}: HTTP {code}")
                if attempt < MODEL_RETRIES - 1:
                    time.sleep(RETRY_BACKOFF_SEC * (attempt + 1))
                    continue
                raise last_exc
            if code >= 400:
                # постоянная ошибка (4xx, кроме 429) — без ретраев
                raise AIError(f"{provider}: HTTP {code}")
            try:
                return resp.json()
            except Exception as e:
                raise AIError(f"{provider}: некорректный JSON: {e}") from e
        raise AIError(f"{provider}: исчерпаны попытки ({last_exc})")

    # --- адаптеры ---

    def _openai_compatible(self, provider: str, model: str, prompt: str, max_tokens: int) -> str:
        api_key = self._resolve_key(provider)
        url = f"{self._base_url(provider)}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
        }
        data = self._post_json(provider, url, headers, body)
        try:
            return data["choices"][0]["message"]["content"]
        except Exception as e:
            raise AIError(f"{provider}: неожиданный формат ответа: {e}") from e

    def _adapter_openrouter(self, model: str, prompt: str, max_tokens: int) -> str:
        return self._openai_compatible("openrouter", model, prompt, max_tokens)

    def _adapter_openai(self, model: str, prompt: str, max_tokens: int) -> str:
        return self._openai_compatible("openai", model, prompt, max_tokens)

    def _adapter_gemini(self, model: str, prompt: str, max_tokens: int) -> str:
        api_key = self._resolve_key("gemini")
        url = f"{self._base_url('gemini')}/models/{model}:generateContent?key={api_key}"
        headers = {"Content-Type": "application/json"}
        body = {"contents": [{"parts": [{"text": prompt}]}]}
        data = self._post_json("gemini", url, headers, body)
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            raise AIError(f"gemini: неожиданный формат ответа: {e}") from e


def _make_client(settings: dict) -> AIClient:
    return AIClient(
        settings.get("providers", {}),
        settings.get("active_provider", "openrouter"),
        key_resolver=lambda p: _effective_api_key(settings, p),
    )


# =========================================================================
# Детекция события отзыва (изолировано → легко заменить)
# =========================================================================

# ID заказа в системном сообщении FunPay: "#ABCDEF12"
_ORDER_ID_RE = re.compile(r"#([A-Z0-9]{8})")

# Имена типов сообщений-отзывов (по .name enum'а MessageTypes Cardinal)
_FEEDBACK_TYPE_NAMES = {"NEW_FEEDBACK", "FEEDBACK_CHANGED"}


def _message_type_name(message: Any) -> str:
    """Возвращает имя типа сообщения как строку (устойчиво к enum/строке)."""
    t = getattr(message, "type", None)
    if t is None:
        return ""
    return getattr(t, "name", str(t))


def _detect_feedback(message: Any) -> str | None:
    """Если сообщение — новый/изменённый отзыв, вернуть order_id (без '#'),
    иначе None. Точка изоляции детекции — единственное место, знающее формат
    события Cardinal.
    """
    name = _message_type_name(message)
    if name not in _FEEDBACK_TYPE_NAMES:
        # подстраховка: некоторые сборки кладут тип как "MessageTypes.NEW_FEEDBACK"
        if not any(name.endswith(n) for n in _FEEDBACK_TYPE_NAMES):
            return None
    m = _ORDER_ID_RE.search(str(message))
    if not m:
        return None
    return m.group(1)


def _build_render_data(order: Any) -> dict:
    """Собирает плейсхолдеры промпта из заказа и отзыва (Req 3.2)."""
    review = getattr(order, "review", None)
    cost = getattr(order, "sum", None)
    if cost is None:
        cost = getattr(order, "price", None)
    subcat = getattr(order, "subcategory", None)
    return {
        "name": getattr(order, "buyer_username", None),
        "item": getattr(order, "title", None),
        "cost": cost,
        "rating": getattr(review, "stars", None) if review is not None else None,
        "text": getattr(review, "text", None) if review is not None else None,
        "currency": getattr(order, "currency", None),
        "seller": getattr(order, "seller_username", None),
        "category": getattr(subcat, "name", None) if subcat is not None else None,
    }


def _buyer_key(order: Any) -> str:
    bid = getattr(order, "buyer_id", None)
    if bid is not None:
        return str(bid)
    return str(getattr(order, "buyer_username", "") or "")


# =========================================================================
# Уведомления оператору
# =========================================================================

def _notify_operator(cardinal: "Cardinal", text: str) -> None:
    s = _load_settings()
    chat_id = s.get("operator_chat_id")
    if not chat_id:
        return
    try:
        cardinal.telegram.bot.send_message(chat_id, text, parse_mode="HTML")
    except Exception:
        logger.debug(f"{LOGGER_PREFIX} notify_operator failed", exc_info=True)


def _send_buyer(cardinal: "Cardinal", chat_id: Any, text: str) -> None:
    try:
        cardinal.send_message(chat_id, text)
    except Exception:
        logger.debug(f"{LOGGER_PREFIX} send_buyer failed", exc_info=True)


# =========================================================================
# Генерация ответа: primary + fallback (Req 2.3, 2.5)
# =========================================================================

def _generate_reply(client: AIClient, settings: dict, prompt: str,
                    max_tokens: int) -> tuple[str | None, str | None]:
    """Перебирает _fallback_iter; на первой успешной модели возвращает
    (text, model_used). При исчерпании — (None, None)."""
    models = _fallback_iter(settings.get("primary_model", ""), settings.get("fallback_models", []))
    for model in models:
        if not model:
            continue
        try:
            text = client.complete(model, prompt, max_tokens=max_tokens)
            if text:
                return text, model
            logger.info(f"{LOGGER_PREFIX} модель {model} вернула пустой ответ")
        except AIError as e:
            logger.info(f"{LOGGER_PREFIX} модель {model} не ответила: {e}")
        except Exception as e:
            logger.info(f"{LOGGER_PREFIX} модель {model} ошибка: {e}")
    return None, None


def _send_review_with_retries(cardinal: "Cardinal", order_id: Any, text: str) -> bool:
    """Отправка ответа на отзыв с ограниченными ретраями (Req 4.5)."""
    last_exc: Exception | None = None
    for attempt in range(SEND_REVIEW_RETRIES):
        try:
            cardinal.account.send_review(order_id, rating=None, text=text)
            return True
        except Exception as e:
            last_exc = e
            logger.info(f"{LOGGER_PREFIX} send_review попытка {attempt + 1} не удалась: {e}")
            if attempt < SEND_REVIEW_RETRIES - 1:
                time.sleep(RETRY_BACKOFF_SEC * (attempt + 1))
    logger.warning(f"{LOGGER_PREFIX} send_review окончательно не удался: {last_exc}")
    return False


# =========================================================================
# Основной обработчик отзыва
# =========================================================================

def _process_feedback(cardinal: "Cardinal", order_id: str, *, chat_id: Any = None) -> None:
    settings = _load_settings()
    try:
        order = cardinal.account.get_order(order_id)
    except Exception as e:
        logger.warning(f"{LOGGER_PREFIX} не удалось получить заказ {order_id}: {e}")
        return

    review = getattr(order, "review", None)
    stars = getattr(review, "stars", None) if review is not None else None
    try:
        stars_val = int(stars)
    except Exception:
        logger.info(f"{LOGGER_PREFIX} у заказа {order_id} нет оценки — пропуск")
        return

    min_stars = int(settings.get("min_stars", 4))
    cooldown = float(settings.get("reply_cooldown_sec", 60))
    buyer_key = _buyer_key(order)
    now = time.time()
    last_ts = _get_last_reply_ts(buyer_key)

    oid = getattr(order, "id", order_id)
    buyer_name = getattr(order, "buyer_username", "") or "—"
    title = getattr(order, "title", "") or ""

    # Негативный отзыв: уведомить оператора, без вызова AI (Req 4.3)
    if stars_val < min_stars:
        review_text = getattr(review, "text", "") if review is not None else ""
        _notify_operator(
            cardinal,
            "⚠️ <b>Негативный отзыв</b>\n"
            f"🧾 Заказ: #{_html_escape(oid)}\n"
            f"👤 Покупатель: {_html_escape(buyer_name)}\n"
            f"⭐ Оценка: {stars_val}/5 (порог {min_stars})\n"
            f"💬 Отзыв: {_html_escape(review_text)}\n"
            "Авто-ответ не отправлен — ответьте вручную.",
        )
        return

    # Кулдаун на покупателя (Req 4.6)
    if not _should_reply(stars_val, min_stars, last_ts, now, cooldown):
        logger.info(f"{LOGGER_PREFIX} кулдаун для покупателя {buyer_key} — пропуск заказа {oid}")
        return

    client = _make_client(settings)
    prompt = _render_prompt(settings.get("prompt_template", ""), _build_render_data(order))

    t0 = time.time()
    reply, model_used = _generate_reply(client, settings, prompt, max_tokens=REVIEW_MAX_CHARS)
    latency_ms = int((time.time() - t0) * 1000)

    if not reply:
        _notify_operator(
            cardinal,
            "❌ <b>AIReviews</b>: не удалось сгенерировать ответ на отзыв\n"
            f"🧾 Заказ: #{_html_escape(oid)} ⭐ {stars_val}/5\n"
            "Все модели недоступны — ответ не отправлен.",
        )
        return

    reply = _truncate(reply, REVIEW_MAX_CHARS)

    if not _send_review_with_retries(cardinal, oid, reply):
        _notify_operator(
            cardinal,
            "❌ <b>AIReviews</b>: не удалось отправить ответ на отзыв на FunPay\n"
            f"🧾 Заказ: #{_html_escape(oid)}\n"
            f"📝 Текст: {_html_escape(reply)}",
        )
        return

    _set_last_reply_ts(buyer_key, now)
    _record_review({
        "order_id": str(oid),
        "stars": stars_val,
        "model_used": model_used,
        "prompt_chars": len(prompt),
        "reply_chars": len(reply),
        "latency_ms": latency_ms,
        "ts": now,
    })
    logger.info(f"{LOGGER_PREFIX} ответ на отзыв заказа {oid} отправлен ({model_used})")

    # Опциональное тёплое сообщение покупателю в чат (Req 5)
    if settings.get("send_in_chat"):
        target_chat = chat_id if chat_id is not None else getattr(order, "chat_id", None)
        if target_chat is not None:
            _maybe_send_chat_thanks(cardinal, settings, client, order, target_chat)


def _maybe_send_chat_thanks(cardinal: "Cardinal", settings: dict, client: AIClient,
                            order: Any, chat_id: Any) -> None:
    """Рендерит chat_template, запрашивает завершение до 500 символов и шлёт в чат."""
    chat_prompt = _render_prompt(settings.get("chat_template", ""), _build_render_data(order))
    reply, _model = _generate_reply(client, settings, chat_prompt, max_tokens=CHAT_MAX_CHARS)
    if not reply:
        logger.info(f"{LOGGER_PREFIX} не удалось сгенерировать сообщение в чат")
        return
    _send_buyer(cardinal, chat_id, _truncate(reply, CHAT_MAX_CHARS))


def _on_new_message(cardinal: "Cardinal", event: Any, *args) -> None:
    """BIND_TO_NEW_MESSAGE: ловим события NEW_FEEDBACK / FEEDBACK_CHANGED."""
    try:
        message = getattr(event, "message", None)
        if message is None:
            return
        order_id = _detect_feedback(message)
        if not order_id:
            return
        chat_id = getattr(message, "chat_id", None)
        _process_feedback(cardinal, order_id, chat_id=chat_id)
    except Exception:
        logger.warning(f"{LOGGER_PREFIX} ошибка обработки сообщения", exc_info=True)


# =========================================================================
# Telegram UI (русский) — design + Req 6, 7
# =========================================================================

CBP = f"aireviews:{UUID[:8]}"
CBT_HOME = f"{CBP}:home"
CBT_PROVIDER = f"{CBP}:provider"
CBT_PROVIDER_SET = f"{CBP}:pset"            # + :name
CBT_PROVIDER_KEY = f"{CBP}:pkey"            # + :name
CBT_EDIT_PRIMARY = f"{CBP}:primary"
CBT_EDIT_FALLBACK = f"{CBP}:fallback"
CBT_EDIT_PROMPT = f"{CBP}:prompt"
CBT_EDIT_CHAT_TPL = f"{CBP}:chattpl"
CBT_TOGGLE_CHAT = f"{CBP}:togglechat"
CBT_EDIT_MIN_STARS = f"{CBP}:minstars"
CBT_EDIT_COOLDOWN = f"{CBP}:cooldown"
CBT_PREVIEW = f"{CBP}:preview"
CBT_HISTORY = f"{CBP}:history"
CBT_LINKS = f"{CBP}:links"
CBT_LINK_ADD = f"{CBP}:linkadd"
CBT_LINK_DEL = f"{CBP}:linkdel"             # + :index

_PROVIDER_LABELS = {
    "openrouter": "OpenRouter",
    "openai": "OpenAI",
    "gemini": "Gemini",
}


def _home_text() -> str:
    s = _load_settings()
    prov = s.get("active_provider", "openrouter")
    key_src = "свой" if ((s.get("providers", {}).get(prov, {}) or {}).get("api_key")) else (
        "из ai_chat_plugin.cfg" if _read_aichat_cfg_key(prov) else "не задан")
    fb = s.get("fallback_models", [])
    return (
        f"<b>🤖 AIReviews v{VERSION}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🧠 Провайдер: <code>{_PROVIDER_LABELS.get(prov, prov)}</code> (ключ: {key_src})\n"
        f"🎯 Основная модель: <code>{_html_escape(s.get('primary_model'))}</code>\n"
        f"🔁 Резервные модели: <code>{len(fb)}</code>\n"
        f"⭐ Мин. оценка для авто-ответа: <code>{s.get('min_stars')}</code>\n"
        f"⏱ Кулдаун на покупателя: <code>{s.get('reply_cooldown_sec')}</code> сек\n"
        f"💬 Сообщение в чат: {'🟢' if s.get('send_in_chat') else '🔴'}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Выберите раздел настроек:"
    )


def _home_kb() -> "K":
    s = _load_settings()
    kb = K(row_width=2)
    kb.row(B("🧠 Провайдер", callback_data=CBT_PROVIDER),
           B("🎯 Основная модель", callback_data=CBT_EDIT_PRIMARY))
    kb.row(B("🔁 Резервные модели", callback_data=CBT_EDIT_FALLBACK),
           B("⭐ Мин. оценка", callback_data=CBT_EDIT_MIN_STARS))
    kb.row(B("📝 Промпт отзыва", callback_data=CBT_EDIT_PROMPT),
           B("💬 Промпт чата", callback_data=CBT_EDIT_CHAT_TPL))
    kb.row(B(("💬 В чат: 🟢" if s.get("send_in_chat") else "💬 В чат: 🔴"), callback_data=CBT_TOGGLE_CHAT),
           B("⏱ Кулдаун", callback_data=CBT_EDIT_COOLDOWN))
    kb.row(B("👁 Превью промпта", callback_data=CBT_PREVIEW),
           B("🗂 Последние ответы", callback_data=CBT_HISTORY))
    kb.row(B("🔗 Полезные ссылки", callback_data=CBT_LINKS),
           B("💛 Донат", callback_data=f"{DONATION_CALLBACK_PREFIX}:donate"))
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

    def _edit_or_send(call, text: str, kb) -> None:
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.id,
                                  parse_mode="HTML", reply_markup=kb)
        except Exception:
            bot.send_message(call.message.chat.id, text, parse_mode="HTML", reply_markup=kb)

    def _edit_home(call) -> None:
        _edit_or_send(call, _home_text(), _home_kb())

    def open_settings_cb(call) -> None:
        _persist_op(call.message.chat.id)
        _edit_home(call)
        _answer(call)

    def home_cb(call) -> None:
        _edit_home(call)
        _answer(call)

    # ---------- провайдер ----------
    def provider_cb(call) -> None:
        s = _load_settings()
        cur = s.get("active_provider", "openrouter")
        lines = ["<b>🧠 AI-провайдер</b>", "", "Выберите активного провайдера и задайте ключ.",
                 "Если ключ пуст — он берётся из <code>configs/ai_chat_plugin.cfg</code>.", ""]
        kb = K(row_width=1)
        for name in DEFAULT_PROVIDERS:
            icon = "✅ " if name == cur else ""
            own = (s.get("providers", {}).get(name, {}) or {}).get("api_key", "")
            shown = _mask_secret(own) if own else (
                f"{_mask_secret(_read_aichat_cfg_key(name))} (cfg)" if _read_aichat_cfg_key(name) else "—")
            lines.append(f"{icon}<b>{_PROVIDER_LABELS[name]}</b>: ключ <code>{shown}</code>")
            kb.add(B(f"{icon}{_PROVIDER_LABELS[name]}", callback_data=f"{CBT_PROVIDER_SET}:{name}"))
            kb.add(B(f"🔑 Ключ {_PROVIDER_LABELS[name]}", callback_data=f"{CBT_PROVIDER_KEY}:{name}"))
        kb.add(B("◀️ Назад", callback_data=CBT_HOME))
        _edit_or_send(call, "\n".join(lines), kb)
        _answer(call)

    def provider_set_cb(call) -> None:
        name = call.data.split(":")[-1]
        if name in DEFAULT_PROVIDERS:
            s = _load_settings()
            s["active_provider"] = name
            _save_settings(s)
            _answer(call, f"✅ {_PROVIDER_LABELS[name]}")
        provider_cb(call)

    def provider_key_cb(call) -> None:
        name = call.data.split(":")[-1]
        if name not in DEFAULT_PROVIDERS:
            return _answer(call, "неизвестный провайдер")
        msg = bot.send_message(call.message.chat.id,
                               f"🔑 Введите API-ключ для {_PROVIDER_LABELS[name]} (или /cancel):")
        _answer(call)

        def handle(m) -> None:
            t = (m.text or "").strip()
            if t.startswith("/"):
                return
            s = _load_settings()
            s.setdefault("providers", {}).setdefault(name, {})["api_key"] = t
            _save_settings(s)
            bot.reply_to(m, f"✅ Ключ {_PROVIDER_LABELS[name]} сохранён: <code>{_mask_secret(t)}</code>",
                         parse_mode="HTML")
        bot.register_next_step_handler(msg, handle)

    # ---------- редакторы текстовых полей ----------
    def _make_text_editor(key: str, label: str, max_len: int):
        def cb(call) -> None:
            s = _load_settings()
            cur = s.get(key, "")
            msg = bot.send_message(
                call.message.chat.id,
                f"✏️ {label}\nТекущее значение:\n<code>{_html_escape(str(cur)[:500])}</code>\n\n"
                f"Введите новое значение (до {max_len} символов):",
                parse_mode="HTML")
            _answer(call)

            def handle(m) -> None:
                t = (m.text or "").strip()
                if not t:
                    return bot.reply_to(m, "❌ Пусто. Прежнее значение сохранено.")
                if len(t) > max_len:
                    return bot.reply_to(m, f"❌ Слишком длинно (макс. {max_len}). Прежнее значение сохранено.")
                s2 = _load_settings()
                s2[key] = t
                _save_settings(s2)
                bot.reply_to(m, "✅ Обновлено.")
            bot.register_next_step_handler(msg, handle)
        return cb

    # ---------- числовые редакторы с проверкой диапазона (Req 6.3) ----------
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
                    bot.reply_to(m, f"❌ «{label}»: вне диапазона ({lo}–{hi}). Прежнее значение сохранено.")
                    return
                s = _load_settings()
                s[key] = v
                _save_settings(s)
                bot.reply_to(m, f"✅ Обновлено: <code>{v}</code>", parse_mode="HTML")
            bot.register_next_step_handler(msg, handle)
        return cb

    def toggle_chat_cb(call) -> None:
        s = _load_settings()
        s["send_in_chat"] = not s.get("send_in_chat", False)
        _save_settings(s)
        _answer(call, "💬 " + ("вкл" if s["send_in_chat"] else "выкл"))
        _edit_home(call)

    # ---------- резервные модели (0–10) ----------
    def fallback_cb(call) -> None:
        s = _load_settings()
        cur = s.get("fallback_models", [])
        text = ("<b>🔁 Резервные модели</b>\n\n"
                "Текущий список:\n" + ("\n".join(f"{i + 1}. <code>{_html_escape(m)}</code>"
                                                  for i, m in enumerate(cur)) or "(пусто)") +
                "\n\nОтправьте список моделей через запятую (0–10, пусто — очистить).")
        kb = K().add(B("◀️ Назад", callback_data=CBT_HOME))
        _edit_or_send(call, text, kb)
        msg = bot.send_message(call.message.chat.id, "Введите модели через запятую (или /cancel):")
        _answer(call)

        def handle(m) -> None:
            t = (m.text or "").strip()
            if t.startswith("/"):
                return
            models = [x.strip() for x in t.split(",") if x.strip()]
            if len(models) > MAX_FALLBACK_MODELS:
                return bot.reply_to(
                    m, f"❌ «Резервные модели»: максимум {MAX_FALLBACK_MODELS}. Прежний список сохранён.")
            if any(len(x) > MAX_MODEL_LEN for x in models):
                return bot.reply_to(
                    m, f"❌ Имя модели длиннее {MAX_MODEL_LEN} символов. Прежний список сохранён.")
            s2 = _load_settings()
            s2["fallback_models"] = models
            _save_settings(s2)
            bot.reply_to(m, f"✅ Резервных моделей: {len(models)}")
        bot.register_next_step_handler(msg, handle)

    def edit_primary_cb(call) -> None:
        msg = bot.send_message(call.message.chat.id,
                               f"🎯 Введите основную модель (до {MAX_MODEL_LEN} символов):")
        _answer(call)

        def handle(m) -> None:
            t = (m.text or "").strip()
            if not t:
                return bot.reply_to(m, "❌ Пусто. Прежнее значение сохранено.")
            if len(t) > MAX_MODEL_LEN:
                return bot.reply_to(m, f"❌ Слишком длинно (макс. {MAX_MODEL_LEN}). Прежнее значение сохранено.")
            s = _load_settings()
            s["primary_model"] = t
            _save_settings(s)
            bot.reply_to(m, f"✅ Основная модель: <code>{_html_escape(t)}</code>", parse_mode="HTML")
        bot.register_next_step_handler(msg, handle)

    # ---------- превью промпта на образце заказа (Req 3.4) ----------
    def preview_cb(call) -> None:
        s = _load_settings()
        sample = {
            "name": "Иван", "item": "Аккаунт Steam", "cost": 499, "rating": 5,
            "text": "Всё супер, спасибо!", "currency": "RUB",
            "seller": "MyShop", "category": "Игровые ценности",
        }
        rendered = _render_prompt(s.get("prompt_template", ""), sample)
        text = ("<b>👁 Превью промпта</b>\n(на образце заказа)\n\n"
                f"<code>{_html_escape(rendered)}</code>")
        kb = K().add(B("◀️ Назад", callback_data=CBT_HOME))
        _edit_or_send(call, text, kb)
        _answer(call)

    # ---------- история ответов (последние 20) ----------
    def history_cb(call) -> None:
        items = _load_reviews()[-20:][::-1]
        lines = ["<b>🗂 Последние ответы</b>", ""]
        if not items:
            lines.append("(пусто)")
        for r in items:
            lines.append(
                f"• #{_html_escape(r.get('order_id'))} ⭐{r.get('stars')} "
                f"<code>{_html_escape(r.get('model_used'))}</code> "
                f"{r.get('reply_chars')} симв., {r.get('latency_ms')} мс")
        kb = K().add(B("◀️ Назад", callback_data=CBT_HOME))
        _edit_or_send(call, "\n".join(lines), kb)
        _answer(call)

    # ---------- полезные ссылки (CRUD, https-валидация) ----------
    def links_cb(call) -> None:
        s = _load_settings()
        links = s.get("links_menu", [])
        kb = K(row_width=1)
        if not links:
            text = "🔗 <b>Полезные ссылки</b>\n\nСписок пуст. Добавьте первую ссылку."
            kb.add(B("➕ Добавить первую ссылку", callback_data=CBT_LINK_ADD))
            kb.add(B("◀️ Назад", callback_data=CBT_HOME))
            _edit_or_send(call, text, kb)
            return _answer(call)
        for item in links:
            url = item.get("url", "")
            if _is_url_https(url):
                kb.add(B(item.get("label", url), url=url))
        for i, item in enumerate(links):
            kb.add(B(f"🗑 Удалить: {item.get('label', '')[:24]}", callback_data=f"{CBT_LINK_DEL}:{i}"))
        kb.add(B("➕ Добавить ссылку", callback_data=CBT_LINK_ADD))
        kb.add(B("◀️ Назад", callback_data=CBT_HOME))
        _edit_or_send(call, "🔗 <b>Полезные ссылки</b> (реф-ссылки и прочее):", kb)
        _answer(call)

    def link_add_cb(call) -> None:
        msg = bot.send_message(call.message.chat.id,
                               "➕ Введите ссылку в формате: Название | https://...")
        _answer(call)

        def handle(m) -> None:
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
        idx = call.data.split(":")[-1]
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
    tg.cbq_handler(provider_cb, lambda c: c.data == CBT_PROVIDER)
    tg.cbq_handler(provider_set_cb, lambda c: (c.data or "").startswith(f"{CBT_PROVIDER_SET}:"))
    tg.cbq_handler(provider_key_cb, lambda c: (c.data or "").startswith(f"{CBT_PROVIDER_KEY}:"))
    tg.cbq_handler(edit_primary_cb, lambda c: c.data == CBT_EDIT_PRIMARY)
    tg.cbq_handler(fallback_cb, lambda c: c.data == CBT_EDIT_FALLBACK)
    tg.cbq_handler(_make_text_editor("prompt_template", "Промпт ответа на отзыв", 2000),
                   lambda c: c.data == CBT_EDIT_PROMPT)
    tg.cbq_handler(_make_text_editor("chat_template", "Промпт сообщения в чат", 2000),
                   lambda c: c.data == CBT_EDIT_CHAT_TPL)
    tg.cbq_handler(toggle_chat_cb, lambda c: c.data == CBT_TOGGLE_CHAT)
    tg.cbq_handler(_make_numeric_editor("min_stars", *RANGE_MIN_STARS, "⭐ Мин. оценка для авто-ответа"),
                   lambda c: c.data == CBT_EDIT_MIN_STARS)
    tg.cbq_handler(_make_numeric_editor("reply_cooldown_sec", *RANGE_COOLDOWN, "⏱ Кулдаун на покупателя (сек)"),
                   lambda c: c.data == CBT_EDIT_COOLDOWN)
    tg.cbq_handler(preview_cb, lambda c: c.data == CBT_PREVIEW)
    tg.cbq_handler(history_cb, lambda c: c.data == CBT_HISTORY)
    tg.cbq_handler(links_cb, lambda c: c.data == CBT_LINKS)
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

    # /aireviews — открыть меню настроек из чата
    def cmd_open(m):
        try:
            bot.send_message(m.chat.id, _home_text(), reply_markup=_home_kb(), parse_mode="HTML")
        except Exception:
            logger.exception("send menu failed")
    try:
        tg.msg_handler(cmd_open, commands=["aireviews"])
    except Exception:
        logger.exception("msg_handler registration failed")
    try:
        cardinal.add_telegram_commands(UUID, [
            ("aireviews", "AI Reviews: открыть меню", True),
        ])
    except Exception:
        logger.exception("add_telegram_commands failed")

    logger.info(f"{LOGGER_PREFIX} v{VERSION} запущен")


BIND_TO_PRE_INIT = [init]
BIND_TO_NEW_MESSAGE = [_on_new_message]
BIND_TO_DELETE = None


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
