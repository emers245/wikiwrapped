<!-- Template for the draft release (.github/workflows/build-installers.yml).
     Edit the draft before publishing: say what is new, and remove this comment. -->

## What's new

TODO(Joe): write before publishing.

## Download

Pick the file for your computer. (The **Source code** downloads that GitHub
adds to every release are the program's code, not an installer.)

| Your computer | File ending in |
|---|---|
| Mac with Apple silicon (M1 or later) | `-macOS-AppleSilicon.pkg` |
| Mac with an Intel processor | `-macOS-Intel.pkg` |
| Windows 10 or 11, 64-bit | `-Windows-x64.exe` |

Not sure which Mac you have? Apple menu > About This Mac: a "Chip" line
means Apple silicon; a "Processor" line naming Intel means Intel.

The installers are not code-signed yet, so macOS and Windows ask you to
confirm once before running them: on a Mac, System Settings > Privacy &
Security > **Open Anyway**; on Windows, **More info** > **Run anyway**. The
README's [Download and install](https://github.com/emers245/wikiwrapped#download-and-install)
section walks through every step, and through updating and uninstalling.
Updating: quit the app and run the new installer; it replaces the old
version and keeps your data.

To check a download is intact, compare its SHA-256 with `SHA256SUMS`:
`shasum -a 256 <file>` in the macOS Terminal, `Get-FileHash <file>` in
Windows PowerShell.
