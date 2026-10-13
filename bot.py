#!/usr/bin/env python3
"""GitHub new-repo alert bot -> Telegram.

Ищет свежие репозитории по тегам (topics) из config.json и присылает
новые в Telegram. Только стандартная библиотека Python, ничего ставить не надо.

Переменные окружения:
  GITHUB_TOKEN        токен GitHub (в Actions выдаётся автоматически)
  TELEGRAM_BOT_TOKEN  токен бота от @BotFather
  TELEGRAM_CHAT_ID    id чата, куда слать
  DRY_RUN=1           ничего не отправлять и не сохранять, печатать в консоль
  CONFIG_PATH         другой путь к конфигу (для тестов)
"""
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", ROOT / "config.json"))
STATE_PATH = ROOT / "seen.json"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
DRY_RUN = os.environ.get("DRY_RUN") == "1"

SEARCH_PAUSE = 2.5        # секунд между запросами к GitHub (лимит 30/мин)
TG_PAUSE = 1.2            # секунд между сообщениями в Telegram
SEEN_KEEP_DAYS = 10       # сколько хранить уже виденные репо
KEEPALIVE_EVERY_DAYS = 14 # раз в сколько дней обновлять метку в seen.json
TRENDING_KEEP_DAYS = 7    # столько дней не слать повторно репо, которое снова в trending


def log(msg):
    print(msg, flush=True)


def http_json(url, headers=None, data=None, timeout=30):
    req = urllib.request.Request(url, headers=headers or {}, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------- GitHub ----------

def gh_headers():
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "repo-alert-bot",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def search_repos(tag, min_stars, since):
    """Возвращает список репо или None, если запрос не удался."""
    query = f"topic:{tag} stars:>={min_stars} created:>={since}"
    params = urllib.parse.urlencode(
        {"q": query, "sort": "stars", "order": "desc", "per_page": 100}
    )
    url = f"https://api.github.com/search/repositories?{params}"
    headers = gh_headers()

    for attempt in range(3):
        try:
            return http_json(url, headers).get("items", [])
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                wait = int(e.headers.get("Retry-After") or 30 * (attempt + 1))
                log(f"[{tag}] лимит GitHub ({e.code}), жду {min(wait, 90)} с")
                time.sleep(min(wait, 90))
                continue
            log(f"[{tag}] ошибка GitHub {e.code}")
            return None
        except Exception as e:
            log(f"[{tag}] сетевая ошибка: {type(e).__name__}")
            time.sleep(3)
    return None


def collect(config, since):
    """Ищет по всем тегам, склеивает дубли по id репо."""
    found = {}
    total = failed = 0
    for cat, spec in config["categories"].items():
        for tag in spec["tags"]:
            total += 1
            items = search_repos(tag, spec["min_stars"], since)
            time.sleep(SEARCH_PAUSE)
            if items is None:
                failed += 1
                continue
            log(f"[{cat}/{tag}] найдено: {len(items)}")
            for repo in items:
                entry = found.setdefault(repo["id"], {"repo": repo, "cats": []})
                if cat not in entry["cats"]:
                    entry["cats"].append(cat)
    return found, total, failed


# ---------- Telegram ----------

def format_message(entry):
    r = entry["repo"]
    cats = " ".join("#" + c.replace("-", "_") for c in entry["cats"])
    desc = (r.get("description") or "без описания").strip()
    if len(desc) > 300:
        desc = desc[:297] + "..."
    lang = r.get("language") or "—"
    created = r["created_at"][:10]
    return (
        f"{html.escape(cats)}\n"
        f'<a href="{html.escape(r["html_url"])}"><b>{html.escape(r["full_name"])}</b></a>\n'
        f"{html.escape(desc)}\n\n"
        f"⭐ {r['stargazers_count']} · 🍴 {r['forks_count']} · {html.escape(lang)} · {created}"
    )


def send_telegram(text):
    if DRY_RUN:
        log("---- (dry run) сообщение ----\n" + text + "\n")
        return True
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = json.dumps(
        {"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"}
    ).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    for _ in range(3):
        try:
            http_json(url, headers, payload)
            return True
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code == 429:
                try:
                    wait = json.loads(body)["parameters"]["retry_after"]
                except Exception:
                    wait = 5
                time.sleep(min(wait + 1, 60))
                continue
            log(f"Telegram ошибка {e.code}: {body[:200]}")
            return False
        except Exception as e:
            log(f"Telegram сетевая ошибка: {type(e).__name__}")
            time.sleep(3)
    return False


# ---------- Trending ----------
# Официального API у GitHub Trending нет, поэтому читаем HTML страницы.
# Если GitHub поменяет вёрстку, парсер перестанет находить репо:
# бот напишет об этом в Telegram (не чаще раза в сутки).

PERIOD_LABEL = {"daily": "сегодня", "weekly": "за неделю", "monthly": "за месяц"}


def fetch_trending(period, language):
    """Список {'full_name', 'stars_period'} в порядке рейтинга или None при ошибке."""
    path = "/trending" + (f"/{urllib.parse.quote(language)}" if language else "")
    url = f"https://github.com{path}?since={period}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; repo-alert-bot)",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            page = resp.read().decode("utf-8", "replace")
    except Exception as e:
        log(f"[trending/{period}] не удалось загрузить страницу: {type(e).__name__}")
        return None

    result = []
    for art in re.findall(r'<article class="Box-row">(.*?)</article>', page, re.S):
        m = re.search(r'<h2[^>]*>.*?<a[^>]*href="/([^"/]+/[^"/]+)"', art, re.S)
        if not m:
            continue
        s = re.search(r"([\d,]+)\s+stars\s+(?:today|this week|this month)", art)
        result.append({"full_name": m.group(1), "stars_period": s.group(1) if s else "?"})
    return result


def fetch_repo(full_name):
    """Данные репо из API (даты, описание, звёзды) или None."""
    try:
        return http_json(f"https://api.github.com/repos/{full_name}", gh_headers())
    except Exception as e:
        log(f"[trending] не удалось получить {full_name}: {type(e).__name__}")
        return None


def format_trending(item, repo):
    name = item["full_name"]
    label = PERIOD_LABEL.get(item["period"], item["period"])
    head = f'🔥 #trending\n<a href="https://github.com/{html.escape(name)}"><b>{html.escape(name)}</b></a>\n'
    if not repo:
        return head + f"+{item['stars_period']} звёзд {label}"
    desc = (repo.get("description") or "без описания").strip()
    if len(desc) > 300:
        desc = desc[:297] + "..."
    lang = repo.get("language") or "—"
    return (
        head
        + f"{html.escape(desc)}\n\n"
        + f"⭐ {repo['stargazers_count']} (+{item['stars_period']} {label}) · "
        + f"🍴 {repo['forks_count']} · {html.escape(lang)} · создан {repo['created_at'][:10]}"
    )


def run_trending(config, state, now):
    cfg = config.get("trending") or {}
    if not cfg.get("enabled"):
        return
    seeded = "trending_seen" in state
    tseen = state.setdefault("trending_seen", {})
    max_age = cfg.get("max_repo_age_days")

    items, names, ok = [], set(), 0
    for period in cfg.get("periods", ["daily"]):
        for lang in cfg.get("languages", [""]):
            got = fetch_trending(period, lang)
            time.sleep(1)
            if got is None:
                continue
            ok += 1
            if not got:
                today = now.strftime("%Y-%m-%d")
                log(f"[trending/{period}] на странице не найдено ни одного репо (вёрстка?)")
                if state.get("trending_warned") != today and not DRY_RUN:
                    send_telegram("⚠️ Trending: парсер ничего не нашёл. Возможно, GitHub поменял вёрстку страницы.")
                    state["trending_warned"] = today
            for it in got:
                key = it["full_name"].lower()
                if key not in names:
                    names.add(key)
                    it["period"] = period
                    items.append(it)
    if not ok:
        return

    new = [it for it in items if it["full_name"].lower() not in tseen]
    log(f"[trending] на странице: {len(items)}, новых: {len(new)}")

    if not seeded and not DRY_RUN:
        for it in new:
            tseen[it["full_name"].lower()] = now.isoformat()
        send_telegram(
            f"Trending подключён: {len(new)} репо сейчас в списке, они помечены как просмотренные. "
            "Дальше буду присылать только те, что попадут в trending позже."
        )
        return

    limit = cfg.get("max_messages_per_run", 5)
    for it in new[:limit]:
        key = it["full_name"].lower()
        repo = fetch_repo(it["full_name"])
        if repo and max_age:
            created = datetime.fromisoformat(repo["created_at"].replace("Z", "+00:00"))
            if (now - created).days > max_age:
                tseen[key] = now.isoformat()  # старое репо: запоминаем без уведомления
                continue
        if send_telegram(format_trending(it, repo)):
            tseen[key] = now.isoformat()
        time.sleep(0 if DRY_RUN else TG_PAUSE)

    cutoff = now - timedelta(days=TRENDING_KEEP_DAYS)
    for key in list(tseen):
        try:
            if datetime.fromisoformat(tseen[key]) < cutoff:
                del tseen[key]
        except ValueError:
            del tseen[key]


# ---------- Состояние ----------

def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text("utf-8")), False
    return {"seen": {}, "keepalive": ""}, True


def save_state(state):
    STATE_PATH.write_text(
        json.dumps(state, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# ---------- Основная логика ----------

def main():
    if not DRY_RUN and not (TG_TOKEN and TG_CHAT):
        sys.exit("Не заданы TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")

    config = json.loads(CONFIG_PATH.read_text("utf-8"))
    window = config.get("window_days", 2)
    limit = config.get("max_messages_per_run", 10)

    state, first_run = load_state()
    seen = state["seen"]
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=window)).strftime("%Y-%m-%d")

    found, total, failed = collect(config, since)
    if total and failed == total:
        sys.exit("Все запросы к GitHub провалились, состояние не меняю")

    new = [e for rid, e in found.items() if str(rid) not in seen]
    new.sort(key=lambda e: e["repo"]["created_at"])  # старые первыми
    log(f"Всего найдено: {len(found)}, новых: {len(new)}, ошибок запросов: {failed}")

    if first_run and not DRY_RUN:
        # Первый запуск: запоминаем всё найденное, ничего не шлём
        for e in new:
            seen[str(e["repo"]["id"])] = now.isoformat()
        send_telegram(
            f"Бот запущен. За последние {window} дн. найдено {len(new)} репо, "
            "они помечены как просмотренные. Дальше сюда будут приходить только новые."
        )
    else:
        sent = 0
        for e in new[:limit]:
            if send_telegram(format_message(e)):
                seen[str(e["repo"]["id"])] = now.isoformat()
                sent += 1
            time.sleep(0 if DRY_RUN else TG_PAUSE)
        if len(new) > limit:
            log(f"Лимит {limit} сообщений за запуск, ещё {len(new) - limit} уйдут в следующий раз")
        log(f"Отправлено: {sent}")

    try:
        run_trending(config, state, now)
    except Exception as e:
        log(f"[trending] ошибка: {type(e).__name__}: {e}")

    if DRY_RUN:
        return

    # Чистим старые записи
    cutoff = now - timedelta(days=max(SEEN_KEEP_DAYS, window + 3))
    for rid in list(seen):
        try:
            if datetime.fromisoformat(seen[rid]) < cutoff:
                del seen[rid]
        except ValueError:
            del seen[rid]

    # Редкий коммит, чтобы GitHub не отключил расписание из-за неактивности
    last = state.get("keepalive", "")
    try:
        stale = not last or (now - datetime.fromisoformat(last)).days >= KEEPALIVE_EVERY_DAYS
    except ValueError:
        stale = True
    if stale:
        state["keepalive"] = now.isoformat()

    save_state(state)


if __name__ == "__main__":
    main()
