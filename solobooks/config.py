"""All settings come from environment variables (or a .env file)."""
import os
from dotenv import load_dotenv

load_dotenv()

# Meta Model API (OpenAI-compatible)
META_BASE_URL = os.getenv("META_BASE_URL", "https://api.meta.ai/v1")
MODEL_API_KEY = os.getenv("MODEL_API_KEY", "")
MUSE_MODEL = os.getenv("MUSE_MODEL", "muse-spark-1.3")

# Twilio
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "")
PUBLIC_URL = os.getenv("PUBLIC_URL", "")  # exact webhook URL set in Twilio, e.g. http://1.2.3.4/sms

# The owner of the books: only this number can use the assistant
OWNER_PHONE_NUMBER = os.getenv("OWNER_PHONE_NUMBER", "")
OWNER_NAME = os.getenv("OWNER_NAME", "the owner")
TRADE = os.getenv("TRADE", "tree_service")
TIMEZONE = os.getenv("TIMEZONE", "America/Los_Angeles")

# Google Sheet
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "google_service_account.json")

# IRS standard mileage rate, dollars per mile (2025: 0.70). Update each January.
MILEAGE_RATE = float(os.getenv("MILEAGE_RATE", "0.70"))

DB_PATH = os.getenv("DB_PATH", "solobooks.db")
PHOTOS_DIR = os.getenv("PHOTOS_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "photos"))
