"""Round-2 watcher fixes: durable reservations, filing, --once, state, notices.

R8 reservations, R9 retried moves, R10 real --once journaling, R11 snapshot
ordering, R12 unsaved state, R13 checkpoint, R14 notifications, R15 to-print
deferral, R16 the apps' to-print folder.
"""

# pylint: disable=missing-function-docstring,protected-access

import json
import logging
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
from ibp_printing.watcher import __main__ as watcher_main
from ibp_printing.watcher import core
from ibp_printing.watcher.detect import sha256_bytes
from ibp_printing.watcher.service import LabelWatcher
from ibp_printing.watcher.state import FolderState, Reservation, StateFile

from test_watcher import (  # pylint: disable=wrong-import-order
    FakeBackend,
    RecordingNotifier,
    TempDirTest,
    make_label,
    quiet_configure,
    setUpModule as _watcher_setup,
)
from test_watcher_recovery import wait_for  # pylint: disable=wrong-import-order

HOUR = 3600.0


def setUpModule() -> None:  # pylint: disable=invalid-name
    _watcher_setup()


def tearDownModule() -> None:  # pylint: disable=invalid-name
    ibp_printing.set_backend(None)


def digest_of(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


class Restartable(TempDirTest):
    """Helpers for "the watcher stops and a new one starts with the same state"."""

    def setUp(self) -> None:
        super().setUp()
        self.state_path = self.state_dir / "watcher-state.json"

    def fresh(self, **overrides: Any) -> tuple[LabelWatcher, RecordingNotifier]:
        """A new watcher process: reads the state file like ``start()`` does."""
        watcher, notifier = self.make_watcher(
            state=StateFile(self.state_path), **overrides
        )
        watcher._sleep = lambda seconds: None
        watcher.load_state()
        return watcher, notifier

    def saved(self) -> dict[str, Reservation]:
        return StateFile(self.state_path).load(self.dir).reservations


class CrossPathDuplicateTests(Restartable):
    """R8: one label in to-print/ and in Downloads prints once, even across a
    restart and minutes apart."""

    def test_downloaded_then_queued_copy_across_restart(self) -> None:
        backend = FakeBackend()
        ibp_printing.set_backend(backend)
        first, _ = self.fresh()
        download = make_label(self.dir / "label.png", seed=11)
        digest = digest_of(download)
        queued = self.queue_label("20260930-101500_TRACK123.png", seed=11)
        self.assertEqual(first.process_path(download).status, "printed")
        self.assertEqual(self.saved()[digest].status, "printed")

        # The watcher restarts; twenty minutes later the to-print copy is retried.
        second, notifier = self.fresh()
        second._now = lambda: time.time() + 20 * 60
        outcomes = second.retry_queue_once("test")
        self.assertEqual([o.status for o in outcomes], ["duplicate"])
        assert outcomes[0].moved_to is not None
        self.assertEqual(outcomes[0].moved_to.parent, self.dir / PRINTED_DIR)
        self.assertTrue(outcomes[0].moved_to.name.startswith("duplicate_"))
        self.assertFalse(queued.exists())
        self.assertEqual(len(backend.printed), 1)
        self.assertIn("already printed", notifier.texts[0])
        self.assertEqual(self.saved()[digest].copies_seen, 1)

    def test_queued_then_downloaded_copy(self) -> None:
        backend = FakeBackend()
        ibp_printing.set_backend(backend)
        first, _ = self.fresh()
        self.queue_label("app.png", seed=12)
        statuses = [o.status for o in first.retry_queue_once("test")]
        self.assertEqual(statuses, ["printed"])
        second, _ = self.fresh()
        second._now = lambda: time.time() + 5 * 60
        download = make_label(self.dir / "label.png", seed=12)
        self.assertEqual(second.process_path(download).status, "duplicate")
        self.assertEqual(len(backend.printed), 1)

    def test_printed_reservation_expires_after_the_window(self) -> None:
        backend = FakeBackend()
        ibp_printing.set_backend(backend)
        first, _ = self.fresh(duplicate_window_hours=24.0)
        digest = digest_of(make_label(self.dir / "a.png", seed=13))
        first.process_path(self.dir / "a.png")
        later, _ = self.fresh(duplicate_window_hours=24.0)
        later._now = lambda: time.time() + 25 * HOUR
        with self.assertLogs("ibp_printing.watcher.core", logging.INFO) as logs:
            later._save_state(clean=False)
        self.assertTrue(any("duplicate window over" in x for x in logs.output))
        self.assertNotIn(digest, self.saved())
        make_label(self.dir / "b.png", seed=13)
        self.assertEqual(later.process_path(self.dir / "b.png").status, "printed")
        self.assertEqual(len(backend.printed), 2)

    def test_window_starts_when_printing_finished(self) -> None:
        backend = FakeBackend()
        ibp_printing.set_backend(backend)
        watcher, _ = self.fresh()
        start = time.time()
        now = [start]
        watcher._now = lambda: now[0]
        real_print = backend.print_image

        def slow_print(*args: Any, **kwargs: Any) -> Any:
            now[0] += 2 * HOUR  # tracking a stuck job takes a long time
            return real_print(*args, **kwargs)

        backend.print_image = slow_print  # type: ignore[method-assign]
        path = make_label(self.dir / "a.png", seed=14)
        digest = digest_of(path)
        watcher.process_path(path)
        reservation = watcher.reservations()[digest]
        self.assertEqual(reservation.first_submitted, start)
        self.assertEqual(reservation.completed, start + 2 * HOUR)


class UncertainTests(Restartable):
    """R8: an uncertain label never expires; REPRINT overrides."""

    def test_uncertain_never_expires_across_restart(self) -> None:
        backend = FakeBackend(outcome=JobOutcome.UNCERTAIN)
        ibp_printing.set_backend(backend)
        first, _ = self.fresh(duplicate_window_hours=1.0)
        path = make_label(self.dir / "label.png", seed=21)
        digest = digest_of(path)
        self.assertEqual(first.process_path(path).status, "check_printer")
        backend.outcome = JobOutcome.COMPLETED
        later, notifier = self.fresh(duplicate_window_hours=1.0)
        later._now = lambda: time.time() + 365 * 24 * HOUR
        later._save_state(clean=False)  # pruning must keep it
        self.assertEqual(self.saved()[digest].status, "uncertain")
        # A volunteer drags the check-printer copy back into to-print.
        moved = next((self.dir / CHECK_PRINTER_DIR).iterdir())
        (self.dir / TO_PRINT_DIR).mkdir()
        shutil.move(str(moved), self.dir / TO_PRINT_DIR / "label.png")
        outcomes = later.retry_queue_once("test")
        self.assertEqual([o.status for o in outcomes], ["duplicate"])
        assert outcomes[0].moved_to is not None
        self.assertEqual(outcomes[0].moved_to.parent.name, CHECK_PRINTER_DIR)
        self.assertEqual(backend.attempts, 1)
        self.assertIn("REPRINT", notifier.texts[0])

    def test_reprint_overrides_uncertain_and_printed(self) -> None:
        backend = FakeBackend(outcome=JobOutcome.UNCERTAIN)
        ibp_printing.set_backend(backend)
        watcher, _ = self.fresh()
        path = make_label(self.dir / "label.png", seed=22)
        digest = digest_of(path)
        watcher.process_path(path)
        backend.outcome = JobOutcome.COMPLETED
        self.queue_label("Reprint-label.png", seed=22)
        with self.assertLogs("ibp_printing.watcher.core", logging.WARNING) as logs:
            outcomes = watcher.retry_queue_once("test")
        self.assertEqual([o.status for o in outcomes], ["printed"])
        self.assertTrue(any("REPRINT: printing" in line for line in logs.output))
        self.assertEqual(self.saved()[digest].status, "printed")
        make_label(self.dir / "REPRINT again.png", seed=22)
        again = watcher.process_path(self.dir / "REPRINT again.png")
        self.assertEqual(again.status, "printed")
        self.assertEqual(backend.attempts, 3)

    def test_failed_reprint_restores_the_old_reservation(self) -> None:
        backend = FakeBackend()
        ibp_printing.set_backend(backend)
        watcher, _ = self.fresh()
        path = make_label(self.dir / "label.png", seed=23)
        digest = digest_of(path)
        watcher.process_path(path)
        backend.raise_error = "StartDoc failed"
        make_label(self.dir / "REPRINT.png", seed=23)
        self.assertEqual(
            watcher.process_path(self.dir / "REPRINT.png").status, "to_print"
        )
        self.assertEqual(self.saved()[digest].status, "printed")

    def test_crash_mid_print_of_queued_label_and_its_copy(self) -> None:
        backend = FakeBackend()
        ibp_printing.set_backend(backend)
        queued = self.queue_label("app.png", seed=24)
        copy = make_label(self.dir / "copy.png", seed=24)
        digest = digest_of(queued)
        state = StateFile(self.state_path)
        reservation = Reservation(
            status="filing",
            first_submitted=time.time(),
            updated=time.time(),
            file=str(queued),
        )
        state.save(self.dir, FolderState(0.0, 0.0), {digest: reservation})
        watcher, notifier = self.fresh()
        outcomes = watcher.retry_queue_once("restart")
        self.assertEqual([o.status for o in outcomes], ["filed"])
        assert outcomes[0].moved_to is not None
        self.assertEqual(outcomes[0].moved_to.parent.name, CHECK_PRINTER_DIR)
        self.assertIn("stopped while this shipping label", notifier.texts[0])
        self.assertEqual(watcher.process_path(copy).status, "duplicate")
        self.assertEqual(backend.attempts, 0)
        self.assertEqual(self.saved()[digest].status, "uncertain")


class FilingTests(Restartable):
    """R9: a printed label whose move fails is moved later, never re-printed."""

    def test_failed_move_is_retried_after_restart_not_reprinted(self) -> None:
        backend = FakeBackend()
        ibp_printing.set_backend(backend)
        first, _ = self.fresh()
        queued = self.queue_label("label.png", seed=31)
        digest = digest_of(queued)
        with mock.patch(
            "ibp_printing.watcher.core.os.rename",
            side_effect=PermissionError(13, "antivirus"),
        ):
            outcomes = first.retry_queue_once("test")
            self.assertEqual([o.status for o in outcomes], ["printed"])
            self.assertIsNone(outcomes[0].moved_to)
            # Later ticks retry the move only.
            first.retry_queue_once("tick")
            first.retry_queue_once("tick")
        self.assertTrue(queued.exists())
        saved = self.saved()[digest]
        self.assertEqual((saved.status, saved.dest), ("filing", PRINTED_DIR))
        self.assertGreaterEqual(saved.move_failures, 3)
        self.assertEqual(backend.attempts, 1)

        second, _ = self.fresh()  # restart: the in-memory state is gone
        with self.assertLogs("ibp_printing.watcher.core", logging.INFO) as logs:
            second.retry_queue_once("restart")
        self.assertFalse(queued.exists())
        self.assertEqual(len(list((self.dir / PRINTED_DIR).iterdir())), 1)
        self.assertEqual(backend.attempts, 1)
        self.assertEqual(self.saved()[digest].status, "printed")
        self.assertTrue(any("not printed again" in line for line in logs.output))

    def test_unfiled_file_that_vanished_completes_the_reservation(self) -> None:
        ibp_printing.set_backend(FakeBackend())
        watcher, _ = self.fresh()
        path = make_label(self.dir / "label.png", seed=32)
        digest = digest_of(path)
        with mock.patch(
            "ibp_printing.watcher.core.os.rename",
            side_effect=PermissionError(13, "in use"),
        ):
            watcher.process_path(path)
        path.unlink()
        self.assertEqual(watcher.reconcile_filing(), [])
        self.assertEqual(watcher.reservations()[digest].status, "printed")

    def test_unfiled_download_seen_by_restarted_watcher(self) -> None:
        backend = FakeBackend(outcome=JobOutcome.TIMEOUT)
        ibp_printing.set_backend(backend)
        first, _ = self.fresh()
        path = make_label(self.dir / "label.png", seed=33)
        with mock.patch(
            "ibp_printing.watcher.core.os.rename",
            side_effect=PermissionError(13, "in use"),
        ):
            self.assertEqual(first.process_path(path).status, "check_printer")
        backend.outcome = JobOutcome.COMPLETED
        second, _ = self.fresh()
        outcome = second.process_path(path)
        self.assertEqual(outcome.status, "filed")
        assert outcome.moved_to is not None
        self.assertEqual(outcome.moved_to.parent.name, CHECK_PRINTER_DIR)
        self.assertEqual(backend.attempts, 1)


class OnceTests(Restartable):
    """R10: a real --once run journals; only --dry-run is stateless."""

    def run_once(self, watch: Path, *extra: str) -> int:
        with (
            mock.patch.object(watcher_main, "configure_logging", quiet_configure),
            mock.patch.object(
                watcher_main, "default_state_dir", lambda: self.state_dir
            ),
            mock.patch.object(
                watcher_main, "to_print_dir", lambda: watch / TO_PRINT_DIR
            ),
        ):
            return watcher_main.main(
                [
                    "--config",
                    str(self.dir / "none.toml"),
                    "--watch-dir",
                    str(watch),
                    "--log-dir",
                    str(self.dir / "logs"),
                    "--once",
                    *extra,
                ]
            )

    def test_real_once_records_and_honours_reservations(self) -> None:
        backend = FakeBackend()
        ibp_printing.set_backend(backend)
        watch = self.dir / "downloads"
        watch.mkdir()
        queued = watch / TO_PRINT_DIR
        queued.mkdir()
        label = make_label(queued / "a.png", seed=41)
        digest = digest_of(label)
        self.assertEqual(self.run_once(watch), 0)
        self.assertEqual(len(backend.printed), 1)
        doc = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(doc["reservations"][digest]["status"], "printed")
        self.assertEqual(doc["watch_dirs"], {})  # no checkpoint from --once
        # A second --once with another copy of the label prints nothing.
        make_label(queued / "copy.png", seed=41)
        self.assertEqual(self.run_once(watch), 0)
        self.assertEqual(len(backend.printed), 1)

    def test_once_after_crash_does_not_reprint(self) -> None:
        backend = FakeBackend()
        ibp_printing.set_backend(backend)
        watch = self.dir / "downloads"
        (watch / TO_PRINT_DIR).mkdir(parents=True)
        label = make_label(watch / TO_PRINT_DIR / "a.png", seed=42)
        digest = digest_of(label)
        reservation = Reservation(
            status="filing", first_submitted=1.0, updated=1.0, file=str(label)
        )
        StateFile(self.state_path).save(watch, None, {digest: reservation})
        self.assertEqual(self.run_once(watch), 0)
        self.assertEqual(backend.attempts, 0)
        self.assertEqual(len(list((watch / CHECK_PRINTER_DIR).iterdir())), 1)

    def test_dry_run_once_stays_stateless(self) -> None:
        ibp_printing.set_backend(FakeBackend())
        watch = self.dir / "downloads"
        watch.mkdir()
        make_label(watch / "label.png")
        self.assertEqual(self.run_once(watch, "--dry-run"), 0)
        self.assertFalse(self.state_path.exists())


class SnapshotOrderTests(Restartable):
    """R11: an older snapshot can never overwrite a newer one."""

    def test_slow_heartbeat_save_cannot_drop_a_new_reservation(self) -> None:
        state = StateFile(self.state_path)
        real_save = state.save
        heartbeat_in_save = threading.Event()
        let_heartbeat_finish = threading.Event()

        def slow_first_save(*args: Any) -> bool:
            if threading.current_thread().name == "heartbeat":
                heartbeat_in_save.set()
                let_heartbeat_finish.wait(5)
            return real_save(*args)

        watcher, _ = self.make_watcher(state=state)
        with mock.patch.object(state, "save", side_effect=slow_first_save):
            heartbeat = threading.Thread(
                target=watcher._save_state, kwargs={"clean": False}, name="heartbeat"
            )
            heartbeat.start()
            self.assertTrue(heartbeat_in_save.wait(5))
            worker = threading.Thread(
                target=watcher._reserve, args=(self.dir / "a.png", "d1"), name="w"
            )
            worker.start()
            time.sleep(0.2)
            # The worker waits for the heartbeat's write instead of racing it.
            self.assertTrue(worker.is_alive())
            let_heartbeat_finish.set()
            heartbeat.join(5)
            worker.join(5)
        self.assertIn("d1", self.saved())


class UnsavedStateTests(Restartable):
    """R12: printing goes on when the state can't be saved; CRITICAL + one box."""

    def test_prints_anyway_logs_critical_and_notifies_once(self) -> None:
        backend = FakeBackend()
        ibp_printing.set_backend(backend)
        blocker = self.state_dir / "blocker"
        blocker.write_text("x")
        watcher, notifier = self.make_watcher(
            state=StateFile(blocker / "watcher-state.json")
        )
        with self.assertLogs("ibp_printing.watcher.core", logging.CRITICAL) as logs:
            first = watcher.process_path(make_label(self.dir / "a.png", seed=51))
            second = watcher.process_path(make_label(self.dir / "b.png", seed=52))
        self.assertEqual((first.status, second.status), ("printed", "printed"))
        self.assertEqual(len(backend.printed), 2)
        self.assertGreaterEqual(len(logs.records), 2)
        self.assertTrue(
            any("printing continues" in record.getMessage() for record in logs.records)
        )
        state_boxes = [t for t in notifier.texts if "cannot save its state" in t]
        self.assertEqual(len(state_boxes), 1)
        # In memory the protection still works for this run.
        make_label(self.dir / "c.png", seed=51)
        self.assertEqual(watcher.process_path(self.dir / "c.png").status, "duplicate")

    def test_state_notice_retried_until_accepted(self) -> None:
        ibp_printing.set_backend(FakeBackend())
        blocker = self.state_dir / "blocker"
        blocker.write_text("x")
        watcher, notifier = self.make_watcher(
            state=StateFile(blocker / "watcher-state.json")
        )
        refuse = mock.patch.object(notifier, "notify", return_value=False)
        with refuse, self.assertLogs("ibp_printing.watcher.core", logging.WARNING):
            watcher._save_state(clean=False)
        self.assertFalse(watcher._state_notified)
        watcher._save_state(clean=False)
        self.assertTrue(watcher._state_notified)
        self.assertEqual(len(notifier.texts), 1)


class CheckpointTests(Restartable):
    """R13: the checkpoint never passes a file that was seen but not processed."""

    def test_old_backlog_queued_but_unprocessed_holds_checkpoint(self) -> None:
        watcher, _ = self.make_watcher(state=StateFile(self.state_path))
        old = make_label(self.dir / "overnight.png", seed=61)
        yesterday = time.time() - 20 * HOUR
        with mock.patch(
            "ibp_printing.watcher.service.arrival_time", lambda path: yesterday
        ):
            self.assertTrue(watcher.enqueue(old, "startup catch-up", force=True))
        watcher.stop(timeout_s=0.1)  # the worker never ran
        folder = StateFile(self.state_path).load(self.dir).folder
        assert folder is not None
        self.assertLessEqual(folder.seen_until, yesterday)

    def test_final_scan_after_observer_stop_holds_checkpoint(self) -> None:
        backend = FakeBackend()
        ibp_printing.set_backend(backend)
        state = StateFile(self.state_path)
        watcher, _ = self.make_watcher(state=state)
        watcher.start()
        real_stop = watcher._observer.stop
        late: list[Path] = []

        def stop_then_download() -> None:
            real_stop()
            watcher._observer.join(5)
            # Lands after the observer stopped: no event will ever arrive.
            late.append(make_label(self.dir / "late.png", seed=62))

        watcher._observer.stop = stop_then_download
        with self.assertLogs("ibp_printing.watcher.service", logging.WARNING) as logs:
            watcher.stop()
        self.assertTrue(any("final scan" in line for line in logs.output))
        folder = StateFile(self.state_path).load(self.dir).folder
        assert folder is not None
        arrived = core.arrival_time(late[0])
        assert arrived is not None
        self.assertLessEqual(folder.seen_until, arrived)
        self.assertEqual(backend.attempts, 0)
        # The next start prints it.
        again, _ = self.make_watcher(state=StateFile(self.state_path))
        again.start()
        self.addCleanup(again.stop)
        self.assertTrue(wait_for(lambda: not late[0].exists()))
        self.assertEqual(backend.attempts, 1)

    def test_final_scan_skips_decided_and_startup_files(self) -> None:
        ibp_printing.set_backend(FakeBackend())
        watcher, _ = self.make_watcher(state=StateFile(self.state_path))
        old = make_label(self.dir / "old.png", seed=63)
        watcher.snapshot_existing()
        photo = make_label(self.dir / "photo.png", size=(1600, 1200), seed=64)
        self.assertEqual(watcher.process_path(photo).status, "not_label")
        watcher._hold_back_unseen()
        self.assertNotIn(old, watcher._first_seen)
        self.assertNotIn(photo, watcher._first_seen)


class NotifyOnceTests(TempDirTest):
    """R14: a refused notification is not marked delivered."""

    def test_refused_notification_is_retried(self) -> None:
        watcher, notifier = self.make_watcher()
        path = self.dir / "a.png"
        with mock.patch.object(notifier, "notify", return_value=False):
            watcher._notify_once(path, "kind", "text")
        self.assertEqual(watcher._notified, set())
        watcher._notify_once(path, "kind", "text")
        watcher._notify_once(path, "kind", "text")
        self.assertEqual(notifier.texts, ["text"])

    def test_unavailable_boxes_are_logged_once(self) -> None:
        watcher, notifier = self.make_watcher()
        notifier._custom = False
        with mock.patch("ibp_printing.watcher.notify.sys.platform", "linux"):
            with self.assertLogs("ibp_printing.watcher.core", logging.WARNING):
                watcher._notify_once(self.dir / "a.png", "kind", "text")
        self.assertEqual(len(watcher._notified), 1)


class ToPrintDeferralTests(TempDirTest):
    """R15: locked to-print files get bounded retries and one notification."""

    def test_locked_queued_file_gives_up_after_bounded_retries(self) -> None:
        backend = FakeBackend()
        ibp_printing.set_backend(backend)
        watcher, notifier = self.make_watcher()
        watcher._sleep = lambda seconds: None
        queued = self.queue_label("a.png", seed=71)
        statuses = []
        with mock.patch.object(
            Path, "read_bytes", side_effect=PermissionError(13, "locked")
        ):
            for _ in range(core.MAX_DEFERRED_RETRIES + 2):
                statuses.extend(o.status for o in watcher.retry_queue_once("tick"))
        self.assertEqual(
            statuses, ["unreadable"] * core.MAX_DEFERRED_RETRIES + ["gave_up"]
        )
        self.assertEqual(len(notifier.texts), 1)
        self.assertIn("could not be read", notifier.texts[0])
        self.assertEqual(watcher.queued_files(), [])
        # Renamed (the box says so): it is picked up and printed.
        renamed = queued.rename(queued.with_name("b.png"))
        self.assertEqual(
            [o.status for o in watcher.retry_queue_once("tick")], ["printed"]
        )
        self.assertFalse(renamed.exists())

    def test_one_locked_file_does_not_stop_the_others(self) -> None:
        backend = FakeBackend()
        ibp_printing.set_backend(backend)
        watcher, _ = self.make_watcher()
        watcher._sleep = lambda seconds: None
        locked = self.queue_label("a.png", seed=72, age_s=100)
        self.queue_label("b.png", seed=73, age_s=10)
        real = Path.read_bytes

        def read(path: Path) -> bytes:
            if path == locked:
                raise PermissionError(13, "locked")
            return real(path)

        with mock.patch.object(Path, "read_bytes", read):
            statuses = [o.status for o in watcher.retry_queue_once("tick")]
        self.assertEqual(statuses, ["unreadable", "printed"])


class AppQueueTests(TempDirTest):
    """R16: the folder the apps save into is retried too."""

    def test_extra_to_print_dir_is_retried_and_filed_beside_it(self) -> None:
        backend = FakeBackend()
        ibp_printing.set_backend(backend)
        downloads = self.dir / "real-downloads"
        app_queue = downloads / TO_PRINT_DIR
        app_queue.mkdir(parents=True)
        watch = self.dir / "custom"
        watch.mkdir()
        config_watcher, _ = self.make_watcher()
        watcher = LabelWatcher(
            config_watcher.config,
            watch,
            notifier=RecordingNotifier(),
            to_print_dirs=[app_queue],
        )
        label = make_label(app_queue / "app.png", seed=81)
        outcomes = watcher.retry_queue_once("test")
        self.assertEqual([o.status for o in outcomes], ["printed"])
        self.assertFalse(label.exists())
        assert outcomes[0].moved_to is not None
        self.assertEqual(outcomes[0].moved_to.parent, downloads / PRINTED_DIR)
        self.assertEqual(watcher.to_print_dir, watch / TO_PRINT_DIR)

    def test_main_lists_both_queues_only_when_different(self) -> None:
        watch = self.dir / "custom"
        with mock.patch.object(
            watcher_main, "to_print_dir", lambda: self.dir / "dl" / TO_PRINT_DIR
        ):
            with self.assertLogs("ibp_printing.watcher", logging.WARNING):
                dirs = watcher_main.to_print_dirs(watch)
        self.assertEqual(dirs, [watch / TO_PRINT_DIR, self.dir / "dl" / TO_PRINT_DIR])
        with mock.patch.object(
            watcher_main, "to_print_dir", lambda: watch / TO_PRINT_DIR
        ):
            self.assertEqual(watcher_main.to_print_dirs(watch), [watch / TO_PRINT_DIR])

    def test_duplicate_folders_are_merged(self) -> None:
        watcher, _ = self.make_watcher(to_print_dirs=None)
        again = LabelWatcher(
            watcher.config, self.dir, to_print_dirs=[self.dir / TO_PRINT_DIR]
        )
        self.assertEqual(again.to_print_dirs, [self.dir / TO_PRINT_DIR])


class BoundTests(Restartable):
    """R8: the reservation map stays bounded."""

    def test_cap_drops_oldest_printed_first(self) -> None:
        watcher, _ = self.make_watcher()
        now = time.time()
        with mock.patch.object(core, "MAX_RESERVATIONS", 3):
            watcher._reservations = {
                "u-old": Reservation("uncertain", 1.0, 1.0),
                "p-old": Reservation("printed", now - 10, now - 10, completed=now),
                "p-new": Reservation("printed", now - 5, now - 5, completed=now),
                "f": Reservation("filing", 0.5, 0.5),
            }
            with self.assertLogs("ibp_printing.watcher.core", logging.WARNING):
                watcher._prune_reservations()
        self.assertEqual(sorted(watcher._reservations), ["f", "p-new", "u-old"])


if __name__ == "__main__":
    unittest.main()
