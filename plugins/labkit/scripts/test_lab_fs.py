"""Safety boundaries for recycling generated files and fixtures."""
from pathlib import Path
import os
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import lab_fs


class TrashTests(unittest.TestCase):
    def setUp(self):
        self.parent = Path(tempfile.gettempdir()).resolve()
        self.root = Path(tempfile.mkdtemp(prefix="labkit-trash-test-", dir=self.parent))
        self.addCleanup(lab_fs.trash, self.root, within=self.parent)

    def test_recycles_only_named_descendant(self):
        target = self.root / "generated"
        target.mkdir()
        (target / "result.txt").write_text("generated", encoding="utf-8")
        sibling = self.root / "keep.txt"
        sibling.write_text("keep", encoding="utf-8")
        lab_fs.trash(target, within=self.root)
        self.assertFalse(target.exists())
        self.assertEqual(sibling.read_text(encoding="utf-8"), "keep")

    def test_refuses_boundary_itself_and_outside_target(self):
        allowed = self.root / "allowed"
        allowed.mkdir()
        for target in (allowed, self.root):
            with self.subTest(target=target), mock.patch("lab_fs._move_to_trash") as move:
                with self.assertRaises(ValueError):
                    lab_fs.trash(target, within=allowed)
                move.assert_not_called()
                self.assertTrue(target.exists())

    def test_missing_descendant_needs_no_backend(self):
        with mock.patch("lab_fs._move_to_trash") as move:
            lab_fs.trash(self.root / "missing", within=self.root)
        move.assert_not_called()

    def test_refuses_reparse_point_inside_directory(self):
        target = self.root / "generated"
        target.mkdir()
        redirected = target / "redirected.txt"
        redirected.write_text("keep", encoding="utf-8")
        lstat = Path.lstat

        def file_info(path, *args, **kwargs):
            info = lstat(path, *args, **kwargs)
            if path == redirected:
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x0400)
            return info

        with mock.patch.object(Path, "lstat", autospec=True, side_effect=file_info), \
                mock.patch("lab_fs._move_to_trash") as move:
            with self.assertRaises(ValueError):
                lab_fs.trash(target, within=self.root)
            move.assert_not_called()
        self.assertTrue(redirected.exists())

    def test_failed_recycling_keeps_target_and_raises(self):
        target = self.root / "generated.txt"
        target.write_text("keep", encoding="utf-8")
        with mock.patch("lab_fs._move_to_trash"):
            with self.assertRaises(OSError):
                lab_fs.trash(target, within=self.root)
        self.assertTrue(target.exists())

    def test_no_nonwindows_backend_preserves_target(self):
        with mock.patch("lab_fs.os.name", "posix"), \
                mock.patch("lab_fs.shutil.which", return_value=None), \
                mock.patch("lab_fs.subprocess.run") as run:
            with self.assertRaises(RuntimeError):
                lab_fs._move_to_trash(self.root)
            run.assert_not_called()
        self.assertTrue(self.root.exists())

    @unittest.skipUnless(os.name == "nt", "Windows recycling callback")
    def test_windows_sink_refuses_permanent_deletion(self):
        from win32com.server.exception import COMException
        sink = lab_fs._RecycleOnlySink(None)
        self.assertIsNone(sink.PreDeleteItem(0x0080, None))
        with self.assertRaises(COMException) as error:
            sink.PreDeleteItem(0, None)
        self.assertEqual(error.exception.scode, -2147467260)

    @unittest.skipUnless(os.name == "nt", "Windows recycling callback")
    def test_windows_shell_honors_sink_refusal(self):
        from win32com.server.exception import COMException
        target = self.root / "blocked.txt"
        target.write_text("preserved", encoding="utf-8")
        refusal = COMException("refusing permanent deletion", scode=-2147467260)
        with mock.patch.object(lab_fs._RecycleOnlySink, "PreDeleteItem", side_effect=refusal):
            with self.assertRaises(OSError):
                lab_fs.trash(target, within=self.root)
        self.assertEqual(target.read_text(encoding="utf-8"), "preserved")

    @unittest.skipUnless(os.name == "nt", "Windows recycling dependency")
    def test_missing_pywin32_preserves_target(self):
        with mock.patch.dict(sys.modules, {"pythoncom": None}):
            with self.assertRaisesRegex(RuntimeError, "requires pywin32"):
                lab_fs.trash(self.root, within=self.parent)
        self.assertTrue(self.root.exists())


if __name__ == "__main__":
    unittest.main()
