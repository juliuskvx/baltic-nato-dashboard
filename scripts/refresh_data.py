#!/usr/bin/env python3
"""
Baltic States NATO Dashboard - Data Refresh Script
No API key required. Uses RSS feeds + keyword matching.
"""

import json, os, sys, re, time, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone
import xml.etree.ElementTree as ET

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

WIKIDATA_ENTITIES = {
    "lithuania": {"entity": "Q809748", "label": "Lithuania"},
    "latvia":    {"entity": "Q216330", "label": "Latvia"},
    "estonia":   {"entity": "Q216193", "label": "Estonia"},
}

# Only feeds confirmed working in last run + fixed alternatives
RSS_FEEDS = [
    "https://defence-blog.com/feed/",
    "https://www.defensenews.com/arc/outboundfeeds/rss/?rss=all",
    "https://eng.lsm.lv/rss",                        # Latvia broadcaster ✅
    "https://www.err.ee/rss",                         # Estonia broadcaster ✅
    "https://www.lrt.lt/?rss",                        # Lithuania broadcaster (fixed URL)
    "https://defenseone.com/rss/all",                 # replacement for broken feeds
    "https://bulgarianmilitary.com/feed/",            # covers Baltic well
    "https://www.defencetalk.com/news/feed/",         # additional source
]

MAX_RSS_ITEMS = 30
WIKIDATA_DELAY = 5  # seconds between Wikidata requests

COUNTRY_KEYWORDS = {
    "lithuania": ["lithuania", "lithuanian", "lietuva", "lietuvos", "kariuomene", "vilnius", "kaunas"],
    "latvia":    ["latvia", "latvian", "latvija", "latvijas", "riga", "zemessardze"],
    "estonia":   ["estonia", "estonian", "eesti", "tallinn", "tartu", "kaitseliit"],
}

DEFENCE_KEYWORDS = [
    "military", "defence", "defense", "armed forces", "army", "navy", "air force",
    "nato", "efp", "battlegroup", "brigade", "battalion", "regiment",
    "procurement", "contract", "purchase", "order", "delivery", "signed",
    "missile", "artillery", "howitzer", "tank", "ifv", "apc", "drone", "uav",
    "himars", "iris-t", "ascod", "boxer", "nasams", "k9", "chunmoo", "patriot",
    "f-35", "leopard", "ammunition", "munition", "radar", "air defence", "air defense",
    "ministry of defence", "ministry of defense",
]

PROCUREMENT_KEYWORDS = [
    "contract", "signed", "procure", "procurement", "purchase", "order",
    "billion", "million", "eur ", "usd ", "€", "$",
    "deliver", "delivery", "agreement", "deal", "acquire", "acquisition",
]

# ── Wikidata ──────────────────────────────────────────────────────────────────

def fetch_wikidata_personnel(entity_id: str) -> dict:
    sparql = f"SELECT ?active ?reserve WHERE {{ OPTIONAL {{ wd:{entity_id} wdt:P1148 ?active. }} OPTIONAL {{ wd:{entity_id} wdt:P2031 ?reserve. }} }}"
    url = "https://query.wikidata.org/sparql?query=" + urllib.parse.quote(sparql) + "&format=json"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "baltic-nato-dashboard/1.0 (https://github.com/juliuskvx/baltic-nato-dashboard)",
            "Accept": "application/json"
        })
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
        result = {}
        for b in data.get("results", {}).get("bindings", []):
            if "active" in b:
                try: result["active"] = int(float(b["active"]["value"]))
                except: pass
            if "reserve" in b:
                try: result["reserve"] = int(float(b["reserve"]["value"]))
                except: pass
        return result
    except Exception as e:
        print(f"  [Wikidata] {entity_id}: {e}", file=sys.stderr)
        return {}

# ── RSS ───────────────────────────────────────────────────────────────────────

def parse_date(s: str) -> str:
    if not s: return ""
    m = re.search(r"(\d{4}-\d{2}-\d{2})", s)
    if m: return m.group(1)
    months = {"jan":"01","feb":"02","mar":"03","apr":"04","may":"05","jun":"06",
               "jul":"07","aug":"08","sep":"09","oct":"10","nov":"11","dec":"12"}
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})", s)
    if m:
        return f"{m.group(3)}-{months.get(m.group(2).lower(),'01')}-{m.group(1).zfill(2)}"
    return ""

def strip_html(t: str) -> str:
    return re.sub(r"<[^>]+>", " ", t or "").strip()

def fetch_rss(feed_url: str) -> list:
    try:
        req = urllib.request.Request(feed_url, headers={
            "User-Agent": "baltic-nato-dashboard/1.0",
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
    except Exception as e:
        print(f"  [RSS] Failed {feed_url}: {e}", file=sys.stderr)
        return []

    raw = raw.lstrip(b"\xef\xbb\xbf")

    # thedefensepost has invalid XML — strip bad chars
    try:
        raw_clean = re.sub(rb'[^\x09\x0A\x0D\x20-\uD7FF\uE000-\uFFFD]', b'', raw)
    except Exception:
        raw_clean = raw

    root = None
    for attempt in [raw_clean, raw]:
        try:
            root = ET.fromstring(attempt)
            break
        except ET.ParseError:
            continue

    if root is None:
        print(f"  [RSS] Parse error {feed_url}", file=sys.stderr)
        return []

    items = []

    # RSS 2.0
    for item in root.findall(".//item"):
        title   = strip_html(item.findtext("title") or "")
        link    = (item.findtext("link") or "").strip()
        pub     = parse_date(item.findtext("pubDate") or "")
        summary = strip_html(item.findtext("description") or "")[:600]
        if title:
            items.append({"title": title, "link": link, "date": pub, "summary": summary})
        if len(items) >= MAX_RSS_ITEMS: break

    # Atom
    if not items:
        for entry in root.findall(".//{http://www.w3.org/2005/Atom}entry"):
            title = strip_html(entry.findtext("{http://www.w3.org/2005/Atom}title") or "")
            link_el = entry.find("{http://www.w3.org/2005/Atom}link")
            link = (link_el.get("href","") if link_el is not None else "").strip()
            pub_raw = (entry.findtext("{http://www.w3.org/2005/Atom}updated") or
                       entry.findtext("{http://www.w3.org/2005/Atom}published") or "")
            summary = strip_html(
                entry.findtext("{http://www.w3.org/2005/Atom}summary") or
                entry.findtext("{http://www.w3.org/2005/Atom}content") or "")[:600]
            if title:
                items.append({"title": title, "link": link, "date": parse_date(pub_raw), "summary": summary})
            if len(items) >= MAX_RSS_ITEMS: break

    return items

# ── Keyword matching ──────────────────────────────────────────────────────────

def matches_country(item, country):
    text = (item["title"] + " " + item["summary"]).lower()
    return any(kw in text for kw in COUNTRY_KEYWORDS[country])

def matches_defence(item):
    text = (item["title"] + " " + item["summary"]).lower()
    return any(kw in text for kw in DEFENCE_KEYWORDS)

def is_procurement(item):
    text = (item["title"] + " " + item["summary"]).lower()
    return any(kw in text for kw in PROCUREMENT_KEYWORDS)

def extract_value(text):
    for pat in [
        r"(EUR\s+[\d.,]+\s*(?:billion|million|bn|m)\b)",
        r"(USD\s+[\d.,]+\s*(?:billion|million|bn|m)\b)",
        r"(€\s*[\d.,]+\s*(?:billion|million|bn|m)\b)",
        r"(\$\s*[\d.,]+\s*(?:billion|million|bn|m)\b)",
        r"([\d.,]+\s*(?:billion|million)\s*(?:euro|dollar)s?)",
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m: return m.group(1).strip()
    return "Not disclosed"

def make_news_bullet(item):
    title = item["title"].strip().rstrip(".")
    date  = f" ({item['date']})" if item.get("date") else ""
    return f"{title}{date}."

def make_procurement_entry(item):
    text = item["title"] + " " + item["summary"]
    return {
        "item":          item["title"][:120],
        "value":         extract_value(text),
        "supplier":      "See source",
        "delivery":      "See source",
        "source_url":    item.get("link", ""),
        "date_reported": item.get("date", ""),
    }

# ── Merge helpers ─────────────────────────────────────────────────────────────

def dedup_news(existing, new_items):
    seen = {n.lower()[:70] for n in existing}
    result = list(existing)
    for item in new_items:
        key = item.lower()[:70]
        if key not in seen:
            result.append(item)
            seen.add(key)
    return result[-8:]

def dedup_procurement(existing, new_items):
    seen = {p["item"].lower()[:50] for p in existing}
    result = list(existing)
    for item in new_items:
        key = item["item"].lower()[:50]
        if key not in seen:
            result.append(item)
            seen.add(key)
    return result[-10:]

def load_json(path):
    with open(path, "r", encoding="utf-8") as f: return json.load(f)

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"  [JSON] Saved {path}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"=== Baltic Dashboard Refresh — {today} ===\n")

    # Step 1: Wikidata (with delay to avoid rate limiting)
    print("Step 1: Fetching Wikidata personnel figures...")
    wikidata = {}
    for i, (country, cfg) in enumerate(WIKIDATA_ENTITIES.items()):
        if i > 0:
            print(f"  Waiting {WIKIDATA_DELAY}s to avoid rate limit...")
            time.sleep(WIKIDATA_DELAY)
        print(f"  {cfg['label']} ({cfg['entity']})...")
        result = fetch_wikidata_personnel(cfg["entity"])
        wikidata[country] = result
        print(f"    → {result}")

    # Step 2: RSS feeds
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
    unique = []
    for item in all_items:
        url = item.get("link", "")
        if url and url in seen_urls: continue
        if url: seen_urls.add(url)
        unique.append(item)
    print(f"  Total unique defence items: {len(unique)}")

    # Step 3: Classify per country
    print("\nStep 3: Classifying items per country...")
    per_news = {c: [] for c in WIKIDATA_ENTITIES}
    per_proc = {c: [] for c in WIKIDATA_ENTITIES}

    for item in unique:
        for country in WIKIDATA_ENTITIES:
            if matches_country(item, country):
                per_news[country].append(make_news_bullet(item))
                if is_procurement(item):
                    per_proc[country].append(make_procurement_entry(item))

    for country in WIKIDATA_ENTITIES:
        print(f"  {country}: {len(per_news[country])} news, {len(per_proc[country])} procurement")

    # Step 4: Merge and write
    print("\nStep 4: Merging and writing JSON files...")
    for country in WIKIDATA_ENTITIES:
        json_path = os.path.join(DATA_DIR, f"{country}.json")
        if not os.path.exists(json_path):
            print(f"  [!] {json_path} not found", file=sys.stderr)
            continue

        data = load_json(json_path)

        wd = wikidata.get(country, {})
        if wd.get("active"):
            data["personnel"]["active"] = wd["active"]
            print(f"  [{country}] active personnel → {wd['active']}")
        if wd.get("reserve"):
            data["personnel"]["reserve"] = wd["reserve"]
            print(f"  [{country}] reserve personnel → {wd['reserve']}")

        if per_news[country]:
            data["news"] = dedup_news(data.get("news", []), per_news[country])
        if per_proc[country]:
            data["procurement"] = dedup_procurement(data.get("procurement", []), per_proc[country])

        data["last_updated"] = today
        save_json(json_path, data)

    print(f"\n=== Refresh complete — {today} ===")

if __name__ == "__main__":
    main()
