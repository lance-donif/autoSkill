# CLAUDE.md

Development and delivery guidance for agents working in this repository. You are the owner and long-term maintainer of this codebase. User-facing text (CLI messages, dashboard, `SKILL.md`, `docs/`) is Chinese; Python code comments and docstrings are English.

## Commands

Run from checkout root. Development requires Python 3.12+ (CI and offline bundles pin Python 3.12), Node 22+ for the DSH plugin, and pinned dependencies (`requirements.txt` on Windows, `requirements-linux.txt` on Linux).

```bash
python -m unittest discover -s tests -p "test_*.py"
PYTHONPATH=tests python -m unittest -v test_flow.FlowTests.test_one_site_is_one_project
node --test tests/*.test.mjs
python scripts/code_review.py check --rule repository
python scripts/validate_review.py --list
review_logs=$(mktemp -d)
python scripts/validate_review.py --suite workflow --suite knowledge --suite host --suite environments --output-dir "$review_logs"

PYTHONPATH=skills/autoskill/app python -m autoskill --help
PYTHONPATH=skills/autoskill/app python -m autoskill dashboard --port 8787
```

Browser and host test env vars:
- `AUTOSKILL_CHROMIUM`: Chromium binary for dashboard E2E (`paths.chrome()`).
- `AUTOSKILL_CHROMEDRIVER` / `AUTOSKILL_AGENT_BROWSER`: matching driver and native recorder binary for Selenium/recording suites.
- Linux visible-window tests require X11 (`xwininfo`, `xprop`). `AUTOSKILL_DSH_SDK_ROOT` enables Node SDK integration checks. Always inspect skip counts when reporting coverage.

## Static checks

PRs, `main` pushes, and manual dispatches run `.github/workflows/lint.yml` (`ruff`, `@biomejs/biome`, `test_conventions.py`, review tools, workflow/knowledge/host/environment contracts, report/load regressions, Landmines, Windows reports, Chromium dashboard E2E, and advisory CLS). See [Python code review and validation](docs/Python代码审查与验证.md).

- Do not weaken `ruff.toml` or `biome.json`, or add blanket suppressions. When a single line legitimately needs a bypass, suppress that line with rule name + reason (`# noqa: T201  # JSON output`, `// biome-ignore lint/<group>/<rule>: <reason>`).
- Never run `ruff format` or `biome format` over existing files.
- Landmines (`.github/landmines`, Node 22.18+ or 24.11+): run `npm run lint:landmines` locally (`-- --deny-warnings` for strict mode).
- `tests/test_conventions.py` enforces CSS token hygiene (`ui/css/tokens.css`), compact navigation reservation, IR step/codegen completeness, CLI docs coverage, plain-text credentials (`no type: 'password'`), no `approvals`, clean LF/whitespace, and English-only Python comments.

## Repository layout

- `skills/autoskill/`: product bundle (`SKILL.md` agent contract, `references/`, `app/autoskill/` Python kernel, `app/ui/` plain ES modules without a build step, `models/captcha/` ONNX assets). `vendor/` is git-ignored. Runtime data defaults to `~/autoSkill-data` (Windows `%USERPROFILE%/autoSkill-data`) unless overridden by `AUTOSKILL_WORKSPACE`.
- `plugin/`: DeepSeek Harness (DSH) integration (`index.mjs` registers skill & sidebars and starts dashboard; `host-adapter.mjs` handles durable `host_outbox`; `client.js` provides collapsed inline tool results & sidebar iframes). In offline packages, `skills/autoskill/` sits at `autoskill/` (`findSkillDir` and `paths.SKILL` support both).
- `docs/`: active usage, operations, design maintenance, alignment, and release notes.
- `scripts/`: maintainer review, static validation, Pages preview generator, asset fetchers, native bundlers, and release scripts.
- `preview/`: standalone GitHub Pages static demo (never packaged into offline bundles).
- `reports/`: active verification ledger (`交付验证报告.md`), release metadata (`offline-package.json`, `release-status.json`), current release proofs (`evidence/releases/`), and the hashed Pages preview fixture.

## Architecture contracts

### CLI, bridge & state projection
- `cli.py` emits one JSON object per invocation (exit 0: query/queued/pass; 1: failed sync run/assertion/`doctor`/`verify`; 2: command error/blocked run).
- Restricted host sandboxes forward commands via `bridge.py` → `/api/bridge` with separate workspace, host session, browser session, and workflow binding.
- `task_state.py` projects lifecycle, phase, `case_outcome` (`pass` / `fail` / `not_run`), evidence validity, reports, and delivery consistently for CLI and dashboard (dispatcher loop 200 ms, dashboard poll 250 ms). Never turn a declared failure green by loosening its oracle.

### Workflow & requirement design delivery
- `workflow.py` drives `sources → requirements → design → coverage/completion → materialize → execute → diagnose/maintain → knowledge`. Lease tokens fence stale submissions; idempotency keys guard requests; `revise` creates a new cycle without erasing prior failures; `projects.save_cases(expected_hashes=...)` publishes only verified passing candidates.
- **14-column Excel & `autoSkill交付/`**: `plan write` generates the 11-column internal sheet, public `delivery_excel` (`功能测试用例.xlsx` with risk priorities `P0`/`P1`/`P2`), and preserved `initial_excel` under `paths.case_skill()` (`.local/case-plans/...`). `deliverables.py` and `delivery_layout.py` deliver only finished workbooks and reports to `autoSkill交付/` (single-requirement at root; multiple requirements migrate into per-requirement subdirs), retain latest-only Excel, append `-2`/`-3` suffixes for same-minute `测试报告-` / `Bug清单-` collisions, detect Windows open locks (`code="file_in_use"`), and compare human edits (`manual_edit_conflict`) rather than silently overwriting.
- `task_reports.py` and `task_report_data.py` maintain frozen task snapshots (`ready` requires valid readback of Excel + concise Markdown + Bug Markdown) and full 6-status counts (`Pass`, `Fail`, `Blocked`, `NotRun`, `Flaky`, `Unverified`).

### Recording, case model & replay engines
- `recorder/session.py` drives `agent-browser`; `--session` or `AUTOSKILL_SESSION` isolates browser windows and recordings. `record start --reuse` replays a fixed UI prerequisite before capturing new steps. `recorder/capture.py` keeps raw CDP/HAR payloads for conversation assets (`conversation build`).
- `core/store.py` validates cases against `references/case.schema.json`. Adding an IR action or assertion requires updating `replay/ir.py`, `replay/ui.py`, drivers, `replay/codegen.py`, `recorder/session.py`, and `SKILL.md`.
- `runner.py` & `execution_inputs.py` freeze definitions, dependencies, environment inputs, and engine identity at enqueue. `replay/ui.py` and `replay/api.py` execute UI (Playwright/Selenium) and HTTP steps; `codegen.py` embeds shared locator/context/assertion logic plus `MISSING_VARIABLE` / `_require_resolved` for standalone scripts.
- `native_assets.py` & `native_run.py` manage Selenium pytest/unittest, Postman/Newman, Apifox, Locust load/pressure runs, and `ai_eval` multi-turn/audio tests (with `dialogue_judge.py` host judging). Missing third-party CLIs report `Blocked`.

### Environments, platform accounts, queueing & variables
- `projects.py` stores `environments[]` (`id`, `name`, `base_url`, `variables`, `login_required`, `auth_mode`, `platform_id`, `platform_name`, `entry`, `account_ids`, `default_account_id`, `is_default`) plus `active_environment_id` and `active_account_id` in `qa-config.json`. All environments are equal (`login_required` decides whether account login applies; never special-case "local").
- `platform_accounts.py` manages shared `.local/platform-accounts.json` platforms and accounts. Saved login caches are isolated per platform + environment + account. Entry modes (`direct`, `redirect`, `nav`) verify target arrival and halt with `ENTRY_NOT_FOUND` + screenshot on mismatch. Cases can override the default account; `sessions.account_lock_key` serializes runs sharing the same `(platform_id, environment_id, account_id)` while global `AUTOSKILL_MAX_PARALLEL` defaults to 2.
- Schedules persist independent `environment_id` and `account_id` via `server/scheduler.py` and `server/jobs.py`.
- Variables: `${ENV:NAME}` resolves stored or process credentials. `{{name}}` expands in HTTP and UI steps/entries with precedence `explicit run inputs > prerequisite extracts > environment variables`; `{{RAND}}` is frozen per run. Missing `{{name}}` triggers preflight errors (`/api/projects/{id}/preflight`, `/api/runs`, schedules, and standalone scripts via `_require_resolved`).

### Sync, conflict resolution & single-ZIP error packs
- `sync_import.py` scans `AUTOSKILL_SYNC_DIR` for format-3 single-ZIP sync packs (`<project>-<YYYYMMDD-HHMM>-<hex>.zip`), defers in-flight uploads (`AUTOSKILL_SYNC_STABLE_SECONDS`), imports only passing cases, uses immutable `extensions.internal_id` to prevent cross-developer collisions, and preserves center-local environments/accounts by default.
- `sync_conflicts.py` & `server/sync.py` expose split conflict resolution (`/api/sync/conflicts/{id}/resolve` for independent `cases` / `environments` / `accounts` / `auth` choices) and side-by-side ZIP download (`/api/sync/conflicts/{id}/export-both`).
- `failure_archive.py` archives failed/blocked runs to `AUTOSKILL_ERROR_DIR` as single sanitized ZIPs (`<project>-<run_id>-errors.zip`, zero plaintext passwords) and serves them on `GET /api/runs/{id}/error-pack`. Passing runs write no error pack.

## Product & UI rules

- **No approval gates**: Users run their own cases as written (`test_conventions.py` forbids `approvals` in dashboard JS and `replay/api.py`).
- **Plain-text credentials**: Store and display credentials in plain text inside Project Settings (`/api/platform-accounts`, sync ZIPs); forbid `type: 'password'` in UI JS; never leak passwords into reports, run history, `/api/projects`, or error ZIPs.
- **Browser lifecycle & preference**: `paths.headed()` resolves saved `dashboard.headed` (visible & maximized by default), overridden per-call by `--headed`/`--headless` or `AUTOSKILL_HEADED=1/0`. Scheduled runs preserve per-task browser mode. Playwright prefers installed Chrome with bundled fallback (`AUTOSKILL_BROWSER`); Selenium uses the configured Chrome/ChromeDriver pair.
- **One site = one project**: `project create --name` supports requirement-stage projects without an address (`--url` added later before execution). Re-writing the identical `--source-doc` content in one session returns `duplicate: true` without creating a second delivery.
- **Apple-style UI & minimal copy**: Single accent color from `ui/css/tokens.css`, translucent bars, compact alignment rules ([Dashboard alignment](docs/仪表盘对齐线.md)), and no intro paragraphs, redundant tooltips, or extra management pages.
- **Dashboard structure**:
  - **Cases**: switch environment & account directly beside each project header (account picker follows `login_required`, no duplicate scope-bar env picker).
  - **Project Settings drawer**: wide two-tab drawer (`基础与登录` and `变量 (count)` supporting search, empty-only filter, inline table editing, copy-keys on new env, and `表格 | 批量文本` `KEY=VALUE` toggle).
  - **Execution Records**: show actual project/environment/account, full 6-metric counts, step/HTTP paired evidence, standalone HTML parity, and single-ZIP error pack download (`ui/js/sync-conflict-drawer.js` handles split sync conflict resolution on Data Management).
  - **Load testing**: under 执行记录 (`压测记录`) with `性能测试` (3 numeric inputs: 并发数, 持续时间（秒）, 目标请求数) and `压力测试` (staged Locust execution + Apache JMeter 5.6.3 calculations & charts).
- Update matching Chromium E2E tests (`test_pages_browser`, `test_dashboard_palette`, `test_pages_load`, `test_load_dashboard`, `test_environments_dashboard`, `test_platform_ui_e2e`) in any commit changing UI behavior.

## Releases

Pushing a `v*` tag or dispatching `.github/workflows/release.yml` on `main` builds Windows x64, Linux x64, and Linux ARM64 offline archives, verifies ZIP/CRC/inventories, executes required Linux x64 browser/recorder/knowledge/native/load tests without skips, publishes all 3 ZIPs + validation log, and commits release evidence back to `main` (always `git pull --rebase` before pushing afterwards). Keep `package.json` and `skills/autoskill/app/autoskill/__init__.py` at the same version.
