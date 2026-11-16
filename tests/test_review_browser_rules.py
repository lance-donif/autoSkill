"""Broken local imports must be caught before the browser opens an empty dashboard."""
import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('browser_dashboard_rule', ROOT / 'scripts/review_rules/browser_dashboard.py')
RULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RULE)


class BrowserDashboardRuleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ui = self.root / 'skills/autoskill/app/ui'
        (self.ui / 'js/pages').mkdir(parents=True)
        (self.ui / 'index.html').write_text('<script type="module" src="/js/main.js"></script>', encoding='utf-8')
        (self.ui / 'js/main.js').write_text("import {render} from './pages/example.js';", encoding='utf-8')
        (self.ui / 'js/pages/example.js').write_text('export const render = () => {};', encoding='utf-8')

    def test_complete_local_graph_ignores_comment_and_text_import_examples(self):
        file = self.ui / 'js/pages/example.js'
        file.write_text("// import './missing.js';\nconst example = \"import './absent.js';\";\nexport const render = () => {};", encoding='utf-8')
        self.assertEqual(RULE.run(self.root), [])

    def test_missing_dynamic_and_transitive_imports_are_actionable(self):
        file = self.ui / 'js/pages/example.js'
        file.write_text("const render = () => import('./missing.js');", encoding='utf-8')
        findings = RULE.run(self.root)
        self.assertEqual(len(findings), 1, findings)
        self.assertEqual(findings[0]['path'], file.relative_to(self.root).as_posix())
        self.assertEqual(findings[0]['line'], 1)
        self.assertIn('./missing.js', findings[0]['message'])

    def test_absolute_module_and_missing_entry_assets_are_rejected(self):
        (self.ui / 'js/main.js').write_text("import '/js/pages/example.js';", encoding='utf-8')
        with (self.ui / 'index.html').open('a', encoding='utf-8') as file:
            file.write('<link rel="stylesheet" href="/css/missing.css">')
        findings = RULE.run(self.root)
        self.assertEqual(len(findings), 2, findings)
        self.assertTrue(all(row['severity'] == 'error' and row['line'] > 0 for row in findings))


if __name__ == '__main__':
    unittest.main()
