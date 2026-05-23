"""Tests for safe file operations and path confinement."""

import sys
from pathlib import Path

import pytest

from neoagent.core.tools import fileops as fileops_mod
from neoagent.core.tools.fileops import _resolve_safe


class TestResolveSafe:
    def test_allows_workspace_relative_path(self, tmp_path, monkeypatch):
        class FakeCfg:
            office_dir = tmp_path

        monkeypatch.setattr(fileops_mod, "get_config", lambda: FakeCfg())
        result = _resolve_safe("subdir/file.txt")
        assert result == (tmp_path / "subdir/file.txt").resolve()

    def test_blocks_path_traversal(self, tmp_path, monkeypatch):
        class FakeCfg:
            office_dir = tmp_path

        monkeypatch.setattr(fileops_mod, "get_config", lambda: FakeCfg())
        with pytest.raises(PermissionError):
            _resolve_safe("../../../etc/passwd")

    def test_blocks_absolute_path_outside_workspace(self, tmp_path, monkeypatch):
        class FakeCfg:
            office_dir = tmp_path

        monkeypatch.setattr(fileops_mod, "get_config", lambda: FakeCfg())
        # Use a path that is clearly outside the temp workspace
        outside = Path("C:/windows/system32") if sys.platform == "win32" else Path("/etc")
        with pytest.raises(PermissionError):
            _resolve_safe(str(outside))
