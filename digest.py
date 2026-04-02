#!/usr/bin/env python3
"""
Daily Digest — generates and emails a structured morning briefing.

Usage:
    python digest.py           # generate and print to terminal (same as --test)
    python digest.py --test    # generate and print to terminal, no email sent
    python digest.py --send    # generate and send email via SendGrid
"""

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin
import urllib.parse

import anthropic
import feedparser
import requests
import yaml
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

from gmail_reader import fetch_newsletter_emails

# ── Worth Your Time paywall domain blacklist ──────────────────────────────────
# Items whose URLs match any of these domains are excluded from the
# Worth Your Time candidate pool before the prompt is sent to Claude.
# economist.com is intentionally absent — Economist access is via cookie auth.
_WYT_BLOCKED_DOMAINS: frozenset[str] = frozenset({
    "wsj.com", "ft.com", "bloomberg.com", "bloomberg.net",
    "nytimes.com", "newyorker.com", "theatlantic.com",
    "foreignaffairs.com", "hbr.org", "businessinsider.com",
    "washingtonpost.com", "thetimes.co.uk", "telegraph.co.uk",
    "theinformation.com",
})


def _is_free_for_wyt(url: str) -> bool:
    """Return True only if the URL is freely accessible (not paywalled)."""
    if not url:
        return False
    url_lower = url.lower()
    return not any(domain in url_lower for domain in _WYT_BLOCKED_DOMAINS)


# ── Setup ───────────────────────────────────────────────────────────────────

load_dotenv()

# Ensure stdout handles emoji on Windows terminals
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

_today_str = datetime.now().strftime("%Y-%m-%d")
_log_file  = LOG_DIR / f"digest_{_today_str}.txt"

# Persistent dedup store for all non-Economist sections (7-day rolling window)
HISTORY_FILE           = LOG_DIR / "story_history.json"
# Permanent record of featured Economist articles (never repeats)
ECONOMIST_HISTORY_FILE = LOG_DIR / "economist_history.json"

HISTORY_DAYS = 7
_HISTORY_SKIP_SECTIONS = {"economist"}   # Economist handled separately

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(_log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ── System prompt ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a senior financial journalist producing a daily briefing for a real \
estate private equity professional focused on affordable housing acquisitions \
and LIHTC transactions. Write in the style of the FT morning newsletter — \
authoritative, concise, no filler. Scale depth to importance: legislation that \
affects LIHTC equity pricing deserves full treatment; routine data gets one line. \
Never include stories that are not genuinely important. For the Markets section \
write a full narrative, not bullets. Every link in Worth Your Time must be a \
real, working URL from the source material provided.

Output clean HTML suitable for email clients (desktop and mobile Gmail). \
Use only inline CSS. Do NOT output a date bar or page title — the email wrapper \
already contains those. Start output directly with the first section.

HTML style rules — follow exactly:

Wrapper div (outermost element you output):
  style="max-width:650px;margin:0 auto;font-family:Arial,Helvetica,sans-serif;\
color:#1a1a1a;line-height:1.8;padding:0 16px 16px;"

Section heading (h2):
  style="font-size:16px;font-weight:bold;margin:32px 0 6px;\
border-left:4px solid #1a1a2e;padding-left:10px;color:#1a1a1a;"

Sub-heading (h3):
  style="font-size:14px;font-weight:bold;margin:16px 0 4px;color:#1a1a1a;"

Body paragraph (p):
  style="margin:6px 0;font-size:14px;line-height:1.8;"

Links (a):
  style="color:#1a3a6e;text-decoration:none;"

Bold callout (number to watch / key insight):
  <p style="margin:12px 0;font-size:14px;"><strong>...</strong></p>

Worth Your Time item card:
  style="margin:0 0 20px;padding:14px 16px;border:1px solid #e8e8e8;\
border-radius:4px;background:#fafafa;"

Do NOT wrap the output in markdown code fences. Output raw HTML only.

Cross-section deduplication — strict: Apply strict cross-section deduplication. Before writing any section check every story, bill, legislation, or development that has already appeared in a previous section. If a topic was covered in any previous section do not cover it again in any subsequent section — not even from a different angle, not even with different framing. Each piece of news appears exactly once in the entire digest in the single most relevant section. Specifically: if a housing bill or legislation appeared in US News do not mention it again in Real Estate; if a geopolitical event appeared in Markets do not cover it again in Macro; if an economic data point appeared in Markets do not reference it again in any other section; if you need to connect a later section to something covered earlier write only 'As noted in [Section Name] above' with no additional detail. This rule has no exceptions.

Science section quality: In the Science & Health section, each story must be written as a single clean paragraph with no repeated language, no repeated phrases, and no restating of the same point. Read each science item back before including it and remove any sentence that repeats information already stated in the same item.

LIHTC connections: Only connect macro developments to LIHTC equity pricing or affordable housing finance when the connection is direct, near-term, and high probability — for example new legislation that explicitly changes LIHTC allocation, Fed rate decisions that will directly affect debt pricing on affordable housing deals, or housing policy that will foreseeably affect Section 8 or HAP contracts. Do not make speculative or distant connections. Do not end every macro item with a LIHTC implication. If the connection is not obvious and concrete, leave it unstated entirely.

Worth Your Time sourcing: The Worth Your Time section should actively seek content from high quality sources beyond the configured RSS feeds. Draw from the full landscape of excellent long-form thinking including philosophy and Stoicism (Daily Stoic / Ryan Holiday at ryanholiday.net, The Marginalian at themarginalian.org, Marcus Aurelius excerpts, Amor Fati and Stoic frameworks, Tim Ferriss on philosophy/decision-making), ideas and mental models (Naval Ravikant at nav.al, Tim Urban / Wait But Why at waitbutwhy.com, Shane Parrish / Farnam Street at fs.blog), science and big ideas (Quanta Magazine at quantamagazine.org, Aeon at aeon.co, Nautilus at nautil.us, Edge.org, Popular Mechanics, Popular Science), health and longevity (Peter Attia at peterattiamd.com, Huberman Lab full episodes only — not Essentials clips), economics and society (Project Syndicate at project-syndicate.org, VoxEU at voxeu.org, Noahpinion long-form pieces). Prioritize in this order: (1) pieces that offer a framework for thinking, living, or deciding — not just information; (2) ideas that compound over time — Stoic philosophy, mental models, scientific principles; (3) content that would be just as valuable to read or listen to in 5 years as today; (4) pieces that would surprise or genuinely expand perspective. Rotate across content types and sources — aim for roughly one philosophical or Stoic piece per week, one science piece, one economics or finance piece. Do not feature the same source two days in a row. Stoicism and Naval-adjacent content should appear roughly once per week when strong material is available. Always include podcasts and videos as candidates alongside articles.

Newsletter content from GZero and The Promote will be labeled as EMAIL SOURCE. Treat these with the same weight as RSS feed content. GZero content belongs in the Macro & Geopolitics section. The Promote content belongs in the Real Estate & Affordable Housing section.

Worth Your Time free-content rule: Every item in Worth Your Time must be completely free to access without any subscription, login, or paywall. You must be 100% certain an item is freely accessible before including it. When in doubt leave it out entirely. Do not include any item from WSJ, FT, Bloomberg, NYT, The Atlantic, New Yorker, Foreign Affairs, HBR, Washington Post, or any other publication that requires a subscription. Strong free sources include: Noahpinion free posts, Aeon, Nautilus, Quanta Magazine, Huberman Lab, Invest Like the Best episode pages, Farnam Street free articles, Wait But Why, The Marginalian, Daily Stoic, Project Syndicate free articles, VoxEU, Popular Science, Popular Mechanics, Stat News, New Scientist free articles, Ars Technica, and any open access research.

The Economist has full article access via authenticated feed. Include Economist long-form pieces, cover stories, and analytical essays in Worth Your Time when they are exceptional quality and have staying power — The Economist is one of the highest-quality sources available. Economist items appear in the WORTH YOUR TIME CANDIDATE POOL and are always eligible.

Markets section length: Write the Markets section at 80 percent of your normal length. Be more concise. Cut any sentence that restates something already said. Every sentence must add new information. The section should read like a tight FT briefing not a full analysis piece.

Links policy: Whenever you recommend that the reader check something, visit a source, or look something up — always provide a direct hyperlink to that specific resource. Never say 'it would be worth checking X' or 'see Y directly' without including the URL as a clickable link. If you do not have the specific URL for a resource, do not recommend it. Only recommend things you can link to directly.

References section: After the Worth Your Time section and before any footer, output a final section titled '📎 Sources & References'. This section lists every article, report, or source that was cited or linked anywhere in today's digest as a numbered list of clickable hyperlinks in this format: [N]. [Headline or title] — [Source name] with the title as a hyperlink to the URL. Include every source that was linked inline in the digest body. Use this exact header HTML:
<h2 style="font-size:16px;font-weight:bold;margin:32px 0 6px;border-left:4px solid #1a1a2e;padding-left:10px;color:#1a1a1a;">📎 Sources &amp; References</h2>
Then a numbered list using <ol style="margin:8px 0 0;padding-left:20px;font-size:13px;line-height:2;color:#333;"> with each <li> containing the linked title and source name.\
"""

# ── Email wrapper ─────────────────────────────────────────────────────────────

EMAIL_WRAPPER = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Jeff's Daily Digest — {date}</title>
</head>
<body style="margin:0;padding:0;background:#f0f0eb;">
<table width="100%" cellpadding="0" cellspacing="0"
  style="background:#f0f0eb;padding:24px 0;">
  <tr><td align="center">
    <table width="100%" cellpadding="0" cellspacing="0"
      style="max-width:650px;margin:0 auto;background:#ffffff;\
border-radius:4px;overflow:hidden;">

      <!-- Header -->
      <tr><td style="background:#1a1a2e;padding:28px 24px 20px;text-align:center;">
        <h1 style="margin:0;font-size:22px;font-weight:bold;\
font-family:Arial,Helvetica,sans-serif;color:#ffffff;letter-spacing:0.5px;">
          Jeff's Daily Digest
        </h1>
        <p style="margin:6px 0 0;font-size:13px;color:#a0a8c0;\
font-family:Arial,Helvetica,sans-serif;">{date}</p>
      </td></tr>

      <!-- Body -->
      <tr><td style="padding:8px 24px 8px;">
        {body}
      </td></tr>

      <!-- Footer -->
      <tr><td style="padding:16px 24px 24px;border-top:1px solid #e8e8e8;\
text-align:center;">
        <p style="margin:0;font-size:12px;color:#aaaaaa;\
font-family:Arial,Helvetica,sans-serif;">
          Powered by Claude &middot; {date}
        </p>
      </td></tr>

    </table>
  </td></tr>
</table>
</body>
</html>"""

# ── Config loading ────────────────────────────────────────────────────────────

def load_sources(path: str = "sources.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)

# ── Story history (7-day rolling dedup) ──────────────────────────────────────

def load_story_history() -> dict:
    """Return {url: {title, date}} from HISTORY_FILE, or {} if missing/corrupt."""
    if not HISTORY_FILE.exists():
        return {}
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        log.warning("Could not read story_history.json: %s — starting fresh", exc)
        return {}


def prune_story_history(history: dict) -> dict:
    """Drop entries older than HISTORY_DAYS and return the pruned dict."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS)
    before  = len(history)
    history = {
        url: entry
        for url, entry in history.items()
        if datetime.fromisoformat(entry["date"]) > cutoff
    }
    pruned = before - len(history)
    if pruned:
        log.info("Pruned %d expired story_history entries (>%d days old)", pruned, HISTORY_DAYS)
    return history


def save_story_history(history: dict) -> None:
    HISTORY_FILE.parent.mkdir(exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)
    log.info("Saved %d entries to story_history.json", len(history))


def filter_seen_content(
    content: dict[str, list[dict]], history: dict
) -> dict[str, list[dict]]:
    """Remove feed items already in history from all non-Economist sections."""
    seen_urls   = set(history.keys())
    seen_titles = {e["title"].lower() for e in history.values() if e.get("title")}
    filtered: dict[str, list[dict]] = {}
    for section, items in content.items():
        if section in _HISTORY_SKIP_SECTIONS:
            filtered[section] = items
            continue
        kept, skipped = [], 0
        for item in items:
            if item["link"] in seen_urls or item["title"].lower() in seen_titles:
                skipped += 1
                log.info("  [dedup] skipping: %s", item["title"][:90])
            else:
                kept.append(item)
        if skipped:
            log.info(
                "Section %-25s  skipped %d seen, kept %d",
                section, skipped, len(kept),
            )
        filtered[section] = kept
    return filtered


def extract_featured_stories(html: str, content: dict[str, list[dict]]) -> dict:
    """
    Scan generated HTML for hrefs that match source feed items.
    Returns {url: {title, date}} ready to merge into story_history.
    """
    url_to_title: dict[str, str] = {}
    for section, items in content.items():
        if section in _HISTORY_SKIP_SECTIONS:
            continue
        for item in items:
            if item["link"]:
                url_to_title[item["link"]] = item["title"]

    now = datetime.now(timezone.utc).isoformat()
    featured: dict = {}
    for url in re.findall(r'href="([^"]+)"', html):
        if url in url_to_title and url not in featured:
            featured[url] = {"title": url_to_title[url], "date": now}

    log.info("Extracted %d featured story URL(s) for history", len(featured))
    return featured

# ── Economist curation (permanent non-repeating rotation) ────────────────────

def load_economist_history() -> set[str]:
    """Return the set of Economist article URLs already featured."""
    if not ECONOMIST_HISTORY_FILE.exists():
        return set()
    try:
        with open(ECONOMIST_HISTORY_FILE, encoding="utf-8") as f:
            return set(json.load(f).get("used_urls", []))
    except Exception as exc:
        log.warning("Could not read economist_history.json: %s — starting fresh", exc)
        return set()


def save_economist_history(used_urls: set[str]) -> None:
    ECONOMIST_HISTORY_FILE.parent.mkdir(exist_ok=True)
    with open(ECONOMIST_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump({"used_urls": sorted(used_urls)}, f, indent=2)
    log.info("Saved %d URL(s) to economist_history.json", len(used_urls))


def _fetch_economist_article_text(url: str, headers: dict) -> str:
    """
    Attempt to fetch full article body from The Economist using cookie auth.

    To get your Economist session cookie:
      1. Log in to economist.com in Chrome
      2. Press F12 → Application tab → Cookies → https://www.economist.com
      3. Find the cookie named 'session_id' (or 'economist_session' / 'session')
      4. Copy the Value and paste into .env as ECONOMIST_SESSION_COOKIE=<value>
    """
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            return ""
        soup = BeautifulSoup(resp.text, "lxml")
        for sel in [
            "article",
            "[class*='article__body']",
            "[class*='article-body']",
            "div.layout-article-body",
            "div.article__content",
            "[data-component='article-body']",
        ]:
            el = soup.select_one(sel)
            if el:
                text = el.get_text(separator=" ", strip=True)
                if len(text) > 200:
                    return text[:2000]
    except Exception:
        pass
    return ""


def fetch_economist_all(source: dict) -> list[dict]:
    """Fetch every entry from The Economist feed regardless of publish date.
    Uses ECONOMIST_SESSION_COOKIE from .env for authenticated access when set.
    """
    items = []
    econ_cookie = os.getenv("ECONOMIST_SESSION_COOKIE", "").strip()
    headers: dict[str, str] = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }
    if econ_cookie:
        headers["Cookie"] = f"session_id={econ_cookie}"

    try:
        feed = feedparser.parse(source["url"], request_headers=headers)
        for entry in feed.entries:
            pub = _parse_entry_date(entry)
            summary = (getattr(entry, "summary", "") or "")[:600]
            link = getattr(entry, "link", "")
            # Attempt full article fetch when cookie is available
            if econ_cookie and link:
                full_text = _fetch_economist_article_text(link, headers)
                if full_text:
                    summary = full_text[:600]
            items.append({
                "source":    source["name"],
                "title":     getattr(entry, "title", ""),
                "link":      link,
                "summary":   summary,
                "published": pub.isoformat() if pub else "",
            })
        log.info("The Economist feed: %d total item(s)", len(items))
    except Exception as exc:
        log.error("Failed to fetch Economist feed: %s", exc)
    return items


def select_economist_article(
    all_items: list[dict],
    used_urls: set[str],
    today_headlines: list[str],
    client: anthropic.Anthropic,
) -> dict | None:
    """
    Use a lightweight Claude call to pick the single best unread Economist
    article. Prioritises quality and analytical depth, avoids topic duplication
    with today's other content. Returns the chosen item dict or None.
    """
    unread = [item for item in all_items if item["link"] not in used_urls]
    if not unread:
        log.info("Economist: no unread articles available")
        return None

    candidates_text = "\n\n".join(
        f"INDEX: {i}\nTITLE: {item['title']}\nURL: {item['link']}\nSUMMARY: {item['summary']}"
        for i, item in enumerate(unread)
    )
    headlines_text = (
        "\n".join(f"- {h}" for h in today_headlines) if today_headlines else "(none)"
    )

    prompt = f"""\
You are selecting one Economist article for a daily digest read by a real \
estate private equity professional focused on affordable housing.

TODAY'S DIGEST ALREADY COVERS THESE TOPICS:
{headlines_text}

UNREAD ECONOMIST ARTICLES (never previously featured):
{candidates_text}

SELECTION CRITERIA (apply in priority order):
1. Prefer long-form analysis, opinion pieces, and cover stories over news briefs.
2. Prefer articles offering a unique analytical angle not covered by wire \
services (Reuters, Bloomberg, The Hill).
3. REJECT articles that merely duplicate breaking news already in today's digest \
unless they offer a distinctly different analytical take.
4. Prefer depth on economics, geopolitics, policy, business, science, or culture.
5. Reject short news-in-brief items.

Reply with ONLY the INDEX number of the best article, or the word NONE if no \
article clears the quality bar. Output nothing else."""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=10,
            messages=[{"role": "user", "content": prompt}],
        )
        choice = response.content[0].text.strip()
        if choice.upper() == "NONE":
            log.info("Economist selection: no suitable article found")
            return None
        selected = unread[int(choice)]
        log.info("Economist selection: [%s] %s", choice, selected["title"][:80])
        return selected
    except Exception as exc:
        log.error("Economist article selection failed: %s", exc)
        return None

# ── Feed fetching ─────────────────────────────────────────────────────────────

def _parse_entry_date(entry) -> datetime | None:
    for attr in ("published_parsed", "updated_parsed"):
        val = getattr(entry, attr, None)
        if val:
            try:
                return datetime(*val[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None


def fetch_feed(source: dict, lookback_hours: int = 24) -> list[dict]:
    """Return items from one RSS feed published within lookback_hours."""
    items  = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    try:
        feed = feedparser.parse(source["url"])
        for entry in feed.entries:
            pub = _parse_entry_date(entry)
            if pub is None or pub < cutoff:
                continue
            items.append({
                "source":    source["name"],
                "title":     getattr(entry, "title", ""),
                "link":      getattr(entry, "link", ""),
                "summary":   (getattr(entry, "summary", "") or "")[:600],
                "published": pub.isoformat(),
            })
        log.info("%-30s  %d item(s) in last %dh", source["name"], len(items), lookback_hours)
    except Exception as exc:
        log.error("Failed to fetch %-30s  %s", source["name"], exc)
    return items

# ── Web scraping (institutional research pages) ───────────────────────────────

_DATE_FORMATS = [
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%d",
    "%B %d, %Y",
    "%b %d, %Y",
    "%d %B %Y",
    "%d %b %Y",
    "%m/%d/%Y",
    "%B %Y",
    "%b %Y",
]


def _parse_date_string(raw: str) -> datetime | None:
    if not raw:
        return None
    raw = raw.strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


_SCRAPE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def scrape_page(source: dict) -> list[dict]:
    """
    Scrape an institutional research listing page for recent articles.
    Returns up to 3 items in the same format as fetch_feed().
    Failures are logged and an empty list is returned — never raises.
    """
    name           = source["name"]
    url            = source["url"]
    lookback_hours = source.get("lookback_hours", 168)
    cutoff         = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

    try:
        resp = requests.get(url, headers=_SCRAPE_HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
    except Exception as exc:
        log.error("Scrape failed  %-30s  %s", name, exc)
        return []

    # Locate article containers — try configured selector first, then heuristics
    article_sel = source.get("article_selector", "")
    if article_sel:
        containers = soup.select(article_sel)[:12]
    else:
        containers = []
        for sel in [
            "article",
            "[class*='article']",
            "[class*='card']",
            "[class*='insight']",
            "[class*='research']",
            "[class*='post']",
            "[class*='item']",
        ]:
            found = [
                el for el in soup.select(sel)
                if el.find(["h2", "h3", "h4"]) and el.find("a", href=True)
            ]
            if found:
                containers = found[:12]
                break

    if not containers:
        log.warning("Scrape %-30s  no article containers found", name)
        return []

    title_sel = source.get("title_selector", "")
    date_sel  = source.get("date_selector", "")
    items: list[dict] = []
    seen_urls: set[str] = set()

    for container in containers:
        # Title
        title_el = container.select_one(title_sel) if title_sel else container.find(["h2", "h3", "h4"])
        title    = title_el.get_text(strip=True) if title_el else ""

        # Link — prefer anchor wrapping the heading
        link_el = (title_el.find_parent("a") or title_el.find("a")) if title_el else None
        if not link_el:
            link_el = container.find("a", href=True)
        href = link_el.get("href", "") if link_el else ""
        if href and not href.startswith("http"):
            href = urljoin(url, href)

        if not title or not href or href in seen_urls:
            continue
        seen_urls.add(href)

        # Date
        if date_sel:
            date_el = container.select_one(date_sel)
        else:
            date_el = container.find("time") or container.find(
                attrs={"class": re.compile(r"date|published|timestamp", re.I)}
            )
        raw_date = ""
        if date_el:
            raw_date = date_el.get("datetime") or date_el.get_text(strip=True)
        pub = _parse_date_string(raw_date)

        # Skip only if we can confirm the article is older than the cutoff
        if pub is not None and pub < cutoff:
            continue

        items.append({
            "source":    name,
            "title":     title,
            "link":      href,
            "summary":   "",
            "published": pub.isoformat() if pub else "",
        })
        if len(items) == 3:
            break

    log.info("%-30s  %d item(s) scraped", name, len(items))
    return items

# ── Content collection ────────────────────────────────────────────────────────

SECTION_LIMITS: dict[str, int] = {
    "markets":             25,
    "macro_geopolitics":   20,
    "us_news":             15,
    "real_estate":         20,
    "research_intel":      20,
    "ai_tech":             12,
    "science_health":      10,
    "podcasts_newsletters": 12,
}


def collect_content(sources: dict) -> dict[str, list[dict]]:
    """
    Fetch every RSS feed and scrape every configured page.
    The 'economist' section is excluded — handled separately.
    """
    result: dict[str, list[dict]] = {}
    for section, feeds in sources.get("sources", {}).items():
        if section == "economist":
            continue
        items: list[dict] = []
        for feed in feeds:
            if feed.get("type") == "scrape":
                items.extend(scrape_page(feed))
            else:
                lookback = feed.get("lookback_hours", 24)
                items.extend(fetch_feed(feed, lookback_hours=lookback))
        result[section] = items
    return result

# ── Prompt building ───────────────────────────────────────────────────────────

def _format_items(items: list[dict], limit: int) -> str:
    lines = []
    for item in items[:limit]:
        lines.append(
            f"SOURCE: {item['source']}\n"
            f"TITLE: {item['title']}\n"
            f"URL: {item['link']}\n"
            f"SUMMARY: {item['summary']}\n"
            f"PUBLISHED: {item['published']}\n"
        )
    return "\n".join(lines) if lines else "(no items)"


def _format_email_items(items: list[dict]) -> str:
    """Format Gmail newsletter items for the Claude prompt."""
    lines = []
    for item in items:
        lines.append(
            f"EMAIL SOURCE: {item['source']}\n"
            f"SUBJECT: {item['title']}\n"
            f"DATE: {item['date']}\n"
            f"CONTENT:\n{item['content']}\n"
        )
    return "\n".join(lines) if lines else "(no items)"


def build_user_prompt(content: dict[str, list[dict]], today: datetime) -> str:
    def section(title: str, key: str) -> str:
        items = content.get(key, [])
        limit = SECTION_LIMITS.get(key, 15)
        return f"=== {title} ===\n{_format_items(items, limit)}"

    # Economist: single pre-selected article already injected into content["economist"]
    econ_items = content.get("economist", [])
    if econ_items:
        e = econ_items[0]
        econ_block = (
            "=== THE ECONOMIST (today's curated selection) ===\n"
            f"SOURCE: {e['source']}\n"
            f"TITLE: {e['title']}\n"
            f"URL: {e['link']}\n"
            f"SUMMARY: {e['summary']}\n"
        )
    else:
        econ_block = (
            "=== THE ECONOMIST ===\n"
            "(no suitable article selected today — omit the section)"
        )

    # Email newsletter block (GZero, The Promote, etc.)
    email_items = content.get("email_newsletters", [])
    if email_items:
        email_block = f"=== EMAIL NEWSLETTER SOURCES (GZero, The Promote, etc.) ===\n{_format_email_items(email_items)}"
    else:
        email_block = None

    raw_blocks = [
        section("MARKETS (Bloomberg, WSJ, FT)", "markets"),
        section("MACRO & GEOPOLITICS (Reuters, FT, Bloomberg, GZero)", "macro_geopolitics"),
        section("US NEWS (Reuters, The Hill)", "us_news"),
        section("REAL ESTATE & AFFORDABLE HOUSING (The Real Deal, AHF, The Promote, Jay Parsons)", "real_estate"),
        section(
            "RESEARCH & MARKET INTELLIGENCE "
            "(Goldman Sachs, Morgan Stanley, JPMorgan, BlackRock, "
            "CBRE, JLL, Newmark, Berkadia, Marcus & Millichap, "
            "Bloomberg Economics, Reuters Finance, CoStar, GlobeSt)",
            "research_intel",
        ),
        section(
            "AI & TECHNOLOGY "
            "(Ben's Bites, Anthropic Blog, The Rundown AI, MIT Technology Review, Ars Technica)",
            "ai_tech",
        ),
        section("SCIENCE & HEALTH (New Scientist, Stat News, Nature News)", "science_health"),
        section("RECENT RELEASES — PODCASTS & NEWSLETTERS (72-hour window)", "podcasts_newsletters"),
        econ_block,
    ]
    if email_block:
        raw_blocks.insert(0, email_block)

    # Build free-only Worth Your Time candidate pool (hard pre-filter).
    # Only items whose URLs pass _is_free_for_wyt() are eligible.
    # Economist items (economist.com) pass because they are not in _WYT_BLOCKED_DOMAINS.
    wyt_seen: set[str] = set()
    wyt_candidates: list[dict] = []
    for section_key, section_items in content.items():
        if not isinstance(section_items, list):
            continue
        for item in section_items:
            if not isinstance(item, dict):
                continue
            url = item.get("link") or item.get("url") or ""
            if url and url not in wyt_seen and _is_free_for_wyt(url):
                wyt_seen.add(url)
                wyt_candidates.append(item)

    if wyt_candidates:
        wyt_lines = []
        for item in wyt_candidates[:60]:
            wyt_lines.append(
                f"SOURCE: {item.get('source', '')}\n"
                f"TITLE: {item.get('title', '')}\n"
                f"URL: {item.get('link') or item.get('url', '')}\n"
                f"SUMMARY: {item.get('summary', '')}\n"
            )
        wyt_block = (
            "=== WORTH YOUR TIME CANDIDATE POOL (free sources only — "
            "use ONLY these for Worth Your Time selection) ===\n"
            + "\n".join(wyt_lines)
        )
    else:
        wyt_block = (
            "=== WORTH YOUR TIME CANDIDATE POOL ===\n"
            "(no free candidates available today — omit Worth Your Time section)"
        )
    raw_blocks.append(wyt_block)

    raw_content = "\n\n".join(raw_blocks)

    return f"""\
Today is {today.strftime("%A, %B %d, %Y")}.

Below is every RSS item collected in the last 24–72 hours (varies by source).
Use only URLs that appear verbatim in this data — never fabricate links.

{raw_content}

─────────────────────────────────────────────────────────────────────────────
DIGEST SECTIONS TO PRODUCE (in this order):

1. 📈 Markets — FT-style narrative covering equities, rates, oil, credit, FX. Write at 80% of normal length — tight and concise. Every sentence must add new information; cut any sentence that restates something already said. Identify the dominant market theme. End with one bolded "Number to watch."

2. 🌍 Macro & Geopolitics — Up to 3 stories. Bold sub-header per story.
   2–3 sentences each.

3. 🇺🇸 US News — Only if genuinely important domestic news exists.
   Skip the section entirely if nothing clears that bar.

4. 🏘️ Real Estate & Affordable Housing — Variable depth based on importance.
   Legislation and LIHTC policy get full treatment. Routine market data gets
   one line.

5. 🏦 Research & Market Intelligence — 2–3 items max. Skip entirely if nothing
   relevant is found. Prioritize in this order:
     (a) Primary institutional research: Goldman Sachs, Morgan Stanley, JPMorgan,
         BlackRock, CBRE, JLL, Newmark, Berkadia, Marcus & Millichap
     (b) Wire-service summaries: Bloomberg Economics, Reuters Finance, CoStar, GlobeSt
   Focus on: multifamily trends, interest rate outlooks, cap rate trends, CRE
   investment volumes, macroeconomic forecasts. Ignore anything already covered in
   Markets or Macro. Bloomberg.com links: plain text with (subscription required).

6. 🤖 AI & Technology — 2–3 items max. Variable depth: breakthrough models or
   major policy shifts get full treatment; routine product news gets one line.

7. 🔬 Science & Health — 2 stories max. Only include if genuinely important
   (major findings, significant public health developments). Skip entirely if
   nothing clears the bar. Variable depth: landmark studies get fuller treatment.

8. 🎙️ Recent Releases — Only include items published in the last 48 hours.
   One-line description + link per item. Skip section if nothing new.

9. 🗞️ Economist — Feature today's pre-selected article (see THE ECONOMIST block
   above). Write: bold headline as <h3>, then exactly two sentences explaining
   why this piece is worth reading and what unique insight it offers, then a link.
   If the block says "(no suitable article selected today — omit the section)",
   skip this section entirely.

10. 💡 One Thing to Learn Today — Single practical insight tied to something in
   the digest. Applicable to real estate PE / affordable housing finance or
   general intellectual growth.

11. 📚 Worth Your Time — Select 1–2 items TOTAL. Skip entirely if nothing clears
   the bar — do not pad with mediocre content.

   CONTENT TYPES AND MINIMUM LENGTHS:
   📄 Articles/essays  — min ~1 000 words / 5 min read; max ~30 min read.
      Must NOT be breaking news or a daily/weekly data recap.
   🎬 Videos           — min 2 min; max 30 min. Substantive only — no trailers.
   🎧 Podcasts         — min 2 min; max 60 min. Full episodes preferred.

   QUALITY TESTS — every item must pass ALL five:
   1. Worth consuming one month from now?
   2. Genuine analysis, original thinking, or narrative depth?
   3. NOT a breaking news report or weekly data summary?
   4. High-quality, credible source?
   5. Freely accessible, or clearly flagged as (subscription required)?

   STRONG CANDIDATES BY TOPIC:
   Economics/markets:  Noahpinion essays, Invest Like the Best episodes,
     Bloomberg Odd Lots deep-dives, Goldman/Morgan Stanley structural notes.
   Science/health:     New Scientist, Stat News, Nature, MIT Technology Review,
     Huberman Lab full episodes (not short clips).
   Technology:         MIT Technology Review long-form, Ars Technica deep dives.
   Real estate:        CBRE/JLL market outlook reports, Berkadia research.
   Ideas/behaviour:    Huberman Lab, Invest Like the Best framework discussions.

   ALWAYS EXCLUDE:
   Breaking news · market recaps · weekly data summaries · press releases ·
   content shorter than minimums · listicles or aggregator posts.

   PAYWALL RULES:
   DO NOT hyperlink these domains — render as plain text with (subscription required):
     wsj.com  ft.com  bloomberg.com  theinformation.com  nytimes.com
     newyorker.com  theatlantic.com  foreignaffairs.com  hbr.org
     wired.com  businessinsider.com  washingtonpost.com
   economist.com — always link freely (authenticated access enabled).
   Always link freely:
     reuters.com  thehill.com  npr.org  noahpinion.blog  bensbites.com
     anthropic.com  housingfinance.com  congress.gov  *.gov  *.edu
     newscientist.com  statnews.com  technologyreview.com  therealdeal.com
     colossus.com  hubermanlab.com  arstechnica.com  therundown.ai  nature.com
     goldmansachs.com  morganstanley.com  jpmorgan.com  blackrock.com
     cbre.com  us.jll.com  nmrk.com  berkadia.com  marcusmillichap.com
     globest.com  costar.com  popularmechanics.com  popsci.com
   When in doubt: do not hyperlink.

   SECTION HEADER — render exactly as:
   <h2 style="font-size:16px;font-weight:bold;margin:32px 0 2px;\
border-left:4px solid #1a1a2e;padding-left:10px;color:#1a1a1a;">
     📚 Worth Your Time
   </h2>
   <p style="margin:0 0 16px;padding-left:14px;font-size:12px;color:#888;\
font-family:Arial,Helvetica,sans-serif;">
     Curated reads, listens, and watches with staying power
   </p>

   ITEM FORMAT — for each selected item:

   Icon by type:  📄 article   🎬 video   🎧 podcast

   Badge colours:
     READ   → background:#d4edda;color:#155724
     WATCH  → background:#cce5ff;color:#004085
     LISTEN → background:#fff3cd;color:#856404

   Duration labels:  "[N] min read" / "[N] min watch" / "[N] min listen"

   ADD TO LIST BUTTON — construct a static percent-encoded href:
   Base:  https://jeffreyeehrlich-ui.github.io/daily-digest/reading-list/?add=
   Append the URL-encoded version of a JSON object with these fields:
     {{"title": "[TITLE]", "url": "[ITEM_URL]", "source": "[SOURCE]", "type": "[article|podcast|video]", "category": "[one of: markets|real-estate|macro|ai|science|health|philosophy|policy|other]", "duration": "[duration]", "description": "[2-3 sentence description]"}}

   Valid category values (use EXACTLY one of these strings):
     markets, real-estate, macro, ai, science, health, philosophy, policy, other

   Encoding rules (apply to the entire JSON string):
     space->%20  newline->%0A  :->%3A  /->%2F  ?->%3F  =->%3D  &->%26  #->%23  +->%2B  quote->%22  open-brace->%7B  close-brace->%7D  open-bracket->%5B  close-bracket->%5D  comma->%2C

   Worked example (title "Why Rates Matter", url "https://noahpinion.blog/p/x"):
   https://jeffreyeehrlich-ui.github.io/daily-digest/reading-list/?add=%7B%22title%22%3A%22Why%20Rates%20Matter%22%2C%22url%22%3A%22https%3A%2F%2Fnoahpinion.blog%2Fp%2Fx%22%2C%22source%22%3A%22Noahpinion%22%2C%22type%22%3A%22article%22%2C%22category%22%3A%22macro%22%2C%22duration%22%3A%2212%20min%20read%22%2C%22description%22%3A%22A%20clear%20look%20at%20rate%20dynamics.%22%7D

   FREELY-LINKED ITEM TEMPLATE:
   <div style="margin:0 0 20px;padding:14px 16px;border:1px solid #e8e8e8;\
border-radius:4px;background:#fafafa;">
     <p style="margin:0 0 4px;font-size:14px;font-weight:bold;color:#1a1a1a;">
       [ICON] <a href="[ITEM_URL]" style="color:#1a3a6e;text-decoration:none;">
         [HEADLINE]
       </a> — <span style="color:#555;">[SOURCE]</span>
     </p>
     <p style="margin:0 0 8px;font-size:12px;">
       <span style="display:inline-block;padding:2px 7px;border-radius:3px;\
font-weight:bold;font-size:11px;[BADGE_STYLE]">[READ|WATCH|LISTEN]</span>
       &nbsp;<span style="color:#888;">[DURATION]</span>
     </p>
     <p style="margin:0 0 10px;font-size:14px;line-height:1.7;color:#333;">
       [2-3 sentence description: what does this argue/explore/teach, why will
       it still be valuable in a month, what will the reader/listener take away?]
     </p>
     <a href="[ADD_TO_LIST_HREF]" target="_blank"
        style="display:inline-block;padding:5px 12px;background:#1a1a2e;\
color:#ffffff;font-family:Arial,Helvetica,sans-serif;font-size:12px;\
text-decoration:none;border-radius:3px;">
       + Add to list
     </a>
   </div>

   PAYWALLED ITEM TEMPLATE (no hyperlinks, no button):
   <div style="margin:0 0 20px;padding:14px 16px;border:1px solid #e8e8e8;\
border-radius:4px;background:#fafafa;">
     <p style="margin:0 0 4px;font-size:14px;font-weight:bold;color:#1a1a1a;">
       [ICON] [HEADLINE] — <span style="color:#555;">[SOURCE]</span>
       <span style="color:#999;font-weight:normal;font-size:13px;">
         (subscription required)
       </span>
     </p>
     <p style="margin:0 0 8px;font-size:12px;">
       <span style="display:inline-block;padding:2px 7px;border-radius:3px;\
font-weight:bold;font-size:11px;[BADGE_STYLE]">[READ|WATCH|LISTEN]</span>
       &nbsp;<span style="color:#888;">[DURATION]</span>
     </p>
     <p style="margin:0;font-size:14px;line-height:1.7;color:#333;">
       [2-3 sentence description]
     </p>
   </div>

Output raw HTML only. No markdown fences."""

# ── Claude call ───────────────────────────────────────────────────────────────

def generate_digest(
    content:   dict,
    today:     datetime,
    test_mode: bool = False,
    client:    anthropic.Anthropic | None = None,
) -> str:
    if client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY is not set.")
        client = anthropic.Anthropic(api_key=api_key)

    user_prompt = build_user_prompt(content, today)
    log.info("Calling Claude API (claude-sonnet-4-6) …")

    html_parts: list[str] = []
    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=8192,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    ) as stream:
        for text in stream.text_stream:
            html_parts.append(text)
            if test_mode:
                print(text, end="", flush=True)

    raw = "".join(html_parts)
    # Strip accidental markdown fences
    raw = re.sub(r"^```[a-z]*\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw)

    log.info("Claude response received (%d chars)", len(raw))
    return raw

# ── Email assembly ────────────────────────────────────────────────────────────

def wrap_email(body_html: str, today: datetime) -> str:
    return EMAIL_WRAPPER.format(
        date=today.strftime("%B %d, %Y"),
        body=body_html,
    )

# ── SendGrid delivery ─────────────────────────────────────────────────────────

_WYT_BASE_URL = "https://jeffreyeehrlich-ui.github.io/daily-digest/reading-list/?add="
_WYT_HREF_RE  = re.compile(
    r'href="(https://jeffreyeehrlich-ui\.github\.io/daily-digest/reading-list/\?add=[^"]*)"',
    re.IGNORECASE,
)


def _fix_wyt_add_links(html: str) -> str:
    """
    Post-process generated email HTML to ensure every ?add= link is correctly
    percent-encoded. Claude sometimes produces partial or inconsistent encoding
    of special characters (apostrophes, quotes, commas) in the JSON payload.
    This function decodes whatever Claude produced and re-encodes it cleanly
    using urllib.parse.quote, making the links identical on all devices.
    """
    import json as _json

    def _fix(match: re.Match) -> str:
        href = match.group(1)
        prefix = _WYT_BASE_URL
        encoded_part = href[len(prefix):]
        try:
            decoded = urllib.parse.unquote(encoded_part)
            obj = _json.loads(decoded)          # validate JSON
            clean_encoded = urllib.parse.quote(
                _json.dumps(obj, ensure_ascii=False, separators=(",", ":")),
                safe="",
            )
            return f'href="{prefix}{clean_encoded}"'
        except Exception:
            return match.group(0)               # leave unchanged if unparseable

    return _WYT_HREF_RE.sub(_fix, html)


def send_email(html: str, today: datetime) -> None:
    api_key    = os.environ.get("SENDGRID_API_KEY")
    from_email = os.environ.get("FROM_EMAIL")
    to_email   = os.environ.get("TO_EMAIL")

    for var, val in [
        ("SENDGRID_API_KEY", api_key),
        ("FROM_EMAIL",       from_email),
        ("TO_EMAIL",         to_email),
    ]:
        if not val:
            raise ValueError(f"{var} is not set.")

    subject  = f"Jeff's Daily Digest — {today.strftime('%B %d, %Y')}"
    full_html = wrap_email(html, today)

    message = Mail(
        from_email=from_email,
        to_emails=to_email,
        subject=subject,
        html_content=full_html,
    )
    sg       = SendGridAPIClient(api_key)
    response = sg.send(message)
    log.info("Email sent  status=%s  to=%s", response.status_code, to_email)

# ── Entry point ───────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate and send the daily digest.")
    mode   = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--test", action="store_true",
        help="Print to terminal instead of sending email (default).",
    )
    mode.add_argument(
        "--send", action="store_true",
        help="Generate and send via SendGrid.",
    )
    parser.add_argument(
        "--sources", default="sources.yaml",
        help="Path to sources YAML config (default: sources.yaml).",
    )
    return parser.parse_args()


def main() -> None:
    args      = parse_args()
    send_mode = args.send
    today     = datetime.now(timezone.utc)

    log.info("=" * 60)
    log.info(
        "Daily Digest  --  %s  (mode=%s)",
        today.strftime("%Y-%m-%d"),
        "send" if send_mode else "test",
    )
    log.info("=" * 60)

    # 1. Load sources config
    sources = load_sources(args.sources)

    # 2. Load and prune 7-day story dedup history
    history = load_story_history()
    history = prune_story_history(history)

    # 3. Load Economist history; fetch all Economist feed items (no date cutoff)
    economist_used    = load_economist_history()
    economist_sources = sources.get("sources", {}).get("economist", [])
    economist_all: list[dict] = []
    for src in economist_sources:
        economist_all.extend(fetch_economist_all(src))

    # 4. Fetch all other feeds / scrape pages
    log.info("Fetching RSS feeds and scraping research pages …")
    content = collect_content(sources)
    log.info("Total items fetched: %d", sum(len(v) for v in content.values()))

    # 5. Remove stories already seen in the last 7 days
    content = filter_seen_content(content, history)
    log.info("Total items after dedup: %d", sum(len(v) for v in content.values()))

    # 5b. Fetch Gmail newsletter emails (GZero, The Promote, etc.)
    log.info("Fetching Gmail newsletter emails …")
    gmail_items = fetch_newsletter_emails()
    if gmail_items:
        log.info("Adding %d Gmail newsletter item(s) to digest", len(gmail_items))
        content["email_newsletters"] = gmail_items
    else:
        content["email_newsletters"] = []

    # 6. Create shared Claude client (reused for Economist selection + main digest)
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY is not set.")
    claude_client = anthropic.Anthropic(api_key=api_key)

    # 7. Select best unread Economist article
    today_headlines = [item["title"] for items in content.values() for item in items]
    economist_pick  = select_economist_article(
        economist_all, economist_used, today_headlines, claude_client
    )
    content["economist"] = [economist_pick] if economist_pick else []

    # 8. Mark Economist article as used and persist
    if economist_pick:
        economist_used.add(economist_pick["link"])
    save_economist_history(economist_used)

    # 9. Generate digest via Claude
    digest_html = generate_digest(
        content, today, test_mode=not send_mode, client=claude_client
    )

    # 9b. Fix any malformed ?add= URLs produced by Claude
    digest_html = _fix_wyt_add_links(digest_html)

    # 10. Record featured stories and persist dedup history
    new_entries = extract_featured_stories(digest_html, content)
    history.update(new_entries)
    save_story_history(history)

    if send_mode:
        send_email(digest_html, today)
        log.info("Done.")
    else:
        print("\n\n" + "─" * 60)
        print("[TEST MODE] Digest generated. No email sent.")
        print(f"Log: {_log_file}")


if __name__ == "__main__":
    main()
