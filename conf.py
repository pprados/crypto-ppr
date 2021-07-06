# Wait MIN_RECONNECT_WAIT before reconnect to the binance API
import os

# TODO: voir https://www.starlette.io/config/
# Debug flag
NO_SAVE:bool = os.environ.get("NO_SAVE", "false").lower() == "true"
EMPTY_PENDING:bool = os.environ.get("EMPTY_PENDING", "false").lower() == "true"
# Active une production d'exception aléatoire, obligeant à reprendre depuis le début
CHECK_RESILIENCE: int = int(os.environ.get("CHECK_RESILIENCE", "0"))

MIN_RECONNECT_WAIT = 10 if not CHECK_RESILIENCE else 0

# Combien de temps avant d'injecter un nouveau bot ?
SLIPPING_TIME = 1

# Every STREAM_MSG_TIMEOUT try to polling (to manage missing events)
STREAM_MSG_TIMEOUT = 0.1  # FIXME

