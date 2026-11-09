# Print server

`ibp-print-server` runs on each intake station (Linux, CUPS) and prints the
request labels the IBP frontend shows, silently, on the station's USB label
printer. The frontend (`ibp-frontend/src/lib/printServer.ts`) posts the label
PNG to `http://localhost:40121/print-image`; if the print server is down or
answers with an error, the frontend offers the label PDF instead.

It replaces the standalone `ibp-print-server` repo (`print-server` command),
with the same HTTP contract, and prints through this library: the same
printer discovery, CUPS printing and logs as everything else here.

## HTTP contract

| Request | Response |
|---|---|
| `GET /health` | `200 {"status": "ok", "service": "print-server", "printers": {"count": 1, "names": ["iDPRT_SP310_0a5f:0001"]}}`; `"status": "degraded"` with `"error"` if the printers could not be listed |
| `POST /print-image`, body: the raw image (`Content-Type: image/png`), at most 1 MB | `200 {"status": "queued"}` once queued |
| | `400` not an image / bad `Content-Length`, `413` too large, `503` no label printer, `500` unexpected: `{"status": "error", "error": "..."}` |
| `OPTIONS` (CORS preflight) | `200` with `Access-Control-Allow-Methods: POST, GET, OPTIONS`, `Access-Control-Allow-Headers: Content-Type` (and `Access-Control-Allow-Private-Network: true` when asked) |
| anything else | `404 {"status": "error", ...}` |

Every response has `Access-Control-Allow-Origin: *`. The query string is
ignored. "Queued" means accepted, not printed: one worker thread prints the
labels one at a time, in the order they came in. A label that fails to print
is logged and not retried. On `SIGINT` / `SIGTERM` the server stops taking
requests, lets the label being printed finish (up to 60 s), and logs how many
queued labels it dropped.

The old server's form-encoded `POST /` (`data=<label JSON>`, rendered into a
Code128 label by the server) is gone: the backend renders the label
(`GET /api/requests/{code}/label`) and nothing posted to it any more.

## Which printer, and how the label is sized

- **Printer:** the best usable label printer from `ibp_printing.discover()`,
  falling back to the next one only when a printer definitely never got the
  job. A CUPS queue is a label printer only if its name ends in the
  printer's USB `VID:PID` (`iDPRT_SP310_0a5f:0001`), and usable while that
  device is plugged in (see "Printer naming rule" in the README). A
  supported direct USB printer (PM2411BT) would be used first.
  `--printer NAME` pins one printer: only it is used, and only while CUPS
  has a queue of that name (plugged in or not).
- **Size:** the image is turned to the orientation of the queue's default
  media and scaled, keeping its aspect ratio, to fill a page of that size at
  300 DPI; that page is sent with `lp` and no scaling options. The label
  PNG from the backend is 1004x378 (85x32 mm at 300 DPI). On a queue whose
  default PageSize is a 1.4x3.5 in label (420x1050 px), it is rotated to
  portrait and scaled to 395x1050, centered: the full length, with about
  0.04 in of white along each long edge. (The old server stretched it to
  420x1050 instead.) A PNG already the page's size is printed pixel for
  pixel. The default media comes from CUPS (`media-default`, via
  pycups); without pycups, from `lpoptions -l`, which cannot show a custom
  default size, and the label is then scaled onto a letter page (a WARNING
  in the log). So install the `linux` extra, and set the queue's default
  PageSize to the label size.

## Install on an intake station

As root, once:

```sh
sudo apt install git build-essential python3-dev libcups2-dev   # pycups builds from source
sudo useradd --create-home ibp        # if the station has no ibp user yet
```

As the `ibp` user:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
uv tool install "ibp-printing[linux] @ git+https://github.com/jonkensta/ibp-printing"
```

Name the CUPS queue after the printer's USB `VID:PID` (`lsusb`) and set its
default PageSize to the label (for example in the CUPS web interface,
*Set Default Options*). Then check it:

```sh
ibp-print-server list                 # same as ibp-print-diag: "=> usable: YES"
ibp-print-server test                 # a test label on the best usable printer
ibp-print-server print label.png      # print a PNG the way the server would
```

Install and start the service (the unit file is
[`packaging/linux/ibp-print-server.service`](../packaging/linux/ibp-print-server.service)):

```sh
sudo install -m 0644 packaging/linux/ibp-print-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ibp-print-server
curl -s localhost:40121/health
```

Every start or restart runs `uv tool upgrade ibp-printing` first, so
`sudo systemctl restart ibp-print-server` deploys the latest `main`. If the
upgrade fails (no network), the installed version starts anyway.

**Moving from the old `print-server`:** stop it first, since both use port
40121.

```sh
sudo systemctl disable --now print-server
sudo rm /etc/systemd/system/print-server.service
sudo -u ibp /home/ibp/.local/bin/uv tool uninstall print-server
```

The CUPS queue keeps its name: the old `_VVVV:PPPP` suffix already follows
the naming rule.

## Commands

```sh
ibp-print-server server [--port 40121] [--printer NAME]   # run the server
ibp-print-server print IMAGE [--printer NAME]             # print one image file
ibp-print-server list                                     # ibp-print-diag --events 0
ibp-print-server test [--printer NAME]                    # ibp-print-diag --events 0 --test-print [NAME]
ibp-print-server --log-dir DIR ...                        # log somewhere else
```

The server listens on every interface. The frontend's default URL is
`http://localhost:40121`; browsers block plain-HTTP requests from an HTTPS
page to any other host.

## Logs

`server` and `print` write `printer-print-server.log` / `.jsonl` in
`~/.local/state/ibp-printing/logs/` of the user running it (`/home/ibp/...`
for the service), and INFO and above to stderr, which ends up in the journal:

```sh
journalctl -u ibp-print-server -f
```

Each label is one attempt (`BEGIN print-server job` ... `END`) with the
printer chosen, the media and page size used (`fitting to media`), and the
`lp` result. `/health` polls are not logged unless they fail.
