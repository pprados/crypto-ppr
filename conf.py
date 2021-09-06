# Wait MIN_RECONNECT_WAIT before reconnect to the binance API
import os

# TODO: voir https://www.starlette.io/config/
# Debug flag
SAVE: bool = os.environ.get("SAVE", "true").lower().strip() == "true"
TELEGRAM: bool = os.environ.get("TELEGRAM", "true").lower().strip() == "true"
EMPTY_PENDING: bool = os.environ.get("EMPTY_PENDING", "false").lower().strip() == "true"
# Active une production d'exception aléatoire, obligeant à reprendre depuis le début
CHECK_RESILIENCE: int = int(os.environ.get("CHECK_RESILIENCE", "0").strip())


# Combien de temps avant d'injecter un nouveau bot ?
SLIPPING_TIME = 1

MIN_RECONNECT_WAIT = 60 if not CHECK_RESILIENCE else 0
MAX_RECONNECTS = 50 # 100
MAX_RECONNECT_SECONDS = 30
KEEPALIVE_TIMEOUT = 60

# Every STREAM_MSG_TIMEOUT try to polling (to manage missing events)
STREAM_MSG_TIMEOUT = 0.1  # FIXME
