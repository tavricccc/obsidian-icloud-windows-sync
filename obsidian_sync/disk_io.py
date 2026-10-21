import os
import asyncio
import shutil
import ctypes
import platform
import time
import tempfile

from datetime import datetime
from ctypes import wintypes

from .icloud_status import ICloudStatusChecker


MOVEFILE_REPLACE_EXISTING = 0x1
MOVEFILE_WRITE_THROUGH = 0x8
FILE_ATTRIBUTE_NORMAL = 0x80

if platform.system() not in ("Windows", "Microsoft"):
    MoveFileExW = None
    SetFileAttributesW = None


def ensure_dir(path: str) -> None:
    if not os.path.exists(path):
        os.makedirs(path, exist_ok=True)


def safe_exists(path: str) -> bool:
    try:
        return os.path.exists(path)
    except Exception:
        return False


def size_or_zero(path: str) -> int:
    try:
        return os.path.getsize(path)
    except Exception:
        return 0


def safe_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except Exception:
        return 0


def same_drive(path_a: str, path_b: str) -> bool:
    return os.path.splitdrive(os.path.abspath(path_a))[0].lower() == os.path.splitdrive(os.path.abspath(path_b))[0].lower()


class DiskIO:
    """
    Handles atomic file writes, stable-file waits, and iCloud-aware copies.
    """
    def __init__(self, config, logger):
        if platform.system() not in ("Windows", "Microsoft"):
            raise RuntimeError("Disk operations are only supported on Windows.")

        self.config = config
        self.log = logger
        self.icloud_checker = ICloudStatusChecker()

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._set_file_attributes = kernel32.SetFileAttributesW
        self._set_file_attributes.argtypes = (wintypes.LPCWSTR, wintypes.DWORD)
        self._set_file_attributes.restype = wintypes.BOOL
        self._move_file_ex = kernel32.MoveFileExW
        self._move_file_ex.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
        self._move_file_ex.restype = wintypes.BOOL

    def set_normal_attributes(self, path: str) -> bool:
        try:
            if not os.path.exists(path):
                return True
            return bool(self._set_file_attributes(str(path), FILE_ATTRIBUTE_NORMAL))
        except Exception:
            return False

    def _replace_with_retries(
        self,
        tmp: str,
        dst: str,
        timeout_seconds: float = 30.0,
        retry_seconds: float = 0.25,
    ) -> None:
        # Replace is used instead of writing dst in-place so readers never see partial bytes.
        # Retries handle transient file locks from editors, AV, or cloud sync daemons.
        deadline = time.time() + timeout_seconds
        last_error = None

        while time.time() < deadline:
            try:
                if os.path.exists(dst):
                    self.set_normal_attributes(dst)
                os.replace(tmp, dst)
                return
            except PermissionError as exc:
                last_error = exc
                time.sleep(retry_seconds)

        try:
            ok = self._move_file_ex(
                tmp,
                dst,
                MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
            )
            if ok:
                return
        except Exception as exc:
            last_error = exc

        try:
            if os.path.exists(dst):
                os.remove(dst)
            os.replace(tmp, dst)
            return
        except Exception as exc:
            last_error = exc

        raise PermissionError(f"Unable to replace {dst}") from last_error

    def _copy_replace_sync(self, src: str, dst: str) -> None:
        ensure_dir(os.path.dirname(dst))
        tmp = dst + ".tmp"
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
            shutil.copy2(src, tmp)
            self._replace_with_retries(tmp, dst)
        except Exception:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            finally:
                raise

    def _copy_overwrite_with_retries(
        self,
        src: str,
        dst: str,
        timeout_seconds: float = 30.0,
        retry_seconds: float = 0.25,
    ) -> None:
        # iCloud can interpret rename-style replacement as delete/create/move churn.
        # For iCloud targets, prefer in-place overwrite with retries to reduce conflict-duplicate creation.
        ensure_dir(os.path.dirname(dst))
        deadline = time.time() + timeout_seconds
        last_error = None

        while time.time() < deadline:
            try:
                if os.path.exists(dst):
                    self.set_normal_attributes(dst)
                shutil.copy2(src, dst)
                return
            except PermissionError as exc:
                last_error = exc
                time.sleep(retry_seconds)

        raise PermissionError(f"Unable to overwrite {dst}") from last_error

    async def wait_for_stable_file(
        self,
        path: str,
        stable_seconds: float = 0.5,
        timeout_seconds: float = 30.0,
        poll_seconds: float = 0.25,
    ) -> bool:
        deadline = time.time() + timeout_seconds
        stable_since = None
        previous = None

        while time.time() < deadline:
            try:
                with open(path, "rb"):
                    current = (os.path.getsize(path), os.path.getmtime(path))
            except OSError:
                stable_since = None
                previous = None
                await asyncio.sleep(poll_seconds)
                continue

            if current == previous:
                if stable_since is None:
                    stable_since = time.time()
                if time.time() - stable_since >= stable_seconds:
                    return True
            else:
                previous = current
                stable_since = None

            await asyncio.sleep(poll_seconds)

        return False

    async def copy_to_disk(self, src: str, dst: str) -> None:
        self.log.info("copy_to_disk", f"{self.config.disp(src)} -> {self.config.disp(dst)}", level="verbose")
        try:
            await asyncio.to_thread(self._copy_replace_sync, src, dst)
        except Exception as e:
            self.log.error("FAILED", f"Copy {self.config.disp(src)} -> {self.config.disp(dst)}: {e}")
            raise

        if not await self.wait_for_stable_file(dst):
            raise TimeoutError(f"Copied file did not become stable: {dst}")
        self.log.success("SUCCESS", f"Updated: {self.config.disp(dst)}", level="verbose")

    async def copy_to_icloud(self, src: str, dst: str) -> None:
        self.log.info("copy_to_icloud", f"{self.config.disp(src)} -> {self.config.disp(dst)}", level="verbose")
        try:
            await asyncio.to_thread(self._copy_overwrite_with_retries, src, dst)
        except Exception as e:
            self.log.error("FAILED", f"Copy {self.config.disp(src)} -> {self.config.disp(dst)}: {e}")
            raise

        if not await self.wait_for_stable_file(dst):
            raise TimeoutError(f"Copied file did not become stable: {dst}")
        self.log.success("SUCCESS", f"Updated: {self.config.disp(dst)}", level="verbose")

        # For iCloud targets, block completion until the daemon reports upload/sync is settled.
        if not getattr(self.config, "check_icloud_status", True):
            return
        self.log.info("ICLOUD_WAIT", f"Waiting for iCloud to settle: {self.config.disp(dst)}", level="verbose")
        stable_seconds = max(1.0, float(getattr(self.config, "stability_window", 0)))
        await self.icloud_checker.wait_until_uploaded(
            dst,
            stable_seconds=stable_seconds,
            on_update=lambda message: self.log.info(
                "ICLOUD_TRACE",
                f"{self.config.disp(dst)} :: {message}",
                level="verbose",
            ),
        )
        self.log.success("SUCCESS", f"iCloud settled: {self.config.disp(dst)}", level="verbose")

    async def wait_for_icloud_readable(self, src: str) -> None:
        if not getattr(self.config, "check_icloud_status", True):
            return
        self.log.info("ICLOUD_WAIT", f"Waiting for iCloud file: {self.config.disp(src)}", level="verbose")
        stable_seconds = max(1.0, float(getattr(self.config, "stability_window", 0)))
        await self.icloud_checker.wait_until_uploaded(
            src,
            stable_seconds=stable_seconds,
            on_update=lambda message: self.log.info(
                "ICLOUD_TRACE",
                f"{self.config.disp(src)} :: {message}",
                level="verbose",
            ),
        )

    async def copy_from_icloud(self, src: str, dst: str) -> None:
        self.log.info("copy_from_icloud", f"{self.config.disp(src)} -> {self.config.disp(dst)}", level="verbose")
        await self.wait_for_icloud_readable(src)
        await self.copy_to_disk(src, dst)

    async def async_copy(self, src: str, dst: str) -> None:
        await self.copy_to_disk(src, dst)

    async def remove_file(self, path: str, description: str):
        try:
            if not os.path.exists(path):
                return
            try:
                await asyncio.to_thread(os.remove, path)
            except FileNotFoundError:
                return
            self.log.success("SUCCESS", f"Removed {description}: {self.config.disp(path)}", level="verbose")

            await self.prune_empty_dirs(os.path.dirname(path))
        except Exception as e:
            self.log.error("FAILED", f"Remove {description} {path}: {e}")

    async def prune_empty_dirs(self, dir_path: str):
        """
        Removes now-empty parent directories, stopping at (and never removing) a sync root.
        Paths are normalized so a trailing slash or different casing in the config can't
        let the walk remove the vault root itself or wander outside it.
        """
        def norm(p: str) -> str:
            return os.path.normcase(os.path.abspath(p))

        roots = [norm(r) for r in (self.config.local_vault, self.config.icloud_vault, self.config.history_dir) if r]
        current = norm(dir_path)
        while any(current != r and current.startswith(r.rstrip(os.sep) + os.sep) for r in roots):
            if not os.path.isdir(current) or os.listdir(current):
                break
            try:
                await asyncio.to_thread(os.rmdir, current)
            except OSError:
                break
            self.log.info("INFO", f"Removed empty dir: {self.config.disp(current)}", level="verbose")
            current = os.path.dirname(current)

    def remove_file_sync(self, path: str, description: str):
        try:
            if os.path.exists(path):
                os.remove(path)
                self.log.success("SUCCESS", f"Removed {description}: {self.config.disp(path)}", level="verbose")
        except Exception as e:
            self.log.error("FAILED", f"Remove {description} {path}: {e}")

    async def create_conflict_duplicate(self, path: str):
        base, ext = os.path.splitext(path)
        conflict = f"{base}_CONFLICT_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}{ext}"
        self.log.warn("WARNING", f"Creating conflict duplicate: {self.config.disp(conflict)}", level="verbose")
        try:
            await asyncio.to_thread(shutil.copy2, path, conflict)
        except Exception as e:
            self.log.error("DANGER", f"Failed to create conflict duplicate: {e}")
