"""Offline regression checks for capture guards; no mem0 service calls.

Run alongside lab_selftest.py, which verifies the real service integration.
"""
import contextlib
import io
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import lab_mem
import lab_selftest
from lab_fs import trash


class Memory:
    """Record mutations while exposing rows only in the requested namespace."""

    def __init__(self, rows=()):
        self.rows = {row["id"]: row for row in rows}
        self.added = []
        self.deleted = []

    def get_all(self, *, filters):
        return [r for r in self.rows.values() if r["user_id"] == filters["user_id"]]

    def add(self, messages, *, user_id, metadata):
        self.added.append(messages)
        self.rows["new"] = {"id": "new", "user_id": user_id, "metadata": metadata,
                            "memory": messages[0]["content"]}
        return {"status": "PENDING"}

    def delete(self, *, memory_id):
        self.deleted.append(memory_id)
        self.rows.pop(memory_id, None)


class CaptureGuards(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(
            prefix="labkit-offline-", dir=Path(tempfile.gettempdir()).resolve()))
        self.addCleanup(trash, self.temp, within=Path(tempfile.gettempdir()).resolve())
        self.finding = self.temp / "finding.md"
        self.finding.write_text("---\nfinding: old claim\nmem0_rows: []\n---\nbody\n",
                                encoding="utf-8")
        self.memory = Memory([{"id": "old", "user_id": "proj-test"}])
        self.args = Namespace(project="test", text="New claim", source=None, page=None,
                              tag=None, kind="finding", agent="human", supersede=[],
                              finding_file=str(self.finding), allow_orphans=False,
                              no_wait=False)
        self.enterContext(patch.object(lab_mem, "client", return_value=self.memory))
        self.enterContext(patch.object(lab_mem.time, "sleep"))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def amend(self):
        lab_mem.write_mem0_rows(self.finding, ["old"])
        self.finding.write_text(self.finding.read_text(encoding="utf-8")
                                .replace("old claim", "new claim"), encoding="utf-8")

    def test_allow_orphans_keeps_drift_visible(self):
        self.amend()
        self.args.allow_orphans = True
        self.assertEqual(lab_mem.cmd_capture(self.args), 0)
        self.assertEqual(lab_mem.read_mem0_rows(self.finding), ["old", "new"])
        self.assertEqual(lab_mem.read_frontmatter_value(self.finding, "mem0_indexed_hash"),
                         lab_mem.UNSTAMPED)

    def test_existing_frontmatter_checks(self):
        with patch.object(lab_selftest, "PASSED", []), patch.object(lab_selftest, "FAILED", []):
            lab_selftest.offline_frontmatter_checks(lab_mem, self.temp)
            self.assertEqual(lab_selftest.FAILED, [])

    def test_legacy_rows_need_full_supersede_before_stamping(self):
        self.finding.write_text("---\nfinding: amended\nmem0_rows: [old]\n---\nbody\n",
                                encoding="utf-8")
        before = self.finding.read_bytes()
        with self.assertRaisesRegex(SystemExit, "supersede old"):
            lab_mem.cmd_capture(self.args)
        self.assertEqual(self.memory.added, [])
        self.assertEqual(self.finding.read_bytes(), before)

    def test_listed_foreign_id_is_not_delete_authorization(self):
        self.memory.rows["foreign"] = {"id": "foreign", "user_id": "proj-other"}
        lab_mem.write_mem0_rows(self.finding, ["old", "foreign", "already-gone"])
        self.args.supersede = ["old", "foreign", "already-gone"]
        self.assertEqual(lab_mem.cmd_capture(self.args), 0)
        self.assertEqual(self.memory.deleted, ["old"])
        self.assertIn("foreign", self.memory.rows)
        self.assertEqual(lab_mem.read_mem0_rows(self.finding), ["new"])

    def test_invalid_finding_is_rejected_before_remote_mutation(self):
        for content in (None, "no frontmatter\n"):
            with self.subTest(content=content):
                self.args.finding_file = str(self.temp / "invalid.md")
                invalid = Path(self.args.finding_file)
                if content is not None:
                    invalid.write_text(content, encoding="utf-8")
                self.args.supersede = ["old"]
                with self.assertRaises(SystemExit):
                    lab_mem.cmd_capture(self.args)
                self.assertEqual(self.memory.added, [])
                self.assertEqual(self.memory.deleted, [])

    def test_full_supersede_clears_drift(self):
        self.amend()
        self.args.supersede = ["old"]
        self.assertEqual(lab_mem.cmd_capture(self.args), 0)
        self.assertEqual(self.memory.deleted, ["old"])
        self.assertEqual(lab_mem.read_mem0_rows(self.finding), ["new"])
        self.assertEqual(lab_mem.read_frontmatter_value(self.finding, "mem0_indexed_hash"),
                         lab_mem.content_hash(self.finding))

    def test_finding_supplies_default_source(self):
        lab_mem.cmd_capture(self.args)
        self.assertEqual(self.memory.rows["new"]["metadata"]["source"],
                         self.finding.as_posix())
        self.assertEqual(self.memory.rows["new"]["metadata"]["finding_file"],
                         self.finding.resolve().as_posix())

    def test_external_citation_keeps_finding_pointer(self):
        self.args.source = "doi:10.1234/example"
        lab_mem.cmd_capture(self.args)
        self.assertEqual(self.memory.rows["new"]["metadata"]["source"], self.args.source)
        self.assertEqual(self.memory.rows["new"]["metadata"]["finding_file"],
                         self.finding.resolve().as_posix())

    def test_unconfirmed_delete_keeps_row_and_drift(self):
        self.amend()
        self.args.supersede = ["old"]
        with patch.object(self.memory, "delete"):
            lab_mem.cmd_capture(self.args)
        self.assertEqual(lab_mem.read_mem0_rows(self.finding), ["old", "new"])
        self.assertEqual(lab_mem.read_frontmatter_value(self.finding, "mem0_indexed_hash"),
                         lab_mem.UNSTAMPED)


if __name__ == "__main__":
    unittest.main()
