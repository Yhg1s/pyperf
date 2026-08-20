"""
Loop counts, decided once for a machine and reused.

Calibration works out how many iterations of a benchmark take at least
--min-time, and it works it out again on every run. The count is therefore a
measurement, with a measurement's variance: the same benchmark on the same
machine can be timed at 2^16 loops today and 2^17 tomorrow, because the
doubling stops at whichever side of --min-time the noise happened to fall.
Chunks of different lengths amortise per-chunk costs differently and see
different GC and cache behaviour, so the loop count is a hidden variable in
every comparison.

A table takes it out. Generate one on the machine you benchmark on:

    python -m pyperf loops_table -o loops.json my_benchmark.py

and then hand it to every run:

    python my_benchmark.py --loops-table loops.json

Every run then measures the same amount of work per chunk, and the numbers
differ only because the thing being benchmarked differs.

A table belongs to the machine it was generated on: the counts suit that
machine's speed. Moving one elsewhere will not fail, but the chunks will be as
much longer or shorter as the machines differ, so generate a table per machine.
The machine is recorded in the file so you can tell which one it came from.
"""

import hashlib
import json
import os
import os.path
import platform
import socket
import subprocess
import sys
import time


# Prefix on each --print-loops line, so the counts can be told apart from
# whatever else the benchmark script writes to stdout.
LOOPS_MARKER = '#pyperf-loops'


class CalibrationFailed(Exception):
    """A benchmark script did not run, so it has no loop counts to record."""


NUMBER_TYPES = (int, float)

# Bumped when the table layout changes in a way that makes old files unusable.
TABLE_VERSION = 1


class LoopsTable:
    """
    Loop counts by benchmark name, and a note of where they came from.
    """

    def __init__(self, data, filename=None):
        self.filename = filename

        # Validate here rather than trusting the file. A table is written by
        # tooling but is plain JSON that people might read and edit, so
        # let's error early when the file is broken.
        if not isinstance(data, dict):
            raise ValueError("a loops table must be a JSON object, not %s"
                             % type(data).__name__)

        version = data.get('table_version')
        if version != TABLE_VERSION:
            raise ValueError("unsupported loops table version %r "
                             "(this pyperf understands %s)"
                             % (version, TABLE_VERSION))

        min_time = data.get('min_time')
        if min_time is not None:
            if not isinstance(min_time, NUMBER_TYPES) or min_time <= 0:
                raise ValueError("min_time must be a positive number, not %r"
                                 % (min_time,))
        self.min_time = min_time

        machine = data.get('machine', {})
        if not isinstance(machine, dict):
            raise ValueError("machine must be a JSON object, not %s"
                             % type(machine).__name__)
        self.machine = machine

        loops = data.get('loops', {})
        if not isinstance(loops, dict):
            raise ValueError("loops must be a JSON object, not %s"
                             % type(loops).__name__)
        for name, count in sorted(loops.items()):
            # bool is an int, and True would silently mean one loop.
            if isinstance(count, bool) or not isinstance(count, int):
                raise ValueError("loop count for %r must be an integer, "
                                 "not %r" % (name, count))
            if count < 1:
                raise ValueError("loop count for %r must be >= 1, not %s"
                                 % (name, count))
        self.loops = loops

    @classmethod
    def load(cls, filename):
        with open(filename, encoding='utf-8') as fp:
            data = json.load(fp)
        return cls(data, filename=filename)

    def check_matches(self, min_time):
        """
        Complain about a table that does not describe the run being asked for.

        Returns a list of problems. The counts in a table are answers to one
        question -- how many iterations reach min_time on this machine -- so a
        run asking a different question gets chunks that are the wrong length,
        with nothing in the output to say so.
        """
        problems = []
        if self.min_time is not None and min_time is not None:
            if self.min_time != min_time:
                problems.append(
                    "it was generated for --min-time %s but this run uses %s, "
                    "so every chunk would be %.3gx the intended length"
                    % (self.min_time, min_time, self.min_time / min_time))
        return problems

    def other_machine(self):
        """
        The machine the table came from, if it was not this one.
        """
        recorded = self.machine.get('hostname')
        if recorded and recorded != socket.gethostname():
            return recorded
        return None

    def content_id(self):
        """
        Identifies the counts themselves, for recording in a result.
        """
        return hashlib.sha256(
            json.dumps(self.loops, sort_keys=True).encode('utf-8')
        ).hexdigest()[:12]

    def loops_for(self, name):
        """
        The loop count for `name`, or None if the table does not have one.
        """
        return self.loops.get(name)

    def describe_machine(self):
        parts = [self.machine.get(key) for key in ('hostname', 'platform')]
        return ', '.join(part for part in parts if part) or 'unknown'


def build_table(loops, min_time):
    """
    Table data for a mapping of benchmark name to loop count.
    """
    return {
        'table_version': TABLE_VERSION,
        'min_time': min_time,
        'machine': {
            'hostname': socket.gethostname(),
            'platform': platform.platform(),
            'python': platform.python_version(),
            'date': time.strftime('%Y-%m-%dT%H:%M:%S'),
        },
        'loops': dict(sorted(loops.items())),
    }


def load_or_none(filename):
    if not filename:
        return None
    return LoopsTable.load(filename)


def calibrate(script, script_args, min_time, python=None, verbose=False):
    """
    Run a benchmark script once and return the loop counts it calibrated to.

    Keyed by the name each benchmark reports, which is what --loops-table
    looks up. A script can report several benchmarks, and they routinely
    want counts orders of magnitude apart, so this has to be per benchmark
    function rather than per script.
    """
    python = python or sys.executable
    cmd = [python, script,
           '--processes', '1', '--values', '1',
           '--min-time', str(min_time),
           '--print-loops']
    cmd.extend(script_args)
    if verbose:
        print("Running %s" % ' '.join(cmd), file=sys.stderr)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode:
        # Print whatever the script said; it is the only clue to why it failed.
        if proc.stdout:
            sys.stdout.write(proc.stdout)
        if proc.stderr:
            sys.stderr.write(proc.stderr)
        raise CalibrationFailed("%s failed with exit code %s"
                                % (script, proc.returncode))

    loops = {}
    for line in proc.stdout.splitlines():
        # Only lines --print-loops wrote. The script can produce its own
        # output, too, so unmarked lines are ignored rather than guessed at.
        parts = line.split('\t')
        if len(parts) != 3 or parts[0] != LOOPS_MARKER:
            continue
        name, count = parts[1], parts[2]
        if name and count.isdigit() and int(count) > 0:
            loops[name] = int(count)
    return loops


def cmd_loops_table(args):
    """`pyperf loops_table`: calibrate benchmarks and write their counts."""
    existing = {}
    min_time = args.min_time
    if args.append and os.path.exists(args.output):
        table = LoopsTable.load(args.output)
        existing = table.loops
        if table.min_time and table.min_time != min_time:
            print("ERROR: %s was generated with --min-time %s, but this run "
                  "uses %s. Loop counts from the two are not comparable; "
                  "write a new table instead of appending."
                  % (args.output, table.min_time, min_time), file=sys.stderr)
            sys.exit(1)

    loops = dict(existing)
    for script in args.scripts:
        if not os.path.isfile(script):
            print("ERROR: no such benchmark script: %s" % script,
                  file=sys.stderr)
            sys.exit(1)
        print("Calibrating %s ..." % script, flush=True)
        try:
            measured = calibrate(script, args.script_args, min_time,
                                 python=args.python, verbose=args.verbose)
        except CalibrationFailed as exc:
            # The script's own output is above; a traceback from here adds
            # nothing but noise.
            print("ERROR: %s" % exc, file=sys.stderr)
            sys.exit(1)
        for name in sorted(measured):
            print("    %-40s %s loops" % (name, measured[name]))
        if not measured:
            print("WARNING: %s reported no loop counts" % script,
                  file=sys.stderr)
        loops.update(measured)

    data = build_table(loops, min_time)
    with open(args.output, 'w', encoding='utf-8') as fp:
        json.dump(data, fp, indent=2, sort_keys=True)
        fp.write('\n')

    added = len(loops) - len(existing)
    print("Wrote %s loop counts to %s%s"
          % (len(loops), args.output,
             " (%s new)" % added if existing else ""))
    print("Use it with: --loops-table %s" % args.output)
