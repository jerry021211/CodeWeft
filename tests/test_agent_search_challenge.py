import hashlib
from pathlib import Path
import tempfile
import unittest
from evals.code_retrieval.challenge import chat_cost, complete_evidence


class AgentChallengeTests(unittest.TestCase):
    def test_disjoint_input_cache_and_output_billing(self):
        usage=dict(input_tokens=1000000,cache_creation_input_tokens=0,cache_read_input_tokens=2000000,output_tokens=100000)
        self.assertAlmostEqual(chat_cost(usage,dict(input_miss=1,input_hit=.02,output=4)),1.44)

    def test_all_evidence_anchors_and_current_quotes_required(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);raw=b'first\nother\nlast\n';(root/'a.py').write_bytes(raw)
            case={'targets':[dict(path='a.py',start=1,end=3,anchors=[1,3])]}
            one=dict(path='a.py',line=1,end_line=1,quote='first',content_hash=hashlib.sha256(raw).hexdigest())
            two=dict(path='a.py',line=3,end_line=3,quote='last',content_hash=one['content_hash'])
            self.assertFalse(complete_evidence(case,[one],root))
            self.assertTrue(complete_evidence(case,[one,two],root))
            self.assertFalse(complete_evidence(case,[one,{**two,'quote':'invented'}],root))
            self.assertFalse(complete_evidence({'targets':[]},[],root))


if __name__=='__main__':unittest.main()
