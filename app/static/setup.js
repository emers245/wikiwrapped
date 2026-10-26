/* First-run setup and the Settings screen.

   One overlay, two modes, the same building blocks:
   - wizard: shown automatically on first run (no settings file yet), one
     section per step, ending with Finish, which saves and starts the
     first sync.
   - settings: opened from the header's Settings button, every section on
     one scrolling page with Save.

   Nothing here decides anything. The server (app/setup_service.py via
   /settings) validates every change and reports problems as plain
   messages, which are shown as they come. The page keeps a draft of the
   choices and sends it with PUT /settings in one go, so Cancel really
   does cancel. The exceptions are the Advanced account actions (import a
   client secret, sign in, remove an account), which take effect at once
   because the browser sign-in itself does.

   Native folder and file pickers come from the app window
   (window.pywebview.api, see app/launcher.py NativeApi). Opened in a
   normal browser (--browser), there is no such object and the pickers
   become text fields.

   The DOM is built with textContent throughout, never innerHTML, so a
   folder path or label can never be interpreted as markup. */

window.WWsetup = (function () {
  const overlay = document.getElementById("setup");
  const titleEl = document.getElementById("setup-title");
  const stepEl = document.getElementById("setup-step");
  const body = document.getElementById("setup-body");
  const errorEl = document.getElementById("setup-error");
  const btnBack = document.getElementById("setup-back");
  const btnCancel = document.getElementById("setup-cancel");
  const btnNext = document.getElementById("setup-next");

  let mode = "settings";
  let status = null;      // GET /settings
  let draft = null;       // the choices being edited
  let profiles = null;    // GET /settings/chrome-profiles, loaded lazily
  let profilesError = null;
  let stepIndex = 0;
  let busy = false;

  /* -- small helpers ------------------------------------------------------- */

  /* el("p", {class: "note"}, "text", childNode, ...) */
  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === undefined || value === null || value === false) continue;
      if (key === "class") node.className = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else if (key === "checked" || key === "disabled" || key === "value") node[key] = value;
      else node.setAttribute(key, value === true ? "" : value);
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  async function api(method, url, payload) {
    const options = { method, headers: {} };
    if (payload !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(payload);
    }
    const response = await fetch(url, options);
    let data = null;
    try {
      data = await response.json();
    } catch (err) {
      /* a non-JSON error page; the status text below has to do */
    }
    if (!response.ok) {
      const detail = data && data.detail ? data.detail : response.statusText;
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return data;
  }

  function showError(message) {
    errorEl.textContent = message || "";
    errorEl.classList.toggle("hidden", !message);
  }

  function hasNativePickers() {
    return !!(window.pywebview && window.pywebview.api &&
              window.pywebview.api.pick_folder);
  }

  function openHelp(anchor) {
    if (window.WWhelp) {
      window.WWhelp.open(anchor);
    } else {
      window.open("/static/help.html" + (anchor ? "#" + anchor : ""), "_blank");
    }
  }

  function helpLink(anchor, text) {
    return el("a", {
      href: "/static/help.html#" + anchor,
      onclick: event => { event.preventDefault(); openHelp(anchor); },
    }, text);
  }

  /* -- the draft ----------------------------------------------------------- */

  function draftFrom(current) {
    return {
      local: {
        enabled: current.local.enabled,
        chrome_profiles: current.local.chrome_profiles.slice(),
      },
      watched_folders: current.watched_folders.map(f => ({ ...f })),
      sync: { ...current.sync },
      takeout: {
        enabled: current.takeout.enabled,
        drive_folder: current.takeout.drive_folder,
      },
      // Directories ticked in the profile list; filled once profiles load.
      selected: null,
      // Accounts left out of the dashboard ("" = no account recorded).
      hidden: new Set(current.dashboard.accounts
        .filter(a => !a.shown).map(a => a.account)),
    };
  }

  function hiddenList() {
    return [...draft.hidden].sort();
  }

  /* The profile checkboxes, turned into what /settings expects:
     all ticked = [] (all, including future profiles); none = disabled. */
  function localChanges() {
    if (!profiles || draft.selected === null) return null;
    const ticked = profiles.filter(p => draft.selected.has(p.directory));
    if (ticked.length === profiles.length) return { enabled: true, chrome_profiles: [] };
    if (!ticked.length) return { enabled: false, chrome_profiles: [] };
    return { enabled: true, chrome_profiles: ticked.map(p => p.directory) };
  }

  function changes() {
    const out = {
      watched_folders: draft.watched_folders.map(f => ({ path: f.path, label: f.label })),
      sync: draft.sync,
      takeout: draft.takeout,
    };
    const local = localChanges();
    if (local) out.local = local;
    out.dashboard = { hidden_accounts: hiddenList() };
    return out;
  }

  async function loadProfiles() {
    try {
      profiles = await api("GET", "/settings/chrome-profiles");
      draft.selected = new Set(profiles.filter(p => p.selected).map(p => p.directory));
    } catch (err) {
      profilesError = err.message;
    }
    if (overlay.classList.contains("hidden")) return;
    rerender();
  }

  /* -- sections ------------------------------------------------------------ */

  function sectionWelcome() {
    return el("div", { class: "setup-section" },
      el("p", {}, "Wikipedia Wrapped shows what you have been reading on " +
        "Wikipedia: a map of the topics, the paths between articles, and " +
        "when you read them."),
      el("h3", {}, "Your data stay on this computer"),
      el("p", {}, "The app reads your Chrome history, and any Google " +
        "Takeout archives you point it to, keeps only the Wikipedia visits, " +
        "and stores them here:"),
      el("p", { class: "setup-path" }, status.paths.user_data),
      el("h3", {}, "What is sent over the internet"),
      el("ul", {},
        el("li", {}, "The titles of the Wikipedia articles you visited are " +
          "sent to Wikipedia's public APIs (run by the Wikimedia " +
          "Foundation) to look up each article's topic, summary, and " +
          "popularity. Nothing about you is sent with them."),
        el("li", {}, "Only if you turn on the Advanced Google Drive option: " +
          "the app lists and downloads your Takeout archives from your own " +
          "Google Drive.")),
      el("p", {}, "Nothing else leaves your computer. The language model " +
        "that places articles on the map comes with the app and runs " +
        "locally. Next, choose where your history comes from."));
  }

  /* The words for where an account's visits came from. */
  const SOURCE_NAMES = {
    chrome_local: "Chrome on this computer",
    takeout: "Google Takeout",
    legacy_db: "recovered from the old database",
    no_record: "saved before sources were recorded",
  };

  function accountName(account) {
    return account === "" ? "No account recorded" : account;
  }

  function sectionShown() {
    const accounts = status.dashboard.accounts;
    const section = el("div", { class: "setup-section" },
      el("p", {}, "Choose whose reading the dashboard shows. This only " +
        "changes what you see: nothing is deleted, syncing carries on, and " +
        "ticking an account again brings all of it back."));
    if (!accounts.length) {
      section.append(el("p", { class: "setup-muted" },
        "No history yet. Accounts appear here after the first sync."));
      return section;
    }
    const list = el("div", { class: "setup-list" });
    for (const entry of accounts) {
      const parts = Object.entries(entry.by_source)
        .sort((a, b) => b[1] - a[1])
        .map(([source, n]) => `${n.toLocaleString()} from ${SOURCE_NAMES[source] || source}`);
      list.append(el("label", { class: "setup-check" },
        el("input", {
          type: "checkbox",
          checked: !draft.hidden.has(entry.account),
          onchange: event => {
            if (event.target.checked) draft.hidden.delete(entry.account);
            else draft.hidden.add(entry.account);
          },
        }),
        el("span", {},
          el("strong", {}, accountName(entry.account)),
          el("span", { class: "setup-muted" },
            ` - ${entry.visits.toLocaleString()} visit${entry.visits === 1 ? "" : "s"}` +
            (parts.length ? ` (${parts.join(", ")})` : "")))));
    }
    section.append(list);
    section.append(el("p", { class: "setup-muted" }, "A visit that two " +
      "accounts both saw (say, through Chrome and through Takeout) counts " +
      "under each, so these can add up to more than your total. It stays " +
      "visible while either account is shown. \"No account recorded\" is " +
      "history saved before the app tracked accounts, or from a Chrome " +
      "profile that is not signed in."));
    return section;
  }

  function sectionProfiles() {
    const section = el("div", { class: "setup-section" },
      el("p", {}, "Wikipedia Wrapped reads Google Chrome's history on this " +
        "computer. Chrome keeps about 90 days of history, so the app saves " +
        "a copy of the Wikipedia visits each time it syncs."));
    if (mode === "settings") {
      section.append(el("p", { class: "setup-muted" }, "These choose what " +
        "the app collects. To hide an account's history from the dashboard " +
        "without stopping its sync, use \"Show in dashboard\" above."));
    }
    if (profilesError) {
      section.append(el("p", { class: "setup-problem" },
        "Could not look for Chrome profiles: " + profilesError));
      return section;
    }
    if (!profiles) {
      section.append(el("p", { class: "setup-muted" }, "Looking for Chrome profiles..."));
      return section;
    }
    if (!profiles.length) {
      section.append(
        el("p", { class: "setup-problem" }, "No Chrome profiles were found on this computer."),
        el("p", {}, "This app reads Google Chrome's history. If you use " +
          "Chrome, open it once so it creates its profile, then reopen " +
          "Settings. If your Chrome history is synced to your Google " +
          "account, Google Takeout (next) can still bring it in. Other " +
          "browsers are not supported."),
        el("p", {}, helpLink("no-profiles", "Help: no Chrome profiles found")));
      return section;
    }
    section.append(el("p", {}, "Choose which Chrome profiles to read. " +
      "Leaving all of them ticked also includes profiles you add later."));
    const list = el("div", { class: "setup-list" });
    for (const profile of profiles) {
      const visits = profile.wikipedia_visits === null
        ? "could not be read right now"
        : `${profile.wikipedia_visits} Wikipedia visit${profile.wikipedia_visits === 1 ? "" : "s"} still in Chrome`;
      list.append(el("label", { class: "setup-check" },
        el("input", {
          type: "checkbox",
          checked: draft.selected.has(profile.directory),
          onchange: event => {
            if (event.target.checked) draft.selected.add(profile.directory);
            else draft.selected.delete(profile.directory);
          },
        }),
        el("span", {},
          el("strong", {}, profile.display_name),
          profile.account ? ` (${profile.account})` : "",
          el("span", { class: "setup-muted" }, ` - ${visits}`))));
    }
    section.append(list);
    // These counts are what Chrome holds today, not what the app has saved,
    // and the two differ by design; say so before someone adds them up.
    section.append(el("p", { class: "setup-muted" }, "Counts are what Chrome " +
      "still holds (about the last 90 days). The app keeps the older visits " +
      "it has already saved, and Google Takeout can add history from your " +
      "other devices, so your dashboard total is usually larger."));
    return section;
  }

  function folderRow(folder, index) {
    return el("div", { class: "setup-folder" },
      el("div", { class: "setup-folder-main" },
        el("div", { class: "setup-path" }, folder.path),
        el("div", {
          class: folder.status && folder.status !== "ok" ? "setup-problem" : "setup-muted",
        }, folder.message || "")),
      el("input", {
        type: "text", class: "setup-label", placeholder: "Label (optional)",
        value: folder.label || "",
        "aria-label": "Label for this folder",
        oninput: event => { draft.watched_folders[index].label = event.target.value; },
      }),
      el("button", {
        onclick: () => { draft.watched_folders.splice(index, 1); rerender(); },
      }, "Remove"));
  }

  async function addFolder(path) {
    if (!path) return;
    showError("");
    const found = await api("POST", "/settings/folders/inspect", { path });
    if (found.status !== "ok") {
      showError(found.message);
      return;
    }
    if (draft.watched_folders.some(f => f.path === found.path)) {
      showError("That folder is already in the list.");
      return;
    }
    draft.watched_folders.push({
      path: found.path, label: "", status: found.status,
      zips_found: found.zips_found, message: found.message,
    });
    rerender();
  }

  function sectionFolders() {
    const section = el("div", { class: "setup-section" },
      el("p", {}, "Optional. Google Takeout can export your Chrome history " +
        "from your Google account, which reaches back further than this " +
        "computer's 90 days and includes your other devices."),
      el("p", {}, "Choose the folder where your Takeout archives arrive: " +
        "the Takeout folder that Google Drive for desktop keeps in sync, or " +
        "a folder you download the archives into. The app checks it on " +
        "every sync and only ever reads the archives; it never changes, " +
        "moves, or deletes them. No Google sign-in is needed."),
      el("p", {}, helpLink("takeout", "How to set up Google Takeout")));

    const list = el("div", { class: "setup-list" });
    if (!draft.watched_folders.length) {
      list.append(el("p", { class: "setup-muted" }, "No folders yet."));
    }
    draft.watched_folders.forEach((folder, index) => list.append(folderRow(folder, index)));
    section.append(list);

    if (hasNativePickers()) {
      section.append(el("button", {
        onclick: async () => {
          const path = await window.pywebview.api.pick_folder("");
          await addFolder(path).catch(err => showError(err.message));
        },
      }, "Add a folder..."));
    } else {
      const input = el("input", {
        type: "text", class: "setup-path-input",
        placeholder: "Full path to a folder", "aria-label": "Folder path",
      });
      section.append(el("div", { class: "setup-inline" }, input,
        el("button", {
          onclick: () => addFolder(input.value.trim()).catch(err => showError(err.message)),
        }, "Add folder")));
    }
    section.append(el("p", { class: "setup-muted" }, "A label such as " +
      "\"Personal\" or your Google email tells archives from different " +
      "accounts apart."));
    return section;
  }

  function accountRow(account) {
    const state = account.needs_reauthorization
      ? el("span", { class: "setup-problem" }, "needs you to sign in again")
      : el("span", { class: "setup-ok" }, "signed in");
    return el("div", { class: "setup-account" },
      el("span", {}, el("strong", {}, account.email), " - ", state),
      el("span", { class: "setup-spacer" }),
      account.needs_reauthorization
        ? el("button", { onclick: () => signIn(account.email) }, "Sign in again")
        : null,
      el("button", { onclick: () => removeAccount(account.email) }, "Remove"));
  }

  async function signIn(emailHint) {
    showError("");
    busy = true;
    rerender();
    try {
      const result = await api("POST", "/settings/accounts/authorize",
                               { email_hint: emailHint || null });
      status.takeout = result.takeout;
      draft.takeout.enabled = result.takeout.enabled;
    } catch (err) {
      showError(err.message);
    } finally {
      busy = false;
      rerender();
    }
  }

  async function removeAccount(email) {
    showError("");
    try {
      status.takeout = await api("DELETE",
        "/settings/accounts/" + encodeURIComponent(email));
    } catch (err) {
      showError(err.message);
    }
    rerender();
  }

  async function chooseClientSecret(path) {
    if (!path) return;
    showError("");
    try {
      status.takeout = await api("POST", "/settings/client-secret", { path });
    } catch (err) {
      showError(err.message);
    }
    rerender();
  }

  function sectionAdvanced() {
    const takeout = status.takeout;
    const details = el("details", { class: "setup-section", open: takeout.enabled || null },
      el("summary", {}, "Fetch Takeout archives straight from Google Drive"));
    // In the wizard this is a whole step; say up front that it is optional
    // so a collapsed section does not read as an empty page.
    const intro = mode === "wizard"
      ? el("p", { class: "setup-muted" }, "Optional, and most people can " +
          "skip it: click Next. Open the section below only if you want the " +
          "app to sign in to Google Drive itself.")
      : null;
    details.append(
      el("p", {}, "Most people can skip this: a watched folder (previous " +
        "step) does the same job without signing in. This option has the " +
        "app sign in to Google Drive itself, using an OAuth client you " +
        "create in your own Google Cloud project. Access is read-only."),
      el("p", { class: "setup-muted" }, "While your OAuth app is in " +
        "\"Testing\", Google signs it out after about 7 days, and the app " +
        "will ask you to sign in again."),
      el("p", {}, helpLink("advanced", "Help: setting up the Google Drive option")));

    if (!takeout.libraries_available) {
      details.append(el("p", { class: "setup-problem" },
        "The Google libraries are not installed, so this option is unavailable."));
      return intro ? el("div", {}, intro, details) : details;
    }

    details.append(el("label", { class: "setup-check" },
      el("input", {
        type: "checkbox", checked: draft.takeout.enabled,
        onchange: event => { draft.takeout.enabled = event.target.checked; },
      }),
      el("span", {}, "Use Google Drive")));

    const secretRow = el("div", { class: "setup-inline" },
      el("span", {}, "OAuth client secret: ",
        takeout.client_secret
          ? el("strong", {}, takeout.client_secret)
          : el("span", { class: "setup-muted" }, "none yet")));
    if (hasNativePickers()) {
      secretRow.append(el("button", {
        onclick: async () => chooseClientSecret(await window.pywebview.api.pick_file("")),
      }, "Choose file..."));
    } else {
      const input = el("input", {
        type: "text", class: "setup-path-input",
        placeholder: "Path to client_secret....json", "aria-label": "Client secret path",
      });
      secretRow.append(input, el("button", {
        onclick: () => chooseClientSecret(input.value.trim()),
      }, "Use this file"));
    }
    details.append(secretRow);

    const accounts = el("div", { class: "setup-list" });
    if (!takeout.accounts.length) {
      accounts.append(el("p", { class: "setup-muted" }, "No Google accounts yet."));
    }
    takeout.accounts.forEach(account => accounts.append(accountRow(account)));
    details.append(accounts);

    if (busy) {
      details.append(el("p", { class: "setup-waiting" }, "A browser window " +
        "opened for you to sign in to Google. Finish there and come back " +
        "here (this waits up to 5 minutes)."));
    } else {
      details.append(el("button", {
        disabled: !takeout.client_secret,
        title: takeout.client_secret ? null : "Choose the client secret file first",
        onclick: () => signIn(null),
      }, "Add a Google account..."));
    }

    details.append(el("label", { class: "setup-field" },
      el("span", {}, "Drive folder to search (optional; blank searches all of Drive)"),
      el("input", {
        type: "text", value: draft.takeout.drive_folder,
        placeholder: "Takeout",
        oninput: event => { draft.takeout.drive_folder = event.target.value; },
      })));
    return intro ? el("div", {}, intro, details) : details;
  }

  function sectionSync() {
    const section = el("div", { class: "setup-section" },
      el("label", { class: "setup-check" },
        el("input", {
          type: "checkbox", checked: draft.sync.on_launch,
          onchange: event => { draft.sync.on_launch = event.target.checked; },
        }),
        el("span", {}, "Sync when the app starts (recommended)")),
      el("p", { class: "setup-muted" }, "Chrome forgets history after " +
        "about 90 days, so a visit is only kept if the app syncs before " +
        "then. Opening the app at least once a month is enough."),
      el("label", { class: "setup-field" },
        el("span", {}, "Wait at least this many hours between syncs"),
        el("input", {
          type: "number", min: "0", max: "720", step: "1",
          value: String(draft.sync.min_interval_hours),
          oninput: event => { draft.sync.min_interval_hours = event.target.value; },
        })));
    if (mode === "settings") {
      section.append(el("div", { class: "setup-inline" },
        el("button", { onclick: syncNow }, "Sync now"),
        el("span", { class: "setup-muted" }, "Saves these settings, then syncs " +
          "every source, ignoring the wait above.")));
      section.append(
        el("h3", {}, "Where your data are"),
        el("p", {}, "Data: ", el("span", { class: "setup-path" }, status.paths.user_data)),
        el("p", {}, "Logs: ", el("span", { class: "setup-path" }, status.paths.logs)));
    }
    return section;
  }

  function sectionFinish() {
    const local = localChanges();
    const profileText = !profiles || !profiles.length ? "No Chrome profiles"
      : !local || (local.enabled && !local.chrome_profiles.length)
        ? `All Chrome profiles (${profiles.length})`
        : local.enabled ? `${local.chrome_profiles.length} of ${profiles.length} Chrome profiles`
          : "No Chrome profiles";
    const folders = draft.watched_folders.length;
    return el("div", { class: "setup-section" },
      el("ul", {},
        el("li", {}, profileText),
        el("li", {}, folders ? `${folders} Takeout folder${folders === 1 ? "" : "s"}` : "No Takeout folders"),
        el("li", {}, draft.takeout.enabled ? "Google Drive: on" : "Google Drive: off"),
        el("li", {}, draft.sync.on_launch ? "Sync when the app starts" : "Sync only when you ask")),
      el("p", {}, "The first sync reads your history, then looks up each " +
        "article on Wikipedia to add its topic and its place on the map. " +
        "With a lot of history that can take several minutes; progress " +
        "shows at the top of the window, and you can use the app meanwhile."),
      el("p", {}, "You can change any of this later from Settings."));
  }

  function sectionProblems() {
    const last = status.last_sync;
    const problems = [];
    for (const email of last.reauthorize) {
      problems.push(`Google account ${email} needs you to sign in again ` +
        "(Advanced, below).");
    }
    for (const message of last.errors) {
      if (message.startsWith("Re-authorization needed")) continue;
      problems.push(message);
    }
    if (status.warning) problems.push(status.warning);
    if (!problems.length) return null;
    return el("div", { class: "setup-section setup-problems" },
      el("h3", {}, "Problems from the last sync"),
      el("ul", {}, problems.map(p => el("li", {}, p))),
      el("p", {}, helpLink("troubleshooting", "Help: troubleshooting")));
  }

  /* -- steps and rendering -------------------------------------------------- */

  const STEPS = [
    { title: "Welcome to Wikipedia Wrapped", build: sectionWelcome, wizardOnly: true },
    { title: "Browser history", build: sectionProfiles },
    { title: "Google Takeout (optional)", build: sectionFolders },
    { title: "Advanced: Google Drive sign-in", build: sectionAdvanced },
    { title: "Syncing", build: sectionSync },
    { title: "All set", build: sectionFinish, wizardOnly: true },
  ];

  function rerender() {
    // Keep the scroll position: rerendering replaces the whole body.
    const scroll = body.scrollTop;
    render();
    body.scrollTop = scroll;
  }

  function render() {
    body.replaceChildren();
    if (mode === "wizard") {
      const step = STEPS[stepIndex];
      titleEl.textContent = step.title;
      stepEl.textContent = `Step ${stepIndex + 1} of ${STEPS.length}`;
      body.append(step.build());
      btnBack.classList.toggle("hidden", stepIndex === 0);
      btnCancel.classList.add("hidden");
      btnNext.textContent = stepIndex === STEPS.length - 1 ? "Save and start" : "Next";
    } else {
      titleEl.textContent = "Settings";
      stepEl.textContent = "";
      const problems = sectionProblems();
      if (problems) body.append(problems);
      body.append(el("h3", { class: "setup-section-title" }, "Show in dashboard"),
                  sectionShown());
      for (const step of STEPS) {
        if (step.wizardOnly) continue;
        body.append(el("h3", { class: "setup-section-title" }, step.title), step.build());
      }
      btnBack.classList.add("hidden");
      btnCancel.classList.remove("hidden");
      btnNext.textContent = "Save";
    }
    btnNext.disabled = busy;
  }

  async function save() {
    showError("");
    status = await api("PUT", "/settings", changes());
  }

  async function syncNow() {
    const hiddenBefore = JSON.stringify(
      status.dashboard.accounts.filter(a => !a.shown).map(a => a.account).sort());
    try {
      await save();
      await api("POST", "/jobs/sync");
    } catch (err) {
      showError(err.message);
      return;
    }
    close();
    // A reload picks the running job straight back up in the job strip.
    if (JSON.stringify(hiddenList()) !== hiddenBefore) location.reload();
    else if (window.WW && WW.pollJobsNow) WW.pollJobsNow();
  }

  async function next() {
    if (busy) return;
    if (mode === "settings") {
      const hiddenBefore = JSON.stringify(
        status.dashboard.accounts.filter(a => !a.shown).map(a => a.account).sort());
      try {
        await save();
      } catch (err) {
        showError(err.message);
        return;
      }
      close();
      // The dashboard is built once from /dashboard; showing a different
      // set of accounts means building it again.
      if (JSON.stringify(hiddenList()) !== hiddenBefore) location.reload();
      return;
    }
    if (stepIndex < STEPS.length - 1) {
      stepIndex += 1;
      showError("");
      render();
      body.scrollTop = 0;
      return;
    }
    // Finish: save, then start the first sync. The job strip takes over.
    try {
      await save();
      await api("POST", "/jobs/sync");
    } catch (err) {
      showError(err.message);
      return;
    }
    close();
    if (window.WW && WW.pollJobsNow) WW.pollJobsNow();
  }

  btnNext.addEventListener("click", next);
  btnBack.addEventListener("click", () => {
    if (stepIndex > 0) {
      stepIndex -= 1;
      showError("");
      render();
    }
  });
  btnCancel.addEventListener("click", close);
  overlay.addEventListener("keydown", event => {
    if (event.key === "Escape" && mode === "settings" && !busy) close();
  });

  /* -- open / close ------------------------------------------------------- */

  async function open(asMode) {
    try {
      status = await api("GET", "/settings");
    } catch (err) {
      return;   // the server is not answering; nothing sensible to show
    }
    mode = asMode || (status.first_run ? "wizard" : "settings");
    draft = draftFrom(status);
    profiles = null;
    profilesError = null;
    stepIndex = 0;
    showError("");
    render();
    overlay.classList.remove("hidden");
    btnNext.focus();
    loadProfiles();
  }

  function close() {
    overlay.classList.add("hidden");
  }

  document.getElementById("btn-settings").addEventListener("click", () => open("settings"));

  /* pywebview attaches window.pywebview.api only after the page has loaded
     and announces it with this event. A screen rendered before then shows
     text fields; re-render so the native pickers replace them. */
  window.addEventListener("pywebviewready", () => {
    if (!overlay.classList.contains("hidden")) rerender();
  });

  // First run: the setup wizard comes up over the (empty) dashboard.
  api("GET", "/settings").then(current => {
    if (current.first_run) open("wizard");
  }).catch(() => {});

  return { open, close };
})();
