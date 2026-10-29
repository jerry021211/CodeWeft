"""Durable user/approved-plan context, independent of model task decomposition."""
from codeagent.messages import extract_text


def execution_context(service, conversation, run_id, messages):
    requests = []
    offset = 0
    run = service.get_run(run_id)
    relevant_runs = {run_id} if run else set()
    while True:
        page = service.list_messages(conversation, offset=offset)
        for message in page:
            # Queued future turns are not instructions for this execution yet.
            if run and message.run_id != run_id and message.created_at > run.created_at:
                continue
            if message.role == 'user' and not message.metadata.get('runtime'):
                text = extract_text(message.content)
                if text:
                    requests.append(dict(id=message.id, text=text))
                    if message.run_id:
                        relevant_runs.add(message.run_id)
        if len(page) < 1000:
            break
        offset += len(page)
    # Direct API users/tests may have no persisted user messages. Never promote
    # tool results or runtime reminders into user instructions.
    if not requests:
        for index, message in enumerate(messages):
            if message.get('role') == 'user' and message.get('_context_source') != 'runtime':
                text = extract_text(message.get('content'))
                if text:
                    requests.append(dict(id=f'{run_id}:user:{index}', text=text))
    answers = []
    for key in sorted(relevant_runs):
        for question in service.list_user_questions(key):
            if question['status'] == 'answered':
                answers.append({field: question[field] for field in ('id', 'question', 'answer', 'resolved_at')})
    result = dict(requests=requests, answers=sorted(answers, key=lambda q: q['resolved_at'] or ''))
    plan_id = run.metadata.get('approved_plan_id') if run else None
    if plan_id:
        plan = service.get_planning_revision(plan_id)
        if (plan['conversation_id'] != conversation
                or plan['status'] not in {'approved', 'starting', 'started', 'blocked'}
                or run_id not in {'run_' + plan_id, plan.get('execution_run_id')}):
            raise ValueError('Execution context requires the matching approved plan')
        result['plan'] = {key: plan[key] for key in ('id', 'revision', 'content_hash', 'markdown', 'payload')}
    return result


def merge_context(previous, current):
    """Keep prior requirements across compaction/restarts; append new user turns."""
    requests = {r['id']: r for r in previous.get('requests', [])}
    for request in current.get('requests', []):
        requests.setdefault(request['id'], request)
    result = dict(previous, requests=list(requests.values()))
    if "approval_mode" in previous or "approval_mode" in current:
        # A later root turn may add instructions, but cannot widen an existing
        # execution's permission snapshot. Old executions retain legacy behavior.
        result["approval_mode"] = previous.get("approval_mode") if previous else current.get("approval_mode")
    answers = {a['id']: a for a in previous.get('answers', [])}
    for answer in current.get('answers', []):
        answers.setdefault(answer['id'], answer)
    result['answers'] = list(answers.values())
    if current.get('plan'):
        if previous.get('plan') and previous['plan'] != current['plan']:
            raise ValueError('Approved plan changed; start a follow-up execution')
        result['plan'] = current['plan']
    return result
