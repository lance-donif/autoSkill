import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('windows_entry', ROOT / 'skills/autoskill/scripts/windows_entry.py')
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)


class IntranetTests(unittest.TestCase):
    def test_portable_environment_disables_dependency_downloads_and_telemetry(self):
        env = entry.environment()
        self.assertEqual(env['PIP_NO_INDEX'], '1')
        self.assertEqual(env['PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD'], '1')
        self.assertEqual(env['OTEL_SDK_DISABLED'], 'true')
        self.assertEqual(env['DO_NOT_TRACK'], '1')
        self.assertEqual(env['npm_config_offline'], 'true')
        self.assertTrue(env['AUTOSKILL_CHROMIUM'].endswith('chrome.exe'))
        self.assertIn(str(entry.VENDOR / 'node'), env['PATH'])

    def test_windows_launcher_never_bootstraps_online(self):
        cmd = (ROOT / 'skills/autoskill/run-windows.cmd').read_text(encoding='utf-8')
        self.assertIn('vendor\\windows-x64', cmd)
        for forbidden in ['curl ', 'wget ', 'pip install', 'npm install', 'npx ']:
            self.assertNotIn(forbidden, cmd)

    def test_browser_background_services_and_service_workers_disabled(self):
        source = (ROOT / 'runtime/engines.py').read_text(encoding='utf-8')
        for flag in ['--disable-component-update', '--disable-background-networking', '--disable-domain-reliability', '--no-pings']:
            self.assertIn(flag, source)
        self.assertIn("service_workers='block'", source)

    def test_ai_has_no_default_cloud_route(self):
        source = (ROOT / 'plugin/ai-worker.mjs').read_text(encoding='utf-8')
        self.assertIn('AUTOSKILL_AI_PROVIDER', source)
        self.assertIn('AUTOSKILL_AI_MODEL', source)
        self.assertIn('if(!command||!provider||!model)', source)
        self.assertNotIn("profile:request.profile??'sdk'", source)

    def test_missing_and_modified_bundled_dependency_fails_verification(self):
        import hashlib, json, tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            file = root / 'binary.exe'
            file.write_bytes(b'original')
            (root / 'runtime-manifest.json').write_text(json.dumps({'files': {'binary.exe': hashlib.sha256(b'original').hexdigest()}}))
            with patch.object(entry, 'VENDOR', root):
                self.assertEqual(entry.verify_bundle(), 1)
                file.write_bytes(b'changed')
                with self.assertRaisesRegex(ValueError, 'changed'):
                    entry.verify_bundle()
                file.unlink()
                with self.assertRaisesRegex(ValueError, 'Missing'):
                    entry.verify_bundle()
