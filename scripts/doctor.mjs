import { spawnSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { runtimeSpec } from '../plugin/index.mjs';
const require=createRequire(import.meta.url);const checks={node:process.version,python:runtimeSpec().python};
for(const name of ['agent-browser','@deepseek-ai/dsh-sdk-client']){try{checks[name]=require.resolve(name+'/package.json');}catch{checks[name]='not installed';}}
try{const sdk=await import('@deepseek-ai/dsh-sdk-client');checks.sdk_run_api=typeof sdk.DeepSeekHarness==='function';}catch{checks.sdk_run_api=false;}
console.log(JSON.stringify(checks,null,2));
const r=spawnSync(checks.python,['skills/autoskill/scripts/qa.py','doctor'],{stdio:'inherit'});if(r.error)process.exitCode=1;
