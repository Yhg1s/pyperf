#!/usr/bin/env python3
"""loops_table_bench with the dispatch-time --no-calibrate check disabled.

The manager and the worker check for themselves as well, and this makes those
backstops reachable: without defeating the outer check they would never run,
and a test of them would pass by never executing them.
"""
import pyperf

runner = pyperf.Runner()
runner._check_no_calibrate = lambda name: None


def fast(loops):
    return loops * 1e-9


def slow(loops):
    return loops * 1e-3


runner.bench_time_func('fast_bench', fast)
runner.bench_time_func('slow_bench', slow)
