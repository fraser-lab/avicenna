#!/usr/bin/env python3
"""
Generalizable data processing and evaluation pipeline.

Classes
-------
RunBase       -- generic launcher for a single processing or analysis job
RunXDS        -- XDS crystallographic data processing
RunPointless  -- Pointless space-group determination / reindexing
RunAimless    -- Aimless scaling and merging
RunRfree      -- R-free flag generation (freerflag / ctruncate)
RunXtriage    -- Phenix Xtriage data quality analysis
RunProcess    -- orchestrator that drives the full pipeline
"""

import importlib.resources
import json
import os
import platform
import sys
import pathlib
import re
import shutil
import stat
import subprocess
import time
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(pathlib.Path(__file__).parents[3] / "src"))
from avicenna.pipelines.sge import SGESubmitter
from avicenna.utils.utils import BRAVAIS_LATTICES, MIN_IMAGES, QRefineWriter, format_log_line, ProcessingWatcher, sg_lookup, count_hdf5_images, qsub_terse, collection_id, dataset_tag, discover_sweeps, numeric_tokens
from avicenna.utils.software import find_xds, find_ccp4, find_phenix, suite_setup_sources
from avicenna.utils.crystal_list import TAG_REJECTED, log_tag
from avicenna.utils import resources


# Rows of the aimless summary block, as (record field, text aimless prints).
# The label is matched as a substring of the line; the first line that carries
# it wins, since the block appears once.  "half-set correlation" identifies the
# CC1/2 row on its own -- the other CC(1/2) lines are table and graph headers.
_AIMLESS_SHELL_ROWS = (
    ("low_res_limit",  "Low resolution limit"),
    ("high_res_limit", "High resolution limit"),
    ("completeness",   "Completeness"),
    ("multiplicity",   "Multiplicity"),
    ("cc_half",        "half-set correlation"),
    ("rpim",           "Rpim (within I+/I-)"),
    ("i_sig_i",        "Mn(I/sd(I))"),
    ("unit_cell",      "Average unit cell"),
)

_AIMLESS_TEXT_ROWS = (
    ("space_group", "Space group"),
    ("mosaicity",   "Average mosaicity"),
)


def format_aimless_summary(stats):
    """The one-line scaling summary, derived from ``_aimless_stats`` values.

    Resolution is the outer shell; the rest are overall.
    """
    parts = []
    if "high_res_limit" in stats: parts.append(f"res={stats['high_res_limit'][-1]}Å")
    if "space_group"    in stats: parts.append(f"SG={stats['space_group']}")
    if "unit_cell"      in stats: parts.append(f"UC={' '.join(stats['unit_cell'])}")
    if "completeness"   in stats: parts.append(f"comp={stats['completeness'][0]}")
    if "rpim"           in stats: parts.append(f"rpim={stats['rpim'][0]}")
    if "cc_half"        in stats: parts.append(f"CC½={stats['cc_half'][0]}")
    return " ".join(parts) if parts else "completed"


class RunBase:
    """
    Generic launcher for a single data processing or analysis job.

    Responsible for assembling a command, executing it in the appropriate
    working directory, and capturing stdout/stderr.  Specific job types
    (RunXDS, RunPointless, ...) subclass this and supply the command details.
    """
    pass


class RunXDS(RunBase):
    """
    Create and run an XDS data processing job.

    Handles the full XDS pipeline for one dataset: generation of XDS.INP
    (via generate_XDS.INP), indexing, integration, and correction.  Supports
    multiple passes (initial indexing, forced integration, recycled detector
    parameters, refined symmetry/resolution) as well as reference datasets
    for known-symmetry constraints.

    The three processing scenarios, each implemented as a method:

        xds_initial_indexing   -- step 1_xds, generate_inp=True
        xds_forced_integration -- step 1_xds, force_integration=True
        xds_recycle_detector   -- step 4_xds, generate_inp=False, recycle_geometry=True
        xds_refine_symmetry    -- step 7_xds, generate_inp=False, update_symmetry=True
    """

    def __init__(self, params, step_id, xds_executable, xdsstat_executable,
                 prev_xds_id=None, prev_aimless_id=None, prev_pointless_id=None):
        """
        Parameters
        ----------
        params : dict
            Pipeline parameters from vic_process.py / RunProcess.
        step_id : str
            Step identifier and output directory name (e.g. "1_xds", "4_xds").
        xds_executable : str or Path
            Full path to the xds_par binary.
        xdsstat_executable : str or Path
            Full path to the xdsstat binary.
        prev_xds_id : str or None
            step_id of the preceding XDS step to copy geometry from.
        prev_aimless_id : str or None
            step_id of the preceding Aimless step to read symmetry/resolution from.
        prev_pointless_id : str or None
            step_id of the preceding Pointless step to read space group from.
        """
        self.params = params
        self.step_id = step_id
        self.prev_xds_id       = prev_xds_id
        self.prev_aimless_id   = prev_aimless_id
        self.prev_pointless_id = prev_pointless_id
        self.xds_executable    = Path(xds_executable)
        self.xdsstat_executable = Path(xdsstat_executable)

        # Convenience aliases derived from params
        self.pin_id            = params.get("pin_id")
        self.image_prefix      = params.get("image_prefix", Path(params.get("image_dir", ".")).name)
        self.image_dir         = Path(params.get("image_dir"))
        self.pipeline_subdir   = Path(params.get("pipeline_subdir"))
        self.image_range       = params.get("image_range", "").replace("-", " ")
        # ncpu / xds_jobs are resolved to concrete ints at submit time (see
        # RunProcess.run); kept raw here so a stray "auto" can still be resolved
        # from the frame count in _set_xds_processors rather than crashing.
        self.ncpu              = params.get("ncpu", 8)
        self.njobs             = params.get("xds_jobs", 1)
        self.force_integration = params.get("force_integration", "false")
        self.known_first_pass  = params.get("known_first_pass", "none")
        self.known_sg          = params.get("known_sg", "none")
        self.known_sg_number   = params.get("known_sg_number", "none")
        self.known_cell        = params.get("known_cell", "none").replace("-", " ")
        self.ref_hkl           = params.get("ref_hkl", "none")
        self.ref_mtz           = params.get("ref_mtz", "none")
        self.sg                = params.get("sg", "none")
        self.sg_number         = params.get("sg_number", "none")
        self.cell              = params.get("cell", "none").replace("-", " ")
        self.refine_cell_integrate = params.get("refine_cell_integrate", "true")
        self.detector_edge     = float(params.get("detector_edge", 0.8))
        self.res_limit         = params.get("res_limit", "cc12")
        self.data_range        = params.get("data_range", "false").replace("-", " ")
        self.data_format       = params.get("data_format", "cbf")
        self._xds_dir          = self.xds_executable.parent.parent

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _run_xds(self):
        """Execute xds_par in the current directory, writing log to <step_id>.log."""
        log = Path(f"{self.step_id}.log")
        with open(log, "w") as fh:
            subprocess.run([str(self.xds_executable)], stdout=fh, stderr=fh)

    def _run_xdsstat(self):
        """Run xdsstat on XDS_ASCII.HKL if it was produced."""
        if Path("XDS_ASCII.HKL").is_file():
            xdsstat_log = self.step_id.replace("_xds", "_xdsstat") + ".log"
            with open(Path(xdsstat_log), "w") as fh:
                subprocess.run([str(self.xdsstat_executable)],
                               input="\n", text=True, stdout=fh, stderr=fh)

    def _apply_refine_cell_integrate(self, xds_inp):
        """
        If refine_cell_integrate is 'false', move CELL out of the REFINE(INTEGRATE) list.

        Modifies the XDS.INP at *xds_inp* in place.
        """
        if self.refine_cell_integrate == "false":
            text = xds_inp.read_text()
            text = text.replace(
                "REFINE(INTEGRATE)= DISTANCE POSITION ORIENTATION CELL BEAM ! AXIS",
                "REFINE(INTEGRATE)= DISTANCE POSITION ORIENTATION BEAM ! CELL AXIS",
            )
            xds_inp.write_text(text)

    def _apply_ref_hkl(self, xds_inp):
        """Uncomment REFERENCE_DATA_SET in XDS.INP if a reference HKL was supplied."""
        if self.ref_hkl != "none":
            text = xds_inp.read_text()
            text = text.replace(
                "! REFERENCE_DATA_SET=",
                f"REFERENCE_DATA_SET={self.ref_hkl}",
            )
            xds_inp.write_text(text)

    def _inject_lib(self, xds_inp):
        """Prepend LIB= to XDS.INP so neggia can read HDF5 data. No-op for CBF
        or when an active LIB= line is already present."""
        if self.data_format != 'hdf5':
            return
        existing = xds_inp.read_text()
        if any(line.lstrip().startswith("LIB=") for line in existing.splitlines()):
            return
        neggia = self._xds_dir / "lib" / "dectris-neggia.so"
        xds_inp.write_text(f"LIB={neggia}\n" + existing)

    def _recycle_detector_params(self, xds_dir, xds_inp, is_job2=False):
        """
        Inject refined detector geometry from a previous XDS run into *xds_inp*.

        Reads ROTATION_AXIS, INCIDENT_BEAM_DIRECTION, ORGX, DETECTOR_DISTANCE
        from XDS_ASCII.HKL and BEAM_DIVERGENCE / REFLECTING_RANGE from
        INTEGRATE.LP.  Replaces XPARM.XDS with GXPARM.XDS.

        Parameters
        ----------
        xds_dir : Path
            Directory of the previous XDS run.
        xds_inp : Path
            XDS.INP file to update in place.
        is_job2 : bool
            True when recycling into the detector-only step: BEAM_DIVERGENCE and
            REFLECTING_RANGE are *inserted* after DETECTOR_DISTANCE rather than
            overwriting existing lines.
        """
        hkl_text = (xds_dir / "XDS_ASCII.HKL").read_text()
        int_lp   = (xds_dir / "INTEGRATE.LP").read_text()

        def _grep(text, keyword):
            for line in text.splitlines():
                if keyword in line:
                    return line.replace("!", "").strip()
            return ""

        rotation_axis           = _grep(hkl_text, "ROTATION_AXIS=")
        incident_beam_direction = _grep(hkl_text, "INCIDENT_BEAM_DIRECTION=")
        orgxy                   = _grep(hkl_text, "ORGX=")
        detector_distance       = _grep(hkl_text, "DETECTOR_DISTANCE=")
        beam_divergence  = next(
            (" ".join(l.split()) for l in int_lp.splitlines() if "BEAM_DIVERGENCE=" in l), "")
        reflecting_range = next(
            (" ".join(l.split()) for l in int_lp.splitlines() if "REFLECTING_RANGE=" in l), "")

        text = xds_inp.read_text()
        text = text.replace(
            "JOB= XYCORR INIT COLSPOT IDXREF DEFPIX INTEGRATE CORRECT",
            "JOB= DEFPIX INTEGRATE CORRECT",
        )
        text = re.sub(r"ROTATION_AXIS=.*!",            f"{rotation_axis} !",           text)
        text = re.sub(r"INCIDENT_BEAM_DIRECTION=.*!",  f"{incident_beam_direction} !", text)
        text = re.sub(r"ORGX=.*!",                     f"{orgxy} !",                   text)
        text = re.sub(r"DETECTOR_DISTANCE=.*!",        f"{detector_distance} !",       text)
        xds_inp.write_text(text)

        shutil.copy(str(xds_dir / "GXPARM.XDS"), "XPARM.XDS")

        if is_job2:
            # Insert BEAM_DIVERGENCE after DETECTOR_DISTANCE line,
            # and REFLECTING_RANGE after the newly inserted BEAM_DIVERGENCE line
            lines = xds_inp.read_text().splitlines(keepends=True)
            out = []
            for line in lines:
                out.append(line)
                if "DETECTOR_DISTANCE=" in line:
                    out.append(beam_divergence + "\n")
                elif "BEAM_DIVERGENCE" in line and beam_divergence:
                    out.append(reflecting_range + "\n")
            xds_inp.write_text("".join(out))
        else:
            # Overwrite existing BEAM_DIVERGENCE / REFLECTING_RANGE lines
            text = xds_inp.read_text()
            text = re.sub(r"BEAM_DIVERGENCE=.*",  beam_divergence,  text)
            text = re.sub(r"REFLECTING_RANGE=.*", reflecting_range, text)
            xds_inp.write_text(text)

    def _xds_processor_settings(self):
        """Return ``(processors, jobs)`` for this XDS run.

        Normally both are concrete ints baked into params.json at submit time
        (RunProcess.run).  If 'auto' leaks through (e.g. a direct/legacy caller),
        size them here from the frame count of the range being processed so the
        run never crashes on a string."""
        procs, jobs = self.ncpu, self.njobs
        if resources.is_auto(procs):
            rng  = self.data_range if self.data_range != "false" else self.image_range
            nums = [int(x) for x in str(rng).split() if x.lstrip("-").isdigit()]
            if len(nums) >= 2:
                frames = nums[-1] - nums[0] + 1
            else:
                frames = nums[0] if nums else 1
            _, jobs, procs = resources.xds_slots(frames)
        return int(procs), int(jobs)

    # ------------------------------------------------------------------
    # Scenario methods
    # ------------------------------------------------------------------

    def xds_initial_indexing(self):
        """
        Default first-pass XDS run (step 1_xds).

        Generates XDS.INP from scratch using the generate_XDS.INP helper
        script, then runs the full XYCORR -> INIT -> COLSPOT -> IDXREF ->
        DEFPIX -> INTEGRATE -> CORRECT pipeline.  Optionally seeds XDS.INP
        with a known space group and unit cell for constrained indexing,
        and injects a reference HKL for isomorphism-guided integration.
        """
        job_dir = self.pipeline_subdir / self.step_id
        job_dir.mkdir(parents=True, exist_ok=True)
        os.chdir(job_dir)

        # Generate XDS.INP from the appropriate script
        if self.data_format == 'hdf5':
            # SBGrid's generate_XDS.INP reads all geometry from the master file directly.
            # SPOT_RANGE is overridden below to use the user-specified indexing subrange.
            # If the HDF5 generate_XDS.INP calling convention changes, update here.
            generate_inp = self._xds_dir / "bin" / "generate_XDS.INP"
            master = self.image_dir / f"{self.image_prefix}_master.h5"
            subprocess.run([str(generate_inp), str(master)])
        else:
            generate_inp = job_dir / "generate_XDS.INP"
            template = importlib.resources.files("avicenna.resources.scripts").joinpath("generate_XDS.INP").read_text()
            generate_inp.write_text(template.replace("sed_replace", self.image_range))
            generate_inp.chmod(generate_inp.stat().st_mode | stat.S_IXUSR | stat.S_IRUSR | stat.S_IWUSR)
            subprocess.run([str(generate_inp),
                            str(self.image_dir / f"{self.image_prefix}_0????.cbf")])

        xds_inp = job_dir / "XDS.INP"
        text = xds_inp.read_text()

        # Set the XDS parallelism.  MAXIMUM_NUMBER_OF_JOBS forks processes over
        # frame batches (COLSPOT / INTEGRATE); MAXIMUM_NUMBER_OF_PROCESSORS
        # threads within each (and drives the phases that do not fork).  Both are
        # resolved from ncpu -- normally concrete ints baked at submit time, with
        # an 'auto' fallback sized here from the frame count.
        procs, jobs = self._xds_processor_settings()
        text = text.replace("MAXIMUM_NUMBER_OF_PROCESSORS=1",
                            f"MAXIMUM_NUMBER_OF_PROCESSORS={procs}")
        text = text.replace("MAXIMUM_NUMBER_OF_JOBS=1",
                            f"MAXIMUM_NUMBER_OF_JOBS={jobs}")

        # Override data range if specified
        if self.data_range != "false":
            text = re.sub(r"DATA_RANGE=.*", f"DATA_RANGE={self.data_range}", text)

        xds_inp.write_text(text)

        if self.data_format == 'hdf5':
            # SBGrid's script sets SPOT_RANGE to the full data range; override
            # with the user-specified indexing subrange.
            text = xds_inp.read_text()
            text = re.sub(r"SPOT_RANGE=.*", f"SPOT_RANGE={self.image_range}", text)
            xds_inp.write_text(text)

        # Seed with known symmetry for constrained indexing
        if self.known_sg_number != "none" and self.known_first_pass != "none":
            text = xds_inp.read_text()
            text = re.sub(r"SPACE_GROUP_NUMBER=.*!",
                          f"SPACE_GROUP_NUMBER= {self.known_sg_number} !", text)
            text = re.sub(r"UNIT_CELL_CONSTANTS=.*!",
                          f"UNIT_CELL_CONSTANTS= {self.known_cell} !", text)
            xds_inp.write_text(text)

        self._apply_refine_cell_integrate(xds_inp)
        self._apply_ref_hkl(xds_inp)
        self._inject_lib(xds_inp)
        self._run_xds()
        self._run_xdsstat()

    def xds_forced_integration(self):
        """
        Force integration using geometry from a prior XPARM.XDS (step 1_xds, force_integration=True).

        Skips spot-finding and indexing, runs only DEFPIX -> INTEGRATE -> CORRECT,
        seeding geometry from XPARM.XDS produced during the very first XDS run
        (../../1_xds relative to the force subdirectory).
        """
        job_dir = self.pipeline_subdir / self.step_id
        job_dir.mkdir(parents=True, exist_ok=True)
        os.chdir(job_dir)

        xds_dir = Path("../../1_xds")
        xparm = xds_dir / "XPARM.XDS"

        if not xparm.is_file():
            print("xds failed to generate XPARM.XDS")
            return

        # Copy XDS.INP from the first run, updating SPOT_RANGE and symmetry
        text = (xds_dir / "XDS.INP").read_text()
        text = re.sub(r"SPOT_RANGE=.*", f"SPOT_RANGE={self.image_range}", text)
        text = re.sub(r"SPACE_GROUP_NUMBER=.*!",
                      f"SPACE_GROUP_NUMBER= {self.sg_number} !", text)
        if self.cell != "none":
            text = re.sub(r"UNIT_CELL_CONSTANTS=.*!",
                          f"UNIT_CELL_CONSTANTS= {self.cell} !", text)
        text = text.replace(
            "JOB= XYCORR INIT COLSPOT IDXREF DEFPIX INTEGRATE CORRECT",
            "JOB= DEFPIX INTEGRATE CORRECT",
        )
        if self.data_format == 'cbf':
            # CBF NAME_TEMPLATE may need to be re-pointed at the original image
            # dir (the source XDS.INP may reference a scratch path from a prior
            # task).  HDF5 templates use a ?????? placeholder that's already
            # correct in the copied XDS.INP, so leave them alone.
            text = re.sub(r"NAME_TEMPLATE_OF_DATA_FRAMES=.*",
                          f"NAME_TEMPLATE_OF_DATA_FRAMES={self.image_dir}/{self.image_prefix}_0????.cbf",
                          text)
        (job_dir / "XDS.INP").write_text(text)
        self._inject_lib(job_dir / "XDS.INP")

        shutil.copy(str(xparm), "XPARM.XDS")
        # Copy XDS-generated correction frames (X-CORRECTIONS, Y-CORRECTIONS,
        # BKGINIT, BLANK, GAIN, ...). Raw images live in image_dir for CBF and
        # in the HDF5 master for hdf5, so this glob only catches XDS output.
        for cbf in xds_dir.glob("*.cbf"):
            shutil.copy(str(cbf), ".")

        self._run_xds()
        self._run_xdsstat()

    def xds_bypass_indexing_failure(self):
        """
        In-place bypass of indexing failure: modify the existing XDS.INP to
        run JOB= DEFPIX INTEGRATE CORRECT using the XPARM.XDS already present
        in the step directory.  Used when on_indexing_failure = bypass.
        """
        job_dir = self.pipeline_subdir / self.step_id
        os.chdir(job_dir)
        xds_inp = job_dir / "XDS.INP"
        text = xds_inp.read_text()
        text = text.replace(
            "JOB= XYCORR INIT COLSPOT IDXREF DEFPIX INTEGRATE CORRECT",
            "JOB= DEFPIX INTEGRATE CORRECT",
        )
        xds_inp.write_text(text)
        self._run_xds()
        self._run_xdsstat()

    def xds_recycle_detector(self):
        """
        Second XDS run recycling refined detector geometry (step 4_xds).

        Copies XDS.INP and geometry files from prev_xds_id, reduces the job
        list to DEFPIX -> INTEGRATE -> CORRECT, and injects refined detector
        parameters from XDS_ASCII.HKL and INTEGRATE.LP of the previous run.
        """
        xds_dir = self.pipeline_subdir / self.prev_xds_id
        job_dir = self.pipeline_subdir / self.step_id
        job_dir.mkdir(parents=True, exist_ok=True)
        os.chdir(job_dir)

        shutil.copy(str(xds_dir / "XDS.INP"), "XDS.INP")
        for cbf in xds_dir.glob("*.cbf"):
            shutil.copy(str(cbf), ".")
        shutil.copy(str(xds_dir / "XPARM.XDS"), "XPARM.XDS")

        self._recycle_detector_params(xds_dir, job_dir / "XDS.INP", is_job2=True)
        self._run_xds()
        self._run_xdsstat()

    def xds_refine_symmetry(self):
        """
        XDS run with updated symmetry and resolution (step 7_xds).

        Copies XDS.INP and geometry from prev_xds_id, recycles refined
        detector parameters, and additionally updates SPACE_GROUP_NUMBER,
        UNIT_CELL_CONSTANTS, and INCLUDE_RESOLUTION_RANGE using values parsed
        from prev_aimless_id and prev_pointless_id logs.
        """
        xds_dir       = self.pipeline_subdir / self.prev_xds_id
        aimless_log   = (self.pipeline_subdir / self.prev_aimless_id
                         / f"{self.prev_aimless_id}.log")
        pointless_log = (self.pipeline_subdir / self.prev_pointless_id
                         / f"{self.prev_pointless_id}.log")

        job_dir = self.pipeline_subdir / self.step_id
        job_dir.mkdir(parents=True, exist_ok=True)
        os.chdir(job_dir)

        shutil.copy(str(xds_dir / "XDS.INP"), "XDS.INP")
        for cbf in xds_dir.glob("*.cbf"):
            shutil.copy(str(cbf), ".")
        shutil.copy(str(xds_dir / "XPARM.XDS"), "XPARM.XDS")

        xds_inp = job_dir / "XDS.INP"
        self._recycle_detector_params(xds_dir, xds_inp, is_job2=False)

        # Parse symmetry from Aimless / Pointless logs
        aim_text = aimless_log.read_text()
        ptl_text = pointless_log.read_text()

        cell = next(
            (line.split(":", 1)[1].strip()
             for line in aim_text.splitlines() if "Average unit cell:" in line), "")
        sg_number = next(
            (re.sub(r"[^0-9]", "", line.split("(", 1)[1])
             for line in ptl_text.splitlines() if "Space group" in line and "number" in line), "")

        # Determine resolution limit
        def _cc12_limit(text):
            for line in text.splitlines():
                if "0.30: limit =" in line:
                    return line.split("=", 1)[1].split("A")[0].strip()
            return "0.00"

        def _completeness_limit(text, threshold):
            high_res_lim = None
            in_block = False
            for line in text.splitlines():
                if ":Multiplicity v Resolution:" in line:
                    in_block = True
                    continue
                if not in_block:
                    continue
                parts = line.split()
                if len(parts) < 7:
                    continue
                try:
                    comp = int(parts[6].split(".")[0])
                    res  = parts[2]
                    if comp > threshold:
                        high_res_lim = res
                except (ValueError, IndexError):
                    continue
            return high_res_lim

        res_lim_cc12 = _cc12_limit(aim_text)

        if self.res_limit == "cc12":
            high_res_lim = res_lim_cc12
        else:
            threshold    = int(self.res_limit)
            res_lim_comp = _completeness_limit(aim_text, threshold)
            if res_lim_comp is None or float(res_lim_comp) < float(res_lim_cc12):
                high_res_lim = res_lim_cc12
            else:
                high_res_lim = res_lim_comp

        # Clip to detector edge
        if float(high_res_lim) < self.detector_edge:
            high_res_lim = str(self.detector_edge)

        low_res_lim = next(
            (line.split("Low resolution limit", 1)[1].split()[0]
             for line in aim_text.splitlines() if "Low resolution limit" in line), "50")

        # Guard against a zero-resolution Aimless result
        if high_res_lim == "0.00":
            print("Aimless failed to detect enough reflections. Check log files.")
            return

        # Update XDS.INP with symmetry and resolution
        text = xds_inp.read_text()
        text = re.sub(r"SPACE_GROUP_NUMBER=.*!",
                      f"SPACE_GROUP_NUMBER= {sg_number} !", text)
        text = re.sub(r"UNIT_CELL_CONSTANTS=.*!",
                      f"UNIT_CELL_CONSTANTS= {cell} !", text)
        text = re.sub(r"INCLUDE_RESOLUTION_RANGE=.*!",
                      f"INCLUDE_RESOLUTION_RANGE={low_res_lim} {high_res_lim} !", text)
        xds_inp.write_text(text)

        self._run_xds()
        self._run_xdsstat()


class RunPointless(RunBase):
    """
    Create and run a Pointless space-group determination job.

    Takes unmerged reflection data (INTEGRATE.HKL or XDS_ASCII.HKL) from a
    preceding XDS directory and runs Pointless to determine or confirm the
    space group.  Optionally enforces a known space group from a previous
    Aimless run.
    """

    def __init__(self, params, step_id, ccp4_bin, prev_xds_id=None, prev_aimless_id=None):
        """
        Parameters
        ----------
        params : dict
            Pipeline parameters from RunProcess.
        step_id : str
            Step identifier and output directory name (e.g. "2_pointless").
        ccp4_bin : Path
            Path to the CCP4 bin/ directory (from _find_ccp4_bin()).
        prev_xds_id : str or None
            step_id of the preceding XDS run to read reflection data from.
        prev_aimless_id : str or None
            step_id of the preceding Aimless run to read space group from.
            None means first pass: Pointless chooses freely.
        """
        self.params          = params
        self.step_id         = step_id
        self.prev_xds_id     = prev_xds_id
        self.prev_aimless_id = prev_aimless_id
        self.sg              = params.get("sg", "none")
        self.known_sg        = params.get("known_sg", "none")
        self.scaling         = params.get("scaling", "correct")
        self.pipeline_subdir = Path(params.get("pipeline_subdir"))
        self.pointless       = Path(ccp4_bin) / "pointless"

    def run(self):
        """
        Execute Pointless for this step.

        Input file is INTEGRATE.HKL when scaling will be done by Aimless, or
        XDS_ASCII.HKL when XDS CORRECT already scaled the data.  The previous
        XDS directory is given by prev_xds_id.

        Space group handling:
          - prev_aimless_id is None: first pass, let Pointless choose freely
            (or enforce known_sg if supplied)
          - prev_aimless_id set: read the space group from that Aimless log
            and enforce it
        """
        job_dir = self.pipeline_subdir / self.step_id
        job_dir.mkdir(parents=True, exist_ok=True)
        os.chdir(job_dir)

        pointless_input = "INTEGRATE.HKL" if self.scaling == "aimless" else "XDS_ASCII.HKL"
        hklin   = f"../{self.prev_xds_id}/{pointless_input}"
        hklout  = f"XDS_ASCII_{self.step_id}.mtz"
        log_path = job_dir / f"{self.step_id}.log"

        if self.prev_aimless_id is None:
            # First pass: no prior aimless result -- let Pointless choose freely
            if self.known_sg != "none":
                stdin = (
                    f"HKLIN {hklin}\n"
                    f"HKLOUT {hklout}\n"
                    "SETTING SYMMETRY-BASED\n"
                    f"SPACEGROUP {self.known_sg}\n"
                    "END\n"
                )
            else:
                stdin = (
                    f"HKLIN {hklin}\n"
                    f"HKLOUT {hklout}\n"
                    "SETTING SYMMETRY-BASED\n"
                    "END\n"
                )
        else:
            # Read space group chosen by the previous Aimless run
            aimless_log = (self.pipeline_subdir / self.prev_aimless_id
                           / f"{self.prev_aimless_id}.log")
            aim_text = aimless_log.read_text()
            sg = next(
                (line.split(":", 1)[1].replace(" ", "").strip()
                 for line in aim_text.splitlines() if "Space group:" in line),
                "P1",
            )
            stdin = (
                f"HKLIN {hklin}\n"
                f"HKLOUT {hklout}\n"
                "SETTING SYMMETRY-BASED\n"
                f"SPACEGROUP {sg}\n"
                "END\n"
            )

        with open(log_path, "w") as fh:
            subprocess.run([str(self.pointless)], input=stdin, text=True, stdout=fh, stderr=fh)


class RunAimless(RunBase):
    """
    Create and run an Aimless scaling and merging job.

    Accepts merged MTZ output from Pointless and applies scaling/merging via
    Aimless.  When apply_res_cutoff is False no resolution cutoff is applied;
    when True the cutoff is derived from CC1/2 or completeness statistics
    extracted from prev_aimless_id's log.
    """

    def __init__(self, params, step_id, ccp4_bin,
                 prev_pointless_id=None, prev_aimless_id=None, apply_res_cutoff=False):
        """
        Parameters
        ----------
        params : dict
            Pipeline parameters from RunProcess.
        step_id : str
            Step identifier and output directory name (e.g. "3_aimless").
        ccp4_bin : Path
            Path to the CCP4 bin/ directory (from _find_ccp4_bin()).
        prev_pointless_id : str or None
            step_id of the preceding Pointless run to read MTZ from.
        prev_aimless_id : str or None
            step_id of a prior Aimless run to derive the resolution cutoff from.
        apply_res_cutoff : bool
            If True, derive and apply a resolution cutoff from prev_aimless_id.
        """
        self.params            = params
        self.step_id           = step_id
        self.prev_pointless_id = prev_pointless_id
        self.prev_aimless_id   = prev_aimless_id
        self.apply_res_cutoff  = apply_res_cutoff
        self.detector_edge     = float(params.get("detector_edge", 0.8))
        self.res_limit         = params.get("res_limit", "cc12")
        self.custom_res        = params.get("custom_res", "false")
        self.pipeline_subdir   = Path(params.get("pipeline_subdir"))
        self.aimless           = Path(ccp4_bin) / "aimless"

    def run(self):
        """
        Execute Aimless for this step.

        When apply_res_cutoff is False, merges without a resolution cutoff so
        that the full range is available for statistics.  When True, the
        resolution limit is derived from prev_aimless_id using either the
        CC1/2 > 0.30 criterion or a completeness threshold, clipped to the
        detector edge and any user-supplied custom resolution.
        """
        job_dir = self.pipeline_subdir / self.step_id
        job_dir.mkdir(parents=True, exist_ok=True)
        os.chdir(job_dir)

        hklin    = f"../{self.prev_pointless_id}/XDS_ASCII_{self.prev_pointless_id}.mtz"
        hklout   = f"XDS_ASCII_{self.step_id}.mtz"
        log_path = job_dir / f"{self.step_id}.log"

        if not self.apply_res_cutoff:
            # First pass: merge without resolution cutoff
            stdin = (
                f"HKLIN {hklin}\n"
                f"HKLOUT {hklout}\n"
                "onlymerge\n"
                "END\n"
            )
        else:
            aimless_log = (self.pipeline_subdir / self.prev_aimless_id
                           / f"{self.prev_aimless_id}.log")
            aim_text = aimless_log.read_text()

            # CC1/2-based limit
            res_lim_cc12 = next(
                (line.split("=", 1)[1].split("A")[0].strip()
                 for line in aim_text.splitlines() if "0.30: limit =" in line),
                "0.00",
            )

            if self.res_limit == "cc12":
                res_lim = res_lim_cc12
            else:
                # Completeness-based limit: last shell where completeness > threshold
                threshold    = int(self.res_limit)
                res_lim_comp = None
                in_block     = False
                for line in aim_text.splitlines():
                    if ":Multiplicity v Resolution:" in line:
                        in_block = True
                        continue
                    if not in_block:
                        continue
                    parts = line.split()
                    if len(parts) < 7:
                        continue
                    try:
                        comp = int(parts[6].split(".")[0])
                        res  = parts[2]
                        if comp > threshold:
                            res_lim_comp = res
                    except (ValueError, IndexError):
                        continue

                if res_lim_comp is None or float(res_lim_comp) < float(res_lim_cc12):
                    res_lim = res_lim_cc12
                else:
                    res_lim = res_lim_comp

            # Clip to detector edge
            if float(res_lim) < self.detector_edge:
                res_lim = str(self.detector_edge)

            # Clip to user-supplied custom resolution
            if self.custom_res not in (None, "false", ""):
                if float(res_lim) < float(self.custom_res):
                    res_lim = self.custom_res

            stdin = (
                f"HKLIN {hklin}\n"
                f"HKLOUT {hklout}\n"
                "onlymerge\n"
                f"RESOLUTION HIGH {res_lim}\n"
                "END\n"
            )

        with open(log_path, "w") as fh:
            subprocess.run([str(self.aimless)], input=stdin, text=True, stdout=fh, stderr=fh)


class RunRfree(RunBase):
    """
    Generate R-free flags and calculate structure-factor amplitudes.

    Accepts scaled MTZ output from Aimless and either generates a fresh set
    of R-free flags (freerflag) or copies flags from a reference MTZ
    (freerflag COMPLETE).  Amplitude conversion is performed by ctruncate.
    """

    def __init__(self, params, step_id, ccp4_bin, prev_aimless_id=None):
        """
        Parameters
        ----------
        params : dict
            Pipeline parameters from RunProcess.
        step_id : str
            Step identifier and output directory name (e.g. "10_rfree").
        ccp4_bin : Path
            Path to the CCP4 bin/ directory (from _find_ccp4_bin()).
        prev_aimless_id : str or None
            step_id of the last Aimless run to read scaled MTZ and stats from.
        """
        self.params          = params
        self.step_id         = step_id
        self.prev_aimless_id = prev_aimless_id
        self.ref_mtz         = params.get("ref_mtz", "none")
        self.pipeline_subdir = Path(params.get("pipeline_subdir"))
        ccp4_bin             = Path(ccp4_bin)
        self.freerflag       = ccp4_bin / "freerflag"
        self.mtzutils        = ccp4_bin / "mtzutils"
        self.unique          = ccp4_bin / "unique"
        self.cad             = ccp4_bin / "cad"
        self.ctruncate       = ccp4_bin / "ctruncate"

    def run(self):
        """
        Generate R-free flags and convert intensities to amplitudes.

        Cell, space group, and resolution are read from the prev_aimless_id log.

        Without a reference MTZ: freerflag assigns a fresh random 5% test set,
        mtzutils strips anomalous columns, and ctruncate converts I -> F.

        With a reference MTZ: unique builds a complete set of Miller indices,
        cad merges the reference FreeR_flag column in, freerflag COMPLETE
        extends the flags to the full resolution range, mtzutils strips
        unwanted columns, and ctruncate converts I -> F.
        """
        job_dir = self.pipeline_subdir / self.step_id
        job_dir.mkdir(parents=True, exist_ok=True)
        os.chdir(job_dir)

        aimless_log = (self.pipeline_subdir / self.prev_aimless_id
                       / f"{self.prev_aimless_id}.log")
        aim_text = aimless_log.read_text()

        res_lim = next(
            (line.split(":", 1)[1].split()[1]
             for line in aim_text.splitlines() if "Resolution range in file" in line),
            "1.5",
        )
        cell = next(
            (line.split(":", 1)[1].strip()
             for line in aim_text.splitlines() if "Average unit cell:" in line),
            "",
        )
        sg = next(
            (line.split(":", 1)[1].replace(" ", "").strip()
             for line in aim_text.splitlines() if "Space group:" in line),
            "P1",
        )

        log_path    = job_dir / f"{self.step_id}.log"
        aimless_mtz = f"../{self.prev_aimless_id}/XDS_ASCII_{self.prev_aimless_id}.mtz"

        if self.ref_mtz in (None, "none", ""):
            # Fresh R-free flags
            with open(log_path, "w") as fh:
                subprocess.run(
                    [str(self.freerflag),
                     "HKLIN", aimless_mtz,
                     "HKLOUT", "XDS_ASCII_rfree.mtz"],
                    input="UNIQUE\nEND\n",
                    text=True, stdout=fh, stderr=fh)

            with open(log_path, "a") as fh:
                subprocess.run(
                    [str(self.mtzutils),
                     "HKLIN", "XDS_ASCII_rfree.mtz",
                     "HKLOUT", "XDS_ASCII.mtz"],
                    input="EXCLUDE N(+) N(-)\nEND\n",
                    text=True, stdout=fh, stderr=fh)

        else:
            # Copy R-free flags from reference MTZ
            unique_stdin = (
                "LABOUT  F=FUNI SIGF=SIGFUNI\n"
                f"SYMM {sg}\n"
                f"RESOL {res_lim}\n"
                f"CELL {cell}\n"
                "END\n"
            )
            with open(log_path, "w") as fh:
                subprocess.run([str(self.unique), "hklout", "unq.mtz"],
                               input=unique_stdin, text=True, stdout=fh, stderr=fh)

            cad_stdin = (
                "LABI FILE 1  ALLIN\n"
                "LABI FILE 2  ALLIN\n"
                "LABI FILE 3  E1=FreeR_flag\n"
                "END\n"
            )
            with open(log_path, "a") as fh:
                subprocess.run(
                    [str(self.cad),
                     "HKLIN1", aimless_mtz,
                     "HKLIN2", "unq.mtz",
                     "HKLIN3", str(self.ref_mtz),
                     "HKLOUT", "XDS_ASCII_uniq.mtz"],
                    input=cad_stdin, text=True, stdout=fh, stderr=fh,
                )

            with open(log_path, "a") as fh:
                subprocess.run(
                    [str(self.freerflag),
                     "HKLIN", "XDS_ASCII_uniq.mtz",
                     "HKLOUT", "XDS_ASCII_rfree.mtz"],
                    input="COMPLETE FREE=FreeR_flag\nEND\n",
                    text=True, stdout=fh, stderr=fh)

            with open(log_path, "a") as fh:
                subprocess.run(
                    [str(self.mtzutils),
                     "HKLIN", "XDS_ASCII_rfree.mtz",
                     "HKLOUT", "XDS_ASCII.mtz"],
                    input="EXCLUDE FUNI SIGFUNI N(+) N(-)\nEND\n",
                    text=True, stdout=fh, stderr=fh)

        # Amplitude conversion via ctruncate (same for both branches)
        with open(log_path, "a") as fh:
            subprocess.run(
                [
                    str(self.ctruncate),
                    "-mtzin",  "XDS_ASCII.mtz",
                    "-mtzout", "XDS_ASCII_ctruncate.mtz",
                    "-colin",  "/*/*/[IMEAN,SIGIMEAN]",
                    "-colano", "/*/*/[I(+),SIGI(+),I(-),SIGI(-)]",
                    "-freein", "/*/*/[FreeR_flag]",
                ],
                stdout=fh, stderr=fh,
            )


class RunXtriage(RunBase):
    """
    Run Phenix Xtriage data quality analysis.

    Accepts unmerged XDS_ASCII.HKL from the final XDS directory and produces
    an Xtriage log reporting twinning, translational NCS, anisotropy, and
    other data pathologies.
    """

    def __init__(self, params, step_id, phenix_bin, prev_xds_id=None):
        """
        Parameters
        ----------
        params : dict
            Pipeline parameters from RunProcess.
        step_id : str
            Step identifier and output directory name (e.g. "11_xtriage").
        phenix_bin : Path
            Path to the Phenix bin directory (from _find_phenix_bin()).
        prev_xds_id : str or None
            step_id of the final XDS run to read XDS_ASCII.HKL from.
        """
        self.params          = params
        self.step_id         = step_id
        self.prev_xds_id     = prev_xds_id
        self.pipeline_subdir = Path(params.get("pipeline_subdir"))
        self.xtriage         = Path(phenix_bin) / "phenix.xtriage"

    def run(self):
        """Execute phenix.xtriage on XDS_ASCII.HKL from prev_xds_id."""
        job_dir = self.pipeline_subdir / self.step_id
        job_dir.mkdir(parents=True, exist_ok=True)
        os.chdir(job_dir)

        hkl_input = f"../{self.prev_xds_id}/XDS_ASCII.HKL"
        log_path  = job_dir / f"{self.step_id}.log"

        with open(log_path, "w") as fh:
            subprocess.run([str(self.xtriage), hkl_input], stdout=fh, stderr=fh)


class RunProcess(SGESubmitter):
    """
    Orchestrator that instantiates and executes the appropriate Run* classes.
    Submits per-dataset jobs to the Wynton cluster as SGE arrays (qsub).

    Intended to be importable and callable from other modules::

        from avicenna.pipelines.processing_runner import RunProcess
        proc = RunProcess(params)
        proc.run()

    Order of events
    ---------------
    run() -- outer loop, mirrors xds_pipeline_prep.sh:
      1.  Read symmetry entries from known_symmetry.txt (or build from params).
      2.  For each pin in params["pins"] (format "collection,dset[,data_range]"):
            - Detect up to 4 collection prefixes; skip datasets with < 180 images.
            - Compute indexing image ranges (first_half / last_half / all /
              quarters / eighths / explicit start-end).
      3.  For each (dataset x symmetry_entry x image_range) combination:
            - Create directory tree:
                {process_dir}/{collection_id}_{dset}_{prefix}/
                  {crystal_number}_{sg}_{refine_cell_integrate}/
                    index_{start}_{end}/          <- pipeline_subdir
            - Write params.json and run_pipeline.py into pipeline_subdir.
      4.  Collect all pipeline_subdir paths into xds_pipeline_index_qsub_list.txt.
      5.  Write and submit xds_pipeline_index_qsub_submit.sh as an SGE array
          (-t 1-N); each task cd's into its pipeline_subdir and calls run_pipeline.py.

    Per-dataset pipeline (executed by ProcessingPipelineInterpreter on the
    compute node):
      Normal indexing path (1_xds produces XDS_ASCII.HKL):
        1_xds   RunXDS.xds_initial_indexing
        2_pointless  RunPointless  (free SG choice)
        3_aimless    RunAimless    (no resolution cutoff)
        4_xds   RunXDS.xds_recycle_detector
        5_pointless  RunPointless  (SG from 3_aimless)
        6_aimless    RunAimless    (CC1/2 cutoff from 3_aimless)
        -- if 6_aimless CC1/2 limit improved over 3_aimless --
        7_xds   RunXDS.xds_refine_symmetry
        8_pointless  RunPointless
        9_aimless    RunAimless
        -- always --
        10_rfree   RunRfree
        11_xtriage RunXtriage

      Forced integration path (on_indexing_failure=retry; 1_xds produces
      XPARM.XDS but not XDS_ASCII.HKL):
        Submits a new SGE job per candidate SG (controlled by retry_mode).
        The forced path runs the same step sequence with xds_forced_integration
        and all Pointless steps as free runs (prev_aimless_id=None); 7_xds
        always runs (no skip check).
    """

    # One definition, in the discovery module, because discovery needs the same
    # threshold to tell a snapshot from a short collection.
    MIN_IMAGES         = MIN_IMAGES

    def __init__(self, params):
        _xds_bin, _xdsstat, _ = find_xds(root=params.get("xds_root"))
        self.XDS_EXECUTABLE     = _xds_bin / "xds_par"
        self.XDSSTAT_EXECUTABLE = _xdsstat
        self.CCP4_BIN           = find_ccp4()[0]
        self.PHENIX_BIN         = find_phenix()[0]
        self.params        = params
        self.process_dir   = Path(params.get("process_dir", os.getcwd()))
        self._output_dir   = self.process_dir / "processing"
        self._logs_dir     = self.process_dir / "processing_logs"
        self.transfer_dir  = Path(params.get("image_dir", ""))
        # Kept raw: "auto" (the default) is resolved per dataset from the frame
        # count at submit time (run); an explicit integer is used verbatim.
        self.ncpu          = params.get("ncpu", "auto")
        self.indexing_images = params.get("indexing_images", "first_half")
        self.no_symmetry   = params.get("no_symmetry", "false")
        self.skip_prescans = params.get("skip_prescans", "true") == "true"
        self.retry_mode    = params.get("retry_mode", params.get("try_alt_sym", "auto")).lower()
        # Absolute path to the src/ directory so worker scripts can import this module
        self._src_dir = str(Path(__file__).parents[3] / "src")

    # ------------------------------------------------------------------
    # Setup helpers
    # ------------------------------------------------------------------

    def _parse_crystal_sections(self):
        """
        Parse ## crystal ## sections from xds_input_parameters.txt in
        process_dir.  Returns a list of dicts (one per crystal form), each
        with keys: first_pass, sg, sg_number, cell, ref_hkl, ref_mtz.
        Missing keys default to "none".
        """
        xds_params_path = self.process_dir / "xds_input_parameters.txt"
        if not xds_params_path.is_file():
            return []
        lines = xds_params_path.read_text().splitlines()

        # Split into sections on ## crystal ## markers
        sections = []
        current = None
        for line in lines:
            if "crystal" in line and line.strip().startswith("##"):
                current = []
                sections.append(current)
            elif current is not None:
                current.append(line)

        crystals = []
        for section in sections:
            kv = {}
            for line in section:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                kv[key.strip()] = val.strip()
            sg = kv.get("sg", "none")
            try:
                sg_number = str(sg_lookup(space_group=sg)[0]) if sg != "none" else "none"
            except ValueError:
                sg_number = "none"
            crystals.append({
                "first_pass": kv.get("first_pass", "none"),
                "sg":         sg,
                "sg_number":  sg_number,
                "cell":       kv.get("cell", "none"),
                "ref_hkl":    kv.get("ref_hkl", "none"),
                "ref_mtz":    kv.get("ref_mtz", "none"),
            })
        return crystals

    def _known_symmetry_entries(self):
        """
        Return a list of symmetry-entry dicts, reading from known_symmetry.txt
        if present, otherwise parsing ## crystal ## sections from
        xds_input_parameters.txt, otherwise building a no-symmetry entry.

        Each dict has keys: crystal_number, first_pass, sg, sg_number, cell,
        ref_hkl, ref_mtz, refine_cell_integrate.
        """
        ks_path = self.process_dir / "known_symmetry.txt"
        entries = []
        if ks_path.is_file():
            for line in ks_path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = line.split(",")
                if len(parts) < 8:
                    continue
                sg = parts[2]
                try:
                    sg_number = str(sg_lookup(space_group=sg)[0]) if sg != "none" else "none"
                except ValueError:
                    sg_number = "none"
                entries.append({
                    "crystal_number":        parts[0],
                    "first_pass":            parts[1],
                    "sg":                    sg,
                    "sg_number":             sg_number,
                    "cell":                  parts[4],
                    "ref_hkl":               parts[5],
                    "ref_mtz":               parts[6],
                    "refine_cell_integrate": parts[7],
                })
        else:
            refcell = self.params.get("refine_cell_integrate", "true")
            crystal_forms = self._parse_crystal_sections()
            if crystal_forms:
                for crystal_number, form in enumerate(crystal_forms, start=1):
                    base = {"crystal_number": str(crystal_number), **form}
                    if refcell == "both":
                        entries.append({**base, "refine_cell_integrate": "true"})
                        entries.append({**base, "refine_cell_integrate": "false"})
                    else:
                        entries.append({**base, "refine_cell_integrate": refcell})
            if self.no_symmetry == "true":
                entries.append({
                    "crystal_number":        "none",
                    "first_pass":            "none",
                    "sg":                    "none",
                    "sg_number":             "none",
                    "cell":                  "none",
                    "ref_hkl":               "none",
                    "ref_mtz":               "none",
                    "refine_cell_integrate": "false",
                })
            if not entries:
                # No crystal sections and no_symmetry=false: single free run
                entries.append({
                    "crystal_number":        "none",
                    "first_pass":            "none",
                    "sg":                    "none",
                    "sg_number":             "none",
                    "cell":                  "none",
                    "ref_hkl":               "none",
                    "ref_mtz":               "none",
                    "refine_cell_integrate": "false",
                })
        return entries

    def _indexing_ranges(self, image_count, first_image):
        """
        Return list of (start, end) tuples for the requested indexing_images tokens.
        Mirrors the indexing list construction in xds_pipeline_prep.sh.
        """
        image_count = int(image_count)
        first_image = int(first_image)
        ranges = []
        for token in self.indexing_images.replace(",", " ").split():
            token = token.strip()
            if token == "first_half":
                half = (image_count + first_image) // 2
                ranges.append((first_image, half))
            elif token == "last_half":
                half = (image_count + first_image) // 2
                ranges.append((half + first_image, image_count))
            elif token == "all":
                ranges.append((first_image, image_count))
            elif token == "quarters":
                quarter = (image_count + 2) // 4
                for n in range(4):
                    start = n * quarter + first_image
                    end   = image_count if n == 3 else (n + first_image) * quarter
                    ranges.append((start, end))
            elif token == "eighths":
                eighth = (image_count + 4) // 8
                for n in range(8):
                    start = n * eighth + first_image
                    end   = image_count if n == 7 else (n + first_image) * eighth
                    ranges.append((start, end))
            elif "-" in token:
                s, e = token.split("-", 1)
                ranges.append((int(s), int(e)))
        return ranges

    def _cell_from_xparm(self, xparm_path):
        """Read unit cell constants from XPARM.XDS (line 4: SG a b c alpha beta gamma)."""
        line = Path(xparm_path).read_text().splitlines()[3]
        parts = line.split()
        # parts[0] is the SG number; parts[1:7] are a b c alpha beta gamma
        return " ".join(parts[1:7])

    def _sym_from_idxref(self, idxref_lp, retry_mode=None):
        """
        Parse IDXREF.LP and map starred Bravais lattice solutions to candidate
        space groups using BRAVAIS_LATTICES from avicenna.utils.utils.

        Returns a list of (sg_prefix, sg_name, sg_number, cell_str) tuples,
        lowest-penalty solution first (matching the tac/reverse ordering in
        the bash).
        """
        retry_mode = retry_mode if retry_mode is not None else self.retry_mode

        # Parse all starred solutions including the first aP/P1 row
        all_solutions = []
        in_table = False
        for line in Path(idxref_lp).read_text().splitlines():
            if "LATTICE-  BRAVAIS-   QUALITY" in line:
                in_table = True
                continue
            if not in_table or "*" not in line:
                continue
            clean = line.replace("*", " ").split()
            if len(clean) < 9:
                continue
            lattice = clean[1]
            cell    = " ".join(clean[3:9])
            all_solutions.append((lattice, cell))

        # IDXREF.LP lists starred rows ascending by quality penalty (aP at 0.0,
        # highest-symmetry lattices at the end).  After skipping aP and reversing,
        # solutions[0] = highest-symmetry candidate (highest penalty, tried first).
        solutions = all_solutions[1:]           # drop aP/P1 -- trivially correct, uninformative
        solutions = list(reversed(solutions))   # highest-symmetry (highest penalty) first

        if retry_mode == "top_ten":
            results = []
            prefix = 0
            for lattice, cell in solutions:
                sg_list = BRAVAIS_LATTICES.get(lattice, [])
                if sg_list:
                    number, name = sg_list[0]
                    prefix += 1
                    results.append((str(prefix), name, str(number), cell.replace(" ", "-")))
            return results
        elif retry_mode == "best":
            if not solutions:
                return []
            lattice, cell = solutions[0]    # highest-symmetry candidate
            sg_list = BRAVAIS_LATTICES.get(lattice, [])
            if sg_list:
                number, name = sg_list[0]
                return [("1", name, str(number), cell.replace(" ", "-"))]
        return []

    def _submit_sge_array(self, job_list_path, submit_script_path, h_rt="06:00:00",
                          logs_dir=None, slots=None):
        """Write and qsub an SGE array job over the paths listed in job_list_path.

        *slots* is the ``-pe smp`` request (one value for the whole array, so it
        is the max of the per-dataset XDS sizing).  When None it falls back to
        ``self.ncpu`` for an explicit override, or 1 when ncpu is 'auto'."""
        if not Path(job_list_path).is_file():
            print(f"No jobs found — {job_list_path} was not created. Check that datasets exist and have >= {self.MIN_IMAGES} images.")
            return
        lines = [l for l in Path(job_list_path).read_text().splitlines() if l.strip()]
        njob  = len(lines)
        if njob == 0:
            return
        if slots is None:
            slots = 1 if resources.is_auto(self.ncpu) else int(self.ncpu)
        self._write_sge_script(
            submit_script_path, "./run_pipeline.py",
            h_rt=h_rt, slots=slots, mem_free="2G", array=njob,
            logs_dir=logs_dir, resources=("scratch=20G",),
            preamble=(f'input_dir=$(sed "${{SGE_TASK_ID}}q;d" {job_list_path})\n'
                      "cd ${input_dir}\n"),
        )
        return qsub_terse([submit_script_path])

    # ------------------------------------------------------------------
    # Per-step dispatch helpers
    # ------------------------------------------------------------------

    def _xds(self, step_id, params, scenario,
             prev_xds_id=None, prev_aimless_id=None, prev_pointless_id=None):
        RunXDS(params, step_id, self.XDS_EXECUTABLE, self.XDSSTAT_EXECUTABLE,
               prev_xds_id=prev_xds_id, prev_aimless_id=prev_aimless_id,
               prev_pointless_id=prev_pointless_id).__getattribute__(scenario)()

    def _pointless(self, step_id, params, prev_xds_id=None, prev_aimless_id=None):
        RunPointless(params, step_id, self.CCP4_BIN,
                     prev_xds_id=prev_xds_id, prev_aimless_id=prev_aimless_id).run()

    def _aimless(self, step_id, params, prev_pointless_id=None,
                 prev_aimless_id=None, apply_res_cutoff=False):
        RunAimless(params, step_id, self.CCP4_BIN,
                   prev_pointless_id=prev_pointless_id, prev_aimless_id=prev_aimless_id,
                   apply_res_cutoff=apply_res_cutoff).run()

    def _rfree(self, step_id, params, prev_aimless_id=None):
        RunRfree(params, step_id, self.CCP4_BIN, prev_aimless_id=prev_aimless_id).run()

    def _xtriage(self, step_id, params, prev_xds_id=None):
        RunXtriage(params, step_id, self.PHENIX_BIN, prev_xds_id=prev_xds_id).run()

    def _xds_ascii_ok(self, pipeline_subdir, step_id):
        return (Path(pipeline_subdir) / step_id / "XDS_ASCII.HKL").is_file()

    def _setup_scratch(self, params):
        """Copy image directory to SGE scratch ($TMPDIR) if not already there.

        Mirrors the cp block in run_xds.sh (lines 42-53): checks for
        $TMPDIR/<image_dir_name>; copies once per job; then updates
        params["image_dir"] to the scratch path so all subsequent XDS
        runs read from fast local storage.

        No-ops silently when $TMPDIR is not set (e.g. local/test runs).
        No-ops for HDF5: data files are accessed by absolute paths embedded
        in the master file and are typically too large to copy to scratch.
        """
        if params.get('data_format') == 'hdf5':
            return
        tmpdir = os.environ.get("TMPDIR")
        if not tmpdir:
            return
        image_dir = Path(params["image_dir"])
        scratch_dir = Path(tmpdir) / image_dir.name
        pipeline_subdir = Path(params["pipeline_subdir"])
        params["original_image_dir"] = str(image_dir)
        if not scratch_dir.exists():
            self._log(pipeline_subdir, "PREP",
                      f"copying images to scratch: {image_dir} → {scratch_dir}")
            shutil.copytree(image_dir, scratch_dir)
        else:
            self._log(pipeline_subdir, "PREP",
                      f"images already in scratch: {scratch_dir}")
        params["image_dir"] = str(scratch_dir)

    def _log(self, pipeline_subdir, tag, message):
        """Append a timestamped line to pipeline.log in pipeline_subdir."""
        context = self.params.get("pin_id") if pipeline_subdir is not None else None
        forced_dir = (
            Path(pipeline_subdir).name
            if pipeline_subdir is not None
            and self.params.get("force_integration") == "true"
            else None
        )
        line = format_log_line(tag, message, context=context,
                               context_width=self.params.get("context_width"),
                               forced_dir=forced_dir)
        if pipeline_subdir is not None:
            with open(Path(pipeline_subdir) / "pipeline.log", "a") as fh:
                fh.write(line)

    def _record_rejection(self, dataset_key, reason):
        """Record a dataset rejected before submission.

        The marker is what the gather reads into info.json; the printed line
        reaches processing.log through the CLI's stdout tee, so the log and the
        record give the same reason.
        """
        rejected_dir = self._output_dir / ".rejected"
        rejected_dir.mkdir(exist_ok=True)
        (rejected_dir / f"{dataset_key}.txt").write_text(reason + "\n")
        print(log_tag(TAG_REJECTED, f"{dataset_key}: {reason}"))

    def _aimless_stats(self, log_path):
        """Parse the aimless summary block into structured values.

        Read where the dataset's identity, attempt and log file are all
        unambiguous, so nothing downstream has to re-derive them by walking
        directories and guessing which log counts.

        Per-shell rows are kept whole, in the order aimless prints them
        (overall, inner shell, outer shell), so a consumer picks the column it
        means rather than the parser choosing one for it.  An absent or
        unrecognisable log gives an empty dict, which is not an error: a step
        that did not reach scaling has nothing to report.
        """
        stats = {}
        if not Path(log_path).is_file():
            return stats
        for line in Path(log_path).read_text().splitlines()[-100:]:
            for field, label in _AIMLESS_TEXT_ROWS:
                if label in line and field not in stats:
                    value = (line.replace(label, "").replace(" ", "")
                             .strip().lstrip(":"))
                    if value:
                        stats[field] = value
            for field, label in _AIMLESS_SHELL_ROWS:
                if label in line and field not in stats:
                    numbers = numeric_tokens(line)
                    if numbers:
                        stats[field] = numbers
        return stats

    def _aimless_summary(self, log_path):
        """The one-line scaling summary for the pipeline log."""
        return format_aimless_summary(self._aimless_stats(log_path))

    def _xds_error(self, pipeline_subdir, step_id):
        """Return the first ERROR line from an XDS log, or None."""
        log = Path(pipeline_subdir) / step_id / f"{step_id}.log"
        if not log.is_file():
            return None
        for line in log.read_text().splitlines():
            if "!!! ERROR !!!" in line:
                return line.strip()
        return None

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def run(self):
        """
        Outer loop: build the directory tree and submit per-dataset SGE array jobs.
        Mirrors xds_pipeline_prep.sh.
        """
        _run_start = time.time()
        qsub_list = self.process_dir / "xds_pipeline_index_qsub_list.txt"
        processed  = self.process_dir / "pin_list_processed.txt"
        for f in (qsub_list, processed):
            if f.is_file():
                f.unlink()

        self._output_dir.mkdir(exist_ok=True)
        self._logs_dir.mkdir(exist_ok=True)

        # Wipe stale rejection markers from any prior run in this output dir.
        rejected_dir = self._output_dir / ".rejected"
        if rejected_dir.is_dir():
            shutil.rmtree(rejected_dir)

        sym_entries = self._known_symmetry_entries()
        sge_slots   = 1   # max XDS slot count across the array (see -pe smp below)

        self._context_width = ProcessingWatcher(procpath=self._output_dir).context_width

        for pin_line in self.params.get("pins", []):
            parts       = pin_line.strip().split(",")
            if len(parts) < 2:
                continue
            collection  = parts[0].strip()
            pin_id      = parts[1].strip()
            data_range  = parts[2].strip() if len(parts) > 2 else "false"
            coll_id     = collection_id(collection)
            pin_root    = Path(collection) / pin_id

            # Frame discovery is shared with the CLI, which builds info.json
            # from the same sweeps before any job exists.
            found = discover_sweeps(pin_root, skip_prescans=self.skip_prescans)
            # Logged, not recorded as a rejection: a rejection marker means a
            # candidate dataset failed a gate, and one marker per grid point
            # would bury the datasets that really did.
            for prefix, reason in found.prescans:
                print(f"Pre-scan, not a dataset — skipping {pin_id}/{prefix} ({reason})")
            if not found:
                self._record_rejection(f"{coll_id}_{pin_id}", found.reason)
                continue
            data_format = found.data_format

            # Each distinct frame series in the pin folder is one sweep (usually
            # one, sometimes more).  Dataset identity is the pin code plus a
            # 1-based sweep number (e.g. FRS046_03_1): this preserves pin
            # provenance, keeps puck positions from colliding across pins, and
            # restores the original naming convention.  The frame prefix carries
            # the beamline's own name (a puck position like C6, or an already
            # sweep-numbered stem like FRS046_03_1) and feeds image_prefix only.
            for sweep in found.sweeps():
                file_prefix = sweep.prefix
                files       = sweep.files
                image_dir   = sweep.image_dir
                series_id   = f"{pin_id}_{sweep.number}"
                tag         = dataset_tag(collection, pin_id, sweep.number)
                pin_dir_check = self._output_dir / tag
                if pin_dir_check.is_dir():
                    print(f"Output folder already exists — skipping {series_id}")
                    continue

                if data_range == "false":
                    if data_format == 'hdf5':
                        try:
                            image_count = count_hdf5_images(files[0])
                        except Exception as exc:
                            reason = f"HDF5 image count unreadable: {exc}"
                            self._record_rejection(tag, reason)
                            continue
                    else:
                        image_count = len(files)
                    first_image = 1
                else:
                    first_str, last_str = data_range.split("-")
                    first_image = int(first_str)
                    image_count = int(last_str)

                if image_count is not None and image_count < self.MIN_IMAGES:
                    reason = f"partial dataset ({image_count} images, minimum is {self.MIN_IMAGES})"
                    self._record_rejection(tag, reason)
                    continue

                pin_dir = self._output_dir / tag
                pin_dir.mkdir(exist_ok=True)

                index_ranges = self._indexing_ranges(image_count, first_image)

                # Resolve the XDS parallelism for this dataset.  auto sizes it
                # from the frame count (split into forked jobs x threads); an
                # explicit ncpu forces that many processors with one XDS job.
                # The SGE slot request (-pe smp, one value for the whole array)
                # is the max across all datasets, so no task is oversubscribed.
                if resources.is_auto(self.ncpu):
                    slots, xds_jobs, xds_procs = resources.xds_slots(image_count or 1)
                else:
                    xds_procs, xds_jobs = int(self.ncpu), 1
                    slots = xds_procs
                sge_slots = max(sge_slots, slots)

                for sym in sym_entries:
                    crystal_number        = sym["crystal_number"]
                    refine_cell_integrate = sym["refine_cell_integrate"]
                    cell_suffix = "_refined" if refine_cell_integrate == "true" else ""
                    pipeline_dir = (pin_dir / "none" if crystal_number == "none"
                                    else pin_dir / f"{crystal_number}_{sym['sg']}{cell_suffix}")
                    pipeline_dir.mkdir(exist_ok=True)

                    for start, end in index_ranges:
                        image_range     = f"{start}-{end}"
                        image_range_dir = f"{start}_{end}"
                        pipeline_subdir = pipeline_dir / f"index_{image_range_dir}"

                        if pipeline_subdir.is_dir():
                            shutil.rmtree(str(pipeline_subdir))
                        pipeline_subdir.mkdir()

                        per_params = {
                            **self.params,
                            "pin_id":                tag,
                            "context_width":         self._context_width,
                            "image_prefix":          file_prefix,
                            "image_dir":             str(image_dir),
                            "pipeline_subdir":       str(pipeline_subdir),
                            "process_dir":           str(self.process_dir),
                            "image_range":           image_range,
                            "force_integration":     "false",
                            "known_first_pass":      sym["first_pass"],
                            "known_sg":              sym["sg"],
                            "known_sg_number":       sym["sg_number"],
                            "known_cell":            sym["cell"],
                            "ref_hkl":               sym["ref_hkl"],
                            "ref_mtz":               sym["ref_mtz"],
                            "refine_cell_integrate": refine_cell_integrate,
                            "sg":                    "none",
                            "sg_number":             "none",
                            "cell":                  "none",
                            "data_range":            data_range,
                            "data_format":           data_format,
                            "ncpu":                  xds_procs,
                            "xds_jobs":              xds_jobs,
                        }
                        self._write_worker_script(pipeline_subdir, per_params, method="run_single")
                        with open(qsub_list, "a") as fh:
                            fh.write(str(pipeline_subdir) + "\n")

                with open(processed, "a") as fh:
                    fh.write(f"{tag}\n")

        submit_sh = self.process_dir / "xds_pipeline_index_qsub_submit.sh"
        job_id = self._submit_sge_array(qsub_list, submit_sh, h_rt="06:00:00",
                                        logs_dir=self._logs_dir, slots=sge_slots)
        print(f"Done in {time.time() - _run_start:.1f}s")
        return job_id

    def _run_forced_integration(self, params, pipeline_subdir, retry_mode=None):
        """
        Enumerate candidate space groups and submit a second SGE array for
        forced integration. Mirrors the XPARM.XDS branch of xds_pipeline_index_qsub.sh.
        """
        retry_mode = retry_mode if retry_mode is not None else self.retry_mode
        if retry_mode == "auto":
            retry_mode = "best"
        known_sg = params.get("known_sg", "none")
        if known_sg != "none":
            sym_to_try = [(
                "1",
                params["known_sg"],
                params["known_sg_number"],
                params.get("known_cell", "none"),
            )]
        elif retry_mode == "p1":
            xparm = pipeline_subdir / "1_xds" / "XPARM.XDS"
            cell = self._cell_from_xparm(xparm)
            sym_to_try = [("1", "P1", "1", cell)]
        else:
            sym_to_try = self._sym_from_idxref(
                pipeline_subdir / "1_xds" / "IDXREF.LP", retry_mode)

        if not sym_to_try:
            print("## No candidate lattices found in IDXREF.LP")
            return

        lattice_list = pipeline_subdir / "xds_pipeline_lattice_qsub_list.txt"
        if lattice_list.is_file():
            lattice_list.unlink()

        sg_list = []
        for sg_prefix, sg, sg_number, cell in sym_to_try:
            force_dir = pipeline_subdir / f"{sg_prefix}_{sg}_force"
            force_dir.mkdir(exist_ok=True)
            per_params = {
                **params,
                "image_dir":         params.get("original_image_dir", params["image_dir"]),
                "pipeline_subdir":   str(force_dir),
                "force_integration": "true",
                "sg":                sg,
                "sg_number":         sg_number,
                "cell":              cell,
            }
            self._write_worker_script(force_dir, per_params, method="run_single_forced")
            with open(lattice_list, "a") as fh:
                fh.write(str(force_dir) + "\n")
            sg_list.append(sg)

        self._log(pipeline_subdir, "FORCE",
                  f"submitting {len(sg_list)} job(s): {', '.join(sg_list)}")
        submit_sh = pipeline_subdir / "xds_pipeline_lattice_qsub_submit.sh"
        # Forced jobs reuse the dataset's resolved XDS sizing (baked into params);
        # the slot request is processors x jobs.
        if resources.is_auto(params.get("ncpu", "auto")):
            forced_slots = None
        else:
            forced_slots = int(params["ncpu"]) * int(params.get("xds_jobs", 1))
        self._submit_sge_array(lattice_list, submit_sh, h_rt="02:00:00",
                               logs_dir=self._logs_dir, slots=forced_slots)
