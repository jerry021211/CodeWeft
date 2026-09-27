"""Run with python -m evals.context_journey; defaults never call a real model."""
import argparse
import json
from pathlib import Path

from evals.context_journey.cases import TITLES, prepare
from evals.context_journey.runner import plan, run_suite
from evals.evidence import seal, verify_seal


def main():
    parser = argparse.ArgumentParser(description="连续任务保持评测：离线默认；live 才会付费调用模型")
    sub = parser.add_subparsers(dest="command", required=True)
    prepared = sub.add_parser("prepare", help="生成六套可阅读材料和项目，不调用模型")
    prepared.add_argument("--output", type=Path, required=True)
    prepared.add_argument("--log-chars", type=int, default=48000)
    prepared.add_argument("--batches", type=int, default=4)
    verified = sub.add_parser("verify", help="检查结果证据未被改动")
    verified.add_argument("directory", type=Path)
    run = sub.add_parser("run")
    run.add_argument("--mode", choices=["offline", "live"], default="offline")
    run.add_argument("--cases", nargs="+", choices=list(TITLES))
    run.add_argument("--variants", nargs="+", choices=list("ABCD"), default=["A", "D"])
    run.add_argument("--repeats", type=int, default=1)
    run.add_argument("--max-iterations", type=int, default=8, help="每个用户回合的轮数上限")
    run.add_argument("--max-tokens", type=int, default=2048)
    run.add_argument("--max-api-calls", type=int, default=64, help="整场所有阶段和摘要共享的调用上限")
    run.add_argument("--max-trials", type=int, default=12)
    run.add_argument("--max-total-api-calls", type=int, default=768)
    run.add_argument("--timeout", type=float, default=600, help="整场所有阶段合计秒数")
    run.add_argument("--order-seed", type=int, default=20260923)
    run.add_argument("--log-chars", type=int, default=48000)
    run.add_argument("--batches", type=int, default=4)
    run.add_argument("--output", type=Path, default=Path("eval-results"))
    run.add_argument("--preview", action="store_true", help="只显示计划，不读凭据、不调用模型")
    args = vars(parser.parse_args())
    command = args.pop("command")
    if command == "prepare":
        root = prepare(args.pop("output"), **args)
        seal(root)
        result = {"directory": str(root.resolve()), "new_model_calls": 0}
    elif command == "verify":
        result = verify_seal(args["directory"])
    elif args.pop("preview"):
        result = {**plan(**args), "new_model_calls": 0}
    else:
        root = run_suite(**args)
        result = {"directory": str(root)}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
