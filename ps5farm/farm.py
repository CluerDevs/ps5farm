"""Scheduler: one job per console at a time, retry on another console on failure."""
import json
import os
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from .client import Console, ConsoleError
from .export import FtpExporter
from .job import Job, JobResult, run_job


class _Task:
    def __init__(self, job):
        self.job = job
        self.attempts = 0
        self.excluded = set()  # hosts that already failed this job
        self.errors = []


class Farm:
    def __init__(self, consoles, out_dir, exporter=None, max_attempts=2,
                 cooldown=30, log=print):
        self.consoles = list(consoles)
        self.out_dir = out_dir
        self.exporter = exporter or FtpExporter()
        self.max_attempts = max_attempts
        self.cooldown = cooldown
        self.log = log
        self._cool = {}  # host -> time it may be tried again
        self._lock = threading.Lock()

    # ---- fleet ----
    def status(self):
        rows = []
        for c in self.consoles:
            ok = c.health()
            info = {}
            if ok:
                try:
                    info = c.model()
                    info["tools"] = [t["function"]["name"] for t in c.tools()]
                except ConsoleError:
                    pass
            rows.append({"host": c.host, "healthy": ok, **info})
        return rows

    def _usable(self, c):
        with self._lock:
            if self._cool.get(c.host, 0) > time.time():
                return False
        if c.health():
            return True
        with self._lock:
            self._cool[c.host] = time.time() + self.cooldown
        self.log(f"{c.host}: not healthy, cooling down {self.cooldown}s")
        return False

    # ---- one attempt ----
    def _attempt(self, console, task):
        job = task.job
        dest = os.path.join(self.out_dir, job.id)
        self.exporter.cleanup(console, job.dir)
        res = run_job(console, job, log=self.log)
        if res.status != "ok":
            return res
        try:
            res.files = self.exporter.fetch(console, job.dir, dest)
        except Exception as e:  # FTP refused, timeout, ...
            res.status, res.error = "failed", f"export failed (is ftpsrv running on {console.ftp_port}?): {e}"
            return res
        missing = [f for f in job.expect if not os.path.isfile(os.path.join(dest, f))
                   or os.path.getsize(os.path.join(dest, f)) == 0]
        if missing:
            res.status, res.error = "failed", f"missing or empty: {', '.join(missing)}"
        return res

    def _work(self, console, task):
        task.attempts += 1
        try:
            res = self._attempt(console, task)
        except ConsoleError as e:
            with self._lock:
                self._cool[console.host] = time.time() + self.cooldown
            res = JobResult(job_id=task.job.id, console=console.host,
                            status="console_error", error=str(e))
        res.attempts = task.attempts
        return res

    # ---- run ----
    def run(self, jobs):
        """Run all jobs; returns one JobResult per job (in completion order)."""
        os.makedirs(self.out_dir, exist_ok=True)
        pending = deque(_Task(j) for j in jobs)
        idle = list(self.consoles)
        running = {}  # future -> (console, task)
        results = []
        idle_since = time.time()
        with ThreadPoolExecutor(max_workers=max(1, len(self.consoles))) as ex:
            while pending or running:
                progressed = True
                while pending and idle and progressed:
                    progressed = False
                    task = pending[0]
                    for c in idle:
                        if c.host in task.excluded and len(self.consoles) > len(task.excluded):
                            continue
                        if not self._usable(c):
                            continue
                        idle.remove(c)
                        pending.popleft()
                        self.log(f"[{task.job.id}] -> {c.host} (attempt {task.attempts + 1})")
                        running[ex.submit(self._work, c, task)] = (c, task)
                        progressed = True
                        break
                if running:
                    idle_since = time.time()
                    done, _ = wait(list(running), timeout=1, return_when=FIRST_COMPLETED)
                    for f in done:
                        c, task = running.pop(f)
                        idle.append(c)
                        res = f.result()
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
                    time.sleep(1)
                    if time.time() - idle_since > self.cooldown * 2 + 5:
                        for task in pending:
                            r = JobResult(job_id=task.job.id, status="console_error",
                                          attempts=task.attempts, error="no healthy console available")
                            results.append(self._record(r))
                        pending.clear()
        return results

    def _record(self, res):
        with self._lock:
            with open(os.path.join(self.out_dir, "results.jsonl"), "a") as fh:
                fh.write(json.dumps(res.to_dict()) + "\n")
        self.log(f"[{res.job_id}] {res.status} on {res.console} ({res.seconds}s, {res.tokens} tok)"
                 + (f" - {res.error}" if res.error else ""))
        return res
