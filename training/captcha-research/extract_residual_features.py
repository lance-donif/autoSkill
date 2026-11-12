"""Extract frozen train/dev features and diagnose the two original RBF heads."""
import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path

import numpy as np
import onnx
import onnxruntime as ort
from onnx import helper
from residual_ctc import sequence_edit_distance
from residual_data import decode_one, frozen_metadata, prepare

SCRIPT = Path(__file__).resolve().parent


def load_rows(manifest):
    """Reject, rather than silently train on, any held-out or external source."""
    rows = [json.loads(line) for line in manifest.read_text().splitlines()]
    if any(row['source'] != 'synthetic-residual-v2' or row['split'] not in ('train', 'dev') for row in rows):
        raise ValueError('Residual manifest may contain only the new synthetic train/dev source')
    return rows


def extract(root, manifest, directory, protocol):
    """Keep every teacher tensor tied to the immutable original-model SHA."""
    rows = load_rows(manifest)
    source = root / 'baselines/original-be9dd198/captcha-ctc.onnx'
    model_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest_sha = hashlib.sha256(manifest.read_bytes()).hexdigest()
    if model_sha != protocol['teacher_model_sha256']:
        raise ValueError('Original baseline snapshot SHA changed')
    directory.mkdir(parents=True, exist_ok=True)
    metadata_file = directory / 'metadata.json'
    cache_file = directory / 'features.npz'
    if metadata_file.is_file() and cache_file.is_file():
        old = json.loads(metadata_file.read_text())
        if old['model_sha256'] == model_sha and old['manifest_sha256'] == manifest_sha:
            return cache_file
        raise ValueError('Refuse to reuse a cache from different weights or data')
    model = onnx.load(source)
    names = {'features': protocol['feature_tensor'], 'logits': protocol['baseline_logits_tensor'],
             'ctc': protocol['ctc_logits_tensor'], 'local': protocol['local_tensor'], 'expanded': protocol['expanded_tensor']}
    available = {value for node in model.graph.node for value in node.output}
    if any(value not in available for value in names.values()):
        raise ValueError('Required frozen intermediate is missing')
    for key, name in names.items():
        model.graph.output.append(helper.make_tensor_value_info(name, onnx.TensorProto.FLOAT, [None, 40, 120 if key == 'features' else 63]))
    graph_file = directory / 'extraction-only.onnx'
    onnx.save(model, graph_file)
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 1
    options.log_severity_level = 3
    session = ort.InferenceSession(str(graph_file), options, providers=['CPUExecutionProvider'])
    input_name = session.get_inputs()[0].name
    arrays = {key: [] for key in names}
    for index, row in enumerate(rows):
        image_file = root / row['file']
        if hashlib.sha256(image_file.read_bytes()).hexdigest() != row['png_sha256']:
            raise ValueError('Image bytes changed after the manifest was frozen')
        tensor = prepare(image_file.read_bytes(), 48, 320)
        result = session.run(list(names.values()), {input_name: tensor})
        for key, value in zip(names, result):
            arrays[key].append(value[0])
        if index % 100 == 0:
            print(json.dumps({'phase': 'frozen-feature-extraction', 'completed': index, 'total': len(rows)}), flush=True)  # noqa: T201  # research progress log
    np.savez_compressed(cache_file, ids=np.asarray([row['id'] for row in rows]),
                        **{key: np.asarray(value, dtype=np.float32) for key, value in arrays.items()})
    metadata = {'model_sha256': model_sha, 'manifest_sha256': manifest_sha, 'cache_sha256': hashlib.sha256(cache_file.read_bytes()).hexdigest(),
                'rows': len(rows), 'source': str(source), 'tensors': names, 'test_images_loaded': 0, 'external_images_loaded': 0,
                'intra_op_threads': 2, 'inter_op_threads': 1}
    metadata_file.write_text(json.dumps(metadata, indent=2))
    return cache_file


def metrics(logits, rows, charset):
    """Report exact matches per existing split/category without test access."""
    measured = defaultdict(lambda: {'exact': 0, 'samples': 0, 'edit_errors': 0, 'characters': 0})
    for item, row in zip(logits, rows):
        prediction = decode_one(item[None], charset)
        correct = prediction == row['label']
        for key in (row['split'] + '/' + row['category'], row['split'] + '/ALL'):
            measured[key]['exact'] += correct
            measured[key]['samples'] += 1
            measured[key]['edit_errors'] += sequence_edit_distance(prediction, row['label'])
            measured[key]['characters'] += len(row['label'])
    for group in measured.values():
        group['exact_match_rate'] = group['exact'] / group['samples']
        group['character_error_rate'] = group['edit_errors'] / group['characters']
    return dict(measured)


def ablate(root, cache_file, manifest, protocol):
    """Diagnostic only: no production head is scaled or selected here."""
    rows = load_rows(manifest)
    cache = np.load(cache_file)
    if list(cache['ids']) != [row['id'] for row in rows]:
        raise ValueError('Feature cache ordering differs from the manifest')
    charset = frozen_metadata(root)['charset']
    trials = []
    for local, expanded in protocol['ablation_scales']:
        logits = cache['ctc'] + local * cache['local'] + expanded * cache['expanded']
        trials.append({'local_scale': local, 'expanded_scale': expanded, 'metrics': metrics(logits, rows, charset)})
    return {'protocol': 'Train/dev-only diagnostic, fixed scales, never selected from external results',
              'trials': trials, 'local_nonzero_values': int(np.count_nonzero(cache['local'])),
              'expanded_nonzero_values': int(np.count_nonzero(cache['expanded'])),
              'baseline_reconstruction_max_abs_error': float(np.abs(cache['ctc'] + cache['local'] + cache['expanded'] - cache['logits']).max()),
              'test_images_loaded': 0, 'external_images_loaded': 0}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--protocol', type=Path, default=SCRIPT / 'residual_protocol.json')
    args = parser.parse_args()
    chosen = json.loads(args.protocol.read_text())
    result_file = extract(args.root, args.manifest, args.directory, chosen)
    result = ablate(args.root, result_file, args.manifest, chosen)
    (args.directory / 'rbf-ablation.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))  # noqa: T201  # research command JSON result
