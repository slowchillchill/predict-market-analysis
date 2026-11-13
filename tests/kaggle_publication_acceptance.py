"""Opt-in live acceptance against one new PRIVATE, tiny QA Dataset.

Run explicitly with --work-dir; unittest discovery never executes this file.
No production Dataset is read or changed. The first create is followed by a
simulated 403 while its owner listing is visible; normal confirmation must
recover without another create. The second real upload receives persistent
simulated status 403s, which must stop version confirmation and retain its
journal. Read-only marker reconciliation then proves that upload succeeded.
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
    inject_failure = "once"
    fail_next_status = False
    force_visible_listing = False
    injected_status_failures = 0

    def runner(command, **kwargs):
        nonlocal fail_next_status, force_visible_listing, injected_status_failures
        command = list(command)
        operation = tuple(command[1:3])
        if fail_next_status and operation == ("datasets", "status"):
            require(command[3] == ref, "Status query escaped the QA Dataset")
            fail_next_status = inject_failure == "persistent"
            force_visible_listing = True
            injected_status_failures += 1
            return subprocess.CompletedProcess(command, 1, "", "HTTP 403 Forbidden (injected QA read failure)")
        if force_visible_listing and operation == ("datasets", "list"):
            require(command[command.index("--search") + 1] == slug, "Owner listing escaped the QA Dataset")
            force_visible_listing = False
            return subprocess.CompletedProcess(command, 0, f"ref,title\n{ref},Private QA\n", "")
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
                          description="Private synthetic QA sample; no production market data.", message="Create confirmation recovers after 403")
    require(injected_status_failures == 1 and mutations == ["create"],
            "Create did not recover from exactly one injected 403 with one upload")
    require(not list((store.work_dir / "pending-publications").glob("*.json")),
            "Confirmed publication retained a pending journal")
    cloud.write_json(folder / "sample.json", {"value": 2})
    inject_failure = "persistent"
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
    require(journal["phase"] == "accepted" and journal["attempt"] == 1,
            "Version failure lost its accepted upload identity")
    require(injected_status_failures == 2, "Version confirmation unexpectedly retried a permanent 403")
    fail_next_status = False
    inject_failure = None
    with store._operation():
        confirmed = store._confirm_publication(slug, marker, first + 1, submitted=True)
    require(mutations == ["create", "version"], "Reconciliation repeated an upload")
    require(confirmed == first + 1, "Unexpected remote version after two uploads")
    cloud.write_json(root / "acceptance.json", {
        "status": "passed", "dataset": ref, "private": True,
        "first_version": first, "reconciled_version": confirmed,
        "publication_id": marker["publication_id"], "mutations": mutations,
        "create_403_recovery": True, "version_403_stops_without_retry": True,
        "injected_status_failures": injected_status_failures,
        "diagnostic": diagnostic, "journal_retained_for_audit": str(journals[0]),
        "production_writes": 0,
    })
    print(f"Live publication acceptance passed: {ref}, versions {first}/{confirmed}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
