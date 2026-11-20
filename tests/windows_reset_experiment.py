"""Compare two cookie-reset commands in a privately extracted public Windows package."""
from __future__ import annotations

import argparse
import contextlib
import faulthandler
import hashlib
import json
import os
import runpy
import sqlite3
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# The Windows embeddable runtime ignores PYTHONPATH. Only add this directory for the test helpers.
sys.path.append(str(Path(__file__).resolve().parent))
import published_release_smoke as smoke
from windows_doctor_diagnostic import run_process

ORIGINAL = 'a740c6c695dcbc23c5f9ae2ddc2dcb4ffd1ef46c1a9f91d09093a51ada602a2e'


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def isolate(out, label):
    from autoskill.replay.selenium_driver import SeleniumDriver

    class Page(BaseHTTPRequestHandler):
        def do_GET(self):
            page = b'<!doctype html><title>Local session</title>'
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(('127.0.0.1', 0), Page) as server, (out / (label + '.trace')).open('x', encoding='utf-8', buffering=1) as trace:
        origins = [f'http://{host}:{server.server_port}' for host in ('127.0.0.1','localhost')]
        threading.Thread(target=server.serve_forever,daemon=True).start()
        faulthandler.dump_traceback_later(25, repeat=True, file=trace)
        def mark(phase):
            print(f'{time.monotonic():.3f} {phase}', file=trace, flush=True)
            print(phase, flush=True)

        mark('create-driver')
        driver = SeleniumDriver({'base_url':origins[0], 'allowed_origins':origins, 'timeout_seconds':15, 'page_load_seconds':20}, headless=True)
        try:
            mark('fresh reset')
            driver.reset()
            for i,origin in enumerate(origins):
                driver.goto(origin + '/')
                driver.wd.add_cookie({'name':'stale','value':str(i),'path':'/','httpOnly':True})
                driver.wd.execute_script("localStorage.setItem('owner',arguments[0])", 'stale-' + str(i))
                assert driver.wd.get_cookie('stale')['value']==str(i)
            for active,host in enumerate(('127.0.0.1','localhost')):
                owner = 'account-' + str(active)
                saved = {'cookies':[{'name':'AS_AUTH','value':owner,'domain':host,'path':'/','httpOnly':True}],
                         'origins':[{'origin':origins[active],'localStorage':[{'name':'owner','value':owner}]}]}
                mark('reset to ' + owner)
                driver.reset(saved)
                for i,origin in enumerate(origins):
                    driver.goto(origin + '/')
                    values = {c['name']:c['value'] for c in driver.wd.get_cookies()}
                    assert 'stale' not in values, values
                    expected = owner if i == active else None
                    assert values.get('AS_AUTH')==expected, (i,values,expected)
                    observed = driver.wd.execute_script("return localStorage.getItem('owner')")
                    assert observed==expected, (i,observed,expected)
                mark('verified both origins for ' + owner)
        finally:
            mark('close-driver')
            driver.close()
            server.shutdown()
            faulthandler.cancel_dump_traceback_later()
        mark('session isolation passed')


def business(install, folder, out, label):
    skill, data = install / 'autoskill', folder / (label + '-business-data')
    seq = 0
    def cli(skill, data, *args, expected=0):
        nonlocal seq
        seq += 1
        entry = f'{label}-command-{seq:02d}-{args[0]}'
        env = {**smoke.workspace_env(data),'AS_SMOKE_CMD':str(skill/'autoskill.cmd'),'AS_SMOKE_ARGS':json.dumps(['--session','release-smoke',*args])}
        invoke = ('[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); $OutputEncoding = [Console]::OutputEncoding; '
                  '$argvList = @(ConvertFrom-Json $env:AS_SMOKE_ARGS); & $env:AS_SMOKE_CMD @argvList; exit $LASTEXITCODE')
        rc = run_process(['powershell.exe','-NoProfile','-NonInteractive','-Command',invoke], env, folder, out, entry, 85)
        stdout = (out/(entry+'.stdout')).read_text(encoding='utf-8-sig',errors='replace')
        stderr = (out/(entry+'.stderr')).read_text(encoding='utf-8-sig',errors='replace')
        if rc != expected: raise RuntimeError(f'{entry} returned {rc}: {(stderr or stdout)[-450:]}')
        return json.loads(stdout)

    smoke.cli = cli
    fixture = runpy.run_path(str(skill/'examples/ticket-desk/server.py'))
    result = {}
    with fixture['make_server'](port=0) as server:
        calls = smoke.observe_api(server)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            verify = cli(skill,data,'verify')
            assert verify['ok'] and not verify['missing'] and not verify['changed']
            result['runtime_files_verified'] = verify['files']
            assert cli(skill,data,'demo','--url',f'http://127.0.0.1:{server.server_port}')['installed']['id']=='ticket-desk'
            cases = [('api','TC-API-003',['--api-only']), ('playwright','TC-UI-002',['--engine','playwright']), ('selenium','TC-UI-003',['--engine','selenium'])]
            for key, case, flags in cases:
                try:
                    run, receipt = smoke.completed(skill,data,case,*flags)
                    with contextlib.closing(sqlite3.connect(data/'.local/autoskill.db')) as db:
                        detail = smoke.execution_detail(db,run,case)
                        if key=='api':
                            response=detail['response'];body=response['body']
                            body=json.loads(body) if isinstance(body,str) else body
                            assert response['status']==201 and body['priority']=='high' and body['title']=='接口回放工单'
                            before=len(calls)
                            retry=smoke.verify_recovery(skill,data,db,receipt)
                            assert len(calls)==before
                            result['retry']={'report_id':retry['report_id'],'missing_status':retry['missing_status'],'restored_status':retry['restored_status'],
                                             'runs_and_results':retry['runs_and_results'],'requests_before_after':[before,len(calls)],'snapshot_sha256':retry['snapshot_sha256']}
                            result[key]={'run':run,'status':201,'title':body['title'],'priority':body['priority']}
                        else:
                            actual='高' if key=='playwright' else '请填写标题'
                            assert any(a.get('passed') and a.get('actual')==actual for a in detail['assertions'])
                            result[key]={'run':run,'assertion':actual,'report_id':receipt['report_id']}
                except Exception as exc:
                    result[key]={'error':str(exc)}
                write_json(out/(label+'-business.json'), result)
                print('Business receipt',key,json.dumps(result.get(key),ensure_ascii=False),flush=True)
        finally:
            server.shutdown()
    print('Independent business results',json.dumps(result,ensure_ascii=False),flush=True)
    return all(k in result and 'error' not in result[k] for k in ('api','retry','playwright','selenium'))


def controller():
    root = Path(os.environ['RUNNER_TEMP']) / 'reset-experiment'
    root.mkdir()
    out = Path(os.environ['GITHUB_WORKSPACE']) / 'reset-evidence'
    out.mkdir()
    install,asset=smoke.download(root,'windows-x64','v2.5.3')
    print('Public attachment',json.dumps(asset),flush=True)
    py=install/'autoskill/vendor/windows-x64/python/python.exe'
    source=install/'autoskill/app/autoskill/replay/selenium_driver.py'
    before=source.read_bytes()
    assert hashlib.sha256(before).hexdigest()==ORIGINAL
    old=b'cdp("Network.clearBrowserCookies", {})'; new=b'cdp("Storage.clearCookies", {})'
    assert before.count(old)==1
    versions={'original':before,'candidate':before.replace(old,new)}
    script=str(Path(__file__).resolve())
    stats={key:[] for key in versions};results={'attachment':asset,'isolation':stats,'business':{}}
    def observe(mode, phase, label, timeout):
        source.write_bytes(versions[mode]);assert source.read_bytes()==versions[mode]
        env={**smoke.workspace_env(root/(label+'-workspace')),'PYTHONDONTWRITEBYTECODE':'1'}
        rc=run_process([str(py),script,'--phase',phase,'--install',str(install),'--folder',str(root),'--out',str(out),'--label',label],env,root,out,label,timeout)
        stdout=(out/(label+'.stdout')).read_text(encoding='utf-8-sig',errors='replace')
        stderr=(out/(label+'.stderr')).read_text(encoding='utf-8-sig',errors='replace')
        print(label,'receipt',rc,stdout[-3200:],stderr[-1500:],flush=True)
        if rc!=0 and (out/(label+'.trace')).is_file():
            trace=(out/(label+'.trace')).read_text(encoding='utf-8',errors='replace')
            frames=[line.strip() for line in trace.splitlines() if 'selenium_driver.py' in line or 'Timeout (' in line]
            print('Trace excerpt',label,frames[-6:],flush=True)
        return rc
    try:
        results['business']['original']=observe('original','business','original-business',340)
        for i,mode in enumerate(('original','candidate','candidate','original','original','candidate'),1):
            label=f'{i}-{mode}'
            stats[mode].append(observe(mode,'isolation',label,65))
            write_json(out/'experiment.json',results)
        results['business']['candidate']=observe('candidate','business','candidate-business',340)
    finally:
        source.write_bytes(before)
        assert hashlib.sha256(source.read_bytes()).hexdigest()==ORIGINAL
        write_json(out/'experiment.json',results)
        print('Bounded comparison',json.dumps(results,ensure_ascii=False),flush=True)
    return 0 if stats['candidate']==[0,0,0] and results['business']['candidate']==0 else 1


if __name__=='__main__':
    args=argparse.ArgumentParser();args.add_argument('--phase',choices=['business','isolation']);args.add_argument('--out',type=Path);args.add_argument('--folder',type=Path);args.add_argument('--install',type=Path);args.add_argument('--label');opts=args.parse_args()
    if opts.phase=='isolation': isolate(opts.out,opts.label)
    elif opts.phase=='business': raise SystemExit(0 if business(opts.install,opts.folder,opts.out,opts.label) else 1)
    else: raise SystemExit(controller())
