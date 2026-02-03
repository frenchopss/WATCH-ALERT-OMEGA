import json, os, re, time, unicodedata
import requests
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (compatible; WatchAlertBot/1.0)"
HEADERS = {"User-Agent": UA}
STATE_FILE = "seen.json"

# Limites anti-spam / anti-rate-limit Discord
MAX_ALERTS_PER_RUN = 40
DISCORD_SLEEP_SEC = 0.8  # pause entre messages

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
    except Exception:
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
        print("[FETCH ERROR]", url, e)
        return None


# ------------------ matching ------------------

def matches(title: str, include, exclude) -> bool:
    t = norm(title)

    # Exclusions simples (ex: "quartz", "bracelet", "piece", etc.)
    if exclude and any(norm(x) in t for x in exclude):
        return False

    # Include = au moins 1 mot-clé match (si liste vide -> ok)
    if include:
        return any(norm(x) in t for x in include)

    return True


# ------------------ discord ------------------

def discord_notify(webhook_env: str, content: str):
    url = os.environ.get(webhook_env)
    if not url:
        print("[NO WEBHOOK]", webhook_env)
        return 0

    try:
        r = requests.post(url, json={"content": content}, timeout=15)
        print(f"[DISCORD] {webhook_env} status={r.status_code}")
        # 204 = OK
        if r.status_code == 429:
            # rate limit : on attend un peu plus
            time.sleep(2.0)
        return r.status_code
    except Exception as e:
        print("[DISCORD ERROR]", e)
        return 0


# ------------------ vinted parsing ------------------

ITEM_ID_RE = re.compile(r"/items/(\d+)")

def canonical_item_id(url: str) -> str:
    """
    Retourne un ID stable: 'vinted:123456789'
    même si l'URL contient des paramètres referrer/utm.
    """
    m = ITEM_ID_RE.search(url or "")
    if not m:
        return url  # fallback
    return f"vinted:{m.group(1)}"

def canonical_item_url(url: str) -> str:
    """
    Retourne une URL propre sans query string.
    """
    m = ITEM_ID_RE.search(url or "")
    if not m:
        return url
    return f"https://www.vinted.fr/items/{m.group(1)}"

def parse_vinted_listings(html: str):
    soup = BeautifulSoup(html, "lxml")
    out = {}

    for a in soup.select("a[href*='/items/']"):
        href = a.get("href")
        if not href:
            continue

        full_url = href if href.startswith("http") else "https://www.vinted.fr" + href
        clean_url = canonical_item_url(full_url)
        cid = canonical_item_id(full_url)

        title = a.get_text(" ", strip=True) or "Annonce Vinted"

        out[cid] = {
            "id": cid,
            "title": title,
            "url": clean_url
        }

    return list(out.values())


# ------------------ main ------------------

def main():
    cfg = load_json("config.json", {})
    state = load_json(STATE_FILE, {"seen_ids": []})

    seen = set(state.get("seen_ids", []))
    new_seen = set(seen)

    total_alerts = 0

    for q in cfg.get("queries", []):
        name = q.get("name", "Query")
        include = q.get("include", [])
        exclude = q.get("exclude", [])
        urls = q.get("vinted_urls", [])
        webhook_env = q.get("webhook_env")

        if not webhook_env:
            print(f"[SKIP] {name} no webhook_env")
            continue

        print(f"[QUERY] {name} urls={len(urls)} webhook_env={webhook_env} env_present={bool(os.environ.get(webhook_env))}")

        for u in urls:
            html = fetch_html(u)
            if not html:
                continue

            items = parse_vinted_listings(html)
            print(f"[READ] {name} -> {len(items)} items | {u}")

            # IMPORTANT: on ne marque "vu" que quand ça matche,
            # sinon tu pollues le seen avec plein de bruit.
            for it in items:
                if it["id"] in new_seen:
                    continue

                if not matches(it["title"], include, exclude):
                    continue

                # si ça matche => on marque comme vu
                new_seen.add(it["id"])

                # alert
                total_alerts += 1
                discord_notify(
                    webhook_env,
                    f"🔔 **{name}**\n{it['title']}\n{it['url']}"
                )
                time.sleep(DISCORD_SLEEP_SEC)

                if total_alerts >= MAX_ALERTS_PER_RUN:
                    print("[STOP] max alerts per run reached")
                    break

            if total_alerts >= MAX_ALERTS_PER_RUN:
                break

        if total_alerts >= MAX_ALERTS_PER_RUN:
            break

    # Sauvegarde état
    state["seen_ids"] = list(new_seen)[-12000:]
    save_json(STATE_FILE, state)

    print(f"[END] alerts={total_alerts} seen_ids={len(state['seen_ids'])}")


if __name__ == "__main__":
    main()