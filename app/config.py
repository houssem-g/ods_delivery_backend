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
    # text = human-readable lines; json = one JSON object per line (Loki / cloud).
    LOG_FORMAT: Literal["text", "json"] = "text"
    # Build identifier (image tag / git sha), reported to Sentry.
    APP_RELEASE: str | None = None

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
        default_factory=lambda: [
            "http://localhost:5190",
            "http://127.0.0.1:5190",
            "http://localhost:5191",
            "http://127.0.0.1:5191",
        ]
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
    # getOfferRank per courier: the price sheet asks while he edits (debounced) + his offers list
    RATE_LIMIT_OFFER_RANK: str = "60/minute"
    # getNetworkPulse: anonymous callers per IP (Welcome screen), signed-in callers per user
    RATE_LIMIT_PULSE_ANONYMOUS: str = "30/minute"
    RATE_LIMIT_PULSE: str = "60/minute"
    # getDemandPulse per courier
    RATE_LIMIT_DEMAND_PULSE: str = "30/minute"
    # translateOrderMessage per user (cached answers count too)
    RATE_LIMIT_TRANSLATE: str = "40/minute"

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
    # "virtual" on DO Spaces (bucket in the host, what the app CSP allows); "path" for MinIO.
    S3_SIGNING_ADDRESSING_STYLE: Literal["path", "virtual"] = "path"
    S3_REGION: str = "us-east-1"
    S3_ACCESS_KEY: str = "odsdlv-local"
    S3_SECRET_KEY: str = "odsdlv-local-secret"
    S3_BUCKET: str = "ods-delivery"
    # Base URL of anonymously readable objects (public/ prefix); default = path-style on the public endpoint.
    S3_PUBLIC_BASE_URL: str | None = None
    # Canned ACL of public/ objects. Empty = the bucket policy makes them readable (MinIO locally);
    # "public-read" on DO Spaces, whose buckets stay private and serve public/ objects per ACL.
    S3_PUBLIC_OBJECT_ACL: str | None = None
    S3_TIMEOUT_SECONDS: float = 15.0
    UPLOAD_MAX_BYTES: int = 10 * 1024 * 1024
    SIGNED_URL_DEFAULT_SECONDS: int = 300
    SIGNED_URL_MAX_SECONDS: int = 3600

    # --- push -----------------------------------------------------------------
    PUSH_PROVIDER: Literal["auto", "fcm", "log"] = "auto"
    FIREBASE_CREDENTIALS_PATH: str | None = None
    PUSH_TIMEOUT_SECONDS: float = 10.0

    # --- WhatsApp (Meta Cloud API) / SMS (WinSMS) ------------------------------
    # OFF unless the secrets exist (docs of ods-delivery: WHATSAPP_SMS_SETUP_FR.md).
    WHATSAPP_TOKEN: str | None = None
    WHATSAPP_PHONE_NUMBER_ID: str | None = None
    WHATSAPP_APP_SECRET: str | None = None  # webhook X-Hub-Signature-256 (POSTs refused without it)
    WHATSAPP_VERIFY_TOKEN: str | None = None  # webhook GET challenge (refused without it)
    WHATSAPP_API_BASE: str = "https://graph.facebook.com"
    WHATSAPP_API_VERSION: str = "v21.0"
    WHATSAPP_TEMPLATE_LANG: str = "fr"
    WHATSAPP_FALLBACK_SECONDS: int = 60
    WHATSAPP_TPL_NO_RESPONSE: str | None = None
    WHATSAPP_TPL_ON_THE_WAY: str | None = None
    WHATSAPP_TPL_NEW_OFFER: str | None = None
    WHATSAPP_TPL_VERIFICATION: str | None = None
    WINSMS_API_KEY: str | None = None
    WINSMS_SENDER: str | None = None
    WINSMS_API_URL: str = "https://www.winsmspro.com/sms/sms/api"
    MESSAGING_DISABLED: bool = False  # kill switch: nothing leaves (rows say "disabled")
    MESSAGING_HTTP_TIMEOUT_SECONDS: float = 8.0
    MSG_LIMIT_PER_NUMBER_10MIN: int = 4
    MSG_LIMIT_PER_NUMBER_DAY: int = 12
    MSG_LIMIT_GLOBAL_MINUTE: int = 30
    MSG_LIMIT_GLOBAL_HOUR: int = 400

    # --- chat translation (DigitalOcean Serverless Inference, OpenAI-compatible) -------
    # OFF while TRANSLATE_API_KEY is empty: translateOrderMessage answers available: false.
    TRANSLATE_API_URL: str = "https://inference.do-ai.run/v1/chat/completions"
    TRANSLATE_API_KEY: str | None = None
    TRANSLATE_MODEL: str = "gemma-4-31B-it"
    # USD per million tokens (input / output), to count the month's spend
    TRANSLATE_PRICE_IN_PER_M: float = 0.18
    TRANSLATE_PRICE_OUT_PER_M: float = 0.50
    # no new call once the month's (UTC) spend reaches it; cached translations still answer
    TRANSLATE_MONTHLY_BUDGET_USD: float = 3.0
    TRANSLATE_TIMEOUT_SECONDS: float = 6.0

    # --- realtime / jobs ------------------------------------------------------
    REALTIME_ENABLED: bool = True
    REALTIME_CHANNEL: str = "delivery_events"
    SCHEDULER_ENABLED: bool = True
    SCHEDULER_LOCK_KEY: int = 5_318_008_110
    SCHEDULER_LEADER_RETRY_SECONDS: int = 30
    CRON_SECRET: str | None = None

    # --- observability ----------------------------------------------------------
    # GET /api/metrics (Prometheus) answers 404 while this is empty; callers send it in X-Metrics-Token.
    METRICS_TOKEN: str | None = None
    # Sentry: empty DSN = off. No PII is sent (app/observability/sentry.py).
    SENTRY_DSN: str | None = None
    SENTRY_ENVIRONMENT: str | None = None
    SENTRY_TRACES_SAMPLE_RATE: float = 0.0

    # --- maps (OpenStreetMap services) ------------------------------------------
    # Identifies us to Nominatim / Overpass (their usage policies require a real contact).
    OSM_USER_AGENT: str = "ODS-Delivery-API/0.1 (local development)"
    NOMINATIM_ENABLED: bool = True
    NOMINATIM_URL: str = "https://nominatim.openstreetmap.org"
    NOMINATIM_TIMEOUT_SECONDS: float = 5.0
    # Public Nominatim allows 1 request/s per application; a call that would wait longer
    # than NOMINATIM_MAX_WAIT_SECONDS falls back to the places index instead.
    NOMINATIM_MIN_INTERVAL_SECONDS: float = 1.0
    NOMINATIM_MAX_WAIT_SECONDS: float = 2.0
    GEOCODE_CACHE_DAYS: int = 30
    GEOCODE_MISS_CACHE_HOURS: int = 24
    OVERPASS_URLS: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "https://overpass-api.de/api/interpreter",
            "https://z.overpass-api.de/api/interpreter",
            "https://overpass.kumi.systems/api/interpreter",
        ]
    )
    OVERPASS_TIMEOUT_SECONDS: float = 90.0
    # Multiplies the polite pauses between Overpass calls (0 in tests).
    OVERPASS_DELAY_SCALE: float = 1.0
    # Daily 03:00 OSM refresh job (one category per weekday); a manual run works either way.
    OSM_REFRESH_ENABLED: bool = False

    # --- orders -----------------------------------------------------------------
    # Accounts the QA suites run with: they alone see QA orders ("QA TEST" / "PW-" in the
    # items) in the open-order list (src/lib/orderUtils.js isVisibleOpenOrder).
    QA_ACCOUNTS: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["lemelec346@sixoplus.com", "vovine2891@sepole.com", "test.admin@ods.tn"]
    )
    # Route ETA (getOrderETA); empty = straight-line fallback only.
    OSRM_URL: str = "https://router.project-osrm.org"
    OSRM_TIMEOUT_SECONDS: float = 4.0

    @model_validator(mode="before")
    @classmethod
    def _split_origins(cls, data: dict) -> dict:
        for name in ("CORS_ORIGINS", "OVERPASS_URLS"):
            raw = data.get(name) if isinstance(data, dict) else None
            if isinstance(raw, str):
                data[name] = [o.strip() for o in raw.split(",") if o.strip()]
        qa = data.get("QA_ACCOUNTS") if isinstance(data, dict) else None
        if isinstance(qa, str):
            data["QA_ACCOUNTS"] = [e.strip().lower() for e in qa.split(",") if e.strip()]
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
    def whatsapp_enabled(self) -> bool:
        return bool(self.WHATSAPP_TOKEN and self.WHATSAPP_PHONE_NUMBER_ID) and not self.MESSAGING_DISABLED

    @property
    def sms_enabled(self) -> bool:
        return bool(self.WINSMS_API_KEY and self.WINSMS_SENDER) and not self.MESSAGING_DISABLED

    @property
    def translate_enabled(self) -> bool:
        return bool(self.TRANSLATE_API_KEY and self.TRANSLATE_API_URL and self.TRANSLATE_MODEL)

    @property
    def public_files_base_url(self) -> str:
        if self.S3_PUBLIC_BASE_URL:
            return self.S3_PUBLIC_BASE_URL.rstrip("/")
        return f"{self.S3_PUBLIC_ENDPOINT_URL.rstrip('/')}/{self.S3_BUCKET}"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
