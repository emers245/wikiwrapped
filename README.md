# wikipedia-wrapped

A visualization tool for personalized Wikipedia reading history: export your
browser history, extract the Wikipedia visits, and explore stats, topics, and
an embedding map of what you read.

Specification: `repo_spec.md` | Phase 1 plan: `claude_phase1.md` |
**User help: [`app/static/help.html`](app/static/help.html)** (the Help
button in the app; also readable on its own in a browser)

## Setup

Building the conda environment is step zero (repo_spec S9) - verify it before
anything else:

```bash
conda env create -f environment.yml
conda activate wikipedia-wrapped
python scripts/check_env.py --full
```

Wikimedia asks every API client to identify itself. The app sends
`WikipediaWrapped/<version> (https://github.com/emers245/wikipedia-wrapped)`,
so no personal contact address is needed. A developer who wants their own
address in it as well can set `WIKIPEDIA_WRAPPED_EMAIL`; it is appended, never
required. (`contact_email.txt` is no longer read and can be deleted.)

### Where the app keeps its files

The program folder is treated as read-only. Everything the app writes is
per-user data, in the platform's standard location:

| OS | User-data directory |
|---|---|
| macOS | `~/Library/Application Support/WikipediaWrapped` |
| Windows | `%APPDATA%\WikipediaWrapped` |
| Linux | `$XDG_DATA_HOME/WikipediaWrapped` (usually `~/.local/share/...`) |

Inside it: `data/visits.jsonl` (the visit log), `data/app.db`,
`data/sync_state.json`, `config.local.toml`, `tokens/`, any
`client_secret*.json`, `logs/`, and `cache/`.

To work against a scratch copy instead, point the whole app somewhere else:

```bash
export WIKIPEDIA_WRAPPED_DATA_DIR=/tmp/ww-scratch
```

The test suite does this automatically (`tests/conftest.py`), so no test can
touch real data.

Read-only app resources (the UMAP reference space) come from
`WIKIPEDIA_WRAPPED_RESOURCE_DIR` if set, then from an installed app's
resource folder (wired up by the packaging step), then from the repo's
`data/reference/` in a dev checkout.

### Migrating from a repo-local checkout

Before this change the same files lived in the repo (`data/`,
`config.local.toml`, `tokens/`, `client_secret*.json`). Nothing moves
automatically. Copy them over explicitly:

```bash
python -m app.migrate --dry-run   # show what would be copied
python -m app.migrate             # copy, verify, report
```

It only ever **copies**: the repo files are hashed before and after and are
never modified or deleted. Each copy is verified (SHA-256, or SQLite's
integrity check plus a full-dump hash for `app.db`). A destination file that
already exists and differs is left alone and reported as a conflict, with one
exception: if both visit logs exist, the repo's records are appended to the
new one (exact dedup), so neither loses a visit. Re-running is safe.

Until the migration has run, `python -m app.sync` warns and `GET /health`
reports `legacy_data`, so a fresh log cannot quietly start in the new
location while the old one sits in the repo. Once you have checked the app
against the new location, deleting the repo copies is up to you.

## Usage (Phase 1 pipeline)

```bash
# 1. Capture your Wikipedia visits into the append-only log and the database
python -m app.sync --force

# 2. One-time: put the pinned model and a reference space in data/. Either
#    fetch the packaged reference space (needs a GitHub token; the repo is
#    private) or build one (hours at full scale; resumable; --head/--tail
#    for small test builds). See packaging/README.md.
python scripts/fetch_model.py
python scripts/fetch_reference_space.py --dest data    # or:
python scripts/build_reference_space.py

# 3. Open the app window (syncs and enriches in the background; see
#    "Launching" below)
python -m app.launcher        # or ./run.sh, which finds the conda env first
```

`app.sync` reads every local Chrome profile, plus any Google Takeout archive
if that integration is enabled, and writes what it finds to the log before
updating the database.

### Which Chrome profiles are read

All of them, by default. The signed-in profile is often not `Default`, so
reading only that one silently captures nothing: on a typical machine the
browsing lives in `Profile 1` or `Profile 2`. Each visit is attributed to
that profile's signed-in account, read from Chrome's `Local State`.

The app's setup screens and Settings (or `scripts/setup_takeout.py`) list
your profiles with their display names, accounts, and Wikipedia visit
counts, and let you narrow the selection if you want only some of them. The
choice is stored as directory names, which are stable when a profile is
renamed:

```toml
[local]
enabled = true                            # false = read no local profile
chrome_profiles = []                      # empty = all profiles
# chrome_profiles = ["Profile 1"]         # or name the ones you want
```

`enabled` exists because an empty list already means "all": unticking every
profile in setup turns the local source off rather than storing `[]`.

Entries match either the directory name (`Profile 1`) or the display name
(`Work`), case-insensitively. If nothing matches, the sync says so and lists
what is available rather than quietly logging zero visits.

The standalone extractor is still there and still the only code that knows
about browser-specific formats. Use it to inspect a single source, or to
convert an archive by hand:

```bash
python extract/extract_history.py --output wikipedia_history.json  # local Chrome
python extract/extract_history.py --input takeout-20260809T120000Z-001.zip
python extract/extract_history.py --input export.json
```

### Where your history is stored

`data/visits.jsonl` (in the user-data directory, see above) is an
append-only log with one JSON record per raw visit (`url`, `title`,
`timestamp`, `source`, `account`). It is the source of truth; `data/app.db`
is derived from it and can be deleted and rebuilt with
`python -m app.sync --rebuild`.

**This distinction matters.** Chrome expires local history after about 90
days, and Takeout archives can be deleted from Drive at any time. The log is
the app's own permanent record: once a visit reaches it, deleting local
Chrome history, the archives, or `app.db` cannot lose it. It is the one file
worth backing up.

**Browsing history never leaves this machine.** The log and the database live
in the user-data directory, outside any git checkout. The extractor's output
and any Takeout archive left in the tree are git-ignored, so none of it can
be pushed to a remote. (The JSON files under
`reference_code/` predate this rule and are already public; they are left
as they are.)

### Not all history is equally recoverable

Two sources, two very different durability stories, and it is worth knowing
which of your visits are which:

| Source | If the log were lost |
|---|---|
| Takeout account | Recoverable. Request a fresh export; it reaches back a year. |
| Local-only Chrome profile | **Gone.** Chrome keeps ~90 days and nothing else has a copy. |

A Google Workspace account whose administrator disables Takeout can only
ever be a local-Chrome source, so its history has no second copy anywhere.
For those profiles the append-only log is not a convenience, it is the
archive, and it only gains a visit when a sync actually runs.

The practical consequence: **sync at least once every 90 days, or that
profile's history is permanently lost.** Launching the dashboard is enough,
since the launcher syncs on every launch. If you go three months without opening it, the
gap is not recoverable by any means.

To see where you stand per source:

```bash
python -c "
import json, collections
import config
rows = [json.loads(l) for l in open(config.VISIT_LOG_PATH)]
by = collections.defaultdict(list)
for r in rows: by[(r['source'], r['account'])].append(r['timestamp'])
for k in sorted(by, key=str):
    v = sorted(by[k]); print(f'{str(k):50s} {len(v):5d}  {v[0][:10]} -> {v[-1][:10]}')
"
```

Each visit records which mechanism and account it came from, in a
`visit_sources` table rather than columns on `visits`, because the same
physical visit can legitimately arrive from both the local Chrome read and a
Takeout export. `GET /health` reports the per-source breakdown.

## Google Takeout integration

Optional. Everything below is additive: with no Takeout source set up, the
local Chrome path works exactly as before.

Local Chrome history has two hard limits. It expires after about 90 days,
and it only covers one account on one device. Google Takeout exports Chrome
history at the **account** level, which Chrome Sync has already merged
across your devices, so it reaches further back and includes a second Google
account and mobile browsing.

There are two independent ways to get Takeout archives in. Either, both, or
neither can be used.

### Watched folders (the default; no Google sign-in)

Point the app at one or more folders. Each sync scans them, up to 4 levels
deep, for zips named `takeout*.zip` and ingests any it has not seen. The
zips get there either by hand (download them from Takeout into the folder)
or through **Google Drive for desktop**, which mirrors the Drive folder that
a scheduled Takeout export delivers into. No Cloud project, OAuth client, or
token is involved.

```bash
python scripts/setup_takeout.py --add-folder "/path/to/Takeout" --label Personal
python scripts/setup_takeout.py --remove-folder "/path/to/Takeout"
```

```toml
[[watched_folders]]
path = "/Users/you/Library/CloudStorage/GoogleDrive-you@example.com/My Drive/Takeout"
label = "Personal"
```

- **Read in place, never changed.** Only the archive's Chrome history member
  is read; the zip is never modified, moved, copied, or deleted, and only
  the Wikipedia subset is kept.
- **The label stands in for the account.** A zip on disk does not say whose
  it is, so visits are logged as source `takeout` with the folder's label
  (or no account if it has none) as the account.
- **Idempotent by path + size + mtime.** Recorded in `processed_archives` as
  `file:<resolved path>` / `<mtime>|size=<bytes>`. `stat()` never downloads
  a Drive online-only file, where a content hash would, on every sync. A
  re-downloaded or copied archive is read once more, and dedup (the log's
  exact key, then the database's `UNIQUE (page_id, timestamp)`) keeps that
  from adding visits: the same archive via Drive and a folder, or in two
  folders, is one visit with two provenance rows.
- **Failures never block startup.** A missing or unreadable folder, a corrupt
  zip, or an online-only Drive file that cannot be fetched (offline) is
  recorded per folder in the sync report, `sync_state.json`, and
  `GET /health` under `sync.folders`. A failed zip is not marked processed,
  so it is retried next sync. Making the Takeout folder **available offline**
  in Drive for desktop avoids the online-only case entirely.

### Google Drive via your own OAuth client (Advanced)

Off until you enable it. Access is **read-only** and password-free. The single OAuth scope is
`drive.readonly`; the app never asks for, receives, stores, or transmits a
Google password, and it never deletes, moves, or modifies anything in your
Drive. Deciding how long to keep archives is yours to make by hand.

#### 1. Create the OAuth client, and 2. schedule Takeout

The step-by-step instructions for both are user help, so they live in one
place: [`app/static/help.html`](app/static/help.html) (the app's Help
button), sections **Setting up Google Takeout** and **Advanced: signing in
to Google Drive**. Google's own wording there was checked on 2026-10-24;
anything that could not be confirmed is marked `TODO(Joe): verify` on the
page.

Developer notes that do not belong in the user help:

- **Audience: External** is required; Internal is only available to
  Workspace organizations. An account not listed under **Test users**
  cannot authorize the app.
- The only scope is `https://www.googleapis.com/auth/drive.readonly`.
- **Publishing status.** While the app stays in "Testing", Google expires
  refresh tokens after roughly **7 days**. The sync handles this gracefully:
  it reports `re-authorization needed for account X` and carries on with
  whatever other sources it can reach, rather than crashing or silently
  skipping. From a terminal: `python scripts/setup_takeout.py --authorize you@example.com`.
- Deliver to Drive rather than email: **emailed Takeout links expire after
  about 7 days**, while files in Drive persist until removed. `.tgz`
  archives are listed but skipped, because the extractor reads `.zip`.

#### 3. Run setup

In the app: **Settings > Advanced: Google Drive sign-in**. Choose the client
secret JSON with the file picker (it is checked, then copied into the
user-data directory with owner-only permissions), then **Add a Google
account**, which opens your browser for the sign-in and waits up to five
minutes for it. Accounts that need signing in again are marked there, with a
**Sign in again** button; **Remove** forgets an account and deletes its
token.

From a terminal, the same thing:

```bash
python scripts/setup_takeout.py
```

It asks whether to enable the integration, runs the browser OAuth flow, and
offers to authorize additional accounts (one token file per account under
`tokens/` in the user-data directory, owner-only permissions). It then asks
whether to sync on launch and writes everything to `config.local.toml`,
also in the user-data directory.

Re-run it any time to add another account or change a preference: it loads
the existing config, changes only what you answer, and writes it back.

If you enable the integration but the OAuth flow does not complete, setup
says so loudly rather than leaving you with a config that looks ready and
warns on every sync. `--status` repeats that warning.

The Drive folder prompt accepts a folder **name** (`Takeout`) as well as an
ID; the name is resolved against Drive at sync time. Leave it blank to
search everywhere, which is usually right.

```bash
python scripts/setup_takeout.py --status              # show current config
python scripts/setup_takeout.py --authorize EMAIL     # re-authorize one account
python scripts/setup_takeout.py --disable             # turn it off, keep data
```

### Check-on-launch syncing

The launcher runs one sync pass as a background job right after the window
opens: it re-reads local Chrome, scans the watched folders, checks Drive (if
enabled) for archives it has not processed, appends everything to the log,
and updates the database. Enrichment of any new pages follows.

Sync preferences cover every source, so they live in their own table:

```toml
[sync]
on_launch = true          # default; local Chrome is only captured by a sync
min_interval_hours = 24
```

They used to be `takeout.sync_on_launch` / `takeout.min_sync_interval_hours`,
and launch sync only ran when Takeout was enabled, which meant local Chrome
was never captured on launch without it. Old files are migrated when loaded
(the legacy on-launch flag is honored only where Takeout was enabled).

It is a single pass, not a daemon or a scheduler, and it runs as its own
child process so a failure cannot take the dashboard down. If Drive is
unreachable, a token has expired, or an archive is malformed, it logs a
warning, records the problem in `data/sync_state.json` where `GET /health`
exposes it, and **serves the dashboard with existing data anyway**.

Takeout only produces a new archive every ~2 months, so the pass early-exits
if one ran within `min_interval_hours` (default 24).

```bash
python -m app.launcher --no-sync      # launch without syncing
python -m app.launcher --force-sync   # sync even if one ran recently

python -m app.sync --force      # sync now
python -m app.sync --no-drive   # skip the Drive (OAuth) route
python -m app.sync --no-folders # skip watched folders
python -m app.sync --rebuild    # re-ingest the whole log into the database

# One-off migration: fold history captured before the log existed into it
python -m app.sync --import wikipedia_history.json --force
python -m app.sync --backfill-from-db --force   # recover DB-only visits
python -m app.sync --rebuild --force            # backfill provenance
python -m app.sync --compact-log                # drop superseded duplicates
```

Those three matter more than they look, and are worth running once on an
install that predates the log.

`--import` reads an extractor JSON written months ago, which usually reaches
further back than local Chrome still does. `--backfill-from-db` catches
visits whose source file is gone entirely, so the database is their only
copy; their URLs are reconstructed from canonical titles and tagged with the
`legacy_db` source, since a reconstruction is not a real capture.

`--compact-log` drops records superseded by a more specific duplicate of
themselves, which happens when a record gains a field it did not have when
first logged (a local visit logged before the account was known, then again
with it). It rewrites the log and the matching provenance rows; the visit
counts are unaffected either way, since dedup already collapses these.

Afterwards the log is the complete record and `data/app.db` is genuinely
disposable: delete it, run `python -m app.sync --rebuild`, and you get the
same pages and visits back. One caveat before you delete it: `app.db` also
holds the topic, embedding, pageview, and UMAP caches, and rebuilding those
costs real time and network. `--rebuild` alone does not touch them.

The first Takeout ingest can take a while: every article title it has not
seen before needs a redirect lookup against the MediaWiki API. Later syncs
hit the persistent redirect cache and are fast.

### Deduplication

The same visit can arrive from both the local Chrome read and a Takeout
export. Duplicates would inflate every statistic, so records are collapsed
on the **canonical post-redirect title** (so `/wiki/Foo`, `/wiki/foo?action=raw`,
the mobile host, and a redirect and its target are one page) plus a
timestamp tolerance.

That tolerance is one constant, `DEDUP_TOLERANCE_S` in `app/dedup.py`, and
it ships at `0`, meaning an exact same-second match. Measured against a real
Takeout archive on 2026-08-09, visits present in both sources agreed to the
second, delta 0.

**That measurement had n=1**, and it will stay small, which is also why it
matters less than it looks. Takeout covers the personal account; the busy
local profile belongs to a Workspace account whose administrator has Takeout
disabled, so it can never be a second Takeout source. The two inputs cover
almost disjoint accounts, so cross-source duplicates are rare by
construction.

Where dedup actually does load-bearing work, both sides come from the *same*
mechanism and timestamps are byte-identical, so exact matching is provably
right: consecutive Takeout archives re-export months of the same visits, and
every sync re-reads the whole local Chrome profile.

If you ever add a second Takeout account, re-check with:

```bash
python scripts/compare_sources.py --takeout takeout-20260809T120000Z-001.zip --by-title
```

It reports exact `(url, timestamp)` matches, URL matches within
1s/60s/same-day, and the full delta distribution, then recommends a
tolerance. It needs the two sources to cover an **overlapping period**.

### A timestamp trap, for anyone extending the extractor

The three inputs do not share an epoch:

| Source | Field | Epoch |
|---|---|---|
| Chrome SQLite | `visits.visit_time` | microseconds since **1601** |
| Google Takeout | `time_usec` (a string) | microseconds since **1970** |
| History extensions | `visitTime` / `lastVisitTime` | milliseconds since 1970 |

Takeout's `time_usec` being the Unix epoch contradicts this repo's own older
notes and the reference scripts, which all called it a 1601 value. Reading a
real archive that way subtracts 11644473600 seconds, lands in 1663 BC, fails
the plausibility bounds, and drops every row: it turned 2804 real Wikipedia
visits into zero with no error message. `usec_to_unix` now decodes both
readings and keeps whichever is a plausible date, and the sync warns loudly
whenever any row is dropped for a bad timestamp.

## First-run setup and Settings

With no settings file in the user-data directory, the window opens on a
short setup flow instead of syncing straight away: what the app does and
what leaves the computer, which Chrome profiles to read, watched Takeout
folders (native folder picker; the zip count is shown as soon as a folder is
picked), the Advanced Drive option (collapsed), and sync preferences.
**Save and start** writes the settings and starts the first sync, whose
progress shows in the window. The same sections, on one page, are behind
the **Settings** button in the header, along with the last sync's problems
and where the data and logs are.

All the rules live in `app/setup_service.py`, which both the window (via
`GET/PUT /settings` and friends) and `scripts/setup_takeout.py` call, so the
two can never disagree. Every change is validated on the server: a new
folder must exist and be readable, a client secret must parse as a Desktop
app OAuth client. A folder that is already configured may be temporarily
unreachable (an unplugged drive) without blocking a save. Opened with
`--browser`, the native pickers become text fields.

### Showing only some accounts

Settings has two separate controls, on purpose. **Browser history**,
**Google Takeout** and **Advanced** choose what the app *collects*. **Show in
dashboard** chooses what the dashboard *shows*: one checkbox per account the
database has history for (an email, a watched-folder label, or "No account
recorded" for visits saved before accounts were tracked or from a profile
that is not signed in), with each account's visits broken down by source.
Unticking an account hides its visits everywhere (totals, map, network,
panels) without deleting anything or pausing its sync; a header note says
how many accounts are hidden and opens Settings. A visit two accounts both
saw stays visible while either is shown.

```toml
[dashboard]
hidden_accounts = ["work@example.com"]    # "" = no account recorded
```

A hidden list rather than a shown one, so an account that appears later
shows up by default. The filter is one function,
`storage.show_only_accounts`: it gives the dashboard's database connection
temporary views named `visits` and `pages` (SQLite resolves an unqualified
name to a TEMP object first), so every existing dashboard query sees only
the shown accounts without being rewritten, and the totals, map, network
and panels cannot disagree. Sync, ingest, enrichment, `/health` and
Settings use unfiltered connections. Pageview lookups always use the whole
history's date range, which is what their cache is keyed on.

The API refuses requests a web page could make on your behalf: every
request must be addressed to `127.0.0.1` or `localhost` (DNS rebinding), and
a state-changing request carrying another site's `Origin` is rejected
(`local_only` in `app/api.py`).

## Launching

`python -m app.launcher` (`app/launcher.py`) is the whole launch procedure,
cross-platform and with no conda or shell assumptions:

1. Logs to `logs/app.log` in the user-data directory (rotating, 1 MB x 5)
   from the first line, since an installed app has no console. The sync and
   enrichment children's output lands there too.
2. If this user's server is already running (recorded in `server.json` in
   the user-data dir and confirmed by the `/health` identity check, which
   also matches the database path), it opens another window on it instead
   of starting a second server.
3. Starts uvicorn on **127.0.0.1 only**, in a background thread, because
   pywebview must own the main thread (macOS requires it). The port is
   48213 when free and any free port otherwise. A fixed preferred port
   matters: localStorage is keyed by origin, port included, so a random
   port would forget the panel layout on every launch.
4. Opens the dashboard in a **pywebview** window as soon as the server
   answers (about a second), then runs the check-on-launch sync and the
   enrichment as a background job whose progress shows in a strip at the
   top of the window. When it finishes, an empty dashboard reloads itself;
   a populated one offers a Reload button.
5. Closing the window stops the job's child process, stops the server, and
   removes `server.json`.

```bash
python -m app.launcher                # the app window
python -m app.launcher --browser      # a normal browser tab (dev/debugging);
                                      # Ctrl-C or kill stops it cleanly
python -m app.launcher --port 8080    # prefer another port
python -m app.launcher --no-sync      # skip the sync pass
python -m app.launcher --force-sync   # sync even if one ran recently
python -m app.launcher --no-enrich    # skip topic/map enrichment
python -m app.launcher --debug        # enable the window's web inspector
```

`./run.sh` is kept as a thin developer wrapper: it finds the conda
environment by path (so no `conda activate` first) and execs the launcher
with the same flags, so an existing alias keeps working:

```bash
alias wikiwrapped="$HOME/Documents/GitHub/wikipedia-wrapped/run.sh"
```

**Why the background work runs in child processes, not threads**
(`app/jobs.py`): a sync stuck on a Drive online-only file, or an enrichment
that crashes inside numba or torch, cannot freeze or kill the window; a
child can be terminated when the window closes, where a thread cannot; and
the language model's memory is released when enrichment exits. Progress
crosses the process boundary as `@@progress {json}` lines on the child's
stdout (`app/progress.py`). One job runs at a time.

**SQLite is shared safely** between those children (writing) and the API
(reading) because `storage.connect` puts the database in WAL mode with a
30-second busy timeout: readers never block the writer or vice versa.

**pywebview comes from PyPI, not conda-forge**: conda-forge only builds it
for Windows and Linux (checked 2026-10-24), so `environment.yml` installs
it through its `pip:` section. Only `app/launcher.py` imports it, and only
when opening a window, so the tests and `--browser` mode never need it.

The API can still be driven over HTTP (the port is in `server.json`):

```bash
curl -X POST localhost:48213/jobs/sync    # sync + enrich now, in the background
curl localhost:48213/jobs                 # progress of that job
curl -X POST localhost:48213/ingest --json @wikipedia_history.json
curl -X POST "localhost:48213/enrich?include_pageviews=true"
curl localhost:48213/stats/summary
```

`GET /health` also reports the per-source visit breakdown and the last sync's
outcome, including any account that needs re-authorization.

Endpoints: `/health`, `/jobs`, `POST /jobs/sync`, `POST /ingest`,
`POST /enrich`, `/stats/summary`, `/stats/top?n=`, `/stats/heatmap?tz=`,
`/stats/daily?tz=`, `/stats/busiest-week?tz=`, `/stats/topics`, `/map`,
`/network`, `/pages/{title}`, `/dashboard`. Interactive docs at `/docs`.
Run tests with `pytest` (`-m "not integration"` to stay offline).

## Dashboard (Phase 2)

How to read each view is in the user help (`app/static/help.html`,
"Reading the dashboard"); this section is about how it is built.

The launcher opens the interactive dashboard. It is a single page (vanilla
JS, no build step; D3 and cytoscape.js vendored in `app/static/vendor/`,
not loaded from a CDN, so it works offline) that loads everything once from
`GET /dashboard` so all hover interactions are instant.

- **Main view, two modes** (toggle top-left):
  - *UMAP*: your pages positioned in the frozen semantic space, sized by
    your visit count, colored by macro topic, over a faint backdrop of
    the reference articles. Scroll to zoom, drag to pan.
  - *Network*: the reading-session graph (visits to two articles within
    15 minutes form an edge), force-directed, node size = PageRank.
    Hovering highlights a node's connections and fades the rest so paths
    can be followed.
- **Hover** any node: it enlarges and a popup shows its core stats
  (your visits, topic, views by other readers, in/out-degree, PageRank)
  with a link to the article. The side panels simultaneously fade to
  highlight that page: its topic's bars in the category chart, and
  vertical markers at its visit dates/times on the time panels.
- **Side panels**: category fractions (you vs the top-viewed "popular
  pages" sample from the reference build), visits per day with 7-day
  rolling average, and visits by hour of day (device timezone).
  They sit in a left sidebar that the **Panels** button in the header
  stows and restores, so the map or network can have the full window.
- **Resizing the panels**: drag the sidebar's right edge to scale them.
  Width is the only control - each chart holds a fixed aspect ratio, so
  widening enlarges the marks and the type together rather than giving
  same-size text more room, and the sidebar scrolls once the panels no
  longer fit the window. Panel size therefore does not follow the window
  size; it is whatever you dragged it to. Double-click the edge to reset,
  or focus it and use the arrow keys. Width and stow state are both
  remembered across reloads (`localStorage`).

Below a 720px viewport the sidebar floats over the visualization as a
drawer instead of taking its own column, and the drag handle is hidden.

The UMAP mode and the "popular" baseline require the reference space
(`data/reference/`) and enriched pages (the launcher enriches after every
sync); without them the app falls back to network mode only.

### Frontend layout contract

The page is a two-column CSS grid: a full-width header over a sidebar
column and the visualization.

`--panel-w` is the single source of truth for the sidebar's size, and the
drag handle writes it as an inline custom property on the root element.
Everything else reads `--panel-w-eff`, which is derived from it: the
sidebar's width, the stow margin, the resizer's position, and the panel
heading size all come from the derived one. That indirection exists so a
media query can cap the width (the drawer's `86vw`) - an inline style
outranks any stylesheet rule, so `--panel-w` itself cannot be overridden,
but nothing ever writes the derived property inline.

Stowing slides the sidebar out with a negative `margin-left` rather than
animating its width to zero. An `auto` grid column measures the item's
margin box, so the column still collapses, but the sidebar's width stays
constant - which means the charts never reflow mid-slide and a scrollbar
can never squeeze them. `body` needs `overflow: hidden` to clip the
off-screen sidebar.

The panel charts are drawn once into a fixed logical coordinate space
(`LOGICAL_W` / `LOGICAL_H` in `panels.js`) and the SVG `viewBox` scales
them to the current sidebar width. Two consequences worth knowing:

- Resizing needs no redraw, so `WWpanels.relayout()` is deliberately a
  no-op. The panels are resolution-independent by construction.
- Font sizes and margins are in logical units, so they scale with the
  panels. `AXIS_FONT` / `LABEL_FONT` / `LEGEND_FONT` and the `MARGIN` /
  `CAT_MARGIN` objects set the *proportions*, not final pixel sizes:
  raising a font still means raising its margin to match.

The panel headings are HTML, not SVG, so they cannot ride the viewBox and
are scaled arithmetically instead: `.panel h2` sizes itself from
`--panel-w-eff` minus `--panel-chrome` (the padding and borders between
the sidebar edge and the chart) over the same 320 logical units, which
keeps them in lockstep with the SVG text. `--panel-chrome` has to be
updated if that padding changes.

Macro topic names too long for the label gutter are abbreviated by hand in
`SHORT_LABELS` in `panels.js`; the character-count slice below it is only
a fallback for topics that turn up in the reference sample without an
entry, and a `<title>` always carries the full name.

The other two views do measure their live container, so each exposes a
`relayout()` that `main.js` calls from one handler:

| module       | `relayout()` does                                     |
|--------------|-------------------------------------------------------|
| `panels.js`  | nothing (see above)                                    |
| `map.js`     | recomputes the equal-aspect scales and redraws         |
| `network.js` | `cy.resize()` (cytoscape caches its container size)    |

Add a view and it needs a `relayout()` too. It is called on window resize,
once the stow animation settles (`SLIDE_MS` in `main.js` must match the
`#panels` transition in `style.css`), and once per animation frame while
the sidebar is being dragged - the drag fires far more often than the
display refreshes, and the map redraws 10k backdrop points each time.

The category chart is drawn with horizontal bars specifically so topic
names stay readable in a narrow column, where rotated labels under
vertical bars did not.

## Layout

```
run.sh                       # dev wrapper: finds the conda env, runs app.launcher
config.py                    # paths (user data vs resources), user-agent, timezone

extract/extract_history.py   # standalone: Takeout zip / Chrome DB / JSON
                             #   -> standardized JSON. The only browser-aware code.
app/
  visitlog.py                # append-only JSONL log: the source of truth
  migrate.py                 # opt-in copy of repo-local data to the user-data dir
  sync.py                    # check-on-launch pass over every input source
  folders.py                 # watched folders: find Takeout zips on disk
  setup_service.py           # setup/Settings rules shared by the window and CLI
  launcher.py                # the app: logging, server thread, pywebview window
  jobs.py                    # background sync/enrich as child processes
  progress.py                # progress events from those children
  drive.py                   # read-only Google Drive client (lazy imports)
  dedup.py                   # collapse one visit seen by two sources
  localconfig.py             # config.local.toml read/write
  ingest, normalize, storage, stats, topics,
  embeddings, umap_space, pageviews, api
scripts/
  setup_takeout.py           # terminal front end to app/setup_service.py
  compare_sources.py         # measure timestamp drift to set the dedup tolerance
  build_reference_space.py, check_env.py
data/reference/              # read-only UMAP reference space (dev resource location)
tests/                       # pytest suite
reference_code/              # prior exploratory scripts (read-only reference)
```
