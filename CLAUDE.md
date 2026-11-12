# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

autoSkill is an agent skill plus a local dashboard for test automation: an AI agent drives a browser once through a CLI, the session is compiled into replayable cases (UI steps and the API requests behind them), and those cases replay without AI through Playwright, Selenium or plain HTTP. It ships as an offline package and as a DeepSeek Harness (DSH) plugin.

User-facing text (CLI messages, dashboard, SKILL.md, docs) is Chinese; code comments and docstrings are English.

## Commands

Python 3.12+ is required (the bundled runtime is 3.12; check which `python` is on PATH). 

```bash
pip install -r requirements.txt                                   # Linux: requirements-linux.txt
python -m unittest discover -s tests -p "test_*.py"               # all Python tests
python -m unittest discover -s tests -p test_flow.py              # one file
python -m unittest discover -s tests -p test_flow.py -k one_site  # one test
node --test tests/plugin.test.mjs                                 # DSH plugin contract tests (Node >= 22)

ruff check .                                                      # Python static checks (ruff.toml)
npm_config_offline=false npx --yes @biomejs/biome@2.6.7 lint      # JS and CSS static checks (biome.json)
python -m unittest discover -s tests -p test_conventions.py       # project conventions, standard library only

PYTHONPATH=skills/autoskill/app python -m autoskill <command>     # run the CLI from source
PYTHONPATH=skills/autoskill/app python -m autoskill dashboard --port 8787

python scripts/fetch-windows-assets.py --assets <dir outside repo>
python scripts/build-windows-bundle.py --assets <dir> --output <new dir outside repo> --zip
python scripts/fetch-linux-assets.py --assets <dir> --arch x64|arm64
python scripts/build-linux-bundle.py --assets <dir> --output <new dir> --arch x64|arm64
```

Browser tests need a runtime named by `AUTOSKILL_CHROMIUM`, `AUTOSKILL_CHROMEDRIVER` and `AUTOSKILL_AGENT_BROWSER`. Selenium and recorder tests skip without them; the Playwright tests do not skip and error unless a browser is available. Tests set `AUTOSKILL_HEADED=0` so no window opens, and point `AUTOSKILL_WORKSPACE` at a temp folder.

## Static checks

`.github/workflows/lint.yml` runs the three checks above on every push and pull request. New code must pass them.

- Do not weaken `ruff.toml` or `biome.json`, and do not add blanket suppressions. When a rule is wrong for one line, suppress that line with the rule name and a reason (`# noqa: T201  # the command's JSON output`, `// biome-ignore lint/<group>/<rule>: <reason>`).
- Neither tool formats this code base; do not run `ruff format` or `biome format` over existing files.
- `tests/test_conventions.py` holds the rules a general linter cannot see: colours only in `ui/css/tokens.css`, every `var(--token)` defined there, every IR action and assertion named and handled by both script generators, every CLI command documented in `SKILL.md` or `references/commands.md`, English code comments, LF and no tabs.

## Layout

- `skills/autoskill/` is the product. `SKILL.md` is the contract the agent reads, `app/autoskill/` the Python package, `app/ui/` the dashboard (plain ES modules, no build step), `models/captcha/` the bundled captcha model. `vendor/` (bundled runtime) and `workspace/` (user data) are git-ignored.
- `plugin/` is the DSH host: `index.mjs` registers the skills and spawns the dashboard process; `client.js` shows the dashboard in an iframe with `?embed=1`.
- In the offline package the skill sits at `autoskill/`, in a checkout at `skills/autoskill/`; `findSkillDir` in the plugin and `paths.SKILL` handle both.
- `scripts/` are maintainer build and release scripts; `reports/` holds release evidence written by CI.

## Architecture

**CLI.** `cli.py` parses and dispatches; every command prints one JSON object. Exit codes: 0 ok, 1 a case failed, 2 the command did not succeed or the run was blocked. When the process is sandboxed (`paths.restricted()`), `bridge.py` forwards the same command to the dashboard service (`/api/bridge`), which runs it outside the sandbox.

**Recording.** `recorder/session.py` drives the `agent-browser` binary (`recorder/agent.py`). The default session keeps its recording in `workspace/.local/recording.json`; `--session` or `AUTOSKILL_SESSION` selects an independent browser and recording under `.local/recordings/<hash>/`. Keep the same identity for every command in one AI task. `record stop` hands the steps and captured HAR to `recorder/compile.py`, which emits one UI case plus API cases. `recorder/har.py` decides what to assert (echoed and enum-like fields, a shallow response schema). Correlated response values become `{{variables}}` with a dependency on the matching resource's producer; ambiguous values stay literal with a compile note.

**Case model.** Cases are JSON files validated by `core/store.py` against `references/case.schema.json`. UI steps are an engine-independent IR defined by the `ACTIONS` and `ASSERTIONS` tables in `replay/ir.py`. Adding an action or assertion means touching `ir.py`, `replay/ui.py`, both drivers if it needs a new primitive, both generators in `replay/codegen.py` (`pw_step`, `se_step`), the recorder command in `recorder/session.py`, and `SKILL.md`.

**Replay.** `runner.py` orders the chosen cases with their prerequisites, reuses or renews the login, and records each result. `replay/ui.py` is the single step loop; `playwright_driver.py` and `selenium_driver.py` only supply primitives, so both engines are judged by the same rules. `replay/api.py` replays API cases over HTTP. A run is a row in SQLite (`db.py`) atomically claimed by one executor, with its task identity and login preference persisted. `server/jobs.py` starts child processes and recovers exited workers; `server/scheduler.py` fires scheduled ones. `AUTOSKILL_MAX_PARALLEL` sets the process budget (default 2); runs sharing a project's saved login queue behind each other.

**Three kinds of placeholders.** `${ENV:NAME}` is a credential (`secrets.py`), `{{name}}` is a value extracted from an earlier API response, and `{{RAND}}` is text that is new on every run so created data never collides.

**Data.** `workspace/projects/<id>/` is shareable: `project.json`, `qa-config.json`, `auth.json`, `testcases/*.json`, `knowledge/`, and a generated `SKILL.md`. `workspace/.local/` holds `autoskill.db`, `credentials.json`, `sessions/`, and run evidence under `runs/`. Reentrant process/thread locks in `locking.py` protect shared project operations and credential/login merges; acquire project locks before SQLite write transactions. Full user-created packs intentionally include credentials and saved logins at the standard workspace path, but omit run history and temporary process files. Ordinary project packs keep their existing optional encrypted credential export.

**Case sheet.** `plan.py` keeps cases designed from a requirement document in an Excel file inside the sibling skill folder `autoskill-cases`; named sessions use `.local/case-plans/<hash>/autoskill-cases`. `AUTOSKILL_CASE_SKILL` overrides either default. A design row is not executable until an AI records actual actions and assertions and links it with `record stop --plan <id>`. `run` writes current-revision verdicts back. The folder is meant to be swapped for another of the same name.

**Dashboard.** `server/app.py` is a FastAPI app bound to loopback; `server/metrics.py` computes every figure in SQL from one scope so cards and lists agree. The UI uses hash routing and the `h()` helper in `ui/js/core.js`.

## Product rules set by the maintainer

- No guard rails on the user's own cases: no access-scope settings, no risk approvals, no feature limits. Do not reintroduce them. The legacy `scripts/qa.py` path in `core/store.py` still has an approvals check; `autoskill run` and the dashboard do not use it.
- Credentials and saved logins are plain text on the local machine and shown in project settings. Sharing them in `pack --full` is intentional; describe what the pack includes accurately, without adding approvals or a passphrase requirement. `seal`/`unseal` in `secrets.py` exist only to read stores written by older versions.
- Browser windows are visible and maximised by default, including scheduled runs (`paths.headed()`); `--headless` or `AUTOSKILL_HEADED=0` hides them.
- One site (host and port) is one project; creating a project for a known site returns the existing one. A case's `module` is its sub-project. Projects without cases are not listed in the dashboard.
- Generated assertions should be few and stable: assert the direct result of the action, never volatile data such as totals, order, timestamps or ids.
- Pages, scripts and styles get no API case; only business requests do.
- Playwright replay and recording use the user's installed Chrome by default and fall back to the bundled browser when it is missing or will not start (`paths.chrome()`, `autoskill settings --browser`, `AUTOSKILL_BROWSER`). Selenium always uses the bundled browser because the driver must match its version.
- The dashboard follows Apple platform conventions: one accent colour, neutral grouped backgrounds, translucent materials for menus and the toolbar, floating inspector panels, spring-like motion. Replay preferences live in one place, the toolbar's 回放设置 popover; do not scatter per-button options.

## Releases

`.github/workflows/release.yml` builds the Windows x64 and two native Linux packages on a tag and publishes them as release assets; runtime binaries never enter git history. The workflow currently skips the functional regression. `scripts/record-release-*.py` commit status documents to `main` during a release, so `main` can move underneath local work: fetch before pushing. The version lives in both `package.json` and `app/autoskill/__init__.py`.

`.gitattributes` forces LF everywhere except `*.cmd` and `*.bat`.
