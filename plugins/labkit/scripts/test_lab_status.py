"""Offline status regressions. Run: python -B scripts/test_lab_status.py"""
import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import lab_init
import lab_mem


class StatusTests(unittest.TestCase):
    def status(self, *, graph=None, source_mtime=None, records=(), prompts=(), findings=(),
               rows=(), json_mode=False):
        root = Path.cwd() / "offline-status-fixture"
        graph_path = root / "graphify-out" / "graph.json"
        source_path = root / "raw" / "paper.md"
        finding_ids = {root / "notes" / "findings" / name: ids for name, ids in findings}
        files = {root / ".labkit.json": json.dumps({"name": "fixture", "slug": "fixture"})}
        if graph is not None:
            files[graph_path] = graph
        if source_mtime is not None:
            files[source_path] = "source text"

        def rglob(path, pattern):
            if path == root / "notes" / "findings":
                return list(finding_ids)
            return [source_path] if path == root / "raw" and source_mtime is not None else []

        def glob(path, pattern):
            names = prompts if pattern == "*.prompt.md" else records + prompts
            return [path / name for name in names]

        output = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(lab_init, "_find_root", return_value=root))
            stack.enter_context(patch.object(Path, "read_text", lambda path, **kw: files[path]))
            stack.enter_context(patch.object(Path, "exists", lambda path: path in files))
            stack.enter_context(patch.object(Path, "is_file", lambda path: path in files))
            stack.enter_context(patch.object(Path, "rglob", rglob))
            stack.enter_context(patch.object(Path, "glob", glob))
            stack.enter_context(patch.object(Path, "stat", lambda path: SimpleNamespace(
                st_mtime=source_mtime if path == source_path else 10)))
            stack.enter_context(patch.object(lab_mem, "resolve_api_key", return_value="offline"))
            stack.enter_context(patch.object(lab_mem, "read_mem0_rows", side_effect=finding_ids.get))
            stack.enter_context(patch.object(lab_mem, "read_frontmatter_value", return_value="hash"))
            stack.enter_context(patch.object(lab_mem, "content_hash", return_value="hash"))
            client = stack.enter_context(patch.object(lab_mem, "client"))
            client.return_value.get_all.return_value = list(rows)
            stack.enter_context(contextlib.redirect_stdout(output))
            result = lab_init.cmd_status(SimpleNamespace(directory=str(root), json=json_mode))
        return result, output.getvalue()

    def test_corpus_health_sets_exit_code_in_both_formats(self):
        current_graph = '{"nodes": [], "edges": []}'
        cases = (
            ({}, True),  # An empty project need not build a graph yet.
            ({"graph": current_graph, "source_mtime": 5}, True),
            ({"source_mtime": 5}, False),
            ({"graph": current_graph, "source_mtime": 20}, False),
            ({"graph": "{broken json"}, False),
        )
        for inputs, expected_ok in cases:
            for json_mode in (False, True):
                with self.subTest(inputs=inputs, json_mode=json_mode):
                    result, output = self.status(**inputs, json_mode=json_mode)
                    self.assertEqual(result, 0 if expected_ok else 2)
                    if json_mode:
                        self.assertEqual(json.loads(output)["ok"], expected_ok)
                    else:
                        self.assertEqual("all four layers in sync" in output, expected_ok)

    def test_handoff_requires_its_own_saved_prompt(self):
        for prompts, expected_ok in ((('round.prompt.md',), True),
                                     (('other.prompt.md',), False),
                                     (('other.prompt.md', 'extra.prompt.md'), False)):
            for json_mode in (False, True):
                with self.subTest(prompts=prompts, json_mode=json_mode):
                    result, output = self.status(records=('round.md',), prompts=prompts,
                                                 json_mode=json_mode)
                    self.assertEqual(result, 0 if expected_ok else 2)
                    if json_mode:
                        data = json.loads(output)
                        self.assertEqual(data["ok"], expected_ok)
                        self.assertEqual(data["collab"]["missing_prompts"],
                                         [] if expected_ok else ["round.md"])
                    else:
                        self.assertEqual("all four layers in sync" in output, expected_ok)
                        if not expected_ok:
                            self.assertIn("round.md", output)

    def test_finding_links_detect_deleted_and_renamed_files(self):
        old_path = (Path.cwd() / "offline-status-fixture" / "notes" / "findings" / "old.md")
        cases = (
            # An explicit file link remains meaningful with a citation unrelated to its path.
            ({"source": "https://doi.org/example", "finding_file": old_path.as_posix()},
             (), "unclaimed"),
            ({"source": "https://doi.org/example",
              "finding_file": (Path.cwd() / "elsewhere" / "deleted.md").as_posix()},
             (), "unclaimed"),
            ({"source": "https://doi.org/example", "finding_file": old_path.as_posix()},
             (("new.md", ["row-1"]),), "mismatched"),
            ({"source": "notes/findings/old.md"},
             (("new.md", ["row-1"]),), "mismatched"),
            ({"source": "https://doi.org/example", "finding_file": old_path.as_posix()},
             (("old.md", ["row-1"]),), None),
            # The explicit link wins over a legacy source path.
            ({"source": "notes/findings/other.md", "finding_file": old_path.as_posix()},
             (("old.md", ["row-1"]),), None),
            # Independent memories and ambiguous legacy citations retain their old contract.
            ({"kind": "finding"}, (), None),
            ({"source": "https://doi.org/example"}, (), None),
            ({"source": "old.md"}, (("new.md", ["row-1"]),), None),
        )
        for metadata, findings, problem in cases:
            for json_mode in (False, True):
                with self.subTest(metadata=metadata, findings=findings, json_mode=json_mode):
                    result, output = self.status(findings=findings,
                                                 rows=({"id": "row-1", "metadata": metadata},),
                                                 json_mode=json_mode)
                    self.assertEqual(result, 2 if problem else 0)
                    if json_mode:
                        data = json.loads(output)
                        self.assertEqual(data["ok"], problem is None)
                        if problem:
                            self.assertTrue(data["notes"][problem])
                            self.assertIn("row-1", json.dumps(data["notes"][problem]))
                    elif problem:
                        self.assertIn("row-1", output)
                        self.assertNotIn("all four layers in sync", output)


if __name__ == "__main__":
    unittest.main()
