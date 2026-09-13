# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`tsync.py` syncs Android game saves between devices over adb. Right now that means Toca Boca World between an Android tablet and BlueStacks on a Windows PC (`emulator-5554`). It uses only the Python stdlib and shells out to `git` and `adb`. `sync.bat` is the double-click entry point for a non-technical end user, so all user-facing text is Czech with simple A/N prompts.

`devices.json`, `store/` and `platform-tools/` are local-only and gitignored. They hold device IDs, save backups (including purchase tokens) and Google binaries. Keep personal data out of tracked files.

**Main rule: the tablet save must never be broken.** Every write has to be preceded by a verified backup and must be undoable. `store/` holds that backup history, so never delete or rewrite it.

## Commands

```
python tsync.py [--app toca] sync|status|backup [dev]|log [dev]|push SRC DST|restore DEV SNAPSHOT|export SNAPSHOT DIR|devices|verify|doctor
```
- `--yes` answers prompts. Conflicts and suspicious writes still refuse unless `--force` is given.
- `status`, `backup`, `log`, `devices`, `verify` and `doctor` never write to devices (`backup` may close the game).
- `setup.bat` → `setup.ps1` bootstraps a new PC. It installs Python and Git via winget and downloads platform-tools, but only what's missing and only after asking. Then it runs `tsync.py doctor`. It must stay idempotent: a rerun with everything present must not prompt or download anything.
- `setup.ps1` must be saved as UTF-8 **with BOM**, otherwise Windows PowerShell 5.1 garbles the Czech text.
- `self-update.bat` is a self-contained cmd/PowerShell polyglot, so a user can bring just this one file. Line 1 runs everything after the `::PS` marker as PowerShell and ends with `exit /b`, which means cmd never reads further and the file can safely replace itself.
  - It downloads the GitHub branch zip (no git needed) and copies only changed files.
  - It never writes into `store/`, `devices.json`, `platform-tools/` or `.git`, and it refuses to run in a git checkout.
  - To test it without the network, point `TSYNC_UPDATE_ZIP=<local zip>` at a zip laid out like GitHub's (`<repo>-main/...`).
- `sync.bat` prefers `py -3` over `python`, because a winget or python.org install often leaves `python.exe` off PATH.
- `find_git()` falls back to Git for Windows' default paths, for the same reason.
- adb is the bundled `platform-tools/adb.exe`, set in `devices.json`. BlueStacks' own `HD-Adb.exe` (1.0.36) can't talk to USB devices.
- In the Bash tool, set `PYTHONIOENCODING=utf-8` when piping output, or cp1252 crashes on Czech text.
- Run device shell snippets from the Bash tool. PowerShell mangles quotes in `adb exec-out '...'`.

### Testing (no test suite; sandboxed manual scenarios)
Never test against the real Toca profile or the tablet. Build a sandbox:
- `TSYNC_HOME=<dir>` redirects `apps/`, `store/` and `devices.json` to that dir.
- Put a fake profile in `<dir>/apps/fake.json` whose `root` is somewhere under `/sdcard/tsync-test/`.
- Make two fake devices on the one BlueStacks instance: aliases with serials `emulator-5554` and `127.0.0.1:5555` (run `adb connect 127.0.0.1:5555` first). Give each a per-device `"roots": {"fake": "/sdcard/tsync-test/A"}` (or `B`) override.
- Set `TSYNC_TEST_IDS=serial`. Devices are normally identified by Android ID, and both serials of one emulator share it, so without this the two fake devices collapse into one.
- The real tablet is often plugged in. List it in the sandbox `devices.json` with `"ignore": true`, otherwise the sandbox offers to adopt it.
- `TSYNC_TEST_FAULT=corrupt_after_apply` corrupts one file right after the apply step, once per process. Use it to exercise the rollback path.
- Scenarios worth rerunning after changes: first sync, one-sided change in each direction, conflict, fault+rollback, shrink guard, `restore`, unknown-device adoption, `verify`.
- Delete `/sdcard/tsync-test` afterwards.

## Architecture

**Profile classification (`apps/<app>.json`).** Every file under `root` gets one of three kinds via `Ctx.kind()`:
- `exclude`: ignored entirely. Caches are pruned on the device with `find -path`.
- `backup_only`: snapshotted but never written to or deleted from a device. Use it for device-bound data: accounts, purchase log, analytics IDs.
- everything else is synced. This is deliberate: state dirs added by future game versions get carried over instead of silently lost.

A glob without `/` matches the top-level entry; a glob with `/` matches the full relative path. `glob_match` and the device-side `find -path ./PATTERN` must stay consistent.

**Store (`store/<app>.git`)** is a bare repo used only through plumbing, so no checkout can ever overwrite anything:
- Snapshots are built with `hash-object -w --no-filters` and a temp `GIT_INDEX_FILE`, then `write-tree`, `commit-tree` and `update-ref`.
- `--no-filters`, `core.autocrlf=false` and `info/attributes` (`* -text`) are there so CRLF conversion can't corrupt saves.
- Each device has its own branch, `refs/heads/<alias>`.
- The commit message is a title line plus a JSON manifest: per file `[size, mtime, sha256]`, plus app version and device clock offset.
- If the tree equals the branch head, no commit is made. That dedup is the "no million saves" property.
- `Store.verify` re-reads the blobs and checks them against the manifest sha256.

**Device identity (`Ctx.connected`).** A device is matched by `id` (Android ID + `ro.serialno`) stored in `devices.json`, not by its adb serial.
- Every BlueStacks shows up as `emulator-5554`, so another PC's emulator must never pass for ours.
- An entry without an `id` learns it on first contact via its serial.
- An online device matching no entry is offered for naming (`adopt`), but only interactively, never with `--yes`.
- `"ignore": true` skips a device entirely.

**Sync decision (`cmd_sync`)** is three-way against `store/<app>.state.json` → `bases["<alias>+<alias>"].sync_id`, with one base per device pair:
- `sync_id` is a hash over the sync-kind files only.
- If only one side differs from base, propose that side as the source. If both differ, it's a conflict and the user chooses.
- With no base for the pair (a new device), make no proposal and let the user pick the direction. The newer save is often a fresh install.
- mtimes are only used for display and tie-breaking, corrected by the clock offset. Device clocks aren't trusted.

**Write path (`do_push` → `write_save`):**
1. Export the snapshot locally and verify it.
2. Re-list the device and require it to still match the backup (the game may have been running).
3. `adb push` individual files into `<root>/.tsync/in` and verify their sha256 on the device.
4. Push and run an on-device `apply.sh`. It `cat`s files over the existing ones in place, keeping inodes and ownership, and `rm`s sync-kind files the source lacks. It starts with `trap '' HUP` so a pulled cable can't stop it half-way.
5. Verify again.

A failure before step 4 raises `StagingError`: the save is untouched, so there is no rollback. Any later failure triggers an automatic rollback, which is `write_save` run with the backup snapshot.

**Guards in `do_push`:**
- `write_protected` in `devices.json` blocks all writes to that device.
- `shrink_warnings`: the source lacks *locations* the target has (only `locations_dir`; small state dirs come and go legitimately), is below 70% of its size, or comes from a newer app version. These need a typed `ANO` or `--force`.
- `ensure_closed` sends HOME, waits until the save files stop changing, then force-stops the app. It runs before the backup snapshot is taken.

## adb quirks that shaped the code
- Old `adbd` can't create missing dirs on `/sdcard` during push, so staging dirs are created with `mkdir -p` first.
- adb versions disagree on what pushing or pulling a *directory* does. Push explicit files per target dir. Pull top-level entries into an existing local dir, then check the result against the device listing.
- Old adb doesn't propagate exit codes. `Device.sh` uses `exec-out` (no pty, binary-safe) with an `echo __TSYNC_RC=$?` sentinel.
- Emulators started after the adb server aren't auto-discovered, so `connected()` runs `adb connect` for `host:port` serials.

## Toca Boca World specifics
- The save is spread over all of `files/`, not just `active_state/` (`*.tlws` locations).
- `playerprefs/` holds ID counters (`TocaInstantiatedIDCounter` etc.), unlocked scenes and home-designer metadata. Syncing locations without it risks duplicate IDs.
- `.bak`, `.backup` and `.pending` files are the game's own save journaling. Mirror them exactly, never filter them.
- Paid packs are tied to the Google Play account the app was *installed* from ("Billing preferred account via installer" in logcat). In BlueStacks, install Toca from the account that owns the purchases.
- Both devices must run the same app version (currently 1.138.1).
