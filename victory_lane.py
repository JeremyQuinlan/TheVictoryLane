"""
The Victory Lane — Morning Game Plan
--------------------------------------
Polls Yahoo Mail via IMAP for Vital Knowledge (and optionally Earnings Whispers /
Hammerstone) newsletters, parses them into structured MGP sections with Claude,
reads Trade Ideas scanner CSVs, fetches Benzinga news for scanner tickers,
and publishes a styled Morning Game Plan dashboard to GitHub Pages.

Folder: C:\\Tools\\TheVictoryLane\\
Secrets (env vars / GitHub Actions secrets):
  YAHOO_EMAIL, YAHOO_APP_PASSWORD, ANTHROPIC_API_KEY,
  NOTION_TOKEN, BENZINGA_API_KEY (Massive reseller)

TO RUN LOCALLY:
  $env:YAHOO_EMAIL="..."; $env:YAHOO_APP_PASSWORD="..."; $env:ANTHROPIC_API_KEY="..."; python victory_lane.py
"""

import imaplib
import email
import base64
import json
import os
import re
import glob
import csv
import subprocess
import requests
import anthropic
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
    # Load .env from the same directory as this script, regardless of where it's run from
    _env_path = Path(__file__).parent / ".env"
    load_dotenv(dotenv_path=_env_path)
except ImportError:
    pass  # dotenv optional — falls back to system env vars

EASTERN = ZoneInfo("America/New_York")

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
CONFIG = {
    "yahoo_email":           os.environ.get("YAHOO_EMAIL",        "YOUR_EMAIL@yahoo.com"),
    "yahoo_app_password":    os.environ.get("YAHOO_APP_PASSWORD", "YOUR_APP_PASSWORD"),
    "anthropic_api_key":     os.environ.get("ANTHROPIC_API_KEY",  "YOUR_ANTHROPIC_API_KEY"),
    "benzinga_api_key":      os.environ.get("BENZINGA_API_KEY",   ""),
    "notion_token":          os.environ.get("NOTION_TOKEN",        ""),
    "stocks_on_watch_db_id": "2ee48333-7409-81a3-a830-000b9ce19118",
    "lookback_hours":        168,
    "scanner_csv_dir":       "scanners" if os.environ.get("GITHUB_ACTIONS", "false").lower() == "true" else r"C:\Users\jerem\Documents\TradeIdeasPro",
    "edge_exe":              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "tts_rate":              1.3,
    "github_actions":        os.environ.get("GITHUB_ACTIONS", "false").lower() == "true",
}

# ── Email sources ──
EMAIL_SOURCES = [
    {"name": "vitalknowledge",  "sender_filter": "vitalknowledge",  "source_type": "vk"},
    {"name": "earningswhispers","sender_filter": "earningswhispers","source_type": "ew"},
    {"name": "hammerstone",     "sender_filter": os.environ.get("HAMMERSTONE_SENDER", "hammerstone"), "source_type": "hs"},
]
# ─────────────────────────────────────────────

SITE_NAME = "The Victory Lane"

# ── Scanner name patterns → display label ──
SCANNER_LABELS = {
    "changing": "Changing Fundamentals",
    "usual":    "Usual Suspects Volume",
    "premarket":"Premarket Volume",
    "high":     "High PM AVOL",
    "low float":"Low Floats & Small Caps",
    "gapper":   "Gappers",
    "earnings": "Earnings Today",
}

# Order scanners appear on the dashboard (unlisted scanners append at end)
SCANNER_DISPLAY_ORDER = [
    "Changing Fundamentals",
    "High PM AVOL",
    "Earnings Today",
    "Usual Suspects Volume",
    "Low Floats & Small Caps",
]

# ─────────────────────────────────────────────
# PROMPTS
# ─────────────────────────────────────────────

VK_MGP_PROMPT = """You are parsing a Vital Knowledge newsletter from Adam Crisafulli for a day trader's Morning Game Plan.

Extract structured data using the store_mgp_data tool. Fill every field accurately from the newsletter content.

Fields:
- email_type: "dawn" (pre-market morning), "midday", "recap" (post-close), or "other"
- macro: 2-3 sentences on broad market context — index moves, risk tone, overnight action
- market_outlook: Adam's directional view — bull or bear leaning, key catalyst to watch today
- company_items: ALL companies with meaningful commentary (max 10). Each needs ticker, company name,
  2-3 sentence summary with specific numbers, catalyst type, and direction (bullish/bearish/mixed)
- sectors: sector-level commentary worth noting (empty string if none)
- earnings_today: list of tickers reporting today e.g. ["AAPL BMO", "MSFT AMC"]
- key_dates: upcoming catalysts e.g. ["Sep 17 — CPI 8:30am", "Sep 18 — FOMC 2pm"]
- tts_summary: 350-450 word audio-ready summary in Adam's direct confident voice. Write for ear not eye.
  No brand names (do not say Vital Knowledge). Lead with the macro read, then key company items, then outlook.

Rules:
- Express all basis-point references as percentages (e.g. "25bp" → "0.25%", "100bp" → "1.0%")
- direction: bullish if net positive for the stock, bearish if negative, mixed if unclear
- Empty string "" or empty array [] for fields with no content

Newsletter content:
{body}"""


EW_SUMMARIZE_PROMPT = """You are summarizing an earnings preview newsletter for a trading team.
For each company create a section:

## TICKER — Company Name

Include ONLY:
- Confirmed earnings date/time (BMO or AMC)
- Consensus EPS and revenue estimates
- Whisper number if different
- Options implied move vs historical average move
- Analyst sentiment (% bullish/bearish)
- Any notable guidance vs consensus divergence

Do NOT include price action, moving averages, or technical commentary.

Newsletter content:
{body}

Rules: direct voice, no newsletter name in output, specific numbers, order by earnings date."""


HS_SUMMARIZE_PROMPT = """You are summarizing a real-time news alert for a trading team.

## MACRO / FED
## SECTOR
## STOCKS

Stocks section: ticker in bold, 2-3 sentences max per item. Include price reaction, move size, catalyst.
No brand name in output. Direct voice.

Alert content:
{body}"""


CF_GRADING_PROMPT = """You are a senior equity trader. Scan this financial news email and identify
CHANGING FUNDAMENTAL (CF) events — news that forces institutional repositioning.

Respond ONLY with valid JSON:

{{
  "items": [
    {{
      "ticker": "AAPL",
      "type": "STOCK",
      "grade": "A",
      "catalyst": "Earnings Beat",
      "setup": "Day 1 Earnings",
      "notes": "Beat by $0.18 EPS on strong services growth; guided above consensus. Classic episodic pivot.",
      "trade_plan": "Watch for gap-and-go or base building off the earnings gap level",
      "key_levels": "195 support (earnings gap); 210 resistance"
    }}
  ]
}}

Grades: A+ (transformative), A (strong CF), A- (meaningful), B+ (notable), B (watch list), B- (marginal)
Types: STOCK, SECTOR, MACRO
Catalysts: Earnings Beat, Earnings Miss, Guidance Raise, Guidance Cut, M&A, FDA Approval, FDA Rejection,
  Massive Capex, Leadership Change, Partnership, Activist Entry, Short Report, Macro Surprise, Fed Pivot,
  Sector Reprice, Buyback, Dividend Cut, Legal Settlement, Regulatory Action, Product Launch, Contract Win,
  Bankruptcy, Restructuring, Other
Setups: Changing Fundamentals, Day 1 Earnings, Day 1 News, Breaking News, Macro Event Swing,
  HTF Reversal, Short Squeeze, Sector Rotation, Other

Rules:
- Only A- or higher if genuinely out of step with consensus
- Max 5 items — pick highest grades if more qualify
- Return {{"items": []}} if nothing meets B- threshold
- Return ONLY the JSON

Email subject: {subject}
Email content:
{body}"""


# ─────────────────────────────────────────────
# EMAIL UTILITIES (unchanged from v1)
# ─────────────────────────────────────────────

def decode_str(s):
    if s is None:
        return ""
    parts = decode_header(s)
    result = []
    for part, enc in parts:
        if isinstance(part, bytes):
            result.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            result.append(part)
    return " ".join(result)


def get_email_body(msg):
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                body = part.get_payload(decode=True).decode("utf-8", errors="replace")
                break
            elif ct == "text/html" and "attachment" not in cd and not body:
                raw_html = part.get_payload(decode=True).decode("utf-8", errors="replace")
                body = re.sub(r"<[^>]+>", " ", raw_html)
                body = re.sub(r"\s+", " ", body).strip()
    else:
        body = msg.get_payload(decode=True).decode("utf-8", errors="replace")
    return body[:30000]


def get_email_date(msg):
    date_str = msg.get("Date", "")
    try:
        dt = parsedate_to_datetime(date_str).astimezone(EASTERN)
        return dt.strftime("%b %d, %Y · %I:%M %p ET")
    except Exception:
        return datetime.now(EASTERN).strftime("%b %d, %Y · %I:%M %p ET")


def get_email_sent_utc(msg):
    date_str = msg.get("Date", "")
    try:
        dt = parsedate_to_datetime(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def load_processed_ids():
    path = "processed_ids.json"
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_processed_ids(ids):
    with open("processed_ids.json", "w", encoding="utf-8") as f:
        json.dump(list(ids), f)


def clean_subject(raw_subject, source_type="vk"):
    s = re.sub(r"^Vital Knowledge:\s*", "", raw_subject).strip()
    date_match = re.search(r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+\w+\s+\d+,?\s*\d{4}", s)
    date_str = date_match.group(0) if date_match else ""
    s_lower = s.lower()
    if source_type == "vk":
        if "dawn" in s_lower or "morning" in s_lower:
            return f"Morning Intelligentsia · {date_str}".strip(" ·")
        elif "mid-day" in s_lower or "midday" in s_lower:
            return f"Mid-Day Update · {date_str}".strip(" ·")
        elif "recap" in s_lower or "close" in s_lower:
            return f"Market Recap · {date_str}".strip(" ·")
        else:
            s = re.sub(r"\b(vital|knowledge|dawn)\b", "", s, flags=re.IGNORECASE)
            return re.sub(r"\s+", " ", s).strip(" -·")
    elif source_type == "ew":
        date_match2 = re.search(r"\w+\s+\d+,?\s*\d{4}", raw_subject)
        date_str2 = date_match2.group(0).strip(", ") if date_match2 else ""
        if "most anticipated" in raw_subject.lower() or "releases" in raw_subject.lower():
            return f"Earnings Calendar · {date_str2}".strip(" ·")
        return f"Earnings Preview · {date_str2}".strip(" ·")
    elif source_type == "hs":
        s2 = re.sub(r"(?i)^hammerstone\s*[-:·]?\s*", "", raw_subject).strip()
        s2 = re.sub(r"(?i)\bhammerstone\b", "", s2).strip(" -·")
        return s2 or "Market Alert"
    return s


def get_category_tag(subject):
    s = subject.lower()
    if "earnings calendar" in s: return "EARNINGS"
    if "earnings preview" in s:  return "EARNINGS"
    if "morning" in s:           return "MORNING"
    if "mid-day" in s:           return "MID-DAY"
    if "recap" in s:             return "RECAP"
    if "alert" in s:             return "ALERT"
    return "UPDATE"


def get_tag_color(tag):
    colors = {
        "MORNING":  ("#1a3a2a", "#4caf82"),
        "MID-DAY":  ("#1a2a3a", "#4c8faf"),
        "RECAP":    ("#2a1a1a", "#af4c4c"),
        "UPDATE":   ("#2a1a3a", "#8f4caf"),
        "EARNINGS": ("#2a2a1a", "#c9b97a"),
        "ALERT":    ("#2a1a1a", "#e07050"),
    }
    return colors.get(tag, ("#1a1a1a", "#888888"))


# ─────────────────────────────────────────────
# CLAUDE API CALLS
# ─────────────────────────────────────────────

def parse_vk_to_mgp(body, api_key):
    """Parse VK email into structured MGP data via tool use — guarantees valid JSON output."""
    client = anthropic.Anthropic(api_key=api_key)
    tools = [{
        "name": "store_mgp_data",
        "description": "Store the parsed Morning Game Plan data from the VK newsletter.",
        "input_schema": {
            "type": "object",
            "properties": {
                "email_type":     {"type": "string", "enum": ["dawn", "midday", "recap", "other"]},
                "macro":          {"type": "string"},
                "market_outlook": {"type": "string"},
                "company_items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "ticker":    {"type": "string"},
                            "company":   {"type": "string"},
                            "summary":   {"type": "string"},
                            "catalyst":  {"type": "string"},
                            "direction": {"type": "string", "enum": ["bullish", "bearish", "mixed"]},
                        },
                        "required": ["ticker", "company", "summary", "catalyst", "direction"],
                    },
                },
                "sectors":       {"type": "string"},
                "earnings_today": {"type": "array", "items": {"type": "string"}},
                "key_dates":      {"type": "array", "items": {"type": "string"}},
                "tts_summary":    {"type": "string"},
            },
            "required": ["email_type", "macro", "market_outlook", "company_items",
                         "sectors", "earnings_today", "key_dates", "tts_summary"],
        },
    }]
    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4000,
            tools=tools,
            tool_choice={"type": "any"},
            messages=[{"role": "user", "content": VK_MGP_PROMPT.format(body=body[:28000])}],
        )
        for block in msg.content:
            if block.type == "tool_use" and block.name == "store_mgp_data":
                data = block.input
                print(f"  VK parse: {len(data.get('company_items', []))} company item(s), type={data.get('email_type')}")
                return data
        print("  VK parse: no tool_use block returned")
        return None
    except Exception as e:
        print(f"  VK parse error: {e}")
        return None


def summarize_generic(body, api_key, source_type):
    """Fallback summarizer for EW and HS emails."""
    client = anthropic.Anthropic(api_key=api_key)
    if source_type == "ew":
        prompt, max_tok = EW_SUMMARIZE_PROMPT, 3000
    else:
        prompt, max_tok = HS_SUMMARIZE_PROMPT, 1500
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tok,
        messages=[{"role": "user", "content": prompt.format(body=body)}]
    )
    return msg.content[0].text


def grade_for_cf(body, subject, api_key):
    """CF grading pass via tool use — returns list of flagged items or []."""
    client = anthropic.Anthropic(api_key=api_key)
    tools = [{
        "name": "store_cf_grades",
        "description": "Store the Changing Fundamentals graded items.",
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "ticker":     {"type": "string"},
                            "type":       {"type": "string"},
                            "grade":      {"type": "string"},
                            "catalyst":   {"type": "string"},
                            "setup":      {"type": "string"},
                            "notes":      {"type": "string"},
                            "trade_plan": {"type": "string"},
                            "key_levels": {"type": "string"},
                        },
                        "required": ["ticker", "type", "grade", "catalyst", "setup",
                                     "notes", "trade_plan", "key_levels"],
                    },
                }
            },
            "required": ["items"],
        },
    }]
    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            tools=tools,
            tool_choice={"type": "any"},
            messages=[{"role": "user", "content": CF_GRADING_PROMPT.format(subject=subject, body=body[:25000])}],
        )
        for block in msg.content:
            if block.type == "tool_use" and block.name == "store_cf_grades":
                items = block.input.get("items", [])
                print(f"  CF grading: {len(items)} flagged item(s)")
                return items
        return []
    except Exception as e:
        print(f"  CF grading error: {e}")
        return []


# ─────────────────────────────────────────────
# NOTION PUSH (unchanged from v1)
# ─────────────────────────────────────────────

def push_to_notion_stocks_on_watch(items, notion_token, db_id, target_date=None):
    if not notion_token or not items:
        if not notion_token:
            print("  Notion: NOTION_TOKEN not set — skipping")
        return
    if target_date is None:
        target_date = datetime.now(EASTERN).strftime("%Y-%m-%d")
    headers = {
        "Authorization": f"Bearer {notion_token}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }
    def rt(text):
        return [{"type": "text", "text": {"content": text[:2000]}}] if text else []

    pushed = 0
    for item in items:
        ticker = item.get("ticker", "").strip()
        if not ticker:
            continue
        properties = {"Ticker": {"title": rt(ticker)}, "Date": {"date": {"start": target_date}}}
        for field, notion_key in [("catalyst","Catalyst"),("grade","Grade"),("setup","Setup")]:
            val = item.get(field, "").strip()
            if val:
                properties[notion_key] = {"multi_select": [{"name": val}]}
        for field, notion_key in [("notes","Notes"),("trade_plan","Trade Plan"),("key_levels","Key Levels")]:
            val = item.get(field, "").strip()
            if val:
                properties[notion_key] = {"rich_text": rt(val)}
        try:
            resp = requests.post("https://api.notion.com/v1/pages",
                                 headers=headers, json={"parent": {"database_id": db_id}, "properties": properties}, timeout=15)
            if resp.status_code in (200, 201):
                pushed += 1
                print(f"  ✓ Notion: pushed {ticker} ({item.get('grade','')})")
            else:
                print(f"  ✗ Notion push failed {ticker}: {resp.status_code}")
        except Exception as e:
            print(f"  ✗ Notion push error {ticker}: {e}")
    print(f"  Notion: {pushed}/{len(items)} pushed")


# ─────────────────────────────────────────────
# SCANNER CSV READER
# ─────────────────────────────────────────────

def read_scanner_csvs(csv_dir):
    """
    Read Trade Ideas scanner CSVs from csv_dir.
    Returns dict: { scanner_label: [ {symbol, price, avol_pct, chg_close, chg_20d, chg_50d, ...}, ... ] }
    Gracefully returns {} if dir doesn't exist or no CSVs found.
    """
    if not os.path.isdir(csv_dir):
        print(f"  Scanner CSV dir not found: {csv_dir} — skipping")
        return {}

    # Find all CSVs modified in the last 24 hours
    cutoff = datetime.now().timestamp() - 86400
    csv_files = [f for f in glob.glob(os.path.join(csv_dir, "*.csv"))
                 if os.path.getmtime(f) >= cutoff]

    if not csv_files:
        print(f"  No recent scanner CSVs found in {csv_dir}")
        return {}

    result = {}
    for filepath in csv_files:
        fname = os.path.basename(filepath).lower()
        # Determine scanner label from filename
        label = "Scanner"
        for key, lbl in SCANNER_LABELS.items():
            if key in fname:
                label = lbl
                break

        rows = []
        try:
            with open(filepath, encoding="utf-8", errors="replace") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    symbol = (row.get("Symbol") or row.get("symbol") or "").strip()
                    if not symbol:
                        continue
                    rows.append({
                        "symbol":    symbol,
                        "price":     _safe_float(row.get("Price") or row.get("price")),
                        "chg_close": _safe_float(row.get("Change from the Close") or row.get("Chg Close")),
                        "vol_today": _safe_float(row.get("Volume Today") or row.get("Vol Today")),
                        "rel_vol":   _safe_float(row.get("Relative Volume") or row.get("Rel Vol")),
                        "chg_20d":   _safe_float(row.get("Change from 20 Day SMA") or row.get("Chg 20 Day")),
                        "chg_50d":   _safe_float(row.get("Change from 50 Day SMA") or row.get("Chg 50 Day")),
                        "chg_200d":  _safe_float(row.get("Change from 200 Day SMA") or row.get("Chg 200 Day")),
                        "pos_yr":    _safe_float(row.get("Position in Year Range") or row.get("Pos Yr Rng (%)")),
                        "pos_life":  _safe_float(row.get("Position in Lifetime Range") or row.get("Pos Lifetime")),
                        "earn_date": (row.get("Earnings Date") or row.get("Earn Date") or "").strip(),
                        "timestamp": (row.get("TimeStamp") or row.get("Timestamp") or "").strip(),
                    })
        except Exception as e:
            print(f"  CSV read error {filepath}: {e}")
            continue

        if rows:
            if label not in result:
                result[label] = []
            result[label].extend(rows)
            print(f"  Scanner '{label}': {len(rows)} row(s) from {os.path.basename(filepath)}")

    return result


def _safe_float(val):
    try:
        return float(str(val).replace(",", "").strip())
    except Exception:
        return None


# ─────────────────────────────────────────────
# BENZINGA NEWS FETCH
# ─────────────────────────────────────────────

def _parse_earnings_beat(title, teaser):
    """
    Scan a Benzinga news headline + teaser for earnings beat/miss or guidance info.
    Returns a short badge string like '+$0.18 EPS beat' or 'Guidance ↑', or None.
    """
    text = (title + " " + teaser).lower()

    # EPS beat with dollar amount: "beats by $0.18" / "beat estimates of $1.05, reported $1.23"
    m = re.search(r'beat[s]?\s+(?:estimates?\s+)?by\s+\$?([\d.]+)', text)
    if m:
        return f"+${m.group(1)} EPS"

    m = re.search(r'miss(?:es)?\s+(?:estimates?\s+)?by\s+\$?([\d.]+)', text)
    if m:
        return f"-${m.group(1)} EPS"

    # Percentage beat: "beats estimates by 12%"
    m = re.search(r'beat[s]?\s+\w+\s+by\s+([\d.]+)%', text)
    if m:
        return f"+{m.group(1)}% beat"

    # Generic beats/misses without amount
    if re.search(r'\bbeat[s]?\b.{0,30}\bestimate', text):
        return "Beat Est."
    if re.search(r'\bmiss(?:es)?\b.{0,30}\bestimate', text):
        return "Miss Est."

    # Guidance
    if re.search(r'rais(?:es?|ed)\s+(?:full.year\s+)?guidance|lifts?\s+guidance|raises?\s+outlook', text):
        return "Guidance ↑"
    if re.search(r'(?:cut|cut|lower|reduc)[s]?\s+guidance|cuts?\s+outlook', text):
        return "Guidance ↓"
    if re.search(r'reaffirm[s]?\s+guidance|maintains?\s+guidance', text):
        return "Guidance ="

    return None


def fetch_benzinga_news(tickers, api_key, hours_back=20):
    """
    Fetch news for a list of tickers via Massive/Benzinga API.
    Returns dict: { ticker: [ {published, title, teaser, channels, beat}, ... ] }
    Gracefully returns {} if no API key or request fails.
    """
    if not api_key:
        print("  BENZINGA_API_KEY not set — skipping news fetch")
        return {}
    if not tickers:
        return {}

    since = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = {}

    for ticker in tickers:
        try:
            resp = requests.get(
                "https://api.massive.com/benzinga/v2/news",
                params={"apiKey": api_key, "stocks": ticker, "published": since,
                        "limit": "5", "sort": "published.desc"},
                timeout=15,
            )
            data = resp.json()
            stories = data.get("results", [])
            if stories:
                result[ticker] = []
                for s in stories:
                    channels = s.get("channels", [])
                    if channels and isinstance(channels[0], dict):
                        channels = [c.get("name", "") for c in channels]
                    title  = s.get("title", "")
                    teaser = s.get("teaser", "")[:200]
                    beat   = _parse_earnings_beat(title, teaser)
                    result[ticker].append({
                        "published": s.get("published", "")[:16],
                        "title":     title,
                        "teaser":    teaser,
                        "channels":  channels,
                        "beat":      beat,
                    })
                print(f"  Benzinga {ticker}: {len(stories)} story(ies)")
        except Exception as e:
            print(f"  Benzinga fetch error {ticker}: {e}")

    return result


# ─────────────────────────────────────────────
# BENZINGA CACHE (per-day, per-ticker)
# ─────────────────────────────────────────────

BENZINGA_CACHE_PATH = "docs/benzinga_cache.json"

def load_benzinga_cache():
    """Load today's Benzinga cache. Returns {} if stale or missing."""
    today = datetime.now(EASTERN).strftime("%Y-%m-%d")
    if os.path.exists(BENZINGA_CACHE_PATH):
        try:
            with open(BENZINGA_CACHE_PATH, encoding="utf-8") as f:
                cached = json.load(f)
            if cached.get("date") == today:
                data = cached.get("data", {})
                print(f"  Benzinga cache: {len(data)} ticker(s) already fetched today")
                return data
        except Exception as e:
            print(f"  Benzinga cache load error: {e}")
    return {}


def save_benzinga_cache(data):
    """Save today's Benzinga results so subsequent runs skip re-fetching."""
    today = datetime.now(EASTERN).strftime("%Y-%m-%d")
    os.makedirs("docs", exist_ok=True)
    with open(BENZINGA_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump({"date": today, "data": data}, f, indent=2, ensure_ascii=False)
    print(f"  Benzinga cache: saved {len(data)} ticker(s)")


# ─────────────────────────────────────────────
# IMAP EMAIL FETCH
# ─────────────────────────────────────────────

def fetch_new_emails(config):
    """Single IMAP connection — fetches new emails AND latest VK for dashboard.
    Returns (results, processed_ids, latest_vk_body, latest_vk_subject)."""
    print("  IMAP: connecting to imap.mail.yahoo.com:993...")
    mail = imaplib.IMAP4_SSL("imap.mail.yahoo.com", 993)
    print(f"  IMAP: logging in as {config['yahoo_email'][:4]}****")
    mail.login(config["yahoo_email"], config["yahoo_app_password"])
    mail.select("inbox")
    print("  IMAP: inbox selected")

    processed = load_processed_ids()
    since_date = (datetime.utcnow() - timedelta(hours=config["lookback_hours"] + 24)).strftime("%d-%b-%Y")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=config["lookback_hours"])
    print(f"  IMAP: searching since {since_date} (lookback={config['lookback_hours']}h)")

    # ── Grab latest VK email for dashboard panel (while connection is open) ──
    latest_vk_body    = None
    latest_vk_subject = None
    try:
        status, data = mail.uid("search", None, f'(SINCE "{since_date}" FROM "vitalknowledge")')
        vk_uids = data[0].split() if data and data[0] else []
        print(f"  [vitalknowledge] {len(vk_uids)} email(s) found for dashboard fetch")
        if vk_uids:
            uid = vk_uids[-1]  # most recent
            status, msg_data = mail.uid("fetch", uid, "(BODY.PEEK[])")
            if msg_data and msg_data[0] is not None:
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)
                latest_vk_body    = get_email_body(msg)
                latest_vk_subject = decode_str(msg.get("Subject", ""))
                print(f"  [vitalknowledge] dashboard email fetched: {latest_vk_subject[:80]}")
            else:
                print("  [vitalknowledge] fetch returned empty — msg_data was None/empty")
        else:
            print(f"  [vitalknowledge] 0 UIDs returned — no email in lookback window")
    except Exception as e:
        import traceback
        print(f"  [vitalknowledge] dashboard fetch FAILED: {e}")
        traceback.print_exc()

    # Search per-sender so we only fetch the emails we actually need
    # (avoids downloading hundreds of unrelated emails and hitting Yahoo's IMAP timeout)
    all_candidate_uids = set()
    for source in EMAIL_SOURCES:
        try:
            status, data = mail.uid("search", None, f'(SINCE "{since_date}" FROM "{source["sender_filter"]}")')
            uids = data[0].split() if data and data[0] else []
            print(f"  [{source['name']}] {len(uids)} email(s) found since {since_date}")
            all_candidate_uids.update(uid.decode() for uid in uids)
        except Exception as e:
            print(f"  Search error for [{source['name']}]: {e}")

    results = []
    for uid_str in sorted(all_candidate_uids, key=lambda x: int(x)):
        if uid_str in processed:
            continue
        try:
            status, msg_data = mail.uid("fetch", uid_str.encode(), "(BODY.PEEK[])")
        except Exception:
            # Reconnect once if Yahoo drops the connection mid-batch
            try:
                mail = imaplib.IMAP4_SSL("imap.mail.yahoo.com", 993)
                mail.login(config["yahoo_email"], config["yahoo_app_password"])
                mail.select("inbox")
                status, msg_data = mail.uid("fetch", uid_str.encode(), "(BODY.PEEK[])")
            except Exception as e2:
                print(f"  Fetch error (uid {uid_str}): {e2}")
                continue

        if not msg_data or msg_data[0] is None:
            continue
        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)
        from_addr = decode_str(msg.get("From", "")).lower()

        matched_source = None
        for source in EMAIL_SOURCES:
            if source["sender_filter"].lower() in from_addr:
                matched_source = source
                break
        if not matched_source:
            continue

        sent_utc = get_email_sent_utc(msg)
        if sent_utc < cutoff:
            print(f"  Skipping old email (sent {sent_utc.strftime('%Y-%m-%d %H:%M UTC')})")
            continue

        raw_subject = decode_str(msg.get("Subject", "(No Subject)"))
        subject = clean_subject(raw_subject, matched_source["source_type"])
        body = get_email_body(msg)
        email_date = get_email_date(msg)

        if body.strip():
            results.append((uid_str, subject, body, email_date, sent_utc, matched_source["source_type"]))
            print(f"  Found [{matched_source['name']}]: {subject} ({email_date})")
            mail.uid("store", uid_str.encode(), "+FLAGS", "\\Seen")

    try:
        mail.logout()
    except Exception:
        pass
    results.sort(key=lambda x: x[4])
    return results, processed, latest_vk_body, latest_vk_subject


# ─────────────────────────────────────────────
# TTS UTILITIES
# ─────────────────────────────────────────────

def prepare_tts_text(text):
    """Clean text for TTS — expand abbreviations, strip brand names."""
    text = re.sub(r"\b(Vital Knowledge|Vital Dawn|Vital)\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*\((?:Bloomberg|WSJ|Wall Street Journal|NYT|FT|Reuters|Axios|CNBC|AP|Barron\'?s|MarketWatch)\)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bbp\b", "basis points", text)
    text = re.sub(r"\bBP\b", "basis points", text)
    text = re.sub(r"\bpct\b", "percent", text, flags=re.IGNORECASE)
    text = re.sub(r"\bYoY\b", "year over year", text, flags=re.IGNORECASE)
    text = re.sub(r"\bQoQ\b", "quarter over quarter", text, flags=re.IGNORECASE)
    text = re.sub(r"\bEPS\b", "earnings per share", text)
    text = re.sub(r"\bET\b",  "Eastern time", text)
    text = re.sub(r"\bBMO\b", "before the open", text)
    text = re.sub(r"\bAMC\b", "after the close", text)
    return re.sub(r"\s+", " ", text).strip()


TTS_JS = r"""
  const CHUNK_SIZE = 4000;
  let rate = TTS_RATE_PLACEHOLDER;
  let charIndex = 0;
  let isPaused = false;
  let utterance = null;
  const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  let iosWarmedUp = false;

  function getChunks(text) {
    const chunks = [];
    let start = 0;
    while (start < text.length) {
      let end = Math.min(start + CHUNK_SIZE, text.length);
      if (end < text.length) {
        const slice = text.slice(start, end);
        const lastPeriod = Math.max(slice.lastIndexOf(". "), slice.lastIndexOf("? "), slice.lastIndexOf("! "));
        if (lastPeriod > CHUNK_SIZE * 0.3) end = start + lastPeriod + 2;
      }
      chunks.push({ text: text.slice(start, end), start: start });
      start = end;
    }
    return chunks;
  }

  function getVoice() {
    const voices = window.speechSynthesis.getVoices();
    return (
      voices.find(v => v.name === "Microsoft David Desktop - English (United States)") ||
      voices.find(v => v.name.includes("David"))  ||
      voices.find(v => v.name.includes("Guy"))    ||
      voices.find(v => v.name === "Microsoft Aria Online (Natural) - English (United States)") ||
      voices.find(v => v.name.includes("Aria"))   ||
      voices.find(v => v.name === "Samantha")     ||
      voices.find(v => v.lang.startsWith("en-US")) ||
      voices.find(v => v.lang.startsWith("en"))   ||
      voices[0]
    );
  }

  function iosWarmup() {
    if (!isIOS || iosWarmedUp) return;
    const warm = new SpeechSynthesisUtterance(""); warm.volume = 0;
    window.speechSynthesis.speak(warm); iosWarmedUp = true;
  }

  function updateSpeed(val) {
    rate = parseFloat(val);
    document.getElementById("speed-val").textContent = rate.toFixed(1) + "x";
    if (window.speechSynthesis.speaking && !isPaused) speakFrom(charIndex);
  }

  function setStatus(msg) { document.getElementById("tts-status").textContent = msg; }

  function setPlayBtn(playing) {
    const btn = document.getElementById("btn-play");
    if (playing) { btn.innerHTML = "&#9646;&#9646; Pause"; btn.classList.add("active"); }
    else { btn.innerHTML = "&#9654; Play"; btn.classList.remove("active"); }
  }

  let _chunks = null;
  function chunks() { if (!_chunks) _chunks = getChunks(ttsText); return _chunks; }

  function findChunkIndex(charPos) {
    const ch = chunks();
    for (let i = ch.length - 1; i >= 0; i--) { if (charPos >= ch[i].start) return i; }
    return 0;
  }

  function speakChain(chunkIdx) {
    const ch = chunks();
    if (chunkIdx >= ch.length || isPaused) {
      if (!isPaused) { charIndex = 0; setPlayBtn(false); setStatus("Done \u2014 press Replay to listen again"); }
      return;
    }
    const chunk = ch[chunkIdx];
    utterance = new SpeechSynthesisUtterance(chunk.text);
    utterance.rate = rate; utterance.pitch = 1.0; utterance.volume = 1.0;
    utterance.onboundary = (e) => { if (e.name === "word") charIndex = chunk.start + e.charIndex; };
    utterance.onend = () => { if (!isPaused) speakChain(chunkIdx + 1); };
    utterance.onerror = (e) => { if (e.error !== "canceled") speakChain(chunkIdx + 1); };
    const voice = getVoice();
    if (voice) utterance.voice = voice;
    if (chunkIdx === 0) setStatus("Voice: " + (voice ? voice.name : "default"));
    setPlayBtn(true);
    window.speechSynthesis.speak(utterance);
  }

  function speakFrom(startChar) {
    window.speechSynthesis.cancel();
    const ch = chunks();
    startChar = Math.max(0, Math.min(startChar, ttsText.length - 1));
    charIndex = startChar;
    const chunkIdx = findChunkIndex(startChar);
    if (startChar > ch[chunkIdx].start) {
      const offset = startChar - ch[chunkIdx].start;
      const orig = ch[chunkIdx].text;
      ch[chunkIdx] = { text: orig.slice(offset), start: startChar };
      speakChain(chunkIdx);
      ch[chunkIdx] = { text: orig, start: startChar - offset };
    } else {
      speakChain(chunkIdx);
    }
  }

  const CHARS_PER_10S = Math.floor(125 * TTS_RATE_PLACEHOLDER);

  function togglePlay() {
    iosWarmup();
    if (isPaused) { isPaused = false; speakFrom(charIndex); setStatus("Resumed..."); }
    else if (window.speechSynthesis.speaking) { isPaused = true; window.speechSynthesis.cancel(); setPlayBtn(false); setStatus("Paused \u2014 press Play to resume"); }
    else { charIndex = 0; isPaused = false; speakFrom(0); }
  }

  function skipForward() {
    const was = window.speechSynthesis.speaking && !isPaused;
    window.speechSynthesis.cancel();
    charIndex = Math.min(charIndex + CHARS_PER_10S, ttsText.length - 1);
    if (was) { isPaused = false; speakFrom(charIndex); setStatus("Skipped forward 10s..."); }
    else setStatus("Skipped forward \u2014 press Play to resume");
  }

  function skipBack() {
    const was = window.speechSynthesis.speaking && !isPaused;
    window.speechSynthesis.cancel();
    charIndex = Math.max(0, charIndex - CHARS_PER_10S);
    if (was) { isPaused = false; speakFrom(charIndex); setStatus("Skipped back 10s..."); }
    else setStatus("Skipped back \u2014 press Play to resume");
  }

  function stopReading() {
    isPaused = false; charIndex = 0; window.speechSynthesis.cancel();
    setPlayBtn(false); setStatus("Stopped \u2014 press Replay to start over");
  }

  function replayReading() {
    isPaused = false; charIndex = 0; window.speechSynthesis.cancel();
    setStatus("Restarting..."); setTimeout(() => speakFrom(0), 300);
  }

  function playChime() {
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      [523.25, 659.25, 783.99].forEach((freq, i) => {
        const osc = ctx.createOscillator(), gain = ctx.createGain();
        osc.type = "sine"; osc.frequency.value = freq;
        gain.gain.setValueAtTime(0.3, ctx.currentTime + i * 0.15);
        gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + i * 0.15 + 0.5);
        osc.connect(gain); gain.connect(ctx.destination);
        osc.start(ctx.currentTime + i * 0.15); osc.stop(ctx.currentTime + i * 0.15 + 0.5);
      });
    } catch(e) {}
  }

  function updateNotifyBtn() {
    const btn = document.getElementById("notify-btn");
    if (!btn) return;
    if (Notification.permission === "granted") { btn.textContent = "Notifications on"; btn.classList.add("enabled"); }
    else if (Notification.permission === "denied") { btn.textContent = "Notifications blocked"; }
    else { btn.textContent = "Notify me"; btn.classList.remove("enabled"); }
  }

  function requestNotifications() {
    if (!("Notification" in window)) { setStatus("Notifications not supported"); return; }
    if (Notification.permission === "granted") { setStatus("Notifications already enabled!"); return; }
    Notification.requestPermission().then(p => {
      updateNotifyBtn();
      if (p === "granted") {
        setStatus("Notifications enabled");
        new Notification("The Victory Lane", { body: "You'll be notified when new dispatches arrive." });
      }
    });
  }

  window.addEventListener("load", () => {
    updateNotifyBtn();
    setStatus("Ready — press Play to start");
  });
  window.addEventListener("pagehide", () => window.speechSynthesis.cancel());
  window.addEventListener("visibilitychange", () => { if (document.hidden) { window.speechSynthesis.cancel(); setPlayBtn(false); } });
"""


def tts_bar_html():
    return """<div id="tts-bar">
  <button id="btn-play" onclick="togglePlay()">&#9654; Play</button>
  <button class="skip" onclick="skipBack()">&#8592; 10s</button>
  <button class="skip" onclick="skipForward()">10s &#8594;</button>
  <button onclick="stopReading()">&#9632; Stop</button>
  <button onclick="replayReading()">&#8635; Replay</button>
  <div class="speed-group">
    <label>Speed</label>
    <input type="range" id="speed-slider" min="0.5" max="2.0" step="0.1" value="1.3" oninput="updateSpeed(this.value)">
    <span id="speed-val">1.3x</span>
  </div>
  <span id="tts-status">Ready &mdash; press Play to start</span>
  <button class="notify-btn" id="notify-btn" onclick="requestNotifications()">Notify me</button>
</div>"""


COMMON_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: Georgia, 'Times New Roman', serif;
  background: #0a0a0a;
  color: #e8e4d9;
  max-width: 1100px;
  margin: 0 auto;
  padding: 32px 28px 100px;
  line-height: 1.72;
  font-size: 15px;
}
a { color: inherit; text-decoration: none; }
.top-bar {
  position: sticky; top: 0; background: #0a0a0a; z-index: 100;
  display: flex; align-items: center; justify-content: space-between;
  padding: 10px 0; margin-bottom: 22px;
  border-bottom: 1px solid #1a1a1a;
}
.back-link { font-size: 12px; color: #555; font-family: 'Courier New', monospace; }
.back-link:hover { color: #c9b97a; }
.site-name { font-size: 11px; color: #333; font-family: 'Courier New', monospace; letter-spacing: 0.12em; }
.section-head {
  font-size: 10px; font-weight: normal; letter-spacing: 0.16em;
  text-transform: uppercase; color: #555;
  margin: 28px 0 10px; padding-bottom: 5px; border-bottom: 1px solid #181818;
}
p { margin-bottom: 11px; color: #c8c4b8; }
strong { color: #c9b97a; }
#tts-bar {
  position: fixed; bottom: 0; left: 0; right: 0;
  background: #0e0e0e; border-top: 1px solid #1e1e1e;
  padding: 7px 14px;
  display: flex; align-items: center; gap: 7px;
  font-family: 'Courier New', monospace; font-size: 11px; flex-wrap: wrap;
}
#tts-bar button {
  background: #181818; border: 1px solid #2a2a2a; color: #d8d4c8;
  padding: 4px 9px; cursor: pointer; font-size: 11px; border-radius: 3px;
  font-family: 'Courier New', monospace; transition: background 0.15s; white-space: nowrap;
}
#tts-bar button:hover { background: #242424; }
#tts-bar button.active { background: #3d3820; border-color: #c9b97a; color: #c9b97a; }
#tts-bar button.skip { color: #666; }
.speed-group { display: flex; align-items: center; gap: 5px; }
.speed-group label { color: #555; font-size: 11px; }
#speed-slider { width: 72px; accent-color: #c9b97a; }
#speed-val { color: #c9b97a; font-size: 11px; min-width: 26px; }
#tts-status { color: #555; flex: 1; font-size: 11px; min-width: 100px; }
.notify-btn { margin-left: auto; background: #1a1a2a !important; border-color: #334 !important; color: #668 !important; }
.notify-btn.enabled { color: #4c8faf !important; border-color: #4c8faf44 !important; background: #1a2a3a !important; }
"""


# ─────────────────────────────────────────────
# MGP DASHBOARD HTML
# ─────────────────────────────────────────────

def _dir_arrow(direction):
    if direction == "bullish":  return '<span style="color:#4caf82">▲</span>'
    if direction == "bearish":  return '<span style="color:#e05050">▼</span>'
    return '<span style="color:#888">◆</span>'


def build_mgp_dashboard(vk_data, scanner_data, news_data, today_str, tts_rate, archive_entries=None):
    """
    Build the main MGP index.html dashboard — 2-column layout:
      LEFT  : Recent dispatch link cards (VK + EW + HS)
      RIGHT : Macro context (top) + Trade Ideas scanners w/ Benzinga (bottom)

    vk_data        : dict from parse_vk_to_mgp (or None)
    scanner_data   : dict from read_scanner_csvs
    news_data      : dict from fetch_benzinga_news
    archive_entries: list of recent dispatch dicts from digests.json
    """
    archive_entries = archive_entries or []

    # ── TTS text (built from VK + scanner tickers) ──
    tts_text = ""
    if vk_data:
        tts_text = prepare_tts_text(vk_data.get("tts_summary",
                                    vk_data.get("macro", "") + " " + vk_data.get("market_outlook", "")))

    # ── LEFT PANEL: Dispatch link cards ──
    left_cards_html = ""
    for entry in archive_entries[:20]:
        filename   = entry.get("filename", "#")
        subject    = entry.get("subject", "Dispatch")
        email_date = entry.get("email_date", "")
        preview    = entry.get("preview", "")
        source_type= entry.get("source_type", "")
        tag        = get_category_tag(subject)
        bg, fg     = get_tag_color(tag)
        src_label  = {"vk": "VK", "ew": "Earnings Whispers", "hs": "Hammerstone"}.get(source_type, "")
        preview_snip = (preview[:110] + "…") if len(preview) > 110 else preview
        left_cards_html += f"""<a class="dispatch-card" href="{filename}">
  <div class="dispatch-header">
    <span class="dispatch-tag" style="color:{fg};border-color:{fg}44;background:{bg};">{tag}</span>
    {"<span class='dispatch-src'>" + src_label + "</span>" if src_label else ""}
    <span class="dispatch-date">{email_date}</span>
  </div>
  <div class="dispatch-title">{subject}</div>
  {"<div class='dispatch-preview'>" + preview_snip + "</div>" if preview_snip else ""}
</a>"""

    if not left_cards_html:
        left_cards_html = '<p class="empty-msg">No dispatches yet — check back after the market open.</p>'

    # ── RIGHT TOP: Macro / Outlook / Calendar ──
    rt_parts = []
    if vk_data:
        macro    = vk_data.get("macro", "")
        outlook  = vk_data.get("market_outlook", "")
        sectors  = vk_data.get("sectors", "")
        earnings = vk_data.get("earnings_today", [])
        key_dates= vk_data.get("key_dates", [])

        if macro:
            rt_parts.append(f"""<div class="section-head">Macro</div>
<div class="prose-block"><p>{macro}</p></div>""")

        if outlook:
            rt_parts.append(f"""<div class="section-head">Market Outlook</div>
<div class="prose-block"><p>{outlook}</p></div>""")

        if sectors:
            rt_parts.append(f"""<div class="section-head">Sector Watch</div>
<div class="prose-block"><p>{sectors}</p></div>""")

        cal_parts = []
        if earnings:
            cal_parts.append('<div class="cal-group"><div class="cal-label">EARNINGS TODAY</div>'
                             + "".join(f'<div class="cal-item">{e}</div>' for e in earnings) + "</div>")
        if key_dates:
            cal_parts.append('<div class="cal-group"><div class="cal-label">KEY DATES</div>'
                             + "".join(f'<div class="cal-item">{d}</div>' for d in key_dates) + "</div>")
        if cal_parts:
            rt_parts.append(f"""<div class="section-head">Calendar</div>
<div class="cal-row">{"".join(cal_parts)}</div>""")

    right_top_body = "\n".join(rt_parts) if rt_parts else '<p class="empty-msg">Macro data will appear after VK is parsed.</p>'

    # ── RIGHT BOTTOM: Scanners + Benzinga ──
    # Apply display order; unlisted scanners appended at end
    sd = scanner_data or {}
    ordered_labels   = [lbl for lbl in SCANNER_DISPLAY_ORDER if lbl in sd]
    remaining_labels = [lbl for lbl in sd if lbl not in SCANNER_DISPLAY_ORDER]
    display_labels   = ordered_labels + remaining_labels

    rb_blocks = []
    for scanner_label in display_labels:
        rows = sd[scanner_label]
        tickers_in_scanner = [r["symbol"] for r in rows]
        if tickers_in_scanner:
            tts_text += f" Scanner alert: {scanner_label}. Tickers: {', '.join(tickers_in_scanner)}."

        rows_html = ""
        for r in rows:
            sym     = r["symbol"]
            price   = f"${r['price']:.2f}"        if r["price"]     is not None else "—"
            chg     = f"{r['chg_close']:+.1f}%"   if r["chg_close"] is not None else "—"
            rel_vol = f"{r['rel_vol']:.1f}x"      if r.get("rel_vol") is not None else "—"
            chg_cls = "sc-up" if (r["chg_close"] or 0) >= 0 else "sc-dn"

            bz_html = ""
            if sym in news_data and news_data[sym]:
                for n in news_data[sym][:3]:
                    beat_badge = f'<span class="bz-beat">{n["beat"]}</span> ' if n.get("beat") else ""
                    ch = n.get("channels", [])
                    if "Earnings" in ch:
                        ch_badge = '<span class="bz-tag">EARNINGS</span> '
                    elif "Guidance" in ch:
                        ch_badge = '<span class="bz-tag bz-guidance">GUIDANCE</span> '
                    else:
                        ch_badge = ""
                    bz_html += f'<div class="scanner-news">{ch_badge}{beat_badge}{n["title"]}</div>'

            rows_html += f"""<div class="scanner-row">
  <span class="sc-sym">{sym}</span>
  <span class="sc-rvol">{rel_vol}</span>
  <span class="sc-chg {chg_cls}">{chg}</span>
  <span class="sc-price">{price}</span>
  {bz_html}
</div>"""

        rb_blocks.append(f"""<div class="scanner-block">
  <div class="scanner-name">{scanner_label}</div>
  {rows_html}
</div>""")

    right_bot_body = ("\n".join(rb_blocks)
                      if rb_blocks
                      else '<p class="empty-msg">Scanner CSVs will appear here once Trade Ideas is running.</p>')

    # ── Assemble ──
    tts_escaped = (tts_text
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", " ")
        .replace("`", "'"))

    updated = datetime.now(EASTERN).strftime("%b %d, %Y · %I:%M %p ET")
    js = TTS_JS.replace("TTS_RATE_PLACEHOLDER", str(tts_rate))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Morning Game Plan · {today_str} · The Victory Lane</title>
<meta http-equiv="refresh" content="300">
<style>
{COMMON_CSS}
/* ── MGP layout ── */
header {{ margin-bottom: 20px; }}
.mgp-title {{ font-size: 28px; font-weight: normal; color: #f0ece0; letter-spacing: 0.02em; }}
.mgp-date  {{ font-size: 12px; color: #444; font-family: 'Courier New', monospace; margin-top: 5px; }}

.mgp-grid {{
  display: grid;
  grid-template-columns: 48% 1fr;
  grid-template-rows: auto auto;
  gap: 16px;
  align-items: start;
}}
@media (max-width: 900px) {{ .mgp-grid {{ grid-template-columns: 1fr; }} }}

/* LEFT: dispatch link list */
.panel-left {{
  grid-row: 1 / 3;
  border: 1px solid #1e1a14;
  border-radius: 6px;
  padding: 16px 18px;
  background: #0c0a08;
}}
.panel-label {{
  font-size: 10px; font-family: 'Courier New', monospace; letter-spacing: 0.18em;
  color: #555; margin-bottom: 4px; text-transform: uppercase;
}}

/* dispatch cards */
.dispatch-card {{
  display: block; border-bottom: 1px solid #161410; padding: 13px 0;
  text-decoration: none; color: inherit; transition: padding-left 0.12s;
}}
.dispatch-card:hover {{ padding-left: 6px; }}
.dispatch-card:hover .dispatch-title {{ color: #c9b97a; }}
.dispatch-card:first-of-type {{ border-top: 1px solid #161410; margin-top: 8px; }}
.dispatch-header {{
  display: flex; align-items: center; gap: 7px; margin-bottom: 4px; flex-wrap: wrap;
}}
.dispatch-tag {{
  font-size: 9px; font-family: 'Courier New', monospace; font-weight: bold;
  letter-spacing: 0.1em; padding: 2px 5px; border-radius: 2px; border: 1px solid;
}}
.dispatch-src  {{ font-size: 10px; color: #444; font-family: 'Courier New', monospace; }}
.dispatch-date {{ font-size: 10px; color: #3a3830; font-family: 'Courier New', monospace; margin-left: auto; }}
.dispatch-title {{ font-size: 13px; color: #b8b4a8; line-height: 1.35; margin-bottom: 3px; transition: color 0.12s; }}
.dispatch-preview {{ font-size: 11px; color: #484440; line-height: 1.5; }}

/* RIGHT TOP: macro */
.panel-macro {{
  border: 1px solid #1a3a22;
  border-radius: 6px;
  padding: 16px 18px;
  background: #080f0a;
}}
/* RIGHT BOTTOM: scanners */
.panel-scanner {{
  border: 1px solid #1a2a3a;
  border-radius: 6px;
  padding: 16px 18px;
  background: #08090f;
}}

/* macro */
.prose-block {{ margin-bottom: 8px; }}

/* calendar */
.cal-row {{ display: flex; gap: 24px; flex-wrap: wrap; margin-bottom: 6px; }}
.cal-group {{ display: flex; flex-direction: column; gap: 3px; }}
.cal-label {{ font-size: 10px; color: #555; font-family: 'Courier New', monospace; letter-spacing: 0.1em; margin-bottom: 3px; }}
.cal-item {{ font-size: 12px; color: #a8a49a; }}

/* scanner */
.scanner-block {{ background: #0a0c10; border: 1px solid #1a1f2a; border-radius: 4px; padding: 10px 12px; margin-bottom: 10px; }}
.scanner-block:last-child {{ margin-bottom: 0; }}
.scanner-name {{ font-size: 10px; color: #4c8faf; font-family: 'Courier New', monospace; letter-spacing: 0.12em; margin-bottom: 8px; text-transform: uppercase; }}
.scanner-row {{ display: flex; align-items: baseline; gap: 10px; padding: 5px 0; border-bottom: 1px solid #121418; flex-wrap: wrap; }}
.scanner-row:last-child {{ border-bottom: none; }}
.sc-sym   {{ font-size: 14px; color: #c9b97a; font-family: 'Courier New', monospace; min-width: 62px; font-weight: bold; }}
.sc-rvol  {{ font-size: 12px; color: #4c8faf; min-width: 40px; }}
.sc-chg   {{ font-size: 12px; min-width: 52px; font-family: 'Courier New', monospace; }}
.sc-up    {{ color: #4caf82; }}
.sc-dn    {{ color: #e05050; }}
.sc-price {{ font-size: 12px; color: #888; }}
.scanner-news {{ width: 100%; font-size: 11px; color: #4a6070; font-family: 'Courier New', monospace;
  padding-top: 3px; margin-top: 2px; border-top: 1px dashed #1a2030; }}
.bz-tag {{ font-size: 9px; font-family: 'Courier New', monospace; font-weight: bold;
  letter-spacing: 0.08em; padding: 1px 4px; border-radius: 2px;
  background: #1a2a1a; color: #4caf82; border: 1px solid #4caf8244; }}
.bz-tag.bz-guidance {{ background: #2a2a1a; color: #c9b97a; border-color: #c9b97a44; }}
.bz-beat {{ font-size: 10px; color: #c9b97a; font-family: 'Courier New', monospace; }}

.empty-msg {{ font-size: 12px; color: #333; font-family: 'Courier New', monospace; font-style: italic; }}
</style>
</head>
<body>

<div class="top-bar">
  <a class="back-link" href="archive.html">&#8592; Archive</a>
  <span class="site-name">THE VICTORY LANE</span>
</div>

<header>
  <div class="mgp-title">Morning Game Plan</div>
  <div class="mgp-date">{today_str} &nbsp;·&nbsp; Updated {updated}</div>
</header>

<div class="mgp-grid">

  <!-- LEFT: Dispatch link list -->
  <div class="panel-left">
    <div class="panel-label">Dispatches</div>
    {left_cards_html}
  </div>

  <!-- RIGHT TOP: Macro / Outlook / Calendar -->
  <div class="panel-macro">
    <div class="panel-label">Macro · Outlook · Key Names</div>
    {right_top_body}
  </div>

  <!-- RIGHT BOTTOM: Scanners + Benzinga -->
  <div class="panel-scanner">
    <div class="panel-label">Scanners · News</div>
    {right_bot_body}
  </div>

</div>

{tts_bar_html()}

<script>
  const ttsText = "{tts_escaped}";
  {js}
</script>

</body>
</html>"""


# ─────────────────────────────────────────────
# INDIVIDUAL DIGEST PAGE (non-VK emails)
# ─────────────────────────────────────────────

def markdown_to_html_body(text):
    lines = text.split("\n")
    parts = []
    for line in lines:
        line = line.strip()
        if not line: continue
        if line.startswith("## "):
            parts.append(f'<div class="section-head">{line[3:]}</div>')
        else:
            line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
            parts.append(f"<p>{line}</p>")
    return "\n".join(parts)


def build_digest_html(digest_text, subject, email_date, tts_rate):
    body_html = markdown_to_html_body(digest_text)
    tts_text = prepare_tts_text(re.sub(r"<[^>]+>", " ", body_html))
    tts_escaped = tts_text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("`", "'")
    tag = get_category_tag(subject)
    bg, fg = get_tag_color(tag)
    js = TTS_JS.replace("TTS_RATE_PLACEHOLDER", str(tts_rate))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{subject} · The Victory Lane</title>
<style>
{COMMON_CSS}
.tag {{ display: inline-block; font-size: 11px; font-family: 'Courier New', monospace; font-weight: bold;
  letter-spacing: 0.1em; padding: 3px 8px; border-radius: 3px; margin-bottom: 10px;
  background: {bg}; color: {fg}; border: 1px solid {fg}44; }}
header h1 {{ font-size: 24px; font-weight: normal; color: #c9b97a; margin-bottom: 7px; }}
header .meta {{ font-size: 12px; color: #555; font-family: 'Courier New', monospace; }}
hr.div {{ border: none; border-top: 1px solid #181818; margin: 18px 0 24px; }}
</style>
</head>
<body>
<div class="top-bar">
  <a class="back-link" href="index.html">&#8592; Game Plan</a>
  <span class="site-name">THE VICTORY LANE</span>
</div>
<header>
  <div class="tag">{tag}</div>
  <h1>{subject}</h1>
  <div class="meta">{email_date}</div>
</header>
<hr class="div">
{body_html}
{tts_bar_html()}
<script>
  const ttsText = "{tts_escaped}";
  {js}
</script>
</body>
</html>"""


# ─────────────────────────────────────────────
# VK STRUCTURED DIGEST PAGE
# ─────────────────────────────────────────────

def build_vk_digest_html(parsed_data, subject, email_date, tts_rate):
    """
    Build a rich structured digest page for a VK email.
    Shows macro, market outlook, sectors, calendar, and company cards.
    """
    macro    = parsed_data.get("macro", "")
    outlook  = parsed_data.get("market_outlook", "")
    sectors  = parsed_data.get("sectors", "")
    earnings = parsed_data.get("earnings_today", [])
    key_dates= parsed_data.get("key_dates", [])
    items    = parsed_data.get("company_items", [])
    tts_sum  = parsed_data.get("tts_summary", "")

    sections = []

    if macro:
        sections.append(
            '<div class="section-head">Macro</div>'
            f'<div class="prose-block"><p>{macro}</p></div>'
        )

    if outlook:
        sections.append(
            '<div class="section-head">Market Outlook</div>'
            f'<div class="prose-block"><p>{outlook}</p></div>'
        )

    if sectors:
        sections.append(
            '<div class="section-head">Sector Watch</div>'
            f'<div class="prose-block"><p>{sectors}</p></div>'
        )

    cal_parts = []
    if earnings:
        cal_parts.append(
            '<div class="cal-group"><div class="cal-label">EARNINGS TODAY</div>'
            + "".join(f'<div class="cal-item">{e}</div>' for e in earnings)
            + "</div>"
        )
    if key_dates:
        cal_parts.append(
            '<div class="cal-group"><div class="cal-label">KEY DATES</div>'
            + "".join(f'<div class="cal-item">{d}</div>' for d in key_dates)
            + "</div>"
        )
    if cal_parts:
        sections.append(
            '<div class="section-head">Calendar</div>'
            f'<div class="cal-row">{"".join(cal_parts)}</div>'
        )

    if items:
        sections.append('<div class="section-head">Company Coverage</div>')
        cards_html = ""
        for item in items:
            ticker    = item.get("ticker", "")
            company   = item.get("company", "")
            summary   = item.get("summary", "")
            catalyst  = item.get("catalyst", "")
            direction = item.get("direction", "mixed")
            arrow     = _dir_arrow(direction)
            cards_html += f"""<div class="company-card">
  <div class="card-header">
    <span class="card-ticker">{arrow} {ticker}</span>
    <span class="card-company">{company}</span>
    <span class="card-catalyst">{catalyst}</span>
  </div>
  <p class="card-summary">{summary}</p>
</div>
"""
        sections.append(cards_html)

    body_html  = "\n".join(sections)
    tts_text   = prepare_tts_text(tts_sum or (macro + " " + outlook))
    tts_escaped = tts_text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("`", "'")
    tag         = get_category_tag(subject)
    bg, fg      = get_tag_color(tag)
    js          = TTS_JS.replace("TTS_RATE_PLACEHOLDER", str(tts_rate))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{subject} · The Victory Lane</title>
<style>
{COMMON_CSS}
.tag {{ display: inline-block; font-size: 11px; font-family: 'Courier New', monospace; font-weight: bold;
  letter-spacing: 0.1em; padding: 3px 8px; border-radius: 3px; margin-bottom: 10px;
  background: {bg}; color: {fg}; border: 1px solid {fg}44; }}
header h1 {{ font-size: 24px; font-weight: normal; color: #c9b97a; margin-bottom: 7px; }}
header .meta {{ font-size: 12px; color: #555; font-family: 'Courier New', monospace; }}
hr.div {{ border: none; border-top: 1px solid #181818; margin: 18px 0 24px; }}
.prose-block {{ margin-bottom: 8px; }}
.cal-row {{ display: flex; gap: 24px; flex-wrap: wrap; margin-bottom: 6px; }}
.cal-group {{ display: flex; flex-direction: column; gap: 3px; }}
.cal-label {{ font-size: 10px; color: #555; font-family: 'Courier New', monospace; letter-spacing: 0.1em; margin-bottom: 3px; }}
.cal-item {{ font-size: 12px; color: #a8a49a; }}
.company-card {{ background: #111; border: 1px solid #1e1e1e; border-radius: 4px;
  padding: 12px 14px; margin-bottom: 10px; }}
.card-header {{ display: flex; align-items: baseline; gap: 8px; margin-bottom: 6px; flex-wrap: wrap; }}
.card-ticker {{ font-size: 16px; color: #c9b97a; font-family: 'Courier New', monospace; font-weight: bold; }}
.card-company {{ font-size: 12px; color: #555; flex: 1; }}
.card-catalyst {{ font-size: 10px; color: #666; font-family: 'Courier New', monospace;
  background: #1a1a1a; border: 1px solid #2a2a2a; padding: 2px 6px; border-radius: 2px; white-space: nowrap; }}
.card-summary {{ font-size: 13px; color: #b8b4a8; margin-bottom: 0; }}
</style>
</head>
<body>
<div class="top-bar">
  <a class="back-link" href="index.html">&#8592; Game Plan</a>
  <span class="site-name">THE VICTORY LANE</span>
</div>
<header>
  <div class="tag">{tag}</div>
  <h1>{subject}</h1>
  <div class="meta">{email_date}</div>
</header>
<hr class="div">
{body_html}
{tts_bar_html()}
<script>
  const ttsText = "{tts_escaped}";
  {js}
</script>
</body>
</html>"""


# ─────────────────────────────────────────────
# ARCHIVE INDEX
# ─────────────────────────────────────────────

def build_archive_html(digests):
    cards = ""
    for i, entry in enumerate(digests):
        tag = get_category_tag(entry["subject"])
        bg, fg = get_tag_color(tag)
        latest = (' <span style="font-size:10px; background:#3d3820; color:#c9b97a; border:1px solid #c9b97a44; '
                  'padding:2px 6px; border-radius:3px; font-family:\'Courier New\',monospace; '
                  'vertical-align:middle; margin-left:6px;">LATEST</span>') if i == 0 else ""
        preview = entry.get("preview", "")
        cards += f"""<a class="card" href="{entry['filename']}">
  <div class="card-tag" style="background:{bg}; color:{fg}; border-color:{fg}44;">{tag}</div>
  <div class="card-meta">{entry['email_date']}</div>
  <div class="card-title">{entry['subject']}{latest}</div>
  {"<div class='card-preview'>" + preview + "</div>" if preview else ""}
</a>"""

    updated = datetime.now(EASTERN).strftime("%b %d, %Y %I:%M %p ET")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Archive · The Victory Lane</title>
<style>
{COMMON_CSS}
.site-header {{ position: sticky; top: 0; background: #0a0a0a; z-index: 100;
  display: flex; align-items: center; justify-content: space-between;
  border-bottom: 1px solid #1a1a1a; padding: 14px 0; margin-bottom: 28px; }}
.site-title {{ font-size: 20px; color: #f0ece0; }}
.site-updated {{ font-size: 11px; color: #444; font-family: 'Courier New', monospace; }}
.card {{ display: block; border-bottom: 1px solid #141414; padding: 18px 0; transition: padding-left 0.15s; }}
.card:hover {{ padding-left: 8px; }}
.card:hover .card-title {{ color: #c9b97a; }}
.card:first-of-type {{ border-top: 1px solid #141414; margin-top: 6px; }}
.card-tag {{ display: inline-block; font-size: 10px; font-family: 'Courier New', monospace; font-weight: bold;
  letter-spacing: 0.1em; padding: 2px 7px; border-radius: 3px; border: 1px solid; margin-bottom: 5px; }}
.card-meta {{ font-size: 12px; color: #444; font-family: 'Courier New', monospace; margin-bottom: 5px; }}
.card-title {{ font-size: 17px; color: #d8d4c8; line-height: 1.4; transition: color 0.15s; margin-bottom: 4px; }}
.card-preview {{ font-size: 13px; color: #555; line-height: 1.5; }}
</style>
</head>
<body>
<div class="site-header">
  <div>
    <div class="site-title">THE VICTORY LANE</div>
    <div style="font-size:11px; color:#555; font-family:'Courier New',monospace;">DISPATCH ARCHIVE</div>
  </div>
  <div class="site-updated">Updated {updated}</div>
</div>
{cards}
</body>
</html>"""


# ─────────────────────────────────────────────
# FILE I/O & GITHUB
# ─────────────────────────────────────────────

def save_to_docs(html, filename):
    os.makedirs("docs", exist_ok=True)
    filepath = f"docs/{filename}"
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  ✓ Saved: {filepath}")
    return filename


def update_archive(new_entries):
    meta_path = "docs/digests.json"
    existing = []
    if os.path.exists(meta_path):
        with open(meta_path, encoding="utf-8") as f:
            existing = json.load(f)
    existing_filenames = {e["filename"] for e in existing}
    for entry_tuple in new_entries:
        filename, subject, email_date, preview, sent_ts = entry_tuple[:5]
        source_type = entry_tuple[5] if len(entry_tuple) > 5 else ""
        if filename not in existing_filenames:
            existing.append({"filename": filename, "subject": subject,
                             "email_date": email_date, "preview": preview,
                             "sent_ts": sent_ts, "source_type": source_type})
    existing.sort(key=lambda x: x.get("sent_ts", x["filename"]), reverse=True)
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)
    archive_html = build_archive_html(existing)
    with open("docs/archive.html", "w", encoding="utf-8") as f:
        f.write(archive_html)
    print(f"  ✓ Archive updated — {len(existing)} dispatch(es)")


def launch_edge(html_path, edge_exe):
    if not os.path.exists(edge_exe):
        edge_exe = r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"
    if os.path.exists(edge_exe):
        subprocess.Popen([edge_exe, html_path])
        print("  ✓ Launched Edge")
    else:
        try:
            os.startfile(html_path)
        except Exception:
            pass


# ─────────────────────────────────────────────
# GIT PUSH
# ─────────────────────────────────────────────

def git_commit_push():
    """Stage docs/, commit, and push — only if there are actual changes.
    Skipped in GitHub Actions — the workflow handles the push as its final step."""
    if os.environ.get("GITHUB_ACTIONS", "false").lower() == "true":
        print("  Git: running in GitHub Actions — push handled by workflow")
        return
    try:
        # Check for changes in docs/
        status = subprocess.run(
            ["git", "status", "--porcelain", "docs/"],
            capture_output=True, text=True
        )
        if not status.stdout.strip():
            print("  Git: no changes to push")
            return
        subprocess.run(["git", "add", "docs/"], check=True)
        msg = f"MGP update {datetime.now(EASTERN).strftime('%H:%M ET')}"
        subprocess.run(
            ["git", "commit", "-m", msg,
             "--author", "Claude <noreply@anthropic.com>"],
            check=True
        )
        subprocess.run(["git", "push"], check=True)
        print("  ✓ Git: pushed to GitHub")
    except subprocess.CalledProcessError as e:
        print(f"  ✗ Git push failed: {e}")
    except Exception as e:
        print(f"  ✗ Git error: {e}")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def run():
    now_et = datetime.now(EASTERN)
    today_str = now_et.strftime("%A, %B %d, %Y")
    print(f"[{now_et.strftime('%H:%M:%S ET')}] The Victory Lane — Morning Game Plan starting...")
    config = CONFIG

    # ── 1. Read scanner CSVs ──
    scanner_data = {}
    if not config["github_actions"]:
        # Only available locally; GitHub Actions can't read C:\Users\jerem\...
        scanner_data = read_scanner_csvs(config["scanner_csv_dir"])
    else:
        # In GitHub Actions, look for CSVs committed to repo under scanners/
        scanner_data = read_scanner_csvs("scanners")

    # Collect all scanner tickers for news fetch
    scanner_tickers = []
    for rows in scanner_data.values():
        for r in rows:
            if r["symbol"] not in scanner_tickers:
                scanner_tickers.append(r["symbol"])

    # ── 2. Fetch Benzinga news for scanner tickers (with daily cache) ──
    news_data = {}
    if scanner_tickers:
        bz_cache = load_benzinga_cache()
        new_tickers = [t for t in scanner_tickers if t not in bz_cache]
        if new_tickers:
            print(f"  Benzinga: fetching {len(new_tickers)} new ticker(s) ({len(bz_cache)} served from cache)")
            fresh = fetch_benzinga_news(new_tickers, config["benzinga_api_key"])
            bz_cache.update(fresh)
            save_benzinga_cache(bz_cache)
        else:
            print(f"  Benzinga: all {len(scanner_tickers)} ticker(s) served from cache")
        news_data = {t: bz_cache[t] for t in scanner_tickers if t in bz_cache}

    # ── 3. Fetch and process emails (single IMAP connection) ──
    emails, processed_ids, latest_vk_body, latest_vk_subject = fetch_new_emails(config)

    # Pre-load VK cache so subject comparison works on the first 5-min run after an email arrives
    vk_data  = None
    vk_cache_path = "docs/last_vk_cache.json"
    if os.path.exists(vk_cache_path):
        try:
            with open(vk_cache_path, encoding="utf-8") as f:
                vk_data = json.load(f)
            print(f"  VK cache loaded (email_type={vk_data.get('email_type','?')})")
        except Exception as e:
            print(f"  VK cache load error: {e}")

    new_ids  = set()
    new_archive_entries = []

    # ── Parse the latest VK email for the dashboard panels (cached by subject) ──
    print("\n  Parsing latest VK email for dashboard...")
    if latest_vk_body:
        cached_subject = vk_data.get("_cached_subject") if vk_data else None
        if cached_subject and cached_subject == latest_vk_subject:
            print(f"  VK: cache hit — '{latest_vk_subject[:70]}' already parsed, skipping API call")
        else:
            print(f"  Parsing: {latest_vk_subject}")
            parsed = parse_vk_to_mgp(latest_vk_body, config["anthropic_api_key"])
            if parsed:
                parsed["_cached_subject"] = latest_vk_subject
                vk_data = parsed
                print(f"  vk_data parsed OK — email_type={vk_data.get('email_type','?')}")
            else:
                print("  parse_vk_to_mgp returned None — will use existing cache")
    else:
        print("  No recent VK email found in lookback window")

    for uid, subject, body, email_date, sent_utc, source_type in emails:
        print(f"\n  ── Processing: {subject} ({email_date}) ──")
        try:
            slug = re.sub(r"[^a-z0-9]+", "-", subject.lower()).strip("-")[:80]
            expected_file = f"docs/{slug}.html"
            if os.path.exists(expected_file):
                print(f"  ✓ Already exists, skipping: {expected_file}")
                new_ids.add(uid)
                continue

            # CF grading on all email types
            print("  Grading for Changing Fundamentals...")
            cf_items = grade_for_cf(body, subject, config["anthropic_api_key"])
            if cf_items:
                target_date = sent_utc.astimezone(EASTERN).strftime("%Y-%m-%d")
                push_to_notion_stocks_on_watch(cf_items, config["notion_token"],
                                               config["stocks_on_watch_db_id"], target_date=target_date)
            else:
                print("  CF grading: nothing flagged above threshold")

            if source_type == "vk":
                # Structured VK parse → rich digest page with company cards + macro sections
                print("  Parsing VK for structured digest page...")
                parsed = parse_vk_to_mgp(body, config["anthropic_api_key"])
                if parsed:
                    # Dawn email takes precedence for the dashboard macro panel
                    if vk_data is None or parsed.get("email_type") == "dawn":
                        vk_data = parsed
                    digest_html = build_vk_digest_html(parsed, subject, email_date, config["tts_rate"])
                    # Build preview from tts_summary or company items
                    tts_sum = parsed.get("tts_summary", "")
                    preview_text = tts_sum if tts_sum else " · ".join(
                        item.get("ticker", "") for item in parsed.get("company_items", [])[:6]
                    )
                else:
                    # VK parse failed — fall back to generic digest from raw body
                    print("  VK structured parse failed, falling back to generic digest")
                    digest_html  = build_digest_html(body[:3000], subject, email_date, config["tts_rate"])
                    preview_text = body[:140]

            else:
                # EW or HS: generic summarizer
                print(f"  Summarizing [{source_type}] with Claude...")
                archive_text = summarize_generic(body, config["anthropic_api_key"], source_type)
                digest_html  = build_digest_html(archive_text, subject, email_date, config["tts_rate"])
                preview_text = archive_text

            # Save individual digest page
            filename = save_to_docs(digest_html, f"{slug}.html")
            preview  = preview_text[:140].replace("\n", " ") + ("…" if len(preview_text) > 140 else "")
            new_archive_entries.append((filename, subject, email_date, preview, sent_utc.isoformat(), source_type))

            new_ids.add(uid)
            save_processed_ids(processed_ids | new_ids)

        except Exception as e:
            print(f"  ✗ Error processing '{subject}': {e}")
            raise

    # ── 4. Persist VK cache if we have fresh data ──
    if vk_data and vk_data.get("_cached_subject"):
        os.makedirs("docs", exist_ok=True)
        with open(vk_cache_path, "w", encoding="utf-8") as f:
            json.dump(vk_data, f, ensure_ascii=False)
        print("  ✓ VK cache updated")
    elif not vk_data:
        print("  No VK data available — dashboard will show placeholders")

    # ── 5. Update archive ──
    if new_archive_entries:
        update_archive(new_archive_entries)

    # ── 6. Load recent dispatches for left panel link list ──
    archive_entries = []
    meta_path = "docs/digests.json"
    if os.path.exists(meta_path):
        try:
            with open(meta_path, encoding="utf-8") as f:
                archive_entries = json.load(f)[:20]
        except Exception as e:
            print(f"  ✗ Could not load digests.json for left panel: {e}")

    # ── 7. Build and save MGP dashboard (always rebuild index.html) ──
    print("\n  Building MGP dashboard...")
    dashboard_html = build_mgp_dashboard(
        vk_data, scanner_data, news_data, today_str, config["tts_rate"],
        archive_entries=archive_entries
    )
    save_to_docs(dashboard_html, "index.html")

    save_processed_ids(processed_ids | new_ids)

    # ── 8. Local: git push + launch Edge ──
    if not config["github_actions"]:
        git_commit_push()
        launch_edge(os.path.abspath("docs/index.html"), config["edge_exe"])

    print(f"\n  Done. Processed {len(new_ids)} new email(s).")


if __name__ == "__main__":
    run()
