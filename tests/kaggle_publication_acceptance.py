"""Opt-in live acceptance against one new PRIVATE, tiny QA Dataset.

Run explicitly with --work-dir; unittest discovery never executes this file.
No production Dataset is read or changed. The second real upload is followed
by one simulated 403 on its status query. Read-only marker reconciliation must
prove that upload succeeded without sending it again.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import polymarket_kaggle_store as cloud


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    root = args.work_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    slug = "qa-publication-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    ref = f"{cloud.OWNER}/{slug}"
    mutations = []
    inject_failure = False
    fail_next_status = False

    def runner(command, **kwargs):
        nonlocal fail_next_status
        command = list(command)
        operation = tuple(command[1:3])
        if fail_next_status and operation == ("datasets", "status"):
            require(command[3] == ref, "Status query escaped the QA Dataset")
            fail_next_status = False
            return subprocess.CompletedProcess(command, 1, "", "HTTP 403 Forbidden (injected QA read failure)")
        mutation = operation in {("datasets", "create"), ("datasets", "version")}
        if mutation:
            folder = Path(command[command.index("-p") + 1])
            metadata = json.loads((folder / "dataset-metadata.json").read_text())
            require(metadata["id"] == ref, "Mutation escaped the QA Dataset")
            require(len(mutations) < 2, "Unexpected duplicate upload")
            command = [part for part in command if part != "--public"]
            mutations.append(operation[-1])
        result = subprocess.run(command, **kwargs)
        output = (result.stdout or "") + (result.stderr or "")
        successful = result.returncode == 0 and "creation error:" not in output
        if mutation and successful and inject_failure:
            fail_next_status = True
        return result

    store = cloud.KaggleStore(work_dir=root / "downloads", runner=runner,
                              deadline=time.monotonic() + 10 * 60)
    folder = root / "snapshot"
    folder.mkdir()
    cloud.write_json(folder / "sample.json", {"value": 1})
    first = store.publish(slug, folder, title="Publication confirmation acceptance",
                          description="Private synthetic QA sample; no production market data.", message="Normal publication")
    require(not list((store.work_dir / "pending-publications").glob("*.json")),
            "Confirmed publication retained a pending journal")
    cloud.write_json(folder / "sample.json", {"value": 2})
    inject_failure = True
    try:
        store.publish(slug, folder, title="Publication confirmation acceptance",
                      description="Private synthetic QA sample; no production market data.", message="Uncertain confirmation")
    except cloud.KaggleError as exc:
        diagnostic = cloud.describe_exception(exc)
        require("Cannot determine" in diagnostic and "403" in diagnostic and ref in diagnostic,
                "Publication failure lost diagnostic context")
    else:
        raise AssertionError("Injected confirmation failure was not observed")
    journals = list((store.work_dir / "pending-publications").glob("*.json"))
    require(len(journals) == 1, "Unconfirmed publication did not retain exactly one journal")
    journal = json.loads(journals[0].read_text())
    marker = json.loads((folder / cloud.PUBLICATION_FILE).read_text())
    require(journal["publication"] == marker, "Pending journal lost the snapshot identity")
    with store._operation():
        confirmed = store._confirm_publication(slug, marker, first + 1, submitted=True)
    require(mutations == ["create", "version"], "Reconciliation repeated an upload")
    require(confirmed == first + 1, "Unexpected remote version after two uploads")
    cloud.write_json(root / "acceptance.json", {
        "status": "passed", "dataset": ref, "private": True,
        "first_version": first, "reconciled_version": confirmed,
        "publication_id": marker["publication_id"], "mutations": mutations,
        "diagnostic": diagnostic, "journal_retained_for_audit": str(journals[0]),
        "production_writes": 0,
    })
    print(f"Live publication acceptance passed: {ref}, versions {first}/{confirmed}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
