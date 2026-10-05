# ibp-printing

Shared USB label-printer discovery, printing, and diagnostics for the
[Inside Books Project](https://insidebooksproject.org/) shipping tools,
[shippy](https://github.com/jonkensta/shippy) (CLI) and
[shippy-gui](https://github.com/jonkensta/shippy-gui) (PySide6).

Both apps buy EasyPost postage and print a 4x6 label on a USB label printer
(DYMO, Zebra, PM2411BT, ...) attached to a Windows PC. This library is the one
place that:

- **prints directly over USB** to supported printers (the PM2411BT), with no
  driver, no print queue and no naming rule, and reads the printer's own
  "printing / done" reports (see [Direct USB printing](#direct-usb-printing-pm2411bt));
- **discovers** other label printers by matching each print queue to a
  plugged-in USB device by VID:PID;
- **prints** a PIL image through the Windows spooler and GDI (CUPS on Linux),
  falling back to the next printer only if one *definitely* never received the
  job;
- **tracks** the spooled job until it prints, errors, is deleted, or times out;
- **logs everything** (queues, USB devices, every GDI step, job status changes,
  PrintService events) to a human-readable log and a JSON-lines log, so a
  failed label can be diagnosed after the fact;
- **remembers recent labels** in a shared per-user journal, so an app can warn
  a volunteer before buying postage twice for the same recipient (see
  [Duplicate-purchase protection](#duplicate-purchase-protection)).

## Install

```sh
uv add "ibp-printing @ git+https://github.com/jonkensta/ibp-printing@v0.1.0"
```

On Windows this pulls in `pywin32` and `WMI` automatically. Optional extras:

| Extra     | Adds                       | For                                      |
|-----------|----------------------------|------------------------------------------|
| `watcher` | `watchdog`, `pypdfium2`    | the [label watcher](#label-watcher)      |
| `linux`   | `pycups`                   | richer CUPS queue info on Linux          |

```sh
uv add "ibp-printing[watcher] @ git+https://github.com/jonkensta/ibp-printing@v0.1.0"
```

## Direct USB printing (PM2411BT)

A supported printer is found and printed to **directly over USB**: there is
nothing to install on Windows, no print queue to create or rename, and no
vendor driver. Plug it in and it shows up as e.g.
`PM2411BT (USB direct, serial Q529E56G9290059)`, ahead of every print queue.

- **Which printers:** the PM2411BT (4x6 thermal), recognised by the model in
  its IEEE 1284 device ID (`MDL:PM2411BT`), never by USB VID:PID alone (its
  VID belongs to a chip maker and is shared by many devices).
- **Windows:** nothing to do. Windows binds its built-in *USB Printing
  Support* driver (`usbprint.sys`) to any USB printer, and the library talks
  to that. If a Windows print queue for the same printer also exists (named
  with the `VID:PID` rule below), it is kept as the fallback, listed after the
  direct entry, and used only when the direct printer could not be opened
  (busy, permission, unplugged) or failed with an I/O error before sending -
  never when its cover is open or it is not answering (see below).
- **Linux:** the printer appears as `/dev/usb/lpN`, which normal users cannot
  open. Install the udev rule once:
  ```sh
  sudo install -m 0644 packaging/linux/60-ibp-label-printer.rules /etc/udev/rules.d/
  sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=usbmisc
  ```
  (or replug the printer). Without it, printing fails with a "permission
  denied ... install the udev rule" error and falls back to the next printer.
- **Busy printer:** the library opens the printer exclusively for the whole
  job, so nothing else (the Windows spooler, another app, a second shippy
  window) can mix its data into the label. If something else has it open, the
  print is "definitely not sent" and falls back to the next printer (or, in the
  label watcher, waits in `to-print\`).
- **Kill switch:** set the environment variable `IBP_PRINTING_DIRECT=0` (or
  call `ibp_printing.set_direct_enabled(False)`, or run
  `ibp-print-diag --no-direct`) to turn direct printing off; the library then
  behaves exactly as before (queues only). Every app's log records which mode
  is active when logging is configured.

**How fallback works.** `print_to_first_available` tries the direct printer
first. If the job *definitely* never reached it, that is a `PrintError` and
the next printer is tried:

- printer busy, no permission, unplugged, or an I/O error before sending:
  the next printer, possibly the same printer's Windows queue;
- cover open or the printer still realigning after the cover was closed
  (`reason` `cover_open` / `realign_busy`), or the printer not answering /
  not taking the job (`no_cover_reply` / `job_not_accepted`; it may be
  holding a half job that would swallow the next one): **not** the same
  printer's Windows queue (same VID:PID). Other printers are still tried. If
  none is left, the `PrintError` has that `.reason` and a message for the
  volunteer ("Close the printer cover: ..." / "Turn the label printer off
  and on again: ..."), so shippy and the label watcher keep the label in
  `to-print\`, and the watcher prints it once the printer is ready.

Once any part of the job has been sent (or may have been: e.g. a Windows
write whose cancellation was never confirmed), there is no fallback: the
result is returned as it is, even when it is not ok.

**What the volunteer sees.** The direct path asks the printer about its cover
before sending, and the printer reports when it starts and finishes printing:

| Situation | Result | What to do |
|---|---|---|
| Label printed | `COMPLETED` (ok) | - |
| Cover open, or closed moments ago and the printer is still realigning | `PrintError` "Close the printer cover: ..." (`reason` `cover_open` / `realign_busy`; nothing sent; never sent to the same printer's queue; the watcher keeps the label in `to-print\` and prints it once the cover is closed) | Close the cover. |
| Printer does not answer or does not take the job | `PrintError` "Turn the label printer off and on again: ..." (`no_cover_reply` / `job_not_accepted`; not sent to the same printer's queue) | Power-cycle the printer. |
| Cover opened or closed while the label was printing | `UNCERTAIN` (the printer's realign after a cover close reports the same "printing"/"done" as a label, so success cannot be proven) | Check whether the label came out. |
| Paper out (roll empty or not loaded) | `UNCERTAIN`, history: "printer stopped accepting data (paper out? jam?) - a partial job is in the printer: power-cycle it before reloading paper" | **Turn the printer off and on before** loading paper, otherwise it may print the leftover half job or swallow the next one. Then check whether a label came out before reprinting. |
| Printer started but did not finish within the wait | `TIMEOUT` | Check for a jam; the label may still come out. |
| Printer never started (no "printing" report within 10 s) | `UNCERTAIN` | Check the printer; the label may still come out. |
| Printer rejected part of the job | `ERROR` | The label may be wrong or missing; check it. |
| Printer busy / unplugged / no permission | `PrintError` (nothing sent) | Fallback as above, including the same printer's queue. |

The printer's paper sensor is not trusted (it reports paper even with the roll
removed), so paper out is only noticed when the printer stops taking data.

**Image placement.** Labels are rasterized to the printer's 812x1218 dots
(4x6 in at 203 dpi). A label that is already 812x1218 (EasyPost at 203 dpi) is
printed dot for dot; anything else (EasyPost's 1200x1800 at 300 dpi, PDFs) is
scaled to fill the label with nearest-neighbour resampling, which keeps
barcode bars on whole dots. Landscape images are rotated, never cropped.

Details, hardware findings and the protocol: [docs/printers/pm2411bt.md](docs/printers/pm2411bt.md).

## Printer naming rule

This rule applies to printers driven through a print queue (everything except
the direct USB printers above). A print queue is treated as a label printer **only if its name ends in the
printer's USB `VID:PID`**, separated from the rest of the name by a space, tab,
`_` or `-`. For example:

```
DYMO LabelWriter 4XL 0922:0028
Zebra ZP450-0A5F:00D1
```

Hex digits may be upper or lower case. The queue is **usable** when a USB
device with that VID:PID is currently plugged in. Windows remembers devices
that were plugged in before; those ghost entries (WMI `Win32_PnPEntity.Present`
false, or `ConfigManagerErrorCode` 45, "not connected") do **not** count as
plugged in. `ibp-print-diag` still lists them, marked `NOT CONNECTED`, so you
can see why a queue is not usable. If a connected device reports an error or
the queue is paused or offline, the printer stays usable but is ranked lower:
printers whose USB device is healthy come first, then queues without a problem
flag, then the Windows default, then alphabetical order.

Only **local** print queues are considered (`EnumPrinters` with
`PRINTER_ENUM_LOCAL`). USB label printers are always local queues; network
printer *connections* are skipped because enumerating them can stall for a long
time when a print server is unreachable.

### Setting it up on Windows

1. **Find the VID:PID.** Open *Device Manager*, then expand *Universal Serial
   Bus controllers* or *Printers*/*Print queues* and find the printer (for
   example *USB Printing Support*). Open *Properties*, then *Details*, and pick
   *Hardware Ids*. The first line looks like `USB\VID_0922&PID_0028&REV_0100`,
   which means the VID:PID is `0922:0028`.
   If you're not sure which device it is, unplug the printer and see which
   entry disappears. `ibp-print-diag` also lists every USB device with its
   VID:PID.
2. **Rename the queue.** Open *Settings*, then *Bluetooth & devices*, then
   *Printers & scanners*. Pick the printer, then *Printer properties*, then
   the *General* tab, and append ` 0922:0028` to the name.
3. Run `ibp-print-diag` and check that the printer shows `=> usable: YES`.

## Public API

Everything is importable from `ibp_printing`:

```python
from PIL import Image
import ibp_printing

ibp_printing.configure_logging(app="shippy")   # once, at startup (see Logs below)

label = Image.open("label.png")

# Print to the best usable label printer, falling back to the next one.
# Raises ibp_printing.PrintError (a RuntimeError) with the message
# "No label printer found plugged in." if there is none.
result = ibp_printing.print_to_first_available(label, job_name="Order 1234")

# Or print to a specific queue, following the job for up to 60 s.
result = ibp_printing.print_image(label, "DYMO LabelWriter 4XL 0922:0028",
                                  track_timeout_s=60)
print(result.printer_name, result.outcome, result.history)
if not result.outcome.ok:
    ...  # The label may not have printed, and may still print later:
         # tell the user to check the printer. Don't refund or resend.

# Discovery
for candidate in ibp_printing.find_label_printers():   # usable, best first
    print(candidate.name, candidate.transport, candidate.vid_pid, candidate.reasons())

discovery = ibp_printing.discover()     # every queue, every USB device, errors
ibp_printing.get_default_printer()      # OS default queue name, or None
```

| Name | What it is |
|------|------------|
| `print_to_first_available(img, *, job_name=None, track_timeout_s=0.0)` | Print to the best usable printer, trying the next one only on `PrintError` (the job definitely never reached that printer's spooler), so a label is never sent to two printers. |
| `print_image(img, printer_name, *, job_name=None, track_timeout_s=0.0)` | Print to one named queue, or to a direct USB printer by its candidate name (`"PM2411BT (USB direct, serial ...)"`). |
| `discover()` | Returns a `Discovery` with `queues`, `usb_devices`, `candidates`, `errors`, `direct_devices`, `direct_enabled`, and the ranked `.usable` list. |
| `direct_enabled()` / `set_direct_enabled(enabled)` | Whether direct USB printing is on; force it on/off (`None` = back to `IBP_PRINTING_DIRECT` / default on). |
| `find_label_printers()` | `discover().usable`. |
| `get_default_printer()` | The OS default printer name. |
| `configure_logging(log_dir=None, *, app="ibp-printing", console=True, level=DEBUG)` | Attach the file handlers for `printer-<app>.log` / `printer-<app>.jsonl`. It's safe to call more than once and returns the log directory. |
| `default_log_dir()` | Where logs go by default. |
| `PrintError` | Raised **only** when the job definitely never reached the spooler (no printer, CreateDC/CreatePrinterDC/StartDoc failed, or a StartPage/draw failure where AbortDoc returned and the queue holds no job with our attempt ID; an `EndPage`/`EndDoc` failure is never a `PrintError`) or the direct USB printer (could not be opened, cover open, no answer, nothing accepted; then it is a `DirectPrintError` with a `.reason` such as `busy`, `permission`, `cover_open`, `realign_busy`). Every `PrintError` has `.reason` (None when there is no code); `print_to_first_available` keeps the direct printer's reason when it skipped that printer's own queue. Safe to retry. Subclass of `RuntimeError`. |
| `PrintResult` | `printer_name`, `job_name`, `job_id` (None for direct USB), `outcome`, `history`, `elapsed_s`. |
| `JobOutcome` | See [Outcomes](#outcomes). `.ok` is true when a label most likely came out. |
| `PrinterCandidate`, `PrintQueue`, `UsbDevice` | Discovery records. `candidate.reasons()` explains each check. `candidate.transport` is `"direct"` (USB, `candidate.direct_device` set) or `"queue"`. |
| `save_for_retry(img, name, *, watch_dir=None, meta=None)` | Save a label that could not be printed into `<Downloads>/to-print/` for the label watcher; returns its path. With `meta`, also writes the sidecar `<png>.json` and marks the label `queued` in the journal (see below). |
| `downloads_dir()` / `to_print_dir(watch_dir=None)` | The (redirection-aware) Downloads folder, and its `to-print` subfolder. |
| `recipient_key`, `record_purchase`, `update_status`, `find_duplicates`, `pending_labels`, `LabelRecord`, `LabelStatus` | The label journal; see [Duplicate-purchase protection](#duplicate-purchase-protection). |
| `get_backend()` / `set_backend(backend)` | Access or replace the backend (`PrinterBackend`), for example with a fake in tests. The default is a `DirectFirstBackend` (direct USB) around the platform backend; a backend passed to `set_backend` replaces both. |

With `track_timeout_s > 0` the Windows backend polls the spooler for the job
(found by the unique `[attempt-id]` at the end of its document name) and
records each status change in `result.history`. A successful `EndDoc` only
means the spooler accepted the job. Tracking shows whether it actually printed.

A **direct USB** job is always followed, whatever `track_timeout_s` is: the
printer itself reports when it starts and finishes, so the outcome is never
`NOT_TRACKED` and a call takes a few seconds longer than an untracked spooler
print. `track_timeout_s` only lengthens the wait for "finished" beyond its
30 s default (`max(track_timeout_s, 30)`); the printer must still start
within 10 s.

### Outcomes

| `JobOutcome` | `.ok` | Meaning |
|---|---|---|
| `COMPLETED` | yes | The spooler reported the job printed, or it left the queue normally (direct USB: the printer reported it finished). |
| `VANISHED_UNSEEN` | yes | The job was never seen in the queue (usually printed before the first poll). The queue contents are logged. |
| `NOT_TRACKED` | yes | Spooled; tracking was not requested (`track_timeout_s=0`). |
| `ERROR` | no | The job sat in an error state (paper out, offline, ...) for 10 s. It may still print once fixed. (Direct USB: the printer rejected part of the job.) |
| `DELETED` | no | The job was deleted from the queue. |
| `TIMEOUT` | no | Still queued when tracking gave up (direct USB: started but did not finish in time). It may still print. |
| `TRACKING_FAILED` | no | Spooled, but following the job raised. History keeps what was seen. |
| `UNCERTAIN` | no | A GDI call failed after `StartDoc` and the library could not rule out that part or all of the job reached the printer (`EndPage` or `EndDoc` failed — with print-while-spooling the page may already be on its way even if the queue is empty — `AbortDoc` raised, the job is still in the queue, or the queue could not be checked). Direct USB: the printer stopped taking the job part-way (paper out: power-cycle it before reloading paper), the connection broke mid-job, or it never reported starting. `history` says which. |

Every non-ok outcome means "the label may or may not come out": tell the user
to check the printer before reprinting. Never refund or resend automatically;
`print_to_first_available` never sends such a job to a second printer.

## Duplicate-purchase protection

When a label can't print, shippy and shippy-gui save it into
`Downloads\to-print\` and the [label watcher](#label-watcher) prints it later.
Volunteers then often enter the same shipment again and buy postage a second
time. `ibp_printing.labels` (everything is also importable from
`ibp_printing`) keeps a shared, per-user record of recent labels so the apps
can warn first:

```python
import ibp_printing
from ibp_printing import LabelStatus

key = ibp_printing.recipient_key(name, street, city, state, zip_code)

# Before buying: anything recent (or still waiting) for this recipient?
for dup in ibp_printing.find_duplicates(key, within_hours=12):   # newest first
    print(dup.recipient_label, dup.tracking_code, dup.status, dup.file)

# Right after buying:
ibp_printing.record_purchase(recipient_key=key, recipient_label="Jane Doe, Huntsville TX",
                             tracking_code=shipment.tracking_code,
                             shipment_id=shipment.id, app="shippy")

# After trying to print:
ibp_printing.update_status(shipment.tracking_code, LabelStatus.PRINTED)
# ...or, if no printer took it, save it for the watcher with its metadata:
ibp_printing.save_for_retry(label, shipment.tracking_code, meta={
    "tracking_code": shipment.tracking_code, "shipment_id": shipment.id,
    "recipient_label": "Jane Doe, Huntsville TX", "recipient_key": key,
    "app": "shippy"})
# ...or, if it may have printed:  update_status(code, LabelStatus.CHECK_PRINTER)
# ...or, after a refund:          update_status(code, LabelStatus.REFUNDED)

# A UI listing what is waiting for the printer (or for a human):
for waiting in ibp_printing.pending_labels():   # oldest first
    print(waiting.recipient_label, waiting.status, waiting.file)
```

| Name | What it is |
|---|---|
| `recipient_key(name, street, city, state, zip_code)` | A normalized text key for a recipient (`"jane doe\|1 main st apt 5\|huntsville\|tx\|77340"`): case, accents, punctuation and spacing are ignored; `Street`/`st`, `Avenue`/`ave`, `Apartment`/`Unit`/`Suite`/`#`/`apt`, `P.O. Box`/`po box`, `North`/`n`, `Fort`/`ft`, `Saint`/`st` and state names/codes are folded together; only the first 5 ZIP digits count. The same in every process. |
| `LabelRecord` | `tracking_code`, `shipment_id`, `recipient_key`, `recipient_label` (e.g. `"Jane Doe, Huntsville TX"`), `app`, `status`, `created`, `updated` (epoch seconds), `file` (the queued / check-printer PNG, or None). |
| `LabelStatus` | A `StrEnum` (members are plain strings; also module constants `labels.PURCHASED` etc.): `purchased`, `printed`, `queued` (waiting in `to-print\`), `check_printer` (may have printed; needs a human), `refunded`. |
| `record_purchase(*, recipient_key, recipient_label, tracking_code, shipment_id, app)` | Record a label just bought (status `purchased`); returns the `LabelRecord`. |
| `update_status(tracking_code, status, *, file=None)` | Record a new status (and file; `None` keeps the recorded one). An unknown label gets a minimal record; an unchanged status and file writes nothing. |
| `find_duplicates(recipient_key, *, within_hours=12)` | `queued` / `check_printer` labels of **any age** whose file still exists (or that have no file), including PNGs in `to-print\` the journal does not know (from their sidecar), plus `purchased` / `printed` labels updated within the window. Never `refunded`. Newest first. |
| `pending_labels()` | `queued` and `check_printer` labels whose file still exists, plus **every** PNG in `Downloads\to-print\` the journal does not point at (described by its journal record via the sidecar's tracking code, else by the sidecar, else a minimal record whose `tracking_code` is guessed from the file name and whose `recipient_label` is the file name). Oldest first. |
| `labels.update_status_from_meta(meta, status, *, file=None)` | `update_status` for a label described by sidecar metadata; creates the record from `meta` when the journal has none (used by `save_for_retry` and the watcher). |
| `labels.journal_path()` / `labels.set_journal_path(path)` | Where the journal is; override it (tests, tools). |

**Sidecars.** `save_for_retry(..., meta={...})` writes `<label>.png.json` (keys
`tracking_code`, `shipment_id`, `recipient_label`, `recipient_key`, `app`,
`created`) atomically **before** the PNG appears, so the watcher always sees a
label's metadata with it. The watcher never prints a `*.json`, moves the sidecar
along with its label (`printed\`, `check-printer\`, `duplicate_...` names),
updates the journal (`printed` / `check_printer` / `queued`), and tells the
volunteer in one information box per pass: "Label for Jane Doe, Huntsville TX
(tracking 9400...) printed."

**Storage.** One journal per user: `%LOCALAPPDATA%\ibp-printing\labels.jsonl`
(Linux: `$XDG_STATE_HOME/ibp-printing/labels.jsonl`, default
`~/.local/state/...`), next to the watcher's state file. It is append-only JSON
lines; the last line for a tracking code wins. Every read and write takes an
OS file lock (`labels.jsonl.lock`, `msvcrt.locking` / `fcntl.flock`, 5 s
timeout), so the apps and the watcher can use it at the same time. A corrupt
or half-written line is logged and skipped. About once a day a write compacts
the file to one line per label and drops labels not updated for 30 days (a
`queued` / `check_printer` label whose file still exists is kept).

**It never blocks printing.** None of these functions raises for a journal
problem (lock timeout, disk full, corrupt file, unknown status): it is logged
at ERROR, `find_duplicates` returns `[]` (with a WARNING saying the check is
unavailable), `record_purchase` still returns the record, and
`save_for_retry` still saves the label.

## Diagnostics CLI

```sh
ibp-print-diag                    # full report: queues, gates, USB devices, verdict, events
ibp-print-diag --json             # the same, machine-readable
ibp-print-diag --events 240       # include PrintService events from the last 4 h (default 60)
ibp-print-diag --test-print       # print a 4x6 test label to the best usable printer
ibp-print-diag --test-print "DYMO LabelWriter 4XL 0922:0028"
ibp-print-diag --test-print "PM2411BT (USB direct, serial Q529E56G9290059)"
ibp-print-diag --direct-status    # only the direct USB printers: cover/paper probe
ibp-print-diag --no-direct        # this run without direct USB (like IBP_PRINTING_DIRECT=0)
ibp-print-diag --log-dir C:\temp\logs -v   # custom log dir, echo log to the console
```

The report starts with the direct USB printers: whether direct printing is on,
and for every USB printer-class device its path, serial, 1284 ID, whether it
is a supported model, present, accessible and not busy, and for a supported
model a **status probe** (cover open/closed, paper sensor) - the probe only
sends the two status queries, so it never feeds or prints a label. Unsupported
devices are listed but never probed.

For every queue the report shows its name, port, driver, decoded status and
attribute bits, each detection gate (name ends in VID:PID / USB device
present) with its result, and the matching USB devices with their status and
error code. It then lists all USB devices that have a VID:PID, the verdict
(usable printers in the order they will be tried), recent PrintService events,
and the log directory. The report is also written to the log. The command exits
with status 1 if no printer is usable or the test print failed.

The test label is 1200x1800 px (4x6 in at 300 DPI) with a border, corner
labels, and a half-inch ruler grid, so you can see scaling, cropping, or
rotation on the printed label.

From Python, `ibp_printing.diagnostics.build_report(discovery, events)` returns
the same report as a string.

> The PrintService *Operational* event channel is off by default. Turn it on
> once (as administrator) for much more detail:
> `wevtutil sl Microsoft-Windows-PrintService/Operational /e:true`

## Logs

Logs are written to `%LOCALAPPDATA%\ibp-printing\logs` on Windows
(`$XDG_STATE_HOME/ibp-printing/logs`, or `~/.local/state/ibp-printing/logs`
elsewhere). Each application writes its **own** pair of files, named by the
`app` passed to `configure_logging`:

| App | Files |
|---|---|
| shippy | `printer-shippy.log`, `printer-shippy.jsonl` |
| shippy-gui | `printer-shippy-gui.log`, `printer-shippy-gui.jsonl` |
| label watcher | `printer-watcher.log`, `printer-watcher.jsonl` |
| `ibp-print-diag` | `printer-diag.log`, `printer-diag.jsonl` |
| anything else (default) | `printer-ibp-printing.log`, `printer-ibp-printing.jsonl` |

Separate files matter on Windows: a process cannot rotate a log file another
process holds open, so sharing one rotating file between the watcher and an
app would stop rotation and lose records. Two copies of the *same* app (two
shippy windows, say) can still share a file; the handler
(`SafeRotatingFileHandler`) then keeps appending to the current file, writes a
"log rotation failed" WARNING into it, and retries rotation after 5 minutes. A
failed rotation never drops a record or disturbs the existing backups.

- `printer-<app>.log`: human-readable lines:
  `time LEVEL [attempt-id] thread logger: message | key=value ...`
- `printer-<app>.jsonl`: one JSON object per line with `ts`, `level`, `logger`,
  `host`, `pid`, `thread`, `attempt_id`, `msg`, an optional `data` object, and
  `exc` for exceptions.

Both rotate at 5 MB and keep 20 backups. Every print call gets an **attempt ID**
that tags all of its log records and is added to the spooler document name
(`Shipping Label [3f9c0a1b2e]`). Nested attempts reuse the outer ID, so when the
label watcher prints a file, the file's whole story and the spooler job share
one ID. That lets you follow one label from discovery through each GDI call to
the spooler's final job status. Records also propagate to the root logger, so
the host app's own log receives them too.

## Label watcher

See [docs/watcher.md](docs/watcher.md).

## Development

```sh
uv sync --extra watcher --dev
uv run black --check src tests
uv run pylint src
uv run mypy src
uv run python -m unittest discover -s tests
```

The tests use fake `win32print` / `win32ui` / `wmi` / `pythoncom` modules and a
scriptable fake printer (`ibp_printing.direct.FakeTransport`), so the whole
suite, including the Windows backend and the direct USB path, runs on Linux
without hardware. CI runs it on Ubuntu and Windows. The barcode tests decode
rasterized labels with zbar (`pyzbar`); they are skipped when the zbar library
is missing (`sudo apt install libzbar0` on Debian/Ubuntu).
