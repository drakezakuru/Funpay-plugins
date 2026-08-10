"""
Email Code Fetcher — плагин для FunPay Cardinal.

Получение кодов верификации из email (IMAP) по команде в чате FunPay.
Управление через Telegram ПУ FPC (inline-меню).

Положите файл в папку plugins/ FunPay Cardinal и перезапустите бота.
"""
from __future__ import annotations

import datetime
import email
import imaplib
import json
import logging
import os
import poplib
import re
import threading
import time
from email.header import decode_header
from typing import TYPE_CHECKING, Any

import requests

from telebot.types import CallbackQuery, InlineKeyboardButton as B, InlineKeyboardMarkup as K, Message

if TYPE_CHECKING:
    from cardinal import Cardinal
    from FunPayAPI.updater.events import NewMessageEvent

# ---------- мета плагина (требуется FPC) ----------
NAME = "Email Code Fetcher"
VERSION = "1.0.0"
DESCRIPTION = (
    "Получение кодов верификации из email по команде в чате FunPay. "
    "Поддерживает IMAP, POP3 и Gmail OAuth, несколько ящиков "
    "с разными фильтрами и доступом по списку чатов."
)
CREDITS = "@drakelovc"
UUID = "3442ec3f-4eb4-4388-aefd-6f9ccddc3a02"
SETTINGS_PAGE = True

logger = logging.getLogger("FPC.plugin.email_code_fetcher")

# ---------- константы ----------
PLUGIN_DIR = os.path.join("storage", "plugins", "email_code_fetcher")
CONFIG_PATH = os.path.join(PLUGIN_DIR, "config.json")
LOG_PATH = os.path.join(PLUGIN_DIR, "log.txt")
LAST_CODE_PATH = os.path.join(PLUGIN_DIR, "last_codes.json")
MAX_LOG_LINES = 200

# CBT-префикс плагина: ECF (уникален, чтобы не конфликтовать с встроенными callback'ами)
CBT_PREFIX = "ECF"
CBT_OPEN = f"{CBT_PREFIX}:O"               # главное меню
CBT_TAB_ACCOUNTS = f"{CBT_PREFIX}:T:A"
CBT_TAB_SETTINGS = f"{CBT_PREFIX}:T:S"
CBT_TAB_ACCESS = f"{CBT_PREFIX}:T:C"
CBT_TAB_LOGS = f"{CBT_PREFIX}:T:L"
CBT_ADD_ACCOUNT = f"{CBT_PREFIX}:A:ADD"
CBT_ADD_POP3 = f"{CBT_PREFIX}:A:ADP"
CBT_ADD_OAUTH = f"{CBT_PREFIX}:A:ADO"
CBT_DEL_ACCOUNT = f"{CBT_PREFIX}:A:DEL"    # +":<idx>"
CBT_TEST_ACCOUNT = f"{CBT_PREFIX}:A:TST"   # +":<idx>"
CBT_EDIT_SETTING = f"{CBT_PREFIX}:S:E"     # +":<key>"
CBT_TOGGLE_ALL_CHATS = f"{CBT_PREFIX}:S:TAC"
CBT_ADD_CHAT = f"{CBT_PREFIX}:C:ADD"
CBT_DEL_CHAT = f"{CBT_PREFIX}:C:DEL"       # +":<idx>"
CBT_CLEAR_LOGS = f"{CBT_PREFIX}:L:CLR"
CBT_TEST_REAL = f"{CBT_PREFIX}:TEST:REAL"
CBT_TEST_FAKE = f"{CBT_PREFIX}:TEST:FAKE"
CBT_NOOP = f"{CBT_PREFIX}:NOOP"
CBT_OPEN_PREFIX = f"{CBT_PREFIX}:S:PFX"          # подменю выбора префикса
CBT_SET_PREFIX = f"{CBT_PREFIX}:S:PFX:SET"       # +":<b64-prefix>" — установить
CBT_CUSTOM_PREFIX = f"{CBT_PREFIX}:S:PFX:CUS"    # запросить свой

STATE_AWAIT_ACCOUNT = f"{CBT_PREFIX}:S_ACC"
STATE_AWAIT_POP3 = f"{CBT_PREFIX}:S_POP"
STATE_AWAIT_OAUTH = f"{CBT_PREFIX}:S_OAU"
STATE_AWAIT_SETTING = f"{CBT_PREFIX}:S_SET"
STATE_AWAIT_CHAT = f"{CBT_PREFIX}:S_CHT"
STATE_AWAIT_PREFIX = f"{CBT_PREFIX}:S_PFX"

# Предустановленные фильтры (regex для извлечения кода).
FILTERS: dict[str, dict[str, str]] = {
    "Rockstar": {
        "from": "rockstargames.com",
        "subject": "",
        "code": r"\b(\d{6})\b",
    },
    "Steam": {
        "from": "steampowered.com",
        "subject": "",
        "code": r"\b([A-Z0-9]{5})\b",
    },
    "EA": {
        "from": "ea.com",
        "subject": "",
        "code": r"\b(\d{6})\b",
    },
    "Epic Games": {
        "from": "epicgames.com",
        "subject": "",
        "code": r"\b(\d{6})\b",
    },
    "Ubisoft": {
        "from": "ubisoft.com",
        "subject": "",
        "code": r"\b(\d{6})\b",
    },
    "Microsoft": {
        "from": "microsoft.com",
        "subject": "",
        "code": r"\b(\d{6,7})\b",
    },
    "Blizzard": {
        "from": "blizzard.com",
        "subject": "",
        "code": r"\b(\d{6})\b",
    },
    "Discord": {
        "from": "discord.com",
        "subject": "",
        "code": r"\b(\d{6})\b",
    },
    "Twitch": {
        "from": "twitch.tv",
        "subject": "",
        "code": r"\b(\d{6})\b",
    },
    "Amazon": {
        "from": "amazon.com",
        "subject": "",
        "code": r"\b(\d{6})\b",
    },
    "Riot Games": {
        "from": "riotgames.com",
        "subject": "",
        "code": r"\b(\d{6})\b",
    },
    "PlayStation": {
        "from": "playstation.com",
        "subject": "",
        "code": r"\b(\d{6})\b",
    },
    "Xbox": {
        "from": "xbox.com",
        "subject": "",
        "code": r"\b(\d{6,7})\b",
    },
    "Google": {
        "from": "google.com",
        "subject": "",
        "code": r"\b(\d{6})\b",
    },
    "Facebook": {
        "from": "facebook.com",
        "subject": "",
        "code": r"\b(\d{6,8})\b",
    },
    "Instagram": {
        "from": "instagram.com",
        "subject": "",
        "code": r"\b(\d{6})\b",
    },
    "Telegram": {
        "from": "telegram.org",
        "subject": "",
        "code": r"\b(\d{5})\b",
    },
    "Custom": {
        "from": "",
        "subject": "",
        "code": r"\b(\d{4,8})\b",
    },
}

DEFAULT_CONFIG: dict[str, Any] = {
    "accounts": [],          # [{email, app_password, imap_host, imap_port, filter, command}]
    "permitted_chats": [],   # [chat_id]
    "settings": {
        "search_window_min": 15,
        "cache_ttl_sec": 60,
        "rate_limit_sec": 10,
        "allow_all_chats": False,
        "command_prefix": "",  # "" = без префикса (обратная совместимость)
    },
}


# ---------- утилиты конфига и логов ----------
def _ensure_dir() -> None:
    os.makedirs(PLUGIN_DIR, exist_ok=True)


def _load_config() -> dict[str, Any]:
    _ensure_dir()
    if not os.path.exists(CONFIG_PATH):
        return json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        logger.exception("Не удалось прочитать config.json, использую значения по умолчанию.")
        return json.loads(json.dumps(DEFAULT_CONFIG))
    # merge defaults
    cfg.setdefault("accounts", [])
    cfg.setdefault("permitted_chats", [])
    s = cfg.setdefault("settings", {})
    for k, v in DEFAULT_CONFIG["settings"].items():
        s.setdefault(k, v)
    return cfg


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
        logger.exception("Не удалось записать лог.")


def _read_logs() -> str:
    if not os.path.exists(LOG_PATH):
        return "Логи пусты."
    with open(LOG_PATH, "r", encoding="utf-8") as f:
        data = f.read().strip()
    return data or "Логи пусты."


def _load_last_codes() -> dict:
    if not os.path.exists(LAST_CODE_PATH):
        return {}
    try:
        with open(LAST_CODE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_last_codes(data: dict) -> None:
    _ensure_dir()
    try:
        tmp = LAST_CODE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, LAST_CODE_PATH)
    except Exception:
        logger.exception("Failed to save last_codes.json")


def _fmt_time_msk(ts: float) -> str:
    """Format timestamp as HH:MM:SS in Moscow time (UTC+3)."""
    import datetime as _dt
    tz_msk = _dt.timezone(_dt.timedelta(hours=3))
    dt = _dt.datetime.fromtimestamp(ts, tz=tz_msk)
    return dt.strftime("%H:%M:%S")


# ---------- префиксы команд ----------
PREFIX_PRESETS: list[str] = ["", "!", "/", ".", "#", "?"]
PREFIX_MAX_LEN = 4  # максимальная длина кастомного префикса


def _prefix_label(p: str) -> str:
    """Человекочитаемая метка префикса для UI."""
    p = p or ""
    return "(нет)" if p == "" else p


def _normalize_prefix(raw: str) -> str | None:
    """Нормализует пользовательский ввод префикса. Возвращает None если невалиден."""
    if raw is None:
        return None
    p = raw.strip()
    # Поддержим явное «нет»: '-', '—', 'нет', 'none', 'off', '""', "''"
    if p.lower() in ("-", "—", "нет", "none", "off", '""', "''", "(нет)"):
        return ""
    # Без пробелов внутри, не длиннее PREFIX_MAX_LEN
    if " " in p or "\n" in p or "\t" in p:
        return None
    if len(p) > PREFIX_MAX_LEN:
        return None
    return p


# ---------- хосты IMAP/POP3 ----------
_HOST_MAP_IMAP: list[tuple[tuple[str, ...], tuple[str, int]]] = [
    (("@gmail.com", "@googlemail.com"), ("imap.gmail.com", 993)),
    (("@yahoo.com",), ("imap.mail.yahoo.com", 993)),
    (("@outlook.com", "@hotmail.com", "@live.com"), ("outlook.office365.com", 993)),
    (("@yandex.ru", "@yandex.com"), ("imap.yandex.com", 993)),
    (("@mail.ru", "@bk.ru", "@inbox.ru", "@list.ru"), ("imap.mail.ru", 993)),
]
_HOST_MAP_POP3: list[tuple[tuple[str, ...], tuple[str, int]]] = [
    (("@gmail.com", "@googlemail.com"), ("pop.gmail.com", 995)),
    (("@yahoo.com",), ("pop.mail.yahoo.com", 995)),
    (("@outlook.com", "@hotmail.com", "@live.com"), ("outlook.office365.com", 995)),
    (("@yandex.ru", "@yandex.com"), ("pop.yandex.com", 995)),
    (("@mail.ru", "@bk.ru", "@inbox.ru", "@list.ru"), ("pop.mail.ru", 995)),
]


def _guess_imap_host(addr: str) -> tuple[str, int]:
    addr = addr.lower().strip()
    for suffixes, hp in _HOST_MAP_IMAP:
        if any(addr.endswith(s) for s in suffixes):
            return hp
    return "imap.gmail.com", 993


def _guess_pop3_host(addr: str) -> tuple[str, int]:
    addr = addr.lower().strip()
    for suffixes, hp in _HOST_MAP_POP3:
        if any(addr.endswith(s) for s in suffixes):
            return hp
    return "pop.gmail.com", 995


# ---------- OAuth (Gmail) ----------
GMAIL_TOKEN_URL = "https://oauth2.googleapis.com/token"


def _oauth_access_token(client_id: str, client_secret: str, refresh_token: str) -> str | None:
    """Меняет refresh_token на свежий access_token у Google."""
    try:
        r = requests.post(
            GMAIL_TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=15,
        )
        if r.status_code != 200:
            _log(f"OAuth refresh HTTP {r.status_code}: {r.text[:200]}")
            return None
        return r.json().get("access_token")
    except Exception as ex:
        _log(f"OAuth refresh fail: {ex}")
        return None


def _xoauth2_string(addr: str, access_token: str) -> bytes:
    raw = f"user={addr}\x01auth=Bearer {access_token}\x01\x01"
    return raw.encode("utf-8")


def _decode(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", errors="replace")
        except Exception:
            return value.decode("latin-1", errors="replace")
    parts = []
    for fragment, enc in decode_header(str(value)):
        if isinstance(fragment, bytes):
            try:
                parts.append(fragment.decode(enc or "utf-8", errors="replace"))
            except Exception:
                parts.append(fragment.decode("latin-1", errors="replace"))
        else:
            parts.append(fragment)
    return "".join(parts)


def _message_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        text_parts: list[str] = []
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype in ("text/plain", "text/html"):
                payload = part.get_payload(decode=True)
                if payload:
                    text_parts.append(_decode(payload))
        return "\n".join(text_parts)
    payload = msg.get_payload(decode=True)
    return _decode(payload) if payload else ""


def _msg_within_window(msg: email.message.Message, window_min: int) -> bool:
    try:
        tup = email.utils.parsedate_tz(msg["Date"])
        msg_ts = email.utils.mktime_tz(tup) if tup else 0
    except Exception:
        msg_ts = 0
    if not msg_ts:
        return True
    return (time.time() - msg_ts) <= window_min * 60


def _scan_for_code(msg: email.message.Message, flt: dict[str, str]) -> str | None:
    body = _message_body(msg)
    subj = _decode(msg.get("Subject", ""))
    sender = _decode(msg.get("From", "")).lower()
    if flt.get("from") and flt["from"].lower() not in sender:
        return None
    if flt.get("subject") and flt["subject"].lower() not in subj.lower():
        return None
    m = re.search(flt["code"], f"{subj}\n{body}")
    return m.group(1) if m else None


def _fetch_imap(account: dict, flt: dict, window_min: int,
                login_func) -> str | None:
    addr = account["email"]
    host = account.get("imap_host") or _guess_imap_host(addr)[0]
    port = int(account.get("imap_port") or _guess_imap_host(addr)[1])
    try:
        mail = imaplib.IMAP4_SSL(host, port, timeout=15)
        login_func(mail)
    except Exception as ex:
        _log(f"[{addr}] IMAP login fail ({type(ex).__name__}): {ex}")
        return None
    try:
        mail.select("INBOX", readonly=True)
        since = time.gmtime(time.time() - 86400)
        date_str = time.strftime("%d-%b-%Y", since)
        criteria = [f'(SINCE "{date_str}")']
        if flt.get("from"):
            criteria.append(f'(FROM "{flt["from"]}")')
        if flt.get("subject"):
            criteria.append(f'(SUBJECT "{flt["subject"]}")')
        query = " ".join(criteria)
        status, data = mail.search(None, query)
        if status != "OK" or not data or not data[0]:
            _log(f"[{addr}] IMAP: пустой результат поиска (status={status}, criteria={query})")
            return None
        ids = data[0].split()
        for num in reversed(ids[-20:]):
            status, msg_data = mail.fetch(num, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            if not _msg_within_window(msg, window_min):
                continue
            code = _scan_for_code(msg, flt)
            if code:
                return code
        return None
    except Exception as ex:
        _log(f"[{addr}] IMAP search fail ({type(ex).__name__}): {ex}")
        return None
    finally:
        try:
            mail.logout()
        except Exception:
            pass


def _fetch_pop3(account: dict, flt: dict, window_min: int) -> str | None:
    addr = account["email"]
    pwd = account["app_password"]
    host = account.get("pop3_host") or _guess_pop3_host(addr)[0]
    port = int(account.get("pop3_port") or _guess_pop3_host(addr)[1])
    try:
        srv = poplib.POP3_SSL(host, port, timeout=20)
        srv.user(addr)
        srv.pass_(pwd)
    except Exception as ex:
        _log(f"[{addr}] POP3 login fail ({type(ex).__name__}): {ex}")
        return None
    try:
        count, _ = srv.stat()
        # последние 20 писем (POP3 не умеет SEARCH)
        start = max(1, count - 19)
        for num in range(count, start - 1, -1):
            try:
                _, raw_lines, _sz = srv.retr(num)
            except Exception:
                continue
            msg = email.message_from_bytes(b"\r\n".join(raw_lines))
            if not _msg_within_window(msg, window_min):
                continue
            code = _scan_for_code(msg, flt)
            if code:
                return code
        _log(f"[{addr}] POP3: просканировано {count - start + 1} писем, код не найден (filter={flt.get('from', 'any')})")
        return None
    except Exception as ex:
        _log(f"[{addr}] POP3 search fail: {ex}")
        return None
    finally:
        try:
            srv.quit()
        except Exception:
            pass


def fetch_code(account: dict[str, Any], window_min: int) -> str | None:
    """Достаёт код. Выбирает IMAP / POP3 / OAUTH-IMAP по полю account['protocol']."""
    addr = account["email"]
    proto = (account.get("protocol") or "IMAP").upper()
    flt_name = account.get("filter", "Custom")
    flt = FILTERS.get(flt_name, FILTERS["Custom"])

    if proto == "POP3":
        return _fetch_pop3(account, flt, window_min)

    if proto in ("OAUTH", "OAUTH_GMAIL"):
        cid = account.get("oauth_client_id") or ""
        csec = account.get("oauth_client_secret") or ""
        rt = account.get("oauth_refresh_token") or ""
        if not all((cid, csec, rt)):
            _log(f"[{addr}] OAuth: не заполнены client_id/secret/refresh_token.")
            return None
        access = _oauth_access_token(cid, csec, rt)
        if not access:
            return None

        def _login(mail: imaplib.IMAP4_SSL) -> None:
            mail.authenticate("XOAUTH2", lambda _: _xoauth2_string(addr, access))

        return _fetch_imap(account, flt, window_min, _login)

    # IMAP (default)
    pwd = account.get("app_password") or ""

    def _login(mail: imaplib.IMAP4_SSL) -> None:
        mail.login(addr, pwd)

    return _fetch_imap(account, flt, window_min, _login)


# ---------- состояние плагина в памяти ----------
class _Runtime:
    cache: dict[str, tuple[str, float]] = {}    # email -> (code, ts)
    last_request: dict[tuple[int, str], float] = {}  # (chat_id, email) -> ts
    lock = threading.Lock()


# ---------- BIND_TO_PRE_INIT: регистрация TG-обработчиков ----------
def _init(cardinal: "Cardinal", *_: Any) -> None:
    _ensure_dir()
    cfg = _load_config()
    _save_config(cfg)

    tg = getattr(cardinal, "telegram", None)
    if tg is None:
        logger.info("Telegram ПУ отключена — настроек через TG не будет, только команды в чате.")
        return

    bot = tg.bot

    # ----- рендеринг меню -----
    def _kb_main() -> K:
        kb = K()
        kb.add(
            B("📧 Аккаунты", callback_data=CBT_TAB_ACCOUNTS),
            B("⚙️ Настройки", callback_data=CBT_TAB_SETTINGS),
        )
        kb.add(
            B("🔑 Доступ", callback_data=CBT_TAB_ACCESS),
            B("📜 Логи", callback_data=CBT_TAB_LOGS),
        )
        return kb

    def _text_main() -> str:
        c = _load_config()
        access_mode = "все чаты" if c["settings"].get("allow_all_chats", False) else "по списку"
        warning = ""
        if not c["settings"].get("allow_all_chats", False) and not c["permitted_chats"]:
            warning = (
                "\n\n⚠️ <b>Внимание:</b> доступ по списку, но список чатов пуст. "
                "Ни один чат не имеет доступа к командам. Добавьте чаты в список "
                "или включите режим \"Все чаты\" в настройках."
            )
        return (
            "<b>Email Code Fetcher</b>\n"
            "Получение кодов верификации из email (IMAP) по команде в чате FunPay.\n\n"
            f"📧 Аккаунтов: <b>{len(c['accounts'])}</b>\n"
            f"🔑 Чатов с доступом: <b>{len(c['permitted_chats'])}</b>\n"
            f"🌐 Доступ: <b>{access_mode}</b>\n"
            f"⌨️ Префикс команд: <b>{_prefix_label(c['settings'].get('command_prefix', ''))}</b>\n"
            f"⏱ Окно поиска: <b>{c['settings']['search_window_min']} мин</b>\n"
            f"🧠 TTL кэша: <b>{c['settings']['cache_ttl_sec']} сек</b>\n"
            f"🐢 Rate limit: <b>{c['settings']['rate_limit_sec']} сек</b>"
            f"{warning}"
        )

    def _render(c: CallbackQuery, text: str, kb: K) -> None:
        try:
            bot.edit_message_text(text, c.message.chat.id, c.message.id, reply_markup=kb, parse_mode="HTML")
        except Exception:
            bot.send_message(c.message.chat.id, text, reply_markup=kb, parse_mode="HTML")

    def open_main(c: CallbackQuery) -> None:
        _render(c, _text_main(), _kb_main())
        bot.answer_callback_query(c.id)

    def open_accounts(c: CallbackQuery) -> None:
        cfg2 = _load_config()
        prefix = cfg2["settings"].get("command_prefix", "")
        if not cfg2["accounts"]:
            text = "<b>Email-аккаунты</b>\n\nПока ничего не подключено."
        else:
            lines = ["<b>Email-аккаунты</b>\n"]
            for i, acc in enumerate(cfg2["accounts"]):
                proto = (acc.get("protocol") or "IMAP").upper()
                cmd_raw = (acc.get("command") or "").strip()
                cmd_display = f"{prefix}{cmd_raw}" if cmd_raw else "(нет)"
                lines.append(
                    f"<code>{i + 1}</code>. <b>{acc['email']}</b> "
                    f"<i>[{proto}]</i>\n"
                    f"    Фильтр: <i>{acc.get('filter', 'Custom')}</i>"
                    f" | Команда: <code>{cmd_display}</code>"
                )
            text = "\n".join(lines)
        kb = K()
        for i, acc in enumerate(cfg2["accounts"]):
            kb.add(
                B(f"🧪 Тест {acc['email']}", callback_data=f"{CBT_TEST_ACCOUNT}:{i}"),
                B("🗑", callback_data=f"{CBT_DEL_ACCOUNT}:{i}"),
            )
        kb.add(B("➕ IMAP аккаунт", callback_data=CBT_ADD_ACCOUNT))
        kb.add(B("➕ POP3 аккаунт", callback_data=CBT_ADD_POP3))
        kb.add(B("➕ Gmail OAuth", callback_data=CBT_ADD_OAUTH))
        kb.add(B("⬅️ Назад", callback_data=CBT_OPEN))
        _render(c, text, kb)
        bot.answer_callback_query(c.id)

    def open_settings(c: CallbackQuery) -> None:
        s = _load_config()["settings"]
        access_label = "все чаты" if s.get("allow_all_chats", False) else "по списку"
        prefix_label = _prefix_label(s.get("command_prefix", ""))
        text = (
            "<b>Параметры поиска</b>\n\n"
            f"⏱ Окно поиска (минуты): <b>{s['search_window_min']}</b>\n"
            f"🧠 TTL кэша (секунды): <b>{s['cache_ttl_sec']}</b>\n"
            f"🐢 Rate limit (секунды): <b>{s['rate_limit_sec']}</b>\n"
            f"🌐 Доступ: <b>{access_label}</b>\n"
            f"⌨️ Префикс команд: <b>{prefix_label}</b>"
        )
        kb = K()
        kb.add(B("⏱ Окно поиска", callback_data=f"{CBT_EDIT_SETTING}:search_window_min"))
        kb.add(B("🧠 TTL кэша", callback_data=f"{CBT_EDIT_SETTING}:cache_ttl_sec"))
        kb.add(B("🐢 Rate limit", callback_data=f"{CBT_EDIT_SETTING}:rate_limit_sec"))
        toggle_text = "\U0001f310 Все чаты" if s.get("allow_all_chats", False) else "\U0001f512 По списку"
        kb.add(B(toggle_text, callback_data=CBT_TOGGLE_ALL_CHATS))
        kb.add(B(f"⌨️ Префикс команд: {prefix_label}", callback_data=CBT_OPEN_PREFIX))
        kb.add(B("⬅️ Назад", callback_data=CBT_OPEN))
        _render(c, text, kb)
        bot.answer_callback_query(c.id)

    def open_access(c: CallbackQuery) -> None:
        cfg2 = _load_config()
        text = "<b>Постоянный доступ (чаты)</b>\n\n"
        if not cfg2["permitted_chats"]:
            text += "Чатов с доступом не настроено."
        else:
            text += "\n".join(f"<code>{i + 1}</code>. <code>{cid}</code>"
                              for i, cid in enumerate(cfg2["permitted_chats"]))
        kb = K()
        for i, cid in enumerate(cfg2["permitted_chats"]):
            kb.add(B(f"🗑 {cid}", callback_data=f"{CBT_DEL_CHAT}:{i}"))
        kb.add(B("➕ Добавить Chat ID", callback_data=CBT_ADD_CHAT))
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
        try:
            if os.path.exists(LOG_PATH):
                os.remove(LOG_PATH)
        except Exception:
            pass
        open_logs(c)

    # ----- ввод значений -----
    def ask_add_account(c: CallbackQuery) -> None:
        filters = ", ".join(FILTERS.keys())
        result = bot.send_message(
            c.message.chat.id,
            "Отправь новый аккаунт одной строкой:\n"
            "<code>email | app_password | filter | command</code>\n"
            f"Доступные фильтры: <i>{filters}</i>. Пример:\n"
            "<code>user@gmail.com | xxxx xxxx xxxx xxxx | Rockstar | rck</code>",
            parse_mode="HTML",
        )
        tg.set_state(c.message.chat.id, result.id, c.from_user.id, STATE_AWAIT_ACCOUNT)
        bot.answer_callback_query(c.id)

    def on_add_account(m: Message) -> None:
        tg.clear_state(m.chat.id, m.from_user.id, True)
        parts = [p.strip() for p in (m.text or "").split("|")]
        if len(parts) < 3:
            bot.send_message(m.chat.id, "Неверный формат. Нужно минимум 3 поля, разделённых '|'.")
            return
        email_addr, app_pwd, flt = parts[0], parts[1], parts[2]
        command = parts[3] if len(parts) >= 4 else ""
        if flt not in FILTERS:
            bot.send_message(m.chat.id, f"Неизвестный фильтр '{flt}'. Доступные: {', '.join(FILTERS.keys())}.")
            return
        host, port = _guess_imap_host(email_addr)
        cfg2 = _load_config()
        cfg2["accounts"].append({
            "email": email_addr,
            "protocol": "IMAP",
            "app_password": app_pwd,
            "imap_host": host,
            "imap_port": port,
            "filter": flt,
            "command": command,
        })
        _save_config(cfg2)
        _log(f"Добавлен email-аккаунт {email_addr} (IMAP, {flt})")
        bot.send_message(m.chat.id, f"✅ IMAP-аккаунт <b>{email_addr}</b> добавлен.", parse_mode="HTML")

    def ask_add_pop3(c: CallbackQuery) -> None:
        filters = ", ".join(FILTERS.keys())
        result = bot.send_message(
            c.message.chat.id,
            "Отправь POP3-аккаунт одной строкой:\n"
            "<code>email | password | filter | command</code>\n"
            f"Доступные фильтры: <i>{filters}</i>. Пример:\n"
            "<code>user@mail.ru | secret | Rockstar | rck</code>\n\n"
            "POP3 включи в настройках почты у провайдера, "
            "хост угадывается автоматически.",
            parse_mode="HTML",
        )
        tg.set_state(c.message.chat.id, result.id, c.from_user.id, STATE_AWAIT_POP3)
        bot.answer_callback_query(c.id)

    def on_add_pop3(m: Message) -> None:
        tg.clear_state(m.chat.id, m.from_user.id, True)
        parts = [p.strip() for p in (m.text or "").split("|")]
        if len(parts) < 3:
            bot.send_message(m.chat.id, "Неверный формат. Нужно минимум 3 поля.")
            return
        email_addr, pwd, flt = parts[0], parts[1], parts[2]
        command = parts[3] if len(parts) >= 4 else ""
        if flt not in FILTERS:
            bot.send_message(m.chat.id, f"Неизвестный фильтр '{flt}'.")
            return
        host, port = _guess_pop3_host(email_addr)
        cfg2 = _load_config()
        cfg2["accounts"].append({
            "email": email_addr,
            "protocol": "POP3",
            "app_password": pwd,
            "pop3_host": host,
            "pop3_port": port,
            "filter": flt,
            "command": command,
        })
        _save_config(cfg2)
        _log(f"Добавлен email-аккаунт {email_addr} (POP3, {flt})")
        bot.send_message(m.chat.id, f"✅ POP3-аккаунт <b>{email_addr}</b> добавлен.", parse_mode="HTML")

    def ask_add_oauth(c: CallbackQuery) -> None:
        filters = ", ".join(FILTERS.keys())
        result = bot.send_message(
            c.message.chat.id,
            "Отправь Gmail OAuth-аккаунт одной строкой:\n"
            "<code>email | client_id | client_secret | refresh_token | filter | command</code>\n\n"
            f"Доступные фильтры: <i>{filters}</i>.\n"
            "Как получить refresh_token:\n"
            "1) Создай OAuth Client (Desktop App) в Google Cloud Console.\n"
            "2) Включи Gmail API.\n"
            "3) Получи refresh_token через oauth2l / "
            "<a href=\"https://developers.google.com/oauthplayground\">"
            "OAuth Playground</a> (scope <code>https://mail.google.com/</code>).",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        tg.set_state(c.message.chat.id, result.id, c.from_user.id, STATE_AWAIT_OAUTH)
        bot.answer_callback_query(c.id)

    def on_add_oauth(m: Message) -> None:
        tg.clear_state(m.chat.id, m.from_user.id, True)
        parts = [p.strip() for p in (m.text or "").split("|")]
        if len(parts) < 5:
            bot.send_message(m.chat.id,
                             "Неверный формат. Нужно минимум 5 полей: "
                             "email | client_id | client_secret | refresh_token | filter | [command]")
            return
        email_addr, cid, csec, rt, flt = parts[0], parts[1], parts[2], parts[3], parts[4]
        command = parts[5] if len(parts) >= 6 else ""
        if flt not in FILTERS:
            bot.send_message(m.chat.id, f"Неизвестный фильтр '{flt}'.")
            return
        host, port = _guess_imap_host(email_addr)
        cfg2 = _load_config()
        cfg2["accounts"].append({
            "email": email_addr,
            "protocol": "OAUTH",
            "oauth_client_id": cid,
            "oauth_client_secret": csec,
            "oauth_refresh_token": rt,
            "imap_host": host,
            "imap_port": port,
            "filter": flt,
            "command": command,
        })
        _save_config(cfg2)
        _log(f"Добавлен email-аккаунт {email_addr} (OAUTH, {flt})")
        bot.send_message(m.chat.id, f"✅ OAuth-аккаунт <b>{email_addr}</b> добавлен.", parse_mode="HTML")

    def del_account(c: CallbackQuery) -> None:
        idx = int(c.data.split(":")[-1])
        cfg2 = _load_config()
        if 0 <= idx < len(cfg2["accounts"]):
            removed = cfg2["accounts"].pop(idx)
            _save_config(cfg2)
            _log(f"Удалён email-аккаунт {removed['email']}")
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
            code = fetch_code(acc, cfg2["settings"]["search_window_min"])
            if code:
                ts_str = _fmt_time_msk(time.time())
                bot.send_message(c.message.chat.id,
                                 f"✅ <b>{acc['email']}</b>: код <code>{code}</code> (получен в {ts_str})",
                                 parse_mode="HTML")
            else:
                # Try to get last cached code
                cached = _Runtime.cache.get(acc["email"])
                if not cached:
                    lc = _load_last_codes()
                    entry = lc.get(acc["email"])
                    if entry:
                        cached = (entry["code"], entry["ts"])
                if cached:
                    ts_str = _fmt_time_msk(cached[1])
                    bot.send_message(c.message.chat.id,
                                     f"⚠️ <b>{acc['email']}</b>: новый код не найден. "
                                     f"Последний: <code>{cached[0]}</code> (получен в {ts_str})",
                                     parse_mode="HTML")
                else:
                    bot.send_message(c.message.chat.id,
                                     f"⚠️ <b>{acc['email']}</b>: код не найден (или ошибка IMAP). См. логи.",
                                     parse_mode="HTML")

        threading.Thread(target=_worker, daemon=True).start()

    def toggle_all_chats(c: CallbackQuery) -> None:
        cfg2 = _load_config()
        current = cfg2["settings"].get("allow_all_chats", False)
        cfg2["settings"]["allow_all_chats"] = not current
        _save_config(cfg2)
        new_state = "все чаты" if not current else "по списку"
        _log(f"Переключён доступ: {new_state}")
        open_settings(c)

    def ask_edit_setting(c: CallbackQuery) -> None:
        key = c.data.split(":")[-1]
        labels = {
            "search_window_min": "Окно поиска (минуты)",
            "cache_ttl_sec": "TTL кэша (секунды)",
            "rate_limit_sec": "Rate limit (секунды)",
        }
        result = bot.send_message(c.message.chat.id,
                                  f"Введи новое значение для «{labels.get(key, key)}» (целое число):")
        tg.set_state(c.message.chat.id, result.id, c.from_user.id, STATE_AWAIT_SETTING, {"key": key})
        bot.answer_callback_query(c.id)

    def on_setting(m: Message) -> None:
        state = tg.get_state(m.chat.id, m.from_user.id)
        if not state:
            return
        key = state["data"]["key"]
        tg.clear_state(m.chat.id, m.from_user.id, True)
        try:
            val = int((m.text or "").strip())
            if val < 0:
                raise ValueError
        except ValueError:
            bot.send_message(m.chat.id, "Нужно неотрицательное целое число.")
            return
        cfg2 = _load_config()
        cfg2["settings"][key] = val
        _save_config(cfg2)
        _log(f"Изменена настройка {key}={val}")
        bot.send_message(m.chat.id, f"✅ Настройка <b>{key}</b> = <b>{val}</b>", parse_mode="HTML")

    # ----- подменю выбора префикса команд -----
    def open_prefix_menu(c: CallbackQuery) -> None:
        cfg2 = _load_config()
        current = cfg2["settings"].get("command_prefix", "")
        text = (
            "<b>⌨️ Префикс команд</b>\n\n"
            f"Текущий префикс: <b>{_prefix_label(current)}</b>\n\n"
            "Покупатель должен начинать сообщение с этого префикса, "
            "иначе команда не сработает. Префикс <b>(нет)</b> = без префикса "
            "(покупатель пишет команду как есть).\n\n"
            "Пример: если команда <code>rck</code> и префикс <b>!</b>, "
            "покупатель пишет <code>!rck</code>."
        )
        kb = K()
        # пресеты — по 3 кнопки в ряд
        row: list = []
        for p in PREFIX_PRESETS:
            label = _prefix_label(p)
            mark = " ✓" if p == current else ""
            row.append(B(f"{label}{mark}", callback_data=f"{CBT_SET_PREFIX}:{p}"))
            if len(row) == 3:
                kb.row(*row)
                row = []
        if row:
            kb.row(*row)
        kb.add(B("✏️ Свой", callback_data=CBT_CUSTOM_PREFIX))
        kb.add(B("⬅️ Назад", callback_data=CBT_TAB_SETTINGS))
        _render(c, text, kb)
        bot.answer_callback_query(c.id)

    def set_prefix_cb(c: CallbackQuery) -> None:
        # data = "ECF:S:PFX:SET:<prefix>" — берём всё после префикса константы
        suffix = c.data[len(CBT_SET_PREFIX):]  # либо "" либо ":<prefix>"
        new_prefix = suffix[1:] if suffix.startswith(":") else ""
        new_prefix = _normalize_prefix(new_prefix) or ""
        cfg2 = _load_config()
        cfg2["settings"]["command_prefix"] = new_prefix
        _save_config(cfg2)
        _log(f"Изменён префикс команд: '{new_prefix}' (label={_prefix_label(new_prefix)})")
        try:
            bot.answer_callback_query(c.id, f"Префикс: {_prefix_label(new_prefix)}")
        except Exception:
            pass
        open_prefix_menu(c)

    def ask_custom_prefix(c: CallbackQuery) -> None:
        result = bot.send_message(
            c.message.chat.id,
            "Введи свой префикс (1–{n} символов, без пробелов).\n"
            "Чтобы убрать префикс — отправь <code>-</code> или <code>нет</code>.".format(n=PREFIX_MAX_LEN),
            parse_mode="HTML",
        )
        tg.set_state(c.message.chat.id, result.id, c.from_user.id, STATE_AWAIT_PREFIX)
        bot.answer_callback_query(c.id)

    def on_prefix(m: Message) -> None:
        tg.clear_state(m.chat.id, m.from_user.id, True)
        new_prefix = _normalize_prefix(m.text or "")
        if new_prefix is None:
            bot.send_message(
                m.chat.id,
                f"❌ Невалидный префикс. Допустимо до {PREFIX_MAX_LEN} символов без пробелов. "
                "Для отключения отправь <code>-</code>.",
                parse_mode="HTML",
            )
            return
        cfg2 = _load_config()
        cfg2["settings"]["command_prefix"] = new_prefix
        _save_config(cfg2)
        _log(f"Изменён префикс команд (custom): '{new_prefix}' (label={_prefix_label(new_prefix)})")
        bot.send_message(
            m.chat.id,
            f"✅ Префикс установлен: <b>{_prefix_label(new_prefix)}</b>",
            parse_mode="HTML",
        )

    def ask_add_chat(c: CallbackQuery) -> None:
        result = bot.send_message(c.message.chat.id, "Введи Chat ID FunPay-чата для постоянного доступа:")
        tg.set_state(c.message.chat.id, result.id, c.from_user.id, STATE_AWAIT_CHAT)
        bot.answer_callback_query(c.id)

    def on_chat(m: Message) -> None:
        tg.clear_state(m.chat.id, m.from_user.id, True)
        cid = (m.text or "").strip()
        if not cid.isdigit():
            bot.send_message(m.chat.id, "Chat ID должен быть числом.")
            return
        cfg2 = _load_config()
        if cid in cfg2["permitted_chats"]:
            bot.send_message(m.chat.id, "Уже в списке.")
            return
        cfg2["permitted_chats"].append(cid)
        _save_config(cfg2)
        _log(f"Добавлен permitted chat {cid}")
        bot.send_message(m.chat.id, f"✅ Чат <code>{cid}</code> добавлен.", parse_mode="HTML")

    def del_chat(c: CallbackQuery) -> None:
        idx = int(c.data.split(":")[-1])
        cfg2 = _load_config()
        if 0 <= idx < len(cfg2["permitted_chats"]):
            removed = cfg2["permitted_chats"].pop(idx)
            _save_config(cfg2)
            _log(f"Удалён permitted chat {removed}")
        open_access(c)

    # ----- регистрация коллбэков -----
    def _cb(prefix: str):
        return lambda c: c.data == prefix or c.data.startswith(prefix + ":")

    tg.cbq_handler(open_main, _cb(CBT_OPEN))
    # PLUGIN_SETTINGS:UUID:offset — кнопка «Настройки» в карточке плагина FPC
    tg.cbq_handler(open_main, lambda c: c.data.startswith(f"47:{UUID}"))

    tg.cbq_handler(open_accounts, _cb(CBT_TAB_ACCOUNTS))
    tg.cbq_handler(open_settings, _cb(CBT_TAB_SETTINGS))
    tg.cbq_handler(open_access, _cb(CBT_TAB_ACCESS))
    tg.cbq_handler(open_logs, _cb(CBT_TAB_LOGS))
    tg.cbq_handler(clear_logs, _cb(CBT_CLEAR_LOGS))
    tg.cbq_handler(ask_add_account, _cb(CBT_ADD_ACCOUNT))
    tg.cbq_handler(ask_add_pop3, _cb(CBT_ADD_POP3))
    tg.cbq_handler(ask_add_oauth, _cb(CBT_ADD_OAUTH))
    tg.cbq_handler(del_account, _cb(CBT_DEL_ACCOUNT))
    tg.cbq_handler(test_account, _cb(CBT_TEST_ACCOUNT))
    tg.cbq_handler(ask_edit_setting, _cb(CBT_EDIT_SETTING))
    tg.cbq_handler(toggle_all_chats, _cb(CBT_TOGGLE_ALL_CHATS))
    # Префикс: набор констант пересекается по startswith — используем exact match.
    tg.cbq_handler(open_prefix_menu, lambda c: c.data == CBT_OPEN_PREFIX)
    tg.cbq_handler(ask_custom_prefix, lambda c: c.data == CBT_CUSTOM_PREFIX)
    tg.cbq_handler(set_prefix_cb, lambda c: c.data.startswith(CBT_SET_PREFIX + ":") or c.data == CBT_SET_PREFIX)
    tg.cbq_handler(ask_add_chat, _cb(CBT_ADD_CHAT))
    tg.cbq_handler(del_chat, _cb(CBT_DEL_CHAT))

    tg.msg_handler(on_add_account, func=lambda m: (tg.get_state(m.chat.id, m.from_user.id) or {}).get("state") == STATE_AWAIT_ACCOUNT)
    tg.msg_handler(on_add_pop3, func=lambda m: (tg.get_state(m.chat.id, m.from_user.id) or {}).get("state") == STATE_AWAIT_POP3)
    tg.msg_handler(on_add_oauth, func=lambda m: (tg.get_state(m.chat.id, m.from_user.id) or {}).get("state") == STATE_AWAIT_OAUTH)
    tg.msg_handler(on_setting, func=lambda m: (tg.get_state(m.chat.id, m.from_user.id) or {}).get("state") == STATE_AWAIT_SETTING)
    tg.msg_handler(on_chat, func=lambda m: (tg.get_state(m.chat.id, m.from_user.id) or {}).get("state") == STATE_AWAIT_CHAT)
    tg.msg_handler(on_prefix, func=lambda m: (tg.get_state(m.chat.id, m.from_user.id) or {}).get("state") == STATE_AWAIT_PREFIX)

    # /email_codes — открыть меню из чата
    def cmd_open(m: Message) -> None:
        bot.send_message(m.chat.id, _text_main(), reply_markup=_kb_main(), parse_mode="HTML")

    tg.msg_handler(cmd_open, commands=["email_codes"])
    try:
        cardinal.add_telegram_commands(UUID, [
            ("email_codes", "Email Code Fetcher: открыть меню", True),
            ("ecf_guide", "Email Code Fetcher: гайд", True),
            ("ecf_test", "Email Code Fetcher: тест", True),
        ])
    except Exception:
        logger.exception("Не удалось зарегистрировать команду /email_codes")

    # /ecf_guide — guide
    def cmd_guide(m: Message) -> None:
        filters_list = ", ".join(FILTERS.keys())
        cur_prefix = _load_config()["settings"].get("command_prefix", "")
        prefix_demo = _prefix_label(cur_prefix)
        example_cmd = f"{cur_prefix}rck" if cur_prefix else "rck"
        guide_text = (
            "<b>📖 Email Code Fetcher — Гайд</b>\n\n"
            "<b>Что делает:</b>\n"
            "Получает коды верификации из email (IMAP/POP3/OAuth) "
            "по команде покупателя в чате FunPay.\n\n"
            "<b>Настройка:</b>\n"
            "1. /email_codes → 📧 Аккаунты → ➕ IMAP аккаунт\n"
            "2. Введите: <code>email | app_password | фильтр | команда</code>\n"
            "3. Добавьте Chat ID в разделе 🔑 Доступ\n"
            "4. (опц.) /email_codes → ⚙️ Параметры → ⌨️ Префикс команд\n\n"
            "<b>Пресеты фильтров:</b>\n"
            f"<i>{filters_list}</i>\n\n"
            "<b>Префикс команд:</b>\n"
            f"Текущий: <b>{prefix_demo}</b>. "
            "Если задан — покупатель должен начинать сообщение с него "
            f"(например, <code>{example_cmd}</code>). "
            "Можно выбрать <code>!</code>, <code>/</code>, <code>.</code>, "
            "<code>#</code>, <code>?</code>, свой или отключить (без префикса).\n\n"
            "<b>Протоколы:</b>\n"
            "• IMAP — стандартный, рекомендуется\n"
            "• POP3 — альтернативный (без SEARCH)\n"
            "• Gmail OAuth — через refresh_token\n\n"
            "<b>Как получить app_password:</b>\n"
            "• Gmail: google.com → Безопасность → Пароли приложений\n"
            "• Yandex: id.yandex.ru → Безопасность → Пароли приложений\n"
            "• Mail.ru: account.mail.ru → Безопасность → Пароли\n\n"
            "<b>Команды:</b>\n"
            "/email_codes — меню настроек\n"
            "/ecf_guide — этот гайд\n"
            "/ecf_test — тест на фейковых данных"
        )
        bot.send_message(m.chat.id, guide_text, parse_mode="HTML")

    tg.msg_handler(cmd_guide, commands=["ecf_guide"])

    # /ecf_test — test with fake data
    def cmd_test(m: Message) -> None:
        kb = K()
        kb.add(
            B("🌐 Реальный тест", callback_data=CBT_TEST_REAL),
            B("🎭 Фейковый тест", callback_data=CBT_TEST_FAKE),
        )
        bot.send_message(
            m.chat.id,
            "🧪 <b>Выберите тип теста:</b>\n\n"
            "🌐 <b>Реальный</b> — подключение к почтовому серверу\n"
            "🎭 <b>Фейковый</b> — проверка конфигурации без подключения",
            parse_mode="HTML",
            reply_markup=kb,
        )

    def _cb_test_real(call: CallbackQuery) -> None:
        bot.answer_callback_query(call.id)
        chat_id = call.message.chat.id
        cfg2 = _load_config()
        if not cfg2["accounts"]:
            bot.send_message(
                chat_id,
                "❌ <b>Тест невозможен:</b> нет подключённых email-аккаунтов.\n"
                "Добавьте аккаунт через /email_codes → 📧 Аккаунты.",
                parse_mode="HTML",
            )
            return

        bot.send_message(chat_id, "🔄 Тестирую подключение к первому аккаунту...")

        acc = cfg2["accounts"][0]
        addr = acc["email"]
        proto = (acc.get("protocol") or "IMAP").upper()

        import threading as _thr

        def _worker() -> None:
            try:
                if proto == "POP3":
                    host = acc.get("pop3_host") or _guess_pop3_host(addr)[0]
                    port = int(acc.get("pop3_port") or _guess_pop3_host(addr)[1])
                    srv = poplib.POP3_SSL(host, port, timeout=15)
                    srv.user(addr)
                    srv.pass_(acc.get("app_password", ""))
                    count, _ = srv.stat()
                    srv.quit()
                    bot.send_message(
                        chat_id,
                        f"✅ <b>POP3 тест пройден!</b>\n\n"
                        f"📧 Аккаунт: <code>{addr}</code>\n"
                        f"📬 Писем в ящике: {count}\n"
                        f"🔌 Хост: {host}:{port}\n"
                        f"🎯 Фильтр: {acc.get('filter', 'Custom')}\n\n"
                        f"Плагин готов к работе!",
                        parse_mode="HTML",
                    )
                elif proto in ("OAUTH", "OAUTH_GMAIL"):
                    cid = acc.get("oauth_client_id", "")
                    csec = acc.get("oauth_client_secret", "")
                    rt = acc.get("oauth_refresh_token", "")
                    if not all((cid, csec, rt)):
                        bot.send_message(chat_id, "❌ OAuth: не заполнены credentials.")
                        return
                    access = _oauth_access_token(cid, csec, rt)
                    if access:
                        bot.send_message(
                            chat_id,
                            f"✅ <b>OAuth тест пройден!</b>\n\n"
                            f"📧 Аккаунт: <code>{addr}</code>\n"
                            f"🔑 Access token получен\n"
                            f"🎯 Фильтр: {acc.get('filter', 'Custom')}\n\n"
                            f"Плагин готов к работе!",
                            parse_mode="HTML",
                        )
                    else:
                        bot.send_message(chat_id, "❌ OAuth: не удалось получить access_token.")
                else:
                    host = acc.get("imap_host") or _guess_imap_host(addr)[0]
                    port = int(acc.get("imap_port") or _guess_imap_host(addr)[1])
                    mail = imaplib.IMAP4_SSL(host, port, timeout=15)
                    mail.login(addr, acc.get("app_password", ""))
                    mail.select("INBOX", readonly=True)
                    status, data = mail.search(None, "ALL")
                    count = len(data[0].split()) if status == "OK" and data[0] else 0
                    mail.logout()
                    bot.send_message(
                        chat_id,
                        f"✅ <b>IMAP тест пройден!</b>\n\n"
                        f"📧 Аккаунт: <code>{addr}</code>\n"
                        f"📬 Писем в INBOX: {count}\n"
                        f"🔌 Хост: {host}:{port}\n"
                        f"🎯 Фильтр: {acc.get('filter', 'Custom')}\n\n"
                        f"Плагин готов к работе!",
                        parse_mode="HTML",
                    )
            except Exception as ex:
                bot.send_message(
                    chat_id,
                    f"❌ <b>Тест не пройден!</b>\n\n"
                    f"📧 Аккаунт: <code>{addr}</code>\n"
                    f"🔌 Протокол: {proto}\n"
                    f"Ошибка: <code>{str(ex)[:200]}</code>\n\n"
                    f"Проверьте:\n"
                    f"• Правильность email и пароля\n"
                    f"• Включён ли IMAP/POP3 в настройках почты\n"
                    f"• Разрешены ли «менее безопасные приложения»",
                    parse_mode="HTML",
                )

        _thr.Thread(target=_worker, daemon=True).start()

    def _cb_test_fake(call: CallbackQuery) -> None:
        bot.answer_callback_query(call.id)
        chat_id = call.message.chat.id
        cfg2 = _load_config()
        if not cfg2["accounts"]:
            bot.send_message(
                chat_id,
                "❌ <b>Фейковый тест невозможен:</b> нет подключённых email-аккаунтов.\n"
                "Добавьте аккаунт через /email_codes → 📧 Аккаунты.",
                parse_mode="HTML",
            )
            return

        acc = cfg2["accounts"][0]
        addr = acc.get("email", "")
        proto = (acc.get("protocol") or "IMAP").upper()
        filt = acc.get("filter", "Custom")

        # Validate config fields without connecting
        issues = []
        if not addr:
            issues.append("email не указан")
        if proto in ("IMAP",):
            host = acc.get("imap_host") or (_guess_imap_host(addr)[0] if addr else "")
            if not acc.get("app_password"):
                issues.append("app_password не указан")
        elif proto == "POP3":
            host = acc.get("pop3_host") or (_guess_pop3_host(addr)[0] if addr else "")
            if not acc.get("app_password"):
                issues.append("app_password не указан")
        elif proto in ("OAUTH", "OAUTH_GMAIL"):
            host = "imap.gmail.com"
            if not acc.get("oauth_client_id"):
                issues.append("oauth_client_id не указан")
            if not acc.get("oauth_client_secret"):
                issues.append("oauth_client_secret не указан")
            if not acc.get("oauth_refresh_token"):
                issues.append("oauth_refresh_token не указан")
        else:
            host = acc.get("imap_host", "")

        if issues:
            bot.send_message(
                chat_id,
                f"⚠️ <b>Фейковый тест: найдены проблемы в конфигурации</b>\n\n"
                f"📧 Email: <code>{addr or '—'}</code>\n"
                f"🔌 Протокол: {proto}\n"
                f"❗ Проблемы:\n" + "\n".join(f"• {i}" for i in issues) +
                f"\n\nРеальное подключение не выполнялось.",
                parse_mode="HTML",
            )
        else:
            bot.send_message(
                chat_id,
                f"✅ <b>Фейковый тест: конфигурация валидна!</b>\n\n"
                f"📧 Email: <code>{addr}</code>\n"
                f"🔌 Протокол: {proto}\n"
                f"🏠 Хост: {host}\n"
                f"🎯 Фильтр: {filt}\n\n"
                f"Реальное подключение не выполнялось.",
                parse_mode="HTML",
            )

    tg.msg_handler(cmd_test, commands=["ecf_test"])
    tg.cbq_handler(_cb_test_real, _cb(CBT_TEST_REAL))
    tg.cbq_handler(_cb_test_fake, _cb(CBT_TEST_FAKE))

    _log("Плагин Email Code Fetcher инициализирован.")


BIND_TO_PRE_INIT = [_init]


def _open_settings_page(cardinal: "Cardinal", msg) -> None:
    """FPC settings page handler - directs user to /email_codes."""
    tg = getattr(cardinal, "telegram", None)
    if tg is None:
        return
    tg.bot.send_message(
        msg.chat.id,
        "<b>Email Code Fetcher</b>\n\n"
        "Для настройки используйте команду /email_codes\n"
        "Для гайда: /ecf_guide",
        parse_mode="HTML",
    )


BIND_TO_SETTINGS_PAGE = _open_settings_page


# ---------- BIND_TO_NEW_MESSAGE: реакция на команды в FunPay-чате ----------
def _on_new_message(cardinal: "Cardinal", event: "NewMessageEvent") -> None:
    msg = event.message
    text = (msg.text or "").strip()
    if not text:
        return
    # Игнорируем системные и собственные сообщения
    if msg.author_id == 0 or msg.author_id == getattr(cardinal.account, "id", None):
        return

    cfg = _load_config()
    chat_id = str(msg.chat_id)
    if not cfg["settings"].get("allow_all_chats", False):
        if chat_id not in cfg["permitted_chats"]:
            return

    # ищем аккаунт, чья команда совпала
    incoming = text.lower()
    prefix = (cfg["settings"].get("command_prefix") or "").lower()
    if prefix:
        if not incoming.startswith(prefix):
            return
        incoming = incoming[len(prefix):].lstrip()
        if not incoming:
            return
    matched = None
    for acc in cfg["accounts"]:
        cmd = (acc.get("command") or "").strip().lower()
        if cmd and (incoming == cmd or incoming.startswith(cmd + " ")):
            matched = acc
            break
    if not matched:
        return

    # rate limit
    key = (msg.chat_id, matched["email"])
    now = time.time()
    rl = cfg["settings"]["rate_limit_sec"]
    if rl > 0 and now - _Runtime.last_request.get(key, 0) < rl:
        try:
            cardinal.send_message(msg.chat_id, "⏳ Запрос слишком частый, подождите немного.")
        except Exception:
            pass
        return
    _Runtime.last_request[key] = now

    # cache
    ttl = cfg["settings"]["cache_ttl_sec"]
    cached = _Runtime.cache.get(matched["email"])
    if cached and ttl > 0 and (now - cached[1]) < ttl:
        code = cached[0]
    else:
        code = fetch_code(matched, cfg["settings"]["search_window_min"])
        if code:
            _Runtime.cache[matched["email"]] = (code, now)
            # Save to persistent cache
            lc = _load_last_codes()
            lc[matched["email"]] = {"code": code, "ts": now}
            _save_last_codes(lc)

    if code:
        ts_str = _fmt_time_msk(now)
        try:
            cardinal.send_message(msg.chat_id, f"🔑 Код: {code} (получен в {ts_str})")
            _log(f"[{matched['email']}] выдан код по команде '{incoming}' в чат {chat_id}")
        except Exception:
            logger.exception("Не удалось отправить код покупателю")
    else:
        # Try to get last cached code
        last_cached = _Runtime.cache.get(matched["email"])
        if not last_cached:
            lc = _load_last_codes()
            entry = lc.get(matched["email"])
            if entry:
                last_cached = (entry["code"], entry["ts"])

        if last_cached:
            ts_str = _fmt_time_msk(last_cached[1])
            try:
                cardinal.send_message(msg.chat_id, f"🔑 Последний код: {last_cached[0]} (получен в {ts_str}, новый не найден)")
                _log(f"[{matched['email']}] выдан кэшированный код по команде '{incoming}' в чат {chat_id}")
            except Exception:
                pass
        else:
            try:
                cardinal.send_message(msg.chat_id, "❌ Код не найден. Попробуйте чуть позже.")
                _log(f"[{matched['email']}] код не найден по команде '{incoming}' в чат {chat_id}")
            except Exception:
                pass


BIND_TO_NEW_MESSAGE = [_on_new_message]


# ---------- BIND_TO_DELETE ----------
def _on_delete(cardinal: "Cardinal", *_: Any) -> None:
    _log("Плагин Email Code Fetcher удалён.")


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
