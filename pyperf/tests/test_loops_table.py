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
# Real scripts rather than strings written to a temp directory: they can be
# run by hand when a test fails, and the linter checks them.
BENCH = os.path.join(TESTDIR, 'loops_table_bench.py')
SINGLE_BENCH = os.path.join(TESTDIR, 'loops_table_single_bench.py')
DEFAULT_LOOPS_BENCH = os.path.join(
    TESTDIR, 'loops_table_default_loops_bench.py')
FAILING_BENCH = os.path.join(TESTDIR, 'loops_table_failing_bench.py')
NOISY_BENCH = os.path.join(TESTDIR, 'loops_table_noisy_bench.py')


def write_table(directory, loops, min_time=0.1,
                version=loops_table.TABLE_VERSION, hostname=None):
    filename = os.path.join(directory, 'loops.json')
    data = {
        'table_version': version,
        'min_time': min_time,
        # This machine by default: a table from elsewhere warns, which is its
        # own test rather than noise in every other one.
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

    def test_rejects_an_unknown_version(self):
        with tests.temporary_directory() as tmpdir:
            filename = write_table(tmpdir, {'bench': 8}, version=999)
            with self.assertRaises(ValueError) as cm:
                loops_table.LoopsTable.load(filename)
            self.assertIn('version', str(cm.exception))

    def test_known_benchmark(self):
        self.assertEqual(self.make_table({'bench': 1024}).loops_for('bench'),
                         1024)

    def test_unknown_benchmark_falls_back_to_calibration(self):
        # None means "calibrate as usual", so a benchmark the table has never
        # seen keeps working.
        self.assertIsNone(self.make_table({'bench': 1024}).loops_for('other'))

    def test_a_zero_count_is_rejected_not_ignored(self):
        # It used to be read as "calibrate this one". Silently ignoring a
        # count someone wrote is worse than saying it cannot be run.
        with self.assertRaises(ValueError):
            self.make_table({'bench': 0})

    def test_records_the_machine_it_came_from(self):
        data = loops_table.build_table({'a': 8}, 0.1)
        self.assertEqual(data['table_version'], loops_table.TABLE_VERSION)
        self.assertEqual(data['min_time'], 0.1)
        self.assertEqual(data['loops'], {'a': 8})
        for key in ('hostname', 'platform', 'python', 'date'):
            self.assertTrue(data['machine'][key], key)

    def test_round_trip_through_a_file(self):
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
    at load with a message naming the problem -- not inside a worker.
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

    def test_a_valid_table_loads(self):
        self.assertEqual(self.load(self.good()).loops_for('bench'), 128)

    def test_rejects_a_json_array(self):
        # Previously an AttributeError escaped the load site's handler.
        self.assert_rejected([1, 2, 3], 'must be a JSON object')

    def test_rejects_a_non_object_loops(self):
        self.assert_rejected(self.good(loops=[1, 2]), 'loops must be')

    def test_rejects_a_non_object_machine(self):
        self.assert_rejected(self.good(machine='here'), 'machine must be')

    def test_rejects_a_null_loops(self):
        # A falsy wrong type has to be caught by the same check a truthy one
        # is: `data.get('loops') or {}` would quietly turn all of these into
        # an empty table, and an empty table calibrates every benchmark --
        # the silent fallback the whole feature exists to avoid.
        self.assert_rejected(self.good(loops=None), 'loops must be')

    def test_rejects_an_empty_list_loops(self):
        self.assert_rejected(self.good(loops=[]), 'loops must be')

    def test_rejects_a_zero_loops(self):
        self.assert_rejected(self.good(loops=0), 'loops must be')

    def test_rejects_a_null_machine(self):
        self.assert_rejected(self.good(machine=None), 'machine must be')

    def test_rejects_an_empty_list_machine(self):
        self.assert_rejected(self.good(machine=[]), 'machine must be')

    def test_an_absent_loops_or_machine_is_still_fine(self):
        # Absent is not the same as present-and-wrong: a table need not carry
        # either key.
        data = self.good()
        del data['loops']
        del data['machine']
        table = loops_table.LoopsTable(data)
        self.assertEqual(table.loops, {})
        self.assertEqual(table.machine, {})

    def test_rejects_a_string_loop_count(self):
        self.assert_rejected(self.good(loops={'bench': 'oops'}),
                             'must be an integer')

    def test_rejects_a_float_loop_count(self):
        self.assert_rejected(self.good(loops={'bench': 12.5}),
                             'must be an integer')

    def test_rejects_a_boolean_loop_count(self):
        # bool is an int in Python, and True would quietly mean one loop.
        self.assert_rejected(self.good(loops={'bench': True}),
                             'must be an integer')

    def test_rejects_a_negative_loop_count(self):
        self.assert_rejected(self.good(loops={'bench': -5}), 'must be >= 1')

    def test_rejects_a_zero_loop_count(self):
        self.assert_rejected(self.good(loops={'bench': 0}), 'must be >= 1')

    def test_rejects_a_bad_min_time(self):
        self.assert_rejected(self.good(min_time=0), 'positive number')
        self.assert_rejected(self.good(min_time='soon'), 'positive number')

    def test_rejects_invalid_json(self):
        with tests.temporary_directory() as tmpdir:
            filename = os.path.join(tmpdir, 'loops.json')
            with open(filename, 'w', encoding='utf-8') as fp:
                fp.write('{not json')
            # JSONDecodeError, not a ValueError rebuilt from it: the
            # original says where in the file the problem is.
            with self.assertRaises(json.JSONDecodeError) as cm:
                loops_table.LoopsTable.load(filename)
            self.assertTrue(cm.exception.lineno)


class TestMinTimeAndMachine(unittest.TestCase):
    def make(self, min_time=0.1, hostname='testhost'):
        return loops_table.LoopsTable({
            'table_version': loops_table.TABLE_VERSION,
            'min_time': min_time,
            'machine': {'hostname': hostname},
            'loops': {'bench': 128},
        })

    def test_matching_min_time_is_no_problem(self):
        self.assertEqual(self.make(0.1).check_matches(0.1), [])

    def test_a_different_min_time_is_a_problem(self):
        # The counts answer "how many iterations reach min_time"; asking a
        # different question silently gets chunks of the wrong length.
        problems = self.make(0.4).check_matches(0.1)
        self.assertEqual(len(problems), 1)
        self.assertIn('--min-time', problems[0])

    def test_another_machine_is_reported(self):
        self.assertEqual(self.make(hostname='somewhere-else').other_machine(),
                         'somewhere-else')

    def test_this_machine_is_not_reported(self):
        import socket
        self.assertIsNone(self.make(hostname=socket.gethostname())
                          .other_machine())

    def test_no_recorded_hostname_is_not_reported(self):
        self.assertIsNone(self.make(hostname='').other_machine())

    def test_content_id_follows_the_counts(self):
        # The id must identify the counts, since the filename does not:
        # "loops.json" is used by both this tool and bench_runner.
        a = self.make()
        b = self.make(min_time=0.1, hostname='different')
        self.assertEqual(a.content_id(), b.content_id())
        other = loops_table.LoopsTable({
            'table_version': loops_table.TABLE_VERSION,
            'min_time': 0.1, 'machine': {}, 'loops': {'bench': 256},
        })
        self.assertNotEqual(a.content_id(), other.content_id())


class TestGeneration(unittest.TestCase):
    """
    `pyperf loops_table` calibrates and writes; that is the whole workflow.
    """

    def run_command(self, *args):
        proc = subprocess.run(
            [sys.executable, '-m', 'pyperf', 'loops_table', *args],
            capture_output=True, text=True)
        return proc

    def test_calibrates_every_benchmark_in_a_script(self):
        with tests.temporary_directory() as tmpdir:
            out = os.path.join(tmpdir, 'loops.json')
            proc = self.run_command('-o', out, BENCH)
            self.assertEqual(proc.returncode, 0, proc.stderr)

            table = loops_table.LoopsTable.load(out)
            self.assertEqual(set(table.loops), {'fast_bench', 'slow_bench'})
            # 1e-3 per loop needs 128 loops to reach 100 ms.
            self.assertEqual(table.loops_for('slow_bench'), 128)

    def test_min_time_is_honoured_and_recorded(self):
        with tests.temporary_directory() as tmpdir:
            out = os.path.join(tmpdir, 'loops.json')
            proc = self.run_command('-o', out, '--min-time', '0.4',
                                    SINGLE_BENCH)
            self.assertEqual(proc.returncode, 0, proc.stderr)

            table = loops_table.LoopsTable.load(out)
            self.assertEqual(table.min_time, 0.4)
            # Four times the work per chunk, so four times the loops.
            self.assertEqual(table.loops_for('slow_bench'), 512)

    def test_append_builds_a_table_up_a_script_at_a_time(self):
        with tests.temporary_directory() as tmpdir:
            out = os.path.join(tmpdir, 'loops.json')

            self.assertEqual(
                self.run_command('-o', out, SINGLE_BENCH).returncode, 0)
            self.assertEqual(
                self.run_command('-o', out, '--append', BENCH).returncode, 0)

            table = loops_table.LoopsTable.load(out)
            self.assertEqual(set(table.loops),
                             {'slow_bench', 'fast_bench'})

    def test_append_refuses_a_different_min_time(self):
        # Counts calibrated against different targets are not comparable, and
        # mixing them in one file would be silently wrong.
        with tests.temporary_directory() as tmpdir:
            out = os.path.join(tmpdir, 'loops.json')
            self.assertEqual(
                self.run_command('-o', out, SINGLE_BENCH).returncode, 0)

            proc = self.run_command('-o', out, '--append',
                                    '--min-time', '0.4', SINGLE_BENCH)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn('not comparable', proc.stderr)

    def test_a_failing_script_reports_why(self):
        with tests.temporary_directory() as tmpdir:
            out = os.path.join(tmpdir, 'loops.json')
            proc = self.run_command('-o', out, FAILING_BENCH)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn('boom', proc.stdout + proc.stderr)

    def test_print_loops_writes_only_marked_lines(self):
        proc = subprocess.run(
            [sys.executable, BENCH, '--processes', '1', '--values', '1',
             '--min-time', '0.1', '--print-loops'],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, '')
        for line in proc.stdout.splitlines():
            parts = line.split('\t')
            self.assertEqual(len(parts), 3, line)
            self.assertEqual(parts[0], loops_table.LOOPS_MARKER)

    def test_print_loops_is_incompatible_with_writing_results(self):
        with tests.temporary_directory() as tmpdir:
            out = os.path.join(tmpdir, 'out.json')
            proc = subprocess.run(
                [sys.executable, BENCH, '--print-loops', '-o', out],
                capture_output=True, text=True)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn('incompatible with --output',
                          proc.stdout + proc.stderr)

    def test_a_script_printing_its_own_output_is_not_mistaken_for_counts(self):
        # The script prints "evil_bench\t999" at import time, which is shaped
        # exactly like a count line. Only the marked lines are read.
        loops = loops_table.calibrate(NOISY_BENCH, [], 0.1)
        self.assertEqual(loops, {'slow_bench': 128})

    def test_no_temporary_file_is_left_behind(self):
        before = set(glob.glob(os.path.join(tempfile.gettempdir(),
                                            'pyperf_loops_*')))
        loops_table.calibrate(SINGLE_BENCH, [], 0.1)
        after = set(glob.glob(os.path.join(tempfile.gettempdir(),
                                           'pyperf_loops_*')))
        self.assertEqual(before, after)

    def test_a_missing_script_is_an_error(self):
        with tests.temporary_directory() as tmpdir:
            out = os.path.join(tmpdir, 'loops.json')
            proc = self.run_command('-o', out,
                                    os.path.join(tmpdir, 'nope.py'))
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn('no such benchmark script', proc.stderr)


class TestUsingATable(unittest.TestCase):
    def run_script(self, tmpdir, *args, script=BENCH):
        output = os.path.join(tmpdir, 'out.json')
        if os.path.exists(output):
            os.unlink(output)
        cmd = [sys.executable, script, '-p', '1', '-n', '1', '-w', '0',
               '--quiet', '-o', output, *args]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return pyperf.BenchmarkSuite.load(output), proc.stderr

    def loops_of(self, suite):
        return {b.get_name(): b.get_metadata().get('loops') for b in suite}

    def test_each_benchmark_gets_its_own_count(self):
        # --loops is one value for a whole process; a table is per benchmark,
        # which is the only way to serve a script reporting several.
        with tests.temporary_directory() as tmpdir:
            table = write_table(tmpdir,
                                {'fast_bench': 2 ** 27, 'slow_bench': 128})
            suite, _ = self.run_script(tmpdir, '--loops-table', table)
            self.assertEqual(self.loops_of(suite),
                             {'fast_bench': 2 ** 27, 'slow_bench': 128})

    def test_no_calibration_runs_happen(self):
        with tests.temporary_directory() as tmpdir:
            table = write_table(tmpdir,
                                {'fast_bench': 2 ** 27, 'slow_bench': 128})
            suite, _ = self.run_script(tmpdir, '--loops-table', table)
            for bench in suite:
                for run in bench.get_runs():
                    self.assertNotIn('calibrate_loops', run.get_metadata())

    def test_the_same_table_gives_the_same_counts_every_run(self):
        with tests.temporary_directory() as tmpdir:
            table = write_table(tmpdir,
                                {'fast_bench': 2 ** 27, 'slow_bench': 128})
            first, _ = self.run_script(tmpdir, '--loops-table', table)
            second, _ = self.run_script(tmpdir, '--loops-table', table)
            self.assertEqual(self.loops_of(first), self.loops_of(second))

    def test_a_benchmark_missing_from_the_table_still_calibrates(self):
        with tests.temporary_directory() as tmpdir:
            table = write_table(tmpdir, {'fast_bench': 2 ** 27})
            suite, _ = self.run_script(tmpdir, '--loops-table', table)
            loops = self.loops_of(suite)
            self.assertEqual(loops['fast_bench'], 2 ** 27)
            self.assertEqual(loops['slow_bench'], 128)

    def test_results_say_they_used_a_table(self):
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

    def test_a_benchmarks_own_default_loops_does_not_suppress_the_table(self):
        # Runner(loops=N) sets the argparser DEFAULT. That is not the user
        # asking for a count, and must not be mistaken for one:
        # pyperformance's bm_btree does this.
        # The script sets Runner(loops=512); the table's 256 must still win.
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
    """
    A table that does not describe the run being asked for must say so.
    """

    def run_script(self, tmpdir, *args, script=BENCH, expect_failure=False):
        cmd = [sys.executable, script, '-p', '1', '-n', '1', '-w', '0',
               '--quiet', *args]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if expect_failure:
            self.assertNotEqual(proc.returncode, 0, proc.stdout)
        else:
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc

    def test_a_table_for_a_different_min_time_is_refused(self):
        # It used to be accepted, and every chunk came out the wrong length
        # with nothing said. Verified at 7x and at 18% of the target.
        with tests.temporary_directory() as tmpdir:
            table = write_table(tmpdir, {'slow_bench': 128}, min_time=0.4)
            proc = self.run_script(tmpdir, '--loops-table', table,
                                   expect_failure=True)
            output = proc.stdout + proc.stderr
            self.assertIn('--min-time', output)
            self.assertIn('0.4', output)

    def test_a_matching_min_time_is_accepted(self):
        with tests.temporary_directory() as tmpdir:
            table = write_table(tmpdir, {'slow_bench': 128}, min_time=0.4)
            self.run_script(tmpdir, '--loops-table', table,
                            '--min-time', '0.4')

    def test_a_table_from_another_machine_warns_but_runs(self):
        with tests.temporary_directory() as tmpdir:
            table = write_table(tmpdir, {'slow_bench': 128},
                                hostname='somewhere-else')
            proc = self.run_script(tmpdir, '--loops-table', table)
            self.assertIn('somewhere-else', proc.stderr)
            self.assertIn('generated on', proc.stderr)

    def test_a_table_from_this_machine_is_quiet(self):
        with tests.temporary_directory() as tmpdir:
            table = write_table(tmpdir, {'slow_bench': 128})
            self.assertEqual(self.run_script(tmpdir, '--loops-table',
                                             table).stderr, '')

    def test_a_malformed_table_is_refused(self):
        # Before any benchmark runs, and saying which value is wrong.
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

    def test_debug_single_value_does_not_conflict_with_a_table(self):
        # --debug-single-value rewrites args.loops internally; that is not the
        # user asking for --loops, and used to be mistaken for it.
        with tests.temporary_directory() as tmpdir:
            table = write_table(tmpdir, {'slow_bench': 128})
            proc = self.run_script(tmpdir, '--loops-table', table,
                                   '--debug-single-value',
                                   script=SINGLE_BENCH)
            self.assertNotIn('incompatible', proc.stdout + proc.stderr)

    def test_results_record_which_table_by_content(self):
        with tests.temporary_directory() as tmpdir:
            table = write_table(tmpdir, {'slow_bench': 128})
            output = os.path.join(tmpdir, 'out.json')
            self.run_script(tmpdir, '--loops-table', table, '-o', output)
            suite = pyperf.BenchmarkSuite.load(output)
            bench = {b.get_name(): b for b in suite}['slow_bench']
            metadata = bench.get_metadata()
            self.assertEqual(metadata['loops_table'], 'loops.json')
            # The name alone does not identify a table.
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

    def test_warns_when_one_loops_covers_several_benchmarks(self):
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

    def test_silent_for_a_single_benchmark(self):
        with tests.temporary_directory() as tmpdir:
            self.assertEqual(
                self.run_script(tmpdir, SINGLE_BENCH, '--loops', '512'), '')

    def test_silent_with_a_loops_table(self):
        with tests.temporary_directory() as tmpdir:
            table = write_table(tmpdir,
                                {'fast_bench': 2 ** 27, 'slow_bench': 128})
            self.assertEqual(
                self.run_script(tmpdir, BENCH, '--loops-table', table), '')

    def test_silent_when_the_script_set_the_default_itself(self):
        # Runner(loops=512) is a default, not --loops on the command line.
        with tests.temporary_directory() as tmpdir:
            self.assertEqual(
                self.run_script(tmpdir, DEFAULT_LOOPS_BENCH), '')


if __name__ == "__main__":
    unittest.main()
