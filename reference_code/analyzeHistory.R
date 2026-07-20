#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Sun Jun 29 01:12:38 2025

@author: Joe
"""

#!/usr/bin/env python3
"""
merge_wiki_history.py

Usage
-----
python merge_wiki_history.py \
       WikipediaHistory_emers245.json \
       WikipediaHistory_emerjoe1779.json \
       -o WikipediaHistory_merged.json
"""
import json, argparse, sys
from pathlib import Path

def load_history(path: Path):
    """Return a list of visit dicts from a Takeout-style JSON file."""
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    # Accept either structure:
    #   { "Browser History": [ … ] }  or  [ … ]
    if isinstance(data, dict) and "Browser History" in data:
        return data["Browser History"]
    if isinstance(data, list):
        return data
    raise ValueError(f"{path} has an unexpected JSON structure")

def merge(*files, dedup=True):
    """Merge multiple history lists; optionally drop duplicate URLs."""
    combined = []
    seen = set()            # url + time_usec  pairs

    for fp in files:
        for entry in load_history(fp):
            if dedup:
                key = (entry.get("url"), entry.get("time_usec"))
                if key in seen:
                    continue
                seen.add(key)
            combined.append(entry)
    return combined

if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Merge multiple WikipediaHistory JSON files")
    ap.add_argument("json_files", nargs="+",
                    help="input history files (*.json)")
    ap.add_argument("-o", "--out", default="-",
                    help="output file (default: stdout)")
    ap.add_argument("--keep-dupes", action="store_true",
                    help="retain duplicate (url,time_usec) entries")
    args = ap.parse_args()

    combined = merge(*(Path(p) for p in args.json_files),
                     dedup=not args.keep_dupes)

    # For Chrome-compatible format keep just the list, or wrap it again:
    # output_obj = {"Browser History": combined}
    output_obj = combined

    if args.out == "-":
        json.dump(output_obj, sys.stdout, ensure_ascii=False, indent=2)
    else:
        with Path(args.out).open("w", encoding="utf-8") as f:
            json.dump(output_obj, f, ensure_ascii=False, indent=2)
        print(f"Saved {len(combined)} entries to {args.out}")
