#!/usr/bin/env python3
"""
Сборщик новостей для портала «Хат» (kazxat.kz).

Раз в 15 минут (по расписанию cron) скачивает RSS-ленты казахстанских СМИ,
берёт заголовок, короткий анонс, картинку, дату и ссылку на статью
и складывает всё в один файл data/news.json рядом с сайтом.
Главная страница читает этот файл и показывает новости.

Только стандартная библиотека Python 3, ничего устанавливать не нужно.
Запуск вручную для проверки:  sudo python3 /opt/hat-site/tools/fetch_news.py
"""

import concurrent.futures as cf
import datetime as dt
import email.utils
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

# ===== НАСТРОЙКИ =====
SITE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # /opt/hat-site
OUT_DIR = os.path.join(SITE_DIR, "data")
OUT_FILE = os.path.join(OUT_DIR, "news.json")
CACHE_FILE = os.path.join(OUT_DIR, "feeds-cache.json")  # какой адрес ленты сработал

PER_SOURCE = 25        # сколько новостей брать с одного сайта
MAX_AGE_DAYS = 3       # новости старше не показываем
MAX_TOTAL = 160        # всего новостей в файле
TEXT_LEN = 220         # длина анонса, символов
TIMEOUT = 15           # секунд на один запрос
USER_AGENT = "Mozilla/5.0 (compatible; HatNewsBot/1.0; +https://kazxat.kz)"

# Источники. Адреса пробуются по порядку; если ни один не подошёл,
# скрипт ищет ссылку на RSS на главной странице сайта (home).
SOURCES = {
    "Kazinform": {
        "home": "https://www.inform.kz/ru",
        "feeds": ["https://www.inform.kz/rss/ru.xml", "https://www.inform.kz/ru/rss",
                  "https://www.inform.kz/rss", "https://www.inform.kz/ru/rss.xml"],
    },
    "Tengrinews": {
        "home": "https://tengrinews.kz",
        "feeds": ["https://tengrinews.kz/news.rss"],
    },
    "24KZ": {
        "home": "https://24.kz/ru",
        "feeds": ["https://24.kz/ru/?format=feed&type=rss", "https://24.kz/ru/rss",
                  "https://24.kz/ru/news?format=feed&type=rss", "https://24.kz/rss"],
    },
    "Bizmedia": {
        "home": "https://bizmedia.kz",
        "feeds": ["https://bizmedia.kz/feed/"],
    },
    "Informburo": {
        "home": "https://informburo.kz",
        "feeds": ["https://informburo.kz/rss/pubsubhubbub.xml", "https://informburo.kz/rss/pubsubhubbub",
                  "https://informburo.kz/rss.xml", "https://informburo.kz/rss", "https://informburo.kz/feed"],
    },
    "Kapital.kz": {
        "home": "https://kapital.kz",
        "feeds": ["https://kapital.kz/rss", "https://kapital.kz/rss.xml", "https://kapital.kz/feed",
                  "https://kapital.kz/yandex.xml"],
    },
    "Atameken Business": {
        "home": "https://inbusiness.kz/ru",
        "feeds": ["https://inbusiness.kz/ru/rss"],
    },
}

NS = {
    "media": "http://search.yahoo.com/mrss/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "atom": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/elements/1.1/",
}


# ===== ЗАГРУЗКА =====
def fetch(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, text/html;q=0.8, */*;q=0.5",
        "Accept-Language": "ru,kk;q=0.9,en;q=0.5",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        data = r.read(8 * 1024 * 1024)  # не больше 8 МБ
        return data, r.geturl()


def looks_like_feed(data):
    head = data[:2000].lstrip().lower()
    return (b"<rss" in head or b"<feed" in head or b"<rdf:rdf" in head) and b"<html" not in head[:200]


def discover(home):
    """Ищет на главной странице ссылку <link rel="alternate" type="application/rss+xml">."""
    data, base = fetch(home)
    text = data.decode("utf-8", "replace")
    found = []
    for tag in re.findall(r"<link\b[^>]*>", text, flags=re.I):
        if re.search(r"rel=[\"']?alternate", tag, re.I) and re.search(r"(rss|atom)\+xml", tag, re.I):
            m = re.search(r"href=[\"']([^\"']+)", tag, re.I)
            if m:
                found.append(urllib.parse.urljoin(base, html.unescape(m.group(1))))
    # запасной вариант: любые ссылки, похожие на RSS
    for m in re.finditer(r"href=[\"']([^\"']*(?:rss|feed)[^\"']*)[\"']", text, flags=re.I):
        u = urllib.parse.urljoin(base, html.unescape(m.group(1)))
        if u not in found and urllib.parse.urlparse(u).netloc.endswith(urllib.parse.urlparse(base).netloc.replace("www.", "")):
            found.append(u)
    return found[:8]


# ===== РАЗБОР ЛЕНТЫ =====
def clean_text(s):
    if not s:
        return ""
    s = re.sub(r"<(script|style)\b.*?</\1>", " ", s, flags=re.I | re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def shorten(s, n=TEXT_LEN):
    if len(s) <= n:
        return s
    cut = s[:n].rsplit(" ", 1)[0].rstrip(",.;:—-– ")
    return cut + "…"


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        d = email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        try:
            d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone(dt.timedelta(hours=5)))  # время Казахстана
    return d.astimezone(dt.timezone.utc)


def safe_url(u):
    u = (u or "").strip()
    return u if u.startswith("https://") or u.startswith("http://") else ""


def first_img(html_text):
    m = re.search(r"<img\b[^>]*\bsrc=[\"']([^\"']+)", html_text or "", re.I)
    return html.unescape(m.group(1)) if m else ""


def txt(el, path):
    x = el.find(path, NS)
    return (x.text or "").strip() if x is not None and x.text else ""


XML_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}


def fix_entities(data):
    """Заменяет HTML-сущности (&nbsp; &laquo; ...) на числовые, иначе XML-разбор падает."""
    def repl(m):
        name = m.group(1)
        if name in XML_ENTITIES:
            return m.group(0)
        ch = html.unescape("&" + name + ";")
        return f"&#{ord(ch)};" if len(ch) == 1 else ""
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    text = re.sub(r"<\?xml[^>]*\?>", "", text, count=1)  # убираем объявление кодировки, текст уже в UTF-8
    return re.sub(r"&([A-Za-z][A-Za-z0-9]{1,10});", repl, text)


def parse_feed(data, base):
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        root = ET.fromstring(fix_entities(data))
    items = []
    tag = root.tag.lower()
    if tag.endswith("feed"):  # Atom
        for e in root.findall("atom:entry", NS):
            link = ""
            for l in e.findall("atom:link", NS):
                if l.get("rel", "alternate") == "alternate":
                    link = l.get("href", "")
                    break
            summary = txt(e, "atom:summary") or txt(e, "atom:content")
            items.append({
                "title": clean_text(txt(e, "atom:title")),
                "link": urllib.parse.urljoin(base, link),
                "text": clean_text(summary),
                "img": first_img(txt(e, "atom:content")),
                "date": parse_date(txt(e, "atom:updated") or txt(e, "atom:published")),
                "tag": "",
            })
        return items
    for it in root.iter("item"):  # RSS 2.0 / RDF
        desc_raw = txt(it, "description")
        full = txt(it, "content:encoded")
        img = ""
        enc = it.find("enclosure")
        if enc is not None and (enc.get("type", "").startswith("image") or re.search(r"\.(jpe?g|png|webp|gif)(\?|$)", enc.get("url", ""), re.I)):
            img = enc.get("url", "")
        if not img:
            for p in ("media:content", "media:thumbnail", "media:group/media:content", "media:group/media:thumbnail"):
                m = it.find(p, NS)
                if m is not None and m.get("url") and m.get("medium", "image") == "image":
                    img = m.get("url")
                    break
        if not img:
            # нестандартный вариант Kazinform: <item><image><url>…</url></image>
            iu = it.find("image/url")
            if iu is not None and iu.text:
                img = iu.text.strip()
        if not img:
            img = first_img(desc_raw) or first_img(full)
        cat = it.find("category")
        items.append({
            "title": clean_text(txt(it, "title")),
            "link": urllib.parse.urljoin(base, txt(it, "link") or txt(it, "guid")),
            "text": clean_text(desc_raw) or clean_text(full),
            "img": urllib.parse.urljoin(base, html.unescape(img)) if img else "",
            "date": parse_date(txt(it, "pubDate") or txt(it, "dc:date")),
            "tag": clean_text(cat.text) if cat is not None and cat.text else "",
        })
    return items


# ===== ОДИН ИСТОЧНИК =====
def load_source(name, cfg, cache):
    tried = []
    candidates = []
    if cache.get(name):
        candidates.append(cache[name])
    candidates += [u for u in cfg["feeds"] if u not in candidates]

    def try_url(u):
        tried.append(u)
        data, final = fetch(u)
        if not looks_like_feed(data):
            raise ValueError("не RSS")
        items = parse_feed(data, final)
        if not items:
            raise ValueError("лента пустая")
        return items

    last_err = ""
    for u in candidates:
        try:
            return u, try_url(u), ""
        except Exception as e:  # noqa: BLE001
            last_err = f"{u}: {e}"
    # автопоиск на главной
    try:
        for u in discover(cfg["home"]):
            if u in tried:
                continue
            try:
                return u, try_url(u), ""
            except Exception as e:  # noqa: BLE001
                last_err = f"{u}: {e}"
    except Exception as e:  # noqa: BLE001
        last_err = f"главная {cfg['home']}: {e}"
    return "", [], last_err or "лента не найдена"


# ===== ГЛАВНОЕ =====
def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}

    now = dt.datetime.now(dt.timezone.utc)
    oldest = now - dt.timedelta(days=MAX_AGE_DAYS)
    status, all_items = {}, []

    with cf.ThreadPoolExecutor(max_workers=7) as pool:
        futures = {pool.submit(load_source, n, c, cache): n for n, c in SOURCES.items()}
        for fut in cf.as_completed(futures):
            name = futures[fut]
            url, items, err = fut.result()
            good = []
            for it in items:
                if not it["title"] or not safe_url(it["link"]):
                    continue
                d = it["date"] or now
                if d < oldest or d > now + dt.timedelta(hours=1):
                    continue
                good.append({
                    "src": name,
                    "title": shorten(it["title"], 200),
                    "text": shorten(it["text"]) if it["text"] != it["title"] else "",
                    "link": safe_url(it["link"]),
                    "img": safe_url(it["img"]),
                    "tag": shorten(it["tag"], 30),
                    "date": d.isoformat(),
                })
            good.sort(key=lambda x: x["date"], reverse=True)
            good = good[:PER_SOURCE]
            all_items += good
            if url:
                cache[name] = url
            status[name] = {"ok": bool(good), "url": url, "count": len(good), "error": err if not good else ""}

    # убираем повторы (одна и та же ссылка или одинаковый заголовок)
    seen, unique = set(), []
    for it in sorted(all_items, key=lambda x: x["date"], reverse=True):
        key1 = it["link"].split("?")[0].rstrip("/")
        key2 = re.sub(r"\W+", "", it["title"].lower())
        if key1 in seen or key2 in seen:
            continue
        seen.update((key1, key2))
        unique.append(it)

    out = {"updated": now.isoformat(), "sources": status, "items": unique[:MAX_TOTAL]}

    # если не скачалось вообще ничего — старый файл не затираем
    if not unique and os.path.exists(OUT_FILE):
        print("Новостей не получено, оставляю прежний news.json", file=sys.stderr)
    else:
        tmp = OUT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
        os.chmod(tmp, 0o644)
        os.replace(tmp, OUT_FILE)  # подмена одним действием, сайт не увидит полупустой файл

    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)

    # отчёт в журнал
    print(now.strftime("%Y-%m-%d %H:%M UTC"), f"всего новостей: {len(unique[:MAX_TOTAL])}")
    for n, s in status.items():
        print(f"  {'OK ' if s['ok'] else 'ERR'} {n}: {s['count']} шт. {s['url'] or ''} {s['error']}")


if __name__ == "__main__":
    main()
