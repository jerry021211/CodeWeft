"""Evaluator-only additional inputs. Not registered as an Agent tool."""
import json
from decimal import Decimal
from pathlib import Path
import sys
from evals.context_journey.checker import pure_module


def check(workspace, suite):
    root = Path(workspace).resolve()
    path = root / 'src' / f'{suite}.py'
    try:
        if path.is_symlink() or not path.resolve().is_relative_to(root) or path.stat().st_size > 30000:
            raise ValueError('Invalid source')
        ns = pure_module(path.read_text('utf-8'))
        if suite == 'parser':
            fn = ns['parse_record']
            checks = [fn(' \tQ9\t | -18.25 \n') == {'id': 'Q9', 'amount': '-18.25'},
                      fn('empty| 0.00') == {'id': 'empty', 'amount': '0.00'}]
        elif suite == 'totals':
            fn = ns['total_amount']
            total, empty = fn([{'amount': '-8.75'}, {'amount': 9}, {'amount': '0.50'}]), fn([])
            numeric = lambda value: isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)
            checks = [numeric(total) and abs(float(total) - .75) < 1e-9, numeric(empty) and empty == 0]
        else:
            fn = ns['export']
            checks = [json.loads(fn([{'id': '核对', 'amount': '-9.75', 'extra': 'omit'}])) == [{'order_id': '核对', 'amount': '-9.75'}],
                      json.loads(fn([{'id': 'zero', 'amount': 0}])) == [{'order_id': 'zero', 'amount': '0.00'}], json.loads(fn([])) == []]
        return {'suite': suite, 'passed': all(checks), 'checks_total': len(checks), 'checks_passed': sum(checks)}
    except Exception as error:
        return {'suite': suite, 'passed': False, 'error_type': type(error).__name__}


if __name__ == '__main__':
    print(json.dumps(check(sys.argv[1], sys.argv[2])))
