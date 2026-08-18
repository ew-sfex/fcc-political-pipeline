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
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from sqlalchemy import select

from . import config, notify
from .db import Filing, get_session, init_db
from .fcc_client import FccClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ingest")

# Per-station walks run concurrently: wall-time then tracks the single slowest
# station instead of the sum of all 14. This matters - FCC's folder API has
# gotten slow (a full sequential pass measured ~65 min on 2026-08-14, over the
# 60-min CI timeout), while a concurrent pass finished in ~17 min. Concurrency
# was verified NOT to lose data: per-station counts from a parallel run match a
# sequential run exactly. Kept modest (few workers) to be polite to the API and
# minimize the occasional transient folder-call failure under load (a failed
# station is logged and simply picked up on the next run via dedup, never
# silently dropped). Override with INGEST_WORKERS.
MAX_WORKERS = int(os.environ.get("INGEST_WORKERS", "4"))


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
        # 1. Resolve any missing entity IDs first, sequentially - the only step
        #    that uses the (non-thread-safe) browser. Cached IDs skip it.
        _resolve_missing_entity_ids(fcc, stations)
        ready = [s for s in stations if s.entity_id]

        # 2. Walk every station's tree concurrently (pure HTTP, own session each).
        results = _walk_all_stations(fcc, ready)

        # 3. Write new rows sequentially on the main thread (the DB session is
        #    not thread-safe), in stable config order.
        _write_new_filings(session, stations, results, new_filings)

    log.info("Done. %d new filings ingested.", len(new_filings))
    notify.post_new_filings(new_filings)


def _resolve_missing_entity_ids(fcc, stations) -> None:
    for station in stations:
        if station.entity_id:
            continue
        try:
            station.entity_id = fcc.resolve_entity_id(station.callsign, station.service)
            log.info("Resolved entity_id for %s: %s (add it to config to skip this next time)",
                     station.callsign, station.entity_id)
        except Exception:
            log.exception("Could not resolve entity_id for %s - skipping this run", station.callsign)


def _walk_all_stations(fcc, stations) -> dict[str, list]:
    """Return {callsign: [FccFiling, ...]} walking stations concurrently. Each
    worker uses its own requests.Session (Sessions aren't safe to share across
    threads). A station that errors is logged and omitted - never silently
    partial - and is re-collected on the next run."""
    results: dict[str, list] = {}

    def work(station):
        sess = requests.Session()
        try:
            return station.callsign, fcc.walk_political_files(
                station.callsign, station.service,
                since=config.BACKFILL_SINCE, entity_id=station.entity_id, session=sess,
            )
        finally:
            sess.close()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(work, s): s for s in stations}
        for fut in as_completed(futures):
            station = futures[fut]
            try:
                callsign, filings = fut.result()
                results[callsign] = filings
                log.info("%s: %d political filings found", callsign, len(filings))
            except Exception:
                log.exception("Folder walk failed for %s - skipping this run", station.callsign)
    return results


def _write_new_filings(session, stations, results, new_filings: list) -> None:
    for station in stations:
        filings = results.get(station.callsign)
        if not filings:
            continue
        for filing in filings:
            if len(new_filings) >= config.MAX_FILINGS_PER_RUN:
                log.warning("Hit MAX_FILINGS_PER_RUN cap (%d), stopping early", config.MAX_FILINGS_PER_RUN)
                return
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
