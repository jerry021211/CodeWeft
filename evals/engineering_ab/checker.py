"""Fixed public checks and held-out inputs; executes outside model workspace."""
import json
from pathlib import Path
import sys
from evals.context_journey.checker import pure_module


def check(workspace, case, hidden=False):
    root = Path(workspace).resolve()
    checks = []
    def module(name):
        path = root / 'src' / (name + '.py')
        if path.is_symlink() or not path.resolve().is_relative_to(root) or path.stat().st_size > 30000:
            raise ValueError('Invalid source path/size')
        return pure_module(path.read_text('utf-8'))
    try:
        if case == 'J01':
            fn = module('search')['search']
            rows = [{'title': 'Alpha', 'id': 1}, {'title': 'Beta ALPHA', 'id': 2}, {'title': 'Gamma', 'id': 3}]
            before = json.dumps(rows)
            checks = [fn(rows, 'aLpHa') == rows[:2], fn(rows, '') == [], fn(rows, 'missing') == [], json.dumps(rows) == before]
            if hidden:
                checks += [fn([{'title': '订单 Q9'}], 'q9') == [{'title': '订单 Q9'}], fn([], 'x') == []]
        elif case == 'J02':
            ns = module('service')
            get, save = ns['get_user'], ns['save_user']
            calls, writes, data, cache = [], [], {('a', 7): 'A', ('b', 7): 'B'}, {}
            def loader(tenant, key):
                calls.append((tenant, key))
                return data.get((tenant, key))
            def writer(tenant, key, value):
                writes.append((tenant, key, value))
                data[(tenant, key)] = value
                return value
            checks = [get(cache, 'a', 7, loader) == 'A', get(cache, 'a', 7, loader) == 'A', len(calls) == 1,
                      get(cache, 'b', 7, loader) == 'B']
            save(cache, 'a', 7, 'NEW', writer)
            checks += [writes == [('a', 7, 'NEW')], get(cache, 'a', 7, loader) == 'NEW', get(cache, 'b', 7, loader) == 'B']
            if hidden:
                start = len(calls)
                checks += [get(cache, 'none', 99, loader) is None, get(cache, 'none', 99, loader) is None, len(calls) == start + 1]
        elif case == 'J03':
            fn = module('retry')['handle']
            checks = [fn([]) == {'status': 0, 'body': '', 'delays': []},
                      fn([{'status': 429, 'retry_after': 4}, {'status': 200, 'body': 'ok'}]) == {'status': 200, 'body': 'ok', 'delays': [4]},
                      fn([{'status': 400}, {'status': 200}]) == {'status': 400, 'body': '', 'delays': []}]
            if hidden:
                checks += [fn([{'status': 503}] * 4) == {'status': 503, 'body': '', 'delays': [1, 1]},
                           fn([{'status': 429, 'retry_after': 0}]) == {'status': 429, 'body': '', 'delays': []}]
        elif case == 'J04':
            render = module('report')['render']
            consume = module('consumer')['total_from_report']
            rows = [{'id': 'A', 'amount': '2.5', 'omit': 'private'}, {'id': 'B', 'amount': 1}]
            checks = [json.loads(render(rows)) == [{'id': 'A', 'amount': '2.5'}, {'id': 'B', 'amount': 1}],
                      abs(float(consume(rows, render)) - 3.5) < 1e-9, consume([], render) == 0]
            if hidden:
                rows = [{'id': '含,逗号', 'amount': '-7.25'}, {'id': 'B', 'amount': 8}]
                checks += [json.loads(render(rows)) == rows, abs(float(consume(rows, render)) - .75) < 1e-9]
        elif case == 'J05':
            fn = module('orders')['valid_orders']
            rows = [{'id': 0}, {'id': None}, {'id': ''}, {'id': 3}]
            before = json.dumps(rows)
            checks = [fn(rows) == [rows[0], rows[3]], json.dumps(rows) == before]
            if hidden:
                checks += [fn([{'id': '0'}, {'id': ' '}, {'id': -1}]) == [{'id': '0'}, {'id': ' '}, {'id': -1}], fn([]) == []]
        else:
            raise ValueError('Unknown case')
        return {'passed': all(checks), 'checks_total': len(checks), 'checks_passed': sum(checks)}
    except Exception as error:
        return {'passed': False, 'error_type': type(error).__name__}


if __name__ == '__main__':
    print(json.dumps(check(sys.argv[1], sys.argv[2], '--hidden' in sys.argv[3:])))
