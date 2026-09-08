from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO


class AlreadyRunningError(RuntimeError):
    pass


class MachineInstanceLock:
    """One collector per Windows machine, across sessions and data directories.

    Keep the named object handle until shutdown. The OS releases it even after
    a crash; no PID file or stale-lock deletion is necessary.
    """

    NAME = "Global\\HawkHive.DPC8001.Collector.Singleton.v1"

    def __init__(self) -> None:
        self._handle = None
        self._kernel = None

    def acquire(self) -> None:
        if os.name != "nt" or self._handle is not None:
            return
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        ctypes.set_last_error(0)
        handle = kernel.CreateMutexW(None, False, self.NAME)
        error = ctypes.get_last_error()
        if not handle:
            if error == 5:  # Existing mutex owned by a different Windows user.
                raise AlreadyRunningError("Collector is running in another Windows session, or its machine lock is inaccessible")
            raise ctypes.WinError(error)
        if error == 183:  # ERROR_ALREADY_EXISTS
            kernel.CloseHandle(handle)
            raise AlreadyRunningError("Another collector is already running on this Windows computer")
        self._kernel, self._handle = kernel, handle

    def release(self) -> None:
        if self._handle is not None:
            self._kernel.CloseHandle(self._handle)
            self._handle = None


class SingleInstanceLock:
    """Cross-session process lock backed by the shared runtime data directory."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._stream: BinaryIO | None = None

    def acquire(self) -> None:
        if self._stream is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                if stream.read(1) == b"":
                    stream.seek(0)
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            stream.close()
            raise AlreadyRunningError("Another collector process is already running") from exc
        self._stream = stream

    def release(self) -> None:
        stream = self._stream
        if stream is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()
            self._stream = None

    def __enter__(self) -> SingleInstanceLock:  # noqa: PYI034 - Python 3.10 compatible
        self.acquire()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.release()
