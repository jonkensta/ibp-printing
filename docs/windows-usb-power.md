# Windows: USB printer dropouts and power settings (PM2411BT)

Field notes from IBP's two Windows shipping laptops (Acer Aspire A315-24P,
BIOS V1.28, Modern Standby), 2026-10-15. Volunteers kept finding that the
PM2411BT on direct USB "wasn't there" when a label printed, even with the new
driver.

## Diagnosis

The watcher's logs (`%LOCALAPPDATA%\ibp-printing\logs\printer-watcher.log`,
see [watcher.md](watcher.md)) showed two kinds of failure: the printer
vanishing mid-I/O (`DeviceGone`), and discovery finding no USB printer at all
(`devices=0`).

The Windows System log on **both** laptops shows why
(source `Microsoft-Windows-USB-USBHUB3`), for VID `2E3C` PID `5760`, several
times a day:

- event **196**: "USB device draining system power when system is idle ...
  Removal action failed", followed by
- event **205**: "Re-enumerating a DRIPS blocking device that was previously
  removed when exiting low power epoch".

These are Modern Standby (S0 low-power idle) laptops. When the screen turns
off, the USB stack tries to power the printer down, fails, tears the device
down and re-enumerates it later. A print in that window finds no printer.
Turning hibernate off does not affect this.

Other findings:

- USB selective suspend was still **Enabled** (AC and DC) even though it had
  been "turned off" before.
- The root hubs already had "Allow the computer to turn off this device"
  unchecked.
- "Device Descriptor Request Failed" ghost devices sit on ports 1-2 of the
  same root hub the printers use (`USB\ROOT_HUB30\5&22c970a0&0&0`, Port 2 of
  Hub 2).

To check it yourself, look in Event Viewer under System for USBHUB3 196/205,
and under `Microsoft-Windows-Kernel-PnP/Configuration`. For a live test, plug
the printer in and run `ibp-print-diag --direct-status` every few seconds to
time how long it takes to appear ([windows-field-test.md](windows-field-test.md)).

## Fixes applied (2026-10-15)

Applied on both laptops. These are the equivalent commands, from an elevated
PowerShell:

```powershell
# USB selective suspend off (AC and DC)
powercfg /setacvalueindex SCHEME_CURRENT 2a737441-1930-4402-8d77-b2bebba308a3 48e6b7a6-50f5-4782-a5d4-53bb8f07e226 0
powercfg /setdcvalueindex SCHEME_CURRENT 2a737441-1930-4402-8d77-b2bebba308a3 48e6b7a6-50f5-4782-a5d4-53bb8f07e226 0
# Plugged in: screen and sleep never time out (battery: 10 min)
powercfg /change monitor-timeout-ac 0
powercfg /change standby-timeout-ac 0
powercfg /setactive SCHEME_CURRENT
```

Per printer: turn off the USB power management in the device's
`Device Parameters` key, for **every** PM2411BT serial the laptop has seen:

```powershell
$serial = 'Q529E5299340263'   # one printer's USB serial
$key = "HKLM:\SYSTEM\CurrentControlSet\Enum\USB\VID_2E3C&PID_5760\$serial\Device Parameters"
foreach ($v in 'EnhancedPowerManagementEnabled','AllowIdleIrpInD3','SelectiveSuspendEnabled') {
    Set-ItemProperty -Path $key -Name $v -Type DWord -Value 0
}
```

IBP's two printers are `Q529E5299340263` and `Q529E65K5250028`; list the ones
a laptop knows with
`Get-ChildItem 'HKLM:\SYSTEM\CurrentControlSet\Enum\USB\VID_2E3C&PID_5760'`.
The values take effect the next time the printer is plugged in, and a **new**
printer serial needs them set again.

Also applied, not USB-related: lid close = shut down (AC and DC; the setting is
hidden in the Acer plan, unhide it with
`powercfg -attributes SUB_BUTTONS LIDACTION -ATTRIB_HIDE`, then
`LIDACTION 3`), and `powercfg /hibernate off` (also turns off Fast Startup,
so shutdown is a real cold boot).

To see whether the fixes worked, watch for USBHUB3 196/205 in the System log;
they should stop or become rare.

Still to try on site if dropouts continue: the USB-C port with a USB-C to
USB-B cable (the laptops have only one USB-A port), and an Acer BIOS update
(on site only, never remotely). Acer has no USB driver updates; those come
from Microsoft. Bluetooth is only worth testing if USB itself keeps failing:
on Windows Phomemo seems to support it only via their Labelife app (pair it
and look for "Standard Serial over Bluetooth link" under
`Get-PnpDevice -Class Ports`).

## Open question: the printer's own auto-off timer

The PM2411BT has an auto-shutdown timer that Phomemo exposes only in the
Labelife app ("Manage Devices"). No raw command for it is documented; the only
commands known to work over USB are `SSSGETCAP` / `SSSGETPAPER`
([printers/pm2411bt.md](printers/pm2411bt.md)), so don't guess bytes at the
firmware.

First find out whether it matters, from the uploaded watcher logs:

- The watcher's usb-monitor logs every disconnect as
  `USB device deletion ... device_id=USB\VID_2E3C&PID_5760\<serial>` (and
  arrivals as `USB device creation`).
- For each deletion, check the same laptop's System log for USBHUB3 196/205 at
  that time (Windows idle teardown), and whether someone just unplugged it (a
  deletion followed by a creation within seconds during a shift is a replug).
- A deletion with no 196/205 nearby, no replug, and a regular idle interval
  since the last print (e.g. ~10/30/60 min) points at the printer's timer.

If it matters, in order of preference:

1. Set the timer to "never" once per printer from Labelife on a **phone** over
   Bluetooth (the setting is stored in the printer).
2. Capture the command: change the setting in Labelife on Android with the
   Bluetooth HCI snoop log on, extract the bytes, and add a setter to the
   direct USB layer.

## To do: stop the watcher printing stale labels (deferred)

**Problem.** The watcher prints everything in `Downloads\to-print\` whenever a
printer is available, however old, with no idea whether the label was replaced
or refunded meanwhile. When first started on 2026-10-15 it printed three old
labels on one laptop, and two were bad: in both cases a volunteer had already
bought a replacement label (one original was already "refund submitted"), so
the printed copies had to be thrown away.

**Proposed fix (not started):**

1. *Age limit in the watcher* (this repo): labels in `to-print` older than a
   few hours (e.g. 6, configurable in `watcher.toml`) are not auto-printed;
   move them to a `check-first\` folder and show a message box asking a
   volunteer to confirm the package wasn't re-shipped. The `.png.json` sidecar
   already has `created`, `shipment_id`, `tracking_code`, `recipient_key`.
2. *Pull the old label on a re-purchase* (shippy / shippy-gui): when the
   duplicate-purchase check (`ibp_printing.labels.find_duplicates`) warns and
   the volunteer buys anyway, move the earlier still-queued label for that
   recipient from `to-print` to `hold-refund` and tell them to refund it.

Rejected for now: having the watcher ask EasyPost for refund/tracking status
(couples ibp-printing to EasyPost and needs the API key on the watcher side).
Estimate: half a day across this repo and both apps. Push ibp-printing before
bumping the apps' pins.
