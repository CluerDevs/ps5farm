import argparse
import json
import re
import sys

from .client import Console
from .farm import Farm
from .job import Job

_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$")


class UsageError(Exception):
    """A problem with the user's input files or arguments (exit code 2)."""


def read_hosts(path):
    try:
        with open(path, encoding="utf-8-sig") as fh:
            lines = fh.read().splitlines()
    except OSError as e:
        raise UsageError(f"cannot read hosts file {path}: {e.strerror or e}") from e
    hosts = []
    for n, line in enumerate(lines, 1):
        line = line.split("#")[0].strip()
        if not line:
            continue
        if not _HOST_RE.match(line):
            raise UsageError(f"{path}:{n}: '{line}' is not a plain host name or IPv4 address "
                             "(no http://, no :port, no spaces; use --port / --ftp-port)")
        if line not in hosts:
            hosts.append(line)
    if not hosts:
        raise UsageError(f"{path} lists no consoles (one IP or host name per line)")
    return hosts


def read_jobs(path):
    try:
        with open(path, encoding="utf-8-sig") as fh:
            lines = fh.read().splitlines()
    except OSError as e:
        raise UsageError(f"cannot read jobs file {path}: {e.strerror or e}") from e
    jobs, seen = [], {}
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            job = Job.from_dict(json.loads(line))
        except ValueError as e:  # json.JSONDecodeError is a ValueError too
            raise UsageError(f"{path}:{n}: {e}") from e
        key = job.id.lower()
        if key in seen:
            raise UsageError(f"{path}:{n}: duplicate job id '{job.id}' (first used on line {seen[key]})")
        seen[key] = n
        jobs.append(job)
    if not jobs:
        raise UsageError(f"{path} contains no jobs")
    return jobs


def main(argv=None):
    p = argparse.ArgumentParser(prog="ps5farm", description="Run jobs on a fleet of PS5LM consoles")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("status", "run"):
        s = sub.add_parser(name)
        s.add_argument("--hosts", required=True, help="file with one console IP/host per line")
        s.add_argument("--port", type=int, default=8081)
        s.add_argument("--ftp-port", type=int, default=2121)
    r = sub.choices["run"]
    r.add_argument("--jobs", required=True, help="JSONL: {id, task, expect?, max_steps?, system?}")
    r.add_argument("--out", default="out")
    r.add_argument("--max-attempts", type=int, default=2)
    a = p.parse_args(argv)

    try:
        if a.cmd == "run" and a.max_attempts < 1:
            raise UsageError("--max-attempts must be at least 1")
        consoles = [Console(h, a.port, a.ftp_port) for h in read_hosts(a.hosts)]
        if a.cmd == "status":
            rows = Farm(consoles, out_dir=".").status()
            for row in rows:
                print(json.dumps(row))
            return 0 if all(r["healthy"] and r.get("ftp", True) for r in rows) else 1

        jobs = read_jobs(a.jobs)
        results = Farm(consoles, a.out, max_attempts=a.max_attempts).run(jobs)
    except UsageError as e:
        print(f"ps5farm: error: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"ps5farm: error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nps5farm: interrupted. Jobs already finished are in results.jsonl; consoles may "
              "still be busy finishing their current request.", file=sys.stderr)
        return 130
    ok = sum(r.status == "ok" for r in results)
    print(f"{ok}/{len(results)} jobs ok; results in {a.out}/results.jsonl")
    return 0 if ok == len(results) else 1
