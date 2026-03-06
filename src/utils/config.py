from dotenv import load_dotenv
load_dotenv()

import os
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    GOOGLE_CLOUD_PROJECT: str
    GEMINI_API_KEY: str = ""
    VERTEX_LOCATION: str = "us-central1"
    SPREADSHEET_ID: str
    TELEGRAM_BOT_TOKEN: str
    PORT: int = 8080
    # GCP service account key — picked up automatically by all GCP SDKs
    GOOGLE_APPLICATION_CREDENTIALS: str = ""

    # Alternative: Raw JSON content (for 3rd party clouds where file upload is hard)
    GCP_CREDENTIALS_JSON: str = ""

    class Config:
        env_file = ".env"


settings = Settings()

# If we have raw JSON content but no file path, write it to a file
if not settings.GOOGLE_APPLICATION_CREDENTIALS and settings.GCP_CREDENTIALS_JSON:
    creds_path = "gcp-credentials.json"
    with open(creds_path, "w") as f:
        f.write(settings.GCP_CREDENTIALS_JSON)
    # Point the SDK to this newly created file
    settings.GOOGLE_APPLICATION_CREDENTIALS = os.path.abspath(creds_path)

# Explicitly set for any GCP SDK that reads it before our imports run
if settings.GOOGLE_APPLICATION_CREDENTIALS:
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = settings.GOOGLE_APPLICATION_CREDENTIALS
