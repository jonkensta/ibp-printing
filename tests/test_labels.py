"""Tests for the label journal (ibp_printing.labels) and label sidecars."""

# pylint: disable=missing-function-docstring,protected-access

import json
import logging
import multiprocessing
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Optional
from unittest import mock

from PIL import Image

import ibp_printing
from ibp_printing import labels, paths
from ibp_printing.log import configure_logging
from ibp_printing.labels import (
    CHECK_PRINTER,
    PRINTED,
    PURCHASED,
    QUEUED,
    REFUNDED,
    LabelRecord,
    LabelStatus,
    find_duplicates,
    pending_labels,
    recipient_key,
    record_purchase,
    update_status,
)

DAY = 86400.0
KEY = recipient_key("Jane Doe", "1 Main St", "Huntsville", "TX", "77340")
OTHER = recipient_key("John Roe", "2 Oak Ave", "Austin", "TX", "78701")


def buy(tracking: str, key: str = KEY, label: str = "Jane Doe, Huntsville TX"):
    return record_purchase(
        recipient_key=key,
        recipient_label=label,
        tracking_code=tracking,
        shipment_id=f"shp_{tracking}",
        app="shippy-test",
    )


_LOG_DIR = tempfile.mkdtemp(prefix="ibp-labels-test-logs-")


def setUpModule() -> None:  # pylint: disable=invalid-name
    configure_logging(Path(_LOG_DIR), console=False)


# ---------------------------------------------------------- process helpers
# Module-level so the "spawn" start method (Windows) can import them.


def _child_writer(journal: str, prefix: str, count: int, start: Any) -> None:
    labels.set_journal_path(Path(journal))
    labels.LOCK_TIMEOUT_S = 30.0
    start.wait(30)
    for number in range(count):
        tracking = f"{prefix}{number:03d}"
        buy(tracking)
        update_status(tracking, PRINTED if number % 2 else QUEUED)
        # Read under the lock too, so readers and writers interleave.
        find_duplicates(KEY)


def _child_holder(journal: str, holding: Any, release: Any) -> None:
    labels.set_journal_path(Path(journal))
    with labels._locked():
        holding.set()
        release.wait(30)


# -------------------------------------------------------------------- tests


class RecipientKeyTests(unittest.TestCase):
    """recipient_key normalization."""

    def same(self, first: tuple[str, ...], second: tuple[str, ...]) -> None:
        self.assertEqual(recipient_key(*first), recipient_key(*second))

    def test_case_punctuation_and_spacing(self) -> None:
        self.same(
            ("Jane  Q. Doe", "123 Main St.", "Huntsville", "TX", "77340"),
            ("jane q doe", "123  MAIN st", " huntsville ", "tx", "77340"),
        )

    def test_street_abbreviations(self) -> None:
        self.same(
            ("A", "12 North Elm Street", "X", "TX", "1"),
            ("A", "12 N Elm St", "X", "TX", "1"),
        )
        self.same(
            ("A", "5 Oak Avenue", "X", "TX", "1"), ("A", "5 oak ave.", "X", "TX", "1")
        )
        self.same(
            ("A", "9 Long Boulevard", "X", "TX", "1"),
            ("A", "9 long blvd", "X", "TX", "1"),
        )

    def test_apartment_unit_and_hash(self) -> None:
        base = ("A", "1 Main St Apt 5", "X", "TX", "1")
        for street in (
            "1 Main Street Apartment 5",
            "1 Main St #5",
            "1 Main St # 5",
            "1 Main St, Apt. #5",
            "1 Main St Unit 5",
            "1 Main St Suite 5",
        ):
            with self.subTest(street=street):
                self.same(base, ("A", street, "X", "TX", "1"))

    def test_po_box(self) -> None:
        for street in ("P.O. Box 4400", "PO Box 4400", "Post Office Box 4400"):
            with self.subTest(street=street):
                self.same(
                    ("A", "po box 4400", "X", "TX", "1"), ("A", street, "X", "TX", "1")
                )

    def test_zip_uses_first_five_digits(self) -> None:
        self.same(("A", "1", "X", "TX", "77340"), ("A", "1", "X", "TX", "77340-1234"))
        self.same(("A", "1", "X", "TX", "77340"), ("A", "1", "X", "TX", " 773401234 "))

    def test_state_names_and_cities(self) -> None:
        self.same(
            ("A", "1", "Fort Worth", "Texas", "1"), ("A", "1", "Ft. Worth", "TX", "1")
        )
        self.same(
            ("A", "1", "Saint Louis", "MO", "1"), ("A", "1", "St Louis", "mo", "1")
        )

    def test_accents_are_ignored(self) -> None:
        self.same(
            ("José Núñez", "1", "X", "TX", "1"), ("Jose Nunez", "1", "X", "TX", "1")
        )

    def test_different_people_or_places_differ(self) -> None:
        base = recipient_key("Jane Doe", "1 Main St", "Huntsville", "TX", "77340")
        self.assertNotEqual(
            base, recipient_key("John Doe", "1 Main St", "Huntsville", "TX", "77340")
        )
        self.assertNotEqual(
            base, recipient_key("Jane Doe", "2 Main St", "Huntsville", "TX", "77340")
        )
        self.assertNotEqual(
            base, recipient_key("Jane Doe", "1 Main St", "Huntsville", "TX", "77341")
        )

    def test_stable_text(self) -> None:
        self.assertEqual(
            recipient_key(
                "Jane Doe", "1 Main Street", "Huntsville", "Texas", "77340-1"
            ),
            "jane doe|1 main st|huntsville|tx|77340",
        )


class JournalTest(unittest.TestCase):
    """Each test gets its own journal and to-print folder."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="ibp-labels-test-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.journal = self.dir / "state" / "labels.jsonl"
        labels.set_journal_path(self.journal)
        self.addCleanup(labels.set_journal_path, None)
        self.downloads = self.dir / "Downloads"
        self.to_print = self.downloads / paths.TO_PRINT_DIR
        patcher = mock.patch.object(
            labels, "to_print_dir", lambda watch_dir=None: self.to_print
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.now = time.time()
        clock = mock.patch.object(labels, "_now", lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def lines(self) -> list[dict[str, Any]]:
        return [
            json.loads(line)
            for line in self.journal.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def make_file(self, name: str, folder: Optional[Path] = None) -> Path:
        folder = folder or self.to_print
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        Image.new("L", (40, 60), 255).save(path, format="PNG")
        return path


class RecordAndUpdateTests(JournalTest):
    """record_purchase / update_status."""

    def test_record_purchase(self) -> None:
        record = buy("TRK1")
        self.assertEqual(record.status, PURCHASED)
        self.assertEqual(record.status, LabelStatus.PURCHASED)
        self.assertEqual(record.created, self.now)
        self.assertIsNone(record.file)
        (line,) = self.lines()
        self.assertEqual(LabelRecord.from_json(line), record)

    def test_last_record_wins(self) -> None:
        buy("TRK1")
        self.now += 10
        update_status("TRK1", QUEUED, file=str(self.dir / "a.png"))
        self.now += 10
        update_status("TRK1", PRINTED)
        latest = labels._read_all()["TRK1"]
        self.assertEqual(latest.status, PRINTED)
        self.assertEqual(latest.file, str(self.dir / "a.png"))  # kept
        self.assertEqual(latest.recipient_label, "Jane Doe, Huntsville TX")
        self.assertEqual(latest.created + 20, latest.updated)
        self.assertEqual(len(self.lines()), 3)

    def test_unchanged_status_writes_nothing(self) -> None:
        buy("TRK1")
        update_status("TRK1", QUEUED, file="x.png")
        update_status("TRK1", QUEUED, file="x.png")
        update_status("TRK1", QUEUED)
        self.assertEqual(len(self.lines()), 2)

    def test_update_unknown_label_creates_minimal_record(self) -> None:
        update_status("NEW1", CHECK_PRINTER)
        record = labels._read_all()["NEW1"]
        self.assertEqual(record.status, CHECK_PRINTER)
        self.assertEqual(record.recipient_key, "")

    def test_update_from_meta_fills_in_unknown_label(self) -> None:
        meta = {
            "tracking_code": "M1",
            "shipment_id": "shp_M1",
            "recipient_label": "Jane",
            "recipient_key": KEY,
            "app": "shippy-gui",
            "created": self.now - 100,
        }
        labels.update_status_from_meta(meta, QUEUED, file="q.png")
        record = labels._read_all()["M1"]
        self.assertEqual(
            (record.recipient_key, record.app, record.created, record.file),
            (KEY, "shippy-gui", self.now - 100, "q.png"),
        )

    def test_bad_status_and_missing_tracking_are_ignored(self) -> None:
        with self.assertLogs("ibp_printing.labels", logging.ERROR):
            update_status("TRK1", "lost")
        with self.assertLogs("ibp_printing.labels", logging.ERROR):
            update_status("", PRINTED)
        self.assertFalse(self.journal.exists())

    def test_io_failure_never_raises(self) -> None:
        blocker = self.dir / "not-a-folder"
        blocker.write_text("x")
        labels.set_journal_path(blocker / "labels.jsonl")
        with self.assertLogs("ibp_printing.labels", logging.ERROR):
            record = buy("TRK1")
        self.assertEqual(record.tracking_code, "TRK1")
        with self.assertLogs("ibp_printing.labels", logging.ERROR):
            update_status("TRK1", PRINTED)
        with self.assertLogs("ibp_printing.labels", logging.WARNING) as logs:
            self.assertEqual(find_duplicates(KEY), [])
        self.assertTrue(any("unavailable" in line for line in logs.output))
        with self.assertLogs("ibp_printing.labels", logging.ERROR):
            self.assertEqual(pending_labels(), [])

    def test_lock_timeout_degrades(self) -> None:
        context = multiprocessing.get_context("spawn")
        holding, release = context.Event(), context.Event()
        child = context.Process(
            target=_child_holder, args=(str(self.journal), holding, release)
        )
        child.start()
        try:
            self.assertTrue(holding.wait(60), "child never took the lock")
            with mock.patch.object(labels, "LOCK_TIMEOUT_S", 0.2):
                started = time.monotonic()
                with self.assertLogs("ibp_printing.labels", logging.ERROR):
                    self.assertEqual(find_duplicates(KEY), [])
                with self.assertLogs("ibp_printing.labels", logging.ERROR):
                    buy("TRK1")
                self.assertLess(time.monotonic() - started, 5)
        finally:
            release.set()
            child.join(30)
        buy("TRK2")  # free again
        self.assertEqual([line["tracking_code"] for line in self.lines()], ["TRK2"])


class ConcurrencyTests(JournalTest):
    """Two processes writing and reading the journal at once."""

    def test_two_processes(self) -> None:
        context = multiprocessing.get_context("spawn")
        start = context.Event()
        count = 40
        children = [
            context.Process(
                target=_child_writer, args=(str(self.journal), prefix, count, start)
            )
            for prefix in ("A", "B")
        ]
        for child in children:
            child.start()
        start.set()
        for child in children:
            child.join(120)
            self.assertEqual(child.exitcode, 0)
        lines = self.journal.read_text(encoding="utf-8").splitlines()
        for line in lines:
            json.loads(line)  # no torn or glued lines
        latest = labels._read_all()
        self.assertEqual(len(latest), 2 * count)
        for prefix in ("A", "B"):
            for number in range(count):
                record = latest[f"{prefix}{number:03d}"]
                self.assertEqual(record.status, PRINTED if number % 2 else QUEUED)
        self.assertEqual(len(lines), 4 * count)


class CorruptionTests(JournalTest):
    """Corrupt and torn lines are logged and skipped."""

    def test_corrupt_lines_skipped(self) -> None:
        buy("TRK1")
        with open(self.journal, "a", encoding="utf-8") as handle:
            handle.write("not json\n[1, 2]\n{}\n")
            handle.write('{"tracking_code": "X", "status": "weird", "created": 1}\n')
        buy("TRK2")
        with self.assertLogs("ibp_printing.labels", logging.WARNING) as logs:
            latest = labels._read_all()
        self.assertEqual(sorted(latest), ["TRK1", "TRK2"])
        self.assertTrue(any("corrupt" in line for line in logs.output))

    def test_torn_last_line_is_not_glued_to_the_next(self) -> None:
        buy("TRK1")
        with open(self.journal, "a", encoding="utf-8") as handle:
            handle.write('{"tracking_code": "TORN", "sta')
        buy("TRK2")
        with self.assertLogs("ibp_printing.labels", logging.WARNING):
            self.assertEqual(sorted(labels._read_all()), ["TRK1", "TRK2"])

    def test_write_after_corruption_compacts(self) -> None:
        buy("TRK1")
        with open(self.journal, "a", encoding="utf-8") as handle:
            handle.write("garbage\n")
        with self.assertLogs("ibp_printing.labels", logging.WARNING):
            update_status("TRK1", PRINTED)
        self.assertNotIn("garbage", self.journal.read_text(encoding="utf-8"))
        self.assertEqual(labels._read_all()["TRK1"].status, PRINTED)


class PruneTests(JournalTest):
    """Old labels are dropped when the journal is compacted."""

    def test_prunes_old_records_keeps_waiting_files(self) -> None:
        queued_file = self.make_file("old-queued.png")
        self.now -= 40 * DAY
        buy("OLD")
        buy("OLDQ")
        update_status("OLDQ", QUEUED, file=str(queued_file))
        buy("OLDGONE")
        update_status("OLDGONE", QUEUED, file=str(self.dir / "deleted.png"))
        self.now += 40 * DAY
        buy("NEW")
        records = self.lines()
        self.assertEqual(records[0]["_meta"], "labels journal")
        self.assertEqual(
            sorted(line["tracking_code"] for line in records[1:]), ["NEW", "OLDQ"]
        )

    def test_recent_journal_is_not_rewritten(self) -> None:
        self.now -= 10 * DAY
        buy("A")
        self.now += 10 * DAY
        buy("B")
        self.assertNotIn("_meta", self.lines()[0])

    def test_compacts_at_most_once_a_day(self) -> None:
        self.now -= 40 * DAY
        buy("OLD")
        self.now += 40 * DAY
        buy("A")  # compacts
        self.assertEqual(len(self.lines()), 2)  # header + A
        update_status("A", PRINTED)
        update_status("A", REFUNDED)
        self.assertEqual(len(self.lines()), 4)  # appended, not compacted
        self.now += DAY + 1
        buy("B")  # a day later: compacted to the latest record per label
        self.assertEqual(
            [line.get("tracking_code") for line in self.lines()], [None, "A", "B"]
        )


class FindDuplicatesTests(JournalTest):
    """find_duplicates rules."""

    def found(self, **kwargs: Any) -> list[str]:
        return [record.tracking_code for record in find_duplicates(KEY, **kwargs)]

    def test_recent_purchase_and_print_within_window(self) -> None:
        buy("P1")
        self.now += 1
        buy("P2")
        update_status("P2", PRINTED)
        buy("OTHER", key=OTHER)
        self.assertEqual(self.found(), ["P2", "P1"])  # newest first
        self.now += 13 * 3600
        self.assertEqual(self.found(), [])
        self.assertEqual(self.found(within_hours=24), ["P2", "P1"])

    def test_waiting_labels_count_at_any_age_while_their_file_exists(self) -> None:
        kept = self.make_file("kept.png")
        gone = self.dir / "gone.png"
        buy("Q1")
        update_status("Q1", QUEUED, file=str(kept))
        buy("Q2")
        update_status("Q2", CHECK_PRINTER, file=str(gone))
        buy("Q3")
        update_status("Q3", CHECK_PRINTER)  # no file: still counts
        self.now += 20 * DAY
        self.assertEqual(sorted(self.found()), ["Q1", "Q3"])

    def test_refunded_never_counts(self) -> None:
        buy("R1")
        update_status("R1", REFUNDED)
        self.assertEqual(self.found(), [])

    def test_empty_key(self) -> None:
        buy("P1", key="")
        self.assertEqual(find_duplicates(""), [])

    def test_includes_sidecar_label_unknown_to_the_journal(self) -> None:
        png = self.make_file("20261004-101500_SIDE1.png")
        paths.write_sidecar(
            png,
            {
                "tracking_code": "SIDE1",
                "recipient_key": KEY,
                "recipient_label": "Jane",
            },
        )
        self.assertEqual(self.found(), ["SIDE1"])

    def test_missing_journal(self) -> None:
        self.assertEqual(self.found(), [])


class PendingLabelsTests(JournalTest):
    """pending_labels."""

    def test_journal_and_orphans_oldest_first(self) -> None:
        check = self.make_file("cp.png", self.downloads / paths.CHECK_PRINTER_DIR)
        buy("CP")
        update_status("CP", CHECK_PRINTER, file=str(check))
        self.now += 1
        queued = self.make_file("20261004-101500_Q1.png")
        buy("Q1")
        update_status("Q1", QUEUED, file=str(queued))
        self.now += 1
        buy("GONE")
        update_status("GONE", QUEUED, file=str(self.to_print / "deleted.png"))
        buy("PR")
        update_status("PR", PRINTED)
        # In to-print/ with a sidecar, but the journal never heard of it.
        side = self.make_file("20261004-101600_S1.png")
        paths.write_sidecar(
            side,
            {
                "tracking_code": "S1",
                "recipient_label": "Sam",
                "recipient_key": OTHER,
                "app": "shippy",
                "created": self.now + 5,
            },
        )
        # Journal knows the label as purchased only (its queued update was lost).
        lost = self.make_file("20261004-101700_L1.png")
        buy("L1")
        paths.write_sidecar(lost, {"tracking_code": "L1"})
        # No sidecar at all.
        bare = self.make_file("20261004-101800_9400TEST.png")
        os.utime(bare, (self.now + 10, self.now + 10))
        (self.to_print / "x.png.partial").write_bytes(b"")
        (self.to_print / "notes.txt").write_text("x")

        pending = pending_labels()
        self.assertEqual(
            [record.tracking_code for record in pending],
            ["CP", "Q1", "L1", "S1", "9400TEST"],
        )
        by_code = {record.tracking_code: record for record in pending}
        self.assertEqual(by_code["CP"].status, CHECK_PRINTER)
        self.assertEqual(by_code["L1"].status, QUEUED)
        self.assertEqual(by_code["L1"].file, str(lost))
        self.assertEqual(by_code["L1"].recipient_label, "Jane Doe, Huntsville TX")
        self.assertEqual(by_code["S1"].recipient_label, "Sam")
        self.assertEqual(by_code["S1"].app, "shippy")
        self.assertEqual(by_code["9400TEST"].recipient_label, bare.name)
        self.assertEqual(by_code["9400TEST"].file, str(bare))

    def test_refunded_label_left_in_to_print_is_listed_as_refunded(self) -> None:
        png = self.make_file("20261004-101500_R1.png")
        paths.write_sidecar(png, {"tracking_code": "R1"})
        buy("R1")
        update_status("R1", REFUNDED)
        with self.assertLogs("ibp_printing.labels", logging.WARNING):
            (record,) = pending_labels()
        self.assertEqual(record.status, REFUNDED)

    def test_journal_failure_still_lists_the_folder(self) -> None:
        self.make_file("20261004-101500_X.png")
        with mock.patch.object(labels, "_read_all", side_effect=OSError("boom")):
            with self.assertLogs("ibp_printing.labels", logging.ERROR):
                (record,) = pending_labels()
        self.assertEqual(record.tracking_code, "X")


class SaveForRetryTests(JournalTest):
    """save_for_retry with metadata writes a sidecar first."""

    META = {
        "tracking_code": "9400TRK",
        "shipment_id": "shp_1",
        "recipient_label": "Jane Doe, Huntsville TX",
        "recipient_key": KEY,
        "app": "shippy",
    }

    def image(self) -> Image.Image:
        return Image.new("L", (40, 60), 255)

    def test_without_meta_no_sidecar(self) -> None:
        path = ibp_printing.save_for_retry(self.image(), "T", watch_dir=self.downloads)
        self.assertTrue(path.is_file())
        self.assertFalse(paths.sidecar_path(path).exists())
        self.assertFalse(self.journal.exists())

    def test_sidecar_written_before_label_and_journal_queued(self) -> None:
        order: list[str] = []
        real_replace = os.replace

        def spy(src: Any, dst: Any) -> None:
            order.append(Path(dst).name)
            real_replace(src, dst)

        with mock.patch.object(paths.os, "replace", spy):
            path = ibp_printing.save_for_retry(
                self.image(), "9400TRK", watch_dir=self.downloads, meta=self.META
            )
        sidecar = paths.sidecar_path(path)
        self.assertEqual(order, [sidecar.name, path.name])
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(set(data), set(paths.SIDECAR_KEYS))
        self.assertEqual(data["tracking_code"], "9400TRK")
        self.assertIsInstance(data["created"], float)
        self.assertEqual(paths.read_sidecar(path), data)
        record = labels._read_all()["9400TRK"]
        self.assertEqual((record.status, record.file), (QUEUED, str(path)))
        self.assertEqual(record.recipient_key, KEY)
        self.assertEqual(
            [p.name for p in self.to_print.iterdir()].count(sidecar.name), 1
        )
        self.assertFalse(list(self.to_print.glob("*.partial")))

    def test_sidecar_failure_still_saves_label(self) -> None:
        with mock.patch.object(paths, "write_sidecar", side_effect=OSError("full")):
            with self.assertLogs("ibp_printing.paths", logging.ERROR):
                path = ibp_printing.save_for_retry(
                    self.image(), "X", watch_dir=self.downloads, meta=self.META
                )
        self.assertTrue(path.is_file())

    def test_corrupt_sidecar_reads_as_none(self) -> None:
        png = self.make_file("a.png")
        paths.sidecar_path(png).write_text("{broken", encoding="utf-8")
        with self.assertLogs("ibp_printing.paths", logging.WARNING):
            self.assertIsNone(paths.read_sidecar(png))
        self.assertIsNone(paths.read_sidecar(self.to_print / "missing.png"))

    def test_is_sidecar(self) -> None:
        self.assertTrue(paths.is_sidecar(Path("a.png.json")))
        self.assertTrue(paths.is_sidecar(Path("A.PNG.JSON")))
        self.assertFalse(paths.is_sidecar(Path("a.png")))


if __name__ == "__main__":
    unittest.main()
