#!/usr/bin/env python3
"""
Conduit Database Cleanup - Wipe demo/test data.

Two modes:
  python cleanup_db.py          Interactive confirmation before deleting
  python cleanup_db.py --yes    Skip confirmation (for scripts/CI)

Uses the REST API (server must be running) or direct DB access.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(_PROJECT_ROOT / ".env")

DEFAULT_BASE_URL = "http://127.0.0.1:8000"


class C:
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    RED    = "\033[91m"
    BOLD   = "\033[1m"
    DIM    = "\033[2m"
    RESET  = "\033[0m"


def main():
    parser = argparse.ArgumentParser(description="Wipe Conduit demo data")
    parser.add_argument(
        "--base-url", default=DEFAULT_BASE_URL,
        help=f"Conduit API URL (default: {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip confirmation prompt",
    )
    args = parser.parse_args()

    api_key = os.environ.get("CONDUIT_API_KEY", "")
    if not api_key:
        print(f"{C.RED}CONDUIT_API_KEY not set. Check .env file.{C.RESET}")
        sys.exit(1)

    headers = {"X-API-Key": api_key}
    base = args.base_url.rstrip("/")

    # Check what's in the database
    print(f"\n{C.BOLD}Conduit Database Cleanup{C.RESET}")
    print(f"{C.DIM}Server: {base}{C.RESET}\n")

    try:
        r = requests.get(f"{base}/api/v1/admin/stats", headers=headers)
        r.raise_for_status()
        stats = r.json()
    except requests.ConnectionError:
        print(f"{C.RED}Cannot reach Conduit at {base}. Is the server running?{C.RESET}")
        sys.exit(1)
    except requests.HTTPError as e:
        print(f"{C.RED}Error: {e.response.status_code} - {e.response.text}{C.RESET}")
        sys.exit(1)

    print(f"  Skills:        {stats['skills']}")
    print(f"  Executions:    {stats['executions']}")
    print(f"  Ratings:       {stats['ratings']}")
    print(f"  Anomaly flags: {stats['anomaly_flags']}")
    print(f"  {C.BOLD}Total:         {stats['total']}{C.RESET}")
    print()

    if stats["total"] == 0:
        print(f"{C.GREEN}Database is already clean. Nothing to delete.{C.RESET}\n")
        sys.exit(0)

    # Confirm
    if not args.yes:
        answer = input(f"{C.YELLOW}Delete all {stats['total']} rows? [y/N] {C.RESET}").strip().lower()
        if answer not in ("y", "yes"):
            print("Cancelled.")
            sys.exit(0)

    # Wipe it
    r = requests.delete(f"{base}/api/v1/admin/reset-demo", headers=headers)
    r.raise_for_status()
    result = r.json()

    deleted = result["deleted"]
    print(f"\n{C.GREEN}Done!{C.RESET} Deleted:")
    print(f"  Ratings:       {deleted['ratings']}")
    print(f"  Executions:    {deleted['executions']}")
    print(f"  Skills:        {deleted['skills']}")
    print(f"  Anomaly flags: {deleted['anomaly_flags']}")
    print(f"  {C.BOLD}Total:         {deleted['total']}{C.RESET}\n")


if __name__ == "__main__":
    main()
