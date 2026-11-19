"""Dedicated Locust process. Its cooperative I/O must never patch the dashboard or queue owner."""
from __future__ import annotations

import copy
import json
import os
import sys
import time
import uuid
from functools import partial

import locust
from gevent import get_hub, sleep
from gevent.event import Event
from locust import User, constant, task
from locust.env import Environment
from locust.exception import StopUser

from . import db, execution_inputs, load_report, projects, runner, runtime_identity, secrets, task_state
from .replay import api


class DispatchClosedError(Exception):
    """The user prepared a request after an admission limit was reached."""


class Campaign:
    """Locust owns user scheduling. The campaign gates measured requests and records stage boundaries."""

    def __init__(self, run, spec, owner):
        self.run = run
        self.spec = spec
        self.owner = owner
        self.level = spec.get("concurrency", spec.get("initial_concurrency"))
        if "target_requests" in spec:
            self.level = min(self.level, spec["target_requests"])
        if "iterations" in spec:
            self.level = min(self.level, spec["iterations"])
        self.maximum = spec.get("max_concurrency", self.level)
        self.environment = Environment(user_classes=[FrozenAPIUser])
        self.environment.campaign = self
        self.local = self.environment.create_local_runner()
        self.release = Event()
        self.ready = set()
        self.startup_misses = set()
        self.misses = {}
        self.initialized = 0
        self.pending_level = None
        self.registered = self.live = self.retired = self.iterations = self.samples = 0
        self.counts = {"done": 0, "passed": 0, "failed": 0, "blocked": 0}
        self.closed = False
        self.owner_lost = False
        self.next_health = 0
        self.deadline = None
        self.stages = []
        self.threads = []
        self.timeline_write = None
        self.errors = []

    def user_count(self, delta):
        self.live += delta
        self.threads.append({"time_ms": time.time_ns() // 1_000_000, "count": self.live})
        if delta > 0:
            self.registered += 1
        else:
            self.retired += 1
        return self.registered

    def initialized_user(self):
        self.initialized += 1
        if self.pending_level and self.initialized >= self.pending_level:
            self.pending_level = None
            self.deadline = time.monotonic() + self.spec["step_duration_seconds"]

    def stage(self):
        now = time.time_ns() // 1_000_000
        if self.stages:
            self.stages[-1]["end_ms"] = now
        self.stages.append({"index": len(self.stages) + 1, "concurrency": self.level, "start_ms": now, "end_ms": None})
        seconds = self.spec.get("duration_seconds", self.spec.get("step_duration_seconds"))
        self.deadline = time.monotonic() + seconds if seconds and not self.pending_level else None

    def publish(self):
        # Keep stage snapshots available without charging the evidence sync to a dispatch deadline.
        if self.timeline_write is not None:
            if not self.timeline_write.ready():
                return
            self.timeline_write.get()
        self.timeline_write = get_hub().threadpool.spawn(
            load_report.write_timeline, self.run["id"], threads=self.threads.copy(),
            stages=[stage.copy() for stage in self.stages])

    def open_when_ready(self):
        count = 1 if "iterations" in self.spec else self.level
        if self.closed or self.release.is_set() or len(self.ready | self.startup_misses) + self.retired < count:
            return
        if self.ready:
            self.stage()
            self.release.set()
            self.publish()
        elif "iterations" not in self.spec:
            self.no_targets()

    def no_targets(self):
        self.errors.append("整组虚拟用户未能发出目标请求，请检查前置用例和输入参数")
        self.stop()

    def no_progress(self, user):
        if self.closed or "iterations" in self.spec:
            return
        if not self.release.is_set():
            self.startup_misses.add(user)
            self.open_when_ready()
            self.release.wait()
            return
        if self.spec.get("duration_seconds") or self.spec["mode"] == "stress":
            sleep(.02)
            return
        self.misses[user] = self.misses.get(user, 0) + 1
        while self.misses.get(user, 0) >= 2 and self.check():
            if len(self.misses) >= self.live and min(self.misses.values()) >= 2:
                self.no_targets()
                return
            sleep(.02)

    def stop(self):
        if not self.closed:
            self.closed = True
            if self.stages:
                self.stages[-1]["end_ms"] = time.time_ns() // 1_000_000
            self.release.set()

    def cancelled(self):
        if os.getppid() != self.owner or not runtime_identity.owns(self.owner, self.run["owner_identity"]):
            self.owner_lost = True
            return True
        row = db.one("SELECT cancel,status,pid,owner_identity FROM runs WHERE id=?", (self.run["id"],))
        if not row or row["pid"] != self.owner or row["owner_identity"] != self.run["owner_identity"] or row["status"] != "Running":
            self.owner_lost = True
            return True
        return bool(row["cancel"])

    def check(self):
        if time.monotonic() >= self.next_health:
            self.next_health = time.monotonic() + .02
            if self.cancelled():
                self.stop()
        if not self.closed and self.deadline is not None and time.monotonic() >= self.deadline:
            if self.level == self.maximum:
                self.stop()
            else:
                self.level = min(self.maximum, self.level + self.spec["step_concurrency"])
                self.pending_level = self.level
                self.stage()
                self.local.start(self.level, spawn_rate=self.level)
                self.publish()
        return not self.closed

    def admit(self, user):
        self.ready.add(user)
        self.open_when_ready()
        self.release.wait()
        if not self.check():
            raise DispatchClosedError
        self.samples += 1
        self.misses.clear()
        if self.samples == self.spec.get("target_requests"):
            self.stop()
        return self.stages[-1]["index"], self.level

    def record(self, case, outcome, prerequisite, started):
        if self.owner_lost:
            return
        runner.record(self.run, case, outcome, prerequisite, started)
        if prerequisite:
            return
        sample = next((ref["load_sample"] for ref in outcome.get("attempts", []) if "load_sample" in ref), None)
        verdict = task_state.case_outcome(outcome, outcome, evidence_valid=True)
        if sample:
            self.environment.events.request.fire(
                request_type=case["method"], name=case["id"], start_time=sample["start_ms"] / 1000,
                response_time=sample["elapsed_ms"], response_length=(outcome.get("response") or {}).get("bytes", 0),
                exception=None if verdict == "pass" else RuntimeError(outcome.get("category") or "检查未通过"),
                context={"stage": sample["stage"]})
        self.counts["done"] += 1
        self.counts[{"pass": "passed", "fail": "failed"}.get(verdict, "blocked")] += 1
        total = self.run["total"] if "iterations" in self.spec else self.counts["done"]
        db.update("runs", self.run["id"], {**self.counts, "total": total, "current": case["title"]})

    def execute(self):
        ramp = self.spec.get("ramp_up_seconds")
        rate = self.level / ramp if ramp else self.level
        self.local.start(self.level, spawn_rate=rate)
        try:
            while True:
                self.check()
                self.open_when_ready()
                if self.owner_lost or (not self.live and (self.closed or self.registered >= self.level)):
                    break
                sleep(.02)
        finally:
            self.stop()
            self.local.quit()
            if self.timeline_write is not None:
                self.timeline_write.get()
            if not self.owner_lost:
                load_report.write_timeline(self.run["id"], threads=self.threads, stages=self.stages)
        if not self.cancelled() and not self.samples and not self.errors:
            self.errors.append("没有目标接口发出请求，请检查用例前置条件、凭据及变量")
        return "；".join(dict.fromkeys(self.errors))


class MeasuredSession(runner.Session):
    """Keep each Locust user's connections and isolate cookies and extracts for each iteration."""

    def __init__(self, campaign, user):
        super().__init__(campaign.run, force_login=True)
        self.campaign = campaign
        self.user = user
        self.independent = api.new_client(self.config)
        self.sent = 0
        self.completing_request = False

    def cancel_requested(self):
        return False if self.completing_request else super().cancel_requested()

    def reset_iteration(self):
        self.variables = {**copy.deepcopy(self.manifest["variables"]), "RAND": uuid.uuid4().hex[:12]}
        self.attempts.clear()
        self.client.cookies.clear()
        self.sent = 0

    def close(self):
        self.independent.close()
        self.client.close()

    def request_case(self, case, selected):
        client = self.client
        if not projects.depends_on(case) and case["id"] != self.auth.get("api_case"):
            client = self.independent
            client.cookies.clear()
        timing = {}
        send = partial(load_report.timed_send, client, timing) if selected else client.send
        denied = False
        stage = None

        def invoke(_identity):
            nonlocal denied, stage
            journal = execution_inputs.on_target_start.get()

            def admit():
                nonlocal denied, stage
                try:
                    if self.cancel_requested():
                        raise DispatchClosedError
                    if selected:
                        stage = self.campaign.admit(self.user)
                    elif not self.campaign.check():
                        raise DispatchClosedError
                except DispatchClosedError:
                    denied = True
                    raise
                journal()
                self.completing_request = True

            token = execution_inputs.on_target_start.set(admit)
            try:
                outcome = api.run_case(client, case, self.config, self.variables, send_prepared=send)
            except DispatchClosedError:
                return runner.blocked("已停止派发新请求", "load_limit")
            finally:
                execution_inputs.on_target_start.reset(token)
            if timing:
                outcome["load_sample"] = {**timing, "stage": stage[0], "concurrency": stage[1]}
            return outcome

        try:
            outcome = self.attempt(case, "case", invoke)
        finally:
            self.completing_request = False
        if denied or (outcome["status"] == "Cancelled" and not outcome.get("target_started")):
            raise DispatchClosedError
        if client is self.independent:
            for cookie in client.cookies.jar:
                self.client.cookies.jar.set_cookie(cookie)
        if sample := outcome.get("load_sample"):
            self.sent += 1
            self.attempts[case["id"]][-1]["ref"]["load_sample"] = sample
        return outcome


class FrozenAPIUser(User):
    wait_time = constant(0)

    def on_start(self):
        self.campaign = self.environment.campaign
        self.number = self.campaign.user_count(1)
        self.session = None
        try:
            self.session = MeasuredSession(self.campaign, self.number)
            self.plan = runner.build_plan(self.session.manifest["selected"], self.session.cases)
            self.campaign.initialized_user()
        except Exception as error:  # A failed user startup must release the initial rendezvous.
            self.campaign.errors.append("虚拟用户初始化失败：" + type(error).__name__)
            self.campaign.stop()
            raise StopUser from error

    def on_stop(self):
        try:
            if self.session:
                self.session.close()
        finally:
            if self.session:
                self.campaign.initialized -= 1
            self.campaign.user_count(-1)
            self.campaign.open_when_ready()

    @task
    def iteration(self):
        campaign = self.campaign
        if not campaign.check() or campaign.iterations >= campaign.spec.get("iterations", float("inf")):
            raise StopUser
        campaign.iterations += 1
        iteration = campaign.iterations
        session = self.session
        session.reset_iteration()
        outcomes = {}
        pending = {}
        try:
            with secrets.use_values(session.manifest["credentials"]):
                for identity, _ in self.plan:
                    if not campaign.check():
                        break
                    selected = identity in session.manifest["selected"]
                    case, started = session.cases[identity], db.now()
                    broken = next((dep for dep in projects.depends_on(case) if outcomes.get(dep) != "Pass"), None)
                    outcome = (runner.blocked(f"前置用例 {broken} 未通过，本用例没有执行", "prerequisite_failed") if broken
                               else session.request_case(case, selected))
                    summary = session.summarize(case, outcome)
                    outcomes[identity] = summary["status"]
                    if case.get("extensions", {}).get("postconditions"):
                        pending[identity] = (summary, started)
                    else:
                        campaign.record(case, {**summary, "load_user": self.number, "load_iteration": iteration}, not selected, started)
                if not session.sent:
                    campaign.no_progress(self.number)
        except DispatchClosedError as stop:
            raise StopUser from stop
        except StopUser:
            raise
        except Exception as error:  # Keep explicit engine errors instead of retrying a broken task forever.
            campaign.errors.append("接口任务执行异常：" + type(error).__name__)
            campaign.stop()
            raise StopUser from error
        finally:
            for identity in reversed(pending):
                source, started = pending[identity]
                case = session.cases[identity]
                rows = [db.one("SELECT * FROM results WHERE run_id=? AND case_id=? AND json_extract(detail,'$.load_iteration')=?",
                               (campaign.run["id"], target, iteration)) for target in case["extensions"]["postconditions"]]
                if all(rows):
                    source = session.summarize(case, session.attempt(case, "postcondition", lambda _attempt, c=case, s=source, r=rows:
                                                                     runner.postcondition_outcome(c, s, r)))
                else:
                    source = {**source, "reason": source.get("reason") or "已停止派发，部分后置验证没有发出"}
                campaign.record(case, {**source, "load_user": self.number, "load_iteration": iteration},
                                identity not in session.manifest["selected"], started)


def main(run_id, owner):
    run = db.one("SELECT * FROM runs WHERE id=?", (run_id,))
    manifest = execution_inputs.load(run, check_engine=True)
    if manifest["load"]["driver_version"] != locust.__version__:
        raise ValueError("Locust 版本与冻结运行不匹配")
    if not manifest.get("api_only") or any(case["kind"] != "api" for case in manifest["cases"].values()):
        raise ValueError("Locust 仅支持 HTTP 接口用例")
    reason = Campaign(run, manifest["load"], owner).execute()
    sys.stdout.write(json.dumps({"reason": reason}))


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]))
