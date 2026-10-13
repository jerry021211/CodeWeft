"""python -m evals.cvm_verification --preview never loads credentials or calls APIs."""
import argparse
import json
from pathlib import Path
from evals.cvm_verification.cases import TITLES
from evals.cvm_verification.runner import plan, run_suite


def main():
    parser = argparse.ArgumentParser(description='CVM 验证建议开关对照；默认离线')
    parser.add_argument('--mode', choices=['offline', 'live'], default='offline')
    parser.add_argument('--cases', choices=list(TITLES), nargs='+')
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--max-api-calls', type=int, default=12)
    parser.add_argument('--max-tokens', type=int, default=4096)
    parser.add_argument('--timeout', type=float, default=180)
    parser.add_argument('--budget-cny', type=float, default=5)
    parser.add_argument('--order-seed', type=int, default=20261013)
    parser.add_argument('--output', type=Path, default=Path('eval-results'))
    parser.add_argument('--preview', action='store_true')
    args = vars(parser.parse_args())
    if args.pop('preview'):
        print(json.dumps({**plan(**args), 'new_model_calls': 0}, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({'directory': str(run_suite(**args))}, ensure_ascii=False))


if __name__ == '__main__':
    main()
