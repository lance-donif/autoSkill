"""Replay canonical UI/API cases into a new, immutable execution snapshot."""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import uuid
from . import engines


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--kind', choices=['ui', 'api'], required=True)
    parser.add_argument('--engine', choices=['script', 'ai'], default='script')
    parser.add_argument('--case', action='append', dest='cases')
    parser.add_argument('--chromium', default=os.environ.get('AUTOSKILL_CHROMIUM'))
    args = parser.parse_args(argv)
    project = args.project.resolve()
    output = (args.output or project.parent / (project.name + '-replays') / ('run-' + uuid.uuid4().hex)).resolve()
    try:
        rows = engines.execute(project, output, args.kind, args.engine, args.cases, args.chromium)
    except (ValueError, OSError) as error:
        print(json.dumps({'status': 'Blocked', 'reason': str(error) if isinstance(error, ValueError) else type(error).__name__, 'output': str(output)}, ensure_ascii=False))
        return 2
    counts = dict(Counter(row['status'] for row in rows))
    status = 'Fail' if counts.get('Fail') else ('Pass' if rows and counts.get('Pass') == len(rows) else 'Blocked')
    print(json.dumps({'status': status, 'counts': counts, 'output': str(output), 'engine': args.engine, 'kind': args.kind}, ensure_ascii=False))
    return {'Pass': 0, 'Fail': 1, 'Blocked': 2}[status]


if __name__ == '__main__':
    raise SystemExit(main())
