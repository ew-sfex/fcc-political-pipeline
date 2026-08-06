"""Entrypoint: pull new Bay Area political filings from the FCC, upload to
Drive with tagging properties, and record in the DB.

Run: python -m src.ingest
"""
from __future__ import annotations

import logging
import sys

from sqlalchemy import select

from . import config
from .db import Filing, get_session, init_db
from .drive_client import DriveClient
from .fcc_client import FccClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ingest")


def already_ingested(session, fcc_file_id: str) -> bool:
    stmt = select(Filing.id).where(Filing.fcc_file_id == fcc_file_id)
    return session.execute(stmt).first() is not None


def run():
    init_db()
    session = get_session()
    fcc = FccClient()
    drive = DriveClient()

    stations = config.load_stations()
    log.info("Loaded %d stations for market pull", len(stations))

    processed = 0
    for station in stations:
        if processed >= config.MAX_FILINGS_PER_RUN:
            log.warning("Hit MAX_FILINGS_PER_RUN cap (%d), stopping early", config.MAX_FILINGS_PER_RUN)
            break

        log.info("Querying FCC filings for %s (%s)", station.callsign, station.service)
        try:
            filings = fcc.search(query=station.callsign, source_service_code=station.service)
        except Exception:
            log.exception("FCC search failed for %s - skipping station this run", station.callsign)
            continue

        station_folder_id = drive.get_or_create_subfolder(station.callsign, config.DRIVE_ROOT_FOLDER_ID)

        for filing in filings:
            if processed >= config.MAX_FILINGS_PER_RUN:
                break
            if not filing.file_id:
                continue
            if already_ingested(session, filing.file_id):
                continue

            log.info("New filing %s / %s - downloading", station.callsign, filing.file_name)
            try:
                pdf_bytes = fcc.download(filing)
            except Exception:
                log.exception("Download failed for file_id=%s - skipping", filing.file_id)
                continue

            properties = {
                "station": station.callsign,
                "market": station.market,
                "fcc_file_id": filing.file_id,
                "political_file_type": filing.political_file_type,
                "office_type": filing.office_type,
                "campaign_year": filing.campaign_year,
                # purchaser is unknown at ingest time - filled in by Phase 2
                # LLM extraction and re-applied to the Drive file then.
                "purchaser": "unknown",
            }

            try:
                drive_id, web_link = drive.upload_pdf(
                    pdf_bytes,
                    filename=filing.file_name or f"{filing.file_id}.pdf",
                    parent_folder_id=station_folder_id,
                    properties=properties,
                )
            except Exception:
                log.exception("Drive upload failed for file_id=%s - skipping", filing.file_id)
                continue

            row = Filing(
                fcc_file_id=filing.file_id,
                folder_id=filing.folder_id,
                file_manager_id=filing.file_manager_id,
                entity_id=filing.entity_id,
                callsign=station.callsign,
                service=station.service,
                market=station.market,
                political_file_type=filing.political_file_type,
                office_type=filing.office_type,
                campaign_year=filing.campaign_year,
                file_name=filing.file_name,
                file_extension=filing.file_extension,
                filed_date=filing.filed_date,
                drive_file_id=drive_id,
                drive_web_link=web_link,
            )
            session.add(row)
            session.commit()
            processed += 1
            log.info("Ingested %s (%s) -> Drive %s", filing.file_name, station.callsign, drive_id)

    log.info("Done. %d new filings ingested.", processed)


if __name__ == "__main__":
    try:
        run()
    except Exception:
        log.exception("Ingest run failed")
        sys.exit(1)
