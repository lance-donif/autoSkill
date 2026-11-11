import { readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
const request=JSON.parse(await readFile(process.argv[2],'utf8'));
process.env.AUTOSKILL_AI_CHILD='1';
let harness;
try {
 const { DeepSeekHarness }=await import('@deepseek-ai/dsh-sdk-client');
 harness=new DeepSeekHarness({profile:request.profile??'sdk',cwd:request.project});
 const prompt=request.mode==='explain'
  ? `只读解释这次自动化测试。读取 ${request.project}/execution 的真实结果与证据，不执行页面操作，不把未执行或Blocked说成通过。将中文结论写到 ${request.project}/ai-explanation.md。`
  : `执行 autoSkill 测试任务。先读取 ${request.skill}，再读取对应标准用例JSON、qa-config.json与知识库。浏览器执行前读取官方Agent Browser技能 ${request.agentBrowserSkill}，并按其中说明加载匹配安装版本的core指南。只使用用例声明且已授权的origin、测试数据与操作，不扩大权限。逐条真实执行并断言，绝不能仅凭页面看似正常判为通过。结果写 ${request.results}，每条包括 case_id、case_revision、case_content_sha256、run_id、started_at、status、actual、reason、assertions、evidence。内容哈希通过 ${request.coreScript} 中的case_fingerprint计算，不能猜测。截图或日志必须存在且为项目相对路径；秘密只用环境变量引用。缺少工具、权限或环境标Blocked，未执行为Not Run。不要调用任意未授权写操作或更改权限。`;
 const result=await harness.run(prompt);
 if(result.finishReason==='error'||result.finishReason==='aborted')throw new Error('AI turn did not complete');
 await writeFile(resolve(request.project,'ai-turn-summary.json'),JSON.stringify({sessionId:result.sessionId,finishReason:result.finishReason,completedAt:new Date().toISOString()},null,2));
} catch(error){console.error(`AI execution blocked: ${error.name}`);process.exitCode=2;}
finally {if(harness)await harness.close();}
