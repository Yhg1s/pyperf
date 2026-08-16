#!/usr/bin/env python3
"""Same as loops_table_bench, but the script sets its own default loops.

Runner(loops=N) is a default rather than a request, so a loops table has to
override it and it must not count as --loops being given on the command line.
"""
import pyperf

runner = pyperf.Runner(loops=512)


def fast(loops):
    return loops * 1e-9


def slow(loops):
    return loops * 1e-3


runner.bench_time_func('fast_bench', fast)
runner.bench_time_func('slow_bench', slow)
