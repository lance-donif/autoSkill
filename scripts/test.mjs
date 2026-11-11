import { spawnSync } from 'node:child_process';
const python=process.env.AUTOSKILL_PYTHON??(process.platform==='win32'?'python':'python3');
for(const [command,args] of [[process.execPath,['--test','tests/plugin.test.mjs']],[python,['-m','unittest','discover','-s','tests','-p','test_*.py','-q']]]){
 const r=spawnSync(command,args,{stdio:'inherit',env:process.env});if(r.error||r.status){process.exitCode=r.status??1;break;}
}
