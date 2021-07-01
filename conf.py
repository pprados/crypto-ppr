# Wait MIN_RECONNECT_WAIT before reconnect to the binance API
import os

MIN_RECONNECT_WAIT = 10

# Combien de temps avant d'injecter un nouveau bot ?
SLIPPING_TIME = 1

# Every STREAM_MSG_TIMEOUT try to polling (to manage missing events)
STREAM_MSG_TIMEOUT = 0.1 # FIXME

# Debug flag
NO_SAVE:bool = os.environ.get("NO_SAVE", "false").lower() == "true"
EMPTY_PENDING:bool = os.environ.get("EMPTY_PENDING", "false").lower() == "true"
