"""Entry point for the bundled Windows runtime; no installers or downloads."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

SKILL = Path(__file__).resolve().parents[1]
VENDOR = SKILL / 'vendor/windows-x64'


def environment():
    env = os.environ.copy()
    env.update(AUTOSKILL_PYTHON=str(VENDOR / 'python/python.exe'),
               AUTOSKILL_CHROMIUM=str(VENDOR / 'chrome/chrome-win64/chrome.exe'),
               AUTOSKILL_AGENT_BROWSER_GUIDE=str(SKILL / 'references/agent-browser-core.offline/SKILL.md'),
               PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD='1', PIP_NO_INDEX='1',
               PIP_DISABLE_PIP_VERSION_CHECK='1', DO_NOT_TRACK='1', OTEL_SDK_DISABLED='true',
               npm_config_offline='true', npm_config_update_notifier='false',
               npm_config_audit='false', npm_config_fund='false')
    env['PATH'] = os.pathsep.join([str(VENDOR / 'node'), str(VENDOR / 'node_modules/.bin'), env.get('PATH', '')])
    return env


def verify_bundle():
    manifest = json.loads((VENDOR / 'runtime-manifest.json').read_text(encoding='utf-8'))
    for name, expected in manifest['files'].items():
        path = (VENDOR / name).resolve()
        if not path.is_relative_to(VENDOR.resolve()) or not path.is_file():
            raise ValueError('Missing or unsafe bundled dependency: ' + name)
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
        if digest.hexdigest() != expected:
            raise ValueError('Bundled dependency was changed: ' + name)
    return len(manifest['files'])


def main():
    args = sys.argv[1:]
    mode = args.pop(0) if args else 'doctor'
    if mode == 'verify':
        print(json.dumps({'verified_files': verify_bundle()}))
        return
    app = VENDOR / 'app'
    env = environment()
    if mode == 'qa':
        command = [sys.executable, str(SKILL / 'scripts/qa.py'), *args]
    elif mode in ('doctor', 'dashboard'):
        script = 'doctor.mjs' if mode == 'doctor' else 'start.mjs'
        command = [str(VENDOR / 'node/node.exe'), str(app / 'scripts' / script), *args]
    else:
        raise SystemExit('Use verify, doctor [--ui|--ai], qa <command> <project>, or dashboard')
    try:
        result = subprocess.run(command, cwd=app, env=env)
        raise SystemExit(result.returncode)
    except OSError:
        raise SystemExit('Bundled runtime is incomplete; no online fallback is permitted')


if __name__ == '__main__':
    main()
