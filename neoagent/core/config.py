"""Centralized configuration management with Pydantic models.

Fixes CyberClaw's scattered os.getenv calls and hardcoded paths.
All config flows through typed models with validation.
"""

import os
from functools import lru_cache
from pathlib import Path

from ._compat import BaseSettings, Field, SettingsConfigDict


def _default_workspace() -> Path:
    return Path(os.getenv("NEOAGENT_WORKSPACE", Path.cwd() / "workspace"))


def _default_skill_repo() -> str:
    env_repo = os.getenv("NEOAGENT_SKILL_REPO", "")
    if env_repo:
        return env_repo
    # Auto-detect a sibling skill repository (e.g., ../skill)
    project_root = Path(__file__).resolve().parent.parent.parent
    sibling = project_root.parent / "skill"
    if sibling.is_dir():
        return str(sibling)
    return ""


class LLMConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)

    # Deepseek-first defaults — override with env vars to switch providers
    default_provider: str = Field(default="deepseek", alias="DEFAULT_PROVIDER")
    default_model: str = Field(default="deepseek-v4-pro", alias="DEFAULT_MODEL")
    temperature: float = Field(default=0.0, alias="TEMPERATURE")
    max_tokens: int = Field(default=4096, alias="MAX_TOKENS")

    # Router — cheap model should stay within the same provider family
    router_enabled: bool = Field(default=True, alias="ROUTER_ENABLED")
    cheap_model: str = Field(default="deepseek-chat", alias="CHEAP_MODEL")
    powerful_model: str = Field(default="deepseek-v4-pro", alias="POWERFUL_MODEL")


class BudgetConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)

    max_input_tokens: int = Field(default=100_000, alias="BUDGET_MAX_INPUT_TOKENS")
    max_output_tokens: int = Field(default=50_000, alias="BUDGET_MAX_OUTPUT_TOKENS")
    max_cost_usd: float = Field(default=5.00, alias="BUDGET_MAX_COST_USD")


class MemoryConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)

    short_term_turns: int = Field(default=20, alias="MEMORY_SHORT_TERM_TURNS")
    long_term_enabled: bool = Field(default=True, alias="MEMORY_LONG_TERM_ENABLED")
    consolidation_interval: int = Field(default=10, alias="MEMORY_CONSOLIDATION_INTERVAL")


class SandboxConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)

    mode: str = Field(default="docker", alias="SANDBOX_MODE")
    timeout_seconds: int = Field(default=30, alias="SANDBOX_TIMEOUT_SECONDS")
    max_memory_mb: int = Field(default=256, alias="SANDBOX_MAX_MEMORY_MB")
    network_enabled: bool = Field(default=False, alias="SANDBOX_NETWORK_ENABLED")


class ServerConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)

    host: str = Field(default="0.0.0.0", alias="SERVER_HOST")
    port: int = Field(default=8000, alias="SERVER_PORT")
    workers: int = Field(default=4, alias="SERVER_WORKERS")
    cors_origins: str = Field(default="*", alias="SERVER_CORS_ORIGINS")
    api_key: str | None = Field(default=None, alias="API_KEY")


class DevOpsIntegrationConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)

    skill_repo_dir: str = Field(default_factory=_default_skill_repo, alias="NEOAGENT_SKILL_REPO")
    auto_inject_skills: bool = Field(default=True, alias="NEOAGENT_AUTO_INJECT_SKILLS")
    max_skill_context_chars: int = Field(default=12_000, alias="NEOAGENT_MAX_SKILL_CONTEXT_CHARS")


class Config(BaseSettings):
    """Root configuration aggregating all sub-configs."""

    model_config = SettingsConfigDict(env_prefix="NEOAGENT_", case_sensitive=False)

    llm: LLMConfig = Field(default_factory=LLMConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    devops: DevOpsIntegrationConfig = Field(default_factory=DevOpsIntegrationConfig)

    # Runtime paths (not from env, computed once)
    workspace_dir: Path = Field(default_factory=_default_workspace)

    @property
    def office_dir(self) -> Path:
        return self.workspace_dir / "office"

    @property
    def skills_dir(self) -> Path:
        return self.office_dir / "skills"

    @property
    def memory_dir(self) -> Path:
        return self.workspace_dir / "memory"

    @property
    def db_path(self) -> str:
        return str(self.workspace_dir / "state.sqlite3")

    @property
    def tasks_path(self) -> str:
        return str(self.workspace_dir / "tasks.json")

    @property
    def chroma_path(self) -> str:
        return str(self.workspace_dir / "chroma")

    @property
    def logs_dir(self) -> Path:
        return self.workspace_dir / "logs"

    def ensure_dirs(self) -> None:
        """Create all required workspace directories."""
        dirs = [
            self.workspace_dir,
            self.office_dir,
            self.skills_dir,
            self.memory_dir,
            self.logs_dir,
        ]
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)

    def api_key_for(self, provider: str) -> str | None:
        """Look up API key for a given provider from environment."""
        mapping = {
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "google": "GOOGLE_API_KEY",
            "deepseek": "DEEPSEEK_API_KEY",
            "groq": "GROQ_API_KEY",
            "tavily": "TAVILY_API_KEY",
        }
        env_var = mapping.get(provider.lower(), f"{provider.upper()}_API_KEY")
        return os.getenv(env_var)


@lru_cache(maxsize=1)
def get_config() -> Config:
    """Singleton config accessor — cached after first call."""
    cfg = Config()
    cfg.ensure_dirs()
    return cfg


def reset_config() -> None:
    """Clear the cached Config singleton (useful for runtime reload and tests)."""
    get_config.cache_clear()
