import json, os, re, time, unicodedata
import requests
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (compatible; WatchAlertBot/1.0)"
HEADERS = {"User-Agent": UA}
STATE_FILE = "seen.json"

# Limites anti-spam / anti-rate-limit
MAX_ALERTS_PER_RUN = 120         # global (toutes queries)
MAX_ALERTS_PER_QUERY = 10        # évite qu’une query bouffe tout
DISCORD_SLEEP_SEC = 0.8          # pause entre messages discord
HTTP_SLEEP_SEC = 1.0             # pause entre pages vinted
ITEM_FETCH_SLEEP_SEC = 0.6       # pause quand on ouvre une page item (og:*)

MAX_SEEN = 12000
MAX_ALERTED = 12000

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

def matches(text: str, include, exclude) -> bool:
    t = norm(text)

    if exclude and any(norm(x) in t for x in exclude):
        return False

    if include:
        return any(norm(x) in t for x in include)

    return True


# ------------------ discord ------------------

def discord_notify(webhook_env: str, title: str, url: str, image_url: str = None, score: int = None):
    webhook = os.environ.get(webhook_env)
    if not webhook:
        print("[NO WEBHOOK]", webhook_env)
        return 0

    content = f"🔔 **{title}**\n{url}"
    if score is not None:
        content = f"🔔 **{title}** (score: {score})\n{url}"

    payload = {"content": content}

    if image_url:
        payload["embeds"] = [{
            "title": title[:256],
            "url": url,
            "image": {"url": image_url}
        }]

    try:
        r = requests.post(webhook, json=payload, timeout=15)
        print(f"[DISCORD] {webhook_env} status={r.status_code}")

        if r.status_code == 429:
            try:
                data = r.json()
                retry_after = float(data.get("retry_after", 2.0))
                time.sleep(min(5.0, max(1.0, retry_after)))
            except Exception:
                time.sleep(2.0)

        return r.status_code
    except Exception as e:
        print("[DISCORD ERROR]", e)
        return 0


# ------------------ vinted parsing ------------------

ITEM_ID_RE = re.compile(r"/items/(\d+)")

def canonical_item_id(url: str) -> str:
    m = ITEM_ID_RE.search(url or "")
    if not m:
        return url  # fallback
    return f"vinted:{m.group(1)}"

def canonical_item_url(url: str) -> str:
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

        # Sur les pages liste, le texte peut être vide ou pas le vrai titre.
        title = a.get_text(" ", strip=True) or "Annonce Vinted"

        out[cid] = {"id": cid, "title": title, "url": clean_url}

    return list(out.values())

def fetch_item_meta(item_url: str):
    """
    Ouvre la page de l’annonce et récupère og:title / og:image / og:description.
    """
    html = fetch_html(item_url)
    if not html:
        return None, None, None

    soup = BeautifulSoup(html, "lxml")

    def get_meta(prop):
        tag = soup.find("meta", attrs={"property": prop})
        return tag.get("content") if tag else None

    title = get_meta("og:title")
    image = get_meta("og:image")
    desc  = get_meta("og:description")

    return title, image, desc


# ------------------ main ------------------

def main():
    cfg = load_json("config.json", {})

    # IMPORTANT: on supporte seen_ids + alerted_ids
    state = load_json(STATE_FILE, {"seen_ids": [], "alerted_ids": []})
    if "alerted_ids" not in state:
        state["alerted_ids"] = []

    seen = set(state.get("seen_ids", []))
    alerted = set(state.get("alerted_ids", []))

    new_seen = set(seen)
    new_alerted = set(alerted)

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

        query_alerts = 0
        print(f"[QUERY] {name} urls={len(urls)} webhook_env={webhook_env} env_present={bool(os.environ.get(webhook_env))}")

        for u in urls:
            html = fetch_html(u)
            if not html:
                continue

            items = parse_vinted_listings(html)
            print(f"[READ] {name} -> {len(items)} items | {u}")

            for it in items:
                # 1) vu dès qu’on le rencontre (évite de re-spammer quand on revoit l’annonce)
                if it["id"] not in new_seen:
                    new_seen.add(it["id"])

                # 2) jamais re-alerter si déjà alerté (persistant)
                if it["id"] in new_alerted:
                    continue

                # 3) filtre sur le titre qu’on a
                if not matches(it["title"], include, exclude):
                    continue

                # 4) si titre trop faible -> on récupère og:title + image depuis la page item
                real_title = it["title"]
                image_url = None
                desc = None

                if real_title == "Annonce Vinted" or len(real_title.strip()) < 6:
                    time.sleep(ITEM_FETCH_SLEEP_SEC)
                    t2, img2, d2 = fetch_item_meta(it["url"])
                    if t2:
                        real_title = t2
                    image_url = img2
                    desc = d2

                    # re-filtrage après vrai titre
                    if not matches(real_title, include, exclude):
                        continue

                # 5) caps anti-spam
                if total_alerts >= MAX_ALERTS_PER_RUN:
                    print("[STOP] max alerts per run reached (global)")
                    break
                if query_alerts >= MAX_ALERTS_PER_QUERY:
                    print(f"[STOP] max alerts per query reached ({name})")
                    break

                # 6) on alerte une seule fois
                new_alerted.add(it["id"])
                total_alerts += 1
                query_alerts += 1

                discord_notify(
                    webhook_env,
                    f"{name} — {real_title}",
                    it["url"],
                    image_url=image_url
                )
                time.sleep(DISCORD_SLEEP_SEC)

            if total_alerts >= MAX_ALERTS_PER_RUN:
                break

            time.sleep(HTTP_SLEEP_SEC)

        if total_alerts >= MAX_ALERTS_PER_RUN:
            break

    # Sauvegarde état
    state["seen_ids"] = list(new_seen)[-MAX_SEEN:]
    state["alerted_ids"] = list(new_alerted)[-MAX_ALERTED:]
    save_json(STATE_FILE, state)

    print(f"[END] alerts={total_alerts} seen_ids={len(state['seen_ids'])} alerted_ids={len(state['alerted_ids'])}")


if __name__ == "__main__":
    main()