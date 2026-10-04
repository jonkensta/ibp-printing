"""Direct USB layer: discovery ranking, routing, error mapping, kill switch."""

import logging
import os
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest import mock

from PIL import Image
from printing_helpers import FakeBackend, dymo, quiet_logging

import ibp_printing
from ibp_printing import PrintError, PrintQueue
from ibp_printing import log as ibp_log
from ibp_printing.backends import create_backend
from ibp_printing.direct import config as direct_config
from ibp_printing.direct import transport as tr
from ibp_printing.direct.backend import DirectFirstBackend, session_timeouts
from ibp_printing.direct.session import DirectPrintError, SessionTimeouts
from ibp_printing.direct.transport import (
    DeviceUnavailable,
    DirectDevice,
    FakeTransport,
    fake_device,
)
from ibp_printing.discovery import (
    PM2411BT_VID_PID,
    direct_candidate_name,
    is_direct_name,
)
from ibp_printing.models import JobOutcome, UsbDevice

FAST = SessionTimeouts(
    drain_s=0.01,
    query_write_s=0.05,
    query_reply_s=0.05,
    realign_wait_s=0.05,
    job_write_s=0.05,
    doing_s=0.1,
    done_s=0.1,
    stall_listen_s=0.01,
    poll_s=0.005,
)
PM_NAME = "PM2411BT (USB direct, serial TESTSERIAL)"
TWIN_QUEUE = "PM2411BT 2E3C:5760"
DYMO_QUEUE = "DYMO 0922:0028"


def pm_usb() -> UsbDevice:
    """The PM2411BT as Windows' PnP sees it (for the twin queue)."""
    return UsbDevice(
        device_id="USB\\VID_2E3C&PID_5760\\Q529",
        name="PM2411BT",
        status="OK",
        error_code=0,
        vid_pid=PM2411BT_VID_PID,
    )


def printer(
    *, cover: str = "CLOSE", doing: bool = True, done: bool = True, **kwargs
) -> FakeTransport:
    """A FakeTransport that answers like a ready PM2411BT."""
    fake = FakeTransport(**kwargs)
    fake.add_reply("SSSGETCAP", f"SSSGETCAP:{cover}")
    fake.add_reply("SSSGETPAPER", "SSSGETPAPER:YES")
    pushes = (["SSSGETPRINTING:DOING"] if doing else []) + (
        ["SSSGETPRINTING:DONE"] if done else []
    )
    fake.add_reply("PRINT 1,1", *pushes)
    return fake


class Harness:
    """A DirectFirstBackend over a FakeBackend with scripted USB devices."""

    def __init__(
        self,
        devices: Optional[list[DirectDevice]] = None,
        queues: Optional[list[PrintQueue]] = None,
        usb: Optional[list[UsbDevice]] = None,
        transport: Optional[FakeTransport] = None,
        enabled: Optional[bool] = None,
        **fake_kwargs,
    ) -> None:
        self.devices = [fake_device()] if devices is None else devices
        self.inner = FakeBackend(
            queues if queues is not None else [PrintQueue(DYMO_QUEUE)],
            usb if usb is not None else [dymo()],
            **fake_kwargs,
        )
        self.transport = transport or printer()
        self.opened: list[DirectDevice] = []
        self.discoveries = 0
        self.discover_error: Optional[BaseException] = None
        self.backend = DirectFirstBackend(
            self.inner,
            discover_devices=self._discover,
            opener=self._open,
            timeouts=FAST,
            enabled=enabled,
        )

    def _discover(self) -> list[DirectDevice]:
        self.discoveries += 1
        if self.discover_error is not None:
            raise self.discover_error
        return list(self.devices)

    def _open(self, device: DirectDevice) -> FakeTransport:
        self.opened.append(device)
        if self.transport.closed:  # a second open: a fresh, ready printer
            self.transport = printer()
        return self.transport.opener()(device)

    @property
    def queue_prints(self) -> list[str]:
        """Queue names the platform backend was asked to print to."""
        return [call[0] for call in self.inner.printed]


class DirectTestCase(unittest.TestCase):
    """Installs nothing global except through use(); cleans the kill switch."""

    def setUp(self):
        quiet_logging(self)
        self.img = Image.new("L", (1200, 1800), 255)
        self.addCleanup(ibp_printing.set_backend, None)
        self.addCleanup(direct_config.set_direct_enabled, None)
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(direct_config.ENV_VAR, None)

    def use(self, harness: Harness) -> Harness:
        """Install the harness backend process-wide."""
        ibp_printing.set_backend(harness.backend)
        return harness


class DiscoveryTests(DirectTestCase):
    """Direct candidates, ranking and the twin queue."""

    def test_direct_candidate_ranks_first(self):
        harness = Harness(queues=[PrintQueue(DYMO_QUEUE, is_default=True)])
        discovery = harness.backend.discover()
        names = [candidate.name for candidate in discovery.usable]
        self.assertEqual(names, [PM_NAME, DYMO_QUEUE])
        direct = discovery.usable[0]
        self.assertEqual(direct.transport, "direct")
        self.assertTrue(direct.is_direct)
        self.assertEqual(direct.vid_pid, PM2411BT_VID_PID)
        self.assertEqual(direct.queue.port, "fake:lp0")
        self.assertEqual(discovery.usable[1].transport, "queue")
        self.assertTrue(discovery.direct_enabled)
        self.assertEqual(len(discovery.direct_devices), 1)
        log = direct.to_log()
        self.assertEqual(log["transport"], "direct")
        self.assertEqual(log["direct_device"]["serial"], "TESTSERIAL")
        self.assertIn("is supported", direct.reasons()[0])

    def test_names(self):
        self.assertEqual(direct_candidate_name(fake_device()), PM_NAME)
        no_serial = fake_device(serial=None, path="/dev/usb/lp3")
        self.assertEqual(
            direct_candidate_name(no_serial), "PM2411BT (USB direct, /dev/usb/lp3)"
        )
        self.assertTrue(is_direct_name(PM_NAME))
        self.assertFalse(is_direct_name(TWIN_QUEUE))
        self.assertEqual(PM2411BT_VID_PID, tr.PM2411BT_VID_PID)

    def test_twin_queue_kept_as_fallback_with_note(self):
        harness = Harness(
            queues=[PrintQueue(TWIN_QUEUE, is_default=True), PrintQueue(DYMO_QUEUE)],
            usb=[pm_usb(), dymo()],
        )
        discovery = harness.backend.discover()
        names = [candidate.name for candidate in discovery.usable]
        self.assertEqual(names, [PM_NAME, TWIN_QUEUE, DYMO_QUEUE])
        twin = discovery.usable[1]
        self.assertTrue(any("same printer as" in reason for reason in twin.reasons()))
        dymo_queue = discovery.usable[2]
        self.assertFalse(
            any("same printer" in reason for reason in dymo_queue.reasons())
        )

    def test_matching_is_by_model_not_vid_pid(self):
        other = fake_device(model="XP-420B", path="fake:lp1", serial="OTHER")
        laser = DirectDevice(path="fake:lp2", vid_pid="04F9:0042", kind="fake")
        laser.model = "HL-L2350DW"
        harness = Harness(devices=[other, laser])
        discovery = harness.backend.discover()
        direct = [c for c in discovery.candidates if c.is_direct]
        # The Artery VID:PID device is explained but not usable; the laser
        # printer is not a candidate at all (only listed as a device).
        self.assertEqual(len(direct), 1)
        self.assertFalse(direct[0].usable)
        self.assertIn("NOT supported", direct[0].reasons()[0])
        self.assertEqual(len(discovery.direct_devices), 2)
        self.assertEqual([c.name for c in discovery.usable], [DYMO_QUEUE])

    def test_model_match_is_case_insensitive(self):
        harness = Harness(devices=[fake_device(model="pm2411bt ")])
        self.assertTrue(harness.backend.discover().usable[0].is_direct)

    def test_busy_device_without_1284_is_not_usable(self):
        busy = DirectDevice(
            path="\\\\?\\usb#vid_2e3c&pid_5760#q529#{guid}",
            vid_pid=PM2411BT_VID_PID,
            serial="Q529",
            kind="usbprint",
            busy=True,
            errors=["in use by another program or the Windows spooler"],
        )
        harness = Harness(
            devices=[busy], queues=[PrintQueue(TWIN_QUEUE)], usb=[pm_usb()]
        )
        discovery = harness.backend.discover()
        self.assertEqual([c.name for c in discovery.usable], [TWIN_QUEUE])
        direct = discovery.candidates[0]
        self.assertFalse(direct.usable)
        reasons = " ".join(direct.reasons())
        self.assertIn("1284 ID", reasons)
        self.assertIn("in use", reasons)

    def test_no_permission_ranks_after_healthy_queues(self):
        device = fake_device()
        device.accessible = False
        harness = Harness(devices=[device])
        discovery = harness.backend.discover()
        self.assertEqual([c.name for c in discovery.usable], [DYMO_QUEUE, PM_NAME])
        self.assertIn("udev", " ".join(discovery.usable[1].reasons()))

    def test_missing_node_is_not_usable(self):
        device = fake_device()
        device.present = False
        discovery = Harness(devices=[device]).backend.discover()
        self.assertEqual([c.name for c in discovery.usable], [DYMO_QUEUE])

    def test_discovery_failure_never_raises(self):
        harness = Harness()
        harness.discover_error = RuntimeError("sysfs exploded")
        discovery = harness.backend.discover()
        self.assertEqual([c.name for c in discovery.usable], [DYMO_QUEUE])
        self.assertTrue(any("sysfs exploded" in error for error in discovery.errors))

    def test_find_label_printers_through_api(self):
        self.use(Harness())
        self.assertEqual(
            [c.name for c in ibp_printing.find_label_printers()], [PM_NAME, DYMO_QUEUE]
        )

    def test_default_backend_is_direct_first(self):
        backend = create_backend()
        self.assertIsInstance(backend, DirectFirstBackend)
        self.assertEqual(backend.platform_name, backend.inner.platform_name)


class RoutingTests(DirectTestCase):
    """print_to_first_available / print_image send direct jobs over USB."""

    def test_prints_directly_and_maps_the_result(self):
        harness = self.use(Harness())
        result = ibp_printing.print_to_first_available(self.img, job_name="Order 9")
        self.assertEqual(result.printer_name, PM_NAME)
        self.assertIs(result.outcome, JobOutcome.COMPLETED)
        self.assertIsNone(result.job_id)
        self.assertRegex(result.job_name, r"^Order 9 \[[0-9a-f]{10}\]$")
        self.assertIn("direct USB: fake:lp0", result.history[0])
        self.assertTrue(any("DONE" in line for line in result.history))
        self.assertGreater(result.elapsed_s, 0)
        self.assertEqual(harness.queue_prints, [])
        self.assertIn("PRINT 1,1", harness.transport.written_lines)
        self.assertEqual(harness.transport.written_lines[-1], "SSSGETCAP")
        self.assertTrue(harness.transport.closed)
        # One discovery pass: the candidate's device is reused for printing.
        self.assertEqual(harness.discoveries, 1)

    def test_untracked_call_is_still_watched(self):
        self.use(Harness())
        result = ibp_printing.print_to_first_available(self.img, track_timeout_s=0)
        self.assertIs(result.outcome, JobOutcome.COMPLETED)

    def test_print_image_by_direct_name(self):
        harness = self.use(Harness())
        result = ibp_printing.print_image(self.img, PM_NAME, track_timeout_s=5)
        self.assertIs(result.outcome, JobOutcome.COMPLETED)
        self.assertEqual(harness.opened[0].path, "fake:lp0")
        self.assertEqual(harness.queue_prints, [])

    def test_print_image_to_queue_goes_to_platform(self):
        harness = self.use(Harness())
        result = ibp_printing.print_image(self.img, DYMO_QUEUE, track_timeout_s=2)
        self.assertEqual(result.printer_name, DYMO_QUEUE)
        self.assertEqual(harness.queue_prints, [DYMO_QUEUE])
        self.assertEqual(harness.opened, [])

    def test_print_image_unknown_direct_name(self):
        self.use(Harness(devices=[]))
        with self.assertRaises(PrintError) as caught:
            ibp_printing.print_image(self.img, PM_NAME)
        self.assertEqual(caught.exception.reason, "not_found")

    def test_print_image_unsupported_device_by_name(self):
        other = fake_device(model="XP-420B")
        self.use(Harness(devices=[other]))
        with self.assertRaises(PrintError) as caught:
            ibp_printing.print_image(
                self.img, "XP-420B (USB direct, serial TESTSERIAL)"
            )
        self.assertEqual(caught.exception.reason, "not_usable")

    def test_track_timeout_extends_done_wait(self):
        self.assertEqual(session_timeouts(FAST, 60).done_s, 60)
        self.assertEqual(session_timeouts(FAST, 0).done_s, FAST.done_s)
        self.assertEqual(session_timeouts(SessionTimeouts(), 10).done_s, 30)
        self.assertEqual(session_timeouts(FAST, 60).doing_s, FAST.doing_s)


class FallbackTests(DirectTestCase):
    """Definite not-sent falls back; anything else never does."""

    def twin(self, **kwargs) -> Harness:
        """The PM2411BT direct plus its (default) Windows queue, plus a DYMO."""
        return self.use(
            Harness(
                queues=[
                    PrintQueue(TWIN_QUEUE, is_default=True),
                    PrintQueue(DYMO_QUEUE),
                ],
                usb=[pm_usb(), dymo()],
                **kwargs,
            )
        )

    def test_busy_device_falls_back_to_its_queue(self):
        busy = DeviceUnavailable("in use by the spooler", reason="busy", path="x")
        harness = self.twin(transport=printer(raise_on_open=busy))
        result = ibp_printing.print_to_first_available(self.img)
        self.assertEqual(result.printer_name, TWIN_QUEUE)
        self.assertEqual(harness.queue_prints, [TWIN_QUEUE])
        self.assertEqual(harness.transport.written, b"")

    def test_open_error_is_print_error_with_reason(self):
        busy = DeviceUnavailable("in use", reason="busy", path="x")
        harness = self.use(Harness(transport=printer(raise_on_open=busy)))
        with self.assertRaises(PrintError) as caught:
            ibp_printing.print_image(self.img, PM_NAME)
        self.assertIsInstance(caught.exception, DirectPrintError)
        self.assertEqual(caught.exception.reason, "busy")
        self.assertEqual(harness.transport.written, b"")

    def test_permission_error_mentions_udev(self):
        denied = tr._linux_open_error(  # pylint: disable=protected-access
            "/dev/usb/lp0", PermissionError(13, "Permission denied")
        )
        self.use(Harness(transport=printer(raise_on_open=denied), queues=[]))
        with self.assertRaises(PrintError) as caught:
            ibp_printing.print_to_first_available(self.img)
        self.assertIn("Every label printer failed", str(caught.exception))
        self.assertIn("udev", str(caught.exception))

    def only_twin(self, transport: FakeTransport) -> Harness:
        """The PM2411BT direct plus its own Windows queue, nothing else."""
        return self.use(
            Harness(
                queues=[PrintQueue(TWIN_QUEUE, is_default=True)],
                usb=[pm_usb()],
                transport=transport,
            )
        )

    def test_cover_open_skips_its_own_queue(self):
        harness = self.twin(transport=printer(cover="OPEN"))
        result = ibp_printing.print_to_first_available(self.img)
        # Another printer is fine; the same printer's queue is not.
        self.assertEqual(result.printer_name, DYMO_QUEUE)
        self.assertEqual(harness.queue_prints, [DYMO_QUEUE])
        self.assertNotIn("PRINT 1,1", harness.transport.written_lines)

    def test_cover_open_is_a_print_error_for_the_volunteer(self):
        harness = self.only_twin(printer(cover="OPEN"))
        with self.assertRaises(PrintError) as caught:
            ibp_printing.print_to_first_available(self.img)
        self.assertEqual(caught.exception.reason, "cover_open")
        self.assertTrue(str(caught.exception).startswith("Close the printer cover"))
        self.assertNotIn("Other printers", str(caught.exception))
        self.assertEqual(harness.queue_prints, [])
        self.assertNotIn("PRINT 1,1", harness.transport.written_lines)

    def test_realign_busy_is_a_print_error_without_the_queue(self):
        fake = printer()
        fake.queue_lines("SSSGETPRINTING:DOING")  # a realign that never ends
        harness = self.only_twin(fake)
        with self.assertRaises(PrintError) as caught:
            ibp_printing.print_to_first_available(self.img)
        self.assertEqual(caught.exception.reason, "realign_busy")
        self.assertIn("Close the printer cover", str(caught.exception))
        self.assertEqual(harness.queue_prints, [])

    def test_no_answer_does_not_use_its_own_queue(self):
        """It may hold a partial job that would swallow the queue's job."""
        fake = FakeTransport()  # answers nothing
        harness = self.only_twin(fake)
        with self.assertRaises(PrintError) as caught:
            ibp_printing.print_to_first_available(self.img)
        self.assertEqual(caught.exception.reason, "no_cover_reply")
        self.assertIn("off and on", str(caught.exception))
        self.assertEqual(harness.queue_prints, [])

    def test_job_not_accepted_does_not_use_its_own_queue(self):
        queries = len(b"SSSGETPAPER\r\n") + len(b"SSSGETCAP\r\n")
        harness = self.only_twin(printer(stall_after_bytes=queries))
        with self.assertRaises(PrintError) as caught:
            ibp_printing.print_to_first_available(self.img)
        self.assertEqual(caught.exception.reason, "job_not_accepted")
        self.assertEqual(harness.queue_prints, [])

    def test_not_found_and_io_errors_still_use_its_own_queue(self):
        gone = DeviceUnavailable("unplugged", reason="not_found", path="x")
        harness = self.only_twin(printer(raise_on_open=gone))
        result = ibp_printing.print_to_first_available(self.img)
        self.assertEqual(result.printer_name, TWIN_QUEUE)

        broken = printer(read_error=tr.DeviceGone("read failed"))
        harness = self.only_twin(broken)
        result = ibp_printing.print_to_first_available(self.img)
        self.assertEqual(result.printer_name, TWIN_QUEUE)
        self.assertEqual(harness.queue_prints, [TWIN_QUEUE])

    def test_no_fallback_after_uncertain_write_stall(self):
        # Paper out: the printer stops taking data part-way through the job.
        harness = self.twin(transport=printer(stall_after_bytes=200))
        result = ibp_printing.print_to_first_available(self.img)
        self.assertEqual(result.printer_name, PM_NAME)
        self.assertIs(result.outcome, JobOutcome.UNCERTAIN)
        self.assertFalse(result.outcome.ok)
        self.assertTrue(any("power-cycle" in line for line in result.history))
        self.assertEqual(harness.queue_prints, [])

    def test_no_fallback_after_no_doing(self):
        harness = self.twin(transport=printer(doing=False, done=False))
        result = ibp_printing.print_to_first_available(self.img)
        self.assertIs(result.outcome, JobOutcome.UNCERTAIN)
        self.assertEqual(harness.queue_prints, [])

    def test_no_fallback_after_timeout(self):
        harness = self.twin(transport=printer(done=False))
        result = ibp_printing.print_to_first_available(self.img)
        self.assertIs(result.outcome, JobOutcome.TIMEOUT)
        self.assertEqual(harness.queue_prints, [])

    def test_no_fallback_after_command_error(self):
        fake = printer()
        fake.add_reply(
            "PRINT 1,1",
            "Cmd error:PRINT 1",
            "SSSGETPRINTING:DOING",
            "SSSGETPRINTING:DONE",
        )
        harness = self.twin(transport=fake)
        result = ibp_printing.print_to_first_available(self.img)
        self.assertIs(result.outcome, JobOutcome.ERROR)
        self.assertEqual(harness.queue_prints, [])

    def test_device_gone_mid_job_is_not_a_print_error(self):
        gone = tr.DeviceGone("unplugged", bytes_accepted=500)
        fake = printer()
        harness = self.twin(transport=fake)
        original = fake.write_all

        def write_all(data: bytes, timeout_s: float) -> int:
            if data.startswith(b"\r\nSIZE"):
                raise gone
            return original(data, timeout_s)

        with mock.patch.object(fake, "write_all", side_effect=write_all):
            result = ibp_printing.print_to_first_available(self.img)
        self.assertIs(result.outcome, JobOutcome.UNCERTAIN)
        self.assertEqual(harness.queue_prints, [])


class KillSwitchTests(DirectTestCase):
    """IBP_PRINTING_DIRECT=0 / set_direct_enabled(False) restore the old path."""

    def test_env_var_disables_direct(self):
        os.environ[direct_config.ENV_VAR] = "0"
        harness = self.use(Harness())
        discovery = ibp_printing.discover()
        self.assertFalse(discovery.direct_enabled)
        self.assertEqual([c.name for c in discovery.usable], [DYMO_QUEUE])
        self.assertEqual(harness.discoveries, 0)
        result = ibp_printing.print_to_first_available(self.img)
        self.assertEqual(result.printer_name, DYMO_QUEUE)
        self.assertEqual(harness.opened, [])
        self.assertFalse(ibp_printing.direct_enabled())

    def test_env_values(self):
        cases = {
            "0": False,
            "false": False,
            " Off ": False,
            "no": False,
            "1": True,
            "yes": True,
            "": True,
            "maybe": True,
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                os.environ[direct_config.ENV_VAR] = value
                self.assertIs(direct_config.direct_enabled(), expected)

    def test_function_overrides_env(self):
        os.environ[direct_config.ENV_VAR] = "1"
        ibp_printing.set_direct_enabled(False)
        self.assertEqual(
            direct_config.direct_mode(), (False, "set_direct_enabled(False)")
        )
        ibp_printing.set_direct_enabled(None)
        self.assertTrue(ibp_printing.direct_enabled())

    def test_direct_name_refused_when_disabled(self):
        ibp_printing.set_direct_enabled(False)
        harness = self.use(Harness())
        with self.assertRaises(PrintError) as caught:
            ibp_printing.print_image(self.img, PM_NAME)
        self.assertEqual(caught.exception.reason, "disabled")
        self.assertEqual(harness.opened, [])

    def test_backend_argument(self):
        harness = Harness(enabled=False)
        self.assertEqual(
            [c.name for c in harness.backend.discover().candidates], [DYMO_QUEUE]
        )

    def test_mode_is_logged_at_configure_time(self):
        os.environ[direct_config.ENV_VAR] = "off"
        package = logging.getLogger(ibp_log.LOGGER_NAME)
        added: list[logging.Handler] = []
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(ibp_log, "_CONFIGURED_DIRS", set()),
        ):
            try:
                with self.assertLogs(package, logging.INFO) as logs:
                    inside = list(package.handlers)
                    ibp_log.configure_logging(
                        Path(tmp), app="direct-test", console=False
                    )
                    added = [h for h in package.handlers if h not in inside]
            finally:
                for handler in added:
                    package.removeHandler(handler)
                    handler.close()
        text = "\n".join(logs.output)
        self.assertIn("direct USB printing DISABLED", text)
        self.assertIn("direct.config", text)


if __name__ == "__main__":
    unittest.main()
