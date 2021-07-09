import os

from dotenv import load_dotenv

load_dotenv()

BINANCE_API_KEY = os.environ["BINANCE_API_KEY"]
BINANCE_API_SECRET = os.environ["BINANCE_API_SECRET"]
BINANCE_TEST_NET = os.environ.get("BINANCE_API_TEST", "false").lower() == "true"

USER = os.environ["BOT_USER"]
PASSWORD = os.environ["BOT_PASSWORD"]

TELEGRAM_API_ID=os.environ["TELEGRAM_API_ID"]
TELEGRAM_API_HASH=os.environ["TELEGRAM_API_HASH"]
TELEGRAM_PHONE=os.environ["TELEGRAM_PHONE"]
