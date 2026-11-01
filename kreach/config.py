"""Run context: where the kernel is, which arch, and the on-disk layout.

Every stage reads the output of the one before and writes a file, so stages can
be rerun, cached and tested on their own.  `Context` owns the names of those
files and nothing else.

Layout of a work directory (one per kernel commit + arch):

    <work>/
      meta.json                 run metadata (kernel commit, arch, versions)
      s1/ kextract  kclause  vars.json  model.smt2
      s2/ kmax.pickle  presence.json.gz  unmodelled.json
      s3/ build-<cfg>/...  symbols.json.gz
      s4/ candidates.jsonl  guarded.jsonl
      s5/ queries.jsonl  witness/<id>.config
      s6/ build/<id>/  results.jsonl
      s7/ findings.jsonl  findings.txt  summary.json
"""

import os
import platform

from kreach.util import read_json, run, write_json

DEFAULT_ARCH = "x86_64"

# Wide configurations used by S3 to harvest symbol references.
WIDE_CONFIGS = ("allmodconfig", "allyesconfig")


class Context:
    def __init__(self, ksrc: str, work: str, arch: str = DEFAULT_ARCH, jobs: int | None = None):
        self.ksrc = os.path.abspath(ksrc)
        self.work = os.path.abspath(work)
        self.arch = arch
        self.jobs = jobs or max(1, (os.cpu_count() or 2))
        os.makedirs(self.work, exist_ok=True)

    # ---- directories -------------------------------------------------
    def dir(self, *parts: str) -> str:
        p = os.path.join(self.work, *parts)
        os.makedirs(p, exist_ok=True)
        return p

    def path(self, *parts: str) -> str:
        p = os.path.join(self.work, *parts)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        return p

    # ---- S1 ----------------------------------------------------------
    @property
    def s1_dir(self) -> str:
        return self.dir("s1")

    @property
    def kextract_file(self) -> str:
        return self.path("s1", "kextract")

    @property
    def kclause_file(self) -> str:
        return self.path("s1", "kclause")

    @property
    def vars_file(self) -> str:
        return self.path("s1", "vars.json")

    @property
    def model_file(self) -> str:
        return self.path("s1", "model.smt2")

    # ---- S2 ----------------------------------------------------------
    @property
    def kmax_file(self) -> str:
        return self.path("s2", "kmax.pickle")

    @property
    def presence_file(self) -> str:
        return self.path("s2", "presence.json.gz")

    @property
    def unmodelled_file(self) -> str:
        return self.path("s2", "unmodelled.json")

    # ---- S3 ----------------------------------------------------------
    @property
    def symbols_file(self) -> str:
        return self.path("s3", "symbols.json.gz")

    def s3_build_dir(self, name: str) -> str:
        return self.dir("s3", "build-%s" % name)

    # ---- S4 ----------------------------------------------------------
    @property
    def candidates_file(self) -> str:
        return self.path("s4", "candidates.jsonl")

    @property
    def guarded_file(self) -> str:
        return self.path("s4", "guarded.jsonl")

    # ---- S5 ----------------------------------------------------------
    @property
    def queries_file(self) -> str:
        return self.path("s5", "queries.jsonl")

    def witness_file(self, fid: str) -> str:
        return self.path("s5", "witness", "%s.config" % fid)

    @property
    def witness_dir(self) -> str:
        return self.dir("s5", "witness")

    # ---- S6 ----------------------------------------------------------
    @property
    def confirm_file(self) -> str:
        return self.path("s6", "results.jsonl")

    def s6_build_dir(self, fid: str) -> str:
        return self.dir("s6", "build", fid)

    def s6_log_file(self, fid: str) -> str:
        return self.path("s6", "build", fid, "build.log")

    # ---- S7 ----------------------------------------------------------
    @property
    def findings_file(self) -> str:
        return self.path("s7", "findings.jsonl")

    @property
    def report_file(self) -> str:
        return self.path("s7", "findings.txt")

    @property
    def summary_file(self) -> str:
        return self.path("s7", "summary.json")

    # ---- kernel facts ------------------------------------------------
    @property
    def srcarch(self) -> str:
        """SRCARCH for this ARCH (the arch/<dir> name)."""
        return {
            "x86_64": "x86", "i386": "x86",
            "arm64": "arm64", "arm": "arm",
            "sparc64": "sparc", "sparc": "sparc",
            "sh64": "sh",
            "um": "x86", "um32": "x86",
        }.get(self.arch, self.arch)

    def kernel_commit(self) -> str:
        try:
            return run(["git", "rev-parse", "HEAD"], cwd=self.ksrc).stdout.strip()
        except Exception:
            return "unknown"

    def kernel_describe(self) -> str:
        try:
            return run(["git", "describe", "--tags", "--always"], cwd=self.ksrc).stdout.strip()
        except Exception:
            return "unknown"

    def kernel_version(self) -> str:
        try:
            return run(["make", "-s", "kernelversion"], cwd=self.ksrc).stdout.strip()
        except Exception:
            return "unknown"

    # ---- metadata ----------------------------------------------------
    def write_meta(self, extra: dict | None = None) -> dict:
        meta = {
            "arch": self.arch,
            "srcarch": self.srcarch,
            "ksrc": self.ksrc,
            "kernel_commit": self.kernel_commit(),
            "kernel_describe": self.kernel_describe(),
            "kernel_version": self.kernel_version(),
            "host": platform.node(),
            "gcc": self._tool_version(["gcc", "--version"]),
            "ld": self._tool_version(["ld", "--version"]),
            "jobs": self.jobs,
        }
        if extra:
            meta.update(extra)
        existing = {}
        if os.path.exists(self.path("meta.json")):
            try:
                existing = read_json(self.path("meta.json"))
            except Exception:
                existing = {}
        existing.update(meta)
        write_json(self.path("meta.json"), existing)
        return existing

    def read_meta(self) -> dict:
        p = self.path("meta.json")
        return read_json(p) if os.path.exists(p) else {}

    @staticmethod
    def _tool_version(cmd) -> str:
        try:
            return run(cmd, check=False).stdout.splitlines()[0].strip()
        except Exception:
            return "unknown"

    def make_env(self) -> dict:
        """Environment for invoking the kernel's make for this arch."""
        env = {"ARCH": self.arch, "SRCARCH": self.srcarch, "KCONFIG_WARN_UNKNOWN_SYMBOLS": ""}
        host_arch = platform.machine()
        cross = {
            ("arm64", "x86_64"): "aarch64-linux-gnu-",
            ("arm", "x86_64"): "arm-linux-gnueabi-",
            ("x86_64", "aarch64"): "x86_64-linux-gnu-",
        }.get((self.arch, host_arch))
        if cross:
            env["CROSS_COMPILE"] = cross
        return env

    def __repr__(self) -> str:
        return "Context(arch=%s, ksrc=%s, work=%s)" % (self.arch, self.ksrc, self.work)
