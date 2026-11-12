"""Linux credentials and saved browser sessions must be encrypted, not just encoded."""
import base64
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/autoskill/app'))
__import__("os").environ.setdefault("AUTOSKILL_HEADED", "0")  # tests never open browser windows
from autoskill import paths, secrets


@unittest.skipIf(os.name == 'nt', 'Linux local key store')
class LinuxSecretsTests(unittest.TestCase):
    def test_roundtrip_requires_private_local_key(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, AUTOSKILL_WORKSPACE=folder):
            value = 'private password and session cookie'
            blob = secrets.seal(value)
            self.assertNotIn(value.encode(), base64.b64decode(blob))
            self.assertEqual(secrets.unseal(blob), value)
            key = paths.local() / 'encryption.key'
            self.assertEqual(key.stat().st_mode & 0o777, 0o600)
            key.unlink()
            with self.assertRaises(ValueError):
                secrets.unseal(blob)
            self.assertFalse(key.exists())

    def test_old_base64_fallback_is_not_silently_treated_as_encrypted(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, AUTOSKILL_WORKSPACE=folder):
            secrets.seal('create local key')
            legacy = base64.b64encode(base64.b64encode(b'old plaintext')).decode()
            with self.assertRaises(ValueError):
                secrets.unseal(legacy)
