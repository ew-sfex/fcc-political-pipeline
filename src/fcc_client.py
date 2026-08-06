"""Client for the FCC Online Public Inspection File (OPIF) political file
search API.

IMPORTANT: This is not an officially documented API. The shape below (base
URL, `f` filter param as a JSON-encoded list of {field: value} dicts, and the
response fields) is reconstructed from observed traffic on
https://publicfiles.fcc.gov, cross-referenced with third-party writeups (see
README). Treat every field/param name here as "best known guess, verify
before trusting at scale." Run `scripts/probe_api.py` against the live
endpoint to confirm before relying on this in production.

Known response fields (per observed API behavior):
    file_id, file_name, file_extension, file_size, file_status, folder_id,
    create_ts, last_update_ts, file_manager_id, file_folder_path,
    full_qualified_file_name, entity_id, source_service_code,
    network_affiliation, nielsen_dma_rank, callsign, political_file_type,
    office_type, campaign_year
"""
from __future__ import annotations

import json
from datetime import datetime
from dataclasses import dataclass

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

SEARCH_URL = "https://www.fcc.gov/search/api"
DOWNLOAD_URL = "https://publicfiles.fcc.gov/api/manager/download"

# Political file type codes observed in the wild. "PA" showed up for
# presidential-cycle filings in the one documented example available;
# political files also use type codes for state/local races. VERIFY the
# full code list against a live query before assuming this is exhaustive.
POLITICAL_FILE_TYPES = ["PA", "PI"]  # PA: political-ad-adjacent, PI: political issue (guess - verify)


@dataclass
class FccFiling:
    file_id: str
    file_name: str
    file_extension: str
    folder_id: str
    file_manager_id: str
    entity_id: str
    callsign: str
    source_service_code: str
    nielsen_dma_rank: str
    political_file_type: str
    office_type: str
    campaign_year: str
    create_ts: str

    @property
    def filed_date(self) -> datetime | None:
        if not self.create_ts:
            return None
        try:
            return datetime.fromisoformat(self.create_ts.replace("Z", "+00:00"))
        except ValueError:
            return None

    @property
    def download_url(self) -> str:
        return f"{DOWNLOAD_URL}?folder_id={self.folder_id}&file_manager_id={self.file_manager_id}"


class FccClient:
    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()

    @retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=1, min=2, max=30))
    def search(
        self,
        query: str,
        campaign_year: str | None = None,
        source_service_code: str | None = None,
        political_file_types: list[str] | None = None,
        page: int = 0,
    ) -> list[FccFiling]:
        """Search the political file index.

        `query` is typically a callsign (narrows results to that station's
        entity) - the FCC search matches call sign, entity name, and file
        path/name text.
        """
        filters = []
        for pft in (political_file_types or POLITICAL_FILE_TYPES):
            filters.append({"political_file_type": pft})
        if source_service_code:
            filters.append({"source_service_code": source_service_code})
        if campaign_year:
            filters.append({"campaign_year": campaign_year})

        params = {
            "q": query,
            "f": json.dumps(filters),
            "page": page,
        }
        resp = self.session.get(SEARCH_URL, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        # NOTE: the exact envelope key holding the result list is unconfirmed
        # (could be "results", "docs", "items" depending on backend - this is
        # a Solr-backed search per the `_version_`/`score` fields observed).
        # Adjust once verified via probe_api.py.
        results = data.get("results") or data.get("docs") or data.get("items") or []

        filings = []
        for r in results:
            filings.append(FccFiling(
                file_id=r.get("file_id"),
                file_name=r.get("file_name"),
                file_extension=r.get("file_extension"),
                folder_id=r.get("folder_id"),
                file_manager_id=r.get("file_manager_id"),
                entity_id=r.get("entity_id"),
                callsign=r.get("callsign"),
                source_service_code=r.get("source_service_code"),
                nielsen_dma_rank=r.get("nielsen_dma_rank"),
                political_file_type=r.get("political_file_type"),
                office_type=r.get("office_type"),
                campaign_year=r.get("campaign_year"),
                create_ts=r.get("create_ts"),
            ))
        return filings

    @retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=1, min=2, max=30))
    def download(self, filing: FccFiling) -> bytes:
        resp = self.session.get(filing.download_url, timeout=60)
        resp.raise_for_status()
        return resp.content
