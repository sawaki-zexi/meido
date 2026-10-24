"""Read-only audit of required PR checks, including names never reported by CI."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
from urllib.parse import quote


def gh_json(*arguments):
    result = subprocess.run(["gh", *arguments], check=True, capture_output=True, text=True, encoding="utf-8", timeout=60)
    return json.loads(result.stdout)


def outcome_of(check):
    if check.get("__typename") == "StatusContext":
        return {"SUCCESS": "PASS", "PENDING": "WAIT"}.get(check.get("state"), "FAIL")
    if check.get("status") != "COMPLETED":
        return "WAIT"
    return "PASS" if check.get("conclusion") in {"SUCCESS", "NEUTRAL", "SKIPPED"} else "FAIL"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="sawaki-zexi/meido")
    parser.add_argument("--pr", type=int)
    parser.add_argument("--snapshot", help="Replay {rules, pull_request} JSON; '-' reads stdin")
    args = parser.parse_args()
    if bool(args.pr) == bool(args.snapshot):
        parser.error("Choose exactly one of --pr or --snapshot")
    if args.snapshot:
        snapshot = json.load(sys.stdin) if args.snapshot == "-" else json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
        rules, pull_request = snapshot["rules"], snapshot["pull_request"]
    else:
        pull_request = gh_json("pr", "view", str(args.pr), "--repo", args.repo,
                               "--json", "headRefOid,baseRefName,mergeStateStatus,statusCheckRollup")
        branch = quote(pull_request["baseRefName"], safe="")
        rules = gh_json("api", f"repos/{args.repo}/rules/branches/{branch}")

    required = {check["context"] for rule in rules if rule["type"] == "required_status_checks"
                for check in rule["parameters"]["required_status_checks"]}
    if not required:
        print("No required checks found; inspect the branch protection configuration.", file=sys.stderr)
        return 2
    passed = 0
    checks = pull_request.get("statusCheckRollup") or []
    for name in sorted(required):
        matching = [check for check in checks if check.get("name", check.get("context")) == name]
        outcomes = {outcome_of(check) for check in matching}
        if not matching:
            outcome = "MISSING"
        elif "FAIL" in outcomes:
            outcome = "FAIL"
        elif "WAIT" in outcomes:
            outcome = "WAIT"
        else:
            outcome = "PASS"
        passed += outcome == "PASS"
        print(f"{outcome}: {name}")
    print(f"Required checks: {passed}/{len(required)} satisfied; head {pull_request.get('headRefOid', 'snapshot')}.")
    print(f"Merge state: {pull_request.get('mergeStateStatus', 'unknown')} (other protections may still apply).")
    return 0 if passed == len(required) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f"Unable to verify required checks: {error}", file=sys.stderr)
        sys.exit(2)
