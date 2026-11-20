"""Capture recording screenshots in the selected session and verify their file receipts."""
from __future__ import annotations

import hashlib
import os
import tempfile
import uuid
from pathlib import Path

from .. import data_refs, paths, task_context
from . import agent


def arguments(argv: list[str]) -> tuple[list[str], list[str], str | None]:
    """Separate screenshot options from the optional selector and destination."""
    options, positional, directory = [], [], None
    tokens = iter(argv)
    for token in tokens:
        if not token.startswith("-"):
            positional.append(token)
            continue
        if token in ("--full", "-f", "--annotate", "--if-changed", "--json"):
            options.append(token)
            continue
        flag, separator, value = token.partition("=")
        if flag not in ("--threshold", "--screenshot-quality", "--screenshot-format", "--screenshot-dir"):
            raise ValueError("截图不支持这个选项：" + flag)
        value = value if separator else next(tokens, "")
        if not value or value.startswith("-"):
            raise ValueError("截图选项缺少参数：" + flag)
        if flag == "--screenshot-dir":
            if directory is not None:
                raise ValueError("每次截图只可指定一个输出目录")
            directory = value
        else:
            options.extend([flag, value])
    if len(positional) > 2:
        raise ValueError("截图参数为可选的元素选择器和输出文件路径")
    return options, positional, directory


def target(positional: list[str]) -> tuple[str | None, str | None]:
    """A second positional is always a path. Keep single CSS selectors and refs intact."""
    if len(positional) == 2:
        return positional[0], positional[1]
    if not positional:
        return None, None
    value = positional[0]
    if Path(value).is_absolute() or value.startswith(("./", "../", ".\\", "..\\", "~/", "~\\")):
        return None, value
    if value.startswith(("#", ".", "@", "[", ":")) or "[" in value or "=" in value:
        return value, None
    if "/" in value or "\\" in value or Path(value).suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
        return None, value
    return value, None


def _default_directory() -> Path:
    relative = f".local/recordings/{task_context.session_key()}/screenshots"
    directory = data_refs.resolve(relative)
    if directory.resolve() != directory:
        raise ValueError("当前会话的截图目录指向其他位置")
    return directory


def output_path(value: str, root: Path | None) -> Path:
    """Resolve a child output against the service's own root, independently of its caller."""
    path = Path(value).expanduser()
    file = ((root / path) if root else path).resolve()
    if root is not None and not file.is_relative_to(root):
        raise ValueError(f"通过后台服务执行时，输出位置必须在 autoSkill 数据目录内（{root}）；省略输出位置即可使用默认位置")
    return file


def _inspect(file: Path) -> dict:
    from PIL import Image

    if file.is_symlink() or not file.is_file():
        raise ValueError("浏览器未生成可核验的普通截图文件")
    with file.open("rb") as stream:
        with Image.open(stream, formats=["PNG", "JPEG"]) as picture:
            width, height = picture.size
            media = picture.format.lower()
            picture.verify()
        stream.seek(0)
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        size = os.fstat(stream.fileno()).st_size
    return {"sha256": digest, "size": size, "width": width, "height": height, "format": media}


def _receipt(reply, temporary: Path, directory: Path, requested: Path | None) -> dict:
    if not isinstance(reply, dict):
        raise ValueError("浏览器未返回本次截图文件")
    metadata = {key: reply[key] for key in ("annotations", "changed", "pixelChangeRatio", "revision", "threshold") if key in reply}
    if metadata.get("changed") is False:
        return metadata
    if not isinstance(reply.get("path"), str) or not reply["path"]:
        raise ValueError("浏览器未返回本次截图文件")
    reported = Path(reply["path"])
    file = reported.resolve()
    if reported.is_symlink() or file.parent != temporary:
        raise ValueError("浏览器返回的截图路径与本次请求不符")
    details = _inspect(file)
    suffix = ".png" if details["format"] == "png" else ".jpg"
    destination = requested or directory / ("screenshot-" + uuid.uuid4().hex + suffix)
    file = file.replace(destination)
    result = {**metadata, **details, "path": str(file)}
    if file.is_relative_to(paths.workspace()):
        result["reference"] = data_refs.store(file)
    return result


def capture(session: str, argv: list[str]) -> dict:
    options, positional, requested_dir = arguments(argv)
    bound = os.environ.get("AUTOSKILL_BRIDGE_OUTPUT_ROOT")
    root = Path(bound).resolve() if bound else None
    if root is not None and root != paths.workspace():
        raise ValueError("截图的数据目录与当前后台服务不符")
    selector, requested_path = target(positional)
    requested_file = output_path(requested_path, root) if requested_path is not None else None
    directory = output_path(requested_dir, root) if requested_dir else None
    if requested_file is not None:
        directory = requested_file.parent
    elif directory is None:
        directory = _default_directory()
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".capture-", dir=directory) as staging:
            temporary = Path(staging)
            capture_args = [selector] if selector else []
            if selector or requested_file is not None:
                capture_args.append(str(temporary / (requested_file.name if requested_file is not None else "capture")))
            reply = agent.call(session, ["screenshot", "--screenshot-dir", str(temporary), *options, *capture_args])
            return _receipt(reply, temporary, directory, requested_file)
    except (OSError, SyntaxError) as error:
        raise agent.AgentError(f"截图文件未能保存或核验（{type(error).__name__}）：{error}") from error
