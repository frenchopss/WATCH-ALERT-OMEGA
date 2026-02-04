import json, os, re, time, unicodedata
import requests
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (compatible; WatchAlertBot/1.0)"
HEADERS = {"User-Agent": UA}
STATE_FILE = "seen.json"

# Anti-spam / cadence
MAX_ALERTS_PER_RUN = 60          # global
MAX_ALERTS_PER_QUERY = 10        # évite que Omega/Tissot mangent tout
DISCORD_SLEEP_SEC = 0.9
HTTP_SLEEP_SEC = 1.0

# Etat
MAX_SEEN = 15000
MAX_ALERTED = 15000

# Fallback (quand le titre est vide sur la page catalog)
MAX_ITEM_FETCH_PER_RUN = 25      # limite de fetch item (coût réseau)

ITEM_ID_RE = re.compile(r"/items/(\d+)")


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

    if exclude and any(norm(x) in t for x in exclude):
        return False

    if include:
        return any(norm(x) in t for x in include)

    return True


# ------------------ discord ------------------

def discord_notify(webhook_env: str, content: str, title: str = None, url: str = None, image_url: str = None, score: int = None):
    wh = os.environ.get(webhook_env)
    if not wh:
        print("[NO WEBHOOK]", webhook_env)
        return 0

    payload = {"content": content}

    # Embed optionnel (image + score)
    embeds = []
    if title or url or image_url or score is not None:
        emb = {}
        if title:
            emb["title"] = title[:250]
        if url:
            emb["url"] = url
        if score is not None:
            emb["description"] = f"Score: **{score}/100**"
        if image_url:
            emb["image"] = {"url": image_url}
        embeds.append(emb)

    if embeds:
        payload["embeds"] = embeds

    try:
        r = requests.post(wh, json=payload, timeout=15)
        print(f"[DISCORD] {webhook_env} status={r.status_code}")

        if r.status_code == 429:
            try:
                data = r.json()
                retry_after = float(data.get("retry_after", 2.0))
                time.sleep(min(6.0, max(1.0, retry_after)))
            except Exception:
                time.sleep(2.0)
        return r.status_code
    except Exception as e:
        print("[DISCORD ERROR]", e)
        return 0


# ------------------ vinted parsing ------------------

def canonical_item_id(url: str) -> str:
    m = ITEM_ID_RE.search(url or "")
    if not m:
        return url
    return f"vinted:{m.group(1)}"

def canonical_item_url(url: str) -> str:
    m = ITEM_ID_RE.search(url or "")
    if not m:
        return url
    return f"https://www.vinted.fr/items/{m.group(1)}"

def extract_title_from_anchor(a):
    # Plusieurs fallback : title / aria-label / img alt
    t = (a.get("title") or a.get("aria-label") or "").strip()
    if t:
        return t

    img = a.select_one("img")
    if img:
        alt = (img.get("alt") or "").strip()
        if alt:
            return alt

    # texte visible
    txt = a.get_text(" ", strip=True)
    return txt.strip() if txt else ""

def extract_image_from_anchor(a):
    img = a.select_one("img")
    if not img:
        return None

    # Vinted varie : src / data-src / srcset
    for k in ("src", "data-src"):
        v = img.get(k)
        if v and v.startswith("http"):
            return v

    srcset = img.get("srcset")
    if srcset:
        # prendre la dernière url (souvent la plus grande)
        parts = [p.strip() for p in srcset.split(",") if p.strip()]
        if parts:
            last = parts[-1].split(" ")[0].strip()
            if last.startswith("http"):
                return last
    return None

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

        title = extract_title_from_anchor(a)
        image_url = extract_image_from_anchor(a)

        out[cid] = {
            "id": cid,
            "title": title or "Annonce Vinted",
            "url": clean_url,
            "image": image_url
        }

    return list(out.values())

def fetch_item_details(item_url: str):
    html = fetch_html(item_url)
    if not html:
        return None

    soup = BeautifulSoup(html, "lxml")

    # og:title / og:image sont assez stables
    ogt = soup.select_one("meta[property='og:title']")
    ogi = soup.select_one("meta[property='og:image']")
    title = (ogt.get("content") if ogt else "") or ""
    image = (ogi.get("content") if ogi else "") or ""

    title = title.strip()
    image = image.strip()

    return {
        "title": title if title else None,
        "image": image if image else None
    }

def score_listing(title: str) -> int:
    t = norm(title)
    score = 50
    # bonus
    if "automatique" in t or "automatic" in t:
        score += 20
    if "mecanique" in t or "mécanique" in t or "manual" in t:
        score += 15
    if "vintage" in t:
        score += 5
    # malus
    if "quartz" in t or "pile" in t or "battery" in t:
        score -= 30
    if "strap" in t or "bracelet" in t or "maillon" in t or "boucle" in t:
        score -= 25
    if "piece" in t or "pièce" in t or "parts" in t or "spares" in t:
        score -= 30
    return max(0, min(100, score))


# ------------------ main ------------------

def main():
    cfg = load_json("config.json", {})
    state = load_json(STATE_FILE, {"seen_ids": [], "alerted_ids": []})

    seen = set(state.get("seen_ids", []))
    alerted = set(state.get("alerted_ids", []))

    new_seen = set(seen)
    new_alerted = set(alerted)

    total_alerts = 0
    item_fetch_budget = MAX_ITEM_FETCH_PER_RUN

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
                # ID stable
                cid = it["id"]

                # Marquer vu
                if cid not in new_seen:
                    new_seen.add(cid)

                # Déjà alerté -> jamais renvoyer
                if cid in new_alerted:
                    continue

                # Si titre vide (= gros risque de bruit), on tente un enrichissement sur la page item
                if it["title"] == "Annonce Vinted" and item_fetch_budget > 0:
                    details = fetch_item_details(it["url"])
                    item_fetch_budget -= 1
                    time.sleep(HTTP_SLEEP_SEC)

                    if details:
                        if details.get("title"):
                            it["title"] = details["title"]
                        if not it.get("image") and details.get("image"):
                            it["image"] = details["image"]

                # Si encore vide -> on skip (sinon tu alertes des accessoires/bruit)
                if it["title"] == "Annonce Vinted":
                    continue

                # Filtre qualité
                if not matches(it["title"], include, exclude):
                    continue

                # Caps anti-spam
                if total_alerts >= MAX_ALERTS_PER_RUN:
                    print("[STOP] max alerts per run reached (global)")
                    break
                if query_alerts >= MAX_ALERTS_PER_QUERY:
                    print(f"[STOP] max alerts per query reached ({name})")
                    break

                # Marquer alerté (même si Discord rate-limit après)
                new_alerted.add(cid)

                total_alerts += 1
                query_alerts += 1

                sc = score_listing(it["title"])
                discord_notify(
                    webhook_env,
                    f"🔔 **{name}**\n{it['title']}\n{it['url']}",
                    title=it["title"],
                    url=it["url"],
                    image_url=it.get("image"),
                    score=sc
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