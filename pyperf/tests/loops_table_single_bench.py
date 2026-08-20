#!/usr/bin/env python3
import pyperf

runner = pyperf.Runner()


def slow(loops):
    return loops * 1e-3


runner.bench_time_func('slow_bench', slow)
