#!/usr/bin/env python3
"""
Baltic States NATO Dashboard - Data Refresh Script
No API key required. Uses RSS feeds + keyword matching.

Steps:
1. Pull personnel figures from Wikidata
2. Fetch RSS feeds from defense news sources
3. Extract relevant news items per country using keyword matching
4. Detect procurement-like items using value/contract keywords
5. Merge results into existing data/*.json files
6. Write updated JSON files (GitHub Actions then commits them)
"""

import json
import os
import sys
import re
import urllib.request
import urllib.error
from datetime import datetime, timezone
import xml.etree.ElementTree as ET

# ── Configuration ────────────────────────────────────────────────────────────

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

# Wikidata entity IDs
WIKIDATA_ENTITIES = {
    "lithuania": {"entity": "Q809748",  "label": "Lithuania"},
    "latvia":    {"entity": "Q216330",  "label": "Latvia"},
    "estonia":   {"entity": "Q216193",  "label": "Estonia"},
}

# RSS feeds — working ones confirmed from last run
RSS_FEEDS = [
    "https://defence-blog.com/feed/",
    "https://www.defensenews.com/arc/outboundfeeds/rss/?rss=all",
    "https://euro-sd.com/feed/",
    "https://eng.lsm.lv/rss",                         # Latvian public broadcaster
    "https://www.err.ee/rss",                          # Estonian public broadcaster
    "https://www.lrt.lt/rss",                          # Lithuanian public broadcaster
    "https://militarnyi.com/feed/",                    # fixed URL
    "https://www.armyrecognition.com/feed/",           # fixed URL
    "https://thedefensepost.com/feed",                 # no trailing slash
    "https://www.baltictimes.com/feed/",               # fixed URL
]

MAX_RSS_ITEMS_PER_FEED = 30

# ── Per-country keyword sets ──────────────────────────────────────────────────

COUNTRY_KEYWORDS = {
    "lithuania": [
        "lithuania", "lithuanian", "lietuva", "lietuvos", "kariuomene",
        "vilnius", "kaunas",
    ],
    "latvia": [
        "latvia", "latvian", "latvija", "latvijas", "riga",
        "zemessardze", "nbs",
    ],
    "estonia": [
        "estonia", "estonian", "eesti", "tallinn", "tartu",
        "kaitseliit", "kaitsejoud",
    ],
}

# Keywords that suggest a defence/military topic
DEFENCE_KEYWORDS = [
    "military", "defence", "defense", "armed forces", "army", "navy", "air force",
    "nato", "efp", "battlegroup", "brigade", "battalion",
    "procurement", "contract", "purchase", "order", "delivery", "signed",
    "missile", "artillery", "howitzer", "tank", "ifv", "apc", "drone", "uav",
    "himars", "iris-t", "ascod", "boxer", "nasams", "k9", "chunmoo", "patriot",
    "f-35", "leopard", "ammunition", "munition", "radar", "air defence",
    "ministry of defence", "ministry of defense", "mod ", "defence ministry",
]

# Keywords that strongly suggest a procurement/contract item
PROCUREMENT_KEYWORDS = [
    "contract", "signed", "procure", "procurement", "purchase", "order",
    "billion", "million", "eur ", "usd ", "€", "$", "deliver", "delivery",
    "agreement", "deal", "acquire", "acquisition",
]

# ── Wikidata ─────────────────────────────────────────────────────────────────

def fetch_wikidata_personnel(entity_id: str) -> dict:
    """Fetch active/reserve personnel from Wikidata entity JSON."""
    # Try SPARQL first as it's more reliable than entity data endpoint
    sparql = f"""
SELECT ?active ?reserve WHERE {{
  OPTIONAL {{ wd:{entity_id} wdt:P1148 ?active. }}
  OPTIONAL {{ wd:{entity_id} wdt:P2031 ?reserve. }}
}}
"""
    url = "https://query.wikidata.org/sparql?query=" + urllib.parse.quote(sparql) + "&format=json"
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "baltic-nato-dashboard/1.0", "Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        bindings = data.get("results", {}).get("bindings", [])
        result = {}
        for b in bindings:
            if "active" in b:
                try:
                    result["active"] = int(float(b["active"]["value"]))
                except Exception:
                    pass
            if "reserve" in b:
                try:
                    result["reserve"] = int(float(b["reserve"]["value"]))
                except Exception:
                    pass
        return result
    except Exception as e:
        print(f"  [Wikidata SPARQL] Failed for {entity_id}: {e}", file=sys.stderr)

    # Fallback: entity data endpoint
    url2 = f"https://www.wikidata.org/wiki/Special:EntityData/{entity_id}.json"
    try:
        req = urllib.request.Request(url2, headers={"User-Agent": "baltic-nato-dashboard/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        entity = data.get("entities", {}).get(entity_id, {})
        claims = entity.get("claims", {})
        result = {}

        def _get_qty(prop):
            for entry in claims.get(prop, []):
                try:
                    val = entry["mainsnak"]["datavalue"]["value"]["amount"]
                    return abs(int(float(val)))
                except Exception:
                    pass
            return None

        active = _get_qty("P1148")
        if active:
            result["active"] = active
        reserve = _get_qty("P2031")
        if reserve:
            result["reserve"] = reserve
        return result
    except Exception as e:
        print(f"  [Wikidata entity] Failed for {entity_id}: {e}", file=sys.stderr)
        return {}

# ── RSS ───────────────────────────────────────────────────────────────────────

def parse_date(date_str: str) -> str:
    """Try to parse various date formats into YYYY-MM-DD."""
    if not date_str:
        return ""
    # Already in YYYY-MM-DD
    m = re.search(r"(\d{4}-\d{2}-\d{2})", date_str)
    if m:
        return m.group(1)
    # RFC 822 e.g. "Mon, 17 Jun 2026 06:00:00 +0000"
    months = {"jan":"01","feb":"02","mar":"03","apr":"04","may":"05","jun":"06",
               "jul":"07","aug":"08","sep":"09","oct":"10","nov":"11","dec":"12"}
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})", date_str)
    if m:
        day = m.group(1).zfill(2)
        mon = months.get(m.group(2).lower(), "01")
        yr  = m.group(3)
        return f"{yr}-{mon}-{day}"
    return ""


def strip_html(text: str) -> str:
    """Remove HTML tags from a string."""
    return re.sub(r"<[^>]+>", " ", text or "").strip()


def fetch_rss(feed_url: str) -> list[dict]:
    """Fetch and parse an RSS/Atom feed."""
    try:
        req = urllib.request.Request(
            feed_url,
            headers={
                "User-Agent": "baltic-nato-dashboard/1.0",
                "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
            }
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
    except Exception as e:
        print(f"  [RSS] Failed {feed_url}: {e}", file=sys.stderr)
        return []

    # Strip byte-order mark if present
    raw = raw.lstrip(b"\xef\xbb\xbf")

    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        # Try stripping non-XML content before first '<'
        try:
            start = raw.index(b"<")
            root = ET.fromstring(raw[start:])
        except Exception as e:
            print(f"  [RSS] Parse error {feed_url}: {e}", file=sys.stderr)
            return []

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    items = []

    # RSS 2.0
    for item in root.findall(".//item"):
        title   = strip_html(item.findtext("title") or "")
        link    = (item.findtext("link") or "").strip()
        pub     = parse_date(item.findtext("pubDate") or "")
        summary = strip_html(item.findtext("description") or "")[:600]
        if title:
            items.append({"title": title, "link": link, "date": pub, "summary": summary})
        if len(items) >= MAX_RSS_ITEMS_PER_FEED:
            break

    # Atom
    if not items:
        for entry in root.findall(".//{http://www.w3.org/2005/Atom}entry"):
            title = strip_html(entry.findtext("{http://www.w3.org/2005/Atom}title") or "")
            link_el = entry.find("{http://www.w3.org/2005/Atom}link")
            link = (link_el.get("href", "") if link_el is not None else "").strip()
            pub_raw = (entry.findtext("{http://www.w3.org/2005/Atom}updated") or
                       entry.findtext("{http://www.w3.org/2005/Atom}published") or "")
            pub = parse_date(pub_raw)
            summary = strip_html(
                entry.findtext("{http://www.w3.org/2005/Atom}summary") or
                entry.findtext("{http://www.w3.org/2005/Atom}content") or ""
            )[:600]
            if title:
                items.append({"title": title, "link": link, "date": pub, "summary": summary})
            if len(items) >= MAX_RSS_ITEMS_PER_FEED:
                break

    return items

# ── Keyword matching ──────────────────────────────────────────────────────────

def matches_country(item: dict, country: str) -> bool:
    text = (item["title"] + " " + item["summary"]).lower()
    return any(kw in text for kw in COUNTRY_KEYWORDS[country])


def matches_defence(item: dict) -> bool:
    text = (item["title"] + " " + item["summary"]).lower()
    return any(kw in text for kw in DEFENCE_KEYWORDS)


def is_procurement(item: dict) -> bool:
    text = (item["title"] + " " + item["summary"]).lower()
    return any(kw in text for kw in PROCUREMENT_KEYWORDS)


def extract_value(text: str) -> str:
    """Try to extract a monetary value from text."""
    patterns = [
        r"(EUR\s+[\d.,]+\s*(?:billion|million|bn|m)\b)",
        r"(USD\s+[\d.,]+\s*(?:billion|million|bn|m)\b)",
        r"(€\s*[\d.,]+\s*(?:billion|million|bn|m)\b)",
        r"(\$\s*[\d.,]+\s*(?:billion|million|bn|m)\b)",
        r"([\d.,]+\s*(?:billion|million)\s*(?:euro|dollar)s?)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return "Not disclosed"


def make_news_bullet(item: dict) -> str:
    """Format an RSS item as a news bullet string."""
    title = item["title"].strip().rstrip(".")
    date  = f" ({item['date']})" if item.get("date") else ""
    return f"{title}{date}."


def make_procurement_entry(item: dict) -> dict:
    """Format an RSS item as a procurement dict."""
    text  = item["title"] + " " + item["summary"]
    value = extract_value(text)
    return {
        "item":          item["title"][:120],
        "value":         value,
        "supplier":      "See source",
        "delivery":      "See source",
        "source_url":    item.get("link", ""),
        "date_reported": item.get("date", ""),
    }

# ── Merge helpers ─────────────────────────────────────────────────────────────

def dedup_news(existing: list, new_items: list) -> list:
    seen = {n.lower()[:70] for n in existing}
    result = list(existing)
    for item in new_items:
        key = item.lower()[:70]
        if key not in seen:
            result.append(item)
            seen.add(key)
    return result[-8:]  # keep latest 8


def dedup_procurement(existing: list, new_items: list) -> list:
    seen = {p["item"].lower()[:50] for p in existing}
    result = list(existing)
    for item in new_items:
        key = item["item"].lower()[:50]
        if key not in seen:
            result.append(item)
            seen.add(key)
    return result[-10:]  # keep latest 10


def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: str, data: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"  [JSON] Saved {path}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"=== Baltic Dashboard Refresh — {today} ===\n")

    # ── Step 1: Wikidata ──────────────────────────────────────────────────────
    print("Step 1: Fetching Wikidata personnel figures...")
    wikidata = {}
    for country, cfg in WIKIDATA_ENTITIES.items():
        print(f"  {cfg['label']} ({cfg['entity']})...")
        result = fetch_wikidata_personnel(cfg["entity"])
        wikidata[country] = result
        print(f"    → {result}")

    # ── Step 2: RSS ───────────────────────────────────────────────────────────
    print("\nStep 2: Fetching RSS feeds...")
    all_items = []
    for feed_url in RSS_FEEDS:
        print(f"  {feed_url}")
        items = fetch_rss(feed_url)
        defence_items = [i for i in items if matches_defence(i)]
        print(f"    → {len(items)} total, {len(defence_items)} defence-related")
        all_items.extend(defence_items)

    # Deduplicate by URL
    seen_urls: set = set()
    unique: list = []
    for item in all_items:
        url = item.get("link", "")
        if url and url in seen_urls:
            continue
        if url:
            seen_urls.add(url)
        unique.append(item)

    print(f"  Total unique defence items: {len(unique)}")

    # ── Step 3: Classify per country ─────────────────────────────────────────
    print("\nStep 3: Classifying items per country...")
    per_country_news: dict = {c: [] for c in WIKIDATA_ENTITIES}
    per_country_proc: dict = {c: [] for c in WIKIDATA_ENTITIES}

    for item in unique:
        for country in WIKIDATA_ENTITIES:
            if matches_country(item, country):
                per_country_news[country].append(make_news_bullet(item))
                if is_procurement(item):
                    per_country_proc[country].append(make_procurement_entry(item))

    for country in WIKIDATA_ENTITIES:
        print(f"  {country}: {len(per_country_news[country])} news, "
              f"{len(per_country_proc[country])} procurement items")

    # ── Step 4: Merge and write ───────────────────────────────────────────────
    print("\nStep 4: Merging and writing JSON files...")
    for country in WIKIDATA_ENTITIES:
        json_path = os.path.join(DATA_DIR, f"{country}.json")
        if not os.path.exists(json_path):
            print(f"  [!] {json_path} not found, skipping.", file=sys.stderr)
            continue

        data = load_json(json_path)

        # Personnel from Wikidata
        wd = wikidata.get(country, {})
        if wd.get("active"):
            data["personnel"]["active"] = wd["active"]
            print(f"  [{country}] Updated active personnel: {wd['active']}")
        if wd.get("reserve"):
            data["personnel"]["reserve"] = wd["reserve"]
            print(f"  [{country}] Updated reserve personnel: {wd['reserve']}")

        # News
        new_news = per_country_news[country]
        if new_news:
            data["news"] = dedup_news(data.get("news", []), new_news)

        # Procurement
        new_proc = per_country_proc[country]
        if new_proc:
            data["procurement"] = dedup_procurement(data.get("procurement", []), new_proc)

        data["last_updated"] = today
        save_json(json_path, data)

    print(f"\n=== Refresh complete — {today} ===")


if __name__ == "__main__":
    main()
