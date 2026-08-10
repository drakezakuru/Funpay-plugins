"""
VipRoblox — плагин для FunPay Cardinal.

Автоматизация аренды приватных VIP-серверов Roblox через FunPay:
учёт Roblox-аккаунтов (.ROBLOSECURITY), пул VIP-серверов на лот,
авто-регенерация share-link при каждом заказе и по окончании аренды
(старая ссылка покупателя становится мёртвой), таймеры аренды,
авто-возврат, Event Mode, бонус за отзыв.

Регенерация работает через `PATCH https://games.roblox.com/v1/vip-servers/{id}`
с кукой админа (newJoinCode=true). Если Roblox изменит схему API,
плагин может перестать выдавать новые ссылки — обновляй endpoint в коде.

Положите файл в папку plugins/ FunPay Cardinal и перезапустите бота.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import TYPE_CHECKING, Any

import requests
from telebot.types import CallbackQuery, InlineKeyboardButton as B, InlineKeyboardMarkup as K, Message

if TYPE_CHECKING:
    from cardinal import Cardinal
    from FunPayAPI.updater.events import NewOrderEvent

# ---------- мета ----------
NAME = "VipRoblox"
VERSION = "1.3.0"
DESCRIPTION = (
    "Автоматизация VIP-серверов Roblox: аренда, FIFO-очередь, уведомления об "
    "истечении, проверка живости, RU/EN локализация, чёрный список, лимиты на "
    "сервер/покупателя, бонус за отзыв и команда !vip."
)
CREDITS = "@drakelovc"
UUID = "03a7544d-85e4-4cc4-94b2-674ddda8d75f"
SETTINGS_PAGE = True

logger = logging.getLogger("FPC.plugin.vip_roblox")

PLUGIN_DIR = os.path.join("storage", "plugins", "vip_roblox")
CONFIG_PATH = os.path.join(PLUGIN_DIR, "config.json")
STATS_PATH = os.path.join(PLUGIN_DIR, "stats.json")
HISTORY_PATH = os.path.join(PLUGIN_DIR, "history.json")
ACTIVE_PATH = os.path.join(PLUGIN_DIR, "active.json")
QUEUE_PATH = os.path.join(PLUGIN_DIR, "queue.json")
LOG_PATH = os.path.join(PLUGIN_DIR, "log.txt")
MAX_LOG_LINES = 300
MAX_HISTORY = 20

# v1.2.0: новые файлы состояния
TEMPLATES_RU_PATH = os.path.join(PLUGIN_DIR, "templates_ru.json")
TEMPLATES_EN_PATH = os.path.join(PLUGIN_DIR, "templates_en.json")
BUYER_LANG_PATH = os.path.join(PLUGIN_DIR, "buyer_lang.json")
BLACKLIST_PATH = os.path.join(PLUGIN_DIR, "blacklist.json")
LIVENESS_PATH = os.path.join(PLUGIN_DIR, "liveness.json")

# ---------- CBT ----------
CBT_PREFIX = "VRX"
CBT_OPEN = f"{CBT_PREFIX}:O"
CBT_TAB_SETTINGS = f"{CBT_PREFIX}:T:S"
CBT_TAB_LOTS = f"{CBT_PREFIX}:T:LO"
CBT_TAB_RENTALS = f"{CBT_PREFIX}:T:R"
CBT_TAB_HISTORY = f"{CBT_PREFIX}:T:H"
CBT_TAB_EXTRA = f"{CBT_PREFIX}:T:E"
CBT_TAB_LOGS = f"{CBT_PREFIX}:T:L"
CBT_TAB_ACCOUNTS = f"{CBT_PREFIX}:T:A"
CBT_TAB_QUEUE = f"{CBT_PREFIX}:T:Q"
CBT_DEL_QUEUE = f"{CBT_PREFIX}:Q:DEL"

CBT_START = f"{CBT_PREFIX}:STR"
CBT_RESTART = f"{CBT_PREFIX}:RST"
CBT_EVENT_MODE = f"{CBT_PREFIX}:EVM"

CBT_ADD_ACCOUNT = f"{CBT_PREFIX}:A:ADD"
CBT_DEL_ACCOUNT = f"{CBT_PREFIX}:A:DEL"
CBT_TEST_ACCOUNT = f"{CBT_PREFIX}:A:TST"

CBT_EDIT = f"{CBT_PREFIX}:S:E"               # +":<key>"

CBT_ADD_LOT = f"{CBT_PREFIX}:LO:ADD"
CBT_DEL_LOT = f"{CBT_PREFIX}:LO:DEL"

CBT_END_RENTAL = f"{CBT_PREFIX}:R:END"

CBT_TOGGLE_REFUND = f"{CBT_PREFIX}:E:RFD"
CBT_TOGGLE_AUTODEACT = f"{CBT_PREFIX}:E:ADL"
CBT_EDIT_INTERVAL = f"{CBT_PREFIX}:E:INT"
CBT_ADD_NOTIF = f"{CBT_PREFIX}:E:NADD"
CBT_DEL_NOTIF = f"{CBT_PREFIX}:E:NDEL"

CBT_CLEAR_LOGS = f"{CBT_PREFIX}:L:CLR"

# v1.2.0: доп. настройки и чёрный список
CBT_TAB_V12 = f"{CBT_PREFIX}:T:V12"
CBT_TAB_BLACKLIST = f"{CBT_PREFIX}:T:BL"
CBT_BL_ADD = f"{CBT_PREFIX}:BL:ADD"
CBT_BL_DEL = f"{CBT_PREFIX}:BL:DEL"     # +":<idx>"
STATE_AWAIT_BL = f"{CBT_PREFIX}:S_BL"

CBT_TAB_GAMES = f"{CBT_PREFIX}:T:G"
CBT_ADD_GAME = f"{CBT_PREFIX}:G:ADD"
CBT_DEL_GAME = f"{CBT_PREFIX}:G:DEL"
CBT_GAME_DETAIL = f"{CBT_PREFIX}:G:DTL"
CBT_ADD_GAME_VIPS = f"{CBT_PREFIX}:G:VADD"
CBT_DEL_GAME_VIP = f"{CBT_PREFIX}:G:VDEL"

STATE_AWAIT_ACCOUNT = f"{CBT_PREFIX}:S_ACC"
STATE_AWAIT_EDIT = f"{CBT_PREFIX}:S_EDT"
STATE_AWAIT_LOT = f"{CBT_PREFIX}:S_LOT"
STATE_AWAIT_INTERVAL = f"{CBT_PREFIX}:S_INT"
STATE_AWAIT_NOTIF = f"{CBT_PREFIX}:S_NTF"
STATE_AWAIT_GAME = f"{CBT_PREFIX}:S_GAM"
STATE_AWAIT_GAME_VIPS = f"{CBT_PREFIX}:S_GVP"

DEFAULT_CONFIG: dict[str, Any] = {
    "running": False,
    "event_mode": False,
    "accounts": [],          # [{cookie, user_id, username, added_at}]
    # lots: [{lot_id, lot_ids, lot_name_match, vip_server_ids: [int], account_idx, hours, price, game_idx}]
    "lots": [],
    # games: [{game_id: str, game_name: str, vip_server_ids: [int]}]
    "games": [],
    "settings": {
        "game_id": "",
        "min_hours": 1,
        "max_hours": 24,
        "payment_msg": (
            "🎮 Спасибо за покупку!\n"
            "✅ Ваша персональная ссылка на сервер:\n"
            "🔗 {link}\n\n"
            "🕒 Время аренды: {hours} час(а/ов)\n"
            "💰 Заказ: #{order_id}\n\n"
            "🎯 Ссылка действует только для тебя. По окончании аренды она будет обновлена.\n"
            "Команды в этом чате:\n"
            "  !time — оставшееся время\n"
            "  !ссылка — показать ссылку ещё раз\n"
            "  !отзыв — продлить аренду за отзыв\n\n"
            "🎉 Приятной игры!"
        ),
        "expiration_msg": (
            "⏰ Время аренды истекло, пожалуйста подтвердите заказ\n"
            "🔗 https://funpay.com/orders/{order_id}/"
        ),
        "review_bonus_hours": 1,
        "event_discount_pct": 0,
        "auto_refund": False,
        "auto_deactivate_lots": True,
        "check_interval_sec": 300,
        "auto_review_check": True,   # авто-сверка отзыва через FunPay API
        "notify_chats": [],   # [tg_chat_id]
        # v1.2.0:
        "default_language": "ru",            # язык по умолчанию для покупателей
        "expiry_warning_offsets_min": [10],  # за сколько минут предупреждать ([] = выкл)
        "operator_expiry_notify": False,     # дублировать предупреждения оператору
        "liveness_enabled": True,            # периодическая проверка живости
        "liveness_interval_sec": 1800,
        "blacklist_enabled": True,
        "auto_blacklist_on_refund": False,
        "per_server_concurrent_limit": 0,    # 0 = без лимита
        "per_server_period_limit": 0,        # 0 = без лимита
        "per_server_period_sec": 86400,
        "per_buyer_concurrent_limit": 0,     # 0 = без лимита
        "review_stars_threshold": 5,
    },
}

DEFAULT_STATS: dict[str, Any] = {
    "total_hours": 0,
    "earnings": 0,
    "orders": 0,
}


# ---------- I/O ----------
def _ensure_dir() -> None:
    os.makedirs(PLUGIN_DIR, exist_ok=True)


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
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _migrate_rental(r: dict[str, Any]) -> dict[str, Any]:
    """Дополняет запись аренды новыми полями v1.2.0 (аддитивно)."""
    r.setdefault("warned_offsets", [])   # минуты-офсеты предупреждений, которые уже сработали
    r.setdefault("review_bonused", False)
    return r


def _parse_lot_ids(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        raw = value
    else:
        raw = re.split(r"[,;\s]+", str(value))
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _lot_ids(lot: dict) -> list[str]:
    return _parse_lot_ids(lot.get("lot_ids") or lot.get("lot_id"))


def _migrate_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Аддитивная идемпотентная миграция конфига к схеме v1.2.0."""
    s = cfg.setdefault("settings", {})
    for k, v in DEFAULT_CONFIG["settings"].items():
        s.setdefault(k, v)
    cfg.setdefault("accounts", [])
    cfg.setdefault("lots", [])
    cfg.setdefault("games", [])
    cfg.setdefault("running", False)
    cfg.setdefault("event_mode", False)
    # миграция со старого формата (server_id/link → vip_server_ids/account_idx)
    for lot in cfg["lots"]:
        if "vip_server_ids" not in lot and "game_idx" not in lot:
            lot["vip_server_ids"] = []
            old_sid = lot.pop("server_id", None)
            if isinstance(old_sid, int) or (isinstance(old_sid, str) and old_sid.isdigit()):
                lot["vip_server_ids"].append(int(old_sid))
            lot.pop("link", None)
            lot.pop("game_id", None)
        lot.setdefault("account_idx", 0)
        ids = _parse_lot_ids(lot.get("lot_ids") or lot.get("lot_id"))
        if ids:
            lot["lot_ids"] = ids
            lot["lot_id"] = ids[0]
    return cfg


def _load_config() -> dict[str, Any]:
    cfg = _load_json(CONFIG_PATH, DEFAULT_CONFIG)
    return _migrate_config(cfg)


def _save_config(cfg: dict[str, Any]) -> None:
    _save_json(CONFIG_PATH, cfg)


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
        return "Логи отсутствуют"
    with open(LOG_PATH, "r", encoding="utf-8") as f:
        return f.read().strip() or "Логи отсутствуют"


# ---------- Roblox API helpers ----------
ROBLOX_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " \
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"


def roblox_validate(cookie: str) -> dict[str, Any] | None:
    """Проверяет .ROBLOSECURITY cookie. Возвращает {id, name} или None."""
    try:
        r = requests.get(
            "https://users.roblox.com/v1/users/authenticated",
            cookies={".ROBLOSECURITY": cookie},
            headers={"User-Agent": ROBLOX_UA},
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            return {"id": data.get("id"), "name": data.get("name") or data.get("displayName")}
    except Exception as ex:
        _log(f"Roblox validate fail: {ex}")
    return None


def _roblox_csrf(cookie: str) -> str | None:
    """Получает X-CSRF-TOKEN через пробный POST (Roblox возвращает 403 с токеном)."""
    try:
        r = requests.post(
            "https://auth.roblox.com/v2/logout",
            cookies={".ROBLOSECURITY": cookie},
            headers={"User-Agent": ROBLOX_UA},
            timeout=10,
        )
        return r.headers.get("x-csrf-token") or r.headers.get("X-CSRF-TOKEN")
    except Exception as ex:
        _log(f"Roblox csrf fail: {ex}")
        return None


def roblox_get_vip_server(cookie: str, vip_server_id: int) -> dict[str, Any] | None:
    try:
        r = requests.get(
            f"https://games.roblox.com/v1/vip-servers/{vip_server_id}",
            cookies={".ROBLOSECURITY": cookie},
            headers={"User-Agent": ROBLOX_UA},
            timeout=15,
        )
        if r.status_code == 200:
            return r.json()
        _log(f"VIP-server {vip_server_id} GET {r.status_code}: {r.text[:200]}")
    except Exception as ex:
        _log(f"VIP-server {vip_server_id} GET fail: {ex}")
    return None


def roblox_regenerate_link(cookie: str, vip_server_id: int) -> dict[str, Any] | None:
    """
    Регенерирует share-link приватного сервера. Возвращает dict с link/joinCode или None.
    Использует PATCH /v1/vip-servers/{id} с newJoinCode=true (Roblox endpoint).
    """
    info = roblox_get_vip_server(cookie, vip_server_id)
    if not info:
        return None
    csrf = _roblox_csrf(cookie)
    if not csrf:
        return None
    body = {
        "active": info.get("active", True),
        "name": info.get("name", "") or "",
        "newJoinCode": True,
    }
    headers = {
        "X-CSRF-TOKEN": csrf,
        "Content-Type": "application/json",
        "User-Agent": ROBLOX_UA,
        "Origin": "https://www.roblox.com",
        "Referer": f"https://www.roblox.com/private-server/configure/{vip_server_id}",
    }
    try:
        r = requests.patch(
            f"https://games.roblox.com/v1/vip-servers/{vip_server_id}",
            json=body,
            headers=headers,
            cookies={".ROBLOSECURITY": cookie},
            timeout=20,
        )
        if r.status_code == 200:
            return r.json()
        # retry once on csrf mismatch
        if r.status_code == 403 and r.headers.get("x-csrf-token"):
            headers["X-CSRF-TOKEN"] = r.headers["x-csrf-token"]
            r = requests.patch(
                f"https://games.roblox.com/v1/vip-servers/{vip_server_id}",
                json=body, headers=headers,
                cookies={".ROBLOSECURITY": cookie}, timeout=20,
            )
            if r.status_code == 200:
                return r.json()
        _log(f"Regenerate {vip_server_id} HTTP {r.status_code}: {r.text[:200]}")
    except Exception as ex:
        _log(f"Regenerate {vip_server_id} fail: {ex}")
    return None


def _extract_link(server_info: dict[str, Any]) -> str | None:
    """Извлекает share-link из ответа Roblox."""
    if not server_info:
        return None
    link = server_info.get("link")
    if isinstance(link, str) and link.startswith("http"):
        return link
    # в новом формате link может быть объектом
    if isinstance(link, dict):
        for k in ("shareLink", "url", "href"):
            if isinstance(link.get(k), str):
                return link[k]
    join_code = server_info.get("joinCode") or server_info.get("linkCode")
    if join_code:
        return f"https://www.roblox.com/share?code={join_code}&type=Server"
    return None


# ---------- bizlogic ----------
def _extract_lot_id(cardinal: "Cardinal", order: Any, full_order: Any | None = None) -> Any:
    """Реальный id лота FunPay. `OrderShortcut` его не содержит (есть только
    `subcategory.id` — id подкатегории, а не лота!), поэтому тянем полный заказ
    (`account.get_order().lot_id`) с фоллбэком на html и `subcategory.id`."""
    full = full_order if full_order is not None else _get_full_order(cardinal, order)
    lid = getattr(full, "lot_id", None) if full is not None else None
    if lid:
        return str(lid)
    html = getattr(order, "html", "") or ""
    for pat in (r'data-offer="(\d+)"', r"offer\?id=(\d+)", r"offers/(\d+)"):
        m = re.search(pat, html)
        if m:
            return m.group(1)
    sub = getattr(order, "subcategory", None)
    return getattr(sub, "id", None) if sub is not None else None


def _get_full_order(cardinal: "Cardinal", order: Any) -> Any | None:
    order_id = getattr(order, "id", None)
    try:
        getter = getattr(getattr(cardinal, "account", None), "get_order", None)
        if callable(getter) and order_id is not None:
            return getter(str(order_id))
    except Exception:
        logger.debug("vip_roblox: get_order не удался", exc_info=True)
    return None


def _order_full_text(order: Any, full_order: Any | None = None) -> str:
    parts: list[str] = []
    for obj in (order, full_order):
        if obj is None:
            continue
        for attr in ("description", "title", "full_description"):
            value = getattr(obj, attr, None)
            if value:
                parts.append(str(value))
    return " ".join(parts).strip()


def _hashtag_time_to_hours(text: str) -> int | None:
    if not text:
        return None
    m = re.search(r"#Hours:\s*(\d+)", text, re.IGNORECASE)
    if m:
        hours = int(m.group(1))
        return hours if hours > 0 else None

    m = re.search(r"#Time:\s*(\d+)\s*(\S*)", text, re.IGNORECASE)
    if not m:
        return None
    value = int(m.group(1))
    suffix = m.group(2).lower().strip()
    if suffix in ("", "h", "ч", "час", "часа", "часов"):
        hours = value
    elif suffix in ("d", "д", "дн", "день", "дня", "дней"):
        hours = value * 24
    elif suffix in ("w", "нед", "неделя", "недель"):
        hours = value * 24 * 7
    elif suffix in ("m", "min", "мин", "минут"):
        hours = max(1, (value + 59) // 60)
    else:
        return None
    return hours if hours > 0 else None


def _match_lot(cfg: dict, order_desc: str, lot_id: int | str | None) -> dict | None:
    """Найти конфиг лота по описанию заказа или по lot_id."""
    desc = (order_desc or "").lower().strip()
    for lot in cfg["lots"]:
        if lot_id and str(lot_id) in _lot_ids(lot):
            return lot
        name_match = (lot.get("lot_name_match") or "").lower().strip()
        if name_match and name_match in desc:
            return lot
    return None


def _busy_vip_ids() -> set[int]:
    """VIP-серверы в активных арендах."""
    active = _load_json(ACTIVE_PATH, [])
    return {int(r["vip_server_id"]) for r in active if r.get("vip_server_id")}


def _lot_vip_pool(lot: dict, cfg: dict) -> list[int]:
    """Пул VIP-серверов для лота (через game_idx или напрямую vip_server_ids)."""
    pool: list[int] = []
    game_idx = lot.get("game_idx")
    if game_idx is not None:
        games = cfg.get("games") or []
        try:
            idx = int(game_idx)
        except (TypeError, ValueError):
            idx = -1
        if 0 <= idx < len(games):
            raw = games[idx].get("vip_server_ids") or []
            for v in raw:
                try:
                    pool.append(int(v))
                except (TypeError, ValueError):
                    continue
    if not pool:
        for v in lot.get("vip_server_ids") or []:
            try:
                pool.append(int(v))
            except (TypeError, ValueError):
                continue
    return pool


def _free_vips_for_lot(lot: dict, cfg: dict) -> int:
    """Сколько свободных VIP-серверов в пуле лота сейчас."""
    busy = _busy_vip_ids()
    return sum(1 for vid in _lot_vip_pool(lot, cfg) if vid not in busy)


# ── Встроенная либа lot-activation (см. steam_rental.py для подробностей) ──
_CARDINAL_REF_VR = None


def _shared_raise_state_vr(cardinal):
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


def _install_raise_skip_shared_vr(cardinal) -> bool:
    st = _shared_raise_state_vr(cardinal)
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
    logger.info("raise-skip: установлен общий патч raise_lots")
    return True


def _register_skip_vr(cardinal, plugin_name: str, category_ids):
    st = _shared_raise_state_vr(cardinal)
    if st is None:
        return
    st["by_plugin"][plugin_name] = {int(x) for x in category_ids
                                      if x is not None}


def _get_funpay_account_vr(cardinal):
    if cardinal is None:
        return None
    acc = getattr(cardinal, "account", None)
    if acc is not None and (hasattr(acc, "save_lot")
                            or hasattr(acc, "save_offer")):
        return acc
    if hasattr(cardinal, "save_lot") or hasattr(cardinal, "save_offer"):
        return cardinal
    return None


def _apply_lot_active_vr(cardinal, lot_id: int, active: bool) -> bool:
    acc = _get_funpay_account_vr(cardinal)
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


def _detect_category_id_vr(cardinal, lot_id: int):
    acc = _get_funpay_account_vr(cardinal)
    if acc is None or not hasattr(acc, "get_lot_fields"):
        return None
    try:
        fields = acc.get_lot_fields(int(lot_id))
    except Exception:
        return None
    cat = getattr(getattr(fields, "subcategory", None), "category", None)
    cid = getattr(cat, "id", None)
    return int(cid) if cid is not None else None


_ACTIONS_ICONS_VR = {
    "lot_activated":   "✅ ЛОТ ВКЛ ",
    "lot_deactivated": "⛔ ЛОТ ВЫКЛ",
    "lot_save_failed": "⚠ ЛОТ FAIL",
    "raise_skipped":   "🚫 RAISE   ",
}


def _make_actions_logger_vr(plugin_name: str, storage_dir: str):
    try:
        os.makedirs(storage_dir, exist_ok=True)
        from logging.handlers import RotatingFileHandler
        log_path = os.path.join(storage_dir, "actions.log")
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


def _do_log_action_vr(lg, action: str, summary: str = "", **extra) -> None:
    if lg is None:
        return
    icon = _ACTIONS_ICONS_VR.get(action, f"• {action:10}")
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


def _common_lib():
    try:
        import lot_activation_common  # type: ignore
        return lot_activation_common
    except Exception:
        pass

    class _Shim:
        @staticmethod
        def get_funpay_account(c):
            return _get_funpay_account_vr(c)

        @staticmethod
        def apply_lot_active(c, lid, act):
            return _apply_lot_active_vr(c, int(lid), bool(act))

        @staticmethod
        def install_raise_skip_patch(c):
            return _install_raise_skip_shared_vr(c)

        @staticmethod
        def register_skip_categories(pname, ids):
            _register_skip_vr(_CARDINAL_REF_VR, pname, ids)

        @staticmethod
        def detect_category_id(c, lid):
            return _detect_category_id_vr(c, int(lid))

        @staticmethod
        def make_actions_logger(pname, sdir):
            return _make_actions_logger_vr(pname, sdir)

        @staticmethod
        def log_action(lg, action, summary="", **extra):
            _do_log_action_vr(lg, action, summary, **extra)

    return _Shim()


_actions_logger_vr: "logging.Logger | None" = None


def _get_actions_logger_vr() -> "logging.Logger | None":
    global _actions_logger_vr
    if _actions_logger_vr is not None:
        return _actions_logger_vr
    lib = _common_lib()
    if lib is None:
        return None
    _actions_logger_vr = lib.make_actions_logger("vip_roblox", PLUGIN_DIR)
    return _actions_logger_vr


def _log_action_vr(action: str, summary: str = "", **extra: Any) -> None:
    lib = _common_lib()
    if lib is None:
        return
    lib.log_action(_get_actions_logger_vr(), action, summary, **extra)


def _update_lot_activation(cardinal: "Cardinal | None") -> None:
    """Деактивирует лоты FunPay без свободных VIP, активирует — при наличии.

    Использует lot_activation_common.apply_lot_active (правильный путь
    через FunPayAPI: get_lot_fields → fields.active → save_lot).
    Управляется флагом `settings.auto_deactivate_lots`. Лоты с нечисловым
    `lot_id` (key-word привязки) пропускаются.

    ВАЖНО: старый код звал `cardinal.set_lot_active(...)` — этот метод в
    FunPayCardinal НЕ существует, потому ничего не работало. Теперь зовём
    `cardinal.account.save_lot` через общую либу.
    """
    if cardinal is None:
        return
    cfg = _load_config()
    if not cfg["settings"].get("auto_deactivate_lots", True):
        return
    if not cfg.get("running"):
        return
    lib = _common_lib()
    if lib is None:
        logger.debug(
            "vip_roblox: lot_activation_common.py не найден — "
            "автоактивация лотов отключена")
        return

    for lot in cfg.get("lots") or []:
        raw_ids = _lot_ids(lot)
        try:
            lot_ids_int = [int(raw_id) for raw_id in raw_ids]
        except (TypeError, ValueError):
            continue  # keyword-привязка, не числовой lot_id — пропуск
        free = _free_vips_for_lot(lot, cfg)
        want_active = free > 0
        for lot_id_int in lot_ids_int:
            try:
                lib.apply_lot_active(cardinal, lot_id_int, want_active)
                if want_active:
                    logger.debug(
                        "vip_roblox: лот %s активен (%d свободных VIP)",
                        lot_id_int, free)
                    _log_action_vr("lot_activated",
                                   f"Лот {lot_id_int} активирован",
                                   lot_id=lot_id_int, free=free)
                else:
                    logger.info(
                        "vip_roblox: деактивирован лот %s (нет свободных VIP)",
                        lot_id_int)
                    _log_action_vr("lot_deactivated",
                                   f"Лот {lot_id_int} деактивирован — нет VIP",
                                   lot_id=lot_id_int, free=free)
            except Exception as e:
                logger.debug(
                    "vip_roblox: apply_lot_active(%s) failed: %s",
                    lot_id_int, e, exc_info=True)
                _log_action_vr("lot_save_failed",
                               f"Не удалось сохранить лот {lot_id_int}",
                               lot_id=lot_id_int, want_active=want_active,
                               error=f"{type(e).__name__}: {str(e)[:120]}")


def _pick_free_vip(lot: dict, cfg: dict | None = None) -> int | None:
    """Первый свободный vip_server_id из пула лота или связанной игры."""
    busy = _busy_vip_ids()
    pool = _lot_vip_pool(lot, cfg or _load_config())
    for vid in pool:
        if vid not in busy:
            return vid
    return None


def _format_msg(template: str, **vars: Any) -> str:
    try:
        return template.format(**vars)
    except Exception:
        return template


# ============================================================================
# v1.2.0 — чистое ядро (pure core) + файлы состояния
# Все функции ниже детерминированы (время инжектируется) и покрыты
# property-тестами. Telebot/Roblox/FunPay — тонкая оболочка поверх них.
# ============================================================================

# ---------- i18n: шаблоны покупателю (RU/EN) ----------
_DEFAULT_TEMPLATES_RU: dict[str, str] = {
    "payment": (
        "🎮 Спасибо за покупку!\n"
        "✅ Ваша персональная ссылка на сервер:\n"
        "🔗 {link}\n\n"
        "🕒 Время аренды: {hours} час(а/ов)\n"
        "💰 Заказ: #{order_id}\n\n"
        "🎯 Ссылка действует только для тебя. По окончании аренды она будет обновлена.\n"
        "Команды в этом чате:\n"
        "  !time — оставшееся время\n"
        "  !ссылка — показать ссылку ещё раз\n"
        "  !vip — статус аренды\n"
        "  !отзыв — продлить аренду за отзыв\n\n"
        "🎉 Приятной игры!"
    ),
    "expiration": (
        "⏰ Время аренды истекло, пожалуйста подтвердите заказ\n"
        "🔗 https://funpay.com/orders/{order_id}/"
    ),
    "queued": (
        "🕒 Все VIP-сервера сейчас заняты. Ты в очереди: позиция #{position}.\n"
        "Ссылка придёт автоматически, как только освободится слот."
    ),
    "queue_advanced": "📈 Очередь продвинулась! Твоя новая позиция: #{position}.",
    "served_from_queue": (
        "🎮 Освободился слот! Твоя персональная ссылка:\n🔗 {link}\n\n"
        "🕒 Время аренды: {hours} час(а/ов)\n🎉 Приятной игры!"
    ),
    "expiry_warning": (
        "⏳ До конца аренды осталось ~{minutes} мин.\n"
        "Чтобы продлить — оставь отзыв и напиши !отзыв."
    ),
    "blocked": "🚫 Извини, выдача для тебя недоступна. Свяжись с продавцом.",
    "review_thanks": "⭐ Спасибо за отзыв ({stars}/5)! Аренда продлена на {hours}ч.",
    "review_not_found": (
        "⚠️ Отзыв не найден. Оставь отзыв на заказ на FunPay, затем напиши !отзыв ещё раз."
    ),
    "review_already": "Бонус за отзыв уже получен по этому заказу.",
    "review_low_stars": "⚠️ Бонус даётся только за отзыв с оценкой {threshold}★ и выше.",
    "vip_status_active": (
        "🎟 Твоя аренда активна.\n"
        "🆔 Сервер: {server_id}\n⏱ Осталось: {remaining}\n🔗 {link}"
    ),
    "vip_status_queue": "🕒 Ты в очереди, позиция #{position}. Ссылка придёт автоматически.",
    "vip_status_none": "У тебя нет активной аренды или места в очереди.",
    "lang_switched": "🌐 Язык переключён на русский.",
    "commands": (
        "Команды:\n!time — оставшееся время\n!ссылка — повторить ссылку\n"
        "!vip — статус аренды\n!отзыв — бонус за отзыв\n!engrent — English"
    ),
}

_DEFAULT_TEMPLATES_EN: dict[str, str] = {
    "payment": (
        "🎮 Thanks for your purchase!\n"
        "✅ Your personal server link:\n"
        "🔗 {link}\n\n"
        "🕒 Rental time: {hours} hour(s)\n"
        "💰 Order: #{order_id}\n\n"
        "🎯 This link is for you only. It will be regenerated when the rental ends.\n"
        "Chat commands:\n"
        "  !time — remaining time\n"
        "  !link — show the link again\n"
        "  !vip — rental status\n"
        "  !review — extend the rental for a review\n\n"
        "🎉 Have fun!"
    ),
    "expiration": (
        "⏰ Your rental time is over, please confirm the order\n"
        "🔗 https://funpay.com/orders/{order_id}/"
    ),
    "queued": (
        "🕒 All VIP servers are busy right now. You are in the queue: position #{position}.\n"
        "The link will arrive automatically once a slot frees up."
    ),
    "queue_advanced": "📈 The queue moved! Your new position: #{position}.",
    "served_from_queue": (
        "🎮 A slot opened up! Your personal link:\n🔗 {link}\n\n"
        "🕒 Rental time: {hours} hour(s)\n🎉 Have fun!"
    ),
    "expiry_warning": (
        "⏳ About {minutes} min left until your rental ends.\n"
        "To extend — leave a review and type !review."
    ),
    "blocked": "🚫 Sorry, delivery is not available for you. Contact the seller.",
    "review_thanks": "⭐ Thanks for the review ({stars}/5)! Rental extended by {hours}h.",
    "review_not_found": (
        "⚠️ Review not found. Leave a review for the order on FunPay, then type !review again."
    ),
    "review_already": "The review bonus has already been granted for this order.",
    "review_low_stars": "⚠️ The bonus is only granted for a review rated {threshold}★ or higher.",
    "vip_status_active": (
        "🎟 Your rental is active.\n"
        "🆔 Server: {server_id}\n⏱ Remaining: {remaining}\n🔗 {link}"
    ),
    "vip_status_queue": "🕒 You are in the queue, position #{position}. The link will arrive automatically.",
    "vip_status_none": "You have no active rental or queue entry.",
    "lang_switched": "🌐 Language switched to English.",
    "commands": (
        "Commands:\n!time — remaining time\n!link — show the link again\n"
        "!vip — rental status\n!review — review bonus\n!rusrent — Русский"
    ),
}


def _load_templates(lang: str) -> dict[str, str]:
    path = TEMPLATES_EN_PATH if lang == "en" else TEMPLATES_RU_PATH
    default = _DEFAULT_TEMPLATES_EN if lang == "en" else _DEFAULT_TEMPLATES_RU
    data = _load_json(path, default)
    if not isinstance(data, dict):
        return dict(default)
    return data


def _load_buyer_lang() -> dict:
    data = _load_json(BUYER_LANG_PATH, {})
    return data if isinstance(data, dict) else {}


def _save_buyer_lang(data: dict) -> None:
    _save_json(BUYER_LANG_PATH, data)


def _load_blacklist() -> list:
    data = _load_json(BLACKLIST_PATH, [])
    return data if isinstance(data, list) else []


def _save_blacklist(data: list) -> None:
    _save_json(BLACKLIST_PATH, data)


def _load_liveness() -> dict:
    data = _load_json(LIVENESS_PATH, {"dead_servers": [], "dead_accounts": [], "checked_at": 0.0})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("dead_servers", [])
    data.setdefault("dead_accounts", [])
    data.setdefault("checked_at", 0.0)
    return data


def _save_liveness(data: dict) -> None:
    _save_json(LIVENESS_PATH, data)


# ---------- i18n: чистые функции ----------
def _lang_for_buyer(buyer_id: Any, buyer_lang: dict, default_lang: str) -> str:
    """Язык покупателя: выбранный, иначе дефолт. Только 'ru'|'en'."""
    chosen = buyer_lang.get(str(buyer_id))
    lang = chosen or default_lang
    return "en" if str(lang).lower() == "en" else "ru"


def _resolve_lang_command(text: str) -> "str | None":
    t = (text or "").strip().lower()
    if t in ("!engrent", "!english"):
        return "en"
    if t in ("!rusrent", "!russian"):
        return "ru"
    return None


def _render_template(templates_ru: dict, templates_en: dict, key: str,
                     lang: str, **vars: Any) -> str:
    """Рендер шаблона на языке покупателя; при отсутствии EN — фолбэк на RU."""
    tpl = (templates_en if lang == "en" else templates_ru).get(key)
    if tpl is None:
        tpl = templates_ru.get(key, "")
    try:
        return tpl.format(**vars)
    except Exception:
        return tpl


# ---------- blacklist: чистые функции ----------
def _is_blacklisted(blacklist: list, buyer_id: Any, username: Any) -> bool:
    ids = {str(e.get("buyer_id")).lower() for e in blacklist if e.get("buyer_id") is not None}
    names = {str(e.get("username")).lower() for e in blacklist if e.get("username")}
    if buyer_id is not None and str(buyer_id).lower() in ids:
        return True
    if username and str(username).lower() in names:
        return True
    return False


def _add_to_blacklist(blacklist: list, buyer_id: Any, username: Any, reason: str = "") -> list:
    if _is_blacklisted(blacklist, buyer_id, username):
        return blacklist
    return blacklist + [{
        "buyer_id": str(buyer_id) if buyer_id is not None else "",
        "username": str(username) if username else "",
        "reason": reason, "ts": time.time(),
    }]


def _remove_from_blacklist(blacklist: list, key: Any) -> list:
    k = str(key).lower()
    return [e for e in blacklist
            if str(e.get("buyer_id", "")).lower() != k
            and str(e.get("username", "")).lower() != k]


# ---------- очередь (FIFO): чистые функции ----------
def _sort_queue(entries: list) -> list:
    return sorted(entries, key=lambda e: float(e.get("queued_at", 0) or 0))


def _queue_position(entries: list, order_id: Any, lot_id: Any) -> int:
    """1-based позиция заказа среди записей того же лота (FIFO)."""
    same = [e for e in _sort_queue(entries) if str(e.get("lot_id")) == str(lot_id)]
    for i, e in enumerate(same):
        if str(e.get("order_id")) == str(order_id):
            return i + 1
    return len(same) + 1


def _positions_changed(before: list, after: list) -> dict:
    """{chat_id: новая_позиция} для записей, чья позиция уменьшилась."""

    def _positions(entries):
        pos: dict = {}
        for lot_id in {str(e.get("lot_id")) for e in entries}:
            same = [e for e in _sort_queue(entries) if str(e.get("lot_id")) == lot_id]
            for i, e in enumerate(same):
                pos[str(e.get("order_id"))] = (e, i + 1)
        return pos

    before_pos = _positions(before)
    after_pos = _positions(after)
    out: dict = {}
    for oid, (entry, new_p) in after_pos.items():
        old = before_pos.get(oid)
        if old and new_p < old[1]:
            out[entry.get("chat_id")] = new_p
    return out


# ---------- лимиты: чистые функции ----------
def _server_active_count(active: list, vip_id: int) -> int:
    return sum(1 for r in active if str(r.get("vip_server_id")) == str(vip_id))


def _server_period_count(history: list, vip_id: int, now: float, period_sec: int) -> int:
    if period_sec <= 0:
        return 0
    lo = now - period_sec
    return sum(1 for h in history
               if str(h.get("vip_server_id")) == str(vip_id)
               and lo <= float(h.get("ts", 0) or 0) <= now)


def _buyer_active_count(active: list, buyer: Any) -> int:
    return sum(1 for r in active if str(r.get("buyer")) == str(buyer))


def _server_eligible(vip_id: int, active: list, history: list, dead_servers: set,
                     now: float, *, concurrent_limit: int, period_limit: int,
                     period_sec: int, busy: set) -> bool:
    if int(vip_id) in busy or int(vip_id) in dead_servers:
        return False
    if concurrent_limit and _server_active_count(active, vip_id) >= concurrent_limit:
        return False
    if period_limit and _server_period_count(history, vip_id, now, period_sec) >= period_limit:
        return False
    return True


def _select_vip(pool: list, active: list, history: list, dead_servers: set,
                now: float, *, concurrent_limit: int, period_limit: int,
                period_sec: int, busy: set) -> "int | None":
    """Первый пригодный VIP из пула: свободен ∧ не мёртв ∧ под лимитами."""
    for vid in pool:
        try:
            vid = int(vid)
        except (TypeError, ValueError):
            continue
        if _server_eligible(vid, active, history, dead_servers, now,
                            concurrent_limit=concurrent_limit, period_limit=period_limit,
                            period_sec=period_sec, busy=busy):
            return vid
    return None


# ---------- liveness: чистые функции ----------
def _liveness_due(checked_at: float, now: float, interval_sec: int) -> bool:
    return (now - float(checked_at or 0)) >= interval_sec


def _filter_dead(pool: list, dead_servers: set) -> list:
    return [v for v in pool if int(v) not in dead_servers]


# ---------- предупреждения об истечении: чистые функции ----------
def _due_warning_offsets(expires_at: float, started_at: float, total_hours: float,
                         offsets_min: list, warned: list, now: float) -> list:
    """Офсеты (мин), которые пора отправить: now>=expires-offset, не отправлены,
    и офсет меньше длительности аренды (Req 2.5)."""
    total_sec = float(total_hours or 0) * 3600
    warned_set = {int(x) for x in (warned or [])}
    out: list = []
    for off in offsets_min or []:
        try:
            off = int(off)
        except (TypeError, ValueError):
            continue
        if off in warned_set:
            continue
        if off * 60 >= total_sec and total_sec > 0:
            continue
        if now >= expires_at - off * 60:
            out.append(off)
    return out


# ---------- бонус за отзыв: чистая функция ----------
def _review_qualifies(review: "dict | None", stars_threshold: int) -> bool:
    return bool(review) and int(review.get("stars", 0) or 0) >= int(stars_threshold)


# ---------- приоритет приёма заказа: чистая функция ----------
def _decide_order_intake(*, blacklist_enabled: bool, is_blacklisted: bool,
                         per_buyer_at_limit: bool, eligible_vip: "int | None") -> str:
    """Возвращает: refuse_blacklist | queue_buyer_limit | queue_no_server | deliver."""
    if blacklist_enabled and is_blacklisted:
        return "refuse_blacklist"
    if per_buyer_at_limit:
        return "queue_buyer_limit"
    if eligible_vip is None:
        return "queue_no_server"
    return "deliver"


# ---------- фоновый поток таймеров аренд ----------
_stop_event = threading.Event()


def _fetch_review(cardinal: "Cardinal", order_id: str) -> dict | None:
    """Возвращает {stars, text, author} если отзыв есть, иначе None."""
    try:
        order = cardinal.account.get_order(str(order_id))
    except Exception as ex:
        _log(f"Заказ #{order_id}: ошибка проверки отзыва: {ex}")
        return None
    review = getattr(order, "review", None)
    if review is None:
        return None
    stars = getattr(review, "stars", None)
    if not stars:
        return None
    return {
        "stars": int(stars),
        "text": getattr(review, "text", "") or "",
        "author": getattr(review, "author", "") or "",
    }


def _grant_review_bonus(cardinal: "Cardinal", rental: dict, bonus_hours: int,
                        review: dict, silent: bool = False) -> None:
    rental["expires_at"] += bonus_hours * 3600
    rental["review_bonused"] = True
    rental["review_stars"] = review["stars"]
    if not silent:
        _send_buyer(cardinal, rental.get("buyer_id"), rental["chat_id"],
                    "review_thanks", stars=review["stars"], hours=bonus_hours)
    _notify_tg(cardinal,
               f"⭐ Отзыв ({review['stars']}/5) на заказ #{rental['order_id']}: "
               f"+{bonus_hours}ч.")
    _log(f"Аренда #{rental['order_id']}: бонус +{bonus_hours}ч (отзыв {review['stars']}/5).")


def _run_liveness_check(cardinal: "Cardinal", cfg: dict, now: float) -> None:
    """Проверка живости аккаунтов и VIP-серверов (Req 3). Не трогает занятые сервера."""
    state = _load_liveness()
    accounts = cfg.get("accounts") or []
    busy = _busy_vip_ids()

    # 1) аккаунты по куке
    dead_accounts: list[int] = []
    for idx, acc in enumerate(accounts):
        ok = roblox_validate(acc.get("cookie", ""))
        if not ok:
            dead_accounts.append(idx)
    new_dead_acc = [i for i in dead_accounts if i not in set(state.get("dead_accounts") or [])]
    for i in new_dead_acc:
        uname = accounts[i].get("username", f"#{i}") if i < len(accounts) else f"#{i}"
        _notify_tg(cardinal, f"⚠️ <b>VipRoblox</b>: аккаунт <b>{uname}</b> не отвечает (кука мертва).")
        _log(f"Liveness: аккаунт #{i} ({uname}) мёртв.")

    # 2) VIP-серверы (только из пулов; занятые арендой не дёргаем — Req 3.9)
    pool_ids: set[int] = set()
    for lot in cfg.get("lots") or []:
        for v in _lot_vip_pool(lot, cfg):
            pool_ids.add(int(v))
    prev_dead = set(state.get("dead_servers") or [])
    dead_servers: set[int] = set()
    # выбираем кук для запроса: первый валидный аккаунт
    cookie = accounts[0].get("cookie", "") if accounts else ""
    for vid in pool_ids:
        if vid in busy:
            # занятый сервер считаем живым, не дёргаем
            continue
        info = roblox_get_vip_server(cookie, vid) if cookie else None
        if info is None:
            dead_servers.add(vid)
    # сохраняем занятые как живые (не перетираем их статус)
    new_dead_srv = dead_servers - prev_dead
    recovered = prev_dead - dead_servers - busy
    for vid in new_dead_srv:
        _notify_tg(cardinal, f"⚠️ <b>VipRoblox</b>: VIP-сервер <code>{vid}</code> недоступен.")
        _log(f"Liveness: VIP-сервер {vid} мёртв.")
    for vid in recovered:
        _log(f"Liveness: VIP-сервер {vid} снова доступен.")

    state["dead_accounts"] = dead_accounts
    state["dead_servers"] = sorted(dead_servers)
    state["checked_at"] = now
    _save_liveness(state)


def _rental_loop(cardinal: "Cardinal") -> None:
    _log("Фоновый цикл аренд VipRoblox запущен.")
    while not _stop_event.is_set():
        try:
            cfg = _load_config()
            s = cfg["settings"]
            bonus = int(s.get("review_bonus_hours", 0) or 0)
            threshold = int(s.get("review_stars_threshold", 5) or 5)
            auto_review = bool(s.get("auto_review_check", True))
            offsets = s.get("expiry_warning_offsets_min") or []
            op_notify = bool(s.get("operator_expiry_notify"))
            now = time.time()

            # ---- liveness pass (по своему интервалу) ----
            if s.get("liveness_enabled", True):
                live = _load_liveness()
                if _liveness_due(live.get("checked_at", 0.0), now,
                                 int(s.get("liveness_interval_sec", 1800) or 1800)):
                    try:
                        _run_liveness_check(cardinal, cfg, now)
                    except Exception:
                        logger.exception("Ошибка в _run_liveness_check")

            active = [_migrate_rental(r) for r in _load_json(ACTIVE_PATH, [])]
            still: list[dict] = []
            changed = False
            for rental in active:
                # авто-проверка отзыва (с порогом звёзд) на активных арендах
                if auto_review and bonus > 0 and not rental.get("review_bonused") \
                        and rental["expires_at"] > now:
                    review = _fetch_review(cardinal, rental["order_id"])
                    if _review_qualifies(review, threshold):
                        _grant_review_bonus(cardinal, rental, bonus, review)
                        changed = True

                # упреждающие предупреждения об истечении (Req 2)
                if rental["expires_at"] > now and offsets:
                    due = _due_warning_offsets(
                        rental["expires_at"], rental.get("started_at", now),
                        rental.get("hours", 0), offsets, rental.get("warned_offsets", []), now)
                    for off in due:
                        _send_buyer(cardinal, rental.get("buyer_id"), rental["chat_id"],
                                    "expiry_warning", minutes=off)
                        if op_notify:
                            _notify_tg(cardinal,
                                       f"⏳ Аренда #{rental['order_id']} ({rental.get('buyer','')}): "
                                       f"до конца ~{off} мин.")
                        rental.setdefault("warned_offsets", []).append(off)
                        changed = True

                if rental["expires_at"] <= now:
                    if auto_review and bonus > 0 and not rental.get("review_bonused"):
                        review = _fetch_review(cardinal, rental["order_id"])
                        if _review_qualifies(review, threshold):
                            _grant_review_bonus(cardinal, rental, bonus, review)
                            changed = True
                            if rental["expires_at"] > now:
                                still.append(rental)
                                continue

                    # 1) сообщение об истечении (на языке покупателя)
                    _send_buyer(cardinal, rental.get("buyer_id"), rental["chat_id"],
                                "expiration", buyer_name=rental.get("buyer", ""),
                                server_id=rental.get("vip_server_id", ""),
                                hours=rental.get("hours", ""),
                                order_id=rental.get("order_id", ""),
                                link=rental.get("link", ""))
                    # 2) регенерируем ссылку — старая покупательская становится мёртвой
                    vip_id = rental.get("vip_server_id")
                    acc_idx = int(rental.get("account_idx", 0))
                    accounts = cfg.get("accounts") or []
                    if vip_id and 0 <= acc_idx < len(accounts):
                        result = roblox_regenerate_link(accounts[acc_idx]["cookie"], int(vip_id))
                        if result:
                            _log(f"Аренда #{rental['order_id']}: ссылка обновлена, старая мёртвая.")
                        else:
                            _log(f"Аренда #{rental['order_id']}: ОШИБКА регенерации после истечения!")
                    _notify_tg(cardinal, f"⏰ Аренда #{rental['order_id']} истекла ({rental.get('buyer', '')}).")
                    _log(f"Аренда #{rental['order_id']} истекла, уведомление отправлено")
                    changed = True
                else:
                    still.append(rental)
            if changed or len(still) != len(active):
                _save_json(ACTIVE_PATH, still)
            # обработка очереди — если освободились слоты
            try:
                _process_queue(cardinal)
            except Exception:
                logger.exception("Ошибка в _process_queue")
            # синхронизация активации лотов FunPay по занятости пулов
            try:
                _update_lot_activation(cardinal)
            except Exception:
                logger.exception("Ошибка в _update_lot_activation")
        except Exception:
            logger.exception("Ошибка в _rental_loop")
        interval = max(_load_config()["settings"].get("check_interval_sec", 300), 30)
        _stop_event.wait(interval)
    _log("Фоновый цикл аренд VipRoblox остановлен.")


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


def _record_history(entry: dict) -> None:
    h = _load_json(HISTORY_PATH, [])
    h.append(entry)
    h = h[-MAX_HISTORY:]
    _save_json(HISTORY_PATH, h)


def _bump_stats(hours: int, earnings: float) -> None:
    s = _load_json(STATS_PATH, DEFAULT_STATS)
    s["total_hours"] = int(s.get("total_hours", 0)) + int(hours)
    s["earnings"] = float(s.get("earnings", 0)) + float(earnings)
    s["orders"] = int(s.get("orders", 0)) + 1
    _save_json(STATS_PATH, s)


# ---------- обработка заказа ----------
def _send_buyer(cardinal: "Cardinal", buyer_id: Any, chat_id: Any, key: str,
                **vars: Any) -> bool:
    """Отправить покупателю шаблон на его языке (Req 4)."""
    cfg = _load_config()
    lang = _lang_for_buyer(buyer_id, _load_buyer_lang(),
                           cfg["settings"].get("default_language", "ru"))
    text = _render_template(_load_templates("ru"), _load_templates("en"), key, lang, **vars)
    try:
        cardinal.send_message(chat_id, text)
        return True
    except Exception:
        logger.exception("vip_roblox: не удалось отправить сообщение покупателю")
        return False


def _enqueue(cardinal: "Cardinal", lot: dict, order_id: str, buyer: str,
             chat_id: Any, hours: int, price: float, buyer_id: Any = None) -> None:
    """Поставить заказ в очередь и уведомить покупателя о позиции (Req 1.1)."""
    q = _load_json(QUEUE_PATH, [])
    if any(str(x.get("order_id")) == str(order_id) for x in q):
        return
    q.append({
        "order_id": str(order_id), "buyer": buyer, "buyer_id": buyer_id,
        "chat_id": chat_id, "lot_id": lot.get("lot_id"),
        "hours": hours, "price": price, "queued_at": time.time(),
    })
    _save_json(QUEUE_PATH, q)
    pos = _queue_position(q, order_id, lot.get("lot_id"))
    _send_buyer(cardinal, buyer_id, chat_id, "queued", position=pos)
    _notify_tg(cardinal,
               f"🕒 Очередь: заказ #{order_id} ({buyer}) поставлен на позицию #{pos}.")
    _log(f"Заказ #{order_id}: добавлен в очередь (поз #{pos}).")


def _select_vip_for_lot(lot: dict, cfg: dict, *, now: float) -> "int | None":
    """Выбрать пригодный VIP: свободен ∧ не мёртв ∧ под пер-серверными лимитами."""
    s = cfg["settings"]
    pool = _lot_vip_pool(lot, cfg)
    dead = {int(v) for v in (_load_liveness().get("dead_servers") or [])}
    return _select_vip(
        pool, _load_json(ACTIVE_PATH, []), _load_json(HISTORY_PATH, []), dead, now,
        concurrent_limit=int(s.get("per_server_concurrent_limit", 0) or 0),
        period_limit=int(s.get("per_server_period_limit", 0) or 0),
        period_sec=int(s.get("per_server_period_sec", 86400) or 86400),
        busy=_busy_vip_ids())


def _deliver(cardinal: "Cardinal", lot: dict, order_id: str, buyer: str,
             chat_id: Any, hours: int, price: float, buyer_id: Any = None,
             template_key: str = "payment") -> str:
    """Выдать VIP покупателю. Возвращает 'delivered' | 'no_server' | 'error'.
    Постановкой в очередь занимается вызывающий код (Req 10)."""
    cfg = _load_config()
    accounts = cfg.get("accounts") or []
    acc_idx = int(lot.get("account_idx", 0) or 0)
    if not (0 <= acc_idx < len(accounts)):
        _log(f"Заказ #{order_id}: account_idx={acc_idx} вне диапазона ({len(accounts)} аккаунтов).")
        return "error"
    cookie = accounts[acc_idx]["cookie"]

    vip_id = _select_vip_for_lot(lot, cfg, now=time.time())
    if not vip_id:
        return "no_server"

    result = roblox_regenerate_link(cookie, vip_id)
    link = _extract_link(result) if result else None
    if not link:
        _log(f"Заказ #{order_id}: не удалось регенерировать ссылку для vip_server_id={vip_id}.")
        return "error"

    sent = _send_buyer(cardinal, buyer_id, chat_id, template_key,
                       buyer_name=buyer, server_id=vip_id, hours=hours,
                       order_id=order_id, link=link)
    if not sent:
        return "error"

    now = time.time()
    active = _load_json(ACTIVE_PATH, [])
    active.append(_migrate_rental({
        "order_id": str(order_id), "buyer": buyer, "buyer_id": buyer_id,
        "chat_id": chat_id, "vip_server_id": vip_id, "account_idx": acc_idx,
        "lot_id": lot.get("lot_id"), "hours": hours, "link": link,
        "started_at": now, "expires_at": now + hours * 3600,
    }))
    _save_json(ACTIVE_PATH, active)
    _bump_stats(hours, price)
    _record_history({
        "order_id": str(order_id), "buyer": buyer, "buyer_id": buyer_id,
        "lot_id": lot.get("lot_id"), "vip_server_id": vip_id,
        "hours": hours, "price": price, "ts": now,
    })
    _log(f"Заказ #{order_id}: выдана ссылка (vip {vip_id}), аренда на {hours}ч.")
    _update_lot_activation(cardinal)
    return "delivered"


def _process_queue(cardinal: "Cardinal") -> None:
    """FIFO-выдача из очереди при наличии пригодных слотов (Req 1.3, 7.6, 10.3, 10.6)."""
    q = _load_json(QUEUE_PATH, [])
    if not q:
        return
    cfg = _load_config()
    s = cfg["settings"]
    pbl = int(s.get("per_buyer_concurrent_limit", 0) or 0)
    before = list(q)
    remaining: list[dict] = []
    for entry in _sort_queue(q):
        lot = next((l for l in cfg["lots"]
                    if str(l.get("lot_id")) == str(entry.get("lot_id"))), None)
        if not lot:
            remaining.append(entry)
            continue
        # пер-байер лимит: держим в очереди, пока не освободится (10.3)
        if pbl and _buyer_active_count(_load_json(ACTIVE_PATH, []), entry.get("buyer")) >= pbl:
            remaining.append(entry)
            continue
        status = _deliver(cardinal, lot, str(entry["order_id"]), entry["buyer"],
                          entry["chat_id"], int(entry["hours"]),
                          float(entry.get("price", 0)), entry.get("buyer_id"),
                          template_key="served_from_queue")
        if status != "delivered":
            remaining.append(entry)  # no_server/error → оставляем (10.6)
            continue
        _notify_tg(cardinal,
                   f"✅ Очередь: заказ #{entry['order_id']} ({entry['buyer']}) "
                   f"выдан после ожидания.")
        _log(f"Очередь: заказ #{entry['order_id']} обработан.")
    if len(remaining) != len(q):
        _save_json(QUEUE_PATH, remaining)
        # уведомляем продвинувшихся в очереди (Req 1.5)
        for chat_id, newpos in _positions_changed(before, remaining).items():
            ent = next((e for e in remaining if e.get("chat_id") == chat_id), None)
            _send_buyer(cardinal, ent.get("buyer_id") if ent else None, chat_id,
                        "queue_advanced", position=newpos)


def _on_new_order(cardinal: "Cardinal", event: "NewOrderEvent") -> None:
    cfg = _load_config()
    if not cfg["running"]:
        logger.debug("vip_roblox: плагин выключен (running=False), заказ #%s пропущен",
                     getattr(event.order, "id", "?"))
        return
    order = event.order
    full_order = _get_full_order(cardinal, order)

    order_text = _order_full_text(order, full_order)
    lot = _match_lot(cfg, order_text, _extract_lot_id(cardinal, order, full_order))
    if not lot:
        logger.debug("vip_roblox: заказ #%s — не наш лот, пропуск", getattr(order, "id", "?"))
        return

    s = cfg["settings"]
    tag_hours = _hashtag_time_to_hours(order_text)
    lot_hours = int(lot.get("hours") or 0)
    if tag_hours is None and lot_hours <= 0:
        _notify_tg(
            cardinal,
            f"⚠️ <b>VIP Roblox</b>: заказ <code>#{getattr(order, 'id', '?')}</code> "
            f"от <b>{getattr(order, 'buyer_username', '?')}</b> — в описании лота "
            f"<code>{lot.get('lot_id')}</code> не найден <code>#Hours: 3</code> "
            f"или <code>#Time: 3ч</code>. Добавь тег в описание FunPay или выдай вручную.",
        )
        return
    hours = tag_hours or lot_hours or int(s.get("min_hours", 1))
    price = float(getattr(order, "price", None) or lot.get("price") or 0)
    buyer = getattr(order, "buyer_username", None) or "buyer"
    buyer_id = getattr(order, "buyer_id", None)
    chat_id = getattr(order, "chat_id", None) or buyer_id

    _log_action_vr("delivery",
                    f"Получен заказ #{getattr(order, 'id', '?')} для лота {lot.get('lot_id')}",
                    order_id=getattr(order, "id", None), lot_id=lot.get("lot_id"),
                    buyer=buyer, buyer_id=buyer_id)

    if chat_id is None:
        _log(f"Заказ #{order.id}: не найден chat_id для отправки ссылки.")
        _notify_tg(cardinal,
                   f"⚠️ <b>VIP Roblox</b>: заказ <code>#{order.id}</code> от <b>{buyer}</b> "
                   f"— не найден chat_id, выдача невозможна. Выдай вручную или верни деньги.")
        return

    # ---- приоритет приёма (Req 10): blacklist → пер-байер лимит → пригодность сервера ----
    now = time.time()
    blacklist = _load_blacklist()
    bl_enabled = bool(s.get("blacklist_enabled", True))
    is_bl = _is_blacklisted(blacklist, buyer_id, buyer)
    pbl = int(s.get("per_buyer_concurrent_limit", 0) or 0)
    per_buyer_at_limit = bool(pbl) and _buyer_active_count(_load_json(ACTIVE_PATH, []), buyer) >= pbl
    eligible = _select_vip_for_lot(lot, cfg, now=now)
    decision = _decide_order_intake(blacklist_enabled=bl_enabled, is_blacklisted=is_bl,
                                    per_buyer_at_limit=per_buyer_at_limit, eligible_vip=eligible)

    if decision == "refuse_blacklist":
        _send_buyer(cardinal, buyer_id, chat_id, "blocked")
        _notify_tg(cardinal,
                   f"🚫 <b>VIP Roblox</b>: заказ <code>#{order.id}</code> от <b>{buyer}</b> "
                   f"отклонён — покупатель в чёрном списке.")
        _log_action_vr("lot_save_failed", f"Заказ #{order.id} отклонён (blacklist)",
                       order_id=order.id, buyer=buyer, buyer_id=buyer_id)
        return

    if decision in ("queue_buyer_limit", "queue_no_server"):
        pool = _lot_vip_pool(lot, cfg)
        dead = {int(v) for v in (_load_liveness().get("dead_servers") or [])}
        if decision == "queue_no_server" and pool and not _filter_dead(pool, dead):
            _notify_tg(cardinal,
                       f"⚠️ <b>VIP Roblox</b>: лот <code>{lot.get('lot_id')}</code> — "
                       f"все VIP-сервера помечены мёртвыми.")
        _enqueue(cardinal, lot, str(order.id), buyer, chat_id, hours, price, buyer_id)
        return

    # ---- выдача ----
    status = _deliver(cardinal, lot, str(order.id), buyer, chat_id, hours, price, buyer_id)
    if status == "no_server":
        _enqueue(cardinal, lot, str(order.id), buyer, chat_id, hours, price, buyer_id)
        return
    if status == "error":
        _log_action_vr("lot_save_failed", f"Не удалось выдать VIP для заказа #{order.id}",
                       order_id=order.id, lot_id=lot.get("lot_id"), buyer=buyer,
                       hours=hours, price=price)
        if s.get("auto_refund"):
            try:
                cardinal.account.refund(order.id)
                _log(f"Заказ #{order.id} возвращён (ошибка доставки).")
            except Exception:
                logger.exception("Не удалось вернуть заказ")
        else:
            try:
                cardinal.send_message(chat_id, "⚠️ Ошибка генерации ссылки. Свяжитесь с продавцом.")
            except Exception:
                pass
        _notify_tg(cardinal,
                   f"⚠️ <b>VIP Roblox</b>: НЕ выдан VIP по заказу <code>#{order.id}</code> "
                   f"от <b>{buyer}</b> (ошибка генерации ссылки).")
        return

    _log_action_vr("rental_start", f"Выдан VIP → {buyer} на {hours}ч",
                   order_id=order.id, lot_id=lot.get("lot_id"), buyer=buyer,
                   hours=hours, price=price)
    _notify_tg(cardinal, f"🛒 Новый заказ #{order.id} от {buyer}: {hours}ч, {price}.")


# ---------- BIND_TO_ORDER_STATUS_CHANGED: возвраты + авто-blacklist (Req 5.5) ----------
_REFUND_STATUSES = ("REFUNDED", "REFUND", "CANCELED", "CANCELLED", "REVERSED")


def _is_refund_status(status_obj: Any) -> bool:
    name = getattr(status_obj, "name", None) or str(status_obj)
    name = str(name).upper()
    return any(s in name for s in _REFUND_STATUSES)


def _on_order_status_changed(cardinal: "Cardinal", event: Any) -> None:
    cfg = _load_config()
    order = getattr(event, "order", None)
    if order is None:
        return
    status = getattr(order, "status", None)
    if not _is_refund_status(status):
        return
    order_id = str(getattr(order, "id", "") or "")
    buyer = getattr(order, "buyer_username", None) or ""
    buyer_id = getattr(order, "buyer_id", None)

    # идемпотентность: помечаем history-запись один раз
    history = _load_json(HISTORY_PATH, [])
    target = next((h for h in history if str(h.get("order_id")) == order_id), None)
    if target is not None and target.get("refunded"):
        return
    if target is not None:
        target["refunded"] = True
        target["refund_ts"] = time.time()
        _save_json(HISTORY_PATH, history)
    _log(f"Заказ #{order_id}: обнаружен возврат/отмена ({getattr(status, 'name', status)}).")

    if cfg["settings"].get("auto_blacklist_on_refund"):
        bl = _load_blacklist()
        new_bl = _add_to_blacklist(bl, buyer_id, buyer, reason=f"refund order #{order_id}")
        if new_bl is not bl and len(new_bl) != len(bl):
            _save_blacklist(new_bl)
            _log(f"Покупатель {buyer} (#{buyer_id}) добавлен в ЧС (возврат заказа #{order_id}).")
            _notify_tg(cardinal,
                       f"🚫 <b>VIP Roblox</b>: {buyer} добавлен в чёрный список "
                       f"(возврат заказа #{order_id}).")


# ---------- BIND_TO_NEW_MESSAGE: команды в чате FunPay (!time, !ссылка, !vip, !отзыв) ----------
def _on_new_message(cardinal: "Cardinal", event) -> None:
    msg = event.message
    text = (msg.text or "").strip().lower()
    if not text or msg.author_id == 0 or msg.author_id == getattr(cardinal.account, "id", None):
        return
    if not text.startswith("!"):
        return
    cfg = _load_config()
    if not cfg["running"]:
        return
    s = cfg["settings"]
    buyer_id = getattr(msg, "author_id", None)

    # Смена языка работает всегда (даже без активной аренды) — Req 4.4/4.5
    new_lang = _resolve_lang_command(text)
    if new_lang:
        bl = _load_buyer_lang()
        bl[str(buyer_id)] = new_lang
        _save_buyer_lang(bl)
        _send_buyer(cardinal, buyer_id, msg.chat_id, "lang_switched")
        return

    active = _load_json(ACTIVE_PATH, [])
    rental = next((r for r in active if r.get("chat_id") == msg.chat_id), None)
    queue = _load_json(QUEUE_PATH, [])
    q_entry = next((e for e in queue if e.get("chat_id") == msg.chat_id), None)

    def _remaining_str(r: dict) -> str:
        rem = max(0, int(r["expires_at"] - time.time()))
        h, rest = divmod(rem, 3600)
        return f"{h}ч {rest // 60}мин"

    # !vip — статус аренды/очереди (Req 8)
    if text.startswith("!vip"):
        if rental:
            _send_buyer(cardinal, buyer_id, msg.chat_id, "vip_status_active",
                        server_id=rental.get("vip_server_id"),
                        remaining=_remaining_str(rental), link=rental.get("link"))
        elif q_entry:
            pos = _queue_position(queue, q_entry.get("order_id"), q_entry.get("lot_id"))
            _send_buyer(cardinal, buyer_id, msg.chat_id, "vip_status_queue", position=pos)
        else:
            _send_buyer(cardinal, buyer_id, msg.chat_id, "vip_status_none")
        return

    # !queue / !очередь — позиция в очереди (Req 1.6, 1.7)
    if text.startswith("!queue") or text.startswith("!очередь"):
        if q_entry:
            pos = _queue_position(queue, q_entry.get("order_id"), q_entry.get("lot_id"))
            _send_buyer(cardinal, buyer_id, msg.chat_id, "vip_status_queue", position=pos)
        elif rental:
            _send_buyer(cardinal, buyer_id, msg.chat_id, "vip_status_active",
                        server_id=rental.get("vip_server_id"),
                        remaining=_remaining_str(rental), link=rental.get("link"))
        else:
            _send_buyer(cardinal, buyer_id, msg.chat_id, "vip_status_none")
        return

    if text.startswith("!команды") or text.startswith("!help") or text.startswith("!commands"):
        _send_buyer(cardinal, buyer_id, msg.chat_id, "commands")
        return

    # Остальные команды требуют активной аренды
    if not rental:
        return

    if text.startswith("!time") or text.startswith("!время"):
        try:
            cardinal.send_message(msg.chat_id, f"⏱ Осталось: {_remaining_str(rental)}")
        except Exception:
            pass
    elif text.startswith("!ссылка") or text.startswith("!link"):
        try:
            cardinal.send_message(msg.chat_id, f"🔗 {rental['link']}")
        except Exception:
            pass
    elif text.startswith("!отзыв") or text.startswith("!review"):
        bonus = int(s.get("review_bonus_hours", 0) or 0)
        threshold = int(s.get("review_stars_threshold", 5) or 5)
        if bonus <= 0:
            try:
                cardinal.send_message(msg.chat_id, "Бонус за отзыв сейчас отключён.")
            except Exception:
                pass
        elif rental.get("review_bonused"):
            _send_buyer(cardinal, buyer_id, msg.chat_id, "review_already")
        else:
            review = _fetch_review(cardinal, rental["order_id"])
            if not review:
                _send_buyer(cardinal, buyer_id, msg.chat_id, "review_not_found")
            elif not _review_qualifies(review, threshold):
                _send_buyer(cardinal, buyer_id, msg.chat_id, "review_low_stars",
                            threshold=threshold)
            else:
                _grant_review_bonus(cardinal, rental, bonus, review)
                _save_json(ACTIVE_PATH, active)


# ---------- инициализация ----------
def _refresh_raise_skip_vr(cardinal: "Cardinal") -> None:
    """Собирает category_id для всех числовых lot_id из cfg["lots"] и
    регистрирует их в lot_activation_common для пропуска авто-поднятия.
    """
    lib = _common_lib()
    if lib is None or cardinal is None:
        return
    cfg = _load_config()
    cat_ids: set[int] = set()
    for lot in cfg.get("lots") or []:
        try:
            lot_id_int = int(lot.get("lot_id"))
        except (TypeError, ValueError):
            continue
        cid = lib.detect_category_id(cardinal, lot_id_int)
        if cid is not None:
            cat_ids.add(int(cid))
    lib.register_skip_categories("vip_roblox", cat_ids)
    if cat_ids:
        logger.info("vip_roblox: raise-skip категории: %s", sorted(cat_ids))


def _seed_templates_on_init() -> None:
    """Создать файлы шаблонов при первом запуске; перенести inline payment/expiration (Req 4.1, 9.4)."""
    ru = _DEFAULT_TEMPLATES_RU.copy()
    cfg = _load_config()
    s = cfg.get("settings", {})
    # одноразовый перенос кастомных inline-сообщений v1.1.1 в RU-шаблоны
    if not os.path.exists(TEMPLATES_RU_PATH):
        if s.get("payment_msg"):
            ru["payment"] = s["payment_msg"]
        if s.get("expiration_msg"):
            ru["expiration"] = s["expiration_msg"]
        _save_json(TEMPLATES_RU_PATH, ru)
    if not os.path.exists(TEMPLATES_EN_PATH):
        _save_json(TEMPLATES_EN_PATH, _DEFAULT_TEMPLATES_EN.copy())
    if not os.path.exists(BUYER_LANG_PATH):
        _save_json(BUYER_LANG_PATH, {})
    if not os.path.exists(BLACKLIST_PATH):
        _save_json(BLACKLIST_PATH, [])
    if not os.path.exists(LIVENESS_PATH):
        _save_json(LIVENESS_PATH, {"dead_servers": [], "dead_accounts": [], "checked_at": 0.0})


def _init(cardinal: "Cardinal", *_: Any) -> None:
    global _CARDINAL_REF_VR
    _CARDINAL_REF_VR = cardinal
    _ensure_dir()
    cfg = _load_config()
    _save_config(cfg)
    _seed_templates_on_init()

    # ── Общий патч raise_lots + первичный кэш категорий ──
    lib = _common_lib()
    if lib is not None:
        try:
            lib.install_raise_skip_patch(cardinal)
            threading.Thread(
                target=lambda: _refresh_raise_skip_vr(cardinal),
                daemon=True, name="vip_roblox-raise-skip").start()
        except Exception:
            logger.debug("vip_roblox: raise-skip setup failed",
                         exc_info=True)

    _stop_event.clear()
    threading.Thread(target=_rental_loop, args=(cardinal,), daemon=True, name="VipRobloxLoop").start()

    tg = getattr(cardinal, "telegram", None)
    if tg is None:
        _log("Telegram ПУ отключена — управление только через config.json.")
        return

    bot = tg.bot

    # ----- рендер -----
    def _status_text() -> str:
        c = _load_config()
        s = _load_json(STATS_PATH, DEFAULT_STATS)
        active = _load_json(ACTIVE_PATH, [])
        queue = _load_json(QUEUE_PATH, [])
        st = "🟢 запущен" if c["running"] else "🔴 остановлен"
        evm = "🟢 Включен" if c["event_mode"] else "🔴 Отключен"
        arc = "🟢" if c["settings"].get("auto_review_check", True) else "🔴"
        games_count = len(c.get("games") or [])
        return (
            f"<b>VipRoblox</b>\n"
            f"Автоматизация VIP-серверов Roblox.\n\n"
            f"📊 Статус: <b>{st}</b>\n"
            f"🎮 Event Mode: <b>{evm}</b>\n"
            f"👤 Roblox аккаунтов: <b>{len(c['accounts'])}</b>\n"
            f"🎮 Игр: <b>{games_count}</b>\n"
            f"🎟 Активных аренд: <b>{len(active)}</b>\n"
            f"🕒 В очереди: <b>{len(queue)}</b>\n"
            f"⭐ Бонус за отзыв: <b>+{c['settings'].get('review_bonus_hours', 0)}ч</b> "
            f"(авто-проверка {arc})\n"
            f"🆔 Game ID: <code>{c['settings'].get('game_id') or '—'}</code>\n\n"
            f"<i>Статистика</i>\n"
            f"  Часы всего: <b>{s.get('total_hours', 0)}</b>\n"
            f"  Заработок: <b>{s.get('earnings', 0)}</b>\n"
            f"  Заказы: <b>{s.get('orders', 0)}</b>"
        )

    def _kb_main() -> K:
        c = _load_config()
        kb = K()
        run_btn = B("⏹ Остановить" if c["running"] else "▶️ Запустить", callback_data=CBT_START)
        kb.add(run_btn, B("🔄 Перезапустить", callback_data=CBT_RESTART))
        evm_btn = B(
            "🎮 Event Mode: ВКЛ" if c["event_mode"] else "🎮 Event Mode: ВЫКЛ",
            callback_data=CBT_EVENT_MODE,
        )
        kb.add(evm_btn)
        kb.row(
            B("👤 Аккаунты", callback_data=CBT_TAB_ACCOUNTS),
            B("🕹 Игры", callback_data=CBT_TAB_GAMES),
        )
        kb.row(
            B("🎯 Лоты", callback_data=CBT_TAB_LOTS),
            B("🎟 Аренды", callback_data=CBT_TAB_RENTALS),
        )
        kb.row(
            B("🕒 Очередь", callback_data=CBT_TAB_QUEUE),
            B("⚙️ Настройки", callback_data=CBT_TAB_SETTINGS),
        )
        kb.row(
            B("🛠 Расширенные", callback_data=CBT_TAB_EXTRA),
            B("⚙️ v1.2", callback_data=CBT_TAB_V12),
        )
        return kb

    def _render(c: CallbackQuery, text: str, kb: K) -> None:
        try:
            bot.edit_message_text(text, c.message.chat.id, c.message.id, reply_markup=kb, parse_mode="HTML")
        except Exception:
            bot.send_message(c.message.chat.id, text, reply_markup=kb, parse_mode="HTML")

    def open_main(c: CallbackQuery) -> None:
        _render(c, _status_text(), _kb_main())
        bot.answer_callback_query(c.id)

    def toggle_running(c: CallbackQuery) -> None:
        cfg2 = _load_config()
        cfg2["running"] = not cfg2["running"]
        _save_config(cfg2)
        _log(f"Статус: {'запущен' if cfg2['running'] else 'остановлен'}")
        try:
            _update_lot_activation(cardinal)
        except Exception:
            logger.exception("vip_roblox: ошибка применения авто-деактивации")
        open_main(c)

    def restart(c: CallbackQuery) -> None:
        cfg2 = _load_config()
        cfg2["running"] = False
        _save_config(cfg2)
        time.sleep(0.5)
        cfg2["running"] = True
        _save_config(cfg2)
        _log("Перезапуск VipRoblox.")
        open_main(c)

    def toggle_event_mode(c: CallbackQuery) -> None:
        cfg2 = _load_config()
        cfg2["event_mode"] = not cfg2["event_mode"]
        _save_config(cfg2)
        _log(f"Event Mode: {'ВКЛ' if cfg2['event_mode'] else 'ВЫКЛ'}")
        open_main(c)

    # ----- аккаунты -----
    def open_accounts(c: CallbackQuery) -> None:
        cfg2 = _load_config()
        if not cfg2["accounts"]:
            text = "<b>Roblox аккаунты</b>\n\nПока ничего не добавлено."
        else:
            lines = ["<b>Roblox аккаунты</b>\n"]
            for i, a in enumerate(cfg2["accounts"]):
                lines.append(f"<code>{i + 1}</code>. <b>{a.get('username', '?')}</b> (ID {a.get('user_id', '?')})")
            text = "\n".join(lines)
        kb = K()
        for i, a in enumerate(cfg2["accounts"]):
            kb.row(
                B(f"🧪 {a.get('username', '?')}", callback_data=f"{CBT_TEST_ACCOUNT}:{i}"),
                B("🗑", callback_data=f"{CBT_DEL_ACCOUNT}:{i}"),
            )
        kb.add(B("➕ Вставить .ROBLOSECURITY", callback_data=CBT_ADD_ACCOUNT))
        kb.add(B("⬅️ Назад", callback_data=CBT_OPEN))
        _render(c, text, kb)
        bot.answer_callback_query(c.id)

    def ask_add_account(c: CallbackQuery) -> None:
        result = bot.send_message(
            c.message.chat.id,
            "Отправь куки <code>.ROBLOSECURITY</code> (только значение, одной строкой).",
            parse_mode="HTML",
        )
        tg.set_state(c.message.chat.id, result.id, c.from_user.id, STATE_AWAIT_ACCOUNT)
        bot.answer_callback_query(c.id)

    def on_account(m: Message) -> None:
        tg.clear_state(m.chat.id, m.from_user.id, True)
        cookie = (m.text or "").strip()
        if not cookie:
            bot.send_message(m.chat.id, "Пустое значение.")
            return
        info = roblox_validate(cookie)
        if not info:
            bot.send_message(m.chat.id, "❌ Кука невалидна или Roblox недоступен.")
            return
        cfg2 = _load_config()
        cfg2["accounts"].append({
            "cookie": cookie,
            "user_id": info["id"],
            "username": info["name"],
            "added_at": time.time(),
        })
        _save_config(cfg2)
        _log(f"Добавлен Roblox-аккаунт {info['name']} ({info['id']})")
        bot.send_message(m.chat.id, f"✅ Авторизован: <b>{info['name']}</b> (ID {info['id']})", parse_mode="HTML")

    def del_account(c: CallbackQuery) -> None:
        idx = int(c.data.split(":")[-1])
        cfg2 = _load_config()
        if 0 <= idx < len(cfg2["accounts"]):
            removed = cfg2["accounts"].pop(idx)
            _save_config(cfg2)
            _log(f"Удалён Roblox-аккаунт {removed.get('username')}")
        open_accounts(c)

    def test_account(c: CallbackQuery) -> None:
        idx = int(c.data.split(":")[-1])
        cfg2 = _load_config()
        if not (0 <= idx < len(cfg2["accounts"])):
            bot.answer_callback_query(c.id, "Аккаунт не найден")
            return
        acc = cfg2["accounts"][idx]
        bot.answer_callback_query(c.id, "Проверяю…")
        def _worker() -> None:
            info = roblox_validate(acc["cookie"])
            if info:
                bot.send_message(c.message.chat.id, f"✅ {info['name']} (ID {info['id']}) активен.")
            else:
                bot.send_message(c.message.chat.id, f"❌ Кука <b>{acc.get('username')}</b> невалидна.", parse_mode="HTML")
        threading.Thread(target=_worker, daemon=True).start()

    # ----- игры -----
    def open_games(c: CallbackQuery) -> None:
        cfg2 = _load_config()
        games = cfg2.get("games") or []
        if not games:
            text = "<b>🕹 Игры</b>\n\nНет добавленных игр."
        else:
            lines = ["<b>🕹 Игры</b>\n"]
            for i, g in enumerate(games):
                pool = g.get("vip_server_ids") or []
                lines.append(
                    f"<code>{i}</code>. <b>{g.get('game_name', '?')}</b> "
                    f"(ID: <code>{g.get('game_id', '?')}</code>) — "
                    f"VIP серверов: <b>{len(pool)}</b>"
                )
            text = "\n".join(lines)
        kb = K()
        for i, g in enumerate(games):
            kb.row(
                B(f"📋 {g.get('game_name', '?')}", callback_data=f"{CBT_GAME_DETAIL}:{i}"),
                B("🗑", callback_data=f"{CBT_DEL_GAME}:{i}"),
            )
        kb.add(B("➕ Добавить игру", callback_data=CBT_ADD_GAME))
        kb.add(B("⬅️ Назад", callback_data=CBT_OPEN))
        _render(c, text, kb)
        bot.answer_callback_query(c.id)

    def ask_add_game(c: CallbackQuery) -> None:
        result = bot.send_message(
            c.message.chat.id,
            "Отправь данные игры в формате:\n"
            "<code>game_id | название игры</code>\n\n"
            "Пример:\n"
            "<code>2753915549 | Murder Mystery 2</code>",
            parse_mode="HTML",
        )
        tg.set_state(c.message.chat.id, result.id, c.from_user.id, STATE_AWAIT_GAME)
        bot.answer_callback_query(c.id)

    def on_game(m: Message) -> None:
        tg.clear_state(m.chat.id, m.from_user.id, True)
        parts = [p.strip() for p in (m.text or "").split("|")]
        if len(parts) < 2:
            bot.send_message(m.chat.id, "Нужно 2 поля через | (game_id, название).")
            return
        game_id = parts[0].strip()
        game_name = parts[1].strip()
        if not game_id or not game_name:
            bot.send_message(m.chat.id, "game_id и название не могут быть пустыми.")
            return
        cfg2 = _load_config()
        games = cfg2.setdefault("games", [])
        games.append({
            "game_id": game_id,
            "game_name": game_name,
            "vip_server_ids": [],
        })
        _save_config(cfg2)
        idx = len(games) - 1
        _log(f"Добавлена игра #{idx}: {game_name} (ID {game_id})")
        bot.send_message(
            m.chat.id,
            f"✅ Игра добавлена: <b>{game_name}</b> (ID {game_id}), индекс: <code>{idx}</code>",
            parse_mode="HTML",
        )

    def open_game_detail(c: CallbackQuery) -> None:
        idx = int(c.data.split(":")[-1])
        cfg2 = _load_config()
        games = cfg2.get("games") or []
        if not (0 <= idx < len(games)):
            bot.answer_callback_query(c.id, "Игра не найдена")
            return
        g = games[idx]
        pool = g.get("vip_server_ids") or []
        lines = [
            f"<b>🕹 {g.get('game_name', '?')}</b>\n",
            f"🆔 Game ID: <code>{g.get('game_id', '?')}</code>",
            f"📊 Индекс: <code>{idx}</code>",
            f"🎮 VIP серверов: <b>{len(pool)}</b>",
        ]
        if pool:
            lines.append("\n<b>VIP Server IDs:</b>")
            for vid in pool:
                lines.append(f"  • <code>{vid}</code>")
        text = "\n".join(lines)
        kb = K()
        kb.add(B("➕ Добавить VIP серверы", callback_data=f"{CBT_ADD_GAME_VIPS}:{idx}"))
        for vi, vid in enumerate(pool):
            kb.add(B(f"🗑 {vid}", callback_data=f"{CBT_DEL_GAME_VIP}:{idx}:{vi}"))
        kb.add(B("🗑 Удалить игру", callback_data=f"{CBT_DEL_GAME}:{idx}"))
        kb.add(B("⬅️ К играм", callback_data=CBT_TAB_GAMES))
        _render(c, text, kb)
        bot.answer_callback_query(c.id)

    def ask_add_game_vips(c: CallbackQuery) -> None:
        idx = c.data.split(":")[-1]
        result = bot.send_message(
            c.message.chat.id,
            "Отправь VIP Server ID (через запятую, если несколько):\n\n"
            "Пример:\n"
            "<code>9876543210, 9876543211, 9876543212</code>\n\n"
            "ID можно найти в URL: <code>/private-server/configure/&lt;ID&gt;</code>",
            parse_mode="HTML",
        )
        tg.set_state(c.message.chat.id, result.id, c.from_user.id, STATE_AWAIT_GAME_VIPS, {"game_idx": int(idx)})
        bot.answer_callback_query(c.id)

    def on_game_vips(m: Message) -> None:
        state = tg.get_state(m.chat.id, m.from_user.id)
        if not state:
            return
        game_idx = state["data"]["game_idx"]
        tg.clear_state(m.chat.id, m.from_user.id, True)
        raw = (m.text or "").strip()
        vip_ids: list[int] = []
        for v in raw.replace(";", ",").split(","):
            v = v.strip()
            if not v:
                continue
            try:
                vip_ids.append(int(v))
            except ValueError:
                bot.send_message(m.chat.id, f"'{v}' не является числом.")
                return
        if not vip_ids:
            bot.send_message(m.chat.id, "Нужно указать хотя бы один VIP Server ID.")
            return
        cfg2 = _load_config()
        games = cfg2.get("games") or []
        if not (0 <= game_idx < len(games)):
            bot.send_message(m.chat.id, "Игра не найдена.")
            return
        existing = games[game_idx].setdefault("vip_server_ids", [])
        added = 0
        for vid in vip_ids:
            if vid not in existing:
                existing.append(vid)
                added += 1
        _save_config(cfg2)
        _log(f"Игра #{game_idx}: добавлено {added} VIP серверов.")
        bot.send_message(m.chat.id, f"✅ Добавлено: {added}. Всего VIP серверов: {len(existing)}.")

    def del_game_vip(c: CallbackQuery) -> None:
        parts = c.data.split(":")
        game_idx = int(parts[-2])
        vip_idx = int(parts[-1])
        cfg2 = _load_config()
        games = cfg2.get("games") or []
        if not (0 <= game_idx < len(games)):
            bot.answer_callback_query(c.id, "Игра не найдена")
            return
        pool = games[game_idx].get("vip_server_ids") or []
        if 0 <= vip_idx < len(pool):
            removed = pool.pop(vip_idx)
            games[game_idx]["vip_server_ids"] = pool
            _save_config(cfg2)
            _log(f"Игра #{game_idx}: удалён VIP сервер {removed}.")
        open_game_detail(c)

    def del_game(c: CallbackQuery) -> None:
        idx = int(c.data.split(":")[-1])
        cfg2 = _load_config()
        games = cfg2.get("games") or []
        if not (0 <= idx < len(games)):
            bot.answer_callback_query(c.id, "Игра не найдена")
            return
        # Проверяем, что ни один лот не ссылается на эту игру
        refs = [lot for lot in cfg2["lots"] if lot.get("game_idx") == idx]
        if refs:
            bot.answer_callback_query(
                c.id,
                f"Нельзя удалить: {len(refs)} лот(ов) ссылаются на эту игру.",
                show_alert=True,
            )
            return
        removed = games.pop(idx)
        # Обновляем game_idx в лотах, которые ссылаются на игры с большим индексом
        for lot in cfg2["lots"]:
            gi = lot.get("game_idx")
            if gi is not None and gi > idx:
                lot["game_idx"] = gi - 1
        _save_config(cfg2)
        _log(f"Удалена игра: {removed.get('game_name')} (ID {removed.get('game_id')})")
        open_games(c)

    # ----- настройки -----
    def open_settings(c: CallbackQuery) -> None:
        s = _load_config()["settings"]
        arc = "🟢 ВКЛ" if s.get("auto_review_check", True) else "🔴 ВЫКЛ"
        text = (
            "<b>Настройки</b>\n\n"
            f"🆔 Game ID: <code>{s.get('game_id') or '—'}</code>\n"
            f"⏱ Min/Max часов: <b>{s.get('min_hours')}</b> / <b>{s.get('max_hours')}</b>\n"
            f"⭐ Бонус за отзыв: <b>{s.get('review_bonus_hours')}ч</b>\n"
            f"🔍 Авто-проверка отзыва: <b>{arc}</b>\n"
            f"🎉 Скидка Event Mode: <b>{s.get('event_discount_pct')}%</b>\n"
            f"\n💬 Сообщение после оплаты:\n<pre>{s.get('payment_msg')[:1000]}</pre>"
            f"\n💬 Сообщение после истечения:\n<pre>{s.get('expiration_msg')[:600]}</pre>\n"
            "\nПеременные: {buyer_name} {server_id} {hours} {order_id} {link}"
        )
        kb = K()
        kb.add(
            B("🆔 Game ID", callback_data=f"{CBT_EDIT}:game_id"),
            B("⏱ Min часов", callback_data=f"{CBT_EDIT}:min_hours"),
            B("⏱ Max часов", callback_data=f"{CBT_EDIT}:max_hours"),
        )
        kb.add(
            B("⭐ Бонус отзыв", callback_data=f"{CBT_EDIT}:review_bonus_hours"),
            B(f"🔍 Авто-отзыв: {arc}", callback_data=f"{CBT_EDIT}:auto_review_check"),
        )
        kb.add(B("🎉 Event скидка %", callback_data=f"{CBT_EDIT}:event_discount_pct"))
        kb.add(B("✏️ Сообщение оплаты", callback_data=f"{CBT_EDIT}:payment_msg"))
        kb.add(B("✏️ Сообщение истечения", callback_data=f"{CBT_EDIT}:expiration_msg"))
        kb.add(B("⬅️ Назад", callback_data=CBT_OPEN))
        _render(c, text, kb)
        bot.answer_callback_query(c.id)

    def ask_edit(c: CallbackQuery) -> None:
        key = c.data.split(":")[-1]
        # булевы тумблеры — переключаем сразу
        bool_keys = {"auto_review_check", "blacklist_enabled", "auto_blacklist_on_refund",
                     "liveness_enabled", "operator_expiry_notify"}
        v12_keys = {"blacklist_enabled", "auto_blacklist_on_refund", "liveness_enabled",
                    "liveness_interval_sec", "operator_expiry_notify", "default_language",
                    "expiry_warning_offsets_min", "per_server_concurrent_limit",
                    "per_server_period_limit", "per_server_period_sec",
                    "per_buyer_concurrent_limit", "review_stars_threshold"}
        if key in bool_keys:
            cfg2 = _load_config()
            cfg2["settings"][key] = not cfg2["settings"].get(key, True if key == "auto_review_check" else False)
            _save_config(cfg2)
            _log(f"Настройка {key}: {'ВКЛ' if cfg2['settings'][key] else 'ВЫКЛ'}")
            (open_v12 if key in v12_keys else open_settings)(c)
            return
        if key == "default_language":
            cfg2 = _load_config()
            cur = cfg2["settings"].get("default_language", "ru")
            cfg2["settings"]["default_language"] = "en" if cur == "ru" else "ru"
            _save_config(cfg2)
            open_v12(c)
            return
        labels = {
            "game_id": "Game ID (число)",
            "min_hours": "минимум часов (целое)",
            "max_hours": "максимум часов (целое)",
            "review_bonus_hours": "часов бонуса за отзыв (целое)",
            "event_discount_pct": "скидку Event Mode в % (целое)",
            "payment_msg": "новый текст сообщения после оплаты",
            "expiration_msg": "новый текст сообщения после истечения",
            "liveness_interval_sec": "интервал проверки живости (сек, целое)",
            "per_server_concurrent_limit": "лимит одновременных аренд на сервер (0 = без лимита)",
            "per_server_period_limit": "лимит аренд на сервер за период (0 = без лимита)",
            "per_server_period_sec": "период для лимита сервера (сек, целое)",
            "per_buyer_concurrent_limit": "лимит одновременных аренд на покупателя (0 = без лимита)",
            "review_stars_threshold": "минимальная оценка отзыва для бонуса (1-5)",
            "expiry_warning_offsets_min": "офсеты предупреждений в минутах через запятую (напр. 30,10)",
        }
        result = bot.send_message(c.message.chat.id, f"Введи {labels.get(key, key)}:")
        tg.set_state(c.message.chat.id, result.id, c.from_user.id, STATE_AWAIT_EDIT, {"key": key})
        bot.answer_callback_query(c.id)

    def on_edit(m: Message) -> None:
        state = tg.get_state(m.chat.id, m.from_user.id)
        if not state:
            return
        key = state["data"]["key"]
        tg.clear_state(m.chat.id, m.from_user.id, True)
        value: Any = m.text or ""
        int_keys = ("min_hours", "max_hours", "review_bonus_hours", "event_discount_pct",
                    "liveness_interval_sec", "per_server_concurrent_limit",
                    "per_server_period_limit", "per_server_period_sec",
                    "per_buyer_concurrent_limit", "review_stars_threshold")
        if key in int_keys:
            try:
                value = int(value.strip())
            except ValueError:
                bot.send_message(m.chat.id, "Нужно целое число.")
                return
        elif key == "expiry_warning_offsets_min":
            offs: list[int] = []
            for x in (value or "").replace(";", ",").split(","):
                x = x.strip()
                if not x:
                    continue
                try:
                    offs.append(int(x))
                except ValueError:
                    bot.send_message(m.chat.id, f"'{x}' не число.")
                    return
            value = offs
        elif key == "game_id":
            value = value.strip()
        cfg2 = _load_config()
        cfg2["settings"][key] = value
        _save_config(cfg2)
        _log(f"Настройка {key} обновлена.")
        bot.send_message(m.chat.id, f"✅ {key} сохранено.")

    # ----- v1.2.0: доп. настройки -----
    def open_v12(c: CallbackQuery) -> None:
        s = _load_config()["settings"]

        def onoff(k, default=False):
            return "🟢 ВКЛ" if s.get(k, default) else "🔴 ВЫКЛ"

        offs = ", ".join(str(x) for x in (s.get("expiry_warning_offsets_min") or [])) or "—"
        text = (
            "<b>⚙️ Доп. настройки (v1.2)</b>\n\n"
            f"🌐 Язык по умолчанию: <b>{s.get('default_language', 'ru').upper()}</b>\n"
            f"🚫 Чёрный список: <b>{onoff('blacklist_enabled', True)}</b>\n"
            f"🚫 Авто-ЧС при возврате: <b>{onoff('auto_blacklist_on_refund')}</b>\n"
            f"❤️ Проверка живости: <b>{onoff('liveness_enabled', True)}</b> "
            f"(каждые {s.get('liveness_interval_sec')}с)\n"
            f"⏳ Предупреждения (мин): <b>{offs}</b>\n"
            f"📣 Дублировать оператору: <b>{onoff('operator_expiry_notify')}</b>\n"
            f"🖥 Лимит на сервер: одновр <b>{s.get('per_server_concurrent_limit')}</b>, "
            f"за период <b>{s.get('per_server_period_limit')}</b>/<b>{s.get('per_server_period_sec')}с</b>\n"
            f"👤 Лимит на покупателя: <b>{s.get('per_buyer_concurrent_limit')}</b>\n"
            f"⭐ Порог звёзд для бонуса: <b>{s.get('review_stars_threshold')}</b>\n"
            "\n<i>0 в лимитах = без ограничений</i>"
        )
        kb = K()
        kb.add(B(f"🌐 Язык по умолч.: {s.get('default_language', 'ru').upper()}",
                 callback_data=f"{CBT_EDIT}:default_language"))
        kb.add(B(f"🚫 Чёрный список: {onoff('blacklist_enabled', True)}",
                 callback_data=f"{CBT_EDIT}:blacklist_enabled"),
               B("📋 Список", callback_data=CBT_TAB_BLACKLIST))
        kb.add(B(f"🚫 Авто-ЧС при возврате: {onoff('auto_blacklist_on_refund')}",
                 callback_data=f"{CBT_EDIT}:auto_blacklist_on_refund"))
        kb.add(B(f"❤️ Проверка живости: {onoff('liveness_enabled', True)}",
                 callback_data=f"{CBT_EDIT}:liveness_enabled"),
               B("⏱ Интервал", callback_data=f"{CBT_EDIT}:liveness_interval_sec"))
        kb.add(B("⏳ Предупреждения (мин)", callback_data=f"{CBT_EDIT}:expiry_warning_offsets_min"),
               B(f"📣 Оператору: {onoff('operator_expiry_notify')}",
                 callback_data=f"{CBT_EDIT}:operator_expiry_notify"))
        kb.add(B("🖥 Лимит серв. (одновр)", callback_data=f"{CBT_EDIT}:per_server_concurrent_limit"),
               B("🖥 За период", callback_data=f"{CBT_EDIT}:per_server_period_limit"))
        kb.add(B("🖥 Период (сек)", callback_data=f"{CBT_EDIT}:per_server_period_sec"),
               B("👤 Лимит покупателя", callback_data=f"{CBT_EDIT}:per_buyer_concurrent_limit"))
        kb.add(B("⭐ Порог звёзд", callback_data=f"{CBT_EDIT}:review_stars_threshold"))
        kb.add(B("⬅️ Назад", callback_data=CBT_OPEN))
        _render(c, text, kb)
        bot.answer_callback_query(c.id)

    # ----- v1.2.0: чёрный список -----
    def open_blacklist(c: CallbackQuery) -> None:
        bl = _load_blacklist()
        if not bl:
            text = "<b>🚫 Чёрный список</b>\n\nПусто."
        else:
            lines = ["<b>🚫 Чёрный список</b>\n"]
            for i, e in enumerate(bl):
                who = e.get("username") or e.get("buyer_id") or "?"
                reason = e.get("reason") or ""
                lines.append(f"<code>{i + 1}</code>. <b>{who}</b> {('— ' + reason) if reason else ''}")
            text = "\n".join(lines)
        kb = K()
        for i, e in enumerate(bl):
            who = e.get("username") or e.get("buyer_id") or "?"
            kb.add(B(f"🗑 {who}", callback_data=f"{CBT_BL_DEL}:{i}"))
        kb.add(B("➕ Добавить", callback_data=CBT_BL_ADD))
        kb.add(B("⬅️ Назад", callback_data=CBT_TAB_V12))
        _render(c, text, kb)
        bot.answer_callback_query(c.id)

    def ask_bl_add(c: CallbackQuery) -> None:
        result = bot.send_message(
            c.message.chat.id,
            "Отправь <b>username</b> или <b>buyer_id</b> покупателя для ЧС "
            "(можно <code>username, 123</code>):",
            parse_mode="HTML")
        tg.set_state(c.message.chat.id, result.id, c.from_user.id, STATE_AWAIT_BL)
        bot.answer_callback_query(c.id)

    def on_bl_add(m: Message) -> None:
        tg.clear_state(m.chat.id, m.from_user.id, True)
        raw = (m.text or "").strip()
        if not raw:
            bot.send_message(m.chat.id, "Пусто.")
            return
        parts = [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
        username, buyer_id = "", None
        for p in parts:
            if p.isdigit():
                buyer_id = p
            else:
                username = p
        bl = _load_blacklist()
        new_bl = _add_to_blacklist(bl, buyer_id, username, reason="manual")
        _save_blacklist(new_bl)
        _log(f"ЧС: добавлен {username or buyer_id}")
        bot.send_message(m.chat.id, f"✅ Добавлен в ЧС: <b>{username or buyer_id}</b>", parse_mode="HTML")

    def del_bl(c: CallbackQuery) -> None:
        idx = int(c.data.split(":")[-1])
        bl = _load_blacklist()
        if 0 <= idx < len(bl):
            entry = bl[idx]
            key = entry.get("buyer_id") or entry.get("username")
            _save_blacklist(_remove_from_blacklist(bl, key))
            _log(f"ЧС: удалён {key}")
        open_blacklist(c)

    # ----- лоты -----
    def open_lots(c: CallbackQuery) -> None:
        cfg2 = _load_config()
        if not cfg2["lots"]:
            text = "<b>Привязка лотов</b>\n\nНет лотов."
        else:
            busy = _busy_vip_ids()
            games = cfg2.get("games") or []
            lines = ["<b>Привязка лотов</b>\n"]
            for i, lot in enumerate(cfg2["lots"]):
                game_idx = lot.get("game_idx")
                if game_idx is not None:
                    try:
                        idx = int(game_idx)
                    except (TypeError, ValueError):
                        idx = -1
                    if 0 <= idx < len(games):
                        game_name = games[idx].get("game_name") or games[idx].get("game_id") or "?"
                        pool = games[idx].get("vip_server_ids") or []
                    else:
                        game_name = f"[#{idx} не найдена]"
                        pool = []
                else:
                    game_name = None
                    pool = lot.get("vip_server_ids") or []
                free = sum(1 for v in pool if int(v) not in busy)
                game_info = f" игра: <b>{game_name}</b>" if game_name else ""
                hours_info = f"{lot.get('hours')}ч" if lot.get("hours") else "часы из #Hours/#Time"
                lines.append(
                    f"<code>{i + 1}</code>. lot <b>{', '.join(_lot_ids(lot))}</b> →"
                    f"{game_info} "
                    f"пул VIP: <b>{free}/{len(pool)}</b> свободно, "
                    f"{hours_info}\n"
                    f"    acc#{lot.get('account_idx', 0)} match: <i>{lot.get('lot_name_match', '')}</i>"
                )
            text = "\n".join(lines)
        text += (
            "\n\n<b>Добавить лот:</b> отправь строку\n"
            "<code>lot_id</code> — дальше бот спросит всё по шагам\n"
            "• несколько FunPay-лотов можно указать через запятую: <code>111,222,333</code>\n"
            "• <b>game_idx</b> — индекс игры (из раздела Игры, начиная с 0)\n"
            "• часы бот возьмёт из описания лота FunPay: <code>#Hours: 3</code> или <code>#Time: 3ч</code>\n"
            "• цена берётся из заказа FunPay автоматически\n"
            "• <b>account_idx</b> — индекс Roblox-аккаунта (по умолчанию 0)\n"
            "• match_name и account_idx можно оставить пустыми\n\n"
            "<i>Старые форматы тоже работают:</i>\n"
            "<code>lot_id | game_idx | price | match_name | account_idx</code>\n"
            "<code>lot_id | game_idx | hours | price | match_name | account_idx</code>\n"
            "<code>lot_id | vip_id1,vip_id2,... | hours | price | match_name | account_idx</code>"
        )
        kb = K()
        for i, _l in enumerate(cfg2["lots"]):
            kb.add(B(f"🗑 lot {', '.join(_lot_ids(_l))}", callback_data=f"{CBT_DEL_LOT}:{i}"))
        kb.add(B("➕ Добавить / обновить", callback_data=CBT_ADD_LOT))
        kb.add(B("⬅️ Назад", callback_data=CBT_OPEN))
        _render(c, text, kb)
        bot.answer_callback_query(c.id)

    def ask_add_lot(c: CallbackQuery) -> None:
        cfg2 = _load_config()
        games = cfg2.get("games") or []
        games_hint = ""
        if games:
            games_hint = "\n\n<b>Доступные игры:</b>\n"
            for i, g in enumerate(games):
                games_hint += f"  <code>{i}</code> — {g.get('game_name', '?')} ({g.get('game_id', '?')})\n"
        result = bot.send_message(
            c.message.chat.id,
            "➕ <b>Добавление лота — шаг 1/5</b>\n\n"
            "Отправь FunPay <b>lot_id</b>.\n"
            "Можно сразу несколько через запятую:\n"
            "<code>123456,123457,123458</code>\n\n"
            "Быстрый режим одной строкой тоже работает:\n"
            "<code>lot_id | game_idx | price | match_name | account_idx</code>\n"
            "Цена в мастере не нужна — бот возьмёт её из заказа FunPay."
            f"{games_hint}",
            parse_mode="HTML",
        )
        tg.set_state(c.message.chat.id, result.id, c.from_user.id, STATE_AWAIT_LOT, {"step": "lot_ids"})
        bot.answer_callback_query(c.id)

    def on_lot(m: Message) -> None:
        state = tg.get_state(m.chat.id, m.from_user.id) or {}
        data = dict(state.get("data") or {})
        text = (m.text or "").strip()
        cfg2 = _load_config()
        games = cfg2.get("games") or []

        def _is_number(value: str) -> bool:
            try:
                float(value)
                return True
            except ValueError:
                return False

        def _save_lot(payload: dict[str, Any]) -> None:
            lot_ids = payload["lot_ids"]
            lot_id = lot_ids[0]
            hours = int(payload.get("hours", 0) or 0)
            price = float(payload.get("price", 0) or 0)
            match_name = payload.get("match_name", "") or ""
            account_idx = int(payload.get("account_idx", 0) or 0)
            replaced = False
            for lot in cfg2["lots"]:
                if set(_lot_ids(lot)) & set(lot_ids):
                    if "vip_ids" in payload:
                        lot.update({
                            "lot_id": lot_id, "lot_ids": lot_ids,
                            "vip_server_ids": payload["vip_ids"], "hours": hours, "price": price,
                            "lot_name_match": match_name, "account_idx": account_idx,
                        })
                        lot.pop("game_idx", None)
                    else:
                        lot.update({
                            "lot_id": lot_id, "lot_ids": lot_ids,
                            "game_idx": int(payload["game_idx"]), "hours": hours, "price": price,
                            "lot_name_match": match_name, "account_idx": account_idx,
                        })
                        lot.pop("vip_server_ids", None)
                    replaced = True
                    break
            if not replaced:
                if "vip_ids" in payload:
                    cfg2["lots"].append({
                        "lot_id": lot_id, "lot_ids": lot_ids,
                        "vip_server_ids": payload["vip_ids"], "hours": hours, "price": price,
                        "lot_name_match": match_name, "account_idx": account_idx,
                    })
                else:
                    cfg2["lots"].append({
                        "lot_id": lot_id, "lot_ids": lot_ids,
                        "game_idx": int(payload["game_idx"]), "hours": hours, "price": price,
                        "lot_name_match": match_name, "account_idx": account_idx,
                    })
            _save_config(cfg2)
            if "vip_ids" in payload:
                target = f"пул {len(payload['vip_ids'])} VIP"
            else:
                game_idx_val = int(payload["game_idx"])
                target = f"игра #{game_idx_val} '{games[game_idx_val].get('game_name', '?')}'"
            _log(f"Лот {', '.join(lot_ids)} {'обновлён' if replaced else 'добавлен'} ({target}, acc#{account_idx}, {hours or 'часы из описания'}).")
            suffix = "Часы будут взяты из #Hours:/#Time: в описании лота." if not hours else f"{hours}ч."
            bot.send_message(m.chat.id, f"✅ Сохранено {len(lot_ids)} лот(а/ов). {target}, acc#{account_idx}. {suffix}")

        def _try_quick(raw: str) -> bool:
            if "|" not in raw:
                return False
            parts = [p.strip() for p in raw.split("|")]
            if len(parts) < 2:
                return False
            lot_ids = _parse_lot_ids(parts[0])
            if not lot_ids:
                bot.send_message(m.chat.id, "lot_id пустой.")
                return True
            if len(parts) == 2 and len(games) == 1 and _is_number(parts[1]):
                _save_lot({"lot_ids": lot_ids, "game_idx": 0, "price": float(parts[1])})
                tg.clear_state(m.chat.id, m.from_user.id, True)
                return True
            if len(parts) >= 3 and _is_number(parts[2]) and (len(parts) == 3 or not _is_number(parts[3])):
                try:
                    game_idx_val = int(parts[1])
                    if not (0 <= game_idx_val < len(games)):
                        raise ValueError
                    account_idx = int(parts[4]) if len(parts) >= 5 and parts[4] else 0
                except ValueError:
                    bot.send_message(m.chat.id, "game_idx/account_idx должен быть числом из списка.")
                    return True
                _save_lot({
                    "lot_ids": lot_ids, "game_idx": game_idx_val, "price": float(parts[2]),
                    "match_name": parts[3] if len(parts) >= 4 else "", "account_idx": account_idx,
                })
                tg.clear_state(m.chat.id, m.from_user.id, True)
                return True
            if len(parts) >= 4:
                try:
                    hours = int(parts[2])
                    price = float(parts[3])
                    account_idx = int(parts[5]) if len(parts) >= 6 and parts[5] else 0
                except ValueError:
                    bot.send_message(m.chat.id, "Неверные числа в старом формате.")
                    return True
                field2 = parts[1]
                if "," not in field2 and ";" not in field2:
                    try:
                        game_idx_val = int(field2)
                        if 0 <= game_idx_val < len(games):
                            _save_lot({
                                "lot_ids": lot_ids, "game_idx": game_idx_val, "hours": hours,
                                "price": price, "match_name": parts[4] if len(parts) >= 5 else "",
                                "account_idx": account_idx,
                            })
                            tg.clear_state(m.chat.id, m.from_user.id, True)
                            return True
                    except ValueError:
                        pass
                vip_ids: list[int] = []
                for value in field2.replace(";", ",").split(","):
                    value = value.strip()
                    if value:
                        try:
                            vip_ids.append(int(value))
                        except ValueError:
                            bot.send_message(m.chat.id, f"'{value}' не похож на vip_server_id.")
                            return True
                if not vip_ids:
                    bot.send_message(m.chat.id, "Нужен game_idx или список vip_server_id.")
                    return True
                _save_lot({
                    "lot_ids": lot_ids, "vip_ids": vip_ids, "hours": hours, "price": price,
                    "match_name": parts[4] if len(parts) >= 5 else "", "account_idx": account_idx,
                })
                tg.clear_state(m.chat.id, m.from_user.id, True)
                return True
            bot.send_message(m.chat.id, "Формат не распознан. Лучше пройди мастер: отправь только lot_id без |.")
            return True

        if _try_quick(text):
            return

        step = data.get("step", "lot_ids")
        if step == "lot_ids":
            lot_ids = _parse_lot_ids(text)
            if not lot_ids:
                bot.send_message(m.chat.id, "❌ Не вижу lot_id. Пришли число или несколько через запятую: 123,456")
                return
            data = {"step": "game", "lot_ids": lot_ids}
            if len(games) == 1:
                data["game_idx"] = 0
                data["step"] = "match"
                result = bot.send_message(
                    m.chat.id,
                    f"✅ Лоты: <code>{', '.join(lot_ids)}</code>\n"
                    f"Игра выбрана автоматически: <b>{games[0].get('game_name', '?')}</b>.\n\n"
                    "Шаг 2/4: отправь match_name (подстрока названия лота) или <code>-</code>, чтобы пропустить.\n"
                    "Цена будет взята из заказа FunPay автоматически.",
                    parse_mode="HTML",
                )
            else:
                games_hint = "\n".join(
                    f"<code>{i}</code> — {g.get('game_name', '?')} ({g.get('game_id', '?')})"
                    for i, g in enumerate(games)
                ) or "Сначала добавь игру в разделе Игры."
                result = bot.send_message(
                    m.chat.id,
                    f"✅ Лоты: <code>{', '.join(lot_ids)}</code>\n\n"
                    f"Шаг 2/4: выбери индекс игры:\n{games_hint}",
                    parse_mode="HTML",
                )
            tg.set_state(m.chat.id, result.id, m.from_user.id, STATE_AWAIT_LOT, data)
            return

        if step == "game":
            try:
                game_idx_val = int(text)
            except ValueError:
                bot.send_message(m.chat.id, "❌ Пришли индекс игры числом, например 0.")
                return
            if not (0 <= game_idx_val < len(games)):
                bot.send_message(m.chat.id, "❌ Нет такой игры. Пришли индекс из списка выше.")
                return
            data["game_idx"] = game_idx_val
            data["step"] = "match"
            result = bot.send_message(
                m.chat.id,
                f"✅ Игра: <b>{games[game_idx_val].get('game_name', '?')}</b>\n\n"
                "Шаг 3/4: отправь match_name (подстрока названия лота) или <code>-</code>, чтобы пропустить.\n"
                "Цена будет взята из заказа FunPay автоматически.",
                parse_mode="HTML",
            )
            tg.set_state(m.chat.id, result.id, m.from_user.id, STATE_AWAIT_LOT, data)
            return

        if step == "price":
            data.pop("price", None)
            data["step"] = "match"
            result = bot.send_message(
                m.chat.id,
                "Цена больше не нужна — бот берёт её из заказа FunPay автоматически.\n\n"
                "Отправь match_name (подстрока названия лота) или <code>-</code>, чтобы пропустить.",
                parse_mode="HTML",
            )
            tg.set_state(m.chat.id, result.id, m.from_user.id, STATE_AWAIT_LOT, data)
            return

        if step == "match":
            data["match_name"] = "" if text in ("-", "нет", "Нет") else text
            accounts = cfg2.get("accounts") or []
            if len(accounts) <= 1:
                data["account_idx"] = 0
                _save_lot(data)
                tg.clear_state(m.chat.id, m.from_user.id, True)
                return
            data["step"] = "account"
            accounts_hint = "\n".join(
                f"<code>{i}</code> — {acc.get('username') or acc.get('user_id') or 'Roblox account'}"
                for i, acc in enumerate(accounts)
            )
            result = bot.send_message(
                m.chat.id,
                f"Шаг 4/4: отправь индекс Roblox-аккаунта или <code>-</code> для 0.\n\n{accounts_hint}",
                parse_mode="HTML",
            )
            tg.set_state(m.chat.id, result.id, m.from_user.id, STATE_AWAIT_LOT, data)
            return

        if step == "account":
            try:
                data["account_idx"] = 0 if text in ("", "-", "нет", "Нет") else int(text)
            except ValueError:
                bot.send_message(m.chat.id, "❌ account_idx должен быть числом. Обычно это 0.")
                return
            accounts = cfg2.get("accounts") or []
            if accounts and not (0 <= int(data["account_idx"]) < len(accounts)):
                accounts_hint = "\n".join(
                    f"<code>{i}</code> — {acc.get('username') or acc.get('user_id') or 'Roblox account'}"
                    for i, acc in enumerate(accounts)
                )
                bot.send_message(
                    m.chat.id,
                    f"❌ Нет аккаунта с индексом {data['account_idx']}. Выбери из списка:\n{accounts_hint}",
                    parse_mode="HTML",
                )
                return
            _save_lot(data)
            tg.clear_state(m.chat.id, m.from_user.id, True)

    def del_lot(c: CallbackQuery) -> None:
        idx = int(c.data.split(":")[-1])
        cfg2 = _load_config()
        if 0 <= idx < len(cfg2["lots"]):
            removed = cfg2["lots"].pop(idx)
            _save_config(cfg2)
            _log(f"Лот {removed.get('lot_id')} удалён.")
        try:
            _update_lot_activation(cardinal)
        except Exception:
            logger.exception("vip_roblox: ошибка применения авто-деактивации")
        open_lots(c)

    # ----- аренды -----
    def open_rentals(c: CallbackQuery) -> None:
        active = _load_json(ACTIVE_PATH, [])
        if not active:
            text = "<b>Активные аренды</b>\n\nНет активных аренд."
        else:
            lines = ["<b>Активные аренды</b>\n"]
            now = time.time()
            for r in active:
                remaining = max(0, int(r["expires_at"] - now))
                h, rem = divmod(remaining, 3600)
                mi = rem // 60
                lines.append(
                    f"#{r['order_id']} <b>{r['buyer']}</b> — VIP {r.get('vip_server_id', '?')} "
                    f"(acc#{r.get('account_idx', 0)}) — {h}ч {mi}мин"
                )
            text = "\n".join(lines)
        kb = K()
        for r in active:
            kb.add(B(f"❌ Завершить #{r['order_id']}", callback_data=f"{CBT_END_RENTAL}:{r['order_id']}"))
        kb.add(B("⬅️ Назад", callback_data=CBT_OPEN))
        _render(c, text, kb)
        bot.answer_callback_query(c.id)

    def end_rental(c: CallbackQuery) -> None:
        order_id = c.data.split(":")[-1]
        active = _load_json(ACTIVE_PATH, [])
        active = [r for r in active if r["order_id"] != order_id]
        _save_json(ACTIVE_PATH, active)
        _log(f"Аренда #{order_id} принудительно завершена.")
        try:
            _update_lot_activation(cardinal)
        except Exception:
            logger.exception("vip_roblox: ошибка применения авто-деактивации")
        open_rentals(c)

    # ----- очередь -----
    def open_queue(c: CallbackQuery) -> None:
        q = _load_json(QUEUE_PATH, [])
        if not q:
            text = "<b>Очередь заказов</b>\n\nОчередь пуста."
        else:
            lines = ["<b>Очередь заказов</b>\n"]
            # позиции считаем по лотам
            per_lot: dict[str, int] = {}
            for entry in q:
                lid = str(entry.get("lot_id"))
                per_lot[lid] = per_lot.get(lid, 0) + 1
                pos = per_lot[lid]
                waited = max(0, int(time.time() - entry.get("queued_at", time.time())))
                m, s = divmod(waited, 60)
                lines.append(
                    f"#{entry['order_id']} <b>{entry['buyer']}</b> — lot {lid} "
                    f"(поз. {pos}) — ждёт {m}м {s}с"
                )
            text = "\n".join(lines)
        kb = K()
        for entry in q[:10]:
            kb.add(B(
                f"🗑 Убрать #{entry['order_id']}",
                callback_data=f"{CBT_DEL_QUEUE}:{entry['order_id']}",
            ))
        kb.add(B("⬅️ Назад", callback_data=CBT_OPEN))
        _render(c, text, kb)
        bot.answer_callback_query(c.id)

    def del_queue(c: CallbackQuery) -> None:
        order_id = c.data.split(":")[-1]
        q = _load_json(QUEUE_PATH, [])
        q = [x for x in q if str(x.get("order_id")) != order_id]
        _save_json(QUEUE_PATH, q)
        _log(f"Заказ #{order_id} убран из очереди.")
        open_queue(c)

    # ----- история -----
    def open_history(c: CallbackQuery) -> None:
        h = _load_json(HISTORY_PATH, [])
        if not h:
            text = "<b>Журнал заказов</b>\n\nИстория пуста."
        else:
            lines = ["<b>Журнал заказов</b> (последние 20)\n"]
            for e in reversed(h):
                ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(e.get("ts", 0)))
                lines.append(
                    f"{ts} — #{e['order_id']} <b>{e['buyer']}</b> "
                    f"lot {e.get('lot_id')} — {e.get('hours')}ч, {e.get('price')}"
                )
            text = "\n".join(lines)
        kb = K().add(B("⬅️ Назад", callback_data=CBT_OPEN))
        _render(c, text, kb)
        bot.answer_callback_query(c.id)

    # ----- расширенные -----
    def open_extra(c: CallbackQuery) -> None:
        s = _load_config()["settings"]
        rfd = "🟢 Включено" if s.get("auto_refund") else "🔴 Выключено"
        adl = "🟢 Включено" if s.get("auto_deactivate_lots", True) else "🔴 Выключено"
        text = (
            "<b>Расширенные параметры</b>\n\n"
            f"🔄 Auto refund: <b>{rfd}</b>\n"
            f"🔁 Авто-деактивация лотов: <b>{adl}</b>\n"
            f"⏱ Проверка лотов (сек): <b>{s.get('check_interval_sec')}</b>\n"
            f"🔔 Чатов уведомлений: <b>{len(s.get('notify_chats', []))}</b>"
        )
        if s.get("notify_chats"):
            text += "\n" + "\n".join(f"  • <code>{cid}</code>" for cid in s["notify_chats"])
        kb = K()
        kb.add(B(f"Auto refund: {rfd}", callback_data=CBT_TOGGLE_REFUND))
        kb.add(B(f"🔁 Авто-деактивация: {adl}", callback_data=CBT_TOGGLE_AUTODEACT))
        kb.add(B(f"⏱ Интервал ({s.get('check_interval_sec')}s)", callback_data=CBT_EDIT_INTERVAL))
        kb.add(B("➕ Чат уведомлений", callback_data=CBT_ADD_NOTIF))
        for i, cid in enumerate(s.get("notify_chats", [])):
            kb.add(B(f"🗑 {cid}", callback_data=f"{CBT_DEL_NOTIF}:{i}"))
        kb.add(B("📜 История", callback_data=CBT_TAB_HISTORY))
        kb.add(B("📋 Логи", callback_data=CBT_TAB_LOGS))
        kb.add(B("⬅️ Назад", callback_data=CBT_OPEN))
        _render(c, text, kb)
        bot.answer_callback_query(c.id)

    def toggle_refund(c: CallbackQuery) -> None:
        cfg2 = _load_config()
        cfg2["settings"]["auto_refund"] = not cfg2["settings"].get("auto_refund", False)
        _save_config(cfg2)
        open_extra(c)

    def toggle_autodeact(c: CallbackQuery) -> None:
        cfg2 = _load_config()
        new_val = not cfg2["settings"].get("auto_deactivate_lots", True)
        cfg2["settings"]["auto_deactivate_lots"] = new_val
        _save_config(cfg2)
        _log(f"Авто-деактивация лотов: {'ВКЛ' if new_val else 'ВЫКЛ'}")
        try:
            _update_lot_activation(cardinal)
        except Exception:
            logger.exception("vip_roblox: ошибка применения авто-деактивации")
        open_extra(c)

    def ask_interval(c: CallbackQuery) -> None:
        result = bot.send_message(c.message.chat.id, "Интервал проверки лотов (секунды, мин. 30):")
        tg.set_state(c.message.chat.id, result.id, c.from_user.id, STATE_AWAIT_INTERVAL)
        bot.answer_callback_query(c.id)

    def on_interval(m: Message) -> None:
        tg.clear_state(m.chat.id, m.from_user.id, True)
        try:
            val = int((m.text or "").strip())
            if val < 30:
                raise ValueError
        except ValueError:
            bot.send_message(m.chat.id, "Нужно число >= 30.")
            return
        cfg2 = _load_config()
        cfg2["settings"]["check_interval_sec"] = val
        _save_config(cfg2)
        bot.send_message(m.chat.id, f"✅ Интервал = {val} сек")

    def ask_notif(c: CallbackQuery) -> None:
        result = bot.send_message(c.message.chat.id, "Введи Telegram Chat ID для уведомлений:")
        tg.set_state(c.message.chat.id, result.id, c.from_user.id, STATE_AWAIT_NOTIF)
        bot.answer_callback_query(c.id)

    def on_notif(m: Message) -> None:
        tg.clear_state(m.chat.id, m.from_user.id, True)
        cid = (m.text or "").strip()
        try:
            int(cid)
        except ValueError:
            bot.send_message(m.chat.id, "Chat ID должен быть числом.")
            return
        cfg2 = _load_config()
        if cid not in cfg2["settings"]["notify_chats"]:
            cfg2["settings"]["notify_chats"].append(cid)
            _save_config(cfg2)
        bot.send_message(m.chat.id, f"✅ Чат {cid} добавлен.")

    def del_notif(c: CallbackQuery) -> None:
        idx = int(c.data.split(":")[-1])
        cfg2 = _load_config()
        chats = cfg2["settings"].get("notify_chats", [])
        if 0 <= idx < len(chats):
            chats.pop(idx)
            cfg2["settings"]["notify_chats"] = chats
            _save_config(cfg2)
        open_extra(c)

    # ----- логи -----
    def open_logs(c: CallbackQuery) -> None:
        text = "<b>Логи</b>\n<pre>" + _read_logs()[-3500:] + "</pre>"
        kb = K()
        kb.add(B("🧹 Очистить", callback_data=CBT_CLEAR_LOGS))
        kb.add(B("⬅️ Назад", callback_data=CBT_OPEN))
        _render(c, text, kb)
        bot.answer_callback_query(c.id)

    def clear_logs(c: CallbackQuery) -> None:
        if os.path.exists(LOG_PATH):
            os.remove(LOG_PATH)
        open_logs(c)

    # ----- регистрация -----
    def _cb(prefix: str):
        return lambda c: c.data == prefix or c.data.startswith(prefix + ":")

    tg.cbq_handler(open_main, _cb(CBT_OPEN))

    def _open_from_plugin_card(c: CallbackQuery) -> None:
        """Открытие из карточки плагина FPC — отправляем новое сообщение."""
        try:
            bot.send_message(c.message.chat.id, _status_text(), reply_markup=_kb_main(), parse_mode="HTML")
        except Exception:
            pass
        bot.answer_callback_query(c.id)

    tg.cbq_handler(_open_from_plugin_card, lambda c: c.data.startswith(f"47:{UUID}"))

    tg.cbq_handler(toggle_running, _cb(CBT_START))
    tg.cbq_handler(restart, _cb(CBT_RESTART))
    tg.cbq_handler(toggle_event_mode, _cb(CBT_EVENT_MODE))

    tg.cbq_handler(open_accounts, _cb(CBT_TAB_ACCOUNTS))
    tg.cbq_handler(ask_add_account, _cb(CBT_ADD_ACCOUNT))
    tg.cbq_handler(del_account, _cb(CBT_DEL_ACCOUNT))
    tg.cbq_handler(test_account, _cb(CBT_TEST_ACCOUNT))

    tg.cbq_handler(open_games, _cb(CBT_TAB_GAMES))
    tg.cbq_handler(ask_add_game, _cb(CBT_ADD_GAME))
    tg.cbq_handler(open_game_detail, _cb(CBT_GAME_DETAIL))
    tg.cbq_handler(ask_add_game_vips, _cb(CBT_ADD_GAME_VIPS))
    tg.cbq_handler(del_game_vip, _cb(CBT_DEL_GAME_VIP))
    tg.cbq_handler(del_game, _cb(CBT_DEL_GAME))

    tg.cbq_handler(open_settings, _cb(CBT_TAB_SETTINGS))
    tg.cbq_handler(ask_edit, _cb(CBT_EDIT))

    tg.cbq_handler(open_lots, _cb(CBT_TAB_LOTS))
    tg.cbq_handler(ask_add_lot, _cb(CBT_ADD_LOT))
    tg.cbq_handler(del_lot, _cb(CBT_DEL_LOT))

    tg.cbq_handler(open_rentals, _cb(CBT_TAB_RENTALS))
    tg.cbq_handler(end_rental, _cb(CBT_END_RENTAL))

    tg.cbq_handler(open_queue, _cb(CBT_TAB_QUEUE))
    tg.cbq_handler(del_queue, _cb(CBT_DEL_QUEUE))

    tg.cbq_handler(open_history, _cb(CBT_TAB_HISTORY))

    tg.cbq_handler(open_extra, _cb(CBT_TAB_EXTRA))
    tg.cbq_handler(toggle_refund, _cb(CBT_TOGGLE_REFUND))
    tg.cbq_handler(toggle_autodeact, _cb(CBT_TOGGLE_AUTODEACT))
    tg.cbq_handler(ask_interval, _cb(CBT_EDIT_INTERVAL))
    tg.cbq_handler(ask_notif, _cb(CBT_ADD_NOTIF))
    tg.cbq_handler(del_notif, _cb(CBT_DEL_NOTIF))

    tg.cbq_handler(open_logs, _cb(CBT_TAB_LOGS))
    tg.cbq_handler(clear_logs, _cb(CBT_CLEAR_LOGS))

    # v1.2.0
    tg.cbq_handler(open_v12, _cb(CBT_TAB_V12))
    tg.cbq_handler(open_blacklist, _cb(CBT_TAB_BLACKLIST))
    tg.cbq_handler(ask_bl_add, _cb(CBT_BL_ADD))
    tg.cbq_handler(del_bl, _cb(CBT_BL_DEL))

    tg.msg_handler(on_account, func=lambda m: (tg.get_state(m.chat.id, m.from_user.id) or {}).get("state") == STATE_AWAIT_ACCOUNT)
    tg.msg_handler(on_edit, func=lambda m: (tg.get_state(m.chat.id, m.from_user.id) or {}).get("state") == STATE_AWAIT_EDIT)
    tg.msg_handler(on_lot, func=lambda m: (tg.get_state(m.chat.id, m.from_user.id) or {}).get("state") == STATE_AWAIT_LOT)
    tg.msg_handler(on_interval, func=lambda m: (tg.get_state(m.chat.id, m.from_user.id) or {}).get("state") == STATE_AWAIT_INTERVAL)
    tg.msg_handler(on_notif, func=lambda m: (tg.get_state(m.chat.id, m.from_user.id) or {}).get("state") == STATE_AWAIT_NOTIF)
    tg.msg_handler(on_game, func=lambda m: (tg.get_state(m.chat.id, m.from_user.id) or {}).get("state") == STATE_AWAIT_GAME)
    tg.msg_handler(on_game_vips, func=lambda m: (tg.get_state(m.chat.id, m.from_user.id) or {}).get("state") == STATE_AWAIT_GAME_VIPS)
    tg.msg_handler(on_bl_add, func=lambda m: (tg.get_state(m.chat.id, m.from_user.id) or {}).get("state") == STATE_AWAIT_BL)

    def cmd_open(m: Message) -> None:
        bot.send_message(m.chat.id, _status_text(), reply_markup=_kb_main(), parse_mode="HTML")

    tg.msg_handler(cmd_open, commands=["vip_roblox"])

    # /vrx_guide — гайд
    def cmd_guide(m: Message) -> None:
        guide_text = (
            "<b>📖 VipRoblox — Гайд</b>\n\n"
            "<b>Что делает:</b>\n"
            "Автоматизация аренды VIP-серверов Roblox через FunPay. "
            "Бот регенерирует share-link при каждом заказе и убивает "
            "старую ссылку по окончании аренды.\n\n"
            "<b>Настройка:</b>\n"
            "1. /vip_roblox → 👤 Аккаунты → ➕ Добавить\n"
            "2. Введите .ROBLOSECURITY cookie от Roblox-аккаунта\n"
            "3. Создайте лот: /vip_roblox → 🎯 Лоты → ➕ Добавить\n"
            "4. Укажите один или несколько FunPay lot_id, игру и Roblox-аккаунт\n"
            "5. Нажмите ▶️ Запустить\n\n"
            "<b>Как указать часы аренды:</b>\n"
            "Часы пишутся НЕ в Telegram, а в описании товара на FunPay.\n"
            "Открой лот FunPay → редактировать описание → добавь отдельной строкой тег:\n"
            "<code>#Hours: 3</code> — аренда на 3 часа\n"
            "<code>#Hours: 24</code> — аренда на 24 часа\n\n"
            "Можно использовать <code>#Time:</code> с суффиксами:\n"
            "<code>#Time: 30m</code> — 30 минут\n"
            "<code>#Time: 2h</code> или <code>#Time: 2ч</code> — 2 часа\n"
            "<code>#Time: 1d</code> — 1 день\n"
            "<code>#Time: 1w</code> — 1 неделя\n\n"
            "Если к одному VIP-пулу привязано несколько FunPay-лотов, у каждого лота на FunPay должен быть свой тег времени в описании.\n"
            "Если тега нет, бот не выдаст VIP и напишет оператору, что надо добавить <code>#Hours:</code> или <code>#Time:</code>.\n\n"
            "<b>Как работает:</b>\n"
            "• Покупатель оплачивает лот\n"
            "• Бот регенерирует share-link (старый мёртв)\n"
            "• Покупатель получает персональную ссылку\n"
            "• Таймер аренды запускается\n"
            "• По окончании — ссылка снова обновляется\n\n"
            "<b>Команды покупателя:</b>\n"
            "• !time — оставшееся время\n"
            "• !ссылка / !link — показать ссылку ещё раз\n"
            "• !vip — статус аренды (сервер, время, ссылка)\n"
            "• !очередь / !queue — позиция в очереди\n"
            "• !отзыв / !review — бонус за отзыв\n"
            "• !engrent / !rusrent — переключить язык (EN/RU)\n\n"
            "<b>Telegram:</b>\n"
            "/vip_roblox — главное меню\n"
            "/vrx_guide — этот гайд\n"
            "/vrx_test — тест Roblox API\n\n"
            "<b>Особенности (v1.2):</b>\n"
            "• Пул VIP-серверов на лот\n"
            "• FIFO-очередь с уведомлениями о позиции\n"
            "• Упреждающие уведомления об истечении\n"
            "• Проверка живости аккаунтов и серверов\n"
            "• RU/EN локализация сообщений покупателю\n"
            "• Чёрный список + авто-ЧС при возврате\n"
            "• Лимиты аренд на сервер и на покупателя\n"
            "• Бонус за отзыв (порог звёзд, 1 раз на заказ)\n"
            "• Event Mode, авто-возврат, уведомления в Telegram"
        )
        try:
            bot.send_message(m.chat.id, guide_text, parse_mode="HTML")
        except Exception as e:
            try:
                bot.send_message(
                    m.chat.id,
                    f"⚠ Не удалось отправить гайд: <code>{e}</code>",
                    parse_mode="HTML")
            except Exception:
                logger.error("vip_roblox: cmd_guide failed", exc_info=True)

    tg.msg_handler(cmd_guide, commands=["vrx_guide"])

    # /vrx_test — тест Roblox cookie
    def cmd_test(m: Message) -> None:
        cfg2 = _load_config()
        accounts = cfg2.get("accounts", [])
        if not accounts:
            bot.send_message(
                m.chat.id,
                "❌ <b>Тест невозможен:</b> нет Roblox-аккаунтов.\n"
                "Добавьте через /vip_roblox → 👤 Аккаунты.",
                parse_mode="HTML",
            )
            return

        acc = accounts[0]
        cookie = acc.get("cookie", "")
        bot.send_message(m.chat.id, "🔄 Тестирую Roblox cookie...")

        import threading as _thr

        def _worker() -> None:
            result = roblox_validate(cookie)
            if result:
                bot.send_message(
                    m.chat.id,
                    f"✅ <b>Тест пройден!</b>\n\n"
                    f"👤 Username: <code>{result.get('name', '?')}</code>\n"
                    f"🆔 User ID: <code>{result.get('id', '?')}</code>\n\n"
                    f"Roblox cookie валиден, плагин готов!",
                    parse_mode="HTML",
                )
            else:
                bot.send_message(
                    m.chat.id,
                    "❌ <b>Тест не пройден!</b>\n\n"
                    "Cookie невалиден или истёк.\n\n"
                    "Проверьте:\n"
                    "• Cookie .ROBLOSECURITY актуален\n"
                    "• Аккаунт не заблокирован\n"
                    "• Roblox API доступен",
                    parse_mode="HTML",
                )

        _thr.Thread(target=_worker, daemon=True).start()

    tg.msg_handler(cmd_test, commands=["vrx_test"])

    try:
        cardinal.add_telegram_commands(UUID, [
            ("vip_roblox", "VipRoblox: меню управления", True),
            ("vrx_guide", "VipRoblox: гайд", True),
            ("vrx_test", "VipRoblox: тест cookie", True),
        ])
    except Exception:
        logger.exception("Не удалось зарегистрировать команду /vip_roblox")

    _log("VipRoblox инициализирован.")
    try:
        _update_lot_activation(cardinal)
    except Exception:
        logger.exception("vip_roblox: ошибка стартовой синхронизации лотов")


def _open_settings_page(cardinal: "Cardinal", msg) -> None:
    """FPC settings page handler - directs user to /vip_roblox."""
    tg = getattr(cardinal, "telegram", None)
    if tg is None:
        return
    tg.bot.send_message(
        msg.chat.id,
        "<b>VipRoblox</b>\n\n"
        "Для настройки используйте команду /vip_roblox\n"
        "Для гайда: /vrx_guide",
        parse_mode="HTML",
    )


BIND_TO_SETTINGS_PAGE = _open_settings_page
BIND_TO_PRE_INIT = [_init]
BIND_TO_NEW_ORDER = [_on_new_order]
BIND_TO_NEW_MESSAGE = [_on_new_message]
BIND_TO_ORDER_STATUS_CHANGED = [_on_order_status_changed]


def _on_delete(cardinal: "Cardinal", *_: Any) -> None:
    _stop_event.set()
    _log("VipRoblox удалён.")


BIND_TO_DELETE = _on_delete



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
