"""Environment and station-list config loading."""
import os
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
MAX_FILINGS_PER_RUN = int(os.environ.get("MAX_FILINGS_PER_RUN", "200"))
