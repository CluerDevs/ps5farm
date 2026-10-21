"""HTTP client for one PS5LM console (llama-server on port 8081)."""
import json
import socket
import urllib.error
import urllib.request


class ConsoleError(Exception):
    """The console could not be reached or answered with a server error."""


class Console:
    def __init__(self, host, port=8081, ftp_port=2121, timeout=900):
        self.host = host
        self.port = port
        self.ftp_port = ftp_port
        self.timeout = timeout

    @property
    def base(self):
        return f"http://{self.host}:{self.port}"

    def __repr__(self):
        return f"Console({self.host})"

    def _request(self, path, body=None, timeout=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            self.base + path, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            if e.code < 500:
                # the tools endpoint reports bad paths etc. as 4xx JSON; hand that to the model
                try:
                    return json.loads(raw)
                except ValueError:
                    return {"error": raw[:300]}
            raise ConsoleError(f"{self.host}: HTTP {e.code} {raw[:200]}") from e
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError, ValueError) as e:
            raise ConsoleError(f"{self.host}: {e}") from e

    def health(self):
        try:
            return self._request("/health", timeout=5).get("status") == "ok"
        except ConsoleError:
            return False

    def tools(self):
        return [t["definition"] for t in self._request("/tools", timeout=15)]

    def run_tool(self, name, params):
        return self._request("/tools", {"tool": name, "params": params}, timeout=60)

    def model(self):
        d = self._request("/v1/models", timeout=15)
        m = (d.get("data") or [{}])[0]
        return {"id": m.get("id"), "n_ctx": (m.get("meta") or {}).get("n_ctx")}

    def chat(self, messages, tools=None, **kw):
        body = {"messages": messages, "temperature": 0.3, "max_tokens": 4000}
        body.update(kw)
        if tools:
            body["tools"] = tools
        return self._request("/v1/chat/completions", body)
