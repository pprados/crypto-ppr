import json
from decimal import Decimal
from json import JSONDecodeError
from pathlib import Path
from random import randint
from typing import Any, Tuple


def _serialize(obj):
    """JSON serializer for objects not serializable by default json code"""

    if isinstance(obj, Decimal):
        return float(obj)

    return obj.__dict__


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, indent=2, default=_serialize)


def atomic_save_json(obj: Any, filename: Path) -> None:
    new_filename = filename.parent / (filename.name + ".new")
    old_filename = filename.parent / (filename.name + ".old")
    with open(new_filename, "w") as f:
        json.dump(obj, f, default=_serialize)
    if filename.exists():
        filename.rename(old_filename)
    new_filename.rename(filename)
    old_filename.unlink(missing_ok=True)


def atomic_load_json(filename: Path) -> Tuple[Any, bool]:
    rollback = False
    new_filename = filename.parent / (filename.name + ".new")
    old_filename = filename.parent / (filename.name + ".old")
    if new_filename.exists():
        # Try to load the new filename
        try:
            with open(new_filename) as f:
                obj = json.load(f)
            filename.unlink(missing_ok=True)
            old_filename.unlink(missing_ok=True)
            new_filename.rename(filename)
            return obj, False
        except JSONDecodeError as e:
            # new filename is dirty
            new_filename.unlink()
            if old_filename.exists():
                old_filename.rename(filename)
            rollback = True
    with open(filename) as f:
        return json.load(f), rollback

def generate_order_id(agent_name:str):
    return agent_name + "-" + str(randint(100000,999999))