# Packaging (maintainer notes)

How the installers are put together: the runtime environment and locks
(Milestone 1), the bundled resources (Milestone 2), and the installer
build itself (Milestone 3). CI and the release procedure come next.

## The pieces

| File | What it is |
|---|---|
| `build-tools.yml` | A separate conda env with the build tools (conda-lock, constructor, rattler-build, uv). Never mixed with the app's envs. |
| `runtime.yml` | What the installed app needs, and nothing else. The source of the installers. |
| `locks/<platform>.txt` | `runtime.yml` solved for `osx-64`, `osx-arm64`, `win-64`: explicit conda lock files, every package pinned by URL and hash. |
| `wheels/requirements.in` | The only pip-installed package: `pywebview` (conda-forge has no macOS build). |
| `wheels/requirements-<platform>.txt` | What pip installs for it on each platform, with sha256 hashes. Installed offline at install time. |
| `lock_wheels.py` | Regenerates the wheel locks from `requirements.in` and the conda locks. |
| `model.json`, `reference-space.json` | The pins for the bundled model and reference space (Milestone 2). |
| `build_installer.py` | Builds the installer for the machine it runs on. |
| `installer/construct.yaml` | The constructor configuration (both platforms). |
| `installer/macos/`, `installer/windows/` | The scripts the installers run, the macOS uninstaller, the macOS installer texts. |
| `recipe/recipe.yaml`, `stage_app.py` | The app's own code as a small conda package. |
| `launch.py` | What the shortcuts run: installed-mode setup, then `app.launcher`. |
| `menu/wikipedia-wrapped.json` | The shortcut definition (menuinst). |
| `icon/icon.png`, `icon/make_icons.py` | The icon source and its conversion to `.icns` / `.ico`. |
| `test_install_macos.py` | Installs a built `.pkg` into a scratch folder, tests it, uninstalls it. |

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
   and reports a size: about 212 MB for the October 2026 build (39,794
   articles), nearly all of it the pickled reducer, which barely
   compresses. Every installer grows by that much.

### The embedding fixture

`app/bundle_fixture.json` holds 8 fixed texts and the vectors the pinned
model gave them (`scripts/make_embedding_fixture.py`). The tests and the
installed app's `--self-test` re-encode them and require cosine similarity
above 0.999 (`app/bundle.COSINE_THRESHOLD`). Measured 2026-10-25: 0.9999999
in both the dev env and the locked runtime env (osx-64, no pandas), with
the packaged reference space projecting them. Regenerate it only when the
model revision or the embedding stack changes on purpose.

## The installer (Milestone 3)

### Building one

Installers are built on the platform they are for. On the Intel Mac:

```bash
conda env update -n wikipedia-wrapped-build -f packaging/build-tools.yml  # once (adds pillow)
conda activate wikipedia-wrapped
python scripts/fetch_model.py --dest packaging/build/resources
python scripts/fetch_reference_space.py   # until reference-v0 is uploaded:
                                          #   --archive packaging/build/reference-space-v0.tar.gz
conda run -n wikipedia-wrapped-build python packaging/build_installer.py
```

The installer lands in `packaging/build/dist/` with a `.sha256` beside it:
`WikipediaWrapped-<version>-macOS-Intel.pkg`, `...-macOS-AppleSilicon.pkg`
or `...-Windows-x64.exe`. A build takes about 10 minutes and leaves about
3 GB of downloads and caches under `packaging/build/` (git-ignored; delete
it freely, the next build downloads again).

`build_installer.py`'s docstring lists the steps. In short: verify the
resources, make the icons, build the app package, download the wheels
(hash-checked), write the package list (the lock plus the app package,
nothing solved), stage the files that are copied in as they are, run
constructor.

### What gets installed, and where

| | macOS | Windows |
|---|---|---|
| Program | `~/Library/WikipediaWrapped-App/v<version>/` | `%LOCALAPPDATA%\WikipediaWrapped-App\v<version>\` |
| Shortcut | `~/Applications/Wikipedia Wrapped.app` | Start menu and desktop, "Wikipedia Wrapped" |
| User data (never touched) | `~/Library/Application Support/WikipediaWrapped/` | `%APPDATA%\WikipediaWrapped\` |
| Uninstall | `bash ~/Library/WikipediaWrapped-App/uninstall.sh` | Settings > Apps > Installed apps |
| Admin rights | never | never ("Just Me", the default) |

Inside the program folder, a complete conda environment, plus:

```
share/wikipedia-wrapped/          the app's code (config.PROJECT_ROOT)
    launch.py                     what the shortcut runs
    config.py  app/  extract/  scripts/enrich_pending.py
    resources/models/all-MiniLM-L6-v2/    the bundled model
    resources/reference/                  the reference space
Menu/wikipedia-wrapped.json       the shortcut definition, and its icons
```

The `resources` folder next to the code is what switches the app into
installed mode (`config.is_installed()`): the model must come from the
bundle, and nothing is downloaded to fill a gap.

### How the app's code gets in: a conda package

`packaging/recipe` builds the code into a small `noarch` conda package
(about 1.3 MB) with rattler-build; `stage_app.py` decides what goes in,
from an explicit list. The other option, copying the code in with
constructor's `extra_files`, was simpler on paper, but menuinst only makes
shortcuts for conda packages that carry a `Menu/*.json`, and a package
also records the app's version and files in `conda-meta` like every other
library. The bundled resources do go in through `extra_files`: they are
large, built elsewhere, and verified separately.

### Installed mode: what `launch.py` and the app do differently

- **No bytecode in the install folder.** `launch.py` turns bytecode
  writing off for the app and its job processes; the installer
  precompiles the code instead.
- **First-load warm-up.** The installer imports every library once at the
  end. macOS checks each newly installed library the first time it loads;
  on the Intel Mac that cost 22 s before the window appeared and over 2
  minutes before the first map update. Paid during installation instead,
  a fresh install's first launch waited 6 s on imports (measured
  2026-10-25 with `test_install_macos.py`).
- **Windows specifics** (`launch.py`): the folders `conda activate` would
  add for DLLs, and the taskbar ID that makes the taskbar show the
  shortcut's name and icon instead of Python's. The background jobs run
  under `python.exe`, not the windowless `pythonw.exe` the shortcut uses
  (`app/jobs.py`).
- **macOS Dock**: the shortcut runs Python through a link inside the
  `.app` (menuinst's `link_in_bundle`), which is what makes the Dock show
  "Wikipedia Wrapped" and its icon rather than "python3.11".

### Upgrades and uninstalling

Each version installs into its own `v<version>` folder, because both
installer types refuse to install into a folder that already exists. The
installer then retires the old version:

- **macOS**: `pre_install.sh` removes the old shortcut (menuinst will not
  overwrite one); `post_install.sh`, once the new version is in place,
  removes the other `v*` folders that are installations of this app.
- **Windows**: `pre_install.bat` finds older versions by their entry in
  Installed apps and runs their own uninstaller, silently and to
  completion, before the new shortcuts are made.

User data are never in the program folder, so they carry over untouched.
When an upgrade ships a different reference space, the map positions of
your pages are placed again automatically on the next enrichment
(`umap_space.forget_stale_coords`); embeddings are cached, so that is
quick.

Reinstalling the *same* version is refused (its folder exists): uninstall
first. Downgrading works like upgrading.

Removing user data as well, after uninstalling:
`rm -rf ~/Library/Application\ Support/WikipediaWrapped` on macOS, and
the `%APPDATA%\WikipediaWrapped` folder on Windows.

### Platform limits

- **macOS**: a `.pkg` offers "Install for me only" and nothing else, at a
  fixed location. Being unsigned, the `.pkg` itself needs System Settings >
  Privacy & Security > "Open Anyway" once; the files it installs are not
  quarantined, so the app then opens normally. `~/Applications` is not in
  Finder's sidebar by default: Spotlight and Launchpad find the app, and
  the installer's last page says how. The installer refuses macOS older
  than 11, which the locked packages need.
- **Windows**: constructor's installer still offers "All Users", which
  needs an administrator; "Just Me" is the default. An "All Users"
  install is not removed automatically by the next version's installer
  (it looks at per-user entries only). The path-has-spaces
  check is off, so a user name with a space can install. Unsigned, so
  SmartScreen shows "More info > Run anyway".
- **Both**: the program is about 2.2 GB installed.

### The self-test

```bash
~/Library/WikipediaWrapped-App/v0.1.0/bin/python \
    ~/Library/WikipediaWrapped-App/v0.1.0/share/wikipedia-wrapped/launch.py --self-test
```

(on Windows: `<install folder>\python.exe <install folder>\share\wikipedia-wrapped\launch.py --self-test`;
in a checkout, `python -m app.launcher --self-test`). Headless; exits 0 or
1. It checks the imports, the resources against their manifests and the
runtime's versions, the model loading with networking blocked and
reproducing the fixture vectors, the reducer placing them, the job
interpreter, and that nothing was written into the install folder.
`app/selftest.py` explains each check. It never touches user data.

### Testing an installer without installing it for real

```bash
conda run -n wikipedia-wrapped-build python packaging/test_install_macos.py \
    packaging/build/dist/WikipediaWrapped-0.1.0-macOS-Intel.pkg \
    --scratch /tmp/ww-install-test
```

Installs into a scratch home folder by running the `.pkg`'s parts by
hand, over a fake older version, then runs the self-test (twice, once
through the shortcut's in-bundle Python), and uninstalls. `--keep` stops
after installing.

### Testing by hand, as a user would

On a Mac account where the app has never been installed (or after
uninstalling), with a copy of the `.pkg` downloaded through a browser, so
macOS quarantines it like a user's download:

1. Double-click the `.pkg`. macOS refuses to open it (unidentified
   developer). Open System Settings > Privacy & Security, scroll down,
   click "Open Anyway", confirm.
2. The installer shows the welcome text and installs for you only, with
   no password prompt. It sits near the end for a few minutes (installing,
   then preparing the libraries); notifications report progress.
3. The last page says how to open the app. Press Command-Space, type
   Wikipedia Wrapped, press Return. The window should appear within a few
   seconds, and the Dock should show the gift icon and "Wikipedia
   Wrapped" (not "python").
4. Go through the first-run setup and let the first sync finish. Check
   Help opens, with its screenshots.
5. Quit (Command-Q), relaunch: no setup screens, your data are there.
6. Upgrade: set `APP_VERSION` to `0.1.1` locally (do not commit), build
   again, install over 0.1.0. One `v0.1.1` folder remains in
   `~/Library/WikipediaWrapped-App`, the app opens on your data, and
   `--self-test` passes. Put the version back.
7. Uninstall: `bash ~/Library/WikipediaWrapped-App/uninstall.sh`. The app
   and its folder are gone; `~/Library/Application Support/WikipediaWrapped`
   is still there.

### The icon

`packaging/icon/icon.png` is a placeholder (a gift box, drawn by
`make_icons.py --placeholder`). To use a real one, replace that file with
a square PNG, ideally 1024 x 1024 with a transparent margin (macOS icons
sit inside a rounded square), and rebuild. The `.icns` and `.ico` are
generated on every build and never committed.

### Signing, later

The installers are unsigned for v1, and nothing here stands in the way of
signing later. Windows has two routes: constructor can sign during the
build (`windows_signing_tool` in `construct.yaml`, which also signs the
uninstaller it embeds), or a service such as SignPath signs the finished
`.exe` in CI, after `build_installer.py` and before the release upload.
macOS uses constructor's `signing_identity_name` and
`notarization_identity_name`. All are unset, with a comment in
`construct.yaml` where they go.
