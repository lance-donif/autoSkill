"""Maintainer assembly from supplied local assets. No network access or installers."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import zipfile
from email.parser import Parser

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def unpack(archive, destination):
    with zipfile.ZipFile(archive) as z:
        for member in z.infolist():
            if not (destination / member.filename).resolve().is_relative_to(destination.resolve()):
                raise ValueError('Unsafe archive member')
        z.extractall(destination)


def copy_app(destination):
    package = json.loads((ROOT / 'package.json').read_text())
    destination.mkdir(parents=True)
    for name in ['package.json', '.npmrc', *package['files']]:
        src, dst = ROOT / name, destination / name
        if src.is_dir():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns('vendor', '__pycache__', '*.pyc', '.env', '*.sqlite*'))
        else:
            shutil.copy2(src, dst)


def build(assets, output):
    if output.exists() or output.is_relative_to(ROOT) or assets.is_relative_to(output):
        raise ValueError('Use a new output directory outside the source and assets')
    metadata = json.loads((assets / 'download-sources.json').read_text())
    expected = next(line.split()[0] for line in metadata['node_checksums'].splitlines() if line.split()[-1] == metadata['node_file'])
    if sha(assets / 'node.zip') != expected:
        raise ValueError('Official Node checksum mismatch')
    copy_app(output)
    skill = output / 'skills/autoskill'
    vendor = skill / 'vendor/windows-x64'
    vendor.mkdir(parents=True)
    unpack(assets / 'python-embed.zip', vendor / 'python')
    unpack(assets / 'chrome.zip', vendor / 'chrome')
    unpack(assets / 'node.zip', vendor / 'node-unpack')
    node_root = next((vendor / 'node-unpack').iterdir())
    (vendor / 'node').mkdir()
    for name in ['node.exe', 'LICENSE', 'README.md']:
        if (node_root / name).exists():
            shutil.copy2(node_root / name, vendor / 'node' / name)
    shutil.rmtree(vendor / 'node-unpack')  # npm, installers and updater tools are not shipped.
    site = vendor / 'python/Lib/site-packages'
    site.mkdir(parents=True)
    wheels = []
    for wheel in sorted((assets / 'pinned-wheels').glob('*.whl')):
        with zipfile.ZipFile(wheel) as archive:
            meta = Parser().parsestr(archive.read(next(n for n in archive.namelist() if n.endswith('.dist-info/METADATA'))).decode())
        wheels.append({'name': meta['Name'], 'version': meta['Version'], 'file': wheel.name, 'sha256': sha(wheel)})
        unpack(wheel, site)
    if any('telemetry' in w['name'].lower() for w in wheels):
        raise ValueError('Telemetry dependency found')
    (vendor / 'python/python312._pth').write_text('python312.zip\n.\nLib/site-packages\n../app\nimport site\n')
    shutil.copytree(assets / 'node_modules', vendor / 'node_modules', symlinks=False,
                    ignore=shutil.ignore_patterns('.bin'))
    agent = vendor / 'node_modules/agent-browser'
    for path in (agent / 'bin').iterdir():
        if path.name != 'agent-browser-win32-x64.exe':
            path.unlink()
    shutil.rmtree(agent / 'scripts', ignore_errors=True)
    (vendor / 'agent-browser-config.json').write_text('{}\n')
    # The only PATH-visible entry invokes our offline policy wrapper, never the raw CLI.
    bin_dir = vendor / 'node_modules/.bin'
    bin_dir.mkdir()
    (bin_dir / 'agent-browser.cmd').write_text('@echo off\r\n"%~dp0..\\..\\node\\node.exe" "%~dp0..\\..\\app\\scripts\\agent-browser-offline.mjs" %*\r\nexit /b %errorlevel%\r\n')
    guide = skill / 'references/agent-browser-core.offline'
    shutil.copytree(agent / 'skill-data/core', guide)
    copy_app(vendor / 'app')
    # App root has its own main Skill copy for schema discovery; copy the local guide there too.
    shutil.copytree(guide, vendor / 'app/skills/autoskill/references/agent-browser-core.offline')
    # App uses the enclosing vendor dependencies. It must not recursively embed runtimes.
    binaries = ['python/python.exe', 'node/node.exe', 'chrome/chrome-win64/chrome.exe',
                'node_modules/agent-browser/bin/agent-browser-win32-x64.exe']
    for name in binaries:
        if (vendor / name).read_bytes()[:2] != b'MZ':
            raise ValueError('Not a Windows executable: ' + name)
    manifest = {'target': 'Windows x64 / Windows 10+', 'python': '3.12.10',
                'node': metadata['node_file'], 'browser': metadata['chrome_version'],
                'agent_browser': '0.42.0', 'dsh_sdk': '0.0.1-rc.1', 'wheels': wheels,
                'source_archives': {name: sha(assets / name) for name in ['python-embed.zip', 'node.zip', 'chrome.zip']},
                'network_install': False, 'windows_execution_verified': False,
                'files': {p.relative_to(output).as_posix(): sha(p) for p in sorted(output.rglob('*')) if p.is_file()}}
    (output / 'offline-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    (output / 'requirements-windows.lock').write_text('\n'.join(f"{w['name']}=={w['version']}" for w in wheels) + '\n')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--assets', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = build(args.assets.resolve(), args.output.resolve())
    print(json.dumps({k: v for k, v in result.items() if k != 'files'}, indent=2))
