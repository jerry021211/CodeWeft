import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import { mkdtempSync, unlinkSync, rmdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { createRequire } from 'node:module';
import { build } from 'esbuild';
const directory = mkdtempSync(join(tmpdir(), 'approval-cards-'));
const bundle = join(directory, 'cards.cjs');
await build({stdin:{contents:`export * from './src/lib/approvalPresentation'; export * from './src/components/ApprovalBanner'; export { createElement } from 'react'; export { renderToStaticMarkup } from 'react-dom/server';`,resolveDir:resolve('.')},tsconfig:'tsconfig.app.json',bundle:true,platform:'node',format:'cjs',outfile:bundle});
const {approvalPresentation,approvalStatus,ApprovalBanner,ApprovalTimeline,createElement:h,renderToStaticMarkup:render}=createRequire(import.meta.url)(bundle);
after(()=>{unlinkSync(bundle);rmdirSync(directory)});
const approval = (extra={}) => ({id:'one',run_id:'run-one',tool_name:'bash',summary:'Remove-Item old.zip',status:'pending',input:{command:'Remove-Item old.zip',description:'重新生成项目压缩包'},reason:'permission rule',...extra});

test('pending card explains scope, preserves command evidence and labels both actions on mobile',()=>{
 const html=render(h(ApprovalBanner,{approval:approval(),onDecision:()=>{}}));
 assert.match(html,/重新生成项目压缩包/); assert.match(html,/Agent 提供的操作说明/);
 assert.match(html,/删除相关指令/); assert.match(html,/访问范围可能超出显式路径/);
 assert.match(html,/允许本次操作/); assert.match(html,/暂不允许/);
 assert.match(html,/Remove-Item old.zip/); assert.match(html,/此前已完成的修改不会自动撤销/);
 assert.doesNotMatch(html,/悬停|高风险|不会影响源码/);
 assert.doesNotMatch(html,/<details[^>]*open/);
});
test('resolved and expired approvals have no executable decision buttons',()=>{
 for(const status of ['allowed','denied','expired']){
  const html=render(h(ApprovalBanner,{approval:approval({status}),onDecision:()=>{}}));
  assert.doesNotMatch(html,/<button/);
  assert.match(html,/<details[^>]*id="approval-one"/);
  if(status==='allowed') assert.match(html,/授权不等于操作成功/);
 }
 assert.equal(approvalStatus(approval({expires_at:'2000-01-01T00:00:00Z'})), 'expired');
 const html=render(h(ApprovalBanner,{approval:approval({expires_at:'2000-01-01T00:00:00Z'}),onDecision:()=>{}}));
 assert.doesNotMatch(html,/<button/);
});
test('concurrent member approvals keep independent operation IDs and task attribution',()=>{
 const team={team:{id:'team'},attempts:[{id:'attempt',task_id:'2',agent_id:'worker'}],tasks:[{task:{id:'2',subject:'命令行工具'}}],agents:[{id:'worker',name:'成员二'}]};
 const html=render(h(ApprovalTimeline,{approvals:[approval(),approval({id:'two',team_run_id:'team',attempt_id:'attempt',agent_id:'worker'}),approval({id:'old',status:'allowed'})],teams:[team],busyIds:['one'],errors:{two:'网络异常'},onDecision:()=>{}}));
 assert.match(html,/id="approval-one"/); assert.match(html,/id="approval-two"/);
 assert.match(html,/成员二 · 任务 #2 · 命令行工具/); assert.match(html,/最近授权记录 · 1/);
 assert.equal((html.match(/disabled=""/g)||[]).length,2);
 assert.match(html,/处理未完成：网络异常/);
});
test('file operations identify their actual target; unknown tools do not invent impact guarantees',()=>{
 const model=approvalPresentation(approval({tool_name:'write_file',input:{file_path:'src/config.py'}}));
 assert.equal(model.scope,'src/config.py'); assert.match(model.effects[0],/覆盖/);
 const unknown=approvalPresentation(approval({tool_name:'external_plugin',input:{}}));
 assert.match(unknown.scope,/没有足够信息/);
});
