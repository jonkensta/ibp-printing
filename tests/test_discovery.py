"""Tests for queue-name / device-id matching and candidate ranking."""

import unittest
from typing import Optional

from ibp_printing.discovery import (
    build_candidates,
    discovery_from,
    vid_pid_from_device_id,
    vid_pid_from_printer_name,
)
from ibp_printing.models import PrinterCandidate, PrintQueue, UsbDevice
from ibp_printing.winconst import PRINTER_ATTRIBUTE_WORK_OFFLINE

DYMO = "0922:0028"


def usb(
    vid_pid: str = DYMO,
    status: str = "OK",
    error_code: int = 0,
    present: Optional[bool] = True,
) -> UsbDevice:
    """A WMI-like USB device for VID:PID ``vid_pid``."""
    vid, pid = vid_pid.split(":")
    return UsbDevice(
        device_id=f"USB\\VID_{vid}&PID_{pid}\\5&1A2B3C4D&0&1",
        name="DYMO LabelWriter 4XL",
        status=status,
        error_code=error_code,
        pnp_class="USB",
        vid_pid=vid_pid,
        present=present,
    )


class PrinterNameTests(unittest.TestCase):
    """vid_pid_from_printer_name."""

    def test_every_separator_is_accepted(self):
        for sep in (" ", "\t", "_", "-"):
            with self.subTest(sep=repr(sep)):
                self.assertEqual(
                    vid_pid_from_printer_name(f"DYMO LabelWriter 4XL{sep}0922:0028"),
                    DYMO,
                )

    def test_trailing_whitespace_is_ignored(self):
        self.assertEqual(vid_pid_from_printer_name("DYMO 0922:0028  \t"), DYMO)

    def test_lowercase_hex_is_uppercased(self):
        self.assertEqual(vid_pid_from_printer_name("Zebra 0a5f:00d1"), "0A5F:00D1")

    def test_name_that_is_only_the_vid_pid(self):
        self.assertEqual(vid_pid_from_printer_name("0922:0028"), DYMO)

    def test_names_without_a_suffix_do_not_match(self):
        for name in (
            "DYMO LabelWriter 4XL",
            "DYMO0922:0028",  # no separator
            "DYMO 0922:00281",  # five hex digits
            "DYMO 922:0028",  # three hex digits
            "DYMO 0922:0028 copy",  # not at the end
            "DYMO 0922-0028",  # wrong inner separator
            "DYMO 0922:00G8",  # not hex
            "",
        ):
            with self.subTest(name=name):
                self.assertIsNone(vid_pid_from_printer_name(name))


class DeviceIdTests(unittest.TestCase):
    """vid_pid_from_device_id."""

    def test_usb_device_id(self):
        self.assertEqual(
            vid_pid_from_device_id("USB\\VID_0922&PID_0028\\5&1A2B3C4D&0&1"), DYMO
        )

    def test_composite_interface_and_lowercase(self):
        self.assertEqual(
            vid_pid_from_device_id("usb\\vid_0a5f&pid_00d1&mi_00\\6&abc&0&0000"),
            "0A5F:00D1",
        )

    def test_ids_without_vid_pid(self):
        for device_id in (
            "USBPRINT\\DYMOLABELWRITER_4XL\\7&2F8E1C5&0&USB001",
            "USB\\ROOT_HUB30\\4&1234&0&0",
            "",
        ):
            with self.subTest(device_id=device_id):
                self.assertIsNone(vid_pid_from_device_id(device_id))


class CandidateTests(unittest.TestCase):
    """build_candidates, PrinterCandidate and Discovery.usable."""

    def test_matching_queue_is_usable(self):
        (candidate,) = build_candidates([PrintQueue("DYMO 0922:0028")], [usb()])
        self.assertTrue(candidate.usable)
        self.assertEqual(candidate.vid_pid, DYMO)
        self.assertEqual(len(candidate.usb_devices), 1)
        self.assertEqual(candidate.reasons(), [f"USB {DYMO} present (1 entities)"])

    def test_queue_without_suffix_is_not_usable(self):
        (candidate,) = build_candidates([PrintQueue("Microsoft Print to PDF")], [usb()])
        self.assertFalse(candidate.usable)
        self.assertIn("does not end in a VID:PID", candidate.reasons()[0])

    def test_queue_whose_device_is_missing_is_not_usable(self):
        (candidate,) = build_candidates(
            [PrintQueue("DYMO 0922:0028")], [usb("0922:0020")]
        )
        self.assertFalse(candidate.usable)
        self.assertEqual(candidate.usb_devices, ())
        self.assertIn("no USB device", candidate.reasons()[0])

    def test_devices_without_vid_pid_are_ignored(self):
        device = UsbDevice(device_id="USBPRINT\\X\\1", vid_pid=None)
        (candidate,) = build_candidates([PrintQueue("DYMO 0922:0028")], [device])
        self.assertFalse(candidate.usable)

    def test_unhealthy_or_problem_queue_is_still_usable(self):
        candidate = PrinterCandidate(
            PrintQueue("DYMO 0922:0028", status=0x80),  # OFFLINE
            DYMO,
            (usb(status="Error", error_code=43),),
        )
        self.assertTrue(candidate.usable)
        self.assertFalse(candidate.device_healthy)
        reasons = " | ".join(candidate.reasons())
        self.assertIn("non-OK status", reasons)
        self.assertIn("OFFLINE", reasons)

    def test_work_offline_attribute_is_a_problem(self):
        queue = PrintQueue("DYMO 0922:0028", attributes=PRINTER_ATTRIBUTE_WORK_OFFLINE)
        self.assertTrue(queue.has_problem)
        candidate = PrinterCandidate(queue, DYMO, (usb(),))
        self.assertIn("WORK_OFFLINE", candidate.reasons()[-1])

    def test_ranking(self):
        queues = [
            PrintQueue("A problem 0922:0028", status=0x2),  # ERROR
            PrintQueue("B sick 0922:0020"),
            PrintQueue("C ok 0922:0029"),
            PrintQueue("D default 0922:002A", is_default=True),
            PrintQueue("E unplugged 0922:0030"),
            PrintQueue("F not a label printer"),
        ]
        devices = [
            usb("0922:0028"),
            usb("0922:0020", status="Degraded"),
            usb("0922:0029"),
            usb("0922:002A"),
        ]
        discovery = discovery_from(queues, devices, ["boom"])
        self.assertEqual(len(discovery.candidates), 6)
        self.assertEqual(
            [candidate.name for candidate in discovery.usable],
            [
                "D default 0922:002A",
                "C ok 0922:0029",
                # Device health ranks above queue problem flags.
                "A problem 0922:0028",
                "B sick 0922:0020",
            ],
        )
        self.assertEqual(discovery.errors, ["boom"])

    def test_ghost_device_not_present_is_not_usable(self):
        (candidate,) = build_candidates(
            [PrintQueue("DYMO 0922:0028")], [usb(present=False)]
        )
        self.assertFalse(candidate.usable)
        self.assertEqual(len(candidate.usb_devices), 1)  # still listed
        self.assertEqual(candidate.connected_devices, ())
        reasons = " | ".join(candidate.reasons())
        self.assertIn("not connected", reasons)
        self.assertIn("Present=False", reasons)

    def test_error_code_45_is_not_usable(self):
        ghost = usb(status="Error", error_code=45, present=None)
        self.assertFalse(ghost.connected)
        self.assertFalse(ghost.healthy)
        (candidate,) = build_candidates([PrintQueue("DYMO 0922:0028")], [ghost])
        self.assertFalse(candidate.usable)
        self.assertIn("error code 45", " | ".join(candidate.reasons()))

    def test_present_unknown_counts_as_present(self):
        # Win32_PnPEntity.Present is missing before Windows 10.
        (candidate,) = build_candidates(
            [PrintQueue("DYMO 0922:0028")], [usb(present=None)]
        )
        self.assertTrue(candidate.usable)
        self.assertTrue(candidate.device_healthy)

    def test_ghost_plus_real_device_is_usable_and_ghost_ignored(self):
        (candidate,) = build_candidates(
            [PrintQueue("DYMO 0922:0028")],
            [usb(present=False), usb(error_code=45), usb()],
        )
        self.assertTrue(candidate.usable)
        self.assertEqual(len(candidate.connected_devices), 1)
        reasons = candidate.reasons()
        self.assertEqual(reasons[0], f"USB {DYMO} present (1 entities)")
        self.assertEqual(sum("ignored, not connected" in r for r in reasons), 2)

    def test_ghost_does_not_make_device_healthy(self):
        candidate = PrinterCandidate(
            PrintQueue("DYMO 0922:0028"),
            DYMO,
            (usb(status="Error", error_code=10), usb(present=False)),
        )
        self.assertTrue(candidate.usable)
        self.assertFalse(candidate.device_healthy)

    def test_healthy_if_any_matching_device_is_healthy(self):
        candidate = PrinterCandidate(
            PrintQueue("DYMO 0922:0028"),
            DYMO,
            (usb(status="Error", error_code=10), usb()),
        )
        self.assertTrue(candidate.device_healthy)

    def test_no_usb_matching_makes_every_queue_usable(self):
        candidate = PrinterCandidate(PrintQueue("Canon"), None, (), usb_matching=False)
        self.assertTrue(candidate.usable)
        self.assertTrue(candidate.device_healthy)
        self.assertIn("no USB matching", candidate.reasons()[0])

    def test_to_log_shape(self):
        (candidate,) = build_candidates([PrintQueue("DYMO 0922:0028")], [usb()])
        data = candidate.to_log()
        self.assertEqual(data["name"], "DYMO 0922:0028")
        self.assertTrue(data["usable"])
        self.assertEqual(data["usb_devices"][0]["vid_pid"], DYMO)


if __name__ == "__main__":
    unittest.main()
