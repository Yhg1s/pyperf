import glob
import json
import os
import os.path
import socket
import subprocess
import sys
import tempfile
import unittest

import pyperf
from pyperf import tests
from pyperf import _loops_table as loops_table


TESTDIR = os.path.dirname(__file__)
BENCH = os.path.join(TESTDIR, 'loops_table_bench.py')
SINGLE_BENCH = os.path.join(TESTDIR, 'loops_table_single_bench.py')
DEFAULT_LOOPS_BENCH = os.path.join(
    TESTDIR, 'loops_table_default_loops_bench.py')
FAILING_BENCH = os.path.join(TESTDIR, 'loops_table_failing_bench.py')
NOISY_BENCH = os.path.join(TESTDIR, 'loops_table_noisy_bench.py')
BYPASS_BENCH = os.path.join(TESTDIR, 'loops_table_bypass_bench.py')


def write_table(directory, loops, min_time=0.1,
                version=loops_table.TABLE_VERSION, hostname=None):
    filename = os.path.join(directory, 'loops.json')
    data = {
        'table_version': version,
        'min_time': min_time,
        'machine': {'hostname': hostname or socket.gethostname(),
                    'platform': 'testplatform'},
        'loops': loops,
    }
    with open(filename, 'w', encoding='utf-8') as fp:
        json.dump(data, fp)
    return filename


class TestLoopsTable(unittest.TestCase):
    def make_table(self, loops, min_time=0.1):
        return loops_table.LoopsTable({
            'table_version': loops_table.TABLE_VERSION,
            'min_time': min_time,
            'machine': {'hostname': 'testhost'},
            'loops': loops,
        })

    def test_unknown_version(self):
        with tests.temporary_directory() as tmpdir:
            filename = write_table(tmpdir, {'bench': 8}, version=999)
            with self.assertRaises(ValueError) as cm:
                loops_table.LoopsTable.load(filename)
            self.assertIn('version', str(cm.exception))

    def test_known_benchmark(self):
        self.assertEqual(self.make_table({'bench': 1024}).loops_for('bench'),
                         1024)

    def test_unknown_benchmark(self):
        self.assertIsNone(self.make_table({'bench': 1024}).loops_for('other'))

    def test_zero_loops(self):
        with self.assertRaises(ValueError):
            self.make_table({'bench': 0})

    def test_record_machine(self):
        data = loops_table.build_table({'a': 8}, 0.1)
        self.assertEqual(data['table_version'], loops_table.TABLE_VERSION)
        self.assertEqual(data['min_time'], 0.1)
        self.assertEqual(data['loops'], {'a': 8})
        for key in ('hostname', 'platform', 'python', 'date'):
            self.assertTrue(data['machine'][key], key)

    def test_round_trip(self):
        with tests.temporary_directory() as tmpdir:
            filename = os.path.join(tmpdir, 'table.json')
            with open(filename, 'w', encoding='utf-8') as fp:
                json.dump(loops_table.build_table({'a': 8}, 0.1), fp)
            table = loops_table.LoopsTable.load(filename)
            self.assertEqual(table.loops_for('a'), 8)
            self.assertIn('hostname', table.machine)


class TestValidation(unittest.TestCase):
    """
    A table is plain JSON that people read and edit, so a bad one must fail
    at load with a message naming the problem, not inside a worker.
    """

    def load(self, data):
        with tests.temporary_directory() as tmpdir:
            filename = os.path.join(tmpdir, 'loops.json')
            with open(filename, 'w', encoding='utf-8') as fp:
                json.dump(data, fp)
            return loops_table.LoopsTable.load(filename)

    def assert_rejected(self, data, expected):
        with self.assertRaises(ValueError) as cm:
            self.load(data)
        self.assertIn(expected, str(cm.exception))

    def good(self, **overrides):
        data = {
            'table_version': loops_table.TABLE_VERSION,
            'min_time': 0.1,
            'machine': {'hostname': 'testhost'},
            'loops': {'bench': 128},
        }
        data.update(overrides)
        return data

    def test_load_valid(self):
        self.assertEqual(self.load(self.good()).loops_for('bench'), 128)

    def test_invalid_table(self):
        self.assert_rejected([1, 2, 3], 'must be a JSON object')
        self.assert_rejected(self.good(loops=[1, 2]), 'loops must be')
        self.assert_rejected(self.good(machine='here'), 'machine must be')
        self.assert_rejected(self.good(loops=None), 'loops must be')
        self.assert_rejected(self.good(loops=[]), 'loops must be')
        self.assert_rejected(self.good(loops=0), 'loops must be')
        self.assert_rejected(self.good(machine=None), 'machine must be')
        self.assert_rejected(self.good(machine=[]), 'machine must be')
        self.assert_rejected(self.good(loops={'bench': 'oops'}),
                             'must be an integer')
        self.assert_rejected(self.good(loops={'bench': 12.5}),
                             'must be an integer')
        self.assert_rejected(self.good(loops={'bench': True}),
                             'must be an integer')
        self.assert_rejected(self.good(loops={'bench': -5}), 'must be >= 1')
        self.assert_rejected(self.good(loops={'bench': 0}), 'must be >= 1')
        self.assert_rejected(self.good(min_time=0), 'positive number')
        self.assert_rejected(self.good(min_time='soon'), 'positive number')

    def test_empty_table(self):
        data = self.good()
        del data['loops']
        del data['machine']
        table = loops_table.LoopsTable(data)
        self.assertEqual(table.loops, {})
        self.assertEqual(table.machine, {})

    def test_rejects_invalid_json(self):
        with tests.temporary_directory() as tmpdir:
            filename = os.path.join(tmpdir, 'loops.json')
            with open(filename, 'w', encoding='utf-8') as fp:
                fp.write('{not json')
            with self.assertRaises(json.JSONDecodeError):
                loops_table.LoopsTable.load(filename)


class TestMinTimeAndMachine(unittest.TestCase):
    def make(self, min_time=0.1, hostname='testhost'):
        return loops_table.LoopsTable({
            'table_version': loops_table.TABLE_VERSION,
            'min_time': min_time,
            'machine': {'hostname': hostname},
            'loops': {'bench': 128},
        })

    def test_matching_min_time(self):
        self.assertEqual(self.make(0.1).check_matches(0.1), [])

    def test_different_min_time(self):
        problems = self.make(0.4).check_matches(0.1)
        self.assertEqual(len(problems), 1)
        self.assertIn('--min-time', problems[0])

    def test_different_machine(self):
        self.assertEqual(self.make(hostname='somewhere-else').other_machine(),
                         'somewhere-else')

    def test_same_machine(self):
        self.assertIsNone(self.make(hostname=socket.gethostname())
                          .other_machine())

    def test_no_recorded_hostname(self):
        self.assertIsNone(self.make(hostname='').other_machine())

    def test_content_id(self):
        a = self.make()
        b = self.make(min_time=0.1, hostname='different')
        self.assertEqual(a.content_id(), b.content_id())
        other = loops_table.LoopsTable({
            'table_version': loops_table.TABLE_VERSION,
            'min_time': 0.1, 'machine': {}, 'loops': {'bench': 256},
        })
        self.assertNotEqual(a.content_id(), other.content_id())


class TestGeneration(unittest.TestCase):
    def run_command(self, *args, check=True):
        proc = subprocess.run(
            [sys.executable, '-m', 'pyperf', 'loops_table', *args],
            capture_output=True, check=check, text=True)
        return proc

    def test_calibrates_every_benchmark_in_a_script(self):
        with tests.temporary_directory() as tmpdir:
            out = os.path.join(tmpdir, 'loops.json')
            self.run_command('-o', out, BENCH)

            table = loops_table.LoopsTable.load(out)
            self.assertEqual(set(table.loops), {'fast_bench', 'slow_bench'})
            self.assertGreater(table.loops_for('fast_bench'),
                               table.loops_for('slow_bench'))
            self.assertGreater(table.loops_for('slow_bench'), 0)

    def test_min_time(self):
        with tests.temporary_directory() as tmpdir:
            out = os.path.join(tmpdir, 'loops.json')
            self.run_command('-o', out, '--min-time', '0.4', SINGLE_BENCH)

            table = loops_table.LoopsTable.load(out)
            self.assertEqual(table.min_time, 0.4)

    def test_append_table(self):
        with tests.temporary_directory() as tmpdir:
            out = os.path.join(tmpdir, 'loops.json')

            self.run_command('-o', out, SINGLE_BENCH)
            table1 = loops_table.LoopsTable.load(out)
            self.assertEqual(set(table1.loops), {'slow_bench'})
            self.run_command('-o', out, '--append', BENCH)

            table2 = loops_table.LoopsTable.load(out)
            self.assertEqual(set(table2.loops),
                             {'slow_bench', 'fast_bench'})

    def test_append_different_min_time(self):
        # Counts calibrated against different targets are not comparable, and
        # mixing them in one file would be silently wrong.
        with tests.temporary_directory() as tmpdir:
            out = os.path.join(tmpdir, 'loops.json')
            self.run_command('-o', out, SINGLE_BENCH)

            proc = self.run_command('-o', out, '--append',
                                    '--min-time', '0.4', SINGLE_BENCH,
                                    check=False)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn('not comparable', proc.stderr)

    def test_failing_benchmark_error(self):
        with tests.temporary_directory() as tmpdir:
            out = os.path.join(tmpdir, 'loops.json')
            proc = self.run_command('-o', out, FAILING_BENCH, check=False)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn('boom', proc.stdout + proc.stderr)

    def test_print_loops(self):
        proc = subprocess.run(
            [sys.executable, BENCH, '--processes', '1', '--values', '1',
             '--min-time', '0.1', '--print-loops'],
            capture_output=True, text=True, check=True)
        self.assertEqual(proc.stderr, '')
        for line in proc.stdout.splitlines():
            parts = line.split('\t')
            self.assertEqual(len(parts), 3, line)
            self.assertEqual(parts[0], loops_table.LOOPS_MARKER)

    def test_print_loops_with_output(self):
        with tests.temporary_directory() as tmpdir:
            out = os.path.join(tmpdir, 'out.json')
            proc = subprocess.run(
                [sys.executable, BENCH, '--print-loops', '-o', out],
                capture_output=True, text=True)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn('incompatible with --output',
                          proc.stdout + proc.stderr)

    def test_noisy_benchmark_loops(self):
        loops = loops_table.calibrate(NOISY_BENCH, [], 0.1)
        self.assertEqual(loops, {'slow_bench': 128})

    def test_missing_benchmark(self):
        with tests.temporary_directory() as tmpdir:
            out = os.path.join(tmpdir, 'loops.json')
            proc = self.run_command('-o', out,
                                    os.path.join(tmpdir, 'nope.py'),
                                    check=False)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn('no such benchmark script', proc.stderr)


class TestUsingATable(unittest.TestCase):
    def run_script(self, tmpdir, *args, script=BENCH):
        output = os.path.join(tmpdir, 'out.json')
        if os.path.exists(output):
            os.unlink(output)
        cmd = [sys.executable, script, '-p', '1', '-n', '1', '-w', '0',
               '--quiet', '-o', output, *args]
        proc = subprocess.run(cmd, capture_output=True, check=True, text=True)
        return pyperf.BenchmarkSuite.load(output), proc.stderr

    def loops_of(self, suite):
        return {b.get_name(): b.get_metadata().get('loops') for b in suite}

    def test_benchmark_function_loops(self):
        with tests.temporary_directory() as tmpdir:
            table = write_table(tmpdir,
                                {'fast_bench': 2 ** 27, 'slow_bench': 128})
            suite, _ = self.run_script(tmpdir, '--loops-table', table)
            self.assertEqual(self.loops_of(suite),
                             {'fast_bench': 2 ** 27, 'slow_bench': 128})

    def test_no_calibration(self):
        with tests.temporary_directory() as tmpdir:
            table = write_table(tmpdir,
                                {'fast_bench': 2 ** 27, 'slow_bench': 128})
            suite, _ = self.run_script(tmpdir, '--loops-table', table)
            for bench in suite:
                for run in bench.get_runs():
                    self.assertNotIn('calibrate_loops', run.get_metadata())

    def test_missing_entry(self):
        with tests.temporary_directory() as tmpdir:
            table = write_table(tmpdir, {'fast_bench': 2 ** 27})
            suite, _ = self.run_script(tmpdir, '--loops-table', table)
            loops = self.loops_of(suite)
            self.assertEqual(loops['fast_bench'], 2 ** 27)
            self.assertIn('slow_bench', loops)

    def test_results_record_table(self):
        with tests.temporary_directory() as tmpdir:
            table = write_table(tmpdir, {'slow_bench': 128})
            suite, _ = self.run_script(tmpdir, '--loops-table', table)
            benches = {b.get_name(): b for b in suite}
            self.assertEqual(
                benches['slow_bench'].get_metadata().get('loops_table'),
                'loops.json')
            # The one that calibrated is not marked.
            self.assertNotIn('loops_table',
                             benches['fast_bench'].get_metadata())

    def test_table_overrides_default_loops(self):
        with tests.temporary_directory() as tmpdir:
            table = write_table(tmpdir, {'slow_bench': 256})
            suite, _ = self.run_script(tmpdir, '--loops-table', table,
                                       script=DEFAULT_LOOPS_BENCH)
            self.assertEqual(self.loops_of(suite)['slow_bench'], 256)

    def test_loops_and_loops_table_together_is_an_error(self):
        with tests.temporary_directory() as tmpdir:
            table = write_table(tmpdir, {'fast_bench': 8})
            proc = subprocess.run(
                [sys.executable, BENCH, '--loops-table', table,
                 '--loops', '16'],
                capture_output=True, text=True)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn('incompatible', proc.stdout + proc.stderr)


class TestRefusalsAndWarnings(unittest.TestCase):
    def run_script(self, tmpdir, *args, script=BENCH, expect_failure=False):
        cmd = [sys.executable, script, '-p', '1', '-n', '1', '-w', '0',
               '--quiet', *args]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if expect_failure:
            self.assertNotEqual(proc.returncode, 0, proc.stdout)
        else:
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc

    def test_regular_run(self):
        with tests.temporary_directory() as tmpdir:
            table = write_table(tmpdir, {'slow_bench': 128})
            self.assertEqual(self.run_script(tmpdir, '--loops-table',
                                             table).stderr, '')

    def test_different_min_time(self):
        with tests.temporary_directory() as tmpdir:
            table = write_table(tmpdir, {'slow_bench': 128}, min_time=0.4)
            proc = self.run_script(tmpdir, '--loops-table', table,
                                   expect_failure=True)
            output = proc.stdout + proc.stderr
            self.assertIn('--min-time', output)
            self.assertIn('0.4', output)

    def test_matching_min_time_is(self):
        with tests.temporary_directory() as tmpdir:
            table = write_table(tmpdir, {'slow_bench': 128}, min_time=0.4)
            self.run_script(tmpdir, '--loops-table', table,
                            '--min-time', '0.4')

    def test_machine_mismatch(self):
        with tests.temporary_directory() as tmpdir:
            table = write_table(tmpdir, {'slow_bench': 128},
                                hostname='somewhere-else')
            proc = self.run_script(tmpdir, '--loops-table', table)
            self.assertIn('somewhere-else', proc.stderr)
            self.assertIn('generated on', proc.stderr)

    def test_malformed_table(self):
        with tests.temporary_directory() as tmpdir:
            table = os.path.join(tmpdir, 'loops.json')
            with open(table, 'w', encoding='utf-8') as fp:
                json.dump({'table_version': loops_table.TABLE_VERSION,
                           'min_time': 0.1, 'machine': {},
                           'loops': {'slow_bench': 'oops'}}, fp)
            proc = self.run_script(tmpdir, '--loops-table', table,
                                   expect_failure=True)
            output = proc.stdout + proc.stderr
            self.assertIn("loop count for 'slow_bench' must be an integer",
                          output)
            self.assertNotIn('slow_bench: ', output)

    def test_debug_single_value(self):
        # --debug-single-value rewrites args.loops internally, but should
        # not be mistaken for --loops=1.
        with tests.temporary_directory() as tmpdir:
            table = write_table(tmpdir, {'slow_bench': 128})
            proc = self.run_script(tmpdir, '--loops-table', table,
                                   '--debug-single-value',
                                   script=SINGLE_BENCH)
            self.assertNotIn('incompatible', proc.stdout + proc.stderr)

    def test_results_record_table_id(self):
        with tests.temporary_directory() as tmpdir:
            table = write_table(tmpdir, {'slow_bench': 128})
            output = os.path.join(tmpdir, 'out.json')
            self.run_script(tmpdir, '--loops-table', table, '-o', output)
            suite = pyperf.BenchmarkSuite.load(output)
            bench = {b.get_name(): b for b in suite}['slow_bench']
            metadata = bench.get_metadata()
            self.assertEqual(metadata['loops_table'], 'loops.json')
            self.assertEqual(metadata['loops_table_id'],
                             loops_table.LoopsTable.load(table).content_id())


class TestSharedLoopsWarning(unittest.TestCase):
    """
    --loops is one value for a whole process; a script can hold several
    benchmarks that each want a different one.
    """

    def run_script(self, tmpdir, script, *args):
        cmd = [sys.executable, script, '-p', '1', '-n', '1', '-w', '0',
               '--quiet', *args]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stderr

    def test_warns_multiple_benchmarks(self):
        with tests.temporary_directory() as tmpdir:
            stderr = self.run_script(tmpdir, BENCH, '--loops', '512')
            self.assertIn('--loops=512 applies to every benchmark', stderr)
            self.assertIn('--loops-table', stderr)

    def test_warns_only_once(self):
        with tests.temporary_directory() as tmpdir:
            stderr = self.run_script(tmpdir, BENCH, '--loops', '512')
            self.assertEqual(stderr.count('applies to every benchmark'), 1)

    def test_silent_when_calibrating(self):
        with tests.temporary_directory() as tmpdir:
            self.assertEqual(self.run_script(tmpdir, BENCH), '')

    def test_silent_for_single_benchmark(self):
        with tests.temporary_directory() as tmpdir:
            self.assertEqual(
                self.run_script(tmpdir, SINGLE_BENCH, '--loops', '512'), '')

    def test_silent_with_table(self):
        with tests.temporary_directory() as tmpdir:
            table = write_table(tmpdir,
                                {'fast_bench': 2 ** 27, 'slow_bench': 128})
            self.assertEqual(
                self.run_script(tmpdir, BENCH, '--loops-table', table), '')

    def test_silent_set_default(self):
        # Runner(loops=512) is a default, not --loops on the command line.
        with tests.temporary_directory() as tmpdir:
            self.assertEqual(
                self.run_script(tmpdir, DEFAULT_LOOPS_BENCH), '')


class TestNoCalibrate(unittest.TestCase):
    """
    --no-calibrate turns the fall back to calibration into an error.
    """

    def run_script(self, tmpdir, *args, script=BENCH):
        output = os.path.join(tmpdir, 'out.json')
        if os.path.exists(output):
            os.unlink(output)
        cmd = [sys.executable, script, '-p', '1', '-n', '1', '-w', '0',
               '--quiet', '-o', output, *args]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        return proc

    def test_needs_a_loop_count_from_somewhere(self):
        with tests.temporary_directory() as tmpdir:
            proc = self.run_script(tmpdir, '--no-calibrate')
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn('--no-calibrate needs a loop count',
                          proc.stdout + proc.stderr)

    def test_explicit_loops_is_enough(self):
        with tests.temporary_directory() as tmpdir:
            proc = self.run_script(tmpdir, '--no-calibrate', '--loops', '128')
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_a_complete_table_is_enough(self):
        with tests.temporary_directory() as tmpdir:
            table = write_table(tmpdir,
                                {'fast_bench': 2 ** 27, 'slow_bench': 128})
            proc = self.run_script(tmpdir, '--no-calibrate',
                                   '--loops-table', table)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            suite = pyperf.BenchmarkSuite.load(
                os.path.join(tmpdir, 'out.json'))
            self.assertEqual(
                {b.get_name(): b.get_metadata().get('loops') for b in suite},
                {'fast_bench': 2 ** 27, 'slow_bench': 128})

    def test_a_benchmark_missing_from_the_table_is_an_error(self):
        # Without --no-calibrate this is the silent fall back that
        # test_a_benchmark_missing_from_the_table_still_calibrates covers.
        with tests.temporary_directory() as tmpdir:
            table = write_table(tmpdir, {'fast_bench': 2 ** 27})
            proc = self.run_script(tmpdir, '--no-calibrate',
                                   '--loops-table', table)
            self.assertNotEqual(proc.returncode, 0)
            out = proc.stdout + proc.stderr
            self.assertIn("'slow_bench' is not in --loops-table", out)

    def test_the_error_names_the_benchmark_not_just_the_table(self):
        with tests.temporary_directory() as tmpdir:
            table = write_table(tmpdir, {'slow_bench': 128})
            proc = self.run_script(tmpdir, '--no-calibrate',
                                   '--loops-table', table)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("'fast_bench'", proc.stdout + proc.stderr)

    def test_without_the_flag_a_missing_entry_still_calibrates(self):
        # The flag is opt-in: the default behaviour is unchanged.
        with tests.temporary_directory() as tmpdir:
            table = write_table(tmpdir, {'fast_bench': 2 ** 27})
            proc = self.run_script(tmpdir, '--loops-table', table)
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_a_run_that_calibrates_nothing_is_unaffected(self):
        # --no-calibrate must not change any recorded number, only refuse.
        with tests.temporary_directory() as tmpdir:
            table = write_table(tmpdir,
                                {'fast_bench': 2 ** 27, 'slow_bench': 128})
            self.run_script(tmpdir, '--loops-table', table)
            without = pyperf.BenchmarkSuite.load(
                os.path.join(tmpdir, 'out.json'))
            self.run_script(tmpdir, '--no-calibrate', '--loops-table', table)
            with_flag = pyperf.BenchmarkSuite.load(
                os.path.join(tmpdir, 'out.json'))

            def loops(suite):
                return {b.get_name(): b.get_metadata().get('loops')
                        for b in suite}
            self.assertEqual(loops(without), loops(with_flag))


class TestNoCalibrateBackstops(unittest.TestCase):
    """
    --no-calibrate is checked again where the decision to calibrate is made.

    The dispatch-time check in _main() and the manager both look the reported
    name up in the same table, so in practice the dispatch check catches
    everything the manager would. These cover the manager and the worker
    refusing on their own account, so that a path which reaches them without
    passing the dispatch check still cannot calibrate.
    """

    def run_script(self, tmpdir, *args, script=BENCH):
        cmd = [sys.executable, script, '-p', '1', '-n', '1', '-w', '0',
               '--quiet', *args]
        return subprocess.run(cmd, capture_output=True, text=True)

    def test_a_name_the_table_spells_differently_is_caught(self):
        # The table names the benchmark 'bench_slow', the script reports
        # 'slow_bench'. The lookup misses, so it would calibrate.
        with tests.temporary_directory() as tmpdir:
            table = write_table(tmpdir, {'fast_bench': 2 ** 27,
                                         'bench_slow': 128})
            proc = self.run_script(tmpdir, '--no-calibrate',
                                   '--loops-table', table)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("'slow_bench' is not in --loops-table",
                          proc.stdout + proc.stderr)

    def test_the_manager_refuses_even_if_dispatch_let_it_through(self):
        with tests.temporary_directory() as tmpdir:
            table = write_table(tmpdir, {'fast_bench': 2 ** 27})
            proc = self.run_script(tmpdir, '--no-calibrate',
                                   '--loops-table', table,
                                   script=BYPASS_BENCH)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("would calibrate its loop count",
                          proc.stdout + proc.stderr)

    def test_the_worker_refuses_when_told_to_calibrate(self):
        # A worker is normally only told to calibrate by a manager that has
        # already checked. Ask one directly.
        cmd = [sys.executable, SINGLE_BENCH, '--worker', '--no-calibrate',
               '--calibrate-loops', '--pipe', '1', '-n', '1', '-w', '1']
        proc = subprocess.run(cmd, capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("was asked to calibrate its loop count",
                      proc.stdout + proc.stderr)

    def test_a_worker_given_a_loop_count_is_unaffected(self):
        cmd = [sys.executable, SINGLE_BENCH, '--worker', '--no-calibrate',
               '--loops', '128', '--pipe', '1', '-n', '1', '-w', '1']
        proc = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
