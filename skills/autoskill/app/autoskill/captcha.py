"""Read a text captcha (letters and digits) with the bundled model. Runs locally; nothing is sent anywhere.

The model is PP-OCR based (models/captcha, see its LICENSE and NOTICE). It reads plain character images
only: no arithmetic, sliders or click-the-picture challenges. A misread is expected now and then, so the
runner retries a case with a fresh image instead of reporting a defect.
"""
from __future__ import annotations

import io
import json

from . import paths

FOLDER = paths.SKILL / "models" / "captcha"
_loaded = None


def _model():
    global _loaded
    if _loaded is None:
        try:
            import onnxruntime
        except ImportError:
            raise ValueError("验证码识别需要 onnxruntime、numpy、pillow（见 requirements.txt）；当前运行时里没有，请使用新版完整离线包") from None
        meta = json.loads((FOLDER / "captcha-ctc.json").read_text(encoding="utf-8"))
        options = onnxruntime.SessionOptions()
        options.log_severity_level = 3
        session = onnxruntime.InferenceSession(str(FOLDER / "captcha-ctc.onnx"), options, providers=["CPUExecutionProvider"])
        _loaded = (session, meta["charset"], meta["imageShape"][1], meta["imageShape"][2])
    return _loaded


def solve(png: bytes) -> str:
    """The characters in a captcha image (PNG/JPEG bytes)."""
    import numpy
    from PIL import Image

    session, charset, height, max_width = _model()
    image = Image.open(io.BytesIO(png)).convert("RGBA")
    flat = Image.new("RGBA", image.size, "white")
    flat.alpha_composite(image)
    width = max(1, min(max_width, -(-image.width * height // image.height)))
    pixels = numpy.asarray(flat.convert("RGB").resize((width, height), Image.BILINEAR), dtype=numpy.float32)
    tensor = numpy.zeros((1, 3, height, max_width), dtype=numpy.float32)  # the model was trained with zero padding on the right
    tensor[0, :, :, :width] = (pixels[:, :, ::-1] / 127.5 - 1).transpose(2, 0, 1)  # BGR, scaled to [-1, 1]
    frames = session.run(None, {session.get_inputs()[0].name: tensor})[0][0]
    text, previous = [], 0
    for index in frames.argmax(axis=1):  # CTC: drop blanks (0) and repeats of the same frame class
        if index and index != previous:
            text.append(charset[int(index)])
        previous = int(index)
    if not text:
        raise ValueError("没有从验证码图片里识别出字符；这类验证码（滑块、点选、算式）不在支持范围内")
    return "".join(text)
