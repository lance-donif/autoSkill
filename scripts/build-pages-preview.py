"""Render the dashboard into static HTML for the Pages preview using only a temporary sample project."""
from __future__ import annotations

import json
import os
import sys
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/autoskill/app"))
ORIGIN = "http://127.0.0.1:8787"
PAGES = {"runs": "index", "runs?view=results": "results", **{name: name for name in (
    "dashboard", "cases", "case-plans", "workflows", "native-assets", "conversations", "schedules",
    "project-materials", "load-tests", "diagnostics", "data")}}

# Serialize the rendered controls as HTML. Only navigable previews and appearance controls stay interactive.
SNAPSHOT = """async (pages) => {
  const entry = document.querySelector('script[type=module][src]').src;
  const { icon } = await import(new URL('./core.js', entry));
  const root = document.documentElement.cloneNode(true);
  root.querySelectorAll('script, noscript, #toasts > *, #layer > *').forEach(node => node.remove());
  for (const sheet of root.querySelectorAll('link[rel=stylesheet]')) {
    sheet.setAttribute('href', '../skills/autoskill/app/ui/css/' + sheet.href.split('/').pop());
  }
  const policy = document.createElement('meta');
  policy.httpEquiv = 'Content-Security-Policy';
  policy.content = "default-src 'none'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:; connect-src 'none'; form-action 'none'; base-uri 'none'";
  root.querySelector('head').prepend(policy);
  const script = document.createElement('script');
  script.src = 'navigation.js';
  script.defer = true;
  root.querySelector('head').append(script);
  const style = document.createElement('style');
  style.textContent = `.btn[data-preview-only]:disabled { opacity: 1; }
    [data-theme="light"] .theme-toggle svg:first-child, [data-theme="dark"] .theme-toggle svg:nth-child(2) { display: none; }`;
  root.querySelector('head').append(style);
  root.querySelector('.theme-toggle svg').replaceWith(icon('sun'), icon('moon'));
  root.querySelector('title').textContent = root.querySelector('#view h1').textContent + ' · autoSkill 界面预览';
  root.querySelector('.service-status .text').textContent = '界面预览 · 示例数据';
  root.querySelector('.service-status').removeAttribute('title');
  root.querySelector('.runtime-status').remove();
  for (const link of root.querySelectorAll('a')) {
    const url = link.getAttribute('href') || '';
    const [path, query] = url.slice(2).split('?');
    const mode = path === 'runs' && new URLSearchParams(query).get('view') === 'results' ? 'runs?view=results' : path;
    if (url.startsWith('#/') && Object.hasOwn(pages, mode)) {
      link.setAttribute('href', pages[mode] + '.html');
    } else {
      link.removeAttribute('download'); link.removeAttribute('href');
      link.setAttribute('aria-disabled', 'true'); link.title = '仅供界面预览';
    }
  }
  const tabs = root.querySelector('[aria-label="查看方式"]');
  if (tabs) [...tabs.children].forEach((tab, index) => { tab.dataset.previewHref = index ? 'results.html' : 'index.html'; });
  const liveOptions = document.querySelectorAll('option');
  [...root.querySelectorAll('option')].forEach((option, index) => option.toggleAttribute('selected', liveOptions[index].selected));
  for (const input of root.querySelectorAll('input')) {
    if (input.value && input.type !== 'password') input.setAttribute('value', input.value);
    if (input.type === 'password') input.removeAttribute('value');
  }
  for (const control of root.querySelectorAll('button:not(.theme-toggle,.nav-toggle,[data-preview-href]), select, input, textarea')) {
    if (!control.disabled) control.setAttribute('data-preview-only', '');
    control.disabled = true;
  }
  return '<!doctype html>\\n' + root.outerHTML + '\\n';
}"""


def seed(root: Path, example_url: str) -> dict:
    from autoskill import builder, db, demo, plan, projects, runner

    db.init()
    project = projects.create("示例工单系统", example_url, category="客户服务", project_id="ticket-desk")["id"]
    cases = [case for case in demo.cases() if case["id"] in ("TC-API-006", "TC-UI-005")]
    cases.extend([
        builder.api_case("TC-API-007", "校验未登录提示", "GET", "/api/tickets", {"status": 401, "json_equals": {"/code": "LOGIN_REQUIRED"}}, module="登录"),
        builder.api_case("TC-API-008", "查看指定工单", "GET", "/api/tickets/{{ticket_id}}", {"status": 200}, module="工单"),
    ])
    projects.save_cases(project, cases)
    source = root / "design.json"
    rows = [{"id": case["id"], "title": case["title"], "module": case["module"], "steps": [case["title"]], "expected": expected}
            for case, expected in zip(cases, ["显示登录表单", "返回未认证状态", "返回登录提示码", "显示工单详情"], strict=True)]
    source.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    plan.write(project, str(source))
    details = {}
    for case_id, expected in [("TC-API-008", "Blocked"), ("TC-API-007", "Fail"), ("TC-API-006", "Pass")]:
        run = runner.create(project, [case_id], headed=False)
        result = runner.execute(run["id"])
        if result["status"] != expected:
            raise RuntimeError(f"Sample {case_id} produced {result['status']}, expected {expected}")
        details["runs/" + run["id"]] = "run-" + expected.lower()
    return details


def render(root: Path, example_url: str, pages: dict) -> None:
    from autoskill import paths
    from autoskill.server.app import create_app

    with TestClient(create_app(start_worker=False), base_url=ORIGIN) as client, sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=paths.chrome(), headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1440, "height": 980})
        def serve(route):
            address = urlsplit(route.request.url)
            if address.netloc != urlsplit(ORIGIN).netloc or route.request.method != "GET":
                route.abort()
                return
            response = client.get(address.path, params=address.query)
            route.fulfill(status=response.status_code, body=response.content, content_type=response.headers.get("content-type", "text/plain"))
        page.route("**/*", serve)
        output = ROOT / "preview"
        for route, name in pages.items():
            page.goto("about:blank")
            page.goto(ORIGIN + "/#/" + route)
            expect(page.locator("#side .theme-toggle")).to_be_attached()
            expect(page.locator('#view h1')).to_be_visible()
            expect(page.locator('#view')).not_to_have_attribute('aria-busy', 'true')
            expect(page.locator('#view .skeleton')).to_have_count(0)
            expect(page.get_by_role("heading", name="没能加载", exact=True)).to_have_count(0)
            if route == "cases":
                expect(page.locator(".folder-tree")).to_be_visible()
            html = page.evaluate(SNAPSHOT, pages).replace(example_url, "https://tickets.example.invalid").replace(str(root), "/示例/autoSkill")
            html = "\n".join(line.rstrip() for line in html.split("\n"))
            (output / (name + ".html")).write_text(html, encoding="utf-8")
        browser.close()


def main() -> None:
    from autoskill.server.app import demo_site

    with TemporaryDirectory() as temporary, ExitStack() as resources:
        root = Path(temporary)
        resources.enter_context(patch.dict(os.environ, {"AUTOSKILL_WORKSPACE": str(root / "data"), "AUTOSKILL_CASE_SKILL": str(root / "design"),
            "AUTOSKILL_BRIDGE": "off", "AUTOSKILL_HEADED": "0", "AUTOSKILL_RUNTIME_CONTEXT": "", "AUTOSKILL_SESSION": "", "AUTOSKILL_WORKFLOW_BINDING": ""}))
        site = demo_site(port=0)
        resources.callback(site.server_close)
        resources.callback(site.shutdown)
        example_url = f"http://127.0.0.1:{site.server_port}"
        render(root, example_url, {**PAGES, **seed(root, example_url)})


if __name__ == "__main__":
    main()
