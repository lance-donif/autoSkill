# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

autoSkill is a local test system operated through an agent skill, CLI and dashboard. A host AI turns sources into requirements, scenarios and executable cases through a durable workflow; the kernel validates references, runs frozen inputs, preserves evidence and publishes validated changes. Browser recording and Playwright/Selenium/HTTP replay remain available directly. Native adapters preserve existing Python, Postman/Apifox and JMeter projects; the AI evaluation engine handles multi-turn HTTP/SSE/WebSocket conversations and asks the host for independent semantic judgements. It ships as an offline package and as a DeepSeek Harness (DSH) plugin. Model configuration belongs to the host, not a second SDK inside the kernel.

User-facing text (CLI messages, dashboard, SKILL.md, docs) is Chinese; code comments and docstrings are English.

## Commands

Python 3.12+ is required (the bundled runtime is 3.12; check which `python` is on PATH).

```bash
pip install -r requirements.txt                                   # Linux: requirements-linux.txt
python -m unittest discover -s tests -p "test_*.py"               # all Python tests
python -m unittest discover -s tests -p test_flow.py              # one file
python -m unittest discover -s tests -p test_flow.py -k one_site  # one test
node --test tests/plugin.test.mjs tests/host-adapter.test.mjs     # DSH SDK contract substitutes (Node >= 22)

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
- `plugin/` is the DSH host: `index.mjs` registers skills, starts the dashboard and supplies runtime context; `host-adapter.mjs` delivers durable work to host sessions; `client.js` shows the dashboard in an iframe with `?embed=1`.
- In the offline package the skill sits at `autoskill/`, in a checkout at `skills/autoskill/`; `findSkillDir` in the plugin and `paths.SKILL` handle both.
- `scripts/` are maintainer build and release scripts; `reports/` holds review and validation evidence. Keep raw authentication and network captures out of public reports.

## Architecture

**CLI and services.** `cli.py` parses and dispatches; every command prints one JSON object. Exit codes: 0 ok, 1 a case failed, 2 the command did not succeed or the run was blocked. CLI and HTTP call the same application services. When the process is sandboxed (`paths.restricted()`), `bridge.py` forwards structured arguments and explicitly selected attachments to `/api/bridge`. Resolve file paths at the caller; preserve attachment content identity across temporary transport paths. Runtime context carries workspace, host session, browser session and workflow binding separately. Paginated service responses expose their cursor/snapshot contract; do not reconstruct full definitions from display summaries.

**Workflow.** `workflow.py` and its state/definition/execution/recovery modules implement sources → requirements → design → coverage/completion → materialize → execute → diagnose/maintain → knowledge. Host AI claims `next` work and submits proposals; deterministic stages and validation run in the kernel. Workflow, item, attempt and operation IDs are independent. Lease tokens fence stale submissions; idempotency keys are bound to request content. Long tasks use heartbeat/release. `revise` starts a source-backed new cycle without erasing old failures. A completed task can have a violated or incomplete business conclusion. Candidates stay staged while every required repetition executes; maintenance publishes only satisfied candidates through a locked content-hash comparison. Initial materialization may retain a verified failing case that exposes a real defect. Never change an oracle to match an observed bug merely to turn the run green.

**Recording.** `recorder/session.py` drives the `agent-browser` binary (`recorder/agent.py`). The default session keeps its recording in `workspace/.local/recording.json`; `--session` or `AUTOSKILL_SESSION` selects an independent browser and recording under `.local/recordings/<hash>/`. Keep the same identity for every command in one AI task. Use the task's explicit workflow binding when recording managed work; the recorder captures that binding at start. Operation intents precede external mutations and receipts follow them; unknown effects require evidence-based recovery, not blind replay after lease expiry. Failed checks retain their declared expected value, actual observation and verdict. `record stop` compiles UI steps and business API traffic. HAR-derived checks are observations, not independent requirement oracles. Correlated response values become `{{variables}}` with a dependency on the matching resource's producer; ambiguous values stay literal with a compile note. `recorder/capture.py` preserves supplementary CDP traffic separately from original HAR, including available stream bytes and authentication. `conversation build` maps explicit captured fields into multi-turn assets; business expectations must be supplied independently.

**Case model.** Cases are JSON files validated by `core/store.py` against `references/case.schema.json`. UI steps are an engine-independent IR defined by the `ACTIONS` and `ASSERTIONS` tables in `replay/ir.py`. Adding an action or assertion means touching `ir.py`, `replay/ui.py`, both drivers if it needs a new primitive, both generators in `replay/codegen.py` (`pw_step`, `se_step`), the recorder command in `recorder/session.py`, and `SKILL.md`.

**Execution and evidence.** `runner.prepare` freezes cases, dependency closure, configuration, credentials, domain inputs and engine identity before `enqueue`; `execution_inputs.py` verifies and supplies that snapshot to execution. Candidate overrides do not write live cases. Results bind case content and execution scope hashes to actual attempt receipts and artifacts; hashes alone do not prove execution. `result_evidence` verifies frozen local evidence, while `result_current` additionally compares current inputs. Every retry keeps a separate attempt and screenshots. `replay/ui.py` is the common step loop; browser drivers supply primitives, and `replay/api.py` handles HTTP. Explicit assertions share temporal evaluation with script exports, but exports disclose differences such as first-failure stopping, locator fallback, captcha input and login/history management. Do not claim all exported/native engines have identical semantics.

**Verdicts.** Action completion is distinct from business verification. No assertion, an observation-only oracle or a reused-login probe cannot establish that the full business scenario passed. Retain an earlier business failure through retries, later successes and environmental blockers. Keep execution, business, declared performance targets and answer quality distinct: business Pass plus a failed SLA is still overall Fail. Temporal assertions are sampled; transition-from-false is appropriate only for an explicit state change, not every click or idempotent operation.

**Native assets and AI evaluation.** `native_assets.py` versions original file trees, entry arguments and working directories in the domain store; `native_run.py` freezes and executes them through the `engines` registry. Probe the actual runtime; unavailable tools produce Blocked, not success or an automatic download. Native reports and artifacts determine results, not exit code alone. `ai_eval` keeps a conversation's history, cookies and server bindings within one case, with separate contexts across cases. `dialogue_judge.py` persists frozen requests, host leases and independent review receipts. Natural-language replies may cite frozen context/user facts/reference values; record `facts_used` and hashes without pretending structural validation proves semantic correctness. Unknown facts remain unresolved. Only explicit `expected_phase="final_answer"` or checks with `when="final_answer"` defer their oracle during clarification; a judge cannot erase an immediate assertion failure. Keep asynchronous native execution so the active host can service judge requests instead of blocking on itself.

**Scheduling and host delivery.** Runs are atomically claimed from SQLite. `server/jobs.py` maintains one independent dispatcher, drains queued IR/native work and ticks enabled schedules after the CLI exits; it retires when no queued/running work or enabled schedules remain. `AUTOSKILL_MAX_PARALLEL` sets the process budget (default 2); shared saved-login access remains serialized, while isolated native runs may execute concurrently. `server/scheduler.py` owns schedule revisions and occurrence identities, committing the occurrence and enqueue/outbox together. `server/deliveries.py` and the DSH host adapter persist delivery intents and receipts. Unknown delivery is investigated before resend. Host acceptance is not workflow completion, and the adapter does not create a second recurring schedule in DSH. The host must be running to perform AI work; this is not machine wake-up or autonomous model inference in Python.

**Three kinds of placeholders.** `${ENV:NAME}` is a credential (`secrets.py`), `{{name}}` is a value extracted from an earlier API response, and `{{RAND}}` is text that is new on every run so created data never collides.

**Data.** `workspace/projects/<id>/` is shareable: project/config/auth JSON, `testcases/*.json`, knowledge, generated `SKILL.md` and `domain/`. `domain.py` stores source documents, workflow definitions, requirements, scenarios, oracles and native assets as immutable generations and content-addressed objects, publishing one atomic `HEAD.json`. Durable staging uses `.local`; publication checks the expected generation or unchanged affected paths. SQLite is local runtime state, leases, receipts, schedules and indexes, not a second editable authority for definitions. `workspace/.local/` also holds credentials, saved sessions and run evidence. Reentrant process/thread locks protect shared operations; acquire project locks before SQLite write transactions. Case publication uses `projects.save_cases(expected_hashes=...)` to preserve the exact tested candidate body. Filesystem publication plus SQLite receipts use recoverable journals, not a claimed cross-store transaction or external exactly-once guarantee. Full user-created packs intentionally include credentials and saved logins but omit local run history; ordinary project packs retain optional encrypted credential export. Imported definitions do not inherit another machine's verified local execution history.

**Case sheet.** `plan.py` keeps compatibility design sheets in the sibling skill folder `autoskill-cases`; named sessions use `.local/case-plans/<hash>/autoskill-cases`. `AUTOSKILL_CASE_SKILL` overrides either default. A design row is not executable until actual actions/assertions are linked, for example with `record stop --plan <id>`. Execution completion synchronizes the sheet through the common service, and current verdicts require matching content/input evidence. A manually marked cell is not execution proof. The folder remains replaceable by another of the same name.

**Dashboard.** `server/app.py` is a FastAPI app bound to loopback; `server/metrics.py` computes every figure in SQL from one scope so cards and lists agree. The UI uses hash routing and the `h()` helper in `ui/js/core.js`.

**Validation boundaries.** See [the architecture review](docs/架构审查与演进方案.md) and [the system/dialogue report](reports/系统化技能与多轮对话验证.md). Linux browser/HTTP/native fixtures exercise real transports; they are not production capacity or model-accuracy measurements. DSH delivery has SDK-shaped substitutes and real local HTTP/CLI receipt tests, not a completed DSH device acceptance. WebRTC audio acceptance is Blocked by managed browser ICE policy; WebSocket audio is a separate tested transport. Windows and UOS have not received device acceptance in this round. Keep first-attempt stability, retry-assisted success, quality pass rate and unfinished/blocked counts separate; a fixture success fraction is not AI accuracy.

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
