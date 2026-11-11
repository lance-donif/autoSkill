import { readFileSync, existsSync } from 'node:fs';
import { resolve, dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
const root=resolve(dirname(fileURLToPath(import.meta.url)),'..');
const candidate=join(root,'skills/autoskill/vendor/windows-x64');
const vendor=existsSync(join(candidate,'python/python.exe'))?candidate:resolve(root,'..');
const args=process.argv.slice(2);
const forbidden=new Set(['install','upgrade','update','doctor','skills','--provider','-p','--proxy','--cdp','--auto-connect','--config','--executable-path','--args','--allowed-domains']);
if(args.some(a=>forbidden.has(a)||['--provider=','--proxy=','--cdp=','--config=','--executable-path=','--args=','--allowed-domains='].some(prefix=>a.startsWith(prefix)))){
 console.error('Offline policy: setup, updates, remote browsers and policy overrides are disabled');process.exit(2);
}
const binary=join(vendor,'node_modules/agent-browser/bin/agent-browser-win32-x64.exe');
if(process.platform!=='win32'||!existsSync(binary)){console.error('Bundled Windows Agent Browser is unavailable; no download attempted');process.exit(2);}
let trusted=[];
if(!args.every(a=>['--help','-h','--version','-V'].includes(a))){
 let cfg;try{cfg=JSON.parse(readFileSync(resolve('qa-config.json'),'utf8'));}catch{console.error('Run inside an authorized project with qa-config.json');process.exit(2);}
 if(cfg.execution_authorized!==true||!Array.isArray(cfg.allowed_origins)||!cfg.allowed_origins.length){console.error('Authorized test origins are required');process.exit(2);}
 const domains=cfg.allowed_origins.map(origin=>new URL(origin).hostname);
 trusted=['--config',join(vendor,'agent-browser-config.json'),'--executable-path',join(vendor,'chrome/chrome-win64/chrome.exe'),'--allowed-domains',domains.join(','),'--args','--disable-background-networking,--disable-component-update,--disable-domain-reliability,--disable-sync,--disable-breakpad,--disable-crash-reporter,--no-pings,--metrics-recording-only'];
}
const env=Object.fromEntries(Object.entries(process.env).filter(([key])=>!key.startsWith('AGENT_BROWSER_')));
Object.assign(env,{DO_NOT_TRACK:'1',OTEL_SDK_DISABLED:'true'});
const result=spawnSync(binary,[...trusted,...args],{stdio:'inherit',env,windowsHide:true});
process.exit(result.status??2);
