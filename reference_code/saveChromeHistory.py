#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Sat Jun 28 22:44:33 2025

@author: Joe
"""

#!/usr/bin/env python3
import sqlite3, json, pathlib, sys, datetime, tempfile, shutil

db_path = "/Users/Joe/Library/Application Support/Google/Chrome/Default/History"

tmp = tempfile.NamedTemporaryFile(delete=False)
shutil.copy2(db_path, tmp.name)

conn = sqlite3.connect(tmp.name)
cur  = conn.cursor()

q = """
SELECT url, title, last_visit_time
FROM urls
WHERE url LIKE '%wikipedia.org%'
ORDER BY last_visit_time DESC
"""
rows = cur.execute(q).fetchall()
conn.close()

def chromium_timestamp(t):
    # microseconds since 1601-01-01 -> ISO string
    unix = t/1_000_000 - 11644473600
    return datetime.datetime.utcfromtimestamp(unix).strftime("%Y-%m-%d %H:%M:%S")

out = [
    {"url": url,
     "title": title,
     "last_visit": chromium_timestamp(t)}
    for url, title, t in rows
]

json.dump(out, sys.stdout, indent=2)
