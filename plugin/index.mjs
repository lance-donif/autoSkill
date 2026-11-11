import { spawn } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import { dirname,join,resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { homedir } from 'node:os';
import { existsSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import { setTimeout as delay } from 'node:timers/promises';
export const name='autoskill';
export const inject=['skills'];
export const PACKAGE_ROOT=resolve(dirname(fileURLToPath(import.meta.url)),'..');

export async function bundledSkills(root=PACKAGE_ROOT){
 const result=[];
 for(const folder of ['autoskill','agent-browser','selenium-ui-cases','selenium-api-cases']){
  const path=join(root,'skills',folder,'SKILL.md');const text=await readFile(path,'utf8');const split=text.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/);
  if(!split)throw new Error('Invalid bundled Skill: '+folder);
  const field=key=>{const match=split[1].match(new RegExp('^'+key+':\\s*(.+)$','m'));if(!match)throw new Error('Missing Skill '+key);let value=match[1].trim();if(value.startsWith('"'))value=JSON.parse(value);else if(value.startsWith("'"))value=value.slice(1,-1).replaceAll("''","'");return value;};
  const id=field('name');if(!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(id))throw new Error('Invalid Skill id');
  result.push({name:id,description:field('description'),content:split[2],source:'bundled',path,resourceBase:{kind:'directory',path:dirname(path)}});
 }
 return result;
}
export function runtimeSpec(config={}){
 const port=config.port??8787;if(!Number.isInteger(port)||port<1024||port>65535)throw new Error('autoSkill port must be 1024–65535');
 const root=resolve(config.stateRoot??join(process.env.DSH_HOME??join(homedir(),'.dsh'),'autoskill'));
 const portable=join(PACKAGE_ROOT,'skills/autoskill/vendor/windows-x64');
 const python=config.python??process.env.AUTOSKILL_PYTHON??(process.platform==='win32'&&existsSync(join(portable,'python/python.exe'))?join(portable,'python/python.exe'):(process.platform==='win32'?'python':'python3'));
 const chromium=config.chromium??process.env.AUTOSKILL_CHROMIUM??(process.platform==='win32'&&existsSync(join(portable,'chrome/chrome-win64/chrome.exe'))?join(portable,'chrome/chrome-win64/chrome.exe'):undefined);
 return {port,root,python,origin:`http://127.0.0.1:${port}`,args:['-m','runtime.service','--root',root,'--port',String(port),...(chromium?['--chromium',chromium]:[])]};
}
export async function apply(ctx,config={}){
 if(process.env.AUTOSKILL_AI_CHILD==='1')return;
 const spec=runtimeSpec(config);const skills=await bundledSkills();
 for(const skill of skills)ctx.effect(()=>ctx.skills.register(skill),'autoskill: '+skill.name);
 const bin=join(PACKAGE_ROOT,'node_modules','.bin');
 const portable=join(PACKAGE_ROOT,'skills/autoskill/vendor/windows-x64');
 const paths=[join(portable,'node'),join(portable,'node_modules','.bin'),bin];
 const instance=randomUUID();
 const child=spawn(spec.python,spec.args,{cwd:PACKAGE_ROOT,env:{...process.env,AUTOSKILL_INSTANCE_ID:instance,PIP_NO_INDEX:'1',PIP_DISABLE_PIP_VERSION_CHECK:'1',PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD:'1',DO_NOT_TRACK:'1',OTEL_SDK_DISABLED:'true',npm_config_update_notifier:'false',npm_config_audit:'false',npm_config_fund:'false',PATH:[...paths,process.env.PATH??''].join(process.platform==='win32'?';':':')},stdio:['ignore','ignore','pipe'],windowsHide:true});
 let exited=false;let failure;
 child.on('error',()=>{exited=true;failure='Python runtime could not start';});
 child.on('exit',()=>{exited=true;});
 // Drain diagnostics without leaking project data or credentials into DSH logs.
 child.stderr.on('data',()=>{});
 ctx.effect(()=>()=>child.kill(),'autoskill: backend process');
 let healthy=false;
 for(let i=0;i<50;i++){
  if(exited)break;
  try{const response=await fetch(spec.origin+'/api/status',{signal:AbortSignal.timeout(500)});const json=await response.json();if(response.ok&&json.name==='autoSkill'&&json.instance_id===instance){healthy=true;break;}}catch{}
  await delay(200);
 }
 if(!healthy){child.kill();throw new Error(failure??'autoSkill backend not ready. Install Python dependencies with requirements.txt and inspect the configured port.');}
 ctx.logger?.info?.('autoSkill dashboard ready at '+spec.origin);
}
