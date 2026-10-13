"""Small execution tools. TaskCreate/Get/List/Update own all task definitions."""
import json
from codeagent.tools.base import ToolDefinition

EXECUTION_TOOL_NAMES = frozenset({'delegate_task', 'delegate_batch', 'execute_plan', 'AgentRunStatus', 'AgentResumeRun',
                                 'AgentRecoverRun', 'AgentCloseRun'})


def register_execution_tools(registry, coordinator, recover, before=lambda: None):
    c = coordinator

    def start(taskIds, mode):
        before()
        task_list = c.repo.service.ensure_conversation_task_list(c.conversation)
        tasks = c.repo.inputs(task_list.id, taskIds, read_only=c.read_only)
        return json.dumps(c.start('；'.join(t['subject'] for t in tasks), taskIds, mode=mode), ensure_ascii=False)

    def single(taskId):
        return start([taskId], 'single')

    def batch(taskIds):
        if not 1 <= len(taskIds) <= 5:
            raise ValueError('delegate_batch accepts 1–5 tasks; use execute_plan for larger plans')
        return start(taskIds, 'batch')

    def plan(taskIds):
        return start(taskIds, 'plan')

    def status(executionId=None):
        before()
        if executionId:
            c.get(executionId)
            value = c.publish(executionId)
        else:
            value = [c.publish(r['id']) for r in c.repo.list(c.conversation, c.workspace)]
        return json.dumps(value, ensure_ascii=False)

    def resume(executionId, additionalTaskIds=None):
        before()
        c.get(executionId)
        if additionalTaskIds:
            c.repo.append_tasks(executionId, additionalTaskIds)
        return json.dumps(c.run(executionId), ensure_ascii=False)

    def recover_run(executionId):
        before()
        return json.dumps(recover(executionId), ensure_ascii=False)

    def close(executionId):
        before()
        return json.dumps(c.close(executionId), ensure_ascii=False)

    for name, description, handler in (
        ('delegate_task', '将一个已有 Task 委派给成员，在当前项目工作区完成后返回结果。适合边界明确的独立子任务。'
         '先用 TaskCreate 创建任务；用 metadata.execution 设置 profile/files/checks。无需团队计划或 Git。', single),
        ('delegate_batch', '批量执行 1–5 个已有 Task，按 blockedBy 与文件冲突安排并行，在当前项目工作区工作并聚合结果。'
         '适合一次性并行调查或小批次工作；需要持久计划和分阶段交付时使用 execute_plan。', batch),
        ('execute_plan', '执行原 Task 列表中的依赖计划，按依赖和文件冲突分波验证，在协作工作目录开发并统一回写本地。'
         '传 taskIds，任务要求和 metadata.execution 只通过 TaskCreate/TaskUpdate 维护。'
         '先用 AgentRunStatus 检查未完成执行，已有现场用 AgentResumeRun。', plan),
        ('AgentRunStatus', '查询当前会话的成员执行、验证证据和恢复原因。任务要求与状态使用 TaskGet/TaskList 查询。', status),
        ('AgentResumeRun', '继续已暂停或受阻的执行，复用 Task ID 和工作现场。普通失败先读取证据、通过 TaskUpdate 修正未完成任务。'
         '需要修改已验收成果时，用 TaskCreate 新建修正任务，再通过 additionalTaskIds 加入此次执行。', resume),
        ('AgentRecoverRun', '仅处理中断后结果不确定的操作；运行时提供恢复确认。不会盲目重放命令，完成确认后使用 AgentResumeRun。', recover_run),
        ('AgentCloseRun', '仅在用户放弃这次执行时关闭，保留工作文件和历史，未完成 Task 标记取消。先停止正在运行的执行。', close),
    ):
        schema = {'type': 'object', 'properties': {'executionId': {'type': 'string'}},
                  'required': [] if name == 'AgentRunStatus' else ['executionId']}
        if name == 'delegate_task':
            schema = {'type': 'object', 'properties': {'taskId': {'type': 'string'}}, 'required': ['taskId']}
        elif name in {'delegate_batch', 'execute_plan'}:
            schema = {'type': 'object', 'properties': {'taskIds': {'type': 'array', 'items': {'type': 'string'},
                      'minItems': 1, 'maxItems': 5 if name == 'delegate_batch' else 32}}, 'required': ['taskIds']}
        elif name == 'AgentResumeRun':
            schema['properties']['additionalTaskIds'] = {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 32}
        registry.register_handler(ToolDefinition(name, description, schema,
                                  effect='read' if name == 'AgentRunStatus' else 'exclusive',
                                  unlimited_time=name in {'delegate_task', 'delegate_batch', 'execute_plan', 'AgentResumeRun', 'AgentRecoverRun'}), handler)
