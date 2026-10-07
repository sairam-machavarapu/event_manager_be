from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://gather:gather_local@localhost:5432/gather"
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6380/0"
    auth_cookie_secure: bool = False
    auth_origin: str = "http://localhost:3000"
    cashfree_enabled: bool = False
    cashfree_client_id: str = ""
    cashfree_client_secret: str = ""
    cashfree_api_version: str = "2026-01-01"
    cashfree_notify_url: str = ""
    email_provider: Literal["smtp", "brevo"] = "smtp"
    brevo_api_key: str = ""
    demo_jobs_enabled: bool = False
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = False
    mail_from: str = "Gather <noreply@gather.local>"
    storage_endpoint: str = "http://localhost:9000"
    storage_region: str = "us-east-1"
    storage_access_key: str = "gather_local"
    storage_secret_key: str = "gather_local_storage_secret"
    storage_bucket: str = "gather-media"
    media_public_url: str = "http://localhost:9000/gather-media"


@lru_cache
def get_settings() -> Settings:
    return Settings()
