"""Fixed correct implementations prove wiring only, never model quality."""
from types import SimpleNamespace

SOLUTIONS = {
 'J01': {'src/search.py': "def search(rows, query):\n    return [r for r in rows if query.lower() in r['title'].lower()] if query else []\n"},
 'J02': {'src/service.py': '''def get_user(cache, tenant, user_id, load):
    key = (tenant, user_id)
    if key not in cache:
        cache[key] = load(tenant, user_id)
    return cache[key]

def save_user(cache, tenant, user_id, value, save):
    result = save(tenant, user_id, value)
    cache.pop((tenant, user_id), None)
    return result
'''},
 'J03': {'src/retry.py': '''def handle(responses):
    delays = []
    for i, r in enumerate(responses[:3]):
        if r['status'] in (429, 503) and i < 2 and i + 1 < len(responses):
            delays.append(r.get('retry_after', 1))
        else:
            return {'status': r['status'], 'body': r.get('body', ''), 'delays': delays}
    return {'status': 0, 'body': '', 'delays': delays}
'''},
 'J04': {'src/report.py': "import json\ndef render(rows):\n    return json.dumps([{'id': r['id'], 'amount': r['amount']} for r in rows])\n",
         'src/consumer.py': "import json\ndef total_from_report(rows, render):\n    return sum(float(r['amount']) for r in json.loads(render(rows)))\n"},
 'J05': {'src/orders.py': "def valid_orders(rows):\n    return [r for r in rows if r['id'] is not None and r['id'] != '']\n"},
}


class OfflineSDK:
    def __init__(self, seed):
        self.seed, self.messages, self.serial = seed, self, 0

    def begin(self, phase):
        self.queue = [('read_file', {'file_path': 'docs/contract.md'})]
        if phase['id'] not in {'analyze', 'challenge'}:
            self.queue += [('write_file', {'file_path': p, 'content': text}) for p, text in SOLUTIONS[self.seed['case_id']].items()]
            self.queue.append(('run_project_tests', {'suite': 'engineering'}))

    def create(self, **kwargs):
        self.serial += 1
        if self.queue:
            name, args = self.queue.pop(0)
            content, stop = [{'type': 'tool_use', 'id': f'offline-{self.serial}', 'name': name, 'input': args}], 'tool_use'
        else:
            content, stop = [{'type': 'text', 'text': '离线固定脚本，仅用于验收接线。'}], 'end_turn'
        return SimpleNamespace(content=content, stop_reason=stop, usage=None)

    def with_options(self, **kwargs):
        return self

    def close(self):
        pass
