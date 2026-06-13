#!/usr/bin/env python3
"""
NWS Weather Story — monitor a National Weather Service office's Weather Story
images for changes and send notifications (email and/or any Apprise-supported
service) with the updated images attached.

Copyright (C) 2026 Rob Wolff <rob@borked.io>
This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License v3 as published by the Free
Software Foundation. This program is distributed WITHOUT ANY WARRANTY. See the
GNU General Public License for more details: <https://www.gnu.org/licenses/>.
"""

import hashlib
import io
import json
import logging
import os
import smtplib
import tempfile
import time
from datetime import datetime
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# ---------------------------------------------------------------------------
# Configuration (via environment variables)
# ---------------------------------------------------------------------------
def _split(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


# Which NWS office (WFO) to watch, e.g. "dtx" (Detroit), "okx" (New York).
# BASE_URL is derived from WFO unless it is set explicitly.
WFO = os.getenv("WFO", "dtx").strip().lower()
BASE_URL = os.getenv("BASE_URL", f"https://www.weather.gov/images/{WFO}/wxstory")

IMAGE_NAMES = _split(
    os.getenv(
        "IMAGE_NAMES",
        "Tab1FileL.png,Tab2FileL.png,Tab3FileL.png,Tab4FileL.png,Tab5FileL.png",
    )
)

# Display name + links used in the email body.
LOCATION_NAME = os.getenv("LOCATION_NAME", WFO.upper())
STORY_URL = os.getenv("STORY_URL", f"https://www.weather.gov/{WFO}/weatherstory")
EXTRA_LINKS = _split(os.getenv("EXTRA_LINKS", ""))  # "Label|https://url, Label2|https://url2"

# Timezone used for timestamps in notifications.
TIMEZONE = os.getenv("TIMEZONE", "America/Detroit")

# Email (optional — leave unset to disable the email channel).
GMAIL_USER = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
NOTIFY_EMAILS = _split(os.getenv("NOTIFY_EMAILS", "") or GMAIL_USER)

# Apprise (optional — comma-separated Apprise URLs for Discord/Telegram/ntfy/etc).
APPRISE_URLS = _split(os.getenv("APPRISE_URLS", ""))

# Uptime Kuma push heartbeat (optional).
UPTIME_KUMA_PUSH_URL = os.getenv("UPTIME_KUMA_PUSH_URL", "").strip()

# OCR-based skip: ignore a change if a region of the image contains any of these
# keywords (comma-separated, case-insensitive). Empty = OCR disabled.
SKIP_KEYWORDS = [k.lower() for k in _split(os.getenv("SKIP_KEYWORDS", ""))]
# Region of the image to scan, as "left,top,right,bottom". Each value is pixels,
# or a percentage of the image's width/height when suffixed with "%". Empty
# defaults to the top band (full width × OCR_CROP_HEIGHT pixels).
OCR_REGION = os.getenv("OCR_REGION", "").strip()
OCR_CROP_HEIGHT = int(os.getenv("OCR_CROP_HEIGHT", "80"))


def _resolve_region(img_w: int, img_h: int) -> tuple[int, int, int, int]:
    """Resolve OCR_REGION (pixels or %) into a pixel box for the given image."""
    if not OCR_REGION:
        return (0, 0, img_w, min(OCR_CROP_HEIGHT, img_h))
    parts = _split(OCR_REGION)
    if len(parts) != 4:
        raise ValueError(f"OCR_REGION must be 'left,top,right,bottom', got {OCR_REGION!r}")
    dims = (img_w, img_h, img_w, img_h)
    box = []
    for value, dim in zip(parts, dims):
        if value.endswith("%"):
            box.append(round(float(value[:-1]) / 100 * dim))
        else:
            box.append(int(value))
    return tuple(box)  # type: ignore[return-value]

# Fetch behaviour.
HASH_FILE = Path(os.getenv("HASH_FILE", "/data/hashes.json"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "3600"))  # seconds
FETCH_RETRIES = int(os.getenv("FETCH_RETRIES", "3"))
FETCH_BACKOFF = float(os.getenv("FETCH_BACKOFF", "5"))  # seconds, doubles each retry
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("weather-monitor")


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def load_hashes() -> dict:
    if HASH_FILE.exists():
        return json.loads(HASH_FILE.read_text())
    return {}


def save_hashes(hashes: dict):
    HASH_FILE.parent.mkdir(parents=True, exist_ok=True)
    HASH_FILE.write_text(json.dumps(hashes, indent=2))


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
def fetch_image(url: str) -> bytes | None:
    """Fetch a URL with retries and exponential backoff."""
    backoff = FETCH_BACKOFF
    for attempt in range(1, FETCH_RETRIES + 1):
        try:
            resp = requests.get(
                url,
                timeout=REQUEST_TIMEOUT,
                headers={"User-Agent": "WeatherStoryMonitor/2.0"},
            )
            resp.raise_for_status()
            return resp.content
        except requests.RequestException as e:
            if attempt < FETCH_RETRIES:
                log.warning(
                    f"Failed to fetch {url} (attempt {attempt}/{FETCH_RETRIES}): {e} "
                    f"— retrying in {backoff:.0f}s"
                )
                time.sleep(backoff)
                backoff *= 2
            else:
                log.warning(f"Failed to fetch {url} after {FETCH_RETRIES} attempts: {e}")
    return None


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def should_skip(data: bytes) -> bool:
    """Return True if the top of the image contains any configured skip keyword."""
    if not SKIP_KEYWORDS:
        return False
    try:
        from PIL import Image
        import pytesseract

        img = Image.open(io.BytesIO(data))
        region = img.crop(_resolve_region(img.width, img.height))
        text = pytesseract.image_to_string(region).lower()
        return any(keyword in text for keyword in SKIP_KEYWORDS)
    except Exception as e:
        log.warning(f"OCR check failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.now(ZoneInfo(TIMEZONE)).strftime("%I:%M %p - %m/%d/%Y")


def _links_html() -> str:
    parts = [f'<a href="{STORY_URL}">View the full Weather Story →</a>']
    for item in EXTRA_LINKS:
        if "|" in item:
            label, url = item.split("|", 1)
            parts.append(f'<a href="{url.strip()}">{label.strip()}</a>')
    return "\n    &nbsp;|&nbsp;\n    ".join(parts)


def send_email(changed_images: list[tuple[str, bytes]]):
    """Send an email with changed weather story images embedded inline."""
    msg = MIMEMultipart("related")
    msg["From"] = GMAIL_USER
    msg["To"] = ", ".join(NOTIFY_EMAILS)
    msg["Subject"] = f"🌦️ NWS {LOCATION_NAME} Weather Story Updated"

    img_html = ""
    for name, _ in changed_images:
        cid = name.replace(".", "_")
        img_html += (
            f'<div style="margin-bottom:20px;">'
            f'<p style="font-weight:bold;margin:0 0 8px 0;">{name}</p>'
            f'<img src="cid:{cid}" style="max-width:100%;border:1px solid #ddd;border-radius:4px;" />'
            f"</div>"
        )

    html = f"""\
<html>
<body style="font-family:sans-serif;color:#333;">
  <p>The following weather story image(s) changed at {_now()}:</p>
  {img_html}
  <p>
    {_links_html()}
  </p>
</body>
</html>"""

    msg.attach(MIMEText(html, "html"))

    for name, data in changed_images:
        cid = name.replace(".", "_")
        img = MIMEImage(data)
        img.add_header("Content-ID", f"<{cid}>")
        img.add_header("Content-Disposition", "inline", filename=name)
        msg.attach(img)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            smtp.sendmail(GMAIL_USER, NOTIFY_EMAILS, msg.as_string())
        log.info(f"Email sent to {', '.join(NOTIFY_EMAILS)} ({len(changed_images)} image(s))")
    except Exception as e:
        log.error(f"Failed to send email: {e}")


def send_apprise(changed_images: list[tuple[str, bytes]]):
    """Send a notification with attachments via Apprise."""
    try:
        import apprise
    except ImportError:
        log.error("APPRISE_URLS set but the 'apprise' package is not installed")
        return

    ap = apprise.Apprise()
    for url in APPRISE_URLS:
        ap.add(url)

    names = ", ".join(name for name, _ in changed_images)
    body = f"{len(changed_images)} weather story image(s) changed at {_now()}: {names}\n{STORY_URL}"

    # Apprise attaches from file paths, so write the images to a temp dir.
    with tempfile.TemporaryDirectory() as tmp:
        attach = apprise.AppriseAttachment()
        for name, data in changed_images:
            path = Path(tmp) / name
            path.write_bytes(data)
            attach.add(str(path))

        ok = ap.notify(
            title=f"🌦️ NWS {LOCATION_NAME} Weather Story Updated",
            body=body,
            attach=attach,
        )
    if ok:
        log.info(f"Apprise notification sent to {len(APPRISE_URLS)} target(s)")
    else:
        log.error("Apprise notification failed")


def notify(changed_images: list[tuple[str, bytes]]):
    if GMAIL_USER and GMAIL_APP_PASSWORD and NOTIFY_EMAILS:
        send_email(changed_images)
    if APPRISE_URLS:
        send_apprise(changed_images)


def ping_heartbeat(ok: bool = True, msg: str = "OK"):
    """Ping an Uptime Kuma push monitor, if configured."""
    if not UPTIME_KUMA_PUSH_URL:
        return
    try:
        requests.get(
            UPTIME_KUMA_PUSH_URL,
            params={"status": "up" if ok else "down", "msg": msg},
            timeout=10,
        )
    except requests.RequestException as e:
        log.warning(f"Heartbeat ping failed: {e}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def check_once():
    hashes = load_hashes()
    changed: list[tuple[str, bytes]] = []

    for name in IMAGE_NAMES:
        url = f"{BASE_URL}/{name}"
        data = fetch_image(url)
        if data is None:
            continue

        new_hash = hash_bytes(data)
        old_hash = hashes.get(name)

        if old_hash is None:
            log.info(f"{name}: first fetch — storing baseline hash")
        elif new_hash != old_hash:
            if should_skip(data):
                log.info(f"{name}: CHANGED but matched skip keyword — skipping")
            else:
                log.info(f"{name}: CHANGED")
                changed.append((name, data))
        else:
            log.debug(f"{name}: unchanged")

        hashes[name] = new_hash

    save_hashes(hashes)

    if changed:
        notify(changed)
    else:
        log.info("No changes detected")


def validate_config():
    email_ok = bool(GMAIL_USER and GMAIL_APP_PASSWORD and NOTIFY_EMAILS)
    if not email_ok and not APPRISE_URLS:
        raise SystemExit(
            "No notification channel configured. Set GMAIL_USER + "
            "GMAIL_APP_PASSWORD (+ NOTIFY_EMAILS) and/or APPRISE_URLS."
        )
    if not IMAGE_NAMES:
        raise SystemExit("IMAGE_NAMES is empty — nothing to monitor.")


def main():
    validate_config()
    channels = []
    if GMAIL_USER and GMAIL_APP_PASSWORD and NOTIFY_EMAILS:
        channels.append("email")
    if APPRISE_URLS:
        channels.append(f"apprise({len(APPRISE_URLS)})")
    log.info(
        f"Starting weather monitor — WFO={WFO}, watching {len(IMAGE_NAMES)} images "
        f"every {CHECK_INTERVAL}s — channels: {', '.join(channels)}"
    )

    while True:
        try:
            check_once()
            ping_heartbeat(ok=True)
        except Exception as e:
            log.error(f"Unhandled error: {e}", exc_info=True)
            ping_heartbeat(ok=False, msg=str(e))

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
