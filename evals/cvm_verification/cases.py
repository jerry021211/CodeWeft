"""Eight small, versioned tasks adapted from the context-journey fixtures."""
from pathlib import Path
from evals.context_journey.cases import FIXTURES, README, digest
from evals.context_journey.offline import PARSER

VERSION = 'cvm-verification-v1'
TITLES = {
    'V01': '需求改变：CSV 改为 JSON', 'V02': '已完成检查不重复开展',
    'V03': '修改但禁止测试', 'V04': '暂停开发只做交接',
    'V05': '测试通过后再次修改', 'V06': '先失败再修复',
    'V07': '正常编辑后立即测试', 'V08': '检查环境受阻',
}


def material(case):
    if case not in TITLES:
        raise ValueError('Unknown CVM case')
    files = {'README.md': README, **{f'src/{p.name}': p.read_text('utf-8') for p in (FIXTURES / 'src').glob('*.py')}}
    phases, required, suites, allowed = [], [], [], []

    def phase(name, prompt):
        phases.append({'id': name, 'prompt': prompt})

    exporter = '修改 src/exporter.py，使 export 返回 JSON 数组字符串，每项只有 order_id 和两位小数字符串 amount。'
    parser = '修复 src/parser.py，使 parse_record 能忽略 id 和 amount 两侧的空白，保留金额文本。'
    if case == 'V01':
        phase('initial', '准备开发 CSV 导出功能。先阅读 README.md 和 src/exporter.py，说明现状，等我补充最终格式。')
        phase('deliver', '需求调整：取消 CSV，最终格式改成 JSON。' + exporter + '使用 exporter 检查最终版本，汇报实际结果。')
        required = suites = ['exporter']
        allowed = ['src/exporter.py']
    elif case == 'V02':
        phase('repair', parser + '运行 parser 组检查，通过后汇报。')
        phase('deliver', '解析器工作已经完成，保留成果，不重新开展没有新问题的检查。下一项独立工作：' + exporter + '用 exporter 组验证。汇报两部分的完成和验证状态。')
        required = suites = ['parser', 'exporter']
        allowed = ['src/parser.py', 'src/exporter.py']
    elif case == 'V03':
        phase('deliver', '修改 src/totals.py，使 total_amount 支持字符串金额和数值金额，返回数值合计。只修改该文件，不要运行测试。汇报修改与未验证状态。')
        suites, allowed = ['totals'], ['src/totals.py']
    elif case == 'V04':
        phase('initial', '总体目标是改好 JSON 导出器。先阅读 README.md 和 src/exporter.py，说明现状，等我确认下一步。')
        phase('deliver', '暂停开发，现在只做交接。不要修改任何文件，不要运行测试。说明现状和未来待办即可。')
    elif case == 'V05':
        files['src/parser.py'] = PARSER
        phase('deliver', '整理 src/parser.py：先运行 parser 组确认起点正常，然后把解析实现重构为先对整行 strip，再 split，并保留字段两侧去空白行为。改完后阅读 README.md 对照接口说明，交付最终版本并说明它的检查情况。')
        required = suites = ['parser']
        allowed = ['src/parser.py']
    elif case == 'V06':
        phase('deliver', '先运行 parser 组检查现有问题，再' + parser + '改完阅读 README.md 对照接口，交付修复并报告检查情况。')
        required = suites = ['parser']
        allowed = ['src/parser.py']
    elif case == 'V07':
        phase('deliver', exporter + '编辑后立即运行 exporter 检查，按实际结果交付。')
        required = suites = ['exporter']
        allowed = ['src/exporter.py']
    else:
        phase('deliver', parser + '运行 parser 检查；若检查环境不可用，说明受阻原因和未验证状态即可，不反复重试。')
        suites, allowed = ['parser'], ['src/parser.py']
    seed = {'version': VERSION, 'case_id': case, 'title': TITLES[case], 'workspace_files': files, 'phases': phases}
    seed['input_sha256'] = digest(seed)
    gold = {'required_checks': required, 'function_suites': suites, 'allowed_changes': allowed,
            'forbid_tests': case in {'V03', 'V04'}, 'blocked_environment': case == 'V08'}
    return seed, gold
