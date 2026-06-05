"""Pure rendering of remote machine + transfer stats for ``jp live --stats``.

Everything here is string-in / string-out (no I/O), so the dashboard is fully
unit-testable. The machine dict is produced read-only by the agent's
``statmachine`` op; this module never opens a file or runs a process.
"""

from __future__ import annotations

from dataclasses import dataclass

_GIB = 1024**3


@dataclass
class TransferStats:
    bytes_read: int
    rpcs: int
    cache_hit_rate: float  # 0.0 - 1.0
    avg_rtt_ms: float


def _gib(n: float) -> str:
    return f"{n / _GIB:.1f} GiB"


def render_machine(machine: dict) -> str:
    """Compact multi-line dashboard. Sections absent from ``machine`` are omitted."""
    lines: list[str] = ["Remote machine"]

    cpu = machine.get("cpu_count")
    if cpu is not None:
        lines.append(f"  CPU   {cpu} cores")

    total_kb = machine.get("mem_total_kb")
    avail_kb = machine.get("mem_available_kb")
    if total_kb:
        if avail_kb is not None:
            used_kb = total_kb - avail_kb
            pct = (used_kb / total_kb * 100) if total_kb else 0.0
            lines.append(f"  RAM   {_gib(used_kb * 1024)} / {_gib(total_kb * 1024)} ({pct:.0f}%)")
        else:
            lines.append(f"  RAM   {_gib(total_kb * 1024)} total")

    disk_total = machine.get("disk_total_bytes")
    disk_free = machine.get("disk_free_bytes")
    if disk_total:
        if disk_free is not None:
            lines.append(f"  Disk  {_gib(disk_free)} free / {_gib(disk_total)}")
        else:
            lines.append(f"  Disk  {_gib(disk_total)} total")

    for i, gpu in enumerate(machine.get("gpus") or []):
        name = gpu.get("name", "GPU")
        util = gpu.get("util_pct")
        used = gpu.get("mem_used_mb")
        total = gpu.get("mem_total_mb")
        util_s = f"{util}%" if util is not None else "?%"
        mem_s = f"{used}/{total} MiB" if used is not None and total is not None else "? MiB"
        lines.append(f"  GPU{i} {name}  {util_s}  {mem_s}")

    return "\n".join(lines)


def render_transfer(t: TransferStats) -> str:
    """One/two-line transfer summary."""
    return (
        f"Transfer  cache {t.cache_hit_rate * 100:.0f}% hit  "
        f"{t.bytes_read} bytes  {t.rpcs} rpcs  {t.avg_rtt_ms:.1f} ms avg RTT"
    )
