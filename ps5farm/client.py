"""HTTP client for one PS5LM console (llama-server on port 8081)."""
import http.client
import json
import socket
import urllib.error
import urllib.request


class ConsoleError(Exception):
    """The console could not be reached or answered with a server error."""


# Consoles live on a private LAN. Never send that traffic through an HTTP(S)_PROXY
# that happens to be set in the user's environment.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


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
            with _OPENER.open(req, timeout=timeout or self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                raw = e.read().decode("utf-8", "replace")
            except (OSError, http.client.HTTPException):
                raw = ""
            if e.code < 500:
                # the tools endpoint reports bad paths etc. as 4xx JSON; hand that to the model
                try:
                    out = json.loads(raw)
                except ValueError:
                    return {"error": raw[:300] or f"HTTP {e.code}"}
                return out if isinstance(out, dict) else {"error": raw[:300]}
            raise ConsoleError(f"{self.host}: HTTP {e.code} {raw[:200]}") from e
        except (urllib.error.URLError, socket.timeout, http.client.HTTPException,
                OSError, ValueError) as e:  # ValueError covers bad JSON / bad UTF-8
            raise ConsoleError(f"{self.host}: {e}") from e

    def health(self):
        try:
            r = self._request("/health", timeout=5)
        except ConsoleError:
            return False
        return isinstance(r, dict) and r.get("status") == "ok"

    def tools(self):
        raw = self._request("/tools", timeout=15)
        try:
            return [t["definition"] for t in raw]
        except (KeyError, TypeError) as e:
            raise ConsoleError(f"{self.host}: unexpected /tools reply: {str(raw)[:200]}") from e

    def run_tool(self, name, params):
        return self._request("/tools", {"tool": name, "params": params}, timeout=60)

    def model(self):
        d = self._request("/v1/models", timeout=15)
        try:
            m = (d.get("data") or [{}])[0]
            return {"id": m.get("id"), "n_ctx": (m.get("meta") or {}).get("n_ctx")}
        except (AttributeError, IndexError, TypeError) as e:
            raise ConsoleError(f"{self.host}: unexpected /v1/models reply") from e

    def chat(self, messages, tools=None, **kw):
        body = {"messages": messages, "temperature": 0.3, "max_tokens": 4000}
        body.update(kw)
        if tools:
            body["tools"] = tools
        return self._request("/v1/chat/completions", body)
