import os
from decimal import Decimal
from json import JSONDecodeError
from pathlib import Path
from typing import Any, Optional, Tuple

import jstyleson as json

from conf import NO_SAVE
from tools import str_d


def _serialize(obj):
    """JSON serializer for objects not serializable by default json code"""

    if isinstance(obj, Decimal):
        return float(str_d(obj))

    # Detecte les generators pour ne pas les sauvers
    if hasattr(obj,'ag_frame'):
        return '_generator'
    return obj.__dict__


def atomic_save_json(obj: Any, filename: Path,comment:Optional[str]=None) -> None:
    if NO_SAVE:
        return
    new_filename = filename.parent / (filename.name + ".new")
    old_filename = filename.parent / (filename.name + ".old")
    with open(new_filename, "w") as f:
        if comment:
            f.write("// "+comment+"\n")
        json.dump(obj, f,
                  default=_serialize,
                  skipkeys=True,
                  indent=2)
    os.sync()
    if filename.exists():
        filename.rename(old_filename)
    new_filename.rename(filename)
    old_filename.unlink(missing_ok=True)
    os.sync()


def atomic_load_json(filename: Path) -> Tuple[Any, bool]:
    rollback = False
    new_filename = filename.parent / (filename.name + ".new")
    old_filename = filename.parent / (filename.name + ".old")
    if new_filename.exists():
        # Try to load the new filename
        try:
            with open(new_filename) as f:
                obj = json.load(
                    f,
                    parse_float=Decimal,
                )  # Try to parse
            filename.unlink(missing_ok=True)
            old_filename.unlink(missing_ok=True)
            new_filename.rename(filename)
            os.sync()
            return obj, False
        except JSONDecodeError as e:
            # new filename is dirty
            new_filename.unlink()
            if old_filename.exists():
                old_filename.rename(filename)
            os.sync()
            rollback = True
    if old_filename.exists():
        # Cela a crashé lors d'un JSONDecodeError, pendant qu'on resoud l'état.
        filename.unlink(missing_ok=True)
        old_filename.rename(filename)
        os.sync()
        rollback = True

    if filename.exists():
        with open(filename) as f:
            return json.load(f,
                             parse_float=Decimal
                             ), rollback
    else:
        return None,False

