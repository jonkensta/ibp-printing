# Label watcher

`ibp-label-watcher` runs on the shipping PC and prints shipping labels that
volunteers download from the EasyPost website.

When shippy or shippy-gui can't print a label, volunteers open the shipment on
easypost.com and download the label image. The watcher sees the new file in
**Downloads**. If it looks like a 4x6 label, the watcher prints it on the first
USB label printer that is plugged in and moves the file to `Downloads\printed\`.
If printing fails, it moves the file to `Downloads\failed\` and shows a warning
box. Every step goes to the log, so we can work out later why the printers fail.

## What it does, step by step

1. It watches the real Downloads folder. On Windows it asks the shell for the
   folder location, so a redirected Downloads folder (OneDrive, a D: drive)
   still works. It doesn't look inside subfolders.
2. It ignores files that are still downloading: `.crdownload` (Chrome/Edge),
   `.part` (Firefox), `.tmp`, and hidden files. It handles the final file when
   the browser renames it.
3. It waits until the file has stopped growing for about 1 second and can be
   opened. It gives up after 60 seconds.
4. It checks the file name against `globs`: PNG, JPG, GIF, BMP or PDF. It skips
   `.zpl` and `.epl` files with a warning, because only images and PDFs can be
   printed. Download the PNG or PDF label instead.
5. It decodes the file. For a PDF it renders the first page at 300 DPI and logs
   the page count.
6. It checks the shape. The long side divided by the short side must be between
   1.4 and 1.6 (a 4x6 label is exactly 1.5), and the short side must be at least
   400 px. Anything else, such as a photo or a letter-size PDF, stays where it
   is, untouched.
7. It skips a file whose content (SHA-256) matches a label printed in the last
   60 seconds. This handles accidental double downloads like `label (1).png`.
8. It prints with `print_to_first_available`, then follows the print job in the
   spooler for up to 60 seconds.
9. It moves the file to `printed\` or `failed\`, adding a
   `YYYYmmdd-HHMMSS_` prefix to the name. If the file is locked (by antivirus or
   an image viewer), it retries a few times. If the move still fails, it leaves
   the file where it is and remembers it, so the file isn't printed twice.
10. If the print failed, a warning box (always on top) tells the volunteer that
    the label did **not** print and where the file went. Only one box is shown
    at a time.

To reprint a failed label, fix the printer, then drag the file from
`Downloads\failed\` back into `Downloads` (or download it again).

Files that are already in Downloads when the watcher starts are ignored unless
you pass `--process-existing`. Only one watcher can run per user at a time; a
second copy exits with code 1. When the second copy has no console, it shows a
message box.

## Install (Windows shipping PC)

1. Install [uv](https://docs.astral.sh/uv/) for the volunteer account:

   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```

2. Install the watcher as a uv tool, straight from GitHub:

   ```powershell
   uv tool install "ibp-printing[watcher] @ git+https://github.com/jonkensta/ibp-printing"
   uv tool update-shell
   ```

   This installs two commands into `%USERPROFILE%\.local\bin\`:

   - `ibp-label-watcher.exe`: console version, for testing. Logs also print in
     the window.
   - `ibp-label-watcherw.exe`: windowless version, for normal use. It writes
     only to the log files and shows message boxes.

   To upgrade later, run `uv tool upgrade ibp-printing`. If that doesn't pick
   up new commits, run
   `uv tool install --force "ibp-printing[watcher] @ git+https://github.com/jonkensta/ibp-printing"`.

3. Enable the PrintService Operational event log. This is a one-time step and
   must be run as administrator. Windows turns this log off by default, and
   it's the best record of what the spooler did with each job. The watcher
   copies the last hour of it into its own log at startup.

   ```powershell
   wevtutil sl Microsoft-Windows-PrintService/Operational /e:true
   ```

4. Test it in the console, without printing anything:

   ```powershell
   ibp-label-watcher --dry-run
   ```

   Download a label, or copy one into Downloads. You should see
   `label decision: LABEL` and `DRY RUN: would print`. Press Ctrl+C to stop.
   Then run it without `--dry-run` and print a real label.

## Start automatically at logon (Task Scheduler)

The watcher has to run **in the volunteer's own logon session**. Otherwise it
can't show message boxes, and it would watch the wrong Downloads folder. Create
the task as that user. Creating a logon task usually needs an elevated
(administrator) prompt.

```bat
schtasks /Create /TN "IBP Label Watcher" /SC ONLOGON /RU "%USERDOMAIN%\%USERNAME%" /IT /RL LIMITED /F ^
  /TR "\"%USERPROFILE%\.local\bin\ibp-label-watcherw.exe\""
```

- `/SC ONLOGON` starts the task when the user logs on.
- `/IT` makes it run only interactively, in the user's desktop session.
- `/RL LIMITED` runs it without administrator rights.

If you run this from an elevated prompt as a *different* (admin) user,
`%USERNAME%` and `%USERPROFILE%` will be that admin's. Type the volunteer
account and path explicitly instead.

By default, Task Scheduler stops a task after **3 days**. After creating the
task, open Task Scheduler, go to *IBP Label Watcher*, then *Properties* >
*Settings*, and untick **"Stop the task if it runs longer than"**. Or create the
task with PowerShell instead, which sets this for you:

```powershell
$action   = New-ScheduledTaskAction -Execute "$env:USERPROFILE\.local\bin\ibp-label-watcherw.exe"
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
              -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "IBP Label Watcher" -Action $action -Trigger $trigger `
  -Settings $settings -RunLevel Limited -Force
```

Useful commands:

```bat
schtasks /Run /TN "IBP Label Watcher"         :: start it now
schtasks /End /TN "IBP Label Watcher"         :: stop it
schtasks /Query /TN "IBP Label Watcher" /V /FO LIST
schtasks /Delete /TN "IBP Label Watcher" /F
```

If the watcher is already running when the task fires, the second copy logs
"another watcher already holds the lock" and exits with code 1. That's harmless.

You can also run it as `pythonw -m ibp_printing.watcher` from any environment
where `ibp-printing[watcher]` is installed.

## Command-line options

| Option | Meaning |
| --- | --- |
| `--config PATH` | Use this config file instead of the default location. |
| `--watch-dir PATH` | Watch this folder instead of Downloads. |
| `--log-dir PATH` | Write logs here instead of the default log folder. |
| `--process-existing` | Also handle matching files already in the folder at startup. |
| `--dry-run` | Do everything except print: decide, log `DRY RUN: would print`, move nothing. |
| `--once` | Process the files already in the folder, then exit (no watching). Combine with `--dry-run` to test detection on a folder of samples. |
| `-v`, `--verbose` | Show DEBUG lines on the console. The log files always get DEBUG. |

## Config file

The config file is optional. Every key has a default. It lives at:

- Windows: `%LOCALAPPDATA%\ibp-printing\watcher.toml`
- Linux/macOS: `$XDG_CONFIG_HOME/ibp-printing/watcher.toml`
  (default `~/.config/ibp-printing/watcher.toml`)

Unknown keys and values of the wrong type are logged as warnings and ignored,
so a typo never stops the watcher from starting. Command-line options override
the file. The full effective config is logged at every startup.

```toml
# Folder to watch. Default: the real Downloads folder.
# watch_dir = 'C:\Users\Shipping\Downloads'

# Log folder. Default: %LOCALAPPDATA%\ibp-printing\logs
# log_dir = 'D:\ibp-logs'

# File names that might be labels (case-insensitive).
globs = ["*.png", "*.jpg", "*.jpeg", "*.gif", "*.bmp", "*.pdf"]

# Shape check: long side / short side, and the minimum short side.
aspect_min = 1.4
aspect_max = 1.6
min_short_side_px = 400

# Identical content printed this many seconds ago is not printed again.
dedupe_seconds = 60

# How long to follow a spooled job before calling it a timeout.
track_timeout_s = 60

# Pop up a warning box when a label fails to print.
notify_on_failure = true

# How often to log a "still alive" line plus a full printer snapshot.
heartbeat_minutes = 15

# A download is finished when its size hasn't changed for stable_seconds;
# give up after stable_timeout_s.
stable_seconds = 1.0
stable_timeout_s = 60

# Resolution used to render PDF labels.
pdf_dpi = 300

# Same as --process-existing / --dry-run.
process_existing = false
dry_run = false
```

Use single quotes for Windows paths in TOML. Inside double quotes, a backslash
starts an escape sequence.

## Logs

Logs are written to `%LOCALAPPDATA%\ibp-printing\logs\` (Linux:
`~/.local/state/ibp-printing/logs/`). This is the same folder shippy and
shippy-gui use through `ibp-printing`, so their print attempts appear alongside
the watcher's.

- `printer.log` is the human-readable log. Each line looks like
  `time level [attempt-id] thread logger: message | key=value ...`.
- `printer.jsonl` has the same records, one JSON object per line.

Both files rotate at 5 MB and keep 20 old copies. The single-instance lock file
is `%LOCALAPPDATA%\ibp-printing\watcher.lock`, in the parent folder of the logs.

What gets logged:

- **Startup**: version, command line, config file path, warnings about the
  config, the effective config, and the printer backend. It also logs a full
  printer discovery: every print queue, its status and attribute flags, its USB
  VID:PID match, and the reasons it is or isn't usable. Finally it copies in
  the last 60 minutes of PrintService events.
- **USB device monitor**: every USB arrival, removal and status change, while
  the watcher runs (Windows).
- **Every filesystem event** at DEBUG, and every decision with its reason:
  temporary file ignored, not matching globs, the stability check (size, wait
  time), the label decision (size, aspect ratio, reasons), duplicate,
  unsupported format.
- **Every label**: the work for one label runs inside an *attempt* with a
  10-character ID, shown as `[0ac22caaac]` in `printer.log` and as
  `attempt_id` in the JSON. The attempt covers the file's SHA-256, the image
  details, each printer tried, the spooled job ID, every job status change,
  the final job outcome (`completed`, `error`, `timeout`, ...), where the file
  was moved, and whether a message box was shown. The job name in the Windows
  print queue ends with the same ID, for example `EasyPost label.png [0ac22caaac]`.
- **Heartbeat** every 15 minutes: whether the folder observer and worker are
  alive, how many files are queued, counts per outcome, and a fresh printer
  snapshot. If the observer has died, the log shows
  `folder observer has died` at CRITICAL level.
- **Crashes**: uncaught exceptions in any thread are logged with their full
  traceback.

### Reading the logs

Follow the log live (PowerShell):

```powershell
Get-Content "$env:LOCALAPPDATA\ibp-printing\logs\printer.log" -Wait -Tail 50
```

Find failed labels, and then everything about one of them:

```powershell
cd "$env:LOCALAPPDATA\ibp-printing\logs"
Select-String -Path printer.log* -Pattern "file outcome: failed"
Select-String -Path printer.log* -Pattern "\[0ac22caaac\]"
```

Query the JSON log:

```powershell
Get-Content printer.jsonl | ConvertFrom-Json |
  Where-Object { $_.msg -like "job outcome*" } |
  Select-Object ts, msg, @{n="printer"; e={$_.data.printer}}
```

On Linux/macOS, `jq` works well:

```sh
jq -c 'select(.attempt_id == "0ac22caaac")' printer.jsonl
jq -r 'select(.msg | startswith("file outcome")) | [.ts, .data.status, .data.file] | @tsv' printer.jsonl
```

Outcomes you'll see in `file outcome: ...` lines:

| Status | Meaning | File moved? |
| --- | --- | --- |
| `printed` | Spooled and the job finished (or left the queue) cleanly. | `printed\` |
| `failed` | No usable printer, the spool failed, or the job errored, was deleted, or timed out. | `failed\` |
| `dry_run` | Would have printed (`--dry-run`). | no |
| `duplicate` | Same content was printed within `dedupe_seconds`. | no |
| `not_label` | Wrong shape or too small. | no |
| `not_matching` | File name doesn't match `globs`. | no |
| `unsupported` | ZPL/EPL, a corrupt image, or a PDF that couldn't be rendered. | no |
| `unstable` | Still changing, empty, or locked after `stable_timeout_s`. | no |

## Developing on Linux

Everything except actual Windows printing runs on Linux, which is useful with
`--dry-run`:

```sh
uv sync --extra watcher
mkdir -p /tmp/dl
uv run ibp-label-watcher --watch-dir /tmp/dl --log-dir /tmp/dl-logs --dry-run -v
# in another shell:
python -c "from PIL import Image; Image.new('L', (1200, 1800), 255).save('/tmp/dl/label.png')"
```

Run the tests with `uv run python -m unittest discover -s tests`.
