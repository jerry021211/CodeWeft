import argparse
import json
from pathlib import Path
from evals.cvm_verification.runner import run_suite
from evals.engineering_ab import suite

parser = argparse.ArgumentParser()
parser.add_argument('--mode', choices=['offline', 'live'], default='offline')
parser.add_argument('--repeats', type=int, default=2)
parser.add_argument('--budget-cny', type=float, default=5)
parser.add_argument('--max-tokens', type=int, default=2048)
parser.add_argument('--cases', nargs='+', choices=list(suite.TITLES))
parser.add_argument('--output', type=Path, default=Path('eval-results/engineering-ab'))
parser.add_argument('--preview', action='store_true')
args = vars(parser.parse_args())
preview = args.pop('preview')
if preview:
    print(json.dumps(suite.plan(**args), ensure_ascii=False, indent=2))
else:
    print(run_suite(experiment=suite, **args))
