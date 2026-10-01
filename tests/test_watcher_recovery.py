"""Watcher recovery: to-print retries, dedupe, transient errors, state, shutdown."""

# pylint: disable=missing-function-docstring,protected-access

import errno
import json
import logging
import os
import shutil
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import ibp_printing
from ibp_printing.models import JobOutcome
from ibp_printing.paths import CHECK_PRINTER_DIR, PRINTED_DIR, TO_PRINT_DIR
from ibp_printing.watcher import service
from ibp_printing.watcher.detect import (
    TransientReadError,
    download_in_progress,
    is_transient_os_error,
    load_label,
    peek_size,
    read_file_bytes,
    sha256_bytes,
    wait_until_stable,
)
from ibp_printing.watcher.service import RETRY
from ibp_printing.watcher.state import FolderState, StateFile

from test_watcher import (  # pylint: disable=wrong-import-order
    FakeBackend,
    FakeClock,
    TempDirTest,
    make_label,
    setUpModule as _watcher_setup,
)


def setUpModule() -> None:  # pylint: disable=invalid-name
    _watcher_setup()


def tearDownModule() -> None:  # pylint: disable=invalid-name
    ibp_printing.set_backend(None)


def wait_for(predicate: Any, timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class DetectHelperTests(TempDirTest):
    """The small detect.py helpers added for W4/W5."""

    def test_in_progress_placeholders(self) -> None:
        empty = self.dir / "label.png"
        empty.write_bytes(b"")
        self.assertIn("empty", download_in_progress(empty) or "")
        full = make_label(self.dir / "full.png")
        self.assertIsNone(download_in_progress(full))
        (self.dir / "full.png.part").write_bytes(b"x")
        self.assertIn("full.png.part", download_in_progress(full) or "")

    def test_transient_classification(self) -> None:
        self.assertTrue(is_transient_os_error(PermissionError(13, "denied")))
        self.assertTrue(is_transient_os_error(OSError(errno.EBUSY, "busy")))
        sharing = OSError(errno.EINVAL, "sharing violation")
        sharing.winerror = 32  # type: ignore[attr-defined]
        self.assertTrue(is_transient_os_error(sharing))
        self.assertFalse(is_transient_os_error(FileNotFoundError(2, "gone")))
        self.assertFalse(is_transient_os_error(OSError(errno.EIO, "io")))

    def test_read_file_bytes_retries_then_gives_up(self) -> None:
        path = make_label(self.dir / "a.png")
        real = Path.read_bytes
        calls = []

        def flaky(self_path: Path) -> bytes:
            calls.append(self_path)
            if len(calls) < 3:
                raise PermissionError(13, "locked")
            return real(self_path)

        sleeps: list[float] = []
        with mock.patch.object(Path, "read_bytes", flaky):
            data = read_file_bytes(path, attempts=3, sleep=sleeps.append)
        self.assertEqual(data, path.read_bytes())
        self.assertEqual(len(sleeps), 2)
        with mock.patch.object(
            Path, "read_bytes", side_effect=PermissionError(13, "locked")
        ):
            with self.assertRaises(TransientReadError):
                read_file_bytes(path, attempts=2, sleep=lambda s: None)
        with self.assertRaises(FileNotFoundError):
            read_file_bytes(self.dir / "missing.png", sleep=lambda s: None)

    def test_load_from_bytes_and_peek(self) -> None:
        path = make_label(self.dir / "a.png")
        data = path.read_bytes()
        path.unlink()  # decoding uses the bytes, not the file
        self.assertEqual(load_label(path, data=data).image.size, (1200, 1800))
        self.assertEqual(peek_size(make_label(self.dir / "b.png")), (1200, 1800))
        self.assertIsNone(peek_size(self.dir / "c.pdf"))

    def test_stability_wait_can_be_cancelled(self) -> None:
        path = self.dir / "a.png"
        path.write_bytes(b"x")
        clock = FakeClock()
        result = wait_until_stable(
            path, clock=clock, sleep=clock.sleep, cancel=lambda: True
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "cancelled")


class ToPrintQueueTests(TempDirTest):
    """W1: the to-print/ folder is retried whenever a printer is usable."""

    def test_waits_for_printer_then_prints_oldest_first(self) -> None:
        backend = FakeBackend(printers=0)
        ibp_printing.set_backend(backend)
        watcher, notifier = self.make_watcher()
        newer = self.queue_label("b-newer.png", seed=1, age_s=10)
        older = self.queue_label("a-older.png", seed=2, age_s=100)
        self.queue_label("c.png.partial", seed=3)  # half-written: ignored
        self.assertEqual(watcher.retry_queue_once("test"), [])
        self.assertEqual(backend.attempts, 0)
        self.assertEqual(notifier.texts, [])  # the app already told them
        backend.printers = 1
        outcomes = watcher.retry_queue_once("test")
        self.assertEqual([o.path for o in outcomes], [older, newer])
        self.assertEqual([o.status for o in outcomes], ["printed", "printed"])
        self.assertEqual(
            sorted(p.name for p in (self.dir / TO_PRINT_DIR).iterdir()),
            ["c.png.partial"],
        )
        self.assertEqual(len(list((self.dir / PRINTED_DIR).iterdir())), 2)

    def test_failure_stops_the_pass_and_notifies_once(self) -> None:
        backend = FakeBackend(raise_error="StartDoc failed")
        ibp_printing.set_backend(backend)
        watcher, notifier = self.make_watcher()
        first = self.queue_label("a.png", seed=1, age_s=100)
        second = self.queue_label("b.png", seed=2, age_s=10)
        for _ in range(3):
            outcomes = watcher.retry_queue_once("test")
            self.assertEqual([o.status for o in outcomes], ["still_queued"])
        self.assertEqual(backend.attempts, 3)  # never tried the second file
        self.assertTrue(first.exists() and second.exists())
        self.assertEqual(len(notifier.texts), 1)
        self.assertIn("keeps trying automatically", notifier.texts[0])
        # A definite failure releases the hash, so the next pass may print it.
        backend.raise_error = None
        statuses = [o.status for o in watcher.retry_queue_once("test")]
        self.assertEqual(statuses, ["printed", "printed"])

    def test_uncertain_queued_label_goes_to_check_printer(self) -> None:
        ibp_printing.set_backend(FakeBackend(outcome=JobOutcome.UNCERTAIN))
        watcher, notifier = self.make_watcher()
        self.queue_label("a.png", seed=1, age_s=100)
        self.queue_label("b.png", seed=2, age_s=10)
        outcomes = watcher.retry_queue_once("test")
        self.assertEqual([o.status for o in outcomes], ["check_printer"])
        assert outcomes[0].moved_to is not None
        self.assertEqual(outcomes[0].moved_to.parent.name, CHECK_PRINTER_DIR)
        self.assertIn("BEFORE printing it again", notifier.texts[0])

    def test_unprintable_queued_file_notified_once_and_skipped(self) -> None:
        backend = FakeBackend()
        ibp_printing.set_backend(backend)
        watcher, notifier = self.make_watcher()
        folder = self.dir / TO_PRINT_DIR
        folder.mkdir()
        (folder / "bad.png").write_bytes(b"not a png")
        (folder / "notes.txt").write_text("hello")
        for _ in range(3):
            watcher.retry_queue_once("test")
        self.assertEqual(backend.attempts, 0)
        self.assertEqual(len(notifier.texts), 2)
        self.assertEqual(watcher.queued_files(), [])  # decided until changed

    def test_odd_shape_in_queue_still_prints(self) -> None:
        backend = FakeBackend()
        ibp_printing.set_backend(backend)
        watcher, _ = self.make_watcher()
        folder = self.dir / TO_PRINT_DIR
        folder.mkdir()
        make_label(folder / "square.png", size=(1000, 1000))
        outcomes = watcher.retry_queue_once("test")
        self.assertEqual([o.status for o in outcomes], ["printed"])

    def test_queued_copy_of_just_printed_label_is_not_printed_again(self) -> None:
        backend = FakeBackend()
        ibp_printing.set_backend(backend)
        watcher, notifier = self.make_watcher()
        download = make_label(self.dir / "label.png", seed=4)
        queued = self.queue_label("copy.png", seed=4)
        self.assertEqual(watcher.process_path(download).status, "printed")
        outcomes = watcher.retry_queue_once("test")
        self.assertEqual([o.status for o in outcomes], ["duplicate"])
        self.assertFalse(queued.exists())
        assert outcomes[0].moved_to is not None
        self.assertEqual(outcomes[0].moved_to.parent.name, CHECK_PRINTER_DIR)
        self.assertEqual(len(backend.printed), 1)
        self.assertEqual(len(notifier.texts), 1)

    def test_request_retry_coalesces(self) -> None:
        watcher, _ = self.make_watcher()
        self.assertTrue(watcher.request_retry("a"))
        self.assertFalse(watcher.request_retry("b"))
        self.assertEqual(watcher._queue.qsize(), 1)
        self.assertIs(watcher._queue.get_nowait(), RETRY)

    def test_running_watcher_retries_on_timer_without_extra_discovery(self) -> None:
        backend = FakeBackend(printers=0)
        ibp_printing.set_backend(backend)
        watcher, notifier = self.make_watcher(retry_seconds=1.0)
        self.queue_label("a.png", seed=1)
        watcher.start()
        self.addCleanup(watcher.stop)
        time.sleep(2.5)
        # One pass at startup plus one per second, each with one discovery.
        self.assertGreaterEqual(backend.discoveries, 2)
        self.assertLessEqual(backend.discoveries, 4)
        self.assertEqual(notifier.texts, [])
        backend.printers = 1
        self.assertTrue(
            wait_for(lambda: not (self.dir / TO_PRINT_DIR / "a.png").exists(), 5)
        )
        self.assertEqual(len(backend.printed), 1)

    def test_retry_with_no_files_does_not_discover(self) -> None:
        backend = FakeBackend()
        ibp_printing.set_backend(backend)
        watcher, _ = self.make_watcher()
        self.assertEqual(watcher.retry_queue_once("test"), [])
        self.assertEqual(backend.discoveries, 0)


class DedupeTests(TempDirTest):
    """W2/W3: reservation of content after any submission."""

    def test_hash_reserved_after_non_ok_submission(self) -> None:
        backend = FakeBackend(outcome=JobOutcome.TIMEOUT)
        ibp_printing.set_backend(backend)
        watcher, notifier = self.make_watcher()
        make_label(self.dir / "label.png")
        shutil.copy(self.dir / "label.png", self.dir / "label (1).png")
        self.assertEqual(
            watcher.process_path(self.dir / "label.png").status, "check_printer"
        )
        dup = watcher.process_path(self.dir / "label (1).png")
        self.assertEqual(dup.status, "duplicate")
        self.assertEqual(backend.attempts, 1)
        self.assertIn("sent to the printer", notifier.texts[-1])
        self.assertIn("rename the file", notifier.texts[-1])

    def test_hash_released_after_definite_failure(self) -> None:
        backend = FakeBackend(raise_error="no spooler")
        ibp_printing.set_backend(backend)
        watcher, _ = self.make_watcher()
        make_label(self.dir / "label.png")
        shutil.copy(self.dir / "label.png", self.dir / "label (1).png")
        self.assertEqual(
            watcher.process_path(self.dir / "label.png").status, "to_print"
        )
        self.assertEqual(
            watcher.process_path(self.dir / "label (1).png").status, "to_print"
        )
        self.assertEqual(backend.attempts, 2)

    def test_stuck_hash_only_marked_when_printed(self) -> None:
        ibp_printing.set_backend(FakeBackend(outcome=JobOutcome.ERROR))
        watcher, _ = self.make_watcher()
        watcher._sleep = lambda seconds: None
        with mock.patch(
            "ibp_printing.watcher.core.os.rename",
            side_effect=PermissionError(13, "in use"),
        ):
            outcome = watcher.process_path(make_label(self.dir / "label.png"))
        self.assertEqual(outcome.status, "check_printer")
        self.assertIsNone(outcome.moved_to)
        self.assertEqual(watcher._stuck_hashes, set())


class TransientTests(TempDirTest):
    """W4/W5: locked files are retried later; placeholders don't block."""

    def test_locked_file_is_deferred_not_cached(self) -> None:
        backend = FakeBackend()
        ibp_printing.set_backend(backend)
        watcher, _ = self.make_watcher()
        watcher._sleep = lambda seconds: None
        path = make_label(self.dir / "label.png")
        with mock.patch.object(
            Path, "read_bytes", side_effect=PermissionError(13, "locked")
        ):
            first = watcher.process_path(path)
        self.assertEqual(first.status, "unreadable")
        self.assertNotIn(path, watcher._decided)
        self.assertEqual(watcher._deferred, {path: 1})
        # The next retry tick re-queues it, and now it prints.
        watcher.request_retry("tick")
        queued = [watcher._queue.get_nowait() for _ in range(watcher._queue.qsize())]
        self.assertIn(path, queued)
        self.assertEqual(watcher.process_path(path).status, "printed")
        self.assertEqual(watcher._deferred, {})

    def test_gives_up_after_bounded_retries(self) -> None:
        ibp_printing.set_backend(FakeBackend())
        watcher, notifier = self.make_watcher()
        watcher._sleep = lambda seconds: None
        path = make_label(self.dir / "label.png")
        statuses = []
        with mock.patch.object(
            Path, "read_bytes", side_effect=PermissionError(13, "locked")
        ):
            for _ in range(service.MAX_DEFERRED_RETRIES + 1):
                statuses.append(watcher.process_path(path).status)
        self.assertEqual(
            statuses, ["unreadable"] * service.MAX_DEFERRED_RETRIES + ["gave_up"]
        )
        self.assertEqual(len(notifier.texts), 1)
        self.assertIn("could not be read", notifier.texts[0])

    def test_zero_byte_placeholder_does_not_wait(self) -> None:
        watcher, _ = self.make_watcher(stable_timeout_s=60.0)
        clock = FakeClock()
        watcher._clock, watcher._sleep = clock, clock.sleep
        path = self.dir / "label.png"
        path.write_bytes(b"")
        outcome = watcher.process_path(path)
        self.assertEqual(outcome.status, "in_progress")
        self.assertEqual(clock.now, 0.0)
        self.assertNotIn(path, watcher._decided)

    def test_part_sibling_means_in_progress(self) -> None:
        ibp_printing.set_backend(FakeBackend())
        watcher, _ = self.make_watcher()
        path = make_label(self.dir / "label.png")
        part = self.dir / "label.png.part"
        part.write_bytes(b"partial")
        self.assertEqual(watcher.process_path(path).status, "in_progress")
        part.unlink()
        self.assertEqual(watcher.process_path(path).status, "printed")

    def test_event_while_processing_requeues_after(self) -> None:
        watcher, _ = self.make_watcher()
        path = make_label(self.dir / "label.png")
        seen = []

        def fake_process(item: Path) -> None:
            seen.append(item)
            # A browser rewrites the file while we are looking at it.
            self.assertFalse(watcher.enqueue(item, "modified"))

        with watcher._pending_lock:
            watcher._pending.add(path)
        with mock.patch.object(watcher, "process_path", side_effect=fake_process):
            watcher._work_on(path)
        self.assertEqual(seen, [path])
        self.assertEqual(watcher._queue.get_nowait(), path)
        self.assertEqual(watcher._dirty, set())

    def test_event_for_queued_path_is_not_dirty(self) -> None:
        watcher, _ = self.make_watcher()
        path = make_label(self.dir / "label.png")
        self.assertTrue(watcher.enqueue(path, "created"))
        self.assertFalse(watcher.enqueue(path, "modified"))
        self.assertEqual(watcher._dirty, set())


class StateFileTests(TempDirTest):
    """W7: the state file round-trips and survives corruption."""

    def test_round_trip_keeps_other_folders(self) -> None:
        state = self.state_file()
        self.assertIsNone(state.load(self.dir))
        folder = FolderState(seen_until=100.0, updated=101.0, clean_shutdown=True)
        folder.in_flight = {"abc": {"file": "x.png", "since": time.time()}}
        self.assertTrue(state.save(self.dir, folder))
        other = self.dir / "other"
        state.save(other, FolderState(seen_until=5.0, updated=5.0))
        loaded = StateFile(state.path).load(self.dir)
        assert loaded is not None
        self.assertEqual(loaded.seen_until, 100.0)
        self.assertTrue(loaded.clean_shutdown)
        self.assertEqual(loaded.in_flight["abc"]["file"], "x.png")
        self.assertIsNotNone(StateFile(state.path).load(other))

    def test_old_in_flight_entries_expire(self) -> None:
        state = self.state_file()
        folder = FolderState(seen_until=1.0, updated=1.0)
        folder.in_flight = {"old": {"file": "x", "since": 1.0}}
        state.save(self.dir, folder)
        loaded = StateFile(state.path).load(self.dir)
        assert loaded is not None
        self.assertEqual(loaded.in_flight, {})

    def test_corrupt_files_are_logged_and_ignored(self) -> None:
        state = self.state_file()
        key = os.path.normcase(str(self.dir))
        for content in (
            "{not json",
            "[]",
            json.dumps({"watch_dirs": {key: {"seen_until": "soon"}}}),
            json.dumps({"watch_dirs": {key: {"seen_until": 1, "in_flight": []}}}),
            "\x00\x01",
        ):
            with self.subTest(content=content):
                state.path.parent.mkdir(parents=True, exist_ok=True)
                state.path.write_text(content, encoding="utf-8")
                with self.assertLogs("ibp_printing.watcher.state", logging.WARNING):
                    self.assertIsNone(state.load(self.dir))
                # And it is simply rewritten on the next save.
                self.assertTrue(state.save(self.dir, FolderState(1.0, 1.0)))
                self.assertIsNotNone(StateFile(state.path).load(self.dir))

    def test_unwritable_state_does_not_raise(self) -> None:
        blocker = self.state_dir / "file"
        blocker.write_text("x")
        state = StateFile(blocker / "watcher-state.json")
        self.assertIsNone(state.load(self.dir))
        self.assertFalse(state.save(self.dir, FolderState(1.0, 1.0)))


class StartupTests(TempDirTest):
    """W6/W7: catching up on files that arrived while the watcher was away."""

    def test_first_run_ignores_existing_and_warns_about_labels(self) -> None:
        backend = FakeBackend()
        ibp_printing.set_backend(backend)
        make_label(self.dir / "old-label.png")
        make_label(self.dir / "photo.png", size=(1600, 1000))
        watcher, _ = self.make_watcher(state=self.state_file())
        with self.assertLogs("ibp_printing.watcher.service", logging.WARNING) as logs:
            watcher.start()
        self.addCleanup(watcher.stop)
        ignored = [line for line in logs.output if "IGNORED" in line]
        self.assertEqual(len(ignored), 1)
        self.assertTrue(watcher.wait_idle(5))
        self.assertEqual(backend.attempts, 0)
        self.assertTrue((self.dir / "old-label.png").exists())

    def test_files_newer_than_seen_until_are_processed(self) -> None:
        backend = FakeBackend()
        ibp_printing.set_backend(backend)
        old = make_label(self.dir / "old.png", seed=1)
        new = make_label(self.dir / "new.png", seed=2)
        state = self.state_file()
        state.save(self.dir, FolderState(seen_until=1000.0, updated=1000.0))
        times = {old: 900.0, new: 1500.0}
        watcher, _ = self.make_watcher(state=StateFile(state.path))
        with mock.patch.object(
            service, "arrival_time", lambda path: times.get(path, time.time())
        ):
            with self.assertLogs(
                "ibp_printing.watcher.service", logging.WARNING
            ) as logs:
                watcher.start()
            self.addCleanup(watcher.stop)
            self.assertTrue(wait_for(lambda: not new.exists()))
            self.assertTrue(watcher.wait_idle(5))
        self.assertTrue(old.exists())
        self.assertEqual(len(backend.printed), 1)
        self.assertTrue(any("did not shut down cleanly" in x for x in logs.output))

    def test_label_in_flight_at_crash_goes_to_check_printer(self) -> None:
        backend = FakeBackend()
        ibp_printing.set_backend(backend)
        path = make_label(self.dir / "label.png", seed=7)
        digest = sha256_bytes(path.read_bytes())
        state = self.state_file()
        folder = FolderState(seen_until=0.0, updated=0.0)
        folder.in_flight = {digest: {"file": str(path), "since": time.time()}}
        state.save(self.dir, folder)
        watcher, notifier = self.make_watcher(state=StateFile(state.path))
        watcher.start()
        self.addCleanup(watcher.stop)
        self.assertTrue(wait_for(lambda: not path.exists()))
        self.assertTrue(watcher.wait_idle(5))
        self.assertEqual(backend.attempts, 0)
        moved = list((self.dir / CHECK_PRINTER_DIR).iterdir())
        self.assertEqual(len(moved), 1)
        self.assertIn("stopped while this shipping label", notifier.texts[0])
        watcher.stop()
        saved = StateFile(state.path).load(self.dir)
        assert saved is not None
        self.assertEqual(saved.in_flight, {})
        self.assertTrue(saved.clean_shutdown)

    def test_file_created_between_snapshot_and_observer_is_caught(self) -> None:
        backend = FakeBackend()
        ibp_printing.set_backend(backend)
        watcher, _ = self.make_watcher()
        real_select = watcher._select_startup_files
        late = self.dir / "late.png"

        def select_then_download(*args: Any) -> list[Path]:
            chosen = real_select(*args)
            make_label(late, seed=9)  # lands before the observer exists
            return chosen

        with mock.patch.object(
            watcher, "_select_startup_files", side_effect=select_then_download
        ):
            watcher.start()
        self.addCleanup(watcher.stop)
        self.assertTrue(wait_for(lambda: not late.exists()))
        self.assertTrue(watcher.wait_idle(5))
        self.assertEqual(len(backend.printed), 1)


class ShutdownTests(TempDirTest):
    """W7: shutdown finishes the label in flight and remembers the rest."""

    def test_stop_waits_for_in_flight_label_and_keeps_queued_for_next_start(
        self,
    ) -> None:
        backend = FakeBackend()
        backend.gate = threading.Event()
        ibp_printing.set_backend(backend)
        state = self.state_file()
        watcher, _ = self.make_watcher(state=state)
        watcher.start()
        first = make_label(self.dir / "first.png", seed=1)
        self.assertTrue(backend.started.wait(10))
        second = make_label(self.dir / "second.png", seed=2)
        self.assertTrue(wait_for(lambda: second in watcher._pending))
        stopper = threading.Thread(target=watcher.stop)
        stopper.start()
        time.sleep(0.3)
        self.assertTrue(stopper.is_alive())  # still waiting for the print
        backend.gate.set()
        stopper.join(10)
        self.assertFalse(stopper.is_alive())
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())
        self.assertEqual(len(backend.printed), 1)
        saved = StateFile(state.path).load(self.dir)
        assert saved is not None
        self.assertTrue(saved.clean_shutdown)
        self.assertEqual(saved.in_flight, {})
        self.assertLessEqual(saved.seen_until, time.time())

        # The next start prints the label that was still queued.
        backend.gate = None
        again, _ = self.make_watcher(state=StateFile(state.path))
        again.start()
        self.addCleanup(again.stop)
        self.assertTrue(wait_for(lambda: not second.exists()))
        self.assertEqual(len(backend.printed), 2)

    def test_enqueue_refused_after_stop(self) -> None:
        watcher, _ = self.make_watcher()
        watcher.stop(timeout_s=0.1)
        path = make_label(self.dir / "label.png")
        self.assertFalse(watcher.enqueue(path, "created"))
        self.assertIn(path, watcher._first_seen)  # holds back seen_until

    def test_no_new_print_once_stopping(self) -> None:
        backend = FakeBackend()
        ibp_printing.set_backend(backend)
        watcher, _ = self.make_watcher()
        watcher.stop_event.set()
        outcome = watcher.process_path(make_label(self.dir / "label.png"))
        self.assertEqual(outcome.status, "shutdown")
        self.assertEqual(backend.attempts, 0)


class HealthTests(TempDirTest):
    """W8: dead observers are restarted; stats are logged as a copy."""

    def test_dead_observer_is_restarted_and_folder_rescanned(self) -> None:
        backend = FakeBackend()
        ibp_printing.set_backend(backend)
        watcher, _ = self.make_watcher()
        watcher.start()
        self.addCleanup(watcher.stop)
        self.assertTrue(watcher.observer_alive())
        old = watcher._observer
        old.stop()
        old.join(5)
        self.assertFalse(watcher.observer_alive())
        missed = make_label(self.dir / "missed.png", seed=3)  # no observer
        with self.assertLogs("ibp_printing.watcher.service", logging.CRITICAL):
            self.assertFalse(watcher.check_observer())
        self.assertIsNot(watcher._observer, old)
        self.assertTrue(watcher.observer_alive())
        self.assertTrue(wait_for(lambda: not missed.exists()))
        later = make_label(self.dir / "later.png", seed=4)
        self.assertTrue(wait_for(lambda: not later.exists()))
        self.assertEqual(len(backend.printed), 2)
        self.assertTrue(watcher.check_observer())

    def test_dead_emitter_counts_as_dead_observer(self) -> None:
        watcher, _ = self.make_watcher()
        emitter = mock.Mock(is_alive=mock.Mock(return_value=False))
        watcher._observer = mock.Mock(
            is_alive=mock.Mock(return_value=True), emitters={emitter}
        )
        self.assertFalse(watcher.observer_alive())
        emitter.is_alive.return_value = True
        self.assertTrue(watcher.observer_alive())

    def test_stats_is_a_copy(self) -> None:
        ibp_printing.set_backend(FakeBackend())
        watcher, _ = self.make_watcher()
        watcher.process_path(make_label(self.dir / "label.png"))
        stats = watcher.stats
        self.assertEqual(stats, {"printed": 1})
        stats["printed"] = 99
        self.assertEqual(watcher.stats, {"printed": 1})

    def test_heartbeat_saves_state_and_logs(self) -> None:
        ibp_printing.set_backend(FakeBackend())
        state = self.state_file()
        watcher, _ = self.make_watcher(state=state)
        with self.assertLogs("ibp_printing.watcher.service", logging.INFO) as logs:
            watcher.heartbeat()
        self.assertTrue(any("heartbeat" in line for line in logs.output))
        self.assertIsNotNone(StateFile(state.path).load(self.dir))


if __name__ == "__main__":
    unittest.main()
