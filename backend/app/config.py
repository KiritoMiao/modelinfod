from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Upstream OpenAI-compatible endpoint that serves /v1/models.
    # Accepts ".../v1", ".../v1/models" or a bare base URL.
    upstream_base_url: str = "http://localhost:4000"
    upstream_api_key: str = ""

    # How often to re-fetch the upstream model list (seconds, 0 disables).
    sync_interval_seconds: int = 300

    # /v1/model/info validates each caller's API key against the upstream
    # /v1/models and only returns the models that key can access. Successful
    # per-key lookups are cached for this long (seconds, 0 = check upstream
    # on every request).
    user_models_cache_ttl_seconds: int = 60

    # LiteLLM community price/metadata map refresh.
    prices_refresh_url: str = (
        "https://raw.githubusercontent.com/BerriAI/litellm/main/"
        "model_prices_and_context_window.json"
    )
    prices_refresh_interval_seconds: int = 86400  # 0 disables

    # OpenRouter public catalog, used as a secondary metadata source for
    # models the LiteLLM map doesn't cover. No API key needed.
    openrouter_models_url: str = "https://openrouter.ai/api/v1/models"
    openrouter_refresh_interval_seconds: int = 86400  # 0 disables

    # models.dev catalog — the primary metadata source. Covers cache pricing,
    # context-tier pricing and the priority ("fast") service tier, none of
    # which the other two carry reliably. No API key needed.
    modelsdev_url: str = "https://models.dev/api.json"
    modelsdev_refresh_interval_seconds: int = 86400  # 0 disables

    # Where mutable state (custom models, caches) is persisted.
    data_dir: str = "/data"

    # Directory of per-model override JSON files (one file per model, shareable
    # between deployments). Empty = "<data_dir>/overrides". Files dropped in or
    # edited externally are picked up automatically.
    overrides_dir: str = ""

    @property
    def overrides_path(self) -> str:
        return self.overrides_dir or f"{self.data_dir.rstrip('/')}/overrides"

    # Optional bearer token for the admin API. Empty = no auth (the fronting
    # nginx/cloudflared layer is expected to gate /modelinfod).
    admin_token: str = ""

    host: str = "0.0.0.0"
    port: int = 8080

    @property
    def upstream_models_url(self) -> str:
        base = self.upstream_base_url.rstrip("/")
        if base.endswith("/v1/models"):
            return base
        if base.endswith("/v1"):
            return f"{base}/models"
        return f"{base}/v1/models"


@lru_cache
def get_settings() -> Settings:
    return Settings()
