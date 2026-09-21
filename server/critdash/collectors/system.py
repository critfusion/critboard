"""system collector: load average, memory, cpu_count, disk usage.

Cross-platform (Linux + macOS), verified on Linux only -- see the per-branch
notes below.

Load average: os.getloadavg() is POSIX and kernel-backed on both platforms
(it reads /proc/loadavg on Linux, calls getloadavg(3) on macOS via libc) --
no platform branch needed at all.

Memory has no portable stdlib API, so it IS platform-branched:
  - Linux: /proc/meminfo's MemTotal/MemAvailable, unchanged from before this
    fix -- verified live on this host.
  - macOS: `sysctl hw.memsize` for total (bytes), `vm_stat` for used --
    computed the same way Activity Monitor does it (active + wired +
    compressed pages) * page size. UNVERIFIED on real macOS (this host is
    Linux-only) -- the parsing functions (parse_sysctl_memsize/parse_vm_stat/
    macos_mem_used_gb) are pure and unit-tested against captured-format
    fixtures matching Apple's documented vm_stat/sysctl output shape, and
    `read_meminfo` takes an injectable `run_command` so the platform branch
    itself is exercised in tests without running on macOS. If vm_stat's
    output doesn't parse (unexpected field names, different macOS version),
    used_gb comes back None rather than a guessed number -- total_gb (from
    sysctl, a much simpler format) still reports on its own.
  - any other platform: mem_used_gb/mem_total_gb are both None.

Disk mounts: no more hardcoded "/srv" -- defaults to "/" plus whatever the
caller configures (main.py wires this to sources.json's "disk_mounts"). A
configured mount that doesn't exist on this host is skipped, not a failure
(see _disk()).

On a platform this collector has no support for at all (not linux, not
darwin), collect() raises a CollectorIssue (dependency_missing, optional)
instead of a raw exception reaching the browser -- the panel shows "not
configured" like any other optional collector, rather than a scary
traceback-shaped error every cycle.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

from . import BaseCollector, CollectorIssue

GB = 1024 ** 3

SUPPORTED_PLATFORMS = ("linux", "darwin")

_SYSCTL_MEMSIZE_RE = re.compile(r"(\d+)")
_VM_STAT_PAGE_SIZE_RE = re.compile(r"page size of (\d+) bytes")
# vm_stat line shape: `Pages active:                            234567.` or
# `"Translation faults":                 123456789.` -- a label (optionally
# double-quoted), a colon, whitespace, a possibly-comma-grouped integer, and
# a trailing period.
_VM_STAT_LINE_RE = re.compile(r'^"?([^":]+)"?:\s*([\d,]+)\.?\s*$')


def _read_meminfo_linux() -> tuple[float, float]:
    total_kb = 0
    avail_kb = 0
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemTotal:"):
                total_kb = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                avail_kb = int(line.split()[1])
    total_gb = total_kb / 1024 / 1024
    used_gb = (total_kb - avail_kb) / 1024 / 1024
    return used_gb, total_gb


def parse_sysctl_memsize(output: str) -> int | None:
    """Parse `sysctl hw.memsize` output (e.g. "hw.memsize: 17179869184\\n" or
    "hw.memsize = 17179869184\\n") -- total physical memory in bytes. None if
    no integer is found at all."""
    m = _SYSCTL_MEMSIZE_RE.search(output)
    return int(m.group(1)) if m else None


def parse_vm_stat(output: str) -> tuple[dict[str, int], int]:
    """Parse `vm_stat` output into ({lowercased field name -> page count},
    page_size_bytes). Page size defaults to 4096 (the universal default on
    every Intel/Apple Silicon Mac to date) if the header line isn't in the
    expected "page size of N bytes" shape."""
    page_size_m = _VM_STAT_PAGE_SIZE_RE.search(output)
    page_size = int(page_size_m.group(1)) if page_size_m else 4096
    pages: dict[str, int] = {}
    for raw_line in output.splitlines():
        m = _VM_STAT_LINE_RE.match(raw_line.strip())
        if not m:
            continue
        key = m.group(1).strip().lower()
        try:
            pages[key] = int(m.group(2).replace(",", ""))
        except ValueError:
            continue
    return pages, page_size


def macos_mem_used_gb(vm_stat_output: str) -> float | None:
    """Used memory the way Activity Monitor reports it: (active + wired +
    compressed) pages * page size. None if vm_stat's output is missing the
    fields this needs -- report total-only rather than guess (module
    docstring)."""
    pages, page_size = parse_vm_stat(vm_stat_output)
    try:
        used_pages = (
            pages["pages active"] + pages["pages wired down"]
            + pages.get("pages occupied by compressor", 0)
        )
    except KeyError:
        return None
    return used_pages * page_size / GB


def _run_command(args: list[str], timeout: float = 5.0) -> str | None:
    """Real command runner used at runtime. Tests never call this directly
    -- they inject a fake in its place via read_meminfo's `run_command`
    param, so macOS's branch is exercised with captured-format fixtures on
    any host, Linux included."""
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def read_meminfo(
    platform_name: str, run_command=_run_command
) -> tuple[float | None, float | None]:
    """(used_gb, total_gb) for `platform_name` ("linux"/"darwin"/anything
    else). `platform_name` and `run_command` are both injectable so the
    macOS branch is unit-testable on a Linux host: pass platform_name=
    "darwin" and a fake run_command returning canned sysctl/vm_stat text."""
    if platform_name == "darwin":
        sysctl_out = run_command(["sysctl", "hw.memsize"])
        total_bytes = parse_sysctl_memsize(sysctl_out) if sysctl_out else None
        total_gb = total_bytes / GB if total_bytes is not None else None
        vm_stat_out = run_command(["vm_stat"])
        used_gb = macos_mem_used_gb(vm_stat_out) if vm_stat_out else None
        return used_gb, total_gb
    if platform_name == "linux":
        return _read_meminfo_linux()
    return None, None


def _disk(mount: str) -> dict | None:
    if not os.path.exists(mount):
        return None
    usage = shutil.disk_usage(mount)
    used_gb = usage.used / GB
    total_gb = usage.total / GB
    pct = round((usage.used / usage.total) * 100, 1) if usage.total else 0.0
    return {
        "mount": mount,
        "used_gb": round(used_gb, 1),
        "total_gb": round(total_gb, 1),
        "pct": pct,
    }


class SystemCollector(BaseCollector):
    name = "system"
    interval_s = 15.0

    def __init__(
        self, ctx=None, disk_mounts: list[str] | None = None,
        platform_name: str | None = None, run_command=_run_command,
    ):
        super().__init__(ctx)
        self.disk_mounts = disk_mounts or ["/"]
        # sys.platform ("linux", "darwin", "win32", ...) rather than
        # platform.system() -- injectable here (defaults to the real value)
        # so tests can force either branch without running on that OS.
        self.platform_name = platform_name or sys.platform
        self._run_command = run_command

    async def collect(self) -> dict:
        if self.platform_name not in SUPPORTED_PLATFORMS:
            raise CollectorIssue(
                "dependency_missing",
                f"system metrics are not implemented for platform {self.platform_name!r}",
                remedy="Supported platforms: Linux, macOS.",
                optional=True,
            )
        load1, load5, load15 = os.getloadavg()
        mem_used_gb, mem_total_gb = read_meminfo(self.platform_name, self._run_command)
        disks = [d for d in (_disk(m) for m in self.disk_mounts) if d is not None]
        return {
            "system": {
                "load1": load1,
                "load5": load5,
                "load15": load15,
                "cpu_count": os.cpu_count() or 0,
                "mem_used_gb": round(mem_used_gb, 1) if mem_used_gb is not None else None,
                "mem_total_gb": round(mem_total_gb, 1) if mem_total_gb is not None else None,
                "disks": disks,
            }
        }
