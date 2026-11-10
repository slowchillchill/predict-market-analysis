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
The KAGGLE_API_TOKEN Secret must be enabled for this Notebook; never paste it
into a code cell. Run only one production instance at a time.
"""

import os
from pathlib import Path
import subprocess
import sys

SOURCE_COMMIT = "f5168a1c7fe83b7e114d251379e2c6baf8ed9e5a"
RUN_ARGUMENTS = ["--max-days", "2"]  # Initial acceptance: one fresh day and one historical day.
SOURCE_URL = "https://github.com/slowchillchill/predict-market-analysis.git"


def main():
    if not Path("/kaggle/working").is_dir():
        raise RuntimeError("Run this entrypoint on Kaggle, not on the local collector")
    from kaggle_secrets import UserSecretsClient
    os.environ["KAGGLE_API_TOKEN"] = UserSecretsClient().get_secret("KAGGLE_API_TOKEN")
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "kaggle==2.2.5", "six==1.17.0"], check=True)
    source = Path("/tmp/polymarket-source")
    if not source.exists():
        subprocess.run(["git", "clone", "--quiet", SOURCE_URL, str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "fetch", "--quiet", "origin", SOURCE_COMMIT], check=True)
    subprocess.run(["git", "-C", str(source), "checkout", "--quiet", SOURCE_COMMIT], check=True)
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "-r",
                    str(source / "requirements-kaggle.txt")], check=True)
    sys.path.insert(0, str(source / "scripts"))
    from polymarket_kaggle_pipeline import main as run
    raise SystemExit(run(RUN_ARGUMENTS))


if __name__ == "__main__":
    main()
