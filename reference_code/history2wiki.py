#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Sun Jun 29 00:46:54 2025

@author: Joe
"""

#!/usr/bin/env python3
import json
from pathlib import Path

SRC = Path("history_emerjoe1779.json")       # your original export
DST = Path("WikipediaHistory_emerjoe1779.json")     # new file to create

# --- 1. Read the export ------------------------------------------------------
with SRC.open(encoding="utf-8") as f:
    data = json.load(f)

# In a real Take-out export `data` is an object with one key:
#   { "Browser History": [ {url, title, time_usec}, … ] }
# For safety, handle either structure.
entries = data["Browser History"] if isinstance(data, dict) else data

# --- 2. Keep only Wikipedia article URLs ------------------------------------
wiki_entries = [
    item for item in entries
    if "wikipedia.org/wiki/" in item.get("url", "")
]

# --- 3. Save the subset ------------------------------------------------------
with DST.open("w", encoding="utf-8") as f:
    json.dump(wiki_entries, f, ensure_ascii=False, indent=2)

print(f"✔  Saved {len(wiki_entries)} items → {DST}")
