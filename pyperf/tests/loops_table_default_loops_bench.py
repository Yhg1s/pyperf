#!/usr/bin/env python3
import pyperf

runner = pyperf.Runner(loops=512)


def fast(loops):
    return loops * 1e-9


def slow(loops):
    return loops * 1e-3


runner.bench_time_func('fast_bench', fast)
runner.bench_time_func('slow_bench', slow)
