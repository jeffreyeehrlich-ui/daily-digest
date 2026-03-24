#!/usr/bin/env python3
"""
Daily Digest — generates and emails a structured morning briefing.

Usage:
    python digest.py          # generate and print to terminal (same as --test)
    python digest.py --test   # generate and print to terminal, no email sent
    python digest.py --send   # generate and send email via SendGrid
"""

import argparse
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
import feedparser
import yaml
from dotenv import load_dotenv
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

# ── Setup ──────────────────────────────────────────────────────────────────

load_dotenv()

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)
_today_str = datetime.now().strftime("%Y-%m-%d")
_log_file = LOG_DIR / f"digest_{_today_str}.txt"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(_log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ── System prompt ──────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a senior financial journalist producing a daily briefing \
for a real estate private equity professional focused on affordable housing \
acquisitions and LIHTC transactions. Write in the style of the FT's morning \
newsletter — authoritative, concise, no filler. Scale depth to importance: \
legislation that affects LIHTC equity pricing deserves full treatment; routine \
data gets one line. Never include stories that are not genuinely important. \
For the Markets section write a full narrative, not bullets. Every link in \
Worth Reading Later must be a real, working URL from the source material provided.

Output clean HTML suitable for email clients. Use only inline CSS. Follow this \
structure exactly:

Wrapper div:
  style="max-width:650px;margin:0 auto;font-family:Georgia,serif;\
color:#1a1a1a;line-height:1.7;padding:16px;"

Date bar:
  <p style="font-size:13px;color:#888;border-bottom:1px solid #ddd;\
padding-bottom:10px;margin-bottom:24px;">

Section heading (h2):
  style="font-size:17px;font-weight:bold;margin:28px 0 6px;\
border-left:3px solid #c0392b;padding-left:8px;color:#1a1a1a;"

Sub-heading (h3):
  style="font-size:15px;font-weight:bold;margin:16px 0 4px;color:#1a1a1a;"

Body paragraph (p):
  style="margin:6px 0;font-size:15px;"

Links (a):
  style="color:#c0392b;text-decoration:none;"

Bold callout (number to watch / one thing to learn):
  <p style="margin:10px 0;font-size:15px;"><strong>...</strong></p>

Worth Reading Later list:
  <ul style="padding-left:20px;margin:8px 0;">
  <li style="margin:4px 0;font-size:15px;">

Do NOT wrap the output in markdown code fences. Output raw HTML only."""

# ── Config loading ─────────────────────────────────────────────────────────


def load_sources(path: str = "sources.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ── Feed fetching ──────────────────────────────────────────────────────────


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
    items = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    try:
        feed = feedparser.parse(source["url"])
        for entry in feed.entries:
            pub = _parse_entry_date(entry)
            if pub is None or pub < cutoff:
                continue
            items.append(
                {
                    "source": source["name"],
                    "title": getattr(entry, "title", ""),
                    "link": getattr(entry, "link", ""),
                    "summary": (getattr(entry, "summary", "") or "")[:600],
                    "published": pub.isoformat(),
                }
            )
        log.info("%-30s  %d item(s) in last %dh", source["name"], len(items), lookback_hours)
    except Exception as exc:
        log.error("Failed to fetch %-30s  %s", source["name"], exc)
    return items


def collect_content(sources: dict) -> dict[str, list[dict]]:
    """Fetch every configured feed; return content keyed by section."""
    PODCAST_SECTIONS = {"podcasts_newsletters"}
    result: dict[str, list[dict]] = {}
    for section, feeds in sources.get("sources", {}).items():
        lookback = 48 if section in PODCAST_SECTIONS else 24
        items: list[dict] = []
        for feed in feeds:
            items.extend(fetch_feed(feed, lookback_hours=lookback))
        result[section] = items
    return result


# ── Prompt building ────────────────────────────────────────────────────────

SECTION_LIMITS = {
    "markets": 25,
    "macro_geopolitics": 20,
    "us_news": 15,
    "real_estate": 20,
    "ai_tech": 12,
    "podcasts_newsletters": 12,
    "economist": 8,
}


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


def build_user_prompt(content: dict[str, list[dict]], today: datetime) -> str:
    is_weekend = today.weekday() >= 5  # Saturday=5, Sunday=6
    weekend_note = (
        "Today is a WEEKEND — include the 🗞️ Weekend Read — The Economist section."
        if is_weekend
        else "Today is a WEEKDAY — OMIT the 🗞️ Weekend Read — The Economist section entirely."
    )

    def section(title: str, key: str) -> str:
        items = content.get(key, [])
        limit = SECTION_LIMITS.get(key, 15)
        return f"=== {title} ===\n{_format_items(items, limit)}"

    raw_blocks = [
        section("MARKETS (Bloomberg, WSJ, FT)", "markets"),
        section("MACRO & GEOPOLITICS (Reuters, FT, Bloomberg, GZero, Economist)", "macro_geopolitics"),
        section("US NEWS (Reuters, The Hill)", "us_news"),
        section("REAL ESTATE & AFFORDABLE HOUSING (The Real Deal, AHF, The Promote, Jay Parsons)", "real_estate"),
        section("AI & TECHNOLOGY (Ben's Bites, Anthropic Blog)", "ai_tech"),
        section("RECENT RELEASES — PODCASTS & NEWSLETTERS (48-hour window)", "podcasts_newsletters"),
        section("THE ECONOMIST (weekend only)", "economist"),
    ]

    raw_content = "\n\n".join(raw_blocks)

    return f"""Today is {today.strftime("%A, %B %d, %Y")}.
{weekend_note}

Below is every RSS item collected in the last 24 hours (48 hours for podcasts/newsletters).
Use only URLs that appear verbatim in this data — never fabricate links.

{raw_content}

─────────────────────────────────────────────────────────────────────────────
DIGEST SECTIONS TO PRODUCE (in this order):

1. 📈 Markets — Full FT-style narrative covering equities, rates, oil, credit, FX.
   Identify the dominant market theme. End with one bolded "Number to watch."

2. 🌍 Macro & Geopolitics — Up to 3 stories. Bold sub-header per story.
   2–3 sentences each.

3. 🇺🇸 US News — Only if genuinely important domestic news exists.
   Skip the section entirely if nothing clears that bar.

4. 🏘️ Real Estate & Affordable Housing — Variable depth based on importance.
   Legislation and LIHTC policy get full treatment. Routine market data gets
   one line.

5. 🤖 AI & Technology — 2–3 items max.

6. 🎙️ Recent Releases — Only include feeds that published in the last 48 hours.
   One-line description + link per item. Skip section if nothing new.

7. 🗞️ Weekend Read — The Economist — Weekend only (per instruction above).
   Cover story + 1–2 lead pieces with links.

8. 💡 One Thing to Learn Today — Single practical insight tied to something in
   the digest where possible. Applicable to real estate PE / affordable housing
   finance or general intellectual growth.

9. 📌 Worth Reading Later — Links only, no summaries. 3–5 most important
   long-form pieces from the source material. Every URL must appear verbatim
   in the data above.

Output raw HTML only. No markdown fences."""


# ── Claude call ────────────────────────────────────────────────────────────


def generate_digest(content: dict, today: datetime, test_mode: bool = False) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY is not set.")

    client = anthropic.Anthropic(api_key=api_key)
    user_prompt = build_user_prompt(content, today)

    log.info("Calling Claude API (claude-sonnet-4-5) ...")

    html_parts: list[str] = []

    with client.messages.stream(
        model="claude-sonnet-4-5",
        max_tokens=8192,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    ) as stream:
        for text in stream.text_stream:
            html_parts.append(text)
            if test_mode:
                print(text, end="", flush=True)

    raw = "".join(html_parts)

    # Strip accidental markdown code fences if Claude added them
    raw = re.sub(r"^```[a-z]*\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw)

    log.info("Claude response received (%d chars)", len(raw))
    return raw


# ── HTML email wrapper ─────────────────────────────────────────────────────

EMAIL_WRAPPER = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Daily Digest — {date}</title>
</head>
<body style="margin:0;padding:0;background:#f5f5f0;">
<table width="100%" cellpadding="0" cellspacing="0"
  style="background:#f5f5f0;padding:24px 0;">
  <tr><td align="center">
    {body}
  </td></tr>
</table>
</body>
</html>"""


def wrap_email(body_html: str, today: datetime) -> str:
    return EMAIL_WRAPPER.format(
        date=today.strftime("%B %d, %Y"),
        body=body_html,
    )


# ── SendGrid delivery ──────────────────────────────────────────────────────


def send_email(html: str, today: datetime) -> None:
    api_key = os.environ.get("SENDGRID_API_KEY")
    from_email = os.environ.get("FROM_EMAIL")
    to_email = os.environ.get("TO_EMAIL")

    for var, val in [("SENDGRID_API_KEY", api_key), ("FROM_EMAIL", from_email), ("TO_EMAIL", to_email)]:
        if not val:
            raise ValueError(f"{var} is not set.")

    subject = f"Jeff's Daily Digest — {today.strftime('%B %d, %Y')}"
    full_html = wrap_email(html, today)

    message = Mail(
        from_email=from_email,
        to_emails=to_email,
        subject=subject,
        html_content=full_html,
    )

    sg = SendGridAPIClient(api_key)
    response = sg.send(message)
    log.info("Email sent  status=%s  to=%s", response.status_code, to_email)


# ── Entry point ────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate and send the daily digest.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--test",
        action="store_true",
        help="Print the digest to the terminal instead of sending email (default).",
    )
    mode.add_argument(
        "--send",
        action="store_true",
        help="Generate the digest and send it via SendGrid.",
    )
    parser.add_argument(
        "--sources",
        default="sources.yaml",
        help="Path to sources YAML config (default: sources.yaml).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    # Default to test/print mode unless --send is explicitly passed
    send_mode = args.send
    today = datetime.now(timezone.utc)

    log.info("=" * 60)
    log.info("Daily Digest  --  %s  (mode=%s)", today.strftime("%Y-%m-%d"), "send" if send_mode else "test")
    log.info("=" * 60)

    # 1. Load sources
    sources = load_sources(args.sources)

    # 2. Fetch all feeds
    log.info("Fetching RSS feeds ...")
    content = collect_content(sources)
    total_items = sum(len(v) for v in content.values())
    log.info("Total items fetched: %d", total_items)

    # 3. Generate digest via Claude
    digest_html = generate_digest(content, today, test_mode=not send_mode)

    if send_mode:
        # 4. Send email
        send_email(digest_html, today)
        log.info("Done.")
    else:
        print("\n\n" + "─" * 60)
        print("[TEST MODE] Digest generated. No email sent.")
        print(f"Log: {_log_file}")


if __name__ == "__main__":
    main()
