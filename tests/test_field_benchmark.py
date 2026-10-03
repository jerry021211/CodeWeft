import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from evals.code_retrieval.benchmark_sources import strip_documentation
from evals.code_retrieval.field_benchmark import matches, score, valid_quote


class FieldBenchmarkTests(unittest.TestCase):
    def test_proxy_query_is_removed_but_executable_strings_and_lines_survive(self):
        code='def echo():\n    """query words"""\n    # query words\n    return "http://value/*keep*/"\n'
        cleaned=strip_documentation(code,'python')
        self.assertNotIn('query words',cleaned)
        self.assertIn('http://value/*keep*/',cleaned)
        self.assertEqual(len(code.splitlines()),len(cleaned.splitlines()))
        java='/** query words */\nString f() { return "http://value/*keep*/"; } // query words\n'
        cleaned=strip_documentation(java,'java')
        self.assertNotIn('query words',cleaned)
        self.assertIn('http://value/*keep*/',cleaned)

    def test_overloads_are_distinguished_by_source_span(self):
        target={'path':'A.java','start':9,'end':12,'anchors':[10]}
        self.assertFalse(matches({'path':'A.java','line':2,'end_line':5},target))
        self.assertTrue(matches({'path':'A.java','line':9,'end_line':9},target))
        self.assertFalse(matches({'path':'A.java','line':9,'end_line':9},target,strict=True))
        self.assertTrue(matches({'path':'A.java','line':10,'end_line':11},target,strict=True))

    def test_current_source_validation_and_multi_evidence_denominator(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);raw=b'def first():\n    return 1\ndef second():\n    return 2\n'
            (root/'a.py').write_bytes(raw)
            one=dict(path='a.py',line=1,end_line=2,quote='def first():\n    return 1',content_hash=hashlib.sha256(raw).hexdigest(),entity_id='first')
            case={'targets':[dict(path='a.py',start=1,end=2,anchors=[2]),dict(path='a.py',start=3,end=4,anchors=[4])]}
            result=score(case,{'results':[one,one]},[],root)
            self.assertEqual(result['hit5'],1)
            self.assertEqual(result['complete5'],0)
            self.assertEqual(result['recall5'],.5)
            self.assertEqual(result['duplicate_results'],1)
            self.assertEqual(result['source_valid'],2)
            (root/'a.py').write_bytes(raw.replace(b'return 1',b'return 9'))
            self.assertFalse(valid_quote(root,one))
            self.assertEqual(score(case,{'results':[one]},[],root)['strict_hit5'],0)

    def test_empty_and_failed_calls_stay_in_denominator(self):
        result=score({'targets':[dict(path='x.py',start=1,end=2,anchors=[2])]}, {}, [], Path('.'))
        self.assertEqual(result['hit5'],0)
        self.assertEqual(result['candidate_recall80'],0)
        absent=score({'targets':[]},{'results':[],'absence_proven':False},[],Path('.'))
        self.assertFalse(absent['positive'])
        self.assertFalse(absent['absence_proven'])

    def test_reference_ranking_values_and_duplicate_gain(self):
        case={'targets':[dict(path='correct.py',start=1,end=2,anchors=[2])]}
        rows=[dict(path=path,line=1,end_line=2,entity_id=path) for path in ('wrong.py','other.py','correct.py','correct.py')]
        with patch('evals.code_retrieval.field_benchmark.valid_quote',return_value=True):
            result=score(case,{'results':rows},[],Path('.'))
        self.assertEqual(result['hit1'],0)
        self.assertEqual(result['mrr5'],1/3)
        self.assertEqual(result['ndcg5'],.5)
        self.assertEqual(result['strict_complete5'],1)

    def test_controlled_edit_records_non_thinking_mode_after_sdk_options(self):
        from evals.code_retrieval.field_agent import FaultFirstSDK, FixtureEvidenceSDK
        class SDK:
            def __init__(self):self.messages=self;self.sent=[]
            def with_options(self,**kw):return self
            def create(self,**kw):
                self.sent.append(kw)
                return SimpleNamespace(id='real-probe',model='fixture',content=[],stop_reason='end_turn',usage=None)
            def close(self):pass
        with tempfile.TemporaryDirectory() as directory:
            sdk=SDK();root=Path(directory)
            recorder=FixtureEvidenceSDK(FaultFirstSDK(sdk,{'file_path':'sample.py'}),root,max_calls=3)
            client=recorder.with_options(timeout=1)
            client.create(messages=[],max_tokens=10)
            client.create(messages=[],max_tokens=10)
            self.assertEqual(len(sdk.sent),1)
            self.assertEqual(sdk.sent[0]['extra_body']['thinking'],{'type':'disabled'})
            recorded=[json.loads(s) for s in (root/'model-requests.jsonl').read_text(encoding='utf-8').splitlines()]
            # The shared evidence sanitizer removes all `thinking` keys,
            # including transport configuration. The protocol amendment records
            # the mode; assert the actual SDK argument above, not retained prose.
            self.assertTrue(all(r['extra_body']=={} for r in recorded))
            recorder.close()

    def test_usage_separates_scripted_injection_cache_and_failed_requests(self):
        from evals.code_retrieval.field_agent import trace_summary
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            rows=[{'provider_response_id':'controlled-fault-injection','usage':None},
                  {'provider_response_id':'real','usage':{'input_tokens':20,'cache_read_input_tokens':80,'output_tokens':5}},
                  {'error_type':'BadRequestError'}]
            (root/'model-responses.jsonl').write_text('\n'.join(json.dumps(r) for r in rows),encoding='utf-8')
            summary=trace_summary(root)
            self.assertEqual(summary['model_requests'],2)
            self.assertFalse(summary['usage_complete'])
            self.assertEqual(summary['usage']['input_tokens'],20)
            self.assertIsNone(summary['cache_input_ratio'])


if __name__=='__main__':unittest.main()
