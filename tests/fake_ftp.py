"""A tiny anonymous FTP server that behaves like ftpsrv where it matters:
no NLST, `226` replies to DELE and RMD, LIST in `ls -l` format, passive mode only.
Remote paths under /data/PS5LM/scratch map to a local directory."""
import os
import socket
import socketserver
import stat
import threading

SCRATCH = "/data/PS5LM/scratch"


class _Handler(socketserver.StreamRequestHandler):
    def reply(self, text):
        self.wfile.write((text + "\r\n").encode("latin-1"))
        self.wfile.flush()

    def local(self, remote):
        remote = remote.strip()
        if not remote.startswith(SCRATCH):
            return None
        return os.path.join(self.server.root, remote[len(SCRATCH):].lstrip("/"))

    def data_conn(self):
        conn, _ = self.pasv.accept()
        self.pasv.close()
        self.pasv = None
        return conn

    def list_line(self, path, name):
        st = os.lstat(os.path.join(path, name))
        kind = "l" if stat.S_ISLNK(st.st_mode) else "d" if stat.S_ISDIR(st.st_mode) else "-"
        line = self.server.line_fmt.format(kind=kind, size=st.st_size, name=name)
        if kind == "l":
            line += " -> " + os.readlink(os.path.join(path, name))
        return line

    def handle(self):
        self.pasv = None
        try:
            self.serve()
        finally:
            if self.pasv:
                self.pasv.close()

    def drop_pasv(self):
        if self.pasv:
            self.pasv.close()
            self.pasv = None

    def serve(self):
        self.reply("220 fake ftpsrv")
        for raw in self.rfile:
            cmd, _, arg = raw.decode("latin-1").rstrip("\r\n").partition(" ")
            cmd = cmd.upper()
            self.server.log.append((cmd, arg))
            if cmd == "USER":
                self.reply("331 ok")
            elif cmd == "PASS":
                self.reply("230 ok")
            elif cmd in ("TYPE", "NOOP"):
                self.reply("200 ok")
            elif cmd == "PASV":
                self.pasv = socket.socket()
                self.pasv.bind(("127.0.0.1", 0))
                self.pasv.listen(1)
                p = self.pasv.getsockname()[1]
                self.reply(f"227 Entering Passive Mode (127,0,0,1,{p >> 8},{p & 255})")
            elif cmd == "LIST":
                path = self.local(arg)
                if path is None or not os.path.isdir(path):
                    self.drop_pasv()
                    self.reply("550 no such directory")
                    continue
                self.reply("150 here it comes")
                conn = self.data_conn()
                lines = ["total 0", self.server.line_fmt.format(kind="d", size=0, name="."),
                         self.server.line_fmt.format(kind="d", size=0, name="..")]
                lines += [self.list_line(path, n) for n in sorted(os.listdir(path))]
                conn.sendall(("\r\n".join(lines) + "\r\n").encode("latin-1"))
                conn.close()
                self.reply("226 done")
            elif cmd == "RETR":
                path = self.local(arg)
                if path is None or not os.path.isfile(path):
                    self.drop_pasv()
                    self.reply("550 no such file")
                    continue
                self.reply("150 sending")
                conn = self.data_conn()
                with open(path, "rb") as fh:
                    conn.sendall(fh.read())
                conn.close()
                self.reply("226 done")
            elif cmd == "DELE":
                path = self.local(arg)
                if path is None or not (os.path.isfile(path) or os.path.islink(path)):
                    self.reply("550 no such file")
                    continue
                os.remove(path)
                self.reply("226 deleted")  # ftpsrv quirk: not 250
            elif cmd == "RMD":
                path = self.local(arg)
                try:
                    os.rmdir(path)
                except (OSError, TypeError):
                    self.reply("550 cannot remove")
                    continue
                self.reply("226 removed")
            elif cmd == "QUIT":
                self.reply("221 bye")
                return
            else:  # includes NLST
                self.reply("502 not implemented")


class FakeFtp(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    DEFAULT_FMT = "{kind}rwxr-xr-x 1 ps5 ps5 {size} Jan  1 12:00 {name}"

    def __init__(self, root, line_fmt=DEFAULT_FMT):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.root = root
        self.line_fmt = line_fmt
        self.log = []
        self.port = self.server_address[1]
        threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def stop(self):
        self.shutdown()
        self.server_close()
