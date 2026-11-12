"""Explicit, portable test microphone input for browser voice workflows.

The browser uses a staged PCM WAV as a fake microphone. This verifies browser
capture/WebRTC transport without opening the machine's real microphone.
"""
from __future__ import annotations


def chrome_args(config: dict, asset_root=None) -> list[str]:
    """Resolve and validate ``media.audio_input_file`` before Chrome launches.

    Kept self-contained so standalone exports can include the identical
    validation and Chrome flags without importing autoSkill.
    """
    from pathlib import Path, PureWindowsPath
    import wave

    media = config.get("media") or {}
    if not isinstance(media, dict):
        raise ValueError("media 必须是对象")
    name = media.get("audio_input_file")
    if name in (None, ""):
        return []
    if (not isinstance(name, str) or "\\" in name or "\x00" in name or name.startswith("/")
            or PureWindowsPath(name).drive or any(part in ("", ".", "..") or ":" in part for part in name.split("/"))
            or "${" in name or "{{" in name):
        raise ValueError("media.audio_input_file 必须是项目内的相对 WAV 路径")
    if asset_root is None:
        raise ValueError("测试麦克风需要项目 asset_root；不能使用当前机器的任意路径")
    root = Path(asset_root).resolve()
    file = (root / name).resolve()
    if not file.is_relative_to(root) or file.suffix.lower() != ".wav" or not file.is_file():
        raise ValueError("测试麦克风 WAV 不存在、格式不对，或通过链接离开了项目目录")
    try:
        with wave.open(str(file), "rb") as audio:
            channels, width, rate, frames, compression, _ = audio.getparams()
            if (compression != "NONE" or channels not in (1, 2) or width != 2 or not 8000 <= rate <= 192000
                    or frames <= 0 or file.stat().st_size < frames * channels * width + 44):
                raise ValueError("测试麦克风需要非空的 16-bit PCM WAV（单声道或双声道，8–192 kHz）")
            if not audio.readframes(min(frames, 4096)):
                raise ValueError("测试麦克风 WAV 没有音频数据")
    except (wave.Error, EOFError) as error:
        raise ValueError("测试麦克风不是有效的 PCM WAV") from error
    return ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
            "--use-file-for-fake-audio-capture=" + str(file)]


def permissions(config: dict) -> list[str]:
    """Grant microphone access only when an explicit test input is configured."""
    media = config.get("media") or {}
    return ["microphone"] if isinstance(media, dict) and media.get("audio_input_file") else []
