"""Automatic SGE slot / processor estimation for the sub-pipelines.

Each sub-pipeline parallelises differently, so each gets its own rule -- a single
global "ncpu" would be wrong for at least two of them:

* XDS (processing) scales with the number of frames and uses two knobs whose
  product is the core count: ``MAXIMUM_NUMBER_OF_JOBS`` (forked processes, each
  integrating a contiguous batch of frames) times ``MAXIMUM_NUMBER_OF_PROCESSORS``
  (threads per batch).  The forked jobs are what actually scale; thread scaling
  flattens after a handful of cores.
* PanDDA (analysis) parallelises across datasets, so its useful core count is
  bounded by the number of datasets (and, in practice, node memory).
* Refinement is REFMAC5-dominated and effectively serial -- one slot.

The matching cost is asymmetric, which is why we cap: over-requesting leaves
cores idle (still counted against fairshare), makes the job harder to schedule,
and multiplies the per-slot ``mem_free`` request; under-requesting only costs
wall-clock.  The caps below keep us off the diminishing-returns tail and out of
the cluster's hard-to-schedule fat-job regime.

All public functions return concrete ints.  ``is_auto`` recognises every form an
"automatic" request can take by the time it reaches a submit path -- the freephil
``Auto`` sentinel, the string ``"auto"`` (how Auto round-trips through a resource
PHIL file and the flat params.json), ``None``, or an empty string.
"""

# XDS: thread/job scaling flattens past ~16 cores for typical (<~3k frame) data.
XDS_MAX_PROCESSORS  = 16
XDS_FRAMES_PER_CORE = 100   # grow the core count one core per ~100 frames
XDS_FRAMES_PER_JOB  = 50    # keep each forked XDS job >= ~50 frames
XDS_THREADS_PER_JOB = 4     # threads per forked job (speeds non-forking phases)

# PanDDA: one core per dataset, capped (memory- and scheduler-bounded).
PANDDA_MAX_CPUS = 36

# Refinement: REFMAC5 is effectively serial.
REFINE_SLOTS = 1


def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


def is_auto(value):
    """True when a slot/cpu setting requests automatic estimation.

    Accepts the freephil ``Auto`` sentinel, the strings ``"auto"``/``"Auto"``
    (how Auto serialises into a resource PHIL and arrives via params.json),
    ``None``, and ``""`` -- so the same check works on both the PHIL side and
    the stringified-params side without importing freephil here."""
    if value is None:
        return True
    return str(value).strip().lower() in ("auto", "none", "")


def xds_slots(frames, cap=XDS_MAX_PROCESSORS, threads_per_job=XDS_THREADS_PER_JOB):
    """Return ``(total, jobs, processors)`` for an XDS job over *frames* images.

    ``total = jobs * processors`` is the SGE slot count to request.  The core
    count grows with frame count (``XDS_FRAMES_PER_CORE``) up to *cap*, then
    splits into forked jobs times threads-per-job: a few threads per job speed
    the phases that do NOT fork over jobs (IDXREF / INIT / CORRECT), while the
    forked jobs parallelise the ones that do (COLSPOT / INTEGRATE), each kept to
    >= ``XDS_FRAMES_PER_JOB`` frames.  ``total`` is the realised ``jobs *
    processors`` (rounding may drop it just below the raw core estimate)."""
    frames = max(1, int(frames))
    cores  = _clamp(round(frames / XDS_FRAMES_PER_CORE), 1, cap)
    procs  = _clamp(cores, 1, threads_per_job)
    jobs   = _clamp(cores // procs, 1, max(1, frames // XDS_FRAMES_PER_JOB))
    return jobs * procs, jobs, procs


def pandda_cpus(n_datasets, cap=PANDDA_MAX_CPUS):
    """Return the cpu count for pandda.analyse: one core per dataset, capped."""
    return _clamp(int(n_datasets), 1, cap)


def refine_slots():
    """Return the SGE slot count for a refinement job (REFMAC5 is serial)."""
    return REFINE_SLOTS
