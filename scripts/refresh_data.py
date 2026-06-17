#!/usr/bin/env python3
"""
Baltic States NATO Dashboard - Data Refresh Script
Runs inside GitHub Actions on workflow_dispatch or schedule.

Steps:
1. Pull personnel figures from Wikidata SPARQL
2. Fetch RSS feeds from defense news sources
3. Call Claude API to extract structured procurement entries per country
4. Merge results into existing data/*.json files, preserving manual fields
5. Write updated JSON files (GitHub Actions then commits them)

Required env vars:
  ANTHROPIC_API_KEY  - for the Claude extraction step
"""

import json
import os
import sys
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone
import xml.etree.ElementTree as ET

# ── Configuration ────────────────────────────────────────────────────────────

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

# Wikidata entity IDs for each country's armed forces article
WIKIDATA_ENTITIES = {
    "lithuania": {
        "armed_forces": "Q809748",   # Lithuanian Armed Forces
        "country_label": "Lithuania",
    },
    "latvia": {
        "armed_forces": "Q216330",   # National Armed Forces (Latvia)
        "country_label": "Latvia",
    },
    "estonia": {
        "armed_forces": "Q216193",   # Estonian Defence Forces
        "country_label": "Estonia",
    },
}

# RSS feeds to monitor — these all have confirmed public feeds
RSS_FEEDS = [
    # Defense Post — covers Baltic procurement extensively
    "https://thedefensepost.com/feed/",
    # Defence Blog — good for Baltic/NATO equipment news
    "https://defence-blog.com/feed/",
    # Defense News
    "https://www.defensenews.com/arc/outboundfeeds/rss/?rss=all",
    # Army Recognition
    "https://www.armyrecognition.com/rss.xml",
    # Euro SD (European Security & Defence)
    "https://euro-sd.com/feed/",
    # Militarnyi (covers Baltic procurement well)
    "https://militarnyi.com/en/feed/",
    # Baltic Times
    "https://www.baltictimes.com/rss/news/",
    # LRT (Lithuanian public broadcaster)
    "https://www.lrt.lt/rss/news",
    # ERR (Estonian public broadcaster)
    "https://www.err.ee/rss/news",
    # LSM (Latvian public broadcaster)
    "https://eng.lsm.lv/rss",
]

# Keywords to filter RSS items — only keep items relevant to Baltic defence
KEYWORDS = [
    "lithuania", "latvija", "latvia", "estonia", "estonian", "lithuanian", "latvian",
    "baltic", "nato", "efp", "enhanced forward presence",
    "procurement", "contract", "defence", "defense", "military", "armed forces",
    "ministry of defence", "ministry of defense",
    "kariuomene", "mil.lv", "mil.ee",
    "himars", "iris-t", "ascod", "boxer", "nasams", "k9", "chunmoo",
    "battlegroup", "brigade", "battalion",
]

MAX_RSS_ITEMS_PER_FEED = 20  # fetch at most this many items per feed
MAX_ITEMS_FOR_LLM = 60       # send at most this many items to Claude in one call

# ── Wikidata ─────────────────────────────────────────────────────────────────

def fetch_wikidata_personnel(entity_id: str) -> dict:
    """
    Fetch personnel/strength figures from a Wikidata entity.
    Returns a dict with keys like 'active', 'reserve', etc. (integers where parseable).
    Falls back to empty dict on any error.
    """
    url = f"https://www.wikidata.org/wiki/Special:EntityData/{entity_id}.json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "baltic-nato-dashboard/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        print(f"  [Wikidata] Failed to fetch {entity_id}: {e}", file=sys.stderr)
        return {}

    entities = data.get("entities", {})
    entity = entities.get(entity_id, {})
    claims = entity.get("claims", {})

    result = {}

    def _get_quantity(prop):
        """Extract the first numeric quantity value for a Wikidata property."""
        entries = claims.get(prop, [])
        for entry in entries:
            try:
                val = entry["mainsnak"]["datavalue"]["value"]["amount"]
                # Wikidata amounts are strings like "+23000" or "-0"
                return abs(int(float(val)))
            except Exception:
                continue
        return None

    # P1148 = number of active military personnel
    active = _get_quantity("P1148")
    if active:
        result["active"] = active

    # P2031 = number of reserve personnel
    reserve = _get_quantity("P2031")
    if reserve:
        result["reserve"] = reserve

    # P1082 = population (sometimes used for paramilitary/guard strength — skip)
    return result


# ── RSS ───────────────────────────────────────────────────────────────────────

def fetch_rss_items(feed_url: str, max_items: int = MAX_RSS_ITEMS_PER_FEED) -> list[dict]:
    """Fetch and parse an RSS/Atom feed, return list of {title, link, published, summary}."""
    try:
        req = urllib.request.Request(
            feed_url,
            headers={
                "User-Agent": "baltic-nato-dashboard/1.0 (https://github.com/juliuskvx/baltic-nato-dashboard)",
                "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
            }
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
    except Exception as e:
        print(f"  [RSS] Failed to fetch {feed_url}: {e}", file=sys.stderr)
        return []

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        print(f"  [RSS] Parse error for {feed_url}: {e}", file=sys.stderr)
        return []

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    items = []

    # RSS 2.0
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        summary = (item.findtext("description") or "").strip()
        items.append({"title": title, "link": link, "published": pub, "summary": summary[:500]})
        if len(items) >= max_items:
            break

    # Atom
    if not items:
        for entry in root.findall(".//atom:entry", ns):
            title = (entry.findtext("atom:title", namespaces=ns) or "").strip()
            link_el = entry.find("atom:link", ns)
            link = link_el.get("href", "") if link_el is not None else ""
            pub = (entry.findtext("atom:updated", namespaces=ns) or
                   entry.findtext("atom:published", namespaces=ns) or "").strip()
            summary = (entry.findtext("atom:summary", namespaces=ns) or
                       entry.findtext("atom:content", namespaces=ns) or "").strip()
            items.append({"title": title, "link": link, "published": pub, "summary": summary[:500]})
            if len(items) >= max_items:
                break

    return items


def filter_relevant_items(items: list[dict]) -> list[dict]:
    """Keep only items that mention Baltic/NATO defence keywords."""
    relevant = []
    for item in items:
        text = (item["title"] + " " + item["summary"]).lower()
        if any(kw in text for kw in KEYWORDS):
            relevant.append(item)
    return relevant


# ── Claude API extraction ─────────────────────────────────────────────────────

CLAUDE_MODEL = "claude-sonnet-4-6"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


def call_claude(system_prompt: str, user_prompt: str) -> str:
    """Call Anthropic /v1/messages and return the text response."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 4096,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode(),
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode())

    # Extract text from content blocks
    texts = [block["text"] for block in data.get("content", []) if block.get("type") == "text"]
    return "\n".join(texts)


EXTRACTION_SYSTEM = """You are a defence procurement analyst. 
You will be given a list of recent news headlines and summaries from defence news RSS feeds.
Your task is to extract structured procurement and news data for Lithuania, Latvia, and Estonia.

Return ONLY a valid JSON object in this exact format — no markdown, no explanation:
{
  "lithuania": {
    "news": ["bullet string 1 (include date at end in parentheses if known)", "..."],
    "procurement": [
      {
        "item": "short name of system or contract",
        "value": "monetary value or 'Not disclosed'",
        "supplier": "company/country",
        "delivery": "timeline or 'Not specified'",
        "source_url": "URL of the article",
        "date_reported": "YYYY-MM-DD or empty string"
      }
    ]
  },
  "latvia": { "news": [], "procurement": [] },
  "estonia": { "news": [], "procurement": [] }
}

Rules:
- Only include items clearly about Lithuania, Latvia, or Estonia defence/military.
- news: array of plain strings, each a 1-2 sentence summary of the item. Max 8 per country.
- procurement: only include items that are contracts, orders, deliveries, or major procurement decisions. Max 6 per country.
- If nothing relevant for a country, return empty arrays for that country.
- Do NOT invent data. If a field is unknown write "Not disclosed" or leave empty string.
- date_reported must be YYYY-MM-DD format or empty string.
- Return only the raw JSON, nothing else."""


def extract_with_claude(items: list[dict]) -> dict:
    """Send RSS items to Claude and get structured extraction per country."""
    if not items:
        return {"lithuania": {"news": [], "procurement": []},
                "latvia": {"news": [], "procurement": []},
                "estonia": {"news": [], "procurement": []}}

    # Build a compact text block of items to send
    lines = []
    for i, item in enumerate(items[:MAX_ITEMS_FOR_LLM], 1):
        lines.append(f"[{i}] {item['title']}")
        if item.get("published"):
            lines.append(f"    Date: {item['published']}")
        if item.get("link"):
            lines.append(f"    URL: {item['link']}")
        if item.get("summary"):
            lines.append(f"    Summary: {item['summary'][:300]}")
        lines.append("")

    user_prompt = (
        f"Today's date: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}\n\n"
        f"Here are {len(items[:MAX_ITEMS_FOR_LLM])} recent defence news items. "
        "Extract structured data for Lithuania, Latvia, and Estonia:\n\n"
        + "\n".join(lines)
    )

    print(f"  [Claude] Sending {min(len(items), MAX_ITEMS_FOR_LLM)} items for extraction...")
    raw = call_claude(EXTRACTION_SYSTEM, user_prompt)

    # Strip any accidental markdown fences
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.rsplit("```", 1)[0]
    raw = raw.strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"  [Claude] JSON parse error: {e}\nRaw response:\n{raw[:500]}", file=sys.stderr)
        return {"lithuania": {"news": [], "procurement": []},
                "latvia": {"news": [], "procurement": []},
                "estonia": {"news": [], "procurement": []}}


# ── Wikidata personnel prompt ─────────────────────────────────────────────────

PERSONNEL_SYSTEM = """You are a military analyst. Given Wikidata personnel data and the current country JSON, 
return an updated personnel object as valid JSON only (no markdown).
Only update fields where Wikidata has a reliable figure. Keep existing values if Wikidata returns nothing useful.
Return only the raw JSON object for the 'personnel' field."""


def merge_personnel(country: str, existing_personnel: dict, wikidata: dict) -> dict:
    """Merge Wikidata personnel data with existing, preferring Wikidata when available."""
    merged = dict(existing_personnel)
    if wikidata.get("active"):
        merged["active"] = wikidata["active"]
        print(f"  [Wikidata] Updated {country} active personnel: {wikidata['active']}")
    if wikidata.get("reserve"):
        merged["reserve"] = wikidata["reserve"]
        print(f"  [Wikidata] Updated {country} reserve personnel: {wikidata['reserve']}")
    return merged


# ── JSON merge ────────────────────────────────────────────────────────────────

def dedup_procurement(existing: list, new_items: list) -> list:
    """Add new procurement items, avoiding duplicates by item name similarity."""
    existing_names = {p["item"].lower()[:40] for p in existing}
    result = list(existing)
    for item in new_items:
        key = item.get("item", "").lower()[:40]
        if key and key not in existing_names:
            result.append(item)
            existing_names.add(key)
    # Keep most recent 10 entries
    return result[-10:]


def dedup_news(existing: list, new_items: list) -> list:
    """Add new news bullets, avoiding near-duplicates."""
    existing_lower = {n.lower()[:60] for n in existing}
    result = list(existing)
    for item in new_items:
        key = item.lower()[:60]
        if key and key not in existing_lower:
            result.append(item)
            existing_lower.add(key)
    # Keep most recent 8 bullets
    return result[-8:]


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

    # ── Step 1: Wikidata personnel ────────────────────────────────────────────
    print("Step 1: Fetching Wikidata personnel figures...")
    wikidata_results = {}
    for country, cfg in WIKIDATA_ENTITIES.items():
        print(f"  Fetching {cfg['country_label']} ({cfg['armed_forces']})...")
        wikidata_results[country] = fetch_wikidata_personnel(cfg["armed_forces"])
        print(f"    → {wikidata_results[country]}")

    # ── Step 2: RSS feeds ─────────────────────────────────────────────────────
    print("\nStep 2: Fetching RSS feeds...")
    all_items = []
    for feed_url in RSS_FEEDS:
        print(f"  {feed_url}")
        items = fetch_rss_items(feed_url)
        relevant = filter_relevant_items(items)
        print(f"    → {len(items)} total, {len(relevant)} relevant")
        all_items.extend(relevant)

    # Deduplicate by URL
    seen_urls = set()
    unique_items = []
    for item in all_items:
        url = item.get("link", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            unique_items.append(item)
        elif not url:
            unique_items.append(item)

    print(f"  Total unique relevant items: {len(unique_items)}")

    # ── Step 3: Claude extraction ─────────────────────────────────────────────
    print("\nStep 3: Extracting structured data with Claude...")
    extracted = {"lithuania": {"news": [], "procurement": []},
                 "latvia": {"news": [], "procurement": []},
                 "estonia": {"news": [], "procurement": []}}

    if ANTHROPIC_API_KEY and unique_items:
        try:
            extracted = extract_with_claude(unique_items)
            print("  [Claude] Extraction complete.")
        except Exception as e:
            print(f"  [Claude] Error: {e}", file=sys.stderr)
    elif not ANTHROPIC_API_KEY:
        print("  [Claude] ANTHROPIC_API_KEY not set — skipping extraction.")
    else:
        print("  [Claude] No relevant RSS items found — skipping extraction.")

    # ── Step 4: Merge and write JSON ──────────────────────────────────────────
    print("\nStep 4: Merging and writing JSON files...")
    for country in ["lithuania", "latvia", "estonia"]:
        json_path = os.path.join(DATA_DIR, f"{country}.json")
        if not os.path.exists(json_path):
            print(f"  [!] {json_path} not found, skipping.", file=sys.stderr)
            continue

        data = load_json(json_path)

        # Personnel from Wikidata
        if wikidata_results.get(country):
            data["personnel"] = merge_personnel(
                country, data.get("personnel", {}), wikidata_results[country]
            )

        # News from Claude
        new_news = extracted.get(country, {}).get("news", [])
        if new_news:
            data["news"] = dedup_news(data.get("news", []), new_news)
            print(f"  [{country}] News: {len(new_news)} new items added/merged")

        # Procurement from Claude
        new_proc = extracted.get(country, {}).get("procurement", [])
        if new_proc:
            data["procurement"] = dedup_procurement(data.get("procurement", []), new_proc)
            print(f"  [{country}] Procurement: {len(new_proc)} new items added/merged")

        # Update last_updated
        data["last_updated"] = today

        save_json(json_path, data)

    print(f"\n=== Refresh complete — {today} ===")


if __name__ == "__main__":
    main()
