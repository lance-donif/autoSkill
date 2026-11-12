"""Compatibility entry point: `python scripts/qa.py <command> <project>`.

The implementation lives in app/autoskill/core/store.py.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from autoskill.core import store as _store
from autoskill.core.store import *  # noqa: F403

# Tests and older callers reach module-level helpers that `import *` skips.
globals().update({name: getattr(_store, name) for name in dir(_store) if not name.startswith("__")})

if __name__ == "__main__":
    try:
        sys.exit(_store.main())
    except (ValueError, KeyError, OSError, ImportError, TypeError) as error:
        print("错误: " + str(error), file=sys.stderr)
        sys.exit(1)
