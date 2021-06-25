"""
Horloge partagée pouvant être simulée.
"""

from datetime import datetime, timezone

import global_flags

_now = 0
def set_now(ts:int) -> None:
    global _now
    _now = ts

def get_now() -> int:
    """ En milliseconde"""
    global _now
    if not global_flags.simulation or not _now:
        return datetime.now(timezone.utc).timestamp()*1000
    else:
        return _now

def ts_to_str(timestamp: int) -> str:
    return datetime.utcfromtimestamp(timestamp / 1000).strftime('%Y-%m-%d %H:%M:%S')

def sleep_speed() -> int:
    if not global_flags.simulation or not _now:
        return 1
    else:
        return 0