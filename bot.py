import json, os, re, time, unicodedata
import requests
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (compatible; WatchAlertBot/2.0)"
HEADERS = {"User-Agent": UA}
STATE_FILE = "seen.json"

# --- limites / anti-spam ---
MAX_ALERTS_TOTAL_PER_RUN = 30
MAX_ALERTS_PER_QUERY = 8

DISCORD_SLEEP_SECONDS = 1.2     # anti-429
ITEM_PAGE_SLEEP_SECONDS = 0.7   # requête page item pour image (og:image)

# --- anti-bruit (hard excludes titre) ---
HARD_EXCLUDE_TITLE = [
    "bracelet", "bracelet montre", "watch band", "strap",
    "boucle", "buckle",
    "maillon", "maillons", "link", "links",
    "pour pièces", "pieces detachees", "pièces détachées", "spares", "parts",
    "mouvement seul", "movement only",
    "cadran", "dial only",
    "boîte seule", "boite seule", "box only",
    "outil", "tool", "watchmaker"
]

# --- scoring (simple, robuste, V2) ---
POSITIVE = [
    ("révisée", 20, "REV"),
    ("revision", 20, "REV"),
    ("serviced", 20, "REV"),
    ("automatique", 12, "AUTO"),
    ("mecanique", 12, "MECA"),
    ("mécanique", 12, "MECA"),
    ("vintage", 6, "VIN"),
    ("très bon état", 8, "TBE"),
    ("bon état", 4, "BE"),
]

NEGATIVE = [
    ("quartz", -8, "QZ"),
    ("pile", -6, "PILE"),
    ("ne fonctionne pas", -40, "HS"),
    ("hs", -40, "HS"),
    ("pour pièces", -60, "PARTS"),
    ("pieces detachees", -60, "PARTS"),
    ("pièces détachées", -60, "PARTS"),
]

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

def title_is_hard_excluded(title: str) -> bool:
    t = norm(title)
    return any(norm(x) in t for x in HARD_EXCLUDE_TITLE)

def matches(title: str, include, exclude) -> bool:
    t = norm(title)

    if exclude and any(norm(x) in t for x in exclude):
        return False

    if include:
        return any(norm(x) in t for x in include)

    return True

def score_title(title: str) -> tuple[int, list[str]]:
    t = norm(title)
    score = 50
    tags = []

    for key, pts, tag in POSITIVE:
        if norm(key) in t:
            score += pts
            tags.append(tag)

    for key, pts, tag in NEGATIVE:
        if norm(key) in t:
            score += pts
            tags.append(tag)

    score = max(0, min(100, score))
    return score, sorted(set(tags))

def get_vinted_og_image(item_url: str) -> str | None:
    html = fetch_html(item_url)
    if not html:
        return None
    soup = BeautifulSoup(html, "lxml")
    meta = soup.select_one("meta[property='og:image']")
    if meta and meta.get("content"):
        return meta["content"]
    return None

def discord_notify_embed(webhook_env: str, title: str, url: str, score: int, tags: list[str], image_url: str | None):
    webhook = os.environ.get(webhook_env)
    if not webhook:
        print("[NO WEBHOOK]", webhook_env)
        return

    color = 0x2ECC71 if score >= 70 else (0xF1C40F if score >= 50 else 0xE74C3C)
    tag_str = " ".join(f"`{t}`" for t in tags) if tags else "`—`"

    embed = {
        "title": f"{title[:180]}",
        "url": url,
        "description": f"Score: **{score}/100**  •  Tags: {tag_str}",
        "color": color,
    }
    if image_url:
        embed["image"] = {"url": image_url}

    payload = {
        "content": None,
        "embeds": [embed],
    }

    try:
        resp = requests.post(webhook, json=payload, timeout=15)
        print(f"[DISCORD] {webhook_env} status={resp.status_code}")
        time.sleep(DISCORD_SLEEP_SECONDS)
    except Exception as e:
        print("[DISCORD ERROR]", e)

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
        print(f"[QUERY] {name} urls={len(urls)}")

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
                    break
                if query_alerts >= MAX_ALERTS_PER_QUERY:
                    break
                if it["id"] in seen:
                    continue

                # anti-bruit hard (titre)
                if title_is_hard_excluded(it["title"]):
                    continue

                if not matches(it["title"], include, exclude):
                    continue

                score, tags = score_title(it["title"])

                # récup image seulement pour une alerte validée
                image_url = get_vinted_og_image(it["url"])
                time.sleep(ITEM_PAGE_SLEEP_SECONDS)

                new_seen.add(it["id"])
                total_alerts += 1
                query_alerts += 1

                discord_notify_embed(
                    webhook_env=webhook_env,
                    title=f"🔔 {name}",
                    url=it["url"],
                    score=score,
                    tags=tags,
                    image_url=image_url
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