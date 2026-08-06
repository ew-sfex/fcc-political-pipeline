"""Run this manually (with real internet access, not in a sandboxed CI dry
run) to sanity-check the FCC search API's actual response shape before
trusting src/fcc_client.py's parsing.

Usage:
    python scripts/probe_api.py KGO-TV
"""
import json
import sys

import requests

SEARCH_URL = "https://www.fcc.gov/search/api"


def main():
    query = sys.argv[1] if len(sys.argv) > 1 else "KGO-TV"
    filters = json.dumps([{"political_file_type": "PA"}])
    params = {"q": query, "f": filters}

    resp = requests.get(SEARCH_URL, params=params, timeout=30)
    print(f"Status: {resp.status_code}")
    print(f"URL: {resp.url}")
    try:
        data = resp.json()
        print(json.dumps(data, indent=2)[:4000])
        print("\n--- Top-level keys in response ---")
        print(list(data.keys()) if isinstance(data, dict) else type(data))
    except Exception as e:
        print(f"Response was not JSON: {e}")
        print(resp.text[:2000])


if __name__ == "__main__":
    main()
