import os
import asyncio
import traceback
import tempfile

from colorama import Fore

from .logger import colored
from .disk_io import safe_exists, size_or_zero, safe_mtime, ensure_dir


class FileSynchronizer:
    """
    Applies three-way sync rules for individual files.
    """
    def __init__(self, config, logger, hasher, disk_io, duplicates):
        """
        Initializes the per-file synchronizer.

        Args:
            config (SyncConfig): The application configuration.
            logger (SyncLogger): The logger instance.
            hasher (FileHasher): The file hashing and state caching service.
            disk_io (DiskIO): The disk I/O handler for safe file operations.
            duplicates (DuplicateScanner): The scanner used for pre-flight cleanup.
        """
        self.config = config
        self.log = logger
        self.hasher = hasher
        self.io = disk_io
        self.duplicates = duplicates
        self.io_semaphore = asyncio.Semaphore(self.config.max_concurrent_io)
        self._vaults_were_available = True

    @staticmethod
    def _has_entries(root: str) -> bool:
        try:
            with os.scandir(root) as it:
                return any(True for _ in it)
        except OSError:
            return False

    def vaults_available(self) -> bool:
        """
        Returns False when a vault looks unmounted or wiped, e.g. iCloud signed out,
        the drive not attached yet, or the folder reset by iCloud for Windows.

        A vault is unavailable if its folder is missing, or if it is empty while
        history still holds files (we have synced before, so an empty side means
        "can't see it", not "user deleted everything"). Syncing in that state would
        apply the delete rules to every file and wipe the other side.
        """
        cfg = self.config
        history_has_files = self._has_entries(cfg.history_dir)
        for root in (cfg.local_vault, cfg.icloud_vault):
            if not os.path.isdir(root):
                return False
            if history_has_files and not self._has_entries(root):
                return False
        return True

    def check_vaults(self) -> bool:
        ok = self.vaults_available()
        if not ok and self._vaults_were_available:
            self.log.error(
                "SAFETY",
                "A vault is missing or empty while history has files; pausing all sync to avoid mass deletion. "
                "Check that iCloud Drive is signed in and both folders are reachable.",
                level="important",
            )
        elif ok and not self._vaults_were_available:
            self.log.success("SAFETY", "Both vaults reachable again; resuming sync.", level="important")
        self._vaults_were_available = ok
        return ok

    async def _copy_via_staging(self, source: str, destinations: list[tuple[str, str]], source_is_icloud: bool = False):
        staging_root = os.path.join(self.config.logs_dir, ".obsidian_sync_staging")
        ensure_dir(staging_root)
        fd, staged = tempfile.mkstemp(dir=staging_root, prefix="snapshot_", suffix=".tmp")
        os.close(fd)

        try:
            if source_is_icloud:
                await self.io.copy_from_icloud(source, staged)
            else:
                await self.io.copy_to_disk(source, staged)

            for method, destination in destinations:
                if method == "icloud":
                    await self.io.copy_to_icloud(staged, destination)
                else:
                    await self.io.copy_to_disk(staged, destination)
        finally:
            try:
                if os.path.exists(staged):
                    await asyncio.to_thread(os.remove, staged)
            except Exception as e:
                self.log.warn("STAGING", f"Failed to remove staging file {self.config.disp(staged)}: {e}", level="verbose")

    async def restore_to_icloud(self, rel: str):
        await self.push_to_icloud(rel)

    # Core file operations

    async def push_to_icloud(self, rel: str):
        """
        Asynchronously pushes a file from the local vault to iCloud and history.
        iCloud is written first; history is only updated once iCloud succeeded.
        History is the "last known synced" baseline, so updating it before
        iCloud would make a failed upload look like a remote change on the
        next pass and overwrite the newer local file with the stale iCloud one.

        Args:
            rel (str): The relative file path to push.
        """
        local = os.path.join(self.config.local_vault, rel)
        icloud = os.path.join(self.config.icloud_vault, rel)
        history = os.path.join(self.config.history_dir, rel)
        await self._copy_via_staging(
            source=local,
            destinations=[("icloud", icloud), ("disk", history)],  # History last: only after iCloud succeeded
            source_is_icloud=False,
        )

    async def restore_from_icloud(self, rel: str):
        """
        Asynchronously pulls a file from the iCloud vault to local and history.

        Args:
            rel (str): The relative file path to pull.
        """
        local = os.path.join(self.config.local_vault, rel)
        icloud = os.path.join(self.config.icloud_vault, rel)
        history = os.path.join(self.config.history_dir, rel)
        await self._copy_via_staging(
            source=icloud,
            destinations=[("disk", local), ("disk", history)],
            source_is_icloud=True,
        )

    # Per-file sync logic

    async def recheck(self, local: str, icloud: str, history: str, rel_path: str) -> tuple[str | None, str | None, str | None]:
        """
        Waits for the stability window and retrieves fresh hashes for all locations.

        Args:
            local (str): Absolute path to the local file.
            icloud (str): Absolute path to the iCloud file.
            history (str): Absolute path to the history file.
            rel_path (str): Relative path of the file (used for caching).
        Returns:
            tuple[str | None, str | None, str | None]: The forced SHA-256 hashes for Local, iCloud, and History respectively.
        """
        await asyncio.sleep(self.config.stability_window)
        Lh = await self.hasher.get_cached_hash(local, 'L', rel_path, force=True) if safe_exists(local) else None
        Ch = await self.hasher.get_cached_hash(icloud, 'C', rel_path, force=True) if safe_exists(icloud) else None
        Hh = await self.hasher.get_cached_hash(history, 'H', rel_path, force=True) if safe_exists(history) else None
        return Lh, Ch, Hh

    async def sync_file(self, rel_path: str):
        """
        Executes the core three-way sync logic for a single file path. Evaluates the existence and hashes across Local, iCloud, and History to determine the correct action (Push, Pull, Delete, Seed, or Conflict Resolution).

        Args:
            rel_path (str): The relative file path to synchronize.
        """
        cfg = self.config
        # if cfg.stability_window > 0:
        #     await asyncio.sleep(cfg.stability_window)

        local = os.path.join(cfg.local_vault, rel_path)
        icloud = os.path.join(cfg.icloud_vault, rel_path)
        history = os.path.join(cfg.history_dir, rel_path)
        d = cfg.disp(rel_path)

        if not self.check_vaults():
            return

        L_exists = safe_exists(local)
        C_exists = safe_exists(icloud)
        H_exists = safe_exists(history)

        # Nothing exists anywhere
        if not L_exists and not C_exists:
            if H_exists:
                self.log.warn("REMOVING HISTORY", f"{colored('No local', Fore.RED)} & {colored('No iCloud', Fore.RED)} for {d}", level="important")
                await self.io.remove_file(history, "history")
            # Clean stale state
            if rel_path in self.hasher.state:
                del self.hasher.state[rel_path]
                self.hasher.dirty = True
            return

        # Local missing, C+H exist
        if not L_exists and C_exists and H_exists:
            self.log.warn("DELETE", f"{colored('Local missing', Fore.RED)}, stabilizing for {d}", level="verbose")
            Lh, Ch, Hh = await self.recheck(local, icloud, history, rel_path)
            if Ch is not None and Hh is not None and Ch == Hh:
                self.log.delete(f"{colored('Removing from iCloud', Fore.CYAN)} & history for {d}", d, level="verbose")
                await self.io.remove_file(icloud, "iCloud")
                await self.io.remove_file(history, "history")
            else:
                self.log.pull(f"{colored('Restoring to local', Fore.GREEN)} from iCloud for {d}", d, level="verbose")
                await self.restore_from_icloud(rel_path)
            return

        # iCloud missing, L+H exist
        if not C_exists and L_exists and H_exists:
            self.log.warn("DELETE", f"{colored('iCloud missing', Fore.RED)}, stabilizing for {d}", level="verbose")
            Lh, Ch, Hh = await self.recheck(local, icloud, history, rel_path)
            if Lh is not None and Hh is not None and Lh == Hh:
                self.log.delete(f"{colored('Removing local', Fore.RED)} & history for {d}", d, level="verbose")
                await self.io.remove_file(local, "local")
                await self.io.remove_file(history, "history")
            else:
                self.log.push(f"Local changed for {d} -> pushing to iCloud", d, level="verbose")
                await self.push_to_icloud(rel_path)
            return

        # New local file (L only)
        if L_exists and not C_exists and not H_exists:
            self.log.new(f"{colored('Local-only', Fore.GREEN)}, stabilizing {d}", d, level="verbose", source="local")
            Lh, Ch, Hh = await self.recheck(local, icloud, history, rel_path)
            if Lh is None:
                self.log.info("SKIP", f"After stabilize local missing for {d}", level="verbose")
                return
            if size_or_zero(local) < cfg.min_seed_size(rel_path):
                self.log.info("SKIP", f"Local too small, deferring {d}", level="verbose")
                return
            self.log.push(f"{colored('Pushing to iCloud', Fore.CYAN)} for {d}", d, level="verbose")
            await self.push_to_icloud(rel_path)
            return

        # New iCloud file (C only)
        if C_exists and not L_exists and not H_exists:
            self.log.new(f"{colored('iCloud-only', Fore.CYAN)}, stabilizing {d}", d, level="verbose", source="icloud")
            Lh, Ch, Hh = await self.recheck(local, icloud, history, rel_path)
            if Ch is None:
                self.log.info("SKIP", f"After stabilize iCloud missing for {d}", level="verbose")
                return
            if size_or_zero(icloud) < cfg.tiny_threshold:
                self.log.info("SKIP", f"iCloud too small, deferring {d}", level="verbose")
                return
            self.log.pull(f"{colored('Restoring to local', Fore.GREEN)} for {d}", d, level="verbose")
            await self.restore_from_icloud(rel_path)
            return

        # Both sides exist or mixed states
        ensure_dir(os.path.dirname(history))

        L = await self.hasher.get_cached_hash(local, 'L', rel_path) if safe_exists(local) else None
        C = await self.hasher.get_cached_hash(icloud, 'C', rel_path) if safe_exists(icloud) else None
        H = await self.hasher.get_cached_hash(history, 'H', rel_path) if safe_exists(history) else None

        # History missing: seed it
        if H is None and (L is not None or C is not None):
            self.log.info("HISTORY MISSING", f"Seeding history for {d}")
            Lh, Ch, Hh = await self.recheck(local, icloud, history, rel_path)

            if Lh is not None and Ch is not None:
                if Lh == Ch:
                    await self.io.copy_to_disk(local, history)
                    H = Lh
                    self.log.info("HISTORY", f"Initialized history (identical) for {d}", level="verbose")
                else:
                    # Conflict at start with no history: newer wins
                    self.log.warn("HISTORY", f"Local and iCloud differ for {d}!", level="verbose")
                    local_m, icloud_m = safe_mtime(local), safe_mtime(icloud)
                    if local_m >= icloud_m:
                        self.log.warn("CONFLICT", f"{colored('Local is newer', Fore.YELLOW)}: {d}", level="important")
                        await self.io.create_conflict_duplicate(icloud)
                        await self.push_to_icloud(rel_path)
                    else:
                        self.log.warn("CONFLICT", f"{colored('iCloud is newer', Fore.YELLOW)}: {d}", level="important")
                        await self.io.create_conflict_duplicate(local)
                        await self.restore_from_icloud(rel_path)
                    return
            elif Lh is not None and size_or_zero(local) >= cfg.min_seed_size(rel_path):
                await self.io.copy_to_disk(local, history)
                H = Lh
                self.log.info("HISTORY", f"Initialized {colored('from local', Fore.GREEN)} for {d}", level="verbose")
            elif Ch is not None and size_or_zero(icloud) >= cfg.min_seed_size(rel_path):
                await self.io.copy_from_icloud(icloud, history)
                H = Ch
                self.log.info("HISTORY", f"Initialized {colored('from iCloud', Fore.CYAN)} for {d}", level="verbose")
            else:
                if size_or_zero(local) > 0 or size_or_zero(icloud) > 0:
                    self.log.error("FAILED", f"Files unreadable for {d}; retrying next pass")
                else:
                    self.log.info("SKIP", f"History seeding skipped for {d}", level="verbose")
                return

        # CASE A: Identical
        if L == C == H:
            return

        # CASE B: Local changed
        if L is not None and H is not None and L != H and C == H:
            self.log.push(f"{colored('Local changed', Fore.GREEN)}, pushing for {d}", d, level="verbose")
            await self.push_to_icloud(rel_path)
            return

        # CASE C: iCloud changed
        if C is not None and H is not None and C != H and L == H:
            self.log.pull(f"{colored('iCloud changed', Fore.CYAN)}, restoring for {d}", d, level="verbose")
            await self.restore_from_icloud(rel_path)
            return

        # CASE D: Both changed (rare)
        self.log.warn("CONFLICT", f"Both changed, stabilizing {d} {cfg.stabilize_wait}s", level="important")
        await asyncio.sleep(cfg.stabilize_wait)

        L2 = await self.hasher.get_cached_hash(local, 'L', rel_path, force=True) if safe_exists(local) else None
        C2 = await self.hasher.get_cached_hash(icloud, 'C', rel_path, force=True) if safe_exists(icloud) else None

        if L2 is not None and C2 is not None and L2 == C2:
            await self.io.copy_to_disk(local, history)
            self.log.info("RESOLVED", f"Both stabilized to same content for {d}", level="verbose")
            return

        if L2 is not None and L2 != L:
            self.log.warn("CONFLICT", f"{colored('Local still changing', Fore.YELLOW)}, choose local: {d}", level="important")
            await self.io.create_conflict_duplicate(icloud)
            await self.push_to_icloud(rel_path)
            return

        if C2 is not None and C2 != C:
            self.log.warn("CONFLICT", f"{colored('iCloud still changing', Fore.YELLOW)}, choose iCloud: {d}", level="important")
            await self.io.create_conflict_duplicate(local)
            await self.restore_from_icloud(rel_path)
            return

        if (L2 is not None and C2 is not None and L2 == L and C2 == C and L2 != C2):
            self.log.warn("CONFLICT", f"{colored('Both stabilized but still differ', Fore.YELLOW)}, resolving by fallback rules: {d}", level="important")

        if not safe_exists(local):
            self.log.pull(f"{colored('Local vanished', Fore.YELLOW)}, restoring from iCloud: {d}", d, level="verbose", warning=True)
            await self.restore_from_icloud(rel_path)
            return

        if not safe_exists(icloud):
            self.log.push(f"{colored('iCloud vanished', Fore.YELLOW)}, pushing local: {d}", d, level="verbose", warning=True)
            await self.push_to_icloud(rel_path)
            return

        # Fallback: mtime comparison
        local_m, icloud_m = safe_mtime(local), safe_mtime(icloud)
        if local_m >= icloud_m:
            self.log.info("CONFLICT", f"{colored('Local is newer', Fore.YELLOW)}, push local: {d}", level="important")
            await self.io.create_conflict_duplicate(icloud)
            await self.push_to_icloud(rel_path)
        else:
            self.log.info("CONFLICT", f"{colored('iCloud is newer', Fore.YELLOW)}, pull iCloud: {d}", level="important")
            await self.io.create_conflict_duplicate(local)
            await self.restore_from_icloud(rel_path)

    # Concurrency wrapper
    async def sync_wrapper(self, rel: str):
        """
        Runs one file sync behind the shared I/O semaphore and logs failures.
        """
        try:
            async with self.io_semaphore:
                await self.sync_file(rel)
        except Exception as e:
            self.log.error("ERROR", f"Error syncing {self.config.disp(rel)}: {e}")
            self.log.error("TRACEBACK", traceback.format_exc())
