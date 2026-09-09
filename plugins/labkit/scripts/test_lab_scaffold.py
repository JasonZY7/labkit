"""Offline scaffold regressions. Run: python -B scripts/test_lab_scaffold.py"""
import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import lab_init
from lab_fs import trash


class ScaffoldTests(unittest.TestCase):
    scaffold_files = (
        "AGENTS.md", "CLAUDE.md", ".gitignore", ".labkit.json", "notes/log.md",
        "raw/SOURCES.md", "notes/findings/_TEMPLATE.md", "handoffs/README.md",
    )

    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(
            prefix="labkit-scaffold-", dir=Path(tempfile.gettempdir()).resolve()))
        self.addCleanup(trash, self.temp, within=Path(tempfile.gettempdir()).resolve())
        self.root = self.temp / "project"

    def initialize(self, name="Original Study", force=False):
        output = io.StringIO()
        with patch.object(lab_init.shutil, "which", return_value=None), \
                patch.object(lab_init.subprocess, "run") as run, \
                contextlib.redirect_stdout(output):
            result = lab_init.cmd_init(SimpleNamespace(
                directory=str(self.root), name=name, force=force))
        self.assertEqual(result, 0)
        run.assert_not_called()
        return output.getvalue()

    def config(self):
        return json.loads((self.root / ".labkit.json").read_text(encoding="utf-8"))

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes()
                for p in self.root.rglob("*") if p.is_file()}

    def assert_canonical_rules(self):
        rules = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        pointer = (self.root / "CLAUDE.md").read_text(encoding="utf-8")
        for identifier in ("corpus", "notes", "memory", "collab", "graphify",
                           "--kind", "--supersede", self.config()["mem0_namespace"]):
            with self.subTest(identifier=identifier):
                self.assertIn(identifier, rules)
        for runtime in ("claude", "codex", "chatgpt"):
            self.assertIn(runtime, rules.lower())
        self.assertIn("@AGENTS.md", pointer)
        for rule_token in ("graphify query", "--supersede", "mem0_rows", "notes/findings/"):
            self.assertNotIn(rule_token, pointer)

    def test_fresh_project_has_shared_rules_and_claude_pointer(self):
        self.initialize()
        self.assertTrue(set(self.scaffold_files) <= self.snapshot().keys())
        cfg = self.config()
        self.assertEqual(cfg["slug"], "original-study")
        self.assertEqual(cfg["mem0_namespace"], "proj-original-study")
        self.assert_canonical_rules()

    def test_repeated_init_preserves_all_bytes_and_existing_identity(self):
        self.initialize()
        for name in ("AGENTS.md", "CLAUDE.md", "notes/log.md"):
            with (self.root / name).open("ab") as stream:
                stream.write(b"\r\nUser-owned content; preserve these exact bytes.\r\n")
        cfg = self.config()
        cfg.update(name="Persisted Study", slug="persisted-slug",
                   mem0_namespace="proj-persisted-slug", custom={"keep": True})
        cfg["layers"]["memory"] = "mem0:proj-persisted-slug"
        (self.root / ".labkit.json").write_text(
            json.dumps(cfg, indent=4) + "\n", encoding="utf-8")
        before = self.snapshot()
        output = self.initialize(name="Renamed Study")
        self.assertEqual(self.snapshot(), before)
        self.assertIn("proj-persisted-slug", output)
        self.assertNotIn("renamed-study", output)

    def test_force_refreshes_scaffold_and_identity(self):
        self.initialize()
        marker = b"user-owned-marker"
        for name in self.scaffold_files:
            if name == ".labkit.json":
                cfg = self.config()
                cfg["custom"] = marker.decode()
                (self.root / name).write_text(json.dumps(cfg), encoding="utf-8")
            else:
                with (self.root / name).open("ab") as stream:
                    stream.write(b"\n" + marker + b"\n")
        self.initialize(name="Replacement Study", force=True)
        cfg = self.config()
        self.assertEqual(cfg["name"], "Replacement Study")
        self.assertEqual(cfg["slug"], "replacement-study")
        self.assertEqual(cfg["mem0_namespace"], "proj-replacement-study")
        self.assertEqual(cfg["layers"]["memory"], "mem0:proj-replacement-study")
        for name in self.scaffold_files:
            with self.subTest(file=name):
                self.assertNotIn(marker, (self.root / name).read_bytes())
        self.assert_canonical_rules()

    def test_custom_agents_gets_missing_claude_pointer_without_overwrite(self):
        self.root.mkdir()
        custom = b"# Existing project rules\r\n\r\nKeep the research archive intact.\r\n"
        (self.root / "AGENTS.md").write_bytes(custom)
        self.initialize()
        self.assertEqual((self.root / "AGENTS.md").read_bytes(), custom)
        pointer = (self.root / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn("@AGENTS.md", pointer)
        self.assertNotIn("graphify query", pointer)
        self.assertNotIn("--supersede", pointer)


if __name__ == "__main__":
    unittest.main()
