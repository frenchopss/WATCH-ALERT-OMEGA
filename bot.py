import json, os, re, time, unicodedata
import requests
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (compatible; WatchAlertBot/1.0)"
HEADERS = {"User-Agent": UA}
STATE_FILE = "seen.json"

# Cadence / limites
MAX_ALERTS_PER_RUN = 60
MAX_ALERTS_PER_QUERY = 10
DISCORD_SLEEP_SEC = 0.9
HTTP_SLEEP_SEC = 1.0

# Etat
MAX_SEEN = 15000
MAX_ALERTED = 15000

# Budgets (important pour images)
MAX_ITEM_FETCH_PER_RUN = 160   # augmente si tu veux + d'images
MAX_ITEMS_PER_PAGE_SCAN = 40   # ne scanne que les plus récentes (newest_first)

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
        print(f"[DISCORD] {webhook_env} status={r.status_code}{' (no-attachment)' if (image_url is None) else ''}")

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
    t = (a.get("title") or a.get("aria-label") or "").strip()
    if t:
        return t

    img = a.select_one("img")
    if img:
        alt = (img.get("alt") or "").strip()
        if alt:
            return alt

    txt = a.get_text(" ", strip=True)
    return txt.strip() if txt else ""

def extract_image_from_anchor(a):
    img = a.select_one("img")
    if not img:
        return None

    for k in ("src", "data-src"):
        v = img.get(k)
        if v and v.startswith("http"):
            return v

    srcset = img.get("srcset")
    if srcset:
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

    ogt = soup.select_one("meta[property='og:title']")
    ogi = soup.select_one("meta[property='og:image']")
    title = (ogt.get("content") if ogt else "") or ""
    image = (ogi.get("content") if ogi else "") or ""

    title = title.strip()
    image = image.strip()

    # Nettoyage basique (parfois og:image contient des params)
    if image and image.startswith("//"):
        image = "https:" + image

    return {
        "title": title if title else None,
        "image": image if image else None
    }

def score_listing(title: str) -> int:
    t = norm(title)
    score = 50

    # Bonus
    if "automatique" in t or "automatic" in t:
        score += 20
    if "mecanique" in t or "mécanique" in t or "manual" in t or "remontage manuel" in t:
        score += 15
    if "vintage" in t:
        score += 5
    if "seamaster" in t or "visodate" in t or "seastar" in t or "flagship" in t or "conquest" in t or "multifort" in t or "commander" in t:
        score += 5

    # Malus (accessoires / pièces / quartz)
    if "quartz" in t or "pile" in t or "battery" in t or "digital" in t:
        score -= 35
    if "bracelet" in t or "strap" in t or "maillon" in t or "boucle" in t or "clasp" in t:
        score -= 30
    if "piece" in t or "pièce" in t or "parts" in t or "spares" in t or "couronne" in t or "verre" in t or "cadran" in t or "dial" in t:
        score -= 35

    return max(0, min(100, score))


# ------------------ main ------------------

def main():
    cfg = load_json("config.json", {})
    state = load_json(STATE_FILE, {"seen_ids": [], "alerted_ids": []})

    seen = set(state.get("seen_ids", []))
    alerted = set(state.get("alerted_ids", []))

    new_seen = set(seen)
    new_alerted = set(alerted)

    total_sent = 0
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

        query_sent = 0
        print(f"[QUERY] {name} urls={len(urls)} webhook_env={webhook_env} env_present={bool(os.environ.get(webhook_env))}")

        for u in urls:
            html = fetch_html(u)
            if not html:
                continue

            items = parse_vinted_listings(html)
            print(f"[READ] {name} -> {len(items)} items | {u}")

            # Ne traiter que les plus récents (tu es en newest_first)
            items = items[:MAX_ITEMS_PER_PAGE_SCAN]

            for it in items:
                cid = it["id"]

                if cid not in new_seen:
                    new_seen.add(cid)

                if cid in new_alerted:
                    continue

                # Pré-filtre rapide si titre exploitable
                # (si "Annonce Vinted", on ne filtre pas encore)
                if it["title"] != "Annonce Vinted":
                    if not matches(it["title"], include, exclude):
                        continue

                # Enrichissement seulement si nécessaire (titre générique OU image manquante)
                need_details = (it["title"] == "Annonce Vinted") or (not it.get("image"))
                if need_details and item_fetch_budget > 0:
                    details = fetch_item_details(it["url"])
                    item_fetch_budget -= 1
                    time.sleep(HTTP_SLEEP_SEC)

                    if details:
                        if details.get("title"):
                            it["title"] = details["title"]
                        if details.get("image"):
                            it["image"] = details["image"]

                # Si encore pas de titre, on skip (évite gros bruit)
                if it["title"] == "Annonce Vinted":
                    continue

                # Filtre final
                if not matches(it["title"], include, exclude):
                    continue

                # Caps
                if total_sent >= MAX_ALERTS_PER_RUN:
                    print("[STOP] max alerts per run reached (global)")
                    break
                if query_sent >= MAX_ALERTS_PER_QUERY:
                    print(f"[STOP] max alerts per query reached ({name})")
                    break

                # Mark alerted (anti-doublons)
                new_alerted.add(cid)

                sc = score_listing(it["title"])

                discord_notify(
                    webhook_env,
                    f"🔔 **{name}**\n{it['title']}\n{it['url']}",
                    title=it["title"],
                    url=it["url"],
                    image_url=it.get("image"),
                    score=sc
                )

                total_sent += 1
                query_sent += 1

                time.sleep(DISCORD_SLEEP_SEC)

            if total_sent >= MAX_ALERTS_PER_RUN:
                break

            time.sleep(HTTP_SLEEP_SEC)

        if total_sent >= MAX_ALERTS_PER_RUN:
            break

    state["seen_ids"] = list(new_seen)[-MAX_SEEN:]
    state["alerted_ids"] = list(new_alerted)[-MAX_ALERTED:]
    save_json(STATE_FILE, state)

    print(f"[END] sent={total_sent} seen_ids={len(state['seen_ids'])} alerted_ids={len(state['alerted_ids'])} item_fetch_left={item_fetch_budget}")


if __name__ == "__main__":
    main()