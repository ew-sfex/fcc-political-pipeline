"""Client for FCC's Online Public Inspection File (OPIF) system.

Two data sources, for two different purposes:

1. Per-station RSS (Atom) feed - https://publicfiles.fcc.gov/[profile-type]/
   [callsign]/rss/ - e.g. https://publicfiles.fcc.gov/tv-profile/kgo-tv/rss/.
   Confirmed working 2026-08-05; supersedes an earlier version of this client
   that queried https://www.fcc.gov/search/api (the general fcc.gov website
   content search, not the political file index - wrong endpoint entirely).
   IMPORTANT caveat confirmed 2026-08-07: this feed is NOT a full upload
   history - it's capped at the 10 most recent uploads, of any category, no
   matter how much history a station has (KGO-TV alone has 913 political
   files going back to 2017; its feed still shows only 10 total). Only used
   here to bootstrap each station's numeric entity/facility ID (embedded in
   every entry's title, e.g. "tv Entity 34470 uploaded...") - there's no
   working public search endpoint for callsign -> entity ID otherwise.

2. OPIF Manager JSON API (publicfiles.fcc.gov/developer) - documented, public,
   no auth required - used for the actual file discovery via
   `walk_political_files()`, which recursively walks a station's "Political
   Files" folder tree (year -> category -> purchaser -> files) via
   `/api/manager/folder/...`. This returns COMPLETE folder contents, no
   windowing, so - unlike the RSS feed - nothing can silently fall off
   between runs regardless of upload volume. Folders carry a `file_count`
   (recursive total under that folder), which is used to skip empty
   branches entirely rather than recursing into them.

Both of the above use plain `requests` - only the RSS feed origin
(publicfiles.fcc.gov) needs the Playwright/headless-browser workaround
below; the JSON API has no such block (confirmed 2026-08-07).

The one endpoint that IS blocked for any automated client, browser or not,
is the actual file *download* (`/api/manager/download/...`) - see
`download()` and README.md's "PDF download status" section. `requests`
alone gets 403'd by Akamai even with a browser-like User-Agent (Akamai
fingerprints the TLS/JS layer, not just headers) - Playwright drives a real
browser engine, which passes that specific check, but not the download one.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime

import requests
from playwright.sync_api import sync_playwright
from tenacity import retry, stop_after_attempt, wait_exponential

FOLDER_PATH_API = "https://publicfiles.fcc.gov/api/manager/folder/path.json"
FOLDER_ID_API = "https://publicfiles.fcc.gov/api/manager/folder/id/{folder_id}.json"

# sourceService param for the folder-path lookup - matches SERVICE_TO_PROFILE_SLUG
# minus the "-profile" suffix; confirmed "tv" works, am/fm assumed analogous.
SERVICE_TO_SOURCE = {"TV": "tv", "FM": "fm", "AM": "am"}

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
XHTML_NS = {"xhtml": "http://www.w3.org/1999/xhtml"}

# publicfiles.fcc.gov's own URL scheme for entity profile pages, per
# observed examples (tv-profile, fm-profile; am-profile assumed analogous -
# verify if an AM station 404s).
SERVICE_TO_PROFILE_SLUG = {
    "TV": "tv-profile",
    "FM": "fm-profile",
    "AM": "am-profile",
}

TITLE_CATEGORY_RE = re.compile(r"uploaded a file in (.+)$")

# FCC's own feed generation doesn't escape bare `&` in filenames/titles (e.g.
# "Issues & Programs 2026 Q2.pdf"), which makes the XML invalid - confirmed
# 2026-08-07 against KQED's feed. Escape any `&` not already part of a valid
# entity before parsing, rather than let the whole station's fetch fail.
BARE_AMPERSAND_RE = re.compile(r"&(?!amp;|lt;|gt;|quot;|apos;|#\d+;|#x[0-9a-fA-F]+;)")


@dataclass
class FccFiling:
    file_id: str            # GUID from <id>
    download_url: str       # direct PDF download link from <link href>
    filename: str
    category_path: str      # e.g. "Political Files/2026/Non-Candidate Issue Ads/BOLD America"
    updated_ts: str
    callsign: str
    service: str

    @property
    def is_political(self) -> bool:
        """Each station's public file has other categories mixed in too
        (EEO reports, ownership filings, issues/programs lists, etc.) -
        callers that only want ad spend should filter on this. Always true
        for filings from `walk_political_files()`, since that only ever
        recurses under the Political Files folder; still relevant for
        `fetch_station_feed()`, which returns everything in the feed."""
        cp = self.category_path or ""
        return cp == "Political Files" or cp.startswith("Political Files/")

    @property
    def purchaser(self) -> str | None:
        if not self.category_path:
            return None
        parts = [p.strip() for p in self.category_path.split("/") if p.strip()]
        return parts[-1] if parts else None

    @property
    def campaign_year(self) -> str | None:
        """Best-effort: pull a 4-digit year out of the category path."""
        if not self.category_path:
            return None
        m = re.search(r"\b(20\d{2})\b", self.category_path)
        return m.group(1) if m else None

    @property
    def updated_dt(self) -> datetime | None:
        if not self.updated_ts:
            return None
        try:
            return datetime.fromisoformat(self.updated_ts.replace("Z", "+00:00"))
        except ValueError:
            return None


class FccClient:
    """Fetches publicfiles.fcc.gov content via a real headless browser
    (see module docstring for why `requests` doesn't work here).
    """

    def __init__(self):
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch()
        self._context = self._browser.new_context()
        self._http = requests.Session()

    def close(self):
        self._http.close()
        self._context.close()
        self._browser.close()
        self._playwright.stop()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def _rss_url(self, callsign: str, service: str) -> str:
        slug = SERVICE_TO_PROFILE_SLUG.get(service.upper())
        if not slug:
            raise ValueError(f"Unknown service type '{service}' - expected one of {list(SERVICE_TO_PROFILE_SLUG)}")
        return f"https://publicfiles.fcc.gov/{slug}/{callsign.lower()}/rss/"

    @retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=1, min=2, max=30))
    def _fetch_bytes(self, url: str) -> bytes:
        """Fetch raw response bytes for a URL via the browser, however the
        browser chooses to handle the response - as an inline navigation
        (feed XML) or as a triggered file download (PDFs, sometimes)."""
        page = self._context.new_page()
        try:
            try:
                response = page.goto(url, timeout=30000)
            except Exception:
                # goto() raises when the navigation gets redirected into a
                # native download instead of rendering - catch the download.
                with page.expect_download(timeout=30000) as dl_info:
                    pass
                download = dl_info.value
                path = download.path()
                return path.read_bytes()
            if response is None:
                raise RuntimeError(f"No response received for {url}")
            if not response.ok:
                raise RuntimeError(f"HTTP {response.status} for {url}")
            return response.body()
        finally:
            page.close()

    def resolve_entity_id(self, callsign: str, service: str) -> str:
        """Bootstrap a station's numeric FCC entity/facility ID from its RSS
        feed - every entry's title embeds it (e.g. "tv Entity 34470
        uploaded..."). Needed by the folder-walk API below; there's no
        working public search endpoint for callsign -> entity ID otherwise.
        Raises if the station has never uploaded anything (feed is empty).
        """
        url = self._rss_url(callsign, service)
        content = self._fetch_bytes(url)
        m = re.search(rb"Entity (\d+)", content)
        if not m:
            raise RuntimeError(f"Could not resolve entity ID for {callsign} - feed may be empty")
        return m.group(1).decode()

    @retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=1, min=2, max=30))
    def _get_folder(self, folder_id: str, entity_id: str) -> dict:
        resp = self._http.get(FOLDER_ID_API.format(folder_id=folder_id), params={"entityId": entity_id}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "success":
            raise RuntimeError(f"Folder API error for folder_id={folder_id}: {data}")
        return data["folder"]

    @retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=1, min=2, max=30))
    def _resolve_political_root(self, entity_id: str, service: str) -> str | None:
        source = SERVICE_TO_SOURCE.get(service.upper())
        resp = self._http.get(
            FOLDER_PATH_API,
            params={"folderPath": "Political Files", "entityId": entity_id, "sourceService": source},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "success" or not data.get("folder"):
            return None
        return data["folder"][0]["entity_folder_id"]

    def walk_political_files(
        self, callsign: str, service: str, since: date | None = None
    ) -> list[FccFiling]:
        """Recursively walk a station's Political Files folder tree via the
        JSON folder API and return every filing found, complete - unlike
        `fetch_station_feed()`, nothing can silently fall off a windowed
        feed here. See module docstring.

        `since`: if given, only filings uploaded on/after this date are
        returned. Enforced two ways: (1) whole year-folders below `since`'s
        year are pruned at the root without recursing (speed), and (2) every
        individual file is checked against `since` by its create timestamp
        (correctness - catches anything mis-filed under a newer year folder).
        """
        entity_id = self.resolve_entity_id(callsign, service)
        root_id = self._resolve_political_root(entity_id, service)
        if root_id is None:
            return []
        filings: list[FccFiling] = []
        self._walk_folder(root_id, entity_id, callsign, service, filings, since, "Political Files", is_root=True)
        return filings

    def _walk_folder(
        self,
        folder_id: str,
        entity_id: str,
        callsign: str,
        service: str,
        out: list[FccFiling],
        since: date | None,
        current_path: str,
        is_root: bool = False,
    ) -> None:
        folder = self._get_folder(folder_id, entity_id)
        if folder.get("file_count") == "0":
            return

        # Use the path we actually traversed to get here, NOT the folder's
        # self-reported `folder_path`. FCC cross-files some folders: e.g.
        # KGO-TV's "Building A Better California" is listed as a child of
        # Political Files/2026/Non-Candidate Issue Ads but self-reports a
        # `folder_path` under "Issues and Programs Lists" (confirmed
        # 2026-08-10). Trusting the self-report would both mislabel the row
        # and push it outside "Political Files/". The traversal path is
        # authoritative for how we categorize a filing.
        for f in folder.get("files") or []:
            # Use the file's real extension - ~10% of political filings are
            # Word docs (.docx traffic instructions etc.), not PDFs, and the
            # download URL's extension must match or FCC won't serve it.
            ext = f.get("file_extension") or "pdf"
            filename = f["file_name"]
            if f.get("file_extension"):
                filename = f"{filename}.{ext}"
            filing = FccFiling(
                file_id=f["file_manager_id"],
                download_url=f"https://publicfiles.fcc.gov/api/manager/download/{folder_id}/{f['file_manager_id']}.{ext}",
                filename=filename,
                category_path=current_path,
                updated_ts=f.get("create_ts"),
                callsign=callsign,
                service=service,
            )
            if since is not None:
                dt = filing.updated_dt
                if dt is not None and dt.date() < since:
                    continue
            out.append(filing)

        for sub in folder.get("subfolders") or []:
            if sub.get("file_count") == "0":
                continue
            name = (sub.get("folder_name") or "").strip()
            # At the root, immediate subfolders are election-cycle year
            # folders ("2017".."2026") - prune whole years below the cutoff
            # so we never walk them. Only applied at root, where names are
            # reliably years (a deeper folder could be a committee literally
            # named "2024"; the per-file guard above still bounds those).
            if is_root and since is not None:
                if name.isdigit() and len(name) == 4 and int(name) < since.year:
                    continue
            self._walk_folder(
                sub["entity_folder_id"], entity_id, callsign, service, out, since, f"{current_path}/{name}"
            )

    def fetch_station_feed(self, callsign: str, service: str) -> list[FccFiling]:
        """Fetch and parse a station's RSS feed - capped at the 10 most
        recent uploads (see module docstring). Kept for `probe_api.py` and
        as the entity-ID bootstrap for `walk_political_files()`; ingest.py
        uses the latter for actual filing discovery, not this.
        """
        url = self._rss_url(callsign, service)
        content = self._fetch_bytes(url)
        content = BARE_AMPERSAND_RE.sub("&amp;", content.decode("utf-8")).encode("utf-8")

        root = ET.fromstring(content)
        filings = []
        for entry in root.findall("atom:entry", ATOM_NS):
            title_el = entry.find("atom:title", ATOM_NS)
            id_el = entry.find("atom:id", ATOM_NS)
            link_el = entry.find("atom:link", ATOM_NS)
            updated_el = entry.find("atom:updated", ATOM_NS)
            content_el = entry.find("atom:content", ATOM_NS)

            title = title_el.text if title_el is not None else ""
            file_id = id_el.text if id_el is not None else None
            download_url = link_el.get("href") if link_el is not None else None
            updated_ts = updated_el.text if updated_el is not None else None

            if not file_id or not download_url:
                continue

            category_match = TITLE_CATEGORY_RE.search(title or "")
            category_path = category_match.group(1) if category_match else ""

            # The human-readable filename lives inside <content>'s xhtml div,
            # in a <strong> tag (e.g. "BOLD America NAB form.pdf") - but the
            # FIRST <strong> in that block wraps a link (the entity number),
            # so its .text is None; we want the first <strong> with actual
            # direct text content. The download URL itself is just a GUID,
            # not useful for browsing, so this is worth getting right.
            filename = None
            if content_el is not None:
                strongs = content_el.findall(".//xhtml:strong", XHTML_NS)
                text_strongs = [s.text.strip() for s in strongs if s.text and s.text.strip()]
                if text_strongs:
                    filename = text_strongs[0]
            if not filename:
                # Fall back to the GUID-based name from the URL if the
                # content block wasn't present/parseable as expected.
                filename = download_url.rsplit("/", 1)[-1]

            filings.append(FccFiling(
                file_id=file_id,
                download_url=download_url,
                filename=filename,
                category_path=category_path,
                updated_ts=updated_ts,
                callsign=callsign,
                service=service,
            ))
        return filings

    def download(self, filing: FccFiling) -> bytes:
        return self._fetch_bytes(filing.download_url)
