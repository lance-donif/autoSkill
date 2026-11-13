"""Observe the recorder's existing CDP connection without issuing application requests."""
from __future__ import annotations

import argparse
import base64
import contextlib
import copy
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .. import data_refs, paths, runtime_identity
from . import agent, traffic

_children: dict[int, tuple[subprocess.Popen, str]] = {}


def _write(file: Path, value: dict) -> None:
    temporary = file.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    temporary.replace(file)


def start(session: str, folder: Path) -> dict:
    """Start a task-owned observer before the recorder navigates away from about:blank."""
    endpoint = (agent.call(session, ['get', 'cdp-url']) or {}).get('cdpUrl')
    if not endpoint:
        raise RuntimeError('浏览器未提供 CDP 地址，无法保存真实网络证据')
    folder = folder / ('network-' + uuid.uuid4().hex)
    folder.mkdir(parents=True)
    token = uuid.uuid4().hex
    env = paths.child_env()
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[2]) + os.pathsep + env.get('PYTHONPATH', '')
    with (folder / 'observer.log').open('wb') as log:
        process = subprocess.Popen([sys.executable, '-m', 'autoskill.recorder.capture', '--endpoint', endpoint,
                                    '--folder', str(folder), '--token', token], env=env, stdout=log, stderr=log,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    handle = {'folder': data_refs.store(folder), 'pid': process.pid, 'token': token, 'source': 'cdp_network',
              'owner': runtime_identity.current(process.pid)}
    _children[process.pid] = (process, token)
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        ready = folder / 'ready.json'
        if ready.is_file() and json.loads(ready.read_text()) == {'token': token, 'ready': True}:
            return handle
        if process.poll() is not None:
            _children.pop(process.pid, None)
            raise RuntimeError('网络证据观察器未能启动；诊断保存在 ' + str(folder / 'observer.log'))
        time.sleep(0.05)
    _write(folder / 'control.json', {'token': token, 'stop': True})
    stop(handle)
    raise RuntimeError('网络证据观察器启动超时；未开始操作目标页面')


def stop(handle: dict | None) -> dict:
    """Wait for the observer to flush its trace. Missing evidence stays explicit."""
    if not handle:
        return {'source': 'cdp_network', 'entries': [], 'available': False}
    folder = data_refs.resolve(handle['folder'])
    active = runtime_identity.owns(handle['pid'], handle.get('owner'))
    if active:
        _write(folder / 'control.json', {'token': handle['token'], 'stop': True})
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        file = folder / 'capture.json'
        if file.is_file():
            result = json.loads(file.read_text(encoding='utf-8'))
            if result.pop('token', None) != handle['token']:
                raise RuntimeError('网络证据不属于当前录制会话')
            child = _children.get(handle['pid'])
            if child and child[1] == handle['token'] and runtime_identity.is_local(handle.get('owner')):
                _children.pop(handle['pid'])
                child[0].wait(timeout=2)
            if not active:
                result['runtime_state'] = 'detached'
            return result
        if not active:
            raise RuntimeError('原网络观察器不属于当前运行位置或已经退出；未控制任何进程，原始证据保留在 ' + str(folder))
        time.sleep(0.05)
    raise RuntimeError('网络证据尚未写完，原始事件已保留在 ' + str(folder / 'events.jsonl'))


def discard(handle: dict | None, owner: Path) -> None:
    """Remove only this recording's finished temporary trace, after archival or explicit cancellation."""
    if not handle:
        return
    if not runtime_identity.is_local(handle.get('owner')):
        return  # Preserve detached recording evidence until an explicit recovery resolves it.
    folder = data_refs.resolve(handle['folder']).resolve()
    if folder.parent != owner.resolve() or not folder.name.startswith('network-'):
        raise RuntimeError('网络证据目录不属于当前录制')
    file = folder / 'capture.json'
    if file.is_file() and json.loads(file.read_text(encoding='utf-8')).get('token') == handle['token']:
        shutil.rmtree(folder)


def _headers(values: dict) -> list[dict]:
    return [{'name': name, 'value': str(value)} for name, value in values.items()]


def _header(headers: list[dict], name: str) -> str:
    return next((item['value'] for item in headers if item['name'].lower() == name), '')


class Observer:
    """One browser-level connection covers existing tabs, popups, frames and worker targets."""

    def __init__(self, endpoint: str, folder: Path, token: str):
        import websocket

        self.ws = websocket.create_connection(endpoint, timeout=0.1, suppress_origin=True)
        self.folder, self.token = folder, token
        self.sequence = 0
        self.pending, self.targets, self.requests = {}, {}, {}
        self.extras, self.response_extras = {}, {}
        self.entries, self.bodies = [], {}
        self.journal = (folder / 'events.jsonl').open('a', encoding='utf-8')
        self.ready = False
        self._auto_attach()

    def send(self, method: str, params: dict, session: str | None = None, entry: dict | None = None) -> None:
        self.sequence += 1
        message = {'id': self.sequence, 'method': method, 'params': params}
        if session:
            message['sessionId'] = session
        self.pending[self.sequence] = (method, session, entry)
        if entry is not None:
            entry.setdefault('_capture_reply_ids', []).append(self.sequence)
        self.ws.send(json.dumps(message))

    def log(self, message: dict) -> None:
        self.journal.write(json.dumps(message, ensure_ascii=False) + '\n')
        self.journal.flush()

    def _auto_attach(self, session: str | None = None) -> None:
        # Keep CDP's default browser/tab-container exclusions when adding browser-UI filtering.
        self.send('Target.setAutoAttach', {'autoAttach': True, 'waitForDebuggerOnStart': True, 'flatten': True,
                                          'filter': [{'type': kind, 'exclude': True} for kind in ('browser', 'tab', 'browser_ui')] + [{}]}, session)

    def _attached(self, params: dict) -> None:
        session, target = params['sessionId'], params['targetInfo']
        self.targets[session] = target
        self.send('Network.enable', {'maxTotalBufferSize': 128 * 1024 * 1024, 'maxResourceBufferSize': 64 * 1024 * 1024}, session)
        self._auto_attach(session)
        # A paused noopener popup can defer Network.enable's reply until initialization resumes.
        # Queue enabling first, but do not wait for its reply before releasing the new target.
        self.send('Runtime.runIfWaitingForDebugger', {}, session)

    def _request(self, session: str, params: dict) -> None:
        key = (session, params['requestId'])
        hops = self.requests.setdefault(key, [])
        if hops and params.get('redirectResponse'):
            self._set_response(hops[-1], params['redirectResponse'])
            hops[-1]['_loading_state'] = 'redirected'
        request = params['request']
        headers = _headers(request.get('headers', {}))
        entry = {'_capture_source': 'cdp_network', '_resourceType': params.get('type', '').lower(),
                 '_initiator': copy.deepcopy(params.get('initiator', {})),
                 '_cdp': {'session_id': session, 'request_id': params['requestId'], 'hop': len(hops),
                          'target_id': self.targets.get(session, {}).get('targetId')},
                 'startedDateTime': datetime.fromtimestamp(params.get('wallTime', time.time()), UTC).isoformat(),
                 'request': {'method': request['method'], 'url': request['url'], 'headers': headers},
                 'response': {}, '_loading_state': 'pending', '_body_complete': False}
        if 'postData' in request:
            entry['request']['postData'] = {'mimeType': _header(headers, 'content-type'), 'text': request['postData']}
        if 'postDataEntries' in request:
            entry['request']['postDataEntries'] = copy.deepcopy(request['postDataEntries'])
        if request.get('hasPostData'):
            entry['_has_post_data'] = True
            entry['_request_body_complete'] = None
        hops.append(entry)
        self.entries.append(entry)
        self._assign_extras(key)
        if request.get('hasPostData'):
            self.send('Network.getRequestPostData', {'requestId': params['requestId']}, session, entry)

    def _assign_extras(self, key: tuple) -> None:
        for entry, extra in zip(self.requests.get(key, []), self.extras.get(key, [])):
            entry['request']['headers'] = _headers(extra.get('headers', {}))
            entry['request']['cookies'] = [item['cookie'] for item in extra.get('associatedCookies', []) if not item.get('blockedReasons')]
            entry['_request_headers_source'] = 'requestWillBeSentExtraInfo'
        for entry, extra in zip(self.requests.get(key, []), self.response_extras.get(key, [])):
            entry['response'].update(headers=_headers(extra.get('headers', {})))

    def _set_response(self, entry: dict, response: dict) -> None:
        entry['response'].update(status=response.get('status'), statusText=response.get('statusText', ''),
                                 content={'mimeType': response.get('mimeType', '')})
        entry['response'].setdefault('headers', _headers(response.get('headers', {})))

    def _response(self, session: str, params: dict) -> None:
        key = (session, params['requestId'])
        if not self.requests.get(key):
            return
        entry = self.requests[key][-1]
        self._set_response(entry, params['response'])
        if 'text/event-stream' in params['response'].get('mimeType', ''):
            self.send('Network.streamResourceContent', {'requestId': params['requestId']}, session, entry)

    def _data(self, entry: dict, encoded: str, *, buffered: bool = False) -> None:
        if encoded and entry.get('_body_source') != 'getResponseBody':
            key = id(entry)
            decoded = base64.b64decode(encoded)
            if buffered:
                self.bodies[key] = bytearray(decoded) + self.bodies.get(key, bytearray())
            else:
                self.bodies.setdefault(key, bytearray()).extend(decoded)
            entry['_body_source'] = 'streamResourceContent'

    def _finished(self, session: str, params: dict, failed: bool) -> None:
        hops = self.requests.get((session, params['requestId']))
        if not hops:
            return
        entry = hops[-1]
        entry['_loading_state'] = 'failed' if failed else 'finished'
        if failed:
            entry['_loading_error'] = params.get('errorText', 'loadingFailed')
        if traffic.category(entry) in ('business', 'authentication') and entry['_resourceType'] != 'websocket':
            self.send('Network.getResponseBody', {'requestId': params['requestId']}, session, entry)

    def _websocket(self, session: str, method: str, params: dict) -> None:
        key = (session, params['requestId'])
        hops = self.requests.setdefault(key, [])
        if not hops:
            entry = {'_capture_source': 'cdp_network', '_resourceType': 'websocket',
                     '_cdp': {'session_id': session, 'request_id': params['requestId'], 'hop': 0,
                              'target_id': self.targets.get(session, {}).get('targetId')},
                     'startedDateTime': datetime.now(UTC).isoformat(),
                     'request': {'method': 'GET', 'url': params.get('url', ''), 'headers': []},
                     'response': {}, '_loading_state': 'pending', '_body_complete': False,
                     '_websocket': {'frames': []}}
            hops.append(entry)
            self.entries.append(entry)
        entry = hops[-1]
        details = entry.setdefault('_websocket', {'frames': []})
        if method == 'Network.webSocketCreated':
            entry['request']['url'] = params['url']
            entry['_initiator'] = copy.deepcopy(params.get('initiator', {}))
        elif method == 'Network.webSocketWillSendHandshakeRequest':
            entry['request']['headers'] = _headers(params['request'].get('headers', {}))
            details['handshake_request'] = copy.deepcopy(params)
            if params.get('wallTime'):
                entry['startedDateTime'] = datetime.fromtimestamp(params['wallTime'], UTC).isoformat()
        elif method == 'Network.webSocketHandshakeResponseReceived':
            self._set_response(entry, params['response'])
            details['handshake_response'] = copy.deepcopy(params)
            entry['_loading_state'] = 'open'
        elif method in ('Network.webSocketFrameSent', 'Network.webSocketFrameReceived'):
            details['frames'].append({'direction': 'sent' if method.endswith('Sent') else 'received', **copy.deepcopy(params)})
        elif method == 'Network.webSocketClosed':
            details['closed'] = copy.deepcopy(params)
            entry['_loading_state'] = 'closed'
            entry['_body_complete'] = True
        elif method == 'Network.webSocketFrameError':
            details.setdefault('errors', []).append(copy.deepcopy(params))

    def _reply(self, message: dict) -> None:
        pending = self.pending.pop(message['id'], None)
        if not pending:
            return
        method, session, entry = pending
        result = message.get('result', {})
        if method == 'Network.enable' and 'error' not in message and self.targets.get(session, {}).get('type') == 'page' and not self.ready:
            self.ready = True
            _write(self.folder / 'ready.json', {'token': self.token, 'ready': True})
        if entry is None:
            return
        if 'error' in message:
            entry.setdefault('_capture_errors', []).append({'method': method, 'error': message['error'].get('message', '')})
        elif method == 'Network.getRequestPostData':
            observed = result.get('postData', '')
            existing = entry['request'].get('postData', {}).get('text', '')
            entry['request']['postData'] = {'mimeType': _header(entry['request']['headers'], 'content-type'),
                                          'text': observed if len(observed) >= len(existing) else existing}
            multipart = 'multipart/form-data' in _header(entry['request']['headers'], 'content-type').lower()
            entry['_request_body_complete'] = None if multipart or len(existing) > len(observed) else True
            if multipart:
                entry['_request_body_note'] = 'CDP 请求正文可能省略文件字节；保留实际可用内容，不推断为完整上传文件'
        elif method == 'Network.streamResourceContent':
            self._data(entry, result.get('bufferedData', ''), buffered=True)
        elif method == 'Network.getResponseBody':
            body = base64.b64decode(result['body']) if result.get('base64Encoded') else result.get('body', '').encode('utf-8')
            self.bodies[id(entry)] = bytearray(body)
            entry['_body_source'] = 'getResponseBody'

    def receive(self, message: dict) -> None:
        self.log(message)
        if 'id' in message:
            self._reply(message)
            return
        method, params, session = message.get('method'), message.get('params', {}), message.get('sessionId', '')
        key = (session, params.get('requestId'))
        if method == 'Target.attachedToTarget':
            self._attached(params)
        elif method == 'Network.requestWillBeSent':
            self._request(session, params)
        elif method and method.startswith('Network.webSocket') and params.get('requestId'):
            self._websocket(session, method, params)
        elif method in ('Network.requestWillBeSentExtraInfo', 'Network.responseReceivedExtraInfo'):
            table = self.extras if method == 'Network.requestWillBeSentExtraInfo' else self.response_extras
            table.setdefault(key, []).append(params)
            self._assign_extras(key)
        elif method == 'Network.responseReceived':
            self._response(session, params)
        elif method == 'Network.dataReceived' and self.requests.get(key):
            self._data(self.requests[key][-1], params.get('data', ''))
        elif method in ('Network.loadingFinished', 'Network.loadingFailed'):
            self._finished(session, params, method == 'Network.loadingFailed')

    def finish(self, error: str = '') -> dict:
        for entry in self.entries:
            body = self.bodies.get(id(entry))
            if body is not None:
                content = entry['response'].setdefault('content', {})
                mime = content.get('mimeType', '').lower()
                textual = not mime or mime.startswith('text/') or any(part in mime for part in ('json', 'xml', 'javascript'))
                try:
                    if not textual:
                        raise UnicodeError('binary content')
                    content['text'] = body.decode('utf-8')
                except UnicodeError:
                    content.update(text=base64.b64encode(body).decode('ascii'), encoding='base64')
                content['size'] = len(body)
                entry['_body_complete'] = entry['_loading_state'] == 'finished'
        selected, scope = traffic.selected(self.entries)
        events = self.folder / 'events.jsonl'
        if events.is_file():
            keys, replies = traffic.event_keys(selected)
            temporary = events.with_suffix('.filtered.tmp')
            with events.open(encoding='utf-8') as source, temporary.open('w', encoding='utf-8') as target:
                for line in source:
                    if traffic.selected_event(json.loads(line), keys, replies):
                        target.write(line)
            temporary.replace(events)
        return {'token': self.token, 'source': 'cdp_network', 'available': self.ready,
                'entries': selected, 'capture_scope': scope, 'error': error, 'events_path': str(events)}

    def run(self) -> None:
        import websocket

        stop_at, error = None, ''
        try:
            while True:
                control = self.folder / 'control.json'
                if stop_at is None and control.is_file() and json.loads(control.read_text()).get('token') == self.token:
                    stop_at = time.monotonic() + 1
                if stop_at is not None and time.monotonic() >= stop_at:
                    break
                try:
                    value = self.ws.recv()
                    if not value:
                        break
                    self.receive(json.loads(value))
                except websocket.WebSocketTimeoutException:
                    continue
        except Exception as problem:  # noqa: BLE001  # Preserve partial evidence if the observed browser exits unexpectedly.
            error = type(problem).__name__
        finally:
            with contextlib.suppress(Exception):
                self.send('Target.setAutoAttach', {'autoAttach': False, 'waitForDebuggerOnStart': False, 'flatten': True})
            self.ws.close()
            self.journal.close()
            _write(self.folder / 'capture.json', self.finish(error))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoint', required=True)
    parser.add_argument('--folder', type=Path, required=True)
    parser.add_argument('--token', required=True)
    args = parser.parse_args()
    Observer(args.endpoint, args.folder, args.token).run()


if __name__ == '__main__':
    main()
