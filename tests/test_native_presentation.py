from unittest import TestCase
from codeagent.multiagent.presentation import enrich
from codeagent.events.collaboration import compact_snapshot


class PresentationTests(TestCase):
    def state(self):
        return dict(id='c', status='running', tasks=[dict(id='1', depends_on=[]), dict(id='2', depends_on=['1'])],
            records={'1': dict(status='running', activity='model_waiting'), '2': dict(status='pending')})

    def test_model_wait_is_not_human_wait_and_dependencies_are_named(self):
        raw = self.state()
        shown = enrich(raw)
        self.assertIn('无需操作', shown['records']['1']['reason'])
        self.assertIn('#1', shown['records']['2']['reason'])
        self.assertNotIn('reason', raw['records']['1'])
        raw['records']['1']['activity'] = 'permission_waiting'
        self.assertIn('审批卡片', enrich(raw)['records']['1']['reason'])

    def test_pause_does_not_show_stale_activity_and_unknown_write_survives_compaction(self):
        raw = self.state()
        raw['status'] = 'paused'
        raw['records']['1'].update(status='paused', reason='现场已保留', inflight={'effect': 'write'})
        shown = enrich(raw)
        self.assertEqual(shown['records']['1']['reason'], '现场已保留')
        self.assertTrue(compact_snapshot(shown)['recovery']['required'])
        raw['records']['1']['inflight'] = {}
        self.assertEqual(enrich(raw)['recovery']['action'], 'resume')
