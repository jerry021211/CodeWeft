"""Real-provider cache/incrementality smoke plus explicit offline faults, isolated copies only."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import time
from codeagent.config import EnvironmentConfig
from codeagent.code_intelligence.models import QueryContext
from codeagent.runtime.cancellation import CancelledError
from codeagent.runtime.execution import ExecutionStopped
from codeagent.tools.search_code import SearchCodeTool
from .field_benchmark import MeteredProvider, valid_quote, append
from evals.evidence import write_json


def run(base):
    env=EnvironmentConfig.from_env();output=base/'integrity-v1';output.mkdir(exist_ok=False)
    root=output/'workspace';root.mkdir();cache=output/'index'
    subprocess.run(['git','init','-q',str(root)],check=True,capture_output=True)
    (root/'.gitignore').write_text('ignored/\n')
    (root/'ignored').mkdir();(root/'ignored/Secret.java').write_text('class MUST_NOT_EMBED {}')
    path=root/'Order.java'
    original='class Order {\n int refund(int value) {\n  return value * 2;\n }\n}\n'
    path.write_text(original)
    class Monitor(MeteredProvider):
        async def _embed_batch(self,texts,check,timeout):
            if any('MUST_NOT_EMBED' in text for text in texts):raise AssertionError('Ignored source reached provider')
            return await super()._embed_batch(texts,check,timeout)
    provider=Monitor(env.embedding_config,base/'run-v1/provider-ledger.jsonl',max_requests=800,max_characters=6000000)
    tool=SearchCodeTool(root,cache,embedding_provider=provider)
    rows=[]
    def step(name):
        provider.phase='integrity:'+name
        started=time.monotonic();result=json.loads(tool.run('refund behavior'))
        assert result['vector_status']=='completed',result
        assert all(valid_quote(root,item) for item in result['results'])
        assert all('ignored/' not in item['path'] for item in result['results'])
        record=dict(name=name,duration_ms=(time.monotonic()-started)*1000,payload=result)
        rows.append(record);append(output/'steps.jsonl',record)
        return result
    first=step('cold');assert first['embedding_usage']['embedded']>0
    warm=step('warm');assert warm['embedding_usage']['embedded']==0
    path.write_text('\n\n'+original)
    shifted=step('line_shift');assert shifted['embedding_usage']['embedded']==0
    path.write_text(('\n\n'+original).replace('value * 2','value * 3'))
    changed=step('body_changed');assert changed['embedding_usage']['embedded']>0
    added=root/'Cancel.java';added.write_text('class Cancel { void cancelRefund() {} }\n')
    fresh=step('file_added');assert fresh['indexed_files_updated']==1
    added.unlink();deleted=step('file_deleted');assert not any(item['path']=='Cancel.java' for item in deleted['results'])
    # Same API model, deliberately distinct namespace: proves cache segregation,
    # without claiming a second model's semantic behavior has been measured.
    class Alias:
        def __init__(self):
            self.provider=provider.provider
            self.model=provider.model+':cache-namespace-probe'
            self.dimensions=provider.dimensions
        def embed(self,*a,**kw):return provider.embed(*a,**kw)
    provider.phase='integrity:model_namespace_changed'
    switched=json.loads(SearchCodeTool(root,cache,embedding_provider=Alias()).run('refund behavior'))
    assert switched['embedding_usage']['cache_hits']==0 and switched['embedding_usage']['embedded']>0
    append(output/'steps.jsonl',dict(name='model_namespace_changed',payload=switched))
    # Deliberate fault provider is a separate robustness test, not a real network outage.
    class Failed:
        provider,model,dimensions='fault','offline',2
        def embed(self,*a,**kw):raise ConnectionError('controlled offline fault')
    fallback=json.loads(SearchCodeTool(root,cache,embedding_provider=Failed()).run('refund behavior'))
    assert fallback['retrieval_backend']=='lexical' and fallback['vector_status']=='failed' and fallback['results']
    assert str(tool.run('refund',path='..')).startswith('Error:')
    def cancelled():raise CancelledError('controlled cancellation')
    for name,context,error in [('cancel',QueryContext(cancellation_check=cancelled),CancelledError),
                                ('budget',QueryContext(remaining_seconds=lambda:0),ExecutionStopped)]:
        started=time.monotonic()
        try:tool.service.search('refund behavior',context=context)
        except error:rows.append(dict(name=name,duration_ms=(time.monotonic()-started)*1000,status='propagated'))
        else:raise AssertionError('Control flow swallowed')
    other=output/'other';other.mkdir();(other/'Other.java').write_text('class Other { void distinct() {} }')
    subprocess.run(['git','init','-q',str(other)],check=True,capture_output=True)
    isolated=json.loads(SearchCodeTool(other,output/'other-index').run('distinct'))
    assert all(item['path']=='Other.java' for item in isolated['results'])
    write_json(output/'report.json',dict(status='passed',checks=['real_cold','real_warm','line_shift_reuse','body_update',
        'add','delete','source_consistency','model_namespace_invalidation','offline_fault','ignore','boundary','workspace','cancel','budget'],
        control_timing=[row for row in rows if row['name'] in ('cancel','budget')],
        limitations='Model change uses a separate identity with the SAME remote model. Offline failure is controlled. Other correctness cases are in unit/integration suites.'))
    print('Integrity checks passed',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--base',type=Path,required=True)
    run(parser.parse_args().base.resolve())
