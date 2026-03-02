import json
import tempfile
import unittest
from pathlib import Path

import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from centaur.engine import (  # noqa: E402
    DEFAULT_TASK_NAME,
    PROJECT_SCHEMA_VERSION,
    RUNTIME_DIR,
    load_or_init_project_config,
    migrate_schema,
    sync_task_bus_to_active,
    task_file_path,
    validate_task_name,
    ensure_active_task_file,
)


class EngineRuntimeTests(unittest.TestCase):
    def test_default_project_config_schema_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "PROPOSAL.md").write_text("proposal", encoding="utf-8")
            config = load_or_init_project_config(workspace)
            self.assertEqual(config["schema_version"], PROJECT_SCHEMA_VERSION)
            self.assertIn("active_task", config)
            self.assertIn("controller_version", config)
            self.assertIn("target_repo", config)
            self.assertIn("target_ref", config)
            self.assertIn("target_version", config)

    def test_active_task_file_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "PROPOSAL.md").write_text("proposal", encoding="utf-8")
            (workspace / "TASK.md").write_text("# 当前任务 (Task)\nA", encoding="utf-8")
            config = load_or_init_project_config(workspace)
            active_task, active_path = ensure_active_task_file(workspace, config)

            self.assertEqual(active_task, DEFAULT_TASK_NAME)
            self.assertTrue(active_path.exists())
            self.assertEqual(active_path.read_text(encoding="utf-8"), (workspace / "TASK.md").read_text(encoding="utf-8"))

            (workspace / "TASK.md").write_text("# 当前任务 (Task)\nB", encoding="utf-8")
            sync_task_bus_to_active(workspace, active_task)
            self.assertEqual(active_path.read_text(encoding="utf-8"), "# 当前任务 (Task)\nB")

    def test_migrate_schema_from_legacy_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "PROPOSAL.md").write_text("proposal", encoding="utf-8")
            (workspace / "TASK.md").write_text("# 当前任务 (Task)\nlegacy", encoding="utf-8")
            (workspace / ".centaur_project.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "centaur_version": "0.1.0",
                        "prompt_set_version": "old",
                        "prompt_mode": "global",
                    }
                ),
                encoding="utf-8",
            )
            (workspace / ".centaur_state.json").write_text(
                json.dumps({"cycle": 3, "next_step": "worker"}),
                encoding="utf-8",
            )

            config = migrate_schema(workspace)
            self.assertEqual(config["schema_version"], PROJECT_SCHEMA_VERSION)
            self.assertEqual(config["active_task"], DEFAULT_TASK_NAME)
            self.assertTrue((workspace / RUNTIME_DIR / "project.json").exists())
            self.assertTrue((workspace / RUNTIME_DIR / "state.json").exists())
            self.assertTrue(task_file_path(workspace, DEFAULT_TASK_NAME).exists())

    def test_validate_task_name(self) -> None:
        self.assertTrue(validate_task_name("task-001"))
        self.assertTrue(validate_task_name("a.b_c-1"))
        self.assertFalse(validate_task_name(" bad"))
        self.assertFalse(validate_task_name(""))
        self.assertFalse(validate_task_name("!invalid"))


if __name__ == "__main__":
    unittest.main()
