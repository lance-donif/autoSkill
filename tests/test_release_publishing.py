"""Publication ordering: never replace an existing asset before digest verification."""
import importlib.util
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('publish_release', ROOT / 'scripts/publish-windows-release.py')
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


class ReleasePublishingTests(unittest.TestCase):
    def test_bad_digest_keeps_old_asset_and_release_unpublished(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            proof = base / 'proof'
            proof.mkdir()
            (proof / 'verify.json').write_text(json.dumps({'ok': True}))
            (proof / 'doctor.json').write_text(json.dumps({'ok': True, 'browser_launch': {'playwright': 'ok', 'selenium': 'ok'}}))
            (proof / 'python-tests.txt').write_text('Ran 117 tests in 100.0s\n\nOK (skipped=2)\n')
            archive = base / 'autoSkill-Windows-x64-Offline.zip'
            with zipfile.ZipFile(archive, 'w') as bundle:
                for name in ('package.json', 'cordis.patch.yml', 'plugin/index.mjs', 'autoskill/SKILL.md',
                             'autoskill/autoskill.cmd', 'autoskill/vendor/windows-x64/python/python.exe'):
                    bundle.writestr(name, b'fixture')
            calls = []
            version = json.loads((ROOT / 'package.json').read_text())['version']
            tag = 'v' + version
            manifest = base / 'package/autoskill/offline-manifest.json'
            manifest.parent.mkdir(parents=True)
            manifest.write_text(json.dumps({'autoskill': version}))

            def fake_api(method, path, body=None, binary=None):
                calls.append((method, path))
                if path.startswith('/releases?'):
                    return [{'id': 1, 'tag_name': tag, 'upload_url': 'https://uploads.github.com/example{?name}'}]
                if path == '/releases/1/assets':
                    return [{'id': 10, 'name': archive.name}]
                if method == 'POST':
                    return {'id': 11}
                if path == '/releases/assets/11':
                    return {'id': 11, 'size': archive.stat().st_size, 'digest': 'sha256:wrong'}
                self.fail('Unexpected mutation: ' + str((method, path)))
                return None

            env = {'RELEASE_TAG': tag, 'RELEASE_COMMIT': 'test', 'RELEASE_REPOSITORY': 'example/repo'}
            argv = ['publish', '--build', str(base), '--proof', str(proof), '--result', str(base / 'result')]
            with patch.dict(os.environ, env), patch('sys.argv', argv), patch.object(publisher, 'api', fake_api), self.assertRaisesRegex(ValueError, 'digest mismatch'):
                publisher.main()
            self.assertFalse(any(method in ('DELETE', 'PATCH') for method, _ in calls))
