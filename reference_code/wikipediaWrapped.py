#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Sun Jun 29 01:12:38 2025

@author: Joe
"""

import json, pandas as pd
from pathlib import Path
import numpy as np

FILES = ["WikipediaHistory_emers245.json",
         "WikipediaHistory_emerjoe1779.json"]

EPOCH_OFFSET_US = 11644473600 * 1_000_000   # 1601 → 1970 in µs

def load_one(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    rows = data["Browser History"] if isinstance(data, dict) else data
    df   = pd.DataFrame(rows)

    # ---- pick whichever time field exists and convert ---------------------
    if "time_usec" in df.columns:              # µs since 1601
        us  = pd.to_numeric(df["time_usec"], errors="coerce")
        unix_us = us - EPOCH_OFFSET_US
        df["timestamp"] = pd.to_datetime(unix_us, unit="us", utc=True,
                                         errors="coerce")

    elif "visitTime" in df.columns:            # ms since 1970 (floats OK)
        ms  = pd.to_numeric(df["visitTime"], errors="coerce")
        df["timestamp"] = pd.to_datetime(ms, unit="ms", utc=True,
                                         errors="coerce")

    elif "lastVisitTime" in df.columns:        # same flavour
        ms  = pd.to_numeric(df["lastVisitTime"], errors="coerce")
        df["timestamp"] = pd.to_datetime(ms, unit="ms", utc=True,
                                         errors="coerce")

    else:
        raise ValueError(f"{path}: no recognised time column")

    return df[["timestamp", "url", "title"]]

# ---------- merge & clean ---------------------------------------------------
df = (pd.concat([load_one(Path(p)) for p in FILES], ignore_index=True)
        .dropna(subset=["timestamp"])
        .drop_duplicates(subset=["url", "timestamp"])
        .reset_index(drop=True))

print(df.head(), "\nTotal rows:", len(df))
#%% #### BASIC STATS ####

#%% Hourly Reading

import seaborn as sns, matplotlib.pyplot as plt

# Convert timestamp to local time (UTC-6)
df['local_time'] = df['timestamp'].dt.tz_convert('Etc/GMT+6')  # Note: GMT+6 = UTC-6

# Extract local hour and day
df['hour'] = df['local_time'].dt.hour
df['day'] = df['local_time'].dt.day_name()

# Count visits by weekday and hour
counts = (df.pivot_table(index='day', columns='hour', values='url', aggfunc='count')
            .reindex(['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']))

# Normalize each day's counts to sum to 1 (density)
density = counts.div(counts.sum(axis=1), axis=0)

# Plot heatmap
plt.figure(figsize=(12, 4))
sns.heatmap(density, cmap='YlGnBu', linewidths=.5)
plt.title('Wikipedia visit density by weekday & hour (UTC-6)')
plt.xlabel('Hour of day')
plt.ylabel('')

#%% Binge Streaks

daily = df.set_index('timestamp').resample('D').size().rolling(7).mean()
daily.plot(figsize=(12,4)); plt.ylabel('7-day avg. pages/day'); plt.title('Reading streaks')

#%% Top N Articles

top = df['title'].value_counts().head(15)
top[::-1].plot.barh(figsize=(8,6)); plt.xlabel('visit count'); plt.title('Most revisited articles')
plt.tight_layout()

#%% Network

import networkx as nx, plotly.graph_objs as go
pairs = (df.sort_values('timestamp')
           .assign(next_url=df['url'].shift(-1),
                   deltat = df['timestamp'].diff().shift(-1).dt.total_seconds())
           .query('deltat <= 900'))
G = nx.from_pandas_edgelist(pairs, 'url', 'next_url', create_using=nx.DiGraph)
# use nx.spring_layout + Plotly scatter for an interactive network

#%% #### CATEGORY ANALYSIS ####

#%% Topic-bucket distribution (ORES articletopic) ───────────────────────────
#
# New figure: “Distribution of pages by Wikipedia ORES ‘ArticleTopic’”
# -----------------------------------------------------------------------------
# Requirements: requests   (pip install requests)
#               pandas     (already in use)
#               matplotlib (already in use)

import requests, time, json
from pathlib import Path
from collections import Counter

HEADERS = {"User-Agent": "WikiSampler/0.1 (emerjoe1779@gmail.com)"}

MW_API   = "https://en.wikipedia.org/w/api.php"
ORES_API = (
    "https://ores.wikimedia.org/v3/scores/enwiki/"
    "?models=articletopic&features=false&format=json&revids="
)

# ---------------------------------------------------------------------------
# Helper 1 ─ Titles → revision-IDs (≤20 at a time, the ORES limit)
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Helper 2 ─ ORES call → {title: top_topic}
# ---------------------------------------------------------------------------
def ores_topics(titles, batch=20):
    title2rev = titles_to_revids(titles, batch)
    if not title2rev:
        return {}
    out = {}
    rev2title = {rev: title for title, rev in title2rev.items()}
    rev_ids = list(rev2title)
    for i in range(0, len(rev_ids), batch):
        chunk = rev_ids[i:i+batch]
        resp = requests.get(
            ORES_API + "|".join(chunk),
            headers=HEADERS
        )
        if resp.status_code != 200:
            print("ORES error:", resp.status_code, resp.text[:200])
            continue
        scores = resp.json().get("enwiki", {}).get("scores", {})
        for rid, blob in scores.items():
            try:
                score = blob["articletopic"]["score"]
                probs = score.get("probability", {})
                out[rev2title[rid]] = max(probs, key=probs.get)
            except Exception:
                continue
        time.sleep(0.1)
    return out


# ---------------------------------------------------------------------------
# 1.  Get **unique** pages you read
# ---------------------------------------------------------------------------
from urllib.parse import urlparse, unquote

def url_to_title(url):
    path = urlparse(url).path  # e.g. "/wiki/Jain_vegetarianism"
    slug = path.split("/wiki/")[-1].split("#")[0]
    return unquote(slug).replace("_", " ")

unique_titles = (
    df["url"]
      .map(url_to_title)
      .drop_duplicates()
      .tolist()
)

# ---------------------------------------------------------------------------
# 2.  Pull a topic label for each page (with caching on disk)
# ---------------------------------------------------------------------------
CACHE = Path("wiki_ores_cache.json")
if CACHE.exists():
    topic_cache = json.loads(CACHE.read_text())
else:
    topic_cache = {}

missing = [t for t in unique_titles if t not in topic_cache]
if missing:
    topic_cache.update(ores_topics(missing))
    CACHE.write_text(json.dumps(topic_cache, indent=2))

# ---------------------------------------------------------------------------
# 3.  Count pages per requested macro-bucket
# ---------------------------------------------------------------------------
BUCKETS = [
    "Culture.Biography",
    "Culture.Food and drink",
    "Culture.Internet culture",
    "Culture.Linguistics",
    "Culture.Literature",
    "Culture.Media",
    "Culture.Performing arts",
    "Culture.Philosophy and religion",
    "Culture.Sports",
    "Culture.Visual arts",
    "Geography",
    "History and Society",
    "STEM",
]

topic_counts = Counter()
for title in unique_titles:
    label = topic_cache.get(title)
    if label is not None:
        label_parts = label.split('.')
        if label_parts[0] != 'Culture':
            macro = label_parts[0]
            #print(macro)
        else:
            macro = label_parts[0] + '.' + label_parts[1]
    else:
        macro=None
    if macro in BUCKETS:        # ignore “Other” or unexpected labels
        topic_counts[macro] += 1
    else:
        topic_counts["Unclassified"] += 1
        print(macro)
        
    # add to dataframe
    

# ---------------------------------------------------------------------------
# 4.  Plot
# ---------------------------------------------------------------------------
topic_series = (
    pd.Series(topic_counts)
      .reindex(BUCKETS + ["Unclassified"])
      .fillna(0)
      .sort_values(ascending=True)
)

plt.figure(figsize=(9, 6))
topic_series.plot.barh()
plt.xlabel("Unique articles")
plt.title("Distribution of pages by ORES ‘ArticleTopic’ (top bucket)")
plt.tight_layout()

#%% Add topics to dataframe

# 1.  extract clean title and map to ORES label
df["page_title"]   = df["url"].map(url_to_title)
df["ores_label"]   = df["page_title"].map(topic_cache)

# 2.  split the label at the first “.”
splits = df["ores_label"].str.split(".", n=1, expand=True)
# splits[0] is everything before the dot, splits[1] is the remainder (if any)

# 3.  build the macro column:
#    – start by assuming it’s just the first part
df["ores_macro"] = splits[0]

#    – but for “Culture”, keep the full second-level, too
culture_mask = df["ores_macro"] == "Culture"
splits2 = df["ores_label"].str.split(".", n=2, expand=True)
df.loc[culture_mask, "ores_macro"] = splits2.loc[culture_mask, 0] + '.' + splits2.loc[culture_mask, 1]

# 4.  fill any missing labels/macros with “Unclassified”
df["ores_label"] = df["ores_label"].fillna("Unclassified")
df["ores_macro"] = df["ores_macro"].fillna("Unclassified")

# %% Summaries  ── add short textual context for every page -------------------
import requests, time, urllib.parse

API_BASE   = "https://en.wikipedia.org/api/rest_v1/page/summary/"
HEADERS    = {
    "User-Agent": (
        "WikipediaWrapped/0.1 (emers245+emerjoe1779 project; "
        "https://github.com/your-repo) requests"
    ),
    "Accept": "application/json"
}

from typing import Union

def canonical_title(url: str) -> Union[str, None]:

    """
    Pull clean Wiki page title from a full URL.
    Returns None for non-article links (images, special pages, etc.).
    """
    try:
        path = urllib.parse.urlparse(url).path            # /wiki/Title_here
        if not path.startswith("/wiki/"):        # skip media/upload/special
            return None
        title = path.split("/wiki/")[1].split("#")[0]      # drop fragments
        if title.startswith(("File:", "Special:", "Help:")):
            return None
        return urllib.parse.unquote(title)                 # keep underscores
    except Exception:
        return None

# — prepare a unique list of titles we still need ­­­­­­­­­­­­­­­­­­­­­­­­­­­­­­­­­
df["page_title"] = df["url"].map(canonical_title)
todo            = df.loc[df["page_title"].notna(), "page_title"].unique()

summary_cache   = {}          # simple in-memory cache so we hit the API once

for t in todo:
    if t in summary_cache:               # already fetched in this session
        continue
    resp = requests.get(API_BASE + urllib.parse.quote(t), headers=HEADERS)
    if resp.status_code == 404:
        summary_cache[t] = {"description": None, "extract": None}
    elif resp.status_code == 429:
        # Wikimedia usually allows ~200 reqs/min.  Back off & retry once.
        time.sleep(3)
        resp = requests.get(API_BASE + urllib.parse.quote(t), headers=HEADERS)
    resp.raise_for_status()              # will raise for any non-2xx left
    data = resp.json()
    summary_cache[t] = {
        "description": data.get("description"),
        "extract":     data.get("extract")
    }
    time.sleep(0.1)                      # polite 10 req/s throttle

# — map cached summaries back onto the main dataframe ­­­­­­­­­­­­­­­­­­­­­­­­­
df["description"] = df["page_title"].map(lambda k: summary_cache.get(k, {}).get("description"))
df["summary"]     = df["page_title"].map(lambda k: summary_cache.get(k, {}).get("extract"))

print(df[["title", "description", "summary"]].head())
df.to_pickle("wiki_with_summaries.pkl")    # optional persistence

#%% Now Generate Text Embeddings for Summaries

# %% 1 ─ load your dataframe with summaries ─────────────────────────────────
from pathlib import Path

df_path = Path("wiki_with_summaries.pkl")         # created in step 1
df      = pd.read_pickle(df_path)

# %% 2 ─ prepare the text payload (description + summary) ───────────────────
def make_payload(row):
    return " ".join(
        str(x) for x in (row.get("description", ""), row.get("summary", ""))
        if x
    ).strip()

df["payload"] = df.apply(make_payload, axis=1)

# %% 3 ─ load the lightweight embedding model ───────────────────────────────
from sentence_transformers import SentenceTransformer, models
import torch, numpy as np
from tqdm import tqdm

device = "cpu"     # will happily use cuda if you have one, but cpu is fine
model  = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2",
                             device=device)

# Normalise (unit-length) right in the model pipeline
model._first_module().auto_truncate = True        # truncate >512 tokens

# %% 4 ─ batch-encode with a progress bar ───────────────────────────────────
BATCH = 64                                        # tweak lower if RAM tight
emb_list = []

for start in tqdm(range(0, len(df), BATCH), unit="batch"):
    chunk = df["payload"].iloc[start:start+BATCH].tolist()
    vecs  = model.encode(
        chunk,
        batch_size=len(chunk),
        convert_to_numpy=True,
        normalize_embeddings=True,               # cosine ↔ Euclidean
        show_progress_bar=False
    )
    emb_list.append(vecs)

embeddings = np.vstack(emb_list)                  # shape (N, 384)

# %% 5 ─ attach & persist ───────────────────────────────────────────────────
df["embedding"] = list(embeddings)                # object column w/ numpy rows
df.to_pickle("wiki_with_embeddings_light.pkl")
print("Saved", len(df), "embeddings ✔")

# %% 1 ─ load embeddings saved earlier

df = pd.read_pickle("wiki_with_embeddings_light.pkl")   # has 'embedding' col
X  = np.vstack(df["embedding"].to_numpy())              # shape (N, 384)

# %% 2 ─ run UMAP on CPU (change 'method' to 'umap_gpu' if you have a GPU)
import umap

umap_model = umap.UMAP(
    n_neighbors=30,        # how many neighbours define "local" (10-50 works)
    min_dist=0.1,          # ≤0.1 keeps clusters tight; larger spreads them
    n_components=2,        # 2-D map
    metric="cosine",       # matches cosine-normalised vectors
    random_state=42,        # reproducible layout
    verbose=True          # ← UMAP prints its own progress info
)

embedding_2d = umap_model.fit_transform(X)

df["x"], df["y"] = embedding_2d[:,0], embedding_2d[:,1]
df.to_pickle("wiki_umap.pkl")
print("UMAP done → saved x,y columns ✅")

#%% 3 - Visualize

fig, ax = plt.subplots()
ax.scatter(df["x"],df["y"])
ax.set_xlabel('UMAP-1')
ax.set_ylabel('UMAP-2')

#%% Clusterize

X  = np.vstack(df["embedding"].values)      # shape (N, 384)

# 2. run HDBSCAN with cosine metric
import hdbscan

clusterer = hdbscan.HDBSCAN(
    metric="euclidean",       # because vectors are unit-normalised ⇒ cos == L2
    min_cluster_size=30,      # tweak: smaller = more, finer clusters
    min_samples=10,           # higher = stricter, more noise (-1)
    cluster_selection_method="eom",   # default: “Excess of Mass”
    prediction_data=True
).fit(X)

df["cluster"]        = clusterer.labels_            # −1 = noise
df["cluster_prob"]   = clusterer.probabilities_     # 0-1 confidence

# quick peek
print(df["cluster"].value_counts().head())

#%% Create human-readable cluster labels

from sklearn.feature_extraction.text import TfidfVectorizer

def top_keywords(texts, k=4):
    vec = TfidfVectorizer(stop_words="english", max_features=2000).fit(texts)
    tfidf = vec.transform(texts).mean(axis=0).A1
    top_ids = tfidf.argsort()[::-1][:k]
    return [vec.get_feature_names_out()[i] for i in top_ids]

label_map = {}
for cid in sorted(df["cluster"].unique()):
    if cid == -1: continue
    texts = df.loc[df["cluster"] == cid, "summary"].fillna("").tolist()
    label_map[cid] = ", ".join(top_keywords(texts))
df["cluster_label"] = df["cluster"].map(label_map).fillna("Noise")

#%% Visualize Clusters

palette = plt.cm.get_cmap("tab20").colors
cluster_ids = sorted(c for c in df["cluster"].unique() if c != -1)
color_map   = {
    cid: palette[i % len(palette)]         # cycle when > 20 clusters
    for i, cid in enumerate(cluster_ids)
}

# noise / unclustered points
noise_color = (0.6, 0.6, 0.6, 0.4)         # semi-transparent grey

# --- plot -------------------------------------------------------------------
fig, ax = plt.subplots(figsize=(10, 10))

# 1) scatter non-noise clusters
for cid in cluster_ids:
    mask = df["cluster"] == cid
    ax.scatter(
        df.loc[mask, "x"],
        df.loc[mask, "y"],
        s         = 6,                     # marker size
        color     = color_map[cid],
        label     = df.loc[mask,'cluster_label'].unique(),
        alpha     = 0.8,
        linewidth = 0
    )

# 2) scatter noise last so it doesn't obscure real clusters
noise_mask = df["cluster"] == -1
ax.scatter(
    df.loc[noise_mask, "x"],
    df.loc[noise_mask, "y"],
    s         = 4,
    color     = noise_color,
    label     = "Noise (unclustered)",
    linewidth = 0
)

# --- cosmetics --------------------------------------------------------------
ax.set_aspect("equal", "box")
ax.set_xlabel("UMAP-1")
ax.set_ylabel("UMAP-2")
ax.set_title("Wikipedia pages clustered with HDBSCAN (UMAP layout)")
ax.legend(
    loc="upper right",
    fontsize="small",
    markerscale=2,
    frameon=True,
    framealpha=0.9
)

plt.tight_layout()
plt.show()

#%% Visualize by topic

color_codes, uniques = pd.factorize(df['ores_macro'])
fig, ax = plt.subplots(figsize=(8,4))
sc = ax.scatter(df["x"], df["y"],
                c=color_codes,
                cmap='tab20',
                s=5,
                #edgecolor='k',
                linewidth=0.2)
ax.set_aspect('equal')

ax.set_xlabel('UMAP-1')
ax.set_ylabel('UMAP-2')

# this creates one handle per unique color
handles, _ = sc.legend_elements()

# now override the labels with your actual category names
ax.legend(handles, uniques,
          title="Macro bucket",
          bbox_to_anchor=(1.05, 1), loc='upper left',
          borderaxespad=0.1)

plt.tight_layout()
plt.show()

from sklearn.metrics import silhouette_score

# X is your 2D array of shape (n_samples, 2), e.g.:
X = np.vstack(df['embedding'].to_numpy())
labels = color_codes    # the integer codes you already made

sil_score = silhouette_score(X, labels)
print("Average silhouette score:", sil_score)
