# Windows field test: direct USB printing (PM2411BT)

> Tests ibp-printing commit `2406c4a` (`2406c4ae84f32ed2901e0758c1ec5779cc1e4ce8`, branch `feat/direct-usb`).
> Every command below is pinned to it, so the code cannot change under you.

For the volunteer coordinator, on one IBP shipping PC. Takes about 20 minutes.
**Nothing here is installed permanently. Nothing here changes how the normal
IBP shipping app works.** The test code runs from one fixed commit of the
`feat/direct-usb` branch; IBP's day-to-day apps (`main`) are not touched.

## Before you start

- PM2411BT plugged into the PC **by USB** and switched on.
- Label roll seated **flush against the guides**, cover **closed**.
- Uses **1 label** (2 at most). **Never** use real postage. No label is bought.
- Close shippy / shippy-gui and the label watcher if they are open.
- Open **PowerShell** (Start menu, type `powershell`). Paste this once. It
  makes a `diag` shortcut and a results folder, for this window only:

```powershell
function diag { uvx --from "git+https://github.com/jonkensta/ibp-printing@2406c4ae84f32ed2901e0758c1ec5779cc1e4ce8" ibp-print-diag @args }
New-Item -ItemType Directory -Force "$HOME\Desktop\ibp-test" | Out-Null
```

The first run downloads for a minute. If PowerShell says `uvx` is not
recognized, stop and tell us.

## Step 1: can we reach the printer? (no label)

```powershell
diag --direct-status | Tee-Object "$HOME\Desktop\ibp-test\1-status.txt"
diag | Tee-Object "$HOME\Desktop\ibp-test\1-report.txt"
```

In the first output, find the block for `vid_pid=2E3C:5760`:

| You see | Means | Do |
|---|---|---|
| `PM2411BT at ...`, `status probe: cover CLOSE ...`, `ready: 1 of 1` | **Good**: Windows lets us open it and it answers | Go on. Write down the `printer name:` line, e.g. `PM2411BT (USB direct, serial Q529...)` |
| `PM2411BT (probably) - BUSY: another program or the Windows print queue has it open` | **Busy**: something else holds the printer | See *If busy* below |
| `PM2411BT ...`, `cover no reply` or `probe failed: ...` | **No reply**: opened, but no answer | Turn the printer off and on, wait 10 s, run the first command once more. Record both results. |
| `cover OPEN` | Cover not latched | Close it firmly, wait 10 s, run again |
| No block with `2E3C:5760` | Windows does not see it as a USB printer | Check cable; record and skip to Step 5 |

**Pushed replies (only if Good):** run this, then within 30 s **open the
cover, wait 3 s, close it**, and wait for the command to finish:

```powershell
diag --listen 30 | Tee-Object "$HOME\Desktop\ibp-test\1-listen.txt"
```

It sends only the cover question (no label). Record whether these lines
appear: `SSSGETCAP:OPEN`, `SSSGETCAP:CLOSE`, then `SSSGETPRINTING:DOING` and
`SSSGETPRINTING:DONE` (the printer's realign after the cover closes; it may
feed a little paper, no label is printed). Ctrl+C stops it early.

**If busy**, try in order, re-running `diag --direct-status` after each:

1. Is the vendor queue printing? `Get-Printer | Format-Table Name,PortName,DriverName`
   then `Get-PrintJob -PrinterName "<the PM2411BT queue name>"`. Wait for jobs
   to finish (do not delete them).
2. Close any printer utility from the vendor (tray icons by the clock, Task
   Manager).
3. *Optional, needs an Administrator PowerShell:* `Stop-Service Spooler`, run
   the check, then **immediately** `Start-Service Spooler`.
4. *Optional last resort, reversible:* write down the vendor queue's exact name,
   remove it (Settings > Bluetooth & devices > Printers & scanners > the
   PM2411BT > Remove), check again. **Re-add it before you leave** (replug the
   printer or re-run the vendor installer, restore the exact name) and confirm
   the normal app still prints - the normal IBP app needs that queue.

If it becomes Good, do the *Pushed replies* check. If it is still busy, record
which steps you tried and go to Step 3.

## Step 2: one direct test print (1 label)

Only if Step 1 was **Good**. Use the exact `printer name:` from Step 1.
`--direct-only` makes sure it can never print through the vendor queue instead:

```powershell
diag --test-print "PM2411BT (USB direct, serial PUT-YOURS-HERE)" --direct-only | Tee-Object "$HOME\Desktop\ibp-test\2-print.txt"
```

**Good:** `outcome=completed`, `job_id=None`, history shows `printer started
printing (DOING)` and `printer finished (DONE ...)`. The label says *IBP TEST
PRINT*, upright, all four corner words and the border visible, not cut off.
`FAILED: --direct-only: ...` means nothing was printed (record the message).
Anything else (`uncertain`, `timeout`, `error`): **do not run it again**. Note
what came out of the printer and take a photo of the label.

## Step 3: turn-off switch (no label)

```powershell
$env:IBP_PRINTING_DIRECT="0"
diag --direct-status | Tee-Object "$HOME\Desktop\ibp-test\3-off-status.txt"
diag | Tee-Object "$HOME\Desktop\ibp-test\3-off.txt"
Remove-Item Env:IBP_PRINTING_DIRECT
```

**Good:** the first says `direct USB printing: OFF (IBP_PRINTING_DIRECT=0)` and
`no USB device was opened`; the second `direct USB printing: OFF (queue-only)`
and no `[direct USB]` under *Verdict*. Do not print in this step.

## Step 4: shippy-gui (optional, no label)

shippy-gui has no test or preview mode, so **do not create a label**; we will
do a real-label test together. You may only *look*: in the folder the IBP
shortcut starts in (shortcut > Properties > *Start in*), run
`uvx --from "git+https://github.com/jonkensta/shippy-gui.git@refactor/use-ibp-printing" shippy-gui`,
check the printer list shows `PM2411BT (USB direct, ...)`, then quit
(Ctrl+Q) **without** clicking *Create Label*.

## Step 5: send us the logs

```powershell
Copy-Item "$env:LOCALAPPDATA\ibp-printing\logs\*" "$HOME\Desktop\ibp-test\"
Compress-Archive "$HOME\Desktop\ibp-test\*" "$HOME\Desktop\ibp-test.zip" -Force
```

The logs are `printer-diag.log` / `.jsonl` (this test), plus
`printer-shippy-gui.*`, `printer-shippy.*`, `printer-watcher.*` if present.
Email `ibp-test.zip` or bring it on a USB stick (OneDrive is not set up yet).
Add photos of any printed label.

> **If anything goes wrong**
> - Never re-run a print to "try again": it could print twice. Write down what happened.
> - After paper runs out: **turn the printer off and on before loading paper**,
>   otherwise it can print half a leftover job.
> - Close the PowerShell window and everything is back to normal: the IBP
>   shipping app, its printer queue and its settings are unchanged (unless you
>   removed the queue in Step 1.4 - re-add it).

## Results

| Question | Result (circle) | Notes |
|---|---|---|
| 1. Direct open works with the vendor queue installed? | good / busy / no reply | If busy: which fix worked? |
| 2a. Cover reply (`cover CLOSE/OPEN`) arrives? | yes / no | |
| 2b. `--listen`: `SSSGETCAP:OPEN`/`CLOSE` pushed on cover open/close? | yes / no | |
| 2c. `--listen`: `DOING`/`DONE` after cover close? | yes / no | |
| 3. Direct test print `completed` and label correct? | yes / no / skipped | outcome: |
| 4. `IBP_PRINTING_DIRECT=0` shows `OFF`, nothing opened? | yes / no | |
| 5. shippy-gui lists the direct printer? | yes / no / skipped | |
| 6. Logs zipped and sent? | email / USB | |
| Vendor queue name / PC name | | |
