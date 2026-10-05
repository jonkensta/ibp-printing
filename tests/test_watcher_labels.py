"""The watcher and label metadata: sidecars, journal updates, "it printed" boxes."""

# pylint: disable=missing-function-docstring,protected-access

import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import ibp_printing
from ibp_printing import labels, paths
from ibp_printing.models import JobOutcome
from ibp_printing.paths import CHECK_PRINTER_DIR, PRINTED_DIR, TO_PRINT_DIR
from ibp_printing.watcher import messages, notify
from ibp_printing.watcher.notify import Notifier

from test_watcher import (  # pylint: disable=wrong-import-order
    FakeBackend,
    TempDirTest,
    make_label,
    setUpModule as _watcher_setup,
)

KEY = labels.recipient_key("Jane Doe", "1 Main St", "Huntsville", "TX", "77340")


def setUpModule() -> None:  # pylint: disable=invalid-name
    _watcher_setup()


def tearDownModule() -> None:  # pylint: disable=invalid-name
    ibp_printing.set_backend(None)


class LabelWatcherTest(TempDirTest):
    """A watcher with its own label journal."""

    def setUp(self) -> None:
        super().setUp()
        self.journal = self.state_dir / "labels.jsonl"
        labels.set_journal_path(self.journal)
        self.addCleanup(labels.set_journal_path, None)

    def queue_with_meta(  # pylint: disable=too-many-arguments
        self,
        name: str,
        tracking: str,
        seed: int,
        recipient: str = "Jane Doe",
        age_s: float = 0.0,
    ) -> Path:
        """A label in to-print/ with a sidecar, as save_for_retry(meta=...) leaves it."""
        path = self.queue_label(name, seed=seed, age_s=age_s)
        meta = {
            "tracking_code": tracking,
            "shipment_id": f"shp_{tracking}",
            "recipient_label": f"{recipient}, Huntsville TX",
            "recipient_key": KEY,
            "app": "shippy",
        }
        labels.record_purchase(
            recipient_key=KEY,
            recipient_label=meta["recipient_label"],
            tracking_code=tracking,
            shipment_id=meta["shipment_id"],
            app="shippy",
        )
        paths.write_sidecar(path, meta)
        labels.update_status_from_meta(meta, labels.QUEUED, file=str(path))
        return path

    def record(self, tracking: str) -> labels.LabelRecord:
        return labels._read_all()[tracking]

    def folder(self, name: str) -> list[str]:
        folder = self.dir / name
        return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


class SidecarTests(LabelWatcherTest):
    """Sidecars are never printed and move with their label."""

    def test_printed_label_takes_its_sidecar_and_updates_the_journal(self) -> None:
        backend = FakeBackend()
        ibp_printing.set_backend(backend)
        watcher, notifier = self.make_watcher()
        self.queue_with_meta("a.png", "TRK1", seed=1)
        outcomes = watcher.retry_queue_once("test")
        self.assertEqual([o.status for o in outcomes], ["printed"])
        self.assertEqual(len(backend.printed), 1)  # the PNG only, never the JSON
        (moved,) = [o.moved_to for o in outcomes]
        assert moved is not None
        self.assertEqual(
            self.folder(PRINTED_DIR), sorted([moved.name, moved.name + ".json"])
        )
        self.assertEqual(self.folder(TO_PRINT_DIR), [])
        record = self.record("TRK1")
        self.assertEqual((record.status, record.file), (labels.PRINTED, str(moved)))
        self.assertEqual(notifier.texts, [])  # no warning
        self.assertEqual(len(notifier.infos), 1)
        self.assertIn(
            "Label for Jane Doe, Huntsville TX (tracking TRK1) printed.",
            notifier.infos[0],
        )

    def test_json_in_to_print_is_never_printed_or_reported(self) -> None:
        backend = FakeBackend()
        ibp_printing.set_backend(backend)
        watcher, notifier = self.make_watcher()
        folder = self.dir / TO_PRINT_DIR
        folder.mkdir()
        (folder / "orphan.png.json").write_text("{}", encoding="utf-8")
        (folder / "notes.JSON").write_text("{}", encoding="utf-8")
        self.assertEqual(watcher.queued_files(), [])
        self.assertEqual(watcher.retry_queue_once("test"), [])
        self.assertEqual(backend.attempts, 0)
        self.assertEqual(notifier.texts + notifier.infos, [])
        self.assertEqual(self.folder(TO_PRINT_DIR), ["notes.JSON", "orphan.png.json"])

    def test_json_in_downloads_is_ignored(self) -> None:
        watcher, _ = self.make_watcher(globs=["*.png", "*.json"])
        path = self.dir / "label.png.json"
        path.write_text("{}", encoding="utf-8")
        self.assertEqual(watcher.process_path(path).status, "ignored")

    def test_check_printer_moves_sidecar_and_records_check_printer(self) -> None:
        ibp_printing.set_backend(FakeBackend(outcome=JobOutcome.UNCERTAIN))
        watcher, notifier = self.make_watcher()
        self.queue_with_meta("a.png", "TRK2", seed=2)
        (outcome,) = watcher.retry_queue_once("test")
        self.assertEqual(outcome.status, "check_printer")
        assert outcome.moved_to is not None
        self.assertTrue(paths.sidecar_path(outcome.moved_to).is_file())
        self.assertEqual(
            self.folder(CHECK_PRINTER_DIR),
            sorted([outcome.moved_to.name, outcome.moved_to.name + ".json"]),
        )
        record = self.record("TRK2")
        self.assertEqual(record.status, labels.CHECK_PRINTER)
        self.assertEqual(record.file, str(outcome.moved_to))
        self.assertEqual(len(notifier.texts), 1)  # the usual warning
        self.assertEqual(notifier.infos, [])

    def test_definite_failure_keeps_sidecar_and_queued_status(self) -> None:
        ibp_printing.set_backend(FakeBackend(raise_error="cover open"))
        watcher, notifier = self.make_watcher()
        path = self.queue_with_meta("a.png", "TRK3", seed=3)
        lines = len(self.journal.read_text(encoding="utf-8").splitlines())
        (outcome,) = watcher.retry_queue_once("test")
        self.assertEqual(outcome.status, "still_queued")
        self.assertTrue(paths.sidecar_path(path).is_file())
        record = self.record("TRK3")
        self.assertEqual((record.status, record.file), (labels.QUEUED, str(path)))
        # Nothing new to record, so nothing is appended on every retry tick.
        self.assertEqual(
            len(self.journal.read_text(encoding="utf-8").splitlines()), lines
        )
        self.assertEqual(notifier.infos, [])

    def test_duplicate_copy_takes_its_sidecar(self) -> None:
        ibp_printing.set_backend(FakeBackend())
        watcher, _ = self.make_watcher()
        watcher._sleep = lambda seconds: None
        self.queue_with_meta("a.png", "TRK4", seed=4)
        watcher.retry_queue_once("first")
        copy = self.queue_with_meta("copy.png", "TRK4", seed=4)  # same content
        (outcome,) = watcher.retry_queue_once("second")
        self.assertEqual(outcome.status, "duplicate")
        assert outcome.moved_to is not None
        self.assertTrue(outcome.moved_to.name.startswith("duplicate_"))
        self.assertTrue(paths.sidecar_path(outcome.moved_to).is_file())
        self.assertFalse(paths.sidecar_path(copy).exists())
        self.assertEqual(self.record("TRK4").status, labels.PRINTED)

    def test_label_without_sidecar_leaves_journal_alone(self) -> None:
        ibp_printing.set_backend(FakeBackend())
        watcher, notifier = self.make_watcher()
        self.queue_label("plain.png", seed=5)
        (outcome,) = watcher.retry_queue_once("test")
        self.assertEqual(outcome.status, "printed")
        self.assertFalse(self.journal.exists())
        self.assertEqual(notifier.infos, [])

    def test_sidecar_without_journal_record_creates_one(self) -> None:
        ibp_printing.set_backend(FakeBackend())
        watcher, _ = self.make_watcher()
        path = self.queue_label("a.png", seed=6)
        paths.write_sidecar(
            path,
            {"tracking_code": "TRK6", "recipient_key": KEY, "recipient_label": "Jo"},
        )
        watcher.retry_queue_once("test")
        record = self.record("TRK6")
        self.assertEqual((record.status, record.recipient_key), (labels.PRINTED, KEY))

    def test_broken_journal_never_blocks_printing(self) -> None:
        backend = FakeBackend()
        ibp_printing.set_backend(backend)
        watcher, _ = self.make_watcher()
        path = self.queue_label("a.png", seed=7)
        paths.write_sidecar(path, {"tracking_code": "TRK7"})
        blocker = self.state_dir / "file"
        blocker.write_text("x")
        labels.set_journal_path(blocker / "labels.jsonl")
        (outcome,) = watcher.retry_queue_once("test")
        self.assertEqual(outcome.status, "printed")
        self.assertEqual(len(backend.printed), 1)

    def test_downloaded_label_moved_to_to_print_keeps_sidecar(self) -> None:
        ibp_printing.set_backend(FakeBackend(raise_error="no printer"))
        watcher, _ = self.make_watcher()
        path = make_label(self.dir / "dl.png", seed=8)
        paths.write_sidecar(path, {"tracking_code": "TRK8", "recipient_key": KEY})
        outcome = watcher.process_path(path)
        self.assertEqual(outcome.status, "to_print")
        assert outcome.moved_to is not None
        self.assertTrue(paths.sidecar_path(outcome.moved_to).is_file())
        record = self.record("TRK8")
        self.assertEqual(record.status, labels.QUEUED)
        self.assertEqual(record.file, str(outcome.moved_to))


class PrintedNoticeTests(LabelWatcherTest):
    """One information box per to-print pass."""

    def test_several_labels_one_box(self) -> None:
        ibp_printing.set_backend(FakeBackend())
        watcher, notifier = self.make_watcher()
        self.queue_with_meta("a.png", "TRKA", seed=11, recipient="Ann", age_s=2)
        self.queue_with_meta("b.png", "TRKB", seed=12, recipient="Bob")
        self.queue_label("plain.png", seed=13)
        outcomes = watcher.retry_queue_once("test")
        self.assertEqual([o.status for o in outcomes], ["printed"] * 3)
        self.assertEqual(len(notifier.infos), 1)
        box = notifier.infos[0]
        self.assertIn("2 shipping labels", box)
        self.assertIn("Label for Ann, Huntsville TX (tracking TRKA) printed.", box)
        self.assertIn("Label for Bob, Huntsville TX (tracking TRKB) printed.", box)
        self.assertLess(box.index("TRKA"), box.index("TRKB"))
        # The next pass has nothing new to say.
        watcher.retry_queue_once("again")
        self.assertEqual(len(notifier.infos), 1)

    def test_notice_survives_a_later_failure_in_the_pass(self) -> None:
        backend = FakeBackend()
        ibp_printing.set_backend(backend)
        watcher, notifier = self.make_watcher()
        self.queue_with_meta("a.png", "TRKA", seed=21, age_s=2)
        self.queue_with_meta("b.png", "TRKB", seed=22)
        original = backend.print_image

        def second_fails(*args: Any, **kwargs: Any) -> Any:
            if backend.attempts >= 1:
                backend.attempts += 1
                raise ibp_printing.PrintError("jammed")
            return original(*args, **kwargs)

        backend.print_image = second_fails  # type: ignore[method-assign]
        statuses = [o.status for o in watcher.retry_queue_once("test")]
        self.assertEqual(statuses, ["printed", "still_queued"])
        self.assertEqual(len(notifier.infos), 1)
        self.assertIn("(tracking TRKA) printed.", notifier.infos[0])
        self.assertNotIn("TRKB", notifier.infos[0])

    def test_notice_respects_notify_on_failure_off(self) -> None:
        ibp_printing.set_backend(FakeBackend())
        watcher, notifier = self.make_watcher(notify_on_failure=False)
        self.queue_with_meta("a.png", "TRKC", seed=31)
        watcher.retry_queue_once("test")
        self.assertEqual(notifier.infos, [])

    def test_message_wording(self) -> None:
        one = messages.queued_printed([("Jane Doe, Huntsville TX", "9400")])
        self.assertTrue(
            one.startswith("Label for Jane Doe, Huntsville TX (tracking 9400) printed.")
        )
        self.assertIn("Do not buy postage", one)
        self.assertIn("an unknown recipient", messages.queued_printed([("", "1")]))


class InfoBoxTests(unittest.TestCase):
    """Notifier: info boxes use the information icon; mixed boxes warn."""

    def test_flags(self) -> None:
        shown: list[tuple[str, int]] = []
        release = threading.Event()

        def fake_box(text: str, title: str = "", flags: int = 0) -> int:
            del title
            shown.append((text, flags))
            release.wait(5)
            return 1

        with (
            mock.patch.object(notify, "message_box", fake_box),
            mock.patch.object(notify.sys, "platform", "win32"),
        ):
            notifier = Notifier(enabled=True)
            self.assertTrue(notifier.notify("good news", info=True))
            deadline = time.monotonic() + 5
            while not shown:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
            notifier.notify("more good news", info=True)
            notifier.notify("bad news")
            release.set()
            while len(shown) < 2:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
            while notifier._busy:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
            notifier.notify("only good", info=True)
            notifier.notify("x", info=True)  # may coalesce; either way info
            while notifier._busy:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
        self.assertEqual(shown[0], ("good news", notify.INFO_FLAGS))
        self.assertIn("bad news", shown[1][0])
        self.assertEqual(shown[1][1], notify.WARNING_FLAGS)  # mixed: warning
        self.assertTrue(all(flags == notify.INFO_FLAGS for _, flags in shown[2:]))

    def test_info_flags_are_not_system_modal(self) -> None:
        self.assertFalse(notify.INFO_FLAGS & notify.MB_SYSTEMMODAL)
        self.assertTrue(notify.INFO_FLAGS & notify.MB_ICONINFORMATION)


if __name__ == "__main__":
    unittest.main()
