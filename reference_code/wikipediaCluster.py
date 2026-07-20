#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Mon Jun 30 10:12:22 2025

@author: Joe
"""

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wikipediaCluster.py

Load sample_titles_with_topics.pkl, fetch Wikipedia summaries via REST API,
compute embeddings with sentence-transformers/all-MiniLM-L6-v2, reduce to 2D,
and perform clustering.

Dependencies:
    pip install pandas requests sentence-transformers umap-learn scikit-learn matplotlib seaborn
"""

import pandas as pd
import requests
from time import sleep
from sentence_transformers import SentenceTransformer
import numpy as np
from sklearn.cluster import KMeans
import umap.umap_ as umap
import matplotlib.pyplot as plt
import seaborn as sns
from tqdm import tqdm

# Constants
PICKLE_FILE = 'sample_titles_with_topics.pkl'
REST_URL = 'https://en.wikipedia.org/api/rest_v1/page/summary/{}'
EMBED_MODEL = 'sentence-transformers/all-MiniLM-L6-v2'
N_CLUSTERS = 13 #10  # adjust as needed


def fetch_summary(title):
    """Fetch description and extract from Wikipedia REST API for a given title."""
    url = REST_URL.format(requests.utils.requote_uri(title))
    try:
        resp = requests.get(url, headers={'User-Agent': 'wikiCluster/1.0'})
        if resp.status_code == 200:
            data = resp.json()
            return data.get('description', ''), data.get('extract', '')
    except Exception as e:
        print(f"Error fetching {title}: {e}")
    return '', ''


# def main():
# Load data
df = pd.read_pickle(PICKLE_FILE)

# %% Fetch summaries
descriptions = []
extracts = []
for title in tqdm(df['title']):
    desc, ext = fetch_summary(title)
    descriptions.append(desc)
    extracts.append(ext)
    sleep(0.1)  # rate-limit

df['description'] = descriptions
df['extract'] = extracts
    
#%% Embed Text
# df = pd.read_pickle('sample_with_embeddings_clusters.pkl')

# Concatenate text for embedding
texts = (df['description'].fillna('') + '\n' + df['extract'].fillna('')).tolist()

# Compute embeddings
model = SentenceTransformer(EMBED_MODEL)
embeddings = model.encode(texts, show_progress_bar=True)

# Dimensionality reduction to 2D
reducer = umap.UMAP(n_components=2, random_state=42)
coords = reducer.fit_transform(embeddings)
df['x'] = coords[:, 0]
df['y'] = coords[:, 1]

# Clustering
kmeans = KMeans(n_clusters=N_CLUSTERS, random_state=42)
df['cluster'] = kmeans.fit_predict(coords)

# Save results
df.to_pickle('sample_with_embeddings_clusters.pkl')
print("Saved results to sample_with_embeddings_clusters.pkl")

# Plot
plt.figure(figsize=(10, 8))
palette = sns.color_palette('tab10', N_CLUSTERS)
sns.scatterplot(x='x', y='y', hue='cluster', data=df, legend='full', palette=palette, s=20)
plt.title('Wikipedia Articles Clusters')
plt.xlabel('UMAP 1')
plt.ylabel('UMAP 2')
plt.legend(title='Cluster')
plt.tight_layout()
plt.savefig('clusters_plot.png')
print("Saved visualization to clusters_plot.png")

# Plot
plt.figure(figsize=(10, 8))
palette = sns.color_palette('tab10', N_CLUSTERS)
sns.scatterplot(x='x', y='y', hue='macrotopic', data=df, legend='full', palette=palette, s=20)
plt.title('Wikipedia Macrotopics')
plt.xlabel('UMAP 1')
plt.ylabel('UMAP 2')
plt.legend(title='Macrotopic')
plt.tight_layout()
plt.savefig('macrotopics_plot.png')
print("Saved visualization to macrotopics_plot.png")

#from sklearn.metrics import silhouette_score

# X is your 2D array of shape (n_samples, 2), e.g.:
# X = np.vstack(df['embedding'].to_numpy())
# labels = color_codes    # the integer codes you already made

# sil_score = silhouette_score(X, labels)
# print("Average silhouette score:", sil_score)


# if __name__ == '__main__':
#     main()
