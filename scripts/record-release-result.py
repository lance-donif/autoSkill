"""Persist CI evidence and publication result; no credentials enter repository files."""
import argparse
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--result', type=Path, required=True)
    parser.add_argument('--proof', type=Path, required=True)
    args = parser.parse_args()
    tag = os.environ['RELEASE_TAG']
    source = args.result / 'offline-package.json'
    published = os.environ['RELEASE_JOB_STATUS'] == 'success' and source.is_file()
    report = {'tag': tag, 'source_commit': os.environ['RELEASE_COMMIT'], 'status': 'published' if published else 'failed',
              'run_id': os.environ['RELEASE_RUN_ID'], 'recorded_at': datetime.now(UTC).isoformat(),
              'run_url': f"https://github.com/{os.environ['RELEASE_REPOSITORY']}/actions/runs/{os.environ['RELEASE_RUN_ID']}"}
    evidence = Path('reports/evidence/releases') / tag
    evidence.mkdir(parents=True, exist_ok=True)
    if args.proof.is_dir():
        shutil.copytree(args.proof, evidence, dirs_exist_ok=True)
    if published:
        data = json.loads(source.read_text(encoding='utf-8'))
        check_text = ('ZIP 结构、CRC、大小与 SHA-256 校验通过；按维护者要求未运行功能测试'
                      if data.get('verification', {}).get('mode') == 'archive-only'
                      else 'Windows CI verify、doctor --launch 和回归通过')
        shutil.copyfile(source, 'reports/offline-package.json')
        report.update({k: data[k] for k in ('bytes', 'sha256', 'release_url', 'download_url', 'asset_id')})
        report['verification'] = data.get('verification')
        if data.get('packages'):
            report['packages'] = [{k: p[k] for k in ('target', 'file', 'bytes', 'sha256', 'download_url', 'asset_id')} for p in data['packages']]
            report['package_count'] = len(data['packages'])
        path = Path('reports/交付验证报告.md')
        text = path.read_text(encoding='utf-8')
        text = text.replace('分支：`rebuild/replay-studio`（基于 `main` 的 `bf43edc`）', '源码现位于 `main`；以下开发阶段记录保留其原有验证边界')
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if line.startswith('| 源码 |'):
                lines[index] = f"| 源码 | `main` / `{tag}`（构建提交 `{data['built_from_commit']}`） | Skill 本体在 `skills/autoskill/`，DSH 插件在 `plugin/` |"
            if line.startswith('| Windows x64 完整离线包 |'):
                lines[index] = (f"| Windows x64 完整离线包 | `{data['file']}`（Release {tag}） | {data['bytes']:,} 字节。"
                                f"SHA-256：`{data['sha256']}`；{check_text}，DSH 智能体未验收 |")
        text = '\n'.join(lines) + '\n'
        if data.get('packages'):
            lines = text.splitlines()
            lines = [line for line in lines if not line.startswith(('| Linux x64 离线包 |', '| Linux ARM64 / UOS 离线包 |'))]
            position = next(i for i, line in enumerate(lines) if line.startswith('| Windows x64 完整离线包 |')) + 1
            for package in reversed(data['packages'][1:]):
                label = 'Linux ARM64 / UOS 离线包' if package['target'] == 'linux-arm64' else 'Linux x64 离线包'
                lines.insert(position, f"| {label} | `{package['file']}`（Release {tag}） | {package['bytes']:,} 字节；SHA-256：`{package['sha256']}`；需要系统共享库，UOS 真机未验证 |")
            text = '\n'.join(lines) + '\n'
        note = (f"\n最新发布 {tag}：构建自 `{data['built_from_commit']}`，大小 {data['bytes']:,} 字节，SHA-256 `{data['sha256']}`。"
                f'{check_text}；默认 DSH 沙箱智能体尚未验收，其他历史结果保持原有边界。\n')
        text = text.replace('## 6. 未验证', note + '\n## 6. 未验证', 1)
        path.write_text(text, encoding='utf-8')
        # Only source-side audit documents change after packaging. Keep shipped plugin docs
        # byte-identical to the tag from which the verified archive was built.
        # Platform bundles also ship docs/: do not mutate them after their archive is hashed.
        for filename in (() if data.get('packages') else ('docs/需求与验收清单.md',)):
            path = Path(filename)
            with path.open('a', encoding='utf-8') as stream:
                stream.write(f"\n发布 {tag}：{check_text}，证据见 `reports/evidence/releases/{tag}/`。"
                             '默认 DSH 沙箱智能体验收仍未完成。\n')
    Path('reports/release-status.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
