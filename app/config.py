"""Settings, read from environment variables (or a local .env file). No secrets live in code."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    ingest_api_key: str
    admin_api_key: str
    # Vergeo caps the diagnostic window at 2 MB and the description at 4,000 chars. 4 MiB leaves
    # room for that plus multipart overhead, and rejects anything that is clearly not a report.
    max_upload_bytes: int = 4 * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()
