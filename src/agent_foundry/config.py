from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FOUNDRY_", env_file=".env", extra="ignore")

    database_url: SecretStr = SecretStr("postgresql://foundry@localhost:55439/foundry")
    api_key: SecretStr = SecretStr("")
    admin_key: SecretStr = SecretStr("")
    job_encryption_key: SecretStr = SecretStr("")
    prompt_hash_key: SecretStr = SecretStr("")
    llm_api_key: SecretStr = SecretStr("")
    llm_base_url: str = "https://api.openai.com/v1"
    main_model: str = ""
    router_model: str = ""
    evaluator_model: str = ""
    builder_model: str = ""
    llm_timeout: float = Field(60, gt=0, le=600)
    main_max_tokens: int = Field(2048, ge=64, le=32000)
    router_max_tokens: int = Field(700, ge=64, le=4000)
    evaluator_max_tokens: int = Field(900, ge=64, le=4000)
    builder_max_tokens: int = Field(10000, ge=1000, le=64000)
    search_top_k: int = Field(5, ge=1, le=10)
    search_min_score: float = Field(0.08, ge=0, le=1)
    direct_threshold: float = Field(0.94, ge=0, le=1)
    direct_margin: float = Field(0.08, ge=0, le=1)
    router_threshold: float = Field(0.8, ge=0, le=1)
    router_context_chars: int = Field(24000, ge=2000, le=100000)
    duplicate_threshold: float = Field(0.82, ge=0, le=1)
    max_plan_steps: int = Field(5, ge=1, le=10)
    max_prompt_chars: int = Field(12000, ge=100, le=50000)
    execution_timeout: float = Field(30, gt=0, le=300)
    memory_mb: int = Field(256, ge=64, le=4096)
    cpu: float = Field(1, gt=0, le=8)
    max_output_bytes: int = Field(1_000_000, ge=1024, le=10_000_000)
    executor_concurrency: int = Field(4, ge=1, le=32)
    builder_retry_count: int = Field(2, ge=0, le=5)
    job_max_attempts: int = Field(3, ge=1, le=10)
    job_lease_seconds: int = Field(120, ge=30, le=3600)
    queue_poll_seconds: float = Field(1, ge=0.1, le=60)
    evaluation_delay_seconds: float = Field(2, ge=0, le=60)
    builder_enabled: bool = True
    creation_threshold: float = Field(0.65, ge=0, le=1)
    evaluation_weights: dict[str, float] = {
        "reuse_score": 0.20,
        "determinism_score": 0.20,
        "token_saving_score": 0.20,
        "latency_saving_score": 0.15,
        "accuracy_gain_score": 0.15,
        "specialized_data_score": 0.10,
    }
    maintenance_weight: float = Field(0.2, ge=0, le=1)
    expected_reuses: int = Field(20, ge=1)
    minimum_cost_ratio: float = Field(1.5, ge=1)
    build_cost_units: float = Field(50000, gt=0)
    tool_repository: str = "https://github.com/qmdch1/agent-tools.git"
    tool_repository_root: Path = Path("../agent-tools")
    state_root: Path = Path(".state")
    git_branch: str = "main"
    git_push: bool = True
    git_author_name: str = "Agent Foundry Builder"
    git_author_email: str = "agent-foundry@localhost"
    docker_binary: str = "docker"
    sandbox_image: str = "agent-foundry-sandbox:0.1.0"
    build_timeout: float = Field(300, ge=30, le=1800)
    approved_dependencies: dict[str, str] = {}  # package==version -> SHA256 wheel digest
    http_allowed_hosts: list[str] = []
    file_root: Path = Path(".state/files")
    database_queries: dict[str, str] = {}  # admin-owned query_id -> parameterized SQL
    query_database_secret: str = "FOUNDRY_QUERY_DATABASE_URL"
    db_pool_max: int = Field(8, ge=2, le=32)

    @model_validator(mode="after")
    def validate_weights(self):
        expected = {
            "reuse_score",
            "determinism_score",
            "token_saving_score",
            "latency_saving_score",
            "accuracy_gain_score",
            "specialized_data_score",
        }
        if set(self.evaluation_weights) != expected or any(v < 0 for v in self.evaluation_weights.values()):
            raise ValueError("Evaluation weights must contain exactly the six nonnegative benefit factors")
        if sum(self.evaluation_weights.values()) <= 0:
            raise ValueError("At least one evaluation weight must be positive")
        if self.api_key.get_secret_value() and self.api_key == self.admin_key:
            raise ValueError("User and administrator API keys must differ")
        return self
