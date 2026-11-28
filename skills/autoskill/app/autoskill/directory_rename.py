"""Bind the platform's no-replace directory rename once, on first POSIX use."""
from __future__ import annotations

import ctypes
import sys

library = ctypes.CDLL(None, use_errno=True)
if sys.platform.startswith("linux") and hasattr(library, "renameat2"):
    rename = library.renameat2
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int

    def no_replace(source: bytes, target: bytes) -> int:
        return rename(-100, source, -100, target, 1)
elif sys.platform == "darwin" and hasattr(library, "renamex_np"):
    rename = library.renamex_np
    rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int

    def no_replace(source: bytes, target: bytes) -> int:
        return rename(source, target, 4)
else:
    no_replace = None
