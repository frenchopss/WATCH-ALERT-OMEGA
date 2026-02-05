import json, os, re, time, unicodedata
import requests
from bs4 import BeautifulSoup

# =======================
# CONFIG
# =======================
UA = "Mozilla/5.0 (compatible; WatchAlertBot/2.0)"
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.7",
}
STATE_FILE = "seen.json"

# Anti-spam / cadence
MAX_ALERTS_PER_RUN = 60
MAX_ALERTS_PER_QUERY = 10
DISCORD_SLEEP_SEC = 0.9
HTTP_SLEEP_SEC = 1.0

# Etat (persisté)
MAX_SEEN = 15000
MAX_ALERTED = 15000

# Enrichissement page item (coût réseau)
MAX_ITEM_FETCH_PER_RUN = 120

# Téléchargements images (coût réseau + taille)
MAX_IMAGE_DL_PER_RUN = 50
MAX_IMAGE_BYTES = 7_500_000  # ~7.5MB safe

# Scoring: si tu veux n'envoyer que les meilleurs, mets par ex 60 (sinon laisse 0)
MIN_SCORE_TO_SEND = 0

ITEM_ID_RE = re.compile(r"/items/(\d+)")


# =======================
# UTILS
# =======================
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


# =======================
# MATCHING (include/exclude)
# =======================
def matches(text: str, include, exclude) -> bool:
    t = norm(text or "")
    if exclude and any(norm(x) in t for x in exclude):
        return False
    if include:
        return any(norm(x) in t for x in include)
    return True


# =======================
# DISCORD (webhook + image attachment)
# =======================
def _discord_success(code: int) -> bool:
    # JSON payload: 204, multipart: souvent 200
    return code in (200, 204)

def discord_notify(
    webhook_env: str,
    content: str,
    title: str = None,
    url: str = None,
    image_url: str = None,
    score: int = None,
):
    wh = os.environ.get(webhook_env)
    if not wh:
        print("[NO WEBHOOK]", webhook_env)
        return 0

    # base embed
    emb = {}
    if title:
        emb["title"] = title[:250]
    if url:
        emb["url"] = url
    if score is not None:
        emb["description"] = f"Score: **{score}/100**"

    # 1) Si image_url -> essayer d'envoyer l'image en PJ (fiable)
    if image_url:
        try:
            img_headers = dict(HEADERS)
            img_headers["Referer"] = "https://www.vinted.fr/"
            img = requests.get(image_url, headers=img_headers, timeout=20, allow_redirects=True)

            if img.status_code == 200 and img.content and len(img.content) <= MAX_IMAGE_BYTES:
                filename = "photo.jpg"
                emb_with_img = dict(emb)
                emb_with_img["image"] = {"url": f"attachment://{filename}"}

                data = {
                    "payload_json": json.dumps({
                        "content": content,
                        "embeds": [emb_with_img],
                    }, ensure_ascii=False)
                }
                files = {
                    "file": (filename, img.content)
                }

                r = requests.post(wh, data=data, files=files, timeout=25)
                print(f"[DISCORD] {webhook_env} status={r.status_code} (attachment)")

                # rate limit
                if r.status_code == 429:
                    try:
                        ra = float(r.json().get("retry_after", 2.0))
                        time.sleep(min(8.0, max(1.0, ra)))
                    except Exception:
                        time.sleep(2.0)

                return r.status_code
            else:
                print("[IMG SKIP]", img.status_code, "bytes=", len(img.content) if img.content else 0, image_url)
        except Exception as e:
            print("[IMG ERROR]", e)

    # 2) Fallback: embed sans image PJ
    payload = {"content": content, "embeds": [emb] if emb else []}
    try:
        r = requests.post(wh, json=payload, timeout=15)
        print(f"[DISCORD] {webhook_env} status={r.status_code} (no-attachment)")

        if r.status_code == 429:
            try:
                ra = float(r.json().get("retry_after", 2.0))
                time.sleep(min(8.0, max(1.0, ra)))
            except Exception:
                time.sleep(2.0)

        return r.status_code
    except Exception as e:
        print("[DISCORD ERROR]", e)
        return 0


# =======================
# VINTED PARSING
# =======================
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

        title = extract_title_from_anchor(a) or "Annonce Vinted"
        image_url = extract_image_from_anchor(a)

        out[cid] = {
            "id": cid,
            "title": title,
            "url": clean_url,
            "image": image_url,
            "price": None,
            "desc": None,
        }
    return list(out.values())


# =======================
# ITEM DETAILS (title + image + price + desc)
# =======================
def fetch_item_details(item_url: str):
    html = fetch_html(item_url)
    if not html:
        return None

    soup = BeautifulSoup(html, "lxml")

    def meta_prop(p):
        tag = soup.select_one(f"meta[property='{p}']")
        return (tag.get("content") if tag and tag.get("content") else None)

    title = meta_prop("og:title")
    image = meta_prop("og:image")
    desc  = meta_prop("og:description")

    # Prix: meta product:price:amount si dispo
    price = meta_prop("product:price:amount")
    price_f = None
    if price:
        try:
            price_f = float(price)
        except Exception:
            price_f = None

    # Fallback JSON-LD
    if price_f is None:
        for s in soup.select("script[type='application/ld+json']"):
            try:
                data = json.loads(s.get_text(strip=True))
            except Exception:
                continue
            candidates = data if isinstance(data, list) else [data]
            for obj in candidates:
                offers = (obj or {}).get("offers") or {}
                if isinstance(offers, dict) and offers.get("price") is not None:
                    try:
                        price_f = float(offers.get("price"))
                        break
                    except Exception:
                        pass
            if price_f is not None:
                break

    return {
        "title": title.strip() if isinstance(title, str) and title.strip() else None,
        "image": image.strip() if isinstance(image, str) and image.strip() else None,
        "desc":  desc.strip()  if isinstance(desc, str)  and desc.strip()  else None,
        "price": price_f,
    }


# =======================
# SCORING V2 (utile + explicable)
# =======================
def _contains_any(t: str, words):
    return any(w in t for w in words)

def _count_any(t: str, words):
    return sum(1 for w in words if w in t)

def _range_from_query_name(query_name: str):
    # "Omega (100-450)" -> (100, 450)
    m = re.search(r"\((\d+)\s*-\s*(\d+)\)", query_name or "")
    if not m:
        return None, None
    return float(m.group(1)), float(m.group(2))

def score_listing_v2(title: str, desc: str | None, price: float | None, query_name: str):
    """
    Retourne: (score:int, reasons:list[str])
    Conçu pour classer rapidement les annonces flip.
    """
    base = (title or "")
    t = norm(base)
    d = norm(desc or "")

    # travailler sur "texte global" (titre + une partie description)
    all_text = (t + " " + d)[:1200]

    reasons = []
    score = 50

    # 1) Mouvement
    auto_words = ["automatique", "automatic", "auto "]
    mech_words = ["mecanique", "mécanique", "manual", "hand-wound", "hand wound", "remontage manuel"]
    quartz_words = ["quartz", "pile", "battery", "digital", "electronique", "électronique", "electronic"]

    if _contains_any(all_text, auto_words):
        score += 22; reasons.append("+auto")
    if _contains_any(all_text, mech_words):
        score += 16; reasons.append("+mécanique")
    if _contains_any(all_text, quartz_words):
        score -= 35; reasons.append("-quartz/pile")

    # 2) Accessoires / parts (downrank fort)
    accessory_words = ["strap", "bracelet", "maillon", "boucle", "ardillon", "deployant", "déployant", "rubber", "cuir", "nato"]
    parts_words = ["piece", "pièce", "pieces", "pièces", "parts", "spares", "couronne", "verre", "aiguilles", "aiguille",
                   "mouvement", "calibre", "cadran", "lunette", "fond", "boitier", "boîtier"]

    acc_hits = _count_any(all_text, accessory_words)
    part_hits = _count_any(all_text, parts_words)

    has_mm = ("mm" in all_text) and any(ch.isdigit() for ch in all_text)
    if acc_hits >= 2 or (has_mm and acc_hits >= 1):
        score -= 25; reasons.append("-accessoire?")
    if part_hits >= 2:
        score -= 30; reasons.append("-pièce/parts?")
    if "strap only" in all_text or "bracelet only" in all_text or "for parts" in all_text or "parts only" in all_text:
        score -= 40; reasons.append("-only parts")

    if "montre" in all_text or "watch" in all_text:
        score += 6; reasons.append("+montre")

    # 3) Opportunité prix (bas de fourchette = meilleur)
    if price is not None:
        lo, hi = _range_from_query_name(query_name)
        if lo is not None and hi is not None and hi > lo:
            pct = (price - lo) / (hi - lo)
            if pct <= 0.25:
                score += 16; reasons.append("+prix bas")
            elif pct <= 0.50:
                score += 8; reasons.append("+prix ok")
            elif pct >= 0.85:
                score -= 6; reasons.append("-prix haut")
        else:
            if price < 200:
                score += 10; reasons.append("+<200€")
            elif price > 400:
                score -= 5; reasons.append("->400€")

    # 4) Qualité / signaux de confiance
    good_words = ["vintage", "revision", "révision", "revisée", "révisée", "serviced",
                  "authentique", "original", "origine", "full set", "boite papiers", "boîte papiers", "facture"]
    bad_words = ["replica", "réplique", "fake", "mod", "custom", "hommage", "homage"]

    if _contains_any(all_text, good_words):
        score += 8; reasons.append("+infos")
    if _contains_any(all_text, bad_words):
        score -= 25; reasons.append("-fake/mod")

    score = max(0, min(100, score))
    return score, reasons[:6]


# =======================
# MAIN
# =======================
def main():
    cfg = load_json("config.json", {})
    state = load_json(STATE_FILE, {"seen_ids": [], "alerted_ids": []})

    # Assure les clés
    if "seen_ids" not in state:
        state["seen_ids"] = []
    if "alerted_ids" not in state:
        state["alerted_ids"] = []

    seen = set(state.get("seen_ids", []))
    alerted = set(state.get("alerted_ids", []))

    new_seen = set(seen)
    new_alerted = set(alerted)

    total_sent = 0
    item_fetch_budget = MAX_ITEM_FETCH_PER_RUN
    img_dl_budget = MAX_IMAGE_DL_PER_RUN

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

            for it in items:
                cid = it["id"]

                # Vu dès qu'on le voit
                if cid not in new_seen:
                    new_seen.add(cid)

                # Déjà alerté historiquement -> jamais renvoyer
                if cid in new_alerted:
                    continue

                # Enrichissement si manque title / image / price / desc
                need_details = (it["title"] == "Annonce Vinted") or (not it.get("image")) or (it.get("price") is None) or (it.get("desc") is None)
                if need_details and item_fetch_budget > 0:
                    details = fetch_item_details(it["url"])
                    item_fetch_budget -= 1
                    time.sleep(HTTP_SLEEP_SEC)

                    if details:
                        if details.get("title"):
                            it["title"] = details["title"]
                        if not it.get("image") and details.get("image"):
                            it["image"] = details["image"]
                        if details.get("price") is not None:
                            it["price"] = details["price"]
                        if details.get("desc"):
                            it["desc"] = details["desc"]

                # Si toujours pas de titre exploitable -> skip (évite bruit/accessoires)
                if it["title"] == "Annonce Vinted":
                    continue

                # Filtre include/exclude sur titre (et aussi sur desc pour attraper "bracelet seul" etc.)
                if not matches(it["title"], include, exclude):
                    continue
                if it.get("desc") and not matches(it["desc"], [], exclude):
                    continue

                # Scoring V2 (titre + desc + prix)
                sc, why = score_listing_v2(it["title"], it.get("desc"), it.get("price"), name)

                if sc < MIN_SCORE_TO_SEND:
                    # si tu veux tout recevoir, laisse MIN_SCORE_TO_SEND = 0
                    continue

                # Caps
                if total_sent >= MAX_ALERTS_PER_RUN:
                    print("[STOP] max alerts per run reached (global)")
                    break
                if query_sent >= MAX_ALERTS_PER_QUERY:
                    print(f"[STOP] max alerts per query reached ({name})")
                    break

                # Prépare message
                price = it.get("price")
                price_line = f"\nPrix: {price:.0f}€" if isinstance(price, (int, float)) else ""
                why_line = " | ".join(why) if why else "—"

                # Image: on tente PJ seulement si budget
                img_url = it.get("image") if img_dl_budget > 0 else None

                status = discord_notify(
                    webhook_env,
                    f"🔔 **{name}**\n{it['title']}{price_line}\n{it['url']}\n**Score:** {sc}/100 ({why_line})",
                    title=it["title"],
                    url=it["url"],
                    image_url=img_url,
                    score=sc,
                )

                if _discord_success(status):
                    new_alerted.add(cid)
                    total_sent += 1
                    query_sent += 1
                    if img_url:
                        img_dl_budget -= 1

                time.sleep(DISCORD_SLEEP_SEC)

            if total_sent >= MAX_ALERTS_PER_RUN:
                break

            time.sleep(HTTP_SLEEP_SEC)

        if total_sent >= MAX_ALERTS_PER_RUN:
            break

    # Sauvegarde état
    state["seen_ids"] = list(new_seen)[-MAX_SEEN:]
    state["alerted_ids"] = list(new_alerted)[-MAX_ALERTED:]
    save_json(STATE_FILE, state)

    print(f"[END] sent={total_sent} seen_ids={len(state['seen_ids'])} alerted_ids={len(state['alerted_ids'])} item_fetch_left={item_fetch_budget} img_dl_left={img_dl_budget}")


if __name__ == "__main__":
    main()