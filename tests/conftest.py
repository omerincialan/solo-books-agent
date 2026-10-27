import os
import sys
import tempfile

# Settings for all tests, before solobooks.config is imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.update(
    DB_PATH=os.path.join(tempfile.mkdtemp(), "test.db"), PHOTOS_DIR=tempfile.mkdtemp(),
    TWILIO_AUTH_TOKEN="tok", PUBLIC_URL="http://example.com/sms", OWNER_PHONE_NUMBER="+15550001111",
    OWNER_NAME="Dana", TRADE="tree_service", MODEL_API_KEY="test", MILEAGE_RATE="0.70",
)
