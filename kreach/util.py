# SPDX-License-Identifier: GPL-2.0-or-later
"""Shared helpers: logging, JSON(L) IO, subprocess running."""

import gzip
import json
import logging
import os
import pickle
import shlex
import subprocess
import sys
import time

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)-12s %(message)s"


def setup_logging(verbose: bool = False, logfile: str | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if logfile:
        os.makedirs(os.path.dirname(os.path.abspath(logfile)), exist_ok=True)
        handlers.append(logging.FileHandler(logfile))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format=LOG_FORMAT,
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


class CommandError(RuntimeError):
    def __init__(self, cmd, returncode, stdout, stderr):
        self.cmd = cmd
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(
            "command failed (rc=%s): %s\n--- stderr ---\n%s"
            % (returncode, cmd, (stderr or "")[-4000:])
        )


def run(
    cmd,
    cwd: str | None = None,
    env: dict | None = None,
    check: bool = True,
    timeout: int | None = None,
    capture: bool = True,
    log: logging.Logger | None = None,
    stdin_data: str | None = None,
):
    """Run a command.  `cmd` is a list (preferred) or a shell string."""
    shell = isinstance(cmd, str)
    pretty = cmd if shell else " ".join(shlex.quote(c) for c in cmd)
    if log:
        log.debug("run: %s (cwd=%s)", pretty, cwd or os.getcwd())
    full_env = None
    if env:
        full_env = dict(os.environ)
        full_env.update({k: str(v) for k, v in env.items()})
    t0 = time.time()
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        env=full_env,
        shell=shell,
        capture_output=capture,
        text=True,
        timeout=timeout,
        input=stdin_data,
    )
    if log:
        log.debug("  -> rc=%s in %.1fs", proc.returncode, time.time() - t0)
    if check and proc.returncode != 0:
        raise CommandError(pretty, proc.returncode, proc.stdout, proc.stderr)
    return proc


def _open(path: str, mode: str, gz: bool | None = None):
    """Open `path`, gzipped when it (or `gz`) says so.

    `gz` is passed explicitly when writing through a temporary file, whose
    name does not carry the destination's .gz suffix.
    """
    if gz is None:
        gz = path.endswith(".gz")
    if gz:
        return gzip.open(path, mode + "t" if "b" not in mode else mode)
    return open(path, mode)


def write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with _open(tmp, "w", gz=path.endswith(".gz")) as fh:
        json.dump(obj, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


def read_json(path: str):
    with _open(path, "r") as fh:
        return json.load(fh)


def write_jsonl(path: str, records) -> int:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    n = 0
    with _open(tmp, "w", gz=path.endswith(".gz")) as fh:
        for rec in records:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
            n += 1
    os.replace(tmp, path)
    return n


def read_jsonl(path: str):
    if not os.path.exists(path):
        return
    with _open(path, "r") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def write_pickle(path: str, obj) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        pickle.dump(obj, fh, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, path)


def read_pickle(path: str):
    with open(path, "rb") as fh:
        return pickle.load(fh)


def human_time(seconds: float) -> str:
    if seconds < 90:
        return "%.1fs" % seconds
    if seconds < 5400:
        return "%.1fm" % (seconds / 60.0)
    return "%.1fh" % (seconds / 3600.0)

def ensure_tool_path() -> str:
    """Put this interpreter's script directory on PATH.

    kreach shells out to the kmax suite's own commands (kextractlinux,
    kclause, kmaxall, klocalizer).  They are installed beside the Python
    that imports kmax, which is not necessarily on the caller's PATH -- for
    instance when kreach is invoked by its absolute path out of a virtualenv.
    """
    import sysconfig

    scripts = sysconfig.get_path("scripts")
    candidates = [scripts, os.path.join(sys.prefix, "bin"),
                  os.path.dirname(os.path.abspath(sys.executable))]
    current = os.environ.get("PATH", "").split(os.pathsep)
    added = []
    for d in candidates:
        if d and os.path.isdir(d) and d not in current:
            current.insert(0, d)
            added.append(d)
    os.environ["PATH"] = os.pathsep.join(current)
    return os.pathsep.join(added)


class WorkDirLocked(RuntimeError):
    pass


class work_lock:
    """Exclusive lock on a work directory for one stage.

    S6 builds into `s6/build/<query id>` and clears each directory before
    using it, so two S6 runs over the same work directory delete each other's
    trees -- which surfaces as `generated/autoconf.h: No such file or
    directory` and gets recorded as a build failure, i.e. as a verdict on a
    finding.  The spec's "parallel across machines" means separate work
    directories; within one, the stage takes a lock.
    """

    def __init__(self, work: str, stage: str, wait: bool = False):
        self.path = os.path.join(work, ".%s.lock" % stage)
        self.stage = stage
        self.wait = wait
        self._fh = None

    def __enter__(self):
        import fcntl

        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self._fh = open(self.path, "w")
        flags = fcntl.LOCK_EX if self.wait else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(self._fh.fileno(), flags)
        except OSError:
            self._fh.close()
            self._fh = None
            raise WorkDirLocked(
                "another %s is already running on this work directory "
                "(%s).  Use a separate -w directory to run them in parallel."
                % (self.stage, self.path))
        self._fh.write("%d\n" % os.getpid())
        self._fh.flush()
        return self

    def __exit__(self, *exc):
        import fcntl

        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None
        return False
