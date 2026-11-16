"""Ambiguous recorded business identities explain the problem without passing negative checks."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_business_locators import BusinessSite

from autoskill import paths
from autoskill.replay import codegen, ui
from autoskill.replay.playwright_driver import DOM_JS, PlaywrightDriver
from autoskill.replay.selenium_driver import SeleniumDriver


class BusinessLocatorDiagnostics(unittest.TestCase):
    def check_engine(self, driver_class):
        with BusinessSite() as site:
            site.rows = [{'id': 'wanted', 'name': '指定商品'}, {'id': 'other', 'name': '其他商品'}]
            driver = driver_class(site.config(), headless=True)
            try:
                driver.reset()
                driver.goto(site.url)
                script = '(' + DOM_JS + ')(document).describe(document.querySelector(".entry button")).locators'
                locators = (driver.page.evaluate('() => ' + script) if driver_class is PlaywrightDriver
                            else driver.wd.execute_script('return ' + script))
                site.rows[1]['name'] = '指定商品'
                driver.goto(site.url)
                with self.assertRaisesRegex(ui.StepError, '业务目标不唯一：'):
                    ui.perform(driver, {'action': 'click', 'locators': locators}, '', [], 0)
                for assertion in ('hidden', 'count'):
                    result = ui.check(driver, {'assertion': assertion, 'expected': 0, 'locators': locators}, 0)
                    self.assertFalse(result['passed'], result)
                    self.assertIn('业务目标不唯一：', result['actual'])
                    self.assertNotIn('ElementHandle', result['actual'])
                    self.assertNotIn('javascript error', result['actual'])
                self.assertEqual(site.hits, [])
                generic = ui.check(driver, {'assertion': 'hidden', 'locators': [{'by': 'xpath', 'value': '/'}]}, 0)
                self.assertFalse(generic['passed'])
                self.assertIn('XPath 结果', generic['actual'])
                if driver_class is PlaywrightDriver:
                    self.check_exports(site, locators)
                site.rows = [{'id': 'other', 'name': '其他商品'}]
                driver.goto(site.url)
                self.assertTrue(ui.check(driver, {'assertion': 'hidden', 'locators': locators}, 0)['passed'])
            finally:
                driver.close()

    def check_exports(self, site, locators):
        case = {'id': 'UI-AMBIGUOUS', 'kind': 'ui', 'title': '操作指定商品', 'extensions': {'automation': {
            'start_url': '/', 'actions': [{'action': 'click', 'locators': locators}], 'assertions': []}}}
        with tempfile.TemporaryDirectory() as directory:
            for engine in ('playwright', 'selenium'):
                file = Path(directory) / (engine + '.py')
                file.write_text(codegen.script([case], site.url, engine), encoding='utf-8')
                environment = {**os.environ, 'PYTHONPATH': '', 'HEADED': '0', 'TIMEOUT': '0',
                               'CHROME_BINARY': paths.chrome(), 'CHROMEDRIVER': paths.chromedriver() or ''}
                result = subprocess.run([sys.executable, '-I', str(file)], cwd=directory, env=environment,
                                        capture_output=True, text=True, timeout=30, check=False)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn('业务目标不唯一：', result.stderr)
                self.assertEqual(site.hits, [])

    def test_playwright_and_exports_keep_ambiguity_distinct_from_absence(self):
        self.check_engine(PlaywrightDriver)

    @unittest.skipUnless(paths.chrome() and paths.chromedriver(), 'matching Chromium and chromedriver required')
    def test_selenium_keeps_ambiguity_distinct_from_absence(self):
        self.check_engine(SeleniumDriver)


if __name__ == '__main__':
    unittest.main()
