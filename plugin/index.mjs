// DeepSeek Harness host entry: registers the autoSkill skill and runs its dashboard.
// Host interactions adapt the portable skill and application services.
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { isIP } from 'node:net';
import { fileURLToPath } from 'node:url';
import { randomUUID } from 'node:crypto';
import { setTimeout as delay } from 'node:timers/promises';
import { attachHostAdapter } from './host-adapter.mjs';
import { hostGuidance } from './host-guidance.mjs';
import { attachHostBackend } from './host-backend.mjs';
import { defaultWorkspace, workspaceFor } from './data-root.mjs';

export { defaultWorkspace } from './data-root.mjs';

export const name = 'autoskill';
export const inject = ['skills'];
export const PACKAGE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');

/** The skill sits in `autoskill/` in the offline package and in `skills/autoskill/` in a source checkout. */
export function findSkillDir(root = PACKAGE_ROOT) {
  const candidates = [join(root, 'autoskill'), join(root, 'skills', 'autoskill')];
  return candidates.find((dir) => existsSync(join(dir, 'SKILL.md'))) ?? candidates[0];
}
export const SKILL_DIR = findSkillDir();

/** Register the product and its rules, with user case sheets owned by the selected data root. */
export async function bundledSkills(skillDir = SKILL_DIR, runtime = null) {
  const rules = join(skillDir, 'references', 'test-cases');
  const skills = [await readSkill(skillDir, true, runtime), await readSkill(rules, false, runtime)];
  const root = runtime?.workspace ?? runtime?.env?.AUTOSKILL_WORKSPACE ?? defaultWorkspace();
  const current = runtime?.caseSkill ?? process.env.AUTOSKILL_CASE_SKILL ?? join(root, '.local', 'case-plans', 'default', 'autoskill-cases');
  const cases = runtime || existsSync(join(current, 'SKILL.md')) ? current : join(dirname(skillDir), 'autoskill-cases');
  if (existsSync(join(cases, 'SKILL.md'))) {
    try { skills.push(await readSkill(cases, false, runtime)); } catch { /* a hand-made case skill must not stop the plugin */ }
  }
  return skills;
}

async function readSkill(skillDir, entry, runtime) {
  const path = join(skillDir, 'SKILL.md');
  const text = await readFile(path, 'utf8');
  const split = text.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/);
  if (!split) throw new Error('Invalid bundled Skill');
  const field = (key) => {
    const match = split[1].match(new RegExp('^' + key + ':s*(.+)$', 'm'));
    if (!match) throw new Error('Missing Skill ' + key);
    return match[1].trim();
  };
  const id = field('name');
  if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(id)) throw new Error('Invalid Skill id');
  // The agent has to know where the entry point is; the skill text itself only says "this folder".
  const where = !entry ? `> 本 Skill 目录：\`${dirname(path)}\`。

` : `> 本技能目录：\`${dirname(path)}\`。下文的 \`autoskill\` 指这个目录里的 \`autoskill.cmd\`（PowerShell / cmd）或 \`autoskill\`（Linux / UOS / Git Bash）。

`;
  const token = runtime ? runtimeContext(runtime) : null;
  const content = token ? split[2].replaceAll('autoskill ', `autoskill --runtime-context=${token} `) : split[2];
  const binding = token ? '> 本插件的命令已绑定数据目录与后台端口。保留每条命令中的 `--runtime-context` 前缀；`--session` 仍按任务分别选择。\n\n' : '';
  const host = entry ? '> ' + hostGuidance.replaceAll('\n', '\n> ') + '\n\n' : '';
  return { name: id, description: field('description'), content: where + binding + host + content, source: 'bundled', path,
    resourceBase: { kind: 'directory', path: dirname(path) } };
}

/** A shell-safe, non-secret binding carried by every registered command example. */
export function runtimeContext(spec) {
  return Buffer.from(JSON.stringify({ version: 1, workspace: workspaceFor(spec), port: spec.port, host: 'dsh' })).toString('base64url');
}

/** Python HTTPX needs bare IPv6; keep DSH's separate undici environment unchanged. */
export function pythonProxyEnvironment(environment = process.env) {
  const current = environment.no_proxy ?? environment.NO_PROXY;
  if (current === undefined) return {};
  const entries = current.split(',').map((entry) => {
    const value = entry.trim();
    const literal = value.match(/^\[([^\]]+)\](?::(\d+))?$/);
    if (!literal || isIP(literal[1]) !== 6) return value;
    return literal[2] ? `all://[${literal[1]}]:${literal[2]}` : literal[1];
  }).filter(Boolean);
  const bypass = [...new Set(entries)].join(',');
  return { NO_PROXY: bypass, no_proxy: bypass };
}

/** Native bundled Python or an explicitly provisioned source venv; never download at startup. */
export function runtimeSpec(config = {}, skillDir = SKILL_DIR) {
  const port = config.port ?? 8787;
  if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('autoSkill port must be 1024-65535');
  const target = process.platform === 'linux' ? `linux-${process.arch}` : 'windows-x64';
  const bundled = join(skillDir, 'vendor', target, 'python', ...(process.platform === 'linux' ? ['bin', 'python3'] : ['python.exe']));
  const linux = join(skillDir, '..', '..', '.venv', 'bin', 'python');
  const python = config.python ?? (process.platform === 'linux'
    ? process.env.AUTOSKILL_DEV_PYTHON ?? (existsSync(bundled) ? bundled : existsSync(linux) ? linux : null)
    : existsSync(bundled) ? bundled : process.env.AUTOSKILL_DEV_PYTHON ?? null);
  const env = { ...pythonProxyEnvironment(), PYTHONPATH: join(skillDir, 'app'), PYTHONUTF8: '1', PYTHONDONTWRITEBYTECODE: '1',
    DO_NOT_TRACK: '1', PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD: '1', PIP_NO_INDEX: '1', SE_OFFLINE: 'true', SE_AVOID_STATS: 'true' };
  if (python === bundled) env.PYTHONNOUSERSITE = '1';
  if (config.workspace || process.env.AUTOSKILL_WORKSPACE) env.AUTOSKILL_WORKSPACE = resolve(config.workspace || process.env.AUTOSKILL_WORKSPACE);
  return { port, python, bundled, env, workspace: defaultWorkspace({ ...process.env, ...env }), origin: `http://127.0.0.1:${port}`, cwd: skillDir,
    args: ['-m', 'autoskill', 'dashboard', '--port', String(port)] };
}

export async function apply(ctx, config = {}) {
  const spec = runtimeSpec(config);
  if (!spec.python) {
    throw new Error('autoSkill：所选目录里没有运行时，仪表盘无法启动（缺少 ' + spec.bundled + '）。'
      + 'Windows 请选择完整离线包；Linux / UOS 请选择匹配 CPU 的离线包并运行 sh setup-linux.sh；源码部署见 docs/Linux-DSH.md。');
  }
  const instance = randomUUID();
  const lifetime = new AbortController();
  const child = spawn(spec.python, spec.args, { cwd: spec.cwd, windowsHide: true, stdio: ['pipe', 'ignore', 'inherit'],
    env: { ...process.env, ...spec.env, AUTOSKILL_INSTANCE_ID: instance, AUTOSKILL_HOST_PIPE: '1' } });
  let exited = false;
  let force;
  const closed = new Promise((resolve) => {
    function done() {
      exited = true;
      clearTimeout(force);
      resolve();
    }
    child.once('error', done);
    child.once('exit', done);
    child.stdin.on('error', () => undefined);
  });
  function stop() {
    if (!lifetime.signal.aborted) {
      lifetime.abort();
      if (!exited) {
        child.stdin.end();
        force = setTimeout(() => child.kill(), 45000);
        force.unref();
      }
    }
    return closed;
  }

  ctx.effect(() => stop, 'autoskill: dashboard process');
  try {
    for (let i = 0; i < 120 && !exited && !lifetime.signal.aborted; i++) {
      let body;
      try {
        const signal = AbortSignal.any([lifetime.signal, AbortSignal.timeout(500)]);
        const response = await fetch(spec.origin + '/api/status', { signal });
        if (response.ok) body = await response.json();
      } catch { /* not up yet */ }
      if (body?.name === 'autoSkill' && body.instance === instance) {
        const connected = { ...spec, workspace: body.workspace, caseSkill: body.case_skill };
        const skills = await bundledSkills(SKILL_DIR, connected);
        if (lifetime.signal.aborted) return;
        for (const skill of skills) ctx.effect(() => ctx.skills.register(skill), 'autoskill: ' + skill.name);
        const context = runtimeContext(connected);
        attachHostAdapter(ctx, connected, context);
        attachHostBackend(ctx, connected, { runtimeContext: context });
        ctx.logger?.info?.('autoSkill dashboard ready at ' + spec.origin);
        return;
      }
      await delay(250);
    }
  } catch (error) {
    await stop();
    throw error;
  }
  if (lifetime.signal.aborted) return;
  const exitedAtStartup = exited;
  await stop();
  throw new Error('autoSkill：仪表盘没有在端口 ' + spec.port + ' 上启动。'
    + (exitedAtStartup ? '运行时进程启动后立即退出，运行 autoskill verify 检查包完整性，再用 doctor --launch 检查系统依赖。' : '这个端口可能被别的程序占用；可在插件配置里换一个 port。'));
}
