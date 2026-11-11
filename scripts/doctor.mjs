import { spawnSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { existsSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { join } from 'node:path';
import { runtimeSpec, PACKAGE_ROOT } from '../plugin/index.mjs';
const require=createRequire(import.meta.url);
const spec=runtimeSpec();
const checks={node:process.version,python:spec.python,network:'No downloads or external requests are performed'};
let failed=Number(process.versions.node.split('.')[0])<22;
for(const name of ['agent-browser','@deepseek-ai/dsh-sdk-client']){
 try{checks[name]=require.resolve(name+'/package.json');}catch{checks[name]=(existsSync(join(PACKAGE_ROOT,'node_modules',name,'package.json'))||existsSync(join(PACKAGE_ROOT,'skills/autoskill/vendor/windows-x64/node_modules',name,'package.json')))?'local package present':'not installed';}
}
try{let sdk;try{sdk=await import('@deepseek-ai/dsh-sdk-client');}catch{const local=createRequire(join(PACKAGE_ROOT,'skills/autoskill/vendor/windows-x64/package.json'));sdk=await import(pathToFileURL(local.resolve('@deepseek-ai/dsh-sdk-client')).href);}checks.sdk_run_api=typeof sdk.DeepSeekHarness==='function';}catch{checks.sdk_run_api=false;}
const code=`import importlib.util,json,os,sys
modules=['httpx','jsonschema','openpyxl','playwright','fastapi','uvicorn','tzdata']
checks={name:importlib.util.find_spec(name) is not None for name in modules}
checks['python_supported']=sys.version_info>=(3,10)
if checks['playwright']:
 from playwright.sync_api import sync_playwright
 with sync_playwright() as p:
  executable=os.environ.get('AUTOSKILL_CHROMIUM') or p.chromium.executable_path
  checks['browser_executable_present']=os.path.isfile(executable)
else: checks['browser_executable_present']=False
print(json.dumps(checks))
`;
const result=spawnSync(spec.python,['-c',code],{cwd:PACKAGE_ROOT,encoding:'utf8',env:process.env});
if(result.error||result.status!==0){checks.python_runtime='unavailable';failed=true;}
else{
 try{checks.python_runtime=JSON.parse(result.stdout);failed ||= Object.entries(checks.python_runtime).some(([key,value])=>key!=='browser_executable_present'&&!value);}
 catch{checks.python_runtime='invalid check response';failed=true;}
}
if(process.argv.includes('--ui'))failed ||= checks.python_runtime?.browser_executable_present!==true;
if(process.argv.includes('--ai'))failed ||= !checks.sdk_run_api || checks['agent-browser']==='not installed';
checks.note='Browser file presence does not prove browser launch; AI model reachability is not tested.';
checks.ok=!failed;
console.log(JSON.stringify(checks,null,2));
if(failed)process.exitCode=1;
