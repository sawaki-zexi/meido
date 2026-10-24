"""Replay public GitHub rules/check payloads through the audit CLI."""

import json
from pathlib import Path
import subprocess
import sys
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/required_checks.py"
NAMES = ["Frontend build", "Backend tests (core)", "Backend tests (channels)", "Backend tests (drift)"]


def check(name, conclusion="SUCCESS", status="COMPLETED"):
    return {"__typename": "CheckRun", "name": name, "conclusion": conclusion, "status": status}


class RequiredChecksAuditTests(unittest.TestCase):
    def audit(self, checks, rules=None):
        snapshot = {"rules": rules if rules is not None else [{"type": "required_status_checks", "parameters": {
            "required_status_checks": [{"context": name} for name in NAMES]}}],
            "pull_request": {"statusCheckRollup": checks}}
        return subprocess.run([sys.executable, str(SCRIPT), "--snapshot", "-"], input=json.dumps(snapshot),
                              text=True, capture_output=True)

    def test_original_successful_run_still_has_three_missing_required_checks(self):
        result = self.audit([check("Frontend build"), check("Detect changed scope"),
                             check("Backend tests (${{ matrix.group }})", "SKIPPED")])
        self.assertEqual(result.returncode, 1)
        for name in NAMES[1:]:
            self.assertIn(f"MISSING: {name}", result.stdout)

    def test_all_required_checks_can_complete_without_running_unrelated_tests(self):
        result = self.audit([check(name, "SKIPPED" if name == "Frontend build" else "SUCCESS") for name in NAMES])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("4/4 satisfied", result.stdout)

    def test_failed_cancelled_timed_out_or_waiting_backend_never_passes(self):
        for conclusion, status in [("FAILURE", "COMPLETED"), ("CANCELLED", "COMPLETED"),
                                   ("TIMED_OUT", "COMPLETED"), (None, "QUEUED"), (None, "IN_PROGRESS")]:
            with self.subTest(conclusion=conclusion, status=status):
                result = self.audit([check(name, conclusion, status) if name == NAMES[1] else check(name) for name in NAMES])
                self.assertEqual(result.returncode, 1)

    def test_a_failing_commit_status_with_the_same_name_cannot_be_hidden_by_a_passing_check(self):
        result = self.audit([*[check(name) for name in NAMES],
                             {"__typename": "StatusContext", "context": NAMES[1], "state": "FAILURE"}])
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"FAIL: {NAMES[1]}", result.stdout)

    def test_missing_protection_configuration_is_an_error(self):
        self.assertEqual(self.audit([check(name) for name in NAMES], rules=[]).returncode, 2)


if __name__ == "__main__":
    unittest.main()
