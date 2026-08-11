"""Slack alerting for newly-ingested political filings.

Posts a digest to a Slack channel via an incoming webhook (config
SLACK_WEBHOOK_URL). Alerting is best-effort and deliberately non-fatal: a
Slack outage must never fail an ingest run or roll back rows that are
already committed to the DB, so every send is wrapped and errors are logged,
not raised.

No-ops (logs and returns) when SLACK_WEBHOOK_URL is unset - so local runs
need no Slack setup - or when there are no new filings to report.
"""
from __future__ import annotations

import logging

import requests

from . import config

log = logging.getLogger("notify")

# Cap how many filings are itemized in a single Slack message; the rest are
# summarized as "...and N more" to avoid an unwieldy wall of text on a busy
# day. The DB still has every row regardless.
MAX_ITEMIZED = 25

_SERVICE_SLUG = {"TV": "tv-profile", "AM": "am-profile", "FM": "fm-profile"}


def _fcc_folder_page(callsign: str, service: str) -> str:
    """Browsable HTML page for the station's political files. Loads reliably
    (unlike the raw download endpoint) and establishes the FCC session that
    makes the direct-download link below work afterward."""
    slug = _SERVICE_SLUG.get((service or "").upper(), "tv-profile")
    return f"https://publicfiles.fcc.gov/{slug}/{str(callsign).lower()}/political-files"


def _direct_url(download_url: str, file_name: str) -> str:
    """Correct the stored download URL's extension at display time - older
    rows hardcoded '.pdf' even for Word docs (see fcc_client)."""
    url = download_url or ""
    fn = file_name or ""
    if url.endswith(".pdf") and "." in fn:
        ext = fn.rsplit(".", 1)[-1]
        if ext and ext.lower() != "pdf":
            url = url[:-4] + "." + ext
    return url


def post_new_filings(filings: list) -> None:
    """Send a Slack digest for the given newly-ingested filings (a list of
    src.db.Filing rows). Safe to call unconditionally: honors the
    SUPPRESS_ALERTS flag, the unset-webhook case, and the empty-list case."""
    if config.SUPPRESS_ALERTS:
        log.info("SUPPRESS_ALERTS set - skipping Slack alert for %d filings", len(filings))
        return
    if not filings:
        return
    if not config.SLACK_WEBHOOK_URL:
        log.info("SLACK_WEBHOOK_URL unset - skipping Slack alert for %d filings", len(filings))
        return

    text = _format_message(filings)
    try:
        resp = requests.post(config.SLACK_WEBHOOK_URL, json={"text": text}, timeout=15)
        resp.raise_for_status()
        log.info("Posted Slack alert for %d new filings", len(filings))
    except Exception:
        # Non-fatal by design - see module docstring.
        log.exception("Slack alert failed (filings already saved to DB)")


def _format_message(filings: list) -> str:
    n = len(filings)
    header = f":ballot_box_with_ballot: *{n} new political filing{'s' if n != 1 else ''}* on Bay Area stations"
    lines = [header, ""]
    for f in filings[:MAX_ITEMIZED]:
        purchaser = f.purchaser or "(unknown purchaser)"
        # category_path is "Political Files/<year>/<...>/<purchaser>"; show
        # the middle segments (drop the constant prefix and the trailing
        # purchaser, which is shown separately) as a short context tag.
        parts = [p for p in (f.category_path or "").split("/") if p][1:-1]
        context = " / ".join(parts[1:]) if len(parts) > 1 else (parts[0] if parts else "")
        tag = f" — _{context}_" if context else ""
        # Slack link syntax is <url|text>. Filename links to the direct PDF;
        # "FCC folder" is the reliable fallback that also unlocks the direct
        # link (see _fcc_folder_page).
        doc = f"<{_direct_url(f.download_url, f.file_name)}|{f.file_name}>"
        folder = f"<{_fcc_folder_page(f.callsign, f.service)}|FCC folder ↗>"
        lines.append(f"• *{f.callsign}* — {purchaser}{tag}: {doc}  ·  {folder}")
    if n > MAX_ITEMIZED:
        lines.append(f"…and {n - MAX_ITEMIZED} more.")
    lines.append("")
    lines.append(
        f"_Browse & search all filings in the <{config.DASHBOARD_URL}|dashboard ↗>. "
        "If a document link shows \"Access Denied,\" open its FCC folder link first "
        "(or visit publicfiles.fcc.gov), then the document opens._"
    )
    return "\n".join(lines)
