"""Compare frozen captcha.solve pipelines in sequential, independent CPU workers."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
RESEARCH_DEFAULT = Path('/workspace/autoSkill-release/captcha-research')
ORIGINAL_SOURCE = Path('/workspace/autoSkill-static-checks/skills/autoskill/app/autoskill/captcha.py')
CATEGORIES = ('plain', 'pale', 'through-line', 'first-symbol-touching')
ORDER = ('original', 'candidate', 'candidate', 'original', 'candidate', 'original', 'original', 'candidate')
CONFIGURATIONS = ({'name': 'ort-default', 'intra': 0, 'inter': 0}, {'name': 'intra2-inter1', 'intra': 2, 'inter': 1})
EXPECTED = {
    'original_source': '18b899e023f29f85da0ab07dd3d10e389f38b85e6fc8ebe9233ecae0f59db6ea',
    'candidate_source': 'ae7c69e1b5adc163771e360e6e92c08369518dd34e9e741073388e7736029ae0',
    'candidate_helper': '202432c2ada74a8eb30d06b8b62b126f574e98f246252ccc750680e15d5dca57',
    'original_model': 'be9dd198e7dff674254d96a9a0d6a21d0653bed3e4d8dc9afc3f325cbee3e3b4',
    'original_meta': '2641a73630a7de539669e622f0fc7b6ddb34f5b970134a091b68021ffee11346',
    'candidate_model': 'b58376af1642a97b0df1be84642170c1f9a88203b86b062c3fe3d272e1759662',
    'candidate_meta': '3d8291576201fe63a270da934fda8159e0d5c9229d2cf781da4e72017bba13de',
    'manifest': 'c2c755ea5c67e50808b05b14f044f4868fb49085e57669853d09438c238e6cdf',
}
THREAD_ENV = {'OMP_NUM_THREADS': '1', 'OPENBLAS_NUM_THREADS': '1'}


def digest(path):
    """Hash models by streaming, without a model-sized temporary allocation."""
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def write_json(path, data):
    with Path(path).open('x', encoding='utf-8') as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write('\n')


def parser():
    result = argparse.ArgumentParser(description='固定 dev 32 图、完整 captcha.solve 链路的独立进程对照；执行此工具会运行 CPU 推理。')
    mode = result.add_mutually_exclusive_group(required=True)
    mode.add_argument('--output', type=Path, help='必须不存在的新结果目录；保留逐 block 原始记录')
    mode.add_argument('--worker-job', type=Path, help=argparse.SUPPRESS)
    result.add_argument('--research-dir', type=Path, default=Path(os.environ.get('CAPTCHA_RESEARCH_DIR', RESEARCH_DEFAULT)))
    result.add_argument('--manifest', type=Path, default=os.environ.get('CAPTCHA_SOLVER_MANIFEST'))
    result.add_argument('--input-root', type=Path, default=os.environ.get('CAPTCHA_SOLVER_INPUT_ROOT'))
    result.add_argument('--original-source', type=Path, default=Path(os.environ.get('CAPTCHA_ORIGINAL_SOURCE', ORIGINAL_SOURCE)))
    result.add_argument('--candidate-source', type=Path, default=Path(os.environ.get('CAPTCHA_CANDIDATE_SOURCE', REPOSITORY / 'skills/autoskill/app/autoskill/captcha.py')))
    result.add_argument('--helper-dir', type=Path, default=os.environ.get('CAPTCHA_SOLVER_HELPER_DIR'), help='候选 autoskill 包目录，提供真实相对导入 .paths/.captcha_image')
    for kind, variable in (('original', 'CAPTCHA_BASELINE'), ('candidate', 'CAPTCHA_CANDIDATE')):
        result.add_argument(f'--{kind}-model', type=Path, default=os.environ.get(f'{variable}_MODEL'))
        result.add_argument(f'--{kind}-meta', type=Path, default=os.environ.get(f'{variable}_META'))
    for name, expected in EXPECTED.items():
        result.add_argument('--' + name.replace('_', '-') + '-sha256', default=os.environ.get('CAPTCHA_SOLVER_' + name.upper() + '_SHA256', expected))
    result.add_argument('--worker-timeout-seconds', type=float, default=600, help='每个独立子进程的超时，不改变固定测量协议')
    return result


def asset_paths(args):
    original_model = args.original_model or args.research_dir / 'baselines/original-be9dd198/captcha-ctc.onnx'
    candidate_model = args.candidate_model or REPOSITORY / 'skills/autoskill/models/captcha/captcha-ctc.onnx'
    helper = args.helper_dir or args.candidate_source.parent
    return {
        'original_source': args.original_source,
        'candidate_source': args.candidate_source,
        'candidate_helper': helper / 'captcha_image.py',
        'original_model': original_model,
        'original_meta': args.original_meta or original_model.with_suffix('.json'),
        'candidate_model': candidate_model,
        'candidate_meta': args.candidate_meta or candidate_model.with_suffix('.json'),
        'manifest': args.manifest or args.research_dir / 'datasets/manifest.jsonl',
        'package_init': helper / '__init__.py',
        'package_paths': helper / 'paths.py',
    }


def identify_assets(args):
    assets = {}
    for name, value in asset_paths(args).items():
        path = value.resolve(strict=True)
        actual = digest(path)
        expected = getattr(args, name + '_sha256', actual)
        if actual != expected:
            raise ValueError(f'{name} SHA256 不匹配：expected={expected}, actual={actual}, file={path}')
        assets[name] = {'path': str(path), 'sha256': actual, 'bytes': path.stat().st_size}
    if assets['original_source']['sha256'] == assets['candidate_source']['sha256']:
        raise ValueError('original 与 candidate 必须加载不同 SHA256 的 captcha.py；不能用当前增强源码冒充 baseline')
    return assets


def select_inputs(manifest, input_root):
    """Take the first eight dev entries per fixed category, in manifest order."""
    groups = {category: [] for category in CATEGORIES}
    with Path(manifest).open(encoding='utf-8') as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            category = row.get('category')
            if row.get('split') != 'dev' or category not in groups or len(groups[category]) == 8:
                continue
            path = (input_root / row['file']).resolve(strict=True)
            actual = digest(path)
            if actual != row['png_sha256']:
                raise ValueError(f'dev 输入与 manifest PNG SHA256 不符：{path}')
            groups[category].append({'category': category, 'file': row['file'], 'path': str(path), 'sha256': actual, 'split': 'dev'})
            if all(len(items) == 8 for items in groups.values()):
                break
    if any(len(items) != 8 for items in groups.values()):
        raise ValueError('manifest 必须为四个固定类别分别提供至少 8 张 dev 输入')
    rows = [row for category in CATEGORIES for row in groups[category]]
    if len({row['path'] for row in rows}) != 32:
        raise ValueError('固定 32 个 dev 条目必须指向 32 个不同文件')
    return [{'index': index, **row} for index, row in enumerate(rows)]


class HashedSourceLoader(importlib.machinery.SourceFileLoader):
    """Compile verified source bytes directly, bypassing potentially stale pyc files."""

    def __init__(self, name, asset, loaded):
        super().__init__(name, asset['path'])
        self.asset = asset
        self.loaded = loaded

    def get_code(self, fullname):
        if fullname != self.name:
            raise ImportError('Source loader module name mismatch')
        source = self.get_data(self.path)
        if hashlib.sha256(source).hexdigest() != self.asset['sha256']:
            raise ImportError(f'Pinned source changed before import: {self.path}')
        self.loaded[fullname] = self.asset
        return self.source_to_code(source, self.path)


class SourcePackageFinder(importlib.abc.MetaPathFinder):
    """Provide a real isolated package with pinned solver and candidate helpers."""

    def __init__(self, name, assets, kind):
        self.name = name
        self.loaded = {}
        self.modules = {
            name: assets['package_init'],
            name + '.captcha': assets[kind + '_source'],
            name + '.paths': assets['package_paths'],
            name + '.captcha_image': assets['candidate_helper'],
        }

    def find_spec(self, fullname, _path=None, _target=None):
        asset = self.modules.get(fullname)
        if asset is None:
            if fullname.startswith(self.name + '.'):
                raise ImportError(f'Unpinned relative helper import: {fullname}')
            return None
        locations = [str(Path(asset['path']).parent)] if fullname == self.name else None
        loader = HashedSourceLoader(fullname, asset, self.loaded)
        return importlib.util.spec_from_file_location(fullname, asset['path'], loader=loader, submodule_search_locations=locations)


class TimedSession:
    """Delegate a single ORT session and accumulate every run inside one solve."""

    def __init__(self, session):
        self.session = session
        self.reset()

    def reset(self):
        self.inference_ms = 0.0
        self.run_count = 0

    def run(self, *args, **kwargs):
        started = time.perf_counter()
        try:
            return self.session.run(*args, **kwargs)
        finally:
            self.inference_ms += (time.perf_counter() - started) * 1000
            self.run_count += 1

    def __getattr__(self, name):
        return getattr(self.session, name)


def measured_solve(solver, session, png):
    session.reset()
    error = None
    started = time.perf_counter()
    try:
        solver.solve(png)
    except ValueError as failure:
        error = str(failure)
    elapsed = (time.perf_counter() - started) * 1000
    return {'solve_ms': elapsed, 'inference_ms': session.inference_ms, 'run_count': session.run_count, 'error': error}


def create_session(ort, model, configuration):
    options = ort.SessionOptions()
    options.log_severity_level = 3
    options.intra_op_num_threads = configuration['intra']
    options.inter_op_num_threads = configuration['inter']
    started = time.perf_counter()
    session = ort.InferenceSession(model, options, providers=['CPUExecutionProvider'])
    elapsed = (time.perf_counter() - started) * 1000
    if session.get_providers() != ['CPUExecutionProvider']:
        raise ValueError('Worker 必须只启用 CPUExecutionProvider')
    return TimedSession(session), elapsed


def peak_rss_kib():
    import resource

    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value / 1024 if sys.platform == 'darwin' else value


def verify_used_assets(job, buffers, loaded):
    """Check identity after timing, without including provenance hashing in timings."""
    for name, asset in job['assets'].items():
        if digest(asset['path']) != asset['sha256']:
            raise ValueError(f'测量期间资产发生变化：{name}')
    for row, png in zip(job['inputs'], buffers):
        if hashlib.sha256(png).hexdigest() != row['sha256']:
            raise ValueError(f'实际使用的 PNG bytes 与预检 SHA256 不符：{row["file"]}')
    expected = job['assets'][job['kind'] + '_source']
    if loaded.get('_solver_benchmark.captcha') != expected:
        raise ValueError('Worker 未加载指定 SHA256 的 solver 源文件')


def worker(job):
    """Start the cold clock after interpreter startup and before scientific imports."""
    cold_started = time.perf_counter()
    import numpy
    import onnxruntime as ort
    import PIL

    finder = SourcePackageFinder('_solver_benchmark', job['assets'], job['kind'])
    sys.meta_path.insert(0, finder)
    solver = importlib.import_module('_solver_benchmark.captcha')
    imports_ms = (time.perf_counter() - cold_started) * 1000
    prepare_started = time.perf_counter()
    buffers = [Path(row['path']).read_bytes() for row in job['inputs']]
    meta = json.loads(Path(job['assets'][job['kind'] + '_meta']['path']).read_text(encoding='utf-8'))
    input_prepare_ms = (time.perf_counter() - prepare_started) * 1000
    session, initialized_ms = create_session(ort, job['assets'][job['kind'] + '_model']['path'], job['configuration'])
    solver._loaded = (session, meta['charset'], meta['imageShape'][1], meta['imageShape'][2])
    first = measured_solve(solver, session, buffers[0])
    process_cold_ms = (time.perf_counter() - cold_started) * 1000
    warmups = [measured_solve(solver, session, buffers[0]) for _ in range(5)]
    samples = [{**row, **measured_solve(solver, session, png)} for row, png in zip(job['inputs'], buffers)]
    rss = peak_rss_kib()
    verify_used_assets(job, buffers, finder.loaded)
    return {
        'kind': job['kind'], 'block_index': job['block_index'], 'configuration': job['configuration'],
        'assets': job['assets'], 'loaded_source_files': finder.loaded,
        'versions': {'python': platform.python_version(), 'numpy': numpy.__version__, 'onnxruntime': ort.__version__, 'Pillow': PIL.__version__},
        'providers': session.get_providers(), 'thread_environment': {name: os.environ.get(name) for name in THREAD_ENV},
        'cold': {'process_cold_ms': process_cold_ms, 'imports_ms': imports_ms, 'input_prepare_ms': input_prepare_ms,
                 'initialization_ms': initialized_ms, 'first_solve': first},
        'warmups': warmups, 'warm_samples': samples, 'peak_rss_kib': rss,
        'provenance_verified_after_timing': True,
    }


def distribution(values):
    ordered = sorted(values)
    return {'n': len(ordered), 'median': statistics.median(ordered), 'p95': ordered[math.ceil(.95 * len(ordered)) - 1]}


def sample_summary(samples):
    return {
        'attempts': len(samples), 'errors': sum(row['error'] is not None for row in samples),
        **{key: distribution([row[key] for row in samples]) for key in ('solve_ms', 'inference_ms', 'run_count')},
        'total_session_runs': sum(row['run_count'] for row in samples),
    }


def group_summary(blocks):
    samples = [sample for block in blocks for sample in block['warm_samples']]
    return {
        'blocks': len(blocks),
        'warm': {'overall': sample_summary(samples), 'by_category': {category: sample_summary([row for row in samples if row['category'] == category]) for category in CATEGORIES}},
        'block_warm': [{'block_index': block['block_index'], **sample_summary(block['warm_samples'])} for block in blocks],
        'cold': {key: distribution([block['cold'][key] for block in blocks]) for key in ('process_cold_ms', 'imports_ms', 'input_prepare_ms', 'initialization_ms')},
        'first_solve': sample_summary([block['cold']['first_solve'] for block in blocks]),
        'peak_rss_kib': distribution([block['peak_rss_kib'] for block in blocks]),
    }


def summarize(blocks):
    result = {}
    for configuration in CONFIGURATIONS:
        groups = {kind: group_summary([block for block in blocks if block['kind'] == kind and block['configuration'] == configuration]) for kind in ('original', 'candidate')}
        groups['warm_median_ratio_original_over_candidate'] = {
            key: groups['original']['warm']['overall'][key]['median'] / groups['candidate']['warm']['overall'][key]['median']
            if groups['candidate']['warm']['overall'][key]['median'] else None
            for key in ('solve_ms', 'inference_ms')
        }
        result[configuration['name']] = groups
    return result


def run_block(output, job, timeout):
    stem = f'{job["configuration"]["name"]}-{job["block_index"]:02d}-{job["kind"]}'
    request = output / (stem + '.request.json')
    write_json(request, job)
    environment = {**os.environ, **THREAD_ENV, 'PYTHONDONTWRITEBYTECODE': '1'}
    try:
        done = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--worker-job', str(request)],
                              env=environment, text=True, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as error:
        for suffix, stream in (('.stdout.json', error.stdout), ('.stderr.txt', error.stderr)):
            captured = stream.decode('utf-8', errors='replace') if isinstance(stream, bytes) else stream or ''
            (output / (stem + suffix)).write_text(captured, encoding='utf-8')
        raise ValueError(f'独立 worker 超时；已保留输出：{output / stem}') from error
    (output / (stem + '.stdout.json')).write_text(done.stdout, encoding='utf-8')
    (output / (stem + '.stderr.txt')).write_text(done.stderr, encoding='utf-8')
    if done.returncode:
        raise ValueError(f'独立 worker 失败，exit={done.returncode}；查看 {output / (stem + ".stderr.txt")} 和 stdout.json')
    block = json.loads(done.stdout)
    if block['kind'] != job['kind'] or block['configuration'] != job['configuration'] or not block['provenance_verified_after_timing']:
        raise ValueError(f'Worker 协议或资产校验失败：{stem}')
    return block


def benchmark(args):
    if not math.isfinite(args.worker_timeout_seconds) or args.worker_timeout_seconds <= 0:
        raise ValueError('--worker-timeout-seconds 必须为有限正数')
    output = args.output.resolve()
    if output.exists():
        raise ValueError(f'--output 必须是新目录，拒绝覆盖：{output}')
    assets = identify_assets(args)
    rows = select_inputs(assets['manifest']['path'], (args.input_root or args.research_dir).resolve())
    protocol = {
        'order_per_configuration': ORDER, 'configurations': CONFIGURATIONS, 'independent_processes': True,
        'blocks_per_model_per_configuration': 4, 'fixed_dev_inputs_per_block': 32, 'category_order': CATEGORIES,
        'selection': 'first 8 dev entries per category in the original manifest order; no labels/predictions used',
        'cold_first_solves_per_block': 1, 'additional_first_input_warmups': 5, 'warm_measured_solves_per_block': 32,
        'percentile': 'nearest rank: sorted[ceil(0.95*n)-1]', 'cold_clock_excludes': 'Python startup; benchmark stdlib imports; bulk asset/model/input provenance hashing',
        'cold_clock_includes': 'scientific/source imports; all 32 PNG byte reads; meta read; one session initialization; first full solve',
        'source_load_note': 'Small source-byte SHA checks occur inside module imports; lazy helper/Pillow imports remain inside first solve',
        'warm_clock': 'full solve including image decoding/preprocessing/all ORT runs/CTC; excludes PNG file reads',
        'os_page_cache_flushed': False, 'cache_note': 'Parent preflight hashes assets and inputs; OS page cache is not cleared and may be warm',
        'thread_environment': THREAD_ENV, 'sessions_per_worker': 1, 'provider': 'CPUExecutionProvider',
        'rss_note': 'Process lifetime peak including startup/imports/ORT/work; sampled before post-timing provenance hashing; KiB (macOS bytes converted)',
        'errors': 'ValueError solve attempts retained in timing distributions; other worker failures abort without a completed summary',
    }
    metadata = {'protocol': protocol, 'assets': assets, 'inputs': rows, 'tool': {'path': str(Path(__file__).resolve()), 'sha256': digest(__file__)},
                'host': {'platform': platform.platform(), 'machine': platform.machine(), 'cpu_count': os.cpu_count(), 'python': sys.executable}}
    output.mkdir(parents=True)
    write_json(output / 'run-metadata.json', metadata)
    blocks = []
    for configuration in CONFIGURATIONS:
        for index, kind in enumerate(ORDER):
            job = {'kind': kind, 'block_index': index, 'configuration': configuration, 'assets': assets, 'inputs': rows}
            blocks.append(run_block(output, job, args.worker_timeout_seconds))
    result = {**metadata, 'summary': summarize(blocks), 'blocks': blocks}
    write_json(output / 'solver-pipeline-benchmark.json', result)
    return {'status': 'Pass', 'output': str(output / 'solver-pipeline-benchmark.json'), 'blocks': len(blocks), 'summary': result['summary']}


def main():
    args = parser().parse_args()
    try:
        result = worker(json.loads(args.worker_job.read_text(encoding='utf-8'))) if args.worker_job else benchmark(args)
    except (OSError, ValueError, ImportError, RuntimeError, subprocess.SubprocessError) as error:
        print(json.dumps({'status': 'Error', 'error': str(error)}, ensure_ascii=False))  # noqa: T201  # Emit the command or worker's single JSON result.
        return 2
    print(json.dumps(result, ensure_ascii=False))  # noqa: T201  # Emit the command or worker's single JSON result.
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
