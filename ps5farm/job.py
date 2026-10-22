"""One job on one console: drive the model's tool-calling loop to completion."""
import json
import posixpath
import re
import time
from dataclasses import dataclass, field

from .client import Console, ConsoleError

# A job id becomes a folder name on the console (jobs/<id>) AND a folder on this PC
# (<out>/<id>), and is interpolated into FTP paths that get deleted. Keep it boring.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_RESERVED = {"results", "con", "prn", "aux", "nul",
             *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_FIELDS = {"id", "task", "expect", "max_steps", "max_tokens", "system"}
MAX_TOOL_OUTPUT = 12000   # chars of one tool result fed back to the model
MAX_TOKENS_CAP = 16000    # ceiling when a reply is cut off and we ask again with more room


def check_job_id(job_id):
    """Raise ValueError unless `job_id` is safe to use as a folder name."""
    if not isinstance(job_id, str) or not _ID_RE.match(job_id):
        raise ValueError(
            f"bad job id {job_id!r}: use 1-64 letters, digits, '.', '_' or '-', "
            "starting with a letter or digit (no spaces or slashes)")
    if job_id.endswith(".") or job_id.split(".")[0].lower() in _RESERVED:
        # 'results.*' would collide with out/results.jsonl; the others are Windows device names
        raise ValueError(f"bad job id {job_id!r}: reserved name")
    return job_id


def _clean_expect(expect):
    if isinstance(expect, str):
        expect = [expect]
    if not isinstance(expect, (list, tuple)):
        raise ValueError("'expect' must be a list of file names")
    out = []
    for e in expect:
        if not isinstance(e, str) or not e.strip() or "\x00" in e or "\\" in e:
            raise ValueError(f"bad 'expect' entry {e!r}")
        p = posixpath.normpath(e.strip())
        if p.startswith("/") or p == ".." or p.startswith("../") or p == ".":
            raise ValueError(f"'expect' entry {e!r} must be a relative path inside the job folder")
        out.append(p)
    return out


@dataclass
class Job:
    id: str
    task: str
    expect: list = field(default_factory=list)  # files that must exist in the job folder
    max_steps: int = 8
    max_tokens: int = 4000
    system: str = ""  # extra instructions appended to the worker prompt

    def __post_init__(self):
        check_job_id(self.id)
        if not isinstance(self.task, str) or not self.task.strip():
            raise ValueError(f"job {self.id}: 'task' must be a non-empty string")
        self.expect = _clean_expect(self.expect)
        for name in ("max_steps", "max_tokens"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int) or v < 1:
                raise ValueError(f"job {self.id}: '{name}' must be a positive integer")
        if not isinstance(self.system, str):
            raise ValueError(f"job {self.id}: 'system' must be a string")

    @property
    def dir(self):
        return f"jobs/{self.id}"

    @classmethod
    def from_dict(cls, d):
        if not isinstance(d, dict):
            raise ValueError("a job must be a JSON object")
        unknown = set(d) - _FIELDS
        if unknown:
            raise ValueError(f"unknown job field(s): {', '.join(sorted(unknown))} "
                             f"(allowed: {', '.join(sorted(_FIELDS))})")
        for req in ("id", "task"):
            if req not in d:
                raise ValueError(f"job is missing '{req}'")
        if isinstance(d["id"], bool) or not isinstance(d["id"], (str, int)):
            raise ValueError(f"bad job id {d['id']!r}")
        try:
            return cls(
                id=str(d["id"]), task=d["task"], expect=d.get("expect") or [],
                max_steps=int(d.get("max_steps", 8)), max_tokens=int(d.get("max_tokens", 4000)),
                system=d.get("system") or "",
            )
        except (TypeError, ValueError) as e:
            raise ValueError(str(e)) from e


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


def _norm(path):
    return posixpath.normpath(str(path).lstrip("/"))


def confined(job, path):
    """True when `path` stays inside the job folder."""
    p = _norm(path)
    return p == job.dir or p.startswith(job.dir + "/")


def _guard(job, name, args):
    """Return (error, args). On success `args` has its path rewritten to the normalised
    relative form that was actually checked, so what we verify is what the console gets."""
    if name == "get_info":
        return None, args
    path = args.get("path")
    if not isinstance(path, str) or path in ("", ".") or "\x00" in path:
        return f"path is required and must be inside {job.dir}/", args
    p = _norm(path)
    if not confined(job, p):
        return f"path '{path}' is outside {job.dir}/. Use paths under {job.dir}/", args
    if p == job.dir and name in ("write_file", "edit_file"):
        return f"'{path}' is the job folder itself; write a file inside it, e.g. {job.dir}/name.txt", args
    return None, dict(args, path=p)


def _parse_args(raw):
    """Tool arguments should be a JSON string, but some servers/models send a dict."""
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            v = json.loads(raw)
        except ValueError:
            raise ValueError("tool arguments were not valid JSON") from None
        if v is None:
            return {}
        if isinstance(v, dict):
            return v
    raise ValueError("tool arguments must be a JSON object")


def run_job(console: Console, job: Job, tools=None, log=lambda s: None) -> JobResult:
    """Run `job` to completion. Raises ConsoleError if the console dies mid-job."""
    res = JobResult(job_id=job.id, console=console.host)
    t0 = time.time()
    tools = tools or console.tools()
    msgs = [{"role": "system", "content": system_prompt(job)},
            {"role": "user", "content": job.task}]
    seen = set()
    max_tokens = job.max_tokens
    made_calls = False
    for step in range(job.max_steps):
        r = console.chat(msgs, tools=tools, max_tokens=max_tokens)
        if not isinstance(r, dict):
            raise ConsoleError(f"{console.host}: bad chat reply: {str(r)[:200]}")
        if "choices" not in r and "error" in r:
            # a 4xx from the server (e.g. prompt too long): a problem with this job, not the console
            res.status, res.error = "failed", f"chat request rejected: {str(r['error'])[:300]}"
            break
        choices = r.get("choices")
        if (not isinstance(choices, list) or not choices or not isinstance(choices[0], dict)
                or not isinstance(choices[0].get("message"), dict)):
            raise ConsoleError(f"{console.host}: bad chat reply: {str(r)[:200]}")
        choice = choices[0]
        msg = choice["message"]
        res.steps += 1
        usage = r.get("usage")
        used = usage.get("completion_tokens") if isinstance(usage, dict) else 0
        res.tokens += used if isinstance(used, int) else 0

        raw_calls = msg.get("tool_calls")
        calls = []
        for i, c in enumerate(raw_calls if isinstance(raw_calls, list) else []):
            c = c if isinstance(c, dict) else {}
            fn = c.get("function") if isinstance(c.get("function"), dict) else {}
            name = fn.get("name") if isinstance(fn.get("name"), str) else ""
            calls.append({"id": str(c.get("id") or f"call_{step}_{i}"), "name": name,
                          "raw": fn.get("arguments")})
        content = msg.get("content")
        content = content.strip() if isinstance(content, str) else ""

        if not calls:
            if choice.get("finish_reason") == "length":
                # Reasoning models can spend the whole budget thinking. Ask once more with more room.
                if max_tokens < MAX_TOKENS_CAP and step + 1 < job.max_steps:
                    log(f"[{job.id}] reply cut off at {max_tokens} tokens; retrying with more")
                    max_tokens = min(max_tokens * 2, MAX_TOKENS_CAP)
                    continue
                res.status = "failed"
                res.error = (f"model ran out of tokens (max_tokens={max_tokens}) before answering; "
                             "raise max_tokens or simplify the task")
                break
            if not content and not made_calls:
                res.status, res.error = "failed", "model returned an empty reply (no text, no tool call)"
                break
            res.final = content
            res.status = "ok"
            break

        made_calls = True
        # Echo the calls back in canonical form (string arguments, guaranteed ids).
        msgs.append({"role": "assistant", "content": content, "tool_calls": [
            {"id": c["id"], "type": "function",
             "function": {"name": c["name"],
                          "arguments": c["raw"] if isinstance(c["raw"], str)
                          else json.dumps(c["raw"] if c["raw"] is not None else {})}}
            for c in calls]})
        for c in calls:
            name = c["name"]
            try:
                args = _parse_args(c["raw"])
            except ValueError as e:
                out = {"error": str(e)}
            else:
                err, args = ("tool call has no name", args) if not name else _guard(job, name, args)
                key = (name, json.dumps(args, sort_keys=True, default=str))
                if err:
                    out = {"error": err}
                elif key in seen and name not in ("write_file", "edit_file"):
                    out = {"error": "you already made this exact call; use its result"}
                else:
                    seen.add(key)
                    out = console.run_tool(name, args)
                    if name in ("write_file", "edit_file") and not (isinstance(out, dict) and "error" in out):
                        seen.clear()  # files changed: re-reading them is legitimate again
            text = json.dumps(out, default=str)
            log(f"[{job.id}] step {step} {name} -> {text[:100]}")
            if len(text) > MAX_TOOL_OUTPUT:
                text = text[:MAX_TOOL_OUTPUT] + " ...[truncated]"
            msgs.append({"role": "tool", "tool_call_id": c["id"], "content": text})
    else:
        res.status = "failed"
        res.error = f"no final answer after {job.max_steps} steps"
    res.seconds = round(time.time() - t0, 1)
    return res
