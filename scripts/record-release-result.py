"""Persist CI evidence and publication result; no credentials enter repository files."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--result', type=Path, required=True)
    parser.add_argument('--proof', type=Path, required=True)
    args = parser.parse_args()
    tag = os.environ['RELEASE_TAG']
    source = args.result / 'offline-package.json'
    published = os.environ['RELEASE_JOB_STATUS'] == 'success' and source.is_file()
    report = {'tag': tag, 'source_commit': os.environ['RELEASE_COMMIT'], 'status': 'published' if published else 'failed',
              'run_id': os.environ['RELEASE_RUN_ID'], 'recorded_at': datetime.now(timezone.utc).isoformat(),
              'run_url': f"https://github.com/{os.environ['RELEASE_REPOSITORY']}/actions/runs/{os.environ['RELEASE_RUN_ID']}"}
    evidence = Path('reports/evidence/releases') / tag
    evidence.mkdir(parents=True, exist_ok=True)
    if args.proof.is_dir():
        shutil.copytree(args.proof, evidence, dirs_exist_ok=True)
    if published:
        data = json.loads(source.read_text(encoding='utf-8'))
        shutil.copyfile(source, 'reports/offline-package.json')
        report.update({k: data[k] for k in ('bytes', 'sha256', 'release_url', 'download_url', 'asset_id')})
        path = Path('reports/交付验证报告.md')
        text = path.read_text(encoding='utf-8')
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if line.startswith('| Windows x64 完整离线包 |'):
                lines[index] = (f"| Windows x64 完整离线包 | `{data['file']}`（Release {tag}） | {data['bytes']:,} 字节。"
                                f"SHA-256：`{data['sha256']}`；Windows CI verify、doctor --launch 和回归通过，DSH 智能体未验收 |")
        text = '\n'.join(lines) + '\n'
        note = (f"\n最新发布 {tag}：构建自 `{data['built_from_commit']}`，大小 {data['bytes']:,} 字节，SHA-256 `{data['sha256']}`。"
                'Windows CI 完成包内校验、两种浏览器真实启动和回归；默认 DSH 沙箱智能体尚未验收，其他历史结果保持原有边界。\n')
        text = text.replace('## 6. 未验证', note + '\n## 6. 未验证', 1)
        path.write_text(text, encoding='utf-8')
        for filename in ('plugin/COMPATIBILITY.md', 'docs/需求与验收清单.md'):
            path = Path(filename)
            with path.open('a', encoding='utf-8') as stream:
                stream.write(f"\n发布 {tag} 的 Windows 包经 CI 校验和启动验证，证据见 `reports/evidence/releases/{tag}/`。"
                             '默认 DSH 沙箱智能体验收仍未完成。\n')
    Path('reports/release-status.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
