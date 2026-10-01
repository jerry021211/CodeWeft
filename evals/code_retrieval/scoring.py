"""Evidence-aware localization scoring and paired reports (stdlib only)."""
from __future__ import annotations

from collections import Counter
import ast
import json
import math
from pathlib import Path
import statistics

from evals.evidence import read_json, write_json
from . import VERSION
from .dataset import digest, load_bundle, read_jsonl
from .pricing import PRICING, format_money, money_number, price_usage, summarize_cost

METRICS = ("hit1", "hit5", "mrr5", "ndcg5", "group_recall5", "complete5")
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
SCORING_VERSION = VERSION + ":decorator-spans-v1"


def citation_documents(target: Path, docs):
    """Extend citation spans only; sealed corpus IDs and gold anchors stay intact."""
    sources = {}
    result = []
    for doc in docs:
        path = doc['path']
        if path not in sources:
            text = (target / path).read_text(encoding='utf-8')
            starts = {node.lineno: min([node.lineno, *(d.lineno for d in node.decorator_list)])
                      for node in ast.walk(ast.parse(text))
                      if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))}
            sources[path] = (text.splitlines(), starts)
        lines, starts = sources[path]
        start = starts.get(doc['start_line'], doc['start_line'])
        result.append(dict(doc, start_line=start, text='\n'.join(lines[start - 1:doc['end_line']])))
    return result


def parse_answer(value):
    if isinstance(value, str):
        value = value.strip()
        if value.startswith("```") and value.endswith("```"):
            value = value.split("\n", 1)[1].rsplit("```", 1)[0]
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return None
    if not isinstance(value, dict) or value.get("status") not in {"found", "not_found", "insufficient_evidence"}:
        return None
    if not isinstance(value.get("results"), list):
        return None
    if (value["status"] == "found") != bool(value["results"]):
        return None
    return value


def check_citation(item, docs):
    if not isinstance(item, dict):
        return None, [], "invalid_result"
    path, symbol, line, quote = (item.get(k) for k in ("path", "symbol", "line", "quote"))
    if not isinstance(path, str) or not isinstance(symbol, str) or type(line) is not int or line < 1:
        return None, [], "invalid_location"
    if not isinstance(quote, str) or not quote.strip() or len(quote) > 4000:
        return None, [], "missing_or_oversized_quote"
    quoted = quote.splitlines()
    if len(quoted) > 20:
        return None, [], "quote_exceeds_20_lines"
    path = path.replace("\\", "/")
    candidates = [d for d in docs if d["path"] == path and d["symbol"] == symbol
                  and d["start_line"] <= line <= d["end_line"]]
    if len(candidates) != 1:
        return None, [], "unknown_or_ambiguous_symbol"
    doc = candidates[0]
    offset = line - doc["start_line"]
    actual = doc["text"].splitlines()[offset:offset + len(quoted)]
    if [s.strip() for s in quoted] != [s.strip() for s in actual]:
        return doc, [], "quote_does_not_match_frozen_source"
    return doc, list(range(line, line + len(quoted))), None


def grade_case(case, prediction, docs):
    answer = parse_answer(prediction.get("answer"))
    completed = prediction.get("outcome") == "completed" and answer is not None
    expected = {m["doc_id"] for g in case["groups"] for m in g["members"]}
    judged_nonrelevant = set(case.get("nonrelevant", []))
    ranks, matched_groups, seen, details, review = [], set(), set(), [], []
    if completed:
        for rank, item in enumerate(answer["results"][:5], 1):
            doc, lines, error = check_citation(item, docs)
            doc_id = doc["_id"] if doc else None
            duplicate = doc_id is not None and doc_id in seen
            if doc_id:
                seen.add(doc_id)
            groups = {g["id"] for g in case["groups"] for member in g["members"]
                      if member["doc_id"] == doc_id and set(lines) & set(member["evidence_lines"])}
            relevant = bool(groups) and not error and not duplicate
            if relevant:
                ranks.append(rank)
                matched_groups.update(groups)
            if doc and not error and not duplicate and doc_id not in expected and doc_id not in judged_nonrelevant and expected:
                review.append({"rank": rank, "doc_id": doc_id, "reason": "unjudged_source_valid_candidate"})
            details.append({"rank": rank, "doc_id": doc_id, "relevant": relevant,
                            "groups": sorted(groups) if relevant else [], "error": error,
                            "duplicate": duplicate})
    positive = bool(expected)
    dcg = sum(1 / math.log2(rank + 1) for rank in ranks)
    idcg = sum(1 / math.log2(rank + 1) for rank in range(1, min(5, len(expected)) + 1))
    result = {
        "case_id": case["id"], "repeat": prediction.get("repeat", 1), "category": case["category"],
        "positive": positive, "completed": completed, "outcome": prediction.get("outcome", "missing"),
        "answer_status": answer["status"] if answer else "invalid_format",
        "hit1": float(1 in ranks), "hit5": float(bool(ranks)),
        "mrr5": 1 / ranks[0] if ranks else 0.0,
        "ndcg5": dcg / idcg if idcg else None,
        "group_recall5": len(matched_groups) / len(case["groups"]) if positive else None,
        "complete5": float(positive and len(matched_groups) == len(case["groups"])),
        "false_positive": bool(not positive and answer and answer["status"] == "found"),
        "correct_abstention": bool(not positive and completed and answer["status"] == "not_found"),
        "returned": len(details), "irrelevant_returned": sum(not d["relevant"] for d in details),
        "citations": details, "review_required": review,
        "duration_ms": prediction.get("duration_ms"), "setup_ms": prediction.get("setup_ms"),
        "usage_complete": prediction.get("usage_complete", False),
        "usage": prediction.get("usage", {}), "tool_calls": prediction.get("tool_calls"),
        "model_calls": prediction.get("model_calls"),
    }
    parts = price_usage(result["usage"]) if result["usage_complete"] else None
    result.update(cost=parts["total"] if parts else None, cost_components_cny=parts)
    return result


def mean(values):
    return statistics.mean(values) if values else None


def valid_number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def aggregate(rows):
    pos = [r for r in rows if r["positive"]]
    cross = [r for r in pos if r["category"] == "cross_file"]
    absent = [r for r in rows if not r["positive"]]
    durations = [r["duration_ms"] for r in rows if valid_number(r["duration_ms"])]
    setups = [r["setup_ms"] for r in rows if valid_number(r["setup_ms"])]
    tool_calls = [r["tool_calls"] for r in rows if valid_number(r["tool_calls"])]
    measured = [r for r in rows if r["usage_complete"] and all(valid_number(r["usage"].get(k)) for k in TOKEN_FIELDS)]
    returns = sum(r["returned"] for r in pos)
    return {
        "trials": len(rows), "questions": len({r["case_id"] for r in rows}),
        "positive_trials": len(pos), "absent_trials": len(absent), "cross_file_trials": len(cross),
        "completed_trials": sum(r["completed"] for r in rows),
        **{k: mean([r[k] for r in pos]) for k in METRICS},
        "cross_complete5": mean([r["complete5"] for r in cross]),
        "hit1_count": sum(r["hit1"] for r in pos), "hit5_count": sum(r["hit5"] for r in pos),
        "cross_complete_count": sum(r["complete5"] for r in cross),
        "false_positive_count": sum(r["false_positive"] for r in absent),
        "false_positive_rate": mean([int(r["false_positive"]) for r in absent]),
        "correct_abstention_count": sum(r["correct_abstention"] for r in absent),
        "incomplete_absent_count": sum(not r["completed"] or r["answer_status"] == "insufficient_evidence" for r in absent),
        "returned_noise_rate": sum(r["irrelevant_returned"] for r in pos) / returns if returns else None,
        "duration_median_ms": statistics.median(durations) if durations else None,
        "duration_measured_trials": len(durations),
        "usage_measured_trials": len(measured),
        "usage_totals": {k: sum(r["usage"][k] for r in measured) if len(measured) == len(rows) and rows else None for k in TOKEN_FIELDS},
        "usage_known_subtotals": {k: sum(r["usage"][k] for r in measured) for k in TOKEN_FIELDS},
        "input_token_median": statistics.median(sum(r["usage"][k] for k in TOKEN_FIELDS if k != "output_tokens") for r in measured) if measured else None,
        "setup_median_ms": statistics.median(setups) if setups else None,
        "tool_call_median": statistics.median(tool_calls) if tool_calls else None,
        "review_candidates": sum(len(r["review_required"]) for r in rows),
        "outcomes": dict(Counter(r["outcome"] for r in rows)),
        **summarize_cost(rows),
    }


def score_predictions(cases, predictions, docs, *, repeats=1):
    if type(repeats) is not int or repeats < 1:
        raise ValueError("repeats must be a positive integer")
    expected = {(c["id"], repeat) for c in cases for repeat in range(1, repeats + 1)}
    indexed = {}
    for prediction in predictions:
        key = (prediction.get("case_id"), prediction.get("repeat", 1))
        if key not in expected or key in indexed:
            raise ValueError(f"Duplicate or unexpected prediction: {key}")
        indexed[key] = prediction
    rows = [grade_case(c, indexed.get((c["id"], repeat), {"repeat": repeat, "outcome": "missing"}), docs)
            for c in cases for repeat in range(1, repeats + 1)]
    return {"metrics": aggregate(rows), "rows": rows, "pricing": PRICING,
            "by_category": {category: aggregate([r for r in rows if r["category"] == category])
                            for category in sorted({r["category"] for r in rows})}}


def fmt(value, *, percent=False):
    if value is None:
        return "未知/不适用"
    return f"{value * 100:.2f}%" if percent else f"{value:.3f}"


def report_text(report):
    m, meta = report["metrics"], report["meta"]
    status = "真实模型定位评测" if meta.get("mode") == "live" else "离线/导入结果；不能视为真实模型能力成绩"
    lines = ["# 代码定位评测报告", "", f"{status}。版本：{meta.get('variant')}。",
             f"{m['questions']} 道不同问题，{m['trials']} 次任务，完成 {m['completed_trials']} 次。",
             "评分对象是最终代码位置及原文证据；解释文本的语义正确性没有自动裁判。", "",
             "| 指标 | 结果 |", "| --- | --- |",
             f"| 首选命中 Hit@1 | {m['hit1_count']:g}/{m['positive_trials']} · {fmt(m['hit1'], percent=True)} |",
             f"| 前五命中 Hit@5 | {m['hit5_count']:g}/{m['positive_trials']} · {fmt(m['hit5'], percent=True)} |",
             f"| 跨文件找全 | {m['cross_complete_count']:g}/{m['cross_file_trials']} · {fmt(m['cross_complete5'], percent=True)} |",
             f"| 无答案误报 | {m['false_positive_count']}/{m['absent_trials']} · {fmt(m['false_positive_rate'], percent=True)} |",
             f"| 无答案正确拒答 / 未完成 | {m['correct_abstention_count']} / {m['incomplete_absent_count']} |",
             f"| MRR@5 / 二值 nDCG@5 | {fmt(m['mrr5'])} / {fmt(m['ndcg5'])} |",
             f"| 必需证据组覆盖率 | {fmt(m['group_recall5'], percent=True)} |",
             f"| 返回噪声比例 | {fmt(m['returned_noise_rate'], percent=True)} |",
             f"| 耗时中位数 | {fmt(m['duration_median_ms'])} ms（{m['duration_measured_trials']} 次有记录） |",
             f"| 输入 token 中位数 | {fmt(m['input_token_median'])}（{m['usage_measured_trials']} 次用量完整） |",
             f"| 工具调用次数中位数 | {fmt(m['tool_call_median'])} |",
             f"| 空闲时段估算总费用 | {format_money(m['cost'])} |",
             f"| 每题平均费用（含重复运行） | {format_money(m['cost_average_per_trial'])} |",
             f"| 用量完整题目的费用小计 | {format_money(m['cost_known_subtotal'])}（{m['cost_measured_trials']}/{m['trials']} 次任务） |",
             f"| 工具/索引初始化中位数 | {fmt(m['setup_median_ms'])} ms |", "",
             f"Token 分项总量：`{json.dumps(m['usage_totals'], ensure_ascii=False)}`。",
             "单价来自用户提供的截图，统一使用空闲时段价，不按实际调用时间切换；这是按指定单价计算的估算费用。", "",
             "| 费用分项 | 单价（元/百万 token） | 已统计金额 |", "| --- | --- | --- |",
             f"| 缓存命中输入 | 0.02 | {format_money(m['cost_known_components_cny']['cache_hit_input'])} |",
             f"| 缓存未命中输入（含缓存写入） | 1 | {format_money(m['cost_known_components_cny']['cache_miss_input'])} |",
             f"| 输出 | 4 | {format_money(m['cost_known_components_cny']['output'])} |", "",
             "费用 =（缓存命中输入 × 0.02 + 缓存未命中输入 × 1 + 输出 × 4）÷ 1,000,000 元。",
             "缓存写入在本次估算中按未命中输入处理；三类输入分别计数，避免把命中部分重复收费。",
             "失败任务只要用量完整也计费；有任务用量不完整时，总费用和整套平均费用标为未知，分项仅显示用量完整任务的小计。其他服务（如独立 embedding）的费用不在此统计内。", "",
             "超时、失败和缺失任务保留在分母；缺失用量不填零。输入中位数包含普通输入、缓存读取和缓存写入；分项分别保留。",
             "索引/工具初始化计入 setup_ms，若工具首次查询时才建索引，则会计入查询耗时。每题新索引，属于冷启动测试。", "",
             "| 类型 | 任务数 | Hit@1 | Hit@5 |", "| --- | --- | --- | --- |",
             *[f"| {category} | {group['trials']} | {fmt(group['hit1'], percent=True)} | {fmt(group['hit5'], percent=True)} |"
               for category, group in report["by_category"].items()], "",
             f"待复核候选：{m['review_candidates']}。存在于源码但未标注的候选暂按未命中计算；有此项时成绩为暂定，不能宣称已完成独立人工复核。", "",
             "| 题目 | 类型 | 执行 | Hit@1 | Hit@5 | 找全 | 待复核 | 估算费用（元） |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in report["rows"]:
        lines.append(f"| {r['case_id']}/r{r['repeat']} | {r['category']} | {r['outcome']} | {r['hit1']:g} | {r['hit5']:g} | {r['complete5']:g} | {len(r['review_required'])} | {format_money(r['cost'])} |")
    lines += ["", "本题库是项目专用 Python 评测，参考 RepoQA、CodeSearchNet、BEIR；不是其官方榜单成绩。",
              "题目由助手核对源码，尚未经过多人独立标注。小样本只支持本题集上的判断。"]
    return "\n".join(lines) + "\n"


def validate_labels(cases, docs):
    lookup = {d["_id"]: d for d in docs}
    for case in cases:
        groups = case["groups"]
        if bool(groups) == (case["category"] == "absent"):
            raise ValueError(f"{case['id']}: groups contradict answerability")
        if len({g["id"] for g in groups}) != len(groups):
            raise ValueError("Duplicate evidence group")
        relevant = set()
        for group in groups:
            if not group["members"]:
                raise ValueError("Empty evidence group")
            for member in group["members"]:
                doc = lookup.get(member["doc_id"])
                if not doc or not member["evidence_lines"] or any(type(line) is not int or not doc["start_line"] <= line <= doc["end_line"] for line in member["evidence_lines"]):
                    raise ValueError("Gold evidence must refer to real frozen code")
                relevant.add(member["doc_id"])
        if set(case.get("nonrelevant", [])) - lookup.keys() or relevant & set(case.get("nonrelevant", [])):
            raise ValueError("Invalid/conflicting nonrelevant labels")


def grade_run(bundle: Path, run: Path, output: Path, *, gold: Path | None = None):
    manifest, docs, cases = load_bundle(bundle)
    docs = citation_documents(bundle / "target", docs)
    meta = read_json(run / "run.json")
    if meta["corpus_hash"] != manifest["corpus_hash"] or meta["queries_hash"] != manifest["queries_hash"]:
        raise ValueError("Run and bundle use different corpus/questions")
    if meta.get("workspace_valid") is not True:
        raise ValueError("Run changed the evaluation source; results are invalid")
    if gold:
        revised = read_json(gold)["cases"]
        if [(c["id"], c["query"], c["split"], c["category"]) for c in revised] != [(c["id"], c["query"], c["split"], c["category"]) for c in cases]:
            raise ValueError("Regrading may change labels, not questions/categories/splits")
        cases = revised
    validate_labels(cases, docs)
    selected = [c for c in cases if c["split"] == meta["split"]]
    result = score_predictions(selected, read_jsonl(run / "predictions.jsonl"), docs, repeats=meta["repeats"])
    result.update(version=SCORING_VERSION, meta=meta, gold_hash=digest(cases))
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "result.json", result)
    (output / "report.md").write_text(report_text(result), encoding="utf-8")
    write_json(output / "review.json", [{"case_id": r["case_id"], "repeat": r["repeat"], **item}
                                        for r in result["rows"] for item in r["review_required"]])
    return result


def compare_reports(a, b):
    if a.get("pricing") != b.get("pricing"):
        raise ValueError("Incomparable pricing; regrade both runs with the same pricing rules")
    for key in ("corpus_hash", "queries_hash", "split", "repeats", "profile", "mode", "provider_models"):
        if a["meta"].get(key) != b["meta"].get(key):
            raise ValueError(f"Incomparable runs: {key} differs")
    if a["gold_hash"] != b["gold_hash"] or a["version"] != b["version"]:
        raise ValueError("Incomparable grading versions/qrels")
    left = {(r["case_id"], r["repeat"]): r for r in a["rows"]}
    right = {(r["case_id"], r["repeat"]): r for r in b["rows"]}
    if left.keys() != right.keys():
        raise ValueError("Different trial sets")
    pairs = []
    for key, before in left.items():
        after = right[key]
        pairs.append({"case_id": key[0], "repeat": key[1], "positive": before["positive"],
                      "before_hit5": before["hit5"], "after_hit5": after["hit5"],
                      "before_complete5": before["complete5"], "after_complete5": after["complete5"],
                      "before_false_positive": before["false_positive"], "after_false_positive": after["false_positive"],
                      "before_cost": before.get("cost"), "after_cost": after.get("cost")})
    deltas = {k: b["metrics"][k] - a["metrics"][k]
              if a["metrics"][k] is not None and b["metrics"][k] is not None else None
              for k in (*METRICS, "cross_complete5", "false_positive_rate", "duration_median_ms", "input_token_median", "setup_median_ms", "tool_call_median")}
    for key in ("cost", "cost_average_per_trial"):
        av, bv = a["metrics"].get(key), b["metrics"].get(key)
        deltas[key] = money_number(str(bv - av)) if av is not None and bv is not None else None
    before_cost = a["metrics"].get("cost")
    cost_change = deltas["cost"] / before_cost * 100 if before_cost and deltas["cost"] is not None else None
    return {"deltas": deltas, "pairs": pairs, "pricing": a.get("pricing"), "cost_change_percent": cost_change,
            "improved_trials": sum(p["positive"] and p["after_hit5"] > p["before_hit5"] for p in pairs),
            "regressed_trials": sum(p["positive"] and p["after_hit5"] < p["before_hit5"] for p in pairs)}


def compare(a_path: Path, b_path: Path, output: Path):
    a, b = read_json(a_path / "result.json"), read_json(b_path / "result.json")
    result = compare_reports(a, b)
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "comparison.json", result)
    lines = ["# 代码定位改造前后对比", "", "差值方向为 B − A；命中率差值用百分点，误报率越低越好。",
             "离线演示不能作为模型能力成绩。存在待复核候选时，以下指标仍为暂定。", "",
             "| 指标 | A | B | 差值 |", "| --- | --- | --- | --- |"]
    for key in ("hit1", "hit5", "cross_complete5", "false_positive_rate", "mrr5", "ndcg5"):
        delta = result["deltas"][key]
        diff = f"{delta * 100:+.2f} 个百分点" if delta is not None and key not in {"mrr5", "ndcg5"} else fmt(delta)
        percent = key not in {"mrr5", "ndcg5"}
        lines.append(f"| {key} | {fmt(a['metrics'][key], percent=percent)} | {fmt(b['metrics'][key], percent=percent)} | {diff} |")
    for key in ("duration_median_ms", "input_token_median", "setup_median_ms", "tool_call_median"):
        lines.append(f"| {key} | {fmt(a['metrics'][key])} | {fmt(b['metrics'][key])} | {fmt(result['deltas'][key])} |")
    for key, label in (("cost", "空闲时段估算总费用"), ("cost_average_per_trial", "每题平均费用")):
        lines.append(f"| {label} | {format_money(a['metrics'].get(key))} | {format_money(b['metrics'].get(key))} | {format_money(result['deltas'][key])} |")
    lines += ["", f"费用相对变化（B − A）/A：{fmt(result['cost_change_percent'])}%（负数表示节省；A 为零或用量缺失时不计算）。"
              if result["cost_change_percent"] is not None else "费用相对变化未知：A 为零或存在缺失用量。",
              "费用统一按用户指定的空闲时段价格估算：缓存命中输入 0.02、未命中输入 1、输出 4 元/百万 token。",
              "费用差值仅在双方全部任务用量完整时计算；否则查双方报告的已知小计与覆盖任务数。"]
    lines += ["", f"A 完成 {a['metrics']['completed_trials']}/{a['metrics']['trials']}，B 完成 {b['metrics']['completed_trials']}/{b['metrics']['trials']}；少完成任务时，耗时降低不能直接视为提速。",
              f"A 待复核 {a['metrics']['review_candidates']} 项，B 待复核 {b['metrics']['review_candidates']} 项。",
              "B 相对冻结程序的变化文件：`" + ", ".join(b["meta"].get("engine_changed_files", [])) + "`。如还改了提示词、上下文或旧工具，应视为组合改造，不能把变化全部归因于 search_code。"]
    lines += ["", f"仅按前五命中（Hit@5）统计的改善次数：{result['improved_trials']}；退步次数：{result['regressed_trials']}。首条命中变化另见上表 Hit@1。重复任务不等于更多独立问题。",
              "", "| 题目 | A 前五命中 | B 前五命中 | A 找全 | B 找全 | A 费用 | B 费用 |", "| --- | --- | --- | --- | --- | --- | --- |"]
    lines.extend(f"| {p['case_id']}/r{p['repeat']} | {p['before_hit5']:g} | {p['after_hit5']:g} | {p['before_complete5']:g} | {p['after_complete5']:g} | {format_money(p['before_cost'])} | {format_money(p['after_cost'])} |" for p in result["pairs"])
    lines += ["", "耗时与用量的差值见 comparison.json；原始分项和缺失情况见双方 report.md。",
              "本对比只测源码定位，不证明代码修改成功率提高。"]
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return result
