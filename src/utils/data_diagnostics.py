"""
Data Diagnostics
================

Lightweight diagnostic utility for inspecting anonymized model-ready JSON
instances used by the Academic Timetabling MILP repository.

The script does not modify the input file. It prints basic information about
sets, course-group pairs, required sessions, rooms, teachers, and aggregate
weekly capacity.

Usage
-----
python src/utils/data_diagnostics.py data/samples/isc_20251_sample.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict


DEFAULT_JSON = "data/samples/isc_20251_sample.json"


def load_json(path: str) -> Dict[str, Any]:
    """Load a JSON file using UTF-8 encoding."""
    with open(path, "r", encoding="utf-8") as file:
        return json.load(file)


def count_items(data: Dict[str, Any], key: str) -> int:
    """Return the number of items stored under a JSON key."""
    value = data.get(key, [])
    if isinstance(value, dict):
        return len(value)
    if isinstance(value, list):
        return len(value)
    return 0


def print_section(title: str) -> None:
    """Print a formatted console section title."""
    print()
    print(title)
    print("-" * len(title))


def main() -> int:
    """Run basic diagnostics for a model-ready JSON instance."""
    json_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_JSON
    path = Path(json_path)

    if not path.is_file():
        print("ERROR: JSON file was not found.")
        print(f"Configured path: {json_path}")
        return 1

    data = load_json(str(path))

    print_section("Instance file")
    print(f"Path: {path}")

    print_section("Set sizes")
    for key in ["P", "A", "AT", "AL", "M", "G", "MG", "D", "H"]:
        print(f"|{key}| = {count_items(data, key)}")

    print_section("Required sessions")
    hreq = data.get("Hreq", {}) or {}
    total_sessions = sum(int(value) for value in hreq.values())
    print(f"Hreq entries: {len(hreq)}")
    print(f"Total required sessions: {total_sessions}")

    print_section("Room capacities")
    cap_a = data.get("cap_A", {}) or {}
    if cap_a:
        capacities = [int(value) for value in cap_a.values()]
        print(f"Rooms with capacity: {len(capacities)}")
        print(f"Minimum capacity: {min(capacities)}")
        print(f"Maximum capacity: {max(capacities)}")
    else:
        print("No room-capacity data found.")

    print_section("Aggregate weekly capacity")
    teachers = count_items(data, "P")
    rooms = count_items(data, "A")
    days = count_items(data, "D")
    hours = count_items(data, "H")
    weekly_blocks = days * hours

    print(f"Weekly blocks: {weekly_blocks}")
    print(f"Teacher capacity: {teachers * weekly_blocks}")
    print(f"Room capacity: {rooms * weekly_blocks}")
    print(f"Required sessions: {total_sessions}")

    print_section("Consistency checks")
    mg_pairs = data.get("MG", []) or []
    mg_keys = {f"{course}|{group}" for course, group in mg_pairs}
    missing_hreq = sorted(mg_keys - set(hreq.keys()))

    if missing_hreq:
        print(f"WARNING: missing Hreq values for {len(missing_hreq)} course-group pairs.")
        print(f"Examples: {missing_hreq[:10]}")
    else:
        print("OK: Hreq covers all course-group pairs.")

    print()
    print("Diagnostics completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())