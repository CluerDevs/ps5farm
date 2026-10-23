"""A fake PS5LM console: same HTTP surface, scripted model, files in a temp dir."""
import json
import os
import posixpath
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ps5farm.client import Console

TOOLS = [{"display_name": n, "tool": n, "type": "server", "permissions": {"write": w},
          "definition": {"type": "function", "function": {"name": n, "description": n,
                         "parameters": {"type": "object", "properties": {}}}}}
         for n, w in [("read_file", False), ("write_file", True), ("get_info", False)]]


class FakeConsole:
    """mode: 'good' writes the expected file; 'lazy' answers without writing;
    'rogue' first tries to write outside the job folder; 'down' refuses health."""

    def __init__(self, root, ip, port=0, mode="good"):
        self.root, self.mode = root, mode
        self.script = []        # mode "script": one entry per chat call, see _scripted()
        self.bodies = []        # every chat request body received
        self.tool_calls = []    # (name, params) of every /tools call
        self.active = 0
        self.max_active = 0
        self.jobs_done = []
        self.rogue_tried = False
        self.bodies = []
        fake = self
        os.makedirs(root, exist_ok=True)

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, obj, code=200):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                if self.path == "/health":
                    return self._send({"status": "ok"} if fake.mode != "down" else {"status": "no"}, 200 if fake.mode != "down" else 503)
                if self.path == "/tools":
                    return self._send(TOOLS)
                if self.path == "/v1/models":
                    return self._send({"data": [{"id": "fake", "meta": {"n_ctx": 32768}}]})
                self._send({"error": "nf"}, 404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/tools":
                    return self._send(fake.tool(body["tool"], body["params"]))
                if self.path == "/v1/chat/completions":
                    out = fake.chat(body)
                    if out == "DROP":  # console dies mid-request: close without answering
                        self.close_connection = True
                        self.connection.close()
                        return
                    if isinstance(out, str):  # non-JSON 200 body
                        b = out.encode()
                        self.send_response(200)
                        self.send_header("Content-Length", str(len(b)))
                        self.end_headers()
                        self.wfile.write(b)
                        return
                    if isinstance(out, tuple):  # (http status, json)
                        return self._send(out[1], out[0])
                    return self._send(out)
                self._send({"error": "nf"}, 404)

        self.server = ThreadingHTTPServer((ip, port), H)
        self.ip, self.port = ip, self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def console(self, ftp_port=2121):
        return Console(self.ip, self.port, ftp_port)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    # --- sandboxed tool execution ---
    def tool(self, name, p):
        self.tool_calls.append((name, p))
        if name == "get_info":
            return {"os": "fake", "cwd": ""}
        rel = posixpath.normpath(p["path"].lstrip("/"))
        if rel.startswith(".."):
            return {"error": "path escapes sandbox"}
        full = os.path.join(self.root, rel)
        if name == "write_file":
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as fh:
                fh.write(p["content"])
            return {"result": "file written successfully", "path": rel}
        if name == "read_file":
            if not os.path.isfile(full):
                return {"error": "no such file"}
            with open(full) as fh:
                return {"content": fh.read()}
        return {"error": "unknown tool"}

    # --- scripted model ---
    def _scripted(self, body):
        """Each script entry answers one chat call. It is a dict {"message": {...},
        "finish_reason": "stop"}, the string "DROP", a raw string (sent as a non-JSON body),
        a (status, json) tuple, or a callable(body) returning one of those."""
        if not self.script:
            return {"choices": [{"message": {"role": "assistant", "content": "done"},
                                 "finish_reason": "stop"}], "usage": {"completion_tokens": 1}}
        item = self.script.pop(0)
        item = item(body) if callable(item) else item
        if isinstance(item, dict) and "message" in item:
            return {"choices": [item], "usage": {"completion_tokens": 5}}
        return item

    def chat(self, body):
        self.bodies.append(body)
        if self.mode == "script":
            return self._scripted(body)
        msgs = body["messages"]
        self.bodies.append(body)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.05)
            job = re.search(r"folder (jobs/[^/\s]+)/", msgs[0]["content"]).group(1)
            need = re.search(r"Required files: (.+?)\.(?:\n|$)", msgs[0]["content"])
            fname = (need.group(1).split(",")[0].strip() if need else "out.txt")
            tool_msgs = [m for m in msgs if m["role"] == "tool"]
            reply = lambda m: {"choices": [{"message": m}], "usage": {"completion_tokens": 10}}
            call = lambda path: reply({"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "write_file",
                 "arguments": json.dumps({"path": path, "content": f"done {job}"})}}]})
            if self.mode == "rogue" and not tool_msgs and not self.rogue_tried:
                self.rogue_tried = True
                return call("../../etc/evil.txt")
            if self.mode == "rogue" and len(tool_msgs) == 1:
                return call(f"{job}/{fname}")
            if self.mode == "good" and not tool_msgs:
                return call(f"{job}/{fname}")
            if tool_msgs or self.mode == "lazy":
                self.jobs_done.append(job)
                return reply({"role": "assistant", "content": f"finished {job}"})
            return reply({"role": "assistant", "content": "?"})
        finally:
            self.active -= 1


class LocalExporter:
    """Stands in for FTP: copies from the fake console's folder."""

    def __init__(self, fakes):
        self.fakes = {f.ip: f for f in fakes}

    def cleanup(self, console, job_dir):
        import shutil
        shutil.rmtree(os.path.join(self.fakes[console.host].root, job_dir), ignore_errors=True)

    def fetch(self, console, job_dir, dest):
        import shutil
        src = os.path.join(self.fakes[console.host].root, job_dir)
        if not os.path.isdir(src):
            return []
        shutil.copytree(src, dest, dirs_exist_ok=True)
        return sorted(os.listdir(src))
