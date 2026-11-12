"""Reject stale tensor caches before they can acquire misleading provenance."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from extract_residual_features import extract
from residual_cache import cache_contract, require_cache_contract, tensor_names
from train_residual_ctc import prepare_inputs


class CacheContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.protocol = json.loads((Path(__file__).parent / 'residual_protocol.json').read_text())
        self.source = self.root / 'baselines/original-be9dd198/captcha-ctc.onnx'
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(b'test model; cache hits must not parse or infer')
        self.protocol['teacher_model_sha256'] = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.manifest = self.root / 'manifest.jsonl'
        rows = [{'id': split, 'source': 'synthetic-residual-v2', 'split': split,
                 'normal': True, 'category': 'plain', 'label': 'a'} for split in ('train', 'dev')]
        self.manifest.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        self.directory = self.root / 'cache'
        self.directory.mkdir()
        self.cache = self.directory / 'features.npz'
        np.savez(self.cache, ids=np.asarray(['train', 'dev']), features=np.zeros((2, 40, 120), np.float32),
                 logits=np.zeros((2, 40, 63), np.float32))
        self.metadata = {'model_sha256': self.protocol['teacher_model_sha256'],
                         'manifest_sha256': hashlib.sha256(self.manifest.read_bytes()).hexdigest(),
                         'cache_sha256': hashlib.sha256(self.cache.read_bytes()).hexdigest(),
                         'tensors': tensor_names(self.protocol), 'contract': cache_contract(self.protocol)}
        self.write_metadata()

    def write_metadata(self):
        (self.directory / 'metadata.json').write_text(json.dumps(self.metadata))

    def test_matching_contract_reuses_existing_cache_without_model_execution(self):
        with patch('extract_residual_features.onnx.load', side_effect=AssertionError('Unexpected model load')):
            self.assertEqual(extract(self.root, self.manifest, self.directory, self.protocol), self.cache)
        with patch('train_residual_ctc.frozen_metadata', return_value={'charset': ['', 'a']}):
            data = prepare_inputs(self.root, self.manifest, self.directory, self.protocol)
        self.assertEqual(data[4].shape, (2, 40, 120))

    def test_changed_tensor_protocol_is_rejected_by_extractor_and_trainer(self):
        for name in ('feature_tensor', 'baseline_logits_tensor'):
            protocol = {**self.protocol, name: 'a/different/tensor'}
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, '张量或预处理协议不一致'):
                extract(self.root, self.manifest, self.directory, protocol)
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, '张量或预处理协议不一致'):
                prepare_inputs(self.root, self.manifest, self.directory, protocol)

    def test_preprocessing_and_legacy_metadata_cannot_be_silently_relabelled(self):
        for field in ('function_sha256', 'metadata_sha256', 'numpy_version', 'pillow_version'):
            metadata = copy.deepcopy(self.metadata)
            metadata['contract']['preprocessing'][field] = 'changed'
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, '新缓存目录'):
                require_cache_contract(metadata, self.protocol)
        legacy = {key: value for key, value in self.metadata.items() if key != 'contract'}
        with self.assertRaisesRegex(ValueError, '保留历史冻结证据'):
            require_cache_contract(legacy, self.protocol)


if __name__ == '__main__':
    unittest.main()
