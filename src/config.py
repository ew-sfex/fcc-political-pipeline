"""Environment and station-list config loading."""
import os
from datetime import date
import yaml
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Station:
    callsign: str
    service: str
    market: str


def load_stations(path: str | None = None) -> list[Station]:
    path = path or os.environ.get("STATION_CONFIG_PATH", "config/bay_area_stations.yaml")
    with open(path) as f:
        raw = yaml.safe_load(f)
    market = raw["market"]
    return [
        Station(callsign=s["callsign"], service=s["service"], market=market)
        for s in raw["stations"]
    ]


DATABASE_URL = os.environ.get("DATABASE_URL") or "sqlite:///pipeline.db"
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
DRIVE_ROOT_FOLDER_ID = os.environ.get("DRIVE_ROOT_FOLDER_ID")
# Safety valve on new rows written per run, not on how much of the folder
# tree gets walked to find them (that's bounded by file_count pruning, not
# this). Raised from 200 now that discovery is a full folder-tree walk
# rather than a 10-item feed window - the first run against any station's
# full history could otherwise take days to catch up across cap-limited runs.
MAX_FILINGS_PER_RUN = int(os.environ.get("MAX_FILINGS_PER_RUN", "20000"))

# Only ingest filings uploaded on/after this date (by FCC create timestamp).
# Older year-folders are pruned without walking. ISO date, YYYY-MM-DD.
BACKFILL_SINCE = date.fromisoformat(os.environ.get("BACKFILL_SINCE", "2025-01-01"))

# Slack incoming-webhook URL for new-filing alerts. Unset -> alerts are a
# no-op (fine for local runs). Set as a GitHub Actions secret in production.
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")

# When true, skip Slack alerts for this run regardless of new-filing count.
# Set for the one-off history backfill so it doesn't fire a giant alert;
# leave unset for scheduled incremental runs.
SUPPRESS_ALERTS = os.environ.get("SUPPRESS_ALERTS", "").lower() in ("1", "true", "yes")
