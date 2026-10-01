"""Run the suite verbosely with a per-test hang watchdog (used by CI).

Usage: python tests/run_tests.py [per-test-timeout-seconds]

If any single test runs longer than the timeout, every thread's stack is
dumped to stderr and the process exits, so a hang shows where it is stuck
instead of eating the CI job's whole time budget.
"""

import faulthandler
import os
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))


class WatchdogResult(unittest.TextTestResult):
    """Arms faulthandler's timer around each test."""

    timeout = 120.0

    def startTest(self, test):
        faulthandler.dump_traceback_later(self.timeout, exit=True)
        super().startTest(test)

    def stopTest(self, test):
        super().stopTest(test)
        faulthandler.cancel_dump_traceback_later()


def main() -> int:
    if len(sys.argv) > 1:
        WatchdogResult.timeout = float(sys.argv[1])
    faulthandler.enable()
    suite = unittest.defaultTestLoader.discover(TESTS_DIR, top_level_dir=TESTS_DIR)
    runner = unittest.TextTestRunner(verbosity=2, resultclass=WatchdogResult)
    return 0 if runner.run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
