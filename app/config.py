import os
from pathlib import Path

import truststore
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


truststore.inject_into_ssl()


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    app_env: str = "development"
    database_url: str = "sqlite:///./data/app.db"
    database_path: str | None = Field(default=None, validation_alias=AliasChoices("DATABASE_PATH"))
    model_name: str = "ngocdang83/HachimiMT-60-QT"
    device: str = "auto"
    audio_dir: str = "./data/audio"
    crawler_timeout: int = 20
    model_service_url: str = Field(
        default="http://127.0.0.1:7861",
        validation_alias=AliasChoices("TRANSLATOR_API_URL", "MODEL_SERVICE_URL"),
    )
    tts_api_url: str | None = Field(default=None, validation_alias=AliasChoices("TTS_API_URL"))
    model_service_timeout: int = 300
    model_beam_size: int = 2

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    def ensure_directories(self) -> None:
        self.data_path.mkdir(parents=True, exist_ok=True)
        self.audio_path.mkdir(parents=True, exist_ok=True)

    @property
    def data_path(self) -> Path:
        if os.getenv("VERCEL"):
            return Path("/tmp/leotruyen-data")
        return PROJECT_ROOT / "data"

    @property
    def audio_path(self) -> Path:
        path = Path(self.audio_dir)
        if path.is_absolute():
            return path
        if os.getenv("VERCEL"):
            return self.data_path / "audio"
        return PROJECT_ROOT / path


settings = Settings()
