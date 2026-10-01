"""Command-line entry point: ``ibp-label-watcher`` / ``ibp-label-watcherw``.

Also runnable as ``python -m ibp_printing.watcher`` (or ``pythonw -m ...``).
"""

import argparse
import importlib.metadata
import logging
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
from ibp_printing.watcher.config import WatcherConfig, load_config
from ibp_printing.watcher.folders import downloads_dir
from ibp_printing.watcher.instance import LOCK_FILENAME, SingleInstance
from ibp_printing.watcher.notify import MB_ICONERROR, MB_SETFOREGROUND, message_box
from ibp_printing.watcher.service import LabelWatcher

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
    parser.add_argument("--log-dir", type=Path, help="folder for printer.log/.jsonl")
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
        log_event(logger, logging.INFO, "PrintService event", **event)


def run(argv: Optional[Sequence[str]] = None) -> int:
    """Parse arguments, set everything up, and run until stopped."""
    args = build_parser().parse_args(argv)
    config, config_path, warnings = load_config(args.config)
    overrides = apply_overrides(config, args)

    log_dir = configure_logging(config.log_dir, console=True)
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
    )
    for warning in warnings:
        log_event(logger, logging.WARNING, "config warning", warning=warning)

    instance = SingleInstance(log_dir.parent / LOCK_FILENAME)
    if not instance.acquire():
        _fatal_box(
            "The IBP label watcher is already running.\n\n"
            "Only one copy can run at a time."
        )
        return EXIT_ALREADY_RUNNING

    try:
        return _run_locked(config, args)
    finally:
        instance.release()


def _run_locked(config: WatcherConfig, args: argparse.Namespace) -> int:
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

    stop = threading.Event()
    watcher = LabelWatcher(config, watch_dir, stop_event=stop)
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
