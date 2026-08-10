"""Entrypoint: pull Bay Area political filings from each station's complete
Political Files folder tree (via FccClient.walk_political_files - see
fcc_client.py) and record filing metadata in the DB. Already-ingested
filings are skipped (see already_ingested), so this is safe to run
repeatedly/frequently - each run only writes rows for filings not already
in the DB, regardless of how much of the tree is walked to find them.

PDF storage in Drive is deferred: FCC's Akamai layer blocks the file-download
endpoint for any automated client (confirmed 2026-08-07, including via a full
Playwright-driven Chromium - only a manually-driven browser tab succeeds).
The metadata API (which this uses) has no such block. Each row still records
`download_url` so PDFs can be fetched later, by hand or once FCC (contacted
re: developer@fcc.gov) confirms a sanctioned automated path. See README.md.

Run: python -m src.ingest
"""
from __future__ import annotations

import logging
import sys

from sqlalchemy import select

from . import config, notify
from .db import Filing, get_session, init_db
from .fcc_client import FccClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ingest")


def already_ingested(session, fcc_file_id: str) -> bool:
    stmt = select(Filing.id).where(Filing.fcc_file_id == fcc_file_id)
    return session.execute(stmt).first() is not None


def run():
    init_db()
    session = get_session()

    stations = config.load_stations()
    log.info("Loaded %d stations for market pull (filings since %s)", len(stations), config.BACKFILL_SINCE)

    new_filings: list[Filing] = []
    with FccClient() as fcc:
        _ingest_stations(fcc, session, stations, new_filings)

    log.info("Done. %d new filings ingested.", len(new_filings))
    notify.post_new_filings(new_filings)


def _ingest_stations(fcc, session, stations, new_filings: list) -> None:
    for station in stations:
        if len(new_filings) >= config.MAX_FILINGS_PER_RUN:
            log.warning("Hit MAX_FILINGS_PER_RUN cap (%d), stopping early", config.MAX_FILINGS_PER_RUN)
            break

        log.info("Walking Political Files tree for %s (%s)", station.callsign, station.service)
        try:
            filings = fcc.walk_political_files(station.callsign, station.service, since=config.BACKFILL_SINCE)
        except Exception:
            log.exception("Folder walk failed for %s - skipping station this run", station.callsign)
            continue

        log.info("%s: %d political filings found", station.callsign, len(filings))

        for filing in filings:
            if len(new_filings) >= config.MAX_FILINGS_PER_RUN:
                break
            if already_ingested(session, filing.file_id):
                continue

            row = Filing(
                fcc_file_id=filing.file_id,
                callsign=station.callsign,
                service=station.service,
                market=station.market,
                category_path=filing.category_path,
                campaign_year=filing.campaign_year,
                file_name=filing.filename,
                filed_date=filing.updated_dt,
                download_url=filing.download_url,
                purchaser=filing.purchaser,
            )
            session.add(row)
            session.commit()
            new_filings.append(row)
            log.info("Ingested %s (%s, purchaser=%s)", filing.filename, station.callsign, filing.purchaser)


if __name__ == "__main__":
    try:
        run()
    except Exception:
        log.exception("Ingest run failed")
        sys.exit(1)
