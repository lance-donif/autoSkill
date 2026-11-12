"""ABBA process-isolated benchmark; no simultaneous competing ORT sessions."""
import json
import os
import resource
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get('CAPTCHA_RESEARCH_DIR', str(Path(__file__).resolve().parent)))
SOURCE = Path(os.environ['CAPTCHA_BASELINE_MODEL'])


def worker(kind, threads):
    cold_started = time.perf_counter()
    import numpy as np
    import onnxruntime as ort
    from PIL import Image

    file = SOURCE if kind == 'original' else ROOT / 'candidates/rbf-distance-matmul.onnx'
    manifest = [json.loads(line) for line in (ROOT / 'datasets/manifest.jsonl').read_text().splitlines()]
    rows = []
    for category in ('plain', 'pale', 'through-line', 'first-symbol-touching'):
        rows += [row for row in manifest if row['split'] == 'dev' and row['category'] == category][:8]
    tensors = []
    for row in rows:
        image = Image.open(ROOT / row['file']).convert('RGB')
        width = min(320, -(-image.width * 48 // image.height))
        pixels = np.asarray(image.resize((width, 48), Image.BILINEAR), dtype=np.float32)
        tensor = np.zeros((1, 3, 48, 320), dtype=np.float32)
        tensor[0, :, :, :width] = (pixels[:, :, ::-1] / 127.5 - 1).transpose(2, 0, 1)
        tensors.append(tensor)
    options = ort.SessionOptions()
    options.log_severity_level = 3
    if threads:
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
    started = time.perf_counter()
    session = ort.InferenceSession(str(file), options, providers=['CPUExecutionProvider'])
    initialized_ms = (time.perf_counter() - started) * 1000
    key = session.get_inputs()[0].name
    started = time.perf_counter()
    session.run(None, {key: tensors[0]})
    first_ms = (time.perf_counter() - started) * 1000
    cold_to_first = (time.perf_counter() - cold_started) * 1000
    for _ in range(5):
        session.run(None, {key: tensors[0]})
    timings = []
    for tensor in tensors:
        started = time.perf_counter()
        session.run(None, {key: tensor})
        timings.append((time.perf_counter() - started) * 1000)
    return {'model': kind, 'threads': threads or 'ORT default', 'initialization_ms': initialized_ms,
            'first_inference_ms': first_ms, 'process_cold_imports_prepare_init_first_ms': cold_to_first,
            'warm_inference_ms': timings, 'warm_median_ms': statistics.median(timings),
            'warm_p95_ms': sorted(timings)[round(.95 * (len(timings) - 1))],
            'peak_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}


if __name__ == '__main__':
    if len(sys.argv) > 1:
        print(json.dumps(worker(sys.argv[1], int(sys.argv[2]))))  # noqa: T201  # the subprocess protocol is one JSON result
    else:
        results = []
        for threads in (0, 2):
            for kind in ('original', 'distance', 'distance', 'original', 'distance', 'original', 'original', 'distance'):
                done = subprocess.run([sys.executable, __file__, kind, str(threads)], env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
                                      text=True, capture_output=True, check=True)
                row = json.loads(done.stdout)
                results.append(row)
                print(json.dumps({key: value for key, value in row.items() if key != 'warm_inference_ms'}), flush=True)  # noqa: T201  # JSON Lines benchmark progress
        summary = {}
        for threads in ('ORT default', 2):
            groups = {}
            for kind in ('original', 'distance'):
                group = [row for row in results if row['model'] == kind and row['threads'] == threads]
                samples = [value for row in group for value in row['warm_inference_ms']]
                groups[kind] = {'median_ms': statistics.median(samples), 'p95_ms': sorted(samples)[round(.95 * (len(samples) - 1))],
                                'block_medians_ms': [row['warm_median_ms'] for row in group],
                                'cold_imports_prepare_init_first_median_ms': statistics.median(row['process_cold_imports_prepare_init_first_ms'] for row in group),
                                'init_median_ms': statistics.median(row['initialization_ms'] for row in group),
                                'first_inference_median_ms': statistics.median(row['first_inference_ms'] for row in group),
                                'peak_rss_median_kib': statistics.median(row['peak_rss_kib'] for row in group)}
            groups['median_speedup'] = groups['original']['median_ms'] / groups['distance']['median_ms']
            summary[str(threads)] = groups
        (ROOT / 'paired-benchmark.json').write_text(json.dumps({'protocol': ('ABBA/BAAB independent processes, 4 blocks per model per thread config, '
                                                                           '32 fixed dev inputs/block, 5 warmups; OS page cache not flushed'),
                                                              'summary': summary, 'blocks': results}, indent=2))
        print('SUMMARY', json.dumps(summary), flush=True)  # noqa: T201  # the benchmark command writes its summary
