# Obsidian iCloud Windows Sync

A highly optimized, asynchronous, three-way sync engine designed to solve the notorious issues between Obsidian, iCloud Drive, and Windows.

![terminal image](assets/image.png)

## Installation & Setup
1. Install iCloud for Windows [Link](https://support.apple.com/en-ca/103232)
2. Clone the repository using `git clone git@github.com:gursimar/obsidian-icloud-windows-sync.git`
3. Install as a package:
   ```bash
   pip install .
   ```
4. Copy and edit the config file with your actual paths:
   ```powershell
   Copy-Item config.yaml my-config.yaml
   ```
   ```yaml
   paths:
     local_vault: "C:\\Obsidian\\Vault"
     icloud_vault: "C:\\Users\\user\\iCloudDrive\\iCloud~md~obsidian"
     history_dir: "C:\\Obsidian\\History"
     logs_dir: "C:\\Obsidian\\Logs"
   ```
5. Run:
   ```bash
   obsidian-sync --config my-config.yaml
   ```
   Or directly:
   ```bash
   python -m obsidian_sync --config my-config.yaml
   ```

> If you have any trouble in setup, raise issue on git.
> Run natively on Windows, not WSL — iCloud placeholders behave incorrectly under WSL.

## Project Structure

```
obsidian_sync/
├── __main__.py      # CLI entry point
├── config.py        # YAML config loading & validation
├── logger.py        # Structured logging (console + file)
├── disk_io.py       # Atomic copy, delete, Windows API
├── hasher.py        # SHA-256 hashing with mtime/size cache
├── icloud_status.py # Windows file attributes for iCloud sync state
├── duplicates.py    # Startup duplicate/conflict scanner
├── sync_engine.py   # File watchers + per-file event queues
└── sync_worker.py   # Three-way sync logic with atomic operations
```

### Modes of Operation

| Mode | Config | Behavior |
|---|---|---|
| **One-Shot** | `run_continuously: false` | Single full pass (no file watcher), then exits. Use with Task Scheduler. |
| **Daemon** | `run_continuously: true` | Watches both vaults for changes and does a full rescan every `rescan_interval` seconds. |

#### Autostart via Task Scheduler

Create `run-sync.ps1`:
```powershell
& py -m obsidian_sync --config "$PSScriptRoot\config.yaml"
```

In `taskschd.msc` → Create Task:
- **Triggers**: At log on, delay 1 minute
- **Actions**: `C:\Windows\System32\conhost.exe` with arguments:
   `--headless powershell.exe -WindowStyle Hidden -NoProfile -NonInteractive -file "C:\PATH\TO\run-sync.ps1"`
- **Settings**: Restart on failure every 1 minute, up to 99 times

---

## How It Works

Three locations are tracked per file:
- **L** = Local vault (`local_vault`)
- **C** = iCloud copy (`icloud_vault`)
- **H** = History snapshot (`history_dir`) — last known good state

Each sync pass walks the union of all three directories and applies these rules:

| State | Action |
|---|---|
| `L` only | New local file → stabilize → push to `C`, seed `H` |
| `C` only | New remote file → stabilize → restore to `L`, seed `H` |
| `L == C == H` | Nothing to do |
| `L != H`, `C == H` | Local changed → push `L` → `C`, update `H` |
| `C != H`, `L == H` | Remote changed → restore `L` from `C`, update `H` |
| `L != H`, `C != H` | Conflict → stabilize → pick newer by mtime, backup loser as `_CONFLICT_TIMESTAMP` |
| `L` missing, `C == H` | Confirmed local delete → remove `C` and `H` |
| `L` missing, `C != H` | Remote changed → restore `L` from `C` |
| `C` missing, `L == H` | Confirmed remote delete → remove `L` and `H` |
| `C` missing, `L != H` | Local changed → push `L` → `C` |
| `L` and `C` missing | Remove orphaned `H` |

### Key Protections

- **Per-file event queues**: each file has dedicated queue and worker task for complete isolation
- **Atomic snapshot propagation**: single source read to temp file, then fan out to all destinations
- **Stabilization** (`stability_window`): waits before acting on creates/deletes to avoid reacting to mid-save or rename workflows
- **Conflict wait** (`stabilize_wait`): longer wait on both-changed scenarios to detect still-active edits
- **Atomic writes**: write to `.tmp` then `os.replace()`, with retries and Win32 `MoveFileEx` fallback
- **Conflict duplicates**: losing side saved as `filename_CONFLICT_TIMESTAMP.ext` before overwrite
- **History is written last**: a push only updates `H` after iCloud accepted the file, so a failed upload is retried instead of being mistaken for a remote change
- **Vault guard**: if either vault folder is missing, or empty while history has files (iCloud signed out, drive not mounted), all syncing pauses instead of propagating mass deletions
- **Trash, not delete**: deletions (including the startup duplicate cleanup) move files to `trash_dir/<date>/<side>/...`, kept for `trash_retention_days`
- **Delete rate limit**: more than `max_deletes_per_minute` deletions are held back and retried later
- **iCloud wait timeout**: a file iCloud won't settle gives up after `icloud_wait_timeout` and is retried on the next rescan, so it can't stall other files

---

## Tuning

All settings live in `config.yaml`:

| Setting | Default | Notes |
|---|---|---|
| `stability_window` | `3s` | Increase for slow disks or large files |
| `stabilize_wait` | `8s` | Increase if you edit very slowly |
| `tiny_threshold` | `8 bytes` | Minimum file size to sync (prevents empty/placeholder files) |
| `max_concurrent_io` | `50` | Maximum concurrent I/O operations |
| `icloud_wait_timeout` | `600s` | Give up waiting for iCloud and retry later; `0` waits forever |
| `rescan_interval` | `300s` | Daemon full rescan period; `0` disables |
| `max_deletes_per_minute` | `50` | Safety brake on deletions; `0` disables |
| `trash_retention_days` | `30` | Days to keep trashed files; `0` keeps forever |
| `trash_dir` (under `paths`) | `<logs_dir>\trash` | Where deleted files go; must be outside the vaults and history |
| `ignore.patterns` | `[]` | Exclude folders like `.obsidian/cache` |
## Development

```bash
pip install -e .[dev]
python -m pytest tests
```
