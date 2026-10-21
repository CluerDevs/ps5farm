"""One job on one console: drive the model's tool-calling loop to completion."""
import json
import posixpath
import time
from dataclasses import dataclass, field

from .client import Console, ConsoleError


@dataclass
class Job:
    id: str
    task: str
    expect: list = field(default_factory=list)  # files that must exist in the job folder
    max_steps: int = 8
    max_tokens: int = 4000
    system: str = ""  # extra instructions appended to the worker prompt

    @property
    def dir(self):
        return f"jobs/{self.id}"

    @classmethod
    def from_dict(cls, d):
        return cls(
            id=str(d["id"]), task=d["task"], expect=list(d.get("expect", [])),
            max_steps=int(d.get("max_steps", 8)), max_tokens=int(d.get("max_tokens", 4000)),
            system=d.get("system", ""),
        )


@dataclass
class JobResult:
    job_id: str
    status: str = "pending"  # ok | failed | console_error
    console: str = ""
    attempts: int = 0
    steps: int = 0
    tokens: int = 0
    seconds: float = 0.0
    final: str = ""
    error: str = ""
    files: list = field(default_factory=list)

    def to_dict(self):
        return self.__dict__.copy()


def system_prompt(job):
    return (
        f"You are a worker node on a PS5 console. Work ONLY inside the folder {job.dir}/ "
        "(use relative paths; the file tools are sandboxed). You can read, search, write and "
        "edit files but you cannot run code. Save every deliverable with write_file, then "
        "reply with a one-line summary of what you produced."
        + (f"\nRequired files: {', '.join(job.expect)}." if job.expect else "")
        + (("\n" + job.system) if job.system else "")
    )


def confined(job, path):
    """True when `path` stays inside the job folder."""
    p = posixpath.normpath(str(path).lstrip("/"))
    return p == job.dir or p.startswith(job.dir + "/")


def _guard(job, name, args):
    if name == "get_info":
        return None
    path = args.get("path")
    if path in (None, "", "."):
        return f"path is required and must be inside {job.dir}/"
    if not confined(job, path):
        return f"path '{path}' is outside {job.dir}/. Use paths under {job.dir}/"
    return None


def run_job(console: Console, job: Job, tools=None, log=lambda s: None) -> JobResult:
    """Run `job` to completion. Raises ConsoleError if the console dies mid-job."""
    res = JobResult(job_id=job.id, console=console.host)
    t0 = time.time()
    tools = tools or console.tools()
    msgs = [{"role": "system", "content": system_prompt(job)},
            {"role": "user", "content": job.task}]
    seen = set()
    for step in range(job.max_steps):
        r = console.chat(msgs, tools=tools, max_tokens=job.max_tokens)
        if "choices" not in r:
            raise ConsoleError(f"{console.host}: bad chat reply: {str(r)[:200]}")
        msg = r["choices"][0]["message"]
        res.steps += 1
        res.tokens += (r.get("usage") or {}).get("completion_tokens", 0)
        calls = msg.get("tool_calls") or []
        msgs.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})
        if not calls:
            res.final = (msg.get("content") or "").strip()
            res.status = "ok"
            break
        for c in calls:
            name = c["function"]["name"]
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except ValueError:
                out = {"error": "tool arguments were not valid JSON"}
            else:
                key = (name, json.dumps(args, sort_keys=True))
                err = _guard(job, name, args)
                if err:
                    out = {"error": err}
                elif key in seen and name not in ("write_file", "edit_file"):
                    out = {"error": "you already made this exact call; use its result"}
                else:
                    seen.add(key)
                    out = console.run_tool(name, args)
            log(f"[{job.id}] step {step} {name} -> {json.dumps(out)[:100]}")
            msgs.append({"role": "tool", "tool_call_id": c.get("id", ""), "content": json.dumps(out)})
    else:
        res.status = "failed"
        res.error = f"no final answer after {job.max_steps} steps"
    res.seconds = round(time.time() - t0, 1)
    return res
