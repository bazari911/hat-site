#!/usr/bin/env python3
"""Хат — регистрация ящиков @kazxat.kz.

Один процесс, только стандартная библиотека Python:
  * HTTP API для страницы auth.html (nginx проксирует /api/reg/ сюда);
  * Telegram-бот (long polling): подтверждение номера кнопкой «Поделиться номером»;
  * код подтверждения на другую почту (через ящик noreply@kazxat.kz);
  * своя картинка-капча (цифры нарисованы линиями, текста в SVG нет);
  * лимиты на IP, номер, почту;
  * создание ящика через API mailcow;
  * «Забыли пароль?»: код в Telegram (или на резервную почту), новый пароль через API mailcow;
  * форма поддержки: письмо на support@kazxat.kz.

Секреты лежат только в /etc/hat-reg/config.json (права 600). В журнал не пишутся.

Команды администратора (запускать через sudo):
  hat_reg.py serve            — запуск сервиса (так его запускает systemd)
  hat_reg.py selftest         — проверка бота, API mailcow и почты, без изменений
  hat_reg.py create-noreply   — создать ящик noreply@kazxat.kz для писем с кодами
  hat_reg.py domain-limits N  — показать/поднять лимит ящиков домена до N
  hat_reg.py support-alias ЯЩИК — направить support@kazxat.kz на ЯЩИК (например zharkyn@kazxat.kz)
"""

import base64
import hashlib
import hmac
import http.client
import json
import math
import os
import random
import re
import secrets
import smtplib
import socket
import sqlite3
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONFIG_PATH = "/etc/hat-reg/config.json"
DOMAIN = "kazxat.kz"
ALLOWED_ORIGINS = {"https://kazxat.kz", "https://www.kazxat.kz"}

# Адреса, которые нельзя занять
RESERVED = set("""
admin administrator root postmaster hostmaster webmaster abuse security noreply no-reply
mailer-daemon daemon support help info contact mail email pochta post hat xat kazxat
kazxat.kz test tester billing payment pay sales news robot bot system sysadmin staff
moderator owner office press privacy legal ssl dns ftp www api null nobody
""".split())

DEFAULT_LIMITS = {
    "session_ttl": 3600,          # сколько живёт незаконченная регистрация, сек
    "starts_per_ip_day": 15,      # попыток начать регистрацию с IP в сутки
    "accounts_per_ip_day": 3,     # ящиков с одного IP в сутки (любым способом)
    "none_per_ip_day": 1,         # ящиков без подтверждения с IP в сутки
    "none_total_day": 30,         # ящиков без подтверждения на весь сервис в сутки
    "accounts_per_phone": 3,      # ящиков на один номер Telegram (всего)
    "accounts_per_email": 3,      # ящиков на одну внешнюю почту (всего)
    "codes_per_email_hour": 3,    # писем с кодом на один адрес в час
    "codes_per_ip_day": 10,       # писем с кодом с одного IP в сутки
    "code_ttl": 900,              # код действует 15 минут
    "code_tries": 5,              # попыток ввести код
    "resend_after": 60,           # повторная отправка не раньше, сек
    "reset_per_ip_day": 10,       # запросов «Забыли пароль?» с одного IP в сутки
    "reset_per_addr_hour": 3,     # кодов восстановления на один ящик в час
    "support_per_ip_day": 5,      # сообщений в поддержку с одного IP в сутки
}

SUPPORT_TOPICS = ("Не могу войти", "Восстановление пароля", "Регистрация", "Письма не приходят",
                  "Спам или мошенничество", "Предложение", "Другое")

log_lock = threading.Lock()


def log(*a):
    with log_lock:
        print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


# ----------------------------------------------------------------- настройки

def load_config():
    path = CONFIG_PATH
    cred = os.environ.get("CREDENTIALS_DIRECTORY")
    if cred and os.path.exists(os.path.join(cred, "config")):
        path = os.path.join(cred, "config")
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("listen", "172.22.1.1")
    cfg.setdefault("port", 8790)
    cfg.setdefault("api_ip", "172.22.1.247")
    cfg.setdefault("api_host", "mail." + DOMAIN)
    cfg.setdefault("smtp_host", "mail." + DOMAIN)
    cfg.setdefault("smtp_port", 587)
    cfg.setdefault("smtp_user", "noreply@" + DOMAIN)
    cfg.setdefault("quota_mb", 3072)
    cfg.setdefault("bot_name", "kazxat_kz_bot")
    cfg.setdefault("support_to", "support@" + DOMAIN)
    cfg.setdefault("db", os.path.join(os.environ.get("STATE_DIRECTORY", "/var/lib/hat-reg"), "reg.db"))
    lim = dict(DEFAULT_LIMITS)
    lim.update(cfg.get("limits") or {})
    cfg["limits"] = lim
    if not cfg.get("salt"):
        raise SystemExit("В config.json нет salt")
    return cfg


CFG = None


def H(*parts):
    """Хэш с солью — номера телефонов и коды в базе не хранятся открыто."""
    msg = "\x1f".join(str(p) for p in parts).encode()
    return hmac.new(CFG["salt"].encode(), msg, hashlib.sha256).hexdigest()


# ----------------------------------------------------------------- база

DB_LOCK = threading.Lock()
_db = None


def db():
    global _db
    if _db is None:
        os.makedirs(os.path.dirname(CFG["db"]), exist_ok=True)
        _db = sqlite3.connect(CFG["db"], check_same_thread=False, isolation_level=None)
        _db.row_factory = sqlite3.Row
        _db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS sessions(
          sid TEXT PRIMARY KEY, created INT, ip TEXT, login TEXT, method TEXT,
          email TEXT, code_hash TEXT, code_sent INT DEFAULT 0, code_count INT DEFAULT 0,
          tries INT DEFAULT 0, verified INT DEFAULT 0, tg_token TEXT, tg_chat INT,
          phone_hash TEXT, done INT DEFAULT 0);
        CREATE INDEX IF NOT EXISTS s_tg ON sessions(tg_token);
        CREATE INDEX IF NOT EXISTS s_chat ON sessions(tg_chat);
        CREATE TABLE IF NOT EXISTS captcha(id TEXT PRIMARY KEY, answer TEXT, created INT);
        CREATE TABLE IF NOT EXISTS accounts(
          address TEXT PRIMARY KEY, created INT, ip TEXT, method TEXT,
          ext_email TEXT, tg_chat INT, phone_hash TEXT);
        CREATE TABLE IF NOT EXISTS events(kind TEXT, k TEXT, ts INT);
        CREATE INDEX IF NOT EXISTS e_k ON events(kind, k, ts);
        CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS resets(
          rid TEXT PRIMARY KEY, created INT, ip TEXT, address TEXT, method TEXT,
          code_hash TEXT, tries INT DEFAULT 0, done INT DEFAULT 0);
        CREATE INDEX IF NOT EXISTS r_addr ON resets(address);
        """)
    return _db


def q(sql, args=(), one=False):
    with DB_LOCK:
        cur = db().execute(sql, args)
        rows = cur.fetchall()
    return (rows[0] if rows else None) if one else rows


def ev_add(kind, k):
    q("INSERT INTO events(kind,k,ts) VALUES(?,?,?)", (kind, k, int(time.time())))


def ev_count(kind, k, window):
    r = q("SELECT COUNT(*) c FROM events WHERE kind=? AND k=? AND ts>?",
          (kind, k, int(time.time()) - window), one=True)
    return r["c"]


def cleanup():
    now = int(time.time())
    q("DELETE FROM sessions WHERE created<?", (now - 2 * 86400,))
    q("DELETE FROM captcha WHERE created<?", (now - 1800,))
    q("DELETE FROM resets WHERE created<?", (now - 2 * 86400,))
    q("DELETE FROM events WHERE ts<?", (now - 3 * 86400,))


# ----------------------------------------------------------------- mailcow API

class PinnedHTTPS(http.client.HTTPSConnection):
    """HTTPS к nginx mailcow по внутреннему адресу 172.22.1.247, сертификат проверяется
    по имени mail.kazxat.kz. Запрос идёт из сети 172.22.1.0/24 — её API и разрешает."""

    def __init__(self, ip, host, timeout=20):
        super().__init__(host, 443, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def api(method, path, body=None):
    conn = PinnedHTTPS(CFG["api_ip"], CFG["api_host"])
    headers = {"X-API-Key": CFG["api_key"], "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    try:
        conn.request(method, "/api/v1/" + path, body=data, headers=headers)
        r = conn.getresponse()
        raw = r.read()
        if r.status != 200:
            raise RuntimeError("mailcow API HTTP %s" % r.status)
        return json.loads(raw or b"null")
    finally:
        conn.close()


def mailbox_exists(addr):
    r = api("GET", "get/mailbox/" + urllib.parse.quote(addr))
    return bool(r) and isinstance(r, dict) and bool(r.get("username"))


def api_ok(resp):
    items = resp if isinstance(resp, list) else [resp]
    ok = any(isinstance(i, dict) and i.get("type") == "success" for i in items)
    msgs = []
    for i in items:
        if isinstance(i, dict) and i.get("type") != "success":
            m = i.get("msg")
            msgs.append(json.dumps(m, ensure_ascii=False) if not isinstance(m, str) else m)
    return ok, "; ".join(msgs)


def add_mailbox(local, name, password, quota_mb):
    body = {
        "local_part": local, "domain": DOMAIN, "name": name,
        "password": password, "password2": password,
        "quota": str(quota_mb), "active": "1", "force_pw_update": "0",
        "tls_enforce_in": "0", "tls_enforce_out": "0",
    }
    return api_ok(api("POST", "add/mailbox", body))


def set_mailbox_password(addr, password):
    body = {"items": [addr], "attr": {"password": password, "password2": password}}
    return api_ok(api("POST", "edit/mailbox", body))


def alias_exists(addr):
    r = api("GET", "get/alias/all")
    return any(isinstance(a, dict) and str(a.get("address", "")).lower() == addr.lower()
               for a in (r if isinstance(r, list) else []))


# ----------------------------------------------------------------- почта с кодом

def send_mail(to, subject, text, reply_to=None):
    msg = EmailMessage()
    msg["From"] = formataddr(("Хат Почта", CFG["smtp_user"]))
    msg["To"] = to
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    msg["Message-ID"] = make_msgid(domain=DOMAIN)
    msg.set_content(text)
    with smtplib.SMTP(CFG["smtp_host"], CFG["smtp_port"], timeout=25) as s:
        s.starttls(context=ssl.create_default_context())
        s.login(CFG["smtp_user"], CFG["smtp_pass"])
        s.send_message(msg)


def code_letter(code, login):
    return (
        f"Код подтверждения: {code}\n\n"
        f"Кто-то (возможно, вы) регистрирует ящик {login}@{DOMAIN} и указал этот адрес "
        f"как резервный. Введите код на странице регистрации. Код действует 15 минут.\n"
        f"Если это были не вы, просто удалите письмо.\n\n"
        f"Растау коды: {code}\n"
        f"{login}@{DOMAIN} поштасын тіркеу үшін кодты тіркеу бетіне енгізіңіз. "
        f"Код 15 минут жарамды. Егер бұл сіз болмасаңыз, хатты өшіріп тастаңыз.\n\n"
        f"— Хат Почта, https://kazxat.kz\n"
    )


def reset_letter(code, addr):
    return (
        f"Код для смены пароля: {code}\n\n"
        f"Кто-то (возможно, вы) восстанавливает пароль от ящика {addr}. "
        f"Введите код на странице восстановления. Код действует 15 минут.\n"
        f"Если это были не вы, никому не сообщайте код и просто удалите письмо.\n\n"
        f"Құпиясөзді өзгерту коды: {code}\n"
        f"{addr} поштасының құпиясөзін қалпына келтіру үшін кодты енгізіңіз. "
        f"Код 15 минут жарамды. Егер бұл сіз болмасаңыз, кодты ешкімге айтпаңыз.\n\n"
        f"— Хат Почта, https://kazxat.kz\n"
    )


# ----------------------------------------------------------------- капча

# Цифры нарисованы ломаными на сетке 4×6 — в SVG нет ни одного символа текста
GLYPHS = {
    "2": [[(0, 1.2), (1, 0), (3, 0), (4, 1), (4, 2.2), (0, 6), (4, 6)]],
    "3": [[(0, 0.4), (1, 0), (3, 0), (4, 1), (4, 2), (2.6, 2.9), (4, 3.8), (4, 5), (3, 6), (1, 6), (0, 5.4)]],
    "4": [[(3, 6), (3, 0), (0, 4.2), (4.2, 4.2)]],
    "5": [[(4, 0), (0.4, 0), (0, 2.6), (3, 2.4), (4, 3.4), (4, 5), (3, 6), (1, 6), (0, 5.3)]],
    "6": [[(3.6, 0.3), (2, 0), (0.6, 1), (0, 3), (0, 5), (1, 6), (3, 6), (4, 5), (4, 3.6), (3, 2.7), (1, 2.7), (0, 3.6)]],
    "7": [[(0, 0), (4, 0), (1.4, 6)], [(1, 3), (3.2, 3)]],
    "8": [[(2, 2.8), (3.6, 2), (3.8, 0.8), (3, 0), (1, 0), (0.2, 0.8), (0.4, 2), (2, 2.8),
           (3.8, 3.8), (4, 5), (3, 6), (1, 6), (0, 5), (0.2, 3.8), (2, 2.8)]],
    "9": [[(4, 2.6), (3, 3.4), (1, 3.4), (0, 2.5), (0, 1), (1, 0), (3, 0), (4, 1), (4, 4), (3.2, 5.6), (1.5, 6), (0.4, 5.6)]],
}
CAPTCHA_LEN = 5


def make_captcha():
    rnd = random.SystemRandom()
    code = "".join(rnd.choice("23456789") for _ in range(CAPTCHA_LEN))
    W, Hh = 210, 64
    colors = ["#06566a", "#0089a0", "#7a5a00", "#1d2a30", "#a42a2a"]
    parts = []
    # шум: кривые
    for _ in range(7):
        pts = [(rnd.uniform(0, W), rnd.uniform(0, Hh)) for _ in range(4)]
        parts.append('<path d="M%.1f %.1f C%.1f %.1f %.1f %.1f %.1f %.1f" stroke="%s" stroke-width="%.1f" fill="none" opacity=".55"/>'
                     % (*pts[0], *pts[1], *pts[2], *pts[3], rnd.choice(colors), rnd.uniform(1, 2.2)))
    for _ in range(40):
        parts.append('<circle cx="%.1f" cy="%.1f" r="%.1f" fill="%s" opacity=".45"/>'
                     % (rnd.uniform(0, W), rnd.uniform(0, Hh), rnd.uniform(.6, 1.8), rnd.choice(colors)))
    # цифры
    x = 12
    for ch in code:
        s = rnd.uniform(5.8, 7.0)
        ang = math.radians(rnd.uniform(-20, 20))
        cx, cy = x + 2 * s, Hh / 2 + rnd.uniform(-5, 5)
        col = rnd.choice(colors)
        for line in GLYPHS[ch]:
            pts = []
            for (gx, gy) in line:
                px = (gx - 2 + rnd.uniform(-.22, .22)) * s
                py = (gy - 3 + rnd.uniform(-.22, .22)) * s
                rx = cx + px * math.cos(ang) - py * math.sin(ang)
                ry = cy + px * math.sin(ang) + py * math.cos(ang)
                pts.append("%.1f %.1f" % (rx, ry))
            parts.append('<path d="M%s" stroke="%s" stroke-width="%.1f" fill="none" stroke-linecap="round" stroke-linejoin="round"/>'
                         % (" L".join(pts), col, rnd.uniform(2.6, 3.6)))
        x += s * 4 + rnd.uniform(6, 10)
    rnd.shuffle(parts)  # порядок элементов в файле не совпадает с порядком цифр
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d">'
           '<rect width="100%%" height="100%%" fill="#f1f9fb"/>%s</svg>') % (W, Hh, W, Hh, "".join(parts))
    cid = secrets.token_urlsafe(16)
    q("INSERT INTO captcha(id,answer,created) VALUES(?,?,?)", (cid, H("cap", cid, code), int(time.time())))
    return cid, "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


def check_captcha(cid, answer):
    if not cid or not answer:
        return False
    r = q("SELECT answer, created FROM captcha WHERE id=?", (cid,), one=True)
    q("DELETE FROM captcha WHERE id=?", (cid,))  # одноразовая
    if not r or time.time() - r["created"] > 900:
        return False
    a = re.sub(r"\D", "", str(answer))
    return hmac.compare_digest(r["answer"], H("cap", cid, a))


# ----------------------------------------------------------------- проверки

LOGIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9]|[._-](?=[a-z0-9])){2,29}$")
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$")


def login_error(login):
    if not login:
        return "Введите адрес"
    if len(login) < 3:
        return "Слишком короткий адрес (минимум 3 символа)"
    if len(login) > 30:
        return "Слишком длинный адрес (максимум 30 символов)"
    if not LOGIN_RE.match(login):
        return "Только латинские буквы, цифры, точка, дефис и подчёркивание"
    if login in RESERVED:
        return "Этот адрес уже занят, попробуйте другой"
    return ""


def login_free(login):
    addr = f"{login}@{DOMAIN}"
    if q("SELECT 1 FROM accounts WHERE address=?", (addr,), one=True):
        return False
    return not mailbox_exists(addr)


def password_error(p):
    if not isinstance(p, str) or len(p) < 8:
        return "Пароль: минимум 8 символов"
    if len(p) > 64:
        return "Пароль слишком длинный"
    if any(ord(c) < 33 or ord(c) > 126 for c in p):
        return "Пароль: только латинские буквы, цифры и знаки, без пробелов"
    if not re.search(r"[A-Za-z]", p) or not re.search(r"\d", p):
        return "Пароль: нужны латинские буквы и цифры"
    return ""


def clean_name(s):
    s = re.sub(r"[\x00-\x1f<>\"\\]", "", str(s or "")).strip()
    return re.sub(r"\s+", " ", s)[:60]


def session(sid):
    if not sid or not isinstance(sid, str):
        return None
    r = q("SELECT * FROM sessions WHERE sid=?", (sid,), one=True)
    if not r or r["done"] or time.time() - r["created"] > CFG["limits"]["session_ttl"]:
        return None
    return r


# ----------------------------------------------------------------- Telegram

TG_API = "https://api.telegram.org/bot{}/{}"


def tg(method, **params):
    data = json.dumps(params).encode()
    req = urllib.request.Request(TG_API.format(CFG["bot_token"], method), data=data,
                                 headers={"Content-Type": "application/json"})
    timeout = params.get("timeout", 0) + 15
    with urllib.request.urlopen(req, timeout=timeout) as r:
        res = json.loads(r.read())
    if not res.get("ok"):
        raise RuntimeError("Telegram: %s" % res.get("description"))
    return res["result"]


def tg_send(chat, text, markup=None):
    """Отправить сообщение в Telegram. True — дошло, False — нет (бот заблокирован и т.п.)."""
    try:
        p = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
        if markup is not None:
            p["reply_markup"] = markup
        tg("sendMessage", **p)
        return True
    except Exception as e:  # noqa
        log("tg send error:", type(e).__name__)
        return False


SHARE_KB = {"keyboard": [[{"text": "Поделиться номером", "request_contact": True}]],
            "resize_keyboard": True, "one_time_keyboard": True}
NO_KB = {"remove_keyboard": True}

HELLO = ("Здравствуйте! Это бот Хат Почты (kazxat.kz).\n"
         "Он подтверждает номер телефона при регистрации ящика @kazxat.kz.\n"
         "Начните регистрацию на сайте: https://kazxat.kz/auth.html#register\n\n"
         "Сәлеметсіз бе! Бұл — Хат Почтаның боты. Тіркелу сайтта басталады.")


def tg_handle(msg):
    chat = msg.get("chat") or {}
    if chat.get("type") != "private":
        return
    cid = chat.get("id")
    frm = (msg.get("from") or {}).get("id")
    text = (msg.get("text") or "").strip()
    lim = CFG["limits"]

    if text.startswith("/start"):
        token = text[6:].strip()
        s = None
        if token:
            r = q("SELECT * FROM sessions WHERE tg_token=?", (token,), one=True)
            if r and not r["done"] and time.time() - r["created"] < lim["session_ttl"]:
                s = r
        if not s:
            tg_send(cid, HELLO, NO_KB)
            return
        if s["verified"]:
            tg_send(cid, "Номер уже подтверждён. Вернитесь на страницу регистрации.", NO_KB)
            return
        q("UPDATE sessions SET tg_chat=? WHERE sid=?", (cid, s["sid"]))
        tg_send(cid,
                f"Регистрация ящика {s['login']}@{DOMAIN}.\n"
                "Нажмите кнопку «Поделиться номером» внизу — так мы убедимся, что вы человек. "
                "Номер не показывается другим людям и нужен, чтобы восстановить пароль.\n\n"
                "Төмендегі «Поделиться номером» батырмасын басыңыз.", SHARE_KB)
        return

    contact = msg.get("contact")
    if contact:
        s = q("SELECT * FROM sessions WHERE tg_chat=? AND verified=0 AND done=0 AND created>? "
              "ORDER BY created DESC LIMIT 1", (cid, int(time.time()) - lim["session_ttl"]), one=True)
        if not s:
            tg_send(cid, "Сначала начните регистрацию на сайте: https://kazxat.kz/auth.html#register", NO_KB)
            return
        if contact.get("user_id") != frm:
            tg_send(cid, "Нужен ваш собственный номер. Нажмите кнопку «Поделиться номером».", SHARE_KB)
            return
        phone = re.sub(r"\D", "", contact.get("phone_number") or "")
        if len(phone) == 11 and phone[0] == "8":
            phone = "7" + phone[1:]
        if not re.fullmatch(r"77\d{9}", phone):
            tg_send(cid, "Пока регистрация доступна только с казахстанским номером +7 7xx.", NO_KB)
            return
        ph = H("phone", phone)
        n = q("SELECT COUNT(*) c FROM accounts WHERE phone_hash=?", (ph,), one=True)["c"]
        if n >= lim["accounts_per_phone"]:
            tg_send(cid, f"На этот номер уже зарегистрировано {n} ящика — это максимум.", NO_KB)
            return
        q("UPDATE sessions SET verified=1, phone_hash=? WHERE sid=?", (ph, s["sid"]))
        tg_send(cid, "Готово, номер подтверждён. Вернитесь на страницу регистрации — "
                     "ящик создастся сам.\n\nНөмір расталды. Тіркеу бетіне оралыңыз.", NO_KB)
        return

    tg_send(cid, HELLO, NO_KB)


def tg_loop():
    me = None
    while me is None:
        try:
            me = tg("getMe")
            log("Telegram-бот подключён: @%s" % me.get("username"))
        except Exception as e:
            log("Telegram getMe:", type(e).__name__, str(e)[:120].replace(CFG["bot_token"], "***"))
            time.sleep(30)
    try:
        tg("deleteWebhook")
    except Exception:
        pass
    r = q("SELECT v FROM kv WHERE k='tg_offset'", one=True)
    offset = int(r["v"]) if r else 0
    while True:
        try:
            ups = tg("getUpdates", offset=offset, timeout=50, allowed_updates=["message"])
            for u in ups:
                offset = u["update_id"] + 1
                q("INSERT OR REPLACE INTO kv(k,v) VALUES('tg_offset',?)", (str(offset),))
                if "message" in u:
                    try:
                        tg_handle(u["message"])
                    except Exception as e:
                        log("tg handle error:", type(e).__name__, str(e)[:200])
        except Exception as e:
            log("tg poll:", type(e).__name__, str(e)[:120].replace(CFG["bot_token"], "***"))
            time.sleep(5)


# ----------------------------------------------------------------- HTTP API

class Err(Exception):
    def __init__(self, msg, code=400, extra=None):
        super().__init__(msg)
        self.msg, self.code, self.extra = msg, code, extra or {}


def do_start(ip, b):
    lim = CFG["limits"]
    login = str(b.get("login") or "").strip().lower()
    method = b.get("method")
    if method not in ("tg", "email", "none"):
        raise Err("Выберите способ подтверждения")
    e = login_error(login)
    if e:
        raise Err(e)
    if ev_count("start", ip, 86400) >= lim["starts_per_ip_day"]:
        raise Err("Слишком много попыток. Попробуйте завтра.", 429)
    if not check_captcha(b.get("captcha_id"), b.get("captcha")):
        raise Err("Неверные цифры с картинки, попробуйте ещё раз", 400, {"captcha": True})
    ev_add("start", ip)
    if ev_count("acc_ip", ip, 86400) >= lim["accounts_per_ip_day"]:
        raise Err("С вашего подключения сегодня уже созданы ящики. Попробуйте завтра.", 429)
    if not login_free(login):
        raise Err("Этот адрес уже занят, попробуйте другой", 409, {"taken": True})

    sid = secrets.token_urlsafe(24)
    now = int(time.time())
    out = {"sid": sid, "method": method}
    if method == "none":
        if ev_count("none_ip", ip, 86400) >= lim["none_per_ip_day"]:
            raise Err("Без подтверждения можно создать 1 ящик в сутки. Подтвердите через Telegram или почту.", 429)
        if ev_count("none_all", "*", 86400) >= lim["none_total_day"]:
            raise Err("Сегодня лимит регистраций без подтверждения исчерпан. Подтвердите через Telegram или почту.", 429)
        q("INSERT INTO sessions(sid,created,ip,login,method,verified) VALUES(?,?,?,?,?,1)",
          (sid, now, ip, login, method))
    elif method == "tg":
        tok = secrets.token_urlsafe(18)  # для ссылки t.me/...?start= (до 64 символов)
        q("INSERT INTO sessions(sid,created,ip,login,method,tg_token) VALUES(?,?,?,?,?,?)",
          (sid, now, ip, login, method, tok))
        out["tg_link"] = f"https://t.me/{CFG['bot_name']}?start={tok}"
        out["bot"] = "@" + CFG["bot_name"]
    else:
        email = str(b.get("email") or "").strip()
        if not EMAIL_RE.match(email):
            raise Err("Проверьте адрес резервной почты")
        if email.lower().endswith("@" + DOMAIN):
            raise Err("Укажите почту на другом сервисе, не @kazxat.kz")
        n = q("SELECT COUNT(*) c FROM accounts WHERE lower(ext_email)=?", (email.lower(),), one=True)["c"]
        if n >= lim["accounts_per_email"]:
            raise Err("К этой почте уже привязано максимальное число ящиков")
        q("INSERT INTO sessions(sid,created,ip,login,method,email) VALUES(?,?,?,?,?,?)",
          (sid, now, ip, login, method, email))
        send_code(sid, ip)
        out["email"] = email
    return out


def send_code(sid, ip):
    lim = CFG["limits"]
    s = session(sid)
    if not s or s["method"] != "email":
        raise Err("Регистрация устарела, начните заново", 410)
    if s["code_sent"] and time.time() - s["code_sent"] < lim["resend_after"]:
        raise Err("Подождите минуту перед повторной отправкой", 429)
    if s["code_count"] >= 5:
        raise Err("Слишком много писем. Начните регистрацию заново.", 429)
    em = s["email"].lower()
    if ev_count("code_mail", em, 3600) >= lim["codes_per_email_hour"]:
        raise Err("На этот адрес уже отправлено несколько кодов. Попробуйте через час.", 429)
    if ev_count("code_ip", ip, 86400) >= lim["codes_per_ip_day"]:
        raise Err("Слишком много писем с вашего подключения. Попробуйте завтра.", 429)
    code = "%06d" % secrets.randbelow(1000000)
    try:
        send_mail(s["email"], f"Код подтверждения Хат Почты: {code}", code_letter(code, s["login"]))
    except Exception as e:
        log("smtp error:", type(e).__name__, str(e)[:200])
        raise Err("Не удалось отправить письмо. Попробуйте позже или выберите Telegram.", 502)
    ev_add("code_mail", em)
    ev_add("code_ip", ip)
    q("UPDATE sessions SET code_hash=?, code_sent=?, code_count=code_count+1, tries=0 WHERE sid=?",
      (H("code", sid, code), int(time.time()), sid))
    return {"ok": True}


def do_code(ip, b):
    lim = CFG["limits"]
    s = session(b.get("sid"))
    if not s or s["method"] != "email":
        raise Err("Регистрация устарела, начните заново", 410)
    if s["verified"]:
        return {"ok": True}
    if s["tries"] >= lim["code_tries"]:
        raise Err("Слишком много неверных попыток. Запросите новый код.", 429)
    code = re.sub(r"\D", "", str(b.get("code") or ""))
    q("UPDATE sessions SET tries=tries+1 WHERE sid=?", (s["sid"],))
    if not s["code_hash"] or time.time() - s["code_sent"] > lim["code_ttl"]:
        raise Err("Код устарел. Нажмите «Отправить ещё раз».", 410)
    if not hmac.compare_digest(s["code_hash"], H("code", s["sid"], code)):
        raise Err("Неверный код")
    q("UPDATE sessions SET verified=1 WHERE sid=?", (s["sid"],))
    return {"ok": True}


def do_status(ip, params):
    s = session(params.get("sid"))
    if not s:
        raise Err("Регистрация устарела, начните заново", 410)
    return {"verified": bool(s["verified"]), "method": s["method"],
            "tg_opened": bool(s["tg_chat"])}


CREATE_LOCK = threading.Lock()


def do_create(ip, b):
    lim = CFG["limits"]
    with CREATE_LOCK:
        s = session(b.get("sid"))
        if not s:
            raise Err("Регистрация устарела, начните заново", 410)
        if not s["verified"]:
            raise Err("Сначала подтвердите регистрацию")
        login = s["login"]
        pe = password_error(b.get("password"))
        if pe:
            raise Err(pe)
        first, last = clean_name(b.get("first")), clean_name(b.get("last"))
        if not first or not last:
            raise Err("Укажите имя и фамилию")
        if ev_count("acc_ip", ip, 86400) >= lim["accounts_per_ip_day"]:
            raise Err("С вашего подключения сегодня уже созданы ящики. Попробуйте завтра.", 429)
        if s["method"] == "none":
            if ev_count("none_ip", ip, 86400) >= lim["none_per_ip_day"]:
                raise Err("Без подтверждения можно создать 1 ящик в сутки.", 429)
        if s["phone_hash"]:
            n = q("SELECT COUNT(*) c FROM accounts WHERE phone_hash=?", (s["phone_hash"],), one=True)["c"]
            if n >= lim["accounts_per_phone"]:
                raise Err("На этот номер уже зарегистрировано максимальное число ящиков")
        if not login_free(login):
            raise Err("Этот адрес уже занят, попробуйте другой", 409, {"taken": True})
        ok, why = add_mailbox(login, f"{first} {last}", b["password"], CFG["quota_mb"])
        addr = f"{login}@{DOMAIN}"
        if not ok:
            log("add mailbox failed:", addr, why[:300])
            if "exists" in why or "alias" in why:
                raise Err("Этот адрес уже занят, попробуйте другой", 409, {"taken": True})
            if "password" in why.lower():
                raise Err("Сервер не принял пароль: сделайте его сложнее", 400)
            raise Err("Не удалось создать ящик. Попробуйте позже или напишите в поддержку.", 502)
        now = int(time.time())
        q("INSERT OR REPLACE INTO accounts(address,created,ip,method,ext_email,tg_chat,phone_hash) "
          "VALUES(?,?,?,?,?,?,?)",
          (addr, now, ip, s["method"], s["email"] if s["method"] == "email" else None,
           s["tg_chat"] if s["method"] == "tg" else None, s["phone_hash"]))
        q("UPDATE sessions SET done=1 WHERE sid=?", (s["sid"],))
        ev_add("acc_ip", ip)
        if s["method"] == "none":
            ev_add("none_ip", ip)
            ev_add("none_all", "*")
        log("создан ящик", addr, "способ", s["method"])
    if s["method"] == "tg" and s["tg_chat"]:
        tg_send(s["tg_chat"], f"Ящик {addr} создан. Войти: https://mail.kazxat.kz/rc/", NO_KB)
    return {"ok": True, "address": addr}


def do_check(ip, params):
    login = str(params.get("login") or "").strip().lower()
    e = login_error(login)
    if e:
        return {"ok": False, "msg": e}
    if ev_count("check", ip, 60) > 40:
        raise Err("Слишком часто", 429)
    ev_add("check", ip)
    if not login_free(login):
        return {"ok": False, "msg": "Этот адрес уже занят, попробуйте другой"}
    return {"ok": True}


# ----------------------------------------------------------------- «Забыли пароль?»


def mask_email(e):
    local, _, dom = e.partition("@")
    return (local[:1] + "***" if len(local) > 1 else "***") + "@" + dom


def reset_row(rid):
    if not rid or not isinstance(rid, str):
        return None
    r = q("SELECT * FROM resets WHERE rid=?", (rid,), one=True)
    if not r or r["done"] or time.time() - r["created"] > CFG["limits"]["code_ttl"]:
        return None
    return r


def do_forgot(ip, b):
    """Шаг 1: адрес + капча → код в Telegram (или на резервную почту)."""
    lim = CFG["limits"]
    login = str(b.get("login") or "").strip().lower()
    if login.endswith("@" + DOMAIN):
        login = login[: -len(DOMAIN) - 1]
    if not login or not LOGIN_RE.match(login):
        raise Err("Проверьте адрес почты")
    if ev_count("rs_ip", ip, 86400) >= lim["reset_per_ip_day"]:
        raise Err("Слишком много попыток. Попробуйте завтра.", 429)
    if not check_captcha(b.get("captcha_id"), b.get("captcha")):
        raise Err("Неверные цифры с картинки, попробуйте ещё раз", 400, {"captcha": True})
    ev_add("rs_ip", ip)
    addr = f"{login}@{DOMAIN}"
    acc = q("SELECT * FROM accounts WHERE address=?", (addr,), one=True)
    if not acc or (not acc["tg_chat"] and not acc["ext_email"]):
        if acc or mailbox_exists(addr):
            raise Err("К этому ящику не привязан ни Telegram, ни резервная почта, поэтому сменить пароль "
                      "самостоятельно нельзя. Напишите в поддержку — поможем.", 404, {"support": True})
        raise Err("Такого ящика нет. Проверьте адрес.", 404)
    if ev_count("rs_addr", addr, 3600) >= lim["reset_per_addr_hour"]:
        raise Err("Код для этого ящика уже отправляли несколько раз. Попробуйте через час.", 429)

    code = "%06d" % secrets.randbelow(1000000)
    rid = secrets.token_urlsafe(24)
    method, to = None, None
    if acc["tg_chat"]:
        ok = tg_send(acc["tg_chat"],
                     f"Код для смены пароля от ящика {addr}: {code}\n\n"
                     "Введите его на странице восстановления. Код действует 15 минут. "
                     "Никому его не сообщайте. Если это были не вы — просто ничего не делайте.\n\n"
                     f"Құпиясөзді өзгерту коды: {code}. Кодты ешкімге айтпаңыз.", NO_KB)
        if ok:
            method, to = "tg", "@" + CFG["bot_name"]
    if not method and acc["ext_email"]:
        try:
            send_mail(acc["ext_email"], f"Код для смены пароля Хат Почты: {code}", reset_letter(code, addr))
            method, to = "email", mask_email(acc["ext_email"])
        except Exception as e:
            log("smtp error (reset):", type(e).__name__, str(e)[:200])
    if not method:
        if acc["tg_chat"]:
            raise Err("Не получилось отправить код в Telegram. Откройте бота @" + CFG["bot_name"] +
                      ", нажмите «Запустить» (или «Разблокировать») и попробуйте ещё раз.", 502)
        raise Err("Не получилось отправить письмо с кодом. Попробуйте позже.", 502)

    ev_add("rs_addr", addr)
    q("INSERT INTO resets(rid,created,ip,address,method,code_hash) VALUES(?,?,?,?,?,?)",
      (rid, int(time.time()), ip, addr, method, H("reset", rid, code)))
    log("код восстановления отправлен", addr, "через", method)
    return {"rid": rid, "method": method, "to": to, "address": addr}


RESET_LOCK = threading.Lock()


def do_reset(ip, b):
    """Шаг 2: код + новый пароль → пароль меняется в mailcow."""
    lim = CFG["limits"]
    with RESET_LOCK:
        r = reset_row(b.get("rid"))
        if not r:
            raise Err("Код устарел. Запросите новый.", 410)
        if r["tries"] >= lim["code_tries"]:
            raise Err("Слишком много неверных попыток. Запросите новый код.", 429)
        pe = password_error(b.get("password"))
        if pe:
            raise Err(pe)
        code = re.sub(r"\D", "", str(b.get("code") or ""))
        q("UPDATE resets SET tries=tries+1 WHERE rid=?", (r["rid"],))
        if not hmac.compare_digest(r["code_hash"], H("reset", r["rid"], code)):
            left = lim["code_tries"] - r["tries"] - 1
            raise Err("Неверный код" + (f" (осталось попыток: {left})" if left > 0 else ". Запросите новый код."))
        addr = r["address"]
        ok, why = set_mailbox_password(addr, b["password"])
        if not ok:
            log("reset password failed:", addr, why[:300])
            if "password" in why.lower():
                raise Err("Сервер не принял пароль: сделайте его сложнее", 400)
            raise Err("Не удалось сменить пароль. Попробуйте позже или напишите в поддержку.", 502)
        # этот и все другие коды для ящика больше не действуют
        q("UPDATE resets SET done=1 WHERE address=?", (addr,))
        log("пароль изменён через восстановление", addr, "способ", r["method"])
    acc = q("SELECT tg_chat FROM accounts WHERE address=?", (addr,), one=True)
    if acc and acc["tg_chat"]:
        tg_send(acc["tg_chat"], f"Пароль от ящика {addr} изменён. Если это были не вы — "
                                "сразу напишите в поддержку: https://kazxat.kz/auth.html#support", NO_KB)
    return {"ok": True, "address": addr}


# ----------------------------------------------------------------- поддержка


def clean_text(s, n):
    s = str(s or "").replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", s)
    return re.sub(r"\n{4,}", "\n\n\n", s).strip()[:n]


def do_support(ip, b):
    lim = CFG["limits"]
    name = clean_name(b.get("name"))
    email = str(b.get("email") or "").strip()
    box = str(b.get("box") or "").strip().lower()
    topic = b.get("topic") if b.get("topic") in SUPPORT_TOPICS else "Другое"
    text = clean_text(b.get("text"), 3000)
    if not name:
        raise Err("Укажите, как к вам обращаться")
    if not EMAIL_RE.match(email):
        raise Err("Укажите почту, на которую вам ответить")
    if box and not re.fullmatch(r"[a-z0-9._-]{1,30}(@" + re.escape(DOMAIN) + ")?", box):
        raise Err("Проверьте адрес вашего ящика @kazxat.kz")
    if box and "@" not in box:
        box += "@" + DOMAIN
    if len(text) < 10:
        raise Err("Опишите вопрос подробнее (хотя бы пару слов)")
    if ev_count("sup_ip", ip, 86400) >= lim["support_per_ip_day"]:
        raise Err("Вы уже отправили несколько сообщений сегодня. Мы ответим на них — подождите, пожалуйста.", 429)
    if not check_captcha(b.get("captcha_id"), b.get("captcha")):
        raise Err("Неверные цифры с картинки, попробуйте ещё раз", 400, {"captcha": True})
    body = (f"Сообщение с формы поддержки kazxat.kz\n\n"
            f"Тема: {topic}\nИмя: {name}\nОтветить на: {email}\n"
            f"Ящик @kazxat.kz: {box or '—'}\n"
            f"Время: {time.strftime('%Y-%m-%d %H:%M')}\nIP: {ip}\n\n"
            f"{text}\n\n— Чтобы ответить, нажмите «Ответить»: письмо уйдёт на {email}\n")
    try:
        send_mail(CFG["support_to"], f"[Поддержка] {topic} — {name}", body, reply_to=email)
    except Exception as e:
        log("smtp error (support):", type(e).__name__, str(e)[:200])
        raise Err("Не удалось отправить сообщение. Попробуйте позже.", 502)
    ev_add("sup_ip", ip)
    log("сообщение в поддержку, тема:", topic)
    return {"ok": True}


class Handler(BaseHTTPRequestHandler):
    server_version = "hat-reg"
    sys_version = ""

    def log_message(self, fmt, *a):
        pass  # без журнала запросов: в нём были бы IP и адреса

    def ip(self):
        return (self.headers.get("X-Real-IP") or self.client_address[0]).strip()[:64]

    def reply(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def route(self):
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/api/reg"):
            path = path[len("/api/reg"):]
        return path.strip("/")

    def run(self, fn, *args):
        try:
            self.reply(200, fn(self.ip(), *args))
        except Err as e:
            self.reply(e.code, dict({"error": e.msg}, **e.extra))
        except Exception as e:
            log("internal error:", type(e).__name__, str(e)[:300])
            self.reply(500, {"error": "Ошибка сервера. Попробуйте позже."})

    def do_GET(self):
        r = self.route()
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))
        if r == "ping":
            return self.reply(200, {"ok": True})
        if r == "captcha":
            def cap(ip):
                if ev_count("cap", ip, 3600) > 120:
                    raise Err("Слишком часто", 429)
                ev_add("cap", ip)
                cid, img = make_captcha()
                return {"id": cid, "img": img}
            return self.run(cap)
        if r == "check":
            return self.run(do_check, params)
        if r == "status":
            return self.run(do_status, params)
        self.reply(404, {"error": "not found"})

    def do_POST(self):
        origin = self.headers.get("Origin")
        if origin not in ALLOWED_ORIGINS:
            return self.reply(403, {"error": "Запрос не с сайта kazxat.kz"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > 16000:
            return self.reply(400, {"error": "bad request"})
        try:
            b = json.loads(self.rfile.read(n))
            assert isinstance(b, dict)
        except Exception:
            return self.reply(400, {"error": "bad request"})
        r = self.route()
        fn = {"start": do_start, "code": do_code, "create": do_create,
              "resend": lambda ip, b: send_code(b.get("sid"), ip),
              "forgot": do_forgot, "reset": do_reset, "support": do_support}.get(r)
        if not fn:
            return self.reply(404, {"error": "not found"})
        self.run(fn, b)


def serve():
    db()
    cleanup()
    threading.Thread(target=tg_loop, daemon=True).start()

    def janitor():
        while True:
            time.sleep(3600)
            try:
                cleanup()
            except Exception as e:
                log("cleanup:", e)
    threading.Thread(target=janitor, daemon=True).start()
    srv = ThreadingHTTPServer((CFG["listen"], int(CFG["port"])), Handler)
    srv.daemon_threads = True
    log("hat-reg слушает %s:%s" % (CFG["listen"], CFG["port"]))
    srv.serve_forever()


# ----------------------------------------------------------------- команды администратора

def cmd_selftest():
    ok = True
    try:
        me = tg("getMe")
        print("Telegram: OK, бот @%s" % me.get("username"))
    except Exception as e:
        ok = False
        print("Telegram: ОШИБКА", type(e).__name__)
    try:
        d = api("GET", "get/domain/" + DOMAIN)
        print("API mailcow: OK, ящиков %s из %s" % (d.get("mboxes_in_domain"), d.get("max_num_mboxes_for_domain")))
    except Exception as e:
        ok = False
        print("API mailcow: ОШИБКА", type(e).__name__, str(e)[:120])
    if CFG.get("smtp_pass"):
        try:
            with smtplib.SMTP(CFG["smtp_host"], CFG["smtp_port"], timeout=20) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(CFG["smtp_user"], CFG["smtp_pass"])
            print("SMTP %s: OK" % CFG["smtp_user"])
        except Exception as e:
            ok = False
            print("SMTP: ОШИБКА", type(e).__name__)
    else:
        print("SMTP: ящик noreply ещё не создан (create-noreply)")
    try:
        sup = CFG["support_to"]
        if mailbox_exists(sup) or alias_exists(sup):
            print("Поддержка: письма уходят на %s — OK" % sup)
        else:
            ok = False
            print("Поддержка: адреса %s нет. Запустите: hat_reg.py support-alias ВАШ_ЯЩИК" % sup)
    except Exception as e:
        ok = False
        print("Поддержка: ОШИБКА проверки", type(e).__name__)
    return 0 if ok else 1


def cmd_support_alias(target=None):
    sup = CFG["support_to"]
    if not target or not EMAIL_RE.match(target):
        print("Укажите ящик, куда пересылать письма поддержки, например: support-alias zharkyn@kazxat.kz")
        return 2
    if mailbox_exists(sup):
        print("%s — это отдельный ящик, письма поддержки приходят в него. Ничего не меняю." % sup)
        return 0
    if alias_exists(sup):
        print("Пересылка %s уже есть. Ничего не меняю." % sup)
        return 0
    ok, why = api_ok(api("POST", "add/alias", {"address": sup, "goto": target, "active": "1"}))
    print(("Готово: письма на %s теперь приходят в %s" % (sup, target)) if ok else ("Ошибка: " + why))
    return 0 if ok else 1


def cmd_create_noreply():
    local, addr = "noreply", "noreply@" + DOMAIN
    if CFG.get("smtp_pass") and mailbox_exists(addr):
        print("noreply@ уже есть, пароль в config.json")
        return 0
    if mailbox_exists(addr):
        print("Ящик noreply@ уже существует, но пароля в config.json нет. Задайте его в mailcow и впишите smtp_pass вручную.")
        return 1
    pw = secrets.token_urlsafe(18) + "9a"
    ok, why = add_mailbox(local, "Хат Почта", pw, 100)
    if not ok:
        print("Не получилось:", why)
        return 1
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["smtp_pass"] = pw
    tmp = CONFIG_PATH + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CONFIG_PATH)
    print("Создан noreply@kazxat.kz (квота 100 МБ), пароль записан в config.json")
    return 0


def cmd_domain_limits(n=None):
    d = api("GET", "get/domain/" + DOMAIN)
    print("Сейчас: ящиков %s из %s; квота на ящик %s МиБ; общая квота домена %s МиБ"
          % (d.get("mboxes_in_domain"), d.get("max_num_mboxes_for_domain"),
             int(d.get("max_quota_for_mbox", 0)) // 1048576, int(d.get("max_quota_for_domain", 0)) // 1048576))
    if n is None:
        return 0
    n = int(n)
    attr = {"mailboxes": str(n), "maxquota": str(CFG["quota_mb"]),
            "quota": str(n * CFG["quota_mb"]), "defquota": str(CFG["quota_mb"])}
    ok, why = api_ok(api("POST", "edit/domain", {"items": [DOMAIN], "attr": attr}))
    print("Изменено" if ok else "Ошибка: " + why)
    return cmd_domain_limits() if ok else 1


def main():
    global CFG
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    CFG = load_config()
    c = sys.argv[1]
    if c == "serve":
        serve()
    elif c == "selftest":
        return cmd_selftest()
    elif c == "create-noreply":
        return cmd_create_noreply()
    elif c == "domain-limits":
        return cmd_domain_limits(sys.argv[2] if len(sys.argv) > 2 else None)
    elif c == "support-alias":
        return cmd_support_alias(sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        print(__doc__)
        return 2


if __name__ == "__main__":
    sys.exit(main() or 0)
