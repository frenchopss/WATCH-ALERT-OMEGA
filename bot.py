import json, os, re, time, unicodedata
import requests
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (compatible; WatchAlertBot/1.4)"
HEADERS = {"User-Agent": UA}
STATE_FILE = "seen.json"

# ✅ Anti-spam / anti-429
MAX_ALERTS_TOTAL_PER_RUN = 30     # plafond global (tous salons confondus)
MAX_ALERTS_PER_QUERY = 8          # plafond par salon (par query)
DISCORD_SLEEP_SECONDS = 1.2       # throttle safe webhook


# ------------------ utils ------------------

def strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", s or "")
        if not unicodedata.combining(c)
    )

def norm(s: str) -> str:
    s = strip_accents(s).lower().strip()
    return re.sub(r"\s+", " ", s)

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def fetch_html(url: str):
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code in (403, 429):
            print(f"[BLOCKED] {r.status_code} {url}")
            return None
        r.raise_for_status()
        return r.text
    except Exception as e:
        print("[FETCH ERROR]", e)
        return None


# ------------------ matching ------------------

def matches(title: str, include, exclude) -> bool:
    t = norm(title)

    if exclude and any(norm(x) in t for x in exclude):
        return False

    if include:
        return any(norm(x) in t for x in include)

    return True


# ------------------ discord ------------------

def discord_notify(webhook_env: str, content: str):
    url = os.environ.get(webhook_env)
    if not url:
        print("[NO WEBHOOK]", webhook_env)
        return

    try:
        resp = requests.post(url, json={"content": content}, timeout=15)
        print(f"[DISCORD] {webhook_env} status={resp.status_code}")
        time.sleep(DISCORD_SLEEP_SECONDS)
    except Exception as e:
        print("[DISCORD ERROR]", e)


# ------------------ vinted parsing ------------------

def parse_vinted_listings(html: str):
    soup = BeautifulSoup(html, "lxml")
    listings = []

    for a in soup.select("a[href*='/items/']"):
        href = a.get("href")
        if not href:
            continue

        url = href if href.startswith("http") else "https://www.vinted.fr" + href

        m = re.search(r"/items/(\d+)", url)
        if not m:
            continue
        item_id = m.group(1)

        title = (
            a.get("title")
            or a.get("aria-label")
            or a.get_text(" ", strip=True)
            or "Annonce Vinted"
        )

        listings.append({"id": item_id, "title": title, "url": url})

    return listings


# ------------------ main ------------------

def main():
    cfg = load_json("config.json", {})
    state = load_json(STATE_FILE, {"seen_ids": []})

    seen = set(state.get("seen_ids", []))
    new_seen = set(seen)

    total_alerts = 0

    for q in cfg.get("queries", []):
        name = q["name"]
        include = q.get("include", [])
        exclude = q.get("exclude", [])
        urls = q.get("vinted_urls", [])
        webhook_env = q["webhook_env"]

        query_alerts = 0
        print(f"[QUERY] {name} urls={len(urls)} webhook_env={webhook_env}")

        for u in urls:
            if total_alerts >= MAX_ALERTS_TOTAL_PER_RUN:
                print("[STOP] max TOTAL alerts reached")
                break

            html = fetch_html(u)
            if not html:
                continue

            items = parse_vinted_listings(html)
            print(f"[READ] {name} -> {len(items)} items")

            for it in items:
                if total_alerts >= MAX_ALERTS_TOTAL_PER_RUN:
                    print("[STOP] max TOTAL alerts reached")
                    break

                if query_alerts >= MAX_ALERTS_PER_QUERY:
                    # ✅ on passe au salon suivant sans bloquer les autres
                    break

                if it["id"] in seen:
                    continue

                if not matches(it["title"], include, exclude):
                    continue

                new_seen.add(it["id"])
                total_alerts += 1
                query_alerts += 1

                discord_notify(
                    webhook_env,
                    f"🔔 **{name}**\n{it['title']}\n{it['url']}"
                )

            time.sleep(1)

        print(f"[QUERY END] {name} alerts={query_alerts}")

        if total_alerts >= MAX_ALERTS_TOTAL_PER_RUN:
            break

    state["seen_ids"] = list(new_seen)[-12000:]
    save_json(STATE_FILE, state)

    print(f"[END] alerts={total_alerts} seen_ids={len(state['seen_ids'])}")


if __name__ == "__main__":
    main()