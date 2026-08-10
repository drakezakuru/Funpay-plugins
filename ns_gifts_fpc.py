"""
NS.Gifts plugin for FunPayCardinal
==================================

Автодоставка кодов и других товаров через API ns.gifts (https://wholesale.ns.gifts/api-docs),
автоответы команд из чата FunPay, авто-обновление цен лотов с наценкой,
и удобная панель управления в Telegram с inline-кнопками.

См. README.md для установки и настройки.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid as uuid_lib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import requests
import telebot
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
DONATION_CALLBACK_PREFIX = "nsg_dn"    # префикс колбэков кнопок баннера
DONATION_PLUGIN_NAME = "NS.Gifts AutoDelivery"  # имя плагина в шапке баннера

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
        f"💳 Карта (европейская): <code>{{_d['card']}}</code>\n"
        f"💎 Gram (TON): <code>{{_d['ton']}}</code>\n"
        f"💵 USDT (TON): <code>{{_d['usdt_ton']}}</code>\n"
        f"🪙 USDT (TRC20): <code>{{_d['usdt']}}</code>\n"
        f"📮 Пожелания и фичи: {{_d['contact']}}\n\n"
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
            if now.hour == DONATION_DAILY_HOUR and now.minute == 0:
                _send_donation_banner(cardinal)
        except Exception:
            pass
        time.sleep(60)


# =========================================================================
# Метаданные плагина (обязательные для FPC)
# =========================================================================

NAME = "NS.Gifts AutoDelivery"
VERSION = "0.5.1"
DESCRIPTION = (
    "Автовыдача кодов и Steam-пополнений через ns.gifts, авто-обновление цен с "
    "наценкой, чат-команды, Telegram inline-keyboard панель управления, "
    "команды /nsgifts_guide и /nsgifts_test, рабочие часы, описания настроек."
)
CREDITS = "@drakelovc"
UUID = "8b4e2c9a-7f31-4a55-9d8b-1e2f6a7c5b03"
SETTINGS_PAGE = True

BIND_TO_DELETE = None

logger = logging.getLogger(f"FPC.{__name__}")


# =========================================================================
# Хранилище: пути, дефолты
# =========================================================================

STORAGE_DIR = Path(f"storage/plugins/{UUID}")
SECRETS_FILE = STORAGE_DIR / "secrets.json"
SETTINGS_FILE = STORAGE_DIR / "settings.json"
MAPPINGS_FILE = STORAGE_DIR / "mappings.json"
DELIVERY_LOG = STORAGE_DIR / "deliveries.log"
DB_FILE = STORAGE_DIR / "db.sqlite"

DEFAULT_SECRETS: dict[str, Any] = {
    "user_id": 0,
    "login": "",
    "password": "",
    "api_secret": "",
    "base_url": "https://api.ns.gifts",
    "totp_code": "",  # если включена 2FA на покупки, ставим сюда временно
}

DEFAULT_SETTINGS: dict[str, Any] = {
    "autodelivery_enabled": True,
    "autoprice_enabled": False,
    "chat_commands_enabled": True,
    "working_hours_enabled": False,
    "working_hours_start": 9,
    "working_hours_end": 23,
    "global_markup_percent": 30.0,
    "fixed_markup": 0.0,                # в валюте лота
    "fixed_markup_rub": 0.0,            # legacy/back-compat
    "min_price": 1.0,                   # в валюте лота
    "min_price_rub": 1.0,               # legacy/back-compat
    "default_lot_currency": "rub",      # rub | uah | kzt | usd | eur
    "usd_to_rub_rate": 0.0,             # 0 = авто из NS exchange_rate
    "usd_to_uah_rate": 0.0,             # 0 = авто из NS exchange_rate
    "usd_to_kzt_rate": 0.0,             # 0 = авто из NS exchange_rate
    "usd_to_eur_rate": 0.92,            # ручной (NS не даёт)
    "usd_to_rub_fallback": 100.0,
    "usd_to_uah_fallback": 41.0,
    "usd_to_kzt_fallback": 480.0,
    "price_sync_interval_min": 30,
    "cmd_balance_aliases": ["!баланс", "!balance"],
    "cmd_price_aliases": ["!прайс", "!price"],
    "cmd_status_aliases": ["!status", "!статус"],
    "deliver_attempts": 3,
    "low_balance_threshold_usd": 5.0,
    "notify_chat_id": 0,
    "auto_disable_oos_lots": True,
    # Steam-login confirmation
    "confirm_steam_login": True,
    "confirm_timeout_min": 5,            # ждём ответ N минут
    "confirm_retries": 1,                # сколько раз повторить запрос
    "yes_aliases": ["да", "д", "yes", "y", "+", "ок", "ok", "подтверждаю", "confirm"],
    "no_aliases": ["нет", "н", "no", "n", "-", "отмена", "cancel"],
    # названия полей FunPay-заказа, в которых лежит Steam-логин
    "steam_login_field_names": ["логин steam", "steam login", "логін steam", "account", "аккаунт"],
    # Защита от убытков: отменяем выдачу, если цена лота < себестоимость × (1 + safety%)
    "loss_protection_enabled": True,
    "loss_safety_margin_percent": 0.0,    # 0% = отмена при цене ниже ровной себестоимости
    # Идемпотентность: повторные попытки при прошлых статусах
    "retry_failed_orders": False,        # True → разрешить повтор выдачи для failed-записей
    # Авто-активация лотов (включаем при появлении в stock, если мы же и выключали)
    "auto_activate_back_in_stock": True,
    # Шаблоны сообщений покупателю (формат — Python str.format)
    "tpl_success": "✅ Заказ #{order_id} ({service_name})\nВаш товар:\n{pins}",
    "tpl_success_async": "⏳ Заказ #{order_id} в обработке у поставщика, напишу как только выдадут.",
    "tpl_failed": "❌ Не удалось выдать заказ #{order_id}: {error}. Продавец уведомлён.",
    "tpl_insufficient": "⚠️ На балансе поставщика недостаточно средств, продавец уведомлён.",
    "tpl_login_request": "Чтобы пополнить Steam, пришлите ваш логин Steam одним сообщением. С ним я перепроверю и запрошу подтверждение.",
    "tpl_login_invalid": "Не нашёл Steam-логин «{login}» ({status}). Пришлите логин одним сообщением.",
    "tpl_confirm_request": "{prefix}Логин Steam: {login}. Это точно ваш логин?\nОтветьте «{yes_alias}» в течение {timeout_min} минут или «{no_alias}» для отмены. Чтобы исправить логин — пришлите правильный одним сообщением.",
    "tpl_confirm_accepted": "Принято, пополняю …",
    "tpl_confirm_cancelled": "Ок, автопополнение отменено. Пришлите правильный логин или свяжитесь с продавцом.",
    "tpl_confirm_timeout": "Подтверждения не получил. Автопополнение отменено, продавец разберётся вручную.",
    "tpl_loss_protection": "⛔️ Автовыдача отменена (цена лота ниже себестоимости). Продавец уведомлён.",
}

DEFAULT_MAPPINGS: dict[str, Any] = {
    "lots": {
        # "12345678": {
        #     "service_id": 449,
        #     "type": "code",
        #     "amount_field": "quantity",
        #     "extra_fields": {},
        #     "markup_percent": null,
        #     "min_price": null,
        #     "currency": null,
        #     "account_field": "Логин Steam",
        #     "account_target_field": "account",
        #     "confirm": true,
        #     "loss_protection": null,           # null = глобальное; true/false перекрывает
        #     "loss_safety_margin_percent": null,
        #     "auto_deactivate": true,           # отключать лот при in_stock==0
        #     "enabled": true
        # }
    }
}

# Сидкар для состояния auto-deactivation: lot_id -> True/False
# (хранится в mappings.json в ключе _state, чтобы не включать лоты, которые
# пользователь отключил вручную)

SETTING_DESCRIPTIONS: dict[str, str] = {
    "autodelivery_enabled": "Включает/выключает автоматическую выдачу товаров при новых заказах",
    "autoprice_enabled": "Автоматическое обновление цен лотов FunPay на основе цен NS.Gifts + наценка",
    "chat_commands_enabled": "Позволяет покупателям использовать команды !баланс, !прайс, !статус в чате",
    "global_markup_percent": "Процент наценки на все товары (поверх цены NS.Gifts)",
    "fixed_markup": "Фиксированная надбавка в валюте лота (добавляется к процентной наценке)",
    "min_price": "Минимальная цена лота (не будет ниже этого значения)",
    "default_lot_currency": "Валюта по умолчанию для расчёта цен (rub/uah/kzt/usd/eur)",
    "price_sync_interval_min": "Как часто обновлять цены (в минутах)",
    "low_balance_threshold_usd": "При балансе ниже этого значения (USD) отправится уведомление",
    "confirm_steam_login": "Запрашивать у покупателя подтверждение Steam-логина перед пополнением",
    "confirm_timeout_min": "Сколько минут ждать подтверждения от покупателя",
    "confirm_retries": "Сколько раз повторить запрос подтверждения при отсутствии ответа",
    "loss_protection_enabled": "Блокирует выдачу если цена лота ниже себестоимости (защита от убытков)",
    "loss_safety_margin_percent": "Запас сверх себестоимости для защиты (0% = ровная себестоимость)",
    "working_hours_enabled": "Автовыдача работает только в указанные часы",
    "working_hours_start": "Час начала рабочего времени (0-23)",
    "working_hours_end": "Час окончания рабочего времени (0-23)",
}


# =========================================================================
# Утилиты ввода/вывода JSON
# =========================================================================

_io_lock = threading.RLock()


def _ensure_storage() -> None:
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)


def _load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    _ensure_storage()
    if not path.exists():
        return json.loads(json.dumps(default))
    try:
        with _io_lock, open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # merge с дефолтом, чтобы новые поля не падали
        merged = json.loads(json.dumps(default))
        if isinstance(data, dict):
            merged.update(data)
        return merged
    except Exception:
        logger.warning(f"NS.Gifts: не смог прочитать {path}, использую дефолт", exc_info=True)
        return json.loads(json.dumps(default))


def _save_json(path: Path, data: dict[str, Any]) -> None:
    _ensure_storage()
    tmp = path.with_suffix(path.suffix + ".tmp")
    with _io_lock:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
        try:
            # секреты доступны только владельцу
            if path == SECRETS_FILE:
                os.chmod(path, 0o600)
        except Exception:
            pass


# =========================================================================
# NS API клиент
# =========================================================================

class NSClientError(Exception):
    """Ошибка вызова NS API."""

    def __init__(self, status: int, message: str, body: Any = None):
        super().__init__(f"NS API {status}: {message}")
        self.status = status
        self.message = message
        self.body = body


class NSClient:
    """
    HMAC-подписанный клиент NS API.

    Авторизация двухслойная: api_secret (base64) + session token (TTL ~2ч).
    Подпись HMAC-SHA256 строится из (METHOD, PATH, QUERY, TS, [TOKEN,] sha256(body)).
    """

    def __init__(self, secrets: dict[str, Any]):
        self.user_id: int = int(secrets.get("user_id") or 0)
        self.login: str = secrets.get("login") or ""
        self.password: str = secrets.get("password") or ""
        self.api_secret_b64: str = secrets.get("api_secret") or ""
        self.base_url: str = (secrets.get("base_url") or "https://api.ns.gifts").rstrip("/")
        self.totp_code: str = secrets.get("totp_code") or ""

        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._lock = threading.RLock()
        self._sess = requests.Session()
        self._sess.headers.update({"User-Agent": "ns-gifts-fpc/0.1"})

    @property
    def configured(self) -> bool:
        return bool(self.user_id and self.api_secret_b64 and self.login and self.password)

    def _sign(self, method: str, path: str, query: str, body: bytes, ts: str,
              token: str | None) -> str:
        body_hash = hashlib.sha256(body or b"").hexdigest()
        parts = [method.upper(), path, query, ts]
        if token is not None:
            parts.append(token)
        parts.append(body_hash)
        string_to_sign = "\n".join(parts).encode("utf-8")
        try:
            key = base64.b64decode(self.api_secret_b64)
        except Exception as e:
            raise NSClientError(0, f"api_secret не base64: {e}")
        digest = hmac.new(key, string_to_sign, hashlib.sha256).digest()
        return base64.b64encode(digest).decode("ascii")

    def login_token(self) -> str:
        """Bootstrap-эндпоинт. Подписывается api_secret без токена."""
        if not self.configured:
            raise NSClientError(0, "NS-учётка не настроена (user_id/login/password/api_secret).")
        body = json.dumps({"login": self.login, "password": self.password},
                          separators=(",", ":")).encode("utf-8")
        ts = str(int(time.time()))
        path = "/api/v2/get_token"
        headers = {
            "X-User-Id": str(self.user_id),
            "X-Timestamp": ts,
            "X-Signature": self._sign("POST", path, "", body, ts, None),
            "Content-Type": "application/json",
        }
        r = self._sess.post(self.base_url + path, headers=headers, data=body, timeout=30)
        self._raise_for_status(r)
        data = r.json()
        with self._lock:
            self._token = data["token"]
            self._token_expires_at = time.time() + int(data.get("expires_in", 7200)) - 60
        return self._token

    def _ensure_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._token_expires_at:
                return self._token
        return self.login_token()

    def call(self, method: str, path: str, *, params: dict[str, Any] | None = None,
             json_body: dict[str, Any] | None = None,
             totp_code: str | None = None) -> dict[str, Any]:
        if not self.configured:
            raise NSClientError(0, "NS-учётка не настроена.")
        token = self._ensure_token()
        # build query
        query = "&".join(f"{k}={v}" for k, v in (params or {}).items())
        body = (
            b""
            if json_body is None
            else json.dumps(json_body, separators=(",", ":")).encode("utf-8")
        )
        ts = str(int(time.time()))
        headers = {
            "X-User-Id": str(self.user_id),
            "X-Timestamp": ts,
            "X-Token": token,
            "X-Signature": self._sign(method, path, query, body, ts, token),
            "Content-Type": "application/json",
        }
        if totp_code or self.totp_code:
            headers["X-Totp-Code"] = totp_code or self.totp_code

        url = self.base_url + path + (f"?{query}" if query else "")
        r = self._sess.request(method, url, headers=headers, data=body, timeout=60)

        # 401 — токен мог истечь, перелогинимся ОДИН раз
        if r.status_code == 401:
            self.login_token()
            token = self._token  # type: ignore[assignment]
            ts = str(int(time.time()))
            headers["X-Token"] = token or ""
            headers["X-Timestamp"] = ts
            headers["X-Signature"] = self._sign(method, path, query, body, ts, token)
            r = self._sess.request(method, url, headers=headers, data=body, timeout=60)

        self._raise_for_status(r)
        try:
            return r.json()
        except Exception:
            return {"raw": r.text}

    @staticmethod
    def _raise_for_status(r: requests.Response) -> None:
        if r.status_code >= 400:
            try:
                payload = r.json()
                msg = payload.get("detail") or payload.get("error") or payload.get("message") or r.text
            except Exception:
                payload = None
                msg = r.text or f"HTTP {r.status_code}"
            raise NSClientError(r.status_code, str(msg)[:500], body=payload)

    # --- удобные обёртки -------------------------------------------------

    def stock(self) -> dict[str, Any]:
        return self.call("GET", "/api/v2/stock")

    def check_balance(self) -> dict[str, Any]:
        return self.call("GET", "/api/v2/check_balance")

    def exchange_rate(self, service_id: int = 1) -> dict[str, Any]:
        return self.call("POST", "/api/v2/exchange_rate", json_body={"service_id": service_id})

    def create_order(self, service_id: int, custom_id: str, fields: list[dict[str, Any]]) -> dict[str, Any]:
        return self.call(
            "POST", "/api/v2/create_order",
            json_body={"service_id": service_id, "custom_id": custom_id, "fields": fields},
        )

    def pay_order(self, custom_id: str, totp_code: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"custom_id": custom_id}
        if totp_code:
            body["totp_code"] = totp_code
        return self.call("POST", "/api/v2/pay_order", json_body=body, totp_code=totp_code)

    def order_info(self, custom_id: str) -> dict[str, Any]:
        return self.call("GET", f"/api/v2/order_info/{custom_id}")

    def steam_check_user(self, steam_id: str) -> dict[str, Any]:
        return self.call("POST", "/api/v2/steam/check_user", json_body={"steam_id": steam_id})

    def steam_gift_apps(self) -> dict[str, Any]:
        return self.call("GET", "/api/v2/steam_gift/get_apps")


# =========================================================================
# Глобальное состояние плагина (singleton)
# =========================================================================

class Runtime:
    def __init__(self) -> None:
        self.secrets: dict[str, Any] = {}
        self.settings: dict[str, Any] = {}
        self.mappings: dict[str, Any] = {}
        self.client: NSClient | None = None
        self.stock_cache: dict[str, Any] = {}
        self.stock_cache_ts: float = 0.0
        self.pending_chat_inputs: dict[int, dict[str, Any]] = {}
        self.pending_tg_states: dict[tuple[int, int], str] = {}
        # ackn: order_id -> ConfirmState (в state хранится chat_id для матчинга)
        self.pending_confirms: dict[Any, dict[str, Any]] = {}
        self.tg_registered: bool = False
        # ссылки на зарегистрированные telebot-хендлеры (для снятия при delete)
        self._tg_handler_fns: list[Any] = []
        self.price_loop_started: bool = False
        self.confirm_loop_started: bool = False
        self.lock = threading.RLock()
        self.db: sqlite3.Connection | None = None
        self.db_lock = threading.RLock()


R = Runtime()


def _load_all() -> None:
    R.secrets = _load_json(SECRETS_FILE, DEFAULT_SECRETS)
    R.settings = _load_json(SETTINGS_FILE, DEFAULT_SETTINGS)
    R.mappings = _load_json(MAPPINGS_FILE, DEFAULT_MAPPINGS)
    R.mappings.setdefault("_state", {})  # для auto-deactivation
    R.client = NSClient(R.secrets)
    _db_init()


def _save_secrets() -> None:
    _save_json(SECRETS_FILE, R.secrets)
    R.client = NSClient(R.secrets)


def _save_settings() -> None:
    _save_json(SETTINGS_FILE, R.settings)


def _save_mappings() -> None:
    _save_json(MAPPINGS_FILE, R.mappings)


def _log_delivery(msg: str) -> None:
    _ensure_storage()
    try:
        with open(DELIVERY_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        logger.debug("Не смог записать deliveries.log", exc_info=True)


# =========================================================================
# SQLite: идемпотентность доставок + статистика прибыли
# =========================================================================

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS deliveries (
    order_id        TEXT PRIMARY KEY,
    custom_id       TEXT,
    lot_id          TEXT,
    service_id      INTEGER,
    service_name    TEXT,
    buyer_username  TEXT,
    amount          INTEGER,
    currency        TEXT,
    ns_cost_usd     REAL,
    ns_cost_local   REAL,
    sold_price      REAL,
    profit_local    REAL,
    profit_usd      REAL,
    status          TEXT,                    -- pending|completed|in_progress|failed|cancelled|loss_aborted
    error           TEXT,
    created_at      INTEGER,
    completed_at    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_deliveries_created_at ON deliveries(created_at);
CREATE INDEX IF NOT EXISTS idx_deliveries_status ON deliveries(status);
"""


def _db_init() -> None:
    _ensure_storage()
    with R.db_lock:
        if R.db is None:
            R.db = sqlite3.connect(str(DB_FILE), check_same_thread=False, timeout=10)
            R.db.row_factory = sqlite3.Row
            R.db.executescript(DB_SCHEMA)
            R.db.commit()


def _db_get_delivery(order_id: str) -> dict[str, Any] | None:
    if R.db is None:
        return None
    with R.db_lock:
        row = R.db.execute(
            "SELECT * FROM deliveries WHERE order_id = ?", (str(order_id),)
        ).fetchone()
    return dict(row) if row else None


def _db_upsert_delivery(data: dict[str, Any]) -> None:
    if R.db is None:
        return
    cols = [
        "order_id", "custom_id", "lot_id", "service_id", "service_name",
        "buyer_username", "amount", "currency", "ns_cost_usd", "ns_cost_local",
        "sold_price", "profit_local", "profit_usd", "status", "error",
        "created_at", "completed_at",
    ]
    values = [data.get(c) for c in cols]
    placeholders = ",".join("?" for _ in cols)
    update_pairs = ",".join(f"{c}=excluded.{c}" for c in cols if c != "order_id")
    sql = (
        f"INSERT INTO deliveries ({','.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(order_id) DO UPDATE SET {update_pairs}"
    )
    with R.db_lock:
        R.db.execute(sql, values)
        R.db.commit()


def _db_stats(since_ts: int) -> dict[str, Any]:
    if R.db is None:
        return {"count": 0, "revenue": {}, "profit_usd": 0.0, "by_currency": {}}
    with R.db_lock:
        rows = R.db.execute(
            "SELECT currency, sold_price, profit_local, profit_usd, ns_cost_usd "
            "FROM deliveries WHERE status='completed' AND created_at >= ?",
            (int(since_ts),),
        ).fetchall()
    by_cur: dict[str, dict[str, float]] = {}
    profit_usd_total = 0.0
    cost_usd_total = 0.0
    for r in rows:
        cur = (r["currency"] or "rub").lower()
        b = by_cur.setdefault(cur, {"count": 0, "revenue": 0.0, "profit": 0.0})
        b["count"] += 1
        b["revenue"] += float(r["sold_price"] or 0)
        b["profit"] += float(r["profit_local"] or 0)
        profit_usd_total += float(r["profit_usd"] or 0)
        cost_usd_total += float(r["ns_cost_usd"] or 0)
    return {
        "count": len(rows),
        "profit_usd": round(profit_usd_total, 4),
        "cost_usd": round(cost_usd_total, 4),
        "by_currency": by_cur,
    }


# =========================================================================
# Stock cache (на N минут, чтобы не дёргать API на каждое сообщение)
# =========================================================================

_STOCK_TTL = 5 * 60


def _get_stock(force: bool = False) -> dict[str, Any]:
    now = time.time()
    if not force and R.stock_cache and now - R.stock_cache_ts < _STOCK_TTL:
        return R.stock_cache
    if not R.client or not R.client.configured:
        return {}
    try:
        R.stock_cache = R.client.stock()
        R.stock_cache_ts = now
    except Exception as e:
        logger.warning(f"NS.Gifts: не смог получить /stock: {e}")
    return R.stock_cache


def _find_service(service_id: int) -> dict[str, Any] | None:
    stock = _get_stock()
    for cat in stock.get("categories", []):
        for svc in cat.get("services", []):
            if int(svc.get("service_id", -1)) == int(service_id):
                return {**svc, "category_name": cat.get("category_name"),
                        "category_id": cat.get("category_id"),
                        "fields_schema": cat.get("fields", [])}
    return None


# =========================================================================
# Курсы USD → RUB/UAH/KZT/USD/EUR
# =========================================================================

SUPPORTED_CURRENCIES = ("rub", "uah", "kzt", "usd", "eur")
_RATE_TTL = 30 * 60
_rate_cache: dict[str, Any] = {"rates": {}, "ts": 0.0}


def _fetch_ns_rates() -> dict[str, float]:
    """Тянет курсы NS (RUB/UAH/KZT per 1 USD) и кеширует на 30 мин."""
    now = time.time()
    if _rate_cache["rates"] and now - _rate_cache["ts"] < _RATE_TTL:
        return _rate_cache["rates"]
    if not R.client or not R.client.configured:
        return _rate_cache["rates"]
    try:
        data = R.client.exchange_rate(service_id=1)
        rates = data.get("rates") or {}
        clean = {k: float(v) for k, v in rates.items()
                 if isinstance(v, (int, float, str)) and float(v) > 0}
        if clean:
            _rate_cache["rates"] = clean
            _rate_cache["ts"] = now
    except Exception as e:
        logger.debug(f"NS.Gifts: exchange_rate failed: {e}")
    return _rate_cache["rates"]


def _usd_to(currency: str) -> float:
    """Курс 1 USD в указанную валюту (rub/uah/kzt/usd/eur)."""
    cur = (currency or "rub").lower()
    if cur == "usd":
        return 1.0
    manual = float(R.settings.get(f"usd_to_{cur}_rate") or 0.0)
    if manual > 0:
        return manual
    if cur in ("rub", "uah", "kzt"):
        rates = _fetch_ns_rates()
        val = float(rates.get(cur) or 0)
        if val > 0:
            return val
    fallback = R.settings.get(f"usd_to_{cur}_fallback")
    if fallback:
        return float(fallback)
    # default fallbacks
    return {"rub": 100.0, "uah": 41.0, "kzt": 480.0, "eur": 0.92}.get(cur, 100.0)


# back-compat shim
def _usd_to_rub() -> float:
    return _usd_to("rub")


CURRENCY_SYMBOLS = {"rub": "₽", "uah": "₴", "kzt": "₸", "usd": "$", "eur": "€"}


def _currency_symbol(currency: str) -> str:
    return CURRENCY_SYMBOLS.get((currency or "").lower(), "")


def _resolve_lot_currency(lot_fields: Any | None, mapping: dict[str, Any] | None = None) -> str:
    """Определяет валюту лота: mapping → LotFields.currency → default_lot_currency."""
    if mapping and (mapping.get("currency") or "").lower() in SUPPORTED_CURRENCIES:
        return mapping["currency"].lower()
    cur = _currency_from_enum(getattr(lot_fields, "currency", None)) if lot_fields is not None else None
    if cur:
        return cur
    return (R.settings.get("default_lot_currency") or "rub").lower()


def _currency_from_enum(enum_val) -> str | None:
    """Преобразует FunPayAPI Currency enum в строку 'rub'/'usd'/'eur'."""
    if enum_val is None:
        return None
    try:
        from FunPayAPI.common.enums import Currency  # type: ignore
        if enum_val is Currency.RUB:
            return "rub"
        if enum_val is Currency.USD:
            return "usd"
        if enum_val is Currency.EUR:
            return "eur"
    except Exception:
        pass
    # fallback по строке
    s = str(enum_val).strip().lower()
    if "rub" in s or "₽" in s:
        return "rub"
    if "usd" in s or "$" in s:
        return "usd"
    if "eur" in s or "€" in s:
        return "eur"
    return None


def _tpl(key: str, **ctx: Any) -> str:
    """Берёт шаблон из настроек, форматирует с переданными переменными.
    Не падает на отсутствующих ключах — заменяет на пустую строку."""
    tpl_str = R.settings.get(key)
    if not tpl_str:
        tpl_str = DEFAULT_SETTINGS.get(key, "")

    class _SafeDict(dict):
        def __missing__(self, k):  # noqa: ARG002
            return ""
    try:
        return str(tpl_str).format_map(_SafeDict(**ctx))
    except Exception:
        return str(tpl_str)


# =========================================================================
# Логика автодоставки
# =========================================================================

ORDER_STATUS_COMPLETED = 2
ORDER_STATUS_IN_PROGRESS = 10
ORDER_STATUS_REFUNDED = 7
ORDER_STATUS_CANCELED = 5
ORDER_STATUS_CREATED = 0


_NS_STATUS_INT_MAP = {
    ORDER_STATUS_COMPLETED: "completed",
    ORDER_STATUS_IN_PROGRESS: "in_progress",
    ORDER_STATUS_REFUNDED: "refunded",
    ORDER_STATUS_CANCELED: "canceled",
    ORDER_STATUS_CREATED: "created",
}

_NS_STATUS_ALIASES = {
    "success": "completed", "ok": "completed", "done": "completed",
    "complete": "completed", "completed": "completed",
    "pending": "in_progress", "processing": "in_progress",
    "in_progress": "in_progress", "inprogress": "in_progress",
    "wait": "in_progress", "waiting": "in_progress",
    "insufficient": "insufficient", "insufficient_balance": "insufficient",
    "no_money": "insufficient", "not_enough": "insufficient",
    "refund": "refunded", "refunded": "refunded",
    "cancel": "canceled", "canceled": "canceled", "cancelled": "canceled",
    "created": "created", "new": "created",
}


def _normalize_ns_status(raw: Any) -> str:
    """Приводит статус заказа NS к единой строке независимо от того, отдаёт
    ли API числовой код (2/10/7/5/0) или строку ("completed"/"in_progress"…).

    Это устраняет рассинхрон между pay_order (строки) и order_info (int)."""
    if raw is None or isinstance(raw, bool):
        return ""
    if isinstance(raw, (int, float)):
        return _NS_STATUS_INT_MAP.get(int(raw), f"code_{int(raw)}")
    s = str(raw).strip().lower()
    if not s:
        return ""
    if s.lstrip("-").isdigit():
        return _NS_STATUS_INT_MAP.get(int(s), f"code_{s}")
    return _NS_STATUS_ALIASES.get(s, s)


def _resolve_lot_mapping(lot_id: str | int | None,
                         subcategory_id: int | None = None,
                         description: str | None = None) -> dict[str, Any] | None:
    if lot_id is None:
        return None
    lots = R.mappings.get("lots", {}) or {}
    m = lots.get(str(lot_id))
    if m and m.get("enabled", True):
        return {**m, "lot_id": str(lot_id)}
    return None


def _format_pins(pins: list[Any]) -> str:
    pins = [str(p) for p in (pins or []) if p]
    if not pins:
        return "—"
    if len(pins) == 1:
        return f"<code>{pins[0]}</code>"
    return "\n".join(f"<code>{p}</code>" for p in pins)


def _perform_delivery(cardinal: "Cardinal", order, mapping: dict[str, Any],
                     dynamic_fields: dict[str, Any] | None = None) -> tuple[bool, str]:
    """
    Делает create_order → pay_order → возвращает (ok, текст_для_покупателя).
    `order` — FunPayAPI OrderShortcut.
    `dynamic_fields` — доп. поля (напр. {"account": "steam_login"}), перекрывают extra_fields.
    """
    if not R.client or not R.client.configured:
        return False, "Плагин NS.Gifts не настроен (нет учётки)."

    service_id = int(mapping.get("service_id") or 0)
    if not service_id:
        return False, "В привязке не указан service_id."

    custom_id = str(uuid_lib.uuid4())
    amount = max(1, int(getattr(order, "amount", 1) or 1))
    amount_field = mapping.get("amount_field") or "quantity"

    fields: list[dict[str, Any]] = []
    used_keys: set[str] = set()

    # 1) Кол-во из FunPay в API-поле amount_field
    fields.append({"key": amount_field, "value": amount})
    used_keys.add(amount_field)

    # 2) Динамические поля (напр. account=login после подтверждения)
    for k, v in (dynamic_fields or {}).items():
        if k in used_keys:
            continue
        fields.append({"key": k, "value": v})
        used_keys.add(k)

    # 3) Static extra_fields из конфига
    for k, v in (mapping.get("extra_fields") or {}).items():
        if k in used_keys:
            continue
        fields.append({"key": k, "value": v})
        used_keys.add(k)

    # 4) Проверяем обязательные поля по схеме сервиса
    svc = _find_service(service_id) or {}
    schema = svc.get("fields_schema") or []
    required_missing = [
        f["key"] for f in schema
        if f.get("required") and f.get("key") not in used_keys
    ]
    if required_missing:
        return False, (
            f"Для service_id={service_id} обязательны поля {required_missing}, "
            "а они не заданы. Заполните `extra_fields` в mappings.json."
        )

    attempts = int(R.settings.get("deliver_attempts", 3))
    last_err = "unknown"

    def _handle(status_raw: Any, payload: dict[str, Any]) -> tuple[bool, str] | None:
        """Разбирает статус NS. Возвращает (ok, текст) либо None если статус
        неизвестен/промежуточный и стоит повторить попытку."""
        status = _normalize_ns_status(status_raw)
        pins = payload.get("pins") or []
        balance = payload.get("balance")

        if status == "completed":
            _log_delivery(
                f"DELIVER ok order={order.id} service={service_id} amount={amount} "
                f"pins_count={len(pins)} balance={balance}"
            )
            pins_text = "\n".join(str(p) for p in pins) if pins else "—"
            text = _tpl(
                "tpl_success",
                order_id=order.id, service_name=svc.get("service_name", ""),
                service_id=service_id, amount=amount, pins=pins_text,
                note=str(payload.get("note") or ""),
            )
            _db_upsert_delivery({
                "order_id": str(order.id),
                "custom_id": custom_id,
                "status": "completed",
            })
            return True, text

        if status == "in_progress":
            _db_upsert_delivery({
                "order_id": str(order.id),
                "custom_id": custom_id,
                "status": "in_progress",
            })
            # Асинхронная доставка (Steam Gift): НЕ блокируем поток NEW_ORDER
            # на время поллинга (до 600с). Запускаем фоновый опрос, который
            # сам допишет покупателю финальный результат.
            threading.Thread(
                target=_poll_and_deliver_async,
                args=(cardinal, custom_id, order),
                daemon=True,
                name=f"ns-poll-{getattr(order, 'id', '?')}",
            ).start()
            return True, _tpl("tpl_success_async", order_id=order.id)

        if status == "insufficient":
            _log_delivery(f"DELIVER insufficient order={order.id} service={service_id}")
            _db_upsert_delivery({
                "order_id": str(order.id),
                "custom_id": custom_id,
                "status": "failed",
                "error": "insufficient balance",
            })
            return False, _tpl("tpl_insufficient", order_id=order.id)

        if status in ("refunded", "canceled"):
            reason = ("заказ отменён поставщиком, средства возвращены"
                      if status == "refunded"
                      else "заказ отменён (таймаут оплаты)")
            _log_delivery(f"DELIVER {status} order={order.id} service={service_id}")
            _db_upsert_delivery({
                "order_id": str(order.id),
                "custom_id": custom_id,
                "status": "failed",
                "error": f"{status} by NS",
            })
            return False, _tpl("tpl_failed", order_id=order.id, error=reason)

        return None

    created = False
    for attempt in range(1, attempts + 1):
        try:
            # На повторной попытке custom_id не меняется, поэтому СНАЧАЛА
            # спрашиваем реальное состояние заказа у NS — чтобы не оплатить
            # один и тот же заказ дважды (двойная выдача/списание).
            if attempt > 1:
                try:
                    info = R.client.order_info(custom_id)
                    res = _handle(info.get("status"), info)
                    if res is not None:
                        return res
                    created = True  # заказ существует, повторно не создаём
                except NSClientError as e:
                    if e.status != 404:
                        raise
                    # 404 — заказ ещё не создан, идём обычным путём

            if not created:
                try:
                    R.client.create_order(service_id, custom_id, fields)
                    created = True
                except NSClientError as e:
                    if e.status in (409, 428):
                        created = True  # уже создан ранее — не дублируем
                    else:
                        raise

            pay = R.client.pay_order(custom_id)
            res = _handle(pay.get("status"), pay)
            if res is not None:
                return res

            last_err = f"status={pay.get('status')}"
        except NSClientError as e:
            last_err = str(e)
            _log_delivery(f"DELIVER err order={order.id} attempt={attempt}: {e}")
            # 428 / 409 нет смысла ретраить
            if e.status in (409, 428):
                break
            time.sleep(min(2 * attempt, 6))
        except Exception as e:
            last_err = str(e)
            _log_delivery(f"DELIVER unexpected order={order.id} attempt={attempt}: {e}")
            time.sleep(min(2 * attempt, 6))

    _db_upsert_delivery({
        "order_id": str(order.id),
        "custom_id": custom_id,
        "status": "failed",
        "error": last_err[:500],
    })
    return False, _tpl("tpl_failed", order_id=order.id, error=last_err)


def _poll_until_final(custom_id: str, fp_order_id: str, timeout_sec: int = 600) -> str:
    """Поллит /order_info до completed/refunded/canceled. Возвращает текст для покупателя."""
    deadline = time.time() + timeout_sec
    last_status: int | None = None
    while time.time() < deadline:
        try:
            info = R.client.order_info(custom_id) if R.client else {}
            status = int(info.get("status", -1))
            if status == ORDER_STATUS_COMPLETED:
                pins = info.get("pins") or []
                lines = [f"✅ Заказ #{fp_order_id} выдан."]
                if pins:
                    lines.append("Код: " + ", ".join(str(p) for p in pins))
                return "\n".join(lines)
            if status == ORDER_STATUS_REFUNDED:
                return f"⚠️ Заказ #{fp_order_id} отменён поставщиком, средства возвращены."
            if status == ORDER_STATUS_CANCELED:
                return f"⚠️ Заказ #{fp_order_id} отменён по таймауту оплаты."
            last_status = status
        except Exception as e:
            logger.debug(f"poll order_info err: {e}")
        time.sleep(5)
    return (
        f"⏳ Заказ #{fp_order_id} в обработке (status={last_status}). "
        "Когда поставщик доставит — напишу повторно."
    )


def _poll_and_deliver_async(cardinal: "Cardinal", custom_id: str, order) -> None:
    """Фоновый поллинг асинхронного заказа NS (Steam Gift и т.п.).

    НЕ блокирует поток NEW_ORDER: покупателю сразу уходит «в обработке»,
    а финальный результат допишется этим потоком, когда поставщик выдаст."""
    try:
        text = _poll_until_final(custom_id, order.id)
        try:
            cardinal.send_message(order.chat_id, text,
                                  getattr(order, "buyer_username", None))
        except Exception:
            logger.warning("NS.Gifts: не смог отправить финал async-заказа",
                           exc_info=True)
        final = ""
        try:
            info = R.client.order_info(custom_id) if R.client else {}
            final = _normalize_ns_status(info.get("status"))
        except Exception:
            pass
        db_status = ("completed" if final == "completed"
                     else "failed" if final in ("refunded", "canceled")
                     else "in_progress")
        _db_upsert_delivery({
            "order_id": str(order.id),
            "custom_id": custom_id,
            "status": db_status,
        })
        _notify_tg(
            cardinal,
            f"🎁 <b>NS.Gifts async</b>: заказ #{order.id} — "
            f"{final or 'обработка'}\n{text[:300]}"
        )
    except Exception:
        logger.error("NS.Gifts: ошибка async-поллинга заказа", exc_info=True)


def deliver_order_handler(cardinal: "Cardinal", event, *args):
    """BIND_TO_NEW_ORDER: смотрим, есть ли привязка по lot_id, делаем выдачу."""
    try:
        if not R.settings.get("autodelivery_enabled"):
            return

        # Working hours check (uses server-local time; no timezone config exposed)
        s = R.settings
        if s.get("working_hours_enabled"):
            now_hour = datetime.datetime.now().hour
            start = int(s.get("working_hours_start", 0))
            end = int(s.get("working_hours_end", 23))
            outside = False
            if start <= end:
                if not (start <= now_hour < end):
                    outside = True
            else:  # overnight range e.g. 22-6
                if end <= now_hour < start:
                    outside = True
            if outside:
                logger.warning(
                    "NS.Gifts: order received outside working hours "
                    "(hour=%d, window=%d-%d), skipping auto-delivery",
                    now_hour, start, end)
                _notify_tg(
                    cardinal,
                    f"⚠️ <b>NS.Gifts:</b> получен заказ вне рабочего времени "
                    f"(сейчас {now_hour}:00, окно {start}:00-{end}:00).\n"
                    f"Автовыдача пропущена — требуется ручная обработка.")
                return

        order = getattr(event, "order", None)
        if order is None:
            return

        # Идемпотентность: проверяем в БДб, не выдавали ли уже
        existing = _db_get_delivery(str(order.id))
        if existing:
            st = existing.get("status")
            if st in ("completed", "in_progress", "pending"):
                logger.info(
                    f"NS.Gifts: заказ {order.id} уже в состоянии {st}, пропуск."
                )
                return
            if st in ("failed", "loss_aborted", "cancelled") \
                    and not R.settings.get("retry_failed_orders"):
                logger.info(
                    f"NS.Gifts: заказ {order.id} уже был в состоянии {st}, retry отключён."
                )
                return

        full_order = _get_full_order(cardinal, order.id)
        lot_id = _extract_lot_id(cardinal, order, full_order)
        if lot_id is None:
            logger.debug(
                "NS.Gifts: заказ #%s — не удалось определить lot_id, пропуск",
                getattr(order, "id", "?"))
            return

        mapping = _resolve_lot_mapping(lot_id)
        if not mapping:
            # Не наш лот — НОРМАЛЬНО. DEBUG, не actions.log.
            logger.debug(
                "NS.Gifts: заказ #%s lot_id=%s — нет привязки в mappings, "
                "пропуск (обработает другой плагин если он его настроил)",
                getattr(order, "id", "?"), lot_id)
            return

        # Дошли — наш лот. Логируем факт получения заказа.
        _log_action_ns("delivery",
                        f"Получен заказ #{getattr(order, 'id', '?')} для "
                        f"lot_id={lot_id} (service_id={mapping.get('service_id')})",
                        order_id=getattr(order, "id", None),
                        lot_id=lot_id,
                        service_id=mapping.get("service_id"),
                        buyer=getattr(order, "buyer_username", None),
                        amount=getattr(order, "amount", 1))

        # Помечаем в БД как pending — чтобы при рестарте FPC не выдавать повторно
        _db_upsert_delivery({
            "order_id": str(order.id),
            "lot_id": str(lot_id),
            "service_id": int(mapping.get("service_id") or 0),
            "buyer_username": getattr(order, "buyer_username", None),
            "amount": int(getattr(order, "amount", 1) or 1),
            "status": "pending",
            "created_at": int(time.time()),
        })

        # Защита от убытков
        loss_ok, loss_msg = _check_loss_protection(cardinal, order, mapping, full_order)
        if not loss_ok:
            _db_upsert_delivery({
                "order_id": str(order.id),
                "status": "loss_aborted",
                "error": loss_msg,
                "completed_at": int(time.time()),
            })
            buyer_text = _tpl("tpl_loss_protection", order_id=order.id)
            try:
                cardinal.send_message(order.chat_id, buyer_text, order.buyer_username)
            except Exception:
                pass
            _notify_tg(
                cardinal,
                f"⛔️ <b>NS.Gifts — выдача отменена</b> (защита от убытков)\n"
                f"Заказ #{order.id}, лот <code>{lot_id}</code>\n{loss_msg}"
            )
            return

        # Для Steam top-up / Gift: пытаемся подтянуть логин и запросить подтверждение
        if mapping.get("type") in ("steam_topup", "steam_gift"):
            login = _extract_steam_login(full_order, mapping)
            do_confirm = mapping.get("confirm")
            if do_confirm is None:
                do_confirm = R.settings.get("confirm_steam_login", True)
            if login and do_confirm:
                _start_confirmation_flow(cardinal, order, mapping, login)
                return
            if login and not do_confirm:
                ok, text = _perform_delivery_with_login(cardinal, order, mapping, login)
                _report_delivery(cardinal, order, lot_id, ok, text)
                return
            if not login:
                _request_steam_login(cardinal, order, mapping)
                return

        ok, text = _perform_delivery(cardinal, order, mapping)
        _report_delivery(cardinal, order, lot_id, ok, text)
    except Exception:
        logger.error("NS.Gifts: ошибка в deliver_order_handler", exc_info=True)


def _report_delivery(cardinal: "Cardinal", order, lot_id: str | None,
                     ok: bool, text: str) -> None:
    try:
        cardinal.send_message(order.chat_id, text, order.buyer_username)
    except Exception:
        logger.warning("NS.Gifts: не смог отправить ответ в FunPay-чат", exc_info=True)
    _notify_tg(
        cardinal,
        f"🎁 <b>NS.Gifts выдача</b>\n"
        f"Заказ #{order.id} — {('успех ✅' if ok else 'ошибка ❌')}\n"
        f"Покупатель: <code>{order.buyer_username}</code>\n"
        f"Лот: <code>{lot_id}</code>\n"
        f"Сообщение: {text[:300]}"
    )
    # Фиксируем финальный статус в БДб + статистику прибыли
    try:
        _persist_delivery_stats(cardinal, order, lot_id, ok, text)
    except Exception:
        logger.debug("NS.Gifts: не смог сохранить статистику доставки", exc_info=True)
    if ok:
        _maybe_low_balance_alert(cardinal)


def _persist_delivery_stats(cardinal: "Cardinal", order, lot_id: str | None,
                             ok: bool, text: str) -> None:
    """Дописывает completed/failed-результат + себестоимость/прибыль в deliveries."""
    mapping = _resolve_lot_mapping(lot_id) or {}
    svc = _find_service(int(mapping.get("service_id") or 0)) or {}
    amount = max(1, int(getattr(order, "amount", 1) or 1))
    ns_cost_usd = float(svc.get("price") or 0) * amount
    cur = None
    sold_price = None
    ns_cost_local = None
    profit_local = None
    profit_usd = None
    try:
        full = _get_full_order(cardinal, order.id)
        cur_enum = getattr(getattr(full, "sum", None), "currency", None) \
            if full is not None else None
        cur = _currency_from_enum(cur_enum) or _resolve_lot_currency(None, mapping)
        sold_price = float(getattr(getattr(full, "sum", None), "value", 0) or 0)
    except Exception:
        cur = _resolve_lot_currency(None, mapping)
        try:
            sold_price = float(getattr(getattr(order, "price", None), "value", 0)
                               or getattr(order, "price", 0) or 0)
        except Exception:
            sold_price = 0.0
    rate = _usd_to(cur)
    if rate > 0:
        ns_cost_local = round(ns_cost_usd * rate, 4)
        if sold_price is not None:
            profit_local = round(sold_price - ns_cost_local, 4)
            profit_usd = round((sold_price - ns_cost_local) / rate, 4)
    _db_upsert_delivery({
        "order_id": str(order.id),
        "lot_id": str(lot_id) if lot_id else None,
        "service_id": int(mapping.get("service_id") or 0) or None,
        "service_name": svc.get("service_name"),
        "buyer_username": getattr(order, "buyer_username", None),
        "amount": amount,
        "currency": cur,
        "ns_cost_usd": round(ns_cost_usd, 6),
        "ns_cost_local": ns_cost_local,
        "sold_price": sold_price,
        "profit_local": profit_local,
        "profit_usd": profit_usd,
        "status": "completed" if ok else "failed",
        "error": None if ok else text[:500],
        "completed_at": int(time.time()),
    })


def _check_loss_protection(cardinal: "Cardinal", order, mapping: dict[str, Any],
                            full_order=None) -> tuple[bool, str]:
    """True = можно выдавать, False = цена лота ниже себестоимости."""
    enabled = mapping.get("loss_protection")
    if enabled is None:
        enabled = R.settings.get("loss_protection_enabled", True)
    if not enabled:
        return True, ""
    try:
        svc = _find_service(int(mapping.get("service_id") or 0)) or {}
        ns_price_usd = float(svc.get("price") or 0)
        if ns_price_usd <= 0:
            return True, ""  # нет данных — не блокируем
        amount = max(1, int(getattr(order, "amount", 1) or 1))
        full = full_order if full_order is not None else _get_full_order(cardinal, order.id)
        sum_obj = getattr(full, "sum", None) if full is not None else None
        sold_value = float(getattr(sum_obj, "value", 0) or 0)
        cur_enum = getattr(sum_obj, "currency", None) if sum_obj else None
        cur = _currency_from_enum(cur_enum) or _resolve_lot_currency(None, mapping)
        rate = _usd_to(cur)
        cost_local = ns_price_usd * amount * rate
        margin_pct = float(
            mapping.get("loss_safety_margin_percent")
            if mapping.get("loss_safety_margin_percent") is not None
            else R.settings.get("loss_safety_margin_percent", 0)
        )
        threshold = cost_local * (1.0 + margin_pct / 100.0)
        sym = _currency_symbol(cur) or cur.upper()
        if sold_value < threshold:
            return False, (
                f"Себестоимость {cost_local:.2f} {sym} "
                f"(NS {ns_price_usd:.4f} USD × {amount} × курс {rate:.2f}), "
                f"порог {threshold:.2f} {sym}, цена лота {sold_value:.2f} {sym}."
            )
        return True, ""
    except Exception:
        logger.debug("NS.Gifts: loss-protection check failed", exc_info=True)
        return True, ""


def _get_full_order(cardinal: "Cardinal", order_id: str):
    try:
        return cardinal.account.get_order(order_id)
    except Exception as e:
        logger.debug(f"NS.Gifts: get_order({order_id}) failed: {e}")
        return None


def _extract_lot_id(cardinal: "Cardinal", order, full_order=None) -> str | None:
    """
    OrderShortcut не содержит lot_id напрямую. Берём полный Order через FunPayAPI.
    """
    full = full_order or _get_full_order(cardinal, order.id)
    if full is not None:
        lot_id = getattr(full, "lot_id", None)
        if lot_id:
            return str(lot_id)

    # fallback: ищем lot_id в html виджета (тег data-offer)
    html = getattr(order, "html", "") or ""
    m = re.search(r"data-offer=\"(\d+)\"", html) or re.search(r"/lots/offer\?id=(\d+)", html)
    if m:
        return m.group(1)
    return None


# =========================================================================
# Steam-логин: извлечение + интерактивное подтверждение
# =========================================================================

def _extract_steam_login(full_order, mapping: dict[str, Any]) -> str | None:
    """Ищет Steam-логин в полях FunPay-заказа."""
    if full_order is None:
        return None
    fields = getattr(full_order, "fields", None) or {}
    if not fields:
        return None
    # 1) явный ключ из mapping
    explicit = (mapping.get("account_field") or "").strip().lower()
    candidates = [explicit] if explicit else []
    # 2) дефолтные имена
    candidates += [s.lower() for s in R.settings.get("steam_login_field_names", [])]

    for key, lf in fields.items():
        name = (getattr(lf, "name", "") or "").strip().lower()
        val = getattr(lf, "value", None)
        if isinstance(val, dict):
            val = val.get("ru") or val.get("en") or next(iter(val.values()), "")
        val = str(val or "").strip()
        if not val:
            continue
        if any(c and (c in name or c == key.lower()) for c in candidates):
            return val
    return None


def _validate_steam_login(login: str) -> tuple[bool, str]:
    """Проверяет логин через /steam/check_user. Возвращает (valid, status_text)."""
    if not R.client or not R.client.configured:
        return False, "NS-плагин не настроен"
    try:
        resp = R.client.steam_check_user(login)
        status = resp.get("accountStatus") if isinstance(resp, dict) else None
        if status is True or str(status).lower() in ("true", "ok", "active", "normal"):
            return True, str(status)
        return False, str(status or "аккаунт не найден")
    except NSClientError as e:
        return False, f"NS API: {e.message}"
    except Exception as e:
        return False, str(e)


def _request_steam_login(cardinal: "Cardinal", order, mapping: dict[str, Any]) -> None:
    """Спрашивает у покупателя Steam-логин в чате и ставит состояние."""
    try:
        cardinal.send_message(
            order.chat_id,
            _tpl("tpl_login_request", order_id=order.id),
            order.buyer_username,
        )
    except Exception:
        logger.debug("NS.Gifts: send login-request failed", exc_info=True)
    R.pending_confirms[order.id] = {
        "state": "awaiting_login",
        "order_id": order.id,
        "chat_id": order.chat_id,
        "order": order,
        "mapping": mapping,
        "login": None,
        "asked_at": time.time(),
        "retries_left": int(R.settings.get("confirm_retries", 1)),
    }
    _ensure_confirm_loop(cardinal)


def _start_confirmation_flow(cardinal: "Cardinal", order, mapping: dict[str, Any],
                              login: str) -> None:
    """Валидирует логин и спрашивает подтверждение в FunPay-чате."""
    ok, status = _validate_steam_login(login)
    if not ok:
        try:
            cardinal.send_message(
                order.chat_id,
                _tpl("tpl_login_invalid", login=login, status=status, order_id=order.id),
                order.buyer_username,
            )
        except Exception:
            pass
        R.pending_confirms[order.id] = {
            "state": "awaiting_login",
            "order_id": order.id,
            "chat_id": order.chat_id,
            "order": order,
            "mapping": mapping,
            "login": None,
            "asked_at": time.time(),
            "retries_left": int(R.settings.get("confirm_retries", 1)),
        }
        _ensure_confirm_loop(cardinal)
        return

    _ask_confirmation(cardinal, order, mapping, login, first=True)


def _ask_confirmation(cardinal: "Cardinal", order, mapping: dict[str, Any],
                      login: str, first: bool = True) -> None:
    yes = (R.settings.get("yes_aliases") or ["да"])[0]
    no = (R.settings.get("no_aliases") or ["нет"])[0]
    timeout = int(R.settings.get("confirm_timeout_min", 5))
    prefix = "" if first else "Повторный запрос. "
    try:
        cardinal.send_message(
            order.chat_id,
            _tpl(
                "tpl_confirm_request",
                prefix=prefix, login=login, yes_alias=yes, no_alias=no,
                timeout_min=timeout, order_id=order.id,
            ),
            order.buyer_username,
        )
    except Exception:
        logger.debug("NS.Gifts: send confirm request failed", exc_info=True)
    prev = R.pending_confirms.get(order.id) or {}
    retries_left = prev.get("retries_left")
    if retries_left is None:
        retries_left = int(R.settings.get("confirm_retries", 1))
    R.pending_confirms[order.id] = {
        "state": "awaiting_confirm",
        "order_id": order.id,
        "chat_id": order.chat_id,
        "order": order,
        "mapping": mapping,
        "login": login,
        "asked_at": time.time(),
        "retries_left": int(retries_left),
    }
    _ensure_confirm_loop(cardinal)


def _perform_delivery_with_login(cardinal: "Cardinal", order, mapping: dict[str, Any],
                                 login: str) -> tuple[bool, str]:
    target = mapping.get("account_target_field") or "account"
    return _perform_delivery(cardinal, order, mapping, dynamic_fields={target: login})


def _confirm_loop(cardinal: "Cardinal") -> None:
    while True:
        try:
            now = time.time()
            timeout_sec = max(60, int(R.settings.get("confirm_timeout_min", 5)) * 60)
            expired_keys: list[Any] = []
            with R.lock:
                for key, state in list(R.pending_confirms.items()):
                    if state.get("state") != "awaiting_confirm":
                        continue
                    if now - float(state.get("asked_at", now)) < timeout_sec:
                        continue
                    if int(state.get("retries_left", 0)) > 0:
                        state["retries_left"] = int(state["retries_left"]) - 1
                        # asked_at обновим в _ask_confirmation
                        order = state["order"]
                        mapping = state["mapping"]
                        login = state["login"]
                        threading.Thread(
                            target=_ask_confirmation,
                            args=(cardinal, order, mapping, login),
                            kwargs={"first": False},
                            daemon=True,
                        ).start()
                    else:
                        expired_keys.append(key)
                for k in expired_keys:
                    st = R.pending_confirms.pop(k, None)
                    if st:
                        order = st.get("order")
                        order_id = st.get("order_id")
                        _notify_tg(
                            cardinal,
                            f"⏰ <b>NS.Gifts</b>: покупатель не подтвердил логин Steam.\n"
                            f"Заказ: <code>{order_id}</code>, логин: <code>{st.get('login')}</code>\n"
                            "Автопополнение отменено, возьмите выдачу вручную."
                        )
                        if order_id:
                            _db_upsert_delivery({
                                "order_id": str(order_id),
                                "status": "cancelled",
                                "error": "confirm timeout",
                                "completed_at": int(time.time()),
                            })
                        try:
                            if order is not None:
                                cardinal.send_message(
                                    order.chat_id,
                                    _tpl("tpl_confirm_timeout", order_id=order_id, login=st.get("login")),
                                    getattr(order, "buyer_username", None),
                                )
                        except Exception:
                            pass
        except Exception:
            logger.error("NS.Gifts: ошибка в _confirm_loop", exc_info=True)
        time.sleep(15)


def _ensure_confirm_loop(cardinal: "Cardinal") -> None:
    with R.lock:
        if R.confirm_loop_started:
            return
        R.confirm_loop_started = True
    t = threading.Thread(target=_confirm_loop, args=(cardinal,), daemon=True,
                         name="NSGiftsConfirmLoop")
    t.start()


# =========================================================================
# Чат-команды (BIND_TO_NEW_MESSAGE)
# =========================================================================

def chat_command_handler(cardinal: "Cardinal", event, *args):
    try:
        msg = getattr(event, "message", None)
        if msg is None:
            return
        if getattr(msg, "author_id", None) == getattr(cardinal.account, "id", -1):
            return
        text = (getattr(msg, "text", "") or "").strip()
        if not text:
            return
        low = text.lower().strip()

        chat_id = getattr(msg, "chat_id", None) or getattr(msg, "node_id", None)
        chat_name = getattr(msg, "chat_name", None) or getattr(msg, "author", None)
        if chat_id is None:
            return

        # 0) Сначала проверяем, ждём ли мы подтверждение/логин в этом чате
        if _handle_pending_confirm(cardinal, chat_id, chat_name, text, low):
            return

        if not R.settings.get("chat_commands_enabled"):
            return

        if any(low.startswith(a.lower()) for a in R.settings.get("cmd_balance_aliases", [])):
            _reply_balance(cardinal, chat_id, chat_name)
            return

        if any(low.startswith(a.lower()) for a in R.settings.get("cmd_status_aliases", [])):
            parts = text.split(maxsplit=1)
            if len(parts) > 1:
                _reply_order_status(cardinal, chat_id, chat_name, parts[1].strip())
            else:
                cardinal.send_message(chat_id, "Использование: !status <custom_id>", chat_name)
            return

        if any(low.startswith(a.lower()) for a in R.settings.get("cmd_price_aliases", [])):
            parts = text.split(maxsplit=1)
            query = parts[1].strip() if len(parts) > 1 else ""
            _reply_price(cardinal, chat_id, chat_name, query)
            return
    except Exception:
        logger.error("NS.Gifts: ошибка в chat_command_handler", exc_info=True)


def _find_pending_by_chat(chat_id) -> tuple[Any, dict[str, Any]] | None:
    """Находит ожидающее подтверждение/логин для данного чата.

    pending_confirms теперь ключуются по order_id (несколько заказов из
    одного чата больше не перетирают друг друга). Ответ покупателя относим
    к самому позднему запросу в этом чате."""
    best_key = None
    best_state = None
    best_ts = -1.0
    for key, st in R.pending_confirms.items():
        if st.get("chat_id") != chat_id:
            continue
        ts = float(st.get("asked_at", 0) or 0)
        if ts >= best_ts:
            best_ts = ts
            best_key = key
            best_state = st
    if best_state is None:
        return None
    return best_key, best_state


def _handle_pending_confirm(cardinal: "Cardinal", chat_id, chat_name,
                            text: str, low: str) -> bool:
    """Если в чате ждём ответ покупателя по Steam-логину — обрабатываем."""
    found = _find_pending_by_chat(chat_id)
    if not found:
        return False
    pc_key, state = found
    yes_aliases = [a.lower() for a in R.settings.get("yes_aliases", [])]
    no_aliases = [a.lower() for a in R.settings.get("no_aliases", [])]

    if state.get("state") == "awaiting_confirm":
        if low in yes_aliases:
            order = state["order"]
            mapping = state["mapping"]
            login = state["login"]
            R.pending_confirms.pop(pc_key, None)
            try:
                cardinal.send_message(
                    chat_id,
                    _tpl("tpl_confirm_accepted", login=login, order_id=getattr(order, "id", "")),
                    chat_name,
                )
            except Exception:
                pass
            threading.Thread(
                target=_run_confirmed_delivery,
                args=(cardinal, order, mapping, login),
                daemon=True,
            ).start()
            return True
        if low in no_aliases:
            order = state.get("order")
            order_id = state.get("order_id")
            login = state.get("login")
            R.pending_confirms.pop(pc_key, None)
            try:
                cardinal.send_message(
                    chat_id,
                    _tpl("tpl_confirm_cancelled", order_id=order_id, login=login),
                    chat_name,
                )
            except Exception:
                pass
            _db_upsert_delivery({
                "order_id": str(order_id),
                "status": "cancelled",
                "error": "buyer rejected confirm",
                "completed_at": int(time.time()),
            })
            _notify_tg(
                cardinal,
                f"⛔️ <b>NS.Gifts</b>: покупатель отказался от автопополнения.\n"
                f"Заказ: <code>{order_id}</code>, логин: <code>{login}</code>"
            )
            return True
        # иначе покупатель, видимо, вводит правильный логин — воспринять как новый логин
        new_login = text.split()[0] if text.split() else ""
        if new_login and new_login != state.get("login"):
            order = state["order"]
            mapping = state["mapping"]
            _start_confirmation_flow(cardinal, order, mapping, new_login)
            return True
        # нераспознанное сообщение — пропускаем, пусть идёт в остальные обработчики
        return False

    if state.get("state") == "awaiting_login":
        new_login = text.split()[0] if text.split() else ""
        if not new_login:
            return False
        order = state["order"]
        mapping = state["mapping"]
        _start_confirmation_flow(cardinal, order, mapping, new_login)
        return True

    return False


def _run_confirmed_delivery(cardinal: "Cardinal", order, mapping: dict[str, Any],
                            login: str) -> None:
    try:
        ok, text = _perform_delivery_with_login(cardinal, order, mapping, login)
        lot_id = mapping.get("lot_id")
        _report_delivery(cardinal, order, lot_id, ok, text)
    except Exception:
        logger.error("NS.Gifts: ошибка в _run_confirmed_delivery", exc_info=True)


def _reply_balance(cardinal: "Cardinal", chat_id, chat_name) -> None:
    if not R.client or not R.client.configured:
        cardinal.send_message(chat_id, "Плагин не настроен.", chat_name)
        return
    try:
        bal = R.client.check_balance()
        usd = float(bal.get("balance") or 0)
        parts = [f"💰 Баланс NS.Gifts: {usd:.4f} USD"]
        for cur in ("rub", "uah", "kzt"):
            rate = _usd_to(cur)
            parts.append(f"~{usd * rate:.2f} {_currency_symbol(cur)} (курс {rate:.2f})")
        cardinal.send_message(chat_id, "\n".join(parts), chat_name)
    except Exception as e:
        cardinal.send_message(chat_id, f"Ошибка получения баланса: {e}", chat_name)


def _reply_price(cardinal: "Cardinal", chat_id, chat_name, query: str) -> None:
    stock = _get_stock()
    if not stock:
        cardinal.send_message(chat_id, "Каталог временно недоступен.", chat_name)
        return

    if not query:
        cardinal.send_message(
            chat_id,
            "Использование: !price <service_id или часть названия> [rub|uah|kzt|usd|eur]",
            chat_name,
        )
        return

    # Парсим валюту в конце запроса
    parts = query.split()
    cur = R.settings.get("default_lot_currency", "rub").lower()
    if parts and parts[-1].lower() in SUPPORTED_CURRENCIES:
        cur = parts[-1].lower()
        parts = parts[:-1]
    real_query = " ".join(parts).strip()
    if not real_query:
        cardinal.send_message(chat_id, "Пустой запрос.", chat_name)
        return

    results: list[str] = []
    q_low = real_query.lower()
    q_int = int(real_query) if real_query.isdigit() else None

    for cat in stock.get("categories", []):
        for svc in cat.get("services", []):
            if q_int is not None and int(svc.get("service_id", -1)) == q_int:
                results = [_format_price_line(svc, cur)]
                break
            if q_low in (svc.get("service_name") or "").lower():
                results.append(_format_price_line(svc, cur))
        if q_int is not None and results:
            break

    if not results:
        cardinal.send_message(chat_id, "По запросу ничего не нашёл.", chat_name)
        return
    cardinal.send_message(chat_id, "\n".join(results[:15]), chat_name)


def _format_price_line(svc: dict[str, Any], currency: str) -> str:
    usd = float(svc.get("price") or 0)
    # используем глобальную наценку без per-lot (лот неизвестен)
    price = _calc_lot_price(usd, {}, currency)
    sym = _currency_symbol(currency) or currency.upper()
    return (
        f"• [{svc.get('service_id')}] {svc.get('service_name')} — "
        f"{price:.2f} {sym}, в наличии: {svc.get('in_stock')}"
    )


def _reply_order_status(cardinal: "Cardinal", chat_id, chat_name, custom_id: str) -> None:
    try:
        info = R.client.order_info(custom_id) if R.client else {}
        cardinal.send_message(
            chat_id,
            f"Статус заказа: {info.get('status_message', info.get('status'))}. "
            f"Товар: {info.get('product', '—')}",
            chat_name,
        )
    except NSClientError as e:
        cardinal.send_message(chat_id, f"NS API: {e.message}", chat_name)
    except Exception as e:
        cardinal.send_message(chat_id, f"Ошибка: {e}", chat_name)


# =========================================================================
# Авто-обновление цен
# =========================================================================

def _calc_lot_price(ns_price_usd: float, mapping: dict[str, Any],
                    currency: str = "rub") -> float:
    """Рассчитывает финальную цену лота в указанной валюте."""
    cur = (currency or "rub").lower()
    rate = _usd_to(cur)
    markup_pct = mapping.get("markup_percent")
    if markup_pct is None:
        markup_pct = R.settings.get("global_markup_percent", 0)
    fixed = R.settings.get("fixed_markup")
    if fixed is None and cur == "rub":
        fixed = R.settings.get("fixed_markup_rub", 0)
    fixed = float(fixed or 0)
    min_price = mapping.get("min_price")
    if min_price is None and cur == "rub":
        min_price = mapping.get("min_price_rub")
    if min_price is None:
        min_price = R.settings.get("min_price")
    if min_price is None and cur == "rub":
        min_price = R.settings.get("min_price_rub", 0)
    price = ns_price_usd * rate * (1.0 + float(markup_pct) / 100.0) + fixed
    if min_price and price < float(min_price):
        price = float(min_price)
    return round(price, 2)


# back-compat shim
def _calc_lot_price_rub(ns_price_usd: float, mapping: dict[str, Any]) -> float:
    return _calc_lot_price(ns_price_usd, mapping, "rub")


# ── Общая либа: actions.log + raise-skip ────────────────────────────────────
# ── Встроенная либа lot-activation ─────────────────────────────────────────
_CARDINAL_REF_NS = None


def _shared_raise_state_ns(cardinal):
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


def _install_raise_skip_shared_ns(cardinal) -> bool:
    st = _shared_raise_state_ns(cardinal)
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
    logger.info("NS.Gifts: установлен общий патч raise_lots")
    return True


def _register_skip_ns(cardinal, plugin_name: str, category_ids):
    st = _shared_raise_state_ns(cardinal)
    if st is None:
        return
    st["by_plugin"][plugin_name] = {int(x) for x in category_ids
                                      if x is not None}


def _get_funpay_account_ns(cardinal):
    if cardinal is None:
        return None
    acc = getattr(cardinal, "account", None)
    if acc is not None and (hasattr(acc, "save_lot")
                            or hasattr(acc, "save_offer")):
        return acc
    return None


def _detect_category_id_ns(cardinal, lot_id: int):
    acc = _get_funpay_account_ns(cardinal)
    if acc is None or not hasattr(acc, "get_lot_fields"):
        return None
    try:
        fields = acc.get_lot_fields(int(lot_id))
    except Exception:
        return None
    cat = getattr(getattr(fields, "subcategory", None), "category", None)
    cid = getattr(cat, "id", None)
    return int(cid) if cid is not None else None


_ACTIONS_ICONS_NS = {
    "lot_activated":   "✅ ЛОТ ВКЛ ",
    "lot_deactivated": "⛔ ЛОТ ВЫКЛ",
    "lot_save_failed": "⚠ ЛОТ FAIL",
    "sync_prices":     "💱 ЦЕНЫ    ",
    "stock_sync":      "📦 СКЛАД   ",
    "delivery":        "📨 ВЫДАЧА  ",
    "raise_skipped":   "🚫 RAISE   ",
}


def _make_actions_logger_ns(plugin_name: str, storage_dir: str):
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


def _do_log_action_ns(lg, action: str, summary: str = "", **extra) -> None:
    if lg is None:
        return
    icon = _ACTIONS_ICONS_NS.get(action, f"• {action:10}")
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


def _common_lib_ns():
    try:
        import lot_activation_common  # type: ignore
        return lot_activation_common
    except Exception:
        pass

    class _Shim:
        @staticmethod
        def install_raise_skip_patch(c):
            return _install_raise_skip_shared_ns(c)

        @staticmethod
        def register_skip_categories(pname, ids):
            _register_skip_ns(_CARDINAL_REF_NS, pname, ids)

        @staticmethod
        def detect_category_id(c, lid):
            return _detect_category_id_ns(c, int(lid))

        @staticmethod
        def make_actions_logger(pname, sdir):
            return _make_actions_logger_ns(pname, sdir)

        @staticmethod
        def log_action(lg, action, summary="", **extra):
            _do_log_action_ns(lg, action, summary, **extra)

    return _Shim()


_actions_logger_ns: "logging.Logger | None" = None


def _get_actions_logger_ns():
    global _actions_logger_ns
    if _actions_logger_ns is not None:
        return _actions_logger_ns
    lib = _common_lib_ns()
    if lib is None:
        return None
    _actions_logger_ns = lib.make_actions_logger(
        "ns_gifts_fpc", str(STORAGE_DIR))
    return _actions_logger_ns


def _log_action_ns(action: str, summary: str = "", **extra: Any) -> None:
    lib = _common_lib_ns()
    if lib is None:
        return
    lib.log_action(_get_actions_logger_ns(), action, summary, **extra)


def _refresh_raise_skip_ns(cardinal: "Cardinal") -> None:
    """Собирает category_id наших NS-лотов и регистрирует в общей либе."""
    lib = _common_lib_ns()
    if lib is None or cardinal is None:
        return
    cat_ids: set[int] = set()
    for lot_id in (R.mappings.get("lots") or {}).keys():
        if str(lot_id).startswith("_"):
            continue
        try:
            lid = int(lot_id)
        except (TypeError, ValueError):
            continue
        cid = lib.detect_category_id(cardinal, lid)
        if cid is not None:
            cat_ids.add(int(cid))
    lib.register_skip_categories("ns_gifts_fpc", cat_ids)
    if cat_ids:
        logger.info("NS.Gifts: raise-skip категории: %s", sorted(cat_ids))


def _sync_prices_once(cardinal: "Cardinal") -> dict[str, Any]:
    if not R.client or not R.client.configured:
        return {"ok": False, "error": "NS-учётка не настроена."}

    try:
        stock = _get_stock(force=True)
    except Exception as e:
        return {"ok": False, "error": f"stock err: {e}"}

    svc_by_id: dict[int, dict[str, Any]] = {}
    for cat in stock.get("categories", []):
        for svc in cat.get("services", []):
            svc_by_id[int(svc["service_id"])] = svc

    auto_off = bool(R.settings.get("auto_disable_oos_lots"))
    auto_on = bool(R.settings.get("auto_activate_back_in_stock"))
    state_map = R.mappings.setdefault("_state", {})
    updated, skipped, errors, toggled = [], [], [], []

    for lot_id, mapping in (R.mappings.get("lots") or {}).items():
        if str(lot_id).startswith("_"):
            continue  # служебные ключи
        if not mapping.get("enabled", True):
            skipped.append(lot_id)
            continue
        svc = svc_by_id.get(int(mapping.get("service_id") or 0))
        if not svc:
            errors.append(f"{lot_id}: service_id не найден в /stock")
            continue
        usd = float(svc.get("price") or 0)
        in_stock = int(svc.get("in_stock") or 0)
        try:
            lf = cardinal.account.get_lot_fields(int(lot_id))
            old_price = lf.price
            cur = _resolve_lot_currency(lf, mapping)
            new_price = _calc_lot_price(usd, mapping, cur)
            patch = {"price": f"{new_price:.2f}"}

            # Auto-deactivation/activation logic
            #
            # ВАЖНО: в FunPayAPI.LotFields метод save_lot() вызывает
            # renew_fields(), который ПОЛНОСТЬЮ перезаписывает ключ
            # "active" в словаре полей исходя из СВОЙСТВА self.active.
            # Поэтому работа через edit_fields({"active": "" / "1"})
            # игнорируется. Правим именно lf.active (булево свойство).
            #
            # Дополнительно: при amount==0 LotFields форсит active=False
            # ("защита"). Для NS-донат-лотов amount обычно «∞», но если
            # вдруг 0 и нет auto_delivery с секретами — поднимаем до 1.
            lot_state = state_map.get(str(lot_id), {}) or {}
            deactivated_by_us = bool(lot_state.get("deactivated_by_plugin"))
            auto_deact_lot = mapping.get("auto_deactivate")
            if auto_deact_lot is None:
                auto_deact_lot = auto_off
            toggle_action = None
            if auto_deact_lot and in_stock == 0 and not deactivated_by_us:
                lf.active = False
                state_map[str(lot_id)] = {"deactivated_by_plugin": True}
                toggle_action = "deactivated"
                toggled.append({"lot_id": lot_id, "action": "deactivated", "in_stock": in_stock})
            elif auto_on and in_stock > 0 and deactivated_by_us:
                if (getattr(lf, "amount", None) in (None, 0)
                        and not getattr(lf, "auto_delivery", False)):
                    try:
                        lf.amount = 1
                    except Exception:
                        pass
                lf.active = True
                state_map.pop(str(lot_id), None)
                toggle_action = "reactivated"
                toggled.append({"lot_id": lot_id, "action": "reactivated", "in_stock": in_stock})

            lf.edit_fields(patch)
            cardinal.account.save_lot(lf)
            # actions.log
            if toggle_action == "deactivated":
                _log_action_ns("lot_deactivated",
                                f"Лот {lot_id} деактивирован — нет в наличии",
                                lot_id=lot_id, in_stock=in_stock,
                                ns_usd=usd)
            elif toggle_action == "reactivated":
                _log_action_ns("lot_activated",
                                f"Лот {lot_id} активирован — есть в наличии",
                                lot_id=lot_id, in_stock=in_stock,
                                ns_usd=usd)
            updated.append({
                "lot_id": lot_id, "old": old_price, "new": new_price,
                "currency": cur,
                "ns_usd": usd, "in_stock": in_stock,
            })
        except Exception as e:
            errors.append(f"{lot_id}: {e}")

    if toggled:
        try:
            _save_mappings()
        except Exception:
            logger.debug("NS.Gifts: save mappings (state) failed", exc_info=True)

    return {"ok": True, "updated": updated, "skipped": skipped,
            "errors": errors, "toggled": toggled}


def _price_sync_loop(cardinal: "Cardinal") -> None:
    while True:
        interval = 1800  # дефолт на случай сбоя до вычисления интервала
        try:
            interval = max(5, int(R.settings.get("price_sync_interval_min", 30))) * 60
            if R.settings.get("autoprice_enabled"):
                result = _sync_prices_once(cardinal)
                if result.get("updated"):
                    logger.info(
                        f"NS.Gifts: цены обновлены для {len(result['updated'])} лотов."
                    )
                for err in result.get("errors", []):
                    logger.warning(f"NS.Gifts price sync: {err}")
        except Exception:
            logger.error("NS.Gifts: ошибка цикла авто-цен", exc_info=True)
        time.sleep(interval)


def _ensure_price_loop(cardinal: "Cardinal") -> None:
    with R.lock:
        if R.price_loop_started:
            return
        R.price_loop_started = True
    t = threading.Thread(target=_price_sync_loop, args=(cardinal,), daemon=True,
                         name="NSGiftsPriceSync")
    t.start()


# =========================================================================
# Low-balance уведомление
# =========================================================================

def _maybe_low_balance_alert(cardinal: "Cardinal") -> None:
    try:
        threshold = float(R.settings.get("low_balance_threshold_usd", 0))
        if threshold <= 0 or not R.client:
            return
        bal = R.client.check_balance()
        usd = float(bal.get("balance") or 0)
        if usd < threshold:
            _notify_tg(
                cardinal,
                f"⚠️ <b>Низкий баланс NS.Gifts:</b> {usd:.4f} USD (порог {threshold} USD).",
            )
    except Exception:
        logger.debug("NS.Gifts: low_balance check failed", exc_info=True)


# =========================================================================
# Telegram: панель + кнопки
# =========================================================================

CBP = "nsgifts"  # prefix


def _tg(cardinal: "Cardinal"):
    return getattr(cardinal, "telegram", None)


def _is_authorized(cardinal: "Cardinal", user_id: int) -> bool:
    tg = _tg(cardinal)
    if tg is None:
        return False
    try:
        return user_id in tg.authorized_users
    except Exception:
        return False


def _notify_tg(cardinal: "Cardinal", text: str) -> None:
    tg = _tg(cardinal)
    if tg is None:
        return
    chat_id = int(R.settings.get("notify_chat_id") or 0)
    if not chat_id:
        return
    try:
        tg.bot.send_message(chat_id, text, parse_mode="HTML")
    except Exception:
        logger.debug("NS.Gifts: send tg notify failed", exc_info=True)


def _rate_display(v: Any) -> str:
    try:
        v = float(v or 0)
    except Exception:
        v = 0
    return "авто" if v <= 0 else f"{v:.2f}"


def _panel_text() -> str:
    on = "🟢"
    off = "🔴"
    s = R.settings
    cfg = "✅" if (R.client and R.client.configured) else "❌"
    confirm_state = on if s.get("confirm_steam_login") else off
    fixed = s.get("fixed_markup")
    if fixed is None:
        fixed = s.get("fixed_markup_rub", 0)
    min_p = s.get("min_price")
    if min_p is None:
        min_p = s.get("min_price_rub", 0)
    lines = [
        "<b>NS.Gifts — управление</b>",
        f"Учётка NS: {cfg}",
        f"Привязок лотов: <code>{len(R.mappings.get('lots') or {})}</code>",
        "",
        f"{on if s['autodelivery_enabled'] else off} Автовыдача",
        f"{on if s['autoprice_enabled'] else off} Автоцены (раз в {s['price_sync_interval_min']} мин)",
        f"{on if s['chat_commands_enabled'] else off} Чат-команды",
        f"{confirm_state} Подтверждение Steam-логина",
        "",
        f"Наценка глобал: <b>{s['global_markup_percent']:.1f}%</b>",
        f"Фикс. надбавка: <b>{float(fixed):.2f}</b>",
        f"Валюта по умолчанию: <b>{s.get('default_lot_currency', 'rub').upper()}</b>",
        f"Курсы USD→: "
        f"₽ <b>{_rate_display(s.get('usd_to_rub_rate'))}</b>, "
        f"₴ <b>{_rate_display(s.get('usd_to_uah_rate'))}</b>, "
        f"₸ <b>{_rate_display(s.get('usd_to_kzt_rate'))}</b>",
        f"Минимальная цена: <b>{float(min_p):.2f}</b>",
    ]
    return "\n".join(lines)


def _panel_kb() -> K:
    kb = K(row_width=2)
    kb.add(
        B("🚚 Выдача", callback_data=f"{CBP}:delivery_settings"),
        B("💲 Цены", callback_data=f"{CBP}:price_settings"),
    )
    kb.add(
        B("♨️ Steam", callback_data=f"{CBP}:steam_settings"),
        B("🛠 Инструменты", callback_data=f"{CBP}:tools"),
    )
    kb.add(
        B("📊 Статистика", callback_data=f"{CBP}:stats"),
        B("📬 Шаблоны", callback_data=f"{CBP}:templates"),
    )
    kb.add(
        B("⚙️ Настройки", callback_data=f"{CBP}:settings"),
    )
    kb.add(B("🔄 Обновить", callback_data=f"{CBP}:refresh"))
    return kb


def _tools_text() -> str:
    s = R.settings
    on = "🟢"
    off = "🔴"
    return (
        "<b>NS.Gifts — инструменты</b>\n\n"
        f"{on if s['chat_commands_enabled'] else off} Чат-команды\n"
        f"{on if s.get('confirm_steam_login') else off} Подтверждение Steam-логина\n"
    )


def _tools_kb() -> K:
    s = R.settings
    on = "🟢"
    off = "🔴"
    kb = K(row_width=2)
    kb.add(
        B("💰 Баланс", callback_data=f"{CBP}:balance"),
        B("💱 Курсы", callback_data=f"{CBP}:rates"),
    )
    kb.add(
        B("📦 Каталог", callback_data=f"{CBP}:stock"),
        B("🔁 Синхр. цены", callback_data=f"{CBP}:sync_now"),
    )
    kb.add(
        B("📝 Привязки", callback_data=f"{CBP}:mappings"),
    )
    kb.add(
        B(f"{on if s['chat_commands_enabled'] else off} Чат-команды",
          callback_data=f"{CBP}:toggle:chat_commands_enabled"),
        B(f"{on if s.get('confirm_steam_login') else off} Подтв. Steam-логина",
          callback_data=f"{CBP}:toggle:confirm_steam_login"),
    )
    kb.add(B("◀️ Назад", callback_data=f"{CBP}:home"))
    return kb


def _settings_kb() -> K:
    kb = K(row_width=2)
    kb.add(
        B("Наценка %", callback_data=f"{CBP}:set:global_markup_percent"),
        B("Фикс. надбавка", callback_data=f"{CBP}:set:fixed_markup"),
    )
    kb.add(
        B("Мин. цена", callback_data=f"{CBP}:set:min_price"),
        B("Валюта по умолч.", callback_data=f"{CBP}:set:default_lot_currency"),
    )
    kb.add(
        B("Курс USD→₽", callback_data=f"{CBP}:set:usd_to_rub_rate"),
        B("Курс USD→₴", callback_data=f"{CBP}:set:usd_to_uah_rate"),
    )
    kb.add(
        B("Курс USD→₸", callback_data=f"{CBP}:set:usd_to_kzt_rate"),
        B("Курс USD→€", callback_data=f"{CBP}:set:usd_to_eur_rate"),
    )
    kb.add(
        B("Интервал синхр. (мин)", callback_data=f"{CBP}:set:price_sync_interval_min"),
        B("Порог low balance USD", callback_data=f"{CBP}:set:low_balance_threshold_usd"),
    )
    kb.add(
        B("Таймаут подтв. (мин)", callback_data=f"{CBP}:set:confirm_timeout_min"),
        B("Повторы подтв.", callback_data=f"{CBP}:set:confirm_retries"),
    )
    kb.add(
        B("Защита от убытков", callback_data=f"{CBP}:toggle:loss_protection_enabled"),
        B("Safety margin %", callback_data=f"{CBP}:set:loss_safety_margin_percent"),
    )
    kb.add(
        B("Авто-откл. OOS", callback_data=f"{CBP}:toggle:auto_disable_oos_lots"),
        B("Авто-вкл. обратно", callback_data=f"{CBP}:toggle:auto_activate_back_in_stock"),
    )
    kb.add(
        B("Working Hours", callback_data=f"{CBP}:toggle:working_hours_enabled"),
        B("WH Start", callback_data=f"{CBP}:set:working_hours_start"),
    )
    kb.add(
        B("WH End", callback_data=f"{CBP}:set:working_hours_end"),
    )
    kb.add(
        B("Retry failed", callback_data=f"{CBP}:toggle:retry_failed_orders"),
        B("📬 Шаблоны", callback_data=f"{CBP}:templates"),
    )
    kb.add(
        B("📥 Загрузить NS-учётку", callback_data=f"{CBP}:set_secrets"),
        B("📥 Загрузить mappings.json", callback_data=f"{CBP}:upload_mappings"),
    )
    kb.add(B("◀️ Назад", callback_data=f"{CBP}:home"))
    return kb


def _settings_text() -> str:
    s = R.settings
    fixed = s.get("fixed_markup")
    if fixed is None:
        fixed = s.get("fixed_markup_rub", 0)
    min_p = s.get("min_price")
    if min_p is None:
        min_p = s.get("min_price_rub", 0)
    return (
        "<b>NS.Gifts — настройки</b>\n\n"
        f"Глобальная наценка: <b>{s['global_markup_percent']}%</b>\n"
        f"Фикс. надбавка (в валюте лота): <b>{fixed}</b>\n"
        f"Мин. цена (в валюте лота): <b>{min_p}</b>\n"
        f"Валюта по умолчанию: <b>{s.get('default_lot_currency', 'rub').upper()}</b>\n\n"
        "<b>Курсы USD→ (0 = авто из NS):</b>\n"
        f"₽ <b>{s.get('usd_to_rub_rate')}</b> (фолбэк {s.get('usd_to_rub_fallback')})\n"
        f"₴ <b>{s.get('usd_to_uah_rate')}</b> (фолбэк {s.get('usd_to_uah_fallback')})\n"
        f"₸ <b>{s.get('usd_to_kzt_rate')}</b> (фолбэк {s.get('usd_to_kzt_fallback')})\n"
        f"€ <b>{s.get('usd_to_eur_rate')}</b> (ручной)\n\n"
        f"Интервал авто-цен: <b>{s['price_sync_interval_min']} мин</b>\n"
        f"Low-balance порог: <b>{s['low_balance_threshold_usd']} USD</b>\n\n"
        "<b>Steam-логин:</b>\n"
        f"Подтверждение: <b>{'да' if s.get('confirm_steam_login') else 'нет'}</b>\n"
        f"Таймаут ожидания: <b>{s.get('confirm_timeout_min', 5)} мин</b>, повторов: <b>{s.get('confirm_retries', 1)}</b>\n\n"
        "Чтобы изменить — нажмите кнопку и пришлите новое значение."
    )


def _delivery_settings_kb() -> K:
    """Sub-menu for delivery-related settings."""
    s = R.settings
    on = "🟢"
    off = "🔴"
    kb = K(row_width=2)
    kb.add(
        B(f"{on if s['autodelivery_enabled'] else off} Автовыдача",
          callback_data=f"{CBP}:toggle:autodelivery_enabled"),
        B(f"{on if s.get('working_hours_enabled') else off} Working Hours",
          callback_data=f"{CBP}:toggle:working_hours_enabled"),
    )
    kb.add(
        B("WH Start", callback_data=f"{CBP}:set:working_hours_start"),
        B("WH End", callback_data=f"{CBP}:set:working_hours_end"),
    )
    kb.add(
        B("Защита от убытков", callback_data=f"{CBP}:toggle:loss_protection_enabled"),
        B("Safety margin %", callback_data=f"{CBP}:set:loss_safety_margin_percent"),
    )
    kb.add(
        B("Авто-откл. OOS", callback_data=f"{CBP}:toggle:auto_disable_oos_lots"),
        B("Авто-вкл. обратно", callback_data=f"{CBP}:toggle:auto_activate_back_in_stock"),
    )
    kb.add(
        B("Retry failed", callback_data=f"{CBP}:toggle:retry_failed_orders"),
    )
    kb.add(B("◀️ Назад", callback_data=f"{CBP}:home"))
    return kb


def _delivery_settings_text() -> str:
    s = R.settings
    on = "🟢"
    off = "🔴"
    wh_status = ""
    if s.get("working_hours_enabled"):
        wh_status = f" ({s.get('working_hours_start', 9)}:00 - {s.get('working_hours_end', 23)}:00)"
    return (
        "<b>NS.Gifts — настройки выдачи</b>\n\n"
        f"{on if s['autodelivery_enabled'] else off} Автовыдача\n"
        f"{on if s.get('working_hours_enabled') else off} Рабочие часы{wh_status}\n"
        f"{on if s.get('loss_protection_enabled') else off} Защита от убытков "
        f"(margin {s.get('loss_safety_margin_percent', 0)}%)\n"
        f"{on if s.get('auto_disable_oos_lots') else off} Авто-откл. при OOS\n"
        f"{on if s.get('auto_activate_back_in_stock') else off} Авто-вкл. обратно\n"
        f"{on if s.get('retry_failed_orders') else off} Повтор failed заказов\n"
    )


def _price_settings_kb() -> K:
    """Sub-menu for price-related settings."""
    kb = K(row_width=2)
    kb.add(
        B("Наценка %", callback_data=f"{CBP}:set:global_markup_percent"),
        B("Фикс. надбавка", callback_data=f"{CBP}:set:fixed_markup"),
    )
    kb.add(
        B("Мин. цена", callback_data=f"{CBP}:set:min_price"),
        B("Валюта по умолч.", callback_data=f"{CBP}:set:default_lot_currency"),
    )
    kb.add(
        B("Курс USD→₽", callback_data=f"{CBP}:set:usd_to_rub_rate"),
        B("Курс USD→₴", callback_data=f"{CBP}:set:usd_to_uah_rate"),
    )
    kb.add(
        B("Курс USD→₸", callback_data=f"{CBP}:set:usd_to_kzt_rate"),
        B("Курс USD→€", callback_data=f"{CBP}:set:usd_to_eur_rate"),
    )
    kb.add(
        B("Интервал синхр. (мин)", callback_data=f"{CBP}:set:price_sync_interval_min"),
        B("Порог low balance USD", callback_data=f"{CBP}:set:low_balance_threshold_usd"),
    )
    kb.add(B("◀️ Назад", callback_data=f"{CBP}:home"))
    return kb


def _price_settings_text() -> str:
    s = R.settings
    fixed = s.get("fixed_markup")
    if fixed is None:
        fixed = s.get("fixed_markup_rub", 0)
    min_p = s.get("min_price")
    if min_p is None:
        min_p = s.get("min_price_rub", 0)
    return (
        "<b>NS.Gifts — настройки цен</b>\n\n"
        f"Глобальная наценка: <b>{s['global_markup_percent']}%</b>\n"
        f"Фикс. надбавка: <b>{float(fixed):.2f}</b>\n"
        f"Мин. цена: <b>{float(min_p):.2f}</b>\n"
        f"Валюта по умолчанию: <b>{s.get('default_lot_currency', 'rub').upper()}</b>\n\n"
        "<b>Курсы USD→ (0 = авто из NS):</b>\n"
        f"₽ <b>{s.get('usd_to_rub_rate')}</b> (фолбэк {s.get('usd_to_rub_fallback')})\n"
        f"₴ <b>{s.get('usd_to_uah_rate')}</b> (фолбэк {s.get('usd_to_uah_fallback')})\n"
        f"₸ <b>{s.get('usd_to_kzt_rate')}</b> (фолбэк {s.get('usd_to_kzt_fallback')})\n"
        f"€ <b>{s.get('usd_to_eur_rate')}</b> (ручной)\n\n"
        f"Интервал авто-цен: <b>{s['price_sync_interval_min']} мин</b>\n"
        f"Low-balance порог: <b>{s['low_balance_threshold_usd']} USD</b>"
    )


def _steam_settings_kb() -> K:
    """Sub-menu for Steam confirmation settings."""
    s = R.settings
    on = "🟢"
    off = "🔴"
    kb = K(row_width=2)
    kb.add(
        B(f"{on if s.get('confirm_steam_login') else off} Подтв. Steam-логина",
          callback_data=f"{CBP}:toggle:confirm_steam_login"),
    )
    kb.add(
        B("Таймаут подтв. (мин)", callback_data=f"{CBP}:set:confirm_timeout_min"),
        B("Повторы подтв.", callback_data=f"{CBP}:set:confirm_retries"),
    )
    kb.add(B("◀️ Назад", callback_data=f"{CBP}:home"))
    return kb


def _steam_settings_text() -> str:
    s = R.settings
    on = "🟢"
    off = "🔴"
    return (
        "<b>NS.Gifts — Steam-настройки</b>\n\n"
        f"{on if s.get('confirm_steam_login') else off} Подтверждение Steam-логина\n"
        f"Таймаут ожидания: <b>{s.get('confirm_timeout_min', 5)} мин</b>\n"
        f"Повторов запроса: <b>{s.get('confirm_retries', 1)}</b>"
    )


def _open_panel(cardinal: "Cardinal", chat_id: int, message_id: int | None = None) -> None:
    tg = _tg(cardinal)
    if tg is None:
        return
    try:
        if message_id:
            tg.bot.edit_message_text(_panel_text(), chat_id, message_id,
                                     parse_mode="HTML", reply_markup=_panel_kb())
        else:
            tg.bot.send_message(chat_id, _panel_text(), parse_mode="HTML",
                                reply_markup=_panel_kb())
    except Exception:
        logger.error("NS.Gifts: open_panel failed", exc_info=True)


def _handle_callback(cardinal: "Cardinal", call) -> None:
    if not R.tg_registered:
        logger.info("NS.Gifts: late TG handler registration triggered from callback")
        _register_tg_handlers(cardinal)
    tg = _tg(cardinal)
    if tg is None:
        return
    if not _is_authorized(cardinal, call.from_user.id):
        try:
            tg.bot.answer_callback_query(call.id, "Нет прав.", show_alert=True)
        except Exception:
            pass
        return

    data = call.data or ""

    # Открытие из настроек плагина FPC: PLUGIN_SETTINGS:<UUID>
    if data.startswith(f"{CBT.PLUGIN_SETTINGS}:{UUID}") or data.startswith(f"47:{UUID}") or data == f"{CBP}:home" or data == f"{CBP}:refresh":
        R.settings.setdefault("notify_chat_id", call.message.chat.id)
        if not R.settings.get("notify_chat_id"):
            R.settings["notify_chat_id"] = call.message.chat.id
            _save_settings()
        _open_panel(cardinal, call.message.chat.id, call.message.message_id)
        try:
            tg.bot.answer_callback_query(call.id)
        except Exception:
            pass
        return

    if not data.startswith(f"{CBP}:"):
        return

    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "toggle" and len(parts) >= 3:
        key = parts[2]
        if key in R.settings and isinstance(R.settings[key], bool):
            R.settings[key] = not R.settings[key]
            _save_settings()
        _open_panel(cardinal, call.message.chat.id, call.message.message_id)
        try:
            tg.bot.answer_callback_query(call.id)
        except Exception:
            pass
        return

    if action == "balance":
        try:
            bal = R.client.check_balance() if R.client else {}
            usd = float(bal.get("balance") or 0)
            rub = usd * _usd_to("rub")
            uah = usd * _usd_to("uah")
            kzt = usd * _usd_to("kzt")
            tg.bot.answer_callback_query(
                call.id,
                f"Баланс: {usd:.4f} USD\n~{rub:.2f} ₽, {uah:.2f} ₴, {kzt:.2f} ₸",
                show_alert=True,
            )
        except Exception as e:
            tg.bot.answer_callback_query(call.id, f"Ошибка: {e}", show_alert=True)
        return

    if action == "rates":
        rub = _usd_to("rub")
        uah = _usd_to("uah")
        kzt = _usd_to("kzt")
        eur = _usd_to("eur")
        try:
            tg.bot.answer_callback_query(
                call.id,
                f"1 USD = {rub:.2f} ₽ / {uah:.2f} ₴ / {kzt:.2f} ₸ / {eur:.4f} €",
                show_alert=True,
            )
        except Exception:
            pass
        return

    if action == "stock":
        try:
            stock = _get_stock(force=True)
            cats = stock.get("categories", [])
            lines = [f"📦 <b>Категорий:</b> {len(cats)}"]
            for cat in cats[:10]:
                lines.append(f"\n<b>{cat.get('category_name')}</b>")
                for svc in cat.get("services", [])[:6]:
                    lines.append(
                        f"• [{svc['service_id']}] {svc['service_name']} — "
                        f"{svc['price']} USD, x{svc.get('in_stock')}"
                    )
            tg.bot.send_message(call.message.chat.id, "\n".join(lines), parse_mode="HTML")
        except Exception as e:
            tg.bot.send_message(call.message.chat.id, f"Ошибка /stock: {e}")
        try:
            tg.bot.answer_callback_query(call.id)
        except Exception:
            pass
        return

    if action == "sync_now":
        tg.bot.answer_callback_query(call.id, "Запускаю синхронизацию…")
        threading.Thread(target=_sync_and_report, args=(cardinal, call.message.chat.id), daemon=True).start()
        return

    if action == "mappings":
        lots = R.mappings.get("lots") or {}
        if not lots:
            text = (
                "Привязок пока нет.\n\n"
                "Создайте файл <code>storage/plugins/"
                f"{UUID}/mappings.json</code> или загрузите через кнопку в настройках.\n"
                "Формат:\n<pre>"
                '{"lots": {"123456": {"service_id": 449, "type": "code", '
                '"amount_field": "quantity", "extra_fields": {}, "markup_percent": null, "enabled": true}}}'
                "</pre>"
            )
        else:
            rows = []
            for lot_id, m in list(lots.items())[:40]:
                rows.append(
                    f"• lot <code>{lot_id}</code> → svc <code>{m.get('service_id')}</code> "
                    f"({m.get('type', 'code')}), markup={m.get('markup_percent', 'глобал')}, "
                    f"{'ON' if m.get('enabled', True) else 'OFF'}"
                )
            text = f"<b>Привязки ({len(lots)}):</b>\n" + "\n".join(rows)
        tg.bot.send_message(call.message.chat.id, text, parse_mode="HTML")
        try:
            tg.bot.answer_callback_query(call.id)
        except Exception:
            pass
        return

    if action == "settings":
        try:
            tg.bot.edit_message_text(_settings_text(), call.message.chat.id,
                                     call.message.message_id, parse_mode="HTML",
                                     reply_markup=_settings_kb())
        except Exception:
            pass
        try:
            tg.bot.answer_callback_query(call.id)
        except Exception:
            pass
        return

    if action == "delivery_settings":
        try:
            tg.bot.edit_message_text(_delivery_settings_text(), call.message.chat.id,
                                     call.message.message_id, parse_mode="HTML",
                                     reply_markup=_delivery_settings_kb())
        except Exception:
            pass
        try:
            tg.bot.answer_callback_query(call.id)
        except Exception:
            pass
        return

    if action == "price_settings":
        try:
            tg.bot.edit_message_text(_price_settings_text(), call.message.chat.id,
                                     call.message.message_id, parse_mode="HTML",
                                     reply_markup=_price_settings_kb())
        except Exception:
            pass
        try:
            tg.bot.answer_callback_query(call.id)
        except Exception:
            pass
        return

    if action == "steam_settings":
        try:
            tg.bot.edit_message_text(_steam_settings_text(), call.message.chat.id,
                                     call.message.message_id, parse_mode="HTML",
                                     reply_markup=_steam_settings_kb())
        except Exception:
            pass
        try:
            tg.bot.answer_callback_query(call.id)
        except Exception:
            pass
        return

    if action == "tools":
        try:
            tg.bot.edit_message_text(_tools_text(), call.message.chat.id,
                                     call.message.message_id, parse_mode="HTML",
                                     reply_markup=_tools_kb())
        except Exception:
            pass
        try:
            tg.bot.answer_callback_query(call.id)
        except Exception:
            pass
        return

    if action == "set" and len(parts) >= 3:
        key = parts[2]
        R.pending_tg_states[(call.message.chat.id, call.from_user.id)] = f"set:{key}"
        desc = SETTING_DESCRIPTIONS.get(key, "")
        desc_text = f"\n\n<i>{desc}</i>" if desc else ""
        try:
            tg.bot.send_message(
                call.message.chat.id,
                f"Введите новое значение для <code>{key}</code> (текущее: <code>{R.settings.get(key)}</code>).{desc_text}\n"
                "Пришлите следующим сообщением.",
                parse_mode="HTML",
            )
        except Exception:
            pass
        try:
            tg.bot.answer_callback_query(call.id)
        except Exception:
            pass
        return

    if action == "set_secrets":
        R.pending_tg_states[(call.message.chat.id, call.from_user.id)] = "secrets"
        tg.bot.send_message(
            call.message.chat.id,
            (
                "Пришлите JSON с учётными данными NS.Gifts следующим сообщением:\n"
                "<pre>"
                '{\n'
                '  "user_id": 1234,\n'
                '  "login": "your_login",\n'
                '  "password": "your_password",\n'
                '  "api_secret": "BASE64-SECRET",\n'
                '  "base_url": "https://api.ns.gifts",\n'
                '  "totp_code": ""\n'
                '}'
                "</pre>\n"
                "Или присылайте по одному ключу: <code>user_id=1234</code>, "
                "<code>login=...</code>, и т.д."
            ),
            parse_mode="HTML",
        )
        try:
            tg.bot.answer_callback_query(call.id)
        except Exception:
            pass
        return

    if action == "upload_mappings":
        R.pending_tg_states[(call.message.chat.id, call.from_user.id)] = "mappings_json"
        tg.bot.send_message(
            call.message.chat.id,
            "Пришлите содержимое <code>mappings.json</code> следующим сообщением "
            "(текст или файл .json).",
            parse_mode="HTML",
        )
        try:
            tg.bot.answer_callback_query(call.id)
        except Exception:
            pass
        return

    if action == "stats":
        try:
            tg.bot.edit_message_text(
                _stats_text(), call.message.chat.id, call.message.message_id,
                parse_mode="HTML", reply_markup=_stats_kb(),
            )
        except Exception:
            tg.bot.send_message(call.message.chat.id, _stats_text(), parse_mode="HTML",
                                reply_markup=_stats_kb())
        try:
            tg.bot.answer_callback_query(call.id)
        except Exception:
            pass
        return

    if action == "stats_period" and len(parts) >= 3:
        try:
            tg.bot.edit_message_text(
                _stats_text(parts[2]), call.message.chat.id, call.message.message_id,
                parse_mode="HTML", reply_markup=_stats_kb(parts[2]),
            )
        except Exception:
            pass
        try:
            tg.bot.answer_callback_query(call.id)
        except Exception:
            pass
        return

    if action == "templates":
        try:
            tg.bot.edit_message_text(
                _templates_text(), call.message.chat.id, call.message.message_id,
                parse_mode="HTML", reply_markup=_templates_kb(),
            )
        except Exception:
            tg.bot.send_message(call.message.chat.id, _templates_text(),
                                parse_mode="HTML", reply_markup=_templates_kb())
        try:
            tg.bot.answer_callback_query(call.id)
        except Exception:
            pass
        return

    if action == "set_tpl" and len(parts) >= 3:
        key = parts[2]
        R.pending_tg_states[(call.message.chat.id, call.from_user.id)] = f"set_tpl:{key}"
        try:
            current = R.settings.get(key, DEFAULT_SETTINGS.get(key, ""))
            tg.bot.send_message(
                call.message.chat.id,
                f"Пришлите новый шаблон для <code>{key}</code> следующим сообщением.\n"
                f"Текущий:\n<pre>{_html_escape(str(current))}</pre>",
                parse_mode="HTML",
            )
        except Exception:
            pass
        try:
            tg.bot.answer_callback_query(call.id)
        except Exception:
            pass
        return


def _sync_and_report(cardinal: "Cardinal", chat_id: int) -> None:
    tg = _tg(cardinal)
    res = _sync_prices_once(cardinal)
    if not res.get("ok"):
        tg.bot.send_message(chat_id, f"Ошибка синхронизации: {res.get('error')}")
        return
    updated = res.get("updated") or []
    errors = res.get("errors") or []
    toggled = res.get("toggled") or []
    lines = [
        f"🔁 <b>Синхронизация цен</b>\nОбновлено: <b>{len(updated)}</b>, "
        f"ошибок: <b>{len(errors)}</b>, тогглов лотов: <b>{len(toggled)}</b>"
    ]
    for u in updated[:15]:
        sym = _currency_symbol(u.get("currency", "rub")) or u.get("currency", "").upper()
        lines.append(
            f"• lot <code>{u['lot_id']}</code>: {u['old']} → <b>{u['new']}</b> {sym} "
            f"(NS {u['ns_usd']} USD, x{u['in_stock']})"
        )
    if toggled:
        lines.append("\n<b>Авто-тоггл:</b>")
        for t in toggled[:10]:
            mark = "🔴 откл." if t["action"] == "deactivated" else "🟢 вкл."
            lines.append(f"• lot <code>{t['lot_id']}</code> — {mark} (stock={t['in_stock']})")
    if errors:
        lines.append("\n<b>Ошибки:</b>")
        lines.extend(f"• {e}" for e in errors[:10])
    try:
        tg.bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML")
    except Exception:
        pass


# =========================================================================
# TG: статистика + редактор шаблонов
# =========================================================================

def _html_escape(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _stats_text(period: str = "day") -> str:
    now = int(time.time())
    spans = {"day": 86400, "week": 7 * 86400, "month": 30 * 86400, "all": 365 * 50 * 86400}
    since = now - spans.get(period, 86400)
    s = _db_stats(since)
    label = {"day": "за сутки", "week": "за неделю",
             "month": "за месяц", "all": "за всё время"}.get(period, period)
    lines = [
        f"<b>NS.Gifts — статистика {label}</b>",
        f"Заказов выдано: <b>{s.get('count', 0)}</b>",
        f"Себестоимость всего: <b>{s.get('cost_usd', 0):.4f} USD</b>",
        f"Прибыль всего: <b>{s.get('profit_usd', 0):.4f} USD</b>",
        "",
    ]
    if not s.get("by_currency"):
        lines.append("По валютам данных нет (заказов в этом периоде не было).")
    else:
        lines.append("<b>По валютам:</b>")
        for cur, b in s["by_currency"].items():
            sym = _currency_symbol(cur) or cur.upper()
            lines.append(
                f"• {sym}: заказов <b>{b['count']}</b>, оборот "
                f"<b>{b['revenue']:.2f}</b>, прибыль <b>{b['profit']:.2f}</b>"
            )
    return "\n".join(lines)


def _stats_kb(active: str = "day") -> K:
    kb = K(row_width=4)
    def mk(period: str, label: str) -> B:
        prefix = "✅ " if period == active else ""
        return B(f"{prefix}{label}", callback_data=f"{CBP}:stats_period:{period}")
    kb.add(mk("day", "Сутки"), mk("week", "Неделя"),
           mk("month", "Месяц"), mk("all", "Всё"))
    kb.add(B("◀️ Назад", callback_data=f"{CBP}:home"))
    return kb


TEMPLATE_KEYS = [
    ("tpl_success", "✅ Успех"),
    ("tpl_success_async", "⏳ Асинхр. выдача"),
    ("tpl_failed", "❌ Ошибка"),
    ("tpl_insufficient", "⚠️ Недост. баланс"),
    ("tpl_login_request", "🔑 Запрос логина"),
    ("tpl_login_invalid", "❌ Неверн. логин"),
    ("tpl_confirm_request", "❓ Запрос подтв."),
    ("tpl_confirm_accepted", "✅ Принято"),
    ("tpl_confirm_cancelled", "⛔ Отмена"),
    ("tpl_confirm_timeout", "⏰ Таймаут"),
    ("tpl_loss_protection", "🛡 Защита от убытков"),
]


def _templates_text() -> str:
    lines = [
        "<b>NS.Gifts — шаблоны сообщений</b>",
        "",
        "Доступные переменные: <code>{order_id}</code>, <code>{login}</code>, "
        "<code>{service_name}</code>, <code>{pins}</code>, <code>{amount}</code>, "
        "<code>{error}</code>, <code>{status}</code>, <code>{yes_alias}</code>, "
        "<code>{no_alias}</code>, <code>{timeout_min}</code>, <code>{prefix}</code>.",
        "",
        "Нажмите кнопку и пришлите новый шаблон следующим сообщением.",
    ]
    return "\n".join(lines)


def _templates_kb() -> K:
    kb = K(row_width=2)
    for key, label in TEMPLATE_KEYS:
        kb.add(B(label, callback_data=f"{CBP}:set_tpl:{key}"))
    kb.add(B("◀️ Назад", callback_data=f"{CBP}:home"))
    return kb


def _handle_message(cardinal: "Cardinal", m) -> None:
    tg = _tg(cardinal)
    if tg is None:
        return
    if not _is_authorized(cardinal, m.from_user.id):
        return

    state = R.pending_tg_states.pop((m.chat.id, m.from_user.id), None)
    if not state:
        return

    text = (m.text or "").strip()
    content_type = m.content_type if hasattr(m, "content_type") else "text"

    if state == "secrets":
        # либо JSON, либо k=v строки
        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError("not dict")
        except Exception:
            data = {}
            for line in text.replace(",", "\n").splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    data[k.strip()] = v.strip()
        if not data:
            tg.bot.send_message(m.chat.id, "Не понял формат. Пришлите JSON или k=v.")
            return
        for k, v in data.items():
            if k in DEFAULT_SECRETS:
                if k == "user_id":
                    try:
                        v = int(v)
                    except Exception:
                        pass
                R.secrets[k] = v
        _save_secrets()
        tg.bot.send_message(m.chat.id, "✅ Учётка обновлена. Проверяю токен…")
        try:
            R.client.login_token() if R.client else None
            tg.bot.send_message(m.chat.id, "🔓 Авторизация в NS.Gifts успешна.")
        except Exception as e:
            tg.bot.send_message(m.chat.id, f"❌ Ошибка авторизации: {e}")
        return

    if state == "mappings_json":
        raw = text
        if content_type == "document":
            try:
                file_info = tg.bot.get_file(m.document.file_id)
                raw_bytes = tg.bot.download_file(file_info.file_path)
                raw = raw_bytes.decode("utf-8", errors="replace")
            except Exception as e:
                tg.bot.send_message(m.chat.id, f"Не смог скачать файл: {e}")
                return
        try:
            data = json.loads(raw)
            if "lots" not in data:
                # допускаем плоский dict { lot_id: mapping }
                data = {"lots": data}
            R.mappings = data
            _save_mappings()
            tg.bot.send_message(m.chat.id,
                                f"✅ mappings.json обновлён, лотов: {len(data.get('lots', {}))}")
        except Exception as e:
            tg.bot.send_message(m.chat.id, f"Ошибка парсинга JSON: {e}")
        return

    if state.startswith("set_tpl:"):
        key = state[len("set_tpl:"):]
        R.settings[key] = m.text or ""
        _save_settings()
        tg.bot.send_message(m.chat.id, f"✅ Шаблон <code>{key}</code> обновлён.", parse_mode="HTML")
        return

    if state.startswith("set:"):
        key = state[4:]
        if key not in R.settings:
            tg.bot.send_message(m.chat.id, f"Неизвестный ключ {key}.")
            return
        cur = R.settings[key]
        new: Any = text
        try:
            if isinstance(cur, bool):
                new = text.lower() in ("1", "true", "yes", "да", "on")
            elif isinstance(cur, int):
                new = int(float(text))
            elif isinstance(cur, float):
                new = float(text.replace(",", "."))
            elif isinstance(cur, list):
                new = [s.strip() for s in text.split(",") if s.strip()]
        except Exception as e:
            tg.bot.send_message(m.chat.id, f"Не смог распарсить значение: {e}")
            return
        R.settings[key] = new
        _save_settings()
        tg.bot.send_message(m.chat.id, f"✅ {key} = {new}")
        return


def _cmd_guide(cardinal: "Cardinal", msg) -> None:
    """Send a formatted guide about the NS.Gifts plugin."""
    tg = _tg(cardinal)
    if tg is None:
        return
    guide_text = (
        "<b>📖 NS.Gifts - гайд по плагину</b>\n\n"
        "<b>Что делает плагин:</b>\n"
        "Автоматическая выдача товаров (коды, Steam-пополнения) через API ns.gifts, "
        "авто-обновление цен лотов FunPay с учётом наценки, чат-команды для покупателей.\n\n"
        "<b>Настройка:</b>\n"
        "1. Получите API-ключи на https://wholesale.ns.gifts\n"
        "2. Откройте панель /ns или /nsgifts\n"
        "3. Нажмите «Настройки» → «Загрузить NS-учётку» и пришлите JSON с credentials\n"
        "4. Создайте привязки лотов (mappings) - свяжите lot_id FunPay с service_id NS\n"
        "5. Включите автовыдачу\n\n"
        "<b>Возможности:</b>\n"
        "• Автовыдача кодов и Steam-пополнений по новым заказам\n"
        "• Авто-обновление цен лотов (наценка + мин. цена)\n"
        "• Подтверждение Steam-логина у покупателя перед пополнением\n"
        "• Защита от убытков (блокировка выдачи при цене ниже себестоимости)\n"
        "• Чат-команды: !баланс, !прайс, !статус\n"
        "• Авто-отключение лотов при отсутствии товара на складе\n"
        "• Рабочие часы (автовыдача только в указанное время)\n"
        "• Статистика заказов и прибыли\n"
        "• Шаблоны сообщений покупателю\n\n"
        "<b>Команды:</b>\n"
        "/ns, /nsgifts - панель управления\n"
        "/nsgifts_guide - этот гайд\n"
        "/nsgifts_test - тест подключения к NS API"
    )
    try:
        tg.bot.send_message(msg.chat.id, guide_text, parse_mode="HTML")
    except Exception:
        logger.debug("NS.Gifts: send guide failed", exc_info=True)


def _cmd_test(cardinal: "Cardinal", msg) -> None:
    """Test NS API connectivity and show balance."""
    tg = _tg(cardinal)
    if tg is None:
        return
    if not R.client or not R.client.configured:
        try:
            tg.bot.send_message(
                msg.chat.id,
                "❌ <b>NS.Gifts не настроен.</b>\n\n"
                "Откройте /ns → Настройки → «Загрузить NS-учётку» "
                "и пришлите JSON с credentials (user_id, login, password, api_secret).",
                parse_mode="HTML",
            )
        except Exception:
            pass
        return
    try:
        tg.bot.send_message(msg.chat.id, "🔄 Проверяю подключение к NS.Gifts API...")
    except Exception:
        pass
    try:
        bal = R.client.check_balance()
        usd = float(bal.get("balance") or 0)
        result = (
            "✅ <b>NS.Gifts API - подключение успешно!</b>\n\n"
            f"💰 Баланс: <b>{usd:.4f} USD</b>\n"
            f"🔑 User ID: <code>{R.client.user_id}</code>\n"
            f"🌐 Base URL: <code>{R.client.base_url}</code>"
        )
    except Exception as e:
        result = (
            "❌ <b>NS.Gifts API - ошибка подключения</b>\n\n"
            f"Ошибка: <code>{_html_escape(str(e)[:300])}</code>"
        )
    try:
        tg.bot.send_message(msg.chat.id, result, parse_mode="HTML")
    except Exception:
        logger.debug("NS.Gifts: send test result failed", exc_info=True)


def _register_tg_handlers(cardinal: "Cardinal") -> None:
    tg = _tg(cardinal)
    if tg is None or R.tg_registered:
        return

    R._tg_handler_fns = []

    # callback с префиксом плагина и из PLUGIN_SETTINGS:<UUID>
    try:
        _cb = lambda c: _handle_callback(cardinal, c)
        R._tg_handler_fns.append(_cb)
        tg.cbq_handler(
            _cb,
            lambda c: (c.data or "").startswith(f"{CBP}:")
            or (c.data or "").startswith(f"{CBT.PLUGIN_SETTINGS}:{UUID}")
            or (c.data or "").startswith(f"47:{UUID}"),
        )
    except Exception as e:
        logger.error(f"NS.Gifts: failed to register cbq_handler: {e}", exc_info=True)

    # /ns и /nsgifts команды
    try:
        _h_ns = lambda mm: _open_panel(cardinal, mm.chat.id) if _is_authorized(cardinal, mm.from_user.id) else None
        R._tg_handler_fns.append(_h_ns)
        tg.msg_handler(
            _h_ns,
            commands=["ns", "nsgifts"],
        )
    except Exception as e:
        logger.error(f"NS.Gifts: failed to register msg_handler for /ns: {e}", exc_info=True)

    # /nsgifts_guide command
    try:
        _h_guide = lambda mm: _cmd_guide(cardinal, mm) if _is_authorized(cardinal, mm.from_user.id) else None
        R._tg_handler_fns.append(_h_guide)
        tg.msg_handler(
            _h_guide,
            commands=["nsgifts_guide"],
        )
    except Exception as e:
        logger.error(f"NS.Gifts: failed to register msg_handler for /nsgifts_guide: {e}", exc_info=True)

    # /nsgifts_test command
    try:
        _h_test = lambda mm: _cmd_test(cardinal, mm) if _is_authorized(cardinal, mm.from_user.id) else None
        R._tg_handler_fns.append(_h_test)
        tg.msg_handler(
            _h_test,
            commands=["nsgifts_test"],
        )
    except Exception as e:
        logger.error(f"NS.Gifts: failed to register msg_handler for /nsgifts_test: {e}", exc_info=True)

    # ответы на наши запросы значений
    try:
        _h_pending = lambda mm: _handle_message(cardinal, mm)
        R._tg_handler_fns.append(_h_pending)
        tg.msg_handler(
            _h_pending,
            func=lambda mm: (mm.chat.id, mm.from_user.id) in R.pending_tg_states,
            content_types=["text", "document"],
        )
    except Exception as e:
        logger.error(f"NS.Gifts: failed to register msg_handler for pending states: {e}", exc_info=True)

    try:
        cardinal.add_telegram_commands(UUID, [
            ("ns", "Управление NS.Gifts", True),
            ("nsgifts", "Управление NS.Gifts", True),
            ("nsgifts_guide", "NS.Gifts: гайд", True),
            ("nsgifts_test", "NS.Gifts: тест", True),
        ])
    except Exception:
        logger.warning("NS.Gifts: add_telegram_commands failed", exc_info=True)

    R.tg_registered = True


# =========================================================================
# Точки входа FPC
# =========================================================================

def init_plugin(cardinal: "Cardinal", *_a, **_kw) -> None:
    global _CARDINAL_REF_NS
    _CARDINAL_REF_NS = cardinal
    _ensure_storage()
    _load_all()
    try:
        _register_tg_handlers(cardinal)
    except Exception as e:
        logger.warning(f"NS.Gifts: early TG handler registration failed (will retry in post_init): {e}")

    # 💛 Донат-баннер (защита реквизитов автора)
    global _donation_cardinal
    _donation_cardinal = cardinal
    try:
        tg = getattr(cardinal, "telegram", None)
        if tg:
            tg.cbq_handler(
                _donation_on_cb,
                lambda c: (c.data or "").startswith("nsg_dn:"))
            _start_donation_reminder(cardinal)
    except Exception:
        pass

    logger.info(
        f"NS.Gifts plugin v{VERSION} loaded. configured={R.client.configured if R.client else False}, "
        f"mappings={len(R.mappings.get('lots') or {})}"
    )


def post_init(cardinal: "Cardinal", *_a, **_kw) -> None:
    _ensure_storage()
    _load_all()
    try:
        _register_tg_handlers(cardinal)
    except Exception as e:
        logger.error(f"NS.Gifts: post_init failed to register TG handlers: {e}", exc_info=True)
    _ensure_price_loop(cardinal)
    _ensure_confirm_loop(cardinal)
    # Общий патч raise_lots + первичная регистрация наших category_id.
    # Делаем в отдельном потоке: detect_category_id — это HTTP-запрос.
    lib = _common_lib_ns()
    if lib is not None:
        try:
            lib.install_raise_skip_patch(cardinal)
            import threading as _th
            _th.Thread(
                target=lambda: _refresh_raise_skip_ns(cardinal),
                daemon=True, name="ns_gifts-raise-skip").start()
        except Exception:
            logger.debug("NS.Gifts: raise-skip setup failed", exc_info=True)


def _unregister_tg_handlers(cardinal: "Cardinal") -> None:
    """Снимает ранее зарегистрированные telebot-хендлеры, чтобы при
    перезагрузке плагина не плодились дубликаты обработчиков."""
    fns = list(getattr(R, "_tg_handler_fns", None) or [])
    if not fns:
        R._tg_handler_fns = []
        return
    try:
        tg = _tg(cardinal)
        bot = getattr(tg, "bot", None) if tg is not None else None
        if bot is not None:
            for attr in ("message_handlers", "callback_query_handlers",
                         "edited_message_handlers"):
                handlers = getattr(bot, attr, None)
                if isinstance(handlers, list):
                    handlers[:] = [
                        h for h in handlers
                        if not (isinstance(h, dict) and h.get("function") in fns)
                    ]
    except Exception:
        logger.debug("NS.Gifts: не удалось снять telebot-хендлеры", exc_info=True)
    finally:
        R._tg_handler_fns = []


def _on_delete(cardinal: "Cardinal", uuid: str) -> None:  # noqa: ARG001
    _unregister_tg_handlers(cardinal)
    R.tg_registered = False


BIND_TO_DELETE = _on_delete

BIND_TO_PRE_INIT = [init_plugin]
BIND_TO_POST_INIT = [post_init]
BIND_TO_NEW_ORDER = [deliver_order_handler]
BIND_TO_NEW_MESSAGE = [chat_command_handler]


def _bind_settings_page(cardinal: "Cardinal", msg) -> None:
    """Called by FPC when user clicks plugin settings button in the admin panel."""
    _open_panel(cardinal, msg.chat.id)


BIND_TO_SETTINGS_PAGE = _bind_settings_page



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
