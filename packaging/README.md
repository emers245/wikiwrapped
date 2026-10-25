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
