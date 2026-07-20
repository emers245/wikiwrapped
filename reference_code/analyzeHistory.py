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

#%% Hourly Reading

import seaborn as sns, matplotlib.pyplot as plt

counts = (df.assign(day=df['timestamp'].dt.day_name(),
                    hour=df['timestamp'].dt.hour)
            .pivot_table(index='day', columns='hour', values='url', aggfunc='count')
            .reindex(['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']))

plt.figure(figsize=(12,4))
sns.heatmap(counts, cmap='YlGnBu', linewidths=.5)
plt.title('Wikipedia visits by weekday & hour'); plt.xlabel('Hour of day'); plt.ylabel('')

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

