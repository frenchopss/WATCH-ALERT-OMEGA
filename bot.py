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
MAX_ITEM_FETCH_PER_RUN = 160
MAX_ITEMS_PER_PAGE_SCAN = 40  # newest_first => on scanne les plus récents

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


# ------------------ scoring (simple) ------------------

# ------------------ scoring FLIP (orienté revente rapide) ------------------

def score_badge(score: int) -> str:
    if score >= 75:
        return "🟢"
    if score >= 55:
        return "🟠"
    return "🔴"


def score_listing(title: str) -> int:
    """
    Scoring orienté FLIP :
    - priorité à la simplicité
    - faible risque
    - faible effort
    - revente rapide
    """
    t = norm(title)
    score = 50

    # ---- 1) Mouvement / simplicité ----
    if any(k in t for k in ["automatique", "automatic", "auto "]):
        score += 25

    if any(k in t for k in ["mecanique", "mécanique", "manual", "remontage manuel", "hand winding"]):
        score += 20

    if any(k in t for k in ["quartz", "pile", "battery", "digital", "electronique", "électronique", "electronic"]):
        score -= 45

    # ---- 2) Fonctionnement / état ----
    if any(k in t for k in ["fonctionne", "fonctionnel", "ok", "marche", "testee", "testée", "parfait etat", "parfait état"]):
        score += 10

    if any(k in t for k in ["revisee", "révisée", "revision", "révision", "serviced", "service"]):
        score += 12

    # ---- 3) Risque fort / annonces à problème ----
    if any(k in t for k in [
        "pour pieces", "pour pièces", "pieces", "pièces",
        "hs", "ne marche pas", "ne fonctionne pas",
        "a reparer", "à réparer", "repair",
        "spares", "parts only",
        "casse", "cassé", "cassée",
        "incomplet", "incomplète", "manque", "missing"
    ]):
        score -= 60

    if any(k in t for k in [
        "a verifier", "à vérifier",
        "je ne sais pas", "je sais pas",
        "inconnu", "unknown",
        "non teste", "non testé", "non testee", "non testée",
        "dans son jus"
    ]):
        score -= 18

    # ---- 4) Accessoires / pièces détachées (bruit) ----
    if any(k in t for k in [
        "bracelet", "strap", "maillon", "boucle", "clasp",
        "couronne", "verre", "cadran", "dial",
        "aiguille", "aiguilles",
        "lunette", "bezel",
        "fond", "boitier", "boîtier",
        "outil", "outils",
        "accessoire", "accessoires"
    ]):
        score -= 35

    if any(k in t for k in ["bracelet seul", "bracelets seuls", "strap only", "bracelet only"]):
        score -= 50

    # ---- 5) Indices de liquidité rapide ----
    if any(k in t for k in ["vintage", "original", "authentique", "full set", "complet", "complète"]):
        score += 6

    # Titres trop courts ou génériques = prudence
    if len(t) < 10 or t in [
        "montre", "omega", "tissot", "longines", "mido", "frederique constant"
    ]:
        score -= 10

    return max(0, min(100, score))


# ------------------ discord (format amélioré) ------------------

def discord_notify(webhook_env: str, title: str, url: str, image_url: str = None, score: int = None, query_name: str = None):
    wh = os.environ.get(webhook_env)
    if not wh:
        print("[NO WEBHOOK]", webhook_env)
        return 0

    s = score if score is not None else 0
    badge = score_badge(s)

    # Contenu texte minimal + clair (mobile friendly)
    content_lines = []
    if query_name:
        content_lines.append(f"🔔 **{query_name}**")
    content_lines.append(f"{badge} **Score {s}/100**")
    content_lines.append(url)
    content = "\n".join(content_lines)

    # Embed propre
    emb = {
        "title": title[:250],
        "url": url,
        "description": f"{badge} **Score {s}/100**",
        "footer": {"text": "Vinted • WatchAlertBot"}
    }
    if image_url:
        emb["image"] = {"url": image_url}

    payload = {"content": content, "embeds": [emb]}

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
        out[cid] = {"id": cid, "title": title or "Annonce Vinted", "url": clean_url, "image": image_url}
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
    if image and image.startswith("//"):
        image = "https:" + image
    return {"title": title if title else None, "image": image if image else None}


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

            items = parse_vinted_listings(html)[:MAX_ITEMS_PER_PAGE_SCAN]
            print(f"[READ] {name} -> {len(items)} items | {u}")

            for it in items:
                cid = it["id"]

                if cid not in new_seen:
                    new_seen.add(cid)

                if cid in new_alerted:
                    continue

                # Pré-filtre rapide si titre exploitable
                if it["title"] != "Annonce Vinted" and not matches(it["title"], include, exclude):
                    continue

                # Enrichissement (priorité image)
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

                if it["title"] == "Annonce Vinted":
                    continue

                if not matches(it["title"], include, exclude):
                    continue

                # Caps
                if total_sent >= MAX_ALERTS_PER_RUN:
                    print("[STOP] max alerts per run reached (global)")
                    break
                if query_sent >= MAX_ALERTS_PER_QUERY:
                    print(f"[STOP] max alerts per query reached ({name})")
                    break

                new_alerted.add(cid)

                sc = score_listing(it["title"])

                # Envoi Discord: format propre + lisible
                discord_notify(
                    webhook_env,
                    title=it["title"],
                    url=it["url"],
                    image_url=it.get("image"),
                    score=sc,
                    query_name=name
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