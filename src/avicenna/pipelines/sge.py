"""Shared SGE submit-script writer.

Every AVICENNA job script is written here, so every job gets the same two
limits: the hard run-time limit (``h_rt``) and a soft one (``s_rt``) shortly
before it.  At the soft limit SGE warns the job with ``SIGUSR1``; the script
hands that warning to the job it runs, which records why it is stopping (see
``jobbase.RuntimeLimited``).  A job killed at ``h_rt`` without warning records
nothing, and whatever waits on its log waits forever.

Scheduler-specific by nature: a different scheduler replaces this module and
``utils.qsub_terse``, nothing else.
"""

import stat
from pathlib import Path

#: Time between the soft and the hard limit.  It has to exceed the queue's
#: ``notify`` time, or the hard kill arrives before the job has been warned.
SOFT_LIMIT_MARGIN_S = 300


def _seconds(hms):
    h, m, s = (int(part) for part in str(hms).split(":"))
    return h * 3600 + m * 60 + s


def _hms(seconds):
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def soft_limit(h_rt):
    """The ``s_rt`` that goes with *h_rt* (``HH:MM:SS``).

    A limit too short for the full margin keeps nine tenths of itself instead,
    so the soft limit always falls strictly before the hard one.
    """
    hard = _seconds(h_rt)
    margin = SOFT_LIMIT_MARGIN_S if hard > 2 * SOFT_LIMIT_MARGIN_S else hard // 10
    return _hms(max(hard - margin, 1))


class SGESubmitter:
    """Mixin for anything that writes an SGE job script."""

    def _write_sge_script(self, path, command, *, h_rt, slots=1, mem_free="2G",
                          array=None, hold_jid=None, logs_dir=None,
                          resources=(), preamble=""):
        """Write an executable SGE job script that runs *command*.

        *array* is the task count of an array job, *resources* any further
        ``-l`` requests, and *preamble* shell lines run before *command*.  The
        command is run in the background, so the warning at the soft limit
        reaches it instead of ending the script.
        """
        path = Path(path)
        header = ["#!/bin/bash",
                  "#$ -S /bin/bash",
                  "#$ -cwd"]
        if array:
            header.append(f"#$ -t 1-{array}")
        header += ["#$ -j y",
                   f"#$ -pe smp {slots}",
                   f"#$ -l mem_free={mem_free}",
                   f"#$ -l h_rt={h_rt}",
                   f"#$ -l s_rt={soft_limit(h_rt)}"]
        header += [f"#$ -l {resource}" for resource in resources]
        if hold_jid:
            header.append(f"#$ -hold_jid {hold_jid}")
        if logs_dir:
            header.append(f"#$ -o {logs_dir}")
        body = [
            "# Hand the run-time warning to the job instead of dying of it here.",
            "trap 'kill -USR1 ${child} 2>/dev/null' USR1",
            f"{command} &",
            "child=$!",
            "wait ${child}; rc=$?",
            "while kill -0 ${child} 2>/dev/null; do wait ${child}; rc=$?; done",
            "exit ${rc}",
        ]
        text = "\n".join(header) + "\n\n" + preamble + "\n".join(body) + "\n"
        path.write_text(text)
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IRUSR | stat.S_IWUSR)
        return path
