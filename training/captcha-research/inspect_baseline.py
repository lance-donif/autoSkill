"""Read-only ONNX inspection and isolated synthetic inference baseline."""
from __future__ import annotations

import gc
import hashlib
import io
import json
import math
import os
import platform
import random
import statistics
import time
from collections import Counter
from pathlib import Path

import numpy as np
import onnxruntime as ort
from PIL import Image, ImageDraw, ImageFont

SOURCE = Path(os.environ.get('CAPTCHA_MODEL_DIR', str(Path.cwd() / 'skills/autoskill/models/captcha')))
OUTPUT = Path(os.environ.get('CAPTCHA_RESEARCH_DIR', str(Path(__file__).resolve().parent)))


def varint(data, pos):
    value = shift = 0
    while True:
        byte = data[pos]
        pos += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, pos
        shift += 7


def fields(data):
    pos = 0
    while pos < len(data):
        tag, pos = varint(data, pos)
        number, wire = tag >> 3, tag & 7
        if wire == 0:
            value, pos = varint(data, pos)
        elif wire == 2:
            length, pos = varint(data, pos)
            value, pos = data[pos:pos + length], pos + length
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
            value, pos = data[pos:pos + size], pos + size
        else:
            raise ValueError(f'Unexpected protobuf wire type {wire}')
        yield number, wire, value


def decode_text(value):
    return value.decode('utf-8', errors='replace') if isinstance(value, bytes) else value


def tensor_info(data):
    dims, dtype, name, raw_bytes = [], None, '', 0
    for number, wire, value in fields(data):
        if number == 1:
            if wire == 0:
                dims.append(value)
            else:
                pos = 0
                while pos < len(value):
                    dim, pos = varint(value, pos)
                    dims.append(dim)
        elif number == 2:
            dtype = value
        elif number == 8:
            name = decode_text(value)
        elif number == 9:
            raw_bytes = len(value)
    return {'name': name, 'dtype': dtype, 'shape': dims, 'elements': math.prod(dims), 'raw_bytes': raw_bytes}


def inspect_model(file):
    content = file.read_bytes()
    model = list(fields(content))
    graph = next(value for number, wire, value in model if number == 7)
    nodes, tensors, graph_name = [], [], ''
    for number, _wire, value in fields(graph):
        if number == 1:
            node = {'inputs': [], 'outputs': []}
            for field, _, item in fields(value):
                if field == 1:
                    node['inputs'].append(decode_text(item))
                elif field == 2:
                    node['outputs'].append(decode_text(item))
                elif field in (3, 4, 7):
                    node[{3: 'name', 4: 'op', 7: 'domain'}[field]] = decode_text(item)
            nodes.append(node)
        elif number == 2:
            graph_name = decode_text(value)
        elif number == 5:
            tensors.append(tensor_info(value))
    opsets = []
    for number, _, value in model:
        if number == 8:
            opsets.append({str(field): decode_text(item) for field, _, item in fields(value)})
    floating = [item for item in tensors if item['dtype'] in (1, 10, 11, 16)]
    return {
        'file': str(file), 'sha256': hashlib.sha256(content).hexdigest(), 'file_bytes': len(content),
        'ir_version': next(value for number, _, value in model if number == 1),
        'producer': {str(number): decode_text(value) for number, _, value in model if number in (2, 3, 4, 5)},
        'graph_name': graph_name, 'opsets': opsets, 'node_count': len(nodes),
        'operator_counts': dict(Counter(node['op'] for node in nodes)),
        'initializer_count': len(tensors), 'initializer_dtype_counts': dict(Counter(item['dtype'] for item in tensors)),
        'floating_initializer_elements': sum(item['elements'] for item in floating),
        'floating_initializer_raw_bytes': sum(item['raw_bytes'] for item in floating),
        'largest_initializers': sorted(floating, key=lambda item: -item['elements'])[:16],
        'first_nodes': nodes[:12], 'last_nodes': nodes[-12:],
    }


def prepare(png, height, max_width):
    image = Image.open(io.BytesIO(png)).convert('RGBA')
    flat = Image.new('RGBA', image.size, 'white')
    flat.alpha_composite(image)
    width = max(1, min(max_width, -(-image.width * height // image.height)))
    pixels = np.asarray(flat.convert('RGB').resize((width, height), Image.BILINEAR), dtype=np.float32)
    tensor = np.zeros((1, 3, height, max_width), dtype=np.float32)
    tensor[0, :, :, :width] = (pixels[:, :, ::-1] / 127.5 - 1).transpose(2, 0, 1)
    return tensor


def decode(frames, charset):
    text, previous = [], 0
    for index in frames[0].argmax(axis=1):
        if index and index != previous:
            text.append(charset[int(index)])
        previous = int(index)
    return ''.join(text)


def samples(charset):
    generator = random.Random(20261112)
    fonts = [Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'), Path('/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf')]
    fonts = [file for file in fonts if file.is_file()]
    records = []
    folder = OUTPUT / 'synthetic-diagnostic'
    folder.mkdir(exist_ok=True)
    for index in range(32):
        font_file = fonts[index % len(fonts)]
        label = ''.join(generator.choice(charset[1:]) for _ in range(4 if index % 2 == 0 else 6))
        noisy = index >= 16
        image = Image.new('RGB', (160, 52), 'white')
        draw = ImageDraw.Draw(image)
        draw.text((8, 7), label, font=ImageFont.truetype(str(font_file), 26), fill=(15, 15, 15))
        if noisy:
            for _ in range(2):
                draw.line((0, generator.randrange(52), 159, generator.randrange(52)), fill=(80, 80, 80), width=1)
        buffer = io.BytesIO()
        image.save(buffer, format='PNG')
        name = f'{index:03d}.png'
        (folder / name).write_bytes(buffer.getvalue())
        records.append({'file': name, 'expected': label, 'font': font_file.name, 'crossed_lines': noisy,
                        'png': buffer.getvalue()})
    return records


def percentile(values, quantile):
    values = sorted(values)
    return values[min(len(values) - 1, round((len(values) - 1) * quantile))]


def benchmark(meta, records):
    results = []
    reference = None
    tensors = [prepare(item['png'], meta['imageShape'][1], meta['imageShape'][2]) for item in records]
    for threads in (1, 2, 0):
        options = ort.SessionOptions()
        options.log_severity_level = 3
        if threads:
            options.intra_op_num_threads = threads
            options.inter_op_num_threads = 1
        start = time.perf_counter()
        session = ort.InferenceSession(str(SOURCE / 'captcha-ctc.onnx'), options, providers=['CPUExecutionProvider'])
        init_ms = (time.perf_counter() - start) * 1000
        key = session.get_inputs()[0].name
        for _ in range(3):
            session.run(None, {key: tensors[0]})
        latency, total_latency, predictions = [], [], []
        for item, tensor in zip(records, tensors):
            started = time.perf_counter()
            result = session.run(None, {key: tensor})[0]
            latency.append((time.perf_counter() - started) * 1000)
            predictions.append(decode(result, meta['charset']))
            started = time.perf_counter()
            decode(session.run(None, {key: prepare(item['png'], meta['imageShape'][1], meta['imageShape'][2])})[0], meta['charset'])
            total_latency.append((time.perf_counter() - started) * 1000)
        row = {
            'intra_op_threads': threads or 'ORT default', 'inter_op_threads': 1 if threads else 'ORT default',
            'initialization_ms': init_ms, 'warm_inference_median_ms': statistics.median(latency),
            'warm_inference_p95_ms': percentile(latency, .95),
            'preprocess_inference_decode_median_ms': statistics.median(total_latency),
            'synthetic_exact_match': sum(pred == item['expected'] for pred, item in zip(predictions, records)),
            'synthetic_total': len(records),
            'predictions_identical_to_single_thread': reference is None or reference == predictions,
            'input': [{'name': inp.name, 'type': inp.type, 'shape': inp.shape} for inp in session.get_inputs()],
            'output': [{'name': out.name, 'type': out.type, 'shape': out.shape} for out in session.get_outputs()],
            'providers': session.get_providers(),
        }
        results.append(row)
        if reference is None:
            reference = predictions
            for item, prediction in zip(records, predictions):
                item['prediction'] = prediction
        print(json.dumps(row), flush=True)  # noqa: T201  # benchmark progress is JSON Lines for the external evidence log
        del session
        gc.collect()
    return results


if __name__ == '__main__':
    meta = json.loads((SOURCE / 'captcha-ctc.json').read_text())
    report = {'model': inspect_model(SOURCE / 'captcha-ctc.onnx'), 'metadata': meta,
              'environment': {'python': platform.python_version(), 'onnxruntime': ort.__version__,
                              'numpy': np.__version__, 'logical_cpus': os.cpu_count(),
                              'platform': platform.platform()},
              'warning': 'Synthetic diagnostic only; no representative labeled validation corpus or training checkpoint is available locally.'}
    print(json.dumps(report['model']), flush=True)  # noqa: T201  # the inspection command writes its JSON result
    records = samples(meta['charset'])
    report['baseline'] = benchmark(meta, records)
    report['synthetic_diagnostic'] = [{key: value for key, value in row.items() if key != 'png'} for row in records]
    (OUTPUT / 'inspection-and-baseline.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
