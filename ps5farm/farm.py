"""Scheduler: one job per console at a time, retry on another console on failure."""
import json
import os
import queue
import shutil
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from .client import ConsoleError
from .export import FtpExporter
from .job import JobResult, check_job_id, run_job


def _default_log(s):
    print(s, flush=True)


class _Task:
    def __init__(self, job):
        self.job = job
        self.attempts = 0
        self.excluded = set()  # hosts that already failed this job
        self.errors = []


class Farm:
    def __init__(self, consoles, out_dir, exporter=None, max_attempts=2,
                 cooldown=30, log=_default_log):
        seen, self.consoles = set(), []
        for c in consoles:  # two entries for one host would run two jobs on a one-slot server
            if c.host not in seen:
                seen.add(c.host)
                self.consoles.append(c)
        self.out_dir = out_dir
        self.exporter = exporter or FtpExporter()
        self.max_attempts = max(1, max_attempts)
        self.cooldown = cooldown
        self.log = log
        self._cool = {}  # host -> time it may be tried again
        self._lock = threading.Lock()

    # ---- fleet ----
    def _status_one(self, c):
        ok = c.health()
        info = {}
        if ok:
            try:
                info = c.model()
                info["tools"] = [t["function"]["name"] for t in c.tools()]
            except (ConsoleError, KeyError, TypeError):
                info = {"error": "healthy but /v1/models or /tools gave an unexpected reply"}
        row = {"host": c.host, "healthy": ok, **info}
        check = getattr(self.exporter, "check", None)
        if check:
            err = check(c)
            row["ftp"] = err is None
            if err:
                row["ftp_error"] = err
        return row

    def status(self):
        """Read-only: health, model, tools and anonymous-FTP login for every console."""
        if not self.consoles:
            return []
        with ThreadPoolExecutor(max_workers=min(16, len(self.consoles))) as ex:
            return list(ex.map(self._status_one, self.consoles))

    def _usable(self, c):
        with self._lock:
            if self._cool.get(c.host, 0) > time.time():
                return False
        if c.health():
            return True
        self._cool_down(c)
        self.log(f"{c.host}: not healthy, cooling down {self.cooldown}s")
        return False

    def _cool_down(self, c):
        with self._lock:
            self._cool[c.host] = time.time() + self.cooldown

    # ---- one attempt ----
    def _attempt(self, console, task):
        job = task.job
        dest = os.path.join(self.out_dir, job.id)
        res = JobResult(job_id=job.id, console=console.host)
        try:
            # Must succeed: stale files from an earlier run must never satisfy `expect`.
            self.exporter.cleanup(console, job.dir)
        except Exception as e:
            res.status, res.error = "failed", f"could not clean job folder over FTP (is ftpsrv running on port {console.ftp_port}?): {e}"
            return res
        shutil.rmtree(dest, ignore_errors=True)  # id is validated: this is <out>/<safe-id>
        res = run_job(console, job, log=self.log)
        if res.status != "ok":
            return res
        try:
            res.files = self.exporter.fetch(console, job.dir, dest)
        except Exception as e:  # FTP refused, timeout, odd listing, ...
            res.status, res.error = "failed", f"export failed (is ftpsrv running on {console.ftp_port}?): {e}"
            return res
        paths = [os.path.join(dest, *f.split("/")) for f in job.expect]
        missing = [f for f, p in zip(job.expect, paths)
                   if not os.path.isfile(p) or os.path.getsize(p) == 0]
        if missing:
            res.status, res.error = "failed", f"missing or empty: {', '.join(missing)}"
        elif not res.files:
            res.status, res.error = "failed", "the model finished but wrote no files"
        return res

    def _work(self, console, task, out):
        """Runs on a worker thread. Always puts exactly one result on `out`."""
        task.attempts += 1
        try:
            res = self._attempt(console, task)
        except ConsoleError as e:
            self._cool_down(console)
            res = JobResult(job_id=task.job.id, console=console.host,
                            status="console_error", error=str(e))
        except BaseException as e:  # a bug must fail this job, not strand the console
            res = JobResult(job_id=task.job.id, console=console.host, status="failed",
                            error=f"internal error: {type(e).__name__}: {e}")
        res.console = res.console or console.host
        res.attempts = task.attempts
        out.put((console, task, res))

    # ---- run ----
    def run(self, jobs):
        """Run all jobs; returns one JobResult per job (in completion order)."""
        jobs = list(jobs)
        seen = set()
        for j in jobs:
            check_job_id(j.id)
            if j.id.lower() in seen:  # also catches 'A' vs 'a' on case-insensitive disks
                raise ValueError(f"duplicate job id {j.id!r}")
            seen.add(j.id.lower())
        if jobs and not self.consoles:
            raise ValueError("no consoles to run on")
        os.makedirs(self.out_dir, exist_ok=True)
        pending = deque(_Task(j) for j in jobs)
        idle = list(self.consoles)
        running = {}  # host -> task
        done_q = queue.Queue()
        results = []
        give_up_after = self.cooldown * 2 + min(5, self.cooldown)
        last_progress = time.time()
        while pending or running:
            for task in list(pending):
                if not idle:
                    break
                pref = [c for c in idle if c.host not in task.excluded]
                other = [c for c in idle if c.host in task.excluded]
                # Prefer a console that has not failed this job; fall back only when nothing
                # else could ever take it (single console, or the rest are down).
                cands = pref + (other if (not running or not pref and
                                          all(c.host in task.excluded for c in self.consoles)) else [])
                for c in cands:
                    if not self._usable(c):
                        continue
                    idle.remove(c)
                    pending.remove(task)
                    self.log(f"[{task.job.id}] -> {c.host} (attempt {task.attempts + 1})")
                    running[c.host] = task
                    threading.Thread(target=self._work, args=(c, task, done_q), daemon=True).start()
                    last_progress = time.time()
                    break
            if running:
                try:
                    c, task, res = done_q.get(timeout=1)
                except queue.Empty:
                    continue
                running.pop(c.host, None)
                idle.append(c)
                last_progress = time.time()
                if res.status == "ok":
                    results.append(self._record(res))
                    continue
                task.errors.append(f"{res.console}: {res.error}")
                task.excluded.add(res.console)
                if task.attempts < self.max_attempts:
                    self.log(f"[{task.job.id}] retrying after: {res.error}")
                    pending.appendleft(task)
                else:
                    res.error = " | ".join(task.errors)
                    results.append(self._record(res))
            else:
                time.sleep(min(1, max(0.05, self.cooldown / 4)))
                if time.time() - last_progress > give_up_after:
                    for task in pending:
                        r = JobResult(job_id=task.job.id, status="console_error",
                                      attempts=task.attempts,
                                      error=" | ".join(["no healthy console available"] + task.errors))
                        results.append(self._record(r))
                    pending.clear()
        return results

    def _record(self, res):
        line = json.dumps(res.to_dict()) + "\n"
        with self._lock:
            with open(os.path.join(self.out_dir, "results.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(line)
        self.log(f"[{res.job_id}] {res.status} on {res.console or '-'} ({res.seconds}s, {res.tokens} tok)"
                 + (f" - {res.error}" if res.error else ""))
        return res
