import json, os, re, time, unicodedata
import requests
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (compatible; WatchAlertBot/1.0)"
HEADERS = {"User-Agent": UA}
STATE_FILE = "seen.json"

def strip_accents(s: str) -> str:
    if not s:
        return ""
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))

def norm(s: str) -> str:
    s = strip_accents(s).lower().strip()
    s = re.sub(r"\s+", " ", s)
    return s

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
            print(f"[BLOCKED] {url} -> {r.status_code}")
            return None
        r.raise_for_status()
        return r.text
    except requests.RequestException as e:
        print(f"[ERROR] fetch failed: {url} -> {e}")
        return None

def matches(text: str, include, exclude) -> bool:
    t = norm(text)
    if exclude and any(norm(x) in t for x in exclude):
        return False
    if include and not any(norm(x) in t for x in include):
        return False
    return True

def discord_notify(webhook_url: str, content: str):
    if not webhook_url:
        print("[NO WEBHOOK]", content)
        return
    r = requests.post(webhook_url, json={"content": content}, timeout=15)
    print("Discord status:", r.status_code)

def parse_vinted_listings(html: str):
    soup = BeautifulSoup(html, "lxml")
    out = {}
    for a in soup.select("a[href*='/items/']"):
        href = a.get("href")
        if not href:
            continue
        url = href if href.startswith("http") else ("https://www.vinted.fr" + href)
        title = a.get_text(" ", strip=True) or "Annonce Vinted"
        out[url] = {"id": url, "title": title, "url": url}
    return list(out.values())

def main():
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    cfg = load_json("config.json", {})
    state = load_json(STATE_FILE, {"seen_ids": []})
    seen = set(state.get("seen_ids", []))

    first_run = (len(seen) == 0)
    new_hits = 0
    new_seen = set(seen)

    for q in cfg.get("queries", []):
        name = q["name"]
        include = q.get("include", [])
        exclude = q.get("exclude", [])
        vinted_url = (q.get("search_urls", {}) or {}).get("vinted")

        if not vinted_url:
            print(f"[SKIP] {name}: missing vinted url")
            continue

        html = fetch_html(vinted_url)
        if not html:
            continue

        listings = parse_vinted_listings(html)
        time.sleep(1)

        for it in listings:
            if it["id"] in new_seen:
                continue
            new_seen.add(it["id"])

            if not matches(it["title"], include, exclude):
                continue

            if first_run:
                continue

            new_hits += 1
            discord_notify(webhook, f"🔔 **{name}**\n{it['title']}\n{it['url']}")

    state["seen_ids"] = list(new_seen)[-6000:]
    save_json(STATE_FILE, state)

    if first_run:
        discord_notify(webhook, "✅ V1 (Vinted-only) initialisée : historique chargé (pas d’alertes au 1er run).")
    else:
        discord_notify(webhook, f"✅ V1 (Vinted-only) run terminé : {new_hits} nouvelle(s) alerte(s).")

if __name__ == "__main__":
    main()
