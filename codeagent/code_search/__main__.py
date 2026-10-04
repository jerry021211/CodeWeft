"""Explicit local index maintenance, independent of Agent/chat credentials."""
import argparse
import json
import math
from pathlib import Path
import time
from codeagent.code_intelligence.models import QueryContext
from codeagent.code_search.index import CodeIndex
from codeagent.code_search.vector import VectorIndex
from codeagent.runtime.data_paths import RuntimeDataPaths


def main():
    parser = argparse.ArgumentParser(description='Build or inspect a workspace code index. Lexical/offline by default.')
    parser.add_argument('operation', choices=('index', 'status'))
    parser.add_argument('--workspace', type=Path, default=Path.cwd())
    parser.add_argument('--embedding', action='store_true', help='Explicitly build vectors using configured remote provider')
    parser.add_argument('--max-chunks', type=int, default=2000, help='Per-invocation resumable embedding budget')
    parser.add_argument('--timeout', type=float, help='Optional time limit in seconds; unlimited by default')
    args = parser.parse_args()
    if (args.timeout is not None and (not math.isfinite(args.timeout) or args.timeout <= 0)) or args.max_chunks <= 0:
        parser.error('timeout and max-chunks must be positive')
    deadline = time.monotonic() + args.timeout if args.timeout is not None else float('inf')
    context = QueryContext(remaining_seconds=lambda: deadline - time.monotonic())
    index = CodeIndex(args.workspace, RuntimeDataPaths.default().code_index_dir(args.workspace))
    try:
        index.sync(context.check)
        result = {'coverage': index.coverage, 'scan_complete': not index.incomplete,
                  'generation': index.generation, 'backend': index.backend, 'notes': index.notes,
                  'languages': sorted({d['language'] for _, d in index.documents})}
        if args.embedding:
            from codeagent.config import embedding_config_from_env, _load_dotenv
            from codeagent.code_search.embedding import ProviderHandle
            _load_dotenv()
            provider = embedding_config_from_env().create_provider()
            if provider is None:
                parser.error('Embedding is not explicitly enabled/configured')
            vector = VectorIndex(index, ProviderHandle(provider), args.max_chunks)
            vector.build('.', context.check, context.remaining_seconds, enabled=args.operation == 'index')
            result['embedding'] = vector.stats
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        index.close_snapshot()


if __name__ == '__main__':
    main()
