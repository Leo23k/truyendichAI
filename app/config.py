from pathlib import Path

import truststore
from pydantic_settings import BaseSettings, SettingsConfigDict


truststore.inject_into_ssl()


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    app_env: str = "development"
    database_url: str = "sqlite:///./data/app.db"
    model_name: str = "ngocdang83/HachimiMT-60-QT"
    device: str = "auto"
    audio_dir: str = "./data/audio"
    crawler_timeout: int = 20
    model_service_url: str = "http://127.0.0.1:7861"
    model_service_timeout: int = 300
    model_beam_size: int = 2

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    def ensure_directories(self) -> None:
        (PROJECT_ROOT / "data").mkdir(parents=True, exist_ok=True)
        self.audio_path.mkdir(parents=True, exist_ok=True)

    @property
    def audio_path(self) -> Path:
        path = Path(self.audio_dir)
        return path if path.is_absolute() else PROJECT_ROOT / path


settings = Settings()
