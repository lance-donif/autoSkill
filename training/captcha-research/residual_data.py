"""Provide pure product-compatible image preparation for frozen residual research."""
import hashlib
import io
import json

import numpy as np
from PIL import Image

ORIGINAL_META_SHA256 = '2641a73630a7de539669e622f0fc7b6ddb34f5b970134a091b68021ffee11346'


def frozen_metadata(root):
    """Load the original charset and shape without following a moving worktree."""
    file = root / 'baselines/original-be9dd198/captcha-ctc.json'
    content = file.read_bytes()
    if hashlib.sha256(content).hexdigest() != ORIGINAL_META_SHA256:
        raise ValueError('The immutable original metadata snapshot changed')
    return json.loads(content)


def prepare(png, height, max_width):
    """Match captcha.solve alpha composition, BGR scaling, resizing and padding."""
    image = Image.open(io.BytesIO(png)).convert('RGBA')
    flat = Image.new('RGBA', image.size, 'white')
    flat.alpha_composite(image)
    width = max(1, min(max_width, -(-image.width * height // image.height)))
    pixels = np.asarray(flat.convert('RGB').resize((width, height), Image.BILINEAR), dtype=np.float32)
    tensor = np.zeros((1, 3, height, max_width), dtype=np.float32)
    tensor[0, :, :, :width] = (pixels[:, :, ::-1] / 127.5 - 1).transpose(2, 0, 1)
    return tensor


def decode_one(frames, charset):
    """Match the product's single-image greedy blank/repeat CTC collapse."""
    text, previous = [], 0
    for index in frames[0].argmax(axis=1):
        if index and index != previous:
            text.append(charset[int(index)])
        previous = int(index)
    return ''.join(text)
