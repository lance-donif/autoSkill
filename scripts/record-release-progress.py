"""Best-effort CI progress over Git, including when the caller cannot read the Actions API."""
import argparse
import json
import os
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', required=True)
    args = parser.parse_args()
    folder = Path(tempfile.mkdtemp(prefix='autoskill-ci-progress-')) / 'checkout'

    def git(*argv, cwd=None):
        return subprocess.run(['git', *argv], cwd=cwd, check=True, capture_output=True, text=True)

    try:
        git('fetch', 'origin', 'main')
        git('worktree', 'add', '--detach', str(folder), 'origin/main')
        result = {'tag': os.environ['RELEASE_TAG'], 'source_commit': os.environ['RELEASE_COMMIT'],
                  'status': 'running', 'stage': args.stage, 'run_id': os.environ['RELEASE_RUN_ID'],
                  'run_url': f"https://github.com/{os.environ['RELEASE_REPOSITORY']}/actions/runs/{os.environ['RELEASE_RUN_ID']}",
                  'recorded_at': datetime.now(UTC).isoformat()}
        target = folder / 'reports/release-status.json'
        target.parent.mkdir(exist_ok=True)
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        git('config', 'user.name', 'github-actions[bot]', cwd=folder)
        git('config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com', cwd=folder)
        git('add', 'reports/release-status.json', cwd=folder)
        git('commit', '-m', f"docs: {result['tag']} release {args.stage}", cwd=folder)
        git('push', 'origin', 'HEAD:refs/heads/main', cwd=folder)
        print(json.dumps(result))
    except (OSError, subprocess.CalledProcessError) as error:
        # Publication validation does not depend on this optional progress write.
        print('Progress write unavailable: ' + type(error).__name__)
    finally:
        subprocess.run(['git', 'worktree', 'remove', '--force', str(folder)], capture_output=True)


if __name__ == '__main__':
    main()
