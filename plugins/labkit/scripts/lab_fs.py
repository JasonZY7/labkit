"""Recycle explicitly scoped paths, without a permanent-delete fallback."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import subprocess


def _reject_redirect(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x0400:
        raise ValueError(f"refusing to recycle through a symlink or reparse point: {path}")


def _canonical(path: str | os.PathLike[str]) -> Path:
    absolute = Path(os.path.abspath(path))
    if absolute.resolve() != absolute:
        raise ValueError(f"refusing to recycle a redirected path: {absolute}")
    for ancestor in (absolute, *absolute.parents):
        _reject_redirect(ancestor)
    return absolute


def _walk_error(error: OSError) -> None:
    raise error


def trash(path: str | os.PathLike[str], *, within: str | os.PathLike[str]) -> None:
    """Recycle a strict descendant of ``within``; missing targets are harmless.

    The boundary, target, ancestors, and directory contents must not redirect via
    symlinks or Windows reparse points. Failed validation or recycling raises.
    """
    boundary, target = _canonical(within), _canonical(path)
    if not boundary.is_dir() or target == boundary or not target.is_relative_to(boundary):
        raise ValueError(f"recycle target must be strictly inside {boundary}: {target}")
    if not target.exists():
        return
    if target.is_dir():
        for directory, dirs, files in os.walk(target, followlinks=False, onerror=_walk_error):
            for name in dirs + files:
                _reject_redirect(Path(directory) / name)
    _move_to_trash(target)
    if target.exists():
        raise OSError(f"recycling did not remove the target: {target}")


def _move_to_trash(path: Path) -> None:
    if os.name != "nt":
        command = shutil.which("trash")
        if command:
            args = [command, str(path)]
        else:
            command = shutil.which("gio")
            if not command:
                raise RuntimeError("recycling requires trash or gio; target was preserved")
            args = [command, "trash", str(path)]
        subprocess.run(args, stdin=subprocess.DEVNULL, check=True)
        return

    _windows_trash(path)


class _RecycleOnlySink:
    """Cancel before the Shell would permanently delete an item."""
    _public_methods_ = [
        "StartOperations", "FinishOperations", "PreRenameItem", "PostRenameItem",
        "PreMoveItem", "PostMoveItem", "PreCopyItem", "PostCopyItem",
        "PreDeleteItem", "PostDeleteItem", "PreNewItem", "PostNewItem",
        "UpdateProgress", "ResetTimer", "PauseTimer", "ResumeTimer",
    ]

    def __init__(self, interface):
        self._com_interfaces_ = [interface]

    def PreDeleteItem(self, flags, item):
        if not flags & 0x0080:  # TSF_DELETE_RECYCLE_IF_POSSIBLE
            from win32com.server.exception import COMException
            # Raising translates to an HRESULT failure; returning one does not.
            raise COMException("refusing permanent deletion", scode=-2147467260)  # E_ABORT

    def _ignore(self, *args):
        return None

    StartOperations = FinishOperations = PreRenameItem = PostRenameItem = _ignore
    PreMoveItem = PostMoveItem = PreCopyItem = PostCopyItem = PostDeleteItem = _ignore
    PreNewItem = PostNewItem = UpdateProgress = ResetTimer = PauseTimer = ResumeTimer = _ignore


def _windows_trash(path: Path) -> None:
    try:
        import pythoncom
        from win32com.server.policy import DesignatedWrapPolicy
        from win32com.shell import shell
    except ImportError as error:
        raise RuntimeError("Windows recycling requires pywin32; target was preserved") from error

    pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
    operation = item = wrapped = None
    try:
        operation = pythoncom.CoCreateInstance(
            shell.CLSID_FileOperation, None, pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IFileOperation)
        # Silent, no confirmation/error UI, early failure, recycle, and record undo.
        operation.SetOperationFlags(0x0004 | 0x0010 | 0x0400 | 0x00100000 | 0x00080000 | 0x20000000)
        item = shell.SHCreateItemFromParsingName(str(path), None, shell.IID_IShellItem)
        sink = _RecycleOnlySink(shell.IID_IFileOperationProgressSink)
        wrapped = pythoncom.WrapObject(DesignatedWrapPolicy(sink), shell.IID_IFileOperationProgressSink)
        operation.DeleteItem(item, wrapped)
        result = operation.PerformOperations()
        if result or operation.GetAnyOperationsAborted():
            raise OSError("recycling was aborted; permanent deletion is forbidden")
    except pythoncom.com_error as error:
        raise OSError(error.hresult, "could not recycle the target", str(path)) from error
    finally:
        operation = item = wrapped = None
        pythoncom.CoUninitialize()
