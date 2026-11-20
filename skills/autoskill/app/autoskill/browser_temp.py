"""Shorten Linux Chromium socket addresses while keeping browser files in the chosen TMPDIR."""
from __future__ import annotations

import errno
import hashlib
import os
import stat
import sys
import uuid
from contextlib import suppress
from pathlib import Path

SHORT_ROOT = Path("/tmp")
SOCKET = "org.chromium.Chromium.XXXXXX/SingletonSocket"


def location(identity: str) -> Path | None:
    if sys.platform != "linux":
        return None
    key = hashlib.sha256(os.fsencode(identity)).hexdigest()[:24]
    return SHORT_ROOT / f"as-chrome-{os.getuid()}-{key}"


def _private(folder: Path) -> bool:
    info = folder.lstat()
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and info.st_mode & 0o077 == 0


def release(folder: Path | None) -> None:
    """Remove only our link and empty private directory, never the user's temporary files."""
    if folder is None:
        return
    try:
        link = folder / "t"
        if _private(folder) and link.is_symlink():
            link.unlink()
            folder.rmdir()
    except FileNotFoundError:
        pass
    except OSError as error:
        if error.errno != errno.ENOTEMPTY:
            raise


def prepare(environment: dict, identity: str | None = None) -> Path | None:
    selected = environment.get("TMPDIR")
    if sys.platform != "linux" or not selected:
        return None
    original = Path(selected).absolute()
    # Linux sockaddr_un.sun_path has 108 bytes including the terminating NUL.
    if len(os.fsencode(original / SOCKET)) < 108:
        return None
    folder = location(identity or uuid.uuid4().hex)
    link = folder / "t"
    created = False
    try:
        try:
            folder.mkdir(mode=0o700)
            created = True
        except FileExistsError:
            if not _private(folder):
                raise PermissionError(f"临时连接目录不属于当前用户或不是私有目录：{folder}") from None
        try:
            link.symlink_to(original, target_is_directory=True)
        except FileExistsError:
            if not link.is_symlink() or link.readlink() != original:
                raise ValueError(f"临时连接与本次 TMPDIR 不匹配：{link}") from None
        environment["TMPDIR"] = str(link)
        return folder
    except (OSError, ValueError) as error:
        if created:
            # A concurrent command can already be using the same recorder address.
            with suppress(OSError):
                folder.rmdir()
        raise ValueError(f"Linux 浏览器的 TMPDIR 超过 Unix 套接字路径上限，无法创建短连接。请设置较短的可写 TMPDIR。{error}") from error
