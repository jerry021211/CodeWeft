"""Small real-model follow-up. Controlled fault injection is labelled, not model output."""
from __future__ import annotations
import argparse
import ast
from collections import Counter
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from types import SimpleNamespace
from anthropic import Anthropic
from codeagent.agent import Agent, AgentConfig
from codeagent.config import EnvironmentConfig
from codeagent.context import ContextConfig, ContextManager
from codeagent.events import EventEmitter, UsageTracker
from codeagent.events.sink import CallbackEventSink
from codeagent.hooks.loop_guard import LoopGuardConfig
from codeagent.lsp import LspConfig
from codeagent.prompts import PromptRuntime, PromptMode
from codeagent.recovery import RecoveryConfig, RecoveryRuntime
from codeagent.tools import ToolRegistry
from codeagent.tools.edit import EditFileTool
from codeagent.tools.grep import GrepTool
from codeagent.tools.glob_tool import GlobTool
from codeagent.tools.read import ReadFileTool
from codeagent.tools.lsp import LspTool
from codeagent.tools.search_code import SearchCodeTool
from codeagent.tools.workspace import WorkspaceGuard
from codeagent.runtime.data_paths import RuntimeDataPaths
from evals.agent_adapter import EvidenceSDK, append_record
from evals.evidence import read_json, write_json, file_hash
from .worker import EvaluationClient
from .field_benchmark import verify, MeteredProvider, score, append
from .scoring import parse_answer


class FaultFirstSDK:
    def __init__(self, sdk, fault, shared=None):
        self.sdk,self.fault=sdk,fault
        self.shared=shared if shared is not None else {'injected':False}
        self.messages=self
    def with_options(self,**kwargs):return FaultFirstSDK(self.sdk.with_options(**kwargs),self.fault,self.shared)
    def create(self,**kwargs):
        if not self.shared['injected']:
            self.shared['injected']=True
            return SimpleNamespace(id='controlled-fault-injection',model='controlled-fixture-not-a-model',
                content=[dict(type='tool_use',id='controlled-edit',name='edit_file',input=self.fault)],stop_reason='tool_use',usage=None)
        return self.sdk.messages.create(**kwargs)
    def close(self):self.sdk.close()


class FixtureEvidenceSDK(EvidenceSDK):
    """Scripted history has no provider thinking blocks; disable thinking explicitly.

    Only the controlled-edit fixture uses this mode, recorded in the protocol
    amendment (the trace sanitizer omits all thinking keys). Retrieval keeps
    the provider's default mode.
    """
    def with_options(self, **kwargs):
        return FixtureEvidenceSDK(self.sdk.with_options(**kwargs), self.root, shared=self.shared)

    def create(self, **kwargs):
        kwargs['extra_body'] = {**kwargs.get('extra_body', {}), 'thinking': {'type': 'disabled'}}
        return super().create(**kwargs)


def build(env,root,trial,registry,*,fault=None):
    sdk=Anthropic(api_key=env.api_key,base_url=env.base_url,max_retries=0,timeout=45)
    if fault:sdk=FaultFirstSDK(sdk,fault)
    recorder=(FixtureEvidenceSDK if fault else EvidenceSDK)(sdk,trial,max_calls=7 if fault else 6)
    emitter=EventEmitter(CallbackEventSink(lambda event:append_record(trial/'events.jsonl',event.to_dict())))
    tracker=UsageTracker()
    client=EvaluationClient(sdk_client=recorder,base_url=env.base_url,stream=False,event_emitter=emitter,usage_tracker=tracker)
    agent=Agent(client=client,tools=registry,config=AgentConfig(model=env.model_id,max_tokens=1800,max_iterations=7 if fault else 6,
        loop_guard=LoopGuardConfig(max_model_calls=7 if fault else 6,max_tool_calls=18,max_total_tokens=60000,max_active_seconds=120)),
        context=ContextManager(config=ContextConfig(mode='off',context_window_tokens=64000,
            transcript_dir=trial/'runtime/transcripts',tool_output_dir=trial/'runtime/tool-results')),
        prompt_runtime=PromptRuntime(workspace=root),prompt_mode=PromptMode.NORMAL,allow_subagents=False,
        event_emitter=emitter,usage_tracker=tracker,
        recovery_runtime=RecoveryRuntime(RecoveryConfig(max_retries=1,side_query_max_retries=0,max_continuations=0,escalated_max_tokens=1800)))
    agent.tools=agent.tools.copy_without({'compact'})
    return agent,recorder


def records(path):return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []


def trace_summary(trial):
    requests=records(trial/'model-requests.jsonl')
    responses=records(trial/'model-responses.jsonl')
    real=[r for r in responses if r.get('provider_response_id')!='controlled-fault-injection']
    complete=bool(real) and all(isinstance(r.get('usage'),dict) for r in real)
    usage={k:sum(r.get('usage',{}).get(k) or 0 for r in real) for k in
           ('input_tokens','output_tokens','cache_read_input_tokens','cache_creation_input_tokens')}
    input_total=usage['input_tokens']+usage['cache_read_input_tokens']+usage['cache_creation_input_tokens']
    visible=[]
    for req in requests:
        for message in req.get('messages',[]):
            content=message.get('content',[])
            if isinstance(content,list):
                for block in content:
                    value=block.get('content','') if isinstance(block,dict) else ''
                    if isinstance(value,str) and '<edit_diagnostics>' in value:visible.append(value)
    snippets=list(dict.fromkeys(visible))
    diagnostics=[]
    for value in snippets:
        try:diagnostics.extend(json.loads(value.split('<edit_diagnostics>',1)[1].split('</edit_diagnostics>',1)[0]))
        except (ValueError,IndexError):pass
    return dict(model_requests=len(real),usage_complete=complete,usage=usage,
        cache_input_ratio=usage['cache_read_input_tokens']/input_total if input_total and complete else None,
        feedback_visible=bool(snippets),feedback_snippets=snippets,
        feedback_statuses=dict(Counter(d.get('status') for d in diagnostics)),
        feedback_error_count=sum(item.get('severity')==1 for d in diagnostics for item in d.get('diagnostics',[])),
        tool_calls=sum(e.get('type')=='tool.started' for e in records(trial/'events.jsonl')))


def retrieval(base):
    suite=verify(base);plan=read_json(base/'agent-plan.json');env=EnvironmentConfig.from_env()
    output=base/'agent-retrieval-v1';output.mkdir(exist_ok=False)
    provider=MeteredProvider(env.embedding_config,base/'run-v1/provider-ledger.jsonl',max_requests=800,max_characters=6000000)
    instruction='只读定位当前工作区代码，不修改文件。请先使用 search_code，再按需读文件核实。最多5条。最终只返回JSON：{"status":"found|not_found|insufficient_evidence","results":[{"path":"相对路径","symbol":"符号名","line":摘录首行整数,"end_line":摘录末行整数,"quote":"最多20行连续源码"}]}。找不到相关实现时不要把无关候选当成答案，检查不足就选 insufficient_evidence。问题：'
    for source in suite['sources']:
        cases=[c for c in source['cases'] if c['id'] in plan['retrieval_case_ids']]
        for case in cases:
            for arm in plan['retrieval_arms']:
                trial=output/(case['id']+'-'+arm);trial.mkdir()
                root=Path(source['root']);guard=WorkspaceGuard(root);registry=ToolRegistry()
                for tool in [ReadFileTool(workspace_guard=guard),GrepTool(workspace_guard=guard),GlobTool(workspace_guard=guard)]:registry.register(tool)
                search=SearchCodeTool(root,base/'run-v1/indexes'/source['id']/arm,embedding_provider=provider if arm=='C' else None)
                search.service.max_vector_chunks=0
                registry.register(search)
                agent,recorder=build(env,root,trial,registry)
                provider.phase='agent:'+case['id']+':'+arm
                start=time.monotonic();record=dict(case_id=case['id'],arm=arm,source=source['id'])
                try:
                    response=agent.run(instruction+case['query'])
                    answer=parse_answer(response.final_text)
                    # Report model-supplied locations, never correct wrong quotes.
                    results=(answer or {}).get('results',[])
                    for item in results:
                        if 'end_line' not in item and type(item.get('line')) is int:
                            item['end_line']=item['line']+len(str(item.get('quote','')).splitlines())-1
                        path=(root/item.get('path','')).resolve()
                        if path.is_relative_to(root.resolve()) and path.is_file():item['content_hash']=file_hash(path)
                    record.update(outcome=response.stop_reason,answer=answer,raw_answer=response.final_text,
                        metrics=score(case,{'results':results},[],root),correct_abstention=not case['targets'] and (answer or {}).get('status')=='not_found',
                        false_positive=not case['targets'] and (answer or {}).get('status')=='found')
                except Exception as exc:record.update(outcome='error',error_type=type(exc).__name__)
                finally:recorder.close()
                record.update(duration_ms=(time.monotonic()-start)*1000,trace=trace_summary(trial))
                write_json(trial/'result.json',record);append(output/'results.jsonl',record)
                print(case['id'],arm,record['outcome'],record.get('metrics',{}).get('strict_hit5'),flush=True)
    verify(base)


FIXTURES={
 'python':('sample.py','def double(value):\n    return value * 2\n','return value +', 'return value * 2'),
 'typescript':('sample.ts','export function double(value: number): number {\n  return value * 2;\n}\n','return value + ;','return value * 2;'),
 'java':('Main.java','public class Main {\n  public static int twice(int value) {\n    return value * 2;\n  }\n  public static void main(String[] args) { if (twice(3) != 6 || twice(-2) != -4) throw new AssertionError(); }\n}\n','return value + ;','return value * 2;'),
}


def check_fixture(root,language):
    if language=='python':
        text=(root/'sample.py').read_text(encoding='utf-8');ast.parse(text)
        scope={};exec(compile(text,'sample.py','exec'),scope)
        return dict(ok=scope['double'](3)==6 and scope['double'](-2)==-4,checker='Python compile + behavior assertions')
    (root/'compiled').mkdir(exist_ok=True)
    if language=='typescript':
        compiler=RuntimeDataPaths.default().root/'tooling/typescript-lsp/node_modules/typescript/lib/tsc.js'
        result=subprocess.run([shutil.which('node'),str(compiler),'--strict','--skipLibCheck','--target','ES2020','--module','commonjs','--outDir','compiled','sample.ts'],cwd=root,capture_output=True,timeout=30)
        if result.returncode==0:
            behavior=subprocess.run([shutil.which('node'),'-e',"const f=require('./compiled/sample.js').double;if(f(3)!==6||f(-2)!==-4)process.exit(1)"],cwd=root,capture_output=True,timeout=10)
            return dict(ok=behavior.returncode==0,checker='TypeScript tsc + Node behavior assertions')
    else:
        result=subprocess.run([shutil.which('javac'),'-d','compiled','Main.java'],cwd=root,capture_output=True,timeout=30)
        if result.returncode==0:
            behavior=subprocess.run([shutil.which('java'),'-cp','compiled','Main'],cwd=root,capture_output=True,timeout=10)
            return dict(ok=behavior.returncode==0,checker='javac + Java behavior assertions')
    return dict(ok=False,checker=language,output=(result.stdout+result.stderr).decode(errors='replace')[:1000])


def edits(base):
    plan=read_json(base/'agent-plan.json');env=EnvironmentConfig.from_env()
    output=base/'agent-edits-v2';output.mkdir(exist_ok=False)
    write_json(output/'protocol-amendment.json', dict(reason='v1 scripted tool history rejected by provider thinking mode (HTTP 400).',
        change='Disable thinking explicitly for ALL editing arms; keep cases, repair prompt, budgets and verifiers unchanged.',
        original_evidence='agent-edits-v1', initial_attempts=7,
        note='v1 stopped printing javac output with Windows GBK encoding; v2 uses UTF-8 console. v1 failures retained, not part of valid repair quality denominator.'))
    for language in plan['editing_languages']:
        filename,original,broken,correct=FIXTURES[language]
        for arm in plan['editing_arms']:
            trial=output/(language+'-'+arm);root=trial/'workspace';root.mkdir(parents=True)
            (root/filename).write_text(original,encoding='utf-8')
            if language=='typescript':(root/'tsconfig.json').write_text('{"compilerOptions":{"strict":true,"target":"ES2020"}}')
            guard=WorkspaceGuard(root);registry=ToolRegistry()
            registry.register(ReadFileTool(workspace_guard=guard));registry.register(EditFileTool(workspace_guard=guard))
            lsp=LspTool(root,LspConfig(timeout_seconds=20,feedback_seconds=1.5))
            owned=[];original_session=lsp.service._session
            def track_session(*args,**kwargs):
                try:return original_session(*args,**kwargs)
                finally:
                    for session in lsp.service.sessions.values():
                        if session['rpc'] not in owned:owned.append(session['rpc'])
            lsp.service._session=track_session
            warm=None
            if arm!='off':
                registry.register(lsp)
                if arm=='warm':warm=lsp.service.execute('diagnostics',filename,timeout=40)
            fault=dict(file_path=filename,old_string=correct,new_string=broken)
            agent,recorder=build(env,root,trial,registry,fault=fault)
            start=time.monotonic();record=dict(language=language,arm=arm,warmup=warm)
            try:
                response=agent.run('这是受控编辑反馈验收。首个编辑由测试夹具注入语法错误；随后请根据工具结果读取并修复 '+filename+'，让函数对正负整数返回输入值的两倍，保留函数名、参数和现有入口。只修改该文件，完成后说明修复。')
                record.update(outcome=response.stop_reason,answer=response.final_text)
            except Exception as exc:record.update(outcome='error',error_type=type(exc).__name__)
            finally:
                lsp.close();recorder.close()
            try:record['verification']=check_fixture(root,language)
            except Exception as exc:record['verification']=dict(ok=False,error_type=type(exc).__name__)
            record.update(duration_ms=(time.monotonic()-start)*1000,trace=trace_summary(trial),sessions_released=not lsp.service.sessions,
                server_started=bool(owned),processes_reaped=all(rpc.process.poll() is not None for rpc in owned),
                io_threads_stopped=all(not rpc.reader.is_alive() and not rpc.writer.is_alive() for rpc in owned))
            write_json(trial/'result.json',record);append(output/'results.jsonl',record)
            print(language,arm,record['outcome'],record['verification'],record['trace']['feedback_visible'],flush=True)


if __name__=='__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    parser=argparse.ArgumentParser();parser.add_argument('operation',choices=['retrieval','edits']);parser.add_argument('--base',type=Path,required=True)
    args=parser.parse_args();base=args.base.resolve()
    if file_hash(base/'agent-plan.json')!=read_json(base/'agent-plan-seal.json')['sha256']:raise ValueError('Agent protocol changed')
    {'retrieval':retrieval,'edits':edits}[args.operation](base)
