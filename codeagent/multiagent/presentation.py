"""Explain native execution waits without inferring success or restarting work."""
from copy import deepcopy

ACTIVITIES = {
    'model_waiting': '等待模型回复，无需操作',
    'model_receiving': '正在接收模型回复',
    'tool_executing': '正在执行工具',
    'model_retry_wait': '模型请求重试等待中',
    'permission_waiting': '等待操作审批，请查看对话中的审批卡片',
    'user_input_waiting': '等待你的回答，请查看对话中的问题',
    'executing': '正在处理任务',
}


def enrich(value):
    result = deepcopy(value)
    records = result['records']
    active = result['status'] in {'pending', 'running', 'verifying', 'delivering'}
    for task in result['tasks']:
        own = records[task['id']]
        if active and own['status'] == 'pending':
            dependencies = [key for key in task['depends_on'] if records[key]['status'] != 'accepted']
            own['reason'] = ('等待前置任务 ' + '、'.join('#' + key for key in dependencies) + ' 通过检查'
                             if dependencies else '等待执行名额或文件/资源占用释放')
        elif active and own['status'] == 'running' and own.get('activity') in ACTIVITIES:
            own['reason'] = ACTIVITIES[own['activity']]
    unknown = bool(result.get('runtime_unknown')) or any(r.get('inflight') for r in records.values())
    result['recovery'] = dict(required=unknown, action='inspect' if unknown else 'resume',
        reason='存在结果尚未确认的操作，需要先检查现场；不会自动重放命令。' if unknown else
               '保留已完成成果和工作现场，继续未完成的执行。')
    return result
