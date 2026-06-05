from jp import stats


def test_render_machine_full():
    machine = {
        "cpu_count": 8,
        "mem_total_kb": 32896744,
        "mem_available_kb": 20000000,
        "disk_total_bytes": 1000 * 1024**3,
        "disk_free_bytes": 400 * 1024**3,
        "gpus": [
            {"name": "NVIDIA H100", "mem_used_mb": 1024, "mem_total_mb": 81920, "util_pct": 37},
            {"name": "NVIDIA H100", "mem_used_mb": 0, "mem_total_mb": 81920, "util_pct": 0},
        ],
    }
    out = stats.render_machine(machine)
    assert "GPU0" in out
    assert "NVIDIA H100" in out
    assert "%" in out
    assert "GPU1" in out


def test_render_machine_degrades():
    assert stats.render_machine({}) is not None
    cpu_only = stats.render_machine({"cpu_count": 4})
    assert "GPU" not in cpu_only
    assert "4" in cpu_only


def test_render_transfer():
    t = stats.TransferStats(bytes_read=2048, rpcs=10, cache_hit_rate=0.5, avg_rtt_ms=12.5)
    out = stats.render_transfer(t)
    assert "10" in out
    assert "%" in out
