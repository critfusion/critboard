"""critdash/collectors/system.py: cross-platform load/mem/disk collection.

Load average and the Linux memory branch are exercised for real (this test
host is Linux). The macOS memory branch is exercised by injecting
platform_name="darwin" and a fake `run_command` returning captured-format
`sysctl hw.memsize` / `vm_stat` text -- this repo has no macOS host to run
against, so that branch is implemented-but-unverified; see the module
docstring.
"""

from __future__ import annotations

import os

import pytest

from critdash.collectors import CollectorIssue
from critdash.collectors.system import (
    SystemCollector,
    macos_mem_used_gb,
    parse_sysctl_memsize,
    parse_vm_stat,
    read_meminfo,
)

# Captured-format fixtures: shape matches Apple's documented `sysctl
# hw.memsize` / `vm_stat` output (16 GiB machine, 4096-byte pages).
_SYSCTL_MEMSIZE_OUTPUT = "hw.memsize: 17179869184\n"
_VM_STAT_OUTPUT = """Mach Virtual Memory Statistics: (page size of 4096 bytes)
Pages free:                             212345.
Pages active:                          1234567.
Pages inactive:                         345678.
Pages speculative:                        5432.
Pages throttled:                             0.
Pages wired down:                       456789.
Pages purgeable:                          1234.
"Translation faults":                123456789.
Pages copy-on-write:                    123456.
Pages zero filled:                    12345678.
Pages reactivated:                        1234.
Pages purged:                             5678.
File-backed pages:                       34567.
Anonymous pages:                        123456.
Pages stored in compressor:              45678.
Pages occupied by compressor:            23456.
Decompressions:                           1234.
Compressions:                             5678.
Pageins:                                123456.
Pageouts:                                  1234.
Swapins:                                       0.
Swapouts:                                      0.
"""


# -- macOS parsing (pure functions, captured-format fixtures) -----------------


def test_parse_sysctl_memsize():
    assert parse_sysctl_memsize(_SYSCTL_MEMSIZE_OUTPUT) == 17179869184


def test_parse_sysctl_memsize_equals_form():
    assert parse_sysctl_memsize("hw.memsize = 17179869184\n") == 17179869184


def test_parse_sysctl_memsize_no_match_is_none():
    assert parse_sysctl_memsize("") is None


def test_parse_vm_stat_page_size_and_fields():
    pages, page_size = parse_vm_stat(_VM_STAT_OUTPUT)
    assert page_size == 4096
    assert pages["pages active"] == 1234567
    assert pages["pages wired down"] == 456789
    assert pages["pages occupied by compressor"] == 23456
    assert pages["translation faults"] == 123456789


def test_parse_vm_stat_missing_header_defaults_page_size_4096():
    _, page_size = parse_vm_stat("Pages free:                             1234.\n")
    assert page_size == 4096


def test_macos_mem_used_gb_matches_active_plus_wired_plus_compressed():
    used_gb = macos_mem_used_gb(_VM_STAT_OUTPUT)
    expected_pages = 1234567 + 456789 + 23456
    assert used_gb == pytest.approx(expected_pages * 4096 / (1024 ** 3))


def test_macos_mem_used_gb_none_when_fields_missing():
    assert macos_mem_used_gb("Pages free: 1234.\n") is None


# -- read_meminfo: platform branch selection, injected command output --------


def test_read_meminfo_darwin_branch_uses_sysctl_and_vm_stat():
    calls = []

    def fake_run(args, timeout=5.0):
        calls.append(args)
        if args[0] == "sysctl":
            return _SYSCTL_MEMSIZE_OUTPUT
        if args[0] == "vm_stat":
            return _VM_STAT_OUTPUT
        raise AssertionError(f"unexpected command: {args}")

    used_gb, total_gb = read_meminfo("darwin", run_command=fake_run)

    assert total_gb == pytest.approx(17179869184 / (1024 ** 3))
    assert used_gb == pytest.approx((1234567 + 456789 + 23456) * 4096 / (1024 ** 3))
    assert calls == [["sysctl", "hw.memsize"], ["vm_stat"]]


def test_read_meminfo_darwin_branch_reports_total_only_when_vm_stat_unparseable():
    def fake_run(args, timeout=5.0):
        if args[0] == "sysctl":
            return _SYSCTL_MEMSIZE_OUTPUT
        return "unexpected vm_stat output shape\n"

    used_gb, total_gb = read_meminfo("darwin", run_command=fake_run)
    assert total_gb == pytest.approx(17179869184 / (1024 ** 3))
    assert used_gb is None


def test_read_meminfo_darwin_branch_both_none_when_commands_fail():
    used_gb, total_gb = read_meminfo("darwin", run_command=lambda args, timeout=5.0: None)
    assert used_gb is None
    assert total_gb is None


def test_read_meminfo_linux_branch_reads_proc_meminfo():
    # Real /proc/meminfo read -- this test host is Linux, so this is verified
    # for real, not injected.
    used_gb, total_gb = read_meminfo("linux")
    assert total_gb > 0
    assert used_gb is not None
    assert 0 <= used_gb <= total_gb


def test_read_meminfo_unsupported_platform_both_none():
    used_gb, total_gb = read_meminfo("win32")
    assert used_gb is None
    assert total_gb is None


# -- SystemCollector.collect() ------------------------------------------------


@pytest.mark.asyncio
async def test_collect_linux_reports_real_loadavg_and_meminfo():
    collector = SystemCollector(disk_mounts=["/"], platform_name="linux")
    result = await collector.collect()
    sys_out = result["system"]
    assert isinstance(sys_out["load1"], float)
    assert sys_out["cpu_count"] == os.cpu_count()
    assert sys_out["mem_total_gb"] > 0
    assert sys_out["mem_used_gb"] is not None
    assert any(d["mount"] == "/" for d in sys_out["disks"])


@pytest.mark.asyncio
async def test_collect_darwin_branch_end_to_end_with_injected_commands():
    def fake_run(args, timeout=5.0):
        if args[0] == "sysctl":
            return _SYSCTL_MEMSIZE_OUTPUT
        if args[0] == "vm_stat":
            return _VM_STAT_OUTPUT
        raise AssertionError(f"unexpected command: {args}")

    collector = SystemCollector(disk_mounts=["/"], platform_name="darwin", run_command=fake_run)
    result = await collector.collect()
    sys_out = result["system"]
    assert sys_out["mem_total_gb"] == pytest.approx(17179869184 / (1024 ** 3), rel=1e-3)
    assert sys_out["mem_used_gb"] is not None
    # os.getloadavg() is used on darwin too -- it's the real POSIX call, not
    # something forced through the injected run_command.
    assert isinstance(sys_out["load1"], float)


@pytest.mark.asyncio
async def test_collect_unsupported_platform_raises_collector_issue():
    collector = SystemCollector(platform_name="win32")
    with pytest.raises(CollectorIssue) as exc_info:
        await collector.collect()
    issue = exc_info.value
    assert issue.reason_code == "dependency_missing"
    assert issue.optional is True
    assert "win32" in issue.detail


# -- disk mounts: default "/" only, configured-missing mount skipped ---------


@pytest.mark.asyncio
async def test_collect_default_disk_mounts_is_root_only_no_srv():
    collector = SystemCollector(platform_name="linux")
    assert collector.disk_mounts == ["/"]
    result = await collector.collect()
    mounts = {d["mount"] for d in result["system"]["disks"]}
    assert mounts == {"/"}


@pytest.mark.asyncio
async def test_collect_skips_configured_mount_that_does_not_exist(tmp_path):
    missing = str(tmp_path / "does-not-exist")
    collector = SystemCollector(disk_mounts=["/", missing], platform_name="linux")
    result = await collector.collect()
    mounts = {d["mount"] for d in result["system"]["disks"]}
    assert mounts == {"/"}
    assert missing not in mounts
