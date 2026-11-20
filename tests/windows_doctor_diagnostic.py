"""Diagnose the public Windows bundle using its packaged Python and a fresh local workspace."""
from __future__ import annotations

import argparse
import contextlib
import faulthandler
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path


def child(trace_file):
    # The embedded Python runs from the downloaded ZIP. Keep its app files untouched.
    from autoskill import cli, platforms, runner

    with trace_file.open('x', encoding='utf-8', buffering=1) as stream:
        def mark(msg):
            print(f'{time.monotonic():.3f} {msg}', file=stream, flush=True)

        def observe(obj, name, label):
            orig = getattr(obj, name)

            def call(*args, **kwargs):
                target = str(args[0]) if name == 'binary_status' and args else ''
                mark(f'begin {label} {name} {target}')
                try:
                    return orig(*args, **kwargs)
                finally:
                    mark(f'end {label} {name}')

            setattr(obj, name, call)

        make_driver = runner.make_driver

        def traced_driver(engine, config):
            mark('begin make_driver ' + engine)
            driver = make_driver(engine, config)
            mark('end make_driver ' + engine)
            for op in ('reset', 'goto', 'title', 'close'):
                observe(driver, op, engine)
            return driver

        runner.make_driver = traced_driver
        for name in ('system_info', 'binary_status', 'browser_pair'):
            observe(platforms, name, 'platform')
        faulthandler.dump_traceback_later(25, repeat=True, file=stream)
        try:
            mark('begin cli doctor')
            return cli.main(['--session', 'release-smoke', 'doctor', '--launch'])
        finally:
            faulthandler.cancel_dump_traceback_later()
            mark('end cli doctor')


def processes(root_pid, directory, label):
    # Only log this test parent's descendants, their executable paths and local port numbers.
    command = r"""
    $all = @(Get-CimInstance Win32_Process)
    $ids = [System.Collections.Generic.HashSet[uint32]]::new()
    $null = $ids.Add([uint32]$env:DIAG_PARENT_PID)
    do {
      $before = $ids.Count
      foreach ($p in $all) { if ($ids.Contains([uint32]$p.ParentProcessId)) { $null=$ids.Add([uint32]$p.ProcessId) } }
    } while ($ids.Count -ne $before)
    $rows = @($all | Where-Object {$ids.Contains([uint32]$_.ProcessId)} | Select-Object ProcessId,ParentProcessId,Name,ExecutablePath,@{n='ChromeDriverPort';e={if ($_.Name -eq 'chromedriver.exe' -and $_.CommandLine -match '--port=(\d+)') {$Matches[1]}}})
    $rows | ConvertTo-Json -Compress
    """
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command], env={**os.environ, 'DIAG_PARENT_PID':str(root_pid)}, capture_output=True, timeout=20)
    (directory / f'{label}-processes.json').write_bytes(result.stdout)


def run_process(cmd, env, cwd, out, label, timeout):
    with (out / f'{label}.stdout').open('wb') as stdout, (out / f'{label}.stderr').open('wb') as stderr:
        proc = subprocess.Popen(cmd, env=env, cwd=cwd, stdout=stdout, stderr=stderr)
        try:
            result = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                processes(proc.pid, out, label)
            subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], check=False, timeout=20)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=10)
            result = 'timeout'
    print(label, result, flush=True)
    return result


def count_plan(skill, folder):
    from hashlib import sha256
    from openpyxl import load_workbook
    from published_release_smoke import cli

    data = folder / 'plan-count-data'
    project = cli(skill, data, 'project', 'create', '--name', '单目标验证', '--url', 'http://127.0.0.1:9')['created']['id']
    definition = folder / 'design.json'
    definition.write_text(json.dumps([{'id':'FLOW','title':'取得目标内容截图','steps':['打开首页','找到目标内容','截取目标内容'],'expected':['1. 首页可见','2. 内容可见','3. 截图完成']}],ensure_ascii=False),encoding='utf-8')
    cmd = ('plan','write',project,'--file',str(definition),'--expected-cases','1')
    saved = cli(skill,data,*cmd)
    book = Path(saved['initial_excel'])
    original = sha256(book.read_bytes()).hexdigest()
    with contextlib.closing(load_workbook(book,read_only=True)) as workbook:
        sheet = workbook['功能测试用例']
        rows = [r for r in sheet.values if r[9]=='未执行']
        assert len(rows)==1 and sheet.max_column==14 and all(len(str(c).splitlines())==3 for c in rows[0][5:7])
    definition.write_text(json.dumps([{'id':k,'title':k} for k in ('OPEN','FIND','CAPTURE')]),encoding='utf-8')
    error = cli(skill,data,*cmd,expected=2)
    assert error['code']=='validation_error' and '期望 1 条' in error['error'] and '实际有 3 条' in error['error']
    assert sha256(book.read_bytes()).hexdigest()==original and cli(skill,data,'plan','show')['task_id']==saved['task_id']
    return {'target':1,'rows':len(rows),'columns':14,'ordered_steps':3,'rejected_split_rows':3,'rejection_exit':2,'initial_sha256':original}


def run(tag):
    from published_release_smoke import cli, download, workspace_env

    folder = Path(os.environ['RUNNER_TEMP']) / ('doctor-probe-' + tag)
    folder.mkdir()
    outputs = Path(os.environ['GITHUB_WORKSPACE']) / ('doctor-proof-' + tag)
    outputs.mkdir()
    install, asset = download(folder, 'windows-x64', tag)
    print(json.dumps(asset), flush=True)
    skill = install / 'autoskill'
    if tag == 'v2.5.3':
        try:
            plan_receipt = count_plan(skill, folder)
        except Exception:
            plan_receipt = {'error': traceback.format_exc()}
        print(json.dumps(plan_receipt), flush=True)
        (outputs / 'plan-receipt.json').write_text(json.dumps(plan_receipt,indent=2)+'\n')
    py = skill / 'vendor/windows-x64/python/python.exe'
    data = folder / '本地数据 (new)'
    inventory = cli(skill, data, 'verify')
    assert inventory['ok'] and not inventory['missing'] and not inventory['changed']
    print('Runtime files verified', inventory['files'], flush=True)
    env = workspace_env(data)
    env.update(AUTOSKILL_HOME=str(skill) + '\\', AUTOSKILL_PY=str(py), PYTHONPATH=str(skill / 'app'), PYTHONDONTWRITEBYTECODE='1')
    invoke = ('[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); $OutputEncoding = [Console]::OutputEncoding; '
              '$argvList = @(ConvertFrom-Json $env:AS_SMOKE_ARGS); & $env:AS_SMOKE_CMD @argvList; exit $LASTEXITCODE')
    arguments = {'AS_SMOKE_CMD':str(skill/'autoskill.cmd'), 'AS_SMOKE_ARGS':json.dumps(['--session','release-smoke','doctor','--launch'])}
    normal = run_process(['powershell.exe','-NoProfile','-NonInteractive','-Command',invoke], {**env,**arguments}, folder, outputs, 'normal', 120)
    try:
        answer = json.loads((outputs / 'normal.stdout').read_text(encoding='utf-8-sig',errors='replace'))
        print('Normal packaged doctor:', json.dumps({key: answer.get(key) for key in ('ok','bundled_runtime','browser_versions','browser_launch','missing')}, ensure_ascii=False), flush=True)
    except ValueError:
        print('Normal packaged command stderr:', (outputs / 'normal.stderr').read_text(encoding='utf-8-sig',errors='replace')[-1000:], flush=True)
    trace = outputs / 'python-stacks.log'
    traced = run_process([str(py), str(Path(__file__).resolve()), '--child', str(trace)], env, folder, outputs, 'traced', 100)
    if trace.exists(): print(trace.read_text(encoding='utf-8',errors='replace')[-13000:])
    for file in sorted(outputs.glob('*-processes.json')):
        print(file.name, file.read_text(errors='replace')[:2800])
    result = {'asset':asset,'normal':normal,'traced':traced,'runtime_files':inventory['files']}
    if normal == 0:
        receipt = folder / 'full-receipt.json'
        result['business'] = run_process([str(py),str(Path(__file__).with_name('published_release_smoke.py')),'--target','windows-x64','--exercise',str(install),'--receipt',str(receipt)], env, folder, outputs,'unmodified-full',300)
        if receipt.is_file():
            print(receipt.read_text(encoding='utf-8'))
            (outputs / 'business-receipt.json').write_bytes(receipt.read_bytes())
    (outputs/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    plan_ok = tag != 'v2.5.3' or 'error' not in plan_receipt
    return 0 if normal == result.get('business') == 0 and plan_ok else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--tag')
    group.add_argument('--child',type=Path)
    args = parser.parse_args()
    raise SystemExit(child(args.child) if args.child else run(args.tag))
