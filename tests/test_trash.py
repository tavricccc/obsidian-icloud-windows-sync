import os
import pytest
from datetime import datetime, timedelta
from unittest.mock import patch
from conftest import DiskIO, FileHasher, FileSynchronizer, SyncConfig
from unittest.mock import MagicMock


def _write(path: str, content: str = "content"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


@pytest.fixture
def dio(cfg, mock_log):
    with patch("platform.system", return_value="Windows"):
        return DiskIO(cfg, mock_log)


@pytest.fixture
def eng(cfg, mock_log, dio):
    return FileSynchronizer(cfg, mock_log, FileHasher(cfg, mock_log), dio, MagicMock())


def _trashed(cfg, label, rel):
    return os.path.join(cfg.trash_path, datetime.now().strftime("%Y-%m-%d"), label, rel)

#  trash_file

class TestTrashFile:
    @pytest.mark.asyncio
    async def test_moves_file_keeping_relative_path(self, dio, cfg):
        src = _write(os.path.join(cfg.local_vault, "Notes", "a.md"), "keep me")
        await dio.trash_file(src, "local")
        assert not os.path.exists(src)
        with open(_trashed(cfg, "local", os.path.join("Notes", "a.md")), encoding="utf-8") as f:
            assert f.read() == "keep me"

    @pytest.mark.asyncio
    async def test_prunes_empty_parent_but_not_root(self, dio, cfg):
        src = _write(os.path.join(cfg.local_vault, "Sub", "a.md"))
        await dio.trash_file(src, "local")
        assert not os.path.exists(os.path.join(cfg.local_vault, "Sub"))
        assert os.path.isdir(cfg.local_vault)

    @pytest.mark.asyncio
    async def test_same_name_twice_keeps_both(self, dio, cfg):
        for text in ("first", "second"):
            await dio.trash_file(_write(os.path.join(cfg.local_vault, "a.md"), text), "local")
        day = os.path.join(cfg.trash_path, datetime.now().strftime("%Y-%m-%d"), "local")
        assert len(os.listdir(day)) == 2

    @pytest.mark.asyncio
    async def test_missing_file_is_noop(self, dio, cfg, mock_log):
        await dio.trash_file(os.path.join(cfg.local_vault, "ghost.md"), "local")
        mock_log.error.assert_not_called()

    def test_custom_trash_dir(self, cfg, tmp_path):
        cfg.trash_dir = str(tmp_path / "elsewhere")
        assert cfg.trash_path == str(tmp_path / "elsewhere")

#  purge_old_trash

class TestPurgeOldTrash:
    def test_removes_only_expired_day_folders(self, dio, cfg):
        cfg.trash_retention_days = 7
        old = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
        new = datetime.now().strftime("%Y-%m-%d")
        for name in (old, new, "not-a-date"):
            os.makedirs(os.path.join(cfg.trash_path, name))
        assert dio.purge_old_trash() == 1
        assert sorted(os.listdir(cfg.trash_path)) == sorted([new, "not-a-date"])

    def test_zero_retention_keeps_everything(self, dio, cfg):
        cfg.trash_retention_days = 0
        old = (datetime.now() - timedelta(days=400)).strftime("%Y-%m-%d")
        os.makedirs(os.path.join(cfg.trash_path, old))
        assert dio.purge_old_trash() == 0

#  sync deletes go to trash

class TestSyncDeletesToTrash:
    @pytest.fixture(autouse=True)
    def _populated_vaults(self, cfg):
        for root in (cfg.local_vault, cfg.icloud_vault, cfg.history_dir):
            _write(os.path.join(root, "anchor.md"), "anchor")

    @pytest.mark.asyncio
    async def test_local_delete_trashes_icloud_copy(self, eng, cfg):
        for root in (cfg.icloud_vault, cfg.history_dir):
            _write(os.path.join(root, "gone.md"), "synced text")
        await eng.sync_file("gone.md")
        assert not os.path.exists(os.path.join(cfg.icloud_vault, "gone.md"))
        assert os.path.exists(_trashed(cfg, "icloud", "gone.md"))

    @pytest.mark.asyncio
    async def test_icloud_delete_trashes_local_copy(self, eng, cfg):
        for root in (cfg.local_vault, cfg.history_dir):
            _write(os.path.join(root, "gone.md"), "synced text")
        await eng.sync_file("gone.md")
        assert not os.path.exists(os.path.join(cfg.local_vault, "gone.md"))
        assert os.path.exists(_trashed(cfg, "local", "gone.md"))

#  delete rate limit

class TestDeleteRateLimit:
    def test_blocks_after_limit(self, eng, cfg, mock_log):
        cfg.max_deletes_per_minute = 3
        assert [eng.allow_delete() for _ in range(5)] == [True, True, True, False, False]
        mock_log.error.assert_called_once()

    def test_zero_disables_limit(self, eng, cfg):
        cfg.max_deletes_per_minute = 0
        assert all(eng.allow_delete() for _ in range(500))

    def test_window_expires(self, eng, cfg):
        cfg.max_deletes_per_minute = 1
        with patch("obsidian_sync.sync_worker.time.monotonic", side_effect=[0.0, 1.0, 61.5]):
            assert eng.allow_delete() is True
            assert eng.allow_delete() is False
            assert eng.allow_delete() is True

    @pytest.mark.asyncio
    async def test_held_back_delete_keeps_file(self, eng, cfg):
        cfg.max_deletes_per_minute = 1
        for root in (cfg.local_vault, cfg.icloud_vault, cfg.history_dir):
            _write(os.path.join(root, "anchor.md"), "anchor")
        for rel in ("a.md", "b.md"):
            for root in (cfg.local_vault, cfg.history_dir):
                _write(os.path.join(root, rel), "synced text")
        await eng.sync_file("a.md")
        await eng.sync_file("b.md")
        assert not os.path.exists(os.path.join(cfg.local_vault, "a.md"))
        assert os.path.exists(os.path.join(cfg.local_vault, "b.md"))

#  config

class TestTrashConfig:
    def test_trash_inside_vault_is_critical(self, cfg):
        cfg.trash_dir = os.path.join(cfg.local_vault, "trash")
        codes = [e[0] for e in cfg.validate()]
        assert "trash_dir_inside_sync_root" in codes

    def test_negative_delete_limit_is_critical(self, cfg):
        cfg.max_deletes_per_minute = -1
        assert any(e[0] == "invalid_value" for e in cfg.validate())

    def test_yaml_loads_new_fields(self, tmp_path):
        p = tmp_path / "c.yaml"
        p.write_text(
            "paths:\n  trash_dir: 'D:/Trash'\nsync:\n  max_deletes_per_minute: 7\n  trash_retention_days: 3\n",
            encoding="utf-8",
        )
        c = SyncConfig.from_yaml(str(p))
        assert (c.trash_dir, c.max_deletes_per_minute, c.trash_retention_days) == ("D:/Trash", 7, 3)
