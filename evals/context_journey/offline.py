"""Answer-injected wiring scripts. Never use their pass rate as model quality."""
import json
from types import SimpleNamespace

from evals.context_suite.request_kinds import is_summary_request

EXPORTER = '''import json
def export(rows):
    return json.dumps([{"order_id": row["id"], "amount": format(float(row["amount"]), ".2f")} for row in rows], ensure_ascii=False)
'''
PARSER = '''def parse_record(line):
    parts = line.split("|")
    return {"id": parts[0].strip(), "amount": parts[1].strip()}
'''
TOTALS = '''def total_amount(rows):
    return sum(float(row["amount"]) for row in rows)
'''


class OfflineSDK:
    def __init__(self, seed, gold):
        self.messages = self
        self.seed, self.gold = seed, gold
        self.phase = None
        self.queue = []
        self.serial = 0
        self.archive_path = None

    def with_options(self, **kwargs):
        return self

    def close(self):
        pass

    def post(self, path, *, body, cast_to):
        return self.create(**body)

    def begin(self, phase):
        self.phase = phase
        case = self.seed["case_id"]
        self.queue = []

        def read(path):
            self.queue.append(("read_file", {"file_path": path}))

        def write(path, text):
            self.queue.append(("write_file", {"file_path": path, "content": text}))

        def test(suite):
            self.queue.append(("run_project_tests", {"suite": suite}))

        if phase.get("evidence_file"):
            read(phase["evidence_file"])
        elif phase["id"] == "repair":
            read("src/parser.py")
            write("src/parser.py", PARSER)
            test("parser")
        elif phase["id"] == "modify":
            read("src/totals.py")
            write("src/totals.py", TOTALS)
        elif phase["id"] == "incident":
            self.queue.append(("read_incident_log", {}))
        elif phase["id"] == "initial":
            read("README.md")
        elif phase["id"] == "deliver":
            if case in {"T01", "T03"}:
                read("src/exporter.py")
                write("src/exporter.py", EXPORTER)
                test("exporter")
            elif case == "T02":
                write("reports/candidate.json", json.dumps(self.gold["expected"]))
            elif case == "T06" and self.archive_path:
                self.queue.append(("load_tool_output", {"file_path": self.archive_path, "offset": 701, "limit": 1}))

    def create(self, **kwargs):
        self.serial += 1
        if is_summary_request(kwargs):
            # Explicit mock memory, independent of real semantic quality.
            text = "离线固定摘要，只验证拼装/切点/连续调用。\n" + "\n".join(
                p["prompt"].split("\n\n以下是启动时已有的历史运行记录", 1)[0]
                for p in self.seed["phases"] if not p.get("evidence_file"))
            content, stop = [{"type": "text", "text": text[:3500]}], "end_turn"
        elif self.queue:
            name, args = self.queue.pop(0)
            content = [{"type": "tool_use", "id": f"journey_offline_{self.serial}", "name": name, "input": args}]
            stop = "tool_use"
        else:
            text = json.dumps(self.gold["final_fields"], ensure_ascii=False) if self.phase["id"] == "deliver" and "final_fields" in self.gold else "离线脚本阶段完成；非模型质量结果。"
            content, stop = [{"type": "text", "text": text}], "end_turn"
        return SimpleNamespace(content=content, stop_reason=stop, usage=None)
