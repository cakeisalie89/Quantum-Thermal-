#!/usr/bin/env python3
"""Gate G: a staged long-horizon campaign through the governed research
loop, with every counter kept apart.

    python tools/research_horizon.py --decisions 1000          # print
    python tools/research_horizon.py --decisions 10000 --write # record

WHAT RUNS. One log, one owner, one scripted agent (and its controller),
objective after objective. Each objective asks one question as data (the
prediction and the documented point). The agent:

- proposes the question's hypothesis and several alternatives about the
  same output;
- asks for one governed run, which is independently checked and decided
  by the reviewer and decides every hypothesis at once;
- concludes on the question's hypothesis.

Some objectives instead abstain, or begin with an answer that is no
proposal. Some crash mid-step at a rotating fault point and resume from
the log in a freshly built controller. At a stage boundary one verified
run is withdrawn and what rested on it is followed.

THE AGENT IS A SCRIPT. Its decisions are SCRIPTED DECISIONS: genuine
steps of the governed loop, each a proposal acted on through the one route
its kind has, and never model decisions. Nothing here says how a model
behaves over a long horizon.

COUNTED APART, at every stage, from the verified log:
- events;
- governed operations (governed tasks created);
- inference calls (results recorded by the gateway);
- scripted decisions (research steps);
- verified records;
- conclusions;
- resets (controllers rebuilt from the log);
- restarts (resumptions after a crash).

Alongside them:
- duplicates: executions, receipts and inference results counted twice;
- recovery failures;
- the cost of replaying the history three ways;
- the memory a fresh load allocates;
- a checkpointed load against a full one;
- what the second reader notes.

Timings are this host's, measured once, and recorded as measurements.

RESUMING AFTER THE PROCESS DIES (AUD-013). The campaign keeps its own
counters and stage records in ``campaign_state.json`` beside the world. The
file is written atomically before each objective, after each stage and after
each withdrawal, and is bound to the log position it describes. ``--resume``
opens the same world without rebuilding it. The log is the authority over
what happened, and the state file is believed only where the log still holds
the position it names:

- a log rolled back or rewritten behind that position is refused;
- an objective the log registered after the last save is taken from the log,
  and finished if it is still open;
- a stage measured but not saved is measured again;
- a withdrawal the dead process began is settled through the governed path.

Counters derived from the log (events, decisions, calls, runs, verified
records, conclusions) cannot be counted twice, because they are counted from
the log. The process counters say what they are. ``restarts`` are
resumptions after a scripted crash, in a process that lived to record them.
``process_restarts`` are resumptions of the campaign itself. One process
holds a campaign at a time: a second is refused while the first lives, and
the lock goes with the process when it dies.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
import tracemalloc
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import adversarial_eval as E  # noqa: E402

from agent_substrate import research as R  # noqa: E402
from agent_substrate import research_loop as L  # noqa: E402
from agent_substrate.checkpoint import CheckpointStore  # noqa: E402
from agent_substrate.events import EventLog  # noqa: E402
from agent_substrate.governed_model import GovernedModelRuns  # noqa: E402
from agent_substrate.reconstruct import reconstruct_subsystems  # noqa: E402
from agent_substrate.store import AuthorityStore  # noqa: E402

SCHEMA = "research-horizon/1"
STATE_SCHEMA = "research-horizon-state/1"
STATE_FILE = "campaign_state.json"
RESULTS = ROOT / "docs" / "assurance" / "research_horizon_results.json"
STAGES = (50, 100, 1000, 5000, 10000)
#: hypotheses per objective besides the question's own
ALTERNATIVES = 7
#: either side of the documented point's mid-plane temperature, so that
#: some hypotheses are supported and some refuted
THRESHOLDS = (400.0, 305.0, 350.0, 310.0, 320.0, 1000.0, 300.0, 500.0)
CRASH_POINTS = ("before_infer", "after_infer", "after_receive", "after_run",
                "after_check", "after_decide", "before_step_record")


def _prediction(threshold: float) -> dict:
    return {"model_id": "thermal.slab_transient", "quantity": "T_at_half",
            "op": "<", "value": threshold, "unit": "K"}


class Agent:
    """One scripted agent across every objective: it reads the objective
    from the prompt's research state and proposes the next step of that
    objective's plan."""

    def __init__(self):
        self.plans: dict = {}

    def plan(self, oid: str, i: int) -> dict:
        main = THRESHOLDS[i % len(THRESHOLDS)]
        alts = [t for t in THRESHOLDS if t != main][:ALTERNATIVES]
        p = {"main": f"{oid}.h0", "hyps": [
            (f"{oid}.h{j}", t) for j, t in enumerate([main] + alts)],
            "abstain": i % 17 == 16, "no_proposal": i % 23 == 22}
        self.plans[oid] = p
        return p

    def __call__(self, req, prompt, n):
        state = next(json.loads(it["text"]) for it in
                     json.loads(prompt)["items"]
                     if it["tier"] == "TASK_STATE")
        oid = state["objective"]["id"]
        p = self.plans[oid]
        mine = {h["id"]: h for h in state["hypotheses"]}
        if p["no_proposal"] and state["steps"] == 0:
            return E._reply("REVISE_POLICY", {"allow": "*"})
        todo = [(h, t) for h, t in p["hyps"] if h not in mine]
        if todo:
            hid, t = todo[0]
            return E._reply("HYPOTHESIS", {
                "hypothesis_id": hid,
                "statement": f"T_at_half stays below {t} K at the point",
                "prediction": _prediction(t), "depends_on": []})
        if p["abstain"]:
            return E._reply("ABSTAIN", {"reason": "scripted abstention"})
        if any(h["status"] == "OPEN" for h in mine.values()):
            return E._reply("MODEL_RUN", E.SLAB_RUN)
        return E._reply("CONCLUDE", {"hypothesis_id": p["main"],
                                     "summary": "the record decided it"})


class CampaignBusy(RuntimeError):
    """Another process holds this campaign."""


class CannotResume(RuntimeError):
    """The state left behind does not describe the log as it stands."""


def _hold(path: Path):
    """An exclusive lock on ``path`` for this process's life. The kernel
    releases it when the process dies, however it dies."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise CampaignBusy(
            f"another process holds the campaign whose lock is {path}; "
            "two processes driving one campaign would each count the "
            "other's work") from None
    return fd


def _die_at(point: str):
    """A step fault that kills the process, rather than the step, at the
    crash point ``point`` (``<crash point>:<objective index>``) names."""
    where = point.split(":")[0]

    def fault(at):
        if at == where:
            _die(point)
    return fault


def _die(point: str) -> None:
    """A test's way to kill this process where it would be killed:
    RESEARCH_HORIZON_DIE_AT names the point. ``os._exit`` runs no handler and
    no ``finally``, as a reclaimed machine does not."""
    if os.environ.get("RESEARCH_HORIZON_DIE_AT") == point:
        os._exit(137)


class Campaign:
    def __init__(self, name: str = "horizon", *, calls: int = 10**6,
                 resume: bool = False):
        self.name = name
        self.calls = calls
        self.agent = Agent()
        # Held before the world is touched: a fresh campaign of the same
        # name would otherwise delete the workspace another process is
        # driving.
        self._lock = _hold(ROOT / E.WS / f"{name}.lock")
        self.w = E.World(name, self.agent, calls=calls, resume=resume)
        self.checkpoints = CheckpointStore(self.w.base / "checkpoints")
        self.resets = self.restarts = self.recovery_failures = 0
        self.process_restarts = 0
        #: objectives a resumed process found the log had registered after
        #: the last save: ``{"objective", "found", "now"}``
        self.resumed: list = []
        self.objectives = 0
        self.invalidations: list = []
        #: the stage targets whose withdrawal is recorded
        self.invalidated: list = []
        self.stages: list = []
        self.started = time.monotonic()
        self._last = (0, self.started)
        self._elapsed_before = 0.0
        if resume:
            self._resume()

    @classmethod
    def resume(cls, name: str = "horizon", *, calls: int = 10**6):
        """The campaign a stopped process left, continued."""
        return cls(name, calls=calls, resume=True)

    # ---- the state beside the world (AUD-013) -----------------------------
    def _save(self, head: tuple) -> None:
        """Write the campaign's own state, bound to the log position
        ``head`` = (seq, hash) it describes. Atomic: a crash leaves the old
        file or the new one, never a torn one."""
        state = {"schema": STATE_SCHEMA, "name": self.name,
                 "calls": self.calls, "objectives": self.objectives,
                 "counters": {"resets": self.resets,
                              "restarts": self.restarts,
                              "process_restarts": self.process_restarts,
                              "recovery_failures": self.recovery_failures},
                 "resumed": self.resumed, "stages": self.stages,
                 "invalidations": self.invalidations,
                 "invalidated": self.invalidated,
                 "head": {"seq": head[0], "hash": head[1]},
                 "elapsed_seconds": self._elapsed()}
        path = self.w.base / STATE_FILE
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(state, indent=1, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        dirfd = os.open(self.w.base, os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)

    def _elapsed(self) -> float:
        return round(self._elapsed_before + time.monotonic() - self.started,
                     1)

    def _resume(self) -> None:
        """Take up where a stopped process left off, believing its state
        only where the log agrees."""
        path = self.w.base / STATE_FILE
        try:
            st = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CannotResume(
                f"no readable campaign state at {path}: "
                f"{type(exc).__name__}") from None
        if st.get("schema") != STATE_SCHEMA or st.get("name") != self.name:
            raise CannotResume(f"{path} is not this campaign's state")
        events = self.w.events()                 # verified, or it raises
        seq, digest = st["head"]["seq"], st["head"]["hash"]
        if seq >= 0 and (seq >= len(events) or events[seq].hash != digest):
            raise CannotResume(
                f"the log no longer holds the position the campaign "
                f"recorded (seq {seq}, hash {str(digest)[:12]}): it was "
                "rolled back or rewritten, and resuming would report work "
                "the history does not show")
        registered = [e.target for e in events
                      if e.action == "research.objective"
                      and e.target.startswith("obj-")]
        done = st["objectives"]
        if not done <= len(registered) <= done + 1:
            raise CannotResume(
                f"the state records {done} objectives and the log "
                f"registers {len(registered)}; a gap of more than the one "
                "in flight means the state is not this log's")
        c = st["counters"]
        self.resets, self.restarts = c["resets"], c["restarts"]
        self.process_restarts = c["process_restarts"] + 1
        self.recovery_failures = c["recovery_failures"]
        self.resumed = list(st["resumed"])
        self.stages = list(st["stages"])
        self.invalidations = list(st["invalidations"])
        self.invalidated = list(st["invalidated"])
        self._elapsed_before = st["elapsed_seconds"]
        for i, oid in enumerate(registered):
            self.agent.plan(oid, i)
        self.objectives = len(registered)
        if len(registered) == done + 1:
            # Registered after the last save: the log says it exists, and
            # its own counters went with the process. One controller is
            # counted for it: the one that finishes it now, or the one
            # that finished it before the process died.
            oid = registered[-1]
            found = R.ResearchLedger(self.w.log).load().get(oid).status
            if found == "OPEN":
                try:
                    self.controller().run(oid)
                except Exception:                # noqa: BLE001 -- counted
                    self.recovery_failures += 1
            else:
                self.resets += 1
            now = R.ResearchLedger(self.w.log).load().get(oid).status
            if now == "OPEN":
                self.recovery_failures += 1
            self.resumed.append({"objective": oid, "found": found,
                                 "now": now})
        # A withdrawal the dead process began and did not follow through
        # is finished by the governed path's own rule; settling twice is
        # settling once.
        GovernedModelRuns(root=ROOT, log=self.w.log,
                          evidence=self.w.evidence).settle()
        events = self.w.events()
        self._save((events[-1].seq, events[-1].hash))

    # ---- driving ---------------------------------------------------------
    def controller(self, fault=None):
        self.resets += 1
        c = self.w.controller()
        c.fault = fault or (lambda point: None)
        return c

    def objective(self):
        i = self.objectives
        self.objectives += 1
        oid = f"obj-{i:05d}"
        p = self.agent.plan(oid, i)
        R.ResearchLedger(self.w.log).load().register(
            objective_id=oid, statement=f"objective {i}", owner="owner",
            max_steps=len(p["hyps"]) + 4,
            max_inference_calls=2 * (len(p["hyps"]) + 4),
            question={"prediction": _prediction(p["hyps"][0][1]),
                      "parameters": E.SLAB_RUN["parameters"]})
        fault = None
        if i % 7 == 3:
            point = CRASH_POINTS[(i // 7) % len(CRASH_POINTS)]
            fired: list = []

            def fault(at, point=point, fired=fired):
                if at == point and not fired:
                    fired.append(at)
                    raise L.Crash(at)
        _die(f"after_register:{i}")
        armed: Any = fault
        dying = os.environ.get("RESEARCH_HORIZON_DIE_AT", "")
        if dying.endswith(f":{i}") and dying.split(":")[0] in CRASH_POINTS:
            armed = _die_at(dying)
        try:
            self.controller(armed).run(oid)
        except L.Crash:
            self.restarts += 1
            try:
                self.controller().run(oid)
            except Exception:                    # noqa: BLE001 -- counted
                self.recovery_failures += 1
        if R.ResearchLedger(self.w.log).load().get(oid).status == "OPEN":
            self.recovery_failures += 1
        _die(f"after_objective:{i}")

    def invalidate_one(self):
        """Withdraw the first verified run, then the bytes it produced, and
        follow what rested on each.

        Every objective here runs one model on the same inputs, so every
        run's bundle is the same bytes. Withdrawing one task therefore
        moves nothing: other tasks standing VERIFIED captured those bytes
        too, and a result keeps its origin while one does, the rule reuse
        applies. That is measured here, not assumed. Withdrawing the bytes
        themselves invalidates every task that captured them, which is the
        widest cascade this history allows (D-2026-188)."""
        task = next(e.target for e in self.w.events()
                    if e.action == "task.create" and e.payload["tool_id"] ==
                    "model.thermal.slab_transient.run")
        gov = GovernedModelRuns(root=ROOT, log=self.w.log,
                                evidence=self.w.evidence).gov
        if gov.projection().tasks[task].state.value == "INVALIDATED":
            # A process that died after withdrawing it, before recording
            # that it had: the withdrawal stands in the log, and its counts
            # went with the process (AUD-013).
            self.invalidations.append({
                "withdrawn": "one task", "origin": f"task:{task}",
                "resumed": "withdrawn by a process that stopped before "
                           "recording it; settled, not repeated"})
            return
        inv = self._withdraw("one task", lambda g: g.invalidate_task(
            task, reason="a stage boundary withdraws one run"))
        if inv.kept:
            runs = GovernedModelRuns(root=ROOT, log=self.w.log,
                                     evidence=self.w.evidence)
            bundle = runs.authority.all_records()[inv.kept[0]].evidence[
                "result_bundle"]
            self._withdraw("its bytes", lambda g: g.withdraw_evidence(
                bundle, reason="a stage boundary withdraws one run's "
                               "bytes"))

    def _withdraw(self, what: str, act):
        t = time.monotonic()
        inv = act(GovernedModelRuns(root=ROOT, log=self.w.log,
                                    evidence=self.w.evidence))
        moved = R.ResearchLedger(self.w.log,
                                 evidence=self.w.evidence).load().propagate()
        self.invalidations.append({
            "withdrawn": what, "origin": inv.origin,
            "tasks_invalidated": len(inv.tasks),
            "results_stale": len(inv.plan.affected),
            "results_kept": len(inv.kept),
            "memories_stale": len(inv.memories),
            "moved": len(moved),
            "objectives_stale": sum(m.startswith("obj-") and "." not in m
                                    for m in moved),
            "seconds": round(time.monotonic() - t, 3)})
        return inv

    # ---- measuring -------------------------------------------------------
    def measure(self, target: int) -> dict:
        log = self.w.log
        t0 = time.monotonic()
        report, events = log.read_verified()
        t1 = time.monotonic()
        tracemalloc.start()
        led = R.ResearchLedger(EventLog(log.path)).load()
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        t2 = time.monotonic()
        anomalies = reconstruct_subsystems(log).anomalies
        t3 = time.monotonic()
        store = AuthorityStore(log, evidence=self.w.evidence).load()
        if os.environ.get("RESEARCH_HORIZON_DIE_AT") == \
                f"mid_checkpoint:{target}":
            # dies with the claim in the log and no checkpoint file: the
            # window AuthorityStore.checkpoint leaves, and calls harmless
            setattr(self.checkpoints, "write",
                    lambda cp: _die(f"mid_checkpoint:{target}"))
        store.checkpoint(self.checkpoints)
        cached = AuthorityStore.load_from(
            EventLog(log.path), self.checkpoints, blobs=self.w.evidence,
            evidence=self.w.evidence, require_checkpoint=True)
        decisions = sum(e.action == "research.step" for e in events)
        now = time.monotonic()
        window = (decisions - self._last[0], now - self._last[1])
        self._last = (decisions, now)
        final = {e.target: "OPEN" for e in events
                 if e.action == "research.objective"
                 and e.target.startswith("obj-")}
        for e in events:
            if e.action == "research.status" and e.target in final:
                final[e.target] = e.payload["status"]
        statuses: dict = {}
        for st in final.values():
            statuses[st] = statuses.get(st, 0) + 1
        del led
        return {
            "target_decisions": target,
            "counters": {
                "events": len(events),
                "governed_operations": sum(e.action == "task.create"
                                           for e in events),
                "inference_calls": sum(e.action == "inference.result"
                                       for e in events),
                "scripted_decisions": decisions,
                "verified_records": sum(
                    e.action == "record.transition" and
                    e.payload.get("dst") == "VERIFIED" for e in events),
                "conclusions": sum(e.action == "research.status" and
                                   e.payload.get("status") == "CONCLUDED"
                                   for e in events),
                # one per stage measured; a stage measured again after a
                # resume adds one, and that is the only record it adds
                "checkpoint_records": sum(e.action == "checkpoint.state"
                                          for e in events),
                "objectives": self.objectives,
                "resets": self.resets, "restarts": self.restarts,
                "process_restarts": self.process_restarts},
            "resumed": list(self.resumed),
            "objective_states": dict(sorted(statuses.items())),
            "duplicates": _duplicates(events),
            "recovery_failures": self.recovery_failures,
            "second_reader_anomalies": len(anomalies),
            "log_verifies": report.ok,
            "checkpoint": {
                "equal_state": cached.state_digest() ==
                store.state_digest(),
                "prefix_verified": cached.loaded_prefix_verified},
            "replay_seconds": {"verify": round(t1 - t0, 3),
                               "research_ledger": round(t2 - t1, 3),
                               "second_reader": round(t3 - t2, 3)},
            "ledger_load_peak_bytes": peak,
            "log_bytes": log.path.stat().st_size,
            "seconds_per_decision_in_stage": round(
                window[1] / window[0], 4) if window[0] else None,
            "elapsed_seconds": self._elapsed(),
        }

    def run(self, decisions: int, *, invalidate_at=(1000,),
            on_stage=None, stages=STAGES) -> dict:
        """Every stage up to ``decisions``. ``on_stage(report)`` is called
        as each stage is measured, so a long campaign that is stopped keeps
        the stages it finished."""
        targets = [s for s in stages if s <= decisions] or [decisions]
        measured = {s["target_decisions"] for s in self.stages}
        for target in targets:
            if target in measured:
                continue                         # a resumed campaign's
            while True:
                events = self.w.events()
                # durable before the next objective: what is done, at the
                # position it was done by
                self._save((events[-1].seq, events[-1].hash))
                if sum(e.action == "research.step" for e in events) >= target:
                    break
                self.objective()
            if target in invalidate_at and target not in self.invalidated:
                self.invalidate_one()
                _die(f"after_invalidation:{target}")
                self.invalidated.append(target)
                events = self.w.events()
                self._save((events[-1].seq, events[-1].hash))
            self.stages.append(self.measure(target))
            _die(f"after_measure:{target}")
            events = self.w.events()
            self._save((events[-1].seq, events[-1].hash))
            if on_stage is not None:
                on_stage(self.report())
        return self.report()

    def report(self) -> dict:
        return {"schema": SCHEMA,
                "agent": "SCRIPTED: every decision counted here is a "
                         "scripted decision of the governed loop, never a "
                         "model's",
                "plan": {"alternatives_per_objective": ALTERNATIVES,
                         "abstain_every": 17, "no_proposal_every": 23,
                         "crash_every": 7, "crash_points": list(
                             CRASH_POINTS)},
                "stages": self.stages, "invalidations": self.invalidations}

    def clean(self):
        self.w.clean()
        (ROOT / E.WS / f"{self.name}.lock").unlink(missing_ok=True)


def _decisions(w) -> int:
    return sum(e.action == "research.step" for e in w.events())


def _duplicates(events) -> dict:
    """Anything counted twice that should be once."""
    executions: dict = {}
    receipts: dict = {}
    results: dict = {}
    for e in events:
        p = e.payload if isinstance(e.payload, dict) else {}
        if e.action == "task.execution" and \
                p.get("phase", "complete") == "complete":
            executions[e.target] = executions.get(e.target, 0) + 1
        elif e.action == "proposal.receive":
            receipts[e.target] = receipts.get(e.target, 0) + 1
        elif e.action == "inference.result":
            key = (e.target, p.get("attempt"))
            results[key] = results.get(key, 0) + 1
    return {name: sum(n > 1 for n in d.values()) for name, d in (
        ("executions", executions), ("receipts", receipts),
        ("inference_results", results))}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--decisions", type=int, default=100)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--name", default="horizon",
                    help="the campaign's workspace and lock")
    ap.add_argument("--resume", action="store_true",
                    help="continue the campaign a stopped process left")
    ap.add_argument("--keep", action="store_true",
                    help="keep the workspace after a complete run")
    ap.add_argument("--stages", default=",".join(map(str, STAGES)),
                    help="the stage boundaries, in scripted decisions")
    ap.add_argument("--invalidate-at", default="1000",
                    help="the stage boundaries a run is withdrawn at")
    args = ap.parse_args(argv)
    stages = tuple(int(x) for x in args.stages.split(",") if x)
    invalidate_at = tuple(int(x) for x in args.invalidate_at.split(",") if x)
    def record(out: dict, complete: bool) -> str:
        out["requested_decisions"] = args.decisions
        out["complete"] = complete
        text = json.dumps(out, indent=1, sort_keys=True) + "\n"
        if args.write:
            RESULTS.write_text(text, encoding="utf-8")
        return text

    c = (Campaign.resume(args.name) if args.resume
         else Campaign(args.name))
    try:
        out = c.run(args.decisions, invalidate_at=invalidate_at,
                    stages=stages,
                    on_stage=lambda partial: record(partial, False))
    except BaseException:
        # A stopped campaign's workspace is what --resume continues
        # (AUD-013); it used to be deleted here on any exit.
        print(f"stopped; the workspace is kept at {c.w.base}: continue "
              f"with --resume --name {args.name}", file=sys.stderr)
        raise
    if not args.keep:
        c.clean()
    print(record(out, True))
    return 0 if all(not any(s["duplicates"].values())
                    and s["recovery_failures"] == 0
                    and s["second_reader_anomalies"] == 0
                    and s["log_verifies"] and s["checkpoint"]["equal_state"]
                    for s in out["stages"]) else 1


if __name__ == "__main__":
    sys.exit(main())
