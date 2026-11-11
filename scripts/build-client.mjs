import { readFile,writeFile,mkdir } from 'node:fs/promises';
const source=await readFile(new URL('../plugin/client.cjs',import.meta.url),'utf8');
await mkdir(new URL('../lib/',import.meta.url),{recursive:true});
const output='window.__ModuleLoader__.load({id:"dsh-plugin-autoskill",factory:(require)=>{\nvar module={exports:{}};var exports=module.exports;\n'+source+'\nreturn module.exports;\n}});\n';
await writeFile(new URL('../lib/client.js',import.meta.url),output);
console.log('Built DSH client closure factory');
