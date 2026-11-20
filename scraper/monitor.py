#!/usr/bin/env python3
"""
Outlet46 Shop-Monitor
---------------------
Liest den kompletten Katalog von outlet46.de (plentymarkets) aus den
Hauptkategorien (100 Artikel pro Seite), vergleicht mit dem letzten Stand
und erkennt:

  - neue Artikel
  - Preissenkungen und Preiserhöhungen
  - geänderte UVP (Streichpreis)
  - Artikel nicht mehr gelistet (ausverkauft / entfernt)
  - Artikel wieder da (war schon einmal gelistet)

Ergebnisse landen in docs/data/ (wird vom Dashboard gelesen).
Benachrichtigungen optional über ntfy.sh, Telegram und/oder E-Mail (SMTP).

Hält sich an robots.txt: nur Kategorieseiten mit ?items= & ?page=,
keine /rest/-Schnittstelle, keine Suche/Sortierung/Filter.

Nur Python-Standardbibliothek – keine Installation nötig.
"""

from __future__ import annotations

import html
import json
import os
import re
import smtplib
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

SHOP = "https://www.outlet46.de"
SHOP_NAME = "Outlet46"
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "data"
STATE_FILE = DATA / "state.json"
CATALOG_FILE = DATA / "catalog.json"
EVENTS_FILE = DATA / "events.json"
META_FILE = DATA / "meta.json"

MAX_EVENTS = 8000
MAX_HISTORY = 60
MISSING_RUNS = 2             # so oft hintereinander fehlen, bis "nicht mehr gelistet"
PAGE_SIZE = 100
REQUEST_PAUSE = 1.0          # Sekunden zwischen Abrufen (schonend)
USER_AGENT = "Mozilla/5.0 (compatible; shop-price-monitor/1.0)"

# Reihenfolge = Priorität für die Kategorie-Zuordnung (spezifisch zuerst).
CATEGORIES = [
    "damen-bekleidung",
    "herren-bekleidung",
    "damen-schuhe",
    "herrenschuhe-guenstig-kaufen",
    "kinder",
    "sport",
    "accessoires-guenstig",
    "home-living",
    "grosser-einzelstuecke-letzte-groessen-sale",
    "a",                      # Sammel-Landingpage mit vielen weiteren Artikeln
]
LAST_SIZES_CAT = "grosser-einzelstuecke-letzte-groessen-sale"
CATEGORY_LABEL_OVERRIDE = {"a": "Weitere Angebote",
                           LAST_SIZES_CAT: "Einzelstücke & letzte Größen"}


# --------------------------------------------------------------------------- #
# Laden
# --------------------------------------------------------------------------- #
def http_get(url: str, retries: int = 4) -> str:
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                       "Accept-Language": "de-DE,de;q=0.9"})
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Abruf fehlgeschlagen: {url}: {last_err}")


def discover_categories() -> list[str]:
    """Feste Liste + neue Hauptkategorien aus der Sitemap (falls der Shop welche ergänzt)."""
    cats = list(CATEGORIES)
    try:
        xml = http_get(f"{SHOP}/sitemap/Sitemap_itemCategory.xml", retries=2)
        for loc in re.findall(r"<loc>([^<]+)</loc>", xml):
            path = urllib.parse.urlparse(loc).path.strip("/")
            parts = path.split("/")
            if len(parts) == 1 and parts[0] and parts[0] not in ("en", "fr", "it", "es") \
                    and parts[0] not in cats and parts[0] != "marken-online-outlet":
                cats.append(parts[0])
    except Exception as e:  # noqa: BLE001
        print(f"Hinweis: Sitemap nicht lesbar ({e}) – nutze feste Kategorienliste.", file=sys.stderr)
    return cats


def to_cents(txt: str | None) -> int | None:
    if not txt:
        return None
    m = re.search(r"(\d{1,3}(?:\.\d{3})*|\d+),(\d{2})", txt)
    if not m:
        return None
    return int(m.group(1).replace(".", "")) * 100 + int(m.group(2))


def clean(txt: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", txt))).strip()


CARD_SPLIT = re.compile(r'<article class="post ')


def parse_cards(page: str) -> list[dict]:
    items = []
    for card in CARD_SPLIT.split(page)[1:]:
        m_id = re.search(r'data-eg-item="(\d+)"', card)
        m_link = re.search(r'<a[^>]+class="product-list article a thumb-title[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                           card, re.S) or re.search(r'<a[^>]+href="([^"]+/a-\d+/)"[^>]*title="([^"]+)"', card)
        if not (m_id and m_link):
            continue
        href, title = m_link.group(1), clean(m_link.group(2))
        m_rrp = re.search(r'class="cat-rrp-price">([^<]+)<', card)
        m_price = re.search(r'class="bold outlet-green font-18">([^<]+)<', card)
        m_brand = re.search(r'<img[^>]*cat-logo[^>]*title="([^"]*)"', card)
        m_img = re.search(r'(https://cdn\d*\.outlet46\.de/item/images/[^"\s]+?\.(?:jpe?g|png|webp))', card)
        sizes = [re.sub(r"\s*\(.*?\)\s*$", "", clean(s))
                 for s in re.findall(r'<span class="availsize">(.*?)</span>', card, re.S)]
        price = to_cents(m_price.group(1) if m_price else None)
        rrp = to_cents(m_rrp.group(1) if m_rrp else None)
        if not price:  # fehlt oder 0,00 € (Datenfehler im Shop) → nicht kaufbar
            continue
        vendor = html.unescape(m_brand.group(1)).strip() if m_brand else ""
        if re.match(r"^O46\w*\s*\(MB\d+\)$", vendor):
            vendor = "Outlet46 No-Name"
        if rrp is not None and rrp <= price:
            rrp = None
        items.append({
            "id": m_id.group(1),
            "title": title,
            "vendor": vendor,
            "url": urllib.parse.urljoin(SHOP, href),
            "image": m_img.group(1) if m_img else None,
            "price": price,
            "compare_at": rrp,
            "sizes": sizes,
        })
    return items


def fetch_catalog() -> tuple[dict[str, dict], dict]:
    catalog: dict[str, dict] = {}
    stats: dict[str, dict] = {}
    for slug in discover_categories():
        label, expected, seen, page_no = None, None, 0, 1
        while page_no <= 120:  # Sicherheitsbremse
            url = f"{SHOP}/{slug}/?items={PAGE_SIZE}&page={page_no}"
            page = http_get(url)
            if page_no == 1:
                m = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
                h1 = clean(m.group(1)) if m else slug
                m_cnt = re.search(r"\((\d+)\)\s*$", h1)
                expected = int(m_cnt.group(1)) if m_cnt else None
                label = CATEGORY_LABEL_OVERRIDE.get(slug, re.sub(r"\s*\(\d+\)\s*$", "", h1).rstrip("!").strip())
            cards = parse_cards(page)
            if not cards:
                break
            seen += len(cards)
            for it in cards:
                if it["id"] not in catalog:
                    it["type"] = label
                    it["tags"] = []
                    catalog[it["id"]] = it
                if slug == LAST_SIZES_CAT and "Letzte Größen" not in catalog[it["id"]]["tags"]:
                    catalog[it["id"]]["tags"].append("Letzte Größen")
            if f"page={page_no + 1}" not in page:
                break
            page_no += 1
            time.sleep(REQUEST_PAUSE)
        stats[slug] = {"label": label, "expected": expected, "seen": seen, "pages": page_no}
        print(f"  {slug}: {seen} Karten" + (f" (Shop nennt {expected})" if expected else ""), flush=True)
        # Unvollständiger Abruf? Dann lieber abbrechen als falsche "entfernt"-Meldungen.
        if expected and expected >= 50 and seen < 0.85 * expected:
            raise RuntimeError(f"Kategorie {slug}: nur {seen} von {expected} Artikeln geladen – Abbruch.")
        time.sleep(REQUEST_PAUSE)
    return catalog, stats


# --------------------------------------------------------------------------- #
# Vergleich
# --------------------------------------------------------------------------- #
def load_json(path: Path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def eur(cents: int | None) -> str:
    if cents is None:
        return "–"
    return f"{cents / 100:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def discount_pct(price, compare) -> int | None:
    if price and compare and compare > price:
        return round((1 - price / compare) * 100)
    return None


def diff(old: dict, new: dict, gone: dict, now: str) -> list[dict]:
    events: list[dict] = []

    def ev(kind: str, item: dict, **extra):
        e = {"ts": now, "type": kind, "id": item["id"], "title": item["title"], "url": item["url"],
             "image": item.get("image"), "price": item.get("price"), "compare_at": item.get("compare_at"),
             "vendor": item.get("vendor", "")}
        e.update(extra)
        events.append(e)

    for vid, n in new.items():
        o = old.get(vid)
        if o is None:
            prev = gone.get(vid)
            if prev:
                extra = {}
                if prev.get("price") and prev["price"] != n["price"]:
                    extra = {"old_price": prev["price"],
                             "change_pct": round((n["price"] / prev["price"] - 1) * 100, 1)}
                ev("back_in_stock", n, **extra)
            else:
                ev("new", n)
            continue
        if n["price"] != o["price"]:
            kind = "price_drop" if n["price"] < o["price"] else "price_up"
            ev(kind, n, old_price=o["price"], change_pct=round((n["price"] / o["price"] - 1) * 100, 1))
        elif n["compare_at"] != o["compare_at"] and n["compare_at"]:
            ev("discount", n, old_compare_at=o["compare_at"])

    for vid, o in old.items():
        if vid not in new:
            ev("removed", o)
    return events


# --------------------------------------------------------------------------- #
# Benachrichtigungen
# --------------------------------------------------------------------------- #
LABELS = {
    "new": "🆕 Neu",
    "price_drop": "📉 Preis gesenkt",
    "price_up": "📈 Preis erhöht",
    "discount": "🏷️ UVP geändert",
    "back_in_stock": "✅ Wieder da",
    "removed": "🗑️ Nicht mehr gelistet",
}


def notify_types() -> set[str]:
    raw = os.environ.get("NOTIFY_TYPES") or "new,price_drop,back_in_stock"
    return {t.strip() for t in raw.split(",") if t.strip()}


def event_line(e: dict) -> str:
    label = LABELS.get(e["type"], e["type"])
    if e.get("old_price") is not None and e.get("change_pct") is not None:
        price = f'{eur(e["old_price"])} → {eur(e["price"])} ({e["change_pct"]:+.1f} %)'
    else:
        price = eur(e["price"])
        d = discount_pct(e["price"], e["compare_at"])
        if d:
            price += f" (−{d} % ggü. UVP {eur(e['compare_at'])})"
    return f"{label}: {e['title']} – {price}"


def rank_events(events: list[dict]) -> list[dict]:
    """Wichtigstes zuerst: größte Preissenkung, dann höchster Rabatt."""
    def rank(e):
        if e.get("old_price") is not None and e.get("change_pct") is not None and e["change_pct"] < 0:
            return (0, e["change_pct"])
        return (1, -(discount_pct(e["price"], e["compare_at"]) or 0))
    return sorted(events, key=rank)


def push_title(e: dict) -> str:
    label = LABELS.get(e["type"], e["type"])
    if e.get("old_price") is not None and e.get("change_pct") is not None:
        return f'{SHOP_NAME} · {label} {e["change_pct"]:+.0f} %'
    d = discount_pct(e["price"], e["compare_at"])
    return f"{SHOP_NAME} · {label}" + (f" −{d} %" if d else "")


def push_body(e: dict) -> str:
    if e.get("old_price") is not None and e.get("change_pct") is not None:
        price = f'{eur(e["old_price"])} → {eur(e["price"])}'
    else:
        price = eur(e["price"])
        if e.get("compare_at") and discount_pct(e["price"], e["compare_at"]):
            price += f" (statt {eur(e['compare_at'])})"
    return f"{e['title']}\n{price}"


def ntfy_publish(payload: dict):
    """Veröffentlicht per JSON (UTF-8-sicher, unterstützt Klick-Link und Bild)."""
    server = (os.environ.get("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
    headers = {"Content-Type": "application/json"}
    if os.environ.get("NTFY_TOKEN"):
        headers["Authorization"] = f'Bearer {os.environ["NTFY_TOKEN"]}'
    req = urllib.request.Request(server + "/", data=json.dumps(payload).encode("utf-8"),
                                 headers=headers, method="POST")
    urllib.request.urlopen(req, timeout=20).read()


def send_ntfy(events: list[dict]):
    """Eine Nachricht pro Artikel – Tippen öffnet direkt die Produktseite."""
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    limit = int(os.environ.get("MAX_PUSH_PER_RUN") or "10")
    single, rest = events[:limit], events[limit:]
    for e in single:
        payload = {"topic": topic, "title": push_title(e), "message": push_body(e),
                   "click": e["url"], "tags": ["shopping_cart"]}
        if e.get("image"):
            payload["attach"] = e["image"]
        ntfy_publish(payload)
        time.sleep(0.5)
    if rest:
        lines = [f"• {event_line(e)}" for e in rest[:20]]
        if len(rest) > 20:
            lines.append(f"… und {len(rest) - 20} weitere")
        ntfy_publish({"topic": topic, "title": f"{SHOP_NAME}: {len(rest)} weitere Änderungen",
                      "message": "\n".join(lines), "tags": ["shopping_cart"]})


def text_digest(events: list[dict]) -> tuple[str, str]:
    counts: dict[str, int] = {}
    for e in events:
        counts[e["type"]] = counts.get(e["type"], 0) + 1
    title = f"{SHOP_NAME}: " + ", ".join(f"{n}× {LABELS[k].split(' ', 1)[1]}" for k, n in counts.items())
    lines = [f"{event_line(e)}\n{e['url']}" for e in events[:40]]
    if len(events) > 40:
        lines.append(f"… und {len(events) - 40} weitere")
    return title, "\n\n".join(lines)


def send_telegram(title: str, body: str):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": f"{title}\n\n{body}"[:4000],
                                   "disable_web_page_preview": "true"}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=20).read()


def send_email(title: str, body: str):
    host, to = os.environ.get("SMTP_HOST"), os.environ.get("MAIL_TO")
    if not (host and to):
        return
    user = os.environ.get("SMTP_USER", "")
    sender = os.environ.get("MAIL_FROM") or user
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = title, sender, to
    with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT") or "587"), timeout=30) as s:
        s.starttls()
        if user:
            s.login(user, os.environ.get("SMTP_PASSWORD", ""))
        s.sendmail(sender, [a.strip() for a in to.split(",")], msg.as_string())


def select_for_notify(events: list[dict]) -> list[dict]:
    wanted = notify_types()
    events = [e for e in events if e["type"] in wanted]
    min_change = float(os.environ.get("MIN_PRICE_CHANGE_PCT") or "0")
    events = [e for e in events
              if e["type"] not in ("price_drop", "price_up") or abs(e["change_pct"]) >= min_change]
    min_disc = float(os.environ.get("MIN_DISCOUNT_PCT") or "0")
    if min_disc:
        events = [e for e in events if e["type"] != "new"
                  or (discount_pct(e["price"], e["compare_at"]) or 0) >= min_disc]
    return rank_events(events)


def notify(events: list[dict]):
    events = select_for_notify(events)
    if not events:
        return
    title, body = text_digest(events)
    for fn, args in ((send_ntfy, (events,)),
                     (send_telegram, (title, body)),
                     (send_email, (title, body))):
        try:
            fn(*args)
        except Exception as e:  # noqa: BLE001  – Benachrichtigung darf Lauf nicht abbrechen
            print(f"Warnung: {fn.__name__} fehlgeschlagen: {e}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Hauptablauf
# --------------------------------------------------------------------------- #
def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    current, stats = fetch_catalog()
    if not current:
        print("Leerer Katalog erhalten – Abbruch, Stand bleibt unverändert.", file=sys.stderr)
        return 1

    state = load_json(STATE_FILE, {})
    old_items: dict = state.get("items", {})
    history: dict = state.get("history", {})
    first_seen: dict = state.get("first_seen", {})
    gone: dict = state.get("gone", {})
    baseline = not old_items

    if old_items and len(current) < 0.8 * len(old_items):
        print(f"Nur {len(current)} von {len(old_items)} Artikeln geladen – Abbruch.", file=sys.stderr)
        return 1

    # Flacker-Schutz: Ein Artikel gilt erst als "nicht mehr gelistet", wenn er in
    # MISSING_RUNS Läufen hintereinander fehlt. Kurzes Verschwinden (Warenkorb-
    # Reservierung, verschobene Seitenaufteilung während des Abrufs) wird ignoriert.
    pending: dict = state.get("pending_missing", {})
    fetched = len(current)
    for vid in list(pending):
        if vid in current:
            pending.pop(vid)
    for vid, o in old_items.items():
        if vid not in current:
            cnt = pending.get(vid, {}).get("count", 0) + 1
            if cnt < MISSING_RUNS:
                pending[vid] = {"count": cnt}
                current[vid] = o            # vorerst als weiterhin gelistet behandeln
            else:
                pending.pop(vid, None)

    events = [] if baseline else diff(old_items, current, gone, now)

    for vid, item in current.items():
        first_seen.setdefault(vid, now)
        gone.pop(vid, None)
        h = history.setdefault(vid, [])
        if not h or h[-1][1] != item["price"] or h[-1][2] != item["compare_at"]:
            h.append([now, item["price"], item["compare_at"]])
            del h[:-MAX_HISTORY]
    for vid, o in old_items.items():
        if vid not in current:
            gone[vid] = {"ts": now, "price": o["price"], "title": o["title"]}
    # Verlauf nicht mehr gelisteter Artikel nach 180 Tagen aufräumen
    cutoff = time.time() - 180 * 86400
    for vid in [v for v, g in gone.items() if datetime.fromisoformat(g["ts"]).timestamp() < cutoff]:
        gone.pop(vid, None)
        history.pop(vid, None)
        first_seen.pop(vid, None)

    all_events = load_json(EVENTS_FILE, [])
    if baseline:
        all_events.append({"ts": now, "type": "baseline", "count": len(current),
                           "title": f"Überwachung gestartet – {len(current)} Artikel erfasst"})
    all_events.extend(events)
    all_events = all_events[-MAX_EVENTS:]

    catalog = []
    for vid, item in current.items():
        # Beim aktuellen Lauf nicht gefunden → im Dashboard sofort ausblenden
        # (Meldung "nicht mehr gelistet" kommt erst nach dem zweiten Fehlen)
        catalog.append({**item, "available": vid not in pending,
                        "discount_pct": discount_pct(item["price"], item["compare_at"]),
                        "first_seen": first_seen.get(vid),
                        "history": history.get(vid, [])})
    catalog.sort(key=lambda x: x["title"].lower())

    state = {"items": current, "history": history, "first_seen": first_seen, "gone": gone,
             "pending_missing": pending}
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")), "utf-8")
    CATALOG_FILE.write_text(json.dumps(catalog, ensure_ascii=False, separators=(",", ":")), "utf-8")
    EVENTS_FILE.write_text(json.dumps(all_events, ensure_ascii=False, separators=(",", ":")), "utf-8")
    meta = load_json(META_FILE, {})
    meta.update({
        "shop": SHOP, "shop_name": SHOP_NAME, "last_run": now,
        "last_change": now if (events or baseline) else meta.get("last_change"),
        "products": len(current), "variants": len(current),
        "last_run_events": len(events), "started": meta.get("started", now),
        "run_seconds": round(time.time() - t0),
        "categories": {k: {"label": v["label"], "seen": v["seen"]} for k, v in stats.items()},
    })
    META_FILE.write_text(json.dumps(meta, ensure_ascii=False, indent=1), "utf-8")

    if pending:
        print(f"  {len(pending)} Artikel fehlen erstmals – werden erst beim nächsten Fehlen gemeldet")
    print(f"{now}: {len(current)} Artikel ({fetched} geladen), {len(events)} Änderungen"
          f"{' (Basislauf)' if baseline else ''}, {meta['run_seconds']} s")
    for e in events[:50]:
        print("  " + event_line(e))
    if events:
        notify(events)
    return 0


if __name__ == "__main__":
    sys.exit(main())
