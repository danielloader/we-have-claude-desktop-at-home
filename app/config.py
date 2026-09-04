from typing import Literal

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class SeedUser(BaseModel):
    username: str
    password: str
    is_admin: bool = False
    token_budget: int | None = None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="APP_", extra="ignore")

    llm_provider: Literal["stub", "anthropic", "azure", "ollama"] = "stub"
    chat_store: Literal["sqlite", "elastic"] = "sqlite"

    sqlite_path: str = "./data/app.db"

    elastic_url: str = "http://localhost:9200"
    elastic_api_key: str | None = None
    elastic_index_prefix: str = "chat"

    jwt_secret: str = "dev-only-change-me-before-you-deploy-anywhere"
    jwt_ttl_minutes: int = 480

    # Created on first start when the user table is empty. Override with a JSON list in
    # APP_SEED_USERS, or set it to [] to disable seeding.
    seed_users: list[SeedUser] = [
        SeedUser(username="admin", password="admin", is_admin=True),
        SeedUser(username="alice", password="alice"),
        SeedUser(username="bob", password="bob"),
    ]

    # Token budgets: total (input + output) tokens a user may consume per period.
    # Per-user overrides live on the user record; None there means "use the default".
    default_token_budget: int = 500_000
    budget_period: Literal["day", "month", "all"] = "month"
    enforce_token_budget: bool = True

    # Telemetry: "file" appends one line per request/span/metric to a tailable file.
    telemetry: Literal["none", "file", "elastic_apm"] = "file"
    telemetry_file: str = "./data/telemetry.log"
    telemetry_file_format: Literal["text", "jsonl"] = "text"
    apm_server_url: str = "http://localhost:8200"
    apm_secret_token: str | None = None
    apm_api_key: str | None = None
    apm_service_name: str = "claude-at-home"
    apm_environment: str = "dev"

    system_prompt: str = "You are a helpful assistant. Be concise."
    llm_model: str = "claude-opus-5"
    llm_max_tokens: int = 64000

    # Azure AI: Claude on Microsoft Foundry. `resource` is the Foundry resource name;
    # the API key can also come from ANTHROPIC_FOUNDRY_API_KEY.
    azure_resource: str | None = None
    azure_api_key: str | None = None

    # Ollama: a local CPU-friendly model. APP_LLM_MODEL names the Ollama model, e.g. qwen3:0.6b.
    ollama_url: str = "http://localhost:11434"
    ollama_think: bool = True

    stub_lag_min_s: float = 0.8
    stub_lag_max_s: float = 3.0
    stub_tokens_per_s_min: float = 15.0
    stub_tokens_per_s_max: float = 60.0
    stub_stall_probability: float = 0.03
