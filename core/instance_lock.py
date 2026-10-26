from __future__ import annotations

import hashlib
import os
from pathlib import Path


class InstanceAlreadyRunningError(RuntimeError):
    pass


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = None
        self._mutex_handle: int | None = None
        self._acquired = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            self._acquire_windows_mutex()
        else:
            self._acquire_file_lock()
        handle = self.path.open("wb")
        handle.write(str(os.getpid()).encode("ascii"))
        handle.flush()
        self._handle = handle
        self._acquired = True

    def _acquire_windows_mutex(self) -> None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        digest = hashlib.sha256(
            str(self.path.resolve()).casefold().encode("utf-8")
        ).hexdigest()[:20]
        handle = kernel32.CreateMutexW(None, False, f"Local\\BlueFishQQBot-{digest}")
        if not handle:
            raise InstanceAlreadyRunningError("无法创建单实例互斥量")
        if ctypes.get_last_error() == 183:
            kernel32.CloseHandle(handle)
            raise InstanceAlreadyRunningError(
                f"已有实例正在运行，锁文件：{self.path}"
            )
        self._mutex_handle = int(handle)

    def _acquire_file_lock(self) -> None:
        handle = self.path.open("a+b")
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            handle.close()
            raise InstanceAlreadyRunningError(
                f"已有实例正在运行，锁文件：{self.path}"
            ) from exc
        self._handle = handle

    def release(self) -> None:
        if not self._acquired:
            return
        try:
            if os.name != "nt" and self._handle is not None:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            if self._handle is not None:
                self._handle.close()
                self._handle = None
            if self._mutex_handle is not None:
                import ctypes
                from ctypes import wintypes

                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
                kernel32.CloseHandle.restype = wintypes.BOOL
                kernel32.CloseHandle(self._mutex_handle)
                self._mutex_handle = None
            try:
                if (
                    self.path.exists()
                    and self.path.read_text(encoding="ascii").strip()
                    == str(os.getpid())
                ):
                    self.path.unlink()
            except OSError:
                pass
            self._acquired = False

