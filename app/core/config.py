from pathlib import Path
from typing import List, Literal
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Project root (the folder containing app/), so paths don't depend on the current working directory.
BASE_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Application configuration loaded from environment variables and .env file."""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Application
    APP_NAME: str = "JEV Catalog Ingestion POC"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False

    # JEV API Configuration (TypeSafe AI System One: POST {JEV_API_BASE_URL}/systemone)
    JEV_API_KEY: str = ""
    JEV_API_BASE_URL: str = "https://api.typesafe.ai/v1"
    JEV_MODEL: str = "jev-latest"
    JEV_MODE: Literal["auto", "live", "mock"] = "auto"  # auto = live with labelled-mock fallback
    JEV_TIMEOUT_SECONDS: float = 10.0
    JEV_MAX_RETRIES: int = 2
    JEV_SAVE_EVIDENCE: bool = True

    # Database Configuration (Defaults to local SQLite, easily overridden with PostgreSQL)
    DATABASE_URL: str = "sqlite:///catalog.db"

    # Business Rules & Confidence Thresholds
    CONFIDENCE_THRESHOLD: float = 0.80
    SUPPORTED_CURRENCIES: str = "USD,EUR,GBP,INR,CAD,AUD,JPY,CHF,SGD,CNY"

    # Project Folders
    DATA_DIR: Path = BASE_DIR / "data"
    OUTPUT_DIR: Path = BASE_DIR / "output"

    @field_validator("DATABASE_URL")
    @classmethod
    def anchor_sqlite_path(cls, url: str) -> str:
        """Resolve a relative SQLite file path against the project root instead of the current directory."""
        prefix = "sqlite:///"
        if url.startswith(prefix):
            db_path = url[len(prefix):]
            if db_path and db_path != ":memory:" and not Path(db_path).is_absolute():
                return f"{prefix}{(BASE_DIR / db_path).as_posix()}"
        return url

    @property
    def supported_currency_list(self) -> List[str]:
        return [c.strip().upper() for c in self.SUPPORTED_CURRENCIES.split(",") if c.strip()]

    @property
    def is_jev_live(self) -> bool:
        """True when ingestion will attempt a live JEV call (a key is set and mock mode is off)."""
        return self.JEV_MODE != "mock" and bool(self.JEV_API_KEY.strip())


settings = Settings()
