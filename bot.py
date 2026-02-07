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

# Filtre final (gain de temps)
SCORE_MIN_TO_ALERT = 60  # <- ajuste 55/60/65 selon ton flux

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


# ------------------ scoring FLIP (orienté revente rapide) ------------------

def score_badge(score: int) -> str:
    if score >= 78:
        return "🟢"
    if score >= 60:
        return "🟠"
    return "🔴"


def score_listing(title: str, query_name: str = ""):
    """
    Scoring FLIP (0-100) basé sur le TITRE.
    Objectif: revente rapide, faible risque, faible effort.
    Retourne: (score:int, reasons:list[str])
    """
    t = norm(title)
    score = 50
    reasons = []

    def add(delta: int, why: str):
        nonlocal score
        score += delta
        reasons.append((delta, why))

    # --- Dictionnaires (mots clés) ---
    watch_signals = [
        "montre", "watch", "automatic", "automatique", "mecanique", "mécanique",
        "chronographe", "chrono", "date", "cal.", "calibre", "ref", "reference", "référence"
    ]

    # Accessoires / pièces : pénalise fort, sans SKIP (Option B)
    accessory_signals = [
        "bracelet", "strap", "band", "maillon", "boucle", "clasp", "ardillon",
        "couronne", "verre", "cadran", "dial", "aiguille", "aiguilles",
        "lunette", "bezel", "insert", "fond", "caseback", "boitier", "boîtier",
        "outil", "outils", "accessoire", "accessoires",
        "parts", "spares", "spare", "piece", "pièce", "pieces", "pièces"
    ]

    accessory_only_phrases = [
        "bracelet seul", "bracelets seuls", "strap only", "bracelet only",
        "cadran seul", "couronne seule", "verre seul", "maillons seuls"
    ]

    # --- 0) Accessoire vs Montre (règle robuste) ---
    is_accessory = any(k in t for k in accessory_signals) or any(p in t for p in accessory_only_phrases)
    looks_like_watch = any(k in t for k in watch_signals)

    if any(p in t for p in accessory_only_phrases):
        add(-70, "accessoire/pièce 'seul' (flip très faible)")
    elif is_accessory and not looks_like_watch:
        add(-55, "probable accessoire/pièce (flip faible)")
    elif is_accessory and looks_like_watch:
        add(-18, "mention accessoire (prudence)")

    # --- 1) Mouvement / simplicité ---
    if any(k in t for k in ["automatique", "automatic", "auto "]):
        add(+22, "automatique (liquidité ↑)")
    if any(k in t for k in ["mecanique", "mécanique", "remontage manuel", "hand winding", "manual"]):
        add(+16, "mécanique/manuel (liquidité ↑)")
    if any(k in t for k in ["quartz", "pile", "battery", "digital", "électronique", "electronique", "electronic"]):
        add(-40, "quartz/digital (flip ↓)")

    # --- 2) Etat / fiabilité ---
    if any(k in t for k in ["fonctionne", "fonctionnel", "marche", "ok", "tested", "testee", "testée", "testé"]):
        add(+10, "fonctionnement annoncé")
    if any(k in t for k in ["révisée", "revisee", "révision", "revision", "serviced", "service"]):
        add(+14, "révisée / service (risque ↓)")
    if any(k in t for k in ["garantie", "warranty"]):
        add(+6, "garantie (risque ↓)")

    # --- 3) Red flags lourds ---
    heavy_redflags = [
        "pour pieces", "pour pièces", "hs", "ne marche pas", "ne fonctionne pas",
        "a reparer", "à réparer", "a réparer", "réparer", "repair",
        "cassé", "cassée", "casse", "incomplet", "incomplète",
        "manque", "missing"
    ]
    if any(k in t for k in heavy_redflags):
        add(-65, "HS/à réparer/pour pièces (risque max)")

    # --- 4) Red flags légers (incertitude) ---
    light_redflags = [
        "a verifier", "à vérifier", "a vérifier",
        "je ne sais pas", "je sais pas",
        "non teste", "non testé", "non testee", "non testée",
        "dans son jus"
    ]
    if any(k in t for k in light_redflags):
        add(-18, "incertitude/non testé (risque ↑)")

    # --- 5) Full set / complétude ---
    if any(k in t for k in ["boite", "boîte", "box", "écrin", "ecrin", "papiers", "papers", "certificat", "full set", "complet", "complète"]):
        add(+8, "boîte/papiers/full set (revente + facile)")
    if any(k in t for k in ["sans bracelet", "sans brac", "bracelet absent"]):
        add(-6, "bracelet absent (revente + dure)")

    # --- 6) Indices négociation (marge potentielle) ---
    if any(k in t for k in ["urgent", "demenagement", "déménagement", "a debattre", "à débattre", "negociable", "négociable", "faire offre", "offre"]):
        add(+6, "prix potentiellement négociable")
    if any(k in t for k in ["prix ferme", "non negociable", "non négociable"]):
        add(-4, "prix ferme (marge ↓)")

    # --- 7) Taille (liquidité) ---
    m = re.search(r"\b(\d{2})\s*mm\b", t)
    if m:
        mm = int(m.group(1))
        if 34 <= mm <= 41:
            add(+4, f"taille {mm}mm (liquide)")
        elif mm <= 32 or mm >= 44:
            add(-6, f"taille {mm}mm (liquidité ↓)")

    # --- 8) Titre trop générique ---
    if len(t) < 12 or t in ["montre", "omega", "tissot", "longines", "mido", "frederique constant", "frederique"]:
        add(-10, "titre trop générique (bruit/risque)")

    # Clamp
    score = max(0, min(100, score))

    # Garder les 6 raisons les plus impactantes
    reasons_sorted = sorted(reasons, key=lambda x: abs(x[0]), reverse=True)[:6]
    reasons_out = [f"{d:+} {w}" for d, w in reasons_sorted]

    return score, reasons_out


# ------------------ discord (format + raisons + image) ------------------

def discord_notify(
    webhook_env: str,
    title: str,
    url: str,
    image_url: str = None,
    score: int = None,
    query_name: str = None,
    reasons=None
):
    wh = os.environ.get(webhook_env)
    if not wh:
        print("[NO WEBHOOK]", webhook_env)
        return 0

    s = int(score or 0)
    badge = score_badge(s)
    reasons = reasons or []

    # Texte (mobile friendly)
    content_lines = []
    if query_name:
        content_lines.append(f"🔔 **{query_name}**")
    content_lines.append(f"{badge} **Score FLIP {s}/100**")
    content_lines.append(url)
    content = "\n".join(content_lines)

    # Embed
    desc_lines = [f"{badge} **Score FLIP {s}/100**"]
    if reasons:
        desc_lines.append("")
        desc_lines.append("**Pourquoi :**")
        desc_lines.extend([f"• {r}" for r in reasons[:6]])

    emb = {
        "title": title[:250],
        "url": url,
        "description": "\n".join(desc_lines)[:3900],
        "footer": {"text": "Vinted • WatchAlertBot"}
    }
    if image_url:
        emb["image"] = {"url": image_url}

    payload = {"content": content, "embeds": [emb]}

    try:
        r = requests.post(wh, json=payload, timeout=15)
        print(f"[DISCORD] {webhook_env} status={r.status_code}" + (" (no-attachment)" if not image_url else ""))
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

                sc, why = score_listing(it["title"], query_name=name)

                # ---- Filtre final : gain de temps ----
                if sc < SCORE_MIN_TO_ALERT:
                    continue

                new_alerted.add(cid)

                discord_notify(
                    webhook_env,
                    title=it["title"],
                    url=it["url"],
                    image_url=it.get("image"),
                    score=sc,
                    query_name=name,
                    reasons=why
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

    print(
        f"[END] sent={total_sent} "
        f"seen_ids={len(state['seen_ids'])} "
        f"alerted_ids={len(state['alerted_ids'])} "
        f"item_fetch_left={item_fetch_budget}"
    )


if __name__ == "__main__":
    main()