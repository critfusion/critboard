"""system collector: /proc/loadavg, /proc/meminfo, cpu_count, disk usage on / and /srv."""

from __future__ import annotations

import os
import shutil

from . import BaseCollector

GB = 1024 ** 3


def _read_loadavg() -> tuple[float, float, float]:
    with open("/proc/loadavg") as f:
        parts = f.read().split()
    return float(parts[0]), float(parts[1]), float(parts[2])


def _read_meminfo() -> tuple[float, float]:
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

    def __init__(self, ctx=None, disk_mounts: list[str] | None = None):
        super().__init__(ctx)
        self.disk_mounts = disk_mounts or ["/", "/srv"]

    async def collect(self) -> dict:
        load1, load5, load15 = _read_loadavg()
        mem_used_gb, mem_total_gb = _read_meminfo()
        disks = [d for d in (_disk(m) for m in self.disk_mounts) if d is not None]
        return {
            "system": {
                "load1": load1,
                "load5": load5,
                "load15": load15,
                "cpu_count": os.cpu_count() or 0,
                "mem_used_gb": round(mem_used_gb, 1),
                "mem_total_gb": round(mem_total_gb, 1),
                "disks": disks,
            }
        }
