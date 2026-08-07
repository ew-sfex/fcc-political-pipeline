"""Manual sanity check for a station's FCC OPIF RSS feed - confirms the feed
loads, and prints parsed filings including the derived purchaser field.

Usage:
    python scripts/probe_api.py KGO-TV TV
"""
import sys

sys.path.insert(0, ".")

from src.fcc_client import FccClient  # noqa: E402


def main():
    callsign = sys.argv[1] if len(sys.argv) > 1 else "KGO-TV"
    service = sys.argv[2] if len(sys.argv) > 2 else "TV"

    with FccClient() as client:
        filings = client.fetch_station_feed(callsign, service)

    print(f"Fetched {len(filings)} entries for {callsign} ({service})\n")
    for f in filings[:10]:
        print(f"- {f.filename}")
        print(f"    purchaser: {f.purchaser}")
        print(f"    category:  {f.category_path}")
        print(f"    updated:   {f.updated_ts}")
        print(f"    url:       {f.download_url}")
        print()


if __name__ == "__main__":
    main()
