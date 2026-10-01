"""python -m evals.code_retrieval --help"""
import argparse
import json
from pathlib import Path

from .dataset import prepare, validate_source
from .scoring import compare, grade_run


def main():
    parser = argparse.ArgumentParser(description="项目代码定位评测：冻结题库、运行、判分、生成前后对比报告。")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="校验题目对应函数和证据仍存在，不调用模型")
    validate.add_argument("--source", type=Path, default=Path.cwd())
    freeze = commands.add_parser("prepare", help="冻结代码、题目、答案和当前评测程序")
    freeze.add_argument("--source", type=Path, default=Path.cwd())
    freeze.add_argument("--output", type=Path, required=True)
    execute = commands.add_parser("run", help="每题新会话，自动生成报告；默认仅离线检查")
    execute.add_argument("--bundle", type=Path, required=True)
    execute.add_argument("--output", type=Path, required=True)
    execute.add_argument("--source", type=Path, default=Path.cwd(), help="连接配置和改造后程序来源")
    execute.add_argument("--variant", choices=["baseline", "enhanced"], default="baseline")
    execute.add_argument("--mode", choices=["offline", "live"], default="offline")
    execute.add_argument("--split", choices=["dev", "test"], default="dev")
    execute.add_argument("--repeats", type=int, default=1)
    execute.add_argument("--timeout", type=float, default=120)
    execute.add_argument("--model")
    execute.add_argument("--tool-factory", help="改造后 search_code 工厂，格式 module:function")
    grade = commands.add_parser("grade", help="根据保留的结果重新统计，也支持复核后统一更换标签")
    grade.add_argument("--bundle", type=Path, required=True)
    grade.add_argument("--run", type=Path, required=True)
    grade.add_argument("--output", type=Path, required=True)
    grade.add_argument("--gold", type=Path)
    comparison = commands.add_parser("compare", help="生成 A/B 逐题和汇总报告")
    comparison.add_argument("--before", type=Path, required=True)
    comparison.add_argument("--after", type=Path, required=True)
    comparison.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "validate":
            print(json.dumps(validate_source(args.source.resolve()), ensure_ascii=False, indent=2))
        elif args.command == "prepare":
            print(prepare(args.source, args.output))
        elif args.command == "run":
            from .runner import run
            print(run(args.bundle.resolve(), args.output, source=args.source.resolve(), variant=args.variant,
                      mode=args.mode, split=args.split, repeats=args.repeats, timeout=args.timeout,
                      model=args.model, tool_factory=args.tool_factory))
        elif args.command == "grade":
            grade_run(args.bundle.resolve(), args.run.resolve(), args.output, gold=args.gold)
            print(args.output.resolve() / "report.md")
        else:
            compare(args.before, args.after, args.output)
            print(args.output.resolve() / "report.md")
    except (ValueError, FileExistsError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    main()
