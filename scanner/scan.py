#!/usr/bin/env python3
"""Angebots-Radar – täglicher Scanner.

Holt die aktuellen Prospekt-Angebote für die PLZ rund um 15806 (Quelle: marktguru),
führt eine eigene Preis-Historie und berechnet einen Deal-Score.
Ausgabe: data/offers.json (für die App) und data/history.json (Gedächtnis).

Nur Standardbibliothek – läuft ohne Installation in GitHub Actions.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import statistics
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "scanner" / "config.json"
OFFERS_PATH = ROOT / "data" / "offers.json"
HISTORY_PATH = ROOT / "data" / "history.json"

SITE = "https://www.marktguru.de"
API = "https://api.marktguru.de/api/v1"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

TZ = dt.timezone(dt.timedelta(hours=1))  # Europe/Berlin (Winterzeit reicht für Datumslogik)


def log(*a):
    print(*a, flush=True)


# --------------------------------------------------------------------------- HTTP

def http_get(url: str, headers: dict | None = None, timeout: int = 25) -> tuple[int, str]:
    h = {"user-agent": UA, "accept-language": "de-DE,de;q=0.9"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace") if e.fp else ""


# --------------------------------------------------------------------------- Keys

def _find_keys_in_obj(obj):
    if isinstance(obj, dict):
        if "apiKey" in obj and isinstance(obj["apiKey"], str):
            return obj["apiKey"], obj.get("clientKey")
        for v in obj.values():
            r = _find_keys_in_obj(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_keys_in_obj(v)
            if r:
                return r
    return None


def _candidates(text: str) -> list[str]:
    found = []
    for rx in (r"x-apikey\s*['\"]?\s*[:=]\s*['\"]([^'\"]{10,})['\"]",
               r"apiKey\s*['\"]?\s*[:=]\s*['\"]([^'\"]{10,})['\"]"):
        found += re.findall(rx, text, flags=re.I)
    for v in re.findall(r"[A-Za-z0-9+/]{40,60}={1,2}", text):
        found.append(v)
    seen, out = set(), []
    for c in found:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def headers_for(keys) -> dict:
    h = {"x-apikey": keys[0], "accept": "application/json"}
    if keys[1]:
        h["x-clientkey"] = keys[1]
    return h


def validate(keys, zip_code: str) -> bool:
    url = f"{API}/offers/search?" + urllib.parse.urlencode(
        {"as": "web", "q": "milch", "limit": 1, "offset": 0, "zipCode": zip_code})
    status, _ = http_get(url, headers_for(keys))
    return status == 200


def get_keys(zip_code: str):
    """API-Schlüssel so beschaffen, wie es die marktguru-Webseite selbst tut."""
    env_key = os.environ.get("MG_APIKEY")
    if env_key:
        keys = (env_key, os.environ.get("MG_CLIENTKEY"))
        if validate(keys, zip_code):
            log("✓ API-Key aus Umgebungsvariable gültig")
            return keys

    html = ""
    for path in ("/", "/suche?q=milch", "/search?q=milch"):
        status, html = http_get(SITE + path, {"accept": "text/html"})
        if status == 200 and html:
            break
    if not html:
        raise RuntimeError("marktguru.de nicht erreichbar")

    # 1) eingebettete JSON-Konfiguration
    for blob in re.findall(r"<script[^>]*type=[\"']application/json[\"'][^>]*>(.*?)</script>", html, re.S):
        try:
            keys = _find_keys_in_obj(json.loads(blob))
        except Exception:
            keys = None
        if keys and validate(keys, zip_code):
            log("✓ API-Key aus Seiten-Konfiguration")
            return keys

    # 2) Kandidaten im HTML und in den JS-Bundles
    cands = _candidates(html)
    scripts = []
    for src in re.findall(r"<script[^>]+src=[\"']([^\"']+)[\"']", html, re.I):
        if src.startswith("//"):
            src = "https:" + src
        elif src.startswith("/"):
            src = SITE + src
        if src.startswith("http"):
            scripts.append(src)
    for s in scripts[:25]:
        st, txt = http_get(s)
        if st == 200:
            cands += _candidates(txt)
    client_cands = re.findall(r"clientKey\s*['\"]?\s*[:=]\s*['\"]([^'\"]{10,})['\"]", html)
    client = client_cands[0] if client_cands else None
    for c in dict.fromkeys(cands):
        for keys in ((c, client), (c, None)):
            if validate(keys, zip_code):
                log("✓ API-Key aus Skript-Scan")
                return keys
    raise RuntimeError("Kein gültiger API-Key gefunden – marktguru hat evtl. die Seite geändert")


# --------------------------------------------------------------------------- Abruf

def search(keys, q: str, zip_code: str, limit: int, offset: int):
    url = f"{API}/offers/search?" + urllib.parse.urlencode(
        {"as": "web", "q": q, "limit": limit, "offset": offset, "zipCode": zip_code})
    status, body = http_get(url, headers_for(keys))
    if status == 401:
        raise PermissionError("401")
    if status != 200:
        raise RuntimeError(f"HTTP {status}")
    return json.loads(body)


def fetch_all(cfg) -> tuple[dict, list[str], dict]:
    zips = list(cfg["zips"].keys())
    keys = get_keys(cfg["home_zip"])
    raw: dict[str, dict] = {}
    seen_zip: dict[str, set] = {}
    errors: list[str] = []
    stats = {"requests": 0}
    limit = cfg["page_limit"]
    pause = cfg["request_pause_s"]

    def run(q, z, max_pages):
        nonlocal keys
        offset, pages, got_total = 0, 0, 0
        while pages < max_pages:
            try:
                data = search(keys, q, z, limit, offset)
            except PermissionError:
                log("↻ Key abgelaufen, hole neuen")
                keys = get_keys(cfg["home_zip"])
                data = search(keys, q, z, limit, offset)
            stats["requests"] += 1
            res = data.get("results") or []
            for o in res:
                oid = str(o.get("id"))
                raw[oid] = o
                seen_zip.setdefault(oid, set()).add(z)
            got_total += len(res)
            total = data.get("totalResults") or 0
            pages += 1
            offset += limit
            time.sleep(pause)
            if len(res) < limit or offset >= total:
                break
        return got_total

    for z in zips:
        broad_hits = 0
        for q in cfg["broad_queries"]:
            try:
                n = run(q, z, cfg["max_pages_broad"])
                broad_hits = max(broad_hits, n)
                log(f"  {z} breit '{q}': {n}")
            except Exception as e:  # noqa: BLE001
                errors.append(f"{z} '{q}': {e}")
        # Breitensuche liefert alles? Dann Begriffe nur für die Heimat-PLZ zur Absicherung.
        terms = cfg["terms"] if (broad_hits < 400 or z == cfg["home_zip"]) else []
        for t in terms:
            try:
                run(t, z, cfg["max_pages_term"])
            except Exception as e:  # noqa: BLE001
                errors.append(f"{z} '{t}': {e}")
        log(f"✓ PLZ {z}: gesamt bisher {len(raw)} Angebote")

    for oid, zs in seen_zip.items():
        raw[oid]["_zips"] = sorted(zs)
    return raw, errors, stats


# --------------------------------------------------------------------------- Aufbereitung

def num(v):
    try:
        f = float(v)
        return f if math.isfinite(f) and f >= 0 else None
    except (TypeError, ValueError):
        return None


def slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def parse_dt(s):
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


CLOTHING = re.compile(
    r"\b(mode|bekleidung|kleidung|textil|fashion|schuhe?|sneaker|stiefel|jacke|mantel|hose|jeans|"
    r"shirt|t-shirt|pullover|pulli|hoodie|sweatshirt|kleid|rock|bluse|hemd|socken|strumpf|"
    r"unterwäsche|unterhose|boxershorts|slip|bh|leggings|schlafanzug|pyjama|mütze|schal|handschuhe)\b",
    re.I)

GROUPS = [
    ("auto", r"\b(auto|kfz|pkw|reifen|winterreifen|sommerreifen|felge|motoröl|motorol|autowäsche|waschanlage|"
             r"autopflege|scheibenfrost|frostschutz|wischblatt|wischblätter|autobatterie|tankstelle|werkstatt|"
             r"inspektion|ölwechsel|dachbox|navi)\w*"),
    ("elektronik", r"\b(elektronik|fernseher|tv|smart-?tv|oled|qled|smartphone|handy|iphone|galaxy|tablet|ipad|"
                   r"laptop|notebook|pc|monitor|kopfhörer|headphones?|earbuds|lautsprecher|soundbar|konsole|"
                   r"playstation|ps5|xbox|nintendo|switch|smartwatch|kamera|drucker|festplatte|ssd|router|"
                   r"powerbank|ladegerät|usb|hdmi|staubsauger|saugroboter|kaffeevollautomat|kaffeemaschine|"
                   r"fritteuse|airfryer|mikrowelle|kühlschrank|gefrier|waschmaschine|trockner|geschirrspüler|"
                   r"küchenmaschine|mixer|wasserkocher|toaster|elektro|haushaltsgerät|media ?markt|saturn|expert|"
                   r"euronics|cyberport)\w*"),
    ("drogerie", r"\b(drogerie|kosmetik|pflege|körper|haar|shampoo|duschgel|deo|zahn|rasier|parfum|creme|"
                 r"windel|baby|waschmittel|weichspüler|spülmittel|reiniger|toilettenpapier|küchenrolle|"
                 r"taschentücher|dm|rossmann|apotheke|vitamin|nahrungsergänzung)\w*"),
    ("baumarkt", r"\b(baumarkt|baumärkt|werkzeug|akkuschrauber|bohr|säge|rasenmäher|garten(?!kräuter|gurke)|grill(?!fleisch|wurst|käse|gemüse|soße|sauce|spieß|steak|kohle)|pflanze|blumen|erde|"
                 r"dünger|holz|farbe|lack|tapete|lampe|leuchte|led|möbel|matratze|bett|sofa|regal|deko|"
                 r"heizlüfter|brennholz|pellets|obi|hornbach|bauhaus|toom|hagebau|globus baumarkt|ikea|"
                 r"pfanne|topf|küche|haushalt(?!sgerät))\w*"),
    ("tier", r"\b(tier|katze|katzen|hund|hunde|futter|katzenstreu|fressnapf|zoo)\w*"),
    ("getraenke", r"\b(getränk|bier|pils|wein|sekt|prosecco|spirituose|wodka|whisky|rum|gin|likör|wasser|"
                  r"mineralwasser|saft|schorle|limonade|cola|fanta|sprite|energy|kaffee|espresso|tee)\w*"),
    ("lebensmittel", r"\b(lebensmittel|supermarkt|supermärkt|discounter|fleisch|wurst|käse|milch|butter|joghurt|quark|"
                     r"obst|gemüse|brot|backwaren|tiefkühl|süßwaren|snack|nudel|reis|fisch|eier|hähnchen|"
                     r"pizza|schokolade|chips|müsli|konserve|gewürz|feinkost|molkerei|protein)\w*"),
]
GROUPS = [(g, re.compile(p, re.I)) for g, p in GROUPS]


def classify(text_meta: str, text_prod: str) -> str:
    for g, rx in GROUPS:
        if rx.search(text_meta):
            return g
    for g, rx in GROUPS:
        if rx.search(text_prod):
            return g
    return "sonstiges"


def is_clothing(cats: list[str], inds: list[str], name: str) -> bool:
    meta = " ".join(cats + inds)
    if CLOTHING.search(meta):
        # Gegenprobe: "Waschmittel für Kleidung" o. Ä. ist keine Kleidung
        if not re.search(r"wasch|reinig|pflege|schrank|bügel", meta, re.I):
            return True
    return bool(CLOTHING.search(name)) and not re.search(r"wasch|reinig|bügel|schrank|haken", name, re.I)


def image_url(o) -> str | None:
    imgs = o.get("images") or {}
    urls = imgs.get("urls") or {}
    for k in ("medium", "small", "large"):
        u = urls.get(k)
        if isinstance(u, str) and u.startswith("http"):
            return u
    return None


def normalize(o, now: dt.datetime, lookahead: dt.timedelta):
    price = num(o.get("price"))
    if price is None or price == 0:
        return None
    adv = (o.get("advertisers") or [{}])[0] or {}
    brand = ((o.get("brand") or {}).get("name") or "").strip()
    prod = ((o.get("product") or {}).get("name") or "").strip()
    desc = (o.get("description") or "").strip()
    cats = [c.get("name") for c in (o.get("categories") or []) if isinstance(c, dict) and c.get("name")]
    inds = [i.get("name") for i in (o.get("industries") or []) if isinstance(i, dict) and i.get("name")]
    inds += [a.get("industryName") for a in (o.get("advertisers") or []) if isinstance(a, dict) and a.get("industryName")]
    name = prod or desc[:80]
    if not name:
        return None
    if is_clothing(cats, inds, f"{prod} {desc}"):
        return "CLOTHING"

    froms, tos = [], []
    for v in o.get("validityDates") or []:
        f, t = parse_dt(v.get("from")), parse_dt(v.get("to"))
        if f:
            froms.append(f)
        if t:
            tos.append(t)
    vfrom = min(froms) if froms else None
    vto = max(tos) if tos else None
    if vto and vto < now:
        return None
    if vfrom and vfrom > now + lookahead:
        return None

    unit = (o.get("unit") or {}).get("shortName")
    retailer = (adv.get("name") or "Unbekannt").strip()
    group = classify(" ".join(cats + inds + [retailer]), f"{brand} {prod} {desc}")

    return {
        "id": str(o.get("id")),
        "key": slug(f"{brand}|{prod}") or slug(desc[:60]),
        "name": name,
        "brand": brand,
        "desc": desc[:220],
        "retailer": retailer,
        "retailerKey": adv.get("uniqueName") or slug(retailer),
        "group": group,
        "cats": cats[:3],
        "price": round(price, 2),
        "oldPrice": num(o.get("oldPrice")),
        "unitPrice": num(o.get("referencePrice")),
        "unit": unit,
        "loyalty": bool(o.get("requiresLoyalityMembership")),
        "from": vfrom.isoformat() if vfrom else None,
        "to": vto.isoformat() if vto else None,
        "img": image_url(o),
        "url": o.get("externalUrl"),
        "zips": o.get("_zips", []),
    }


# --------------------------------------------------------------------------- Historie & Score

def iso_week(d: dt.date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def load_json(p: Path, default):
    try:
        return json.loads(p.read_text("utf-8"))
    except Exception:
        return default


def update_history(hist: dict, offers: list[dict], today: dt.date, max_obs: int):
    meta = hist.setdefault("_meta", {"scanDates": []})
    ds = today.isoformat()
    if ds not in meta["scanDates"]:
        meta["scanDates"].append(ds)
    meta["scanDates"] = meta["scanDates"][-400:]
    items = hist.setdefault("items", {})
    for o in offers:
        it = items.setdefault(o["key"], {"name": o["name"], "brand": o["brand"], "obs": {}})
        it["name"] = o["name"]
        obs = it["obs"]
        if o["id"] not in obs:
            obs[o["id"]] = {"d": ds, "w": iso_week(today), "p": o["price"], "o": o["oldPrice"],
                            "u": o["unitPrice"], "un": o["unit"], "r": o["retailer"]}
        if len(obs) > max_obs:
            for k in sorted(obs, key=lambda k: obs[k]["d"])[: len(obs) - max_obs]:
                del obs[k]
    # Altlasten: Produkte, die > 1 Jahr nicht mehr auftauchten
    cutoff = (today - dt.timedelta(days=400)).isoformat()
    for k in [k for k, it in items.items() if all(v["d"] < cutoff for v in it["obs"].values())]:
        del items[k]


def compare_value(o_or_obs, unit_key="unitPrice", unit_name_key="unit"):
    return o_or_obs.get(unit_key), o_or_obs.get(unit_name_key)


def score_offer(o: dict, hist: dict, today: dt.date) -> None:
    items = hist.get("items", {})
    it = items.get(o["key"], {"obs": {}})
    prev = [v for k, v in it["obs"].items() if k != o["id"]]

    # Vergleichswert: Grundpreis (€/kg …) wenn gleiche Einheit, sonst Stückpreis
    cur_u, cur_un = o["unitPrice"], o["unit"]
    same_unit = [v["u"] for v in prev if cur_u and v.get("u") and v.get("un") == cur_un]
    if cur_u and len(same_unit) >= 2:
        cur_cmp, prev_cmp = cur_u, same_unit
    else:
        cur_cmp, prev_cmp = o["price"], [v["p"] for v in prev if v.get("p")]

    # Referenzpreis ("Normalpreis")
    ref, ref_src = None, None
    if o["oldPrice"] and o["oldPrice"] > o["price"] * 1.001 and o["oldPrice"] < o["price"] * 10:
        ref, ref_src = o["oldPrice"], "prospekt"
    else:
        prev_old = [v["o"] for v in prev if v.get("o") and v["o"] > (v.get("p") or 0)]
        if len(prev_old) >= 1:
            med_old = statistics.median(prev_old)
            if med_old > o["price"]:
                ref, ref_src = round(med_old, 2), "historie"

    disc = (1 - o["price"] / ref) if ref else None
    if disc is not None and not (0 < disc < 0.9):
        disc, ref, ref_src = None, None, None

    n_prev = len(prev_cmp)
    hist_low = min(prev_cmp) if n_prev else None
    hist_med = statistics.median(prev_cmp) if n_prev else None
    vs_hist = (1 - cur_cmp / hist_med) if (hist_med and n_prev >= 2) else None
    best = bool(n_prev >= 2 and (cur_cmp < hist_low * 0.999 or (cur_cmp <= hist_low * 1.0001 and hist_med > hist_low * 1.01)))

    # Dauerangebot? (in vielen der beobachteten Wochen im Angebot)
    weeks_scanned = {iso_week(dt.date.fromisoformat(d)) for d in hist.get("_meta", {}).get("scanDates", [])}
    weeks_on = {v["w"] for v in it["obs"].values()}
    permanent = len(weeks_scanned) >= 4 and len(weeks_on) / max(1, len(weeks_scanned)) >= 0.6

    s = 0.0
    if disc:
        s += min(disc, 0.6) / 0.6 * 60
        saving = ref - o["price"]
        s += min(15.0, 7.5 * math.log10(1 + saving))
    elif n_prev == 0:
        s += 8
    if best:
        s += 15
    if vs_hist and vs_hist > 0.03:
        s += min(12.0, vs_hist * 40)
    if vs_hist is not None and vs_hist < -0.05:
        s -= 10  # teurer als sonst im Angebot
    if permanent:
        s -= 15
    if o["loyalty"]:
        s -= 3
    s = max(0, min(100, round(s)))

    o.update({
        "ref": ref, "refSrc": ref_src,
        "disc": round(disc * 100) if disc else None,
        "saving": round(ref - o["price"], 2) if ref else None,
        "histN": n_prev, "histLow": round(hist_low, 2) if hist_low else None,
        "histMed": round(hist_med, 2) if hist_med else None,
        "vsHist": round(vs_hist * 100) if vs_hist is not None else None,
        "cmpUnit": cur_un if cur_cmp == cur_u and cur_u else None,
        "best": best, "permanent": permanent, "score": s,
        "label": "top" if s >= 70 else "gut" if s >= 50 else "ok" if s >= 30 else "normal",
    })


# --------------------------------------------------------------------------- Main

def main():
    cfg = json.loads(CONFIG_PATH.read_text("utf-8"))
    now = dt.datetime.now(dt.timezone.utc)
    today = now.astimezone(TZ).date()
    previous = load_json(OFFERS_PATH, {"meta": {}, "offers": []})

    try:
        raw, errors, stats = fetch_all(cfg)
    except Exception as e:  # noqa: BLE001
        log(f"✗ Scan fehlgeschlagen: {e}")
        previous.setdefault("meta", {})["lastError"] = f"{now.isoformat()}: {e}"
        OFFERS_PATH.write_text(json.dumps(previous, ensure_ascii=False, separators=(",", ":")), "utf-8")
        sys.exit(1)

    finished = process(raw, cfg, now, today)
    finished["meta"].update({"errors": errors[:30], "requests": stats["requests"]})
    if not finished["offers"]:
        log("✗ 0 Angebote – vorherige Daten bleiben erhalten")
        previous.setdefault("meta", {})["lastError"] = f"{now.isoformat()}: 0 Angebote erhalten"
        OFFERS_PATH.write_text(json.dumps(previous, ensure_ascii=False, separators=(",", ":")), "utf-8")
        sys.exit(1)
    log(f"✓ Fertig: {len(finished['offers'])} Angebote, {stats['requests']} Abrufe, {len(errors)} Fehler")


def process(raw: dict, cfg: dict, now: dt.datetime, today: dt.date) -> dict:
    """Rohdaten → bewertete Angebote; schreibt offers.json und history.json."""
    lookahead = dt.timedelta(days=cfg["lookahead_days"])
    offers, clothing = [], 0
    for o in raw.values():
        n = normalize(o, now, lookahead)
        if n == "CLOTHING":
            clothing += 1
        elif n:
            offers.append(n)

    hist = load_json(HISTORY_PATH, {})
    update_history(hist, offers, today, cfg["history_max_obs"])
    for o in offers:
        score_offer(o, hist, today)
    offers.sort(key=lambda o: (-o["score"], -(o["disc"] or 0)))

    groups, retailers = {}, {}
    for o in offers:
        groups[o["group"]] = groups.get(o["group"], 0) + 1
        retailers[o["retailer"]] = retailers.get(o["retailer"], 0) + 1

    out = {
        "meta": {
            "generated": now.isoformat(),
            "source": "marktguru.de (Prospektangebote)",
            "homeZip": cfg["home_zip"],
            "zips": cfg["zips"],
            "count": len(offers),
            "clothingFiltered": clothing,
            "groups": groups,
            "retailers": dict(sorted(retailers.items(), key=lambda kv: -kv[1])),
            "historyItems": len(hist.get("items", {})),
            "scanDays": len(hist.get("_meta", {}).get("scanDates", [])),
            "lastError": None,
        },
        "offers": offers,
    }
    OFFERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    OFFERS_PATH.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), "utf-8")
    HISTORY_PATH.write_text(json.dumps(hist, ensure_ascii=False, separators=(",", ":")), "utf-8")
    return out


if __name__ == "__main__":
    main()
