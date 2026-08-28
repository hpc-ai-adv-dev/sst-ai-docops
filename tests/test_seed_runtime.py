# Copyright Hewlett Packard Enterprise Development LP.
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "seed" / "entrypoint.sh"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


open_webui_patch = load_module(
    "patch_open_webui",
    ROOT / "seed" / "patch-open-webui.py",
)


class SeedEntrypointTests(unittest.TestCase):
    def run_entrypoint(
        self,
        data_dir: Path,
        seed_dir: Path,
        sync_script: Path,
        command: Path,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(
            {
                "DATA_DIR": str(data_dir),
                "APP_SEED_DIR": str(seed_dir),
                "SYNC_SCRIPT": str(sync_script),
                "FILTER_SOURCE": str(seed_dir / "confidence-gate.py"),
            }
        )
        return subprocess.run(
            [str(ENTRYPOINT), str(command)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    @staticmethod
    def write_sync_script(path: Path, *, exit_code: int) -> None:
        path.write_text(f"raise SystemExit({exit_code})\n")

    def test_first_run_copies_seed_and_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            seed_dir = root / "seed"
            data_dir = root / "data"
            seed_dir.mkdir()
            data_dir.mkdir()
            (seed_dir / "webui.db").write_text("seed database")
            (seed_dir / "vector_db").mkdir()
            (seed_dir / "vector_db" / "vectors").write_text("vectors")
            (seed_dir / "confidence-gate.py").write_text("# filter\n")
            sync_script = root / "sync.py"
            self.write_sync_script(sync_script, exit_code=0)

            result = self.run_entrypoint(
                data_dir,
                seed_dir,
                sync_script,
                Path("/usr/bin/true"),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                (data_dir / "webui.db").read_text(),
                "seed database",
            )
            self.assertEqual(
                (data_dir / "vector_db" / "vectors").read_text(),
                "vectors",
            )
            self.assertFalse((data_dir / "webui.db.seed-tmp").exists())

    def test_existing_database_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            seed_dir = root / "seed"
            data_dir = root / "data"
            seed_dir.mkdir()
            data_dir.mkdir()
            (seed_dir / "webui.db").write_text("new")
            (seed_dir / "confidence-gate.py").write_text("# filter\n")
            (data_dir / "webui.db").write_text("existing")
            sync_script = root / "sync.py"
            self.write_sync_script(sync_script, exit_code=0)

            result = self.run_entrypoint(
                data_dir,
                seed_dir,
                sync_script,
                Path("/usr/bin/true"),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                (data_dir / "webui.db").read_text(),
                "existing",
            )

    def test_existing_database_warns_when_the_corpus_is_stale(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            seed_dir = root / "seed"
            data_dir = root / "data"
            seed_dir.mkdir()
            data_dir.mkdir()
            (seed_dir / "webui.db").write_text("new")
            (seed_dir / "sst-corpus-lock.json").write_text("new corpus")
            (seed_dir / "confidence-gate.py").write_text("# filter\n")
            (data_dir / "webui.db").write_text("existing")
            (data_dir / "sst-corpus-lock.json").write_text("old corpus")
            sync_script = root / "sync.py"
            self.write_sync_script(sync_script, exit_code=0)

            result = self.run_entrypoint(
                data_dir,
                seed_dir,
                sync_script,
                Path("/usr/bin/true"),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                "runtime data uses a different SST corpus",
                result.stderr,
            )
            self.assertEqual(
                (data_dir / "webui.db").read_text(),
                "existing",
            )

    def test_synchronization_failure_stops_startup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            seed_dir = root / "seed"
            data_dir = root / "data"
            seed_dir.mkdir()
            data_dir.mkdir()
            (seed_dir / "webui.db").write_text("seed database")
            (seed_dir / "confidence-gate.py").write_text("# filter\n")
            sync_script = root / "sync.py"
            self.write_sync_script(sync_script, exit_code=1)
            started = root / "started"
            command = root / "command.sh"
            command.write_text(
                "#!/usr/bin/env bash\n"
                f"touch {started}\n"
            )
            command.chmod(0o755)

            result = self.run_entrypoint(
                data_dir,
                seed_dir,
                sync_script,
                command,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(started.exists())


class SeedImageTests(unittest.TestCase):
    def test_open_webui_patch_is_exact_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "chroma.py"
            target.write_text(
                "prefix\n" + open_webui_patch.ORIGINAL + "\nsuffix\n"
            )

            self.assertTrue(open_webui_patch.apply_patch(target))
            first_result = target.read_text()
            self.assertIn(open_webui_patch.PATCHED, first_result)
            self.assertNotIn(open_webui_patch.ORIGINAL, first_result)

            self.assertFalse(open_webui_patch.apply_patch(target))
            self.assertEqual(target.read_text(), first_result)

    def test_open_webui_patch_rejects_unknown_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "chroma.py"
            target.write_text("unexpected upstream implementation\n")

            with self.assertRaises(SystemExit):
                open_webui_patch.apply_patch(target)

    def test_retrieval_patch_is_exact_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "utils.py"
            target.write_text(
                "prefix\n"
                + open_webui_patch.RETRIEVAL_ORIGINAL
                + "\nsuffix\n"
            )

            self.assertTrue(
                open_webui_patch.apply_retrieval_patch(target)
            )
            first_result = target.read_text()
            self.assertIn(
                open_webui_patch.RETRIEVAL_PATCHED,
                first_result,
            )
            self.assertNotIn(
                open_webui_patch.RETRIEVAL_ORIGINAL,
                first_result,
            )

            self.assertFalse(
                open_webui_patch.apply_retrieval_patch(target)
            )
            self.assertEqual(target.read_text(), first_result)

    def test_retrieval_patch_rejects_unknown_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "utils.py"
            target.write_text("unexpected upstream implementation\n")

            with self.assertRaises(SystemExit):
                open_webui_patch.apply_retrieval_patch(target)

    def test_seed_tools_use_the_containerfile_base_digest(self):
        digest_pattern = re.compile(
            r"ghcr\.io/open-webui/open-webui@sha256:[0-9a-f]{64}"
        )
        containerfile = (ROOT / "seed" / "Containerfile").read_text()
        expected = digest_pattern.search(containerfile)
        self.assertIsNotNone(expected)

        for script_name in ("start-seed.sh", "refresh-sst-corpus.sh"):
            script = (ROOT / "seed" / script_name).read_text()
            matches = digest_pattern.findall(script)
            self.assertEqual(matches, [expected.group(0)])


if __name__ == "__main__":
    unittest.main()
