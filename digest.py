#!/usr/bin/env python3
"""
Daily Digest — generates and emails a structured morning briefing.

Usage:
    python digest.py           # generate and print to terminal (same as --test)
    python digest.py --test    # generate and print to terminal, no email sent
    python digest.py --send    # generate and send email via Gmail SMTP
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

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import anthropic
import feedparser
import requests
import yaml
from bs4 import BeautifulSoup
from dotenv import load_dotenv

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
Never include stories that are not genuinely important. \
Every link in Worth Your Time must be a real, working URL from the source \
material provided.

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

Cross-section deduplication — strict: Apply strict cross-section deduplication. Before writing any section check every story, bill, legislation, or development that has already appeared in a previous section. If a topic was covered in any previous section do not cover it again in any subsequent section — not even from a different angle, not even with different framing. Each piece of news appears exactly once in the entire digest in the single most relevant section. Specifically: if a housing bill or legislation appeared in US News do not mention it again in Real Estate; if a geopolitical event appeared in Markets do not cover it again in Macro; if an economic data point appeared in Markets do not reference it again in any other section; if you need to connect a later section to something covered earlier write only 'As noted above' with no additional detail. This rule has no exceptions.

Science section quality: In the Science & Health section, each story must be written as a single clean paragraph with no repeated language, no repeated phrases, and no restating of the same point. Read each science item back before including it and remove any sentence that repeats information already stated in the same item.

LIHTC connections: Only connect macro developments to LIHTC equity pricing or affordable housing finance when the connection is direct, near-term, and high probability — for example new legislation that explicitly changes LIHTC allocation, Fed rate decisions that will directly affect debt pricing on affordable housing deals, or housing policy that will foreseeably affect Section 8 or HAP contracts. Do not make speculative or distant connections. Do not end every macro item with a LIHTC implication. If the connection is not obvious and concrete, leave it unstated entirely.

Critical News: Include this section only when something genuinely urgent, consequential, and time-sensitive has occurred — something a real estate PE professional would need to know before a morning call with an investor or lender. Maximum 3 items, minimum 0. What clears the bar: major geopolitical event with immediate market impact; central bank emergency action or surprise decision; significant legislation passing that directly affects real estate, housing finance, or capital markets; major economic data miss that moves markets significantly; black swan events — natural disasters, political crises, military escalation with global implications. What does NOT belong: routine economic data releases, political news without direct economic impact, company earnings unless market-moving, real estate deal announcements, technology news, fund flows, bank earnings. Stories appearing in Critical News should not be repeated below — reference with 'as noted above' only when essential. Include market context inline when a critical story has direct market implications — do not duplicate that context in the Markets section.

Macro & Geopolitics: Cap at 3 stories with discrete bold sub-headers. Weave Economist analysis into this section when relevant — attribute inline as '(The Economist)' after the relevant sentence or paragraph. Include market context inline when a macro story has direct market implications — note rates, spreads, or commodity impacts in 1 sentence within the story rather than saving it for the Markets section. Stories that appeared in Critical News above should not be repeated here — skip or use 'as noted above'. Focus on developments that affect global business conditions, trade, geopolitical stability, and capital flows — not political drama for its own sake.

US News: Enforce a strict 50/50 political vs non-political split: maximum 1 political story per day, minimum 1 non-political story per day. Non-political stories can be: technology policy, public health, infrastructure, education, climate, science, culture, criminal justice, sports policy, demographic trends, labor market developments, immigration economic impact, housing policy. Political stories must have genuine policy or economic relevance — not political drama or partisan conflict for its own sake. Weave Economist US analysis inline when relevant — attribute as '(The Economist)'. Skip section entirely if nothing clears the bar.

Markets: Use 3-5 tight bullet points — not a narrative paragraph. Each bullet states what happened and what it means for real estate capital markets in one sentence. Lead with the most impactful item. End with one bolded line: '<strong>Rate to watch:</strong> 10-year Treasury at [X]%, [direction] week-over-week.' What belongs: interest rate moves and Fed decisions affecting borrowing costs; credit market conditions — spreads, lending standards, construction and permanent financing availability; oil, commodity, and inflation data affecting construction costs or operating expenses; currency moves significant enough to affect capital flows into US real estate; equity market moves only if severe enough to signal recession risk or affect institutional investor appetite. What does NOT belong: fund flows and fund manager positioning; bank earnings unless signaling credit crisis; individual stock moves; crypto; routine weekly data unless significantly surprising; anything already covered in Critical News or Macro with market context. Write for a real estate operator — what does this mean for cost of capital, cap rates, investor appetite, construction budget? Not for a trader or portfolio manager. Source actively from FT, WSJ, Bloomberg, and CNBC — attribute in the inline sources line. FT and WSJ links: render as plain text with (subscription required) — never hyperlink these.

Real Estate & Affordable Housing: Policy, LIHTC, and market-level trends only. Do not include specific deal announcements unless they pass a significance test: does this deal signal a market trend? Does it involve a major institutional player making a notable move? Does it involve a policy, structure, or financing mechanism that is unusual or instructive? Would a senior real estate PE professional at a competitor firm find this genuinely notable? A routine construction loan closing is not notable. A financing closing under a new HUD program that signals a policy shift might be notable. Err toward exclusion. Weave Economist real estate or housing analysis inline when available — attribute as '(The Economist)'. Legislation and LIHTC policy get full treatment. Macro trends get one line.

Economist integration: The Economist content is provided as a pre-selected article. Do not create a standalone Economist section. Instead, integrate Economist analysis inline throughout the digest wherever it is most relevant: geopolitical analysis goes in Macro; US policy analysis goes in US News; housing or real estate analysis goes in Real Estate; markets analysis goes in Markets; science or technology goes in Science & Health or AI & Tech. Attribution: end the relevant sentence or paragraph with '(The Economist)' in parentheses. If the article does not fit any current section's content, include it as an additional item in the most relevant section.

One Thing to Learn Today: Write one practical insight that expands Jeff's thinking — not something obvious to someone in his position. 3-5 sentences. Connect to today's digest when natural but do not force the connection. The topic history is provided in the data — avoid sub-topics used recently. After the insight text, include this HTML comment so the topic can be tracked: <!-- LEARN_SUBTOPIC: [subtopic_key] --> where subtopic_key is a snake_case string identifying the sub-topic (e.g. 'multifamily_market_dynamics', 'macroeconomics_concept', 'philosophy_decision_making', 'construction_finance', 'capital_markets_concept', 'science_health_insight', 'history_geopolitics', 'leadership_management').

Worth Your Time is Jeff's curated reading shelf — not a news feed extension. Every item must make him meaningfully smarter, wiser, or better informed in a way that sticks. The section draws from three pools: LIVE (recent feed items), ARCHIVE (thinker blogs), and EVERGREEN LIBRARY (pre-vetted classics). Each day has a tier: EVERGREEN (Tier 1 — shelf life 50+ years), DURABLE (Tier 2 — 1-3 years), TOPICAL (Tier 3 — 1-4 weeks). Today's tier and under-quota topics are in the candidate pool header — prioritize accordingly. For topics outside Jeff's domain (science, philosophy, history, linguistics), prefer journalism over academia, narrative over jargon. Select exactly 1 item. The Evergreen Library always has something worthy — never leave Worth Your Time empty.

Newsletter content from GZero and The Promote will be labeled as EMAIL SOURCE. Treat these with the same weight as RSS feed content. GZero content belongs in the Macro & Geopolitics section. The Promote content belongs in the Real Estate & Affordable Housing section.

Worth Your Time free-content rule: Every item in Worth Your Time must be completely free to access without any subscription, login, or paywall. You must be 100% certain an item is freely accessible before including it. Do not include any item from WSJ, FT, Bloomberg, NYT, The Atlantic, New Yorker, Foreign Affairs, HBR, Washington Post, or any other publication that requires a subscription. Strong free sources include: Noahpinion free posts, Aeon, Nautilus, Quanta Magazine, Huberman Lab, Invest Like the Best episode pages, Farnam Street free articles, Wait But Why, The Marginalian, Daily Stoic, Project Syndicate free articles, VoxEU, Popular Science, Popular Mechanics, Stat News, New Scientist free articles, Ars Technica, and any open access research.

Links policy: Whenever you recommend that the reader check something, visit a source, or look something up — always provide a direct hyperlink to that specific resource. Never say 'it would be worth checking X' or 'see Y directly' without including the URL as a clickable link. If you do not have the specific URL for a resource, do not recommend it. Only recommend things you can link to directly.

Inline sources: At the end of each section (Critical News, Markets, Macro & Geopolitics, US News, Real Estate, Research & Market Intelligence, AI & Technology, Science & Health), output a compact sources line listing only the outlets whose content was actually used in that section. Format exactly as:
<p style="margin:8px 0 0;font-size:11px;color:#aaa;border-top:1px solid #f0f0f0;padding-top:6px;">Sources: [Outlet 1] · [Outlet 2] · [Outlet 3]</p>
Use short outlet names (e.g. FT, Bloomberg, NPR, NYT, Reuters). Only list outlets that contributed at least one story to that section. Do not add a sources line to the One Thing to Learn Today, Worth Your Time, or Recent Releases sections. Do not output a separate Sources & References section at the bottom.

Never note, mention, or explain when content has been excluded, filtered, or is unavailable. Do not write phrases like 'No deal news met the threshold today', 'Nothing in this category cleared the bar', 'The Economist had no relevant content', 'No critical news today', 'This section is intentionally brief', or any similar language. If a section has no content simply omit it entirely with no explanation. If a sub-category within a section has nothing skip it silently. The reader should never know what was considered and rejected.\
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
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            url = item.get("link") or item.get("url") or ""
            title = item.get("title", "")
            if url and title:
                url_to_title[url] = title

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
    """Fetch every entry from The Economist across multiple section feeds.
    The full RSS feed (the_economist_full_rss.xml) is dead as of 2025.
    We now pull from individual section feeds which are publicly accessible.
    Uses ECONOMIST_SESSION_COOKIE from .env for full article text when set.
    """
    ECONOMIST_SECTION_FEEDS = [
        "https://www.economist.com/leaders/rss.xml",
        "https://www.economist.com/briefing/rss.xml",
        "https://www.economist.com/finance-and-economics/rss.xml",
        "https://www.economist.com/the-world-this-week/rss.xml",
        "https://www.economist.com/business/rss.xml",
        "https://www.economist.com/international/rss.xml",
    ]

    items = []
    seen_links: set[str] = set()
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

    for feed_url in ECONOMIST_SECTION_FEEDS:
        try:
            feed = feedparser.parse(feed_url, request_headers=headers)
            feed_items = 0
            for entry in feed.entries[:10]:  # cap at 10 per section — plenty for selection
                link = getattr(entry, "link", "")
                if not link or link in seen_links:
                    continue
                seen_links.add(link)
                pub = _parse_entry_date(entry)
                # Use feed summary only — per-article fetches are too slow (300+ items)
                summary = (getattr(entry, "summary", "") or "")[:600]
                items.append({
                    "source":    "The Economist",
                    "title":     getattr(entry, "title", ""),
                    "link":      link,
                    "summary":   summary,
                    "published": pub.isoformat() if pub else "",
                })
                feed_items += 1
            log.info("Economist feed %s: %d item(s)", feed_url.split("/")[-2], feed_items)
        except Exception as exc:
            log.error("Failed to fetch Economist feed %s: %s", feed_url, exc)

    log.info("The Economist total: %d unique item(s)", len(items))
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

# ── Feed fallback map (self-healing) ─────────────────────────────────────────
# When a feed URL fails, we try these alternatives in order.
# If one works, sources.yaml is updated automatically for future runs.

FEED_FALLBACKS: dict[str, list[str]] = {
    # Bloomberg
    "https://feeds.bloomberg.com/markets/news.rss":       ["https://feeds.bloomberg.com/markets/news.rss"],
    "https://feeds.bloomberg.com/economics/news.rss":     ["https://feeds.bloomberg.com/economics/news.rss"],
    "https://feeds.bloomberg.com/podcast/odd-lots.xml":   ["https://feeds.megaphone.fm/GLT1412515089"],
    # Reuters
    "https://feeds.reuters.com/reuters/topNews":          ["https://feeds.npr.org/1001/rss.xml"],
    "https://feeds.reuters.com/reuters/financialsNews":   ["https://feeds.bloomberg.com/economics/news.rss"],
    # Podcasts
    "https://feeds.simplecast.com/SFm2B67j":              ["https://feeds.megaphone.fm/investlikethebest"],
    "https://feeds.libsyn.com/233774/rss":                ["https://feeds.megaphone.fm/hubermanlab"],
    # MIT Tech Review feedburner
    "https://feeds.feedburner.com/mittechnologyreview":   ["https://www.technologyreview.com/feed/"],
    # Anthropic
    "https://www.anthropic.com/rss.xml":                  ["https://www.bensbites.com/feed"],
    # GZero
    "https://www.gzeromedia.com/feed":                    ["https://feeds.npr.org/1001/rss.xml"],
    # Affordable Housing Finance
    "https://www.housingfinance.com/rss.xml":             ["https://www.multifamilydive.com/feeds/news/"],
    # The Promote
    "https://www.thepromotenewsletter.com/feed":          ["https://www.bisnow.com/rss/national"],
    # Rundown AI
    "https://www.therundown.ai/rss":                      ["https://www.bensbites.com/feed"],
    # CoStar
    "https://www.costar.com/rss/news":                    ["https://www.globest.com/rss/"],
    # The Real Deal
    "https://therealdeal.com/feed/":                      ["https://www.bisnow.com/rss/national"],
    # GlobeSt
    "https://www.globest.com/rss/":                       ["https://www.connectcre.com/feed/"],
    # Economist full RSS (dead)
    "https://www.economist.com/rss/the_economist_full_rss.xml": [
        "https://www.economist.com/leaders/rss.xml"
    ],
}


def _probe_url(url: str) -> bool:
    """Return True if the URL returns a valid RSS/Atom feed."""
    import urllib.request, ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8, context=ctx) as r:
            chunk = r.read(512)
            return any(tag in chunk for tag in (b"<rss", b"<feed", b"<channel"))
    except Exception:
        return False


def _heal_sources_yaml(old_url: str, new_url: str, sources_path: str = "sources.yaml") -> None:
    """Replace old_url with new_url in sources.yaml and save."""
    try:
        with open(sources_path, encoding="utf-8") as f:
            text = f.read()
        if old_url in text:
            text = text.replace(old_url, new_url)
            with open(sources_path, "w", encoding="utf-8") as f:
                f.write(text)
            log.warning("AUTO-HEALED sources.yaml: %s → %s", old_url, new_url)
    except Exception as exc:
        log.error("Failed to auto-heal sources.yaml: %s", exc)


def _feed_is_healthy(url: str) -> bool:
    """Quick check: does feedparser get at least one entry from this URL?"""
    try:
        feed = feedparser.parse(url)
        return len(feed.entries) > 0
    except Exception:
        return False


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
    """Return items from one RSS feed published within lookback_hours.
    If the feed fails, attempts fallback URLs from FEED_FALLBACKS and
    auto-patches sources.yaml on success.
    """
    items  = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    url    = source["url"]

    def _parse_entries(feed_url: str) -> list[dict]:
        result = []
        feed = feedparser.parse(feed_url)
        if not feed.entries:
            return result
        for entry in feed.entries:
            pub = _parse_entry_date(entry)
            if pub is None or pub < cutoff:
                continue
            result.append({
                "source":    source["name"],
                "title":     getattr(entry, "title", ""),
                "link":      getattr(entry, "link", ""),
                "summary":   (getattr(entry, "summary", "") or "")[:600],
                "published": pub.isoformat(),
            })
        return result

    try:
        items = _parse_entries(url)
        if items or _feed_is_healthy(url):
            log.info("%-30s  %d item(s) in last %dh", source["name"], len(items), lookback_hours)
            return items
        # Feed returned no entries at all — treat as broken
        raise ValueError("feed returned 0 entries")
    except Exception as exc:
        log.warning("Feed unhealthy %-30s  %s — trying fallbacks", source["name"], exc)

    # Try fallback URLs
    for fallback_url in FEED_FALLBACKS.get(url, []):
        if fallback_url == url:
            continue
        log.info("  trying fallback: %s", fallback_url)
        try:
            items = _parse_entries(fallback_url)
            log.warning("  FALLBACK OK: %s — auto-healing sources.yaml", fallback_url)
            _heal_sources_yaml(url, fallback_url)
            source["url"] = fallback_url  # update in-memory for this run
            return items
        except Exception as fb_exc:
            log.warning("  fallback failed: %s  %s", fallback_url, fb_exc)

    log.error("All URLs failed for %-30s — section will be empty for this feed", source["name"])
    return []

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

# ── WYT — New System: topic rotation, accessibility filter, evergreen library ──

WYT_LIBRARY_FILE  = Path(__file__).parent / "wyt_library.yaml"
WYT_HISTORY_FILE  = LOG_DIR / "wyt_history.json"
WYT_RATINGS_FILE  = LOG_DIR / "wyt_ratings.json"

# Day-of-week → tier (Monday=0 … Sunday=6)
_WYT_DAY_TIER: dict[int, int] = {
    0: 2,  # Monday
    1: 3,  # Tuesday
    2: 1,  # Wednesday
    3: 2,  # Thursday
    4: 3,  # Friday
    5: 1,  # Saturday
    6: 2,  # Sunday
}

_TIER_NAMES = {1: "EVERGREEN", 2: "DURABLE", 3: "TOPICAL"}

# 14-day quota targets per category
_WYT_QUOTA_14D: dict[str, tuple[int, int]] = {
    # category: (min_target, max_target)
    "philosophy_stoicism":   (3, 3),
    "philosophy_other":      (1, 1),
    "investing_finance":     (2, 3),
    "hard_science":          (2, 2),
    "applied_science_tech":  (2, 2),
    "explainer":             (2, 6),
    "human_performance":     (1, 2),
    "ideas_mental_models":   (2, 2),
    "real_estate_adjacent":  (1, 1),
    "viral_topical":         (1, 1),
    "surprise":              (1, 1),
}


def get_today_tier(today: datetime | None = None) -> tuple[int, str]:
    """Return (tier_number, tier_name) for today's day of week."""
    if today is None:
        today = datetime.now(timezone.utc)
    tier = _WYT_DAY_TIER[today.weekday()]
    return tier, _TIER_NAMES[tier]


def load_wyt_history() -> dict:
    """Return the WYT history dict from wyt_history.json."""
    if not WYT_HISTORY_FILE.exists():
        return {"featured": [], "topic_counts_14d": {}, "source_counts_7d": {},
                "source_appearances_7d": {}, "explainer_count_28d": 0, "last_updated": ""}
    try:
        with open(WYT_HISTORY_FILE, encoding="utf-8") as f:
            data = json.load(f)
        # Backfill field if missing from older history files
        data.setdefault("source_appearances_7d", {})
        return data
    except Exception as exc:
        log.warning("Could not read wyt_history.json: %s — starting fresh", exc)
        return {"featured": [], "topic_counts_14d": {}, "source_counts_7d": {},
                "source_appearances_7d": {}, "explainer_count_28d": 0, "last_updated": ""}


def save_wyt_history(history: dict) -> None:
    WYT_HISTORY_FILE.parent.mkdir(exist_ok=True)
    with open(WYT_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)
    log.info("Saved wyt_history.json")


def load_wyt_library() -> list[dict]:
    """Load items from wyt_library.yaml. Returns empty list on failure."""
    if not WYT_LIBRARY_FILE.exists():
        log.warning("wyt_library.yaml not found")
        return []
    try:
        with open(WYT_LIBRARY_FILE, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data.get("items", [])
    except Exception as exc:
        log.warning("Could not load wyt_library.yaml: %s", exc)
        return []


def save_wyt_library(items: list[dict]) -> None:
    try:
        with open(WYT_LIBRARY_FILE, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        data["items"] = items
        with open(WYT_LIBRARY_FILE, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
    except Exception as exc:
        log.warning("Could not save wyt_library.yaml: %s", exc)


def load_wyt_ratings() -> dict:
    """Return the ratings data from wyt_ratings.json."""
    if not WYT_RATINGS_FILE.exists():
        return {"ratings": [], "category_scores": {}, "source_scores": {}}
    try:
        with open(WYT_RATINGS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"ratings": [], "category_scores": {}, "source_scores": {}}


def calculate_topic_quotas(history: dict) -> tuple[list[str], list[str]]:
    """
    Read the 14-day featured history and return:
      under_quota: categories below their minimum target
      over_quota:  categories at or above their maximum target
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    recent_featured = [
        f for f in history.get("featured", [])
        if f.get("date", "") >= cutoff.strftime("%Y-%m-%d")
    ]
    counts: dict[str, int] = {}
    for entry in recent_featured:
        cat = entry.get("category", "")
        if cat:
            counts[cat] = counts.get(cat, 0) + 1

    under_quota: list[str] = []
    over_quota:  list[str] = []
    for cat, (min_t, max_t) in _WYT_QUOTA_14D.items():
        c = counts.get(cat, 0)
        if c < min_t:
            under_quota.append(cat)
        if c >= max_t:
            over_quota.append(cat)

    log.info("WYT topic counts (14d): %s", counts)
    log.info("WYT under-quota: %s", under_quota)
    log.info("WYT over-quota: %s", over_quota)
    return under_quota, over_quota


def filter_by_accessibility(items: list[dict]) -> list[dict]:
    """Remove expert-only items from the candidate pool."""
    return [i for i in items if i.get("accessibility", "general") != "expert-only"]


def apply_rating_boost(items: list[dict], ratings: dict) -> list[dict]:
    """
    Adjust item priority_score based on category ratings:
      - category avg > 0.5: boost 20%
      - category avg < -0.3: reduce 20%
      - cumulative_rating < -2: exclude for 60 days
    """
    cat_scores = ratings.get("category_scores", {})
    result = []
    for item in items:
        cr = item.get("cumulative_rating", 0)
        if cr < -2:
            # Check if we should exclude
            # Use times_featured as a proxy — if it's a library item, skip
            if item.get("_library_item"):
                log.info("WYT exclude (low rating): %s", item.get("title", "")[:60])
                continue
        cat = item.get("category", "")
        score_data = cat_scores.get(cat, {})
        count = score_data.get("count", 0)
        if count > 0:
            avg = score_data["total"] / count
            if avg > 0.5:
                item["_priority_boost"] = 1.2
            elif avg < -0.3:
                item["_priority_boost"] = 0.8
        result.append(item)
    return result


def get_eligible_library_items(
    library: list[dict],
    history: dict,
    over_quota: list[str],
) -> list[dict]:
    """
    Filter library items:
    - Remove items within their cooldown window
    - Remove items in over-quota categories
    - Sort: under-quota categories first, then by cumulative_rating/max(times_featured,1)
    """
    now = datetime.now(timezone.utc)
    eligible = []
    for item in library:
        if item.get("accessibility") == "expert-only":
            continue
        url = item.get("url", "")
        cat = item.get("category", "")
        if cat in over_quota:
            continue
        cooldown = item.get("cooldown_days", 21)
        # Check if item appeared in history within cooldown
        cutoff_str = (now - timedelta(days=cooldown)).strftime("%Y-%m-%d")
        recently_used = any(
            f.get("url") == url and f.get("date", "") >= cutoff_str
            for f in history.get("featured", [])
        )
        if recently_used:
            log.info("WYT library cooldown: %s", item.get("title", "")[:60])
            continue
        item["_library_item"] = True
        eligible.append(item)

    # Sort: prioritize items whose category is under-quota
    def _sort_key(item):
        cat = item.get("category", "")
        tf  = max(item.get("times_featured", 0), 1)
        cr  = item.get("cumulative_rating", 0)
        under = -1 if cat in [c for c, (mn, _) in _WYT_QUOTA_14D.items() if mn > 0] else 0
        return (under, -(cr / tf))

    eligible.sort(key=_sort_key)
    return eligible


def record_wyt_selections(
    digest_html: str,
    wyt_candidates: list[dict],
    history: dict,
    library: list[dict],
) -> None:
    """
    After digest generation, find WYT section URLs in the HTML,
    match against candidates, record in wyt_history.json,
    and update times_featured in wyt_library.yaml.
    """
    # Extract the WYT section from the HTML
    wyt_section_match = re.search(
        r'Worth Your Time.*?(?=<h2[^>]*>(?!.*Worth Your Time)|$)',
        digest_html, re.DOTALL | re.IGNORECASE
    )
    if not wyt_section_match:
        log.info("WYT record: could not isolate WYT section in HTML")
        wyt_section = digest_html  # fall back to full HTML
    else:
        wyt_section = wyt_section_match.group(0)

    # Build URL→candidate map
    url_to_candidate: dict[str, dict] = {}
    for item in wyt_candidates:
        url = item.get("url") or item.get("link") or ""
        if url:
            url_to_candidate[url] = item

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    found_urls: list[str] = []
    for href in re.findall(r'href="([^"]+)"', wyt_section):
        if href in url_to_candidate and href not in found_urls:
            found_urls.append(href)

    if not found_urls:
        log.info("WYT record: no candidate URLs found in WYT section")
        return

    log.info("WYT record: recording %d selection(s)", len(found_urls))
    library_url_to_idx = {item.get("url", ""): i for i, item in enumerate(library)}

    for url in found_urls:
        candidate = url_to_candidate[url]
        entry = {
            "url":      url,
            "title":    candidate.get("title", ""),
            "category": candidate.get("category", ""),
            "source":   candidate.get("source", ""),
            "tier":     candidate.get("tier", 2),
            "date":     today_str,
        }
        history.setdefault("featured", []).append(entry)
        log.info("WYT recorded: %s", candidate.get("title", "")[:80])

        # Update library times_featured
        lib_idx = library_url_to_idx.get(url)
        if lib_idx is not None:
            library[lib_idx]["times_featured"] = library[lib_idx].get("times_featured", 0) + 1

    # Prune history older than 28 days
    cutoff_str = (datetime.now(timezone.utc) - timedelta(days=28)).strftime("%Y-%m-%d")
    history["featured"] = [
        f for f in history.get("featured", [])
        if f.get("date", "") >= cutoff_str
    ]
    history["last_updated"] = today_str


# ── WYT 1-year archive fetch ──────────────────────────────────────────────────
# Keep WYT_ARCHIVE_SOURCES and fetch_wyt_archive() — used for Pool B


# ── WYT 1-year archive fetch ──────────────────────────────────────────────────
# Sources whose long-form content is worth surfacing up to 1 year back.
# These are fetched separately from the normal news feeds — the long lookback
# is only used for WYT candidate building, not for the digest sections.

WYT_ARCHIVE_SOURCES = [
    {"name": "Naval Ravikant",            "url": "https://nav.al/feed"},
    {"name": "Naval Ravikant (Substack)", "url": "https://naval.substack.com/feed"},
    {"name": "Tim Ferriss",               "url": "https://tim.blog/feed/"},
    {"name": "Wait But Why (Tim Urban)",  "url": "https://waitbutwhy.com/feed"},
    {"name": "Ray Dalio",                 "url": "https://medium.com/feed/@raydalio"},
    {"name": "Ryan Holiday",              "url": "https://ryanholiday.net/feed/"},
    {"name": "Daily Stoic (Ryan Holiday)","url": "https://dailystoic.com/feed/"},
    {"name": "Noahpinion",                "url": "https://www.noahpinion.blog/feed"},
    {"name": "Farnam Street",             "url": "https://fs.blog/feed/"},
    {"name": "Paul Graham",               "url": "https://feeds.feedburner.com/PaulGrahamEssays"},
]


def fetch_wyt_archive() -> list[dict]:
    """Fetch posts from long-form thinker sources for WYT.
    No date cutoff — these sources are selected for staying power, so older
    posts are just as valid as recent ones. The per-source weekly cap and
    7-day article dedup prevent repetition.
    """
    items: list[dict] = []
    seen: set[str] = set()

    for source in WYT_ARCHIVE_SOURCES:
        try:
            feed = feedparser.parse(source["url"])
            count = 0
            for entry in feed.entries:
                link = getattr(entry, "link", "")
                if not link or link in seen:
                    continue
                seen.add(link)
                pub = _parse_entry_date(entry)
                items.append({
                    "source":    source["name"],
                    "title":     getattr(entry, "title", ""),
                    "link":      link,
                    "summary":   (getattr(entry, "summary", "") or "")[:600],
                    "published": pub.isoformat() if pub else "",
                    "wyt_archive": True,
                })
                count += 1
            log.info("WYT archive %-30s  %d item(s)", source["name"], count)
        except Exception as exc:
            log.warning("WYT archive fetch failed %-25s  %s", source["name"], exc)

    log.info("WYT archive total: %d item(s)", len(items))
    return items



# ── Learn history (One Thing to Learn Today topic rotation) ───────────────────

LEARN_HISTORY_FILE = LOG_DIR / "learn_history.json"

_LEARN_REAL_ESTATE_SUBTOPICS = [
    "multifamily_market_dynamics",
    "construction_finance",
    "capital_markets_debt_structures",
    "property_management_operations",
    "affordable_housing_policy",
    "cre_adjacent_sectors",
    "real_estate_investment_structures",
    "underwriting_valuation",
    "zoning_land_use",
    "real_estate_history_cycles",
]

_LEARN_OTHER_SUBTOPICS = [
    "macroeconomics_concept",
    "capital_markets_concept",
    "technology_ai_business",
    "science_health_insight",
    "history_geopolitics",
    "philosophy_decision_making",
    "leadership_management",
]


def load_learn_history() -> dict:
    if not LEARN_HISTORY_FILE.exists():
        return {"entries": [], "real_estate_count_14d": 0,
                "other_count_14d": 0, "subtopic_last_used": {}}
    try:
        with open(LEARN_HISTORY_FILE, encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("entries", [])
        data.setdefault("real_estate_count_14d", 0)
        data.setdefault("other_count_14d", 0)
        data.setdefault("subtopic_last_used", {})
        return data
    except Exception as exc:
        log.warning("Could not read learn_history.json: %s — starting fresh", exc)
        return {"entries": [], "real_estate_count_14d": 0,
                "other_count_14d": 0, "subtopic_last_used": {}}


def save_learn_history(history: dict) -> None:
    LEARN_HISTORY_FILE.parent.mkdir(exist_ok=True)
    with open(LEARN_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)
    log.info("Saved learn_history.json")


def compute_learn_context(history: dict) -> str:
    """Build a text block summarizing recent learn topics for the prompt."""
    now = datetime.now(timezone.utc)
    cutoff_7d  = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    cutoff_14d = (now - timedelta(days=14)).strftime("%Y-%m-%d")

    recent_7d  = [e for e in history.get("entries", []) if e.get("date", "") >= cutoff_7d]
    recent_14d = [e for e in history.get("entries", []) if e.get("date", "") >= cutoff_14d]

    subtopics_7d  = [e.get("subtopic", "") for e in recent_7d if e.get("subtopic")]
    re_count_14d  = sum(1 for e in recent_14d if e.get("category") == "real_estate")
    oth_count_14d = sum(1 for e in recent_14d if e.get("category") == "other")

    avoid_str = ", ".join(subtopics_7d) if subtopics_7d else "none"
    lines = [
        "=== ONE THING TO LEARN — TOPIC HISTORY (inform selection, avoid repetition) ===",
        f"Sub-topics used in last 7 days (avoid repeating): {avoid_str}",
        f"Real estate sub-topics used in last 14 days: {re_count_14d} of 7 target",
        f"Other sub-topics used in last 14 days: {oth_count_14d} of 7 target",
    ]
    if re_count_14d < oth_count_14d:
        lines.append("Preference: lean toward a real estate sub-topic today to rebalance.")
    elif oth_count_14d < re_count_14d:
        lines.append("Preference: lean toward a non-real-estate sub-topic today to rebalance.")
    return "\n".join(lines)


def record_learn_subtopic(digest_html: str, history: dict) -> None:
    """Parse <!-- LEARN_SUBTOPIC: key --> from HTML and record in learn_history."""
    match = re.search(r'<!--\s*LEARN_SUBTOPIC:\s*([\w]+)\s*-->', digest_html)
    if not match:
        log.info("learn_history: no LEARN_SUBTOPIC comment found in HTML")
        return
    subtopic = match.group(1).strip()
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    category = "real_estate" if any(
        subtopic.startswith(s.split("_")[0]) for s in _LEARN_REAL_ESTATE_SUBTOPICS
    ) else "other"
    # More precise mapping
    if subtopic in _LEARN_REAL_ESTATE_SUBTOPICS:
        category = "real_estate"
    elif subtopic in _LEARN_OTHER_SUBTOPICS:
        category = "other"
    entry = {"subtopic": subtopic, "category": category, "date": today_str}
    history.setdefault("entries", []).append(entry)
    # Prune to 28 days
    cutoff = (datetime.now(timezone.utc) - timedelta(days=28)).strftime("%Y-%m-%d")
    history["entries"] = [e for e in history["entries"] if e.get("date", "") >= cutoff]
    history["subtopic_last_used"][subtopic] = today_str
    log.info("learn_history: recorded subtopic '%s' (%s)", subtopic, category)

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


def compute_wyt_source_cap_exceeded(history: dict) -> set:
    """Return set of source names that have appeared >= 2 times in WYT in last 7 days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    counts: dict[str, int] = {}
    for entry in history.get("featured", []):
        if entry.get("date", "") >= cutoff and entry.get("source"):
            src_name = entry["source"]
            counts[src_name] = counts.get(src_name, 0) + 1
    exceeded = {s for s, c in counts.items() if c >= 2}
    if exceeded:
        log.info("WYT source cap exceeded (2/7d): %s", exceeded)
    return exceeded

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
            "=== THE ECONOMIST (integrate inline — do NOT create a standalone Economist section) ===\n"
            f"SOURCE: {e['source']}\n"
            f"TITLE: {e['title']}\n"
            f"URL: {e['link']}\n"
            f"SUMMARY: {e['summary']}\n"
        )
    else:
        econ_block = None

    # Email newsletter block (GZero, The Promote, etc.)
    email_items = content.get("email_newsletters", [])
    if email_items:
        email_block = f"=== EMAIL NEWSLETTER SOURCES (GZero, The Promote, etc.) ===\n{_format_email_items(email_items)}"
    else:
        email_block = None

    raw_blocks = [
        section("MARKETS (Bloomberg, WSJ, FT, CNBC)", "markets"),
        section("MACRO & GEOPOLITICS (FT, Bloomberg, NPR, NYT World, Foreign Affairs, The Atlantic)", "macro_geopolitics"),
        section("US NEWS (NPR, NYT, The Hill, Axios)", "us_news"),
        section("REAL ESTATE & AFFORDABLE HOUSING (The Real Deal, Multifamily Dive, Bisnow, ConnectCRE, Jay Parsons)", "real_estate"),
        section(
            "RESEARCH & MARKET INTELLIGENCE "
            "(Goldman Sachs, Morgan Stanley, JPMorgan, BlackRock, "
            "CBRE, JLL, Newmark, Berkadia, Marcus & Millichap, "
            "Bloomberg Economics, RealPage, GlobeSt)",
            "research_intel",
        ),
        section(
            "AI & TECHNOLOGY "
            "(Ben's Bites, MIT Technology Review, Ars Technica)",
            "ai_tech",
        ),
        section("SCIENCE & HEALTH (New Scientist, Stat News, Nature, NYT Science, Popular Mechanics, Popular Science)", "science_health"),
        section("RECENT RELEASES — PODCASTS & NEWSLETTERS (72-hour window)", "podcasts_newsletters"),
        section("THINKERS & PERSONAL BLOGS — PRIORITY WORTH YOUR TIME CANDIDATES (Naval Ravikant, Tim Ferriss, Tim Urban, Ray Dalio, Ryan Holiday / Daily Stoic — 7-day window)", "thinkers"),
    ]
    if econ_block:
        raw_blocks.append(econ_block)
    if email_block:
        raw_blocks.insert(0, email_block)

    # ── Build WYT candidate pool ──────────────────────────────────────────────
    wyt_seen: set[str] = set()

    # Metadata injected by main()
    wyt_tier, wyt_tier_name   = content.get("_wyt_tier", (2, "DURABLE"))
    wyt_under_quota: list[str] = content.get("_wyt_under_quota", [])
    wyt_over_quota:  list[str] = content.get("_wyt_over_quota", [])
    # Sources that have already appeared >= 2x in WYT in the last 7 days
    wyt_source_cap_exceeded: set[str] = content.get("_wyt_source_cap_exceeded", set())

    # Pool A: Live feed items (free, from today's news feeds)
    pool_a: list[dict] = []
    for section_key, section_items in content.items():
        if section_key.startswith("_") or not isinstance(section_items, list):
            continue
        for item in section_items:
            if not isinstance(item, dict):
                continue
            url = item.get("link") or item.get("url") or ""
            cat = item.get("category", "")
            src_name = item.get("source", "")
            if url and url not in wyt_seen and _is_free_for_wyt(url):
                if cat not in wyt_over_quota and src_name not in wyt_source_cap_exceeded:
                    wyt_seen.add(url)
                    pool_a.append(item)

    # Pool B: Archive (thinker blogs — WYT_ARCHIVE_SOURCES, up to 1 year old)
    pool_b: list[dict] = []
    for item in content.get("_wyt_archive", []):
        url = item.get("link", "")
        cat = item.get("category", "")
        src_name = item.get("source", "")
        if url and url not in wyt_seen and _is_free_for_wyt(url):
            if cat not in wyt_over_quota and src_name not in wyt_source_cap_exceeded:
                wyt_seen.add(url)
                pool_b.append(item)

    # Pool C: Evergreen library (wyt_library.yaml, already filtered by cooldown)
    pool_c_raw: list[dict] = content.get("_wyt_library", [])
    pool_c: list[dict] = []
    for item in pool_c_raw:
        url = item.get("url", "")
        src_name = item.get("source", "")
        if url and url not in wyt_seen:
            # Allow library items even if source cap exceeded — library is guaranteed fallback
            wyt_seen.add(url)
            pool_c.append(item)

    # Store all candidates for post-processing in main()
    content["_wyt_all_candidates"] = pool_a + pool_b + pool_c

    def _fmt_wyt_item(item: dict, pool_label: str = "") -> str:
        lines = [
            f"SOURCE: {item.get('source', '')}",
            f"TITLE: {item.get('title', '')}",
            f"URL: {item.get('link') or item.get('url', '')}",
            f"SUMMARY: {item.get('summary') or item.get('why_worth_reading', '')}",
        ]
        if item.get("published"):
            lines.append(f"PUBLISHED: {item['published'][:10]}")
        if pool_label:
            lines.append(f"POOL: {pool_label}")
        duration = item.get("duration") or (
            f"{item.get('estimated_read_minutes')} min read"
            if item.get("estimated_read_minutes") else ""
        )
        if duration:
            lines.append(f"DURATION: {duration}")
        cat = item.get("category", "")
        if cat:
            lines.append(f"CATEGORY: {cat}")
        tier = item.get("tier", "")
        if tier:
            lines.append(f"ITEM_TIER: {tier}")
        acc = item.get("accessibility", "general")
        if acc and acc != "general":
            lines.append(f"ACCESSIBILITY: {acc}")
        return "\n".join(lines)

    pool_a_block = "\n\n".join(
        _fmt_wyt_item(i, "LIVE — recent from news feeds") for i in pool_a[:30]
    )
    pool_b_block = "\n\n".join(
        _fmt_wyt_item(i, "ARCHIVE — thinker blogs, long-form") for i in pool_b[:25]
    )
    pool_c_block = "\n\n".join(
        _fmt_wyt_item(i, "EVERGREEN LIBRARY — pre-vetted classics") for i in pool_c[:20]
    )

    under_quota_str = (
        ", ".join(wyt_under_quota).replace("_", " ") if wyt_under_quota else "none"
    )

    learn_context = content.get("_learn_context", "")
    if learn_context:
        raw_blocks.append(learn_context)

    wyt_block = (
        f"=== WORTH YOUR TIME CANDIDATE POOL ===\n"
        f"TODAY'S TIER: {wyt_tier} — {wyt_tier_name}\n"
        f"DAY: {today.strftime('%A')}\n"
        f"UNDER-QUOTA TOPICS (prioritize these): {under_quota_str}\n"
    )
    if pool_a_block:
        wyt_block += f"\n-- POOL A: LIVE (recent feed items, free only) --\n{pool_a_block}\n"
    if pool_b_block:
        wyt_block += f"\n-- POOL B: ARCHIVE (thinker blogs, Naval/Ferriss/Holiday/Farnam St) --\n{pool_b_block}\n"
    if pool_c_block:
        wyt_block += f"\n-- POOL C: EVERGREEN LIBRARY (curated, high cooldown) --\n{pool_c_block}\n"
    if not any([pool_a_block, pool_b_block, pool_c_block]):
        wyt_block += "(no candidates available — this should not happen)\n"

    raw_blocks.append(wyt_block)

    raw_content = "\n\n".join(raw_blocks)

    return f"""\
Today is {today.strftime("%A, %B %d, %Y")}.

Below is every RSS item collected in the last 24–72 hours (varies by source).
Use only URLs that appear verbatim in this data — never fabricate links.

{raw_content}

─────────────────────────────────────────────────────────────────────────────
DIGEST SECTIONS TO PRODUCE (in this order):

LENGTH RULE: Write every section at 75% of what you would normally produce. Cut every sentence that restates, qualifies, or hedges something already stated. One idea, one sentence.

1. 🚨 Critical News — Include only if something genuinely critical happened today. Maximum 3 items, minimum 0. See system prompt for what clears the bar. Bold sub-header per item. If nothing qualifies, omit entirely with no explanation.

2. 🌍 Macro & Geopolitics — Up to 3 stories. Bold sub-header per story. 2 sentences each max. Weave Economist analysis inline with attribution. Include market context inline when directly relevant.

3. 🇺🇸 US News — Max 1 political story, min 1 non-political story. Political must have genuine policy/economic relevance. Skip entirely if nothing clears the bar.

4. 📈 Markets — 3-5 tight bullet points. Each bullet: what happened + what it means for real estate capital markets in one sentence. End with bolded 'Rate to watch' line showing current 10-year Treasury yield and week-over-week direction.

5. 🏘️ Real Estate & Affordable Housing — Policy, LIHTC, and market-level trends only. Apply the deal significance test before including any transaction. Legislation and LIHTC policy get full treatment. Weave Economist analysis inline when relevant.

6. 🏦 Research & Market Intelligence — 2 items max. Skip if nothing relevant. Prioritize: (a) institutional research (Goldman, Morgan Stanley, JPMorgan, BlackRock, CBRE, JLL, Newmark, Berkadia, Marcus & Millichap), (b) wire summaries (Bloomberg Economics, RealPage, GlobeSt). Focus: multifamily trends, rate outlooks, cap rates, CRE volumes. Skip anything already in Markets or Macro.

7. 🤖 AI & Technology — 2 items max. Breakthroughs or major policy shifts get 2–3 sentences; routine news gets one line.

8. 🔬 Science & Health — 1 story max. Only landmark findings or major public health news. Skip entirely if nothing clears the bar.

9. 🎙️ Recent Releases — Articles and written content only — no podcasts, no videos. Only items published in the last 48 hours. For each item: one-line description + link + an "Add to Worth Your Time" button using the same ADD TO LIST BUTTON format and encoding rules defined in section 11. Use category "other" if unsure. Skip section if nothing new.

10. 💡 One Thing to Learn Today — One practical insight. 3-5 sentences. See topic history in the data block — avoid sub-topics used recently. After the insight, include the HTML comment: <!-- LEARN_SUBTOPIC: [key] --> with the snake_case sub-topic key.

11. 📚 Worth Your Time — Always populate this section with exactly 1 item.
   The CANDIDATE POOL is labeled with today's tier and under-quota topics.
   Select items that match today's tier first; under-quota topics second.
   The Evergreen Library always has something worthy — never leave this empty.

   SELECTION PRIORITY:
   1. Items from under-quota topic categories
   2. Items that match today's tier (Tier 1=EVERGREEN, 2=DURABLE, 3=TOPICAL)
   3. Items with highest quality and staying power

   EVERY ITEM MUST PASS ALL FOUR:
   1. A thoughtful well-read person would enthusiastically recommend this to a smart friend
   2. Freely accessible — no paywall, no login required
   3. 5–10 minutes to read (flag if outside this range)
   4. Written for an intelligent general reader if outside Jeff's domain

   TIER DEFINITIONS:
   - EVERGREEN (Tier 1): shelf life 50+ years — classical philosophy, foundational thinking
   - DURABLE (Tier 2): shelf life 1-3 years — high quality analysis, well-reasoned essays
   - TOPICAL (Tier 3): shelf life 1-4 weeks — exceptional pieces highly relevant right now

   ACCESSIBILITY RULES:
   - For topics WITHIN Jeff's domain (LIHTC, affordable housing, real estate finance): depth and technicality are fine
   - For topics OUTSIDE his domain (science, philosophy, history, linguistics): always prefer journalism over academia
   - If dense but exceptional: flag as 'This one is dense, worth the effort — [one sentence why]'
   - Never include arXiv preprints or academic papers requiring expert background

   ALWAYS EXCLUDE:
   Breaking news · market recaps · weekly data summaries · press releases ·
   under 3 minutes · over 20 minutes (unless truly landmark, flag explicitly) ·
   listicles · aggregator roundups · podcast clips or highlight reels ·
   WSJ · FT · Bloomberg · NYT · New Yorker · The Atlantic · Foreign Affairs ·
   HBR · Washington Post (paywalled)

   ALWAYS FREE — link without restriction:
     aeon.co  nautil.us  quantamagazine.org  waitbutwhy.com  paulgraham.com
     fs.blog  jamesclear.com  dailystoic.com  ryanholiday.net  nav.al
     hubermanlab.com  peterattiamd.com  collaborativefund.com  noahpinion.blog
     oaktreecapital.com  berkshirehathaway.com  notboring.co  epsilontheory.com
     writings.stephenwolfram.com  eugenewei.com  a16z.com  medium.com
     themarginalian.org  outsideonline.com  vox.com  strongtowns.org
     stlouisfed.org  huduser.gov  r2d3.us  classics.mit.edu  en.wikisource.org
     economist.com (authenticated access) · npr.org · statnews.com · technologyreview.com
   When in doubt: do not hyperlink.

   TIER BADGE HTML STYLES:
   - EVERGREEN: style="display:inline-block;padding:2px 8px;border-radius:3px;font-size:11px;font-weight:bold;background:#1a1a2e;color:#fff;"
   - DURABLE:   style="display:inline-block;padding:2px 8px;border-radius:3px;font-size:11px;font-weight:bold;background:#1e4d2b;color:#fff;"
   - TOPICAL:   style="display:inline-block;padding:2px 8px;border-radius:3px;font-size:11px;font-weight:bold;background:#b45309;color:#fff;"

   TOPIC BADGE HTML STYLE:
   style="display:inline-block;padding:2px 8px;border-radius:3px;font-size:11px;background:#f0f0f0;color:#555;margin-left:6px;"

   SECTION HEADER — render exactly as:
   <h2 style="font-size:16px;font-weight:bold;margin:32px 0 2px;\
border-left:4px solid #1a1a2e;padding-left:10px;color:#1a1a1a;">
     📚 Worth Your Time
   </h2>
   <p style="margin:0 0 16px;padding-left:14px;font-size:12px;color:#888;\
font-family:Arial,Helvetica,sans-serif;">
     Curated reads, listens, and watches with staying power
   </p>

   ITEM FORMAT — for each selected item render in this order:

   1. Icon + Title link + Author + Source:
      📄 <a href="[URL]">[TITLE]</a> — [AUTHOR], [SOURCE] ([YEAR if not current year])

   2. Tier badge + Topic badge + Duration badge:
      [TIER BADGE] [TOPIC BADGE] <span style="color:#888;font-size:12px;">[N] min read</span>

   3. Optional flag (if dense or extended):
      <p style="margin:4px 0;font-size:12px;color:#b45309;font-style:italic;">
        This one is dense, worth the effort — [one sentence why]
      </p>

   4. Description — 2-3 sentences:
      Sentence 1: What this piece argues, explores, or teaches
      Sentence 2: What Jeff will specifically take away
      Sentence 3: Why it has staying power or matters now

   5. Rating buttons (ALWAYS include):
      <p style="margin:8px 0 0;font-size:12px;color:#888;">
        Rate this pick:
        <a href="https://jeffreyeehrlich-ui.github.io/daily-digest/reading-list/rate/?item=[url-encoded-article-url]&rating=1&title=[url-encoded-title]&category=[category]&tier=[tier]" style="text-decoration:none;">👍</a> &nbsp;
        <a href="https://jeffreyeehrlich-ui.github.io/daily-digest/reading-list/rate/?item=[url-encoded-article-url]&rating=0&title=[url-encoded-title]&category=[category]&tier=[tier]" style="text-decoration:none;">😐</a> &nbsp;
        <a href="https://jeffreyeehrlich-ui.github.io/daily-digest/reading-list/rate/?item=[url-encoded-article-url]&rating=-1&title=[url-encoded-title]&category=[category]&tier=[tier]" style="text-decoration:none;">👎</a>
      </p>

   6. Add to list button (ALWAYS include for free items):
      <a href="[ADD_TO_LIST_HREF]" target="_blank"
         style="display:inline-block;padding:5px 12px;background:#1a1a2e;\
color:#ffffff;font-family:Arial,Helvetica,sans-serif;font-size:12px;\
text-decoration:none;border-radius:3px;">
        + Add to list
      </a>

   FULL ITEM CARD TEMPLATE:
   <div style="margin:0 0 20px;padding:14px 16px;border:1px solid #e8e8e8;\
border-radius:4px;background:#fafafa;">
     <p style="margin:0 0 6px;font-size:14px;font-weight:bold;color:#1a1a1a;">
       [ICON] <a href="[ITEM_URL]" style="color:#1a3a6e;text-decoration:none;">[HEADLINE]</a>
       — <span style="color:#555;">[AUTHOR], [SOURCE]</span>
       [YEAR SPAN if not current year: <span style="color:#888;font-size:13px;font-weight:normal;"> ([YEAR])</span>]
     </p>
     <p style="margin:0 0 8px;font-size:12px;">
       [TIER BADGE] [TOPIC BADGE] &nbsp;<span style="color:#888;">[DURATION]</span>
     </p>
     [OPTIONAL DENSE FLAG]
     <p style="margin:0 0 10px;font-size:14px;line-height:1.7;color:#333;">
       [2-3 sentence description]
     </p>
     <p style="margin:8px 0 0;font-size:12px;color:#888;">
       Rate this pick:
       <a href="https://jeffreyeehrlich-ui.github.io/daily-digest/reading-list/rate/?item=[URL_ENC_ARTICLE_URL]&rating=1&title=[URL_ENC_TITLE]&category=[CATEGORY]&tier=[TIER_NUM]" style="text-decoration:none;">👍</a> &nbsp;
       <a href="https://jeffreyeehrlich-ui.github.io/daily-digest/reading-list/rate/?item=[URL_ENC_ARTICLE_URL]&rating=0&title=[URL_ENC_TITLE]&category=[CATEGORY]&tier=[TIER_NUM]" style="text-decoration:none;">😐</a> &nbsp;
       <a href="https://jeffreyeehrlich-ui.github.io/daily-digest/reading-list/rate/?item=[URL_ENC_ARTICLE_URL]&rating=-1&title=[URL_ENC_TITLE]&category=[CATEGORY]&tier=[TIER_NUM]" style="text-decoration:none;">👎</a>
     </p>
     <a href="[ADD_TO_LIST_HREF]" target="_blank"
        style="display:inline-block;margin-top:8px;padding:5px 12px;background:#1a1a2e;\
color:#ffffff;font-family:Arial,Helvetica,sans-serif;font-size:12px;\
text-decoration:none;border-radius:3px;">
       + Add to list
     </a>
   </div>

   ADD TO LIST HREF construction:
   Base:  https://jeffreyeehrlich-ui.github.io/daily-digest/reading-list/?add=
   Append URL-encoded JSON: {{"title":"[TITLE]","url":"[ITEM_URL]","source":"[SOURCE]","type":"[article|podcast|video]","category":"[one of: markets|real-estate|macro|ai|science|health|philosophy|policy|other]","duration":"[duration]","description":"[2-3 sentence description]"}}
   Encoding: space->%20 :->%3A /->%2F ?->%3F =->%3D &->%26 #->%23 +->%2B quote->%22 open-brace->%7B close-brace->%7D open-bracket->%5B close-bracket->%5D comma->%2C

   RATE LINK URL encoding: percent-encode both the article URL and the title using standard URL encoding (space->%20, etc.). Tier should be the integer tier number (1, 2, or 3). Category should use the library category key (e.g. philosophy_stoicism, investing_finance, hard_science).

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
    gmail_user     = os.environ.get("FROM_EMAIL")
    gmail_password = os.environ.get("GMAIL_APP_PASSWORD")
    to_email       = os.environ.get("TO_EMAIL")

    for var, val in [
        ("FROM_EMAIL",         gmail_user),
        ("GMAIL_APP_PASSWORD", gmail_password),
        ("TO_EMAIL",           to_email),
    ]:
        if not val:
            raise ValueError(f"{var} is not set.")

    subject   = f"Jeff's Daily Digest — {today.strftime('%B %d, %Y')}"
    full_html = wrap_email(html, today)
    plain     = "Your daily digest is ready. Open in an HTML-capable email client to view."

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From']    = f'"Jeff\'s Daily Digest" <{gmail_user}>'
    msg['To']      = to_email
    msg['List-Unsubscribe'] = f'<mailto:{gmail_user}>'

    msg.attach(MIMEText(plain,     'plain'))
    msg.attach(MIMEText(full_html, 'html'))

    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
        smtp.login(gmail_user, gmail_password)
        smtp.sendmail(gmail_user, to_email, msg.as_string())
    log.info("Email sent via Gmail SMTP to %s", to_email)

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
        help="Generate and send via Gmail SMTP.",
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

    # 5c. WYT — tier, quota analysis, library, archive
    wyt_today_tier, wyt_today_tier_name = get_today_tier(today)
    log.info("WYT today: Tier %d (%s) — %s", wyt_today_tier, wyt_today_tier_name,
             today.strftime("%A"))

    wyt_history  = load_wyt_history()
    wyt_ratings  = load_wyt_ratings()
    wyt_under_quota, wyt_over_quota = calculate_topic_quotas(wyt_history)

    wyt_library_all = load_wyt_library()
    wyt_library_eligible = get_eligible_library_items(
        wyt_library_all, wyt_history, wyt_over_quota
    )
    wyt_library_eligible = filter_by_accessibility(wyt_library_eligible)
    wyt_library_eligible = apply_rating_boost(wyt_library_eligible, wyt_ratings)
    content["_wyt_library"] = wyt_library_eligible
    log.info("WYT library: %d total, %d eligible after cooldown/quota/accessibility",
             len(wyt_library_all), len(wyt_library_eligible))

    # 5d. Fetch WYT archive from thinker/long-form sources
    log.info("Fetching WYT archive (thinker blogs) …")
    content["_wyt_archive"] = fetch_wyt_archive()

    # 5e. Inject WYT metadata for use in build_user_prompt()
    content["_wyt_tier"]        = (wyt_today_tier, wyt_today_tier_name)
    content["_wyt_under_quota"] = wyt_under_quota
    content["_wyt_over_quota"]  = wyt_over_quota
    # Source cap: which sources have appeared >= 2x in WYT in last 7 days
    content["_wyt_source_cap_exceeded"] = compute_wyt_source_cap_exceeded(wyt_history)

    # 5f. Load learn history and build context for One Thing to Learn Today
    learn_history = load_learn_history()
    content["_learn_context"] = compute_learn_context(learn_history)

    # 6. Create shared Claude client (reused for Economist selection + main digest)
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY is not set.")
    claude_client = anthropic.Anthropic(api_key=api_key)

    # 7. Select best unread Economist article
    today_headlines = [
        item["title"]
        for items in content.values()
        if isinstance(items, list)
        for item in items
        if isinstance(item, dict) and "title" in item
    ]
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

    # 10b. Record WYT selections — update wyt_history.json and wyt_library.yaml
    all_wyt_candidates = content.get("_wyt_all_candidates", [])
    record_wyt_selections(digest_html, all_wyt_candidates, wyt_history, wyt_library_all)
    save_wyt_history(wyt_history)
    save_wyt_library(wyt_library_all)

    # 10c. Record learn subtopic for topic rotation tracking
    record_learn_subtopic(digest_html, learn_history)
    save_learn_history(learn_history)

    if send_mode:
        send_email(digest_html, today)
        log.info("Done.")
    else:
        print("\n\n" + "─" * 60)
        print("[TEST MODE] Digest generated. No email sent.")
        print(f"Log: {_log_file}")


if __name__ == "__main__":
    main()
