# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

autoSkill is an agent skill plus a local dashboard for test automation: an AI agent drives a browser once through a CLI, the session is compiled into replayable cases (UI steps and the API requests behind them), and those cases replay without AI through Playwright, Selenium or plain HTTP. It ships as an offline package and as a DeepSeek Harness (DSH) plugin.

User-facing text (CLI messages, dashboard, SKILL.md, docs) is Chinese; code comments and docstrings are English.

## Commands

Python 3.12+ is required (the bundled runtime is 3.12; check which `python` is on PATH). There is no lint or format configuration.

```bash
pip install -r requirements.txt                                   # Linux: requirements-linux.txt
python -m unittest discover -s tests -p "test_*.py"               # all Python tests
python -m unittest discover -s tests -p test_flow.py              # one file
python -m unittest discover -s tests -p test_flow.py -k one_site  # one test
node --test tests/plugin.test.mjs                                 # DSH plugin contract tests (Node >= 22)

PYTHONPATH=skills/autoskill/app python -m autoskill <command>     # run the CLI from source
PYTHONPATH=skills/autoskill/app python -m autoskill dashboard --port 8787

python scripts/fetch-windows-assets.py --assets <dir outside repo>
python scripts/build-windows-bundle.py --assets <dir> --output <new dir outside repo> --zip
python scripts/fetch-linux-assets.py --assets <dir> --arch x64|arm64
python scripts/build-linux-bundle.py --assets <dir> --output <new dir> --arch x64|arm64
```

Browser tests need a runtime named by `AUTOSKILL_CHROMIUM`, `AUTOSKILL_CHROMEDRIVER` and `AUTOSKILL_AGENT_BROWSER`. Selenium and recorder tests skip without them; the Playwright tests do not skip and error unless a browser is available. Tests set `AUTOSKILL_HEADED=0` so no window opens, and point `AUTOSKILL_WORKSPACE` at a temp folder.

## Layout

- `skills/autoskill/` is the product. `SKILL.md` is the contract the agent reads, `app/autoskill/` the Python package, `app/ui/` the dashboard (plain ES modules, no build step), `models/captcha/` the bundled captcha model. `vendor/` (bundled runtime) and `workspace/` (user data) are git-ignored.
- `plugin/` is the DSH host: `index.mjs` registers the skills and spawns the dashboard process; `client.js` shows the dashboard in an iframe with `?embed=1`.
- In the offline package the skill sits at `autoskill/`, in a checkout at `skills/autoskill/`; `findSkillDir` in the plugin and `paths.SKILL` handle both.
- `scripts/` are maintainer build and release scripts; `reports/` holds release evidence written by CI.

## Architecture

**CLI.** `cli.py` parses and dispatches; every command prints one JSON object. Exit codes: 0 ok, 1 a case failed, 2 the command did not succeed or the run was blocked. When the process is sandboxed (`paths.restricted()`), `bridge.py` forwards the same command to the dashboard service (`/api/bridge`), which runs it outside the sandbox.

**Recording.** `recorder/session.py` drives the `agent-browser` binary (`recorder/agent.py`) and keeps the in-progress recording in `workspace/.local/recording.json`. `record stop` hands the steps and captured HAR to `recorder/compile.py`, which emits one UI case plus API cases. `recorder/har.py` decides what to assert (echoed and enum-like fields, a shallow response schema) and which response values later requests reuse; those become `{{variables}}` with a `depends_on` on the case that produces them.

**Case model.** Cases are JSON files validated by `core/store.py` against `references/case.schema.json`. UI steps are an engine-independent IR defined by the `ACTIONS` and `ASSERTIONS` tables in `replay/ir.py`. Adding an action or assertion means touching `ir.py`, `replay/ui.py`, both drivers if it needs a new primitive, both generators in `replay/codegen.py` (`pw_step`, `se_step`), the recorder command in `recorder/session.py`, and `SKILL.md`.

**Replay.** `runner.py` orders the chosen cases with their prerequisites, reuses or renews the login, and records each result. `replay/ui.py` is the single step loop; `playwright_driver.py` and `selenium_driver.py` only supply primitives, so both engines are judged by the same rules. `replay/api.py` replays API cases over HTTP. A run is a row in SQLite (`db.py`) executed in its own process (`python -m autoskill.runner <run_id>`); `server/jobs.py` starts queued runs (`MAX_PARALLEL`, one per project) and `server/scheduler.py` fires scheduled ones.

**Three kinds of placeholders.** `${ENV:NAME}` is a credential (`secrets.py`), `{{name}}` is a value extracted from an earlier API response, and `{{RAND}}` is text that is new on every run so created data never collides.

**Data.** `workspace/projects/<id>/` is shareable: `project.json`, `qa-config.json`, `auth.json`, `testcases/*.json`, `knowledge/`, and a generated `SKILL.md`. `workspace/.local/` is machine-private: `autoskill.db`, `credentials.json`, `sessions/`, and run evidence under `runs/`.

**Case sheet.** `plan.py` keeps cases designed from a requirement document in an Excel file inside the sibling skill folder `autoskill-cases` (override with `AUTOSKILL_CASE_SKILL`). `record stop --plan <id>` links a recording to a row and `run` writes verdicts back. The folder is meant to be swapped for another of the same name.

**Dashboard.** `server/app.py` is a FastAPI app bound to loopback; `server/metrics.py` computes every figure in SQL from one scope so cards and lists agree. The UI uses hash routing and the `h()` helper in `ui/js/core.js`.

## Product rules set by the maintainer

- No guard rails on the user's own cases: no access-scope settings, no risk approvals, no feature limits. Do not reintroduce them. The legacy `scripts/qa.py` path in `core/store.py` still has an approvals check; `autoskill run` and the dashboard do not use it.
- Credentials and saved logins are plain text on the local machine and shown in project settings. `seal`/`unseal` in `secrets.py` exist only to read stores written by older versions.
- Browser windows are visible and maximised by default, including scheduled runs (`paths.headed()`); `--headless` or `AUTOSKILL_HEADED=0` hides them.
- One site (host and port) is one project; creating a project for a known site returns the existing one. A case's `module` is its sub-project. Projects without cases are not listed in the dashboard.
- Generated assertions should be few and stable: assert the direct result of the action, never volatile data such as totals, order, timestamps or ids.
- Pages, scripts and styles get no API case; only business requests do.

## Releases

`.github/workflows/release.yml` builds the Windows x64 and two native Linux packages on a tag and publishes them as release assets; runtime binaries never enter git history. The workflow currently skips the functional regression. `scripts/record-release-*.py` commit status documents to `main` during a release, so `main` can move underneath local work: fetch before pushing. The version lives in both `package.json` and `app/autoskill/__init__.py`.

`.gitattributes` forces LF everywhere except `*.cmd` and `*.bat`.
