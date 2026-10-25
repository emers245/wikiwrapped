# Packaging (maintainer notes)

How the installers are put together. Started in Milestone 1 (runtime
environment and locks); later milestones add the resources, the
constructor build, CI, and the release procedure.

## The pieces

| File | What it is |
|---|---|
| `build-tools.yml` | A separate conda env with the build tools (conda-lock, constructor, rattler-build, uv). Never mixed with the app's envs. |
| `runtime.yml` | What the installed app needs, and nothing else. The source of the installers. |
| `locks/<platform>.txt` | `runtime.yml` solved for `osx-64`, `osx-arm64`, `win-64`: explicit conda lock files, every package pinned by URL and hash. |
| `wheels/requirements.in` | The only pip-installed package: `pywebview` (conda-forge has no macOS build). |
| `wheels/requirements-<platform>.txt` | What pip installs for it on each platform, with sha256 hashes. Installed offline at install time. |
| `lock_wheels.py` | Regenerates the wheel locks from `requirements.in` and the conda locks. |

`environment.yml` (repo root) is the developer env: `runtime.yml` verbatim
plus dev tools. `tests/test_environment_specs.py` fails if the two drift,
if a lock no longer satisfies `runtime.yml`, if a lock contains a CUDA
pytorch or a dev-only package, or if pip and conda would ever install the
same package.

## Setup (once)

```bash
conda env create -f packaging/build-tools.yml
```

## Re-locking after changing a dependency

1. Edit `packaging/runtime.yml`.
2. Copy the same change into the runtime block of `environment.yml`.
3. Re-lock conda, then the wheels:

   ```bash
   conda run -n wikipedia-wrapped-build conda-lock lock \
       -f packaging/runtime.yml -p osx-64 -p osx-arm64 -p win-64 \
       --kind explicit --filename-template 'packaging/locks/{platform}.txt'
   conda run -n wikipedia-wrapped-build python packaging/lock_wheels.py
   ```

4. `pytest tests/test_environment_specs.py`.

**The fragile cluster** (python, numpy, numba, llvmlite, umap-learn,
scikit-learn, pynndescent) is pinned because the reference space's pickled
UMAP reducer depends on it. Changing any of them means rebuilding and
re-uploading the reference space; pytorch and sentence-transformers
likewise affect the embedding numbers the space was built from.

## Why each runtime dependency is there (import audit, 2026-10-24)

Audited by parsing every import in `config.py`, `app/`, `extract/` and
`scripts/enrich_pending.py`, plus recording what unpickling
`umap_reducer.pkl` imports.

| Package | Imported by |
|---|---|
| fastapi | `app/api.py` |
| uvicorn | `app/launcher.py` (lazy) |
| requests | `app/wiki_http.py`, `app/normalize.py` |
| numpy | `app/embeddings.py`, `app/storage.py`, `app/umap_space.py` |
| sentence-transformers (+ pytorch, transformers, tokenizers) | `app/embeddings.py` (lazy) |
| umap-learn (+ pynndescent, numba, llvmlite, scikit-learn, scipy) | unpickling the reducer in `app/umap_space.py` |
| networkx | `app/network.py` |
| tqdm | `app/embeddings.py`, `app/topics.py`, `app/pageviews.py` |
| tzlocal, platformdirs | `config.py` (lazy) |
| google-api-python-client, google-auth, google-auth-oauthlib | `app/drive.py` (lazy) |
| pywebview (pip) | `app/launcher.py` (lazy) |
| proxy-tools | pywebview; from conda-forge because PyPI has no wheel for it |
| pip | installs the pywebview wheels at install time |

**Not shipped**, with the evidence:

| Package | Why not |
|---|---|
| pytest, httpx | imported only by `tests/` (httpx still arrives as a transitive dependency of the Hugging Face stack) |
| matplotlib | imported nowhere; ad-hoc plotting only |
| pandas, pyarrow | imported nowhere in the repo, so dropped from the dev env too (2026-10-25). Unpickling the reducer imports pandas only because `sklearn.utils.fixes` tries `import pandas` when it is installed (pandas then pulls pyarrow); umap-learn does not depend on either. Verified: the runtime env (no pandas) unpickles the reducer and runs `transform()`. |
| python-multipart | not listed: FastAPI needs it only for form uploads, which the app has none of. It still arrives in the locks as a transitive dependency of the web stack, like httpx. |

## Sizes (Milestone 1 measurement, before the model and reference space)

| Platform | Conda download | Installed | Wheels | Biggest |
|---|---|---|---|---|
| osx-64 | 341 MB | 2.0 GB | 7.5 MB | mkl 119 MB, libtorch 50 MB, pytorch 23 MB |
| osx-arm64 | 200 MB | 1.1 GB | 7.5 MB | libtorch 32 MB, pytorch 23 MB, libllvm14 21 MB |
| win-64 | 345 MB | 1.8 GB | 0.9 MB | mkl 118 MB, libtorch 34 MB, pytorch 22 MB |

Installed sizes: osx-64 is a real install; the other two are the extracted
packages (`conda create --platform`), which is close to the installed size.
Intel macOS and Windows use MKL builds of pytorch (the only CPU builds
conda-forge has for Windows; the dev env that built the reference space
also uses MKL on Intel macOS), which is most of the difference from Apple
Silicon.

## Bundled resources (Milestone 2)

Two read-only resource sets ship inside every installer. Each is a folder
with a `manifest.json` holding a sha256 for every file
(`app/bundle.py`), and the installed app's `--self-test` checks both.

```
<resources>/models/all-MiniLM-L6-v2/   the sentence-transformers model (91.6 MB)
<resources>/reference/                 the UMAP reference space
```

`<resources>` is `config.resource_dir()`: in an install, the `resources`
folder next to the code; in a checkout, the repo's git-ignored `data/`.

### The model

- Pinned to one Hugging Face commit, `config.EMBEDDING_MODEL_REVISION`
  (`1110a243`, the revision that built the current reference space), and
  to the sha256 of each of its 10 files, `packaging/model.json`.
- `python scripts/fetch_model.py [--dest DIR]` downloads exactly those
  files and refuses a mismatch. `--record` rewrites `model.json`; use it
  only after bumping the revision on purpose, which also means rebuilding
  the reference space.
- `app/embeddings.py` loads the bundled copy when present (never touching
  the network). An installed app without it raises instead of
  downloading, with `HF_HUB_OFFLINE`/`TRANSFORMERS_OFFLINE` set. A checkout
  without it downloads the pinned revision (dev fallback).

### The reference space

Only three files are needed at runtime: `umap_reducer.pkl`,
`reference_points.json`, `reference_meta.json`. The build's
`reference_embeddings.npy` is never read by the app (the reducer pickle
holds what `.transform()` needs) and is not shipped.

| Step | Command |
|---|---|
| Package | `python scripts/package_reference_space.py --tag reference-vN` |
| Upload (you) | the two `gh` commands it prints |
| Fetch for a build | `python scripts/fetch_reference_space.py [--dest DIR] [--platform P]` |

`package_reference_space.py` writes
`packaging/build/reference-space-vN.tar.gz` (deterministic: same files,
same sha256) and the committed trust anchor `packaging/reference-space.json`
(asset name, sha256, versions). It refuses a space whose recorded library
versions differ from any lock file.

`fetch_reference_space.py` downloads the asset from the GitHub Release (the
repo is private: it needs `GH_TOKEN`/`GITHUB_TOKEN` or a logged-in `gh`),
then refuses it unless its sha256 equals the committed anchor, every file
matches its manifest, and its versions match the target platform's lock.
The anchor, not anything downloaded, is what is trusted: the archive holds
a pickle, and loading a pickle runs code.

`gh` is not installed by default: `conda install -c conda-forge gh`, then
`gh auth login`.

### The current anchor: the test build (`reference-v0`)

`packaging/reference-space.json` currently describes the July 2026 test
build (1,989 articles, 3.3 MB), packaged with `--versions-from-env`
because it predates builds recording their versions. Upload it so the
installer pipeline can be developed end to end:

```bash
python scripts/package_reference_space.py --tag reference-v0 --versions-from-env
gh release create reference-v0 --repo emers245/wikipedia-wrapped \
    --title "Reference space v0" --prerelease \
    --notes "Test build (1,989 articles); replace with reference-v1"
gh release upload reference-v0 packaging/build/reference-space-v0.tar.gz \
    --repo emers245/wikipedia-wrapped
python scripts/fetch_reference_space.py      # round trip from the release
```

### Building the full reference space (you, on your Mac)

Hours of Wikipedia API calls; resumable (re-running reuses the build's
caches). Run in the dev environment:

```bash
conda activate wikipedia-wrapped
cp -R data/reference data/reference-test-build   # optional: keep the test build
python scripts/fetch_model.py                    # the pinned model, verified
python scripts/build_reference_space.py          # 30,000 top + 10,000 random
python scripts/package_reference_space.py --tag reference-v1
gh release create reference-v1 --repo emers245/wikipedia-wrapped \
    --title "Reference space v1" --prerelease --notes "Full build"
gh release upload reference-v1 packaging/build/reference-space-v1.tar.gz \
    --repo emers245/wikipedia-wrapped
python scripts/fetch_reference_space.py          # round trip from the release
git add packaging/reference-space.json && git commit -m "Reference space v1"
```

The build's caches go to `data/reference-build.db`, never your app
database. Check afterwards:

1. The build ended with `fitting UMAP on N x 384` and
   `wrote reference space (N points)`, with N in the high 30,000s (40,000
   requested, less redirects, duplicates and pages with no summary).
2. `data/reference/reference_meta.json` has `"embedding_model_bundled": true`,
   `embedding_model_revision` equal to `config.EMBEDDING_MODEL_REVISION`,
   and a `versions` block.
3. Few unlabeled points:
   `python -c "import json; p=json.load(open('data/reference/reference_points.json')); print(sum(x['macro_topic'] is None for x in p), 'of', len(p))"`
   should print a small fraction (under ~5%).
4. `pytest tests/test_bundle_consistency.py` passes (the reducer loads and
   projects the fixture).
5. Launch the app from the checkout (`./run.sh`) and look at the map: the
   grey backdrop should be a dense, structured cloud, and your pages should
   fall in sensible neighborhoods.
6. `package_reference_space.py` accepted it (versions match every lock)
   and reports a size; expect very roughly 100-200 MB, mostly the pickled
   reducer.

### The embedding fixture

`app/bundle_fixture.json` holds 8 fixed texts and the vectors the pinned
model gave them (`scripts/make_embedding_fixture.py`). The tests and the
installed app's `--self-test` re-encode them and require cosine similarity
above 0.999 (`app/bundle.COSINE_THRESHOLD`). Measured 2026-10-25: 0.9999999
in both the dev env and the locked runtime env (osx-64, no pandas), with
the packaged reference space projecting them. Regenerate it only when the
model revision or the embedding stack changes on purpose.
