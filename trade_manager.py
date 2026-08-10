"""
Trade Manager — плагин для FunPay Cardinal.

Авто-управление лотами по расписанию:
внутри заданного временного окна лоты аккаунта выключаются,
вне окна — автоматически включаются.

Управление через Telegram ПУ FPC (inline-меню).
Положите файл в папку plugins/ FunPay Cardinal и перезапустите бота.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING, Any

from telebot.types import CallbackQuery, InlineKeyboardButton as B, InlineKeyboardMarkup as K, Message

if TYPE_CHECKING:
    from cardinal import Cardinal

# ---------- мета ----------
NAME = "TradeManager"
VERSION = "1.3.0"
DESCRIPTION = (
    "Управление лотами по временным окнам. Внутри окна лоты выключаются, вне — "
    "включаются. Пикер лотов кнопками, имена лотов, дни недели, пресеты, разовые "
    "оверрайды и уведомления о переключениях."
)
CREDITS = "@drakelovc"
UUID = "45222663-9966-453d-ae26-7cc21c03d597"
SETTINGS_PAGE = True

logger = logging.getLogger("FPC.plugin.trade_manager")

PLUGIN_DIR = os.path.join("storage", "plugins", "trade_manager")
CONFIG_PATH = os.path.join(PLUGIN_DIR, "config.json")
LOG_PATH = os.path.join(PLUGIN_DIR, "log.txt")
MAX_LOG_LINES = 200

CBT_PREFIX = "TMG"
CBT_OPEN = f"{CBT_PREFIX}:O"
CBT_TAB_RULES = f"{CBT_PREFIX}:T:R"
CBT_TAB_SETTINGS = f"{CBT_PREFIX}:T:S"
CBT_TAB_LOGS = f"{CBT_PREFIX}:T:L"
CBT_ADD_RULE = f"{CBT_PREFIX}:R:ADD"
CBT_DEL_RULE = f"{CBT_PREFIX}:R:DEL"
CBT_TOGGLE_RULE = f"{CBT_PREFIX}:R:TGL"
CBT_TEST_RULE = f"{CBT_PREFIX}:R:TST"      # +":<idx>"
CBT_EDIT_EXCLUDE = f"{CBT_PREFIX}:R:EXC"   # +":<idx>"
CBT_EDIT_INCLUDE = f"{CBT_PREFIX}:R:INC"   # +":<idx>"
CBT_EDIT_INTERVAL = f"{CBT_PREFIX}:S:INT"
CBT_TOGGLE_LOG_OK = f"{CBT_PREFIX}:S:LOG"
CBT_EXPORT = f"{CBT_PREFIX}:S:EXP"
CBT_IMPORT = f"{CBT_PREFIX}:S:IMP"
CBT_CLEAR_LOGS = f"{CBT_PREFIX}:L:CLR"

STATE_AWAIT_RULE = f"{CBT_PREFIX}:S_RULE"
STATE_AWAIT_INTERVAL = f"{CBT_PREFIX}:S_INT"
STATE_AWAIT_EXCLUDE = f"{CBT_PREFIX}:S_EXC"
STATE_AWAIT_INCLUDE = f"{CBT_PREFIX}:S_INC"
STATE_AWAIT_IMPORT = f"{CBT_PREFIX}:S_IMP"
STATE_AWAIT_TIMEZONE = f"{CBT_PREFIX}:S_TZ"

CBT_TOGGLE_ENABLED = f"{CBT_PREFIX}:S:TOG"
CBT_CHANGE_TZ = f"{CBT_PREFIX}:S:TZ"

# ---------- v1.3.0: пикер, дни недели, пресеты, оверрайды, уведомления ----------
# Пикер лотов (пагинированный чекбокс). Callback_data несёт только опкод + малые int.
CBT_PICK_TOGGLE = f"{CBT_PREFIX}:P:T"    # +":<page>:<idx>"
CBT_PICK_PAGE = f"{CBT_PREFIX}:P:G"      # +":<page>"
CBT_PICK_OK = f"{CBT_PREFIX}:P:OK"
CBT_PICK_CANCEL = f"{CBT_PREFIX}:P:X"
CBT_PICK_NOP = f"{CBT_PREFIX}:P:NOP"
STATE_PICKER = f"{CBT_PREFIX}:S_PICK"

# Дни недели
CBT_TOGGLE_WD = f"{CBT_PREFIX}:R:WD"     # +":<rule_idx>:<day0..6>"
CBT_EDIT_WEEKDAYS = f"{CBT_PREFIX}:R:WDE"  # +":<rule_idx>" — открыть редактор дней
CBT_RULE_CARD = f"{CBT_PREFIX}:R:CARD"     # +":<rule_idx>" — карточка правила
CBT_PICK_INC = f"{CBT_PREFIX}:R:PINC"      # +":<rule_idx>" — пикер include
CBT_PICK_EXC = f"{CBT_PREFIX}:R:PEXC"      # +":<rule_idx>" — пикер exclude

# Пресеты лотов
CBT_TAB_PRESETS = f"{CBT_PREFIX}:T:P"
CBT_PRESET_ADD = f"{CBT_PREFIX}:PR:ADD"
CBT_PRESET_DEL = f"{CBT_PREFIX}:PR:DEL"  # +":<name_idx>"
CBT_PRESET_EDIT = f"{CBT_PREFIX}:PR:ED"  # +":<name_idx>"
CBT_RULE_PRESET = f"{CBT_PREFIX}:R:PR"   # +":<rule_idx>" — назначить/снять пресет на правиле
STATE_AWAIT_PRESET_NAME = f"{CBT_PREFIX}:S_PRNAME"

# Разовые оверрайды
CBT_TAB_OVERRIDES = f"{CBT_PREFIX}:T:O"
CBT_OVR_FORCE_OFF = f"{CBT_PREFIX}:OV:FO"
CBT_OVR_SKIP_RULE = f"{CBT_PREFIX}:OV:SK"  # +":<rule_idx>"
CBT_OVR_CANCEL = f"{CBT_PREFIX}:OV:X"      # +":<ovr_idx>"

# Уведомления
CBT_TOGGLE_NOTIFY = f"{CBT_PREFIX}:S:NTF"
NOTIFY_MAX_LOTS = 20

_WD_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]  # index == datetime.weekday() (Пн=0)

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    # rules: [{id, name, start, end, utc_offset, active, note,
    #          exclude_lot_ids: [int], include_lot_ids: [int],
    #          weekdays: [int 0..6], preset: str|None}]
    # include_lot_ids задан → правило трогает ТОЛЬКО эти лоты;
    # пусто → все лоты, кроме exclude_lot_ids.
    # weekdays пусто → каждый день; preset → имя пресета или None.
    "rules": [],
    # presets: {name: [lot_id, ...]} — именованные наборы лотов (v1.3.0).
    "presets": {},
    # overrides: разовые правила (v1.3.0):
    #   {"kind": "force_off", "lot_ids": [int], "until_ts": float, "label": str}
    #   {"kind": "skip_rule", "rule_id": str, "until_ts": float, "label": str}
    "overrides": [],
    # lot_name_cache: зеркало кэша имён лотов {"<id>": "name"} (v1.3.0, опционально).
    "lot_name_cache": {},
    "settings": {
        "interval_sec": 60,
        "log_successes": True,
        "timezone": "UTC+3",
        # v1.3.0:
        "notifications_enabled": True,
        "lot_name_cache_ttl_sec": 300,
        "picker_page_size": 8,
        "override_morning_time": "08:00",
        "operator_chat_id": None,
    },
}

TIMEZONE_PRESETS: dict[str, int] = {
    "МСК": 3, "MSK": 3,
    "Киев": 2, "Kyiv": 2, "Київ": 2,
    "UTC": 0, "UTC+1": 1, "UTC+2": 2, "UTC+3": 3, "UTC+4": 4, "UTC+5": 5,
    "UTC-1": -1, "UTC-2": -2, "UTC-3": -3,
}


def _ensure_dir() -> None:
    os.makedirs(PLUGIN_DIR, exist_ok=True)


def _new_rule_id() -> str:
    """Короткий стабильный id правила (для ссылок из overrides)."""
    return uuid.uuid4().hex[:8]


def _migrate_rule(r: dict[str, Any]) -> dict[str, Any]:
    """Дополняет правило недостающими полями (аддитивно, идемпотентно)."""
    r.setdefault("exclude_lot_ids", [])
    r.setdefault("include_lot_ids", [])
    r.setdefault("weekdays", [])        # [] = каждый день; int 0..6 (Пн=0)
    r.setdefault("preset", None)        # имя пресета или None
    if not r.get("id"):
        r["id"] = _new_rule_id()
    return r


def _migrate_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Аддитивная идемпотентная миграция конфига к схеме v1.3.0."""
    cfg.setdefault("enabled", True)
    cfg.setdefault("rules", [])
    cfg.setdefault("presets", {})
    cfg.setdefault("overrides", [])
    cfg.setdefault("lot_name_cache", {})
    s = cfg.setdefault("settings", {})
    for k, v in DEFAULT_CONFIG["settings"].items():
        s.setdefault(k, v)
    for r in cfg["rules"]:
        _migrate_rule(r)
    return cfg


def _load_config() -> dict[str, Any]:
    _ensure_dir()
    if not os.path.exists(CONFIG_PATH):
        return _migrate_config(json.loads(json.dumps(DEFAULT_CONFIG)))
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return _migrate_config(json.loads(json.dumps(DEFAULT_CONFIG)))
    return _migrate_config(cfg)


def _save_config(cfg: dict[str, Any]) -> None:
    _ensure_dir()
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


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
        data = f.read().strip()
    return data or "Логи отсутствуют"


# ---------- логика окон ----------
def _parse_hhmm(s: str):
    return datetime.strptime(s, "%H:%M").time()


def _effective_offset(rule: dict[str, Any], global_offset: int) -> int:
    """Смещение правила: своё utc_offset, иначе глобальный пояс (Req 8.5)."""
    v = rule.get("utc_offset")
    if v is None:
        return int(global_offset)
    try:
        return int(v)
    except (TypeError, ValueError):
        return int(global_offset)


def _time_in_window(start, end, now_t) -> bool:
    """Попадает ли время now_t в окно start→end (с переносом через полночь)."""
    if start <= end:
        return start <= now_t < end
    return now_t >= start or now_t < end


def _window_anchor_weekday(start, end, now_dt: datetime) -> int:
    """День недели (0..6) дня НАЧАЛА окна для текущего момента.

    Для окна через полночь утренний сегмент (now < end) принадлежит
    предыдущему календарному дню (Req 3.5).
    """
    now_t = now_dt.time()
    if start <= end:
        return now_dt.weekday()
    if now_t >= start:           # вечерний сегмент — окно стартовало сегодня
        return now_dt.weekday()
    return (now_dt - timedelta(days=1)).weekday()  # утренний сегмент


def _rule_window_active(rule: dict[str, Any], now_dt: datetime) -> bool:
    """True, если окно правила активно: время-в-окне И день недели подходит.

    now_dt должен быть в эффективном поясе правила. Пустой/отсутствующий
    weekdays → правило действует все 7 дней (Req 3.3–3.6).
    """
    try:
        start = _parse_hhmm(rule["start"])
        end = _parse_hhmm(rule["end"])
    except Exception:
        return False
    if not _time_in_window(start, end, now_dt.time()):
        return False
    weekdays = rule.get("weekdays") or []
    if not weekdays:
        return True
    anchor = _window_anchor_weekday(start, end, now_dt)
    try:
        return anchor in {int(d) for d in weekdays}
    except (TypeError, ValueError):
        return True


def _in_window(rule: dict[str, Any], default_offset: int = 3) -> bool:
    """True, если текущее время попадает в окно (с учётом дней недели)."""
    offset = _effective_offset(rule, default_offset)
    tz = timezone(timedelta(hours=offset))
    now_dt = datetime.now(tz)
    return _rule_window_active(rule, now_dt)


# ---------- v1.3.0: кэш имён лотов ----------
_lot_name_cache: dict[int, str] = {}   # id -> name, живёт всё время процесса
_lot_name_cache_ts: float = 0.0        # epoch последнего обновления


def _lot_name_from_shortcut(lot_shortcut: Any) -> str:
    """Имя лота из shortcut профиля: description → title → name → str(id)."""
    for attr in ("description", "title", "name"):
        v = getattr(lot_shortcut, attr, None)
        if v:
            return str(v)
    return str(getattr(lot_shortcut, "id", ""))


def _refresh_lot_names(lots: list, ttl_sec: int, *, now: float, force: bool = False) -> bool:
    """Перестроить кэш имён из профиля, если истёк TTL (Req 2.4–2.6, 2.8).

    Возвращает True, если кэш был перестроен; False — если переиспользован.
    """
    global _lot_name_cache_ts
    if not force and (now - _lot_name_cache_ts) < ttl_sec:
        return False
    new_cache: dict[int, str] = {}
    for ls in lots:
        try:
            lid = int(ls.id)
        except (TypeError, ValueError, AttributeError):
            continue
        new_cache[lid] = _lot_name_from_shortcut(ls)
    _lot_name_cache.clear()
    _lot_name_cache.update(new_cache)
    _lot_name_cache_ts = now
    return True


def _fallback_name(lid: int) -> str:
    return f"Лот #{lid} (имя неизвестно)"


def _lot_display_name(lid: int) -> str:
    """Имя лота из кэша, иначе русский placeholder с numeric id (Req 2.7)."""
    try:
        name = _lot_name_cache.get(int(lid))
    except (TypeError, ValueError):
        name = None
    return name if name else _fallback_name(lid)


def _lots_inline(lot_ids) -> str:
    """Список лотов «Имя (#id)» для рендера в меню/тесте (Req 2.1, 2.2)."""
    return ", ".join(f"{_lot_display_name(l)} (#{l})" for l in lot_ids)


# ---------- v1.3.0: разрешение целей правила ----------
def _resolve_rule_targets(rule: dict[str, Any], presets: dict[str, list],
                          all_lot_ids) -> set[int]:
    """Эффективное множество лотов правила (Req 8, порядок разрешения).

    (preset ∪ include) если непусто, иначе все лоты; затем − exclude; ∩ лоты аккаунта.
    Отсутствующий пресет → пустой вклад.
    """
    all_set = {int(x) for x in all_lot_ids}
    preset_name = rule.get("preset")
    preset_ids: set[int] = set()
    if preset_name and preset_name in (presets or {}):
        preset_ids = {int(x) for x in (presets.get(preset_name) or [])}
    include = {int(x) for x in (rule.get("include_lot_ids") or [])}
    exclude = {int(x) for x in (rule.get("exclude_lot_ids") or [])}
    effective_include = preset_ids | include
    base = effective_include if effective_include else all_set
    return (base - exclude) & all_set


# ---------- v1.3.0: оверрайды ----------
def _morning_target_ts(now_dt: datetime, morning_hhmm: str) -> float:
    """Epoch ближайшего наступления утреннего времени в поясе now_dt."""
    try:
        t = datetime.strptime(morning_hhmm, "%H:%M").time()
    except Exception:
        t = datetime.strptime("08:00", "%H:%M").time()
    target = now_dt.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
    if now_dt >= target:
        target = target + timedelta(days=1)
    return target.timestamp()


def _end_of_day_ts(now_dt: datetime) -> float:
    """Epoch 23:59:59 текущего дня в поясе now_dt."""
    return now_dt.replace(hour=23, minute=59, second=59, microsecond=0).timestamp()


def _expire_overrides(overrides: list, now_ts: float) -> tuple[list, bool]:
    """Оставляет только неистёкшие (until_ts > now); changed=True если что-то удалено."""
    kept = [o for o in overrides if float(o.get("until_ts", 0)) > now_ts]
    return kept, len(kept) != len(overrides)


def _force_off_lot_ids(overrides: list, now_ts: float) -> set[int]:
    """Объединение lot_ids активных force_off оверрайдов."""
    out: set[int] = set()
    for o in overrides:
        if o.get("kind") != "force_off":
            continue
        if float(o.get("until_ts", 0)) <= now_ts:
            continue
        for v in o.get("lot_ids") or []:
            try:
                out.add(int(v))
            except (TypeError, ValueError):
                pass
    return out


def _skipped_rule_ids(overrides: list, now_ts: float) -> set[str]:
    """rule_id с активным skip_rule оверрайдом."""
    out: set[str] = set()
    for o in overrides:
        if o.get("kind") != "skip_rule":
            continue
        if float(o.get("until_ts", 0)) <= now_ts:
            continue
        rid = o.get("rule_id")
        if rid:
            out.add(str(rid))
    return out


def _rule_active_now(rule: dict[str, Any], now_dt: datetime,
                     skipped_rule_ids: set[str]) -> bool:
    """Активно ли правило: окно активно И нет активного skip_rule (Req 5.4)."""
    if str(rule.get("id")) in skipped_rule_ids:
        return False
    return _rule_window_active(rule, now_dt)


def _decide_lot_state(lid: int, in_window_targets: set[int],
                      force_off_ids: set[int]) -> bool:
    """True, если лот должен быть ВЫКЛЮЧЕН в этом цикле (Req 8.3, 8.6)."""
    lid = int(lid)
    return lid in force_off_ids or lid in in_window_targets


# ---------- v1.3.0: пагинация и пикер ----------
def _paginate(order: list, page_size: int) -> list[list]:
    """Разбить order на страницы по page_size, сохраняя порядок (Req 1.2)."""
    ps = max(int(page_size), 1)
    return [order[i:i + ps] for i in range(0, len(order), ps)]


def _picker_apply_toggle(selected: list, lid) -> list:
    """Переключить членство lid в selected (Req 1.3, 3.2; своя инверсия)."""
    lid = int(lid)
    out = [x for x in selected if int(x) != lid]
    if len(out) == len(selected):
        out.append(lid)
    return out


def _pick_cb_toggle(page: int, idx: int) -> str:
    return f"{CBT_PICK_TOGGLE}:{page}:{idx}"


def _pick_cb_page(page: int) -> str:
    return f"{CBT_PICK_PAGE}:{page}"


def _pick_keyboard(order: list, selected: list, page: int, page_size: int) -> "K":
    """Клавиатура пикера: ✅/⬜ + имя на лот, навигация, подтвердить/отмена."""
    pages = _paginate(order, page_size)
    total = len(pages) or 1
    page = max(0, min(int(page), total - 1))
    sel_set = {int(x) for x in selected}
    kb = K()
    cur = pages[page] if pages else []
    for j, lid in enumerate(cur):
        mark = "✅" if int(lid) in sel_set else "⬜"
        label = f"{mark} {_lot_display_name(lid)}"
        kb.add(B(label[:40], callback_data=_pick_cb_toggle(page, j)))
    nav: list = []
    if page > 0:
        nav.append(B("◀️", callback_data=_pick_cb_page(page - 1)))
    nav.append(B(f"{page + 1}/{total}", callback_data=CBT_PICK_NOP))
    if page < total - 1:
        nav.append(B("▶️", callback_data=_pick_cb_page(page + 1)))
    kb.row(*nav)
    kb.row(B("✅ Подтвердить", callback_data=CBT_PICK_OK),
           B("❌ Отмена", callback_data=CBT_PICK_CANCEL))
    return kb


def _find_rule_by_id(cfg: dict, rid) -> "dict | None":
    for r in cfg.get("rules", []):
        if str(r.get("id")) == str(rid):
            return r
    return None


def _weekdays_ru(weekdays) -> str:
    """Список дней недели по-русски: 'Пн, Вт, …' или 'Каждый день' (Req 3.7)."""
    days = sorted({int(d) for d in (weekdays or []) if 0 <= int(d) <= 6})
    if not days or len(days) == 7:
        return "Каждый день"
    return ", ".join(_WD_RU[d] for d in days)


# ---------- v1.3.0: уведомления ----------
def _should_notify(changed: int, notifications_enabled: bool) -> bool:
    return bool(notifications_enabled) and changed > 0


def _build_notification(changes_by_rule: dict, max_lots: int) -> str:
    """Русское резюме об изменённых лотах с усечением (Req 6.1, 6.4, 6.7)."""
    total = sum(len(v) for v in changes_by_rule.values())
    lines = [f"🔔 <b>TradeManager</b>: изменено лотов: <b>{total}</b>"]
    shown = 0
    done = False
    for label, changes in changes_by_rule.items():
        if done:
            break
        lines.append(f"\n<b>{label}</b>:")
        for lid, turned_off in changes:
            if shown >= max_lots:
                done = True
                break
            mark = "⛔ выкл" if turned_off else "✅ вкл"
            lines.append(f"  {mark}: {_lot_display_name(lid)} (#{lid})")
            shown += 1
    remainder = total - shown
    if remainder > 0:
        lines.append(f"\n… и ещё {remainder} лот(ов)")
    return "\n".join(lines)



# ── Общая либа: actions.log (raise-skip не нужен — trade_manager управляет
# ВСЕМИ лотами по расписанию, у него нет «своих» категорий) ─────────────
# ── Встроенная либа actions.log ─────────────────────────────────────────────
_ACTIONS_ICONS_TM = {
    "lot_activated":   "✅ ЛОТ ВКЛ ",
    "lot_deactivated": "⛔ ЛОТ ВЫКЛ",
    "lot_save_failed": "⚠ ЛОТ FAIL",
    "rule_applied":    "📐 ПРАВИЛО ",
}


def _make_actions_logger_tm(plugin_name: str, storage_dir: str):
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


def _do_log_action_tm(lg, action: str, summary: str = "", **extra) -> None:
    if lg is None:
        return
    icon = _ACTIONS_ICONS_TM.get(action, f"• {action:10}")
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


def _common_lib_tm():
    try:
        import lot_activation_common  # type: ignore
        return lot_activation_common
    except Exception:
        pass

    class _Shim:
        @staticmethod
        def make_actions_logger(pname, sdir):
            return _make_actions_logger_tm(pname, sdir)

        @staticmethod
        def log_action(lg, action, summary="", **extra):
            _do_log_action_tm(lg, action, summary, **extra)

    return _Shim()


_actions_logger_tm: "logging.Logger | None" = None


def _get_actions_logger_tm():
    global _actions_logger_tm
    if _actions_logger_tm is not None:
        return _actions_logger_tm
    lib = _common_lib_tm()
    if lib is None:
        return None
    _actions_logger_tm = lib.make_actions_logger("trade_manager", PLUGIN_DIR)
    return _actions_logger_tm


def _log_action_tm(action: str, summary: str = "", **extra) -> None:
    lib = _common_lib_tm()
    if lib is None:
        return
    lib.log_action(_get_actions_logger_tm(), action, summary, **extra)


def _excluded_for_active_rules(rules: list[dict], default_offset: int = 3) -> set[int]:
    """Объединяет exclude_lot_ids всех активных (в окне) правил."""
    out: set[int] = set()
    for r in rules:
        if not _in_window(r, default_offset):
            continue
        for v in r.get("exclude_lot_ids") or []:
            try:
                out.add(int(v))
            except (TypeError, ValueError):
                pass
    return out


def _rule_targets_lot(rule: dict[str, Any], lot_id: int) -> bool:
    """Трогает ли правило этот лот?

    • include_lot_ids задан  → правило управляет ТОЛЬКО этими лотами.
    • include_lot_ids пуст   → правило управляет всеми лотами, кроме
      указанных в exclude_lot_ids (старое поведение).
    """
    try:
        lid = int(lot_id)
    except (TypeError, ValueError):
        return False
    inc = rule.get("include_lot_ids") or []
    if inc:
        try:
            return lid in {int(x) for x in inc}
        except (TypeError, ValueError):
            return False
    exc = rule.get("exclude_lot_ids") or []
    try:
        return lid not in {int(x) for x in exc}
    except (TypeError, ValueError):
        return True


def _global_offset_from_settings(settings: dict) -> int:
    """Глобальное смещение из настроек (пресет или 'UTC±N')."""
    tz_name = settings.get("timezone", "UTC+3")
    off = TIMEZONE_PRESETS.get(tz_name)
    if off is None:
        try:
            off = int(str(tz_name).replace("UTC", "").replace("+", ""))
        except (ValueError, AttributeError):
            off = 3
    return off


def _warm_name_cache_from_mirror(cfg: dict) -> None:
    """Прогреть кэш имён из зеркала config.json (после рестарта, до первого fetch)."""
    if _lot_name_cache:
        return
    mirror = cfg.get("lot_name_cache") or {}
    for k, v in mirror.items():
        try:
            _lot_name_cache[int(k)] = v
        except (TypeError, ValueError):
            pass


def _resolve_operator_chat_id(cardinal: "Cardinal", settings: dict):
    """chat_id оператора: из настроек, иначе первый authorized_user FPC."""
    cid = settings.get("operator_chat_id")
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


_notif_no_chat_logged = False


def _send_notification(cardinal: "Cardinal", settings: dict, changes_by_rule: dict) -> None:
    """Отправить уведомление оператору (Req 6.4, 6.6 — устойчиво к сбоям)."""
    global _notif_no_chat_logged
    tg = getattr(cardinal, "telegram", None)
    bot = getattr(tg, "bot", None) if tg else None
    if bot is None:
        return
    chat_id = _resolve_operator_chat_id(cardinal, settings)
    if not chat_id:
        if not _notif_no_chat_logged:
            _log("Уведомление пропущено: не определён chat_id оператора.")
            _notif_no_chat_logged = True
        return
    text = _build_notification(changes_by_rule, NOTIFY_MAX_LOTS)
    try:
        bot.send_message(chat_id, text, parse_mode="HTML")
    except Exception as ex:
        _log(f"Ошибка отправки уведомления: {type(ex).__name__}: {str(ex)[:120]}")


def _apply_rules(cardinal: "Cardinal") -> None:
    """Один цикл проверки правил. Выключаем или включаем лоты."""
    cfg = _load_config()
    if not cfg.get("enabled", True):
        return

    settings = cfg["settings"]
    now_ts = time.time()

    # 1) Истечение оверрайдов (персист, если что-то удалили).
    overrides = cfg.get("overrides") or []
    kept, ovr_changed = _expire_overrides(overrides, now_ts)
    if ovr_changed:
        cfg["overrides"] = kept
        _save_config(cfg)
    overrides = kept

    active_rules = [r for r in cfg.get("rules", []) if r.get("active", True)]
    force_off_ids = _force_off_lot_ids(overrides, now_ts)
    if not active_rules and not force_off_ids:
        return

    global_offset = _global_offset_from_settings(settings)

    account = cardinal.account
    if not account or not getattr(account, "is_initiated", False):
        return

    lots = cardinal.profile.get_lots() if cardinal.profile else []
    if not lots and not force_off_ids:
        return

    # 2) Кэш имён: прогрев из зеркала + TTL-обновление (пиггибэк на текущий fetch).
    _warm_name_cache_from_mirror(cfg)
    ttl = int(settings.get("lot_name_cache_ttl_sec", 300))
    if _refresh_lot_names(lots, ttl, now=now_ts):
        cfg["lot_name_cache"] = {str(k): v for k, v in _lot_name_cache.items()}
        _save_config(cfg)

    all_ids = [int(ls.id) for ls in lots]
    lot_by_id = {int(ls.id): ls for ls in lots}
    presets = cfg.get("presets") or {}
    skipped = _skipped_rule_ids(overrides, now_ts)

    # 3) Разрешаем цели правил, отделяем «в окне».
    managed: set[int] = set()
    in_window_targets: set[int] = set()
    rule_for_lot: dict[int, str] = {}
    missing_presets_logged: set[str] = set()
    for r in active_rules:
        offset = _effective_offset(r, global_offset)
        now_dt = datetime.now(timezone(timedelta(hours=offset)))
        pn = r.get("preset")
        if pn and pn not in presets and pn not in missing_presets_logged:
            _log(f"Правило '{r.get('name')}': пресет '{pn}' не найден")
            missing_presets_logged.add(pn)
        targets = _resolve_rule_targets(r, presets, all_ids)
        managed |= targets
        if _rule_active_now(r, now_dt, skipped):
            in_window_targets |= targets
            for lid in targets:
                rule_for_lot[lid] = r.get("name") or "Правило"

    # 4) Решение и применение по каждому управляемому лоту (+ force-off).
    changed = 0
    changes_by_rule: dict[str, list[tuple[int, bool]]] = {}

    def _track(label: str, lid: int, turned_off: bool) -> None:
        changes_by_rule.setdefault(label, []).append((lid, turned_off))

    for lid in sorted(managed | force_off_ids):
        lot_shortcut = lot_by_id.get(lid)
        if lot_shortcut is None:
            continue  # лота больше нет на аккаунте (Req 8.4)
        should_off = _decide_lot_state(lid, in_window_targets, force_off_ids)
        try:
            lot_fields = account.get_lot_fields(lot_shortcut.id)
        except Exception as ex:
            _log(f"Ошибка получения лота {lid}: {ex}")
            continue
        current_active = lot_fields.active
        name = _lot_display_name(lid)
        if should_off and current_active:
            lot_fields.active = False
            try:
                lot_fields.renew_fields()
                account.save_lot(lot_fields)
                changed += 1
                _log(f"Лот {name} (#{lid}) ВЫКЛЮЧЕН")
                _log_action_tm("lot_deactivated",
                               f"Лот {name} (#{lid}) выключен",
                               lot_id=lid, reason="window_active")
                label = (rule_for_lot.get(lid, "Правило")
                         if lid in in_window_targets else "🎯 Разовый оверрайд")
                _track(label, lid, True)
            except Exception as ex:
                _log(f"Ошибка выключения лота {lid}: {ex}")
                _log_action_tm("lot_save_failed",
                               f"Ошибка выключения лота {lid}",
                               lot_id=lid,
                               error=f"{type(ex).__name__}: {str(ex)[:120]}")
        elif not should_off and not current_active:
            # Защита от amount==0 (LotFields форсит active=False).
            if (getattr(lot_fields, "amount", None) in (None, 0)
                    and not getattr(lot_fields, "auto_delivery", False)):
                try:
                    lot_fields.amount = 1
                except Exception:
                    pass
            lot_fields.active = True
            try:
                lot_fields.renew_fields()
                account.save_lot(lot_fields)
                changed += 1
                _log(f"Лот {name} (#{lid}) ВКЛЮЧЕН")
                _log_action_tm("lot_activated",
                               f"Лот {name} (#{lid}) включён",
                               lot_id=lid, reason="window_inactive")
                _track("🌅 Вне окна", lid, False)
            except Exception as ex:
                _log(f"Ошибка включения лота {lid}: {ex}")
                _log_action_tm("lot_save_failed",
                               f"Ошибка включения лота {lid}",
                               lot_id=lid,
                               error=f"{type(ex).__name__}: {str(ex)[:120]}")

    # 5) Уведомление (только при реальных изменениях).
    if _should_notify(changed, settings.get("notifications_enabled", True)):
        _send_notification(cardinal, settings, changes_by_rule)

    if changed > 0:
        _log(f"Цикл завершён: изменено лотов: {changed}")
    elif settings.get("log_successes", True):
        state = "ОКНО" if in_window_targets else "ВНЕ ОКНА"
        _log(f"Цикл завершён: изменений нет ({state})")


# ---------- фоновый поток ----------
_stop_event = threading.Event()


def _loop(cardinal: "Cardinal") -> None:
    _log("Фоновый цикл TradeManager запущен.")
    while not _stop_event.is_set():
        cfg = _load_config()
        interval = max(cfg["settings"]["interval_sec"], 10)
        try:
            _apply_rules(cardinal)
        except Exception:
            logger.exception("TradeManager: ошибка в цикле")
        _stop_event.wait(interval)
    _log("Фоновый цикл TradeManager остановлен.")


# ---------- инициализация ----------
def _init(cardinal: "Cardinal", *_: Any) -> None:
    _ensure_dir()
    cfg = _load_config()
    _save_config(cfg)

    _stop_event.clear()
    threading.Thread(target=_loop, args=(cardinal,), daemon=True, name="TradeManagerLoop").start()

    tg = getattr(cardinal, "telegram", None)
    if tg is None:
        return

    bot = tg.bot

    # ----- рендер -----
    def _stats(cfg2: dict) -> str:
        active_rules = [r for r in cfg2["rules"] if r.get("active", True)]
        total_rules = len(cfg2["rules"])
        try:
            lots_count = len(cardinal.profile.get_lots()) if cardinal.profile else 0
        except Exception:
            lots_count = "?"
        return (
            f"<b>TradeManager</b>\n\n"
            f"Правила: <b>{len(active_rules)}/{total_rules}</b> вкл./всего\n"
            f"Аккаунтов: <b>1</b>\n"
            f"Интервал: <b>{cfg2['settings']['interval_sec']}s</b>\n"
            f"Лотов: <b>{lots_count}</b>"
        )

    def _kb_main() -> K:
        kb = K()
        kb.add(
            B("📋 Правила", callback_data=CBT_TAB_RULES),
            B("⚙️ Настройки", callback_data=CBT_TAB_SETTINGS),
            B("📜 Логи", callback_data=CBT_TAB_LOGS),
        )
        return kb

    def _render(c: CallbackQuery, text: str, kb: K) -> None:
        try:
            bot.edit_message_text(text, c.message.chat.id, c.message.id, reply_markup=kb, parse_mode="HTML")
        except Exception:
            bot.send_message(c.message.chat.id, text, reply_markup=kb, parse_mode="HTML")

    def _capture_chat(chat_id) -> None:
        """Запоминаем chat_id оператора для уведомлений (Req 6)."""
        try:
            cfg2 = _load_config()
            if cfg2["settings"].get("operator_chat_id") != chat_id:
                cfg2["settings"]["operator_chat_id"] = chat_id
                _save_config(cfg2)
        except Exception:
            pass

    def open_main(c: CallbackQuery) -> None:
        _capture_chat(c.message.chat.id)
        _render(c, _stats(_load_config()), _kb_main())
        bot.answer_callback_query(c.id)

    def open_rules(c: CallbackQuery) -> None:
        cfg2 = _load_config()
        presets = cfg2.get("presets") or {}
        if not cfg2["rules"]:
            text = "<b>Правила</b>\n\nНет правил."
        else:
            lines = ["<b>Правила</b>\n"]
            for i, r in enumerate(cfg2["rules"]):
                stt = "🟢" if r.get("active", True) else "🔴"
                incl = r.get("include_lot_ids") or []
                excl = r.get("exclude_lot_ids") or []
                preset = r.get("preset")
                if preset:
                    scope = f"🏷 Пресет: <b>{preset}</b>"
                    if preset not in presets:
                        scope += " ⚠️(нет)"
                elif incl:
                    scope = "🎯 Только: " + _lots_inline(incl)
                else:
                    scope = ("🚫 Исключения: " + _lots_inline(excl)) if excl else "🚫 Все лоты"
                lines.append(
                    f"{stt} <b>{i + 1}. {r.get('name', f'Правило {i + 1}')}</b>\n"
                    f"    {r['start']} — {r['end']} (UTC+{r.get('utc_offset', 3)})\n"
                    f"    📅 {_weekdays_ru(r.get('weekdays'))}\n"
                    f"    {scope}"
                )
            text = "\n".join(lines)

        text += (
            "\n\n<b>Новое правило</b> — отправь:\n"
            "<code>Название | HH:MM | HH:MM | UTC_offset</code>\n"
            "Пример: <code>Ночное отключение | 23:00 | 08:00 | 3</code>"
        )

        kb = K()
        for i, r in enumerate(cfg2["rules"]):
            stt = "🟢" if r.get("active", True) else "🔴"
            name_short = (r.get("name") or f"Правило {i + 1}")[:28]
            kb.add(B(f"{stt} {i + 1}. {name_short}",
                     callback_data=f"{CBT_RULE_CARD}:{i}"))
        kb.add(B("➕ Добавить правило", callback_data=CBT_ADD_RULE))
        kb.row(
            B("🏷 Пресеты", callback_data=CBT_TAB_PRESETS),
            B("⏱ Разовые", callback_data=CBT_TAB_OVERRIDES),
        )
        kb.row(
            B("📤 Экспорт JSON", callback_data=CBT_EXPORT),
            B("📥 Импорт JSON", callback_data=CBT_IMPORT),
        )
        kb.add(B("⬅️ Назад", callback_data=CBT_OPEN))
        _render(c, text, kb)
        bot.answer_callback_query(c.id)

    # ---- карточка одного правила ----
    def _show_rule_card(c: CallbackQuery, idx: int) -> None:
        cfg2 = _load_config()
        if not (0 <= idx < len(cfg2["rules"])):
            bot.answer_callback_query(c.id, "Правило не найдено.")
            return open_rules(c)
        r = cfg2["rules"][idx]
        presets = cfg2.get("presets") or {}
        incl = r.get("include_lot_ids") or []
        excl = r.get("exclude_lot_ids") or []
        preset = r.get("preset")
        if preset:
            scope = f"🏷 Пресет: <b>{preset}</b>" + (" ⚠️ (не найден)" if preset not in presets else "")
        elif incl:
            scope = "🎯 Только эти: " + _lots_inline(incl)
        else:
            scope = ("🚫 Исключения: " + _lots_inline(excl)) if excl else "🚫 Действует на все лоты"
        active_st = "🟢 Включено" if r.get("active", True) else "🔴 Выключено"
        text = (
            f"<b>Правило {idx + 1}: {r.get('name')}</b>\n\n"
            f"Статус: <b>{active_st}</b>\n"
            f"Время: <b>{r['start']} — {r['end']}</b> (UTC+{r.get('utc_offset', 3)})\n"
            f"📅 Дни: <b>{_weekdays_ru(r.get('weekdays'))}</b>\n"
            f"{scope}"
        )
        preset_label = f"🏷 Пресет: {preset}" if preset else "🏷 Пресет: нет"
        kb = K()
        kb.row(
            B("🔴 Выключить" if r.get("active", True) else "🟢 Включить",
              callback_data=f"{CBT_TOGGLE_RULE}:{idx}"),
            B("🗑 Удалить", callback_data=f"{CBT_DEL_RULE}:{idx}"),
        )
        kb.row(
            B("🎯 Только эти 🔘", callback_data=f"{CBT_PICK_INC}:{idx}"),
            B("✍️ ID", callback_data=f"{CBT_EDIT_INCLUDE}:{idx}"),
        )
        kb.row(
            B("🚫 Исключения 🔘", callback_data=f"{CBT_PICK_EXC}:{idx}"),
            B("✍️ ID", callback_data=f"{CBT_EDIT_EXCLUDE}:{idx}"),
        )
        kb.add(B("📅 Дни недели", callback_data=f"{CBT_EDIT_WEEKDAYS}:{idx}"))
        kb.add(B(preset_label, callback_data=f"{CBT_RULE_PRESET}:{idx}"))
        kb.row(
            B("🧪 Тест сейчас", callback_data=f"{CBT_TEST_RULE}:{idx}"),
            B("⏱ Пропуск сегодня", callback_data=f"{CBT_OVR_SKIP_RULE}:{idx}"),
        )
        kb.add(B("⬅️ К правилам", callback_data=CBT_TAB_RULES))
        _render(c, text, kb)
        bot.answer_callback_query(c.id)

    def open_rule_card(c: CallbackQuery) -> None:
        _show_rule_card(c, int(c.data.split(":")[-1]))

    # ---- пикер лотов (пагинированный чекбокс) ----
    def _picker_order() -> list:
        try:
            lots = cardinal.profile.get_lots() if cardinal.profile else []
        except Exception:
            lots = []
        _refresh_lot_names(lots, 0, now=time.time(), force=True)
        return [int(ls.id) for ls in lots]

    def _picker_back(c: CallbackQuery, data: dict) -> None:
        kind = data.get("kind")
        if kind in ("inc", "exc"):
            idx = data.get("rule_idx")
            if idx is not None:
                return _show_rule_card(c, int(idx))
            return open_rules(c)
        if kind == "preset":
            return open_presets(c)
        if kind == "force_off":
            return open_overrides(c)
        return open_rules(c)

    def _picker_title(data: dict) -> str:
        kind = data.get("kind")
        n = len(data.get("selected") or [])
        head = {
            "inc": "🎯 Выбор лотов «только эти»",
            "exc": "🚫 Выбор исключаемых лотов",
            "preset": f"🏷 Пресет «{data.get('preset_name')}»",
            "force_off": "⏱ Выключить до утра — выбор лотов",
        }.get(kind, "Выбор лотов")
        return f"<b>{head}</b>\nВыбрано: <b>{n}</b>\nОтметь лоты и нажми «Подтвердить»."

    def _open_picker(c: CallbackQuery, kind: str, *, rule_idx=None, rule_id=None,
                     preset_name=None, preselected=None) -> None:
        order = _picker_order()
        cfg2 = _load_config()
        page_size = int(cfg2["settings"].get("picker_page_size", 8))
        if not order:
            kb = K()
            back = {"inc": f"{CBT_RULE_CARD}:{rule_idx}",
                    "exc": f"{CBT_RULE_CARD}:{rule_idx}",
                    "preset": CBT_TAB_PRESETS,
                    "force_off": CBT_TAB_OVERRIDES}.get(kind, CBT_TAB_RULES)
            kb.add(B("⬅️ Назад", callback_data=back))
            _render(c, "На аккаунте нет доступных лотов.", kb)
            bot.answer_callback_query(c.id)
            return
        data = {"kind": kind, "rule_idx": rule_idx, "rule_id": rule_id,
                "preset_name": preset_name,
                "selected": [int(x) for x in (preselected or [])], "page": 0}
        tg.set_state(c.message.chat.id, c.message.id, c.from_user.id, STATE_PICKER, data)
        _render(c, _picker_title(data), _pick_keyboard(order, data["selected"], 0, page_size))
        bot.answer_callback_query(c.id)

    def _picker_state(c: CallbackQuery):
        state = tg.get_state(c.message.chat.id, c.from_user.id) or {}
        if state.get("state") != STATE_PICKER:
            return None
        return state.get("data") or None

    def pick_inc(c: CallbackQuery) -> None:
        idx = int(c.data.split(":")[-1])
        cfg2 = _load_config()
        if not (0 <= idx < len(cfg2["rules"])):
            bot.answer_callback_query(c.id, "Правило не найдено.")
            return
        r = cfg2["rules"][idx]
        _open_picker(c, "inc", rule_idx=idx, rule_id=r.get("id"),
                     preselected=r.get("include_lot_ids"))

    def pick_exc(c: CallbackQuery) -> None:
        idx = int(c.data.split(":")[-1])
        cfg2 = _load_config()
        if not (0 <= idx < len(cfg2["rules"])):
            bot.answer_callback_query(c.id, "Правило не найдено.")
            return
        r = cfg2["rules"][idx]
        _open_picker(c, "exc", rule_idx=idx, rule_id=r.get("id"),
                     preselected=r.get("exclude_lot_ids"))

    def picker_toggle(c: CallbackQuery) -> None:
        data = _picker_state(c)
        if not data:
            bot.answer_callback_query(c.id, "Сессия выбора истекла.")
            return
        parts = c.data.split(":")
        page, idx = int(parts[-2]), int(parts[-1])
        order = _picker_order()
        page_size = int(_load_config()["settings"].get("picker_page_size", 8))
        pos = page * page_size + idx
        if not (0 <= pos < len(order)):
            bot.answer_callback_query(c.id, "Список изменился, обновите.")
            return
        data["selected"] = _picker_apply_toggle(data.get("selected") or [], order[pos])
        data["page"] = page
        tg.set_state(c.message.chat.id, c.message.id, c.from_user.id, STATE_PICKER, data)
        _render(c, _picker_title(data), _pick_keyboard(order, data["selected"], page, page_size))
        bot.answer_callback_query(c.id)

    def picker_page(c: CallbackQuery) -> None:
        data = _picker_state(c)
        if not data:
            bot.answer_callback_query(c.id, "Сессия выбора истекла.")
            return
        page = int(c.data.split(":")[-1])
        order = _picker_order()
        page_size = int(_load_config()["settings"].get("picker_page_size", 8))
        data["page"] = page
        tg.set_state(c.message.chat.id, c.message.id, c.from_user.id, STATE_PICKER, data)
        _render(c, _picker_title(data), _pick_keyboard(order, data.get("selected") or [], page, page_size))
        bot.answer_callback_query(c.id)

    def picker_cancel(c: CallbackQuery) -> None:
        data = _picker_state(c) or {}
        tg.clear_state(c.message.chat.id, c.from_user.id, True)
        bot.answer_callback_query(c.id, "Отменено.")
        _picker_back(c, data)

    def picker_confirm(c: CallbackQuery) -> None:
        data = _picker_state(c)
        if not data:
            bot.answer_callback_query(c.id, "Сессия выбора истекла.")
            return open_rules(c)
        tg.clear_state(c.message.chat.id, c.from_user.id, True)
        kind = data.get("kind")
        selected = [int(x) for x in (data.get("selected") or [])]
        cfg2 = _load_config()
        if kind in ("inc", "exc"):
            rule = _find_rule_by_id(cfg2, data.get("rule_id"))
            if rule is None:
                bot.answer_callback_query(c.id, "Правило не найдено.")
                return open_rules(c)
            rule["include_lot_ids" if kind == "inc" else "exclude_lot_ids"] = selected
            _save_config(cfg2)
            _log(f"Правило '{rule.get('name')}': {kind} = {selected}")
            bot.answer_callback_query(c.id, "Сохранено.")
            return _picker_back(c, data)
        if kind == "preset":
            name = data.get("preset_name")
            cfg2.setdefault("presets", {})[name] = selected
            _save_config(cfg2)
            _log(f"Пресет '{name}' = {selected}")
            bot.answer_callback_query(c.id, "Пресет сохранён.")
            return open_presets(c)
        if kind == "force_off":
            s = cfg2["settings"]
            off = _global_offset_from_settings(s)
            now_dt = datetime.now(timezone(timedelta(hours=off)))
            until = _morning_target_ts(now_dt, s.get("override_morning_time", "08:00"))
            cfg2.setdefault("overrides", []).append({
                "kind": "force_off", "lot_ids": selected, "until_ts": until,
                "label": "До утра"})
            _save_config(cfg2)
            _log(f"Оверрайд force_off до утра: {selected}")
            bot.answer_callback_query(c.id, "Создан оверрайд.")
            return open_overrides(c)
        return open_rules(c)

    # ---- редактор дней недели ----
    def open_weekdays(c: CallbackQuery) -> None:
        idx = int(c.data.split(":")[-1])
        cfg2 = _load_config()
        if not (0 <= idx < len(cfg2["rules"])):
            bot.answer_callback_query(c.id, "Правило не найдено.")
            return
        r = cfg2["rules"][idx]
        days = {int(d) for d in (r.get("weekdays") or [])}
        text = (
            f"<b>Дни недели — {r.get('name')}</b>\n\n"
            f"Активно: <b>{_weekdays_ru(r.get('weekdays'))}</b>\n\n"
            "Пустой набор = правило действует каждый день."
        )
        kb = K()
        row = []
        for d in range(7):
            mark = "✅" if d in days else "⬜"
            row.append(B(f"{mark} {_WD_RU[d]}", callback_data=f"{CBT_TOGGLE_WD}:{idx}:{d}"))
            if len(row) == 4:
                kb.row(*row)
                row = []
        if row:
            kb.row(*row)
        kb.add(B("⬅️ К правилу", callback_data=f"{CBT_RULE_CARD}:{idx}"))
        _render(c, text, kb)
        bot.answer_callback_query(c.id)

    def toggle_weekday(c: CallbackQuery) -> None:
        parts = c.data.split(":")
        idx, day = int(parts[-2]), int(parts[-1])
        cfg2 = _load_config()
        if not (0 <= idx < len(cfg2["rules"])) or not (0 <= day <= 6):
            bot.answer_callback_query(c.id, "Ошибка.")
            return
        days = [int(d) for d in (cfg2["rules"][idx].get("weekdays") or [])]
        days = _picker_apply_toggle(days, day)
        cfg2["rules"][idx]["weekdays"] = sorted(set(days))
        _save_config(cfg2)
        # переоткрываем редактор
        c.data = f"{CBT_EDIT_WEEKDAYS}:{idx}"
        open_weekdays(c)

    # ---- назначение пресета на правило (циклически) ----
    def cycle_rule_preset(c: CallbackQuery) -> None:
        idx = int(c.data.split(":")[-1])
        cfg2 = _load_config()
        if not (0 <= idx < len(cfg2["rules"])):
            bot.answer_callback_query(c.id, "Правило не найдено.")
            return
        names = sorted((cfg2.get("presets") or {}).keys())
        options = [None] + names
        cur = cfg2["rules"][idx].get("preset")
        try:
            nxt = options[(options.index(cur) + 1) % len(options)]
        except ValueError:
            nxt = None
        cfg2["rules"][idx]["preset"] = nxt
        _save_config(cfg2)
        bot.answer_callback_query(c.id, f"Пресет: {nxt or 'нет'}")
        _show_rule_card(c, idx)

    # ---- пресеты ----
    def open_presets(c: CallbackQuery) -> None:
        cfg2 = _load_config()
        presets = cfg2.get("presets") or {}
        if not presets:
            text = "<b>🏷 Пресеты лотов</b>\n\nНет пресетов. Создай переиспользуемый набор лотов."
        else:
            lines = ["<b>🏷 Пресеты лотов</b>\n"]
            for name in sorted(presets):
                lines.append(f"• <b>{name}</b> — лотов: {len(presets[name] or [])}")
            text = "\n".join(lines)
        kb = K()
        for j, name in enumerate(sorted(presets)):
            kb.row(
                B(f"✏️ {name}"[:28], callback_data=f"{CBT_PRESET_EDIT}:{j}"),
                B("🗑", callback_data=f"{CBT_PRESET_DEL}:{j}"),
            )
        kb.add(B("➕ Новый пресет", callback_data=CBT_PRESET_ADD))
        kb.add(B("⬅️ К правилам", callback_data=CBT_TAB_RULES))
        _render(c, text, kb)
        bot.answer_callback_query(c.id)

    def ask_preset_name(c: CallbackQuery) -> None:
        result = bot.send_message(
            c.message.chat.id,
            "Введи <b>имя</b> нового пресета (затем выберешь лоты кнопками):",
            parse_mode="HTML")
        tg.set_state(c.message.chat.id, result.id, c.from_user.id, STATE_AWAIT_PRESET_NAME)
        bot.answer_callback_query(c.id)

    def on_preset_name(m: Message) -> None:
        tg.clear_state(m.chat.id, m.from_user.id, True)
        name = (m.text or "").strip()
        if not name:
            bot.send_message(m.chat.id, "Имя не может быть пустым.")
            return
        cfg2 = _load_config()
        if name in (cfg2.get("presets") or {}):
            bot.send_message(m.chat.id, "⚠️ Пресет с таким именем уже существует.")
            return
        cfg2.setdefault("presets", {})[name] = []
        _save_config(cfg2)
        bot.send_message(
            m.chat.id,
            f"✅ Пресет <b>{name}</b> создан. Открой 🏷 Пресеты → ✏️, чтобы выбрать лоты.",
            parse_mode="HTML")

    def edit_preset(c: CallbackQuery) -> None:
        j = int(c.data.split(":")[-1])
        cfg2 = _load_config()
        names = sorted((cfg2.get("presets") or {}).keys())
        if not (0 <= j < len(names)):
            bot.answer_callback_query(c.id, "Пресет не найден.")
            return
        name = names[j]
        _open_picker(c, "preset", preset_name=name,
                     preselected=cfg2["presets"].get(name))

    def del_preset(c: CallbackQuery) -> None:
        j = int(c.data.split(":")[-1])
        cfg2 = _load_config()
        names = sorted((cfg2.get("presets") or {}).keys())
        if 0 <= j < len(names):
            removed = names[j]
            cfg2["presets"].pop(removed, None)
            _save_config(cfg2)
            _log(f"Пресет '{removed}' удалён")
        open_presets(c)

    # ---- разовые оверрайды ----
    def open_overrides(c: CallbackQuery) -> None:
        cfg2 = _load_config()
        now_ts = time.time()
        kept, changed = _expire_overrides(cfg2.get("overrides") or [], now_ts)
        if changed:
            cfg2["overrides"] = kept
            _save_config(cfg2)
        overrides = kept
        if not overrides:
            text = "<b>⏱ Разовые правила</b>\n\nНет активных оверрайдов."
        else:
            lines = ["<b>⏱ Разовые правила</b>\n"]
            for o in overrides:
                tz = timezone(timedelta(hours=_global_offset_from_settings(cfg2["settings"])))
                until = datetime.fromtimestamp(float(o.get("until_ts", 0)), tz).strftime("%d.%m %H:%M")
                if o.get("kind") == "force_off":
                    what = "⛔ Выкл до " + until + ": " + _lots_inline(o.get("lot_ids") or [])
                else:
                    rule = _find_rule_by_id(cfg2, o.get("rule_id"))
                    rname = rule.get("name") if rule else o.get("rule_id")
                    what = f"⏸ Пропуск правила «{rname}» до {until}"
                lines.append(f"• {what}")
            text = "\n".join(lines)
        kb = K()
        for k, o in enumerate(overrides):
            label = ("⛔ до утра" if o.get("kind") == "force_off" else "⏸ пропуск")
            kb.add(B(f"❌ Отменить: {label}", callback_data=f"{CBT_OVR_CANCEL}:{k}"))
        kb.add(B("⛔ Выключить лоты до утра", callback_data=CBT_OVR_FORCE_OFF))
        kb.add(B("⬅️ К правилам", callback_data=CBT_TAB_RULES))
        _render(c, text, kb)
        bot.answer_callback_query(c.id)

    def ovr_force_off(c: CallbackQuery) -> None:
        _open_picker(c, "force_off")

    def ovr_skip_rule(c: CallbackQuery) -> None:
        idx = int(c.data.split(":")[-1])
        cfg2 = _load_config()
        if not (0 <= idx < len(cfg2["rules"])):
            bot.answer_callback_query(c.id, "Правило не найдено.")
            return
        r = cfg2["rules"][idx]
        off = _effective_offset(r, _global_offset_from_settings(cfg2["settings"]))
        now_dt = datetime.now(timezone(timedelta(hours=off)))
        cfg2.setdefault("overrides", []).append({
            "kind": "skip_rule", "rule_id": r.get("id"),
            "until_ts": _end_of_day_ts(now_dt), "label": "Пропуск на сегодня"})
        _save_config(cfg2)
        _log(f"Оверрайд skip_rule на сегодня: правило '{r.get('name')}'")
        bot.answer_callback_query(c.id, "Правило пропущено до конца дня.")
        _show_rule_card(c, idx)

    def ovr_cancel(c: CallbackQuery) -> None:
        k = int(c.data.split(":")[-1])
        cfg2 = _load_config()
        now_ts = time.time()
        kept, _ = _expire_overrides(cfg2.get("overrides") or [], now_ts)
        if 0 <= k < len(kept):
            kept.pop(k)
            cfg2["overrides"] = kept
            _save_config(cfg2)
            _log("Оверрайд отменён оператором")
        open_overrides(c)

    # ---- исключения по лотам ----
    def ask_edit_exclude(c: CallbackQuery) -> None:
        idx = int(c.data.split(":")[-1])
        cfg2 = _load_config()
        if not (0 <= idx < len(cfg2["rules"])):
            bot.answer_callback_query(c.id, "Правило не найдено.")
            return
        cur = ", ".join(str(x) for x in (cfg2["rules"][idx].get("exclude_lot_ids") or [])) or "—"
        result = bot.send_message(
            c.message.chat.id,
            f"Правило <b>{cfg2['rules'][idx].get('name')}</b>\n"
            f"Текущие исключения: <code>{cur}</code>\n\n"
            "Отправь lot_id через запятую (эти лоты не будут выключаться):\n"
            "<code>12345, 67890, 11111</code>\n"
            "Пустым сообщением или <code>-</code> — очистить список.",
            parse_mode="HTML",
        )
        tg.set_state(c.message.chat.id, result.id, c.from_user.id, STATE_AWAIT_EXCLUDE,
                     {"rule_idx": idx})
        bot.answer_callback_query(c.id)

    def on_exclude(m: Message) -> None:
        state = tg.get_state(m.chat.id, m.from_user.id) or {}
        data = state.get("data", {})
        tg.clear_state(m.chat.id, m.from_user.id, True)
        idx = int(data.get("rule_idx", -1))
        cfg2 = _load_config()
        if not (0 <= idx < len(cfg2["rules"])):
            bot.send_message(m.chat.id, "Правило не найдено.")
            return
        raw = (m.text or "").strip()
        if not raw or raw == "-":
            cfg2["rules"][idx]["exclude_lot_ids"] = []
            _save_config(cfg2)
            _log(f"Правило #{idx}: исключения очищены.")
            bot.send_message(m.chat.id, "✅ Исключения очищены.")
            return
        ids: list[int] = []
        for x in raw.replace(";", ",").split(","):
            x = x.strip()
            if not x:
                continue
            try:
                ids.append(int(x))
            except ValueError:
                bot.send_message(m.chat.id, f"'{x}' не похоже на lot_id.")
                return
        cfg2["rules"][idx]["exclude_lot_ids"] = ids
        _save_config(cfg2)
        _log(f"Правило #{idx}: исключения = {ids}")
        bot.send_message(m.chat.id, f"✅ Исключено лотов: <b>{len(ids)}</b>", parse_mode="HTML")

    # ---- «только эти лоты» (include) ----
    def ask_edit_include(c: CallbackQuery) -> None:
        idx = int(c.data.split(":")[-1])
        cfg2 = _load_config()
        if not (0 <= idx < len(cfg2["rules"])):
            bot.answer_callback_query(c.id, "Правило не найдено.")
            return
        cur = ", ".join(
            str(x) for x in (cfg2["rules"][idx].get("include_lot_ids") or [])
        ) or "—"
        result = bot.send_message(
            c.message.chat.id,
            f"Правило <b>{cfg2['rules'][idx].get('name')}</b>\n"
            f"Сейчас «только эти лоты»: <code>{cur}</code>\n\n"
            "Отправь lot_id через запятую — правило будет выключать на ночь "
            "<b>только эти</b> лоты, остальные не трогает:\n"
            "<code>12345, 67890</code>\n"
            "Пустым сообщением или <code>-</code> — очистить (правило снова "
            "будет действовать на все лоты, кроме исключений).",
            parse_mode="HTML",
        )
        tg.set_state(c.message.chat.id, result.id, c.from_user.id,
                     STATE_AWAIT_INCLUDE, {"rule_idx": idx})
        bot.answer_callback_query(c.id)

    def on_include(m: Message) -> None:
        state = tg.get_state(m.chat.id, m.from_user.id) or {}
        data = state.get("data", {})
        tg.clear_state(m.chat.id, m.from_user.id, True)
        idx = int(data.get("rule_idx", -1))
        cfg2 = _load_config()
        if not (0 <= idx < len(cfg2["rules"])):
            bot.send_message(m.chat.id, "Правило не найдено.")
            return
        raw = (m.text or "").strip()
        if not raw or raw == "-":
            cfg2["rules"][idx]["include_lot_ids"] = []
            _save_config(cfg2)
            _log(f"Правило #{idx}: список 'только эти' очищен.")
            bot.send_message(
                m.chat.id,
                "✅ Список очищен — правило снова действует на все лоты "
                "(кроме исключений).")
            return
        ids: list[int] = []
        for x in raw.replace(";", ",").split(","):
            x = x.strip()
            if not x:
                continue
            try:
                ids.append(int(x))
            except ValueError:
                bot.send_message(m.chat.id, f"'{x}' не похоже на lot_id.")
                return
        cfg2["rules"][idx]["include_lot_ids"] = ids
        _save_config(cfg2)
        _log(f"Правило #{idx}: только эти лоты = {ids}")
        bot.send_message(
            m.chat.id,
            f"✅ Правило теперь выключает на ночь только <b>{len(ids)}</b> "
            f"выбранных лотов.", parse_mode="HTML")

    # ---- тест правила ----
    def test_rule(c: CallbackQuery) -> None:
        idx = int(c.data.split(":")[-1])
        cfg2 = _load_config()
        if not (0 <= idx < len(cfg2["rules"])):
            bot.answer_callback_query(c.id, "Правило не найдено.")
            return
        rule = cfg2["rules"][idx]
        in_win = _in_window(rule)
        bot.answer_callback_query(c.id, "Проверяю…")
        try:
            lots = cardinal.profile.get_lots() if cardinal.profile else []
        except Exception as ex:
            bot.send_message(c.message.chat.id, f"Ошибка выборки лотов: {ex}")
            return
        _refresh_lot_names(lots, 0, now=time.time(), force=True)
        all_ids = [int(lot.id) for lot in lots]
        targets = _resolve_rule_targets(rule, cfg2.get("presets") or {}, all_ids)
        affected = [lid for lid in all_ids if lid in targets]
        skipped = [lid for lid in all_ids if lid not in targets]
        verdict = (
            "🟢 ОКНО АКТИВНО — эти лоты будут ВЫКЛЮЧЕНЫ:"
            if in_win else "⚪ ОКНО НЕАКТИВНО — эти лоты БУДУТ ВКЛЮЧЕНЫ (если выключены):"
        )
        lines = [
            f"<b>Тест правила</b> '{rule.get('name')}'",
            f"Время: {rule['start']} — {rule['end']} (UTC+{rule.get('utc_offset', 3)})",
            f"📅 Дни: {_weekdays_ru(rule.get('weekdays'))}",
            f"{verdict}",
            f"  Затронутых лотов: <b>{len(affected)}</b>",
            f"  Исключено: <b>{len(skipped)}</b>",
        ]
        if affected:
            sample = _lots_inline(affected[:15])
            more = f" … (ещё {len(affected) - 15})" if len(affected) > 15 else ""
            lines.append(f"\n📍 Затронуты: {sample}{more}")
        if skipped:
            sample = _lots_inline(skipped[:15])
            lines.append(f"⚪ Не затронуты: {sample}")
        bot.send_message(c.message.chat.id, "\n".join(lines), parse_mode="HTML")

    # ---- экспорт / импорт ----
    def export_rules(c: CallbackQuery) -> None:
        cfg2 = _load_config()
        payload = {"rules": cfg2.get("rules", []), "settings": cfg2.get("settings", {})}
        dump = json.dumps(payload, ensure_ascii=False, indent=2)
        bot.answer_callback_query(c.id, "Экспорт готов")
        if len(dump) < 3800:
            bot.send_message(c.message.chat.id,
                             f"<b>Экспорт правил</b>\n<pre>{dump}</pre>",
                             parse_mode="HTML")
        else:
            tmp = os.path.join(PLUGIN_DIR, "export.json")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(dump)
            with open(tmp, "rb") as f:
                bot.send_document(c.message.chat.id, f, caption="Экспорт TradeManager")

    def ask_import(c: CallbackQuery) -> None:
        result = bot.send_message(
            c.message.chat.id,
            "Пришли JSON-строку в формате экспорта (<code>{\"rules\": […]}</code>).\n"
            "Правила будут <b>добавлены</b> к текущим. Настройки (интервал/логи) перезапишутся.",
            parse_mode="HTML",
        )
        tg.set_state(c.message.chat.id, result.id, c.from_user.id, STATE_AWAIT_IMPORT)
        bot.answer_callback_query(c.id)

    def on_import(m: Message) -> None:
        tg.clear_state(m.chat.id, m.from_user.id, True)
        try:
            payload = json.loads((m.text or "").strip())
        except Exception as ex:
            bot.send_message(m.chat.id, f"⚠️ Битый JSON: {ex}")
            return
        if not isinstance(payload, dict):
            bot.send_message(m.chat.id, "⚠️ Ожидаю объект <code>{...}</code>.", parse_mode="HTML")
            return
        new_rules = payload.get("rules") or []
        if not isinstance(new_rules, list):
            bot.send_message(m.chat.id, "⚠️ 'rules' должен быть списком.")
            return
        cfg2 = _load_config()
        added = 0
        for r in new_rules:
            if not isinstance(r, dict):
                continue
            r.setdefault("active", True)
            r.setdefault("note", "")
            r.setdefault("utc_offset", 3)
            _migrate_rule(r)
            cfg2["rules"].append(r)
            added += 1
        if isinstance(payload.get("settings"), dict):
            cfg2["settings"].update({k: v for k, v in payload["settings"].items()
                                     if k in DEFAULT_CONFIG["settings"]})
        _save_config(cfg2)
        _log(f"Импортировано правил: {added}")
        bot.send_message(m.chat.id, f"✅ Добавлено правил: <b>{added}</b>", parse_mode="HTML")

    def open_settings(c: CallbackQuery) -> None:
        cfg2 = _load_config()
        s = cfg2["settings"]
        log_st = "🟢 Вкл" if s.get("log_successes", True) else "🔴 Выкл"
        enabled_st = "🟢 Включен" if cfg2.get("enabled", True) else "🔴 Выключен"
        notify_st = "🟢 Вкл" if s.get("notifications_enabled", True) else "🔴 Выкл"
        tz_name = s.get("timezone", "UTC+3")
        text = (
            "<b>Настройки цикла</b>\n\n"
            f"Статус: <b>{enabled_st}</b>\n"
            f"🕐 Часовой пояс: <b>{tz_name}</b>\n"
            f"⏱ Интервал проверки: <b>{s['interval_sec']} сек</b>\n"
            f"📝 Логировать успешные: <b>{log_st}</b>\n"
            f"🔔 Уведомления о переключениях: <b>{notify_st}</b>"
        )
        kb = K()
        kb.add(B(enabled_st, callback_data=CBT_TOGGLE_ENABLED))
        kb.add(B(f"🕐 Часовой пояс: {tz_name}", callback_data=CBT_CHANGE_TZ))
        kb.add(B(f"⏱ Интервал ({s['interval_sec']}s)", callback_data=CBT_EDIT_INTERVAL))
        kb.add(B(f"📝 Лог успешных: {log_st}", callback_data=CBT_TOGGLE_LOG_OK))
        kb.add(B(f"🔔 Уведомления: {notify_st}", callback_data=CBT_TOGGLE_NOTIFY))
        kb.add(B("⬅️ Назад", callback_data=CBT_OPEN))
        _render(c, text, kb)
        bot.answer_callback_query(c.id)

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

    def toggle_rule(c: CallbackQuery) -> None:
        idx = int(c.data.split(":")[-1])
        cfg2 = _load_config()
        if 0 <= idx < len(cfg2["rules"]):
            cfg2["rules"][idx]["active"] = not cfg2["rules"][idx].get("active", True)
            _save_config(cfg2)
            state = "активировано" if cfg2["rules"][idx]["active"] else "деактивировано"
            _log(f"Правило '{cfg2['rules'][idx].get('name')}' {state}")
        _show_rule_card(c, idx)

    def del_rule(c: CallbackQuery) -> None:
        idx = int(c.data.split(":")[-1])
        cfg2 = _load_config()
        if 0 <= idx < len(cfg2["rules"]):
            removed = cfg2["rules"].pop(idx)
            _save_config(cfg2)
            _log(f"Удалено правило '{removed.get('name')}'")
        open_rules(c)

    def ask_add_rule(c: CallbackQuery) -> None:
        result = bot.send_message(
            c.message.chat.id,
            "Отправь правило:\n"
            "<code>Название | Начало (HH:MM) | Конец (HH:MM) | UTC_offset</code>\n"
            "Пример: <code>Ночное | 23:00 | 08:00 | 3</code>",
            parse_mode="HTML",
        )
        tg.set_state(c.message.chat.id, result.id, c.from_user.id, STATE_AWAIT_RULE)
        bot.answer_callback_query(c.id)

    def on_rule(m: Message) -> None:
        tg.clear_state(m.chat.id, m.from_user.id, True)
        parts = [p.strip() for p in (m.text or "").split("|")]
        if len(parts) < 4:
            bot.send_message(m.chat.id, "Нужно 4 поля через | (название, начало, конец, utc_offset).")
            return
        name, start, end, offset_str = parts[0], parts[1], parts[2], parts[3]
        # validate
        for t in (start, end):
            try:
                datetime.strptime(t, "%H:%M")
            except ValueError:
                bot.send_message(m.chat.id, f"Неверный формат времени: '{t}'. Нужно HH:MM.")
                return
        try:
            utc_off = int(offset_str)
        except ValueError:
            bot.send_message(m.chat.id, f"UTC offset должен быть числом, получено '{offset_str}'.")
            return
        cfg2 = _load_config()
        cfg2["rules"].append({
            "name": name,
            "start": start,
            "end": end,
            "utc_offset": utc_off,
            "active": True,
            "note": "",
            "exclude_lot_ids": [],
            "include_lot_ids": [],
        })
        _save_config(cfg2)
        _log(f"Добавлено правило '{name}' ({start}—{end}, UTC+{utc_off})")
        bot.send_message(m.chat.id, f"✅ Правило <b>{name}</b> добавлено.", parse_mode="HTML")

    def ask_edit_interval(c: CallbackQuery) -> None:
        result = bot.send_message(c.message.chat.id, "Введи новый интервал проверки (секунды, мин. 10):")
        tg.set_state(c.message.chat.id, result.id, c.from_user.id, STATE_AWAIT_INTERVAL)
        bot.answer_callback_query(c.id)

    def on_interval(m: Message) -> None:
        tg.clear_state(m.chat.id, m.from_user.id, True)
        try:
            val = int((m.text or "").strip())
            if val < 10:
                raise ValueError
        except ValueError:
            bot.send_message(m.chat.id, "Нужно целое число >= 10.")
            return
        cfg2 = _load_config()
        cfg2["settings"]["interval_sec"] = val
        _save_config(cfg2)
        _log(f"Интервал изменён: {val} сек")
        bot.send_message(m.chat.id, f"✅ Интервал = <b>{val}</b> сек", parse_mode="HTML")

    def toggle_log_ok(c: CallbackQuery) -> None:
        cfg2 = _load_config()
        cfg2["settings"]["log_successes"] = not cfg2["settings"].get("log_successes", True)
        _save_config(cfg2)
        open_settings(c)

    def toggle_notify(c: CallbackQuery) -> None:
        cfg2 = _load_config()
        cfg2["settings"]["notifications_enabled"] = not cfg2["settings"].get("notifications_enabled", True)
        _save_config(cfg2)
        state = "включены" if cfg2["settings"]["notifications_enabled"] else "выключены"
        _log(f"Уведомления {state}")
        open_settings(c)

    def toggle_enabled(c: CallbackQuery) -> None:
        cfg2 = _load_config()
        cfg2["enabled"] = not cfg2.get("enabled", True)
        _save_config(cfg2)
        state = "включен" if cfg2["enabled"] else "выключен"
        _log(f"TradeManager {state}")
        open_settings(c)

    def ask_change_tz(c: CallbackQuery) -> None:
        presets_list = ", ".join(f"<code>{k}</code>" for k in TIMEZONE_PRESETS)
        result = bot.send_message(
            c.message.chat.id,
            f"Введи часовой пояс.\n\nДоступные пресеты:\n{presets_list}\n\n"
            "Или введи числовое смещение (например <code>3</code> для UTC+3).",
            parse_mode="HTML",
        )
        tg.set_state(c.message.chat.id, result.id, c.from_user.id, STATE_AWAIT_TIMEZONE)
        bot.answer_callback_query(c.id)

    def on_timezone(m: Message) -> None:
        tg.clear_state(m.chat.id, m.from_user.id, True)
        raw = (m.text or "").strip()
        if raw in TIMEZONE_PRESETS:
            tz_name = raw
        else:
            try:
                offset_val = int(raw)
                tz_name = f"UTC+{offset_val}" if offset_val >= 0 else f"UTC{offset_val}"
                if tz_name not in TIMEZONE_PRESETS:
                    TIMEZONE_PRESETS[tz_name] = offset_val
            except ValueError:
                bot.send_message(m.chat.id, "Неизвестный пресет. Попробуй ещё раз через настройки.")
                return
        cfg2 = _load_config()
        cfg2["settings"]["timezone"] = tz_name
        _save_config(cfg2)
        _log(f"Часовой пояс изменён: {tz_name}")
        bot.send_message(m.chat.id, f"✅ Часовой пояс: <b>{tz_name}</b>", parse_mode="HTML")

    # ----- callbacks -----
    def _cb(prefix: str):
        return lambda c: c.data == prefix or c.data.startswith(prefix + ":")

    tg.cbq_handler(open_main, _cb(CBT_OPEN))
    tg.cbq_handler(open_main, lambda c: c.data.startswith(f"47:{UUID}"))

    tg.cbq_handler(open_rules, _cb(CBT_TAB_RULES))
    tg.cbq_handler(open_rule_card, _cb(CBT_RULE_CARD))
    tg.cbq_handler(open_settings, _cb(CBT_TAB_SETTINGS))
    tg.cbq_handler(open_logs, _cb(CBT_TAB_LOGS))
    tg.cbq_handler(clear_logs, _cb(CBT_CLEAR_LOGS))
    tg.cbq_handler(toggle_rule, _cb(CBT_TOGGLE_RULE))
    tg.cbq_handler(del_rule, _cb(CBT_DEL_RULE))
    tg.cbq_handler(ask_add_rule, _cb(CBT_ADD_RULE))
    tg.cbq_handler(ask_edit_interval, _cb(CBT_EDIT_INTERVAL))
    tg.cbq_handler(toggle_log_ok, _cb(CBT_TOGGLE_LOG_OK))
    tg.cbq_handler(toggle_notify, _cb(CBT_TOGGLE_NOTIFY))
    tg.cbq_handler(toggle_enabled, _cb(CBT_TOGGLE_ENABLED))
    tg.cbq_handler(ask_change_tz, _cb(CBT_CHANGE_TZ))
    tg.cbq_handler(ask_edit_exclude, _cb(CBT_EDIT_EXCLUDE))
    tg.cbq_handler(ask_edit_include, _cb(CBT_EDIT_INCLUDE))
    tg.cbq_handler(test_rule, _cb(CBT_TEST_RULE))
    tg.cbq_handler(export_rules, _cb(CBT_EXPORT))
    tg.cbq_handler(ask_import, _cb(CBT_IMPORT))
    # v1.3.0: пикер
    tg.cbq_handler(pick_inc, _cb(CBT_PICK_INC))
    tg.cbq_handler(pick_exc, _cb(CBT_PICK_EXC))
    tg.cbq_handler(picker_toggle, _cb(CBT_PICK_TOGGLE))
    tg.cbq_handler(picker_page, _cb(CBT_PICK_PAGE))
    tg.cbq_handler(picker_confirm, _cb(CBT_PICK_OK))
    tg.cbq_handler(picker_cancel, _cb(CBT_PICK_CANCEL))
    tg.cbq_handler(lambda c: bot.answer_callback_query(c.id), _cb(CBT_PICK_NOP))
    # v1.3.0: дни недели
    tg.cbq_handler(open_weekdays, _cb(CBT_EDIT_WEEKDAYS))
    tg.cbq_handler(toggle_weekday, _cb(CBT_TOGGLE_WD))
    # v1.3.0: пресеты
    tg.cbq_handler(open_presets, _cb(CBT_TAB_PRESETS))
    tg.cbq_handler(ask_preset_name, _cb(CBT_PRESET_ADD))
    tg.cbq_handler(edit_preset, _cb(CBT_PRESET_EDIT))
    tg.cbq_handler(del_preset, _cb(CBT_PRESET_DEL))
    tg.cbq_handler(cycle_rule_preset, _cb(CBT_RULE_PRESET))
    # v1.3.0: оверрайды
    tg.cbq_handler(open_overrides, _cb(CBT_TAB_OVERRIDES))
    tg.cbq_handler(ovr_force_off, _cb(CBT_OVR_FORCE_OFF))
    tg.cbq_handler(ovr_skip_rule, _cb(CBT_OVR_SKIP_RULE))
    tg.cbq_handler(ovr_cancel, _cb(CBT_OVR_CANCEL))
    tg.cbq_handler(lambda c: bot.answer_callback_query(c.id), _cb(f"{CBT_PREFIX}:NOOP"))

    tg.msg_handler(on_rule, func=lambda m: (tg.get_state(m.chat.id, m.from_user.id) or {}).get("state") == STATE_AWAIT_RULE)
    tg.msg_handler(on_interval, func=lambda m: (tg.get_state(m.chat.id, m.from_user.id) or {}).get("state") == STATE_AWAIT_INTERVAL)
    tg.msg_handler(on_exclude, func=lambda m: (tg.get_state(m.chat.id, m.from_user.id) or {}).get("state") == STATE_AWAIT_EXCLUDE)
    tg.msg_handler(on_include, func=lambda m: (tg.get_state(m.chat.id, m.from_user.id) or {}).get("state") == STATE_AWAIT_INCLUDE)
    tg.msg_handler(on_import, func=lambda m: (tg.get_state(m.chat.id, m.from_user.id) or {}).get("state") == STATE_AWAIT_IMPORT)
    tg.msg_handler(on_timezone, func=lambda m: (tg.get_state(m.chat.id, m.from_user.id) or {}).get("state") == STATE_AWAIT_TIMEZONE)
    tg.msg_handler(on_preset_name, func=lambda m: (tg.get_state(m.chat.id, m.from_user.id) or {}).get("state") == STATE_AWAIT_PRESET_NAME)

    def cmd_open(m: Message) -> None:
        _capture_chat(m.chat.id)
        bot.send_message(m.chat.id, _stats(_load_config()), reply_markup=_kb_main(), parse_mode="HTML")

    tg.msg_handler(cmd_open, commands=["trade_manager"])

    # /tm_guide — гайд
    def cmd_guide(m: Message) -> None:
        guide_text = (
            "<b>📖 TradeManager — Гайд</b>\n\n"
            "<b>Что делает:</b>\n"
            "Управление лотами по расписанию. В заданное временное окно "
            "лоты выключаются, вне окна — включаются автоматически.\n\n"
            "<b>Настройка:</b>\n"
            "1. /trade_manager → 📋 Правила → ➕ Добавить правило\n"
            "2. Формат: <code>Название | HH:MM | HH:MM | UTC_offset</code>\n"
            "3. Пример: <code>Ночное | 23:00 | 08:00 | 3</code>\n\n"
            "<b>Часовой пояс:</b>\n"
            "Глобальный часовой пояс задаётся в настройках (⚙️ Настройки → 🕐 Часовой пояс).\n"
            "Доступные пресеты: МСК/MSK (UTC+3), Киев/Kyiv/Київ (UTC+2), "
            "UTC, UTC+1 ... UTC+5, UTC-1 ... UTC-3.\n"
            "Если правило содержит свой utc_offset, он используется вместо глобального.\n\n"
            "<b>Включение/Выключение:</b>\n"
            "Плагин можно включить или выключить через ⚙️ Настройки.\n"
            "В выключенном состоянии фоновый цикл не изменяет лоты.\n\n"
            "<b>Как работает:</b>\n"
            "• Фоновый цикл проверяет правила каждые N секунд\n"
            "• Если текущее время внутри окна — лоты ВЫКЛЮЧАЮТСЯ\n"
            "• Вне окна — лоты ВКЛЮЧАЮТСЯ обратно\n"
            "• Можно исключить определённые lot_id\n\n"
            "<b>Функции:</b>\n"
            "• Несколько правил одновременно\n"
            "• Перенос через полночь (23:00 → 08:00)\n"
            "• 📅 Дни недели — правило действует только в выбранные дни\n"
            "• 🔘 Выбор лотов кнопками (пикер с ✅/⬜) + имена лотов\n"
            "• 🏷 Пресеты — именованный набор лотов для нескольких правил\n"
            "• ⏱ Разовые: «выключить до утра» и «пропуск правила сегодня»\n"
            "• 🔔 Уведомления в Telegram при переключении лотов\n"
            "• 🎯 «Только эти лоты» / 🚫 Исключения по lot_id\n"
            "• Экспорт/импорт правил в JSON\n"
            "• Тест правила (какие лоты затронутся)\n"
            "• Настройка интервала проверки\n"
            "• Глобальный часовой пояс (МСК, Київ и др.)\n"
            "• Быстрое включение/выключение плагина\n\n"
            "<b>Команды:</b>\n"
            "/trade_manager — главное меню\n"
            "/tm_guide — этот гайд\n"
            "/tm_test — проверка работы"
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
                logger.error("trade_manager: cmd_guide failed", exc_info=True)

    tg.msg_handler(cmd_guide, commands=["tm_guide"])

    # /tm_test — тест: проверяет правила и показывает текущее состояние
    def cmd_test(m: Message) -> None:
        cfg2 = _load_config()
        rules = cfg2.get("rules", [])
        if not rules:
            bot.send_message(
                m.chat.id,
                "❌ <b>Тест:</b> нет правил. Добавьте через /trade_manager → 📋 Правила.",
                parse_mode="HTML",
            )
            return

        lines = ["✅ <b>TradeManager — Тест правил</b>\n"]
        active_rules = [r for r in rules if r.get("active", True)]
        any_active_window = False

        for i, r in enumerate(rules):
            is_active = r.get("active", True)
            in_win = _in_window(r) if is_active else False
            if in_win:
                any_active_window = True
            status = "🟢" if is_active else "🔴"
            window = "⚡ В ОКНЕ" if in_win else "💤 вне окна"
            lines.append(
                f"  {status} {i+1}. <b>{r.get('name', '?')}</b> "
                f"({r['start']}—{r['end']} UTC+{r.get('utc_offset', 3)}) "
                f"— {window}"
            )

        lines.append("")
        if any_active_window:
            lines.append("🔴 <b>Сейчас:</b> лоты должны быть ВЫКЛЮЧЕНЫ (окно активно)")
        else:
            lines.append("🟢 <b>Сейчас:</b> лоты должны быть ВКЛЮЧЕНЫ (вне окна)")

        lines.append(f"\n⏱ Интервал проверки: {cfg2['settings']['interval_sec']} сек")
        lines.append(f"📋 Правил: {len(active_rules)}/{len(rules)} активных")

        bot.send_message(m.chat.id, "\n".join(lines), parse_mode="HTML")

    tg.msg_handler(cmd_test, commands=["tm_test"])

    try:
        cardinal.add_telegram_commands(UUID, [
            ("trade_manager", "TradeManager: расписание лотов", True),
            ("tm_guide", "TradeManager: гайд", True),
            ("tm_test", "TradeManager: тест правил", True),
        ])
    except Exception:
        logger.exception("Не удалось зарегистрировать команду /trade_manager")

    _log("TradeManager инициализирован.")


BIND_TO_PRE_INIT = [_init]


def _on_delete(cardinal: "Cardinal", *_: Any) -> None:
    _stop_event.set()
    _log("TradeManager удалён.")


BIND_TO_DELETE = _on_delete


def open_settings_page(cardinal: "Cardinal", msg: Any) -> None:
    """Called by FPC when user clicks plugin settings button."""
    tg = getattr(cardinal, "telegram", None)
    if tg is None:
        return
    cfg = _load_config()
    status = "🟢 Включен" if cfg.get("enabled", True) else "🔴 Выключен"
    tz = cfg["settings"].get("timezone", "UTC+3")
    rules_count = len(cfg.get("rules", []))
    text = (
        f"<b>TradeManager</b>\n\n"
        f"Статус: {status}\n"
        f"Часовой пояс: <b>{tz}</b>\n"
        f"Правил: <b>{rules_count}</b>\n\n"
        f"Используйте /trade_manager для полного меню."
    )
    tg.bot.send_message(msg.chat.id, text, parse_mode="HTML")


BIND_TO_SETTINGS_PAGE = open_settings_page



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
