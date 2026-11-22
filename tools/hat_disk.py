#!/usr/bin/env python3
"""Хат Диск — большие файлы в письмах ссылкой (как «Облако» у Mail.ru).

Один процесс, только стандартная библиотека Python:
  * приём файлов из почты Roundcube (до 2 ГБ, 5 ГБ на человека), без посторонних облаков;
  * страница скачивания https://mail.kazxat.kz/disk/f/<id> с датой удаления;
  * файлы удаляются сами через 30 дней;
  * следит за местом на сервере: мало места — предупреждение в Telegram и на почту,
    совсем мало — загрузка временно останавливается.

Кто загружает: только вошедшие в почту @kazxat.kz. Почта (плагин hat_theme) выдаёт
подписанный пропуск на 6 часов, подпись — общим секретом disk_secret.

Настройки и секреты — в /etc/hat-reg/config.json (тот же файл, что у регистрации).

Команды администратора (через sudo):
  hat_disk.py serve        — запуск (так запускает systemd)
  hat_disk.py selftest     — проверка настроек и места на диске
  hat_disk.py admin-tg [N] — куда слать предупреждения в Telegram (берёт из ящиков, подтверждённых ботом)
  hat_disk.py alert-test   — прислать пробное предупреждение
  hat_disk.py usage        — кто сколько занял
"""

import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import shutil
import smtplib
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
PUBLIC = "https://mail.kazxat.kz/disk"
ALLOWED_ORIGINS = {"https://mail.kazxat.kz"}
REG_DB_PATHS = ["/var/lib/hat-reg/reg.db", "/var/lib/private/hat-reg/reg.db"]

MB = 1024 * 1024
GB = 1024 * MB
CHUNK = MB

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
    cfg.setdefault("disk_listen", "172.22.1.1")
    cfg.setdefault("disk_port", 8791)
    cfg.setdefault("disk_max_file_mb", 2048)      # один файл — до 2 ГБ
    cfg.setdefault("disk_quota_mb", 5120)         # на человека — до 5 ГБ одновременно
    cfg.setdefault("disk_days", 30)               # хранится 30 дней
    cfg.setdefault("disk_reserve_gb", 5)          # меньше этого свободного места — загрузку не принимаем
    cfg.setdefault("disk_alert_gb", 10)           # меньше этого — предупреждаем администратора
    cfg.setdefault("support_to", "support@" + DOMAIN)
    cfg.setdefault("smtp_host", "mail." + DOMAIN)
    cfg.setdefault("smtp_port", 587)
    cfg.setdefault("smtp_user", "noreply@" + DOMAIN)
    cfg.setdefault("disk_dir", os.environ.get("STATE_DIRECTORY", "/var/lib/hat-disk"))
    return cfg


CFG = None


def max_file():
    return int(CFG["disk_max_file_mb"]) * MB


def quota():
    return int(CFG["disk_quota_mb"]) * MB


def files_dir():
    return os.path.join(CFG["disk_dir"], "files")


# ----------------------------------------------------------------- база

DB_LOCK = threading.Lock()
_db = None


def db():
    global _db
    if _db is None:
        os.makedirs(files_dir(), exist_ok=True)
        _db = sqlite3.connect(os.path.join(CFG["disk_dir"], "disk.db"), check_same_thread=False, isolation_level=None)
        _db.row_factory = sqlite3.Row
        _db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS files(
          id TEXT PRIMARY KEY, owner TEXT, name TEXT, size INT, created INT, expires INT,
          downloads INT DEFAULT 0, deleted INT DEFAULT 0);
        CREATE INDEX IF NOT EXISTS f_owner ON files(owner, deleted);
        CREATE INDEX IF NOT EXISTS f_exp ON files(deleted, expires);
        CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
        """)
    return _db


def q(sql, args=(), one=False):
    with DB_LOCK:
        rows = db().execute(sql, args).fetchall()
    return (rows[0] if rows else None) if one else rows


def kv_get(k):
    r = q("SELECT v FROM kv WHERE k=?", (k,), one=True)
    return r["v"] if r else None


def kv_set(k, v):
    q("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, str(v)))


# ----------------------------------------------------------------- пропуск из почты

def b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def check_token(tok):
    """Пропуск выдаёт плагин hat_theme: base64url("адрес|срок") + "." + HMAC-SHA256."""
    secret = CFG.get("disk_secret")
    if not secret or not tok or "." not in tok:
        return None
    p64, sig = tok.rsplit(".", 1)
    try:
        payload = b64d(p64).decode()
    except Exception:
        return None
    good = hmac.new(secret.encode(), ("disk|" + payload).encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(good, sig):
        return None
    user, _, exp = payload.rpartition("|")
    try:
        if int(exp) < time.time():
            return None
    except ValueError:
        return None
    user = user.strip().lower()
    if not re.fullmatch(r"[a-z0-9._-]{1,64}@" + re.escape(DOMAIN), user):
        return None
    return user


# ----------------------------------------------------------------- место на диске и предупреждения

def free_bytes():
    return shutil.disk_usage(CFG["disk_dir"]).free


def used_by(owner):
    r = q("SELECT COALESCE(SUM(size),0) s FROM files WHERE owner=? AND deleted=0", (owner,), one=True)
    return int(r["s"])


def fmt_size(n):
    if n <= 0:
        return "0 МБ"
    if n >= GB:
        return ("%.1f ГБ" % (n / GB)).replace(".", ",").replace(",0 ", " ")
    if n >= MB:
        return "%d МБ" % max(1, round(n / MB))
    return "%d КБ" % max(1, round(n / 1024))


def tg_send(chat, text):
    data = json.dumps({"chat_id": chat, "text": text, "disable_web_page_preview": True}).encode()
    req = urllib.request.Request("https://api.telegram.org/bot%s/sendMessage" % CFG["bot_token"], data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        if not json.loads(r.read()).get("ok"):
            raise RuntimeError("telegram not ok")


def send_mail(to, subject, text):
    msg = EmailMessage()
    msg["From"] = formataddr(("Хат Диск", CFG["smtp_user"]))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=DOMAIN)
    msg.set_content(text)
    with smtplib.SMTP(CFG["smtp_host"], CFG["smtp_port"], timeout=25) as s:
        s.starttls(context=ssl.create_default_context())
        s.login(CFG["smtp_user"], CFG["smtp_pass"])
        s.send_message(msg)


def alert(kind, text, every=12 * 3600, force=False):
    """Предупредить администратора в Telegram и письмом (не чаще раза в 12 часов на один вид)."""
    last = kv_get("alert_" + kind)
    if not force and last and time.time() - float(last) < every:
        return False
    kv_set("alert_" + kind, time.time())
    full = "⚠️ Хат Диск (kazxat.kz)\n\n" + text
    sent = []
    if CFG.get("admin_tg_chat") and CFG.get("bot_token"):
        try:
            tg_send(CFG["admin_tg_chat"], full)
            sent.append("telegram")
        except Exception as e:
            log("alert telegram error:", type(e).__name__)
    if CFG.get("smtp_pass"):
        try:
            send_mail(CFG["support_to"], "⚠️ Хат Диск: " + text.split("\n")[0][:80], full)
            sent.append("почта")
        except Exception as e:
            log("alert mail error:", type(e).__name__)
    log("предупреждение", kind, "→", ", ".join(sent) or "никуда (не настроено)")
    return bool(sent)


def check_space():
    free = free_bytes()
    total = shutil.disk_usage(CFG["disk_dir"]).total
    if free < CFG["disk_alert_gb"] * GB:
        alert("low_space",
              "На сервере мало места: свободно %s из %s.\n"
              "Когда свободного станет меньше %d ГБ, загрузка новых файлов на Хат Диск остановится "
              "(почта при этом продолжит работать). Освободите место или увеличьте диск в PS.kz."
              % (fmt_size(free), fmt_size(total), CFG["disk_reserve_gb"]))
    return free


# ----------------------------------------------------------------- файлы

UPLOAD_LOCK = threading.Lock()
# Загрузка в два шага: /api/start проверяет лимиты и бронирует место (короткий запрос —
# отказ браузер видит сразу), затем /api/upload принимает сам файл по номеру брони.
RESERVED = {}         # uid -> {"owner", "name", "size", "at", "busy"}
RESERVE_TTL = 2 * 3600


def reserved_bytes(owner=None):
    now = time.time()
    for k in [k for k, r in RESERVED.items() if not r["busy"] and now - r["at"] > RESERVE_TTL]:
        RESERVED.pop(k, None)
    return sum(r["size"] for r in RESERVED.values() if owner is None or r["owner"] == owner)


def clean_filename(name):
    name = urllib.parse.unquote(name or "")
    name = name.replace("\\", "/").split("/")[-1]
    name = re.sub(r"[\x00-\x1f\x7f<>:\"|?*]", "", name).strip(" .")
    if len(name) > 180:
        root, dot, ext = name.rpartition(".")
        name = (root[:170] + "." + ext[:9]) if dot and len(ext) <= 9 else name[:180]
    return name or "file"


def file_path(fid):
    return os.path.join(files_dir(), fid)


def delete_file(row):
    try:
        os.remove(file_path(row["id"]))
    except FileNotFoundError:
        pass
    q("UPDATE files SET deleted=1 WHERE id=?", (row["id"],))


def cleanup():
    now = int(time.time())
    n = 0
    for r in q("SELECT id FROM files WHERE deleted=0 AND expires<?", (now,)):
        delete_file(r)
        n += 1
    # недогруженные куски старше суток
    for fn in os.listdir(files_dir()):
        p = os.path.join(files_dir(), fn)
        if fn.endswith(".part") and now - os.path.getmtime(p) > 86400:
            os.remove(p)
    # записи об удалённых файлах храним ещё 60 дней (чтобы ссылка показывала «срок истёк»)
    q("DELETE FROM files WHERE deleted=1 AND expires<?", (now - 60 * 86400,))
    if n:
        log("удалено просроченных файлов:", n)


def janitor():
    while True:
        try:
            cleanup()
            check_space()
        except Exception as e:
            log("janitor:", type(e).__name__, str(e)[:200])
        time.sleep(1800)


# ----------------------------------------------------------------- страницы

PAGE = """<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title} — Хат Диск</title>
<style>
*{{box-sizing:border-box;margin:0}}
body{{font-family:-apple-system,"Segoe UI",Roboto,Arial,sans-serif;background:#f2f6f8;color:#1d2a30;min-height:100vh;display:flex;flex-direction:column}}
header{{background:linear-gradient(90deg,#06566a,#00AFCA);padding:14px 16px}}
header a{{color:#FEC50C;font-weight:800;font-size:26px;text-decoration:none}}
header small{{color:#e6f7fb;font-size:13px;font-weight:600;margin-left:8px}}
main{{flex:1;display:flex;align-items:flex-start;justify-content:center;padding:40px 16px}}
.card{{background:#fff;border-radius:16px;box-shadow:0 6px 24px rgba(6,86,106,.10);padding:32px;max-width:520px;width:100%;text-align:center}}
.ico{{width:72px;height:72px;border-radius:18px;background:#e3f6fa;display:grid;place-items:center;font-size:36px;margin:0 auto 16px}}
h1{{font-size:21px;word-break:break-word;margin-bottom:6px}}
.meta{{color:#6b7c85;font-size:14px;margin-bottom:20px;line-height:1.5}}
.btn{{display:block;background:#00AFCA;color:#fff;text-decoration:none;font-weight:700;font-size:16px;border-radius:10px;padding:14px}}
.btn:hover{{background:#0089a0}}
.warn{{background:#fff8dc;color:#7a5a00;border-radius:10px;padding:10px 12px;font-size:13px;margin-top:16px;line-height:1.45}}
.gone{{background:#fdecec;color:#a42a2a}}
footer{{color:#6b7c85;font-size:12px;text-align:center;padding:16px}}
footer a{{color:#0089a0}}
</style></head><body>
<header><a href="https://kazxat.kz/">Хат</a><small>Диск · @kazxat.kz</small></header>
<main><div class="card">{body}</div></main>
<footer>Файлы на Хат Диске хранятся на серверах в Казахстане и удаляются через {days} дней. · <a href="https://kazxat.kz/auth.html#support">Пожаловаться на файл</a></footer>
</body></html>"""


def ru_date(ts):
    return time.strftime("%d.%m.%Y", time.localtime(ts))


def page(title, body):
    return PAGE.format(title=html.escape(title), body=body, days=CFG["disk_days"]).encode()


def file_page(row):
    e = html.escape
    left = max(0, int(-(-(row["expires"] - time.time()) // 86400)))   # округление вверх
    body = (f'<div class="ico">📎</div><h1>{e(row["name"])}</h1>'
            f'<div class="meta">{e(fmt_size(row["size"]))} · отправил {e(row["owner"])}</div>'
            f'<a class="btn" href="/disk/f/{e(row["id"])}/download">Скачать</a>'
            f'<div class="warn">Файл хранится до <b>{ru_date(row["expires"])}</b>'
            f'{" (осталось дней: %d)" % left if left > 1 else " (последний день)"}, '
            f'потом удалится автоматически. Скачайте его заранее.</div>')
    return page(row["name"], body)


def gone_page():
    body = ('<div class="ico">🗑️</div><h1>Файл больше недоступен</h1>'
            f'<div class="warn gone">Срок хранения ({CFG["disk_days"]} дней) истёк или отправитель удалил файл. '
            'Попросите отправителя прислать его ещё раз.</div>')
    return page("Файл недоступен", body)


# ----------------------------------------------------------------- HTTP

class Err(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.msg, self.code = msg, code


class Handler(BaseHTTPRequestHandler):
    server_version = "hat-disk"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        pass

    def ip(self):
        return (self.headers.get("X-Real-IP") or self.client_address[0]).strip()[:64]

    def path_only(self):
        p = urllib.parse.urlsplit(self.path).path
        return p[len("/disk"):] if p.startswith("/disk") else p

    def send_body(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, code, obj):
        self.send_body(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def html_page(self, code, body):
        self.send_body(code, body, "text/html; charset=utf-8", {
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
            "X-Frame-Options": "DENY"})

    def user(self):
        u = check_token(self.headers.get("X-Hat-Token"))
        if not u:
            raise Err("Войдите в почту заново", 401)
        return u

    def drain(self):
        """Дочитать тело запроса, если мы отказали заранее (иначе браузер видит обрыв)."""
        if getattr(self, "body_read", False):
            return
        self.body_read = True
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if 0 < n <= 64 * 1024:
            self.rfile.read(n)
        else:
            self.close_connection = True

    # --- GET
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        p = self.path_only()
        try:
            if p == "/ping":
                return self.json(200, {"ok": True})
            if p == "/api/info":
                u = self.user()
                rows = q("SELECT id,name,size,created,expires,downloads FROM files WHERE owner=? AND deleted=0 "
                         "ORDER BY created DESC", (u,))
                return self.json(200, {
                    "user": u, "used": used_by(u), "quota": quota(), "max_file": max_file(),
                    "days": CFG["disk_days"],
                    "files": [dict(r, url="%s/f/%s" % (PUBLIC, r["id"])) for r in rows]})
            m = re.fullmatch(r"/f/([A-Za-z0-9_-]{16,40})(/download)?", p)
            if m:
                row = q("SELECT * FROM files WHERE id=?", (m.group(1),), one=True)
                ok = row and not row["deleted"] and row["expires"] > time.time() and os.path.exists(file_path(row["id"]))
                if not ok:
                    return self.html_page(410 if row else 404, gone_page())
                if not m.group(2):
                    return self.html_page(200, file_page(row))
                return self.download(row)
            self.json(404, {"error": "not found"})
        except Err as e:
            self.json(e.code, {"error": e.msg})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log("GET error:", type(e).__name__, str(e)[:200])
            try:
                self.json(500, {"error": "Ошибка сервера"})
            except Exception:
                pass

    def download(self, row):
        size = row["size"]
        start, end = 0, size - 1
        rng = self.headers.get("Range") or ""
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())
        partial = False
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            partial = True
        name = row["name"]
        ascii_name = re.sub(r'[^A-Za-z0-9._ ()-]', "_", name) or "file"
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", "attachment; filename=\"%s\"; filename*=UTF-8''%s"
                         % (ascii_name, urllib.parse.quote(name, safe="")))
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "private, no-store")
        self.end_headers()
        if self.command == "HEAD":
            return
        if start == 0:
            q("UPDATE files SET downloads=downloads+1 WHERE id=?", (row["id"],))
        left = end - start + 1
        with open(file_path(row["id"]), "rb") as f:
            f.seek(start)
            while left > 0:
                chunk = f.read(min(CHUNK, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    # --- POST
    def do_POST(self):
        self.body_read = False
        p = self.path_only()
        origin = self.headers.get("Origin")
        try:
            if origin and origin not in ALLOWED_ORIGINS:
                raise Err("Запрос не с сайта почты", 403)
            if p == "/api/start":
                return self.start()
            if p == "/api/upload":
                return self.upload()
            if p == "/api/delete":
                u = self.user()
                b = self.read_json()
                row = q("SELECT * FROM files WHERE id=? AND owner=? AND deleted=0", (str(b.get("id")), u), one=True)
                if not row:
                    raise Err("Файл не найден", 404)
                delete_file(row)
                log("удалён владельцем:", row["id"])
                return self.json(200, {"ok": True, "used": used_by(u)})
            self.drain()
            self.json(404, {"error": "not found"})
        except Err as e:
            self.drain()
            self.json(e.code, {"error": e.msg})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log("POST error:", type(e).__name__, str(e)[:200])
            try:
                self.close_connection = True
                self.json(500, {"error": "Ошибка сервера. Попробуйте позже."})
            except Exception:
                pass

    def read_json(self, limit=4096):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > limit:
            raise Err("bad request")
        self.body_read = True
        try:
            b = json.loads(self.rfile.read(n))
            assert isinstance(b, dict)
            return b
        except Exception:
            raise Err("bad request")

    def start(self):
        """Шаг 1: проверить лимиты и забронировать место. Тела файла тут нет — отказ доходит до браузера."""
        u = self.user()
        b = self.read_json()
        try:
            n = int(b.get("size"))
        except (TypeError, ValueError):
            raise Err("bad request")
        if n < 1:
            raise Err("Пустой файл")
        if n > max_file():
            raise Err("Файл больше %s — такой не отправить даже через Хат Диск" % fmt_size(max_file()), 413)
        name = clean_filename(b.get("name"))
        with UPLOAD_LOCK:
            used = used_by(u) + reserved_bytes(u)
            if used + n > quota():
                raise Err("На вашем Хат Диске занято %s из %s. Файлы освобождают место через %d дней после загрузки."
                          % (fmt_size(used), fmt_size(quota()), CFG["disk_days"]), 413)
            free = free_bytes() - reserved_bytes()
            if free - n < CFG["disk_reserve_gb"] * GB:
                alert("full", "Загрузка на Хат Диск остановлена: на сервере свободно всего %s. "
                              "Пользователи сейчас не могут отправлять большие файлы. Освободите место." % fmt_size(free_bytes()),
                      every=3 * 3600)
                raise Err("Хат Диск временно переполнен. Попробуйте позже или отправьте файл меньше.", 507)
            uid = secrets.token_urlsafe(18)
            RESERVED[uid] = {"owner": u, "name": name, "size": n, "at": time.time(), "busy": False}
        return self.json(200, {"uid": uid})

    def upload(self):
        """Шаг 2: принять сам файл по номеру брони."""
        u = self.user()
        uid = self.headers.get("X-Upload-Id") or ""
        try:
            n = int(self.headers.get("Content-Length") or -1)
        except ValueError:
            n = -1
        with UPLOAD_LOCK:
            r = RESERVED.get(uid)
            if not r or r["owner"] != u or r["busy"]:
                raise Err("Загрузка устарела, попробуйте ещё раз", 409)
            if n != r["size"]:
                raise Err("Размер файла не совпадает", 400)
            r["busy"] = True
        self.body_read = True
        name = r["name"]
        fid = secrets.token_urlsafe(18)
        part = file_path(fid) + ".part"
        try:
            left = n
            with open(part, "wb") as f:
                while left > 0:
                    chunk = self.rfile.read(min(CHUNK, left))
                    if not chunk:
                        raise ConnectionResetError("upload interrupted")
                    f.write(chunk)
                    left -= len(chunk)
            os.replace(part, file_path(fid))
            now = int(time.time())
            exp = now + int(CFG["disk_days"]) * 86400
            q("INSERT INTO files(id,owner,name,size,created,expires) VALUES(?,?,?,?,?,?)", (fid, u, name, n, now, exp))
        except BaseException:
            try:
                os.remove(part)
            except FileNotFoundError:
                pass
            self.close_connection = True
            raise
        finally:
            with UPLOAD_LOCK:
                RESERVED.pop(uid, None)
        log("загружен файл", fmt_size(n), "владелец", u)
        try:
            check_space()
        except Exception:
            pass
        self.json(200, {"id": fid, "url": "%s/f/%s" % (PUBLIC, fid), "name": name, "size": n,
                        "expires": exp, "used": used_by(u), "quota": quota()})


def serve():
    if not CFG.get("disk_secret"):
        raise SystemExit("В config.json нет disk_secret — запустите hat-disk-setup.sh install")
    db()
    threading.Thread(target=janitor, daemon=True).start()
    srv = ThreadingHTTPServer((CFG["disk_listen"], int(CFG["disk_port"])), Handler)
    srv.daemon_threads = True
    log("hat-disk слушает %s:%s, файлы в %s" % (CFG["disk_listen"], CFG["disk_port"], files_dir()))
    srv.serve_forever()


# ----------------------------------------------------------------- команды администратора

def save_config_key(k, v):
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg[k] = v
    tmp = CONFIG_PATH + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CONFIG_PATH)


def cmd_selftest():
    ok = True
    print("Секрет пропусков (disk_secret):", "есть" if CFG.get("disk_secret") else "НЕТ")
    ok &= bool(CFG.get("disk_secret"))
    du = shutil.disk_usage(CFG["disk_dir"] if os.path.isdir(CFG["disk_dir"]) else "/")
    print("Диск сервера: свободно %s из %s (стоп загрузки при < %d ГБ, предупреждение при < %d ГБ)"
          % (fmt_size(du.free), fmt_size(du.total), CFG["disk_reserve_gb"], CFG["disk_alert_gb"]))
    print("Лимиты: файл до %s, на человека %s, хранение %d дней"
          % (fmt_size(max_file()), fmt_size(quota()), CFG["disk_days"]))
    print("Предупреждения в Telegram:", "настроены" if CFG.get("admin_tg_chat") else "НЕТ (hat-disk-setup.sh admin)")
    print("Предупреждения на почту:", CFG["support_to"] if CFG.get("smtp_pass") else "НЕТ (нет пароля noreply)")
    return 0 if ok else 1


def reg_accounts_with_tg():
    for p in REG_DB_PATHS:
        if os.path.exists(p):
            con = sqlite3.connect("file:%s?mode=ro" % p, uri=True)
            rows = con.execute("SELECT address, tg_chat FROM accounts WHERE tg_chat IS NOT NULL ORDER BY created").fetchall()
            con.close()
            return rows
    return []


def cmd_admin_tg(choice=None):
    rows = reg_accounts_with_tg()
    if not rows:
        print("Нет ни одного ящика, подтверждённого через Telegram. Зарегистрируйте свой ящик через бота и повторите.")
        return 1
    if choice is None and len(rows) > 1:
        print("Ящики, подтверждённые через Telegram. Выберите свой номером: hat-disk-setup.sh admin N")
        for i, (a, _) in enumerate(rows, 1):
            print("  %d) %s" % (i, a))
        return 2
    idx = 0 if choice is None else int(choice) - 1
    if not 0 <= idx < len(rows):
        print("Нет такого номера")
        return 2
    addr, chat = rows[idx]
    save_config_key("admin_tg_chat", chat)
    CFG["admin_tg_chat"] = chat
    print("Предупреждения Хат Диска будут приходить в Telegram того, кто подтверждал ящик %s" % addr)
    return cmd_alert_test()


def cmd_alert_test():
    if CFG.get("disk_dir") and not os.path.isdir(CFG["disk_dir"]):
        CFG["disk_dir"] = "/tmp"
    global _db
    _db = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
    _db.row_factory = sqlite3.Row
    _db.execute("CREATE TABLE kv(k TEXT PRIMARY KEY, v TEXT)")
    ok = alert("test", "Пробное предупреждение: так будут приходить сообщения, когда на сервере "
                       "заканчивается место. Ничего делать не нужно.", force=True)
    print("Пробное предупреждение отправлено" if ok else "Не удалось отправить предупреждение")
    return 0 if ok else 1


def cmd_usage():
    path = os.path.join(CFG["disk_dir"], "disk.db")
    for p in (path, "/var/lib/private/hat-disk/disk.db"):
        if os.path.exists(p):
            con = sqlite3.connect("file:%s?mode=ro" % p, uri=True)
            rows = con.execute("SELECT owner, COUNT(*), SUM(size) FROM files WHERE deleted=0 GROUP BY owner ORDER BY 3 DESC").fetchall()
            con.close()
            print("Хат Диск: %d человек, всего %s" % (len(rows), fmt_size(sum(r[2] for r in rows))))
            for o, c, s in rows:
                print("  %-36s файлов %3d  %s" % (o, c, fmt_size(s)))
            return 0
    print("Файлов пока нет")
    return 0


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
    elif c == "admin-tg":
        return cmd_admin_tg(sys.argv[2] if len(sys.argv) > 2 else None)
    elif c == "alert-test":
        return cmd_alert_test()
    elif c == "usage":
        return cmd_usage()
    else:
        print(__doc__)
        return 2


if __name__ == "__main__":
    sys.exit(main() or 0)
