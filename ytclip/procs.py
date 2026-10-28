"""Processes on this PC: is one still running, what program is it, which run from a folder.

Plain Windows API calls through ctypes (no PowerShell or WMI), with /proc and os.kill elsewhere.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path


def boot_time() -> float:
    """When this PC last started (a time.time()), or 0 when it can't be told. A heartbeat from before
    then is from a process that's gone, whatever has its process id now."""
    try:
        if sys.platform == "win32":
            import ctypes

            ticks = ctypes.WinDLL("kernel32").GetTickCount64
            ticks.restype = ctypes.c_ulonglong
            return time.time() - ticks() / 1000
        if sys.platform.startswith("linux"):
            with open("/proc/stat", encoding="ascii") as f:
                return float(next(line.split()[1] for line in f if line.startswith("btime")))
    except (OSError, AttributeError, StopIteration, ValueError):
        pass
    return 0.0


def process_alive(pid: str | int) -> bool:
    """Whether a process with this id is running on this PC. True when it can't be told."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: it exists, it's just not ours
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # it exists, but belongs to someone else
        return True
    return True


def process_path(pid: str | int) -> str:
    """The full path of a running process's program file, or '' when it can't be told (or it isn't Windows)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return ""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                        ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return ""
            return buf.value
        finally:
            kernel32.CloseHandle(handle)
    return ""


def process_name(pid: str | int) -> str:
    """The program file name of a running process ("ClipMonster.exe", "python3"), or '' when it can't be told."""
    if sys.platform == "win32":
        path = process_path(pid)
        return Path(path).name if path else ""
    try:
        with open(f"/proc/{int(pid)}/comm", encoding="utf-8") as f:
            return f.read().strip()
    except (OSError, TypeError, ValueError):
        return ""


def app_processes(folder: Path) -> list[tuple[int, Path]]:
    """(process id, program file) of each running program whose file is inside `folder`, other than
    this process. Windows only (elsewhere nothing runs from an installed app folder)."""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    snap = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == wintypes.HANDLE(-1).value:
        return []
    pids = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            pids.append(int(entry.th32ProcessID))
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    root = os.path.normcase(str(Path(folder).resolve())).rstrip("\\/") + os.sep
    found = []
    for pid in pids:
        if pid in (0, 4, os.getpid()):
            continue
        path = process_path(pid)
        if path and os.path.normcase(path).startswith(root):
            found.append((pid, Path(path)))
    return found
