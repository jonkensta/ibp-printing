"""Command-line entry point: ``ibp-label-watcher`` / ``ibp-label-watcherw``.

Also runnable as ``python -m ibp_printing.watcher`` (or ``pythonw -m ...``).
"""

import argparse
import importlib.metadata
import logging
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Any, Optional, Sequence

import ibp_printing
from ibp_printing.log import (
    configure_logging,
    describe_exception,
    get_logger,
    install_exception_hooks,
    log_event,
)
from ibp_printing.paths import TO_PRINT_DIR, downloads_dir, to_print_dir
from ibp_printing.watcher.config import WatcherConfig, load_config
from ibp_printing.watcher.instance import SingleInstance
from ibp_printing.watcher.notify import MB_ICONERROR, MB_SETFOREGROUND, message_box
from ibp_printing.watcher.service import LabelWatcher
from ibp_printing.watcher.state import (
    LOCK_FILENAME,
    STATE_FILENAME,
    StateFile,
    default_state_dir,
)

logger = get_logger("watcher")

EXIT_OK = 0
EXIT_ALREADY_RUNNING = 1
EXIT_BAD_WATCH_DIR = 2


def build_parser() -> argparse.ArgumentParser:
    """The command-line interface."""
    parser = argparse.ArgumentParser(
        prog="ibp-label-watcher",
        description=(
            "Watch the Downloads folder and print downloaded EasyPost 4x6 "
            "labels on the first available USB label printer."
        ),
    )
    parser.add_argument("--config", type=Path, help="path to watcher.toml")
    parser.add_argument("--watch-dir", type=Path, help="folder to watch")
    parser.add_argument(
        "--log-dir", type=Path, help="folder for printer-watcher.log/.jsonl"
    )
    parser.add_argument(
        "--process-existing",
        action="store_true",
        help="also print labels already in the folder at startup",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="do everything except printing; log what would print, move nothing",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="process files already in the folder, then exit",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="show debug messages on the console (files always get everything)",
    )
    return parser


def apply_overrides(config: WatcherConfig, args: argparse.Namespace) -> list[str]:
    """Apply command-line flags on top of the file config. Returns what changed."""
    changed = []
    if args.watch_dir is not None:
        config.watch_dir = args.watch_dir
        changed.append("watch_dir")
    if args.log_dir is not None:
        config.log_dir = args.log_dir
        changed.append("log_dir")
    if args.process_existing:
        config.process_existing = True
        changed.append("process_existing")
    if args.dry_run:
        config.dry_run = True
        changed.append("dry_run")
    return changed


def _has_console() -> bool:
    return sys.stderr is not None


def _fatal_box(text: str) -> None:
    """Tell a pythonw user something went wrong (they have no console)."""
    if sys.platform == "win32" and not _has_console():
        try:
            message_box(text, flags=MB_ICONERROR | MB_SETFOREGROUND)
        except Exception:  # pylint: disable=broad-exception-caught
            logger.exception("could not show message box")


def _install_signal_handlers(stop: threading.Event) -> None:
    def _handler(signum: int, _frame: Any) -> None:
        log_event(logger, logging.INFO, "signal received; shutting down", signal=signum)
        stop.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError) as exc:
            log_event(
                logger,
                logging.DEBUG,
                "could not install signal handler",
                signal=name,
                error=describe_exception(exc),
            )


def _log_startup(watcher: LabelWatcher) -> None:
    backend = ibp_printing.get_backend()
    log_event(
        logger,
        logging.INFO,
        "printer backend",
        backend=type(backend).__name__,
        platform=backend.platform_name,
    )
    watcher.log_discovery("startup")
    try:
        events = backend.recent_print_events(minutes=60)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        log_event(
            logger,
            logging.WARNING,
            "could not read print events",
            error=describe_exception(exc),
        )
        events = []
    log_event(
        logger, logging.INFO, "PrintService events, last 60 min", count=len(events)
    )
    for event in events:
        log_event(logger, logging.INFO, "PrintService event", event=event)


def run(argv: Optional[Sequence[str]] = None) -> int:
    """Parse arguments, take the lock, set up logging, and run until stopped."""
    args = build_parser().parse_args(argv)
    config, config_path, warnings = load_config(args.config)
    overrides = apply_overrides(config, args)

    # The lock lives at a fixed per-user place (not under --log-dir), and is
    # taken before logging opens any file, so a second copy never touches the
    # running watcher's rotating log files.
    state_dir = default_state_dir()
    instance = SingleInstance(state_dir / LOCK_FILENAME)
    if not instance.acquire():
        if _has_console():
            print(
                "ibp-label-watcher: another label watcher is already running "
                f"(lock {instance.lock_path}); exiting.",
                file=sys.stderr,
            )
        _fatal_box(
            "The IBP label watcher is already running.\n\n"
            "Only one copy can run at a time."
        )
        return EXIT_ALREADY_RUNNING

    try:
        configure_logging(config.log_dir, app="watcher", console=True)
        if args.verbose:
            for handler in logging.getLogger("ibp_printing").handlers:
                # File handlers subclass StreamHandler; only the console one changes.
                if type(handler) is logging.StreamHandler:  # pylint: disable=C0123
                    handler.setLevel(logging.DEBUG)
        install_exception_hooks()

        log_event(
            logger,
            logging.INFO,
            "label watcher starting",
            version=_version(),
            argv=sys.argv,
            config_file=str(config_path),
            config_file_exists=config_path.exists(),
            cli_overrides=overrides,
            console=_has_console(),
            lock=str(instance.lock_path),
            state_dir=str(state_dir),
        )
        for warning in warnings:
            log_event(logger, logging.WARNING, "config warning", warning=warning)
        return _run_locked(config, args, state_dir)
    finally:
        instance.release()


def _run_locked(
    config: WatcherConfig, args: argparse.Namespace, state_dir: Path
) -> int:
    watch_dir = Path(config.watch_dir) if config.watch_dir else downloads_dir()
    watch_dir = watch_dir.expanduser().resolve()
    config.watch_dir = watch_dir
    log_event(logger, logging.INFO, "effective config", **config.to_log())

    if not watch_dir.is_dir():
        log_event(
            logger,
            logging.CRITICAL,
            "watch folder does not exist",
            watch_dir=str(watch_dir),
        )
        _fatal_box(f"The label watcher cannot find the folder:\n{watch_dir}")
        return EXIT_BAD_WATCH_DIR

    # A dry run is stateless: it must not move the "seen up to" mark (files it
    # only pretended to print would then be skipped by the next real run) nor
    # record reservations. A real --once run prints for real, so it reads and
    # writes the content reservations, but leaves the Downloads checkpoint.
    state: Optional[StateFile] = None
    if config.dry_run:
        log_event(
            logger,
            logging.INFO,
            "dry run: state file not read or written; files already in the "
            "folder are handled as on a first run",
        )
    else:
        state = StateFile(state_dir / STATE_FILENAME)
        if args.once:
            log_event(
                logger,
                logging.INFO,
                "--once: label reservations are read and saved; the Downloads "
                "checkpoint is left unchanged",
            )
    queues = to_print_dirs(watch_dir)
    stop = threading.Event()
    watcher = LabelWatcher(
        config,
        watch_dir,
        stop_event=stop,
        state=state,
        to_print_dirs=queues,
        checkpoint=not args.once,
    )
    _log_startup(watcher)

    if args.once:
        outcomes = watcher.process_existing_now()
        log_event(
            logger,
            logging.INFO,
            "--once finished",
            outcomes=[outcome.to_log() for outcome in outcomes],
        )
        return EXIT_OK

    _install_signal_handlers(stop)
    try:
        monitor_threads = ibp_printing.get_backend().start_device_monitor(stop)
        log_event(
            logger,
            logging.INFO,
            "device monitor started",
            threads=[thread.name for thread in monitor_threads],
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        log_event(
            logger,
            logging.ERROR,
            "device monitor failed to start",
            error=describe_exception(exc),
        )

    heartbeat = threading.Thread(
        target=watcher.heartbeat_loop, name="heartbeat", daemon=True
    )
    heartbeat.start()
    watcher.start()
    log_event(logger, logging.INFO, "label watcher running; Ctrl+C to stop")
    try:
        while not stop.wait(0.5):
            pass
    except KeyboardInterrupt:
        log_event(logger, logging.INFO, "Ctrl+C; shutting down")
    finally:
        watcher.stop()
        heartbeat.join(2.0)
    log_event(logger, logging.INFO, "label watcher exited cleanly", stats=watcher.stats)
    return EXIT_OK


def to_print_dirs(watch_dir: Path) -> list[Path]:
    """The to-print folders to retry: ``<watch_dir>/to-print`` first, plus the
    folder shippy/shippy-gui save into (``paths.to_print_dir()``) when it is a
    different one (the watcher watches a custom folder). Both are logged."""
    own = watch_dir / TO_PRINT_DIR
    try:
        apps = to_print_dir().expanduser().resolve()
    except Exception as exc:  # pylint: disable=broad-exception-caught
        log_event(
            logger,
            logging.WARNING,
            "could not work out where the apps save unprinted labels; retrying "
            "only the watch folder's to-print",
            error=describe_exception(exc),
        )
        apps = own
    same = os.path.normcase(str(apps)) == os.path.normcase(str(own))
    log_event(
        logger,
        logging.INFO if same else logging.WARNING,
        "to-print folders retried",
        watch_folder_queue=str(own),
        app_queue=str(apps),
        note=(
            "same folder"
            if same
            else "the apps save labels outside the watch folder; both are retried"
        ),
    )
    return [own] if same else [own, apps]


def _version() -> str:
    try:
        return importlib.metadata.version("ibp-printing")
    except Exception:  # pylint: disable=broad-exception-caught
        return "unknown"


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Console/gui-script entry point; returns the process exit code."""
    try:
        return run(argv)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        logger.info("interrupted during startup")
        return 130
    except BaseException:  # pylint: disable=broad-exception-caught
        logger.critical("label watcher crashed", exc_info=True)
        _fatal_box(
            "The IBP label watcher crashed and has stopped.\n\n"
            "Please tell the shipping coordinator. Details are in the log folder."
        )
        return 3


if __name__ == "__main__":
    sys.exit(main())
