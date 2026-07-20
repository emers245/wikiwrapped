#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Mon Jun 30 14:55:27 2025

@author: Joe
"""

import pandas as pd
import gzip, json, requests, random, time, os
from collections import defaultdict, Counter
from tqdm import tqdm

#%% Get top articles
url = "https://dumps.wikimedia.org/other/pageviews/2025/2025-05/pageviews-20250501-000000.gz"
fn  = "pageviews-202505.gz"
os.system(f'curl -L -o {fn} {url}')
rows = []
with gzip.open(fn, "rt", encoding="utf-8", errors="replace") as f:
    for line in f:
        parts = line.rstrip("\n").split()
        if len(parts) != 4:
            continue
        proj, title, views, _ = parts
        if proj != "en":
            continue
        if title.startswith(("Special:", "Talk:", "File:")):
            continue
        rows.append((title, int(views)))
pv = pd.DataFrame(rows, columns=["title", "views"])
head_titles = pv.nlargest(30000, "views")["title"].tolist()

#%% Helpers
# Random title generator
def random_titles(n):
    while n > 0:
        k = min(500, n)
        data = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={"action":"query","list":"random","rnnamespace":0,
                    "rnlimit":k,"format":"json"},
            headers={"User-Agent":"WikiSampler/0.1"}
        ).json()
        for itm in data.get("query", {}).get("random", []):
            yield itm.get("title")
        n -= k
        time.sleep(0.1)

# Map titles to revision IDs
def titles_to_revids(titles, batch=20):
    rev_map = {}
    for i in range(0, len(titles), batch):
        block = titles[i:i+batch]
        # Directly inline MW API URL to avoid undefined variable
        resp = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={"action": "query", "prop": "revisions", "rvprop": "ids",
                    "titles": "|".join(block), "format": "json"},
            headers={"User-Agent": "WikiSampler/0.1"}
        ).json()
        pages = resp.get("query", {}).get("pages", {})
        for page in pages.values():
            if page.get("missing"):
                continue
            revs = page.get("revisions")
            if not revs:
                continue
            rev0 = revs[0]
            rev_id = rev0.get("revid")
            title = page.get("title")
            if title and rev_id is not None:
                rev_map[title] = str(rev_id)
        time.sleep(0.1)
    return rev_map

# Fetch ORES articletopic scores
def ores_topics(titles, batch=20):
    # Max requests 200/s
    title2rev = titles_to_revids(titles, batch)
    if not title2rev:
        return {}
    out = {}
    rev2title = {rev: title for title, rev in title2rev.items()}
    rev_ids = list(rev2title)
    for i in range(0, len(rev_ids), batch):
        chunk = rev_ids[i:i+batch]
        resp = requests.get(
            "https://ores.wikimedia.org/v3/scores/enwiki/?models=articletopic&features=false&format=json&revids=" + "|".join(chunk),
            headers={"User-Agent":"WikiSampler/0.1"}
        )
        if resp.status_code != 200:
            continue
        scores = resp.json().get("enwiki", {}).get("scores", {})
        for rid, blob in scores.items():
            try:
                score = blob["articletopic"]["score"]
                probs = score.get("probability", {})
                out[rev2title[rid]] = max(probs, key=probs.get)
            except Exception:
                continue
        time.sleep(0.1) #wait 0.1 seconds before making next request
    return out

# Collapse fine to macrotopic
def macro(label):
    if not label:
        return "Unscored"
    parts = label.split('.')
    return parts[0] if parts[0] != 'Culture' else '.'.join(parts[:2])

#%% Label head_titles
#print("Labeling head_titles...")
records = []
topic_counter = Counter()
batch_count = (len(head_titles) + 199) // 200
for chunk in tqdm((head_titles[i:i+200] for i in range(0, len(head_titles), 200)),total=batch_count, desc="Labeling head_titles"):
    fine = ores_topics(chunk)
    for title in chunk:
        title_spaces = title.replace('_',' ')
        m = macro(fine.get(title_spaces))
        records.append({'title': title_spaces, 'macrotopic': m, 'set': 'head'})
        topic_counter[m] += 1
print("Head topics:", dict(topic_counter))

#%% Sampling logic
samp_type = 'random'  # 'random' or 'random_strat'

if samp_type == 'random_strat':
    print("Starting stratified random sampling...")
    target = 400
    bucket = defaultdict(list)
    iter_n, t0 = 0, time.time()
    while True:
        iter_n += 1
        candidates = list(random_titles(1000))
        topics = ores_topics(candidates)
        for title in candidates:
            full = topics.get(title)
            if not full or title in head_titles:
                continue
            m = macro(full)
            if len(bucket[m]) < target:
                bucket[m].append(title)
                records.append({'title': title, 'macrotopic': m, 'set': 'tail'})
        print(f"\rBatch {iter_n}: total sampled = {len(records)}", end="")
        if all(len(lst) >= target for lst in bucket.values()) and len(bucket) >= 13:
            break
    print(f"\nDone stratified in {time.time()-t0:.1f}s")

elif samp_type == 'random':
    print("Starting simple random sampling...")
    TOTAL_TARGET = 10000
    sample = []
    seen = set(head_titles)
    t0 = time.time()
    while len(sample) < TOTAL_TARGET:
        for title in random_titles(500):
            if title in seen:
                continue
            sample.append(title);
            seen.add(title)
            if len(sample) == TOTAL_TARGET:
                break
    print(f"Collected {len(sample)} titles in {time.time()-t0:.1f}s")
    batch_count = TOTAL_TARGET // 200
    for chunk in tqdm((sample[i:i+200] for i in range(0, len(sample), 200)),total=batch_count, desc="Labeling tail_titles"):
        fine = ores_topics(chunk)
        for title in chunk:
            m = macro(fine.get(title))
            records.append({'title': title, 'macrotopic': m, 'set': 'tail'})
    print("\nDone random in {:.1f}s".format(time.time()-t0))
else:
    raise ValueError(f"Unknown samp_type: {samp_type}")

#%% Combine & save full sample DataFrame

df_sample = pd.DataFrame(records)
df_sample.to_pickle('sample_titles_with_topics.pkl')
print(f"Saved {len(df_sample)} records to sample_titles_with_topics.pkl")
# sample_titles list for downstream steps
sample_titles = df_sample['title'].tolist()
with open('sample_titles.json', 'w') as f:
    json.dump({'sample_titles': sample_titles}, f)

# view distribution
print(df_sample['macrotopic'].value_counts())

# Next: fetch summaries, embed, UMAP, cluster
