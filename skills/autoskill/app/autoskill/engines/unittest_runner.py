"""Run the original unittest CLI with a result observer, preserving suites and fixture lifecycles."""
from __future__ import annotations

import json
import os
import runpy
import sys
import time
import unittest
from pathlib import Path
from typing import ClassVar


class Result(unittest.TextTestResult):
    records: ClassVar[list[dict]] = []

    def journal(self, event, **fields):
        file = Path(os.environ["AUTOSKILL_UNITTEST_REPORT"]).with_suffix(".jsonl")
        with file.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"event": event, **fields}, ensure_ascii=True) + "\n")

    def startTest(self, test):  # noqa: N802  # unittest defines this callback name.
        self.current_test = test
        self.started = time.monotonic()
        self.journal("start", id=test.id(), title=str(test))
        super().startTest(test)

    def stopTest(self, test):  # noqa: N802  # unittest defines this callback name.
        self.journal("stop")
        super().stopTest(test)
        self.current_test = None

    def record(self, test, status, reason="", *, started=True):
        row = {"id": test.id(), "title": str(test), "status": status, "reason": reason, "target_started": started,
               "duration_ms": round((time.monotonic() - getattr(self, "started", time.monotonic())) * 1000)}
        self.journal("result", row=row)
        self.records.append(row)

    def addSuccess(self, test):  # noqa: N802  # unittest defines this callback name.
        super().addSuccess(test)
        self.record(test, "Pass")

    def addFailure(self, test, err):  # noqa: N802  # unittest defines this callback name.
        super().addFailure(test, err)
        self.record(test, "Fail", self._exc_info_to_string(err, test))

    def addError(self, test, err):  # noqa: N802  # unittest defines this callback name.
        super().addError(test, err)
        self.record(test, "Blocked", self._exc_info_to_string(err, test), started=test is getattr(self, "current_test", None))

    def addSkip(self, test, reason):  # noqa: N802  # unittest defines this callback name.
        super().addSkip(test, reason)
        self.record(test, "Blocked", reason, started=False)

    def addExpectedFailure(self, test, err):  # noqa: N802  # unittest defines this callback name.
        super().addExpectedFailure(test, err)
        self.record(test, "Blocked", "expected failure: " + self._exc_info_to_string(err, test))

    def addUnexpectedSuccess(self, test):  # noqa: N802  # unittest defines this callback name.
        super().addUnexpectedSuccess(test)
        self.record(test, "Fail", "unexpected success")

    def addSubTest(self, test, subtest, err):  # noqa: N802  # unittest defines this callback name.
        super().addSubTest(test, subtest, err)
        self.record(subtest, "Pass" if err is None else "Fail" if issubclass(err[0], test.failureException) else "Blocked",
                    "" if err is None else self._exc_info_to_string(err, test))


def main() -> None:
    report = Path(os.environ["AUTOSKILL_UNITTEST_REPORT"])
    unittest.TextTestRunner.resultclass = Result
    entry = json.loads(os.environ["AUTOSKILL_UNITTEST_ENTRY"])
    sys.argv = entry
    sys.path.insert(0, os.getcwd())
    try:
        if entry[0] == "-m":
            sys.argv = entry[1:]
            runpy.run_module(entry[1], run_name="__main__", alter_sys=True)
        else:
            runpy.run_path(entry[0], run_name="__main__")
    finally:
        temporary = report.with_suffix(".tmp")
        temporary.write_text(json.dumps({"cases": Result.records}, ensure_ascii=False), encoding="utf-8")
        temporary.replace(report)


if __name__ == "__main__":
    main()
