import json, os, re, time, unicodedata
import requests
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (compatible; WatchAlertBot/1.2)"
HEADERS = {"User-Agent": UA}
STATE_FILE = "seen.json"


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

        # ✅ ID stable (évite les doublons même si l'URL change)
        m = re.search(r"/items/(\d+)", url)
        if not m:
            continue
        item_id = m.group(1)

        # ✅ titre plus robuste que get_text seul
        title = (
            a.get("title")
            or a.get("aria-label")
            or a.get_text(" ", strip=True)
            or "Annonce Vinted"
        )

        listings.append({
            "id": item_id,   # on stocke l'ID numérique, pas l'URL
            "title": title,
            "url": url
        })

    return listings


# ------------------ main ------------------

def main():
    cfg = load_json("config.json", {})
    state = load_json(STATE_FILE, {"seen_ids": []})

    seen = set(state.get("seen_ids", []))
    new_seen = set(seen)

    alerts = 0

    for q in cfg.get("queries", []):
        name = q["name"]
        include = q.get("include", [])
        exclude = q.get("exclude", [])
        urls = q.get("vinted_urls", [])
        webhook_env = q["webhook_env"]

        print(f"[QUERY] {name} urls={len(urls)} webhook_env={webhook_env} env_present={bool(os.environ.get(webhook_env))}")

        for u in urls:
            html = fetch_html(u)
            if not html:
                continue

            items = parse_vinted_listings(html)
            print(f"[READ] {name} -> {len(items)} items")

            # DEBUG léger : affiche 3 titres max
            for i, it in enumerate(items[:3]):
                print(f"[SAMPLE] {name} #{i+1}: {it['title'][:120]} (id={it['id']})")

            for it in items:
                if it["id"] in seen:
                    continue

                if not matches(it["title"], include, exclude):
                    continue

                # ✅ On marque "vu" uniquement si ça déclenche une alerte
                new_seen.add(it["id"])
                alerts += 1

                discord_notify(
                    webhook_env,
                    f"🔔 **{name}**\n{it['title']}\n{it['url']}"
                )

            time.sleep(1)

    state["seen_ids"] = list(new_seen)[-12000:]
    save_json(STATE_FILE, state)

    print(f"[END] alerts={alerts} seen_ids={len(state['seen_ids'])}")


if __name__ == "__main__":
    main()