import os
import pytest
from unittest.mock import call, patch, MagicMock
from conftest import DuplicateScanner

@pytest.fixture
def scanner(cfg, mock_log, mock_disk_io):
    return DuplicateScanner(cfg, mock_log, mock_disk_io)

#  No Duplicates

class TestNoDuplicates:
    def test_logs_clean_when_no_duplicates(self, scanner, cfg):
        (cfg.local_vault_path if hasattr(cfg, 'local_vault_path') else None)
        with patch("builtins.input", return_value="n"):
            scanner.scan_and_clean()
        scanner.log.success.assert_called()

    def test_skips_missing_vaults(self, scanner, cfg):
        cfg.icloud_vault = "/nonexistent/path"
        with patch("builtins.input", return_value="n"):
            scanner.scan_and_clean()

    def test_skips_none_vault(self, scanner, cfg):
        cfg.history_dir = None
        with patch("builtins.input", return_value="n"):
            scanner.scan_and_clean()

#  Pattern Detection

class TestPatternDetection:
    def _create(self, base_dir, name):
        p = os.path.join(base_dir, name)
        with open(p, "w"):
            pass
        return p

    def test_detects_conflict_file(self, scanner, cfg):
        self._create(cfg.local_vault, "note_CONFLICT_20260101_120000_123456.md")
        found = []
        with patch("builtins.input", return_value="n") as _:
            scanner.scan_and_clean()
        scanner.log.warn.assert_called()

    def test_detects_icloud_duplicate(self, scanner, cfg):
        self._create(cfg.local_vault, "My Note (1).md")
        with patch("builtins.input", return_value="n"):
            scanner.scan_and_clean()
        scanner.log.warn.assert_called()

    def test_detects_tmp_file(self, scanner, cfg):
        self._create(cfg.local_vault, "stale.tmp")
        with patch("builtins.input", return_value="n"):
            scanner.scan_and_clean()
        scanner.log.warn.assert_called()

    def test_ignores_trash_directory(self, scanner, cfg):
        trash = os.path.join(cfg.local_vault, ".trash"); os.makedirs(trash)
        self._create(trash, "deleted (1).md")
        scanner.log.warn.reset_mock()
        scanner.log.error.reset_mock()
        with patch("builtins.input", return_value="n"):
            scanner.scan_and_clean()
        scanner.log.error.assert_not_called()
        for call in scanner.log.warn.call_args_list:
            assert "deleted (1).md" not in str(call)

    def test_scans_icloud_vault(self, scanner, cfg):
        self._create(cfg.icloud_vault, "file_CONFLICT_20260101_120000_000001.md")
        with patch("builtins.input", return_value="n"):
            scanner.scan_and_clean()
        scanner.log.warn.assert_called()

    def test_scans_history_dir(self, scanner, cfg):
        self._create(cfg.history_dir, "archive (2).md")
        with patch("builtins.input", return_value="n"):
            scanner.scan_and_clean()
        scanner.log.warn.assert_called()

#  User Interaction

class TestUserInteraction:
    def _create_dup(self, cfg):
        p = os.path.join(cfg.local_vault, "note_CONFLICT_20260101_120000_000001.md")
        open(p, "w").close()
        return p

    def test_user_yes_triggers_deletion(self, scanner, cfg):
        p = self._create_dup(cfg)
        with patch("builtins.input", return_value="y"):
            scanner.scan_and_clean()
        scanner.io.trash_file_sync.assert_called()

    def test_user_YES_uppercase_triggers_deletion(self, scanner, cfg):
        self._create_dup(cfg)
        with patch("builtins.input", return_value="YES"):
            scanner.scan_and_clean()
        scanner.io.trash_file_sync.assert_called()

    def test_user_no_skips_deletion(self, scanner, cfg):
        self._create_dup(cfg)
        with patch("builtins.input", return_value="n"):
            scanner.scan_and_clean()
        scanner.io.trash_file_sync.assert_not_called()

    def test_user_empty_skips_deletion(self, scanner, cfg):
        self._create_dup(cfg)
        with patch("builtins.input", return_value=""):
            scanner.scan_and_clean()
        scanner.io.trash_file_sync.assert_not_called()

    def test_all_removed_logs_success(self, scanner, cfg):
        p = self._create_dup(cfg)
        scanner.io.trash_file_sync.side_effect = lambda path, label: os.remove(path)
        with patch("builtins.input", return_value="y"):
            scanner.scan_and_clean()
        scanner.log.success.assert_called()

    def test_partial_failure_logs_error(self, scanner, cfg):
        self._create_dup(cfg)
        scanner.io.trash_file_sync.return_value = None
        with patch("builtins.input", return_value="y"):
            scanner.scan_and_clean()
        scanner.log.error.assert_called()

    def test_no_console_skips_without_crashing(self, scanner, cfg):
        self._create_dup(cfg)
        with patch("builtins.input", side_effect=EOFError):
            scanner.scan_and_clean()
        scanner.io.trash_file_sync.assert_not_called()

    def test_count_in_error_message(self, scanner, cfg):
        for i in range(3):
            p = os.path.join(cfg.local_vault, f"note_CONFLICT_20260101_12000{i}_00000{i}.md")
            open(p, "w").close()
        with patch("builtins.input", return_value="n"):
            scanner.scan_and_clean()
        err_args = scanner.log.error.call_args_list[0][0]
        assert "3" in str(err_args)
