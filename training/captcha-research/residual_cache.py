"""Bind cached teacher arrays to tensor selection and image preparation."""
import hashlib
import inspect

import numpy as np
from PIL import __version__ as pillow_version
from residual_data import ORIGINAL_META_SHA256, prepare


def tensor_names(protocol):
    """Use one tensor mapping for extraction, persistence and consumption."""
    return {'features': protocol['feature_tensor'], 'logits': protocol['baseline_logits_tensor'],
            'ctc': protocol['ctc_logits_tensor'], 'local': protocol['local_tensor'],
            'expanded': protocol['expanded_tensor']}


def cache_contract(protocol):
    """Invalidate cached pixels when their actual preprocessing implementation changes."""
    return {'version': 1, 'tensors': tensor_names(protocol),
            'preprocessing': {'function_sha256': hashlib.sha256(inspect.getsource(prepare).encode()).hexdigest(),
                              'metadata_sha256': ORIGINAL_META_SHA256, 'image_shape': [3, 48, 320],
                              'numpy_version': np.__version__, 'pillow_version': pillow_version}}


def require_cache_contract(metadata, protocol):
    """Never silently attest a legacy cache to an unrecorded preprocessing contract."""
    expected = cache_contract(protocol)
    if metadata.get('tensors') != expected['tensors'] or metadata.get('contract') != expected:
        raise ValueError('特征缓存的张量或预处理协议不一致；请使用新缓存目录重新提取，保留历史冻结证据')
