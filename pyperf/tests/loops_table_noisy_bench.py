#!/usr/bin/env python3
"""A script that prints to stdout before pyperf does.

Whatever a script prints at import time is repeated by every process spawned
for calibration, so --print-loops output has to be distinguishable from it.
The third line below is shaped exactly like a real one.
"""
print("hello from import")
print("fake_bench\tnotanumber")
print("evil_bench\t999")

import pyperf  # noqa: E402

runner = pyperf.Runner()


def slow(loops):
    return loops * 1e-3


runner.bench_time_func('slow_bench', slow)
