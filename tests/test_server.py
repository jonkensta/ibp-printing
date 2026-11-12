"""ibp-print-server: the HTTP contract the IBP frontend uses, with a fake backend."""

import http.client
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Optional
from unittest import mock

from PIL import Image
from printing_helpers import FakeBackend, PrintError, dymo, quiet_logging

import ibp_printing
from ibp_printing import PrintQueue, server
from ibp_printing.models import JobOutcome, PrintResult

LABEL = "iDPRT_SP310_0a5f:0001"
OFFICE = "Office_Laser"


def png(size: tuple[int, int] = (1050, 420)) -> bytes:
    """A label-shaped PNG."""
    out = io.BytesIO()
    Image.new("L", size, 255).save(out, format="PNG")
    return out.getvalue()


class RecordingBackend(FakeBackend):
    """FakeBackend that also keeps the size of every image it printed."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.sizes: list[tuple[int, int]] = []
        self.done = threading.Condition()

    def print_image(self, img, printer_name, *, job_name, track_timeout_s=0.0):
        with self.done:
            self.sizes.append(img.size)
            self.done.notify_all()
        return super().print_image(
            img, printer_name, job_name=job_name, track_timeout_s=track_timeout_s
        )

    def wait_for(self, count: int, timeout_s: float = 5.0) -> None:
        """Block until ``count`` images were printed."""
        with self.done:
            if not self.done.wait_for(lambda: len(self.sizes) >= count, timeout_s):
                raise AssertionError(f"printed {len(self.sizes)} of {count}")


class BrokenBackend(FakeBackend):
    """Discovery itself fails."""

    def discover(self):
        raise RuntimeError("CUPS is down")


class ServerTestCase(unittest.TestCase):
    """Starts a PrintServer on a free port around each test."""

    def setUp(self):
        quiet_logging(self)
        self.addCleanup(ibp_printing.set_backend, None)

    def start(
        self, backend: FakeBackend, preferred: Optional[str] = None
    ) -> server.PrintServer:
        """Serve with ``backend`` installed; stopped at cleanup."""
        ibp_printing.set_backend(backend)
        srv = server.PrintServer(("127.0.0.1", 0), server.Printers(preferred))
        srv.start()
        self.addCleanup(srv.stop, 5.0)
        self.srv = srv  # pylint: disable=attribute-defined-outside-init
        return srv

    def label_printer(self, **kwargs) -> RecordingBackend:
        """One plugged-in label printer and an office printer (never usable)."""
        return RecordingBackend(
            [PrintQueue(OFFICE), PrintQueue(LABEL)], [dymo("0A5F:0001")], **kwargs
        )

    def request(
        self,
        method: str,
        path: str,
        body: Optional[bytes] = None,
        headers: Optional[dict] = None,
    ) -> tuple[int, dict, dict]:
        """Send one request; returns ``(status, headers, json body or {})``."""
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=5)
        self.addCleanup(conn.close)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        raw = response.read()
        payload = json.loads(raw) if raw else {}
        return response.status, dict(response.getheaders()), payload


class HealthTests(ServerTestCase):
    """GET /health."""

    def test_lists_usable_label_printers(self):
        self.start(self.label_printer())
        status, headers, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
        self.assertEqual(
            body,
            {
                "status": "ok",
                "service": "print-server",
                "printers": {"count": 1, "names": [LABEL]},
            },
        )

    def test_no_printers(self):
        self.start(RecordingBackend([PrintQueue(LABEL)], []))
        _, _, body = self.request("GET", "/health")
        self.assertEqual(body["printers"], {"count": 0, "names": []})

    def test_degraded_when_printers_cannot_be_listed(self):
        self.start(BrokenBackend([], []))
        status, _, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "degraded")
        self.assertEqual(body["service"], "print-server")
        self.assertIn("CUPS is down", body["error"])

    def test_unknown_path_is_404_with_cors(self):
        self.start(self.label_printer())
        status, headers, body = self.request("GET", "/nope")
        self.assertEqual(status, 404)
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
        self.assertEqual(body["status"], "error")


class PreflightTests(ServerTestCase):
    """OPTIONS."""

    def test_cors_preflight(self):
        self.start(self.label_printer())
        status, headers, _ = self.request(
            "OPTIONS",
            "/print-image",
            headers={"Access-Control-Request-Private-Network": "true"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
        self.assertEqual(headers["Access-Control-Allow-Methods"], "POST, GET, OPTIONS")
        self.assertEqual(headers["Access-Control-Allow-Headers"], "Content-Type")
        self.assertEqual(headers["Access-Control-Allow-Private-Network"], "true")


class PrintImageTests(ServerTestCase):
    """POST /print-image."""

    def post(self, data: bytes, path: str = "/print-image"):
        """Post ``data`` as a PNG."""
        return self.request(
            "POST", path, body=data, headers={"Content-Type": "image/png"}
        )

    def test_queues_and_prints_on_the_label_printer(self):
        backend = self.label_printer()
        self.start(backend)
        status, headers, body = self.post(png())
        self.assertEqual((status, body), (200, {"status": "queued"}))
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
        backend.wait_for(1)
        self.assertEqual(backend.sizes, [(1050, 420)])
        self.assertEqual(backend.printed[0][0], LABEL)
        self.assertTrue(backend.printed[0][1].startswith(server.JOB_NAME + " ["))

    def test_jobs_are_tracked_and_failed_outcomes_logged(self):
        backend = self.label_printer(outcomes={LABEL: JobOutcome.ERROR})
        self.start(backend)
        with self.assertLogs("ibp_printing.server", level="ERROR") as logs:
            self.assertEqual(self.post(png())[0], 200)
            backend.wait_for(1)
            deadline = time.monotonic() + 5
            while not logs.output and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertEqual(backend.printed[0][2], server.TRACK_TIMEOUT_S)
        self.assertIn("label may not have printed (error)", logs.output[0])

    def test_query_string_is_ignored(self):
        backend = self.label_printer()
        self.start(backend)
        status, _, _ = self.post(png(), "/print-image?media=label")
        self.assertEqual(status, 200)
        backend.wait_for(1)

    def test_jobs_print_one_at_a_time_in_order(self):
        backend = self.label_printer()
        self.start(backend)
        sizes = [(100 + i, 40) for i in range(5)]
        for size in sizes:
            self.assertEqual(self.post(png(size))[0], 200)
        backend.wait_for(len(sizes))
        self.assertEqual(backend.sizes, sizes)

    def test_not_an_image(self):
        backend = self.label_printer()
        self.start(backend)
        for data in (b"", b"not a png", png()[:40]):
            with self.subTest(data=data[:12]):
                status, headers, body = self.post(data)
                self.assertEqual(status, 400)
                self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
                self.assertEqual(
                    body, {"status": "error", "error": "Body is not a valid image"}
                )
        self.assertTrue(self.srv.jobs.empty())

    def test_too_large(self):
        self.start(self.label_printer())
        status, _, body = self.post(b"\0" * (server.MAX_PAYLOAD_SIZE + 1))
        self.assertEqual(status, 413)
        self.assertEqual(body["status"], "error")

    def test_missing_content_length(self):
        self.start(self.label_printer())
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=5)
        self.addCleanup(conn.close)
        conn.putrequest("POST", "/print-image")
        conn.endheaders()
        response = conn.getresponse()
        self.assertEqual(response.status, 400)
        self.assertIn("Content-Length", json.loads(response.read())["error"])

    def test_no_printer_is_503(self):
        backend = RecordingBackend([PrintQueue(LABEL), PrintQueue(OFFICE)], [])
        self.start(backend)
        status, headers, body = self.post(png())
        self.assertEqual(status, 503)
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
        self.assertEqual(
            body, {"status": "error", "error": "No label printers are available"}
        )
        self.assertTrue(self.srv.jobs.empty())

    def test_other_post_paths_are_404(self):
        # The old form-encoded "data=<label json>" path is gone.
        self.start(self.label_printer())
        status, _, body = self.request(
            "POST",
            "/",
            body=b"data=%7B%7D",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual((status, body["status"]), (404, "error"))

    def test_print_failure_does_not_stop_the_worker(self):
        backend = self.label_printer(fail={LABEL: PrintError("printer busy")})
        self.start(backend)
        self.assertEqual(self.post(png((10, 4)))[0], 200)
        self.assertEqual(self.post(png((20, 8)))[0], 200)
        backend.wait_for(2)
        self.assertEqual(backend.sizes, [(10, 4), (20, 8)])


class PreferredPrinterTests(ServerTestCase):
    """--printer: only that printer, if it exists (plugged in or not)."""

    def test_preferred_printer_is_used(self):
        backend = self.label_printer()
        self.start(backend, preferred=OFFICE)
        _, _, body = self.request("GET", "/health")
        self.assertEqual(body["printers"]["names"], [OFFICE])
        status, _, _ = self.request("POST", "/print-image", body=png())
        self.assertEqual(status, 200)
        backend.wait_for(1)
        self.assertEqual(backend.printed[0][0], OFFICE)

    def test_missing_preferred_printer_means_no_printer(self):
        self.start(self.label_printer(), preferred="Gone_0922:0028")
        _, _, body = self.request("GET", "/health")
        self.assertEqual(body["printers"], {"count": 0, "names": []})
        status, _, _ = self.request("POST", "/print-image", body=png())
        self.assertEqual(status, 503)


class StopTests(ServerTestCase):
    """stop() lets the current label finish and drops the queued ones."""

    def test_stop_drops_queued_labels(self):
        release = threading.Event()

        class SlowBackend(RecordingBackend):
            """Holds the first print until released."""

            def print_image(self, *args, **kwargs):
                release.wait(5)
                return super().print_image(*args, **kwargs)

        backend = SlowBackend([PrintQueue(LABEL)], [dymo("0A5F:0001")])
        ibp_printing.set_backend(backend)
        srv = server.PrintServer(("127.0.0.1", 0))
        srv.start()
        for size in ((10, 4), (20, 8), (30, 12)):
            self.assertEqual(srv.submit(png(size))[0], 200)
        deadline = time.monotonic() + 5
        while srv.jobs.qsize() > 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        stopper = threading.Thread(target=srv.stop, args=(5.0,))
        stopper.start()
        release.set()
        stopper.join(10)
        self.assertFalse(stopper.is_alive())
        self.assertEqual(backend.sizes, [(10, 4)])
        self.assertEqual(srv.jobs.qsize(), 2)


class CliTests(unittest.TestCase):
    """The ibp-print-server subcommands."""

    def setUp(self):
        quiet_logging(self)
        self.addCleanup(ibp_printing.set_backend, None)

    def test_list_and_test_delegate_to_ibp_print_diag(self):
        cases = {
            ("list",): ["--events", "0"],
            ("test",): ["--events", "0", "--test-print"],
            ("test", "--printer", LABEL): ["--events", "0", "--test-print", LABEL],
        }
        for argv, expected in cases.items():
            with self.subTest(argv=argv):
                with mock.patch.object(
                    server.diagnostics, "main", return_value=0
                ) as diag:
                    self.assertEqual(server.main(list(argv)), 0)
                diag.assert_called_once_with(expected)

    def test_print_prints_an_image_file(self):
        backend = RecordingBackend([PrintQueue(LABEL)], [dymo("0A5F:0001")])
        ibp_printing.set_backend(backend)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "label.png"
            path.write_bytes(png())
            with (
                mock.patch.object(server, "configure_logging"),
                mock.patch.object(server, "install_exception_hooks"),
                mock.patch("builtins.print"),
            ):
                code = server.main(["print", str(path)])
        self.assertEqual(code, 0)
        self.assertEqual(backend.sizes, [(1050, 420)])

    def test_print_job_never_raises(self):
        printers = mock.Mock(spec=server.Printers)
        printers.print.side_effect = ValueError("bug")
        with self.assertLogs("ibp_printing", level="ERROR"):
            self.assertIsNone(server.print_job(server.PrintJob(png()), printers))
        printers.print.side_effect = None
        printers.print.return_value = PrintResult(printer_name=LABEL, job_name="x")
        self.assertIsNotNone(server.print_job(server.PrintJob(png()), printers))


if __name__ == "__main__":
    unittest.main()
