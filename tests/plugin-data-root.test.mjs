// Program replacements retain the same data root before and after dashboard readiness.
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, cp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { defaultWorkspace, runtimeSpec, runtimeContext, bundledSkills, SKILL_DIR } from '../plugin/index.mjs';

test('the default data directory is independent of the program and supports explicit overrides', () => {
  assert.equal(defaultWorkspace({}, 'linux', '/home/example'), '/home/example/autoSkill-data');
  assert.equal(defaultWorkspace({ USERPROFILE: 'C:/Users/example' }, 'win32', '/unused'), resolve('C:/Users/example/autoSkill-data'));
  assert.equal(defaultWorkspace({ AUTOSKILL_WORKSPACE: './chosen 资料' }, 'linux', '/unused'), resolve('./chosen 资料'));
  const first = runtimeSpec({}, '/program/first/autoskill');
  const second = runtimeSpec({}, '/program/replacement/autoskill');
  assert.equal(first.workspace, second.workspace);
  assert.equal(JSON.parse(Buffer.from(runtimeContext(first), 'base64url')).workspace, first.workspace);
});

test('registered case sheets come from the selected data folder when the old program still exists', async () => {
  const root = await mkdtemp(join(tmpdir(), 'autoskill-data-root-'));
  try {
    const program = join(root, 'program', 'autoskill');
    const workspace = join(root, 'data');
    const cases = join(workspace, '.local', 'case-plans', 'default', 'autoskill-cases');
    const legacy = join(root, 'program', 'autoskill-cases');
    await mkdir(program, { recursive: true });
    await mkdir(cases, { recursive: true });
    await mkdir(legacy, { recursive: true });
    await cp(join(SKILL_DIR, 'references', 'test-cases'), join(program, 'references', 'test-cases'), { recursive: true });
    await cp(join(SKILL_DIR, 'SKILL.md'), join(program, 'SKILL.md'));
    await writeFile(join(cases, 'SKILL.md'), '---\nname: autoskill-cases\ndescription: 数据目录用例\n---\nCurrent data sheet.\n');
    await writeFile(join(legacy, 'SKILL.md'), '---\nname: autoskill-cases\ndescription: 旧目录用例\n---\nOld program sheet.\n');
    const registered = await bundledSkills(program, runtimeSpec({ workspace }, program));
    assert.equal(registered[2].resourceBase.path, cases);
    assert.match(registered[2].content, /Current data sheet/);
    assert.doesNotMatch(registered[2].content, /Old program sheet/);
    const imported = join(workspace, '.local', 'case-plans', 'imported', 'sheet', 'autoskill-cases');
    await mkdir(imported, { recursive: true });
    await writeFile(join(imported, 'SKILL.md'), '---\nname: autoskill-cases\ndescription: 已归集的表\n---\nImported data sheet.\n');
    const selected = await bundledSkills(program, { ...runtimeSpec({ workspace }, program), caseSkill: imported });
    assert.equal(selected[2].resourceBase.path, imported);
    assert.match(selected[2].content, /Imported data sheet/);
  } finally { await rm(root, { recursive: true, force: true }); }
});
