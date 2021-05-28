import json
from decimal import Decimal
from typing import Any


def _serialize(obj):
    """JSON serializer for objects not serializable by default json code"""

    if isinstance(obj, Decimal):
        return float(obj)

    return obj.__dict__


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, indent=2, default=_serialize)

