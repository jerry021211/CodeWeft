"""Observer delivery and control precedence are independent contracts."""
import unittest

from codeagent.hooks import HookDecision, HookManager


class HookManagerTests(unittest.TestCase):
    def test_observer_results_never_short_circuit_or_become_decisions(self):
        hooks, seen = HookManager(), []
        hooks.observe('event', lambda: seen.append('first') or HookDecision('finalize'))
        hooks.observe('event', lambda: seen.append('second') or 'stop')
        hooks.register('event', lambda: seen.append('control') or 'blocked')
        self.assertIsNone(hooks.notify('event'))
        self.assertEqual(seen, ['first', 'second'])
        self.assertEqual(hooks.decide('event'), 'blocked')
        self.assertEqual(seen, ['first', 'second', 'control'])

    def test_control_precedence_and_legacy_trigger_are_preserved(self):
        hooks, seen = HookManager(), []
        hooks.register('event', lambda: seen.append('unreached'))
        decision = HookDecision('respond', 'blocked')
        hooks.register('event', lambda: decision, first=True)
        hooks.observe('event', lambda: seen.append('observer'))
        self.assertIs(hooks.decide('event'), decision)
        self.assertIs(hooks.trigger('event'), decision)
        self.assertEqual(seen, [])

    def test_observer_failure_is_reported_after_other_observers_run(self):
        hooks, seen = HookManager(), []
        failure = OSError('recording unavailable')
        def fail():
            raise failure
        def also_fail():
            raise ValueError('second failure')
        hooks.observe('event', fail)
        hooks.observe('event', also_fail)
        hooks.observe('event', lambda: seen.append('recorded'))
        with self.assertRaises(OSError) as raised:
            hooks.notify('event')
        self.assertIs(raised.exception, failure)
        self.assertEqual(seen, ['recorded'])
        self.assertIn('second failure', failure.__notes__[0])

    def test_copy_preserves_custom_observers_but_excludes_agent_local_owners(self):
        class Local:
            agent_local = True
            def handler(self):
                raise AssertionError('parent observer leaked')
        hooks, seen = HookManager(), []
        hooks.observe('event', Local().handler)
        hooks.register('event', Local().handler)
        hooks.observe('event', lambda: seen.append('custom'))
        clone = hooks.copy()
        self.assertTrue(clone.has_handlers)
        clone.notify('event')
        self.assertIsNone(clone.decide('event'))
        self.assertEqual(seen, ['custom'])
        clone.observe('event', lambda: seen.append('clone'))
        self.assertEqual(len(hooks._observers['event']), 2)

    def test_copy_rebinds_and_excludes_owners_for_both_channels(self):
        class Owner:
            def handler(self):
                raise AssertionError('excluded owner ran')
        hooks, owner, seen = HookManager(), Owner(), []
        for register in (hooks.register, hooks.observe):
            register('event', owner.handler)
            register('event', lambda: 'original')
        clone = hooks.copy(exclude_owner=owner, rebind=lambda _: lambda: seen.append('rebound'))
        clone.notify('event')
        clone.decide('event')
        self.assertEqual(seen, ['rebound', 'rebound'])

    def test_process_interrupt_is_not_swallowed(self):
        hooks = HookManager()
        def interrupt():
            raise KeyboardInterrupt()
        hooks.observe('event', interrupt)
        with self.assertRaises(KeyboardInterrupt):
            hooks.notify('event')
