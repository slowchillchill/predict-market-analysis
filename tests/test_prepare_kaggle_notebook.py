import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import prepare_kaggle_notebook as packaging


class PrepareKaggleNotebookTests(unittest.TestCase):
    def test_cli_packages_production_source_as_valid_notebook_without_changing_metadata(self):
        original = json.loads((ROOT / "kaggle/kernel-metadata.json").read_text())
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "package"
            subprocess.run([sys.executable, str(ROOT / "scripts/prepare_kaggle_notebook.py"),
                            "--output-dir", str(destination)], check=True, capture_output=True, text=True)
            metadata = json.loads((destination / "kernel-metadata.json").read_text())
            self.assertEqual(metadata, {**original, "code_file": "notebook.ipynb", "kernel_type": "notebook"})
            notebook = json.loads((destination / metadata["code_file"]).read_text())
            self.assertEqual((notebook["nbformat"], notebook["nbformat_minor"]), (4, 5))
            self.assertEqual(len(notebook["cells"]), 1)
            cell = notebook["cells"][0]
            self.assertEqual(cell["cell_type"], "code")
            self.assertIsNone(cell["execution_count"])
            self.assertEqual(cell["outputs"], [])
            self.assertRegex(cell["id"], re.compile(r"^[A-Za-z0-9_-]{1,64}$"))
            self.assertEqual(cell["source"], (ROOT / "kaggle" / original["code_file"]).read_text())
            self.assertEqual(notebook["metadata"]["kernelspec"],
                             {"display_name": "Python 3", "language": "python", "name": "python3"})
            self.assertEqual({path.name for path in destination.iterdir()}, {"kernel-metadata.json", "notebook.ipynb"})

    def test_custom_source_preserves_private_qa_identity_and_exact_code(self):
        with tempfile.TemporaryDirectory() as temporary:
            source_dir = Path(temporary) / "source"
            source_dir.mkdir()
            source = '# 私有验收\nprint("quoted \\\"text\\\"")\n'
            (source_dir / "acceptance.py").write_text(source, encoding="utf-8")
            metadata = {"id": "owner/private-acceptance", "title": "Private QA", "is_private": True,
                        "kernel_type": "script", "code_file": "acceptance.py", "language": "python",
                        "kernel_sources": ["owner/previous-qa"], "enable_gpu": False}
            (source_dir / "kernel-metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
            output = Path(temporary) / "output"
            notebook_path = packaging.prepare_notebook(source_dir, output)
            self.assertEqual(json.loads(notebook_path.read_text())["cells"][0]["source"], source)
            self.assertEqual(json.loads((output / "kernel-metadata.json").read_text()),
                             {**metadata, "code_file": "notebook.ipynb", "kernel_type": "notebook"})
            self.assertEqual(json.loads((source_dir / "kernel-metadata.json").read_text()), metadata)
            self.assertFalse((source_dir / "notebook.ipynb").exists())


if __name__ == "__main__":
    unittest.main()
