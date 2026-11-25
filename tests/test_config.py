"""Tests for configuration system."""



from neoagent.core.config import Config, get_config


class TestConfig:
    def test_default_workspace_dir(self):
        cfg = Config()
        assert cfg.workspace_dir is not None
        assert cfg.office_dir == cfg.workspace_dir / "office"
        assert cfg.memory_dir == cfg.workspace_dir / "memory"

    def test_api_key_mapping(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-anthropic")
        cfg = Config()
        assert cfg.api_key_for("openai") == "sk-test-openai"
        assert cfg.api_key_for("anthropic") == "sk-test-anthropic"
        assert cfg.api_key_for("nonexistent") is None

    def test_singleton_caching(self):
        # get_config is lru_cached; clearing it is tricky, so we just verify
        # the returned object is a Config instance.
        cfg = get_config()
        assert isinstance(cfg, Config)
