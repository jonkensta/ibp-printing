# ibp-printing

Shared USB label-printer discovery, printing, and diagnostics for the
[Inside Books Project](https://insidebooksproject.org/) shipping tools,
[shippy](https://github.com/jonkensta/shippy) (CLI) and
[shippy-gui](https://github.com/jonkensta/shippy-gui) (PySide6).

Both apps buy EasyPost postage and print a 4x6 label on a USB label printer
(DYMO, Zebra, ...) attached to a Windows PC. This library is the one place that:

- **discovers** label printers by matching each print queue to a plugged-in USB
  device by VID:PID;
- **prints** a PIL image through the Windows spooler and GDI (CUPS on Linux),
  falling back to the next printer only if one *definitely* never received the
  job;
- **tracks** the spooled job until it prints, errors, is deleted, or times out;
- **logs everything** (queues, USB devices, every GDI step, job status changes,
  PrintService events) to a human-readable log and a JSON-lines log, so a
  failed label can be diagnosed after the fact.

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

## Printer naming rule

A print queue is treated as a label printer **only if its name ends in the
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
    print(candidate.name, candidate.vid_pid, candidate.reasons())

discovery = ibp_printing.discover()     # every queue, every USB device, errors
ibp_printing.get_default_printer()      # OS default queue name, or None
```

| Name | What it is |
|------|------------|
| `print_to_first_available(img, *, job_name=None, track_timeout_s=0.0)` | Print to the best usable printer, trying the next one only on `PrintError` (the job definitely never reached that printer's spooler), so a label is never sent to two printers. |
| `print_image(img, printer_name, *, job_name=None, track_timeout_s=0.0)` | Print to one named queue. |
| `discover()` | Returns a `Discovery` with `queues`, `usb_devices`, `candidates`, `errors`, and the ranked `.usable` list. |
| `find_label_printers()` | `discover().usable`. |
| `get_default_printer()` | The OS default printer name. |
| `configure_logging(log_dir=None, *, app="ibp-printing", console=True, level=DEBUG)` | Attach the file handlers for `printer-<app>.log` / `printer-<app>.jsonl`. It's safe to call more than once and returns the log directory. |
| `default_log_dir()` | Where logs go by default. |
| `PrintError` | Raised **only** when the job definitely never reached the spooler (no printer, CreateDC/CreatePrinterDC/StartDoc failed, or a later failure where AbortDoc returned and the queue holds no job with our attempt ID). Safe to retry. Subclass of `RuntimeError`. |
| `PrintResult` | `printer_name`, `job_name`, `job_id`, `outcome`, `history`, `elapsed_s`. |
| `JobOutcome` | See [Outcomes](#outcomes). `.ok` is true when a label most likely came out. |
| `PrinterCandidate`, `PrintQueue`, `UsbDevice` | Discovery records. `candidate.reasons()` explains each check. |
| `get_backend()` / `set_backend(backend)` | Access or replace the platform backend (`PrinterBackend`), for example with a fake in tests. |

With `track_timeout_s > 0` the Windows backend polls the spooler for the job
(found by the unique `[attempt-id]` at the end of its document name) and
records each status change in `result.history`. A successful `EndDoc` only
means the spooler accepted the job. Tracking shows whether it actually printed.

### Outcomes

| `JobOutcome` | `.ok` | Meaning |
|---|---|---|
| `COMPLETED` | yes | The spooler reported the job printed, or it left the queue normally. |
| `VANISHED_UNSEEN` | yes | The job was never seen in the queue (usually printed before the first poll). The queue contents are logged. |
| `NOT_TRACKED` | yes | Spooled; tracking was not requested (`track_timeout_s=0`). |
| `ERROR` | no | The job sat in an error state (paper out, offline, ...) for 10 s. It may still print once fixed. |
| `DELETED` | no | The job was deleted from the queue. |
| `TIMEOUT` | no | Still queued when tracking gave up. It may still print. |
| `TRACKING_FAILED` | no | Spooled, but following the job raised. History keeps what was seen. |
| `UNCERTAIN` | no | A GDI call failed after `StartDoc` and the library could not rule out that part or all of the job reached the printer (`EndDoc` failed, `AbortDoc` raised, the job is still in the queue, or the queue could not be checked). `history` says which. |

Every non-ok outcome means "the label may or may not come out": tell the user
to check the printer before reprinting. Never refund or resend automatically;
`print_to_first_available` never sends such a job to a second printer.

## Diagnostics CLI

```sh
ibp-print-diag                    # full report: queues, gates, USB devices, verdict, events
ibp-print-diag --json             # the same, machine-readable
ibp-print-diag --events 240       # include PrintService events from the last 4 h (default 60)
ibp-print-diag --test-print       # print a 4x6 test label to the best usable printer
ibp-print-diag --test-print "DYMO LabelWriter 4XL 0922:0028"
ibp-print-diag --log-dir C:\temp\logs -v   # custom log dir, echo log to the console
```

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
app would stop rotation and lose records.

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

The tests use fake `win32print` / `win32ui` / `wmi` / `pythoncom` modules, so
the whole suite, including the Windows backend, runs on Linux. CI runs
it on Ubuntu and Windows.
