#!/usr/bin/env python3
"""
Feed health check — tests every RSS source in sources.yaml.

Usage:
    python check_feeds.py            # print report, exit 0 even if broken
    python check_feeds.py --strict   # exit 1 if any feeds are broken (for CI)

Outputs a human-readable report and, if run with --strict in CI, causes the
workflow to fail so you get notified via GitHub Actions email.
"""

import argparse
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

SOURCES_FILE = Path(__file__).parent / "sources.yaml"
TIMEOUT = 10
USER_AGENT = "Mozilla/5.0 (compatible; digest-feed-checker/1.0)"

# Feeds that require a subscription and are expected to fail — don't flag these
EXPECTED_FAILURES = {
    "https://www.economist.com/rss/the_economist_full_rss.xml",
}


def check_url(url: str) -> tuple[bool, str]:
    """Return (ok, detail_string)."""
    if url in EXPECTED_FAILURES:
        return True, "skipped (subscription required)"

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
            chunk = r.read(512)
            is_feed = any(tag in chunk for tag in (b"<rss", b"<feed", b"<channel"))
            label = "RSS" if is_feed else f"HTTP {r.status} (no RSS tags)"
            return True, label
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return False, f"URL error: {e.reason}"
    except Exception as e:
        return False, str(e)[:80]


def main():
    parser = argparse.ArgumentParser(description="Check all digest feed sources.")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit with code 1 if any feeds are broken (for CI use).",
    )
    args = parser.parse_args()

    with open(SOURCES_FILE) as f:
        config = yaml.safe_load(f)

    seen: set[str] = set()
    results: list[tuple[str, str, str, bool, str]] = []  # section, name, url, ok, detail

    for section, sources in config.get("sources", {}).items():
        if not isinstance(sources, list):
            continue
        for source in sources:
            url = source.get("url", "")
            if not url or url in seen:
                continue
            seen.add(url)
            name = source.get("name", url)
            # Skip scrape-type sources — they use BeautifulSoup, not RSS
            if source.get("type") == "scrape":
                results.append((section, name, url, True, "scrape (not tested)"))
                continue
            ok, detail = check_url(url)
            results.append((section, name, url, ok, detail))
            time.sleep(0.2)  # be polite

    ok_count = sum(1 for *_, ok, _ in results if ok)
    fail_count = sum(1 for *_, ok, _ in results if not ok)

    print(f"\n{'='*60}")
    print(f"  Feed Health Check — {ok_count} OK  |  {fail_count} BROKEN")
    print(f"{'='*60}\n")

    current_section = None
    for section, name, url, ok, detail in results:
        if section != current_section:
            print(f"[{section}]")
            current_section = section
        status = "  OK  " if ok else " FAIL "
        print(f"  {status} {name}")
        if not ok:
            print(f"         {url}")
            print(f"         → {detail}")

    if fail_count:
        print(f"\n{fail_count} broken feed(s) found. Update sources.yaml to fix.\n")
    else:
        print("\nAll feeds healthy.\n")

    if args.strict and fail_count:
        sys.exit(1)


if __name__ == "__main__":
    main()
