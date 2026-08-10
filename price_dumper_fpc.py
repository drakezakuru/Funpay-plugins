"""
PriceDumper plugin for FunPayCardinal
=====================================

Чистая реализация авто-демпинга цен. Плагин периодически сканирует заданные
подкатегории FunPay и опускает цену своих лотов чуть ниже самого дешёвого
конкурента (с учётом whitelist), но не ниже минимальной цены. Цену он никогда
не повышает. Управление — через русскоязычное меню FunPay Cardinal в Telegram.

Это переписанная с нуля версия незавершённого `AutoDump/auto_dumper.py`, который
использовал несуществующий пакет `funpayapi`. Здесь — только bundled FunPayAPI
Cardinal: чтение публичных лотов подкатегории и правка цены через поля лота.

Чисто, без бэкдоров: НЕТ подключения к БД, НЕТ удалённой «активации»,
kill-switch и лицензий. Outbound только к funpay.com и Telegram Bot API.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
DONATION_CALLBACK_PREFIX = "pd_dn"     # префикс колбэков кнопок баннера
DONATION_PLUGIN_NAME = "PriceDumper"   # имя плагина в шапке баннера

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

NAME = "PriceDumper"
VERSION = "1.0.1"
DESCRIPTION = (
    "Авто-демпинг цен: периодически сканирует подкатегории FunPay и опускает "
    "цену своих лотов чуть ниже самого дешёвого конкурента (с учётом whitelist), "
    "не ниже минимума. Цена никогда не повышается. Без бэкдоров и удалённой "
    "активации. Управление через Telegram-меню."
)
CREDITS = "@drakelovc"
UUID = "45657c26-fd8d-4cdb-9e9b-6ee8b427af87"
SETTINGS_PAGE = True

logger = logging.getLogger(f"FPC.{__name__}")
LOGGER_PREFIX = "[PRICEDUMPER]"


# =========================================================================
# Хранилище: пути и дефолты
# =========================================================================

PLUGIN_DIR = Path("storage/plugins/price_dumper")
SETTINGS_PATH = PLUGIN_DIR / "settings.json"
CHANGES_LOG_PATH = PLUGIN_DIR / "changes_log.json"

RANGE_CHECK_INTERVAL = (60, 86400)        # сек
CHANGES_LOG_MAX = 500                     # кольцевой буфер истории изменений
RETRY_ATTEMPTS = 3                        # попыток save_lot за цикл
RETRY_BASE_DELAY = 1.0                    # базовая задержка backoff (сек)

DEFAULT_SETTINGS: dict[str, Any] = {
    "rules": [],
    "global_whitelist": [],
    "check_interval_sec": 600,
    "operator_chat_id": None,
}

_io_lock = threading.RLock()


# =========================================================================
# Чистое ядро (pure core)
# =========================================================================

def _filter_eligible(competitors: list[dict], whitelist: set[str]) -> list[dict]:
    """Возвращает только конкурентов, чей `seller_username` И `str(seller_id)`
    отсутствуют в whitelist (Property 1, Req 2.1)."""
    wl = whitelist or set()
    return [c for c in competitors
            if c.get("seller_username") not in wl
            and str(c.get("seller_id", "")) not in wl]


def _pick_cheapest(eligible: list[dict]) -> dict | None:
    """Возвращает конкурента с минимальной `price`; для пустого списка — None
    (Property 2, Req 2.3)."""
    if not eligible:
        return None
    return min(eligible, key=lambda c: float(c.get("price", float("inf"))))


def _compute_new_price(my_price: float, competitor_price: float,
                       step: float, floor: float) -> float | None:
    """Новая цена лота, либо None, если менять не нужно (Properties 3–4,
    Req 2.4/2.5/3.2):
      • если конкурент дороже — None (цену не поднимаем);
      • иначе target = max(competitor_price - step, floor);
      • если round(target, 2) == round(my_price, 2) — None (нет эффекта)."""
    if competitor_price > my_price:
        return None
    target = max(competitor_price - step, floor)
    if round(target, 2) == round(my_price, 2):
        return None
    return round(target, 2)


# =========================================================================
# Вспомогательные функции
# =========================================================================

def _mask_secret(s: str | None, head: int = 4, tail: int = 2) -> str:
    if not s:
        return ""
    s = str(s)
    if len(s) <= head + tail:
        return "***"
    return s[:head] + "…" + s[-tail:]


def _exp_backoff(attempt: int, base: float = RETRY_BASE_DELAY, cap: float = 3600.0) -> float:
    try:
        return min(cap, base * (2 ** max(0, int(attempt))))
    except Exception:
        return cap


def _clamp(v: Any, lo: int, hi: int) -> int:
    try:
        x = int(v)
    except Exception:
        return lo
    return max(lo, min(hi, x))


def _coerce_pos_number(value: Any) -> float | None:
    """Положительное число из int/float/строки; иначе None."""
    try:
        x = float(str(value).strip())
    except Exception:
        return None
    if x <= 0:
        return None
    return x


def _coerce_pos_int(value: Any) -> int | None:
    """Положительное целое из int/строки цифр; иначе None."""
    try:
        x = int(str(value).strip())
    except Exception:
        return None
    if x <= 0:
        return None
    return x


def _html_escape(s: Any) -> str:
    return (str(s if s is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _new_rule_id(rules: list[dict]) -> str:
    nums = []
    for r in rules:
        rid = str(r.get("id", ""))
        if rid.startswith("r"):
            try:
                nums.append(int(rid[1:]))
            except Exception:
                pass
    return f"r{(max(nums) + 1) if nums else 1}"


# =========================================================================
# Хранилище: load/save (атомарно tmp + os.replace)
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


def _normalize_rule(r: dict) -> dict:
    """Приводит правило к каноничной форме с дефолтами (setdefault-миграция)."""
    if not isinstance(r, dict):
        r = {}
    r.setdefault("id", "r1")
    r.setdefault("subcategory_id", 0)
    r.setdefault("my_lot_id", 0)
    r.setdefault("min_price", 0.0)
    r.setdefault("price_step", 1.0)
    r.setdefault("enabled", True)
    if not isinstance(r.get("whitelist"), list):
        r["whitelist"] = []
    return r


def _load_settings() -> dict[str, Any]:
    merged = json.loads(json.dumps(DEFAULT_SETTINGS))
    data = _load_json(SETTINGS_PATH, {})
    if isinstance(data, dict):
        for k, v in data.items():
            merged[k] = v
    # setdefault-миграция ключей для старых конфигов
    merged.setdefault("rules", [])
    merged.setdefault("global_whitelist", [])
    merged.setdefault("check_interval_sec", DEFAULT_SETTINGS["check_interval_sec"])
    merged.setdefault("operator_chat_id", None)
    if not isinstance(merged.get("rules"), list):
        merged["rules"] = []
    if not isinstance(merged.get("global_whitelist"), list):
        merged["global_whitelist"] = []
    merged["rules"] = [_normalize_rule(r) for r in merged["rules"]]
    merged["check_interval_sec"] = _clamp(merged.get("check_interval_sec"), *RANGE_CHECK_INTERVAL)
    return merged


def _save_settings(s: dict[str, Any]) -> None:
    _save_json(SETTINGS_PATH, s)


def _load_changes() -> list[dict]:
    data = _load_json(CHANGES_LOG_PATH, [])
    return data if isinstance(data, list) else []


def _save_changes(data: list[dict]) -> None:
    _save_json(CHANGES_LOG_PATH, data)


def _append_change(entry: dict) -> None:
    """Добавляет запись в кольцевой буфер истории (последние CHANGES_LOG_MAX)."""
    with _io_lock:
        log = _load_changes()
        log.append(entry)
        if len(log) > CHANGES_LOG_MAX:
            log = log[-CHANGES_LOG_MAX:]
        _save_changes(log)


# =========================================================================
# Cardinal-хелперы: чтение публичных лотов, правка цены
# =========================================================================

def _get_funpay_account(cardinal: "Cardinal"):
    """Достаёт объект FunPay-аккаунта из Cardinal."""
    if cardinal is None:
        return None
    acc = getattr(cardinal, "account", None)
    if acc is not None:
        return acc
    return cardinal


def _common_subcategory_type():
    """Тип подкатегории COMMON для `get_subcategory_public_lots`.

    Изолировано здесь, потому что точное расположение enum в bundled FunPayAPI
    может отличаться между версиями Cardinal. Если импорт не удался —
    возвращаем 0 (числовое значение SubCategoryTypes.COMMON)."""
    try:
        from FunPayAPI.common.enums import SubCategoryTypes
        return SubCategoryTypes.COMMON
    except Exception:
        pass
    try:
        from FunPayAPI.types import SubCategoryTypes
        return SubCategoryTypes.COMMON
    except Exception:
        pass
    return 0


def _call_get_public_lots(account, subcategory_id: int):
    """Единственная точка вызова bundled FunPayAPI для чтения публичных лотов
    подкатегории. Изолирована, чтобы её было легко заменить, если сигнатура в
    конкретной сборке Cardinal отличается.

    Предположение: используется `account.get_subcategory_public_lots(
    subcategory_type, subcategory_id)` (как в bundled FunPayAPI), где
    subcategory_type = SubCategoryTypes.COMMON. Возвращает список объектов
    LotShortcut с атрибутами `.id`, `.price`, `.seller` (`.id`, `.username`)."""
    return account.get_subcategory_public_lots(_common_subcategory_type(), int(subcategory_id))


def _fetch_competitors(account, subcategory_id: int) -> list[dict]:
    """Возвращает список конкурентов подкатегории в виде словарей
    {price, seller_username, seller_id, lot_id} (Req 2.1)."""
    lots = _call_get_public_lots(account, subcategory_id)
    result: list[dict] = []
    for lot in lots or []:
        seller = getattr(lot, "seller", None)
        seller_username = getattr(seller, "username", None) if seller is not None else None
        seller_id = getattr(seller, "id", None) if seller is not None else None
        try:
            price = float(getattr(lot, "price", 0) or 0)
        except Exception:
            continue
        result.append({
            "price": price,
            "seller_username": seller_username,
            "seller_id": seller_id,
            "lot_id": getattr(lot, "id", None),
        })
    return result


def _get_my_price(account, my_lot_id: int) -> float | None:
    """Текущая цена своего лота через `account.get_lot_fields(lot_id).price`."""
    try:
        fields = account.get_lot_fields(int(my_lot_id))
    except Exception:
        logger.warning(f"{LOGGER_PREFIX} не удалось получить поля лота {my_lot_id}", exc_info=True)
        return None
    try:
        price = getattr(fields, "price", None)
        return float(price) if price is not None else None
    except Exception:
        return None


def _update_lot_price(account, my_lot_id: int, new_price: float,
                      attempts: int = RETRY_ATTEMPTS) -> bool:
    """Обновляет цену лота: get_lot_fields → fields.price → save_lot.
    До `attempts` попыток с экспоненциальным backoff (Req 3.1/3.3)."""
    last_exc: Exception | None = None
    for attempt in range(max(1, attempts)):
        try:
            fields = account.get_lot_fields(int(my_lot_id))
            fields.price = new_price
            account.save_lot(fields)
            return True
        except Exception as e:
            last_exc = e
            if attempt < attempts - 1:
                time.sleep(_exp_backoff(attempt))
                continue
    logger.warning(f"{LOGGER_PREFIX} save_lot для лота {my_lot_id} не удался: {last_exc}")
    return False


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


def _notify_change(cardinal: "Cardinal", entry: dict) -> None:
    """Русское уведомление об эффективном изменении цены (Req 3.4)."""
    text = (
        "📉 <b>Цена понижена</b>\n"
        f"🧾 Лот: <code>{_html_escape(entry.get('my_lot_id'))}</code>\n"
        f"💰 Было: <code>{_html_escape(entry.get('old_price'))}</code> → "
        f"стало: <code>{_html_escape(entry.get('new_price'))}</code>\n"
        f"🥇 Демпим: <b>{_html_escape(entry.get('competitor_seller'))}</b> "
        f"(<code>{_html_escape(entry.get('competitor_price'))}</code>)"
    )
    _notify_operator(cardinal, text)


# =========================================================================
# Обработка правил и цикл
# =========================================================================

def _process_rule(cardinal: "Cardinal", account, rule: dict,
                  global_whitelist: set[str]) -> dict | None:
    """Обрабатывает одно правило демпинга. Возвращает запись об изменении
    цены, либо None, если цена осталась прежней (Req 2.2–2.6, 3.1–3.4)."""
    subcategory_id = rule.get("subcategory_id")
    my_lot_id = rule.get("my_lot_id")

    competitors = _fetch_competitors(account, subcategory_id)
    whitelist = {str(x) for x in rule.get("whitelist", [])} | global_whitelist
    eligible = _filter_eligible(competitors, whitelist)
    cheapest = _pick_cheapest(eligible)
    if cheapest is None:
        # нет подходящих конкурентов — цену не трогаем (Req 2.2/2.6)
        return None

    my_price = _get_my_price(account, my_lot_id)
    if my_price is None:
        logger.warning(f"{LOGGER_PREFIX} правило {rule.get('id')}: неизвестна текущая цена лота {my_lot_id}")
        return None

    competitor_price = float(cheapest.get("price"))
    step = float(rule.get("price_step", 1.0) or 0.0)
    floor = float(rule.get("min_price", 0.0) or 0.0)

    new_price = _compute_new_price(my_price, competitor_price, step, floor)
    if new_price is None:
        return None

    if not _update_lot_price(account, my_lot_id, new_price):
        return None

    entry = {
        "ts": time.time(),
        "rule_id": rule.get("id"),
        "my_lot_id": my_lot_id,
        "old_price": round(my_price, 2),
        "new_price": new_price,
        "competitor_seller": cheapest.get("seller_username"),
        "competitor_price": competitor_price,
    }
    _append_change(entry)
    _notify_change(cardinal, entry)
    return entry


def _run_cycle(cardinal: "Cardinal") -> list[dict]:
    """Один проход по всем включённым правилам. Сбой одного правила не
    прерывает обработку остальных (Req 4.1, error handling)."""
    settings = _load_settings()
    account = _get_funpay_account(cardinal)
    if account is None:
        logger.warning(f"{LOGGER_PREFIX} FunPay-аккаунт недоступен, цикл пропущен")
        return []
    global_whitelist = {str(x) for x in settings.get("global_whitelist", [])}
    changes: list[dict] = []
    for rule in settings.get("rules", []):
        if not rule.get("enabled", True):
            continue
        try:
            change = _process_rule(cardinal, account, rule, global_whitelist)
            if change:
                changes.append(change)
        except Exception:
            logger.warning(
                f"{LOGGER_PREFIX} правило {rule.get('id')} упало в этом цикле",
                exc_info=True)
    return changes


# =========================================================================
# Планировщик (фоновый поток)
# =========================================================================

_dump_thread: threading.Thread | None = None
_dump_stop = threading.Event()


def _dump_loop(cardinal: "Cardinal", stop_event: threading.Event) -> None:
    """Демон-поток: будится каждые check_interval_sec и обрабатывает все
    включённые правила. Интервал перечитывается каждый цикл, чтобы правки из
    меню применялись без рестарта. Останов — через threading.Event (Req 4.1/4.3)."""
    while not stop_event.is_set():
        try:
            _run_cycle(cardinal)
        except Exception:
            logger.warning(f"{LOGGER_PREFIX} ошибка в цикле демпинга", exc_info=True)
        interval = _clamp(_load_settings().get("check_interval_sec", 600), *RANGE_CHECK_INTERVAL)
        stop_event.wait(interval)


def _ensure_dump_thread(cardinal: "Cardinal") -> None:
    global _dump_thread
    if _dump_thread and _dump_thread.is_alive():
        return
    _dump_stop.clear()
    _dump_thread = threading.Thread(
        target=_dump_loop, args=(cardinal, _dump_stop),
        name="price-dumper", daemon=True)
    _dump_thread.start()


# =========================================================================
# Telegram-меню оператора (RU)
# =========================================================================

CBP = "pricedumper"
CBT_HOME = f"{CBP}:home"
CBT_RULES = f"{CBP}:rules"
CBT_RULE_ADD = f"{CBP}:radd"
CBT_RULE_VIEW = f"{CBP}:rview"          # + :rid
CBT_RULE_TOGGLE = f"{CBP}:rtgl"         # + :rid
CBT_RULE_DEL = f"{CBP}:rdel"            # + :rid
CBT_RULE_EDIT_SUB = f"{CBP}:resub"      # + :rid
CBT_RULE_EDIT_LOT = f"{CBP}:relot"      # + :rid
CBT_RULE_EDIT_MIN = f"{CBP}:remin"      # + :rid
CBT_RULE_EDIT_STEP = f"{CBP}:restep"    # + :rid
CBT_RULE_WL = f"{CBP}:rwl"              # + :rid
CBT_GLOBAL_WL = f"{CBP}:gwl"
CBT_EDIT_INTERVAL = f"{CBP}:interval"
CBT_RUN_NOW = f"{CBP}:runnow"
CBT_HISTORY = f"{CBP}:history"


def _find_rule(settings: dict, rid: str) -> dict | None:
    for r in settings.get("rules", []):
        if str(r.get("id")) == str(rid):
            return r
    return None


def _home_text() -> str:
    s = _load_settings()
    rules = s.get("rules", [])
    enabled = sum(1 for r in rules if r.get("enabled", True))
    return (
        f"<b>📉 PriceDumper v{VERSION}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📋 Правил: <code>{len(rules)}</code> (включено: <code>{enabled}</code>)\n"
        f"⛔ Глобальный whitelist: <code>{len(s.get('global_whitelist', []))}</code>\n"
        f"⏱ Интервал опроса: <code>{int(s.get('check_interval_sec', 600))}</code> сек"
    )


def _home_kb() -> "K":
    kb = K(row_width=1)
    kb.add(B("📋 Правила демпинга", callback_data=CBT_RULES))
    kb.add(B("⛔ Глобальный whitelist", callback_data=CBT_GLOBAL_WL))
    kb.add(B("⏱ Интервал опроса", callback_data=CBT_EDIT_INTERVAL))
    kb.add(B("▶️ Запустить цикл сейчас", callback_data=CBT_RUN_NOW))
    kb.add(B("📜 История изменений цен", callback_data=CBT_HISTORY))
    kb.add(B("💛 Донат", callback_data=f"{DONATION_CALLBACK_PREFIX}:donate"))
    return kb


def _rule_label(r: dict) -> str:
    flag = "🟢" if r.get("enabled", True) else "🔴"
    return (f"{flag} #{r.get('id')} · подкат {r.get('subcategory_id')} · "
            f"лот {r.get('my_lot_id')}")


def _rule_view_text(r: dict) -> str:
    return (
        f"<b>⚙️ Правило #{_html_escape(r.get('id'))}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📂 Подкатегория: <code>{_html_escape(r.get('subcategory_id'))}</code>\n"
        f"🧾 Мой лот: <code>{_html_escape(r.get('my_lot_id'))}</code>\n"
        f"⬇️ Минимальная цена: <code>{_html_escape(r.get('min_price'))}</code>\n"
        f"➖ Шаг демпинга: <code>{_html_escape(r.get('price_step'))}</code>\n"
        f"⛔ Whitelist правила: <code>{len(r.get('whitelist', []))}</code>\n"
        f"⚡ Статус: {'🟢 включено' if r.get('enabled', True) else '🔴 выключено'}"
    )


def init(cardinal: "Cardinal", *args) -> None:
    if not getattr(cardinal, "telegram", None):
        # без Telegram всё равно поднимаем фоновый цикл
        _ensure_dump_thread(cardinal)
        logger.info(f"{LOGGER_PREFIX} v{VERSION} запущен (без Telegram)")
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

    # ---------- корень ----------
    def open_settings_cb(call) -> None:
        _persist_op(call.message.chat.id)
        _edit_home(call)
        _answer(call)

    def home_cb(call) -> None:
        _edit_home(call)
        _answer(call)

    # ---------- список правил ----------
    def rules_cb(call) -> None:
        s = _load_settings()
        kb = K(row_width=1)
        for r in s.get("rules", []):
            kb.add(B(_rule_label(r), callback_data=f"{CBT_RULE_VIEW}:{r.get('id')}"))
        kb.add(B("➕ Добавить правило", callback_data=CBT_RULE_ADD))
        kb.add(B("◀️ Назад", callback_data=CBT_HOME))
        text = "<b>📋 Правила демпинга</b>\nВыберите правило или добавьте новое:"
        if not s.get("rules"):
            text += "\n\n(список пуст)"
        _edit_or_send(call, text, kb)
        _answer(call)

    def rule_view_cb(call) -> None:
        rid = call.data.split(":", 2)[-1]
        s = _load_settings()
        r = _find_rule(s, rid)
        if not r:
            _answer(call, "не найдено")
            return rules_cb(call)
        kb = K(row_width=2)
        kb.row(B("📂 Подкатегория", callback_data=f"{CBT_RULE_EDIT_SUB}:{rid}"),
               B("🧾 Мой лот", callback_data=f"{CBT_RULE_EDIT_LOT}:{rid}"))
        kb.row(B("⬇️ Мин. цена", callback_data=f"{CBT_RULE_EDIT_MIN}:{rid}"),
               B("➖ Шаг", callback_data=f"{CBT_RULE_EDIT_STEP}:{rid}"))
        kb.add(B("⛔ Whitelist правила", callback_data=f"{CBT_RULE_WL}:{rid}"))
        kb.add(B(("🔴 Выключить" if r.get("enabled", True) else "🟢 Включить"),
                 callback_data=f"{CBT_RULE_TOGGLE}:{rid}"))
        kb.add(B("🗑 Удалить правило", callback_data=f"{CBT_RULE_DEL}:{rid}"))
        kb.add(B("◀️ Назад", callback_data=CBT_RULES))
        _edit_or_send(call, _rule_view_text(r), kb)
        _answer(call)

    def rule_add_cb(call) -> None:
        s = _load_settings()
        rid = _new_rule_id(s.get("rules", []))
        s.setdefault("rules", []).append({
            "id": rid, "subcategory_id": 0, "my_lot_id": 0,
            "min_price": 0.0, "price_step": 1.0, "enabled": False, "whitelist": [],
        })
        _save_settings(s)
        _answer(call, f"➕ Правило {rid} создано")
        call.data = f"{CBT_RULE_VIEW}:{rid}"
        rule_view_cb(call)

    def rule_toggle_cb(call) -> None:
        rid = call.data.split(":", 2)[-1]
        s = _load_settings()
        r = _find_rule(s, rid)
        if r:
            r["enabled"] = not r.get("enabled", True)
            _save_settings(s)
            _answer(call, "🟢 включено" if r["enabled"] else "🔴 выключено")
        call.data = f"{CBT_RULE_VIEW}:{rid}"
        rule_view_cb(call)

    def rule_del_cb(call) -> None:
        rid = call.data.split(":", 2)[-1]
        s = _load_settings()
        s["rules"] = [r for r in s.get("rules", []) if str(r.get("id")) != str(rid)]
        _save_settings(s)
        _answer(call, "🗑 Удалено")
        rules_cb(call)

    # --- редакторы полей правила ---
    def _make_rule_int_editor(field: str, label: str):
        def cb(call) -> None:
            rid = call.data.split(":", 2)[-1]
            msg = bot.send_message(call.message.chat.id, f"{label}\nВведите целое число > 0:")
            _answer(call)

            def handle(m) -> None:
                val = _coerce_pos_int((m.text or "").strip())
                if val is None:
                    bot.reply_to(m, f"❌ «{label}»: нужно целое число > 0. Прежнее значение сохранено.")
                    return
                s = _load_settings()
                r = _find_rule(s, rid)
                if not r:
                    return bot.reply_to(m, "❌ Правило не найдено.")
                r[field] = val
                _save_settings(s)
                bot.reply_to(m, f"✅ Обновлено: <code>{val}</code>", parse_mode="HTML")
            bot.register_next_step_handler(msg, handle)
        return cb

    def _make_rule_num_editor(field: str, label: str):
        def cb(call) -> None:
            rid = call.data.split(":", 2)[-1]
            msg = bot.send_message(call.message.chat.id, f"{label}\nВведите число > 0:")
            _answer(call)

            def handle(m) -> None:
                val = _coerce_pos_number((m.text or "").strip())
                if val is None:
                    bot.reply_to(m, f"❌ «{label}»: нужно число > 0. Прежнее значение сохранено.")
                    return
                s = _load_settings()
                r = _find_rule(s, rid)
                if not r:
                    return bot.reply_to(m, "❌ Правило не найдено.")
                r[field] = val
                _save_settings(s)
                bot.reply_to(m, f"✅ Обновлено: <code>{val}</code>", parse_mode="HTML")
            bot.register_next_step_handler(msg, handle)
        return cb

    # --- whitelist правила ---
    def rule_wl_cb(call) -> None:
        rid = call.data.split(":", 2)[-1]
        s = _load_settings()
        r = _find_rule(s, rid)
        if not r:
            _answer(call, "не найдено")
            return rules_cb(call)
        wl = r.get("whitelist", [])
        text = f"<b>⛔ Whitelist правила #{_html_escape(rid)}</b>\n"
        text += ("\n".join(f"• <code>{_html_escape(x)}</code>" for x in wl) if wl else "(пусто)")
        text += "\n\nОтправьте «+ник» / «+id» чтобы добавить или «-ник» / «-id» чтобы удалить."
        kb = K().add(B("◀️ Назад", callback_data=f"{CBT_RULE_VIEW}:{rid}"))
        _edit_or_send(call, text, kb)
        _answer(call)
        msg = bot.send_message(call.message.chat.id, "Введите «+значение» / «-значение» (или /cancel):")

        def handle(m) -> None:
            t = (m.text or "").strip()
            if t.startswith("/") or not (t.startswith("+") or t.startswith("-")):
                bot.reply_to(m, "❌ Нужен формат «+значение» или «-значение». Прежний список сохранён.")
                return
            val = t[1:].strip()
            if not val:
                bot.reply_to(m, "❌ Пустое значение. Прежний список сохранён.")
                return
            s2 = _load_settings()
            r2 = _find_rule(s2, rid)
            if not r2:
                return bot.reply_to(m, "❌ Правило не найдено.")
            wl2 = list(r2.get("whitelist", []))
            if t.startswith("+"):
                if val in wl2:
                    return bot.reply_to(m, "ℹ️ Уже в списке.")
                wl2.append(val)
                r2["whitelist"] = wl2
                _save_settings(s2)
                bot.reply_to(m, f"✅ Добавлено: {val}")
            else:
                if val not in wl2:
                    return bot.reply_to(m, "ℹ️ Не найдено в списке.")
                wl2 = [x for x in wl2 if x != val]
                r2["whitelist"] = wl2
                _save_settings(s2)
                bot.reply_to(m, f"✅ Удалено: {val}")
        bot.register_next_step_handler(msg, handle)

    # ---------- глобальный whitelist ----------
    def global_wl_cb(call) -> None:
        s = _load_settings()
        wl = s.get("global_whitelist", [])
        text = "<b>⛔ Глобальный whitelist</b>\n"
        text += ("\n".join(f"• <code>{_html_escape(x)}</code>" for x in wl) if wl else "(пусто)")
        text += "\n\nОтправьте «+ник» / «+id» чтобы добавить или «-ник» / «-id» чтобы удалить."
        kb = K().add(B("◀️ Назад", callback_data=CBT_HOME))
        _edit_or_send(call, text, kb)
        _answer(call)
        msg = bot.send_message(call.message.chat.id, "Введите «+значение» / «-значение» (или /cancel):")

        def handle(m) -> None:
            t = (m.text or "").strip()
            if t.startswith("/") or not (t.startswith("+") or t.startswith("-")):
                bot.reply_to(m, "❌ Нужен формат «+значение» или «-значение». Прежний список сохранён.")
                return
            val = t[1:].strip()
            if not val:
                bot.reply_to(m, "❌ Пустое значение. Прежний список сохранён.")
                return
            s2 = _load_settings()
            wl2 = list(s2.get("global_whitelist", []))
            if t.startswith("+"):
                if val in wl2:
                    return bot.reply_to(m, "ℹ️ Уже в списке.")
                wl2.append(val)
                s2["global_whitelist"] = wl2
                _save_settings(s2)
                bot.reply_to(m, f"✅ Добавлено: {val}")
            else:
                if val not in wl2:
                    return bot.reply_to(m, "ℹ️ Не найдено в списке.")
                wl2 = [x for x in wl2 if x != val]
                s2["global_whitelist"] = wl2
                _save_settings(s2)
                bot.reply_to(m, f"✅ Удалено: {val}")
        bot.register_next_step_handler(msg, handle)

    # ---------- интервал опроса ----------
    def interval_cb(call) -> None:
        lo, hi = RANGE_CHECK_INTERVAL
        msg = bot.send_message(call.message.chat.id,
                               f"⏱ Интервал опроса (сек).\nВведите число от {lo} до {hi}:")
        _answer(call)

        def handle(m) -> None:
            try:
                v = int((m.text or "").strip())
                if not lo <= v <= hi:
                    raise ValueError
            except Exception:
                bot.reply_to(m, f"❌ «Интервал опроса»: допустимо {lo}–{hi} сек. "
                                f"Прежнее значение сохранено.")
                return
            s = _load_settings()
            s["check_interval_sec"] = v
            _save_settings(s)
            bot.reply_to(m, f"✅ Обновлено: <code>{v}</code> сек", parse_mode="HTML")
        bot.register_next_step_handler(msg, handle)

    # ---------- ручной цикл ----------
    def run_now_cb(call) -> None:
        _answer(call, "▶️ Запускаю цикл…")

        def worker():
            try:
                changes = _run_cycle(cardinal)
                if changes:
                    bot.send_message(call.message.chat.id,
                                     f"✅ Цикл завершён. Изменено цен: {len(changes)}.")
                else:
                    bot.send_message(call.message.chat.id,
                                     "✅ Цикл завершён. Изменений цен нет.")
            except Exception as e:
                bot.send_message(call.message.chat.id, f"❌ Ошибка цикла: {e}")
        threading.Thread(target=worker, name="price-dumper-manual", daemon=True).start()

    # ---------- история ----------
    def history_cb(call) -> None:
        log = _load_changes()
        kb = K().add(B("◀️ Назад", callback_data=CBT_HOME))
        if not log:
            _edit_or_send(call, "📜 История изменений цен пуста.", kb)
            _answer(call)
            return
        lines = ["<b>📜 Последние изменения цен</b>", ""]
        for e in log[-15:][::-1]:
            ts = time.strftime("%d.%m %H:%M", time.localtime(e.get("ts", 0)))
            lines.append(
                f"• {ts} · лот <code>{_html_escape(e.get('my_lot_id'))}</code>: "
                f"{_html_escape(e.get('old_price'))} → {_html_escape(e.get('new_price'))} "
                f"(демп {_html_escape(e.get('competitor_seller'))} "
                f"@ {_html_escape(e.get('competitor_price'))})"
            )
        _edit_or_send(call, "\n".join(lines), kb)
        _answer(call)

    # --- регистрация ---
    tg.cbq_handler(open_settings_cb, lambda c: f"{CBT.PLUGIN_SETTINGS}:{UUID}" in (c.data or ""))
    tg.cbq_handler(home_cb, lambda c: c.data == CBT_HOME)
    tg.cbq_handler(rules_cb, lambda c: c.data == CBT_RULES)
    tg.cbq_handler(rule_add_cb, lambda c: c.data == CBT_RULE_ADD)
    tg.cbq_handler(rule_view_cb, lambda c: (c.data or "").startswith(f"{CBT_RULE_VIEW}:"))
    tg.cbq_handler(rule_toggle_cb, lambda c: (c.data or "").startswith(f"{CBT_RULE_TOGGLE}:"))
    tg.cbq_handler(rule_del_cb, lambda c: (c.data or "").startswith(f"{CBT_RULE_DEL}:"))
    tg.cbq_handler(_make_rule_int_editor("subcategory_id", "📂 ID подкатегории"),
                   lambda c: (c.data or "").startswith(f"{CBT_RULE_EDIT_SUB}:"))
    tg.cbq_handler(_make_rule_int_editor("my_lot_id", "🧾 ID моего лота"),
                   lambda c: (c.data or "").startswith(f"{CBT_RULE_EDIT_LOT}:"))
    tg.cbq_handler(_make_rule_num_editor("min_price", "⬇️ Минимальная цена"),
                   lambda c: (c.data or "").startswith(f"{CBT_RULE_EDIT_MIN}:"))
    tg.cbq_handler(_make_rule_num_editor("price_step", "➖ Шаг демпинга"),
                   lambda c: (c.data or "").startswith(f"{CBT_RULE_EDIT_STEP}:"))
    tg.cbq_handler(rule_wl_cb, lambda c: (c.data or "").startswith(f"{CBT_RULE_WL}:"))
    tg.cbq_handler(global_wl_cb, lambda c: c.data == CBT_GLOBAL_WL)
    tg.cbq_handler(interval_cb, lambda c: c.data == CBT_EDIT_INTERVAL)
    tg.cbq_handler(run_now_cb, lambda c: c.data == CBT_RUN_NOW)
    tg.cbq_handler(history_cb, lambda c: c.data == CBT_HISTORY)

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

    # ---------- команда открытия меню ----------
    def cmd_open(m):
        try:
            bot.send_message(m.chat.id, _home_text(), reply_markup=_home_kb(), parse_mode="HTML")
        except Exception:
            logger.exception("cmd_open failed")
    tg.msg_handler(cmd_open, commands=["pricedumper"])
    try:
        cardinal.add_telegram_commands(UUID, [
            ("pricedumper", "Price Dumper: открыть меню", True),
        ])
    except Exception:
        logger.exception("add_telegram_commands failed")

    _ensure_dump_thread(cardinal)
    logger.info(f"{LOGGER_PREFIX} v{VERSION} запущен")


def _on_delete(cardinal: "Cardinal", *args) -> None:
    _dump_stop.set()


BIND_TO_PRE_INIT = [init]
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
