from dotenv import load_dotenv
load_dotenv()

import os
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    GOOGLE_CLOUD_PROJECT: str
    GEMINI_API_KEY: str
    SPREADSHEET_ID: str
    TELEGRAM_BOT_TOKEN: str
    PORT: int = 8080
    # GCP service account key — picked up automatically by all GCP SDKs
    GOOGLE_APPLICATION_CREDENTIALS: str = ""

    class Config:
        env_file = ".env"


settings = Settings()

# Explicitly set for any GCP SDK that reads it before our imports run
if settings.GOOGLE_APPLICATION_CREDENTIALS:
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = settings.GOOGLE_APPLICATION_CREDENTIALS
