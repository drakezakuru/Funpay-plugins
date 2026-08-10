"""
Steam Offline — полностью автономный плагин для FunPayCardinal.

Назначение: продажа Steam-аккаунтов «навсегда» (без срока), с лимитом
на количество выдач Steam Guard-кода через настраиваемую команду
(по умолчанию `!код`, можно задать любую через config: `guardik_command`).

Идея:
  * Плагин держит СВОЙ пул Steam-аккаунтов, независимый от любых других
    плагинов.
  * Покупатель оплачивает офлайн-лот → бот выдаёт логин/пароль навсегда.
  * Команда `!код` (или другая настроенная) работает строго N раз
    (настраивается в Telegram).
  * После исчерпания лимита бот отвечает шаблоном "guard_limit_reached".

Управление — Telegram-команда /soffline (inline-меню).

Совместимость:
  * SteamSession и генерация паролей берутся из общего модуля
    _steam_session_common.py. Плагин НЕ зависит от steam_rental.py.
  * Команда `!код` (или настроенная) обрабатывается через собственный
    BIND_TO_NEW_MESSAGE handler. Никаких monkey-patch.
  * Аккаунты steam_offline хранятся в собственной директории
    storage/plugins/steam_offline/.

Автор: @drakelovc.
Лицензия: MIT.
"""
from __future__ import annotations

import datetime
import hashlib
import io
import csv
import json
import logging
import os
import random
import re
import secrets as pysecrets
import string
import threading
import time
import uuid as _uuid
from base64 import b64encode
from typing import TYPE_CHECKING, Any

import requests

if TYPE_CHECKING:
    from cardinal import Cardinal
    from FunPayAPI.updater.events import NewMessageEvent, NewOrderEvent


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
DONATION_CALLBACK_PREFIX = "sof_dn"    # префикс колбэков кнопок баннера
DONATION_PLUGIN_NAME = "Steam Offline"  # имя плагина в шапке баннера

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


# Соседний плагин — обязательная зависимость (Steam-клиент
# и вспомогательные функции). Пулы аккаунтов раздельные!
# Steam-клиент и утилиты — встроены прямо в плагин, чтобы он работал
# автономно, без зависимостей от других плагинов.
#
# (Это копия из _steam_session_common.py; оставлено здесь чтобы плагин
# можно было использовать автономно. Если у тебя уже есть общий модуль —
# можешь удалить этот блок и заменить на
# `from _steam_session_common import SteamSession, SteamError, ...`.)

class SteamError(RuntimeError):
    """Базовое исключение для ошибок Steam API."""
    pass


def _sr_gen_password(length: int = 16) -> str:
    """Генерирует пароль: ≥1 lower, ≥1 upper, ≥1 digit."""
    alphabet = string.ascii_letters + string.digits
    while True:
        pw = "".join(pysecrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in pw)
                and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw)):
            return pw


_SR_API = "https://api.steampowered.com"
_SR_COMMUNITY = "https://steamcommunity.com"
_SR_STORE = "https://store.steampowered.com"
_SR_LOGIN_HOST = "https://login.steampowered.com"
_SR_HELP = "https://help.steampowered.com"
_SR_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


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
        self.sess.headers.update({"User-Agent": _SR_USER_AGENT})
        self._guard = steam_guard

    def generate_2fa_code(self) -> str:
        return self._guard.generate_one_time_code(self.shared_secret)

    def get_guard_code(self) -> str:
        return self.generate_2fa_code()

    def login(self) -> None:
        from rsa import PublicKey, encrypt as rsa_encrypt

        rsa_resp = self.sess.get(
            f"{_SR_API}/IAuthenticationService/GetPasswordRSAPublicKey/v1/",
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
                f"{_SR_API}/IAuthenticationService/BeginAuthSessionViaCredentials/v1/",
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
            f"{_SR_API}/IAuthenticationService/UpdateAuthSessionWithSteamGuardCode/v1/",
            data={"client_id": client_id, "steamid": steam_id,
                  "code_type": 3, "code": code}, timeout=15)

        refresh_token = None
        for _ in range(10):
            poll = self.sess.post(
                f"{_SR_API}/IAuthenticationService/PollAuthSessionStatus/v1/",
                data={"client_id": client_id, "request_id": request_id},
                timeout=15)
            refresh_token = poll.json().get("response", {}).get("refresh_token")
            if refresh_token:
                break
            time.sleep(2)
        if not refresh_token:
            raise SteamError("Не удалось получить refresh_token (Steam Guard fail)")

        self.sess.get(_SR_COMMUNITY, timeout=15)
        sessionid = self.sess.cookies.get("sessionid", "")
        self.sess.post(
            f"{_SR_LOGIN_HOST}/jwt/finalizelogin",
            data={"nonce": refresh_token, "sessionid": sessionid,
                  "redir": f"{_SR_COMMUNITY}/login/home/?goto="},
            timeout=15)

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
        sessionid = self.sessionid_for(_SR_STORE)
        if not sessionid:
            raise SteamError("Нет sessionid для store.steampowered.com")
        endpoints = [
            (f"{_SR_STORE}/twofactor/manage_action",
             {"action": "deauthorize", "sessionid": sessionid}),
            (f"{_SR_COMMUNITY}/profiles/{self.steamid}/edit/info",
             {"sessionID": sessionid, "type": "deauthorize"}),
        ]
        ok = False
        for url, data in endpoints:
            try:
                r = self.sess.post(url, data=data, timeout=15,
                                    headers={"Referer": f"{_SR_STORE}/account/"})
                if r.status_code < 400:
                    ok = True
            except Exception:
                LOGGER.debug("steam_offline: revoke endpoint %s failed", url,
                             exc_info=True)
        return ok

    def change_password(self, new_password: str) -> None:
        from rsa import PublicKey, encrypt as rsa_encrypt
        from urllib.parse import urlparse, parse_qs

        sid_help = self.sessionid_for(_SR_HELP)
        if not sid_help:
            raise SteamError("Нет sessionid для help.steampowered.com (логин истёк?)")

        r1 = self.sess.get(
            f"{_SR_HELP}/wizard/HelpChangePassword?redir=store/account/",
            headers={"User-Agent": _SR_USER_AGENT,
                     "Referer": f"{_SR_STORE}/", "Accept": "text/html"},
            allow_redirects=True, timeout=15)
        final_url = r1.url
        qs = parse_qs(urlparse(final_url).query)
        params = {k: qs.get(k, [""])[0] for k in
                  ("s", "account", "reset", "lost", "issueid")}
        if not params["s"]:
            raise SteamError(
                "Не удалось получить параметры wizard-recovery "
                "(нужен валидный логин в Steam)")

        self.sess.get(
            f"{_SR_HELP}/en/wizard/HelpWithLoginInfoEnterCode",
            params={**params, "sessionid": sid_help,
                    "wizard_ajax": 1, "gamepad": 0},
            headers={"User-Agent": _SR_USER_AGENT,
                     "X-Requested-With": "XMLHttpRequest"}, timeout=15)

        r3 = self.sess.post(
            f"{_SR_HELP}/en/wizard/AjaxSendAccountRecoveryCode",
            data={"sessionid": sid_help, "wizard_ajax": "1", "gamepad": "0",
                  "s": params["s"], "method": "8", "link": "", "n": "1"},
            headers={"User-Agent": _SR_USER_AGENT,
                     "X-Requested-With": "XMLHttpRequest",
                     "Origin": _SR_HELP,
                     "Referer":
                         f"{_SR_HELP}/en/wizard/HelpWithLoginInfoEnterCode"},
            timeout=15)
        r3_json = self._safe_json(r3)
        if r3_json.get("errorMsg"):
            raise SteamError(
                f"AjaxSendAccountRecoveryCode: {r3_json['errorMsg']}")

        self._mobile_confirm_recovery(params["s"])

        self.sess.post(
            f"{_SR_HELP}/en/wizard/AjaxPollAccountRecoveryConfirmation",
            data={"sessionid": sid_help, "wizard_ajax": 1,
                  "s": params["s"], "reset": params["reset"],
                  "lost": params["lost"], "method": 8,
                  "issueid": params["issueid"], "gamepad": 0},
            headers={"User-Agent": _SR_USER_AGENT,
                     "X-Requested-With": "XMLHttpRequest",
                     "Origin": _SR_HELP}, timeout=15)

        self.sess.get(
            f"{_SR_HELP}/en/wizard/AjaxVerifyAccountRecoveryCode",
            params={"code": "", "s": params["s"], "reset": params["reset"],
                    "lost": params["lost"], "method": 8,
                    "issueid": params["issueid"], "sessionid": sid_help,
                    "wizard_ajax": 1, "gamepad": 0},
            headers={"User-Agent": _SR_USER_AGENT,
                     "X-Requested-With": "XMLHttpRequest"}, timeout=15)

        self.sess.post(
            f"{_SR_HELP}/en/wizard/AjaxAccountRecoveryGetNextStep",
            data={"sessionid": sid_help, "wizard_ajax": 1, "s": params["s"],
                  "account": params["account"], "reset": params["reset"],
                  "issueid": params["issueid"], "lost": 2},
            headers={"User-Agent": _SR_USER_AGENT,
                     "X-Requested-With": "XMLHttpRequest",
                     "Content-Type":
                         "application/x-www-form-urlencoded; charset=UTF-8",
                     "Origin": _SR_HELP}, timeout=15)

        def _fetch_rsa() -> tuple["PublicKey", str]:
            rsa_r = self.sess.post(
                f"{_SR_HELP}/en/login/getrsakey/",
                data={"sessionid": sid_help, "username": self.account_name},
                headers={"User-Agent": _SR_USER_AGENT,
                         "X-Requested-With": "XMLHttpRequest",
                         "Origin": _SR_HELP}, timeout=15)
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
            f"{_SR_HELP}/en/wizard/AjaxAccountRecoveryVerifyPassword/",
            data={"sessionid": sid_help, "s": params["s"], "lost": 2,
                  "reset": 1, "password": enc_old, "rsatimestamp": ts_old},
            headers={"User-Agent": _SR_USER_AGENT,
                     "X-Requested-With": "XMLHttpRequest",
                     "Origin": _SR_HELP}, timeout=15)
        vp_json = self._safe_json(vp)
        if vp_json.get("errorMsg"):
            raise SteamError(
                f"AjaxAccountRecoveryVerifyPassword: {vp_json['errorMsg']}")

        # ── CheckPasswordAvailable: новый пароль (plaintext) ───────────────
        chk = self.sess.post(
            f"{_SR_HELP}/en/wizard/AjaxCheckPasswordAvailable/",
            data={"sessionid": sid_help, "wizard_ajax": 1,
                  "password": new_password},
            headers={"User-Agent": _SR_USER_AGENT, "Origin": _SR_HELP},
            timeout=15)
        chk_json = self._safe_json(chk)
        if not chk_json.get("available", True):
            raise SteamError(
                "Steam: новый пароль недоступен (слишком простой/похожий)")

        # ── ChangePassword: НОВЫЙ пароль (со свежим RSA timestamp) ─────────
        rsa_key_new, ts_new = _fetch_rsa()
        enc_new = b64encode(rsa_encrypt(new_password.encode("ascii"),
                                         rsa_key_new)).decode()
        ch = self.sess.post(
            f"{_SR_HELP}/en/wizard/AjaxAccountRecoveryChangePassword/",
            data={"sessionid": sid_help, "wizard_ajax": 1, "s": params["s"],
                  "account": params["account"], "password": enc_new,
                  "rsatimestamp": ts_new},
            headers={"User-Agent": _SR_USER_AGENT,
                     "X-Requested-With": "XMLHttpRequest",
                     "Origin": _SR_HELP}, timeout=15)
        ch_json = self._safe_json(ch)
        if ch_json.get("errorMsg"):
            raise SteamError(
                f"AjaxAccountRecoveryChangePassword: {ch_json['errorMsg']}")

        self.password = new_password
        LOGGER.info("steam_offline: пароль успешно изменён для %s",
                    self.account_name)

    @staticmethod
    def _safe_json(r: "requests.Response") -> dict:
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


# ── Плагин-метаданные (FPC читает эти константы из файла) ────────────────────
NAME = "Steam Offline"
VERSION = "1.11.2"
DESCRIPTION = (
    "Офлайн-выдача Steam-аккаунтов навсегда (без срока) для FunPay Cardinal. "
    "Свой пул аккаунтов, своё меню /soffline, мультивыдача "
    "(один аккаунт можно продать нескольким покупателям одновременно), "
    "лимит Steam Guard через настраиваемую команду (по умолчанию !код). "
    "Автор: @drakelovc."
)
CREDITS = "@drakelovc"
UUID = "856a2ee0-accb-48e3-be23-f98e7ba8d682"
SETTINGS_PAGE = True
BIND_TO_DELETE = None

LOGGER = logging.getLogger("FPC.steam_offline")

STORAGE_DIR = os.path.join("storage", "plugins", "steam_offline")
ACCOUNTS_FILE = os.path.join(STORAGE_DIR, "accounts.json")
ASSIGNMENTS_FILE = os.path.join(STORAGE_DIR, "assignments.json")
LOTS_FILE = os.path.join(STORAGE_DIR, "lots.json")
GAMES_FILE = os.path.join(STORAGE_DIR, "games.json")
CONFIG_FILE = os.path.join(STORAGE_DIR, "config.json")
HISTORY_FILE = os.path.join(STORAGE_DIR, "history.json")
EVENTS_FILE = os.path.join(STORAGE_DIR, "events.json")
METRICS_FILE = os.path.join(STORAGE_DIR, "metrics.json")  # v5
# v1.9.0: blacklist покупателей. Срабатывает на NEW_ORDER + auto-add при refund.
BLACKLIST_FILE = os.path.join(STORAGE_DIR, "blacklist.json")
# Состояние авто-(де)активации лотов FunPay (для отображения в TG-меню).
LOT_STATE_FILE = os.path.join(STORAGE_DIR, "lot_activation.json")
# Кэш ID категорий (игр) наших лотов для пропуска в авто-поднятии FPC.
RAISE_SKIP_FILE = os.path.join(STORAGE_DIR, "raise_skip_categories.json")
# v1.10.0: шаблоны сообщений вынесены в отдельные JSON-файлы (RU/EN).
# Канонический источник правды — файлы; admin может редактировать через
# TG-меню «📝 Шаблоны» (с переключателем 🇷🇺/🇬🇧) или прямо в файле.
TEMPLATES_RU_FILE = os.path.join(STORAGE_DIR, "templates_ru.json")
TEMPLATES_EN_FILE = os.path.join(STORAGE_DIR, "templates_en.json")
# v1.10.0: язык конкретного покупателя ({str(buyer_id): "ru"|"en"}).
# Переключается командами !engrent / !rusrent в чате FunPay.
BUYER_LANG_FILE = os.path.join(STORAGE_DIR, "buyer_lang.json")
# Человекочитаемый журнал действий (ротация по размеру).
ACTIONS_LOG_FILE = os.path.join(STORAGE_DIR, "actions.log")

# ── Шаблоны по умолчанию ─────────────────────────────────────────────────────
_DEFAULT_TEMPLATES: dict[str, str] = {
    "issue": (
        "🟩 АККАУНТ ВЫДАН НАВСЕГДА!\n"
        "🎮 Игра: {game}\n\n"
        "🔑 Логин: {login}\n"
        "🔒 Пароль: {password}\n\n"
        "♾ Срок: безлимит\n"
        "🔢 Кодов Steam Guard: {codes_limit} шт.\n\n"
        "💬 Команды:\n"
        "   !код {login} — получить Steam Guard\n"
        "   !статус — посмотреть остаток кодов\n"
        "   !помощь — список команд\n\n"
        "⚠ Берегите пароль — повторно он не выдаётся.\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🌐 If you prefer English: type !engrent"
    ),
    "guard_code": (
        "🟥 Steam Guard для {login}: {code}\n"
        "(действителен ~30 секунд)\n\n"
        "🔢 Осталось кодов: {codes_left} из {codes_limit}"
    ),
    "guard_last_code": (
        "🟥 Steam Guard для {login}: {code}\n"
        "(действителен ~30 секунд)\n\n"
        "⚠ Это БЫЛ ПОСЛЕДНИЙ доступный код!\n"
        "Следующая попытка получить код будет отклонена."
    ),
    "guard_limit_reached": (
        "⛔ ЛИМИТ ИСЧЕРПАН\n\n"
        "🔑 Логин: {login}\n"
        "🔢 Использовано кодов: {codes_used} из {codes_limit}\n\n"
        "Steam Guard-коды по этой выдаче больше не выдаются.\n"
        "Обратитесь к продавцу, если нужен ещё один."
    ),
    "guard_error": (
        "⚠ ОШИБКА\n\n"
        "✖ У вас нет выданного навсегда аккаунта\n\n"
        "Возможные причины:\n"
        "• Неверный логин\n"
        "• Выдача была отозвана\n"
        "• Вы покупали аккаунт у другого продавца\n\n"
        "💡 Попробуйте !код без логина — автоопределение"
    ),
    "guard_error_no_secret": (
        "⚠ ОШИБКА\n\n"
        "✖ Steam Guard недоступен\n\n"
        "Для этого аккаунта не настроен\n"
        "мобильный аутентификатор.\n\n"
        "📧 Обратитесь к продавцу для\n"
        "получения кода вручную."
    ),
    "no_accounts": (
        "✖ К сожалению, все аккаунты этой категории распроданы\n\n"
        "🎮 Игра: {game}\n\n"
        "📧 Напишите продавцу, чтобы он добавил новый аккаунт."
    ),
    "status": (
        "📊 ВАША ВЫДАЧА\n\n"
        "🔑 Логин: {login}\n"
        "🎮 Игра: {game}\n"
        "♾ Срок: навсегда\n"
        "🔢 Кодов использовано: {codes_used} из {codes_limit}\n"
        "🔢 Осталось: {codes_left}"
    ),
    "help": (
        "🟥 ПОМОЩЬ (офлайн-аккаунты) 🟥\n\n"
        "💬 Доступные команды:\n\n"
        "  !код [логин] — получить Steam Guard\n"
        "  !статус — остаток кодов\n"
        "  !помощь — это сообщение\n\n"
        "Аккаунт ваш навсегда, но Steam Guard\n"
        "выдаётся ограниченное число раз."
    ),
    "revoked": (
        "❌ ВЫДАЧА ОТОЗВАНА\n\n"
        "🔑 Логин: {login}\n\n"
        "Продавец отозвал доступ к этому аккаунту.\n"
        "Если это ошибка — свяжитесь с продавцом."
    ),
    "order_received": (
        "🟩 Заказ получен, оформляем выдачу аккаунта..."
    ),
    "accounts_list": (
        "📋 Доступные аккаунты\n\n"
        "{lots}\n\n"
        "💬 Чтобы купить — оплатите лот на FunPay."
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
}

# v1.10.0: English translations of all RU templates above. Used when
# buyer's lang is "en" (set via !engrent in FunPay chat). Names match
# RU keys 1:1. Canonical source of truth at runtime is the JSON file
# `storage/plugins/steam_offline/templates_en.json` — this dict only
# seeds it on first run. Same approach as steam_rental v2.22.
_DEFAULT_TEMPLATES_EN: dict[str, str] = {
    "issue": (
        "🟩 ACCOUNT DELIVERED FOREVER!\n"
        "🎮 Game: {game}\n\n"
        "🔑 Login: {login}\n"
        "🔒 Password: {password}\n\n"
        "♾ Term: lifetime\n"
        "🔢 Steam Guard codes: {codes_limit} pcs.\n\n"
        "💬 Commands:\n"
        "   !code {login} — get Steam Guard\n"
        "   !status — show remaining codes\n"
        "   !help — list of commands\n\n"
        "⚠ Keep your password safe — it won't be sent again."
    ),
    "guard_code": (
        "🟥 Steam Guard for {login}: {code}\n"
        "(valid ~30 seconds)\n\n"
        "🔢 Codes remaining: {codes_left} of {codes_limit}"
    ),
    "guard_last_code": (
        "🟥 Steam Guard for {login}: {code}\n"
        "(valid ~30 seconds)\n\n"
        "⚠ This was the LAST available code!\n"
        "Next code request will be rejected."
    ),
    "guard_limit_reached": (
        "⛔ LIMIT REACHED\n\n"
        "🔑 Login: {login}\n"
        "🔢 Codes used: {codes_used} of {codes_limit}\n\n"
        "Steam Guard codes for this delivery are no longer issued.\n"
        "Contact the seller if you need another."
    ),
    "guard_error": (
        "⚠ ERROR\n\n"
        "✖ You don't have an account delivered forever\n\n"
        "Possible reasons:\n"
        "• Wrong login\n"
        "• Delivery was revoked\n"
        "• You bought the account from a different seller\n\n"
        "💡 Try !code without a login — auto-detect"
    ),
    "guard_error_no_secret": (
        "⚠ ERROR\n\n"
        "✖ Steam Guard unavailable\n\n"
        "This account doesn't have\n"
        "the mobile authenticator set up.\n\n"
        "📧 Contact the seller for a manual code."
    ),
    "no_accounts": (
        "✖ Sorry, all accounts in this category are sold out\n\n"
        "🎮 Game: {game}\n\n"
        "📧 Message the seller to add a new account."
    ),
    "status": (
        "📊 YOUR DELIVERY\n\n"
        "🔑 Login: {login}\n"
        "🎮 Game: {game}\n"
        "♾ Term: lifetime\n"
        "🔢 Codes used: {codes_used} of {codes_limit}\n"
        "🔢 Remaining: {codes_left}"
    ),
    "help": (
        "🟥 HELP (offline accounts) 🟥\n\n"
        "💬 Available commands:\n\n"
        "  !code [login] — get Steam Guard\n"
        "  !status — remaining codes\n"
        "  !help — this message\n\n"
        "🌐 !rusrent — switch chat to Russian\n\n"
        "The account is yours forever, but Steam Guard\n"
        "codes are limited."
    ),
    "revoked": (
        "❌ DELIVERY REVOKED\n\n"
        "🔑 Login: {login}\n\n"
        "The seller has revoked access to this account.\n"
        "If this is a mistake — contact the seller."
    ),
    "order_received": (
        "🟩 Order received, processing account delivery..."
    ),
    "accounts_list": (
        "📋 Available accounts\n\n"
        "{lots}\n\n"
        "💬 To buy — pay for the lot on FunPay."
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
}

# v1.10.0: словарь старых дефолтов RU-шаблонов, которые поменялись в этом
# релизе. Используется для бесшовной миграции: если в `templates_ru.json`
# лежит ровно один из этих old-default-вариантов (т.е. seller не правил
# его руками), он автоматически апгрейдится до текущего дефолта.
# Если в файле кастомный текст — НЕ трогаем.
_OLD_DEFAULT_TEMPLATES_RU: dict[str, list[str]] = {
    # v1.9.0 → v1.10.0: добавлена строка-подсказка про английский в issue.
    "issue": [
        (
            "🟩 АККАУНТ ВЫДАН НАВСЕГДА!\n"
            "🎮 Игра: {game}\n\n"
            "🔑 Логин: {login}\n"
            "🔒 Пароль: {password}\n\n"
            "♾ Срок: безлимит\n"
            "🔢 Кодов Steam Guard: {codes_limit} шт.\n\n"
            "💬 Команды:\n"
            "   !код {login} — получить Steam Guard\n"
            "   !статус — посмотреть остаток кодов\n"
            "   !помощь — список команд\n\n"
            "⚠ Берегите пароль — повторно он не выдаётся."
        ),
    ],
    # v1.10.0 → v1.11.0: добавлен плейсхолдер {logins} с реальными
    # Steam-логинами свободных аккаунтов в группе игры.
    # v1.11.0 → v1.11.1: убраны HTML-теги <b> (FunPay-чат их не рендерит,
    # показывал буквально «<b>1</b>»).
    "accounts_list_lot_line": [
        "🎮 {game}: <b>{free}</b> шт.",
        "🎮 {game} (<b>{free}</b> шт.)\n   {logins}",
    ],
    "accounts_list": [
        (
            "📋 <b>Доступные аккаунты</b>\n\n"
            "{lots}\n\n"
            "💬 Чтобы купить — оплатите лот на FunPay."
        ),
    ],
    "accounts_list_empty": [
        (
            "📋 <b>Доступные аккаунты</b>\n\n"
            "✖ К сожалению, сейчас нет свободных аккаунтов.\n"
            "Напишите продавцу — он добавит."
        ),
    ],
}

# v1.11.0: аналогичная карта для EN. Используется такой же миграцией.
_OLD_DEFAULT_TEMPLATES_EN: dict[str, list[str]] = {
    "accounts_list_lot_line": [
        "🎮 {game}: <b>{free}</b> pcs.",
        "🎮 {game} (<b>{free}</b> pcs.)\n   {logins}",
    ],
    "accounts_list": [
        (
            "📋 <b>Available accounts</b>\n\n"
            "{lots}\n\n"
            "💬 To buy — pay for the lot on FunPay."
        ),
    ],
    "accounts_list_empty": [
        (
            "📋 <b>Available accounts</b>\n\n"
            "✖ Sorry, no free accounts right now.\n"
            "Message the seller — they'll add some."
        ),
    ],
}

_DEFAULT_CONFIG: dict[str, Any] = {
    "default_guard_limit": 10,
    "auto_deliver": True,
    "change_password_on_issue": False,
    # ⚠ По умолчанию ВЫКЛ. Управляет ручной кнопкой «📤 Отозвать сессии»
    # в карточке аккаунта. До v1.7 кнопка была ХАРДКОДОМ выключена и
    # показывала «временно отключено». Теперь работает по флагу:
    # вкл → реально отозвать чужие сессии в Steam; выкл → кнопка
    # сообщает «включи в настройках». Включить — ⚙ Настройки.
    "revoke_sessions_enabled": False,
    "tg_notify": True,
    "guardik_command": "!код",
    "templates": dict(_DEFAULT_TEMPLATES),
    # v1.10.0: язык по умолчанию для новых покупателей. Покупатель
    # может переключить себе командой !engrent (en) / !rusrent (ru) в
    # чате FunPay.
    "default_language": "ru",
    # v5: Telegram operator panel под уведомлением о выдаче
    "operator_buttons_on_issue": True,
    # v5: Daily summary в Telegram (00:00 МСК = 21 UTC)
    "daily_summary_enabled": True,
    "daily_summary_hour_utc": 21,
    # v5: Prometheus /metrics. Порт отличается от steam_rental (9101).
    "metrics_enabled": False,
    "metrics_port": 9102,
    "metrics_bind": "0.0.0.0",
    # v1.9.0: blacklist покупателей. Включает блокировку выдачи на NEW_ORDER
    # и авто-добавление buyer'а в ЧС после REFUND/CANCELED.
    "blacklist_enabled": True,
    "auto_blacklist_on_refund": True,
    # ── Denuvo: лимит «новых устройств в день» по 00:00 UTC ──
    # Ровно 5 — это лимит самого Steam/Denuvo для Denuvo-игр. Можно
    # переопределить в настройках или per-lot.
    "denuvo_default_limit": 5,
    # Авто-(де)активация лотов FunPay: выключать лот, когда нет свободных
    # аккаунтов, и включать обратно когда появляются. Управляется через
    # /soffline → ⚙ Настройки или ручной правкой config.json.
    "auto_deactivate_lots": True,
    # v1.5: разрешить выдавать ОДИН И ТОТ ЖЕ аккаунт нескольким покупателям
    # одновременно (для не-Denuvo лотов). Каждый покупатель получает свою
    # выдачу со своим счётчиком кодов; alias не "замораживается" после
    # первой продажи. Picker распределяет покупателей по аккаунтам по
    # принципу least-loaded (балансировка). Поставь False, чтобы вернуть
    # старое поведение «1 alias = 1 активная выдача» (как было до v1.5).
    "allow_multi_issue": True,
}


# ── Denuvo: helpers (UTC день, счётчик устройств per account) ────────────────
def _denuvo_today_utc() -> str:
    """Текущая UTC-дата в формате YYYY-MM-DD. Сброс счётчика — 00:00 UTC."""
    return datetime.datetime.utcnow().strftime("%Y-%m-%d")


def _denuvo_get_counter(acc: dict[str, Any]) -> tuple[str, int]:
    """Возвращает (день, счётчик_использованных_слотов_сегодня) для аккаунта."""
    today = _denuvo_today_utc()
    day = str(acc.get("denuvo_day") or "")
    cnt = int(acc.get("denuvo_count") or 0)
    if day != today:
        return today, 0
    return today, cnt


def _lot_is_denuvo(lot: dict[str, Any] | None) -> bool:
    """v1.8.0: резолвер «эта выдача — Denuvo?».

    Логика (per-lot override + per-game дефолт):
      * lot.denuvo == True  → принудительно Denuvo для этого лота
      * lot.denuvo == False → принудительно НЕ Denuvo для этого лота
      * lot.denuvo == None / отсутствует → наследуем от game.denuvo
    Игре можно один раз поставить «💎 Denuvo: ✅», и все её лоты
    автоматически становятся Denuvo. Отдельный лот можно явно
    отключить (например, демо-версия) — `lot.denuvo = False`.

    Legacy: до v1.8.0 lot.denuvo был bool с дефолтом False — старые
    конфиги продолжают работать как раньше.
    """
    if lot is None:
        return False
    val = lot.get("denuvo")
    if val is True or val is False:
        return bool(val)
    # None / отсутствует → смотрим игру
    gkey = (lot.get("game_key") or "").strip()
    if gkey:
        g = get_game(gkey)
        if g and g.get("denuvo"):
            return True
    return False


def _denuvo_lot_limit(lot: dict[str, Any] | None) -> int:
    """Возвращает per-lot Denuvo-лимит, либо дефолт из конфига (5)."""
    if lot is None:
        return int(get_config().get("denuvo_default_limit", 5))
    val = lot.get("denuvo_limit")
    if val is None or val <= 0:
        return int(get_config().get("denuvo_default_limit", 5))
    return int(val)


def _denuvo_slots_left(acc: dict[str, Any], lot_limit: int) -> int:
    _, cnt = _denuvo_get_counter(acc)
    return max(0, int(lot_limit) - int(cnt))


def _denuvo_increment(alias: str) -> tuple[int, int, bool]:
    """Атомарно инкрементирует счётчик. Возвращает (новый_счётчик, лимит_не_передан, был_сброс).
    Здесь лимит сам по себе не известен — вызвавший код проверяет до."""
    with _lock:
        acc = find_account(alias)
        if not acc:
            return 0, 0, False
        today = _denuvo_today_utc()
        prev_day = str(acc.get("denuvo_day") or "")
        was_reset = (prev_day != today)
        cnt = 0 if was_reset else int(acc.get("denuvo_count") or 0)
        cnt += 1
        acc["denuvo_day"] = today
        acc["denuvo_count"] = cnt
        upsert_account(acc)
        return cnt, 0, was_reset


# ── Storage helpers ──────────────────────────────────────────────────────────
def _ensure_storage() -> None:
    os.makedirs(STORAGE_DIR, exist_ok=True)


def _load_json(path: str, default: Any) -> Any:
    _ensure_storage()
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        LOGGER.warning("steam_offline: не удалось прочитать %s", path,
                       exc_info=True)
        return default


def _save_json(path: str, data: Any) -> None:
    _ensure_storage()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _now() -> int:
    return int(time.time())


def _fmt_ts(ts: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts))


# ── Config ───────────────────────────────────────────────────────────────────
def get_config() -> dict[str, Any]:
    cfg = _load_json(CONFIG_FILE, dict(_DEFAULT_CONFIG))
    updated = False
    # v1.10.0: создаём templates_*.json из дефолтов (идемпотентно) и
    # одноразово мигрируем cfg["templates"] → templates_ru.json. Также
    # апгрейдим устаревшие RU-дефолты (например, добавили строку про
    # !engrent в issue в v1.10.0) — без правки кастомизированных шаблонов.
    try:
        _ensure_templates_files()
    except Exception:
        LOGGER.debug("steam_offline: ensure templates files failed",
                     exc_info=True)
    if _migrate_legacy_templates_into_files(cfg):
        updated = True
    try:
        _migrate_outdated_template_defaults()
    except Exception:
        LOGGER.debug(
            "steam_offline: migrate_outdated_template_defaults failed",
            exc_info=True)
    for k, v in _DEFAULT_CONFIG.items():
        if k not in cfg:
            cfg[k] = v
            updated = True
    if "templates" not in cfg or not isinstance(cfg.get("templates"), dict):
        # После миграции v1.10.0 cfg["templates"] намеренно пуст
        # (источник правды — templates_*.json). Не пере-заполняем его
        # дефолтами, иначе бакфилл «воскресит» legacy-override и
        # сломает приоритет файла над cfg.
        cfg["templates"] = {} if cfg.get("_templates_externalized_v1_10") \
            else dict(_DEFAULT_TEMPLATES)
        updated = True
    else:
        if not cfg.get("_templates_externalized_v1_10"):
            for tk, tv in _DEFAULT_TEMPLATES.items():
                if tk not in cfg["templates"]:
                    cfg["templates"][tk] = tv
                    updated = True
    if updated:
        _save_json(CONFIG_FILE, cfg)
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    _save_json(CONFIG_FILE, cfg)


# ── i18n: per-buyer language + JSON-файлы шаблонов (v1.10.0) ────────────────
# Порт идеи steam_rental v2.22: шаблоны RU/EN живут в отдельных JSON-файлах
# в `storage/plugins/steam_offline/templates_*.json`. Покупатель выбирает
# себе язык командой `!engrent` / `!rusrent` в чате FunPay (хранится в
# `buyer_lang.json`). Admin редактирует шаблоны через TG-меню «📝 Шаблоны»
# с переключателем 🇷🇺/🇬🇧, либо прямо в файле — оба пути пишут в одно и
# то же место.
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
    """Возвращает язык конкретного покупателя ("ru" или "en"). Если
    buyer_id неизвестен — fallback на `cfg.default_language` (default "ru").
    """
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
    """Читает шаблоны из JSON-файла. Кэширует с проверкой mtime, чтобы
    редактирование файла снаружи (vim/nano) подхватывалось без рестарта.
    """
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
    """Атомарно пишет шаблоны языка `lang` в JSON-файл и сбрасывает кэш."""
    path = _templates_file_for(lang)
    _save_json(path, data)
    with _templates_cache_lock:
        _templates_cache.pop(path, None)


def _ensure_templates_files() -> None:
    """Создаёт `templates_*.json` из встроенных дефолтов, если файлов нет.
    Идемпотентно: повторные вызовы не перезаписывают существующие файлы.
    """
    if not os.path.exists(TEMPLATES_RU_FILE):
        _save_templates_file("ru", dict(_DEFAULT_TEMPLATES))
    if not os.path.exists(TEMPLATES_EN_FILE):
        _save_templates_file("en", dict(_DEFAULT_TEMPLATES_EN))


def _migrate_outdated_template_defaults() -> bool:
    """Заменяет в `templates_ru.json` / `templates_en.json` те ключи,
    чьи значения точно совпадают со старыми дефолтами (т.е. seller их
    не редактировал) — на свежие дефолты. Возвращает True если что-то
    поменялось. Идемпотентно: повторный вызов после миграции — no-op.

    v1.11.0: миграция работает и для EN (раньше — только RU).
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
                            "steam_offline: миграция шаблона %s %r — "
                            "старый дефолт заменён на новый",
                            lang.upper(), key)
            if new_file != cur_file:
                _save_templates_file(lang, new_file)
                changed_any = True
        except Exception:
            LOGGER.debug(
                "steam_offline: outdated-defaults migration (%s) failed",
                lang, exc_info=True)
    return changed_any


def _migrate_legacy_templates_into_files(cfg: dict[str, Any]) -> bool:
    """Одноразовая миграция: переносит cfg["templates"] (legacy override
    до v1.10.0) в `templates_ru.json`. Помечает результат флагом
    `_templates_externalized_v1_10` чтобы не повторять. Возвращает True
    если что-то изменилось в cfg (нужно пересохранить).
    """
    if cfg.get("_templates_externalized_v1_10"):
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
    cfg["_templates_externalized_v1_10"] = True
    return True


def _render_template(name: str, *, buyer_id: Any = None,
                     lang: str | None = None, **kwargs: Any) -> str:
    """Рендерит шаблон по имени с плейсхолдерами {key}.

    Источник правды (в порядке убывания приоритета):
      1) Файл templates_ru.json / templates_en.json — то, что показывает
         и пишет TG-меню «📝 Шаблоны» (с переключателем 🇷🇺/🇬🇧).
      2) cfg["templates"][name] — legacy-override до v1.10.0 (только RU).
      3) `_DEFAULT_TEMPLATES` / `_DEFAULT_TEMPLATES_EN` — встроенные дефолты.
      4) RU как последний fallback, если EN-ключ отсутствует.

    Язык:
      - Если задан явно `lang` — берём его.
      - Иначе если задан `buyer_id` — читаем из buyer_lang.json
        (по умолчанию `cfg.default_language`).
      - Иначе RU.
    """
    cfg = get_config()
    if lang is None:
        lang = (_get_buyer_lang(buyer_id) if buyer_id
                else (cfg.get("default_language", "ru") or "ru"))
    if lang not in ("ru", "en"):
        lang = "ru"

    legacy_overrides = cfg.get("templates") or {}
    file_overrides = _load_templates_file(lang)
    if lang == "ru":
        tpl = (file_overrides.get(name)
               or legacy_overrides.get(name)
               or _DEFAULT_TEMPLATES.get(name, ""))
    else:
        tpl = (file_overrides.get(name)
               or _DEFAULT_TEMPLATES_EN.get(name, "")
               or _DEFAULT_TEMPLATES.get(name, ""))
    for k, v in kwargs.items():
        tpl = tpl.replace("{" + k + "}", str(v))
    return tpl


# ── История ──────────────────────────────────────────────────────────────────
def _log_event(event_type: str, **extra: Any) -> None:
    entry: dict[str, Any] = {"ts": _now(), "event": event_type}
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
        LOGGER.debug("steam_offline: failed to log event", exc_info=True)


def list_history() -> list[dict[str, Any]]:
    return _load_json(HISTORY_FILE, [])


def export_history_csv() -> bytes:
    history = list_history()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["timestamp", "event", "assignment_id", "alias",
                     "lot_id", "game_key", "buyer_username", "buyer_id",
                     "order_id", "codes_used", "codes_limit", "free",
                     "error"])
    for entry in history:
        writer.writerow([
            _fmt_ts(entry.get("ts", 0)),
            entry.get("event", ""),
            entry.get("assignment_id", ""),
            entry.get("alias", ""),
            entry.get("lot_id", ""),
            entry.get("game_key", ""),
            entry.get("buyer_username", ""),
            entry.get("buyer_id", ""),
            entry.get("order_id", ""),
            entry.get("codes_used", ""),
            entry.get("codes_limit", ""),
            entry.get("free", ""),
            entry.get("error", ""),
        ])
    return output.getvalue().encode("utf-8-sig")


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


# ── Accounts (СВОЙ пул) ──────────────────────────────────────────────────────
_lock = threading.RLock()


def list_accounts() -> list[dict[str, Any]]:
    return _load_json(ACCOUNTS_FILE, [])


def save_accounts(accs: list[dict[str, Any]]) -> None:
    _save_json(ACCOUNTS_FILE, accs)


def find_account(alias: str) -> dict[str, Any] | None:
    if not alias:
        return None
    for a in list_accounts():
        if a.get("alias", "").lower() == alias.lower():
            return a
    return None


def find_account_by_login(login: str) -> dict[str, Any] | None:
    if not login:
        return None
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


# ── Assignments ──────────────────────────────────────────────────────────────


def list_assignments() -> dict[str, dict[str, Any]]:
    """Возвращает dict {assignment_id: assignment}."""
    return _load_json(ASSIGNMENTS_FILE, {})


def save_assignments(asgns: dict[str, dict[str, Any]]) -> None:
    _save_json(ASSIGNMENTS_FILE, asgns)


def find_assignment(assignment_id: str) -> dict[str, Any] | None:
    return list_assignments().get(assignment_id)


def find_active_assignment_by_alias(alias: str) -> dict[str, Any] | None:
    """Активная (не revoked) выдача для конкретного аккаунта.

    В multi-issue режиме (v1.5+) под одним alias может быть несколько
    активных выдач одновременно; этот helper возвращает ПЕРВУЮ найденную
    (для legacy-кода и обратной совместимости). Для отображения всех
    выдач используй ``find_active_assignments_list_by_alias``.
    """
    if not alias:
        return None
    al = alias.lower()
    for a in list_assignments().values():
        if a.get("status") != "active":
            continue
        if str(a.get("alias", "")).lower() == al:
            return a
    return None


def find_active_assignments_list_by_alias(alias: str) -> list[dict[str, Any]]:
    """Все активные (не revoked) выдачи под одним alias.

    В multi-issue режиме один и тот же аккаунт может быть продан
    нескольким покупателям одновременно. Используется в Telegram-UI,
    чтобы показывать полный список одновременных выдач, а не только
    первую (иначе кажется что аккаунт «выдан» одному покупателю и
    «заморожен», хотя он по-прежнему доступен для новых продаж).
    """
    out: list[dict[str, Any]] = []
    if not alias:
        return out
    al = alias.lower()
    for a in list_assignments().values():
        if a.get("status") != "active":
            continue
        if str(a.get("alias", "")).lower() == al:
            out.append(a)
    # Стабильная сортировка: сначала по ts (если есть), иначе по id.
    out.sort(key=lambda x: (int(x.get("ts", 0) or 0), str(x.get("id", ""))))
    return out


def find_active_assignments_by_buyer(buyer_id: int) -> list[dict[str, Any]]:
    """Все активные выдачи покупателя."""
    out = []
    for a in list_assignments().values():
        if a.get("status") != "active":
            continue
        try:
            if int(a.get("buyer_id", -1)) == int(buyer_id):
                out.append(a)
        except (TypeError, ValueError):
            continue
    return out


def find_active_assignment_by_buyer_and_login(buyer_id: int,
                                              login: str) -> dict[str, Any] | None:
    """Активная выдача покупателю, аккаунт которого соответствует
    введённому логину (alias или account_name)."""
    if not login:
        return None
    target = login.strip().lower()
    for a in find_active_assignments_by_buyer(buyer_id):
        if str(a.get("alias", "")).lower() == target:
            return a
        if str(a.get("account_name", "")).lower() == target:
            return a
    return None


def upsert_assignment(asgn: dict[str, Any]) -> None:
    with _lock:
        asgns = list_assignments()
        asgns[asgn["id"]] = asgn
        save_assignments(asgns)


def count_active_assignments_by_order(order_id: str) -> int:
    """Сколько активных выдач уже создано по этому заказу.

    Используется для идемпотентности: повторный NEW_ORDER (FunPay может
    пере-эмитить событие при реконнекте) не должен выдать второй аккаунт."""
    if not order_id:
        return 0
    target = str(order_id)
    cnt = 0
    for a in list_assignments().values():
        if a.get("status") == "active" and str(a.get("order_id", "")) == target:
            cnt += 1
    return cnt


def revoke_assignment(assignment_id: str) -> bool:
    with _lock:
        asgns = list_assignments()
        a = asgns.get(assignment_id)
        if not a:
            return False
        a["status"] = "revoked"
        a["revoked_at"] = _now()
        save_assignments(asgns)
    return True


def delete_assignment(assignment_id: str) -> bool:
    with _lock:
        asgns = list_assignments()
        if assignment_id not in asgns:
            return False
        asgns.pop(assignment_id)
        save_assignments(asgns)
    return True


def reset_assignment_codes(assignment_id: str) -> bool:
    with _lock:
        asgns = list_assignments()
        a = asgns.get(assignment_id)
        if not a:
            return False
        a["codes_used"] = 0
        save_assignments(asgns)
    return True


def set_assignment_limit(assignment_id: str, new_limit: int) -> bool:
    with _lock:
        asgns = list_assignments()
        a = asgns.get(assignment_id)
        if not a:
            return False
        a["codes_limit"] = max(0, int(new_limit))
        save_assignments(asgns)
    return True


def _new_assignment_id() -> str:
    return _uuid.uuid4().hex[:12]


def _get_unclosed_assignments() -> list[dict[str, Any]]:
    """Находит выдачи, заказ по которым не подтверждён/не закрыт (>24 ч)."""
    asgns = list_assignments()
    now = _now()
    result = []
    for aid, a in asgns.items():
        if a.get("status") != "active":
            continue
        issued_at = a.get("created_at", 0)
        if issued_at and (now - issued_at) > 86400:
            result.append({
                "id": aid,
                "alias": a.get("alias", "?"),
                "buyer_username": a.get("buyer_username", "?"),
                "order_id": a.get("order_id", ""),
                "issued_at": _fmt_ts(issued_at),
                "age_hours": (now - issued_at) // 3600,
            })
    return result


# ── Lots (офлайн-лоты) ───────────────────────────────────────────────────────
def list_lots() -> dict[str, dict[str, Any]]:
    return _load_json(LOTS_FILE, {})


def save_lots(lots: dict[str, dict[str, Any]]) -> None:
    _save_json(LOTS_FILE, lots)


def set_lot(lot_id_or_keyword: str, *, aliases: list[str], game: str = "",
            guard_limit: int | None = None,
            denuvo: bool | None = None,
            denuvo_limit: int | None = None,
            game_key: str | None = None,
            kind: str | None = None) -> None:
    with _lock:
        lots = list_lots()
        existing = lots.get(str(lot_id_or_keyword), {})
        if kind is None:
            effective_kind = "ext" if (denuvo is None
                                      and existing.get("is_extension")) else "main"
        else:
            effective_kind = str(kind)
        # v1.8.0: tri-state denuvo. Если параметр denuvo=None — сохраняем
        # текущее значение существующего лота (или None для новых).
        # None означает «наследовать от game.denuvo» в _lot_is_denuvo.
        if denuvo is None:
            _denuvo_val = existing.get("denuvo")  # может быть None / True / False
        else:
            _denuvo_val = bool(denuvo)
        lots[str(lot_id_or_keyword)] = {
            "aliases": aliases,
            "game": game or existing.get("game", ""),
            "game_key": (game_key
                         if game_key is not None
                         else existing.get("game_key", "")),
            "kind": effective_kind,
            "guard_limit": (guard_limit if guard_limit is not None
                            else existing.get("guard_limit")),
            "denuvo": _denuvo_val,
            "denuvo_limit": (denuvo_limit if denuvo_limit is not None
                              else existing.get("denuvo_limit")),
        }
        save_lots(lots)

        # Синхронизируем games.json: добавляем lot_id в lot_ids/ext_lot_ids
        # нужной игры (как в steam_rental.set_lot).
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
                # Если лот переехал из main в ext (или наоборот) — убираем
                # его из противоположного списка.
                other_key = ("lot_ids" if effective_kind == "ext"
                             else "ext_lot_ids")
                g[other_key] = [x for x in (g.get(other_key) or [])
                                 if str(x) != str(lot_id_or_keyword)]
                games[gk] = g
                save_games(games)


def delete_lot(lot_id_or_keyword: str) -> bool:
    with _lock:
        lots = list_lots()
        if str(lot_id_or_keyword) not in lots:
            return False
        gk = lots[str(lot_id_or_keyword)].get("game_key") or ""
        del lots[str(lot_id_or_keyword)]
        save_lots(lots)
        # Снимаем lot_id из lot_ids/ext_lot_ids у привязанной игры,
        # чтобы в games.json не оставалось «висящих» ссылок.
        if gk:
            games = list_games()
            g = games.get(gk)
            if g:
                lid = str(lot_id_or_keyword)
                changed = False
                for key in ("lot_ids", "ext_lot_ids"):
                    new_lst = [x for x in (g.get(key) or [])
                               if str(x) != lid]
                    if new_lst != list(g.get(key) or []):
                        g[key] = new_lst
                        changed = True
                if changed:
                    games[gk] = g
                    save_games(games)
        return True


# ── Games (game → lots, общая иерархия) ───────────────────────────────────
def list_games() -> dict[str, dict[str, Any]]:
    return _load_json(GAMES_FILE, {})


def save_games(games: dict[str, dict[str, Any]]) -> None:
    _save_json(GAMES_FILE, games)


def get_game(game_key: str) -> dict[str, Any] | None:
    if not game_key:
        return None
    return list_games().get(str(game_key))


def _slugify_game(name: str) -> str:
    import re as _re
    if not name:
        return ""
    s = name.strip().lower()
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
            "ts": int(time.time()),
        }
        save_games(games)
    return key


def delete_game(game_key: str) -> bool:
    """Удаляет игру и снимает game_key с её лотов (лоты остаются)."""
    with _lock:
        games = list_games()
        if str(game_key) not in games:
            return False
        del games[str(game_key)]
        save_games(games)
        # Снимаем game_key с лотов этой игры (лоты не удаляем —
        # они продолжат работать через legacy match по lot_id).
        lots = list_lots()
        changed = False
        for lid, lot in list(lots.items()):
            if lot.get("game_key") == str(game_key):
                lot["game_key"] = ""
                changed = True
        if changed:
            save_lots(lots)
    return True


def add_lot_to_game(game_key: str, lot_id: str, kind: str = "main") -> bool:
    """Привязывает существующий лот к игре (kind: 'main' или 'ext').

    Если лот ещё не существует в lots.json — создаёт пустой каркас.
    """
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
        # Уберём из противоположного списка, если переехал
        other_key = "ext_lot_ids" if kind == "main" else "lot_ids"
        g[other_key] = [x for x in (g.get(other_key) or [])
                         if str(x) != str(lot_id)]
        games[str(game_key)] = g
        save_games(games)

        # Создаём/обновляем сам лот, чтобы у него был game_key и kind.
        lots = list_lots()
        existing = lots.get(str(lot_id), {})
        lots[str(lot_id)] = {
            "aliases": existing.get("aliases", []),
            "game": existing.get("game", "") or g.get("name", ""),
            "game_key": str(game_key),
            "kind": kind,
            "guard_limit": existing.get("guard_limit"),
            "denuvo": existing.get("denuvo", False),
            "denuvo_limit": existing.get("denuvo_limit"),
        }
        save_lots(lots)
    return True


def remove_lot_from_game(game_key: str, lot_id: str,
                          kind: str = "main") -> bool:
    """Отвязывает лот от игры (lot из lots.json не удаляется)."""
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
        # Сбрасываем game_key у лота, если он указывал на эту игру.
        lots = list_lots()
        lot = lots.get(str(lot_id))
        if lot and lot.get("game_key") == str(game_key):
            lot["game_key"] = ""
            save_lots(lots)
    return True


def _match_lot(order_desc: str, lot_id: str | None) -> dict[str, Any] | None:
    """Legacy-фоллбэк: по lot_id или ключевому слову."""
    lots = list_lots()
    if lot_id and str(lot_id) in lots:
        return {"key": str(lot_id), **lots[str(lot_id)]}
    desc_low = (order_desc or "").lower()
    for key, val in lots.items():
        if not key.isdigit() and key.lower() in desc_low:
            return {"key": key, **val}
    return None


def _match_lot_by_game(order_title: str) -> dict[str, Any] | None:
    """Матчит заказ по `Order.title` к играм в games.json (новое).

    Стратегия (как в steam_rental):
      1. Найти игру с самым длинным совпадением имени в названии заказа.
      2. Из её main-лотов:
         a) если есть лот со свободными аккаунтами — возвращаем его;
         b) иначе возвращаем первый main-лот (чтобы покупатель получил
            корректное сообщение "no_accounts" вместо тихого пропуска).
    """
    if not order_title:
        return None
    title_low = order_title.lower()
    games = list_games()
    if not games:
        return None
    lots = list_lots()

    cands: list[tuple[dict, int]] = []
    for gkey, g in games.items():
        gname = (g.get("name") or "").strip()
        if not gname:
            continue
        gn_low = gname.lower()
        if gn_low in title_low:
            cands.append((g, len(gn_low)))
        else:
            for token in re.findall(r"[\w\-]{3,}", gn_low):
                if token in title_low:
                    cands.append((g, len(token)))
                    break
    if not cands:
        return None
    cands.sort(key=lambda x: -x[1])

    # Двухпроходный поиск: сначала ищем main-лот со свободными,
    # потом — любой main-лот (чтобы выдать корректное "no_accounts").
    fallback: dict[str, Any] | None = None
    for g, _ in cands:
        for lot_id in g.get("lot_ids") or []:
            lot = lots.get(str(lot_id))
            if not lot:
                continue
            if (lot.get("kind") or "main") != "main":
                continue
            aliases = _combined_lot_pool_offline(lot)
            if aliases and any(not _is_alias_busy_offline(a)
                                for a in aliases):
                return {"key": str(lot_id), **lot}
            if fallback is None:
                fallback = {"key": str(lot_id), **lot}
    return fallback


def _combined_lot_pool_offline(lot: dict[str, Any]) -> list[str]:
    """Гибридный пул aliases лота:
       1) per-game `global_aliases` из games.json (legacy ручной список);
       2) v1.8.0: все аккаунты, у которых `acc.game_key == lot.game_key`
          (привязка аккаунт↔игра — выставляется через ⚙ TG-меню «🎮 Игра»
          в карточке аккаунта или через picker «👥 Аккаунты игры» в карточке
          игры). Один раз поставил game_key — акк попадает в пул всех
          лотов этой игры автоматически.
       3) per-lot `aliases` (legacy / точечная привязка).
    Дедупликация сохраняет порядок (приоритет game-pool над lot-pool).
    """
    seen: set[str] = set()
    out: list[str] = []
    gkey = (lot.get("game_key") or "").strip()
    if gkey:
        g = get_game(gkey)
        # 1) global_aliases на уровне игры (legacy)
        if g:
            for a in g.get("global_aliases") or []:
                al = str(a).lower()
                if al not in seen:
                    seen.add(al)
                    out.append(str(a))
        # 2) v1.8.0: акки с тем же game_key
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
                "steam_offline: combined_lot_pool game_key scan failed",
                exc_info=True)
    # 3) per-lot aliases
    for a in lot.get("aliases") or []:
        al = str(a).lower()
        if al not in seen:
            seen.add(al)
            out.append(str(a))
    return out


def _is_alias_busy_offline(alias: str) -> bool:
    """True если alias недоступен для новой выдачи.

    В режиме multi-issue (default, v1.5+): занятым считается только
    замороженный/несуществующий аккаунт — наличие активного assignment
    НЕ блокирует, т.к. один акк может быть выдан нескольким покупателям
    одновременно (у каждого свой счётчик !код).

    В legacy single-issue режиме (config.allow_multi_issue=False):
    наличие активного assignment тоже блокирует (старое поведение)."""
    acc = find_account(alias)
    if not acc:
        return True
    if acc.get("frozen") or acc.get("status") == "frozen":
        return True
    if not get_config().get("allow_multi_issue", True):
        if find_active_assignment_by_alias(alias):
            return True
    return False


def _migrate_lots_to_games_so(cardinal: "Cardinal") -> None:
    """v6: миграция в games.json (steam_offline).

    Идемпотентно: если game_key уже проставлен у лота — пропускаем.
    """
    try:
        lots = list_lots()
        if not lots:
            return
        if all(lot.get("game_key") for lot in lots.values()):
            return
        account_obj = getattr(cardinal, "account", None) if cardinal else None
        for lot_id, lot in lots.items():
            try:
                if lot.get("game_key"):
                    continue
                game_name = (lot.get("game") or "").strip()
                if not game_name:
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
                            "steam_offline: migrate get_lot_fields(%s) failed",
                            lot_id, exc_info=True)
                gkey = set_game(_slugify_game(game_name), game_name,
                                subcategory_id=sub_id, category_id=cat_id)
                with _lock:
                    lots = list_lots()
                    if lot_id in lots:
                        lots[lot_id]["game_key"] = gkey
                        lots[lot_id]["kind"] = lots[lot_id].get("kind") or "main"
                    save_lots(lots)
                LOGGER.info(
                    "steam_offline: migrate: lot %s → game '%s' (key=%s)",
                    lot_id, game_name, gkey)
            except Exception:
                LOGGER.warning(
                    "steam_offline: migrate: failed for lot %s", lot_id,
                    exc_info=True)
    except Exception:
        LOGGER.error("steam_offline: games migration crashed", exc_info=True)


def _pick_free_alias_for_offline(
    aliases: list[str],
    exclude_aliases: set[str] | None = None,
) -> str | None:
    """Выбирает alias из СВОЕГО пула для новой выдачи.

    Поведение зависит от config.allow_multi_issue:

      * multi-issue (default, v1.5+): один и тот же аккаунт можно выдать
        нескольким покупателям одновременно. Picker возвращает alias
        с НАИМЕНЬШИМ числом активных выдач (least-loaded балансировка),
        чтобы покупатели равномерно распределялись по пулу.

      * legacy (allow_multi_issue=False): один аккаунт = одна активная
        выдача за раз; alias с активным assignment пропускается.

    Замороженные аккаунты (frozen / status="frozen") пропускаются всегда.
    `exclude_aliases` — нижний регистр, набор alias-ов уже выданных в
    рамках одного NEW_ORDER (защита от дубля кредов внутри заказа
    с amount > 1).

    Для Denuvo используется `_pick_denuvo_alias` (см. логику daily-слотов).
    """
    excl = {a.lower() for a in (exclude_aliases or set())}
    multi = bool(get_config().get("allow_multi_issue", True))
    asgns = list_assignments()

    # Считаем активные выдачи на каждый alias — нужно и для legacy-фильтра,
    # и для multi-issue load-balancing.
    counts: dict[str, int] = {}
    for a in asgns.values():
        if a.get("status") != "active":
            continue
        al = str(a.get("alias", "")).lower()
        if al:
            counts[al] = counts.get(al, 0) + 1

    best: str | None = None
    best_count = -1
    for alias in aliases:
        acc = find_account(alias)
        if not acc:
            continue
        if acc.get("frozen") or acc.get("status") == "frozen":
            continue
        al_low = str(alias).lower()
        if al_low in excl:
            continue
        cur = counts.get(al_low, 0)
        if not multi:
            # Legacy: alias считается «занятым» при любом активном asgn.
            if cur > 0:
                continue
            return alias
        # Multi-issue: ищем alias с минимальным числом активных выдач.
        if best is None or cur < best_count:
            best = alias
            best_count = cur
            if best_count == 0:
                # Уже нашли совсем свободный — раньше можно не выходить,
                # т.к. для распределения хватает любого нулевого. Но чтобы
                # был детерминированный порядок (первый встретившийся
                # с min-count) — оставляем full-loop.
                continue
    return best


def _pick_denuvo_alias(
    aliases: list[str],
    lot_limit: int,
    exclude_aliases: set[str] | None = None,
) -> str | None:
    """Denuvo-ротация: выбирает аккаунт с максимальным числом свободных
    «устройств в день». Активные assignment'ы НЕ блокируют — каждый покупатель
    занимает один из 5 (по умолч.) Denuvo-слотов на этот UTC-день.

    Аккаунт «свободен», если: не заморожен и `denuvo_count` за сегодняшний
    UTC-день меньше `lot_limit`. `exclude_aliases` — alias-ы, уже выданные
    в рамках одного NEW_ORDER (защита от дубля внутри заказа с amount > 1)."""
    excl = {a.lower() for a in (exclude_aliases or set())}
    best: str | None = None
    best_left = 0
    for alias in aliases:
        acc = find_account(alias)
        if not acc:
            continue
        if acc.get("frozen"):
            continue
        if str(alias).lower() in excl:
            continue
        left = _denuvo_slots_left(acc, lot_limit)
        if left <= 0:
            continue
        # Берём аккаунт с максимальным числом оставшихся слотов — равномерная
        # ротация. При равенстве — первый попавшийся.
        if left > best_left:
            best = alias
            best_left = left
    return best


def _pick_free_alias_for_lot(
    lot: dict[str, Any],
    exclude_aliases: set[str] | None = None,
) -> str | None:
    """Универсальный выбор: учитывает флаг Denuvo лота. `exclude_aliases`
    защищает от выдачи одного и того же аккаунта дважды в рамках одного
    NEW_ORDER с amount > 1."""
    aliases = lot.get("aliases", []) or []
    if _lot_is_denuvo(lot):
        return _pick_denuvo_alias(
            aliases, _denuvo_lot_limit(lot), exclude_aliases=exclude_aliases)
    return _pick_free_alias_for_offline(
        aliases, exclude_aliases=exclude_aliases)


def _count_free_for_offline(aliases: list[str]) -> int:
    """Сколько аккаунтов считается «свободными» в текущем режиме.

    В multi-issue (default): любой не-замороженный alias — свободен (его
    можно выдать ещё одному покупателю). Возвращаем количество таких.

    В legacy single-issue: считаем alias-ы без активной выдачи (старое
    поведение)."""
    multi = bool(get_config().get("allow_multi_issue", True))
    if multi:
        cnt = 0
        for alias in aliases:
            acc = find_account(alias)
            if not acc:
                continue
            if acc.get("frozen") or acc.get("status") == "frozen":
                continue
            cnt += 1
        return cnt
    return sum(1 for a in aliases if _pick_free_alias_for_offline([a]))


def _count_free_for_lot(lot: dict[str, Any]) -> int:
    """Свободные акки с учётом Denuvo. Для Denuvo считается по слотам."""
    aliases = lot.get("aliases", []) or []
    if not _lot_is_denuvo(lot):
        return _count_free_for_offline(aliases)
    limit = _denuvo_lot_limit(lot)
    cnt = 0
    for a in aliases:
        acc = find_account(a)
        if not acc or acc.get("frozen"):
            continue
        if _denuvo_slots_left(acc, limit) > 0:
            cnt += 1
    return cnt


# ── Авто-(де)активация лотов FunPay + raise-skip + actions.log ──────────────
# Реализация — поверх общей библиотеки lot_activation_common.py. Если её рядом
# нет (например при изолированной установке) — функции просто log.debug и не
# падают (плагин продолжит работать без авто-активации).

_LOT_ACTIVATION_CACHE: dict[str, dict[str, Any]] = {}
_RAISE_LOT_TO_CATEGORY: dict[str, int] = {}
_RAISE_SKIP_CATEGORY_IDS: set[int] = set()
_RAISE_PATCH_INSTALLED: bool = False
_actions_logger_so: logging.Logger | None = None


# ── Встроенная либа lot-activation ─────────────────────────────────────────
# Общее состояние raise-skip хранится на cardinal.account, чтобы несколько
# плагинов делили один dict (см. steam_rental.py — там аналогичный блок).
def _shared_raise_state_so(cardinal: "Cardinal"):
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


def _install_raise_skip_shared_so(cardinal: "Cardinal") -> bool:
    st = _shared_raise_state_so(cardinal)
    if st is None:
        return False
    if st["patched"]:
        return True
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
                LOGGER.info(
                    "raise-skip: пропуск авто-поднятия категории %s "
                    "(плагин: %s)", cid, owner or "?")
                return True
        except Exception:
            LOGGER.debug("raise-skip: check failed", exc_info=True)
        return orig(category_id, subcategories=subcategories,
                     exclude=exclude)

    _patched._lot_raise_patched = True  # type: ignore[attr-defined]
    acc.raise_lots = _patched  # type: ignore[method-assign]
    st["patched"] = True
    LOGGER.info(
        "raise-skip: установлен общий патч raise_lots — "
        "категории, зарегистрированные плагинами, будут пропускаться")
    return True


def _register_skip_so(cardinal: "Cardinal", plugin_name: str,
                       category_ids) -> None:
    st = _shared_raise_state_so(cardinal)
    if st is None:
        return
    st["by_plugin"][plugin_name] = {int(x) for x in category_ids
                                      if x is not None}


def _get_funpay_account_so(cardinal):
    if cardinal is None:
        return None
    acc = getattr(cardinal, "account", None)
    if acc is not None and (hasattr(acc, "save_lot")
                            or hasattr(acc, "save_offer")):
        return acc
    if hasattr(cardinal, "save_lot") or hasattr(cardinal, "save_offer"):
        return cardinal
    return None


def _apply_lot_active_so(cardinal, lot_id: int, active: bool) -> bool:
    acc = _get_funpay_account_so(cardinal)
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


def _detect_category_id_so(cardinal, lot_id: int):
    acc = _get_funpay_account_so(cardinal)
    if acc is None or not hasattr(acc, "get_lot_fields"):
        return None
    try:
        fields = acc.get_lot_fields(int(lot_id))
    except Exception:
        return None
    cat = getattr(getattr(fields, "subcategory", None), "category", None)
    cid = getattr(cat, "id", None)
    return int(cid) if cid is not None else None


_ACTIONS_ICONS_SO = {
    "lot_activated":   "✅ ЛОТ ВКЛ ",
    "lot_deactivated": "⛔ ЛОТ ВЫКЛ",
    "lot_save_failed": "⚠ ЛОТ FAIL",
    "delivery":        "📨 ВЫДАЧА  ",
    "rental_end":      "🏁 КОНЕЦ   ",
    "acc_freeze":      "❄️ ЗАМОР   ",
    "acc_unfreeze":    "🔥 РАЗМОР  ",
    "acc_vac_ban":     "🚨 VAC BAN ",
    "raise_skipped":   "🚫 RAISE   ",
    "reactivation":    "🔁 ПЕРЕАКТ ",
}


def _make_actions_logger_so(plugin_name: str, storage_dir: str):
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
        LOGGER.debug("steam_offline: actions logger init failed",
                     exc_info=True)
        return None


def _do_log_action_so(actions_logger, action: str, summary: str = "",
                      **extra) -> None:
    if actions_logger is None:
        return
    icon = _ACTIONS_ICONS_SO.get(action, f"• {action:10}")
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
        pass


def _common_lib():
    """Shim — возвращает объект с теми же методами, что у внешней либы.
    Также поддерживает внешний lot_activation_common, если он есть."""
    try:
        import lot_activation_common  # type: ignore
        return lot_activation_common
    except Exception:
        pass

    class _Shim:
        @staticmethod
        def get_funpay_account(c):
            return _get_funpay_account_so(c)

        @staticmethod
        def apply_lot_active(c, lid, act):
            return _apply_lot_active_so(c, int(lid), bool(act))

        @staticmethod
        def install_raise_skip_patch(c):
            return _install_raise_skip_shared_so(c)

        @staticmethod
        def register_skip_categories(pname, ids):
            from_c = _CARDINAL_REF_SO
            _register_skip_so(from_c, pname, ids)

        @staticmethod
        def detect_category_id(c, lid):
            return _detect_category_id_so(c, int(lid))

        @staticmethod
        def make_actions_logger(pname, sdir):
            return _make_actions_logger_so(pname, sdir)

        @staticmethod
        def log_action(lg, action, summary="", **extra):
            _do_log_action_so(lg, action, summary, **extra)

    return _Shim()


# Локальная ссылка на cardinal (для register_skip_categories shim'а).
_CARDINAL_REF_SO = None


def _get_actions_logger_so() -> logging.Logger | None:
    """actions.log для steam_offline через общую либу."""
    global _actions_logger_so
    if _actions_logger_so is not None:
        return _actions_logger_so
    lib = _common_lib()
    if lib is None:
        return None
    _actions_logger_so = lib.make_actions_logger("steam_offline", STORAGE_DIR)
    return _actions_logger_so


def _log_action_so(action: str, summary: str = "", **extra: Any) -> None:
    lib = _common_lib()
    if lib is None:
        return
    lib.log_action(_get_actions_logger_so(), action, summary, **extra)


def _save_lot_state_so() -> None:
    try:
        _save_json(LOT_STATE_FILE, _LOT_ACTIVATION_CACHE)
    except Exception:
        LOGGER.debug("steam_offline: save lot state failed", exc_info=True)


def _load_raise_skip_so() -> None:
    global _RAISE_SKIP_CATEGORY_IDS, _RAISE_LOT_TO_CATEGORY
    data = _load_json(RAISE_SKIP_FILE, {})
    try:
        ids = data.get("category_ids") or []
        _RAISE_SKIP_CATEGORY_IDS = {int(x) for x in ids if str(x).strip()}
    except Exception:
        _RAISE_SKIP_CATEGORY_IDS = set()
    try:
        m = data.get("lot_to_category") or {}
        _RAISE_LOT_TO_CATEGORY = {str(k): int(v) for k, v in m.items()
                                   if str(v).strip()}
    except Exception:
        _RAISE_LOT_TO_CATEGORY = {}


def _save_raise_skip_so() -> None:
    try:
        _save_json(RAISE_SKIP_FILE, {
            "category_ids": sorted(_RAISE_SKIP_CATEGORY_IDS),
            "lot_to_category": _RAISE_LOT_TO_CATEGORY,
            "ts": int(time.time()),
        })
    except Exception:
        LOGGER.debug("steam_offline: save raise-skip cache failed",
                     exc_info=True)


def _refresh_raise_skip_so(cardinal: "Cardinal") -> set[int]:
    """Идёт по lots.json, через get_lot_fields собирает category_id наших
    лотов. Регистрирует множество в общей либе для патча raise_lots.
    """
    global _RAISE_SKIP_CATEGORY_IDS, _RAISE_LOT_TO_CATEGORY
    lib = _common_lib()
    if lib is None or cardinal is None:
        return set(_RAISE_SKIP_CATEGORY_IDS)
    acc = lib.get_funpay_account(cardinal)
    if acc is None or not hasattr(acc, "get_lot_fields"):
        return set(_RAISE_SKIP_CATEGORY_IDS)

    lots = list_lots()
    numeric_ids = [k for k in lots.keys() if str(k).isdigit()]
    if not numeric_ids:
        _RAISE_SKIP_CATEGORY_IDS = set()
        _RAISE_LOT_TO_CATEGORY = {}
        _save_raise_skip_so()
        lib.register_skip_categories("steam_offline", set())
        return set()

    changed = False
    for lot_id in numeric_ids:
        if lot_id in _RAISE_LOT_TO_CATEGORY:
            continue
        cid = lib.detect_category_id(cardinal, int(lot_id))
        if cid is not None:
            _RAISE_LOT_TO_CATEGORY[str(lot_id)] = int(cid)
            changed = True

    stale = [k for k in _RAISE_LOT_TO_CATEGORY if k not in numeric_ids]
    if stale:
        for k in stale:
            _RAISE_LOT_TO_CATEGORY.pop(k, None)
        changed = True

    new_set = {cid for cid in _RAISE_LOT_TO_CATEGORY.values()}
    if new_set != _RAISE_SKIP_CATEGORY_IDS:
        _RAISE_SKIP_CATEGORY_IDS = new_set
        changed = True

    if changed:
        _save_raise_skip_so()
        LOGGER.info("steam_offline: raise-skip обновлены: %s",
                    sorted(_RAISE_SKIP_CATEGORY_IDS) or "—")
    lib.register_skip_categories("steam_offline", _RAISE_SKIP_CATEGORY_IDS)
    return set(_RAISE_SKIP_CATEGORY_IDS)


def _install_raise_skip_so(cardinal: "Cardinal") -> None:
    """Ставит общий патч raise_lots (через lot_activation_common) и
    регистрирует наши категории."""
    global _RAISE_PATCH_INSTALLED
    if _RAISE_PATCH_INSTALLED:
        return
    lib = _common_lib()
    if lib is None:
        LOGGER.debug(
            "steam_offline: lot_activation_common недоступен — "
            "raise-skip не активирован")
        return
    try:
        lib.register_skip_categories("steam_offline",
                                      _RAISE_SKIP_CATEGORY_IDS)
    except Exception:
        LOGGER.debug("steam_offline: register_skip_categories failed",
                     exc_info=True)
    if lib.install_raise_skip_patch(cardinal):
        _RAISE_PATCH_INSTALLED = True
        LOGGER.info(
            "steam_offline: используется общий патч raise_lots из "
            "lot_activation_common")


def _update_lot_activation_so(cardinal: "Cardinal", *, force: bool = False,
                               verbose: bool = False) -> dict[str, Any]:
    """Деактивирует лоты без свободных аккаунтов, активирует обратно.

    Аналог steam_rental._update_lot_activation. Для каждого числового
    lot_id из lots.json вычисляет _count_free_for_lot и зовёт
    lot_activation_common.apply_lot_active.

    :param force: игнорировать config.auto_deactivate_lots (для кнопок).
    """
    counters: dict[str, Any] = {
        "activated": 0, "deactivated": 0, "skipped": 0, "failed": 0,
        "total_lots": 0, "numeric_lots": 0,
        "stopped_reason": None, "api_method": "save_lot",
        "failures": [],
    }
    cfg = get_config()
    if not force and not cfg.get("auto_deactivate_lots"):
        counters["stopped_reason"] = "auto_deactivate_lots выключен"
        return counters
    if cardinal is None:
        counters["stopped_reason"] = "cardinal=None"
        return counters

    lib = _common_lib()
    if lib is None:
        counters["stopped_reason"] = (
            "lot_activation_common.py не найден рядом с плагином")
        counters["api_method"] = None
        return counters

    acc = lib.get_funpay_account(cardinal)
    if acc is None or not hasattr(acc, "get_lot_fields"):
        counters["stopped_reason"] = (
            "cardinal.account.save_lot/get_lot_fields недоступен")
        counters["api_method"] = None
        return counters

    lots = list_lots()
    counters["total_lots"] = len(lots)
    counters["numeric_lots"] = sum(1 for k in lots if str(k).isdigit())
    if not lots:
        counters["stopped_reason"] = "лотов в базе нет"
        return counters

    now_ts = int(time.time())
    for key, val in lots.items():
        if not str(key).isdigit():
            counters["skipped"] += 1
            continue
        free = _count_free_for_lot(val)
        want_active = free > 0
        prev = _LOT_ACTIVATION_CACHE.get(str(key)) or {}
        prev_active = prev.get("active")
        state_changed = (prev_active is None
                         or bool(prev_active) != bool(want_active))
        try:
            lib.apply_lot_active(cardinal, int(key), want_active)
            _LOT_ACTIVATION_CACHE[str(key)] = {
                "active": bool(want_active),
                "ts": now_ts, "result": "ok",
            }
            if want_active:
                counters["activated"] += 1
                _log_action_so("lot_activated",
                                f"Лот {key} активирован",
                                lot_id=key, free=free)
                # В TG-историю шлём только при смене состояния, чтобы не
                # засорять её одинаковыми событиями каждый цикл проверки.
                if state_changed:
                    _log_event("lot_activated", lot_id=str(key), free=free,
                               game=val.get("game") or "")
                if verbose:
                    LOGGER.info(
                        "steam_offline: лот %s активирован (%d свободных)",
                        key, free)
            else:
                counters["deactivated"] += 1
                _log_action_so("lot_deactivated",
                                f"Лот {key} деактивирован — нет свободных",
                                lot_id=key, free=free)
                if state_changed:
                    _log_event("lot_deactivated", lot_id=str(key), free=free,
                               game=val.get("game") or "")
                LOGGER.info(
                    "steam_offline: лот %s деактивирован (нет свободных)",
                    key)
        except Exception as e:
            counters["failed"] += 1
            counters["failures"].append({
                "lot": str(key),
                "error": f"{type(e).__name__}: {str(e)[:150]}",
            })
            _LOT_ACTIVATION_CACHE[str(key)] = {
                "active": None, "ts": now_ts, "result": "fail",
            }
            _log_action_so("lot_save_failed",
                            f"Не удалось сохранить лот {key}",
                            lot_id=key, want_active=want_active,
                            error=f"{type(e).__name__}: {str(e)[:120]}")
            _log_event("lot_save_failed", lot_id=str(key),
                       want_active=bool(want_active),
                       error=f"{type(e).__name__}: {str(e)[:120]}")
            LOGGER.warning(
                "steam_offline: save_lot(%s, active=%s) failed: %s",
                key, want_active, str(e)[:200], exc_info=True)
    _save_lot_state_so()
    return counters


def _sum_denuvo_slots_for_lot(lot: dict[str, Any]) -> tuple[int, int]:
    """Возвращает (использовано_сегодня, всего_лимит) по всем
    Denuvo-аккаунтам лота (для статистики в TG-меню)."""
    aliases = lot.get("aliases", []) or []
    limit = _denuvo_lot_limit(lot)
    total = 0
    used = 0
    for a in aliases:
        acc = find_account(a)
        if not acc or acc.get("frozen"):
            continue
        total += limit
        _, cnt = _denuvo_get_counter(acc)
        used += min(cnt, limit)
    return used, total


def list_account_assignment(alias: str) -> dict[str, Any] | None:
    """Возвращает активную выдачу по этому аккаунту, если есть."""
    return find_active_assignment_by_alias(alias)


# ── v5: Per-account аналитика + Prometheus метрики ───────────────────────────
def _load_metrics() -> dict[str, Any]:
    return _load_json(METRICS_FILE, {
        "delivered_total": 0,
        "guard_sent_total": 0,
        "guard_limit_reached_total": 0,
        "operator_freeze_total": 0,
        "operator_replace_total": 0,
        # v1.9.0
        "blocked_blacklist_total": 0,
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
    asgns = list_assignments()
    snap: dict[str, float] = {
        "aso_accounts_total": float(len(accs)),
        "aso_accounts_frozen": float(sum(1 for a in accs if a.get("frozen"))),
        "aso_accounts_assigned": float(sum(
            1 for a in accs
            if find_active_assignment_by_alias(a["alias"]))),
        "aso_assignments_active": float(sum(
            1 for x in asgns.values()
            if (x.get("status") or "active") == "active")),
        # v1.9.0: размер blacklist для дашборда
        "aso_blacklist_size": float(len(list_blacklist())),
    }
    for k in ("delivered_total", "guard_sent_total",
              "guard_limit_reached_total",
              "operator_freeze_total", "operator_replace_total",
              "blocked_blacklist_total", "blacklist_auto_refund_total"):
        snap[f"aso_{k}"] = float(m.get(k, 0))
    return snap


def _bump_acc_stat(alias: str, **fields: Any) -> None:
    """Обновляет acc['stats'] (steam_offline). Поддержка inc_*/add_*/set_*."""
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
                st[k] = v
        acc["stats"] = st
        upsert_account(acc)


# ── v1.9.0: Buyer blacklist ──────────────────────────────────────────────────
# Блок-лист покупателей: записи `{buyer_id, username, reason, ts}`.
# Срабатывает на NEW_ORDER (если `blacklist_enabled=True`) и
# авто-пополняется при REFUND/CANCELED (если `auto_blacklist_on_refund=True`).
# Логика 1-в-1 портирована из steam_rental.py v5+ (с урок v2.22.2: для
# совпадения buyer_id нужно ОБА fallback-источника — event.order и
# full_order, иначе FunPay иногда отдаёт buyer_id=None и проверка тихо
# проходит).
def list_blacklist() -> list[dict[str, Any]]:
    """Список заблокированных покупателей.

    Структура: `[{'buyer_id': int|None, 'username': str|None,
                   'reason': str, 'ts': int}, ...]`.
    """
    return _load_json(BLACKLIST_FILE, [])


def _save_blacklist(items: list[dict[str, Any]]) -> None:
    _save_json(BLACKLIST_FILE, items)


def _normalize_bl_key(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().lower()


def is_blacklisted(buyer_id: Any = None, username: Any = None) -> bool:
    """Совпадение по buyer_id ИЛИ username (case-insensitive).

    Если оба ключа пусты — возвращает False (нет смысла проверять).
    """
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
    """Добавляет покупателя в blacklist. Идемпотентно.

    Возвращает True если запись добавлена (или уже существовала),
    False если оба ключа пустые.
    """
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
    """Удаляет ВСЕ записи, попадающие под любой из ключей.
    Возвращает True, если хотя бы одна запись удалена.
    """
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


# ── v1.9.0: Refund → profit ──────────────────────────────────────────────────
# На REFUND/CANCELED-событие списываем выручку с прибыли аккаунта:
#   1) находим в history последнее событие 'issue' с тем же order_id;
#   2) если уже refunded — повтор не делаем (идемпотентность);
#   3) помечаем issue.refunded=True, refund_ts;
#   4) добавляем новое событие 'refund' с amount=-original_amount,
#      чтобы все формулы выручки (sum по period) автоматически вычитали
#      его. duration/alias/order_id переносятся для трассировки.
#   5) декрементим acc.stats.total_revenue, инкрементим refunded_count.
# Логика портирована из steam_rental v2.16+ (урок v2.22.4: refund-учёт
# должен быть виден ВО ВСЕХ местах статистики, включая per-account periods).
def _apply_refund_to_stats(order_id: Any, buyer_username: str | None,
                           buyer_id: int | None) -> tuple[str | None, float]:
    """Учитывает возврат денег по заказу: вычитает выручку из per-account
    статистики и записывает в history событие 'refund' с отрицательной
    суммой.

    Возвращает (alias, amount) если refund применён или ранее уже был
    учтён; (None, 0.0) — если в history нет 'issue' с таким order_id
    (например, старый заказ до начала ведения per-issue revenue).
    """
    if not order_id:
        return None, 0.0
    try:
        history = _load_json(HISTORY_FILE, [])
    except Exception:
        return None, 0.0
    target = None
    for h in reversed(history):
        if (h.get("event") == "issue"
                and str(h.get("order_id") or "") == str(order_id)):
            target = h
            break
    if target is None:
        return None, 0.0
    amount = float(target.get("amount", 0) or 0)
    alias = target.get("alias") or None
    if target.get("refunded"):
        LOGGER.info(
            "steam_offline: refund для заказа %s уже учтён, пропускаю",
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
        "assignment_id": target.get("assignment_id"),
    })
    try:
        _save_json(HISTORY_FILE, history)
    except Exception:
        LOGGER.error(
            "steam_offline: не удалось сохранить history после refund %s",
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
                "steam_offline: bump_acc_stat refund(%s, %s) failed",
                alias, amount, exc_info=True)
    return alias, amount


# ── Выдача ───────────────────────────────────────────────────────────────────
def deliver_account_offline(cardinal: "Cardinal", *, alias: str,
                             buyer_id: int, buyer_username: str,
                             chat_id: int | str, order_id: str,
                             guard_limit: int,
                             denuvo: bool = False,
                             denuvo_lot_limit: int | None = None,
                             ) -> dict[str, Any] | None:
    """Выдаёт Steam-аккаунт навсегда. Возвращает assignment или None.

    `denuvo=True` снимает блок «один аккаунт = одна активная выдача» и
    инкрементирует daily-счётчик устройств; `denuvo_lot_limit` — лимит из
    конкретного лота (None = из конфига).

    Если `denuvo=False`: блок «1 alias = 1 активная выдача» применяется
    ТОЛЬКО когда в config.allow_multi_issue=False (legacy). По умолчанию
    (multi-issue) один и тот же alias можно выдать нескольким покупателям
    — у каждого свой счётчик !код."""
    cfg = get_config()
    with _lock:
        acc = find_account(alias)
        if not acc:
            LOGGER.warning("steam_offline: аккаунт %s не найден", alias)
            return None
        if acc.get("frozen"):
            LOGGER.warning("steam_offline: аккаунт %s заморожен вручную", alias)
            return None
        # Не-Denuvo: один акк — одна активная выдача (только если в config
        # выключен allow_multi_issue; иначе разрешаем мультивыдачу).
        # Denuvo: разрешаем несколько выдач, проверяем daily-слоты.
        if denuvo:
            d_limit = (int(denuvo_lot_limit)
                       if denuvo_lot_limit and int(denuvo_lot_limit) > 0
                       else int(cfg.get("denuvo_default_limit", 5)))
            if _denuvo_slots_left(acc, d_limit) <= 0:
                LOGGER.warning(
                    "steam_offline: Denuvo daily-лимит %d исчерпан для %s",
                    d_limit, alias)
                return None
        elif not cfg.get("allow_multi_issue", True):
            existing = find_active_assignment_by_alias(alias)
            if existing is not None:
                LOGGER.warning(
                    "steam_offline: аккаунт %s уже выдан по выдаче %s "
                    "(allow_multi_issue=False)",
                    alias, existing["id"])
                return None

        # Резервируем выдачу СРАЗУ (под локом), чтобы alias/Denuvo-слот был
        # занят и параллельный заказ (или повторный NEW_ORDER) не выдал тот
        # же аккаунт повторно.
        asgn_id = _new_assignment_id()
        asgn = {
            "id": asgn_id,
            "alias": alias,
            "account_name": acc["account_name"],
            "buyer_id": int(buyer_id),
            "buyer_username": str(buyer_username),
            "chat_id": int(chat_id) if isinstance(chat_id, str) and chat_id.isdigit()
                       else chat_id,
            "order_id": str(order_id),
            "created_at": _now(),
            "codes_used": 0,
            "codes_limit": int(guard_limit),
            "status": "active",
            "denuvo": bool(denuvo),
        }
        upsert_assignment(asgn)
        # Инкрементируем Denuvo-счётчик уже после upsert, чтобы было
        # консистентно: акк выдан → один из 5 daily-слотов занят.
        if denuvo:
            d_limit = (int(denuvo_lot_limit)
                       if denuvo_lot_limit and int(denuvo_lot_limit) > 0
                       else int(cfg.get("denuvo_default_limit", 5)))
            new_cnt, _, was_reset = _denuvo_increment(alias)
            if was_reset:
                LOGGER.info(
                    "steam_offline: Denuvo день сброшен для %s "
                    "(новая UTC-дата)", alias)
        acc_snapshot = dict(acc)

    # Опционально: меняем пароль перед выдачей. Сетевые вызовы Steam
    # выполняем ВНЕ глобального _lock, чтобы не блокировать весь плагин
    # на время медленных HTTP-запросов. На Denuvo-режим не рекомендуется —
    # там у нескольких покупателей общий пароль.
    if cfg.get("change_password_on_issue") and not denuvo:
        try:
            new_pw = _sr_gen_password()
            sess = SteamSession(
                account_name=acc_snapshot["account_name"],
                password=acc_snapshot["password"],
                shared_secret=acc_snapshot["shared_secret"],
                identity_secret=acc_snapshot["identity_secret"],
                steamid=acc_snapshot.get("steamid"),
            )
            sess.login()
            sess.change_password(new_pw)
            with _lock:
                acc_cur = find_account(alias) or acc_snapshot
                acc_cur["password"] = new_pw
                upsert_account(acc_cur)
                acc_snapshot = dict(acc_cur)
        except Exception:
            LOGGER.error("steam_offline: смена пароля перед выдачей "
                         "не удалась для %s", alias, exc_info=True)

    acc = acc_snapshot
    game = _get_game_for_alias(alias)
    text = _render_template(
        "issue",
        buyer_id=buyer_id,
        login=acc["account_name"],
        password=acc["password"],
        game=game or "—",
        codes_limit=str(guard_limit),
    )
    try:
        cardinal.send_message(chat_id, text, chat_name=buyer_username,
                              interlocutor_id=buyer_id, watermark=False)
        LOGGER.info(
            "steam_offline: выдан %s навсегда покупателю %s (order=%s, "
            "лимит кодов=%d, denuvo=%s, assignment=%s)",
            alias, buyer_username, order_id, guard_limit, denuvo, asgn_id)
        # v1.9.0: цену заказа берём из Cardinal._so_last_price
        # (его выставляет _handler_new_order перед вызовом deliver). Это
        # нужно для refund→profit: 'issue'-event должен содержать amount,
        # иначе при refund/cancel мы не сможем найти оригинальную сумму.
        _last_price_v = getattr(cardinal, "_so_last_price", None)
        try:
            _last_price_f = float(_last_price_v) if _last_price_v is not None else 0.0
        except Exception:
            _last_price_f = 0.0
        # v5: per-account stats + Prometheus
        _bump_acc_stat(
            alias,
            inc_delivered_count=1,
            add_total_revenue=float(_last_price_f or 0),
            set_last_delivered_at=_now(),
            set_last_buyer_id=int(buyer_id),
            set_last_buyer_username=str(buyer_username),
            set_last_order_id=str(order_id),
            set_last_assignment_id=str(asgn_id))
        _metric_inc("delivered_total")
        denuvo_tag = ""
        if denuvo:
            d_limit = (int(denuvo_lot_limit)
                       if denuvo_lot_limit and int(denuvo_lot_limit) > 0
                       else int(cfg.get("denuvo_default_limit", 5)))
            acc_now = find_account(alias) or {}
            _, used = _denuvo_get_counter(acc_now)
            denuvo_tag = (f"\n🛡 Denuvo-слот: <b>{used}/{d_limit}</b>")
            if used >= d_limit:
                denuvo_tag += " — на сегодня лимит исчерпан"
        _notify_tg(cardinal,
                   f"♾ <b>Steam Offline</b>: выдан <code>{alias}</code> "
                   f"навсегда покупателю <b>{buyer_username}</b> "
                   f"(заказ #{order_id}, лимит кодов: {guard_limit}). "
                   f"Выдача: <code>{asgn_id}</code>"
                   f"{denuvo_tag}",
                   op_alias=alias, op_assignment_id=asgn_id)
        _log_event("issue", assignment_id=asgn_id, alias=alias,
                   buyer_username=buyer_username, buyer_id=int(buyer_id),
                   order_id=str(order_id), codes_used=0,
                   codes_limit=int(guard_limit),
                   amount=float(_last_price_f or 0),
                   denuvo=bool(denuvo))
        # Алёрт на исчерпание слотов конкретного аккаунта.
        if denuvo:
            d_limit = (int(denuvo_lot_limit)
                       if denuvo_lot_limit and int(denuvo_lot_limit) > 0
                       else int(cfg.get("denuvo_default_limit", 5)))
            acc_now = find_account(alias) or {}
            _, used = _denuvo_get_counter(acc_now)
            if used >= d_limit:
                _notify_tg(cardinal,
                           f"⛔ <b>Denuvo</b>: у аккаунта <code>{alias}</code> "
                           f"исчерпан daily-лимит ({d_limit}). "
                           f"Слоты сбросятся в 00:00 UTC.")
        _log_action_so("delivery",
                       f"Выдан {alias} → {buyer_username} (offline)",
                       alias=alias, buyer=buyer_username, buyer_id=buyer_id,
                       order_id=order_id, denuvo=denuvo,
                       guard_limit=int(guard_limit))
        try:
            _update_lot_activation_so(cardinal)
        except Exception:
            LOGGER.debug("steam_offline: lot activation after deliver failed",
                         exc_info=True)
        return asgn
    except Exception:
        LOGGER.error("steam_offline: не удалось отправить креды в чат %s",
                     chat_id, exc_info=True)
        # Креды НЕ доставлены покупателю — это инцидент, НЕ считаем успехом.
        # Аккаунт уже зарезервирован (assignment активен / Denuvo-слот занят),
        # поэтому шлём оператору данные для ручной выдачи и возвращаем None,
        # чтобы вызывающий код не засчитал доставку.
        _log_action_so("lot_save_failed",
                       f"Не удалось отправить креды покупателю "
                       f"{buyer_username} (заказ #{order_id})",
                       order_id=order_id, alias=alias,
                       buyer=buyer_username, buyer_id=buyer_id)
        _notify_tg(cardinal,
                   f"⛔ <b>Steam Offline</b>: аккаунт <code>{alias}</code> "
                   f"выдан покупателю <b>{buyer_username}</b> "
                   f"(заказ #{order_id}), но <b>сообщение с кредами не "
                   f"доставлено</b>. Отправьте вручную:\n"
                   f"Логин: <code>{acc.get('account_name', alias)}</code>\n"
                   f"Пароль: <code>{acc.get('password', '?')}</code>\n"
                   f"Выдача: <code>{asgn_id}</code>",
                   op_alias=alias, op_assignment_id=asgn_id)
        return None


def _get_game_for_alias(alias: str) -> str:
    """Определяет game: сначала из аккаунта, потом из офлайн-лотов."""
    acc = find_account(alias)
    if acc and acc.get("game"):
        return acc["game"]
    for val in list_lots().values():
        if alias in val.get("aliases", []) and val.get("game"):
            return val["game"]
    return ""


def _notify_tg(cardinal: "Cardinal", text: str,
               *, op_alias: str | None = None,
               op_assignment_id: str | None = None) -> None:
    """v5: при op_alias/op_assignment_id под уведомлением появится
    inline-клавиатура «🛑 Заморозить / 🔁 Заменить»."""
    cfg = get_config()
    if not cfg.get("tg_notify", True):
        return
    tg = getattr(cardinal, "telegram", None)
    if not tg:
        return
    reply_markup = None
    if (op_alias or op_assignment_id) and cfg.get(
            "operator_buttons_on_issue", True):
        try:
            from telebot import types as tbtypes  # type: ignore
            sid = _sid(op_alias or op_assignment_id or "")
            asg_short = (op_assignment_id or "")[:24]
            reply_markup = tbtypes.InlineKeyboardMarkup(row_width=2)
            reply_markup.add(
                tbtypes.InlineKeyboardButton(
                    "🛑 Заморозить", callback_data=f"so:frz:{sid}"),
                tbtypes.InlineKeyboardButton(
                    "🔁 Заменить",
                    callback_data=f"so:rep:{sid}:{asg_short}"),
            )
            reply_markup.add(tbtypes.InlineKeyboardButton(
                "📊 Статистика аккаунта",
                callback_data=f"so:stat:{sid}"))
        except Exception:
            reply_markup = None
    try:
        for uid in getattr(tg, "authorized_users", []) or []:
            try:
                if reply_markup is not None:
                    tg.bot.send_message(
                        uid, text, parse_mode="HTML",
                        disable_web_page_preview=True,
                        reply_markup=reply_markup)
                else:
                    tg.bot.send_message(
                        uid, text, parse_mode="HTML",
                        disable_web_page_preview=True)
            except Exception:
                LOGGER.debug("steam_offline: tg notify failed for uid=%s", uid,
                             exc_info=True)
    except Exception:
        LOGGER.debug("steam_offline: tg notify outer failed", exc_info=True)


# ── Обработка !код (или настроенной команды) для офлайн-аккаунтов ─────────
# Плагин НЕ зависит от steam_rental — у нас собственный handler
# BIND_TO_NEW_MESSAGE, который смотрит cfg.guardik_command.
def _try_offline_guard_code(cardinal: "Cardinal", msg: Any, text: str) -> bool:
    """Если у автора msg есть активная офлайн-выдача — выдаёт код и считает
    лимит. Возвращает True если обработали (steam_rental вызывать не нужно)."""
    try:
        author_id = msg.author_id
        chat_id = msg.chat_id
        chat_name = getattr(msg, "chat_name", None)

        parts = text.split(None, 1)
        requested = parts[1].strip() if len(parts) > 1 else None

        # Сначала ищем по логину, если он указан и принадлежит этому покупателю.
        asgn: dict[str, Any] | None = None
        if requested:
            asgn = find_active_assignment_by_buyer_and_login(
                int(author_id), requested)
        if asgn is None:
            buyer_active = find_active_assignments_by_buyer(int(author_id))
            if requested and not asgn and buyer_active:
                # Покупатель ввёл логин чужой выдачи — пусть отвалится
                # на исходном steam_rental, оттуда придёт нормальная ошибка.
                pass
            elif not requested and len(buyer_active) == 1:
                asgn = buyer_active[0]
            elif not requested and len(buyer_active) > 1:
                # Не можем определить однозначно — просим уточнить логин.
                cardinal.send_message(
                    chat_id,
                    "У вас несколько выданных аккаунтов. Уточните логин: "
                    "`!код логин`",
                    chat_name=chat_name,
                    interlocutor_id=author_id, watermark=False)
                return True

        if asgn is None:
            return False  # пусть отрабатывает steam_rental (вдруг это аренда)

        # Лимит исчерпан?
        used = int(asgn.get("codes_used", 0))
        limit = int(asgn.get("codes_limit", 0))
        if limit > 0 and used >= limit:
            text_resp = _render_template(
                "guard_limit_reached",
                buyer_id=author_id,
                login=asgn.get("account_name", asgn.get("alias", "")),
                codes_used=str(used),
                codes_limit=str(limit),
            )
            cardinal.send_message(
                chat_id, text_resp,
                chat_name=chat_name,
                interlocutor_id=author_id, watermark=False)
            _log_event("guard_limit_reached",
                       assignment_id=asgn["id"], alias=asgn.get("alias"),
                       buyer_username=asgn.get("buyer_username"),
                       buyer_id=int(author_id),
                       codes_used=used, codes_limit=limit)
            _metric_inc("guard_limit_reached_total")
            _bump_acc_stat(asgn.get("alias", ""),
                           inc_guard_limit_reached=1)
            return True

        # Получаем аккаунт.
        acc = find_account(asgn["alias"])
        if not acc:
            text_resp = _render_template(
                "guard_error", buyer_id=author_id)
            cardinal.send_message(
                chat_id, text_resp,
                chat_name=chat_name,
                interlocutor_id=author_id, watermark=False)
            return True

        if not acc.get("shared_secret"):
            text_resp = _render_template(
                "guard_error_no_secret", buyer_id=author_id)
            cardinal.send_message(
                chat_id, text_resp,
                chat_name=chat_name,
                interlocutor_id=author_id, watermark=False)
            return True

        # Генерация кода.
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
            LOGGER.error("steam_offline: 2FA gen failed for %s",
                         asgn.get("alias"), exc_info=True)
            cardinal.send_message(
                chat_id, f"Ошибка генерации кода: {exc}",
                chat_name=chat_name,
                interlocutor_id=author_id, watermark=False)
            return True

        # Инкрементируем счётчик.
        used += 1
        with _lock:
            asgns = list_assignments()
            cur = asgns.get(asgn["id"])
            if cur is not None:
                cur["codes_used"] = used
                cur["last_code_at"] = _now()
                save_assignments(asgns)

        left = max(0, limit - used) if limit > 0 else 0
        if limit > 0 and used >= limit:
            tpl_name = "guard_last_code"
        else:
            tpl_name = "guard_code"

        text_resp = _render_template(
            tpl_name,
            buyer_id=author_id,
            login=acc["account_name"],
            code=code,
            codes_used=str(used),
            codes_limit=str(limit),
            codes_left=str(left),
        )
        cardinal.send_message(
            chat_id, text_resp,
            chat_name=chat_name,
            interlocutor_id=author_id, watermark=False)
        _log_event("guard_code", assignment_id=asgn["id"],
                   alias=asgn.get("alias"),
                   buyer_username=asgn.get("buyer_username"),
                   buyer_id=int(author_id),
                   codes_used=used, codes_limit=limit)
        # v5: stats + Prometheus
        _bump_acc_stat(asgn.get("alias", ""),
                       inc_guard_sent_count=1,
                       set_last_guard_at=_now())
        _metric_inc("guard_sent_total")
        return True

    except Exception:
        LOGGER.error("steam_offline: _try_offline_guard_code crashed",
                     exc_info=True)
        return False


def _patched_cmd_guard_code(cardinal: "Cardinal", msg: Any, text: str) -> None:
    """Совместимости-фоллбэк: не делает ничего (мы используем свой handler)."""
    # Этот метод оставлен на случай, если другой плагин дернет его явно.
    return


# ── Доп. перехваты для команд !статус и !помощь (для офлайн-выдач) ──────────
def _try_offline_status(cardinal: "Cardinal", msg: Any) -> bool:
    try:
        author_id = msg.author_id
        chat_id = msg.chat_id
        chat_name = getattr(msg, "chat_name", None)
        asgns = find_active_assignments_by_buyer(int(author_id))
        if not asgns:
            return False
        chunks: list[str] = []
        for a in asgns:
            game = _get_game_for_alias(a.get("alias", ""))
            used = int(a.get("codes_used", 0))
            limit = int(a.get("codes_limit", 0))
            left = max(0, limit - used) if limit > 0 else 0
            chunks.append(_render_template(
                "status",
                buyer_id=author_id,
                login=a.get("account_name", a.get("alias", "")),
                game=game or "—",
                codes_used=str(used),
                codes_limit=str(limit),
                codes_left=str(left),
            ))
        cardinal.send_message(
            chat_id, "\n\n──────\n\n".join(chunks),
            chat_name=chat_name,
            interlocutor_id=author_id, watermark=False)
        return True
    except Exception:
        LOGGER.error("steam_offline: _try_offline_status crashed", exc_info=True)
        return False


# ── v5: Prometheus + Daily summary + SQLite ─────────────────────────────────
_stop_event = threading.Event()
_metrics_http_server: Any = None
_metrics_http_thread: threading.Thread | None = None
_daily_summary_thread: threading.Thread | None = None
_sqlite_thread: threading.Thread | None = None
_lot_activation_thread: threading.Thread | None = None


_SQLITE_DUMP_DISABLED_SO = False


def _sqlite_dump_now() -> bool:
    """Если модуля steam_sqlite нет рядом с плагином — кэшируем флаг и
    больше не пытаемся импортировать. Иначе каждые 30с в лог сыпятся
    идентичные ModuleNotFoundError-трейсбеки."""
    global _SQLITE_DUMP_DISABLED_SO
    if _SQLITE_DUMP_DISABLED_SO:
        return False
    try:
        from steam_sqlite import dump_offline  # type: ignore
        return dump_offline(
            list_accounts(), list_assignments(), list_history())
    except ModuleNotFoundError:
        _SQLITE_DUMP_DISABLED_SO = True
        LOGGER.info(
            "steam_offline: steam_sqlite.py не найден рядом с плагином — "
            "sqlite-дамп отключён (это не влияет на работу плагина).")
        return False
    except Exception:
        LOGGER.debug("steam_offline: sqlite dump failed", exc_info=True)
        return False


def _sqlite_loop(cardinal: "Cardinal") -> None:
    while not _stop_event.is_set():
        try:
            _sqlite_dump_now()
        except Exception:
            LOGGER.debug("steam_offline: sqlite tick failed", exc_info=True)
        _stop_event.wait(60)


def _metrics_render() -> str:
    snap = _metric_snapshot()
    lines: list[str] = []
    for key, val in sorted(snap.items()):
        is_counter = key.endswith("_total")
        mtype = "counter" if is_counter else "gauge"
        lines.append(f"# HELP steam_offline_{key} {key}")
        lines.append(f"# TYPE steam_offline_{key} {mtype}")
        lines.append(f"steam_offline_{key} {float(val)}")
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
        port = int(cfg.get("metrics_port", 9102))
        srv = ThreadingHTTPServer((bind, port), _Handler)
        _metrics_http_server = srv

        def _run() -> None:
            try:
                srv.serve_forever()
            except Exception:
                LOGGER.error("steam_offline: metrics server crashed",
                              exc_info=True)

        t = threading.Thread(target=_run, daemon=True,
                              name="steam_offline-metrics")
        t.start()
        _metrics_http_thread = t
        LOGGER.info("steam_offline: Prometheus metrics on %s:%d/metrics",
                    bind, port)
    except Exception:
        LOGGER.error("steam_offline: failed to start metrics server",
                      exc_info=True)


def _daily_summary_text() -> str:
    history = list_history()
    accs = list_accounts()
    asgns = list_assignments()
    now = _now()
    day_ago = now - 86400
    issues = [h for h in history
              if h.get("event") == "issue" and h.get("ts", 0) >= day_ago]
    guards = [h for h in history
              if h.get("event") == "guard_code" and h.get("ts", 0) >= day_ago]
    limits = [h for h in history
              if h.get("event") == "guard_limit_reached"
              and h.get("ts", 0) >= day_ago]
    # v1.9.0: refund-учёт в дневной сводке. Выручка = sum(issue.amount) +
    # sum(refund.amount) — refund.amount уже отрицательный.
    refunds = [h for h in history
               if h.get("event") == "refund" and h.get("ts", 0) >= day_ago]
    revenue = sum(float(h.get("amount", 0) or 0)
                  for h in (issues + refunds))
    refund_total = -sum(float(h.get("amount", 0) or 0) for h in refunds)
    active = [x for x in asgns.values()
              if (x.get("status") or "active") == "active"]
    frozen = [a for a in accs if a.get("frozen")]
    refund_line = ""
    if refunds:
        refund_line = (
            f"💸 Возвратов: <b>{len(refunds)}</b> "
            f"(−{refund_total:.0f}₽)\n"
        )
    return (
        "📊 <b>Steam Offline — сводка за сутки</b>\n\n"
        f"📦 Новых выдач: <b>{len(issues)}</b>\n"
        f"🔑 Guard-кодов: <b>{len(guards)}</b>\n"
        f"⛔ Лимит Guard достигнут: <b>{len(limits)}</b>\n"
        f"{refund_line}"
        f"💰 Выручка: <b>{revenue:.0f}₽</b>\n\n"
        f"📦 Аккаунтов всего: <b>{len(accs)}</b>, "
        f"в выдаче: <b>{len(active)}</b>, "
        f"заморожено: <b>{len(frozen)}</b>"
    )


def _daily_summary_loop(cardinal: "Cardinal") -> None:
    last_sent_day = -1
    last_denuvo_day = ""
    while not _stop_event.is_set():
        try:
            cfg = get_config()
            # ── Daily summary в указанный час UTC ──
            if cfg.get("daily_summary_enabled", True):
                target_hour = int(cfg.get("daily_summary_hour_utc", 21)) % 24
                now_utc = datetime.datetime.utcnow()
                if (now_utc.hour == target_hour
                        and now_utc.toordinal() != last_sent_day):
                    try:
                        _notify_tg(cardinal, _daily_summary_text())
                        last_sent_day = now_utc.toordinal()
                    except Exception:
                        LOGGER.error("steam_offline: daily summary failed",
                                      exc_info=True)
            # ── Denuvo: уведомление об освобождении слотов в 00:00 UTC ──
            today = _denuvo_today_utc()
            if last_denuvo_day != today:
                # Первый тик после смены UTC-даты — ищем аккаунты, у которых
                # вчера был исчерпан лимит, и шлём «слоты снова доступны».
                try:
                    lots = list_lots()
                    denuvo_aliases: dict[str, int] = {}
                    for v in lots.values():
                        if not v.get("denuvo"):
                            continue
                        d_lim = _denuvo_lot_limit(v)
                        for a in v.get("aliases") or []:
                            cur = denuvo_aliases.get(a.lower(), 0)
                            denuvo_aliases[a.lower()] = max(cur, d_lim)
                    freed: list[str] = []
                    for acc in list_accounts():
                        al = (acc.get("alias") or "").lower()
                        if al not in denuvo_aliases:
                            continue
                        prev_day = str(acc.get("denuvo_day") or "")
                        prev_cnt = int(acc.get("denuvo_count") or 0)
                        lim = denuvo_aliases[al]
                        if prev_day and prev_day != today and prev_cnt >= lim:
                            freed.append(acc["alias"])
                    if freed and last_denuvo_day:
                        # Не шлём при самом первом запуске бота, только когда
                        # реально перевалили через полночь.
                        _notify_tg(
                            cardinal,
                            f"🛡 <b>Denuvo</b>: новый UTC-день, слоты "
                            f"освободились на "
                            f"<b>{len(freed)}</b> акк(ах): "
                            f"<code>{_esc(', '.join(freed[:15]))}</code>"
                            + (f" (+{len(freed) - 15})"
                               if len(freed) > 15 else ""))
                except Exception:
                    LOGGER.debug(
                        "steam_offline: denuvo midnight notify failed",
                        exc_info=True)
                last_denuvo_day = today
        except Exception:
            LOGGER.debug("steam_offline: daily summary tick failed",
                          exc_info=True)
        _stop_event.wait(60)


def _lot_activation_loop(cardinal: "Cardinal") -> None:
    """Периодически перепроверяет авто-(де)активацию лотов."""
    while not _stop_event.is_set():
        try:
            _update_lot_activation_so(cardinal)
        except Exception:
            LOGGER.debug("steam_offline: lot activation tick failed",
                         exc_info=True)
        _stop_event.wait(60)


# ── Хэндлеры FPC ─────────────────────────────────────────────────────────────
def _handler_pre_init(cardinal: "Cardinal") -> None:
    global _daily_summary_thread, _sqlite_thread, _lot_activation_thread, _CARDINAL_REF_SO
    _CARDINAL_REF_SO = cardinal

    # 💛 Донат-баннер (защита реквизитов автора)
    global _donation_cardinal
    _donation_cardinal = cardinal
    try:
        tg = getattr(cardinal, "telegram", None)
        if tg:
            tg.cbq_handler(
                _donation_on_cb,
                lambda c: (c.data or "").startswith("sof_dn:"))
            _start_donation_reminder(cardinal)
    except Exception:
        pass

    _ensure_storage()
    get_config()
    list_accounts()
    list_assignments()
    list_lots()
    list_games()
    # ── v6: миграция → games.json (фоном) ──
    try:
        threading.Thread(
            target=lambda c=cardinal: _migrate_lots_to_games_so(c),
            daemon=True, name="steam_offline-migrate-games-v6").start()
    except Exception:
        LOGGER.debug("steam_offline: games migration thread failed to start",
                     exc_info=True)
    # ── Авто-(де)активация лотов + raise-skip ──
    try:
        _load_raise_skip_so()
        _install_raise_skip_so(cardinal)
        # Обновление кэша категорий и одна синхронизация активаций — в
        # отдельном потоке: get_lot_fields/save_lot — HTTP-запросы, не
        # блокируем pre_init.
        def _bootstrap_offline():
            try:
                _refresh_raise_skip_so(cardinal)
            except Exception:
                LOGGER.debug("steam_offline: refresh raise-skip failed",
                             exc_info=True)
            try:
                _update_lot_activation_so(cardinal)
            except Exception:
                LOGGER.debug("steam_offline: initial lot activation failed",
                             exc_info=True)
        threading.Thread(
            target=_bootstrap_offline, daemon=True,
            name="steam_offline-lotact-bootstrap").start()
    except Exception:
        LOGGER.error("steam_offline: lot-activation setup failed",
                      exc_info=True)
    LOGGER.info("steam_offline: storage initialised at %s", STORAGE_DIR)
    # license check removed
    _register_tg_commands(cardinal)
    # v5: Prometheus + daily summary
    _stop_event.clear()
    try:
        _start_metrics_server(cardinal)
    except Exception:
        LOGGER.error("steam_offline: metrics server boot crash",
                      exc_info=True)
    if not (_daily_summary_thread and _daily_summary_thread.is_alive()):
        _daily_summary_thread = threading.Thread(
            target=_daily_summary_loop, args=(cardinal,), daemon=True,
            name="steam_offline-daily-summary")
        _daily_summary_thread.start()
    # v5: SQLite sidecar + periodic dump
    try:
        from steam_sqlite import autotune  # type: ignore
        autotune(STORAGE_DIR, "steam_offline")
    except Exception:
        LOGGER.debug("steam_offline: sqlite sidecar disabled",
                      exc_info=True)
    if not (_sqlite_thread and _sqlite_thread.is_alive()):
        _sqlite_thread = threading.Thread(
            target=_sqlite_loop, args=(cardinal,), daemon=True,
            name="steam_offline-sqlite")
        _sqlite_thread.start()
    # Периодическая перепроверка авто-(де)активации лотов
    if not (_lot_activation_thread and _lot_activation_thread.is_alive()):
        _lot_activation_thread = threading.Thread(
            target=_lot_activation_loop, args=(cardinal,), daemon=True,
            name="steam_offline-lot-activation")
        _lot_activation_thread.start()


def _handler_new_order(cardinal: "Cardinal", event: "NewOrderEvent") -> None:
    """Если оплачен офлайн-лот — выдаём аккаунт навсегда."""
    try:
        try:
            from FunPayAPI.common.enums import OrderStatuses
        except Exception:
            OrderStatuses = None  # type: ignore[assignment]

        order = event.order
        if OrderStatuses is not None and getattr(order, "status", None) \
                not in (OrderStatuses.PAID,):
            return

        full_order = None
        try:
            full_order = cardinal.get_order_from_object(order)
        except Exception:
            pass

        # v1.9.0: цену заказа достаём из full_order.sum.value (или fallback
        # на price). Сохраняем на cardinal — deliver_account_offline её
        # читает, чтобы записать в 'issue'-event и acc.stats.total_revenue.
        # Без этого refund→profit не сможет найти оригинальную сумму.
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
            cardinal._so_last_price = (
                float(_price_v) if _price_v is not None else None)
        except Exception:
            cardinal._so_last_price = None

        lot_id = None
        for attr in ("subcategory_id", "lot_id"):
            src = full_order or order
            lot_id = getattr(src, attr, None) or lot_id
        desc = getattr(order, "description", "") or ""

        if lot_id is None:
            import re
            for src in (full_order, order):
                if src is None:
                    continue
                for attr in ("html", "raw_html"):
                    html = getattr(src, attr, "") or ""
                    m = re.search(r"offers/(\d+)", html)
                    if m:
                        lot_id = m.group(1)
                        break
                if lot_id is not None:
                    break

        # v6: матчинг в 2 уровня — сначала по игре (Order.title),
        # потом legacy фоллбэк (lot_id / keyword)
        order_title = None
        if full_order is not None:
            order_title = getattr(full_order, "title", None) or None
        if not order_title:
            order_title = desc
        lot = _match_lot_by_game(order_title) or _match_lot(desc, lot_id)
        if not lot:
            # Не наш лот — это НОРМАЛЬНО, его обработает другой плагин
            # (например steam_rental или ns_gifts). В actions.log не пишем
            # каждый чужой заказ, иначе будет спам. Только DEBUG.
            LOGGER.debug(
                "steam_offline: заказ #%s lot_id=%s НЕ наш — пропуск "
                "(обработает другой плагин если он его настроил)",
                getattr(order, "id", "?"), lot_id)
            return

        # Дошли сюда — лот наш. Теперь любой выход = инцидент, логируем.
        _log_action_so("delivery",
                       f"Получен заказ #{getattr(order, 'id', '?')} "
                       f"для лота {lot.get('key')}",
                       order_id=getattr(order, "id", None),
                       lot_id=lot.get("key"),
                       buyer=getattr(order, "buyer_username", None),
                       buyer_id=getattr(order, "buyer_id", None))

        cfg = get_config()

        # ── v1.9.0: blacklist-чек ────────────────────────────────────
        # Срабатывает ПОСЛЕ матчинга лота (чужие заказы нас не касаются),
        # но ДО auto_deliver/picker — чтобы заблокированный покупатель
        # ничего не получил.
        # Урок rental v2.22.2: buyer_id/username берём с fallback
        # event.order → full_order. FunPay/FPC иногда отдаёт buyer_id=None
        # в event.order, и тогда is_blacklisted("","") тихо возвращает
        # False — покупатель из ЧС спокойно покупал ещё раз.
        buyer_id_v = (getattr(order, "buyer_id", None)
                      or getattr(full_order, "buyer_id", None))
        buyer_un_v = (getattr(order, "buyer_username", None)
                      or getattr(full_order, "buyer_username", None))
        bl_hit = (is_blacklisted(buyer_id_v, buyer_un_v)
                  if (buyer_id_v or buyer_un_v) else False)
        bl_enabled = bool(cfg.get("blacklist_enabled", True))
        LOGGER.info(
            "steam_offline: blacklist-check order=%s buyer=%s id=%s "
            "blacklist_enabled=%s is_blacklisted=%s bl_size=%d",
            getattr(order, "id", "?"), buyer_un_v, buyer_id_v,
            bl_enabled, bl_hit, len(list_blacklist()))
        if bl_hit and bl_enabled:
            _metric_inc("blocked_blacklist_total")
            LOGGER.info(
                "steam_offline: blacklist hit — заказ %s от %s (id=%s) "
                "проигнорирован", order.id, buyer_un_v, buyer_id_v)
            _log_action_so("lot_save_failed",
                           f"Заказ #{order.id} от {buyer_un_v} — "
                           f"покупатель в blacklist",
                           order_id=order.id, lot_id=lot.get("key"),
                           buyer=buyer_un_v, buyer_id=buyer_id_v)
            _notify_tg(cardinal,
                       f"🚫 <b>Steam Offline</b>: заказ #{order.id} "
                       f"от <b>{buyer_un_v}</b> "
                       f"(id <code>{buyer_id_v}</code>) "
                       f"проигнорирован — покупатель в blacklist.")
            return
        if bl_hit and not bl_enabled:
            # Покупатель в ЧС, но опция «Блокировка на NEW_ORDER» выключена.
            # Не блокируем (поведение по настройке), но громко уведомляем
            # оператора, чтобы баг «не работает blacklist» был очевиден.
            _notify_tg(cardinal,
                       f"⚠️ <b>Steam Offline</b>: заказ #{order.id} от "
                       f"<b>{buyer_un_v}</b> "
                       f"(id <code>{buyer_id_v}</code>) — "
                       f"покупатель в blacklist, НО опция "
                       f"«Блокировка на NEW_ORDER» выключена в настройках, "
                       f"поэтому заказ выдаётся. Включи в "
                       f"<code>/soffline → ⚙ Настройки → 🚫 Blacklist</code>.")
            LOGGER.warning(
                "steam_offline: blacklist hit для %s, но blacklist_enabled=False"
                " — заказ %s НЕ блокируется", buyer_un_v, order.id)

        if not cfg.get("auto_deliver", True):
            LOGGER.info("steam_offline: auto_deliver выключен, заказ %s "
                        "оставлен на ручную выдачу", order.id)
            _log_action_so("lot_save_failed",
                            f"Заказ #{order.id} — auto_deliver выключен",
                            order_id=order.id,
                            lot_id=lot.get("key"),
                            buyer=order.buyer_username)
            _notify_tg(cardinal,
                       f"📦 <b>Steam Offline</b>: оплачен лот "
                       f"<code>{lot.get('key')}</code> "
                       f"(заказ #{order.id}, "
                       f"покупатель {order.buyer_username}). "
                       f"Автовыдача выключена — выдай вручную.")
            return

        guard_limit = lot.get("guard_limit")
        if guard_limit is None or int(guard_limit) <= 0:
            guard_limit = int(cfg.get("default_guard_limit", 10))

        is_denuvo = _lot_is_denuvo(lot)
        denuvo_limit = _denuvo_lot_limit(lot) if is_denuvo else None

        amount = int(getattr(order, "amount", 1) or 1)
        # Идемпотентность: если по этому заказу уже есть активные выдачи
        # (повторный/дублирующий NEW_ORDER), выдаём только недостающее.
        already = count_active_assignments_by_order(getattr(order, "id", ""))
        if already >= amount:
            LOGGER.info(
                "steam_offline: заказ %s уже обработан (%d/%d выдач) — "
                "пропуск дубля NEW_ORDER", order.id, already, amount)
            return
        remaining = amount - already
        delivered = 0
        # Защита от выдачи одного и того же аккаунта дважды в рамках одного
        # NEW_ORDER с amount > 1 (актуально в multi-issue режиме, где picker
        # больше не помечает alias как «занятый» после первой выдачи).
        issued_aliases: set[str] = set()
        for _ in range(remaining):
            alias = _pick_free_alias_for_lot(
                lot, exclude_aliases=issued_aliases)
            if not alias:
                # Для Denuvo нет смысла слать "no_accounts" — там слоты
                # сбрасываются в 00:00 UTC. Но шаблон тот же.
                LOGGER.warning(
                    "steam_offline: нет свободных аккаунтов для лота %s "
                    "(пул=%s, denuvo=%s)",
                    lot.get("key"), lot.get("aliases"), is_denuvo)
                _log_action_so("lot_save_failed",
                                f"Нет свободных аккаунтов для лота "
                                f"{lot.get('key')} (заказ #{order.id})",
                                order_id=order.id,
                                lot_id=lot.get("key"),
                                buyer=order.buyer_username,
                                denuvo=is_denuvo,
                                aliases=",".join(
                                    lot.get("aliases", [])[:5]))
                text = _render_template("no_accounts",
                                         buyer_id=order.buyer_id,
                                         game=lot.get("game") or "—")
                try:
                    cardinal.send_message(
                        order.chat_id, text,
                        chat_name=order.buyer_username,
                        interlocutor_id=order.buyer_id, watermark=False)
                except Exception:
                    LOGGER.debug("steam_offline: no_account msg failed",
                                 exc_info=True)
                denuvo_note = ""
                if is_denuvo:
                    denuvo_note = (" — все Denuvo-слоты исчерпаны до "
                                   "00:00 UTC")
                _notify_tg(cardinal,
                           f"⚠️ <b>Steam Offline</b>: нет свободных аккаунтов "
                           f"для лота <code>{lot.get('key')}</code> "
                           f"(заказ #{order.id}, "
                           f"покупатель {order.buyer_username})"
                           f"{denuvo_note}.")
                break

            asgn = deliver_account_offline(
                cardinal, alias=alias,
                buyer_id=order.buyer_id,
                buyer_username=order.buyer_username,
                chat_id=order.chat_id,
                order_id=order.id,
                guard_limit=int(guard_limit),
                denuvo=is_denuvo,
                denuvo_lot_limit=denuvo_limit,
            )
            if asgn is not None:
                delivered += 1
                issued_aliases.add(str(alias).lower())
            else:
                break

        LOGGER.info("steam_offline: order %s — выдано %d/%d аккаунтов",
                    order.id, delivered, amount)
    except Exception:
        LOGGER.error("steam_offline: handler_new_order crashed", exc_info=True)


def _try_offline_accounts_list(cardinal: "Cardinal", msg: Any) -> bool:
    """Показывает покупателю список лотов со свободными аккаунтами.

    v1.11.0:
      * Дедупликация лотов по `game_key` (или по `game.lower()` для
        legacy-лотов без game_key). Если у одной игры несколько лотов
        с общим пулом — выводим её одной строкой, а не N одинаковых.
      * В строке игры показываем реальные Steam-логины свободных
        аккаунтов (`acc.account_name`), а не только число. Лимит — 10
        логинов; если больше, в конце добавляем «… ещё N» (на языке
        покупателя).
    """
    try:
        chat_id = msg.chat_id
        chat_name = getattr(msg, "chat_name", None)
        author_id = getattr(msg, "author_id", None)
        lang = _get_buyer_lang(author_id)

        lots = list_lots()
        # ── Группируем лоты по игре ───────────────────────────────────
        # Ключ группы — game_key.lower() если он есть; иначе
        # game.lower(); иначе lot_key.lower() (для совсем legacy-лотов
        # без game_key и без game-имени).
        groups: dict[str, dict[str, Any]] = {}
        for key, val in lots.items():
            gkey = (val.get("game_key") or "").strip().lower()
            game_name = (val.get("game") or "").strip()
            group_key = gkey or game_name.lower() or str(key).lower()
            grp = groups.setdefault(group_key, {
                "game_name": game_name or game_name or str(key),
                "lots": [],
                "is_denuvo": False,
            })
            # Имя игры берём первое непустое в рамках группы.
            if not grp["game_name"] and game_name:
                grp["game_name"] = game_name
            grp["lots"].append(val)
            if _lot_is_denuvo(val):
                grp["is_denuvo"] = True

        # ── Для каждой группы строим пул и считаем свободные ─────────
        # Пул объединяем по всем лотам группы (de-dup по lower-case
        # alias). Counter «свободных» считаем по объединённому пулу,
        # чтобы один и тот же аккаунт не считался несколько раз.
        lines: list[str] = []
        for group_key, grp in groups.items():
            seen_low: set[str] = set()
            combined: list[str] = []
            for lot in grp["lots"]:
                for a in _combined_lot_pool_offline(lot):
                    al = str(a).lower()
                    if al not in seen_low:
                        seen_low.add(al)
                        combined.append(str(a))
            if not combined:
                continue
            # Determine free aliases. For Denuvo — учитываем daily-слоты
            # (если хоть один лот группы Denuvo — применяем его limit).
            if grp["is_denuvo"]:
                # Берём максимальный лимит по лотам группы (на практике
                # обычно одинаковый).
                d_limit = max(
                    (_denuvo_lot_limit(lot) for lot in grp["lots"]),
                    default=0)
                free_aliases: list[str] = []
                for a in combined:
                    acc = find_account(a)
                    if not acc or acc.get("frozen"):
                        continue
                    if d_limit > 0 and _denuvo_slots_left(acc, d_limit) > 0:
                        free_aliases.append(a)
            else:
                multi = bool(get_config().get("allow_multi_issue", True))
                free_aliases = []
                for a in combined:
                    acc = find_account(a)
                    if not acc:
                        continue
                    if acc.get("frozen") or acc.get("status") == "frozen":
                        continue
                    if not multi and find_active_assignment_by_alias(a):
                        continue
                    free_aliases.append(a)

            if not free_aliases:
                continue

            # ── Рендерим список логинов с лимитом ──────────────────
            max_logins = 10
            shown = free_aliases[:max_logins]
            login_strs: list[str] = []
            for a in shown:
                acc = find_account(a)
                login = ""
                if acc:
                    login = (acc.get("account_name") or "").strip()
                # Fallback на alias, если по какой-то причине нет
                # account_name (напр. ещё не дозаполнен).
                login_strs.append(login or a)
            extra = len(free_aliases) - len(shown)
            logins_text = ", ".join(login_strs)
            if extra > 0:
                more = (f" … и ещё {extra}" if lang == "ru"
                        else f" … and {extra} more")
                logins_text += more

            line = _render_template(
                "accounts_list_lot_line",
                buyer_id=author_id,
                game=grp["game_name"] or "—",
                free=str(len(free_aliases)),
                logins=logins_text,
            )
            lines.append(line)

        if not lines:
            text = _render_template("accounts_list_empty",
                                    buyer_id=author_id)
        else:
            text = _render_template("accounts_list",
                                    buyer_id=author_id,
                                    lots="\n\n".join(lines))

        cardinal.send_message(
            chat_id, text,
            chat_name=chat_name,
            interlocutor_id=author_id, watermark=False)
        return True
    except Exception:
        LOGGER.error("steam_offline: _try_offline_accounts_list crashed", exc_info=True)
        return False


def _try_offline_help(cardinal: "Cardinal", msg: Any) -> bool:
    """Показывает краткую справку по командам офлайн-выдачи.

    v1.10.0: текст выбирается по языку покупателя
    (`buyer_lang.json` → `cfg.default_language`). Хардкод вместо
    шаблона — он формируется из cfg.guardik_command (которая может быть
    переопределена админом) и потому в шаблоне его удобно держать
    нельзя.
    """
    try:
        cfg = get_config()
        guardik = cfg.get("guardik_command") or "!код"
        lang = _get_buyer_lang(getattr(msg, "author_id", None))
        if lang == "en":
            help_text = (
                "🟥 <b>OFFLINE DELIVERY — HELP</b> 🟥\n\n"
                f"🔐 <b>{guardik}</b> [login] — get Steam Guard code\n"
                "   (without login — auto-detect your delivery)\n\n"
                "📊 <b>!status</b> — remaining code count\n\n"
                "🎮 <b>!accounts</b> — list of lots with free accounts\n\n"
                "🌐 <b>!rusrent</b> — switch chat to Russian\n\n"
                "💡 Commands work only in chat with the seller."
            )
        else:
            help_text = (
                "🟥 <b>ОФЛАЙН-ВЫДАЧА — ПОМОЩЬ</b> 🟥\n\n"
                f"🔐 <b>{guardik}</b> [логин] — получить Steam Guard код\n"
                "   (без логина — автоопределение вашей выдачи)\n\n"
                "📊 <b>!статус</b> — остаток выдач кода\n\n"
                "🎮 <b>!аккаунты</b> — список лотов со свободными аккаунтами\n\n"
                "🌐 <b>!engrent</b> — switch chat to English\n\n"
                "💡 Команды работают только в чате с продавцом."
            )
        cardinal.send_message(
            msg.chat_id, help_text, chat_name=getattr(msg, "chat_name", None),
            interlocutor_id=msg.author_id, watermark=False)
        return True
    except Exception:
        LOGGER.error("steam_offline: _try_offline_help crashed", exc_info=True)
        return False


def _handler_order_status_changed(cardinal: "Cardinal",
                                   event: "Any") -> None:
    """v1.9.0: обработка изменения статуса заказа.

    Ловит REFUND/CANCELED:
      * списывает выручку с per-account stats и пишет refund-event
        в history (см. `_apply_refund_to_stats`);
      * авто-добавляет покупателя в blacklist
        (если включена `auto_blacklist_on_refund`).

    Эти ветки независимы — refund-stats работает всегда, blacklist-add
    — по флагу. Идемпотентно: повторный refund по тому же order_id
    не дублирует ни списание (см. `_apply_refund_to_stats`), ни
    запись в blacklist (см. `add_to_blacklist`).
    """
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

        if not is_refund:
            return

        # ── списываем выручку с прибыли (всегда) ──────────────────────
        try:
            refund_alias, refund_amount = _apply_refund_to_stats(
                order_id_v, buyer_un_v, buyer_id_v)
        except Exception:
            LOGGER.error(
                "steam_offline: _apply_refund_to_stats(%s) crash",
                order_id_v, exc_info=True)
            refund_alias, refund_amount = None, 0.0
        if refund_alias and refund_amount > 0:
            _notify_tg(cardinal,
                       f"💸 <b>Steam Offline</b>: возврат по заказу "
                       f"#{order_id_v} — выручка "
                       f"<b>−{refund_amount:.2f}</b>₽ списана с "
                       f"аккаунта <code>{refund_alias}</code>.")

        # ── auto-blacklist (по флагу) ────────────────────────────────
        # v1.11.1: добавляем в blacklist ТОЛЬКО если это НАШ заказ (т.е.
        # _apply_refund_to_stats нашёл issue-event в нашем history).
        # Раньше blacklist-add срабатывал для ВСЕХ refund-ов, включая
        # заказы других плагинов (ns_gifts, minecraft_donate и т.д.) —
        # покупатель попадал в ЧС даже если аккаунт ему не выдавался.
        if cfg.get("auto_blacklist_on_refund", True) and refund_alias:
            if buyer_id_v or buyer_un_v:
                if add_to_blacklist(
                        buyer_id_v, buyer_un_v,
                        reason=f"refund order={order_id_v}"):
                    _metric_inc("blacklist_auto_refund_total")
                    _notify_tg(cardinal,
                               f"🚫 <b>Steam Offline</b>: покупатель "
                               f"<b>{buyer_un_v}</b> "
                               f"(id <code>{buyer_id_v}</code>) "
                               f"добавлен в blacklist после refund/cancel "
                               f"заказа #{order_id_v}.")
    except Exception:
        LOGGER.error(
            "steam_offline: _handler_order_status_changed crashed",
            exc_info=True)


def _handler_new_message(cardinal: "Cardinal", event: "NewMessageEvent") -> None:
    """Обрабатывает команды покупателя:
      * !код (или другая настроенная `guardik_command`) — выдаёт Steam Guard
        если у автора есть активная офлайн-выдача.
      * !статус / !status — статус выдачи.
      * !аккаунты / !accounts — список свободных аккаунтов.
      * !помощь / !help — краткая справка.
    Плагин НЕ зависит от steam_rental.
    """
    try:
        msg = event.message
        text = (getattr(msg, "text", "") or "").strip()
        if not text:
            return
        if getattr(msg, "author_id", None) == cardinal.account.id:
            return

        cfg = get_config()
        # Настраиваемая команда для Steam Guard (по умолчанию "!код").
        # Поддерживаем варианты: "!код", "!код alias", "!арбуз", "!арбуз alias"
        guardik_cmd = (cfg.get("guardik_command") or "!код").strip().lower()

        text_lower = text.lower()
        # Команда Steam Guard: начинается с настроенной команды.
        if (text_lower == guardik_cmd
                or text_lower.startswith(guardik_cmd + " ")
                or text_lower.startswith(guardik_cmd + "\n")
                or text_lower.startswith(guardik_cmd + "\t")):
            _try_offline_guard_code(cardinal, msg, text)
            return
        # Алиасы по умолчанию — !код и !guardik на случай если меняли
        # конфиг и старые команды ещё работают.
        for legacy in ("!код", "!guardik", "!guard"):
            if legacy == guardik_cmd:
                continue
            if (text_lower == legacy
                    or text_lower.startswith(legacy + " ")):
                _try_offline_guard_code(cardinal, msg, text)
                return
        # Остальные команды.
        if text_lower.startswith("!статус") or text_lower.startswith("!status"):
            _try_offline_status(cardinal, msg)
        elif (text_lower.startswith("!аккаунты")
                or text_lower.startswith("!accounts")):
            _try_offline_accounts_list(cardinal, msg)
        elif (text_lower.startswith("!помощь")
                or text_lower.startswith("!help")
                or text_lower.startswith("!help_offline")):
            _try_offline_help(cardinal, msg)
        # v1.10.0: переключение языка чата покупателя
        elif (text_lower.startswith("!engrent")
                or text_lower.startswith("!english")):
            _cmd_set_lang(cardinal, msg, "en")
        elif (text_lower.startswith("!rusrent")
                or text_lower.startswith("!russian")):
            _cmd_set_lang(cardinal, msg, "ru")
    except Exception:
        LOGGER.error("steam_offline: handler_new_message crashed",
                     exc_info=True)


def _cmd_set_lang(cardinal: "Cardinal", msg: Any, lang: str) -> None:
    """v1.10.0: !engrent / !rusrent — переключение языка диалога с
    ботом для конкретного покупателя. Меняем сразу и шлём подтверждение
    на выбранном языке (захардкожено, не через _render_template).
    """
    if lang not in ("ru", "en"):
        return
    try:
        _set_buyer_lang(getattr(msg, "author_id", None), lang)
    except Exception:
        LOGGER.warning(
            "steam_offline: set_buyer_lang failed", exc_info=True)
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
            chat_name=getattr(msg, "chat_name", None),
            interlocutor_id=msg.author_id, watermark=False)
    except Exception:
        LOGGER.warning(
            "steam_offline: _cmd_set_lang send_message failed",
            exc_info=True)


# ── Telegram UI (inline-меню) ───────────────────────────────────────────────
_pending_state: dict[int, dict[str, Any]] = {}
# v1.10.0: текущий язык в меню «📝 Шаблоны» для каждого админа.
# Меняется кнопкой 🇷🇺 RU / 🇬🇧 EN. По умолчанию RU.
_template_admin_lang: dict[int, str] = {}


def _get_admin_lang(uid: int) -> str:
    return _template_admin_lang.get(uid, "ru")


def _set_admin_lang(uid: int, lang: str) -> None:
    if lang in ("ru", "en"):
        _template_admin_lang[uid] = lang


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


def _resolve_assignment(sid: str) -> str | None:
    for aid in list_assignments().keys():
        if _sid(aid) == sid:
            return aid
    return None


def _resolve_game(sid: str) -> str | None:
    for gkey in list_games().keys():
        if _sid(gkey) == sid:
            return gkey
    return None


def _esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _register_tg_commands(cardinal: "Cardinal") -> None:
    tg = getattr(cardinal, "telegram", None)
    if tg is None:
        return

    from telebot import types as tbtypes  # type: ignore

    def _is_admin(uid: int) -> bool:
        try:
            return uid in tg.authorized_users
        except Exception:
            return False

    # ───── Рендер главного меню ─────────────────────────────────────────
    def _text_main() -> str:
        accs = list_accounts()
        asgns = list_assignments()
        active = [a for a in asgns.values() if a.get("status") == "active"]
        revoked = [a for a in asgns.values() if a.get("status") == "revoked"]
        frozen = [a for a in accs if a.get("frozen")]
        busy = {str(x.get("alias", "")).lower() for x in active}
        free = [a for a in accs
                if not a.get("frozen")
                and str(a.get("alias", "")).lower() not in busy]
        lots = list_lots()
        games = list_games()
        cfg = get_config()
        return (
            f"<b>♾ Steam Offline v{VERSION}</b>\n\n"
            f"Аккаунтов: <b>{len(accs)}</b> "
            f"(свободно: {len(free)}, выдано: {len(active)}, "
            f"заморожено: {len(frozen)})\n"
            f"Отозвано выдач: <b>{len(revoked)}</b>\n"
            f"Игр: <b>{len(games)}</b> • "
            f"Лотов настроено: <b>{len(lots)}</b>\n\n"
            f"Лимит кодов по умолчанию: <b>{cfg.get('default_guard_limit')}</b>\n"
            f"Автовыдача: "
            f"<b>{'вкл' if cfg.get('auto_deliver') else 'выкл'}</b>\n\n"
            "Выбери раздел:"
        )

    def _kb_main() -> tbtypes.InlineKeyboardMarkup:
        accs = list_accounts()
        asgns = list_assignments()
        active = [a for a in asgns.values() if a.get("status") == "active"]
        lots = list_lots()
        games = list_games()
        kb = tbtypes.InlineKeyboardMarkup(row_width=2)
        kb.add(
            tbtypes.InlineKeyboardButton(
                f"📋 Аккаунты ({len(accs)})",
                callback_data="so:accs:0"),
            tbtypes.InlineKeyboardButton(
                f"♾ Выдачи ({len(active)})", callback_data="so:asgns:0"),
        )
        kb.add(
            tbtypes.InlineKeyboardButton(
                f"🎮 Игры ({len(games)})", callback_data="so:games"),
            tbtypes.InlineKeyboardButton(
                f"🎯 Лоты ({len(lots)})", callback_data="so:lots"),
        )
        kb.add(
            tbtypes.InlineKeyboardButton(
                "⚙ Настройки", callback_data="so:settings"),
            tbtypes.InlineKeyboardButton(
                "📊 Статистика", callback_data="so:stats"),
        )
        kb.add(
            tbtypes.InlineKeyboardButton(
                "🔧 Инструменты", callback_data="so:tools"),
        )
        kb.add(
            tbtypes.InlineKeyboardButton(
                "❌ Закрыть", callback_data="so:close"),
        )
        return kb

    def _text_tools() -> str:
        return "<b>🔧 Инструменты</b>\n\nВыберите раздел:"

    def _kb_tools() -> tbtypes.InlineKeyboardMarkup:
        kb = tbtypes.InlineKeyboardMarkup(row_width=2)
        kb.add(
            tbtypes.InlineKeyboardButton(
                "📝 Шаблоны", callback_data="so:templates"),
            tbtypes.InlineKeyboardButton(
                "📜 История", callback_data="so:history"),
        )
        kb.add(
            tbtypes.InlineKeyboardButton(
                "🚩 Ивенты", callback_data="so:events"),
            tbtypes.InlineKeyboardButton(
                "🛡 Denuvo", callback_data="so:denuvo"),
        )
        kb.add(
            tbtypes.InlineKeyboardButton(
                "📝 Инструкция", callback_data="so:instructions"),
            tbtypes.InlineKeyboardButton(
                "❓ Помощь", callback_data="so:help"),
        )
        kb.add(
            tbtypes.InlineKeyboardButton(
                "◀️ Назад", callback_data="so:main"),
        )
        return kb

    # ───── Аккаунты ──────────────────────────────────────────────────
    def _text_accs() -> str:
        accs = list_accounts()
        if not accs:
            return ("<b>📋 Аккаунты</b>\n\nПул пуст. Добавь "
                    "Steam-аккаунт через «➕ Добавить».\n\n"
                    "Потребуется: alias (ярлык), .maFile, пароль.")
        active_aliases = {
            str(a.get("alias", "")).lower()
            for a in list_assignments().values()
            if a.get("status") == "active"
        }
        multi = bool(get_config().get("allow_multi_issue", True))
        frozen = sum(1 for a in accs if a.get("frozen"))
        in_work = sum(1 for a in accs
                      if str(a.get("alias", "")).lower() in active_aliases
                      and not a.get("frozen"))
        if multi:
            # В multi-issue режиме «выдан» != «занят»: акк остаётся
            # доступным для новых покупателей. Поэтому считаем «свободно»
            # = все не-замороженные (любой из них можно продать ещё раз),
            # а «в работе» — отдельный inform-счётчик.
            free = sum(1 for a in accs if not a.get("frozen"))
            return (
                "<b>📋 Аккаунты</b>\n\n"
                f"Всего: <b>{len(accs)}</b> "
                f"(свободно: {free}, в работе: {in_work}, "
                f"заморожено: {frozen})"
            )
        # Legacy single-issue: «выдано» реально блокирует акк.
        free = sum(1 for a in accs
                    if not a.get("frozen")
                    and str(a.get("alias", "")).lower() not in active_aliases)
        return (
            "<b>📋 Аккаунты</b>\n\n"
            f"Всего: <b>{len(accs)}</b> "
            f"(свободно: {free}, выдано: {in_work}, заморожено: {frozen})"
        )

    def _kb_accs(page: int = 0) -> tbtypes.InlineKeyboardMarkup:
        accs = list_accounts()
        # alias.lower() -> count активных выдач
        active_counts: dict[str, int] = {}
        for a in list_assignments().values():
            if a.get("status") != "active":
                continue
            al = str(a.get("alias", "")).lower()
            if al:
                active_counts[al] = active_counts.get(al, 0) + 1
        multi = bool(get_config().get("allow_multi_issue", True))
        per_page = 8
        kb = tbtypes.InlineKeyboardMarkup(row_width=1)
        start = page * per_page
        for a in accs[start:start + per_page]:
            al_low = str(a.get("alias", "")).lower()
            n_active = active_counts.get(al_low, 0)
            is_issued = n_active > 0
            if a.get("frozen"):
                marker = "❄️"
            elif is_issued and not multi:
                # Legacy: 1 alias = 1 активная выдача → акк действительно занят
                marker = "🔴"
            else:
                # Multi-issue (default): акк остаётся доступным даже при
                # активных выдачах. Показываем 🟢, чтобы оператор не путался.
                marker = "🟢"
            label = f"{marker} {a['alias']}"
            if a.get("game"):
                label += f" • {a['game'][:10]}"
            if is_issued:
                if multi:
                    # В multi-issue показываем суммарную нагрузку, а не
                    # одного покупателя (их может быть много).
                    label += f" — {n_active} в работе"
                else:
                    asgn = find_active_assignment_by_alias(a["alias"])
                    if asgn:
                        label += (f" — {asgn.get('buyer_username', '?')[:12]}"
                                  f" {asgn.get('codes_used', 0)}/"
                                  f"{asgn.get('codes_limit', 0)}")
            kb.add(tbtypes.InlineKeyboardButton(
                label, callback_data=f"so:acc:{_sid(a['alias'])}"))
        nav: list[tbtypes.InlineKeyboardButton] = []
        if page > 0:
            nav.append(tbtypes.InlineKeyboardButton(
                "◀️", callback_data=f"so:accs:{page - 1}"))
        if start + per_page < len(accs):
            nav.append(tbtypes.InlineKeyboardButton(
                "▶️", callback_data=f"so:accs:{page + 1}"))
        if nav:
            kb.row(*nav)
        kb.add(tbtypes.InlineKeyboardButton(
            "➕ Добавить аккаунт", callback_data="so:acc_add"))
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ Назад", callback_data="so:main"))
        return kb

    def _text_acc(alias: str, *, show_pw: bool = False,
                  show_active: bool = False) -> str:
        acc = find_account(alias)
        if not acc:
            return "Аккаунт не найден."
        active_asgns = find_active_assignments_list_by_alias(alias)
        asgn = active_asgns[0] if active_asgns else None
        n_active = len(active_asgns)
        multi = bool(get_config().get("allow_multi_issue", True))
        # В multi-issue режиме (default v1.5+) активные выдачи НЕ блокируют
        # аккаунт — он по-прежнему доступен для новых продаж, лоты висят
        # активированными. Поэтому показываем 🟢 (свободен/в работе),
        # а не 🔴 (выдан/занят) — чтобы оператор не пугался «заморозки».
        if acc.get("frozen"):
            state = "❄️ заморожен"
        elif n_active == 0:
            state = "🟢 свободен"
        elif multi:
            state = (f"🟢 свободен • в работе у "
                     f"<b>{n_active}</b> "
                     f"{'покупателя' if n_active == 1 else 'покупателей'}")
        else:
            state = "🔴 выдан (assignment)"
        pw_line = (f"🔑 Пароль: <code>{_esc(acc.get('password', '?'))}</code>\n"
                    if show_pw
                    else "🔑 Пароль: <i>скрыт (нажми «🔑 Пароль»)</i>\n")
        text = (
            f"<b>📋 Аккаунт</b> <code>{_esc(alias)}</code>\n\n"
            f"Статус: <b>{state}</b>\n"
            f"👤 Login: <code>{_esc(acc.get('account_name', '?'))}</code>\n"
            + pw_line +
            f"🎮 Игра: <b>{_esc(acc.get('game') or '—')}</b>\n"
            f"🆔 SteamID: <code>{_esc(str(acc.get('steamid') or '—'))}</code>\n"
            f"⚠️ Ошибок логина: "
            f"<b>{acc.get('login_failures', 0)}</b>"
        )
        if active_asgns:
            # v1.7.1: по умолчанию скрываем подробный список выдач —
            # в multi-issue режиме под одним alias может быть до 10
            # покупателей одновременно, и портянка из них раздувает
            # карточку без необходимости. Раскрыть — кнопка
            # «👀 Показать выдачи» в меню аккаунта (callback show_active).
            if not show_active and n_active > 1:
                text += (
                    f"\n\n♾ <b>В работе у {n_active} "
                    f"{'покупателя' if n_active == 1 else 'покупателей'}</b>"
                    f"  <i>(жми «👀 Активные выдачи» для деталей)</i>")
            elif n_active == 1:
                a0 = active_asgns[0]
                text += (
                    f"\n\n♾ Активная выдача: "
                    f"<code>{_esc(a0['id'])}</code>\n"
                    f"Покупатель: "
                    f"<b>{_esc(a0.get('buyer_username', '?'))}</b>\n"
                    f"Коды: <b>{a0.get('codes_used', 0)} / "
                    f"{a0.get('codes_limit', 0)}</b>"
                )
            else:
                # Multi-issue + show_active=True: компактный список выдач
                # (до 8 — лимит длины Telegram-сообщения).
                MAX_SHOW = 8
                lines = [f"\n\n♾ <b>Активных выдач: {n_active}</b>"]
                for a0 in active_asgns[:MAX_SHOW]:
                    lines.append(
                        f"  • <code>{_esc(a0['id'])}</code> — "
                        f"<b>{_esc(a0.get('buyer_username', '?'))}</b> "
                        f"({a0.get('codes_used', 0)}/"
                        f"{a0.get('codes_limit', 0)})"
                    )
                if n_active > MAX_SHOW:
                    lines.append(f"  … и ещё {n_active - MAX_SHOW}")
                text += "\n".join(lines)

        # ── 💵 Финансы (краткая строка; полная — по кнопке 📊) ─────
        st = acc.get("stats") or {}
        delivered = int(st.get("delivered_count", 0) or 0)
        revenue = float(st.get("total_revenue", 0) or 0)
        cost = float(acc.get("cost", 0.0) or 0.0)
        profit = revenue - cost
        roi_str = ""
        if cost > 0:
            roi_str = f" • ROI {(profit / cost) * 100:+.0f}%"
        text += (
            f"\n\n💵 <b>Финансы:</b> "
            f"{revenue:.0f}₽ − {cost:.0f}₽ = "
            f"<b>{profit:+.0f}₽</b>{roi_str}\n"
            f"📦 Выдач: <b>{delivered}</b> • "
            "<i>(подробнее — 📊 Статистика)</i>"
        )
        return text

    def _kb_acc(alias: str) -> tbtypes.InlineKeyboardMarkup:
        acc = find_account(alias)
        kb = tbtypes.InlineKeyboardMarkup(row_width=2)
        if not acc:
            kb.add(tbtypes.InlineKeyboardButton(
                "◀️ К списку", callback_data="so:accs:0"))
            return kb
        sid = _sid(alias)
        kb.add(
            tbtypes.InlineKeyboardButton(
                "🔑 Пароль", callback_data=f"so:show:{sid}"),
            tbtypes.InlineKeyboardButton(
                "🛡 Guard", callback_data=f"so:guardacc:{sid}"),
        )
        kb.add(
            tbtypes.InlineKeyboardButton(
                "🔁 Сменить пароль",
                callback_data=f"so:chpwd:{sid}"),
            tbtypes.InlineKeyboardButton(
                "📤 Отозвать сессии",
                callback_data=f"so:revoke_sess:{sid}"),
        )
        if acc.get("frozen"):
            kb.add(tbtypes.InlineKeyboardButton(
                "🔥 Разморозить",
                callback_data=f"so:acc_freeze:{sid}"))
        else:
            kb.add(tbtypes.InlineKeyboardButton(
                "❄️ Заморозить",
                callback_data=f"so:acc_freeze:{sid}"))
        kb.add(
            tbtypes.InlineKeyboardButton(
                "🎮 Установить игру",
                callback_data=f"so:setgame:{sid}"),
            tbtypes.InlineKeyboardButton(
                "🔍 Проверить",
                callback_data=f"so:check:{sid}"),
        )
        cost_val = float(acc.get("cost", 0.0) or 0.0)
        cost_lbl = f"💰 Стоимость: {cost_val:.0f}₽" if cost_val > 0 \
            else "💰 Стоимость"
        kb.add(
            tbtypes.InlineKeyboardButton(
                cost_lbl, callback_data=f"so:setcost:{sid}"),
            tbtypes.InlineKeyboardButton(
                "📊 Статистика",
                callback_data=f"so:accstats:{sid}"),
        )
        # v1.7.1: кнопка раскрытия списка активных выдач (multi-issue).
        # По умолчанию список скрыт в карточке аккаунта чтобы не шумел.
        n_active_for_btn = len(find_active_assignments_list_by_alias(alias))
        if n_active_for_btn > 0:
            kb.add(tbtypes.InlineKeyboardButton(
                f"👀 Активные выдачи ({n_active_for_btn})",
                callback_data=f"so:accactive:{sid}"))
        kb.add(
            tbtypes.InlineKeyboardButton(
                "🗝 Удалить",
                callback_data=f"so:acc_del:{sid}"),
        )
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ К списку", callback_data="so:accs:0"))
        return kb

    # ───── Выдачи ────────────────────────────────────────────────────────
    def _text_asgns() -> str:
        asgns = [a for a in list_assignments().values()
                 if a.get("status") == "active"]
        if not asgns:
            return ("<b>♾ Активные выдачи</b>\n\nПока нет ни одной выдачи.\n\n"
                    "Чтобы выдать аккаунт — клиент должен оплатить лот "
                    "из «🎯 Лоты», либо выдай вручную через карточку лота.")
        return f"<b>♾ Активные выдачи ({len(asgns)})</b>"

    def _kb_asgns(page: int = 0) -> tbtypes.InlineKeyboardMarkup:
        asgns = [a for a in list_assignments().values()
                 if a.get("status") == "active"]
        per_page = 8
        kb = tbtypes.InlineKeyboardMarkup(row_width=1)
        start = page * per_page
        for a in asgns[start:start + per_page]:
            used = int(a.get("codes_used", 0))
            limit = int(a.get("codes_limit", 0))
            label = (f"{a.get('alias', '?')} • "
                     f"{a.get('buyer_username', '?')[:14]} • "
                     f"{used}/{limit}")
            kb.add(tbtypes.InlineKeyboardButton(
                label, callback_data=f"so:asgn:{_sid(a['id'])}"))
        nav: list[tbtypes.InlineKeyboardButton] = []
        if page > 0:
            nav.append(tbtypes.InlineKeyboardButton(
                "◀️", callback_data=f"so:asgns:{page - 1}"))
        if start + per_page < len(asgns):
            nav.append(tbtypes.InlineKeyboardButton(
                "▶️", callback_data=f"so:asgns:{page + 1}"))
        if nav:
            kb.row(*nav)
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ Назад", callback_data="so:main"))
        return kb

    def _text_asgn(asgn_id: str) -> str:
        a = find_assignment(asgn_id)
        if not a:
            return "Выдача не найдена."
        used = int(a.get("codes_used", 0))
        limit = int(a.get("codes_limit", 0))
        left = max(0, limit - used) if limit > 0 else 0
        last = a.get("last_code_at")
        last_str = _fmt_ts(last) + " UTC" if last else "—"
        status = "🟢 активна" if a.get("status") == "active" else "❌ отозвана"
        return (
            f"<b>♾ Выдача</b> <code>{_esc(asgn_id)}</code>\n\n"
            f"Статус: <b>{status}</b>\n"
            f"Аккаунт: <code>{_esc(a.get('alias', '?'))}</code> "
            f"(login: <code>{_esc(a.get('account_name', '?'))}</code>)\n"
            f"Покупатель: <b>{_esc(a.get('buyer_username', '?'))}</b> "
            f"(<code>{_esc(str(a.get('buyer_id', '?')))}</code>)\n"
            f"Заказ: <code>#{_esc(str(a.get('order_id', '?')))}</code>\n"
            f"Создана: <code>{_fmt_ts(int(a.get('created_at', 0)))}</code> UTC\n\n"
            f"🔢 Кодов использовано: <b>{used} / {limit}</b>\n"
            f"🔢 Осталось: <b>{left}</b>\n"
            f"⏱ Последний код: <code>{last_str}</code>"
        )

    def _kb_asgn(asgn_id: str) -> tbtypes.InlineKeyboardMarkup:
        a = find_assignment(asgn_id)
        kb = tbtypes.InlineKeyboardMarkup(row_width=2)
        if not a:
            kb.add(tbtypes.InlineKeyboardButton(
                "◀️ Назад", callback_data="so:asgns:0"))
            return kb
        sid = _sid(asgn_id)
        if a.get("status") == "active":
            kb.add(
                tbtypes.InlineKeyboardButton(
                    "🔢 Лимит", callback_data=f"so:limit:{sid}"),
                tbtypes.InlineKeyboardButton(
                    "🔄 Сбросить счётчик", callback_data=f"so:reset:{sid}"),
            )
            kb.add(
                tbtypes.InlineKeyboardButton(
                    "🛡 Дать код вручную", callback_data=f"so:guard:{sid}"),
                tbtypes.InlineKeyboardButton(
                    "❌ Отозвать", callback_data=f"so:revoke:{sid}"),
            )
        kb.add(
            tbtypes.InlineKeyboardButton(
                "🗑 Удалить запись", callback_data=f"so:del:{sid}"),
        )
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ К списку", callback_data="so:asgns:0"))
        return kb

    # ───── Лоты ──────────────────────────────────────────────────────────
    def _text_lots() -> str:
        lots = list_lots()
        if not lots:
            return ("<b>🎯 Офлайн-лоты</b>\n\nНи одного лота не настроено.\n\n"
                    "Добавь лот: укажи его FunPay-ID (или ключевое слово в "
                    "названии), пул alias-ов аккаунтов и опц. лимит кодов.")
        return f"<b>🎯 Офлайн-лоты ({len(lots)})</b>"

    def _kb_lots() -> tbtypes.InlineKeyboardMarkup:
        lots = list_lots()
        kb = tbtypes.InlineKeyboardMarkup(row_width=1)
        for key, val in lots.items():
            denuvo_tag = "🛡 " if val.get("denuvo") else ""
            free = _count_free_for_lot(val)
            total = len(val.get("aliases", []))
            limit = val.get("guard_limit") or "по умолч."
            label = f"{denuvo_tag}{key} • {free}/{total} своб. • лимит: {limit}"
            kb.add(tbtypes.InlineKeyboardButton(
                label, callback_data=f"so:lot:{_sid(key)}"))
        kb.add(tbtypes.InlineKeyboardButton(
            "➕ Добавить лот", callback_data="so:lot_add"))
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ Назад", callback_data="so:main"))
        return kb

    def _text_lot(key: str) -> str:
        val = list_lots().get(key)
        if not val:
            return "Лот не найден."
        free = _count_free_for_lot(val)
        total = len(val.get("aliases", []))
        denuvo_block = ""
        # v1.8.0: визуально показываем эффективное состояние (с учётом
        # игры) и подсвечиваем источник.
        eff_denuvo = _lot_is_denuvo(val)
        own = val.get("denuvo")
        if eff_denuvo:
            d_limit = _denuvo_lot_limit(val)
            used, cap = _sum_denuvo_slots_for_lot(val)
            if own is True:
                src_lbl = "ВКЛ (per-lot)"
            elif own is False:
                src_lbl = "ВКЛ"  # не достижимо логически, но безопасно
            else:
                src_lbl = "ВКЛ (от игры)"
            denuvo_block = (
                f"\n🛡 <b>Denuvo-режим:</b> {src_lbl}\n"
                f"  Лимит/акк/день: <b>{d_limit}</b>\n"
                f"  Слоты сегодня (UTC): <b>{used} / {cap}</b>\n"
                f"  Сброс: 00:00 UTC\n")
        elif own is False:
            denuvo_block = (
                "\n🛡 <b>Denuvo-режим:</b> ВЫКЛ "
                "<i>(переопределено per-lot — игра помечена Denuvo, "
                "но этот лот его не использует)</i>\n")
        return (
            f"<b>🎯 Лот</b> <code>{_esc(key)}</code>\n\n"
            f"Игра: <b>{_esc(val.get('game') or '—')}</b>\n"
            f"Лимит кодов: <b>{val.get('guard_limit') or 'из настроек'}</b>"
            f"{denuvo_block}\n"
            f"Аккаунтов в пуле: <b>{total}</b> "
            f"(свободно для выдачи: <b>{free}</b>)\n\n"
            f"Aliases: <code>{_esc(', '.join(val.get('aliases', [])) or '—')}</code>"
        )

    def _kb_lot(key: str) -> tbtypes.InlineKeyboardMarkup:
        sid = _sid(key)
        val = list_lots().get(key) or {}
        # v1.8.0: tri-state Denuvo. own — что записано на лоте; eff —
        # реальное поведение (с учётом игры).
        own = val.get("denuvo")
        eff_denuvo = _lot_is_denuvo(val)
        if own is True:
            denuvo_lbl = "💎 Denuvo: ✅ перекл-вкл"
        elif own is False:
            denuvo_lbl = "💎 Denuvo: ❌ перекл-выкл"
        else:
            denuvo_lbl = (f"💎 Denuvo: 🔄 от игры "
                          f"({'✅' if eff_denuvo else '❌'})")
        kb = tbtypes.InlineKeyboardMarkup(row_width=2)
        kb.add(
            tbtypes.InlineKeyboardButton(
                "👥 Пул", callback_data=f"so:lot_pool:{sid}"),
            tbtypes.InlineKeyboardButton(
                "🎮 Игра", callback_data=f"so:lot_game:{sid}"),
        )
        kb.add(
            tbtypes.InlineKeyboardButton(
                "🔢 Лимит кодов", callback_data=f"so:lot_limit:{sid}"),
            tbtypes.InlineKeyboardButton(
                denuvo_lbl, callback_data=f"so:lot_denuvo:{sid}"),
        )
        if eff_denuvo:
            d_limit = _denuvo_lot_limit(val)
            kb.add(tbtypes.InlineKeyboardButton(
                f"🛡 Лимит/день: {d_limit}",
                callback_data=f"so:lot_denuvo_lim:{sid}"))
        kb.add(tbtypes.InlineKeyboardButton(
            "🗑 Удалить", callback_data=f"so:lot_del:{sid}"))
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ К лотам", callback_data="so:lots"))
        return kb

    # ───── Игры (game → lots) ────────────────────────────────────────────
    def _text_games() -> str:
        games = list_games()
        if not games:
            return ("<b>🎮 Игры</b>\n\n"
                    "Пока нет ни одной игры.\n\n"
                    "Создай игру через «➕ Добавить игру», "
                    "потом привяжи к ней FunPay-лоты, и бот сам "
                    "будет матчить заказы по названию игры.")
        # Сводка по лотам
        lots = list_lots()
        n_main = n_ext = 0
        for g in games.values():
            n_main += len(g.get("lot_ids") or [])
            n_ext += len(g.get("ext_lot_ids") or [])
        return (f"<b>🎮 Игры ({len(games)})</b>\n\n"
                f"Привязано лотов: <b>{n_main}</b> main"
                + (f", <b>{n_ext}</b> ext" if n_ext else "")
                + "\n\nВыбери игру:")

    def _kb_games() -> tbtypes.InlineKeyboardMarkup:
        games = list_games()
        lots = list_lots()
        kb = tbtypes.InlineKeyboardMarkup(row_width=1)
        for gkey, g in games.items():
            main_ids = g.get("lot_ids") or []
            # Сколько свободных аккаунтов суммарно по main-лотам этой игры
            all_aliases: set[str] = set()
            for lid in main_ids:
                lot = lots.get(str(lid), {})
                all_aliases.update(_combined_lot_pool_offline(lot))
            free = sum(1 for a in all_aliases
                        if not _is_alias_busy_offline(a))
            label = (f"🎮 {g.get('name', gkey)} — "
                     f"{len(main_ids)} лот." +
                     (f" • {free} св." if free else " • 0 св."))
            kb.add(tbtypes.InlineKeyboardButton(
                label, callback_data=f"so:game:{_sid(gkey)}"))
        kb.add(tbtypes.InlineKeyboardButton(
            "🔁 Переактивировать все лоты",
            callback_data="so:reacttlots"))
        kb.add(tbtypes.InlineKeyboardButton(
            "➕ Добавить игру", callback_data="so:addgame"))
        kb.add(tbtypes.InlineKeyboardButton(
            "📋 Старый список лотов (legacy)", callback_data="so:lots"))
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ Назад", callback_data="so:main"))
        return kb

    def _text_game(gkey: str) -> str:
        g = get_game(gkey)
        if not g:
            return "Игра не найдена."
        lots = list_lots()
        main_ids = g.get("lot_ids") or []
        # Считаем свободные суммарно
        all_aliases: set[str] = set()
        for lid in main_ids:
            all_aliases.update(_combined_lot_pool_offline(
                lots.get(str(lid), {})))
        free = sum(1 for a in all_aliases
                    if not _is_alias_busy_offline(a))

        def _lot_lbl(lid):
            lot = lots.get(str(lid), {})
            if not lot:
                return f"<code>{_esc(str(lid))}</code> (нет в БД)"
            l_free = _count_free_for_lot(lot)
            cache = _LOT_ACTIVATION_CACHE.get(str(lid))
            if cache is None or cache.get("result") == "fail":
                icon = "❓"
            elif cache.get("active"):
                icon = "✅"
            else:
                icon = "⛔"
            return f"{icon} <code>{_esc(str(lid))}</code> ({l_free} св.)"

        main_lines = ", ".join(_lot_lbl(x) for x in main_ids) or "—"
        return (
            f"<b>🎮 {_esc(g.get('name', gkey))}</b>\n"
            f"<i>key: <code>{_esc(gkey)}</code></i>\n\n"
            f"<b>Лоты</b>: {main_lines}\n\n"
            f"Свободно сейчас: <b>{free}</b>\n\n"
            "<i>✅ — лот включён на FunPay, ⛔ — выключен, "
            "❓ — состояние ещё не синхронизировано.</i>"
        )

    def _kb_game(gkey: str) -> tbtypes.InlineKeyboardMarkup:
        g = get_game(gkey)
        if not g:
            return tbtypes.InlineKeyboardMarkup()
        lots = list_lots()
        main_ids = g.get("lot_ids") or []
        kb = tbtypes.InlineKeyboardMarkup(row_width=1)

        def _lot_btn(lid):
            lot = lots.get(str(lid), {})
            cache = _LOT_ACTIVATION_CACHE.get(str(lid))
            if cache is None or cache.get("result") == "fail":
                icon = "❓"
            elif cache.get("active"):
                icon = "✅"
            else:
                icon = "⛔"
            l_free = _count_free_for_lot(lot) if lot else 0
            label = (f"{icon} 🎯 {str(lid)[:18]} ({l_free} св.)")
            return tbtypes.InlineKeyboardButton(
                label, callback_data=f"so:lot:{_sid(str(lid))}")

        if main_ids:
            for lid in main_ids:
                kb.add(_lot_btn(lid))

        kb.add(tbtypes.InlineKeyboardButton(
            "🔄 Обновить статус лотов этой игры",
            callback_data=f"so:game_react:{_sid(gkey)}"))
        kb.add(tbtypes.InlineKeyboardButton(
            "➕ Добавить лот", callback_data=f"so:gaddmain:{_sid(gkey)}"))
        # v1.8.0: общий picker аккаунтов игры. Привязывает acc.game_key
        # сразу для всех выбранных аккаунтов, расширяет пул всех лотов
        # этой игры через _combined_lot_pool_offline.
        n_acc_for_game = sum(
            1 for a in list_accounts()
            if (a.get("game_key") or "").strip().lower() == gkey.lower()
        )
        kb.add(tbtypes.InlineKeyboardButton(
            f"👥 Аккаунты игры ({n_acc_for_game})",
            callback_data=f"so:gameacc:{_sid(gkey)}"))
        # v1.8.0: Denuvo per-game toggle.
        denuvo_on = bool(g.get("denuvo"))
        kb.add(tbtypes.InlineKeyboardButton(
            f"💎 Denuvo для игры: {'✅' if denuvo_on else '❌'}",
            callback_data=f"so:gdenuvo:{_sid(gkey)}"))
        kb.add(tbtypes.InlineKeyboardButton(
            "🗑 Удалить игру", callback_data=f"so:gdel:{_sid(gkey)}"))
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ К играм", callback_data="so:games"))
        return kb

    # ───── Denuvo обзор ──────────────────────────────────────────────────
    def _text_denuvo() -> str:
        cfg = get_config()
        accs = list_accounts()
        lots = list_lots()
        denuvo_lots = [(k, v) for k, v in lots.items() if v.get("denuvo")]
        # Аккаунты, фигурирующие хоть в одном Denuvo-лоте.
        denuvo_aliases: set[str] = set()
        for _, v in denuvo_lots:
            for a in v.get("aliases", []) or []:
                denuvo_aliases.add(a.lower())

        lines = ["<b>🛡 Denuvo — daily-слоты устройств</b>", ""]
        lines.append(
            "Лимит Denuvo: <b>5 новых устройств в день</b> на акк "
            "(сброс в 00:00 UTC). Можно переопределить per-lot.")
        lines.append(f"Лимит по умолчанию: "
                     f"<b>{cfg.get('denuvo_default_limit', 5)}</b>")
        lines.append("")
        lines.append(f"Лотов с Denuvo: <b>{len(denuvo_lots)}</b>")
        lines.append(f"Аккаунтов в Denuvo-пулах: <b>{len(denuvo_aliases)}</b>")
        lines.append("")
        if not denuvo_lots:
            lines.append(
                "<i>Включи Denuvo на нужных лотах через "
                "🎯 Лоты → выбрать лот → Denuvo: ВЫКЛ → ВКЛ.</i>")
        else:
            lines.append("<b>По лотам:</b>")
            for k, v in denuvo_lots:
                used, cap = _sum_denuvo_slots_for_lot(v)
                free = _count_free_for_lot(v)
                total = len(v.get("aliases", []) or [])
                d_limit = _denuvo_lot_limit(v)
                lines.append(
                    f"• <code>{_esc(k)}</code> "
                    f"(лимит {d_limit}/день): "
                    f"<b>{used}/{cap}</b> слотов, "
                    f"<b>{free}/{total}</b> акков с свободными слотами")
            lines.append("")
            lines.append("<b>По аккаунтам (с использованными слотами):</b>")
            shown = 0
            for a in accs:
                al = (a.get("alias") or "").lower()
                if al not in denuvo_aliases:
                    continue
                _, cnt = _denuvo_get_counter(a)
                if cnt <= 0 and not a.get("frozen"):
                    continue
                # find max d_limit among lots that include this alias
                max_lim = 0
                for _, v in denuvo_lots:
                    if al in [x.lower() for x in (v.get("aliases") or [])]:
                        max_lim = max(max_lim, _denuvo_lot_limit(v))
                if max_lim == 0:
                    max_lim = int(cfg.get("denuvo_default_limit", 5))
                marker = "❄️" if a.get("frozen") else (
                    "⛔" if cnt >= max_lim else "🔵")
                lines.append(
                    f"  {marker} <code>{_esc(a['alias'])}</code>: "
                    f"<b>{cnt}/{max_lim}</b>")
                shown += 1
                if shown >= 30:
                    lines.append("  …")
                    break
        return "\n".join(lines)

    def _kb_denuvo() -> tbtypes.InlineKeyboardMarkup:
        cfg = get_config()
        kb = tbtypes.InlineKeyboardMarkup(row_width=1)
        kb.add(tbtypes.InlineKeyboardButton(
            f"🛡 Лимит по умолчанию: {cfg.get('denuvo_default_limit', 5)}",
            callback_data="so:set:denuvo_default_limit"))
        kb.add(tbtypes.InlineKeyboardButton(
            "♻️ Сбросить дневные счётчики (всем)",
            callback_data="so:denuvo_reset_all"))
        kb.add(tbtypes.InlineKeyboardButton(
            "🔄 Обновить", callback_data="so:denuvo"))
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ Назад", callback_data="so:main"))
        return kb

    # ───── Настройки ─────────────────────────────────────────────────────
    _SETTINGS_LABELS = {
        "default_guard_limit": "🔢 Лимит кодов по умолчанию",
        "guardik_command": "✏️ Команда (info)",
    }
    _SETTINGS_TOGGLES = {
        "auto_deliver": "Автовыдача после оплаты",
        "change_password_on_issue": "Менять пароль при выдаче",
        "revoke_sessions_enabled": "📤 Отзыв сессий (ручная кнопка)",
        "tg_notify": "Уведомления в Telegram",
    }

    def _text_settings() -> str:
        cfg = get_config()
        lines = ["<b>⚙ Настройки</b>\n"]
        for k, label in _SETTINGS_TOGGLES.items():
            v = cfg.get(k)
            mark = "✅" if v else "❌"
            lines.append(f"{mark} {label}")
        lines.append("")
        lines.append(f"🔢 Лимит кодов по умолчанию: "
                     f"<b>{cfg.get('default_guard_limit')}</b>")
        lines.append(f"✏️ Команда Steam Guard: "
                     f"<code>{_esc(cfg.get('guardik_command', '!код'))}</code>")
        return "\n".join(lines)

    def _kb_settings() -> tbtypes.InlineKeyboardMarkup:
        cfg = get_config()
        kb = tbtypes.InlineKeyboardMarkup(row_width=1)
        for k, label in _SETTINGS_TOGGLES.items():
            v = cfg.get(k)
            kb.add(tbtypes.InlineKeyboardButton(
                ("✅ " if v else "❌ ") + label,
                callback_data=f"so:toggle:{k}"))
        kb.add(tbtypes.InlineKeyboardButton(
            "🔢 Лимит кодов по умолчанию",
            callback_data="so:set:default_guard_limit"))
        kb.add(tbtypes.InlineKeyboardButton(
            f"✏️ Команда: {cfg.get('guardik_command', '!код')}",
            callback_data="so:set:guardik_command"))
        # v5
        m_on = "✅" if cfg.get("metrics_enabled") else "❌"
        kb.add(tbtypes.InlineKeyboardButton(
            f"{m_on} Prometheus /metrics", callback_data="so:metset"))
        s_on = "✅" if cfg.get("daily_summary_enabled", True) else "❌"
        kb.add(tbtypes.InlineKeyboardButton(
            f"{s_on} Daily summary", callback_data="so:dsumset"))
        # v1.9.0: blacklist покупателей
        bl_n = len(list_blacklist())
        kb.add(tbtypes.InlineKeyboardButton(
            f"🚫 Blacklist ({bl_n})", callback_data="so:blist"))
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ Назад", callback_data="so:main"))
        return kb

    # ── v5: Prometheus + daily summary меню ──────────────────────────
    def _kb_metset() -> tbtypes.InlineKeyboardMarkup:
        cfg = get_config()
        kb = tbtypes.InlineKeyboardMarkup(row_width=1)
        on = "✅" if cfg.get("metrics_enabled") else "❌"
        kb.add(tbtypes.InlineKeyboardButton(
            f"{on} Включить /metrics",
            callback_data="so:toggle:metrics_enabled"))
        kb.add(tbtypes.InlineKeyboardButton(
            f"🔌 Порт: {cfg.get('metrics_port', 9102)}",
            callback_data="so:set:metrics_port"))
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ Назад", callback_data="so:settings"))
        return kb

    def _text_metset() -> str:
        cfg = get_config()
        running = "✅" if (_metrics_http_thread is not None
                          and _metrics_http_thread.is_alive()) else "❌"
        return (
            "<b>📈 Prometheus /metrics (steam_offline)</b>\n\n"
            f"Состояние сервера: {running}\n"
            f"Порт: <b>{cfg.get('metrics_port', 9102)}</b>\n"
            f"Bind: <code>{_esc(cfg.get('metrics_bind') or '0.0.0.0')}</code>\n\n"
            "Метрики: <code>steam_offline_aso_*</code>.\n"
            "ВАЖНО: если используется и steam_rental — порты должны "
            "отличаться (по умолчанию 9101 / 9102)."
        )

    def _kb_dsumset() -> tbtypes.InlineKeyboardMarkup:
        cfg = get_config()
        kb = tbtypes.InlineKeyboardMarkup(row_width=1)
        on = "✅" if cfg.get("daily_summary_enabled", True) else "❌"
        kb.add(tbtypes.InlineKeyboardButton(
            f"{on} Включить ежедневную сводку",
            callback_data="so:toggle:daily_summary_enabled"))
        kb.add(tbtypes.InlineKeyboardButton(
            f"🕐 Час UTC: {cfg.get('daily_summary_hour_utc', 21)}",
            callback_data="so:set:daily_summary_hour_utc"))
        kb.add(tbtypes.InlineKeyboardButton(
            "📤 Прислать сейчас", callback_data="so:dsumnow"))
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ Назад", callback_data="so:settings"))
        return kb

    def _text_dsumset() -> str:
        cfg = get_config()
        return (
            "<b>📊 Daily summary (steam_offline)</b>\n\n"
            "Раз в сутки бот шлёт сводку: выдач / Guard-кодов / лимитов / "
            "состояние пула.\n\n"
            f"Включено: "
            f"<b>{'да' if cfg.get('daily_summary_enabled', True) else 'нет'}</b>\n"
            f"Час отправки: "
            f"<b>{cfg.get('daily_summary_hour_utc', 21)}:00 UTC</b> "
            "(21 UTC = 00:00 МСК)."
        )

    # ── v1.9.0: Blacklist меню ───────────────────────────────────────
    def _kb_blacklist() -> tbtypes.InlineKeyboardMarkup:
        kb = tbtypes.InlineKeyboardMarkup(row_width=1)
        cfg = get_config()
        on1 = "✅" if cfg.get("blacklist_enabled", True) else "❌"
        on2 = "✅" if cfg.get("auto_blacklist_on_refund", True) else "❌"
        kb.add(tbtypes.InlineKeyboardButton(
            f"{on1} Блокировка на NEW_ORDER",
            callback_data="so:toggle:blacklist_enabled"))
        kb.add(tbtypes.InlineKeyboardButton(
            f"{on2} Авто-добавление при refund",
            callback_data="so:toggle:auto_blacklist_on_refund"))
        for entry in list_blacklist()[:20]:
            label = (entry.get("username")
                     or f"id:{entry.get('buyer_id')}" or "?")
            kb.add(tbtypes.InlineKeyboardButton(
                f"❌ {label}",
                callback_data=f"so:blrm:{_sid(str(label))}"))
        kb.add(tbtypes.InlineKeyboardButton(
            "➕ Добавить", callback_data="so:bladd"))
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ Назад", callback_data="so:settings"))
        return kb

    def _text_blacklist() -> str:
        items = list_blacklist()
        if not items:
            body = ("Пусто. Покупатель попадёт сюда автоматически после "
                    "refund/cancel (если включено) или вручную.")
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

    def _text_acc_stats(alias: str, st: dict[str, Any]) -> str:
        delivered = int(st.get("delivered_count", 0))
        guard_sent = int(st.get("guard_sent_count", 0))
        guard_limit = int(st.get("guard_limit_reached", 0))
        last_buyer = st.get("last_buyer_username") or "—"
        first = _fmt_ts(int(st.get("first_used_at", 0) or 0))
        last = _fmt_ts(int(st.get("last_used_at", 0) or 0))
        revenue = float(st.get("total_revenue", 0) or 0)
        acc = find_account(alias) or {}
        cost = float(acc.get("cost", 0) or 0)
        profit = revenue - cost
        roi_line = ""
        if cost > 0:
            roi = (profit / cost) * 100
            roi_line = f"  ▸ ROI: <b>{roi:+.0f}%</b>\n"
        return (
            f"<b>📊 Статистика {_esc(alias)}</b>\n\n"
            f"Выдач: <b>{delivered}</b>\n"
            f"Guard-кодов отправлено: <b>{guard_sent}</b>\n"
            f"Достигнут лимит кодов: <b>{guard_limit}</b>\n"
            f"Последний покупатель: <b>{_esc(str(last_buyer))}</b>\n"
            f"Впервые использован: <b>{first}</b> UTC\n"
            f"Последний раз: <b>{last}</b> UTC\n\n"
            f"💵 <b>Финансы по аккаунту</b>\n"
            f"  ▸ Выручка: <b>{revenue:.0f}₽</b>\n"
            f"  ▸ Расход: <b>{cost:.0f}₽</b>\n"
            f"  ▸ Прибыль: <b>{profit:+.0f}₽</b>\n"
            f"{roi_line}"
        )

    # ───── Шаблоны ───────────────────────────────────────────────────────
    # v1.10.0: меню двуязычное. Язык per-admin хранится в
    # `_template_admin_lang[uid]` (по умолчанию "ru"). В заголовке
    # показывается активный флаг, в клавиатуре есть переключатель.
    def _text_templates(uid: int = 0) -> str:
        lang = _get_admin_lang(uid)
        flag = "🇷🇺 RU" if lang == "ru" else "🇬🇧 EN"
        data = _load_templates_file(lang)
        # Считаем по дефолтам, чтобы число всегда соответствовало
        # «сколько ключей доступно для редактирования», а не «сколько
        # переопределено».
        defaults = (_DEFAULT_TEMPLATES if lang == "ru"
                    else _DEFAULT_TEMPLATES_EN)
        n_total = len(defaults)
        n_overridden = sum(
            1 for k, v in data.items()
            if v and v != defaults.get(k))
        return (
            "<b>📝 Шаблоны сообщений</b>\n\n"
            f"Текущий язык: <b>{flag}</b>\n"
            f"Шаблонов: <b>{n_total}</b> "
            f"(переопределено: <b>{n_overridden}</b>)\n\n"
            "Выберите шаблон для редактирования или переключите язык."
        )

    def _kb_templates(uid: int = 0) -> tbtypes.InlineKeyboardMarkup:
        lang = _get_admin_lang(uid)
        kb = tbtypes.InlineKeyboardMarkup(row_width=2)
        # Переключатель языка
        ru_btn = tbtypes.InlineKeyboardButton(
            ("✅ 🇷🇺 RU" if lang == "ru" else "🇷🇺 RU"),
            callback_data="so:tpl_lang:ru")
        en_btn = tbtypes.InlineKeyboardButton(
            ("✅ 🇬🇧 EN" if lang == "en" else "🇬🇧 EN"),
            callback_data="so:tpl_lang:en")
        kb.row(ru_btn, en_btn)
        # Список шаблонов из дефолтов выбранного языка (полнота
        # списка не зависит от того, что seller уже переопределил)
        defaults = (_DEFAULT_TEMPLATES if lang == "ru"
                    else _DEFAULT_TEMPLATES_EN)
        for k in sorted(defaults.keys()):
            kb.add(tbtypes.InlineKeyboardButton(
                k, callback_data=f"so:tpl:{k}"))
        kb.add(tbtypes.InlineKeyboardButton(
            "♻️ Сбросить все к дефолту", callback_data="so:tpl_reset"))
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ Назад", callback_data="so:main"))
        return kb

    def _text_template(tpl_key: str, uid: int = 0) -> str:
        lang = _get_admin_lang(uid)
        flag = "🇷🇺 RU" if lang == "ru" else "🇬🇧 EN"
        defaults = (_DEFAULT_TEMPLATES if lang == "ru"
                    else _DEFAULT_TEMPLATES_EN)
        data = _load_templates_file(lang)
        tpl = data.get(tpl_key) or defaults.get(tpl_key, "")
        is_default = (tpl == defaults.get(tpl_key))
        marker = " <i>(дефолт)</i>" if is_default else " <i>(изменён)</i>"
        return (
            f"<b>📝 Шаблон</b> <code>{_esc(tpl_key)}</code> "
            f"({flag}){marker}\n\n"
            f"<pre>{_esc(tpl)}</pre>\n\n"
            f"Плейсхолдеры: {_placeholders_hint(tpl_key)}"
        )

    def _placeholders_hint(tpl_key: str) -> str:
        hints = {
            "issue": "{login} {password} {game} {codes_limit}",
            "guard_code": "{login} {code} {codes_used} {codes_limit} "
                           "{codes_left}",
            "guard_last_code": "{login} {code}",
            "guard_limit_reached": "{login} {codes_used} {codes_limit}",
            "no_accounts": "{game}",
            "status": "{login} {game} {codes_used} {codes_limit} "
                       "{codes_left}",
            "revoked": "{login}",
            "accounts_list": "{lots}",
            "accounts_list_empty": "—",
            "accounts_list_lot_line": "{game} {free} {logins}",
        }
        return hints.get(tpl_key, "—")

    def _kb_template(tpl_key: str) -> tbtypes.InlineKeyboardMarkup:
        kb = tbtypes.InlineKeyboardMarkup(row_width=2)
        kb.add(
            tbtypes.InlineKeyboardButton(
                "✏️ Изменить", callback_data=f"so:tpl_edit:{tpl_key}"),
            tbtypes.InlineKeyboardButton(
                "♻️ Сбросить", callback_data=f"so:tpl_one_reset:{tpl_key}"),
        )
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ К шаблонам", callback_data="so:templates"))
        return kb

    # ───── История ───────────────────────────────────────────────────────
    def _text_history() -> str:
        hist = list_history()
        last = hist[-15:][::-1]
        if not last:
            return "<b>📜 История</b>\n\nПусто."
        # Иконки/подписи событий, чтобы лог читался быстро
        icons = {
            "issue":             ("📨", "ВЫДАЧА"),
            "guard_code":        ("🛡", "GUARD"),
            "guard_limit_reached": ("⛔", "ЛИМИТ"),
            "revoke":            ("❌", "ОТЗЫВ"),
            "operator_freeze":   ("❄️", "ЗАМОРОЗКА"),
            "operator_replace":  ("🔁", "ЗАМЕНА"),
            "acc_freeze":        ("❄️", "ЗАМОРОЗКА"),
            "acc_unfreeze":      ("🔥", "РАЗМОРОЗКА"),
            "lot_activated":     ("✅", "ЛОТ ВКЛ"),
            "lot_deactivated":   ("⛔", "ЛОТ ВЫКЛ"),
            "lot_save_failed":   ("⚠", "ЛОТ FAIL"),
            "game_added":        ("➕", "ИГРА+"),
            "game_deleted":      ("🗑", "ИГРА-"),
            "lot_added":         ("➕", "ЛОТ+"),
        }
        lines = ["<b>📜 История</b> (последние 15)\n"]
        for e in last:
            ts = _fmt_ts(int(e.get("ts", 0)))
            ev = e.get("event", "?")
            icon, lbl = icons.get(ev, ("•", ev.upper()))
            line = f"<code>{ts}</code> {icon} <b>{_esc(lbl)}</b>"
            if e.get("alias"):
                line += f" • <code>{_esc(e['alias'])}</code>"
            if e.get("lot_id"):
                line += f" • лот <code>{_esc(str(e['lot_id']))}</code>"
            if e.get("game_key"):
                line += f" • игра <code>{_esc(str(e['game_key']))}</code>"
            if e.get("buyer_username"):
                line += f" • {_esc(e['buyer_username'])}"
            if ev == "guard_code":
                line += (f" • {e.get('codes_used', '?')}/"
                         f"{e.get('codes_limit', '?')}")
            if ev in ("lot_activated", "lot_deactivated"):
                line += f" • своб: {e.get('free', '?')}"
            if ev == "lot_save_failed" and e.get("error"):
                err_short = str(e['error'])[:60]
                line += f" • <i>{_esc(err_short)}</i>"
            lines.append(line)
        return "\n".join(lines)

    def _kb_history() -> tbtypes.InlineKeyboardMarkup:
        kb = tbtypes.InlineKeyboardMarkup(row_width=2)
        kb.add(
            tbtypes.InlineKeyboardButton(
                "📥 CSV экспорт", callback_data="so:hist_csv"),
            tbtypes.InlineKeyboardButton(
                "🧹 Очистить", callback_data="so:hist_clear"),
        )
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ Назад", callback_data="so:main"))
        return kb

    # ───── Ивенты (события) ─────────────────────────────────────────────
    def _kb_events() -> tbtypes.InlineKeyboardMarkup:
        events = _load_events()
        unclosed = events.get("unclosed_notify", {})
        kb = tbtypes.InlineKeyboardMarkup(row_width=1)
        enabled = unclosed.get("enabled", True)
        kb.add(tbtypes.InlineKeyboardButton(
            f"{'✅' if enabled else '❌'} Уведомление незакрытых заказов",
            callback_data="so:ev_toggle"))
        kb.add(tbtypes.InlineKeyboardButton(
            "⚠ Уведомить незакрытые заказы",
            callback_data="so:ev_run"))
        kb.add(tbtypes.InlineKeyboardButton(
            "⏰ Интервал (часы)",
            callback_data="so:ev_interval"))
        kb.add(
            tbtypes.InlineKeyboardButton(
                "🔄 Обновить", callback_data="so:events"),
        )
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ Назад", callback_data="so:main"))
        return kb

    def _text_events() -> str:
        events = _load_events()
        unclosed = events.get("unclosed_notify", {})
        enabled = unclosed.get("enabled", True)
        interval = unclosed.get("interval_hours", 24)
        last_run = unclosed.get("last_run", 0)
        next_run = unclosed.get("next_run", 0)
        unclosed_list = _get_unclosed_assignments()
        last_str = _fmt_ts(last_run) if last_run else "никогда"
        next_str = _fmt_ts(next_run) if next_run else "не запланировано"
        return (
            "<b>🚩 Ивенты steam_offline</b>\n\n"
            f"⚠ <b>Уведомление незакрытых заказов:</b>\n"
            f"  Статус: {'✅ вкл' if enabled else '❌ выкл'}\n"
            f"  Интервал: {interval} ч.\n"
            f"  · Последнее: {last_str}\n"
            f"  · Следующее: {next_str}\n\n"
            f"Незакрытых выдач сейчас: <b>{len(unclosed_list)}</b>"
        )

    def _run_unclosed_notify_offline() -> str:
        unclosed = _get_unclosed_assignments()
        events = _load_events()
        ev = events.setdefault("unclosed_notify", {})
        ev["last_run"] = _now()
        interval = ev.get("interval_hours", 24)
        ev["next_run"] = _now() + interval * 3600
        _save_events(events)
        if not unclosed:
            return "Незакрытых выдач нет."
        lines = [f"⚠ <b>Незакрытых выдач: {len(unclosed)}</b>\n"]
        for u in unclosed:
            lines.append(
                f"• <b>{_esc(u['alias'])}</b> — "
                f"{_esc(u['buyer_username'])} "
                f"(выдан {u['issued_at']}, "
                f"{u['age_hours']} ч. назад)")
        return "\n".join(lines)

    # ───── Статистика ─────────────────────────────────────────────────────
    def _calc_stats_offline() -> dict[str, Any]:
        history = list_history()
        accs = list_accounts()
        asgns = list_assignments()
        now = _now()
        day_ago = now - 86400
        week_ago = now - 7 * 86400
        month_ago = now - 30 * 86400

        issues = [h for h in history if h.get("event") == "issue"]
        issues_day = [h for h in issues if h.get("ts", 0) >= day_ago]
        issues_week = [h for h in issues if h.get("ts", 0) >= week_ago]
        issues_month = [h for h in issues if h.get("ts", 0) >= month_ago]

        active = [a for a in asgns.values() if a.get("status") == "active"]
        revoked = [a for a in asgns.values() if a.get("status") == "revoked"]
        total_codes_used = sum(
            int(a.get("codes_used", 0)) for a in asgns.values())
        total_codes_limit = sum(
            int(a.get("codes_limit", 0)) for a in active
            if a.get("codes_limit"))

        games: dict[str, int] = {}
        for h in issues:
            g = h.get("game", "").strip()
            if g and g != "—":
                games[g] = games.get(g, 0) + 1
        top_games = sorted(games.items(), key=lambda x: x[1], reverse=True)[:5]

        return {
            "total_accs": len(accs),
            "frozen": sum(1 for a in accs if a.get("frozen")),
            "total_issues": len(issues),
            "issues_day": len(issues_day),
            "issues_week": len(issues_week),
            "issues_month": len(issues_month),
            "active": len(active),
            "revoked": len(revoked),
            "total_codes_used": total_codes_used,
            "total_codes_limit": total_codes_limit,
            "top_games": top_games,
        }

    def _calc_issues_periods(alias: str | None = None) -> dict[str, int]:
        """Подсчёт выдач за day/week/month/total из history.

        alias=None → глобально; иначе — только по этому аккаунту.
        """
        history = list_history()
        now = _now()
        day_ago = now - 86400
        week_ago = now - 7 * 86400
        month_ago = now - 30 * 86400
        out = {"day": 0, "week": 0, "month": 0, "total": 0}
        for h in history:
            if h.get("event") != "issue":
                continue
            if alias is not None and h.get("alias") != alias:
                continue
            ts = int(h.get("ts", 0) or 0)
            out["total"] += 1
            if ts >= day_ago:
                out["day"] += 1
            if ts >= week_ago:
                out["week"] += 1
            if ts >= month_ago:
                out["month"] += 1
        return out

    def _calc_finance_periods_offline(alias: str | None = None) -> dict[str, float]:
        """v1.9.0: финансы по периодам (day/week/month/total) с учётом
        возвратов.

        alias=None → глобально; иначе — только по этому аккаунту.
        Возвращает {"day", "week", "month", "total", "count_day",
                    "count_week", "count_month", "count_total"}.

        v1.9.0 (порт rental v2.22.4): учитываем 'issue' (положительная
        сумма) И 'refund' (отрицательная) — refund-события автоматически
        вычитаются из дневной/недельной/месячной/общей выручки. Счётчик
        «прод.» считаем только по 'issue' — он отражает количество
        выданных аккаунтов, а не нетто-сделки.
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
        for h in history:
            ev = h.get("event")
            if ev not in ("issue", "refund"):
                continue
            if alias is not None and h.get("alias") != alias:
                continue
            ts = int(h.get("ts", 0) or 0)
            amt = float(h.get("amount", 0) or 0)  # refund.amount уже отрицательный
            is_issue = (ev == "issue")
            out["total"] += amt
            if is_issue:
                out["count_total"] += 1
            if ts >= day_ago:
                out["day"] += amt
                if is_issue:
                    out["count_day"] += 1
            if ts >= week_ago:
                out["week"] += amt
                if is_issue:
                    out["count_week"] += 1
            if ts >= month_ago:
                out["month"] += amt
                if is_issue:
                    out["count_month"] += 1
        return out

    def _format_acc_stats_compact_off(alias: str) -> str:
        """Компактная per-account сводка для steam_offline."""
        acc = find_account(alias)
        if not acc:
            return f"Аккаунт <code>{_esc(alias)}</code> не найден."
        st = acc.get("stats") or {}
        delivered = int(st.get("delivered_count", 0) or 0)
        guard_sent = int(st.get("guard_sent_count", 0) or 0)
        guard_limit_hit = int(st.get("guard_limit_reached", 0) or 0)
        revenue = float(st.get("total_revenue", 0) or 0)
        cost = float(acc.get("cost", 0.0) or 0.0)
        profit = revenue - cost
        roi_str = "—"
        if cost > 0:
            roi_str = f"{(profit / cost) * 100:+.0f}%"
        game = acc.get("game") or "—"

        if acc.get("frozen"):
            status = "❄️ Заморожен"
        elif find_active_assignment_by_alias(alias):
            status = "🔴 Выдан (assignment)"
        else:
            status = "🟢 Свободен"

        ip = _calc_issues_periods(alias)
        # v1.9.0: refund-блок — видно сразу, был ли возврат и сколько.
        refunded_count = int(st.get("refunded_count", 0) or 0)
        refund_amount = 0.0
        if refunded_count:
            for h in list_history():
                if h.get("event") == "refund" and h.get("alias") == alias:
                    refund_amount += float(h.get("amount", 0) or 0)
        # refund_amount уже отрицательный (мы пишем -original в history)
        refund_block = ""
        if refunded_count:
            refund_block = (
                f"💸 Возвратов: <b>{refunded_count}</b>  "
                f"(<b>{refund_amount:+.0f}₽</b>)\n"
                "━━━━━━━━━━━━━━━━━━\n"
            )
        # v1.9.0: финансы по периодам с учётом возвратов
        fp = _calc_finance_periods_offline(alias)
        last_used = int(st.get("last_used_at", 0) or 0)
        last_str = _fmt_ts(last_used) + " UTC" if last_used else "—"

        return (
            f"<b>📊 {_esc(alias)}</b> — {status}\n"
            f"🎮 {_esc(game)}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"💵 <b>Расход:</b> {cost:.0f}₽\n"
            f"💰 <b>Выручка:</b> {revenue:.0f}₽\n"
            f"📈 <b>Прибыль:</b> {profit:+.0f}₽  (ROI {roi_str})\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"📦 Выдач: <b>{delivered}</b>  •  "
            f"🛡 Кодов: <b>{guard_sent}</b>  •  "
            f"🚫 Лимит-хит: <b>{guard_limit_hit}</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"{refund_block}"
            f"📅 <b>Выдачи по периодам</b>\n"
            f"  День:    <b>{ip['day']}</b>\n"
            f"  Неделя:  <b>{ip['week']}</b>\n"
            f"  Месяц:   <b>{ip['month']}</b>\n"
            f"  Всего:   <b>{ip['total']}</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"💰 <b>Выручка по периодам</b> (с учётом возвратов)\n"
            f"  День:    <b>{fp['day']:.0f}₽</b>\n"
            f"  Неделя:  <b>{fp['week']:.0f}₽</b>\n"
            f"  Месяц:   <b>{fp['month']:.0f}₽</b>\n"
            f"  Всего:   <b>{fp['total']:.0f}₽</b>\n"
            f"\n🕒 Последняя активность: {last_str}"
        )

    def _text_stats() -> str:
        s = _calc_stats_offline()
        lines = [
            "<b>📊 Статистика steam_offline</b>\n",
            f"Аккаунтов: <b>{s['total_accs']}</b> "
            f"(заморожено: {s['frozen']})",
            f"Активных выдач: <b>{s['active']}</b>",
            f"Отозвано: <b>{s['revoked']}</b>\n",
            "<b>Выдачи:</b>",
            f"  Всего: {s['total_issues']}",
            f"  За день: {s['issues_day']}",
            f"  За неделю: {s['issues_week']}",
            f"  За месяц: {s['issues_month']}\n",
            f"Кодов использовано: <b>{s['total_codes_used']}</b>",
        ]
        if s["total_codes_limit"]:
            lines.append(f"Лимит кодов (активные): {s['total_codes_limit']}")
        if s["top_games"]:
            lines.append("\n<b>Топ игр:</b>")
            for g, c in s["top_games"]:
                lines.append(f"  • {_esc(g)}: {c}")

        # ── 💰 Финансы (с топ-3) ───────────────────────────────────
        accs_all = list_accounts()
        total_cost = sum(float(a.get("cost", 0) or 0) for a in accs_all)
        total_revenue_all = sum(
            float((a.get("stats") or {}).get("total_revenue", 0) or 0)
            for a in accs_all)
        total_profit = total_revenue_all - total_cost
        n_accs = max(1, len(accs_all))
        avg_profit_per_acc = total_profit / n_accs
        roi_str = "—"
        if total_cost > 0:
            roi_str = f"{(total_profit / total_cost) * 100:+.0f}%"
        lines += [
            "",
            "━━━━━━━━━━━━━━━━━━",
            "<b>💰 Финансы (итог)</b>",
            f"  💵 Расход:    <b>{total_cost:.0f}₽</b>",
            f"  💰 Выручка:   <b>{total_revenue_all:.0f}₽</b>",
            f"  📈 Прибыль:   <b>{total_profit:+.0f}₽</b>  (ROI {roi_str})",
            f"  📊 Ср. прибыль/акк: <b>{avg_profit_per_acc:+.0f}₽</b>",
            "",
            "<b>📅 Выдачи по периодам</b>",
            f"  День:    <b>{s['issues_day']}</b>",
            f"  Неделя:  <b>{s['issues_week']}</b>",
            f"  Месяц:   <b>{s['issues_month']}</b>",
        ]

        # ── 🏆 Топ-3 самых прибыльных ──────────────────────────────
        scored = []
        for a in accs_all:
            alias = a.get("alias", "")
            if not alias:
                continue
            rev = float(
                (a.get("stats") or {}).get("total_revenue", 0) or 0)
            cst = float(a.get("cost", 0) or 0)
            pft = rev - cst
            delivered = int(
                (a.get("stats") or {}).get("delivered_count", 0) or 0)
            if delivered == 0 and rev == 0 and cst == 0:
                continue
            scored.append((pft, alias, rev, cst, delivered))
        scored.sort(reverse=True, key=lambda x: x[0])
        top3 = scored[:3]
        if top3:
            lines += ["", "<b>🏆 Топ-3 прибыли</b>"]
            medals = ["🥇", "🥈", "🥉"]
            for i, (pft, alias, rev, cst, dlv) in enumerate(top3):
                roi_s = "—"
                if cst > 0:
                    roi_s = f"{(pft / cst) * 100:+.0f}%"
                lines.append(
                    f"  {medals[i]} <code>{_esc(alias)}</code>: "
                    f"<b>{pft:+.0f}₽</b> "
                    f"(выручка {rev:.0f}₽, ROI {roi_s}, {dlv} выд.)")
        return "\n".join(lines)

    def _kb_stats() -> tbtypes.InlineKeyboardMarkup:
        kb = tbtypes.InlineKeyboardMarkup()
        kb.add(tbtypes.InlineKeyboardButton(
            "📊 Статистика по аккаунтам",
            callback_data="so:accstatslist:0"))
        kb.add(tbtypes.InlineKeyboardButton(
            "🔄 Обновить", callback_data="so:stats"))
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ Назад", callback_data="so:main"))
        return kb

    # ───── Инструкция ─────────────────────────────────────────────────────
    INSTRUCTIONS_TEXT_OFFLINE = (
        "<b>📝 Инструкция Steam Offline</b>\n\n"
        "<b>1. Добавление аккаунта:</b>\n"
        "  • /soffline → 📋 Аккаунты → ➕ Добавить\n"
        "  • Укажи alias, пришли .maFile, введи пароль\n\n"
        "<b>2. Настройка лота:</b>\n"
        "  • /soffline → 🎯 Лоты → ➕ Добавить\n"
        "  • Укажи ID лота FunPay, пул аккаунтов, лимит кодов\n\n"
        "<b>3. Работа с выдачей:</b>\n"
        "  • Покупатель оплачивает → бот выдаёт логин/пароль навсегда\n"
        "  • Команды в чате: !код, !статус, !помощь\n"
        "  • Лимит кодов ограничивает количество !код запросов\n\n"
        "<b>4. Управление выдачами:</b>\n"
        "  • ♾ Выдачи — просмотр активных выдач\n"
        "  • Отозвать — вернуть аккаунт в пул\n"
        "  • Сбросить счётчик — обнулить использованные коды\n"
        "  • Изменить лимит — новый лимит кодов\n\n"
        "<b>5. Ивенты:</b>\n"
        "  • Автоуведомление о незакрытых заказах\n"
        "  • Настройка интервала в меню Ивенты"
    )

    def _kb_instructions() -> tbtypes.InlineKeyboardMarkup:
        kb = tbtypes.InlineKeyboardMarkup()
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ Назад", callback_data="so:main"))
        return kb

    # ───── Помощь ────────────────────────────────────────────────────────
    def _text_help() -> str:
        return (
            "<b>❓ Steam Offline — помощь</b>\n\n"
            "Этот плагин — компаньон к <b>steam_rental</b>.\n"
            "Он выдаёт Steam-аккаунты <b>навсегда</b> (без срока), а "
            "<code>!код</code> от покупателя работает строго ограниченное "
            "число раз.\n\n"
            "Что нужно настроить:\n"
            "1. Добавь Steam-аккаунты в <b>steam_rental</b> (тот же пул).\n"
            "2. Здесь создай <b>офлайн-лот</b>: укажи FunPay ID лота (или "
            "ключевое слово в названии), пул alias-ов аккаунтов и "
            "лимит кодов (если отличается от настройки по умолчанию).\n"
            "3. Когда покупатель оплачивает офлайн-лот — плагин автоматически "
            "выбирает свободный аккаунт, отправляет логин/пароль и фиксирует "
            "выдачу.\n"
            "4. Команда <code>!код</code> в чате FunPay для офлайн-покупателя "
            "уменьшает счётчик. Когда счётчик дошёл до лимита — больше "
            "коды не выдаются (см. шаблон <code>guard_limit_reached</code>).\n\n"
            "<b>v1.7 — отзыв сессий (опционально):</b>\n"
            "Возвращена кнопка <code>📤 Отозвать сессии</code> в карточке "
            "аккаунта. По умолчанию <b>выключена</b> — клик показывает "
            "«включи в настройках». Включить — ⚙ Настройки → "
            "<code>📤 Отзыв сессий (ручная кнопка)</code>. Когда флаг ON, "
            "кнопка реально отзовёт чужие сессии Steam (login + "
            "revoke_all_other_sessions). Имей в виду: после отзыва все "
            "ранее выданные оффлайн-аккаунты разлогинятся и покупателям "
            "придётся вводить логин/пароль заново.\n\n"
            "<b>v1.5 — мультивыдача (default):</b>\n"
            "Один и тот же аккаунт выдаётся нескольким покупателям "
            "одновременно — у каждого свой счётчик <code>!код</code>. "
            "Picker распределяет покупателей по пулу по принципу "
            "least-loaded. Отключить можно флагом "
            "<code>allow_multi_issue: false</code> в config.json — тогда "
            "вернётся старое поведение «1 alias = 1 активная выдача».\n\n"
            "Управление: меню «♾ Выдачи» — отозвать выдачу, сбросить "
            "счётчик, выдать код вручную, изменить лимит."
        )

    def _kb_help() -> tbtypes.InlineKeyboardMarkup:
        kb = tbtypes.InlineKeyboardMarkup()
        kb.add(tbtypes.InlineKeyboardButton(
            "◀️ Назад", callback_data="so:main"))
        return kb

    # ───── Edit / send menu utils ───────────────────────────────────────
    def _edit_menu(chat_id: int, message_id: int, text: str,
                    kb: tbtypes.InlineKeyboardMarkup) -> None:
        try:
            tg.bot.edit_message_text(text, chat_id=chat_id,
                                     message_id=message_id,
                                     reply_markup=kb, parse_mode="HTML",
                                     disable_web_page_preview=True)
        except Exception as _edit_ex:
            # "message is not modified" — обычная идемпотентная правка
            # (та же кнопка/тот же текст), не шумим traceback'ом.
            _msg = str(_edit_ex).lower()
            if "not modified" in _msg:
                LOGGER.debug(
                    "steam_offline: edit_menu noop (message not modified)")
                return
            LOGGER.warning(
                "steam_offline: edit_menu failed (chat=%s msg=%s "
                "text_len=%s): %s",
                chat_id, message_id, len(text or ""), _edit_ex,
                exc_info=True)

    def _send_menu(chat_id: int) -> "tbtypes.Message":
        return tg.bot.send_message(chat_id, _text_main(),
                                    reply_markup=_kb_main(),
                                    parse_mode="HTML",
                                    disable_web_page_preview=True)

    def _prompt(chat_id: int, msg_id: int | None, text: str) -> None:
        kb = tbtypes.InlineKeyboardMarkup()
        kb.add(tbtypes.InlineKeyboardButton(
            "❌ Отмена", callback_data="so:cancel_input"))
        if msg_id:
            _edit_menu(chat_id, msg_id, text, kb)
        else:
            tg.bot.send_message(chat_id, text, reply_markup=kb,
                                 parse_mode="HTML")

    # ── Интерактивный выбор алиасов (пул аккаунтов лота) ────────────
    _ALIASES_PER_PAGE = 20

    def _alias_picker_text(st: dict) -> str:
        mode = st.get("picker_mode", "editlot")
        sel = st.get("picker_sel") or []
        n_total = len([a for a in list_accounts() if a.get("alias")])
        if mode == "gameacc":
            gkey = st.get("ctx", "")
            g = get_game(gkey) or {}
            gname = g.get("name") or gkey
            title = (
                f"<b>👥 Аккаунты игры</b> <b>{_esc(gname)}</b>\n\n"
                "Отметь аккаунты, которые принадлежат этой игре. "
                "Они автоматически попадут в пул <b>всех лотов</b> этой "
                "игры — добавлять в каждый лот отдельно не нужно.\n\n"
                "<i>Снятая галочка = аккаунт отвязан от игры.</i>"
            )
        else:
            key = st.get("ctx", "")
            title = (
                f"<b>👥 Пул лота</b> <code>{_esc(key)}</code>\n\n"
                "Отметь аккаунты, которые войдут в пул."
            )
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
        # v1.7.1: 🔒 теперь только в single-issue режиме (там аккаунт
        # реально занят, повторно не выдаётся). В multi-issue (дефолт)
        # 🔒 вводил в заблуждение — аккаунт остаётся доступным новым
        # покупателям. Вместо 🔒 показываем лёгкий счётчик «(N)».
        _multi_issue = bool(get_config().get("allow_multi_issue", True))
        for acc in chunk:
            alias = acc.get("alias", "")
            mark = "✅" if alias.lower() in sel_lower else "⬜"
            frozen = " ❄️" if acc.get("frozen") else ""
            n_active = len(find_active_assignments_list_by_alias(alias))
            if n_active > 0:
                busy = (f" ({n_active})" if _multi_issue else " 🔒")
            else:
                busy = ""
            label = f"{mark} {alias}{frozen}{busy}"
            kb.add(tbtypes.InlineKeyboardButton(
                label[:60], callback_data=f"so:apick:{alias}"))
        if pages > 1:
            nav = []
            if page > 0:
                nav.append(tbtypes.InlineKeyboardButton(
                    "◀️", callback_data=f"so:appg:{page - 1}"))
            nav.append(tbtypes.InlineKeyboardButton(
                f"{page + 1}/{pages}", callback_data="so:noop"))
            if page < pages - 1:
                nav.append(tbtypes.InlineKeyboardButton(
                    "▶️", callback_data=f"so:appg:{page + 1}"))
            kb.row(*nav)
        kb.row(
            tbtypes.InlineKeyboardButton(
                "✅ Все", callback_data="so:apall"),
            tbtypes.InlineKeyboardButton(
                "⬜ Очистить", callback_data="so:apclr"),
        )
        kb.row(
            tbtypes.InlineKeyboardButton(
                "✏️ Ввести вручную", callback_data="so:apman"),
        )
        kb.row(
            tbtypes.InlineKeyboardButton(
                "💾 Готово", callback_data="so:apdone"),
            tbtypes.InlineKeyboardButton(
                "❌ Отмена", callback_data="so:cancel_input"),
        )
        return kb

    def _show_alias_picker(chat_id, msg_id, st):
        _edit_menu(chat_id, msg_id,
                   _alias_picker_text(st), _alias_picker_kb(st))

    # ───── Wizard: добавить игру / лот к игре ────────────────────────────
    def _start_add_game(uid, chat_id, msg_id, cb_id):
        """Wizard: создать новую игру с привязанными лотами.
        Шаг 1 — название игры, шаг 2 — список ID лотов через запятую."""
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
                "<b>Шаг 1/2.</b> Отправь <b>название игры</b> "
                "(например, <code>GTA 5</code> или "
                "<code>Counter-Strike 2</code>).\n\n"
                "<i>Название должно встречаться в названиях лотов FunPay "
                "для авто-матчинга по заказу.</i>")

    def _start_add_game_lot(uid, chat_id, msg_id, cb_id, gkey: str):
        """Wizard: добавить main-лот к существующей игре."""
        _pending_state[uid] = {
            "step": "addgame_main_lot",
            "ctx": gkey,
            "chat_id": chat_id, "main_msg_id": msg_id,
        }
        try:
            tg.bot.answer_callback_query(cb_id)
        except Exception:
            pass
        _prompt(chat_id, msg_id,
                f"<b>🎮 {_esc(gkey)}: добавить лот</b>\n\n"
                f"Отправь <b>ID лота</b> FunPay "
                f"(число из URL <code>?id=12345678</code>).")

    # ───── /soffline ─────────────────────────────────────────────────────
    def cmd_soffline(message):
        if not _is_admin(message.from_user.id):
            return
        _send_menu(message.chat.id)

    def cmd_soffline_cancel(message):
        if not _is_admin(message.from_user.id):
            return
        if _pending_state.pop(message.from_user.id, None):
            tg.bot.send_message(message.chat.id, "Отменено.")
        else:
            tg.bot.send_message(message.chat.id, "Нет активного ввода.")

    def cmd_soffline_stats(message):
        """Глобальная статистика offline-плагина (продажи / финансы).

        С аргументом alias — детально по конкретному аккаунту
        (пример: /soffline_stats cs1).
        """
        if not _is_admin(message.from_user.id):
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
                text = _format_acc_stats_compact_off(alias)
            except Exception as e:
                LOGGER.error(
                    "steam_offline: cmd_soffline_stats acc failed",
                    exc_info=True)
                text = f"⚠ Ошибка: <code>{_esc(str(e))}</code>"
            tg.bot.send_message(message.chat.id, text,
                                parse_mode="HTML",
                                disable_web_page_preview=True)
            return
        try:
            text = _text_stats()
            tg.bot.send_message(message.chat.id, text,
                                parse_mode="HTML",
                                disable_web_page_preview=True)
        except Exception as e:
            tg.bot.send_message(message.chat.id,
                f"⚠ Ошибка: <code>{_esc(str(e))}</code>",
                parse_mode="HTML")
            LOGGER.error("steam_offline: cmd_soffline_stats failed",
                         exc_info=True)

    def cmd_soffline_acc_stats(message):
        """Меню/детальная статистика по аккаунту offline.

        /soffline_acc_stats — список с inline-кнопками.
        /soffline_acc_stats <alias> — сразу сводка.
        """
        if not _is_admin(message.from_user.id):
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
                text = _format_acc_stats_compact_off(alias)
            except Exception as e:
                LOGGER.error(
                    "steam_offline: cmd_soffline_acc_stats failed",
                    exc_info=True)
                text = f"⚠ Ошибка: <code>{_esc(str(e))}</code>"
            tg.bot.send_message(message.chat.id, text,
                                parse_mode="HTML",
                                disable_web_page_preview=True)
            return
        accs = sorted(list_accounts(), key=lambda a: a.get("alias", ""))
        if not accs:
            tg.bot.send_message(message.chat.id,
                "Аккаунтов нет. Добавь через /soffline → 📋 Аккаунты.")
            return
        kb = tbtypes.InlineKeyboardMarkup(row_width=2)
        for a in accs[:40]:
            alias = a.get("alias", "")
            if not alias:
                continue
            rev = float(
                (a.get("stats") or {}).get("total_revenue", 0) or 0)
            cst = float(a.get("cost", 0) or 0)
            pft = rev - cst
            label = f"{alias} ({pft:+.0f}₽)"
            kb.add(tbtypes.InlineKeyboardButton(
                label, callback_data=f"so:accstats:{_sid(alias)}"))
        tg.bot.send_message(message.chat.id,
            "<b>📊 Статистика по аккаунтам</b>\n\n"
            "Выбери аккаунт для подробной сводки:",
            reply_markup=kb, parse_mode="HTML")

    # ───── Callback router ───────────────────────────────────────────────
    def on_cb(call):
        uid = call.from_user.id
        if not _is_admin(uid):
            tg.bot.answer_callback_query(call.id, "Нет доступа.")
            return
        data = (call.data or "")
        if not data.startswith("so:"):
            return
        parts = data.split(":", 2)
        action = parts[1] if len(parts) > 1 else ""
        arg = parts[2] if len(parts) > 2 else ""
        chat_id = call.message.chat.id
        msg_id = call.message.message_id

        try:
            if action == "main":
                _edit_menu(chat_id, msg_id, _text_main(), _kb_main())
            elif action == "close":
                try:
                    tg.bot.delete_message(chat_id, msg_id)
                except Exception:
                    pass
            elif action == "cancel_input":
                _pending_state.pop(uid, None)
                _edit_menu(chat_id, msg_id, _text_main(), _kb_main())
            elif action == "help":
                try:
                    tg.bot.answer_callback_query(call.id)
                except Exception:
                    pass
                _edit_menu(chat_id, msg_id, _text_help(), _kb_help())
            elif action == "asgns":
                page = int(arg) if arg.isdigit() else 0
                _edit_menu(chat_id, msg_id, _text_asgns(), _kb_asgns(page))
            elif action == "asgn":
                aid = _resolve_assignment(arg)
                if not aid:
                    tg.bot.answer_callback_query(call.id, "Выдача не найдена.")
                    _edit_menu(chat_id, msg_id, _text_asgns(), _kb_asgns(0))
                    return
                _edit_menu(chat_id, msg_id, _text_asgn(aid), _kb_asgn(aid))
            elif action == "reset":
                aid = _resolve_assignment(arg)
                if aid and reset_assignment_codes(aid):
                    tg.bot.answer_callback_query(
                        call.id, "Счётчик кодов сброшен.")
                    _edit_menu(chat_id, msg_id, _text_asgn(aid), _kb_asgn(aid))
            elif action == "limit":
                aid = _resolve_assignment(arg)
                if not aid:
                    return
                _pending_state[uid] = {
                    "step": "asgn_limit", "ctx": aid,
                    "chat_id": chat_id, "main_msg_id": msg_id,
                }
                _prompt(chat_id, msg_id,
                        "Введи новый <b>лимит кодов</b> (целое число, "
                        "0 = без лимита):")
            elif action == "guard":
                aid = _resolve_assignment(arg)
                if not aid:
                    return
                a = find_assignment(aid)
                if not a:
                    return
                acc = find_account(a["alias"])
                if not acc or not acc.get("shared_secret"):
                    tg.bot.answer_callback_query(
                        call.id, "Нет shared_secret для аккаунта.")
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
                    tg.bot.answer_callback_query(call.id, f"Код: {code}")
                    tg.bot.send_message(
                        chat_id,
                        f"🛡 Код Steam Guard для "
                        f"<code>{_esc(acc['account_name'])}</code>: "
                        f"<code>{_esc(code)}</code>",
                        parse_mode="HTML")
                except Exception as exc:
                    tg.bot.answer_callback_query(
                        call.id, f"Ошибка: {exc}", show_alert=True)
            elif action == "revoke":
                aid = _resolve_assignment(arg)
                if not aid:
                    return
                _pending_state[uid] = {
                    "step": "confirm_revoke", "ctx": aid,
                    "chat_id": chat_id, "main_msg_id": msg_id,
                }
                kb = tbtypes.InlineKeyboardMarkup(row_width=2)
                kb.add(
                    tbtypes.InlineKeyboardButton(
                        "✅ Да, отозвать", callback_data=f"so:revoke_yes:{arg}"),
                    tbtypes.InlineKeyboardButton(
                        "❌ Отмена", callback_data=f"so:asgn:{arg}"),
                )
                a = find_assignment(aid)
                _edit_menu(chat_id, msg_id,
                           f"❗ Отозвать выдачу <code>{_esc(aid)}</code>?\n\n"
                           f"Аккаунт <code>{_esc(a.get('alias', '?'))}</code> "
                           f"вернётся в пул steam_offline как свободный, "
                           f"покупатель потеряет возможность получать "
                           f"коды Steam Guard.\n\n"
                           f"Покупатель будет уведомлён шаблоном "
                           f"<code>revoked</code>.", kb)
            elif action == "revoke_yes":
                aid = _resolve_assignment(arg)
                if not aid:
                    return
                a = find_assignment(aid)
                ok = revoke_assignment(aid)
                if ok and a:
                    try:
                        cardinal.send_message(
                            a.get("chat_id"),
                            _render_template(
                                "revoked",
                                buyer_id=a.get("buyer_id"),
                                login=a.get("account_name", a.get("alias", ""))),
                            chat_name=a.get("buyer_username"),
                            interlocutor_id=a.get("buyer_id"),
                            watermark=False)
                    except Exception:
                        LOGGER.debug("steam_offline: revoke notify failed",
                                     exc_info=True)
                    _log_event("revoke", assignment_id=aid,
                               alias=a.get("alias"),
                               buyer_username=a.get("buyer_username"),
                               buyer_id=a.get("buyer_id"))
                    _log_action_so("rental_end",
                                   f"Отозван аккаунт {a.get('alias')}",
                                   alias=a.get("alias"),
                                   buyer=a.get("buyer_username"),
                                   reason="manual_revoke")
                    try:
                        _update_lot_activation_so(cardinal)
                    except Exception:
                        pass
                tg.bot.answer_callback_query(call.id, "Отозвано.")
                _edit_menu(chat_id, msg_id, _text_asgn(aid), _kb_asgn(aid))
            elif action == "del":
                aid = _resolve_assignment(arg)
                if not aid:
                    return
                if delete_assignment(aid):
                    tg.bot.answer_callback_query(call.id, "Удалено.")
                    _edit_menu(chat_id, msg_id, _text_asgns(), _kb_asgns(0))
                    try:
                        _update_lot_activation_so(cardinal)
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
                if not alias:
                    return
                _edit_menu(chat_id, msg_id,
                           _text_acc(alias, show_pw=True), _kb_acc(alias))
            elif action == "accactive":
                # v1.7.1: «👀 Активные выдачи» — раскрываем список
                # покупателей, который по умолчанию скрыт в карточке.
                alias = _resolve_alias(arg)
                if not alias:
                    return
                _edit_menu(chat_id, msg_id,
                           _text_acc(alias, show_active=True),
                           _kb_acc(alias))
            elif action == "guardacc":
                alias = _resolve_alias(arg)
                if not alias:
                    return
                acc = find_account(alias)
                if not acc or not acc.get("shared_secret"):
                    tg.bot.answer_callback_query(
                        call.id, "Нет shared_secret для аккаунта.",
                        show_alert=True)
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
                    tg.bot.answer_callback_query(call.id, f"Код: {code}")
                    tg.bot.send_message(
                        chat_id,
                        f"🛡 Steam Guard для "
                        f"<code>{_esc(acc['account_name'])}</code>: "
                        f"<code>{_esc(code)}</code>",
                        parse_mode="HTML")
                except Exception as exc:
                    tg.bot.answer_callback_query(
                        call.id, f"Ошибка: {exc}", show_alert=True)
            elif action == "acc_freeze":
                alias = _resolve_alias(arg)
                if not alias:
                    return
                acc = find_account(alias)
                if not acc:
                    return
                acc["frozen"] = not bool(acc.get("frozen"))
                if not acc["frozen"]:
                    acc["login_failures"] = 0
                upsert_account(acc)
                if acc["frozen"]:
                    _log_action_so("acc_freeze",
                                    f"Ручная заморозка {alias} из TG",
                                    alias=alias, mode="manual", user_id=uid)
                    _log_event("acc_freeze", alias=alias, mode="manual",
                               user_id=int(uid))
                else:
                    _log_action_so("acc_unfreeze",
                                    f"Ручная разморозка {alias} из TG",
                                    alias=alias, mode="manual", user_id=uid)
                    _log_event("acc_unfreeze", alias=alias, mode="manual",
                               user_id=int(uid))
                tg.bot.answer_callback_query(
                    call.id,
                    "Заморожен." if acc["frozen"] else "Разморожен.")
                _edit_menu(chat_id, msg_id, _text_acc(alias), _kb_acc(alias))
                try:
                    _update_lot_activation_so(cardinal)
                except Exception:
                    pass
            elif action == "setgame":
                alias = _resolve_alias(arg)
                if not alias:
                    return
                _pending_state[uid] = {
                    "step": "acc_setgame", "ctx": alias,
                    "chat_id": chat_id, "main_msg_id": msg_id,
                }
                # v1.8.0: подсказка с текущей привязкой и списком уже
                # существующих игр — чтобы оператор ввёл точно то же
                # имя/ключ и аккаунт попал в общий пул.
                acc_v = find_account(alias) or {}
                cur_game = (acc_v.get("game") or "").strip()
                cur_key = (acc_v.get("game_key") or "").strip()
                cur_lbl = (f"<code>{_esc(cur_game)}</code>"
                           + (f" (<code>{_esc(cur_key)}</code>)"
                              if cur_key else "")
                           if cur_game else "—")
                games_lst = list_games() or {}
                ex_block = ""
                if games_lst:
                    ex_lines = []
                    for k, g in list(games_lst.items())[:20]:
                        gn = (g.get("name") or k).strip()
                        ex_lines.append(
                            f"  • <b>{_esc(gn)}</b> "
                            f"(<code>{_esc(k)}</code>)")
                    ex_block = ("\n\n📋 Уже есть игры:\n"
                                + "\n".join(ex_lines))
                _prompt(chat_id, msg_id,
                        f"Привязка аккаунта <b>{_esc(alias)}</b> к игре.\n\n"
                        f"Сейчас: {cur_lbl}\n\n"
                        f"💡 Аккаунт привязывается к ИГРЕ (не к каждому "
                        f"лоту отдельно) — тогда любой лот этой игры "
                        f"автоматически сможет его выдать.{ex_block}\n\n"
                        f"Введи название игры. Если такая уже есть в "
                        f"списке — пиши точно её название (или ключ "
                        f"через <code>:</code>, например "
                        f"<code>GTA 5:gta5</code>).\n\n"
                        f"• <code>-</code> — снять привязку")
            elif action == "setcost":
                alias = _resolve_alias(arg)
                if not alias:
                    return
                acc_ = find_account(alias)
                cur = float((acc_ or {}).get("cost", 0.0) or 0.0)
                cur_lbl = (f"{cur:.2f}".rstrip("0").rstrip(".") + "₽"
                           if cur > 0 else "—")
                _pending_state[uid] = {
                    "step": "acc_set_cost", "ctx": alias,
                    "chat_id": chat_id, "main_msg_id": msg_id,
                }
                _prompt(chat_id, msg_id,
                        f"Введи <b>стоимость аккаунта</b> в ₽ для "
                        f"<b>{_esc(alias)}</b>.\n\n"
                        f"Сейчас: <code>{cur_lbl}</code>\n\n"
                        "Можно дробное или целое.\n"
                        "Отправь <code>0</code> или <code>-</code> чтобы обнулить.")
            elif action == "accstats":
                try:
                    tg.bot.answer_callback_query(call.id)
                except Exception:
                    pass
                alias = _resolve_alias(arg)
                if not alias:
                    return
                try:
                    text = _format_acc_stats_compact_off(alias)
                except Exception as e:
                    LOGGER.error(
                        "steam_offline: accstats callback failed",
                        exc_info=True)
                    text = f"⚠ Ошибка: <code>{_esc(str(e))}</code>"
                kb_back = tbtypes.InlineKeyboardMarkup()
                kb_back.add(tbtypes.InlineKeyboardButton(
                    f"◀️ К аккаунту {alias}",
                    callback_data=f"so:acc:{_sid(alias)}"))
                kb_back.add(tbtypes.InlineKeyboardButton(
                    "📊 Все аккаунты",
                    callback_data="so:accstatslist:0"))
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
                        label, callback_data=f"so:accstats:{_sid(alias)}"))
                nav = []
                if page > 0:
                    nav.append(tbtypes.InlineKeyboardButton(
                        "◀ Стр.",
                        callback_data=f"so:accstatslist:{page-1}"))
                if page + 1 < total_pages:
                    nav.append(tbtypes.InlineKeyboardButton(
                        "Стр. ▶",
                        callback_data=f"so:accstatslist:{page+1}"))
                if nav:
                    kb_list.row(*nav)
                kb_list.add(tbtypes.InlineKeyboardButton(
                    "◀️ К статистике", callback_data="so:stats"))
                _edit_menu(chat_id, msg_id,
                    f"<b>📊 Статистика по аккаунтам</b>\n\n"
                    f"Страница {page + 1}/{total_pages}. "
                    f"Аккаунтов: {len(accs)}.\n\n"
                    f"Выбери аккаунт (в скобках — прибыль):",
                    kb_list)
            elif action == "chpwd":
                alias = _resolve_alias(arg)
                if not alias:
                    return
                _pending_state[uid] = {
                    "step": "acc_chpwd_confirm", "ctx": alias,
                    "chat_id": chat_id, "main_msg_id": msg_id,
                }
                kb = tbtypes.InlineKeyboardMarkup(row_width=2)
                kb.add(
                    tbtypes.InlineKeyboardButton(
                        "✅ Да, сменить",
                        callback_data=f"so:chpwd_yes:{arg}"),
                    tbtypes.InlineKeyboardButton(
                        "❌ Отмена",
                        callback_data=f"so:acc:{arg}"),
                )
                _edit_menu(chat_id, msg_id,
                           f"Сменить пароль для "
                           f"<code>{_esc(alias)}</code>?\n\n"
                           f"Будет залогинен в Steam и установлен "
                           f"новый случайный пароль.", kb)
            elif action == "chpwd_yes":
                alias = _resolve_alias(arg)
                if not alias:
                    return
                acc = find_account(alias)
                if not acc:
                    return
                try:
                    new_pw = _sr_gen_password()
                    sess = SteamSession(
                        account_name=acc["account_name"],
                        password=acc["password"],
                        shared_secret=acc["shared_secret"],
                        identity_secret=acc["identity_secret"],
                        steamid=acc.get("steamid"),
                    )
                    sess.login()
                    sess.change_password(new_pw)
                    acc["password"] = new_pw
                    acc["login_failures"] = 0
                    upsert_account(acc)
                    tg.bot.answer_callback_query(
                        call.id, "Пароль обновлён.")
                except Exception as exc:
                    tg.bot.answer_callback_query(
                        call.id, f"Ошибка: {exc}", show_alert=True)
                _edit_menu(chat_id, msg_id,
                           _text_acc(alias), _kb_acc(alias))
            elif action == "revoke_sess":
                alias = _resolve_alias(arg)
                if not alias:
                    return
                acc = find_account(alias)
                if not acc:
                    return
                # Гейт по флагу из настроек. По умолчанию (v1.7+) выкл —
                # чтобы случайно не отозвать сессии у покупателей.
                cfg = get_config()
                if not cfg.get("revoke_sessions_enabled", False):
                    tg.bot.answer_callback_query(
                        call.id,
                        "⚠ Отзыв сессий выключен. Включи в "
                        "⚙ Настройки → «📤 Отзыв сессий».",
                        show_alert=True)
                    return
                tg.bot.answer_callback_query(
                    call.id, "📤 Отзываю сессии...")
                try:
                    sess = SteamSession(
                        account_name=acc["account_name"],
                        password=acc["password"],
                        shared_secret=acc["shared_secret"],
                        identity_secret=acc["identity_secret"],
                        steamid=acc.get("steamid"),
                    )
                    sess.login()
                    ok_rv = sess.revoke_all_other_sessions()
                    LOGGER.info(
                        "steam_offline: revoke_sess %s → %s "
                        "(operator request)", alias, ok_rv)
                    try:
                        tg.bot.send_message(
                            chat_id,
                            f"{'✅' if ok_rv else '⚠'} Revoke "
                            f"<code>{_esc(alias)}</code>: "
                            f"<code>{ok_rv}</code>",
                            parse_mode="HTML")
                    except Exception:
                        LOGGER.debug(
                            "steam_offline: revoke_sess notify failed",
                            exc_info=True)
                except Exception as exc:
                    LOGGER.warning(
                        "steam_offline: revoke_sess for %s failed: %s",
                        alias, exc)
                    try:
                        tg.bot.send_message(
                            chat_id,
                            f"❌ Revoke <code>{_esc(alias)}</code>: "
                            f"<code>{_esc(str(exc))}</code>",
                            parse_mode="HTML")
                    except Exception:
                        pass
                _edit_menu(chat_id, msg_id,
                           _text_acc(alias), _kb_acc(alias))
            elif action == "check":
                alias = _resolve_alias(arg)
                if not alias:
                    return
                acc = find_account(alias)
                if not acc:
                    return
                try:
                    sess = SteamSession(
                        account_name=acc["account_name"],
                        password=acc["password"],
                        shared_secret=acc["shared_secret"],
                        identity_secret=acc["identity_secret"],
                        steamid=acc.get("steamid"),
                    )
                    sess.login()
                    acc["login_failures"] = 0
                    upsert_account(acc)
                    tg.bot.answer_callback_query(
                        call.id, "Логин ОК.")
                except Exception as exc:
                    acc["login_failures"] = int(
                        acc.get("login_failures", 0)) + 1
                    if acc["login_failures"] >= 3:
                        acc["frozen"] = True
                    upsert_account(acc)
                    tg.bot.answer_callback_query(
                        call.id, f"Ошибка: {exc}", show_alert=True)
                _edit_menu(chat_id, msg_id,
                           _text_acc(alias), _kb_acc(alias))
            elif action == "acc_del":
                alias = _resolve_alias(arg)
                if not alias:
                    return
                asgn = find_active_assignment_by_alias(alias)
                if asgn:
                    tg.bot.answer_callback_query(
                        call.id,
                        "У аккаунта есть активная выдача — отзови её.",
                        show_alert=True)
                    return
                kb = tbtypes.InlineKeyboardMarkup(row_width=2)
                kb.add(
                    tbtypes.InlineKeyboardButton(
                        "✅ Да, удалить",
                        callback_data=f"so:acc_del_yes:{arg}"),
                    tbtypes.InlineKeyboardButton(
                        "❌ Отмена", callback_data=f"so:acc:{arg}"),
                )
                _edit_menu(chat_id, msg_id,
                           f"Удалить аккаунт "
                           f"<code>{_esc(alias)}</code>?\n\n"
                           f"<b>Безвозвратно.</b> Аккаунт будет удалён "
                           f"только из пула steam_offline.", kb)
            elif action == "acc_del_yes":
                alias = _resolve_alias(arg)
                if alias and delete_account(alias):
                    tg.bot.answer_callback_query(call.id, "Удалено.")
                    _edit_menu(chat_id, msg_id, _text_accs(), _kb_accs(0))
            elif action == "acc_add":
                _pending_state[uid] = {
                    "step": "acc_add_alias", "ctx": None,
                    "chat_id": chat_id, "main_msg_id": msg_id,
                    "draft": {},
                }
                _prompt(chat_id, msg_id,
                        "Шаг 1/4. Введи <b>alias</b> (короткий ярлык, "
                        "например <code>cs1</code>, <code>dota_premium</code>):")
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
                    _start_add_game_lot(uid, chat_id, msg_id, call.id, gkey)
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
                            "✅ Да, удалить",
                            callback_data=f"so:gdel_yes:{arg}"),
                        tbtypes.InlineKeyboardButton(
                            "❌ Отмена", callback_data=f"so:game:{arg}"),
                    )
                    try:
                        _edit_menu(chat_id, msg_id,
                                   "Удалить игру и отвязать её лоты?\n\n"
                                   "Лоты <b>останутся</b> в lots.json, "
                                   "но привязка (game_key) сбросится.",
                                   kb)
                    except Exception:
                        pass
            elif action == "gdel_yes":
                gkey = _resolve_game(arg)
                if gkey and delete_game(gkey):
                    tg.bot.answer_callback_query(call.id, "Удалено.")
                    _log_event("game_deleted", game_key=gkey)
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
                        counters = _update_lot_activation_so(
                            cardinal, force=True, verbose=False)
                        _log_action_so(
                            "reactivation",
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
                    except Exception:
                        LOGGER.warning(
                            "steam_offline: game_react failed for %s",
                            gkey, exc_info=True)
                    _edit_menu(chat_id, msg_id,
                               _text_game(gkey), _kb_game(gkey))
            elif action == "reacttlots":
                try:
                    tg.bot.answer_callback_query(
                        call.id, "Переактивирую лоты на FunPay...")
                except Exception:
                    pass
                try:
                    counters = _update_lot_activation_so(
                        cardinal, force=True, verbose=True)
                except Exception as e:
                    LOGGER.error(
                        "steam_offline: reacttlots failed", exc_info=True)
                    counters = None
                    err_text = str(e)[:200]
                else:
                    err_text = ""
                if counters is not None:
                    _log_action_so(
                        "reactivation",
                        "Ручная переактивация лотов из TG",
                        activated=counters.get("activated", 0),
                        deactivated=counters.get("deactivated", 0),
                        skipped=counters.get("skipped", 0),
                        failed=counters.get("failed", 0),
                        user_id=uid)
                if counters is None:
                    text = (f"<b>🔁 Переактивация лотов</b>\n\n"
                            f"❌ Ошибка: <code>{_esc(err_text)}</code>")
                else:
                    text = (
                        f"<b>🔁 Переактивация лотов</b>\n\n"
                        f"📊 <b>Результат</b>\n"
                        f"  ✅ Активировано: <b>{counters['activated']}</b>\n"
                        f"  ⛔ Деактивировано: "
                        f"<b>{counters['deactivated']}</b>\n"
                        f"  ➖ Пропущено: <b>{counters['skipped']}</b>\n"
                        f"  ⚠ Ошибок: <b>{counters['failed']}</b>")
                    if counters.get("stopped_reason"):
                        text += (f"\n\n⚠ Остановлено: "
                                 f"<code>"
                                 f"{_esc(counters['stopped_reason'])}"
                                 f"</code>")
                try:
                    tg.bot.send_message(chat_id, text, parse_mode="HTML")
                except Exception:
                    pass
                _edit_menu(chat_id, msg_id, _text_games(), _kb_games())
            elif action == "gameacc":
                # v1.8.0: picker «Аккаунты игры». Открываем из карточки
                # игры. Сохраняем выбранные → проставляем acc.game_key
                # для всех выбранных, у не-выбранных, чьим game_key был
                # этот gkey, очищаем привязку.
                gkey = _resolve_game(arg)
                if not gkey:
                    tg.bot.answer_callback_query(call.id, "Не найдено.")
                    return
                # Текущая привязка: aliases где acc.game_key == gkey
                gkey_lc = str(gkey).lower()
                cur_sel: list[str] = []
                for a in list_accounts():
                    if (a.get("game_key") or "").strip().lower() == gkey_lc:
                        al = a.get("alias", "")
                        if al:
                            cur_sel.append(al)
                _pending_state[uid] = {
                    "step": "gameacc_pool", "ctx": gkey,
                    "chat_id": chat_id, "main_msg_id": msg_id,
                    "picker_mode": "gameacc",
                    "picker_sel": cur_sel,
                    "picker_page": 0,
                }
                try:
                    tg.bot.answer_callback_query(call.id)
                except Exception:
                    pass
                _show_alias_picker(chat_id, msg_id, _pending_state[uid])
            elif action == "gdenuvo":
                # v1.8.0: переключатель Denuvo на уровне игры. Per-lot
                # значение (lot.denuvo) — независимое — может либо
                # наследовать, либо явно переопределить.
                gkey = _resolve_game(arg)
                if not gkey:
                    tg.bot.answer_callback_query(call.id, "Не найдено.")
                    return
                with _lock:
                    games = list_games()
                    g = games.get(str(gkey)) or {}
                    new_val = not bool(g.get("denuvo"))
                    g["denuvo"] = new_val
                    games[str(gkey)] = g
                    save_games(games)
                tg.bot.answer_callback_query(
                    call.id,
                    f"💎 Denuvo для игры: {'✅ ВКЛ' if new_val else '❌ ВЫКЛ'}")
                _edit_menu(chat_id, msg_id, _text_game(gkey),
                           _kb_game(gkey))
            elif action == "lot":
                key = _resolve_lot(arg)
                if not key:
                    tg.bot.answer_callback_query(call.id, "Лот не найден.")
                    _edit_menu(chat_id, msg_id, _text_lots(), _kb_lots())
                    return
                _edit_menu(chat_id, msg_id, _text_lot(key), _kb_lot(key))
            elif action == "lot_add":
                _pending_state[uid] = {
                    "step": "lot_add_id", "ctx": None,
                    "chat_id": chat_id, "main_msg_id": msg_id,
                }
                _prompt(chat_id, msg_id,
                        "Введи <b>ID лота FunPay</b> (числовой) или "
                        "ключевое слово, которое содержится в названии лота:")
            elif action == "lot_pool":
                key = _resolve_lot(arg)
                if not key:
                    return
                cur_pool = list(list_lots().get(key, {}).get("aliases", []))
                _pending_state[uid] = {
                    "step": "lot_pool", "ctx": key,
                    "chat_id": chat_id, "main_msg_id": msg_id,
                    "picker_sel": cur_pool,
                    "picker_page": 0,
                }
                tg.bot.answer_callback_query(call.id)
                _show_alias_picker(chat_id, msg_id,
                                   _pending_state[uid])
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
                aliases_sel = list(st_.get("picker_sel") or [])
                key = st_.get("ctx", "")
                mode = st_.get("picker_mode", "editlot")
                tg.bot.answer_callback_query(
                    call.id, f"Готово: {len(aliases_sel)} акк.")
                if mode == "gameacc":
                    # v1.8.0: проставляем acc.game_key для выбранных
                    # и снимаем с не-выбранных, у кого был этот gkey.
                    gkey = key
                    sel_set = {a.lower() for a in aliases_sel}
                    g = get_game(gkey) or {}
                    gname = g.get("name") or gkey
                    n_set = 0
                    n_unset = 0
                    with _lock:
                        accs = list_accounts()
                        for a in accs:
                            al = (a.get("alias") or "")
                            if not al:
                                continue
                            cur_gk = (a.get("game_key") or "").strip().lower()
                            if al.lower() in sel_set:
                                if cur_gk != str(gkey).lower():
                                    a["game_key"] = str(gkey)
                                    a["game"] = gname
                                    n_set += 1
                            elif cur_gk == str(gkey).lower():
                                a["game_key"] = ""
                                # Имя оставляем — может пригодиться для
                                # шаблонов/статистики.
                                n_unset += 1
                        save_accounts(accs)
                    _pending_state.pop(uid, None)
                    tg.bot.send_message(chat_id,
                        f"✅ Игра <b>{_esc(gname)}</b> "
                        f"(<code>{_esc(gkey)}</code>): "
                        f"привязано <b>{n_set}</b>, "
                        f"отвязано <b>{n_unset}</b>.\n"
                        f"<i>Аккаунты автоматически попадают в пул "
                        f"всех лотов этой игры.</i>",
                        parse_mode="HTML")
                    if msg_id:
                        _edit_menu(chat_id, msg_id,
                                   _text_game(gkey), _kb_game(gkey))
                    return
                # Default: editlot — сохраняем aliases на лоте.
                with _lock:
                    lots = list_lots()
                    if key not in lots:
                        tg.bot.send_message(chat_id, "Лот не найден.")
                        _pending_state.pop(uid, None)
                        return
                    lots[key]["aliases"] = aliases_sel
                    save_lots(lots)
                _pending_state.pop(uid, None)
                tg.bot.send_message(chat_id,
                    f"✅ {_esc(key)}: пул → "
                    f"<code>{_esc(', '.join(aliases_sel) or '—')}</code>",
                    parse_mode="HTML")
                if msg_id:
                    _edit_menu(chat_id, msg_id,
                               _text_lot(key), _kb_lot(key))
            elif action == "lot_game":
                key = _resolve_lot(arg)
                if not key:
                    return
                _pending_state[uid] = {
                    "step": "lot_game", "ctx": key,
                    "chat_id": chat_id, "main_msg_id": msg_id,
                }
                _prompt(chat_id, msg_id,
                        "Введи название игры (для шаблонов и статистики) "
                        "или прочерк <code>-</code>, чтобы убрать:")
            elif action == "lot_limit":
                key = _resolve_lot(arg)
                if not key:
                    return
                _pending_state[uid] = {
                    "step": "lot_limit", "ctx": key,
                    "chat_id": chat_id, "main_msg_id": msg_id,
                }
                _prompt(chat_id, msg_id,
                        "Введи <b>лимит кодов</b> для этого лота (целое, "
                        "0 = без лимита, прочерк <code>-</code> = "
                        "использовать значение по умолчанию):")
            elif action == "lot_del":
                key = _resolve_lot(arg)
                if key and delete_lot(key):
                    tg.bot.answer_callback_query(call.id, "Удалено.")
                    _edit_menu(chat_id, msg_id, _text_lots(), _kb_lots())
            elif action == "lot_denuvo":
                key = _resolve_lot(arg)
                if not key:
                    tg.bot.answer_callback_query(call.id, "Лот не найден.")
                    return
                # v1.8.0: tri-state cycler — None → True → False → None.
                # None = наследовать от game.denuvo (новый дефолт).
                with _lock:
                    lots = list_lots()
                    if key in lots:
                        cur = lots[key].get("denuvo")
                        if cur is None:
                            new_val = True
                            label = "💎 Denuvo: ✅ принудительно ВКЛ"
                        elif cur is True:
                            new_val = False
                            label = ("💎 Denuvo: ❌ принудительно ВЫКЛ "
                                     "(даже если игра Denuvo)")
                        else:  # False
                            new_val = None
                            label = "💎 Denuvo: 🔄 наследовать от игры"
                        if new_val is None:
                            lots[key].pop("denuvo", None)
                        else:
                            lots[key]["denuvo"] = new_val
                        save_lots(lots)
                tg.bot.answer_callback_query(call.id, label)
                _edit_menu(chat_id, msg_id, _text_lot(key), _kb_lot(key))
            elif action == "lot_denuvo_lim":
                key = _resolve_lot(arg)
                if not key:
                    return
                _pending_state[uid] = {
                    "step": "lot_denuvo_lim", "ctx": key,
                    "chat_id": chat_id, "main_msg_id": msg_id,
                }
                _prompt(chat_id, msg_id,
                        f"Введи <b>Denuvo daily-лимит</b> для лота "
                        f"<code>{_esc(key)}</code>. Целое число (1..50). "
                        f"Прочерк <code>-</code> = "
                        f"использовать значение по умолчанию.")
            elif action == "tools":
                _edit_menu(chat_id, msg_id, _text_tools(), _kb_tools())
            elif action == "denuvo":
                _edit_menu(chat_id, msg_id, _text_denuvo(), _kb_denuvo())
            elif action == "denuvo_reset_all":
                cnt = 0
                with _lock:
                    accs = list_accounts()
                    for a in accs:
                        if a.get("denuvo_count") or a.get("denuvo_day"):
                            a["denuvo_count"] = 0
                            a["denuvo_day"] = _denuvo_today_utc()
                            cnt += 1
                    if cnt:
                        save_accounts(accs)
                tg.bot.answer_callback_query(
                    call.id, f"♻️ Сброшено: {cnt} аккаунтов")
                _edit_menu(chat_id, msg_id, _text_denuvo(), _kb_denuvo())
            elif action == "settings":
                _edit_menu(chat_id, msg_id, _text_settings(), _kb_settings())
            elif action == "toggle":
                key = arg
                if key in _SETTINGS_TOGGLES:
                    cfg = get_config()
                    cfg[key] = not cfg.get(key, False)
                    save_config(cfg)
                    _edit_menu(chat_id, msg_id, _text_settings(),
                               _kb_settings())
            elif action == "set":
                key = arg
                if key not in ("default_guard_limit",
                               "denuvo_default_limit",
                               "guardik_command"):
                    return
                _pending_state[uid] = {
                    "step": "edit_setting", "ctx": key,
                    "chat_id": chat_id, "main_msg_id": msg_id,
                }
                cfg = get_config()
                if key == "guardik_command":
                    _prompt(chat_id, msg_id,
                            "✏️ <b>Команда для запроса Steam Guard</b>\n\n"
                            f"Текущая: <code>"
                            f"{_esc(str(cfg.get(key, '!код')))}</code>\n\n"
                            "Введи новую команду одной строкой "
                            "(без пробелов, ≤ 32 символа). Обычно "
                            "начинается с <code>!</code>, <code>/</code> "
                            "или <code>.</code>, например: "
                            "<code>!код</code>, <code>!guard</code>, "
                            "<code>/2fa</code>, <code>!код2fa</code>.\n\n"
                            "⚠️ После смены не забудь поправить шаблон "
                            "<code>issue</code> в "
                            "<b>⚙ Настройки → 🧩 Шаблоны</b> — там команда "
                            "вшита в текст уведомления о выдаче.")
                else:
                    _prompt(chat_id, msg_id,
                            f"Текущее значение "
                            f"<code>{_esc(key)}</code> = "
                            f"<code>{_esc(str(cfg.get(key)))}</code>\n\n"
                            f"Введи новое значение:")
            elif action == "templates":
                _edit_menu(chat_id, msg_id,
                           _text_templates(uid), _kb_templates(uid))
            elif action == "tpl_lang":
                # v1.10.0: переключатель языка в меню шаблонов
                if arg in ("ru", "en"):
                    _set_admin_lang(uid, arg)
                _edit_menu(chat_id, msg_id,
                           _text_templates(uid), _kb_templates(uid))
                tg.bot.answer_callback_query(call.id)
            elif action == "tpl":
                _edit_menu(chat_id, msg_id, _text_template(arg, uid),
                           _kb_template(arg))
            elif action == "tpl_edit":
                lang = _get_admin_lang(uid)
                _pending_state[uid] = {
                    "step": "tpl_edit", "ctx": arg,
                    "tpl_lang": lang,
                    "chat_id": chat_id, "main_msg_id": msg_id,
                }
                flag = "🇷🇺 RU" if lang == "ru" else "🇬🇧 EN"
                _prompt(chat_id, msg_id,
                        f"Введи новый текст шаблона "
                        f"<code>{_esc(arg)}</code> ({flag}).\n\n"
                        f"Плейсхолдеры: {_placeholders_hint(arg)}")
            elif action == "tpl_one_reset":
                # v1.10.0: сбрасываем шаблон к дефолту в выбранном языке
                lang = _get_admin_lang(uid)
                defaults = (_DEFAULT_TEMPLATES if lang == "ru"
                            else _DEFAULT_TEMPLATES_EN)
                data = dict(_load_templates_file(lang))
                data[arg] = defaults.get(arg, "")
                _save_templates_file(lang, data)
                tg.bot.answer_callback_query(call.id, "Сброшено.")
                _edit_menu(chat_id, msg_id, _text_template(arg, uid),
                           _kb_template(arg))
            elif action == "tpl_reset":
                # v1.10.0: сброс всех шаблонов выбранного языка к дефолтам
                lang = _get_admin_lang(uid)
                defaults = (_DEFAULT_TEMPLATES if lang == "ru"
                            else _DEFAULT_TEMPLATES_EN)
                _save_templates_file(lang, dict(defaults))
                tg.bot.answer_callback_query(
                    call.id,
                    f"Все шаблоны сброшены ({lang.upper()}).")
                _edit_menu(chat_id, msg_id, _text_templates(uid),
                           _kb_templates(uid))
            elif action == "history":
                _edit_menu(chat_id, msg_id, _text_history(), _kb_history())
            elif action == "hist_csv":
                try:
                    blob = export_history_csv()
                    tg.bot.send_document(
                        chat_id,
                        ("steam_offline_history.csv", blob),
                        caption="История офлайн-выдач (CSV)")
                except Exception as exc:
                    tg.bot.answer_callback_query(
                        call.id, f"Ошибка: {exc}", show_alert=True)
            elif action == "hist_clear":
                _save_json(HISTORY_FILE, [])
                tg.bot.answer_callback_query(call.id, "История очищена.")
                _edit_menu(chat_id, msg_id, _text_history(), _kb_history())
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
                result = _run_unclosed_notify_offline()
                tg.bot.send_message(chat_id, result, parse_mode="HTML")
                _edit_menu(chat_id, msg_id, _text_events(), _kb_events())
            elif action == "ev_interval":
                _pending_state[uid] = {
                    "step": "ev_interval",
                    "chat_id": chat_id, "main_msg_id": msg_id,
                }
                kb = tbtypes.InlineKeyboardMarkup()
                kb.add(tbtypes.InlineKeyboardButton(
                    "❌ Отмена", callback_data="so:cancel_input"))
                events = _load_events()
                cur = events.get("unclosed_notify", {}).get(
                    "interval_hours", 24)
                tg.bot.edit_message_text(
                    f"Текущий интервал: <b>{cur}</b> ч.\n\n"
                    f"Введи новый интервал (часы, целое число):",
                    chat_id=chat_id, message_id=msg_id,
                    reply_markup=kb, parse_mode="HTML")
            elif action == "stats":
                _edit_menu(chat_id, msg_id, _text_stats(), _kb_stats())
            elif action == "instructions":
                try:
                    tg.bot.answer_callback_query(call.id)
                except Exception:
                    pass
                _edit_menu(chat_id, msg_id, INSTRUCTIONS_TEXT_OFFLINE,
                           _kb_instructions())

            # ── v5: Operator buttons из inline-уведомления ───────
            elif action == "frz":
                alias = _resolve_alias(arg)
                if not alias:
                    tg.bot.answer_callback_query(
                        call.id, "Аккаунт не найден.")
                    return
                with _lock:
                    acc = find_account(alias)
                    if not acc:
                        tg.bot.answer_callback_query(
                            call.id, "Аккаунт не найден.")
                        return
                    acc["frozen"] = True
                    upsert_account(acc)
                _metric_inc("operator_freeze_total")
                _log_event("operator_freeze", alias=alias)
                _log_action_so("acc_freeze",
                                f"Заморозка {alias} оператором",
                                alias=alias, mode="operator", user_id=uid)
                try:
                    _update_lot_activation_so(cardinal)
                except Exception:
                    pass
                tg.bot.answer_callback_query(
                    call.id, f"🛑 {alias} заморожен.")
            elif action == "rep":
                # arg = "{sid}:{assignment_id[:24]}"
                if ":" not in arg:
                    tg.bot.answer_callback_query(
                        call.id, "Bad arg.")
                    return
                sid_v, _, asg_short = arg.partition(":")
                alias = _resolve_alias(sid_v)
                if not alias:
                    tg.bot.answer_callback_query(
                        call.id, "Аккаунт не найден.")
                    return
                # Найдём активную выдачу по аккаунту
                asgn = find_active_assignment_by_alias(alias)
                if not asgn:
                    tg.bot.answer_callback_query(
                        call.id, "Активная выдача не найдена.")
                    return
                # Собираем pool union тех же лотов
                pool_union: list[str] = []
                for _lot in list_lots().values():
                    a_list = _lot.get("aliases") or []
                    if alias.lower() in [x.lower() for x in a_list]:
                        for _a in a_list:
                            if _a.lower() != alias.lower() \
                                    and _a not in pool_union:
                                pool_union.append(_a)
                new_alias = _pick_free_alias_for_offline(pool_union)
                if not new_alias:
                    tg.bot.answer_callback_query(
                        call.id, "Нет свободных аккаунтов.",
                        show_alert=True)
                    return
                # Отзываем старую выдачу
                revoke_assignment(asgn["id"])
                with _lock:
                    acc_old = find_account(alias)
                    if acc_old:
                        acc_old["frozen"] = True
                        upsert_account(acc_old)
                # Выдаём новый аккаунт с тем же лимитом
                new_asgn = deliver_account_offline(
                    cardinal, alias=new_alias,
                    buyer_id=int(asgn.get("buyer_id", 0)),
                    buyer_username=str(asgn.get("buyer_username", "")),
                    chat_id=asgn.get("chat_id", 0),
                    order_id=str(asgn.get("order_id", "")),
                    guard_limit=int(asgn.get("codes_limit", 0)))
                _metric_inc("operator_replace_total")
                _log_event("operator_replace",
                           alias_old=alias,
                           alias_new=new_alias if new_asgn else None)
                tg.bot.answer_callback_query(
                    call.id,
                    f"🔁 {alias} → {new_alias}" if new_asgn else
                    f"Не удалось выдать {new_alias}.")
            elif action == "stat":
                alias = _resolve_alias(arg)
                if not alias:
                    tg.bot.answer_callback_query(
                        call.id, "Не найден.")
                    return
                acc = find_account(alias)
                stats = (acc.get("stats") if acc else None) or {}
                txt = _text_acc_stats(alias, stats)
                tg.bot.answer_callback_query(call.id)
                try:
                    tg.bot.send_message(
                        chat_id, txt, parse_mode="HTML",
                        disable_web_page_preview=True)
                except Exception:
                    LOGGER.debug(
                        "steam_offline: stat reply failed", exc_info=True)
            elif action == "metset":
                _edit_menu(chat_id, msg_id, _text_metset(), _kb_metset())
            elif action == "dsumset":
                _edit_menu(chat_id, msg_id, _text_dsumset(), _kb_dsumset())
            elif action == "blist":
                # v1.9.0: blacklist меню
                _edit_menu(chat_id, msg_id,
                           _text_blacklist(), _kb_blacklist())
            elif action == "blrm":
                # v1.9.0: удалить запись из blacklist (arg = sid метки)
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
                _edit_menu(chat_id, msg_id,
                           _text_blacklist(), _kb_blacklist())
            elif action == "bladd":
                # v1.9.0: добавить вручную через текстовый prompt
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
                    accs2 = list_accounts()
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
                                f"Steam Guard работает корректно!",
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
                        sess = SteamSession(
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
        except Exception:
            LOGGER.error("steam_offline: on_cb crashed", exc_info=True)
            try:
                tg.bot.answer_callback_query(call.id, "Ошибка.",
                                              show_alert=False)
            except Exception:
                pass

    def _is_pending_text(m) -> bool:
        st = _pending_state.get(m.from_user.id)
        if not st:
            return False
        return st.get("step") not in ("confirm_revoke",)

    def _handle_pending_text(message):
        uid = message.from_user.id
        st = _pending_state.get(uid)
        if not st:
            return
        text = (message.text or "").strip()
        chat_id = st["chat_id"]
        msg_id = st.get("main_msg_id")
        step = st.get("step")

        try:
            if step == "lot_add_id":
                if not text:
                    tg.bot.send_message(chat_id, "Пусто, попробуй ещё раз.")
                    return
                key = text
                set_lot(key, aliases=[], game="", guard_limit=None)
                _pending_state.pop(uid, None)
                tg.bot.send_message(chat_id,
                    f"✅ Лот <code>{_esc(key)}</code> добавлен. Теперь укажи "
                    f"пул аккаунтов в карточке лота.",
                    parse_mode="HTML")
                if msg_id:
                    _edit_menu(chat_id, msg_id, _text_lot(key), _kb_lot(key))
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
                    f"<b>Шаг 2/2.</b> Отправь ID <b>лотов</b> "
                    f"через запятую (числа из URL "
                    f"<code>?id=...</code>).\n\n"
                    f"Можно пусто — отправь <code>-</code>.\n\n"
                    f"<i>Лоты будут привязаны к игре. Аккаунты привяжешь "
                    f"к лотам отдельно (🎯 Лоты → выбери лот → 👥 Пул).</i>",
                    parse_mode="HTML")
                return
            elif step == "addgame_main_lots":
                name = _pending_state[uid].get("ctx", "")
                main_ids: list[str] = []
                txt = text.strip()
                if txt and txt != "-":
                    for raw in txt.replace(";", ",").split(","):
                        raw = raw.strip()
                        if raw.isdigit():
                            main_ids.append(raw)
                        elif raw:
                            tg.bot.send_message(chat_id,
                                f"⚠ Игнорирую <code>{_esc(raw)}</code> — "
                                f"не похоже на числовой ID.",
                                parse_mode="HTML")
                # Создаём игру
                gkey = set_game(_slugify_game(name), name)
                # Создаём/привязываем лоты
                for lid in main_ids:
                    set_lot(lid, aliases=[], game=name,
                            guard_limit=None,
                            game_key=gkey, kind="main")
                _pending_state.pop(uid, None)
                _log_event("game_added", game_key=gkey, name=name,
                           lots=len(main_ids))
                tg.bot.send_message(chat_id,
                    f"✅ Игра создана: <b>{_esc(name)}</b> "
                    f"(<code>{_esc(gkey)}</code>)\n"
                    f"  • Привязано лотов: <b>{len(main_ids)}</b>\n\n"
                    f"Дальше: открой <b>🎯 Лоты</b> → выбери лот → "
                    f"<b>👥 Пул</b> и привяжи аккаунты.",
                    parse_mode="HTML")
                if msg_id:
                    _edit_menu(chat_id, msg_id,
                               _text_game(gkey), _kb_game(gkey))
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
                    tg.bot.send_message(chat_id,
                        "Игра не найдена. /soffline → 🎮 Игры")
                    return
                set_lot(lid, aliases=[], game=g.get("name", ""),
                        guard_limit=None, game_key=gkey, kind="main")
                _pending_state.pop(uid, None)
                _log_event("lot_added", game_key=gkey, lot_id=lid)
                tg.bot.send_message(chat_id,
                    f"✅ Лот <code>{_esc(lid)}</code> привязан к игре "
                    f"<b>{_esc(g.get('name', gkey))}</b>.\n\n"
                    f"Не забудь добавить аккаунты в пул лота "
                    f"(🎯 Лоты → <code>{_esc(lid)}</code> → 👥 Пул).",
                    parse_mode="HTML")
                if msg_id:
                    _edit_menu(chat_id, msg_id,
                               _text_game(gkey), _kb_game(gkey))
                return
            elif step == "lot_pool":
                key = st["ctx"]
                aliases = [a.strip() for a in text.replace(";", ",").split(",")
                           if a.strip()]
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
                    _edit_menu(chat_id, msg_id, _text_lot(key), _kb_lot(key))
            elif step == "lot_game":
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
            elif step == "lot_limit":
                key = st["ctx"]
                if text == "-":
                    new_val: int | None = None
                else:
                    try:
                        new_val = max(0, int(text))
                    except ValueError:
                        tg.bot.send_message(chat_id,
                            "Нужно число или прочерк <code>-</code>.",
                            parse_mode="HTML")
                        return
                with _lock:
                    lots = list_lots()
                    if key not in lots:
                        tg.bot.send_message(chat_id, "Лот не найден.")
                        _pending_state.pop(uid, None)
                        return
                    lots[key]["guard_limit"] = new_val
                    save_lots(lots)
                _pending_state.pop(uid, None)
                tg.bot.send_message(chat_id,
                    f"✅ {_esc(key)}: лимит кодов → "
                    f"<b>{new_val if new_val is not None else 'из настроек'}</b>",
                    parse_mode="HTML")
                if msg_id:
                    _edit_menu(chat_id, msg_id, _text_lot(key), _kb_lot(key))
            elif step == "lot_denuvo_lim":
                key = st["ctx"]
                if text == "-":
                    new_val_d: int | None = None
                else:
                    try:
                        new_val_d = int(text)
                        if new_val_d < 1 or new_val_d > 50:
                            raise ValueError("range")
                    except ValueError:
                        tg.bot.send_message(chat_id,
                            "Нужно число 1..50 или прочерк "
                            "<code>-</code>.",
                            parse_mode="HTML")
                        return
                with _lock:
                    lots = list_lots()
                    if key not in lots:
                        tg.bot.send_message(chat_id, "Лот не найден.")
                        _pending_state.pop(uid, None)
                        return
                    lots[key]["denuvo_limit"] = new_val_d
                    save_lots(lots)
                _pending_state.pop(uid, None)
                tg.bot.send_message(chat_id,
                    f"✅ {_esc(key)}: Denuvo daily-лимит → "
                    f"<b>{new_val_d if new_val_d else 'из настроек'}</b>",
                    parse_mode="HTML")
                if msg_id:
                    _edit_menu(chat_id, msg_id, _text_lot(key), _kb_lot(key))
            elif step == "edit_setting":
                key = st["ctx"]
                cfg = get_config()
                if key not in cfg:
                    tg.bot.send_message(chat_id, "Неизвестная настройка.")
                    _pending_state.pop(uid, None)
                    return
                old_val = cfg[key]
                if isinstance(old_val, int) and not isinstance(old_val, bool):
                    try:
                        cfg[key] = int(text)
                    except ValueError:
                        tg.bot.send_message(chat_id, "Нужно число.")
                        return
                elif isinstance(old_val, float):
                    try:
                        cfg[key] = float(text)
                    except ValueError:
                        tg.bot.send_message(chat_id, "Нужно число.")
                        return
                elif key == "guardik_command":
                    val = (text or "").strip()
                    # Команда покупателя — должна быть короткой,
                    # без пробелов / переносов / HTML, иначе сломаем
                    # _try_offline_guard_code и _handler_new_message.
                    if not val:
                        tg.bot.send_message(chat_id,
                            "Команда не может быть пустой.")
                        return
                    if any(c.isspace() for c in val):
                        tg.bot.send_message(chat_id,
                            "Команда не должна содержать пробелов.")
                        return
                    if len(val) > 32:
                        tg.bot.send_message(chat_id,
                            "Слишком длинная команда (макс. 32 символа).")
                        return
                    cfg[key] = val
                else:
                    cfg[key] = text
                save_config(cfg)
                _pending_state.pop(uid, None)
                tg.bot.send_message(chat_id,
                    f"✅ <code>{_esc(key)}</code> обновлено.",
                    parse_mode="HTML")
                if msg_id:
                    _edit_menu(chat_id, msg_id, _text_settings(),
                               _kb_settings())
            elif step == "tpl_edit":
                tpl_key = st["ctx"]
                # v1.10.0: пишем в файл выбранного языка, а не в
                # cfg["templates"]. lang берём из state (зафиксирован в
                # момент клика по «✏️ Изменить»), чтобы переключение
                # переключателем во время редактирования не повлияло.
                lang = st.get("tpl_lang") or _get_admin_lang(uid)
                if lang not in ("ru", "en"):
                    lang = "ru"
                data = dict(_load_templates_file(lang))
                data[tpl_key] = text
                _save_templates_file(lang, data)
                _pending_state.pop(uid, None)
                lang_label = "🇷🇺 RU" if lang == "ru" else "🇬🇧 EN"
                tg.bot.send_message(chat_id,
                    f"✅ Шаблон <code>{_esc(tpl_key)}</code> "
                    f"({lang_label}) обновлён.",
                    parse_mode="HTML")
                if msg_id:
                    _edit_menu(chat_id, msg_id, _text_template(tpl_key, uid),
                               _kb_template(tpl_key))
            elif step == "asgn_limit":
                aid = st["ctx"]
                try:
                    val = max(0, int(text))
                except ValueError:
                    tg.bot.send_message(chat_id, "Нужно целое число.")
                    return
                set_assignment_limit(aid, val)
                _pending_state.pop(uid, None)
                tg.bot.send_message(chat_id,
                    f"✅ Новый лимит кодов: <b>{val}</b>",
                    parse_mode="HTML")
                if msg_id:
                    _edit_menu(chat_id, msg_id, _text_asgn(aid), _kb_asgn(aid))
            elif step == "acc_setgame":
                alias = st["ctx"]
                acc = find_account(alias)
                if not acc:
                    tg.bot.send_message(chat_id, "Аккаунт не найден.")
                    _pending_state.pop(uid, None)
                    return
                t = (text or "").strip()
                if t == "-" or t == "":
                    # очистить привязку
                    acc["game"] = ""
                    acc["game_key"] = ""
                    upsert_account(acc)
                    summary = "🗑 привязка снята"
                else:
                    # v1.8.0: одновременно проставляем game и game_key
                    # Поддерживаем формат «Name:key» и просто «Name».
                    if ":" in t:
                        name_part, key_part = t.split(":", 1)
                        gname = name_part.strip()
                        gkey_in = key_part.strip()
                    else:
                        gname = t
                        gkey_in = ""
                    # Ищем существующую игру по совпадению имени/ключа
                    existing_games = list_games() or {}
                    matched_key = ""
                    for gk, gv in existing_games.items():
                        if gkey_in and gk == gkey_in:
                            matched_key = gk
                            gname = gv.get("name") or gname
                            break
                        if (gv.get("name") or "").strip().lower() == gname.lower():
                            matched_key = gk
                            break
                    if not matched_key:
                        # Новая игра — создаём через set_game (idempotent).
                        try:
                            matched_key = set_game(
                                gkey_in or _slugify_game(gname),
                                gname)
                        except Exception:
                            matched_key = _slugify_game(gname) or "game"
                    acc["game"] = gname
                    acc["game_key"] = matched_key
                    upsert_account(acc)
                    summary = (f"🎮 <b>{_esc(gname)}</b> "
                               f"(<code>{_esc(matched_key)}</code>)")
                _pending_state.pop(uid, None)
                tg.bot.send_message(chat_id,
                    f"✅ Аккаунт <code>{_esc(alias)}</code> → {summary}\n"
                    f"<i>Аккаунт автоматически попадает в пул всех лотов "
                    f"этой игры через game_key.</i>",
                    parse_mode="HTML")
                if msg_id:
                    _edit_menu(chat_id, msg_id,
                               _text_acc(alias), _kb_acc(alias))
            elif step == "acc_set_cost":
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
                acc = find_account(alias)
                if not acc:
                    tg.bot.send_message(chat_id, "Аккаунт не найден.")
                    _pending_state.pop(uid, None)
                    return
                acc["cost"] = cost_val
                upsert_account(acc)
                _pending_state.pop(uid, None)
                tg.bot.send_message(chat_id,
                    f"✅ Стоимость <code>{_esc(alias)}</code> → "
                    f"<b>{cost_val:.2f}₽</b>",
                    parse_mode="HTML")
                if msg_id:
                    _edit_menu(chat_id, msg_id,
                               _text_acc(alias), _kb_acc(alias))
            elif step == "acc_add_alias":
                alias = text
                if not alias or " " in alias or ":" in alias:
                    tg.bot.send_message(chat_id,
                        "Alias должен быть без пробелов и двоеточий. "
                        "Попробуй ещё раз:")
                    return
                if find_account(alias):
                    tg.bot.send_message(chat_id,
                        f"Аккаунт <code>{_esc(alias)}</code> уже есть. "
                        "Введи другой alias:",
                        parse_mode="HTML")
                    return
                st["draft"]["alias"] = alias
                st["step"] = "acc_add_mafile"
                tg.bot.send_message(chat_id,
                    "Шаг 2/4. Пришли <b>.maFile</b> аккаунта "
                    "(Steam Guard mobile authenticator файл).\n\n"
                    "Можно отправить как документ или как текст JSON.",
                    parse_mode="HTML")
            elif step == "acc_add_mafile":
                # Если прислал JSON текстом — принимаем здесь.
                try:
                    data = json.loads(text)
                except Exception:
                    tg.bot.send_message(chat_id,
                        "Не похоже на JSON .maFile. Пришли документом или "
                        "вставь содержимое .maFile целиком.")
                    return
                shared = data.get("shared_secret")
                identity = data.get("identity_secret")
                account_name = data.get("account_name")
                steamid = (data.get("Session", {}).get("SteamID")
                            or data.get("steamid"))
                if not shared or not identity or not account_name:
                    tg.bot.send_message(chat_id,
                        "В .maFile не хватает обязательных полей: "
                        "shared_secret / identity_secret / account_name.")
                    return
                st["draft"]["shared_secret"] = str(shared)
                st["draft"]["identity_secret"] = str(identity)
                st["draft"]["account_name"] = str(account_name)
                st["draft"]["steamid"] = str(steamid) if steamid else ""
                st["step"] = "acc_add_password"
                tg.bot.send_message(chat_id,
                    f"Шаг 3/4. Введи <b>пароль</b> от аккаунта "
                    f"<code>{_esc(account_name)}</code>:",
                    parse_mode="HTML")
            elif step == "acc_add_password":
                password = text
                if not password:
                    tg.bot.send_message(chat_id, "Пароль пустой. Введи ещё раз:")
                    return
                st["draft"]["password"] = password
                try:
                    tg.bot.delete_message(message.chat.id, message.message_id)
                except Exception:
                    pass
                st["step"] = "acc_add_cost"
                tg.bot.send_message(chat_id,
                    "Шаг 4/4. Введи <b>стоимость аккаунта</b> в ₽ для "
                    "калькулятора прибыли.\n\n"
                    "Целое или дробное число, например: <code>1500</code>.\n"
                    "Отправь <code>-</code>, чтобы пропустить.",
                    parse_mode="HTML")
            elif step == "acc_add_cost":
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
                d = st["draft"]
                acc = {
                    "alias": d["alias"],
                    "account_name": d["account_name"],
                    "password": d.get("password", ""),
                    "shared_secret": d["shared_secret"],
                    "identity_secret": d["identity_secret"],
                    "steamid": d.get("steamid", ""),
                    "frozen": False,
                    "game": "",
                    "login_failures": 0,
                    "added_at": _now(),
                    "cost": cost_val,
                }
                upsert_account(acc)
                _pending_state.pop(uid, None)
                tg.bot.send_message(chat_id,
                    f"✅ Аккаунт <code>{_esc(d['alias'])}</code> "
                    f"(<code>{_esc(d['account_name'])}</code>) добавлен"
                    + (f" (стоимость: {cost_val:.0f}₽)" if cost_val > 0 else "")
                    + ".\n\nОткрыть карточку: /soffline → 📋 Аккаунты",
                    parse_mode="HTML")
                if msg_id:
                    _edit_menu(chat_id, msg_id,
                               _text_acc(d["alias"]), _kb_acc(d["alias"]))
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
            elif step == "bl_add":
                # v1.9.0: ручное добавление в blacklist
                _pending_state.pop(uid, None)
                tokens = [t.strip() for t in re.split(r"[\s,]+", text)
                          if t.strip()]
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
                    _edit_menu(chat_id, msg_id,
                               _text_blacklist(), _kb_blacklist())
        except Exception:
            LOGGER.error("steam_offline: pending text crashed", exc_info=True)

    def _is_pending_doc(m) -> bool:
        st = _pending_state.get(m.from_user.id)
        if not st:
            return False
        return st.get("step") == "acc_add_mafile"

    def _handle_pending_doc(message):
        uid = message.from_user.id
        st = _pending_state.get(uid)
        if not st or st.get("step") != "acc_add_mafile":
            return
        chat_id = st["chat_id"]
        msg_id = st.get("main_msg_id")
        try:
            file_info = tg.bot.get_file(message.document.file_id)
            content = tg.bot.download_file(file_info.file_path)
            try:
                data = json.loads(content.decode("utf-8"))
            except Exception:
                tg.bot.send_message(chat_id,
                    "Не удалось распарсить файл как JSON. "
                    "Попробуй пришли заново.")
                return
            shared = data.get("shared_secret")
            identity = data.get("identity_secret")
            account_name = data.get("account_name")
            steamid = (data.get("Session", {}).get("SteamID")
                        or data.get("steamid"))
            if not shared or not identity or not account_name:
                tg.bot.send_message(chat_id,
                    "В .maFile не хватает: "
                    "shared_secret / identity_secret / account_name.")
                return
            st["draft"]["shared_secret"] = str(shared)
            st["draft"]["identity_secret"] = str(identity)
            st["draft"]["account_name"] = str(account_name)
            st["draft"]["steamid"] = str(steamid) if steamid else ""
            st["step"] = "acc_add_password"
            tg.bot.send_message(chat_id,
                f"Шаг 3/4. Введи <b>пароль</b> от аккаунта "
                f"<code>{_esc(account_name)}</code>:",
                parse_mode="HTML")
        except Exception:
            LOGGER.error("steam_offline: doc handler failed", exc_info=True)
            tg.bot.send_message(chat_id, "Ошибка при чтении файла.")

    # ───── Регистрация в telebot ─────────────────────────────────────────
    tg.msg_handler(cmd_soffline, commands=["soffline"])
    tg.msg_handler(cmd_soffline_cancel, commands=["soffline_cancel"])
    tg.msg_handler(cmd_soffline_stats, commands=["soffline_stats"])
    tg.msg_handler(cmd_soffline_acc_stats, commands=["soffline_acc_stats"])
    tg.msg_handler(_handle_pending_text, func=_is_pending_text)
    tg.msg_handler(_handle_pending_doc, func=_is_pending_doc,
                   content_types=["document"])
    tg.cbq_handler(on_cb, lambda c: (c.data or "").startswith("so:"))

    # /soffline_guide — гайд
    def cmd_guide(m) -> None:
        guide_text = (
            "<b>📖 Steam Offline — Гайд</b>\n\n"
            "<b>Что делает:</b>\n"
            "Продажа Steam-аккаунтов «навсегда» (без срока аренды). "
            "Покупатель получает логин/пароль, а Steam Guard коды "
            "выдаются ограниченное число раз (лимит задаётся на лот).\n\n"
            "<b>Быстрый старт:</b>\n"
            "1. /soffline → 📋 Аккаунты → ➕ Добавить\n"
            "2. Загрузите .maFile (shared_secret + identity_secret)\n"
            "3. /soffline → 🎯 Лоты → ➕ Добавить\n"
            "4. Привяжите alias к лоту, задайте лимит выдач кода\n"
            "5. (опц.) Включите Denuvo-режим, если нужно несколько кодов в день\n\n"
            "<b>Как работает:</b>\n"
            "• Покупатель оплачивает лот → бот выдаёт логин/пароль\n"
            "• Покупатель пишет !код → приходит Steam Guard (TOTP)\n"
            "• Каждый запрос !код уменьшает лимит\n"
            "• После исчерпания лимита — команда перестаёт работать\n"
            "• Аккаунт остаётся у покупателя НАВСЕГДА (без авто-смены пароля)\n\n"
            "<b>📨 Команды покупателя:</b>\n"
            "• <code>!код [логин]</code> — Steam Guard. Без логина — для активного заказа\n"
            "• <code>!статус</code> — остаток выдач кода\n"
            "• <code>!помощь</code> — список команд\n\n"
            "<b>Особенности:</b>\n"
            "• Отдельный пул аккаунтов от steam_rental "
            "(не пересекается!)\n"
            "• Denuvo-режим — несколько выдач кода в день "
            "(под игры с Denuvo и сменой железа)\n"
            "• Заморозка аккаунта (при подозрении/бане)\n"
            "• Чёрный список покупателей\n"
            "• История выдач + CSV-экспорт\n"
            "• Авто-установка steampy при первом запуске\n\n"
            "<b>Telegram-команды:</b>\n"
            "/soffline — главное меню\n"
            "/soffline_guide — этот гайд\n"
            "/soffline_test — тест Steam Guard (реальный/фейковый)\n"
            "/soffline_cancel — отменить ввод (если завис диалог)"
        )
        tg.bot.send_message(m.chat.id, guide_text, parse_mode="HTML")

    tg.msg_handler(cmd_guide, commands=["soffline_guide"])

    # /soffline_test — тест Steam Guard на фейковых данных
    def cmd_test(m) -> None:
        kb = tbtypes.InlineKeyboardMarkup(row_width=2)
        kb.add(
            tbtypes.InlineKeyboardButton(
                "🌐 Реальный тест", callback_data="so:test:REAL"),
            tbtypes.InlineKeyboardButton(
                "🎭 Фейковый тест", callback_data="so:test:FAKE"),
        )
        tg.bot.send_message(
            m.chat.id,
            "🧪 <b>Выберите тип теста:</b>\n\n"
            "🌐 <b>Реальный</b> — подключение к Steam (первый аккаунт)\n"
            "🎭 <b>Фейковый</b> — проверка логики генерации без подключения",
            parse_mode="HTML",
            reply_markup=kb,
        )

    tg.msg_handler(cmd_test, commands=["soffline_test"])

    try:
        cardinal.add_telegram_commands(UUID, [
            ("soffline", "Steam Offline: открыть меню", True),
            ("soffline_stats", "Steam Offline: статистика и финансы", True),
            ("soffline_acc_stats", "Steam Offline: статистика по аккаунту", True),
            ("soffline_cancel", "Steam Offline: отменить ввод", False),
            ("soffline_guide", "Steam Offline: гайд", True),
            ("soffline_test", "Steam Offline: тест", True),
        ])
    except Exception:
        LOGGER.debug("steam_offline: add_telegram_commands failed",
                     exc_info=True)


def _open_settings_page(cardinal: "Cardinal", msg) -> None:
    """FPC settings page handler - directs user to /soffline."""
    tg = getattr(cardinal, "telegram", None)
    if tg is None:
        return
    tg.bot.send_message(
        msg.chat.id,
        "<b>Steam Offline</b>\n\n"
        "Для настройки используйте команду /soffline\n"
        "Для гайда: /soffline_guide\n"
        "Для теста: /soffline_test",
        parse_mode="HTML",
    )


# ── Экспорт хэндлеров для FPC ───────────────────────────────────────────────
BIND_TO_SETTINGS_PAGE = _open_settings_page
BIND_TO_PRE_INIT = [_handler_pre_init]
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


