"""Publish only after Windows verification. Never print or persist the API token."""
import argparse
import hashlib
import json
import os
import re
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path


def api(method, path, body=None, binary=None):
    repo = os.environ['RELEASE_REPOSITORY']
    url = path if path.startswith('https://') else f'https://api.github.com/repos/{repo}{path}'
    headers = {'Authorization': 'Bearer ' + os.environ['GH_TOKEN'], 'Accept': 'application/vnd.github+json',
               'User-Agent': 'autoSkill-release', 'X-GitHub-Api-Version': '2022-11-28'}
    data = binary if binary is not None else json.dumps(body).encode() if body is not None else None
    if data is not None:
        headers['Content-Type'] = 'application/zip' if binary is not None else 'application/json'
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers, method=method), timeout=600) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('build', 'proof', 'result'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--skip-functional-tests', action='store_true', help='Explicit archive-only release; disclose missing test coverage')
    args = parser.parse_args()
    tag, commit = os.environ['RELEASE_TAG'], os.environ['RELEASE_COMMIT']
    version = json.loads(Path('package.json').read_text())['version']
    if tag != 'v' + version:
        raise ValueError('Release tag must match package.json version')
    doctor = verify = None
    if not args.skip_functional_tests:
        doctor = json.loads((args.proof / 'doctor.json').read_text(encoding='utf-8-sig'))
        verify = json.loads((args.proof / 'verify.json').read_text(encoding='utf-8-sig'))
        if not verify.get('ok'):
            raise ValueError('Packaged runtime verification must pass')
        if not doctor.get('ok') or doctor.get('browser_launch') != {'playwright': 'ok', 'selenium': 'ok'}:
            raise ValueError('Both packaged browser engines must launch successfully')
        tests = (args.proof / 'python-tests.txt').read_text(encoding='utf-8-sig')
        if not re.search(r'Ran \d+ tests in [\d.]+s\s+OK', tests):
            raise ValueError('Python regression evidence missing or failed')
    archive = args.build / 'autoSkill-Windows-x64-Offline.zip'
    with zipfile.ZipFile(archive) as bundle:
        required = {'package.json', 'cordis.patch.yml', 'plugin/index.mjs', 'autoskill/SKILL.md', 'autoskill/autoskill.cmd',
                    'autoskill/vendor/windows-x64/python/python.exe'}
        if not required.issubset(bundle.namelist()) or any(n.startswith('autoskill/workspace/') for n in bundle.namelist()):
            raise ValueError('Incorrect package layout or private workspace included')
        if bundle.testzip() is not None:
            raise ValueError('ZIP integrity check failed')
    manifest = json.loads((args.build / 'package/autoskill/offline-manifest.json').read_text(encoding='utf-8'))
    if manifest.get('autoskill') != version:
        raise ValueError('Offline manifest version mismatch')
    payload = archive.read_bytes()
    digest, size = hashlib.sha256(payload).hexdigest(), len(payload)
    args.proof.mkdir(parents=True, exist_ok=True)
    (args.proof / 'archive-check.json').write_text(json.dumps({
        'file': archive.name, 'bytes': size, 'sha256': digest, 'source_commit': commit,
        'layout_ok': True, 'crc_ok': True, 'functional_tests_run': not args.skip_functional_tests,
    }, indent=2) + '\n', encoding='utf-8')
    check_text = ('按维护者要求，本次只打包发布，未运行功能回归或浏览器启动测试。已检查 ZIP 结构、CRC、大小和 SHA-256。'
                  if args.skip_functional_tests else 'Windows CI 已完成包内 verify、doctor --launch（两种浏览器）和完整回归。')
    notes = (f'## autoSkill {tag}\n\nWindows x64 完整离线包；解压目录本身可在 DSH 中安装。'
             '\n\n本次包含 Linux 源码部署、移除站点代理围栏、系统代理兼容，以及跨 Tab/窗口录制、切换、关闭返回、两种引擎和独立脚本回放。'
             '\n\n' + check_text +
             '\n\n已知问题：Windows 独立 Selenium 导出脚本偶发停留在初始空白页（data:,），上一轮 CI 因此未通过；本次没有宣称此问题已修复。'
             '\n\nDSH 默认沙箱智能体验收仍未通过：开发环境模型网关被出站代理 403 阻断；不将后台转交模拟验证写成真实 DSH 验收。'
             '\n旧跨窗口用例需要重新录制。Linux 请下载源码并阅读 docs/Linux-DSH.md，本附件为 Windows 包。'
             f'\n\n源码提交：`{commit}`\n\n附件：`{archive.name}`\n大小：{size:,} 字节\nSHA-256：`{digest}`\n')
    releases = api('GET', '/releases?per_page=100')
    release = next((r for r in releases if r['tag_name'] == tag), None)
    if release is None:
        release = api('POST', '/releases', {'tag_name': tag, 'target_commitish': commit,
                                          'name': 'autoSkill ' + tag, 'body': notes, 'draft': True})
    temporary = archive.stem + '.' + os.environ.get('GITHUB_RUN_ID', 'new') + '.upload.zip'
    old = api('GET', f"/releases/{release['id']}/assets")
    for asset in old:
        if asset['name'] == temporary:
            api('DELETE', f"/releases/assets/{asset['id']}")
    uploaded = api('POST', release['upload_url'].split('{')[0] + '?name=' + urllib.parse.quote(temporary), binary=payload)
    checked = api('GET', f"/releases/assets/{uploaded['id']}")
    if checked['size'] != size or checked.get('digest') != 'sha256:' + digest:
        raise ValueError('Uploaded size/digest mismatch; prior final asset has been retained')
    for asset in old:
        if asset['name'] == archive.name:
            api('DELETE', f"/releases/assets/{asset['id']}")
    final = api('PATCH', f"/releases/assets/{uploaded['id']}", {'name': archive.name})
    (args.proof / 'upload-check.json').write_text(json.dumps({
        'asset_id': final['id'], 'name': final['name'], 'bytes': checked['size'],
        'digest': checked['digest'], 'matches_local_archive': True,
    }, indent=2) + '\n', encoding='utf-8')
    release = api('PATCH', f"/releases/{release['id']}", {'name': 'autoSkill ' + tag, 'body': notes,
                                                       'draft': False, 'prerelease': False, 'make_latest': 'true'})
    # A draft asset URL can contain an untagged-* placeholder until publication.
    final = api('GET', f"/releases/assets/{uploaded['id']}")
    result = {**manifest, 'file': archive.name, 'bytes': size, 'sha256': digest, 'built_from_commit': commit,
              'tag': tag, 'release_id': release['id'], 'release_url': release['html_url'], 'asset_id': final['id'],
              'download_url': final['browser_download_url'], 'windows_verify': verify, 'windows_doctor': doctor,
              'verification': {'mode': 'archive-only' if args.skip_functional_tests else 'full',
                               'functional_tests_run': not args.skip_functional_tests, 'zip_crc_ok': True,
                               'uploaded_digest_verified': True},
              'real_dsh_agent_sandbox_verified': False}
    args.result.mkdir(parents=True, exist_ok=True)
    (args.result / 'offline-package.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'release': result['release_url'], 'bytes': size, 'sha256': digest}))


if __name__ == '__main__':
    main()
