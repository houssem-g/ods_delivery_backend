"""Application settings. Every variable is documented in .env.example.

Defaults are safe for a local run against the docker compose stack; anything
secret defaults to a clearly-local placeholder and is refused outside `local`/`test`.
"""

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

LOCAL_JWT_SECRET = "local-dev-only-jwt-secret-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    ENVIRONMENT: Literal["local", "test", "staging", "production"] = "local"
    LOG_LEVEL: str = "INFO"

    # --- database -----------------------------------------------------------
    DATABASE_URL: str = "postgresql+asyncpg://ods_delivery:ods_delivery_local@localhost:5451/ods_delivery"
    DB_SSLMODE: Literal["disable", "require"] = "disable"
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 5
    DB_POOL_TIMEOUT: int = 30
    DB_MAX_CONNECTIONS: int = 25
    DB_CONNECTION_RESERVE: int = 5
    WEB_CONCURRENCY: int = 1
    DEPLOYMENT_REPLICAS: int = 1
    # NullPool: every session opens its own connection (tests, one-shot scripts).
    DB_NULLPOOL: bool = False

    # --- HTTP -----------------------------------------------------------------
    CORS_ORIGINS: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5190", "http://127.0.0.1:5190"]
    )
    PUBLIC_APP_URL: str = "http://localhost:5190"
    API_PUBLIC_URL: str = "http://localhost:8110"

    # --- auth -----------------------------------------------------------------
    JWT_SECRET: str = LOCAL_JWT_SECRET
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_MINUTES: int = 60
    REFRESH_TOKEN_DAYS: int = 30
    REFRESH_COOKIE_NAME: str = "odsd_refresh"
    REFRESH_COOKIE_PATH: str = "/api/auth"
    COOKIE_SECURE: bool = False
    COOKIE_DOMAIN: str | None = None
    EMAIL_CODE_TTL_MINUTES: int = 10
    EMAIL_CODE_MAX_ATTEMPTS: int = 5
    EMAIL_CODE_RESEND_SECONDS: int = 60
    PASSWORD_MIN_LENGTH: int = 8
    BCRYPT_ROUNDS: int = 12

    GOOGLE_CLIENT_ID: str | None = None
    GOOGLE_CLIENT_SECRET: str | None = None
    GOOGLE_REDIRECT_URI: str = "http://localhost:8110/api/auth/google/callback"

    # --- rate limiting --------------------------------------------------------
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_DEFAULT: str = "300/minute"
    RATE_LIMIT_AUTH: str = "20/minute"
    RATE_LIMIT_AUTH_EMAIL: str = "6/minute"

    # --- e-mail ---------------------------------------------------------------
    EMAIL_PROVIDER: Literal["smtp", "log"] = "smtp"
    EMAIL_FROM: str = "ODS Delivery <no-reply@ods.local>"
    SMTP_HOST: str = "localhost"
    SMTP_PORT: int = 1125
    SMTP_USERNAME: str | None = None
    SMTP_PASSWORD: str | None = None
    SMTP_STARTTLS: bool = False
    SMTP_TLS: bool = False
    SMTP_TIMEOUT_SECONDS: float = 10.0

    # --- object storage (S3 / MinIO / DO Spaces) ------------------------------
    S3_ENDPOINT_URL: str = "http://localhost:9110"
    # Host the browser can reach; presigned URLs are signed for it.
    S3_PUBLIC_ENDPOINT_URL: str = "http://localhost:9110"
    S3_REGION: str = "us-east-1"
    S3_ACCESS_KEY: str = "odsdlv-local"
    S3_SECRET_KEY: str = "odsdlv-local-secret"
    S3_BUCKET: str = "ods-delivery"
    # Base URL of anonymously readable objects (public/ prefix); default = path-style on the public endpoint.
    S3_PUBLIC_BASE_URL: str | None = None
    S3_TIMEOUT_SECONDS: float = 15.0
    UPLOAD_MAX_BYTES: int = 10 * 1024 * 1024
    SIGNED_URL_DEFAULT_SECONDS: int = 300
    SIGNED_URL_MAX_SECONDS: int = 3600

    # --- push -----------------------------------------------------------------
    PUSH_PROVIDER: Literal["auto", "fcm", "log"] = "auto"
    FIREBASE_CREDENTIALS_PATH: str | None = None
    PUSH_TIMEOUT_SECONDS: float = 10.0

    # --- realtime / jobs ------------------------------------------------------
    REALTIME_ENABLED: bool = True
    REALTIME_CHANNEL: str = "delivery_events"
    SCHEDULER_ENABLED: bool = True
    SCHEDULER_LOCK_KEY: int = 5_318_008_110
    SCHEDULER_LEADER_RETRY_SECONDS: int = 30
    CRON_SECRET: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _split_origins(cls, data: dict) -> dict:
        raw = data.get("CORS_ORIGINS") if isinstance(data, dict) else None
        if isinstance(raw, str):
            data["CORS_ORIGINS"] = [o.strip() for o in raw.split(",") if o.strip()]
        return data

    @model_validator(mode="after")
    def _refuse_local_secrets_outside_local(self) -> "Settings":
        if self.ENVIRONMENT in ("staging", "production"):
            if self.JWT_SECRET == LOCAL_JWT_SECRET or len(self.JWT_SECRET) < 32:
                raise ValueError("JWT_SECRET must be set (>= 32 chars) outside local/test")
            if not self.COOKIE_SECURE:
                raise ValueError("COOKIE_SECURE must be true outside local/test")
        return self

    @property
    def is_local(self) -> bool:
        return self.ENVIRONMENT in ("local", "test")

    @property
    def google_enabled(self) -> bool:
        return bool(self.GOOGLE_CLIENT_ID and self.GOOGLE_CLIENT_SECRET)

    @property
    def public_files_base_url(self) -> str:
        if self.S3_PUBLIC_BASE_URL:
            return self.S3_PUBLIC_BASE_URL.rstrip("/")
        return f"{self.S3_PUBLIC_ENDPOINT_URL.rstrip('/')}/{self.S3_BUCKET}"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
