"""Move job files off a console over ftpsrv (port 2121, anonymous)."""
import os
import re
from ftplib import FTP, error_perm, error_reply

from .job import check_job_id

SCRATCH = "/data/PS5LM/scratch"
MAX_DEPTH = 8
MAX_FILES = 2000

# Unix `ls -l` style: perms, links, owner, group, size, "Mon DD HH:MM" or "Mon DD  YYYY", name.
_UNIX = re.compile(
    r"^([-dlbcpsDL])[rwxXsStTlL-]{9}[+@.]?\s+\d+\s+\S+\s+\S+\s+\d+\s+\w{3}\s+\d{1,2}\s+"
    r"(?:\d{4}|\d{1,2}:\d{2})\s(.+)$")
# DOS style: "01-02-24  10:00AM  <DIR>  name"
_DOS = re.compile(r"^\d\d-\d\d-\d{2,4}\s+\d\d:\d\d(?:[AP]M)?\s+(<DIR>|\d+)\s+(.+)$")


def parse_list_line(line):
    """Parse one LIST line into ('dir'|'file'|'link', name), or None for lines to ignore
    ('total N', blanks, '.' and '..'). Raises ValueError for a line it cannot understand,
    so a surprising server format fails loudly instead of silently dropping files."""
    line = line.rstrip("\r\n")
    if not line.strip() or line.startswith("total "):
        return None
    m = _UNIX.match(line)
    if m:
        kind = {"d": "dir", "l": "link"}.get(m.group(1).lower(), "file")
        name = m.group(2)
        if kind == "link" and " -> " in name:
            name = name.split(" -> ", 1)[0]
    else:
        m = _DOS.match(line)
        if not m:
            raise ValueError(f"unrecognised FTP LIST line: {line!r}")
        kind = "dir" if m.group(1) == "<DIR>" else "file"
        name = m.group(2)
    if name in (".", ".."):
        return None
    return kind, name


def _lines(ftp, path):
    out = []
    ftp.retrlines("LIST " + path, out.append)  # ftpsrv has no NLST
    return out


def _entries(ftp, path):
    res = []
    for line in _lines(ftp, path):
        e = parse_list_line(line)
        if e:
            res.append(e)
    return res


def _safe_name(name):
    """Names come from the console, so treat them as untrusted."""
    if (not name or name in (".", "..") or any(ch in name for ch in "/\\\r\n\x00")):
        raise ValueError(f"refusing unsafe file name from console: {name!r}")
    return name


def _local_name(name):
    # The control connection is latin-1 (lossless); recover UTF-8 names for the local disk.
    try:
        return name.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


def _job_path(job_dir):
    """/data/PS5LM/scratch/jobs/<id>, refusing anything that is not exactly that shape.
    cleanup() deletes this tree recursively, so a bad id must never get this far."""
    prefix, _, ident = job_dir.partition("/")
    if prefix != "jobs":
        raise ValueError(f"bad job folder {job_dir!r}")
    check_job_id(ident)
    return f"{SCRATCH}/jobs/{ident}"


class FtpExporter:
    """Fetch and clean job folders. One short-lived connection per call."""

    def __init__(self, timeout=60):
        self.timeout = timeout

    def _connect(self, console):
        ftp = FTP()
        ftp.encoding = "latin-1"
        ftp.connect(console.host, console.ftp_port, timeout=self.timeout)
        ftp.login()
        ftp.sendcmd("TYPE I")
        return ftp

    def check(self, console):
        """Return None if anonymous FTP login works, else a short error string."""
        try:
            ftp = self._connect(console)
        except Exception as e:
            return str(e) or type(e).__name__
        _quit(ftp)
        return None

    def fetch(self, console, job_dir, dest):
        """Copy <scratch>/<job_dir> to `dest`. Returns the relative file list.
        A job folder that does not exist yields an empty list."""
        root = _job_path(job_dir)
        ftp = self._connect(console)
        files = []
        real_dest = os.path.realpath(dest)
        try:
            def walk(remote, local, rel, depth, top=False):
                if depth > MAX_DEPTH:
                    raise ValueError(f"folder nesting deeper than {MAX_DEPTH} under {job_dir}")
                try:
                    entries = _entries(ftp, remote)
                except error_perm:
                    if top:
                        return  # folder does not exist: the model wrote nothing
                    raise
                os.makedirs(local, exist_ok=True)
                for kind, name in entries:
                    if kind == "link":
                        continue  # never follow links off the job folder
                    _safe_name(name)
                    lpath = os.path.join(local, _local_name(name))
                    if not os.path.realpath(lpath).startswith(real_dest + os.sep):
                        raise ValueError(f"refusing to write outside {dest}: {name!r}")
                    if kind == "dir":
                        walk(f"{remote}/{name}", lpath, f"{rel}{_local_name(name)}/", depth + 1)
                    else:
                        if len(files) >= MAX_FILES:
                            raise ValueError(f"more than {MAX_FILES} files in {job_dir}")
                        with open(lpath, "wb") as fh:
                            ftp.retrbinary(f"RETR {remote}/{name}", fh.write)
                        files.append(f"{rel}{_local_name(name)}")
            walk(root, dest, "", 0, top=True)
        finally:
            _quit(ftp)
        return files

    def cleanup(self, console, job_dir):
        """Remove a job folder so a retry starts clean. A folder that does not exist is fine;
        anything else that goes wrong raises, so stale files can never pass for fresh ones."""
        root = _job_path(job_dir)
        ftp = self._connect(console)
        try:
            def rm(remote, top=False, depth=0):
                if depth > MAX_DEPTH:
                    raise ValueError(f"folder nesting deeper than {MAX_DEPTH} under {job_dir}")
                try:
                    entries = _entries(ftp, remote)
                except error_perm:
                    if top:
                        return  # did not exist
                    raise
                for kind, name in entries:
                    _safe_name(name)
                    if kind == "dir":
                        rm(f"{remote}/{name}", depth=depth + 1)
                    else:  # files and symlinks: DELE removes the link itself, never its target
                        _tolerate(ftp.delete, f"{remote}/{name}")
                try:
                    _tolerate(ftp.rmd, remote)
                except error_perm:
                    if not (top and not entries):
                        raise  # an empty/absent top folder that will not go away is harmless
            rm(root, top=True)
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
