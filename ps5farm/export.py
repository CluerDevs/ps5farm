"""Move job files off a console over ftpsrv (port 2121, anonymous)."""
import os
from ftplib import FTP, error_perm, error_reply

SCRATCH = "/data/PS5LM/scratch"


def _lines(ftp, path):
    out = []
    ftp.retrlines("LIST " + path, out.append)  # ftpsrv has no NLST
    return out


def _entries(ftp, path):
    for line in _lines(ftp, path):
        parts = line.split(None, 8)
        if len(parts) < 9 or parts[8] in (".", ".."):
            continue
        yield parts[0].startswith("d"), parts[8]


class FtpExporter:
    """Fetch and clean job folders. One short-lived connection per call."""

    def __init__(self, timeout=60):
        self.timeout = timeout

    def _connect(self, console):
        ftp = FTP()
        ftp.connect(console.host, console.ftp_port, timeout=self.timeout)
        ftp.login()
        ftp.sendcmd("TYPE I")
        return ftp

    def fetch(self, console, job_dir, dest):
        """Copy <scratch>/<job_dir> to `dest`. Returns the relative file list."""
        ftp = self._connect(console)
        files = []
        try:
            def walk(remote, local, rel):
                os.makedirs(local, exist_ok=True)
                for is_dir, name in _entries(ftp, remote):
                    if is_dir:
                        walk(f"{remote}/{name}", os.path.join(local, name), f"{rel}{name}/")
                    else:
                        with open(os.path.join(local, name), "wb") as fh:
                            ftp.retrbinary(f"RETR {remote}/{name}", fh.write)
                        files.append(f"{rel}{name}")
            walk(f"{SCRATCH}/{job_dir}", dest, "")
        finally:
            _quit(ftp)
        return files

    def cleanup(self, console, job_dir):
        """Remove a job folder (best effort) so a retry starts clean."""
        try:
            ftp = self._connect(console)
        except OSError:
            return
        try:
            def rm(remote):
                for is_dir, name in _entries(ftp, remote):
                    if is_dir:
                        rm(f"{remote}/{name}")
                    else:
                        _tolerate(ftp.delete, f"{remote}/{name}")
                _tolerate(ftp.rmd, remote)
            try:
                rm(f"{SCRATCH}/{job_dir}")
            except error_perm:
                pass  # folder did not exist
        finally:
            _quit(ftp)


def _tolerate(fn, *a):
    """ftpsrv answers 226 to delete/rmdir, which ftplib treats as an error."""
    try:
        fn(*a)
    except error_reply as e:
        if not str(e).startswith(("226", "250")):
            raise


def _quit(ftp):
    try:
        ftp.quit()
    except Exception:
        ftp.close()
