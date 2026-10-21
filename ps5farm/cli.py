import argparse
import json
import sys

from .client import Console
from .farm import Farm
from .job import Job


def read_hosts(path):
    hosts = []
    for line in open(path):
        line = line.split("#")[0].strip()
        if line:
            hosts.append(line)
    return hosts


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

    consoles = [Console(h, a.port, a.ftp_port) for h in read_hosts(a.hosts)]
    if a.cmd == "status":
        farm = Farm(consoles, out_dir=".")
        rows = farm.status()
        for row in rows:
            print(json.dumps(row))
        return 0 if all(r["healthy"] for r in rows) else 1

    jobs = [Job.from_dict(json.loads(l)) for l in open(a.jobs) if l.strip()]
    ids = [j.id for j in jobs]
    if len(set(ids)) != len(ids):
        sys.exit("duplicate job ids in jobs file")
    results = Farm(consoles, a.out, max_attempts=a.max_attempts).run(jobs)
    ok = sum(r.status == "ok" for r in results)
    print(f"{ok}/{len(results)} jobs ok; results in {a.out}/results.jsonl")
    return 0 if ok == len(results) else 1
