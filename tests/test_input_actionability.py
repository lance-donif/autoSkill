"""Input must wait for a native modal to stop making the background inert."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))
from autoskill import builder, paths
from autoskill.replay import codegen, ui
from autoskill.replay.playwright_driver import PlaywrightDriver

HTML = """<!doctype html><meta charset=utf-8><title>Delayed modal input</title>
<input id=background value=before><select id=choice><option value=a>A</option><option value=b>B</option></select>
<output id=result></output><dialog id=modal><button id=save>Save asynchronously</button></dialog>
<script>
const background=document.querySelector('#background'), choice=document.querySelector('#choice');
const result=document.querySelector('#result'), modal=document.querySelector('#modal'), save=document.querySelector('#save');
window.realClicks=0;
background.addEventListener('click',()=>realClicks++);
choice.addEventListener('change',()=>result.textContent='modal-open:'+modal.open);
save.onclick=()=>setTimeout(()=>modal.close(),350);
modal.showModal();
</script>"""


@unittest.skipUnless(paths.chrome(), "local full Chrome required")
class InputActionabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Page(BaseHTTPRequestHandler):
            def do_GET(self):
                body = HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:" + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_background_input_waits_before_mutating_in_driver_and_standalone(self):
        reopen = {"action": "eval", "script": "() => document.querySelector('#modal').showModal()"}
        save = {"action": "click", "selector": "#save"}
        actions = [save, {"action": "fill", "selector": "#background", "value": "after"},
            {"assertion": "value", "selector": "#background", "expected": "after"},
            reopen, save, {"action": "type", "selector": "#background", "value": "-typed"},
            {"assertion": "value", "selector": "#background", "expected": "after-typed"},
            reopen, save, {"action": "focus", "selector": "#background"},
            {"action": "keyboard", "operation": "inserttext", "value": "-focused"},
            {"assertion": "value", "selector": "#background", "expected": "after-typed-focused"},
            reopen, save, {"action": "select", "selector": "#choice", "value": "b"},
            {"assertion": "text", "selector": "#result", "expected": "modal-open:false"},
            {"action": "eval", "script": "() => document.querySelector('#result').textContent = String(window.realClicks)"},
            {"assertion": "text", "selector": "#result", "expected": "0"}]
        case = builder.ui_case("TC-UI-001", "Native modal delayed background input", {
            "start_url": self.base, "actions": actions, "assertions": []})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = {"timeout_seconds": 3, "page_load_seconds": 5}
            previous = os.environ.get("AUTOSKILL_HEADED")
            os.environ["AUTOSKILL_HEADED"] = "0"
            try:
                driver = PlaywrightDriver(config, headless=True)
                try:
                    driver.reset()
                    result = ui.run_case(driver, case, config, root, "modal.png")
                    self.assertEqual(result["status"], "Pass", result)
                finally:
                    driver.close()
                script = root / "modal_playwright.py"
                script.write_text(codegen.script([case], self.base, "playwright"), encoding="utf-8")
                done = subprocess.run([sys.executable, str(script)], cwd=root,
                    env={**paths.child_env(), "CHROME_BINARY": paths.chrome(), "HEADED": "0", "TIMEOUT": "3"},
                    capture_output=True, text=True, timeout=30)
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertIn("PASS", done.stdout)
            finally:
                if previous is None:
                    os.environ.pop("AUTOSKILL_HEADED", None)
                else:
                    os.environ["AUTOSKILL_HEADED"] = previous


if __name__ == "__main__":
    unittest.main()
