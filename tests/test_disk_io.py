import os
import asyncio
import pytest
from unittest.mock import patch, MagicMock, AsyncMock, call
from conftest import DiskIO, safe_exists, size_or_zero, safe_mtime, ensure_dir

#  Safe Exists

class TestSafeExists:
    def test_returns_true_for_existing_file(self, tmp_path):
        f = tmp_path / "a.txt"; f.write_text("x")
        assert safe_exists(str(f)) is True

    def test_returns_false_for_missing(self, tmp_path):
        assert safe_exists(str(tmp_path / "ghost.txt")) is False

    def test_returns_false_on_exception(self):
        with patch("os.path.exists", side_effect=PermissionError):
            assert safe_exists("/some/path") is False

#  Size Or Zero

class TestSizeOrZero:
    def test_returns_file_size(self, tmp_path):
        f = tmp_path / "a.txt"; f.write_bytes(b"hello")
        assert size_or_zero(str(f)) == 5

    def test_returns_zero_for_missing(self, tmp_path):
        assert size_or_zero(str(tmp_path / "ghost.txt")) == 0

    def test_returns_zero_on_exception(self):
        with patch("os.path.getsize", side_effect=OSError):
            assert size_or_zero("/some/path") == 0

#  Safe Mtime

class TestSafeMtime:
    def test_returns_mtime(self, tmp_path):
        f = tmp_path / "a.txt"; f.write_text("x")
        assert safe_mtime(str(f)) > 0

    def test_returns_zero_for_missing(self, tmp_path):
        assert safe_mtime(str(tmp_path / "ghost.txt")) == 0

    def test_returns_zero_on_exception(self):
        with patch("os.path.getmtime", side_effect=OSError):
            assert safe_mtime("/path") == 0

#  Ensure Dir

class TestEnsureDir:
    def test_creates_missing_directory(self, tmp_path):
        d = tmp_path / "new" / "nested"
        ensure_dir(str(d))
        assert d.exists()

    def test_no_error_if_already_exists(self, tmp_path):
        d = tmp_path / "existing"; d.mkdir()
        ensure_dir(str(d))

#  DiskIO

class TestDiskIOInit:
    def test_requires_windows_platform(self, cfg, mock_log):
        with patch("platform.system", return_value="Linux"):
            with pytest.raises(RuntimeError, match="Windows"):
                DiskIO(cfg, mock_log)

    def test_accepts_windows_platform(self, cfg, mock_log):
        with patch("platform.system", return_value="Windows"):
            dio = DiskIO(cfg, mock_log)
            assert dio is not None

#  Async Copy

class TestAsyncCopy:
    @pytest.mark.asyncio
    async def test_successful_copy(self, cfg, tmp_path, mock_log):
        src = tmp_path / "src.md"; src.write_text("content")
        dst = tmp_path / "dst.md"
        with patch("platform.system", return_value="Windows"):
            dio = DiskIO(cfg, mock_log)
        await dio.async_copy(str(src), str(dst))
        assert dst.exists()
        assert dst.read_text() == "content"

    @pytest.mark.asyncio
    async def test_creates_parent_dirs(self, cfg, tmp_path, mock_log):
        src = tmp_path / "src.md"; src.write_text("x")
        dst = tmp_path / "deep" / "path" / "dst.md"
        with patch("platform.system", return_value="Windows"):
            dio = DiskIO(cfg, mock_log)
        await dio.async_copy(str(src), str(dst))
        assert dst.exists()

    @pytest.mark.asyncio
    async def test_retries_on_permission_error(self, cfg, tmp_path, mock_log):
        src = tmp_path / "src.md"; src.write_text("x")
        dst = tmp_path / "dst.md"
        call_count = [0]
        orig_replace = os.replace

        def flaky_replace(s, d):
            call_count[0] += 1
            if call_count[0] < 3:
                raise PermissionError("locked")
            orig_replace(s, d)

        with patch("platform.system", return_value="Windows"):
            dio = DiskIO(cfg, mock_log)
        with patch("os.replace", side_effect=flaky_replace):
            await dio.async_copy(str(src), str(dst))
        assert call_count[0] == 3

    @pytest.mark.asyncio
    async def test_cleans_tmp_on_write_failure(self, cfg, tmp_path, mock_log):
        src = tmp_path / "src.md"; src.write_text("x")
        dst = tmp_path / "dst.md"
        with patch("platform.system", return_value="Windows"):
            dio = DiskIO(cfg, mock_log)
        with patch("obsidian_sync.disk_io.shutil.copy2", side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                await dio.async_copy(str(src), str(dst))
        tmp_file = str(dst) + ".tmp"
        assert not os.path.exists(tmp_file)

    @pytest.mark.asyncio
    async def test_copy_to_icloud_stages_outside_icloud_dir(self, cfg, tmp_path, mock_log):
        src = tmp_path / "src.md"; src.write_text("content")
        dst = tmp_path / "icloud" / "note.md"
        cfg.icloud_vault = str(tmp_path / "icloud")
        cfg.logs_dir = str(tmp_path / "logs")
        cfg.check_icloud_status = False
        with patch("platform.system", return_value="Windows"):
            dio = DiskIO(cfg, mock_log)

        with patch.object(dio, "_replace_with_retries") as replace_mock:
            await dio.copy_to_icloud(str(src), str(dst))

        assert dst.exists()
        assert dst.read_text() == "content"
        replace_mock.assert_not_called()
        assert not os.path.exists(str(dst) + ".tmp")

    @pytest.mark.asyncio
    async def test_copy_to_icloud_falls_back_to_direct_copy_without_same_drive_staging(self, cfg, tmp_path, mock_log):
        src = tmp_path / "src.md"; src.write_text("content")
        dst = tmp_path / "icloud" / "direct.md"
        cfg.icloud_vault = str(tmp_path / "icloud")
        cfg.logs_dir = "D:\\logs"
        cfg.history_dir = "D:\\history"
        cfg.local_vault = "D:\\local"
        cfg.check_icloud_status = False
        with patch("platform.system", return_value="Windows"):
            dio = DiskIO(cfg, mock_log)

        with patch.object(dio, "_replace_with_retries") as replace_mock:
            await dio.copy_to_icloud(str(src), str(dst))

        assert dst.exists()
        assert dst.read_text() == "content"
        replace_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_copy_to_disk_uses_tmp_file_next_to_destination(self, cfg, tmp_path, mock_log):
        src = tmp_path / "src.md"; src.write_text("data")
        dst = tmp_path / "history" / "note.md"
        with patch("platform.system", return_value="Windows"):
            dio = DiskIO(cfg, mock_log)

        observed = {}
        original_replace = dio._replace_with_retries

        def capture_replace(tmp, final_dst, *args, **kwargs):
            observed["tmp"] = tmp
            observed["dst"] = final_dst
            return original_replace(tmp, final_dst, *args, **kwargs)

        with patch.object(dio, "_replace_with_retries", side_effect=capture_replace):
            await dio.copy_to_disk(str(src), str(dst))

        assert dst.exists()
        assert dst.read_text() == "data"
        assert observed["dst"] == str(dst)
        assert observed["tmp"] == str(dst) + ".tmp"

#  Remove File

class TestRemoveFile:
    @pytest.mark.asyncio
    async def test_removes_existing_file(self, cfg, tmp_path, mock_log):
        f = tmp_path / "del.md"; f.write_text("x")
        with patch("platform.system", return_value="Windows"):
            dio = DiskIO(cfg, mock_log)
        await dio.remove_file(str(f), "test")
        assert not f.exists()

    @pytest.mark.asyncio
    async def test_no_error_if_file_missing(self, cfg, tmp_path, mock_log):
        with patch("platform.system", return_value="Windows"):
            dio = DiskIO(cfg, mock_log)
        await dio.remove_file(str(tmp_path / "ghost.md"), "test")
        mock_log.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_removes_empty_parent_dirs(self, cfg, tmp_path, mock_log):
        sub = tmp_path / "empty_sub"; sub.mkdir()
        f = sub / "note.md"; f.write_text("x")
        cfg.local_vault = str(tmp_path)
        cfg.icloud_vault = str(tmp_path / "ic")
        cfg.history_dir = str(tmp_path / "hi")
        with patch("platform.system", return_value="Windows"):
            dio = DiskIO(cfg, mock_log)
        await dio.remove_file(str(f), "test")
        assert not sub.exists()

    @pytest.mark.asyncio
    async def test_never_removes_root_with_unnormalized_config(self, cfg, tmp_path, mock_log):
        vault = tmp_path / "Vault"; vault.mkdir()
        f = vault / "only.md"; f.write_text("x")
        cfg.local_vault = str(vault).upper() + os.sep  # different case + trailing slash
        with patch("platform.system", return_value="Windows"):
            dio = DiskIO(cfg, mock_log)
        await dio.remove_file(str(f), "test")
        assert vault.exists()
        assert tmp_path.exists()

#  Remove File Sync

class TestRemoveFileSync:
    def test_removes_file(self, cfg, tmp_path, mock_log):
        f = tmp_path / "sync_del.md"; f.write_text("x")
        with patch("platform.system", return_value="Windows"):
            dio = DiskIO(cfg, mock_log)
        dio.remove_file_sync(str(f), "test")
        assert not f.exists()

    def test_no_error_if_missing(self, cfg, tmp_path, mock_log):
        with patch("platform.system", return_value="Windows"):
            dio = DiskIO(cfg, mock_log)
        dio.remove_file_sync(str(tmp_path / "ghost.md"), "test")

#  Create Conflict Duplicate

class TestCreateConflictDuplicate:
    @pytest.mark.asyncio
    async def test_creates_conflict_file(self, cfg, tmp_path, mock_log):
        f = tmp_path / "note.md"; f.write_text("original")
        with patch("platform.system", return_value="Windows"):
            dio = DiskIO(cfg, mock_log)
        await dio.create_conflict_duplicate(str(f))
        conflicts = list(tmp_path.glob("*_CONFLICT_*"))
        assert len(conflicts) == 1
        assert conflicts[0].read_text() == "original"

    @pytest.mark.asyncio
    async def test_conflict_preserves_extension(self, cfg, tmp_path, mock_log):
        f = tmp_path / "note.md"; f.write_text("x")
        with patch("platform.system", return_value="Windows"):
            dio = DiskIO(cfg, mock_log)
        await dio.create_conflict_duplicate(str(f))
        conflicts = list(tmp_path.glob("*_CONFLICT_*.md"))
        assert len(conflicts) == 1

    @pytest.mark.asyncio
    async def test_no_raise_on_copy_failure(self, cfg, tmp_path, mock_log):
        f = tmp_path / "note.md"; f.write_text("x")
        with patch("platform.system", return_value="Windows"):
            dio = DiskIO(cfg, mock_log)
        with patch("shutil.copy2", side_effect=OSError("fail")):
            await dio.create_conflict_duplicate(str(f))
        mock_log.error.assert_called()
