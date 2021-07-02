import os

api_key = os.environ["BINANCE_API_KEY"]
api_secret = os.environ["BINANCE_API_SECRET"]
test_net = os.environ.get("BINANCE_API_TEST", "false").lower() == "true"