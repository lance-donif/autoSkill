import test from 'node:test';import assert from 'node:assert/strict';import{spawnSync}from'node:child_process';
test('installable archive includes runtime and UI without caches or secrets',()=>{
 const npm=process.platform==='win32'?'npm.cmd':'npm';const r=spawnSync(npm,['pack','--dry-run','--json','--ignore-scripts'],{encoding:'utf8',shell:process.platform==='win32'});assert.equal(r.status,0,r.stderr);
 const pack=JSON.parse(r.stdout)[0];const paths=new Set(pack.files.map(f=>f.path));
 for(const path of ['package.json','cordis.patch.yml','plugin/index.mjs','plugin/ai-worker.mjs','lib/client.js','ui/index.html','ui/app.js','ui/style.css','runtime/service.py','runtime/engines.py','scripts/start.mjs','skills/autoskill/SKILL.md','skills/agent-browser/SKILL.md','skills/autoskill/references/agent-browser-core.offline/SKILL.md','skills/autoskill/references/agent-browser-core.offline/references/commands.md'])assert.ok(paths.has(path),path);
 assert.equal([...paths].filter(p=>p.includes('__pycache__')||p.endsWith('.pyc')||p.startsWith('.env')||p.endsWith('.sqlite')).length,0);
});
