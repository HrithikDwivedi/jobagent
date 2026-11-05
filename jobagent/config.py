"""Environment-based configuration. Secrets come only from the environment / .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, does not override variables already set."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass
class AppConfig:
    data_dir: Path
    database_url: str
    secret_key: str
    resume_path: Path | None
    ai_provider: str = "none"
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    cron_secret: str | None = None
    setup_token: str | None = None
    deployment: str = "local"           # local | vercel
    host: str = "127.0.0.1"
    port: int = 8765
    allow_remote: bool = False
    https: bool = False
    session_idle_minutes: int = 30
    session_absolute_hours: int = 8
    run_budget_seconds: int = 270     # serverless time box per run (Vercel maxDuration is 300)
    user_agent: str = "JobAgent/0.1 (personal job search tool)"
    # multi-user sign-up / email
    admin_email: str | None = None      # JOBAGENT_ADMIN_EMAIL: receives sign-up approval requests
    public_url: str | None = None       # JOBAGENT_PUBLIC_URL: base URL used in emailed links
    resend_api_key: str | None = None
    mail_from: str | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    def redacted(self) -> dict:
        """Config safe to store in run records and logs."""
        return {
            "database": self.database_url.split("://")[0] if "://" in self.database_url else "sqlite",
            "resume_path": str(self.resume_path) if self.resume_path else None,
            "ai_provider": self.ai_provider,
            "openai_api_key": "set" if self.openai_api_key else "unset",
            "anthropic_api_key": "set" if self.anthropic_api_key else "unset",
            "host": self.host,
            "port": self.port,
            "https": self.https,
        }


def load_config(env_file: Path | None = None, **overrides) -> AppConfig:
    # JOBAGENT_ENV_FILE lets a local worker use another environment's settings (e.g. pulled Vercel production
    # variables) without touching the local .env.
    extra = os.environ.get("JOBAGENT_ENV_FILE")
    if extra:
        load_dotenv(Path(extra))
    load_dotenv(env_file or PROJECT_ROOT / ".env")
    data_dir = Path(os.environ.get("JOBAGENT_DATA_DIR", PROJECT_ROOT / "data"))
    database_url = (os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
                    or "sqlite:///" + str(Path(os.environ.get("JOBAGENT_DB_PATH", data_dir / "jobagent.db"))))
    secret = os.environ.get("JOBAGENT_SECRET_KEY", "")
    resume = os.environ.get("JOBAGENT_RESUME_PATH")
    cfg = AppConfig(
        data_dir=data_dir,
        database_url=database_url,
        secret_key=secret,
        resume_path=Path(resume) if resume else None,
        ai_provider=os.environ.get("JOBAGENT_AI_PROVIDER", "none").lower(),
        openai_api_key=os.environ.get("OPENAI_API_KEY") or None,
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        cron_secret=os.environ.get("CRON_SECRET") or None,
        setup_token=os.environ.get("JOBAGENT_SETUP_TOKEN") or None,
        deployment="vercel" if os.environ.get("VERCEL") else "local",
        host=os.environ.get("JOBAGENT_HOST", "127.0.0.1"),
        port=int(os.environ.get("JOBAGENT_PORT", "8765")),
        allow_remote=os.environ.get("JOBAGENT_ALLOW_REMOTE", "0") == "1",
        https=os.environ.get("JOBAGENT_HTTPS", "0") == "1" or bool(os.environ.get("VERCEL")),
        session_idle_minutes=int(os.environ.get("JOBAGENT_SESSION_IDLE_MINUTES", "30")),
        session_absolute_hours=int(os.environ.get("JOBAGENT_SESSION_ABSOLUTE_HOURS", "8")),
        run_budget_seconds=max(30, min(780, int(os.environ.get("JOBAGENT_RUN_BUDGET_SECONDS", "270")))),
        admin_email=(os.environ.get("JOBAGENT_ADMIN_EMAIL") or "").strip().lower() or None,
        public_url=_public_url_from_env(),
        resend_api_key=os.environ.get("RESEND_API_KEY") or None,
        mail_from=os.environ.get("JOBAGENT_MAIL_FROM") or None,
        smtp_host=os.environ.get("SMTP_HOST") or None,
        smtp_port=int(os.environ.get("SMTP_PORT") or "587"),
        smtp_user=os.environ.get("SMTP_USER") or None,
        smtp_password=os.environ.get("SMTP_PASSWORD") or None,
        smtp_from=os.environ.get("SMTP_FROM") or None,
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _public_url_from_env() -> str | None:
    url = (os.environ.get("JOBAGENT_PUBLIC_URL") or "").strip().rstrip("/")
    if url:
        return url
    # Vercel system variable (trusted, unlike the request Host header)
    prod = (os.environ.get("VERCEL_PROJECT_PRODUCTION_URL") or "").strip().rstrip("/")
    return f"https://{prod}" if prod and os.environ.get("VERCEL") else None


def acceptable_base_url(url: str | None) -> str | None:
    """Links in emails must be https, except for local development hosts."""
    from urllib.parse import urlsplit
    if not url:
        return None
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if not host or parts.username or parts.password:
        return None
    if parts.scheme == "https" or (parts.scheme == "http" and host in ("localhost", "127.0.0.1", "::1")):
        return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"
    return None


class ConfigError(Exception):
    pass


def validate_config(cfg: AppConfig) -> list[str]:
    """Return a list of problems; empty list means OK."""
    problems = []
    if len(cfg.secret_key) < 32:
        problems.append("JOBAGENT_SECRET_KEY is missing or shorter than 32 characters.")
    if cfg.deployment == "vercel":
        if not cfg.database_url.startswith(("postgres://", "postgresql://")):
            problems.append("On Vercel, DATABASE_URL must point to PostgreSQL (the filesystem is not persistent).")
        if not cfg.cron_secret or len(cfg.cron_secret) < 24:
            problems.append("CRON_SECRET (24+ chars) is required on Vercel to protect scheduled runs.")
    elif cfg.host not in ("127.0.0.1", "localhost", "::1") and not cfg.allow_remote:
        problems.append("Binding to a non-localhost address requires JOBAGENT_ALLOW_REMOTE=1.")
    if cfg.allow_remote and not cfg.https:
        problems.append("Remote access requires HTTPS (JOBAGENT_HTTPS=1 behind a TLS proxy).")
    if cfg.ai_provider not in ("none", "openai", "anthropic"):
        problems.append(f"Unknown JOBAGENT_AI_PROVIDER '{cfg.ai_provider}'.")
    if cfg.ai_provider == "openai" and not cfg.openai_api_key:
        problems.append("JOBAGENT_AI_PROVIDER=openai but OPENAI_API_KEY is not set.")
    if cfg.ai_provider == "anthropic" and not cfg.anthropic_api_key:
        problems.append("JOBAGENT_AI_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set.")
    return problems
