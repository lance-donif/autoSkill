import hashlib
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

root = Path('/workspace/autoSkill')
proof = Path(__file__).parent


def git(*args):
    return subprocess.check_output(['git', *args], cwd=root, text=True).strip()


def manifest():
    names = subprocess.check_output(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=root, text=True).split('\0')
    result = {}
    for name in sorted(set(names)):
        file = root / name
        if file.is_file() and (name.startswith(('skills/autoskill/app/', 'plugin/', 'scripts/', 'tests/')) or name in ('package.json', 'ruff.toml', 'biome.json')):
            with file.open('rb') as stream:
                result[name] = hashlib.file_digest(stream, 'sha256').hexdigest()
    return result


before = manifest()
metadata = {'started_at': datetime.now(timezone.utc).isoformat(), 'head': git('rev-parse', 'HEAD'),
            'status': git('status', '--short'), 'files_before': before, 'checks': []}
env = {**{key:value for key,value in os.environ.items() if key in ('PATH','HOME','LANG','LC_ALL','TMPDIR','TMP','TEMP','XDG_RUNTIME_DIR','DISPLAY','JAVA_HOME','NODE_PATH','SSL_CERT_FILE','SSL_CERT_DIR','REQUESTS_CA_BUNDLE','HTTPS_PROXY','HTTP_PROXY','ALL_PROXY','NO_PROXY')}, 'npm_config_offline': 'false', 'npm_config_prefer_offline': 'true',
       'AUTOSKILL_DSH_SDK_ROOT': '/workspace/autoskill-linux-tools',
       'AUTOSKILL_NEWMAN': '/workspace/autoskill-native-tools/node_modules/.bin/newman',
       'AUTOSKILL_APIFOX': '/workspace/autoskill-native-tools/node_modules/.bin/apifox',
       'AUTOSKILL_JMETER': '/workspace/autoskill-native-tools/apache-jmeter-5.6.3/bin/jmeter',
       'NATIVE_BROWSER': str(root / 'skills/autoskill/vendor/linux-x64/chrome/chrome-linux64/chrome'),
       'NATIVE_DRIVER': str(root / 'skills/autoskill/vendor/linux-x64/chromedriver/chromedriver-linux64/chromedriver'),
       'PYTHONPATH': '/workspace/autoskill-native-tools/python'}
commands = [
    ('ruff', ['.venv/bin/ruff', 'check', '.']),
    ('biome', ['npx', '--yes', '@biomejs/biome@2.6.7', 'lint']),
    ('conventions', ['.venv/bin/python', '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_conventions.py', '-v']),
    ('node', ['node', '--test', *map(str, sorted(Path('tests').glob('*.test.mjs')))]),
    ('python', ['.venv/bin/python', '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_*.py', '-v']),
]
for name, command in commands:
    started = time.monotonic()
    print('running ' + name, flush=True)
    with (proof / (name + '.txt')).open('w', encoding='utf-8') as log:
        log.write('HEAD: ' + metadata['head'] + '\nCommand: ' + json.dumps(command) + '\n\n')
        log.flush()
        completed = subprocess.run(command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT, check=False)
    item = {'name': name, 'command': command, 'exit': completed.returncode, 'seconds': round(time.monotonic() - started, 3)}
    metadata['checks'].append(item)
    (proof / 'metadata.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(item), flush=True)
metadata['finished_at'] = datetime.now(timezone.utc).isoformat()
after = manifest()
metadata['files_after'] = after
metadata['files_changed_during_checks'] = [name for name in sorted(before.keys() | after.keys()) if before.get(name) != after.get(name)]
(proof / 'metadata.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
raise SystemExit(int(any(item['exit'] for item in metadata['checks'])))
