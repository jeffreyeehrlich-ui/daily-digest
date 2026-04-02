"""
Gmail reader for digest-sources label.

Authentication:
  First run: place credentials.json (OAuth2 desktop app) in project root.
             A browser window will open for authorization.
             token.json is saved automatically for future runs.
  Subsequent runs: token.json is used directly.
  GitHub Actions: Store the contents of token.json as a GitHub Secret named
                  GMAIL_TOKEN_JSON, then write it to token.json at the start
                  of the workflow with:
                    echo "$GMAIL_TOKEN_JSON" > token.json
"""

import base64
import email as email_lib
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_FILE = Path("credentials.json")
TOKEN_FILE = Path("token.json")
LABEL_NAME = "digest-sources"
MAX_EMAILS = 5
LOOKBACK_HOURS = 48


def _get_service():
    """Return an authenticated Gmail service, or None if credentials missing."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        log.warning("Gmail dependencies not installed. Run: pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client")
        return None

    if not CREDENTIALS_FILE.exists() and not TOKEN_FILE.exists():
        log.warning("credentials.json not found and no token.json — skipping Gmail. Place credentials.json in project root to enable.")
        return None

    creds = None
    if TOKEN_FILE.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
        except Exception as e:
            log.warning("Failed to load token.json: %s", e)
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                log.warning("Token refresh failed: %s", e)
                creds = None
        if not creds:
            if not CREDENTIALS_FILE.exists():
                log.warning("credentials.json not found and token invalid — skipping Gmail.")
                return None
            try:
                flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
                creds = flow.run_local_server(port=0)
            except Exception as e:
                log.warning("Gmail OAuth flow failed: %s", e)
                return None
        try:
            TOKEN_FILE.write_text(creds.to_json())
        except Exception as e:
            log.warning("Could not save token.json: %s", e)

    try:
        service = build("gmail", "v1", credentials=creds)
        return service
    except Exception as e:
        log.warning("Gmail service build failed: %s", e)
        return None


def _clean_text(text: str) -> str:
    """Strip HTML, unsubscribe footers, tracking noise."""
    # Remove HTML tags
    text = re.sub(r"<[^>]+>", " ", text)
    # Decode HTML entities
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    # Remove URLs (keep surrounding text)
    text = re.sub(r"https?://\S+", "", text)
    # Remove unsubscribe / tracking footer patterns
    unsubscribe_patterns = [
        r"(?i)unsubscribe.*$",
        r"(?i)to stop receiving.*$",
        r"(?i)manage your (email )?preferences.*$",
        r"(?i)you (are|were) receiving this (email|newsletter).*$",
        r"(?i)copyright \d{4}.*$",
        r"(?i)all rights reserved.*$",
        r"(?i)view (this|in) (email|browser).*$",
        r"(?i)if you no longer.*$",
        r"(?i)this email was sent to.*$",
    ]
    for pat in unsubscribe_patterns:
        text = re.sub(pat, "", text, flags=re.MULTILINE | re.DOTALL)
    # Collapse whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _extract_plain_text(payload) -> str:
    """Recursively extract plain text from a Gmail message payload."""
    mime_type = payload.get("mimeType", "")
    body = payload.get("body", {})
    parts = payload.get("parts", [])

    if mime_type == "text/plain":
        data = body.get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    elif mime_type == "text/html" and not parts:
        data = body.get("data", "")
        if data:
            html = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
            return _clean_text(html)

    # Prefer plain text part in multipart
    plain = ""
    html_fallback = ""
    for part in parts:
        if part.get("mimeType") == "text/plain":
            data = part.get("body", {}).get("data", "")
            if data:
                plain = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
        elif part.get("mimeType") == "text/html":
            data = part.get("body", {}).get("data", "")
            if data:
                html_fallback = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
        elif part.get("parts"):
            result = _extract_plain_text(part)
            if result:
                plain = result

    if plain:
        return plain
    if html_fallback:
        return _clean_text(html_fallback)
    return ""


def _parse_sender(from_header: str):
    """Return (name, address) from a From: header."""
    match = re.match(r'^"?([^"<]+?)"?\s*<([^>]+)>', from_header.strip())
    if match:
        return match.group(1).strip(), match.group(2).strip()
    if "@" in from_header:
        return from_header.strip(), from_header.strip()
    return from_header.strip(), ""


def fetch_newsletter_emails() -> list:
    """
    Fetch emails from Gmail label 'digest-sources' in the last 48 hours.
    Returns list of dicts: {title, source, content, date, url}
    Returns empty list on any error.
    """
    try:
        service = _get_service()
        if service is None:
            return []

        # Find label ID for 'digest-sources'
        try:
            labels_resp = service.users().labels().list(userId="me").execute()
            label_id = None
            for lbl in labels_resp.get("labels", []):
                if lbl["name"].lower() == LABEL_NAME.lower():
                    label_id = lbl["id"]
                    break
            if not label_id:
                log.warning("Gmail label '%s' not found — create it in Gmail and apply it to GZero/The Promote emails.", LABEL_NAME)
                return []
        except Exception as e:
            log.warning("Failed to fetch Gmail labels: %s", e)
            return []

        # Build query: label + last 48h
        cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
        after_ts = int(cutoff.timestamp())
        query = f"after:{after_ts}"

        try:
            msgs_resp = service.users().messages().list(
                userId="me",
                labelIds=[label_id],
                q=query,
                maxResults=MAX_EMAILS,
            ).execute()
        except Exception as e:
            log.warning("Gmail messages.list failed: %s", e)
            return []

        messages = msgs_resp.get("messages", [])
        if not messages:
            log.info("Gmail: no messages found in '%s' label in last %dh", LABEL_NAME, LOOKBACK_HOURS)
            return []

        items = []
        for msg_stub in messages[:MAX_EMAILS]:
            try:
                msg = service.users().messages().get(
                    userId="me",
                    id=msg_stub["id"],
                    format="full",
                ).execute()
            except Exception as e:
                log.warning("Failed to fetch Gmail message %s: %s", msg_stub["id"], e)
                continue

            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            subject = headers.get("Subject", "(no subject)")
            from_header = headers.get("From", "")
            date_str = headers.get("Date", "")
            sender_name, _ = _parse_sender(from_header)

            # Parse date
            try:
                from email.utils import parsedate_to_datetime
                date_dt = parsedate_to_datetime(date_str)
                date_fmt = date_dt.strftime("%Y-%m-%d")
            except Exception:
                date_fmt = datetime.now().strftime("%Y-%m-%d")

            # Extract body
            raw_text = _extract_plain_text(msg.get("payload", {}))
            clean = _clean_text(raw_text)
            # Truncate to avoid overwhelming Claude
            if len(clean) > 4000:
                clean = clean[:4000] + "\n[truncated]"

            if not clean.strip():
                continue

            items.append({
                "title": subject,
                "source": sender_name or "Email newsletter",
                "content": clean,
                "date": date_fmt,
                "url": "",
            })
            log.info("Gmail: fetched '%s' from %s", subject, sender_name)

        log.info("Gmail: %d newsletter item(s) fetched", len(items))
        return items

    except Exception as e:
        log.warning("Gmail fetch_newsletter_emails failed unexpectedly: %s", e)
        return []
