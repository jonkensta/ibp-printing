"""Tests for the label watcher (detection, stability, filing, config, end to end).

Recovery behaviour (state file, startup catch-up, shutdown, retries) is tested
in test_watcher_recovery.py, which reuses the helpers defined here.
"""

# pylint: disable=missing-function-docstring,protected-access

import argparse
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Optional
from unittest import mock

from PIL import Image

import ibp_printing
from ibp_printing.backends import PrinterBackend, PrintError
from ibp_printing.log import configure_logging
from ibp_printing.models import (
    Discovery,
    JobOutcome,
    PrinterCandidate,
    PrintQueue,
    PrintResult,
)
from ibp_printing.watcher import __main__ as watcher_main
from ibp_printing.watcher import notify
from ibp_printing.watcher.config import (
    WatcherConfig,
    config_from_mapping,
    default_config_path,
    load_config,
)
from ibp_printing.watcher.detect import (
    UnsupportedFormat,
    classify_shape,
    is_temp_name,
    load_label,
    matches_globs,
    sha256_file,
    wait_until_stable,
)
from ibp_printing.paths import CHECK_PRINTER_DIR, PRINTED_DIR, TO_PRINT_DIR
from ibp_printing.watcher.instance import SingleInstance
from ibp_printing.watcher.notify import Notifier
from ibp_printing.watcher.service import LabelWatcher
from ibp_printing.watcher.state import StateFile

_LOG_DIR = tempfile.mkdtemp(prefix="ibp-watcher-test-logs-")

# Texts of every message box the code under test tried to show. On Windows the
# real MessageBoxW would block forever on a headless machine, so tests never
# let it run (see setUpModule).
SHOWN_BOXES: list[str] = []


def fake_message_box(text: str, title: str = notify.TITLE, flags: int = 0) -> int:
    """Stands in for notify.message_box: records the text, shows nothing."""
    del title, flags
    SHOWN_BOXES.append(text)
    return 1  # IDOK


def setUpModule() -> None:  # pylint: disable=invalid-name
    """Log to a temp folder and replace every real message box with a fake.

    test_watcher_recovery and test_watcher_reservations call this too.
    """
    configure_logging(Path(_LOG_DIR), console=False)
    for target in (notify, watcher_main):
        patcher = mock.patch.object(target, "message_box", fake_message_box)
        patcher.start()
        unittest.addModuleCleanup(patcher.stop)


def tearDownModule() -> None:  # pylint: disable=invalid-name
    ibp_printing.set_backend(None)


def make_label(path: Path, size: tuple[int, int] = (1200, 1800), seed: int = 0) -> Path:
    """Write a PNG of the given size; ``seed`` changes the content (and hash)."""
    img = Image.new("L", size, 255)
    img.putpixel((seed % size[0], 0), 0)
    img.save(path, format="PNG")
    return path


class FakeBackend(PrinterBackend):
    """One always-present printer whose behaviour each test chooses."""

    platform_name = "fake"

    def __init__(
        self,
        outcome: JobOutcome = JobOutcome.COMPLETED,
        raise_error: Optional[str] = None,
        printers: int = 1,
        raise_exc: Optional[BaseException] = None,
    ) -> None:
        self.outcome = outcome
        self.raise_error = raise_error
        self.raise_exc = raise_exc
        self.printers = printers
        self.printed: list[dict[str, Any]] = []
        self.attempts = 0
        self.discoveries = 0
        self.gate: Optional[threading.Event] = None
        self.started = threading.Event()

    def discover(self) -> Discovery:
        self.discoveries += 1
        queues = [PrintQueue(name=f"Fake Label {i}") for i in range(self.printers)]
        return Discovery(
            queues=queues,
            candidates=[
                PrinterCandidate(queue, None, (), usb_matching=False)
                for queue in queues
            ],
        )

    def get_default_printer(self) -> Optional[str]:
        return None

    def print_image(
        self,
        img: Image.Image,
        printer_name: str,
        *,
        job_name: str,
        track_timeout_s: float = 0.0,
    ) -> PrintResult:
        self.attempts += 1
        self.started.set()
        if self.gate is not None:
            self.gate.wait(10)
        if self.raise_error:
            raise PrintError(self.raise_error)
        if self.raise_exc is not None:
            raise self.raise_exc
        self.printed.append(
            {
                "printer": printer_name,
                "job_name": job_name,
                "size": img.size,
                "track_timeout_s": track_timeout_s,
            }
        )
        return PrintResult(printer_name, job_name, job_id=7, outcome=self.outcome)


class RecordingNotifier(Notifier):
    """Collects notification texts instead of showing boxes."""

    def __init__(self) -> None:
        super().__init__(enabled=True, show=lambda text, title: None)
        self.texts: list[str] = []

    def notify(self, text: str, title: str = "") -> bool:
        self.texts.append(text)
        return True


class TempDirTest(unittest.TestCase):
    """Gives each test its own watch folder."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="ibp-watcher-test-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.state_dir = Path(tempfile.mkdtemp(prefix="ibp-watcher-state-"))
        self.addCleanup(shutil.rmtree, self.state_dir, True)
        self.addCleanup(ibp_printing.set_backend, None)

    def make_watcher(
        self, state: Optional[StateFile] = None, **overrides: Any
    ) -> tuple[LabelWatcher, RecordingNotifier]:
        config = WatcherConfig(stable_seconds=0.05, stable_timeout_s=5.0)
        for key, value in overrides.items():
            setattr(config, key, value)
        notifier = RecordingNotifier()
        watcher = LabelWatcher(config, self.dir, notifier=notifier, state=state)
        return watcher, notifier

    def state_file(self) -> StateFile:
        return StateFile(self.state_dir / "watcher-state.json")

    def queue_label(self, name: str, seed: int = 0, age_s: float = 0.0) -> Path:
        """Put a label into to-print/ the way save_for_retry does."""
        folder = self.dir / TO_PRINT_DIR
        folder.mkdir(exist_ok=True)
        path = make_label(folder / name, seed=seed)
        if age_s:
            when = time.time() - age_s
            os.utime(path, (when, when))
        return path


class DetectionTests(TempDirTest):
    """Tests for Detection."""

    def test_temp_names(self) -> None:
        for name in (
            "label.png.crdownload",
            "Unconfirmed 123.crdownload",
            "label.png.part",
            "abc.tmp",
            ".hidden.png",
            "~$doc.pdf",
        ):
            self.assertTrue(is_temp_name(Path(name)), name)
        for name in ("label.png", "label (1).PNG", "x.pdf"):
            self.assertFalse(is_temp_name(Path(name)), name)

    def test_globs_case_insensitive(self) -> None:
        globs = WatcherConfig().globs
        self.assertEqual(matches_globs(Path("LABEL.PNG"), globs), "*.png")
        self.assertEqual(matches_globs(Path("a.Pdf"), globs), "*.pdf")
        self.assertIsNone(matches_globs(Path("notes.txt"), globs))
        self.assertIsNone(matches_globs(Path("label.zpl"), globs))

    def test_shape(self) -> None:
        self.assertTrue(classify_shape((1200, 1800)).is_label)
        self.assertTrue(classify_shape((1800, 1200)).is_label)
        self.assertTrue(classify_shape((800, 1200)).is_label)
        square = classify_shape((1000, 1000))
        self.assertFalse(square.is_label)
        self.assertIn("outside", square.reasons[0])
        small = classify_shape((200, 300))
        self.assertFalse(small.is_label)
        self.assertIn("< 400px", small.reasons[1])
        self.assertFalse(classify_shape((1700, 2200)).is_label)  # letter page
        self.assertFalse(classify_shape((0, 0)).is_label)
        self.assertTrue(
            classify_shape((1000, 1700), aspect_min=1.6, aspect_max=1.8).is_label
        )

    def test_load_png(self) -> None:
        loaded = load_label(make_label(self.dir / "a.png"))
        self.assertEqual(loaded.image.size, (1200, 1800))
        self.assertEqual(loaded.source_format, "PNG")

    def test_load_pdf_first_page_at_300dpi(self) -> None:
        pdf = self.dir / "label.pdf"
        pages = [Image.new("L", (600, 900), 255), Image.new("L", (600, 900), 0)]
        pages[0].save(pdf, format="PDF", resolution=150.0, save_all=True,
                      append_images=pages[1:])  # fmt: skip
        loaded = load_label(pdf, pdf_dpi=300)
        self.assertEqual(loaded.source_format, "PDF")
        self.assertEqual(loaded.info["pages"], 2)
        width, height = loaded.image.size  # PDFium may round up by a pixel
        self.assertLessEqual(abs(width - 1200), 2)
        self.assertLessEqual(abs(height - 1800), 2)

    def test_pdf_without_pypdfium2(self) -> None:
        pdf = self.dir / "label.pdf"
        Image.new("L", (600, 900), 255).save(pdf, format="PDF", resolution=150.0)
        with mock.patch.dict(sys.modules, {"pypdfium2": None}):
            with self.assertRaises(UnsupportedFormat):
                load_label(pdf)

    def test_raw_and_corrupt(self) -> None:
        zpl = self.dir / "label.zpl"
        zpl.write_text("^XA^XZ")
        with self.assertRaises(UnsupportedFormat):
            load_label(zpl)
        bad = self.dir / "bad.png"
        bad.write_bytes(b"not a png")
        with self.assertRaises(UnsupportedFormat):
            load_label(bad)


class FakeClock:
    """A clock that only moves when ``sleep`` is called."""

    def __init__(self) -> None:
        self.now = 0.0
        self.on_sleep: list[Any] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        for hook in self.on_sleep:
            hook(self.now)


class StabilityTests(TempDirTest):
    """Tests for Stability."""

    def test_stable_file(self) -> None:
        path = make_label(self.dir / "a.png")
        clock = FakeClock()
        result = wait_until_stable(path, clock=clock, sleep=clock.sleep)
        self.assertTrue(result.ok)
        self.assertEqual(result.size, path.stat().st_size)
        self.assertGreaterEqual(result.waited_s, 1.0)
        self.assertLess(result.waited_s, 2.0)

    def test_growing_file_waits(self) -> None:
        path = self.dir / "a.png"
        path.write_bytes(b"x")
        clock = FakeClock()

        def grow(now: float) -> None:
            if now < 3.0:
                with path.open("ab") as handle:
                    handle.write(b"x")

        clock.on_sleep.append(grow)
        result = wait_until_stable(path, clock=clock, sleep=clock.sleep)
        self.assertTrue(result.ok)
        self.assertGreaterEqual(result.waited_s, 3.5)  # last write at 2.75s

    def test_empty_file_times_out(self) -> None:
        path = self.dir / "a.png"
        path.write_bytes(b"")
        clock = FakeClock()
        result = wait_until_stable(path, timeout_s=5, clock=clock, sleep=clock.sleep)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "file is empty")

    def test_disappearing_file(self) -> None:
        path = make_label(self.dir / "a.png")
        clock = FakeClock()
        clock.on_sleep.append(lambda now: path.unlink(missing_ok=True))
        result = wait_until_stable(path, clock=clock, sleep=clock.sleep)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "file disappeared")


class ConfigTests(TempDirTest):
    """Tests for Config."""

    def test_defaults(self) -> None:
        config = WatcherConfig()
        self.assertEqual((config.aspect_min, config.aspect_max), (1.4, 1.6))
        self.assertEqual(config.min_short_side_px, 400)
        self.assertEqual(config.duplicate_window_hours, 24)
        self.assertEqual(config.track_timeout_s, 60)
        self.assertEqual(config.heartbeat_minutes, 15)
        self.assertEqual(config.retry_seconds, 60)
        self.assertTrue(config.notify_on_failure)

    def test_load_toml(self) -> None:
        path = self.dir / "watcher.toml"
        path.write_text(
            'watch_dir = "~/Labels"\n'
            "duplicate_window_hours = 5\n"
            "dedupe_seconds = 60\n"
            "notify_on_failure = false\n"
            'globs = ["*.png"]\n'
            'track_timeout_s = "soon"\n'
            "bogus = 1\n",
            encoding="utf-8",
        )
        config, used, warnings = load_config(path)
        self.assertEqual(used, path)
        self.assertEqual(config.watch_dir, Path.home() / "Labels")
        self.assertEqual(config.duplicate_window_hours, 5.0)
        self.assertIsInstance(config.duplicate_window_hours, float)
        self.assertFalse(config.notify_on_failure)
        self.assertEqual(config.globs, ["*.png"])
        self.assertEqual(config.track_timeout_s, 60.0)
        self.assertEqual(len(warnings), 3)
        self.assertTrue(any("bogus" in warning for warning in warnings))
        self.assertTrue(any("no longer used" in warning for warning in warnings))
        self.assertTrue(any("track_timeout_s" in warning for warning in warnings))

    def test_missing_and_broken_files(self) -> None:
        config, _, warnings = load_config(self.dir / "nope.toml")
        self.assertEqual(config, WatcherConfig())
        self.assertEqual(warnings, [])
        broken = self.dir / "broken.toml"
        broken.write_text("this is = = not toml")
        config, _, warnings = load_config(broken)
        self.assertEqual(config, WatcherConfig())
        self.assertEqual(len(warnings), 1)

    def test_bad_aspect_range(self) -> None:
        config, warnings = config_from_mapping({"aspect_min": 2.0})
        self.assertEqual((config.aspect_min, config.aspect_max), (1.4, 1.6))
        self.assertEqual(len(warnings), 1)

    @unittest.skipIf(sys.platform == "win32", "XDG path is for non-Windows")
    def test_default_path_xdg(self) -> None:
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.dir)}):
            self.assertEqual(
                default_config_path(), self.dir / "ibp-printing" / "watcher.toml"
            )

    def test_cli_overrides(self) -> None:
        args = watcher_main.build_parser().parse_args(
            ["--watch-dir", str(self.dir), "--dry-run", "--process-existing"]
        )
        config = WatcherConfig()
        changed = watcher_main.apply_overrides(config, args)
        self.assertEqual(config.watch_dir, self.dir)
        self.assertTrue(config.dry_run)
        self.assertTrue(config.process_existing)
        self.assertIsNone(config.log_dir)
        self.assertEqual(set(changed), {"watch_dir", "dry_run", "process_existing"})
        self.assertIsInstance(args, argparse.Namespace)


class ProcessingTests(TempDirTest):
    """Tests for Processing."""

    def test_prints_and_moves_to_printed(self) -> None:
        backend = FakeBackend()
        ibp_printing.set_backend(backend)
        watcher, notifier = self.make_watcher(track_timeout_s=12.0)
        path = make_label(self.dir / "label.png")
        outcome = watcher.process_path(path)
        self.assertEqual(outcome.status, "printed")
        self.assertFalse(path.exists())
        assert outcome.moved_to is not None
        self.assertEqual(outcome.moved_to.parent, self.dir / PRINTED_DIR)
        self.assertRegex(outcome.moved_to.name, r"^\d{8}-\d{6}_label\.png$")
        self.assertEqual(len(backend.printed), 1)
        self.assertTrue(backend.printed[0]["job_name"].startswith("EasyPost label.png"))
        self.assertEqual(backend.printed[0]["track_timeout_s"], 12.0)
        self.assertEqual(notifier.texts, [])

    def test_spooled_but_not_ok_goes_to_check_printer(self) -> None:
        for job_outcome in (
            JobOutcome.ERROR,
            JobOutcome.TIMEOUT,
            JobOutcome.UNCERTAIN,
            JobOutcome.TRACKING_FAILED,
        ):
            with self.subTest(outcome=job_outcome):
                backend = FakeBackend(outcome=job_outcome)
                ibp_printing.set_backend(backend)
                watcher, notifier = self.make_watcher()
                outcome = watcher.process_path(
                    make_label(self.dir / "label.png", seed=hash(job_outcome) % 99)
                )
                self.assertEqual(outcome.status, "check_printer")
                assert outcome.moved_to is not None
                self.assertEqual(outcome.moved_to.parent, self.dir / CHECK_PRINTER_DIR)
                self.assertEqual(len(notifier.texts), 1)
                self.assertIn("may NOT have printed", notifier.texts[0])
                self.assertIn("BEFORE printing it again", notifier.texts[0])
                self.assertIn(str(outcome.moved_to), notifier.texts[0])
                self.assertIn(job_outcome.value, outcome.detail)
                self.assertFalse((self.dir / TO_PRINT_DIR).exists())

    def test_unexpected_print_exception_is_uncertain(self) -> None:
        ibp_printing.set_backend(FakeBackend(raise_exc=OSError("boom")))
        watcher, notifier = self.make_watcher()
        outcome = watcher.process_path(make_label(self.dir / "label.png"))
        self.assertEqual(outcome.status, "check_printer")
        assert outcome.moved_to is not None
        self.assertEqual(outcome.moved_to.parent.name, CHECK_PRINTER_DIR)
        self.assertEqual(len(notifier.texts), 1)

    def test_print_error_and_no_printer_go_to_to_print(self) -> None:
        for backend in (
            FakeBackend(raise_error="spool failed"),
            FakeBackend(printers=0),
        ):
            ibp_printing.set_backend(backend)
            watcher, notifier = self.make_watcher()
            outcome = watcher.process_path(make_label(self.dir / "label.png"))
            self.assertEqual(outcome.status, "to_print")
            assert outcome.moved_to is not None
            self.assertEqual(outcome.moved_to.parent.name, TO_PRINT_DIR)
            self.assertEqual(len(notifier.texts), 1)
            self.assertIn("did NOT print", notifier.texts[0])
            self.assertIn("print automatically", notifier.texts[0])
            self.assertIn(str(outcome.moved_to), notifier.texts[0])

    def test_print_error_when_move_fails_tells_volunteer(self) -> None:
        ibp_printing.set_backend(FakeBackend(printers=0))
        watcher, notifier = self.make_watcher()
        watcher._sleep = lambda seconds: None
        path = make_label(self.dir / "label.png")
        with mock.patch(
            "ibp_printing.watcher.core.os.rename",
            side_effect=PermissionError(13, "in use"),
        ):
            outcome = watcher.process_path(path)
        self.assertEqual(outcome.status, "to_print")
        self.assertIsNone(outcome.moved_to)
        self.assertIn("could not be moved", notifier.texts[0])
        self.assertIn(str(self.dir / TO_PRINT_DIR), notifier.texts[0])

    def test_notify_disabled(self) -> None:
        ibp_printing.set_backend(FakeBackend(printers=0))
        watcher, notifier = self.make_watcher(notify_on_failure=False)
        watcher.process_path(make_label(self.dir / "label.png"))
        self.assertEqual(notifier.texts, [])

    def test_failed_print_is_retried_from_to_print(self) -> None:
        backend = FakeBackend(printers=0)
        ibp_printing.set_backend(backend)
        watcher, _ = self.make_watcher()
        first = watcher.process_path(make_label(self.dir / "label.png"))
        self.assertEqual(first.status, "to_print")
        self.assertEqual(watcher.retry_queue_once("test"), [])  # still no printer
        backend.printers = 1
        outcomes = watcher.retry_queue_once("test")
        self.assertEqual([o.status for o in outcomes], ["printed"])
        assert outcomes[0].moved_to is not None
        self.assertEqual(outcomes[0].moved_to.parent.name, PRINTED_DIR)
        self.assertEqual(list((self.dir / TO_PRINT_DIR).iterdir()), [])
        self.assertTrue(backend.printed[0]["job_name"].startswith("Queued "))

    def test_content_dedupe(self) -> None:
        backend = FakeBackend()
        ibp_printing.set_backend(backend)
        watcher, notifier = self.make_watcher()
        make_label(self.dir / "label.png")
        shutil.copy(self.dir / "label.png", self.dir / "label (1).png")
        shutil.copy(self.dir / "label.png", self.dir / "REPRINT label.png")
        self.assertEqual(watcher.process_path(self.dir / "label.png").status, "printed")
        with self.assertLogs("ibp_printing.watcher", logging.WARNING) as logs:
            dup = watcher.process_path(self.dir / "label (1).png")
        self.assertEqual(dup.status, "duplicate")
        self.assertTrue(any("DUPLICATE not printed" in line for line in logs.output))
        self.assertTrue(any("REPRINT" in line for line in logs.output))
        assert dup.moved_to is not None
        self.assertEqual(dup.moved_to.parent, self.dir / PRINTED_DIR)
        self.assertTrue(dup.moved_to.name.startswith("duplicate_"))
        self.assertIn("already printed", notifier.texts[-1])
        self.assertIn("REPRINT", notifier.texts[-1])
        self.assertEqual(len(backend.printed), 1)
        # A deliberate reprint: the name starts with REPRINT.
        again = watcher.process_path(self.dir / "REPRINT label.png")
        self.assertEqual(again.status, "printed")
        self.assertEqual(len(backend.printed), 2)

    def test_dry_run_prints_and_moves_nothing(self) -> None:
        backend = FakeBackend()
        ibp_printing.set_backend(backend)
        watcher, _ = self.make_watcher(dry_run=True)
        path = make_label(self.dir / "label.png")
        self.assertEqual(watcher.process_path(path).status, "dry_run")
        self.assertTrue(path.exists())
        self.assertEqual(backend.printed, [])
        self.assertFalse((self.dir / PRINTED_DIR).exists())
        # A later modified event for the unchanged file is not re-decided.
        self.assertEqual(watcher.process_path(path).status, "unchanged")

    def test_non_labels_left_alone(self) -> None:
        backend = FakeBackend()
        ibp_printing.set_backend(backend)
        watcher, _ = self.make_watcher()
        photo = make_label(self.dir / "photo.png", size=(1600, 1200))
        tiny = make_label(self.dir / "icon.png", size=(40, 60))
        notes = self.dir / "notes.txt"
        notes.write_text("hello")
        zpl = self.dir / "label.zpl"
        zpl.write_text("^XA^XZ")
        self.assertEqual(watcher.process_path(photo).status, "not_label")
        self.assertEqual(watcher.process_path(tiny).status, "not_label")
        self.assertEqual(watcher.process_path(notes).status, "not_matching")
        self.assertEqual(watcher.process_path(zpl).status, "unsupported")
        for path in (photo, tiny, notes, zpl):
            self.assertTrue(path.exists())
        self.assertEqual(backend.printed, [])

    def test_temp_extension_events_ignored(self) -> None:
        watcher, _ = self.make_watcher()
        for name in ("label.png.crdownload", "label.png.part", "x.tmp"):
            (self.dir / name).write_bytes(b"x")
            watcher.on_fs_event("created", str(self.dir / name))
        sub = self.dir / "printed"
        sub.mkdir()
        watcher.on_fs_event("moved", str(sub / "label.png"))
        self.assertEqual(watcher._queue.qsize(), 0)
        watcher.on_fs_event("moved", str(self.dir / "label.png"))
        watcher.on_fs_event("modified", str(self.dir / "label.png"))
        self.assertEqual(watcher._queue.qsize(), 1)  # de-duped while pending

    def test_existing_files_ignored_until_changed(self) -> None:
        watcher, _ = self.make_watcher()
        path = make_label(self.dir / "old.png")
        watcher.snapshot_existing()
        watcher.on_fs_event("modified", str(path))
        self.assertEqual(watcher._queue.qsize(), 0)
        make_label(path, size=(1000, 1500))
        watcher.on_fs_event("modified", str(path))
        self.assertEqual(watcher._queue.qsize(), 1)

    def test_move_retries_then_gives_up_and_remembers(self) -> None:
        backend = FakeBackend()
        ibp_printing.set_backend(backend)
        watcher, _ = self.make_watcher()
        watcher._sleep = lambda seconds: None
        path = make_label(self.dir / "label.png")
        with mock.patch(
            "ibp_printing.watcher.core.os.rename",
            side_effect=PermissionError(13, "in use"),
        ) as rename:
            outcome = watcher.process_path(path)
        self.assertEqual(rename.call_count, 5)
        self.assertEqual(outcome.status, "printed")
        self.assertIsNone(outcome.moved_to)
        self.assertTrue(path.exists())
        # Seen again: the move is retried, the label is not printed again.
        again = watcher.process_path(path)
        self.assertEqual(again.status, "filed")
        assert again.moved_to is not None
        self.assertEqual(again.moved_to.parent, self.dir / PRINTED_DIR)
        self.assertEqual(len(backend.printed), 1)

    def test_move_retry_succeeds(self) -> None:
        ibp_printing.set_backend(FakeBackend())
        watcher, _ = self.make_watcher()
        watcher._sleep = lambda seconds: None
        real_rename = os.rename
        calls = []

        def flaky(src: Any, dst: Any) -> None:
            calls.append(dst)
            if len(calls) < 3:
                raise PermissionError(13, "in use")
            real_rename(src, dst)

        with mock.patch("ibp_printing.watcher.core.os.rename", side_effect=flaky):
            outcome = watcher.process_path(make_label(self.dir / "label.png"))
        assert outcome.moved_to is not None
        self.assertTrue(outcome.moved_to.exists())
        self.assertEqual(len(calls), 3)

    def test_name_collisions(self) -> None:
        watcher, _ = self.make_watcher()
        first = watcher.move_to(make_label(self.dir / "a.png"), PRINTED_DIR)
        second = watcher.move_to(make_label(self.dir / "a.png", seed=3), PRINTED_DIR)
        assert first is not None and second is not None
        self.assertNotEqual(first, second)
        self.assertTrue(first.exists() and second.exists())

    def test_sha256(self) -> None:
        path = self.dir / "x"
        path.write_bytes(b"abc")
        self.assertEqual(
            sha256_file(path),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        )


class NotifierTests(unittest.TestCase):
    """Tests for Notifier."""

    def test_one_box_at_a_time_and_queued_messages_coalesce(self) -> None:
        release = threading.Event()
        shown: list[str] = []

        def show(text: str, title: str) -> int:
            del title
            shown.append(text)
            release.wait(5)
            return 1

        notifier = Notifier(enabled=True, show=show)
        self.assertTrue(notifier.notify("first"))
        deadline = time.monotonic() + 5
        while not shown:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        # Accepted (queued), not dropped, while the first box is open.
        self.assertTrue(notifier.notify("second"))
        self.assertTrue(notifier.notify("third"))
        self.assertEqual(notifier.pending, 2)
        self.assertEqual(shown, ["first"])
        release.set()
        while len(shown) < 2:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        self.assertEqual(len(shown), 2)  # one combined box, not two
        self.assertIn("second", shown[1])
        self.assertIn("third", shown[1])
        self.assertIn("2 more messages", shown[1])
        while notifier._busy:  # the thread finishes; a new box starts at once
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        self.assertTrue(notifier.notify("fourth"))
        while len(shown) < 3:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        self.assertEqual(shown[2], "fourth")

    def test_pending_messages_are_bounded(self) -> None:
        release = threading.Event()
        shown: list[str] = []

        def show(text: str, title: str) -> int:
            del title
            shown.append(text)
            release.wait(5)
            return 1

        notifier = Notifier(enabled=True, show=show)
        notifier.notify("open")
        for number in range(notify.MAX_PENDING + 5):
            self.assertTrue(notifier.notify(f"msg {number}"))
        self.assertEqual(notifier.pending, notify.MAX_PENDING)
        release.set()
        deadline = time.monotonic() + 5
        while len(shown) < 2:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        self.assertIn("and 5 older messages", shown[1])
        self.assertNotIn("msg 0", shown[1])

    def test_disabled(self) -> None:
        shown: list[str] = []
        notifier = Notifier(enabled=False, show=lambda t, _: shown.append(t))
        self.assertFalse(notifier.notify("x"))
        self.assertEqual(shown, [])

    @unittest.skipIf(sys.platform == "win32", "checks the non-Windows no-op")
    def test_default_is_noop_off_windows(self) -> None:
        self.assertFalse(Notifier(enabled=True).notify("x"))


class InstanceTests(TempDirTest):
    """Tests for Instance."""

    def test_second_instance_refused(self) -> None:
        lock = self.dir / "watcher.lock"
        first = SingleInstance(lock)
        self.assertTrue(first.acquire())
        self.assertFalse(SingleInstance(lock).acquire())
        first.release()
        # The holder's pid was written despite the byte-range lock (Windows).
        self.assertEqual(lock.read_text("utf-8"), f"pid={os.getpid()}\n")
        again = SingleInstance(lock)
        self.assertTrue(again.acquire())
        again.release()


def quiet_configure(log_dir: Optional[Path] = None, **kwargs: Any) -> Path:
    """configure_logging without the console handler (keeps test output clean)."""
    assert kwargs.get("app") == "watcher", kwargs
    return configure_logging(log_dir, app="watcher", console=False)


class EndToEndTests(TempDirTest):
    """Tests for EndToEnd."""

    def test_running_watcher_prints_dropped_label(self) -> None:
        backend = FakeBackend()
        ibp_printing.set_backend(backend)
        watcher, _ = self.make_watcher()
        make_label(self.dir / "already-here.png", seed=1)
        watcher.start()
        self.addCleanup(watcher.stop)

        # Simulate Chrome: write a .crdownload, then rename to the final name.
        partial = self.dir / "label-abc.png.crdownload"
        make_label(self.dir / "staging.png", seed=2).rename(partial)
        partial.rename(self.dir / "label-abc.png")

        printed_dir = self.dir / PRINTED_DIR
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if printed_dir.exists() and any(printed_dir.iterdir()):
                break
            time.sleep(0.05)
        self.assertTrue(watcher.wait_idle(10))
        moved = [path.name for path in printed_dir.iterdir()]
        self.assertEqual(len(moved), 1, moved)
        self.assertTrue(moved[0].endswith("_label-abc.png"))
        self.assertEqual(len(backend.printed), 1)
        self.assertEqual(backend.printed[0]["size"], (1200, 1800))
        self.assertTrue((self.dir / "already-here.png").exists())
        self.assertTrue(watcher.observer_alive())
        watcher.stop()
        self.assertFalse(watcher.observer_alive())

    def test_main_once_dry_run(self) -> None:
        backend = FakeBackend()
        ibp_printing.set_backend(backend)
        logs = self.dir / "state" / "logs"
        watch = self.dir / "downloads"
        watch.mkdir()
        label = make_label(watch / "label.png")
        (watch / TO_PRINT_DIR).mkdir()
        queued = make_label(watch / TO_PRINT_DIR / "x.png", seed=5)
        state = self.dir / "state"

        with (
            mock.patch.object(watcher_main, "configure_logging", quiet_configure),
            mock.patch.object(watcher_main, "default_state_dir", lambda: state),
        ):
            code = watcher_main.main(
                [
                    "--config",
                    str(self.dir / "missing.toml"),
                    "--watch-dir",
                    str(watch),
                    "--log-dir",
                    str(logs),
                    "--once",
                    "--dry-run",
                ]
            )
        self.assertEqual(code, 0)
        self.assertTrue(label.exists())
        self.assertTrue(queued.exists())
        self.assertEqual(backend.printed, [])
        for handler in logging.getLogger("ibp_printing").handlers:
            handler.flush()
        text = (logs / "printer-watcher.log").read_text(encoding="utf-8")
        self.assertIn("DRY RUN: would print", text)
        self.assertIn("x.png", text)  # the to-print queue is handled too
        self.assertIn("effective config", text)
        self.assertTrue((state / "watcher.lock").exists())
        self.assertFalse((state / "watcher-state.json").exists())  # dry run
        self.assertFalse((self.dir / "state" / "logs" / "watcher.lock").exists())

    def test_main_refuses_second_instance_whatever_the_log_dir(self) -> None:
        state = self.dir / "state"
        holder = SingleInstance(state / "watcher.lock")
        self.assertTrue(holder.acquire())
        self.addCleanup(holder.release)
        configure = mock.Mock(side_effect=quiet_configure)
        for logs in (self.dir / "logs-a", self.dir / "logs-b"):
            with (
                mock.patch.object(watcher_main, "configure_logging", configure),
                mock.patch.object(watcher_main, "default_state_dir", lambda: state),
                mock.patch.object(watcher_main, "_has_console", lambda: False),
            ):
                code = watcher_main.main(
                    ["--config", str(self.dir / "none.toml"), "--log-dir", str(logs)]
                )
            self.assertEqual(code, watcher_main.EXIT_ALREADY_RUNNING)
            self.assertFalse(logs.exists())  # lock taken before logging opens files
        configure.assert_not_called()

    def test_no_console_refusal_shows_a_box_only_on_windows(self) -> None:
        state = self.dir / "state"
        holder = SingleInstance(state / "watcher.lock")
        self.assertTrue(holder.acquire())
        self.addCleanup(holder.release)
        del SHOWN_BOXES[:]
        with (
            mock.patch.object(watcher_main, "configure_logging", quiet_configure),
            mock.patch.object(watcher_main, "default_state_dir", lambda: state),
            mock.patch.object(watcher_main, "_has_console", lambda: False),
        ):
            code = watcher_main.main(["--config", str(self.dir / "none.toml")])
        self.assertEqual(code, watcher_main.EXIT_ALREADY_RUNNING)
        if sys.platform == "win32":
            self.assertEqual(len(SHOWN_BOXES), 1, SHOWN_BOXES)
            self.assertIn("already running", SHOWN_BOXES[0])
        else:
            self.assertEqual(SHOWN_BOXES, [])


if __name__ == "__main__":
    unittest.main()
