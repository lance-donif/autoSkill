import { spawn } from 'node:child_process';
import { runtimeSpec,PACKAGE_ROOT } from '../plugin/index.mjs';
const spec=runtimeSpec({port:Number(process.env.AUTOSKILL_PORT??8787),stateRoot:process.env.AUTOSKILL_STATE,chromium:process.env.AUTOSKILL_CHROMIUM});
const child=spawn(spec.python,spec.args,{cwd:PACKAGE_ROOT,stdio:'inherit',env:{...process.env,PIP_NO_INDEX:'1',PIP_DISABLE_PIP_VERSION_CHECK:'1',PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD:'1',DO_NOT_TRACK:'1',OTEL_SDK_DISABLED:'true',npm_config_offline:'true',npm_config_update_notifier:'false',npm_config_audit:'false',npm_config_fund:'false'}});
child.on('error',e=>{console.error('Python startup failed: '+e.name);process.exitCode=1;});child.on('exit',code=>process.exitCode=code??1);
for(const signal of ['SIGINT','SIGTERM'])process.on(signal,()=>child.kill(signal));
console.log('autoSkill: '+spec.origin);
