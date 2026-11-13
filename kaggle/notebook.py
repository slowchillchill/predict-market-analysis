# Copyright 2026 slowchillchill
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy at https://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
"""Polymarket Crypto Updown Markets Analysis.

Public, reproducible market-end-UTC trading statistics. The producer downloads
public API records directly on Kaggle, publishes daily SQLite Datasets and
monthly analysis snapshots, and resumes historical collection across sessions.
Project source: https://github.com/slowchillchill/predict-market-analysis (MIT).
Credentials stay on Kaggle. The release selects a platform or personal source;
never paste a credential into a code cell. Run one production instance at a time.
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen

RUN_ARGUMENTS = []  # Production: fresh UTC days, then continuous historical backfill.
SOURCE_URL = "https://github.com/slowchillchill/predict-market-analysis.git"
RELEASE_URL = ("https://raw.githubusercontent.com/slowchillchill/predict-market-analysis/"
               "kaggle-release/config/kaggle-release.json")
PINNED_PACKAGES = ("kaggle==2.2.5", "kagglesdk==0.1.38", "six==1.17.0")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate release configuration key")
        result[key] = value
    return result


def load_release():
    """Read one release and fail closed; never substitute a moving code branch."""
    request = Request(RELEASE_URL, headers={"Cache-Control": "no-cache"})
    with urlopen(request, timeout=40) as response:
        payload = response.read(65537)
    if len(payload) > 65536:
        raise ValueError("Release configuration is too large")
    release = json.loads(payload, object_pairs_hook=_unique_object)
    if not isinstance(release, dict) or set(release) != {"schema_version", "source_commit", "auth_mode"}:
        raise ValueError("Unexpected release configuration fields")
    if type(release["schema_version"]) is not int or release["schema_version"] != 1:
        raise ValueError("Unsupported release configuration schema")
    if not isinstance(release["source_commit"], str) or not re.fullmatch(r"[0-9a-f]{40}", release["source_commit"]):
        raise ValueError("Release source_commit must be an exact 40-character Git commit")
    if not isinstance(release["auth_mode"], str) or release["auth_mode"] not in {"auto", "platform", "personal"}:
        raise ValueError("Unsupported release authentication mode")
    return release


def main():
    # Include setup and restoration in the session clock; never restart the
    # deadline when collection begins or when a day is resumed.
    os.environ.setdefault("POLYMARKET_RUN_STARTED_MONOTONIC", str(time.monotonic()))
    if not Path("/kaggle/working").is_dir():
        raise RuntimeError("Run this entrypoint on Kaggle, not on the local collector")
    release = load_release()
    source_commit = release["source_commit"]
    os.environ["POLYMARKET_SOURCE_COMMIT"] = source_commit
    print("Kaggle release: " + json.dumps(release, sort_keys=True), flush=True)
    # A fresh checkout prevents files or edits from an interactive session from
    # changing the code represented by the recorded commit.
    source = Path(tempfile.mkdtemp(prefix="polymarket-source-"))
    subprocess.run(["git", "clone", "--quiet", "--no-checkout", SOURCE_URL, str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "fetch", "--quiet", SOURCE_URL, source_commit], check=True)
    subprocess.run(["git", "-C", str(source), "checkout", "--quiet", "--detach", source_commit], check=True)
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "-r",
                    str(source / "requirements-kaggle.txt"), *PINNED_PACKAGES], check=True)
    sys.path.insert(0, str(source / "scripts"))
    from polymarket_kaggle_auth import kaggle_auth
    with kaggle_auth(release["auth_mode"]) as authentication:
        record = {"schema_version": 1, "release": release, "authentication": authentication,
                  "started_at_utc": datetime.now(timezone.utc).isoformat()}
        Path("/kaggle/working/bootstrap.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        from polymarket_kaggle_pipeline import main as run
        exit_code = run(RUN_ARGUMENTS)
    if exit_code:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
