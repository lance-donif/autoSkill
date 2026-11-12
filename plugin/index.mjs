// DeepSeek Harness host entry: registers the autoSkill skill and runs its dashboard.
// The plugin adds nothing of its own; the skill directory is the product.
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { randomUUID } from 'node:crypto';
import { setTimeout as delay } from 'node:timers/promises';

export const name = 'autoskill';
export const inject = ['skills'];
export const PACKAGE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');

/** The skill sits in `autoskill/` in the offline package and in `skills/autoskill/` in a source checkout. */
export function findSkillDir(root = PACKAGE_ROOT) {
  const candidates = [join(root, 'autoskill'), join(root, 'skills', 'autoskill')];
  return candidates.find((dir) => existsSync(join(dir, 'SKILL.md'))) ?? candidates[0];
}
export const SKILL_DIR = findSkillDir();

/** autoSkill itself, plus the swappable case skill (always the sibling folder `autoskill-cases`) when one exists. */
export async function bundledSkills(skillDir = SKILL_DIR) {
  const skills = [await readSkill(skillDir, true)];
  const cases = join(dirname(skillDir), 'autoskill-cases');
  if (existsSync(join(cases, 'SKILL.md'))) {
    try { skills.push(await readSkill(cases, false)); } catch { /* a hand-made case skill must not stop the plugin */ }
  }
  return skills;
}

async function readSkill(skillDir, entry) {
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
  const where = !entry ? `> 本用例目录：\`${dirname(path)}\`。

` : `> 本技能目录：\`${dirname(path)}\`。下文的 \`autoskill\` 指这个目录里的 \`autoskill.cmd\`（PowerShell / cmd）或 \`autoskill\`（Linux / UOS / Git Bash）。

`;
  return { name: id, description: field('description'), content: where + split[2], source: 'bundled', path,
    resourceBase: { kind: 'directory', path: dirname(path) } };
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
  const env = { PYTHONPATH: join(skillDir, 'app'), PYTHONUTF8: '1', PYTHONDONTWRITEBYTECODE: '1',
    DO_NOT_TRACK: '1', PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD: '1', PIP_NO_INDEX: '1', SE_OFFLINE: 'true', SE_AVOID_STATS: 'true' };
  if (python === bundled) env.PYTHONNOUSERSITE = '1';
  if (config.workspace) env.AUTOSKILL_WORKSPACE = resolve(config.workspace);
  return { port, python, bundled, env, origin: `http://127.0.0.1:${port}`, cwd: skillDir,
    args: ['-m', 'autoskill', 'dashboard', '--port', String(port)] };
}

export async function apply(ctx, config = {}) {
  for (const skill of await bundledSkills()) ctx.effect(() => ctx.skills.register(skill), 'autoskill: ' + skill.name);
  const spec = runtimeSpec(config);
  if (!spec.python) {
    throw new Error('autoSkill：所选目录里没有运行时，仪表盘无法启动（缺少 ' + spec.bundled + '）。'
      + 'Windows 请选择完整离线包；Linux / UOS 请选择匹配 CPU 的离线包并运行 sh setup-linux.sh；源码部署见 docs/Linux-DSH.md。');
  }
  const instance = randomUUID();
  const child = spawn(spec.python, spec.args, { cwd: spec.cwd, windowsHide: true, stdio: ['ignore', 'ignore', 'inherit'],
    env: { ...process.env, ...spec.env, AUTOSKILL_INSTANCE_ID: instance } });
  let exited = false;
  child.on('error', () => { exited = true; });
  child.on('exit', () => { exited = true; });
  ctx.effect(() => () => child.kill(), 'autoskill: dashboard process');
  for (let i = 0; i < 120 && !exited; i++) {
    try {
      const response = await fetch(spec.origin + '/api/status', { signal: AbortSignal.timeout(500) });
      const body = await response.json();
      if (response.ok && body.name === 'autoSkill' && body.instance === instance) {
        ctx.logger?.info?.('autoSkill dashboard ready at ' + spec.origin);
        return;
      }
    } catch { /* not up yet */ }
    await delay(250);
  }
  child.kill();
  throw new Error('autoSkill：仪表盘没有在端口 ' + spec.port + ' 上启动。'
    + (exited ? '运行时进程启动后立即退出，运行 autoskill verify 检查包完整性，再用 doctor --launch 检查系统依赖。' : '这个端口可能被别的程序占用；可在插件配置里换一个 port。'));
}
