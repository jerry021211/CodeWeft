"""Apply a revised independent oracle to every retained trial, preserving originals."""
import argparse
from pathlib import Path
import shutil
from evals.evidence import file_hash, read_json, seal, verify_seal, write_json
from evals.cvm_verification.grading import grade, write_report


def reassess(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    if not verify_seal(source)['valid']:
        raise ValueError('Original evidence changed; do not regrade as an intact experiment')
    if output == source or output.is_relative_to(source):
        raise ValueError('Write regrading beside the sealed run, never inside it')
    output.mkdir(parents=True, exist_ok=False)
    checker = output / 'checker.py'
    shutil.copyfile(Path(__file__).with_name('checker.py'), checker)
    results = []
    original = read_json(source / 'result.json')
    for old in original['trials']:
        trial = source / old['trial_directory']
        result = grade(trial, old['execution'], source / 'engine-snapshot', checker_path=checker,
                       output=output / old['trial_directory'])
        results.append(result)
    write_report(output, results, read_json(source / 'suite.json'), original['budget'])
    with (output / 'report.md').open('a', encoding='utf-8') as report:
        report.write(f'\n本目录是统一重评分结果，没有新增模型调用。原始答复、请求和工具记录位于 [{source.name}]({source.as_uri()})，按同名 trial 目录查阅。\n')
    write_json(output / 'reassessment.json', {'source': str(source), 'source_checksums_sha256': file_hash(source / 'checksums.json'),
               'checker_sha256': file_hash(checker), 'reason': '数值合计允许 Decimal；纠正独立验收器的 Decimal 与 float 混算错误。所有场次统一重评分，不重新请求模型。',
               'new_model_calls': 0, 'changed_results': [r['trial_directory'] for r, old in zip(results, original['trials']) if r['automatic_success'] != old['automatic_success']]})
    seal(output)
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    print(reassess(args.source, args.output))
