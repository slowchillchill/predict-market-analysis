import json
from contextlib import contextmanager
import re
import runpy
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import prepare_kaggle_notebook as packaging


class PrepareKaggleNotebookTests(unittest.TestCase):
    def test_successful_bootstrap_finishes_a_notebook_cell_without_system_exit(self):
        bootstrap = runpy.run_path(str(ROOT / "kaggle/notebook.py"), run_name="bootstrap_test")
        producer = Mock(return_value=0)
        release = {"schema_version": 1, "source_commit": "a" * 40, "auth_mode": "platform"}
        @contextmanager
        def authenticate(mode):
            self.assertEqual(mode, "platform")
            yield {"source": "platform", "mode": mode, "sdk_source": "KAGGLE_API_V1_TOKEN"}
        with patch.dict(sys.modules, {"polymarket_kaggle_auth": SimpleNamespace(kaggle_auth=authenticate),
                                     "polymarket_kaggle_pipeline": SimpleNamespace(main=producer)}), \
                patch.dict(bootstrap["main"].__globals__, {"load_release": Mock(return_value=release)}), \
                patch.dict("os.environ", {}, clear=False), patch.object(Path, "is_dir", return_value=True), \
                patch.object(Path, "exists", return_value=True), patch.object(subprocess, "run") as commands, \
                patch.object(tempfile, "mkdtemp", return_value="/tmp/polymarket-source-unit-test"), \
                patch.object(Path, "write_text") as write, \
                patch.object(sys, "path", list(sys.path)):
            bootstrap["main"]()
            producer.assert_called_once_with([])
            self.assertEqual(bootstrap["main"].__globals__["load_release"].call_count, 1)
            self.assertIn(["git", "-C", "/tmp/polymarket-source-unit-test", "checkout", "--quiet", "--detach", "a" * 40],
                          [call.args[0] for call in commands.call_args_list])
            self.assertIn("kagglesdk==0.1.38", commands.call_args.args[0])
            self.assertEqual(json.loads(write.call_args.args[0])["release"], release)
            producer.return_value = 1
            with self.assertRaises(SystemExit) as exit_status:
                bootstrap["main"]()
            self.assertEqual(exit_status.exception.code, 1)

    def test_release_accepts_exact_commit_and_explicit_auth_mode(self):
        bootstrap = runpy.run_path(str(ROOT / "kaggle/notebook.py"), run_name="bootstrap_test")
        release = {"schema_version": 1, "source_commit": "a" * 40, "auth_mode": "auto"}
        response = Mock()
        response.read.return_value = json.dumps(release).encode()
        opener = Mock()
        opener.return_value.__enter__ = Mock(return_value=response)
        opener.return_value.__exit__ = Mock(return_value=False)
        with patch.dict(bootstrap["load_release"].__globals__, {"urlopen": opener}):
            self.assertEqual(bootstrap["load_release"](), release)
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, bootstrap["RELEASE_URL"])
        self.assertEqual(opener.call_args.kwargs["timeout"], 40)

    def test_release_rejects_moving_refs_unknown_fields_and_ambiguous_json(self):
        bootstrap = runpy.run_path(str(ROOT / "kaggle/notebook.py"), run_name="bootstrap_test")
        good = {"schema_version": 1, "source_commit": "a" * 40, "auth_mode": "platform"}
        payloads = [json.dumps({**good, "source_commit": "main"}),
                    json.dumps({**good, "schema_version": True}),
                    json.dumps({**good, "source_url": "https://other.example/repo"}),
                    json.dumps({**good, "auth_mode": []}),
                    '{"schema_version":1,"schema_version":1,"source_commit":"' + "a" * 40 + '","auth_mode":"auto"}',
                    "[]", " " * 65537]
        for payload in payloads:
            response = Mock()
            response.read.return_value = payload.encode()
            opener = Mock()
            opener.return_value.__enter__ = Mock(return_value=response)
            opener.return_value.__exit__ = Mock(return_value=False)
            with self.subTest(payload=payload[:80]), \
                    patch.dict(bootstrap["load_release"].__globals__, {"urlopen": opener}):
                with self.assertRaises(ValueError):
                    bootstrap["load_release"]()

    def test_release_failure_stops_before_dependencies_or_producer(self):
        bootstrap = runpy.run_path(str(ROOT / "kaggle/notebook.py"), run_name="bootstrap_test")
        with patch.dict(bootstrap["main"].__globals__, {"load_release": Mock(side_effect=OSError("offline"))}), \
                patch.object(Path, "is_dir", return_value=True), \
                patch.dict("os.environ", {}, clear=False), patch.object(subprocess, "run") as commands:
            with self.assertRaises(OSError):
                bootstrap["main"]()
            commands.assert_not_called()

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
