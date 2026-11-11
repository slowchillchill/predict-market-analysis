"""Package the Python entrypoint as a standard Kaggle Notebook using stdlib only."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


DEFAULT_SOURCE_DIR = Path(__file__).resolve().parents[1] / "kaggle"


def prepare_notebook(source_dir: Path, output_dir: Path) -> Path:
    metadata = json.loads((source_dir / "kernel-metadata.json").read_text(encoding="utf-8"))
    source = (source_dir / metadata["code_file"]).read_text(encoding="utf-8")
    notebook = {
        "cells": [{
            "cell_type": "code", "execution_count": None, "id": "producer",
            "metadata": {}, "outputs": [], "source": source,
        }],
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    metadata.update(code_file="notebook.ipynb", kernel_type="notebook")
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, value in (("notebook.ipynb", notebook), ("kernel-metadata.json", metadata)):
        (output_dir / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output_dir / "notebook.ipynb"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    notebook = prepare_notebook(args.source_dir, args.output_dir)
    print(f"Prepared Kaggle Notebook: {notebook}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
