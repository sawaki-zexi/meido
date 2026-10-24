"""Regression checks for the GitHub status checks required by Protect main."""

from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]


class RequiredChecksTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with (ROOT / ".github/workflows/ci.yml").open(encoding="utf-8") as source:
            # BaseLoader preserves GitHub's `on` key instead of YAML 1.1 booleans.
            cls.workflow = yaml.load(source, Loader=yaml.BaseLoader)

    def test_required_backend_matrix_is_not_skipped_before_expansion(self):
        backend = self.workflow["jobs"]["backend"]
        self.assertIn(
            backend.get("if"),
            (None, "${{ !cancelled() }}", "${{ always() }}"),
            "Required matrix jobs must expand even for frontend-only and docs-only PRs; "
            "put changed-scope conditions on steps, not on the matrix job.",
        )

    def test_required_check_names_match_main_protection(self):
        backend = self.workflow["jobs"]["backend"]
        names = {self.workflow["jobs"]["frontend"]["name"]}
        for group in backend["strategy"]["matrix"]["group"]:
            names.add(backend["name"].replace("${{ matrix.group }}", group))
        self.assertEqual(names, {
            "Frontend build", "Backend tests (core)",
            "Backend tests (channels)", "Backend tests (drift)",
        })
        self.assertNotEqual(backend["strategy"].get("fail-fast"), "true")

    def test_frontend_only_skips_checks_for_an_explicit_false_scope(self):
        self.assertNotIn("needs.changes.outputs.frontend == 'true'", self.workflow["jobs"]["frontend"]["if"],
                         "Missing or malformed scope output must not suppress frontend validation")

    def test_unrelated_changes_skip_every_expensive_backend_step(self):
        steps = self.workflow["jobs"]["backend"]["steps"]
        self.assertEqual(steps[0]["id"], "scope")
        for step in steps[1:]:
            with self.subTest(step=step["name"]):
                self.assertIn("steps.scope.outputs.run == 'true'", step.get("if", ""))
                self.assertNotEqual(step.get("continue-on-error"), "true")

    def test_actual_backend_selector_falls_back_to_full_checks(self):
        git = Path(shutil.which("git"))
        windows_bash = git.parent.parent / "bin/bash.exe"
        bash = str(windows_bash) if windows_bash.is_file() else shutil.which("bash")
        self.assertIsNotNone(bash, "Install Bash to replay the workflow selector")
        selector = self.workflow["jobs"]["backend"]["steps"][0]["run"]
        cases = [
            ("success", "false", "false"),
            ("success", "true", "true"),
            ("failure", "false", "true"),
            ("skipped", "false", "true"),
            ("success", "", "true"),
            ("success", "unexpected", "true"),
        ]
        for result, changed, expected in cases:
            with self.subTest(result=result, changed=changed), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "outputs"
                environment = {**os.environ, "CHANGES_RESULT": result, "BACKEND_CHANGED": changed,
                               "GITHUB_OUTPUT": output.as_posix(),
                               "GITHUB_STEP_SUMMARY": (Path(directory) / "summary").as_posix()}
                subprocess.run([bash, "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", selector],
                               env=environment, check=True, capture_output=True, text=True)
                self.assertEqual(output.read_text().strip(), f"run={expected}")

    def test_ci_file_changes_cannot_skip_the_workflow_contract_tests(self):
        cases = [
            (["web/src/styles.css"], "frontend=true\nbackend=false\n"),
            (["backend/app/main.py"], "frontend=false\nbackend=true\n"),
            (["docs/handbook/ci.md"], "frontend=false\nbackend=false\n"),
            (["web/src/App.tsx", "backend/app/main.py"], "frontend=true\nbackend=true\n"),
            ([".github/workflows/ci.yml"], "frontend=true\nbackend=true\n"),
            ([".github/tests/test_ci_workflow.py"], "frontend=true\nbackend=true\n"),
            (["unknown/new-config"], "frontend=true\nbackend=true\n"),
        ]
        for paths, expected in cases:
            with self.subTest(paths=paths):
                result = subprocess.run([sys.executable, str(ROOT / ".github/scripts/ci_scope.py")],
                                        input=b"\0".join(path.encode() for path in paths) + b"\0",
                                        check=True, capture_output=True)
                self.assertEqual(result.stdout.decode().replace("\r\n", "\n"), expected)
        core_checks = next(step for step in self.workflow["jobs"]["backend"]["steps"]
                           if "unittest discover" in step.get("run", ""))
        self.assertNotEqual(core_checks.get("continue-on-error"), "true")


if __name__ == "__main__":
    unittest.main()
