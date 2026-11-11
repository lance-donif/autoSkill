import { spawn } from 'node:child_process';
import { runtimeSpec,PACKAGE_ROOT } from '../plugin/index.mjs';
const spec=runtimeSpec({port:Number(process.env.AUTOSKILL_PORT??8787),stateRoot:process.env.AUTOSKILL_STATE,chromium:process.env.AUTOSKILL_CHROMIUM});
const child=spawn(spec.python,spec.args,{cwd:PACKAGE_ROOT,stdio:'inherit',env:process.env});
child.on('error',e=>{console.error('Python startup failed: '+e.name);process.exitCode=1;});child.on('exit',code=>process.exitCode=code??1);
for(const signal of ['SIGINT','SIGTERM'])process.on(signal,()=>child.kill(signal));
console.log('autoSkill: '+spec.origin);
