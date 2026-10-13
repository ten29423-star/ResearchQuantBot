"""
Research alerts bot.

Что делает:
  - раз в запуск обходит источники, находит новые статьи и шлёт их в Telegram;
  - при первом запуске (и с флагом --check) присылает отчёт: какие источники
    работают, у каких нет ленты, какие не отвечают.

Запуск:
  python bot.py            обычный запуск
  python bot.py --check    только проверка источников + отчёт (ничего не запоминает)

Переменные окружения: BOT_TOKEN, CHAT_ID.
Без них бот ничего не отправляет, а печатает всё в консоль (удобно для теста).
"""
import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import feedparser
import requests

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

STATE_FILE = Path(__file__).parent / "state.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; research-alerts-bot)"}
TIMEOUT = 20
FAIL_ALERT_AFTER = 24     # сколько запусков подряд источник молчит, прежде чем бот напишет
DIGEST_THRESHOLD = 8      # если новых больше — шлём одним сообщением-дайджестом
SEEN_CAP = 5000           # сколько ссылок помнить на источник
WEEKLY_SUMMARY_EVERY = 7 * 24 * 3600  # как часто слать сводку «бот жив» (секунд)
GUESS_SUFFIXES = ["feed", "feed.xml", "rss", "rss.xml", "index.xml", "atom.xml"]

# Источник:
#   name    — как показывать в сообщениях
#   page    — обычная страница (по ней ищем ленту автоматически)
#   feeds   — известные адреса лент (пробуем первыми)
#   sitemap — для сайтов без ленты: {"url": ..., "contains": [подстроки в нужных ссылках]}
SOURCES = [
    # --- AI-компании ---
    {"name": "OpenAI", "page": "https://openai.com/news", "feeds": ["https://openai.com/news/rss.xml"]},
    {"name": "Google DeepMind", "page": "https://deepmind.google/research", "feeds": ["https://deepmind.google/blog/rss.xml"]},
    {"name": "Google Research", "page": "https://research.google/blog", "feeds": ["https://research.google/blog/rss/"]},
    {"name": "Microsoft Research", "page": "https://www.microsoft.com/en-us/research/blog/", "feeds": ["https://www.microsoft.com/en-us/research/feed/"]},
    {"name": "NVIDIA Research", "page": "https://blogs.nvidia.com/blog/tag/nvidia-research/", "feeds": ["https://blogs.nvidia.com/blog/tag/nvidia-research/feed/"]},
    {"name": "Apple ML Research", "page": "https://machinelearning.apple.com"},
    {"name": "Amazon Science", "page": "https://www.amazon.science", "feeds": ["https://www.amazon.science/index.rss"]},
    {"name": "IBM Research", "page": "https://research.ibm.com/blog"},
    {"name": "Hugging Face (blog)", "page": "https://huggingface.co/blog", "feeds": ["https://huggingface.co/blog/feed.xml"]},
    {"name": "Anthropic", "page": "https://www.anthropic.com/research",
     "sitemap": {"url": "https://www.anthropic.com/sitemap.xml", "contains": ["/research/", "/news/", "/engineering/"]}},
    {"name": "Mistral", "page": "https://mistral.ai/news", "feeds": ["https://mistral.ai/news/rss"]},
    {"name": "Allen AI", "page": "https://allenai.org/blog"},
    {"name": "Thinking Machines", "page": "https://thinkingmachines.ai/blog"},
    {"name": "Databricks", "page": "https://www.databricks.com/blog", "feeds": ["https://www.databricks.com/feed"]},
    {"name": "BAIR (Berkeley)", "page": "https://bair.berkeley.edu/blog"},
    # --- Solana ---
    {"name": "Solana News", "page": "https://solana.com/news"},
    {"name": "Firedancer (releases)", "page": "https://github.com/firedancer-io/firedancer/releases",
     "feeds": ["https://github.com/firedancer-io/firedancer/releases.atom"]},
    {"name": "Solana SIMD", "page": "https://github.com/solana-foundation/solana-improvement-documents",
     "feeds": ["https://github.com/solana-foundation/solana-improvement-documents/commits/main.atom"]},
    {"name": "Helius", "page": "https://www.helius.dev/blog"},
    # --- Bitcoin ---
    {"name": "Bitcoin Optech", "page": "https://bitcoinops.org", "feeds": ["https://bitcoinops.org/feed.xml"]},
    {"name": "Bitcoin BIPs", "page": "https://github.com/bitcoin/bips",
     "feeds": ["https://github.com/bitcoin/bips/commits/master.atom"]},
    {"name": "Delving Bitcoin", "page": "https://delvingbitcoin.org", "feeds": ["https://delvingbitcoin.org/latest.rss"]},
    # --- Крипто-аналитика ---
    {"name": "a16z crypto", "page": "https://a16zcrypto.com"},
]


# ---------- сеть и разбор ----------

def get(url):
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r


def short(e):
    if isinstance(e, requests.HTTPError) and e.response is not None:
        return f"HTTP {e.response.status_code}"
    if isinstance(e, requests.Timeout):
        return "таймаут"
    if isinstance(e, requests.ConnectionError):
        return "нет соединения"
    return str(e)[:80]


def parse_feed(url):
    feed = feedparser.parse(get(url).content)
    items = [((e.get("title") or "").strip() or "(без названия)", e.link) for e in feed.entries if e.get("link")]
    if not items:
        raise ValueError("не RSS/Atom или лента пустая")
    return items


def parse_sitemap(cfg, depth=0):
    text = get(cfg["url"]).text
    locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", text)
    if "<sitemapindex" in text and depth == 0:  # индекс sitemap'ов — идём на один уровень вглубь
        items = []
        for sub in locs[:5]:
            try:
                items += parse_sitemap({**cfg, "url": sub}, depth=1)
            except Exception:
                pass
        if not items:
            raise ValueError("в sitemap нет подходящих ссылок")
        return items
    locs = [u for u in locs if any(s in u for s in cfg["contains"])]
    if not locs:
        raise ValueError("в sitemap нет подходящих ссылок")
    return [(u.rstrip("/").split("/")[-1].replace("-", " ").title(), u) for u in locs]


def _attr(tag, name):
    m = re.search(rf'{name}\s*=\s*["\']([^"\']+)["\']', tag, re.I)
    return m.group(1) if m else None


def discover(page_url):
    """Ищем <link rel="alternate" type="application/rss+xml"> на странице."""
    html = get(page_url).text
    found = []
    for tag in re.findall(r"<link\b[^>]*>", html, re.I):
        rel, typ, href = _attr(tag, "rel"), _attr(tag, "type"), _attr(tag, "href")
        if href and rel and "alternate" in rel.lower() and typ and ("rss" in typ.lower() or "atom" in typ.lower()):
            found.append(urljoin(page_url, href))
    return found


def normalize(url):
    p = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith("utm_")]
    host = p.netloc.lower().removeprefix("www.")
    return urlunsplit((p.scheme, host, p.path.rstrip("/"), urlencode(q), ""))


# ---------- проверка источника ----------

def resolve(src, cached_feed):
    """Возвращает словарь: ok, items, via, feed, status ('ok'|'no_feed'|'down'), detail."""
    name, page = src["name"], src.get("page")
    errors, tried = [], set()

    def done(items, via, feed=None):
        return {"name": name, "ok": True, "items": items, "via": via, "feed": feed,
                "status": "ok", "detail": "", "page": page}

    def try_feed(url):
        if url in tried:
            return None
        tried.add(url)
        try:
            return parse_feed(url)
        except Exception as e:
            errors.append(f"{url} → {short(e)}")
            return None

    # 1. кэшированная и известные ленты
    for url in ([cached_feed] if cached_feed else []) + src.get("feeds", []):
        items = try_feed(url)
        if items:
            return done(items, "RSS/Atom", url)

    # 2. sitemap
    if "sitemap" in src:
        try:
            return done(parse_sitemap(src["sitemap"]), "sitemap")
        except Exception as e:
            errors.append(f"sitemap → {short(e)}")

    # 3. автопоиск ленты на странице и типовые адреса
    page_ok, page_err = False, None
    if page:
        try:
            for url in discover(page):
                items = try_feed(url)
                if items:
                    return done(items, "RSS/Atom (найдена автоматически)", url)
            page_ok = True
        except Exception as e:
            page_err = short(e)
        origin = "{0.scheme}://{0.netloc}".format(urlsplit(page))
        for base in (page.rstrip("/"), origin):
            for suf in GUESS_SUFFIXES:
                items = try_feed(f"{base}/{suf}")
                if items:
                    return done(items, "RSS/Atom (угадан адрес)", f"{base}/{suf}")

    if page_ok:
        return {"name": name, "ok": False, "items": [], "via": "", "feed": None,
                "status": "no_feed", "detail": "страница открывается, ленты нет", "page": page}
    detail = f"страница: {page_err}" if page_err else "нет страницы"
    if errors:
        detail += f"; {errors[0]}"
    return {"name": name, "ok": False, "items": [], "via": "", "feed": None,
            "status": "down", "detail": detail, "page": page}


# ---------- Telegram ----------

def send(text, preview=True):
    print(text + "\n")
    if not (BOT_TOKEN and CHAT_ID):
        print("[нет BOT_TOKEN/CHAT_ID — в Telegram не отправлено]\n")
        return False
    for attempt in range(2):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": not preview},
                timeout=20,
            )
            if r.status_code == 429 and attempt == 0:
                time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
                continue
            r.raise_for_status()
            return True
        except Exception as e:
            print(f"[!] Telegram: {short(e)}", file=sys.stderr)
            return False
    return False


def send_long(text, preview=False):
    """Режем по строкам на куски до ~3900 символов (лимит Telegram — 4096)."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3900:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    ok = True
    for c in chunks:
        ok = send(c.rstrip(), preview) and ok
        time.sleep(1)
    return ok


def build_fail_alert(name, n, detail, page, last):
    lines = [f"⚠️ {name} не отвечает (запусков подряд: {n}): {detail}", ""]
    if last:
        note = "" if last.get("exact", True) else " (в sitemap порядок не гарантирован, это может быть не самая свежая)"
        lines += [f"Последняя статья, которую бот видел{note}:", last["title"], last["url"]]
    else:
        lines += ["Бот ещё ни разу не получал статей с этого источника."]
    if page:
        lines += ["", f"Страница источника: {page}"]
    return "\n".join(lines)


def build_weekly_summary(state):
    wk = state["weekly"]
    lines = ["📊 Недельная сводка: бот жив",
             f"Проверок за неделю: {wk['runs']} (при запуске раз в 30 минут ожидается около 336)",
             f"Новых материалов отправлено: {wk['sent']}"]
    top = sorted(wk["by_source"].items(), key=lambda kv: kv[1], reverse=True)[:5]
    if top:
        lines.append("Больше всего: " + ", ".join(f"{n} — {c}" for n, c in top))
    failing = [(n, c) for n, c in state["fails"].items() if c > 0]
    if failing:
        lines.append("Сейчас не отвечают: " + ", ".join(f"{n} (запусков подряд: {c})" for n, c in failing))
    else:
        lines.append(f"Все {len(SOURCES)} источников отвечают.")
    return "\n".join(lines)


def maybe_send_weekly(state):
    wk = state["weekly"]
    if time.time() - wk["since"] < WEEKLY_SUMMARY_EVERY:
        return
    if send(build_weekly_summary(state)):
        state["weekly"] = {"since": time.time(), "runs": 0, "sent": 0, "by_source": {}}


def build_report(results):
    ok = [r for r in results if r["ok"]]
    nof = [r for r in results if r["status"] == "no_feed"]
    down = [r for r in results if r["status"] == "down"]
    lines = [f"🔎 Проверка источников: работает {len(ok)}, без ленты {len(nof)}, "
             f"не отвечает {len(down)} (всего {len(results)})"]
    if down:
        lines += ["", "❌ Не отвечают:"] + [f"• {r['name']} — {r['detail']}" for r in down]
    if nof:
        lines += ["", "⚠️ Страница открывается, но ленту найти не удалось:"] + [f"• {r['name']} — {r['page']}" for r in nof]
    if ok:
        lines += ["", "✅ Работают:"] + [f"• {r['name']} — {r['via']}, записей: {len(r['items'])}" for r in ok]
    return "\n".join(lines)


# ---------- состояние ----------

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return None


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1))


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="только проверить источники и прислать отчёт")
    args = ap.parse_args()

    state = load_state()
    first_run = state is None
    state = state or {"seen": {}, "feeds": {}, "fails": {}}
    state.setdefault("last", {})  # последняя известная статья по каждому источнику
    # счётчики для недельной сводки (старый state.json без них тоже работает)
    state.setdefault("weekly", {"since": time.time(), "runs": 0, "sent": 0, "by_source": {}})

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda s: resolve(s, state["feeds"].get(s["name"])), SOURCES))

    if args.check or first_run:
        send_long(build_report(results))
    if args.check:
        return

    pending = []  # (источник, заголовок, ссылка, нормализованная ссылка)
    exact = {}    # источник -> можно ли доверять порядку записей (для sitemap нельзя)
    for r in results:
        name = r["name"]
        if not r["ok"]:
            n = state["fails"].get(name, 0) + 1
            state["fails"][name] = n
            if n == FAIL_ALERT_AFTER:
                send(build_fail_alert(name, n, r["detail"], r.get("page"), state["last"].get(name)))
            continue
        state["fails"][name] = 0
        if r["feed"]:
            state["feeds"][name] = r["feed"]

        current = {}
        for title, url in r["items"]:
            current.setdefault(normalize(url), (title, url))

        exact[name] = r["via"] != "sitemap"
        if name not in state["last"]:
            first_title, first_url = next(iter(current.values()))
            state["last"][name] = {"title": first_title, "url": first_url, "exact": exact[name]}

        if name not in state["seen"]:
            # первая успешная проверка этого источника: запоминаем всё, ничего не шлём
            state["seen"][name] = list(current)
            continue
        seen = set(state["seen"][name])
        for norm, (title, url) in reversed(list(current.items())):  # старые → новые
            if norm not in seen:
                pending.append((name, title, url, norm))

    def mark(item):
        lst = state["seen"][item[0]]
        lst.append(item[3])
        state["seen"][item[0]] = lst[-SEEN_CAP:]
        state["last"][item[0]] = {"title": item[1], "url": item[2], "exact": exact.get(item[0], True)}
        wk = state["weekly"]
        wk["sent"] += 1
        wk["by_source"][item[0]] = wk["by_source"].get(item[0], 0) + 1

    if len(pending) <= DIGEST_THRESHOLD:
        for item in pending:
            if not send(f"🆕 {item[0]}\n{item[1]}\n{item[2]}"):
                break  # не получилось отправить — остальное уйдёт при следующем запуске
            mark(item)
            time.sleep(1)
    else:
        text = f"🆕 Новых материалов: {len(pending)}\n" + "\n".join(f"• {n}: {t}\n  {u}" for n, t, u, _ in pending)
        if send_long(text):
            for item in pending:
                mark(item)

    state["weekly"]["runs"] += 1
    maybe_send_weekly(state)

    save_state(state)
    print(f"Готово. Новых найдено: {len(pending)}")


if __name__ == "__main__":
    main()
