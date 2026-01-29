import json, os, re, time, unicodedata
import requests
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (compatible; WatchAlertBot/1.3)"
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
        r = requests.get(url, headers=HEADERS, timeout=25)
        if r.status_code in (403, 429):
            print(f"[BLOCKED] {r.status_code} {url}")
            return None
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"[ERROR] fetch {url} -> {e}")
        return None

def matches(title: str, include, exclude) -> bool:
    t = norm(title)
    if exclude and any(norm(x) in t for x in exclude):
        return False
    if include and not any(norm(x) in t for x in include):
        return False
    return True

def discord_notify(webhook_env: str, content: str):
    url = os.environ.get(webhook_env)
    if not url:
        print(f"[NO_WEBHOOK_ENV] {webhook_env} missing -> not sent")
        return
    try:
        resp = requests.post(url, json={"content": content}, timeout=10)
        print(f"[DISCORD] {webhook_env} status={resp.status_code}")
    except Exception as e:
        print(f"[ERROR] discord post -> {e}")

def parse_vinted_listings(html: str):
    soup = BeautifulSoup(html, "lxml")
    out = {}
    for a in soup.select("a[href*='/items/']"):
        href = a.get("href")
        if not href:
            continue
        url = href if href.startswith("http") else "https://www.vinted.fr" + href
        title = a.get_text(" ", strip=True) or "Annonce Vinted"
        out[url] = {"id": url, "title": title, "url": url}
    return list(out.values())

def expand_pages(url: str):
    # on surveille toujours page 1 + 2
    if "page=" not in url:
        return [url, url + "&page=2"]
    if "page=1" in url:
        return [url, url.replace("page=1", "page=2")]
    # si page != 1, on ajoute quand même une variante page=2
    return [url, re.sub(r"page=\d+", "page=2", url)]

def main():
    cfg = load_json("config.json", {})
    state = load_json(STATE_FILE, {"seen_ids": []})
    seen = set(state.get("seen_ids", []))
    new_seen = set(seen)

    queries = cfg.get("queries", [])
    print(f"[START] queries={len(queries)} seen_ids={len(seen)}")

    total_alerts = 0

    for q in queries:
        name = q["name"]
        webhook_env = q["webhook_env"]
        include = q.get("include", [])
        exclude = q.get("exclude", [])
        urls = q.get("vinted_urls", [])

        env_present = bool(os.environ.get(webhook_env))
        print(f"[QUERY] {name} webhook_env={webhook_env} env_present={env_present} urls={len(urls)}")

        all_urls = []
        for u in urls:
            all_urls.extend(expand_pages(u))

        for u in all_urls:
            html = fetch_html(u)
            if not html:
                continue

            listings = parse_vinted_listings(html)
            print(f"[READ] {name} -> {len(listings)} items | {u}")
            time.sleep(1)

            for it in listings:
                if it["id"] in new_seen:
                    continue
                new_seen.add(it["id"])

                if not matches(it["title"], include, exclude):
                    continue

                # ✅ Nouvelle annonce qui match
                total_alerts += 1
                print(f"[ALERT] {name} -> {it['url']}")
                discord_notify(webhook_env, f"🔔 **{name}**\n{it['title']}\n{it['url']}")

    state["seen_ids"] = list(new_seen)[-10000:]
    save_json(STATE_FILE, state)
    print(f"[END] alerts={total_alerts} seen_ids={len(state['seen_ids'])}")

if __name__ == "__main__":
    main()