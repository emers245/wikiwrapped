# Wikipedia Wrapped

Wikipedia Wrapped shows what you have been reading on Wikipedia: a map of
the topics you read about, the paths you took from one article to the
next, and when you read them. It builds all of this from your own browsing
history (Google Chrome, and optionally Google Takeout archives), on your
own computer: your history never leaves it.

## Download and install

Wikipedia Wrapped runs on macOS 11 or later (Apple silicon or Intel) and on
64-bit Windows 10 or 11. It installs for you only, needs no administrator
password, and keeps everything it reads and saves on your computer.

The installers are on the
[Releases page](https://github.com/emers245/wikipedia-wrapped/releases)
(while the repository is private, only people with access to it can see
them). Download the one for your computer:

| Your computer | Download the file ending in |
|---|---|
| Mac with Apple silicon (M1 or later) | `-macOS-AppleSilicon.pkg` |
| Mac with an Intel processor | `-macOS-Intel.pkg` |
| Windows 10 or 11, 64-bit | `-Windows-x64.exe` |

Not sure which Mac you have? Choose **Apple menu > About This Mac**: a
**Chip** line means Apple silicon; a **Processor** line naming Intel means
Intel. Skip the **Source code** downloads on the release page: they are
the program's code, not an installer.

The installers are not yet signed by a registered developer, so your
computer asks you to confirm once before running each one.

### On a Mac

1. Double-click the downloaded `.pkg`. The first time, macOS refuses to
   open it because it cannot check the developer. Close the message.
2. Choose **Apple menu > System Settings**, click **Privacy & Security** in
   the sidebar, and scroll down to **Security**. Next to the note about
   the installer, click **Open Anyway**, enter your login password, and
   confirm with **Open**. The button is there for about an hour after you
   tried to open the file.
   On macOS 14 or earlier you can instead Control-click the `.pkg` in
   Finder, choose **Open**, then **Open** again. (On macOS 11 and 12 the
   setting above is in **System Preferences > Security & Privacy >
   General**.)
3. Follow the installer. It installs for you only, without asking for an
   administrator password. Near the end it sits still for a few minutes
   while it prepares the app; notifications show it is still working.
4. Open the app: press **Command-Space**, type Wikipedia Wrapped, and press
   Return. It is also in the **Applications** folder inside your home
   folder (in Finder, **Go > Home**); drag it to the Dock to keep it there.

> Screenshot placeholder: Privacy & Security, Security section, with Open Anyway

### On Windows

1. Your browser may say the file is not commonly downloaded. In Microsoft
   Edge, open the download's **...** menu, choose **Keep**, then **Show
   more > Keep anyway**.
2. Double-click the `.exe`. Windows shows **Windows protected your PC**:
   click **More info**, then **Run anyway**.
3. Follow the installer and keep its suggestions: **Just Me** (no
   administrator needed) and the folder it proposes. Installing takes a
   few minutes.
4. Open the app from the Start menu or from the shortcut on your desktop.

> Screenshot placeholder: Windows protected your PC, after clicking More info

On some Windows 11 computers **Smart App Control** blocks unsigned apps
outright, with no **Run anyway**. It can only be turned off for every app
at once (**Windows Security > App & browser control > Smart App Control
settings**), which you may prefer not to do; a signed installer, planned
for a later version, avoids this.

### Updating

Quit the app, download the new installer, and run it the same way (the
confirmation step comes again for each new download). It installs the new
version and removes the old one; your history and settings are kept.
Reinstalling the version you already have is refused: uninstall it first.

### Uninstalling

Uninstalling removes the program and keeps your data, so installing again
later picks up where you left off.

- **Mac:** quit the app, open Terminal (in **Applications > Utilities**),
  and run

  ```bash
  bash ~/Library/WikipediaWrapped-App/uninstall.sh
  ```

  It removes the program (`~/Library/WikipediaWrapped-App`) and the app in
  your Applications folder. Dragging the app to the Trash is not enough:
  it leaves the program folder, about 2 GB, behind.
- **Windows:** **Settings > Apps > Installed apps** (on Windows 10, **Apps
  & features**), find **Wikipedia Wrapped**, and choose **Uninstall**.

To remove your data as well, delete the data folder below afterwards,
after copying `data/visits.jsonl` somewhere safe if you might want it. If
you used Google Drive sign-in (the Advanced option), also remove the app's
access at [myaccount.google.com/connections](https://myaccount.google.com/connections).

### Where things are

| | Mac | Windows |
|---|---|---|
| Your data (history, settings, logs) | `~/Library/Application Support/WikipediaWrapped` | `%APPDATA%\WikipediaWrapped` |
| The program | `~/Library/WikipediaWrapped-App` | `%LOCALAPPDATA%\WikipediaWrapped-App` |
| The shortcut | `Applications` in your home folder | Start menu and desktop |

If the app does not open, the in-app Help ("Troubleshooting") explains how
to run its self-check; the same Help covers everything after installing.

## Using the app

1. **The first time you open it**, a short setup asks which Chrome
   profiles to read, whether to add Google Takeout archives (optional;
   they reach further back and cover your other devices), and whether to
   sync each time the app starts.
2. **The first sync** reads your history and looks up each article on
   Wikipedia to find its topic and its place on the map. With a lot of
   history this takes several minutes; progress shows at the top of the
   window, and you can use the app meanwhile.
3. **After that**, the app syncs each time it starts, so new reading
   appears on its own. Change anything later from **Settings**.

**Open the app at least once a month.** Chrome deletes history older than
about 90 days; the app keeps a permanent copy of every Wikipedia visit it
has synced, but only of what Chrome still had at the time.

Everything else, from reading the map to setting up Google Takeout and
fixing problems, is in the app's **Help** (the same page is
[`app/static/help.html`](app/static/help.html) in this repository).

## For developers

Running from source, the architecture, and the data pipeline are in
[DEVELOPMENT.md](DEVELOPMENT.md); building installers and cutting releases
in [packaging/README.md](packaging/README.md).
