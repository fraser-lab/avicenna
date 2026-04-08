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

import os
import sys
import pathlib
import re
import shutil
import stat
import subprocess
from pathlib import Path
sys.path.insert(0, str(pathlib.Path(__file__).parents[3] / "src"))
from avicenna.utils.utils import BRAVAIS_LATTICES


class RunBase:
    """
    Generic launcher for a single data processing or analysis job.

    Responsible for assembling a command, executing it in the appropriate
    working directory, and capturing stdout/stderr.  Specific job types
    (RunXDS, RunPointless, …) subclass this and supply the command details.
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

    The four processing scenarios, each implemented as a method, mirror the
    branching logic in run_xds.sh:

        xds_initial_indexing   -- job 1, force_integration=False (default)
        xds_forced_integration -- job 1, force_integration=True
        xds_recycle_detector   -- job 2
        xds_refine_symmetry    -- jobs 5 / 8
    """

    def __init__(self, params, job, xds_executable, xdsstat_executable):
        """
        Parameters
        ----------
        params : dict
            Pipeline parameters from vic_submit.py / RunProcess.
        job : int
            Sequential job number within the pipeline (1, 2, 5, or 8).
        xds_executable : str or Path
            Full path to the xds_par binary.
        xdsstat_executable : str or Path
            Full path to the xdsstat binary.
        """
        self.params = params
        self.job = job
        self.xds_executable    = Path(xds_executable)
        self.xdsstat_executable = Path(xdsstat_executable)

        # Convenience aliases derived from params
        self.dset              = params.get("dset")
        self.image_dir         = Path(params.get("image_dir"))
        self.script_dir        = Path(params.get("script_dir"))
        self.pipeline_subdir   = Path(params.get("pipeline_subdir"))
        self.image_range       = params.get("image_range", "").replace("-", " ")
        self.ncpu              = params.get("ncpu", 8)
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
        self.recycle_xds       = params.get("recycle_xds", "false")
        self.refine_cell_integrate = params.get("refine_cell_integrate", "true")
        self.detector_edge     = float(params.get("detector_edge", 0.8))
        self.res_limit         = params.get("res_limit", "cc12")
        self.data_range        = params.get("data_range", "false").replace("-", " ")

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _run_xds(self):
        """Execute xds_par in the current directory, writing log to <job>_xds.log."""
        log = Path(f"{self.job}_xds.log")
        with open(log, "w") as fh:
            subprocess.run([str(self.xds_executable)], stdout=fh, stderr=fh)

    def _run_xdsstat(self):
        """Run xdsstat on XDS_ASCII.HKL if it was produced, writing <job>_xdsstat.log."""
        if Path("XDS_ASCII.HKL").is_file():
            log = Path(f"{self.job}_xdsstat.log")
            with open(log, "w") as fh:
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
            True when this is job 2: the divergence/range parameters are
            *inserted* after DETECTOR_DISTANCE rather than overwriting
            existing lines.
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

    # ------------------------------------------------------------------
    # Scenario methods
    # ------------------------------------------------------------------

    def xds_initial_indexing(self):
        """
        Default first-pass XDS run (job 1, force_integration=False).

        Generates XDS.INP from scratch using the generate_XDS.INP helper
        script, then runs the full XYCORR → INIT → COLSPOT → IDXREF →
        DEFPIX → INTEGRATE → CORRECT pipeline.  Optionally seeds XDS.INP
        with a known space group and unit cell for constrained indexing,
        and injects a reference HKL for isomorphism-guided integration.
        Corresponds to the ``job == 1 and force_integration == false``
        branch in run_xds.sh.
        """
        job_dir = self.pipeline_subdir / f"{self.job}_xds"
        job_dir.mkdir(parents=True, exist_ok=True)
        os.chdir(job_dir)

        # Generate XDS.INP from the template script
        generate_inp = job_dir / "generate_XDS.INP"
        template = (self.script_dir / "generate_XDS.INP").read_text()
        generate_inp.write_text(template.replace("sed_replace", self.image_range))
        generate_inp.chmod(generate_inp.stat().st_mode | stat.S_IXUSR | stat.S_IRUSR | stat.S_IWUSR)
        subprocess.run([str(generate_inp),
                        str(self.image_dir / f"{self.dset}_1_0????.cbf")])

        xds_inp = job_dir / "XDS.INP"
        text = xds_inp.read_text()

        # Set number of processors
        text = text.replace("MAXIMUM_NUMBER_OF_PROCESSORS=1",
                            f"MAXIMUM_NUMBER_OF_PROCESSORS={self.ncpu}")

        # Override data range if specified
        if self.data_range != "false":
            text = re.sub(r"DATA_RANGE=.*", f"DATA_RANGE={self.data_range}", text)

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
        self._run_xds()
        self._run_xdsstat()

    def xds_forced_integration(self):
        """
        Force integration using geometry from a prior XPARM.XDS (job 1, force_integration=True).

        Skips the spot-finding and indexing steps (XYCORR, INIT, COLSPOT,
        IDXREF) and runs only DEFPIX → INTEGRATE → CORRECT, seeding the
        geometry from the XPARM.XDS produced during the very first XDS run
        (``../../1_xds``).  Used when auto-indexing fails but a good initial
        geometry estimate is already available from a previous dataset.
        Corresponds to the ``job == 1 and force_integration == true`` branch
        in run_xds.sh.
        """
        job_dir = self.pipeline_subdir / f"{self.job}_xds"
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
        text = re.sub(r"UNIT_CELL_CONSTANTS=.*!",
                      f"UNIT_CELL_CONSTANTS= {self.cell} !", text)
        text = text.replace(
            "JOB= XYCORR INIT COLSPOT IDXREF DEFPIX INTEGRATE CORRECT",
            "JOB= DEFPIX INTEGRATE CORRECT",
        )
        text = re.sub(r"NAME_TEMPLATE_OF_DATA_FRAMES=.*",
                      f"NAME_TEMPLATE_OF_DATA_FRAMES={self.image_dir}/{self.dset}_1_0????.cbf",
                      text)
        (job_dir / "XDS.INP").write_text(text)

        shutil.copy(str(xparm), "XPARM.XDS")
        for cbf in xds_dir.glob("*.cbf"):
            shutil.copy(str(cbf), ".")

        self._run_xds()
        self._run_xdsstat()

    def xds_recycle_detector(self):
        """
        Second XDS run recycling refined detector geometry (job 2).

        Copies XDS.INP and geometry files from the first XDS run, reduces
        the job list to DEFPIX → INTEGRATE → CORRECT, and injects refined
        detector parameters (ROTATION_AXIS, INCIDENT_BEAM_DIRECTION, ORGX,
        DETECTOR_DISTANCE, BEAM_DIVERGENCE, REFLECTING_RANGE) extracted from
        the XDS_ASCII.HKL and INTEGRATE.LP outputs of job 1.  No symmetry
        update is performed at this stage.
        Corresponds to the ``job == 2`` branch in run_xds.sh.
        """
        last_xds_job = self.job - 1
        xds_dir = self.pipeline_subdir / f"{last_xds_job}_xds"

        job_dir = self.pipeline_subdir / f"{self.job}_xds"
        job_dir.mkdir(parents=True, exist_ok=True)
        os.chdir(job_dir)

        shutil.copy(str(xds_dir / "XDS.INP"), "XDS.INP")
        for cbf in xds_dir.glob("*.cbf"):
            shutil.copy(str(cbf), ".")
        shutil.copy(str(xds_dir / "XPARM.XDS"), "XPARM.XDS")

        if self.recycle_xds == "true":
            self._recycle_detector_params(xds_dir, job_dir / "XDS.INP", is_job2=True)

        self._run_xds()
        self._run_xdsstat()

    def xds_refine_symmetry(self):
        """
        Third (and optional fourth) XDS run with updated symmetry and resolution (jobs 5 / 8).

        Copies XDS.INP and geometry files from the previous XDS run, recycles
        refined detector parameters (same as xds_recycle_detector), and
        additionally updates SPACE_GROUP_NUMBER, UNIT_CELL_CONSTANTS, and
        INCLUDE_RESOLUTION_RANGE using values parsed from the most recent
        Aimless and Pointless log files.  The resolution limit is derived from
        the CC½ criterion or a completeness threshold, then clipped to the
        detector edge.
        Corresponds to the ``job == 5`` and ``job == 8`` branches in run_xds.sh.
        """
        # Determine which previous XDS directory to use
        if self.job == 5:
            last_xds_job = (self.job - 1) - 2 if self.recycle_xds == "true" else (self.job - 1) - 3
        elif self.job == 8:
            last_xds_job = (self.job - 1) - 2
        else:
            raise ValueError(f"xds_refine_symmetry does not apply to job {self.job}")

        last_job        = self.job - 1
        second_last_job = self.job - 2
        xds_dir         = self.pipeline_subdir / f"{last_xds_job}_xds"
        aimless_log     = self.pipeline_subdir / f"{last_job}_aimless" / f"{last_job}_aimless.log"
        pointless_log   = (self.pipeline_subdir / f"{second_last_job}_pointless"
                           / f"{second_last_job}_pointless.log")

        job_dir = self.pipeline_subdir / f"{self.job}_xds"
        job_dir.mkdir(parents=True, exist_ok=True)
        os.chdir(job_dir)

        shutil.copy(str(xds_dir / "XDS.INP"), "XDS.INP")
        for cbf in xds_dir.glob("*.cbf"):
            shutil.copy(str(cbf), ".")
        shutil.copy(str(xds_dir / "XPARM.XDS"), "XPARM.XDS")

        xds_inp = job_dir / "XDS.INP"

        if self.recycle_xds == "true":
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

    def __init__(self, params, job):
        """
        Parameters
        ----------
        params : dict
            Pipeline parameters from RunProcess.
        job : int
            Sequential job number within the pipeline (e.g. 3, 6, 9).
        """
        self.params      = params
        self.job         = job
        self.sg          = params.get("sg", "none")
        self.scaling     = params.get("scaling", "correct")
        self.recycle_xds = params.get("recycle_xds", "false")
        self.pipeline_subdir = Path(params.get("pipeline_subdir"))

    def run(self):
        """
        Execute Pointless for this job.

        Input file is INTEGRATE.HKL when scaling will be done by Aimless, or
        XDS_ASCII.HKL when XDS CORRECT already scaled the data.  The previous
        XDS directory is job-1, except when recycle_xds is false and this is
        job 3, in which case job-2 is used (no second XDS run was performed).

        Space group handling:
          - job 3, sg=="none": let Pointless choose freely (first pass, no prior)
          - job >3, sg=="none": read the space group decided by the previous
            Aimless run from its log and enforce it
          - sg provided: enforce the supplied space group unconditionally
        """
        job_dir = self.pipeline_subdir / f"{self.job}_pointless"
        job_dir.mkdir(parents=True, exist_ok=True)
        os.chdir(job_dir)

        pointless_input = "INTEGRATE.HKL" if self.scaling == "aimless" else "XDS_ASCII.HKL"

        # Which XDS directory to read from
        if self.recycle_xds == "false" and self.job == 3:
            last_xds_job = self.job - 2
        else:
            last_xds_job = self.job - 1

        hklin = f"../{last_xds_job}_xds/{pointless_input}"
        hklout = f"XDS_ASCII_{self.job}_pointless.mtz"
        log_path = job_dir / f"{self.job}_pointless.log"

        sg = self.sg
        if sg == "none":
            if self.job == 3:
                # First Pointless pass: no space group constraint
                stdin = (
                    f"HKLIN {hklin}\n"
                    f"HKLOUT {hklout}\n"
                    "SETTING SYMMETRY-BASED\n"
                    "END\n"
                )
            else:
                # Read space group chosen by the previous Aimless run
                last_aimless_job = last_xds_job - 1
                aimless_log = (self.pipeline_subdir
                               / f"{last_aimless_job}_aimless"
                               / f"{last_aimless_job}_aimless.log")
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
        else:
            stdin = (
                f"HKLIN {hklin}\n"
                f"HKLOUT {hklout}\n"
                "SETTING SYMMETRY-BASED\n"
                f"SPACEGROUP {sg}\n"
                "END\n"
            )

        with open(log_path, "w") as fh:
            subprocess.run(["pointless"], input=stdin, text=True, stdout=fh, stderr=fh)


class RunAimless(RunBase):
    """
    Create and run an Aimless scaling and merging job.

    Accepts merged MTZ output from Pointless and applies scaling/merging via
    Aimless.  On the first pass no resolution cutoff is applied; on subsequent
    passes the cutoff is derived from CC1/2 or completeness statistics
    extracted from the previous Aimless log, subject to detector-edge and
    user-supplied limits.
    """

    def __init__(self, params, job):
        """
        Parameters
        ----------
        params : dict
            Pipeline parameters from RunProcess.
        job : int
            Sequential job number within the pipeline (e.g. 4, 7, 10).
        """
        self.params          = params
        self.job             = job
        self.detector_edge   = float(params.get("detector_edge", 0.8))
        self.res_limit       = params.get("res_limit", "cc12")
        self.custom_res      = params.get("custom_res", "false")
        self.pipeline_subdir = Path(params.get("pipeline_subdir"))

    def run(self):
        """
        Execute Aimless for this job.

        Job 4 is the first Aimless pass: no resolution cutoff is applied so
        that the full resolution range is merged and statistics can be used to
        determine the cutoff for subsequent runs.

        For later jobs the resolution limit is derived from the previous Aimless
        log using either the CC½ > 0.30 criterion or the last shell where
        completeness exceeds a numeric threshold, whichever is less restrictive.
        The limit is then clipped to the detector edge (never finer than the
        physical limit) and to any user-supplied custom resolution.
        """
        last_job = self.job - 1
        job_dir  = self.pipeline_subdir / f"{self.job}_aimless"
        job_dir.mkdir(parents=True, exist_ok=True)
        os.chdir(job_dir)

        hklin  = f"../{last_job}_pointless/XDS_ASCII_{last_job}_pointless.mtz"
        hklout = f"XDS_ASCII_{self.job}_aimless.mtz"
        log_path = job_dir / f"{self.job}_aimless.log"

        if self.job == 4:
            # First pass: merge without resolution cutoff
            stdin = (
                f"HKLIN {hklin}\n"
                f"HKLOUT {hklout}\n"
                "onlymerge\n"
                "END\n"
            )
        else:
            last_aimless_job = last_job - 2
            aimless_log = (self.pipeline_subdir
                           / f"{last_aimless_job}_aimless"
                           / f"{last_aimless_job}_aimless.log")
            aim_text = aimless_log.read_text()

            # CC½-based limit
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
            subprocess.run(["aimless"], input=stdin, text=True, stdout=fh, stderr=fh)


class RunRfree(RunBase):
    """
    Generate R-free flags and calculate structure-factor amplitudes.

    Accepts scaled MTZ output from Aimless and either generates a fresh set
    of R-free flags (freerflag) or copies flags from a reference MTZ
    (freerflag COMPLETE).  Amplitude conversion is performed by ctruncate.
    """

    def __init__(self, params, job):
        """
        Parameters
        ----------
        params : dict
            Pipeline parameters from RunProcess.
        job : int
            Sequential job number within the pipeline.
        """
        self.params          = params
        self.job             = job
        self.ref_mtz         = params.get("ref_mtz", "none")
        self.pipeline_subdir = Path(params.get("pipeline_subdir"))

    def run(self):
        """
        Generate R-free flags and convert intensities to amplitudes.

        The last Aimless job is job-1; if that directory does not exist (the
        third XDS pass was skipped) job-4 is used instead.  Cell, space group,
        and resolution are read from the Aimless log.

        Without a reference MTZ: freerflag assigns a fresh random 5% test set,
        mtzutils strips anomalous columns, and ctruncate converts I → F.

        With a reference MTZ: unique builds a complete set of Miller indices,
        cad merges the reference FreeR_flag column in, freerflag COMPLETE
        extends the flags to the full resolution range, mtzutils strips
        unwanted columns, and ctruncate converts I → F.
        """
        last_job = self.job - 1
        job_dir  = self.pipeline_subdir / f"{self.job}_rfree"
        job_dir.mkdir(parents=True, exist_ok=True)
        os.chdir(job_dir)

        # Fall back to job-4 if the expected Aimless directory is absent
        if not (self.pipeline_subdir / f"{last_job}_aimless").is_dir():
            last_job = self.job - 4

        aimless_log = (self.pipeline_subdir
                       / f"{last_job}_aimless"
                       / f"{last_job}_aimless.log")
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

        log_path  = job_dir / f"{self.job}_rfree.log"
        aimless_mtz = f"../{last_job}_aimless/XDS_ASCII_{last_job}_aimless.mtz"

        if self.ref_mtz in (None, "none", ""):
            # Fresh R-free flags
            freerflag_stdin = (
                f"HKLIN {aimless_mtz}\n"
                "HKLOUT XDS_ASCII_rfree.mtz\n"
                "UNIQUE\n"
                "END\n"
            )
            with open(log_path, "w") as fh:
                subprocess.run(["freerflag"], input=freerflag_stdin,
                               text=True, stdout=fh, stderr=fh)

            mtzutils_stdin = (
                "HKLIN XDS_ASCII_rfree.mtz\n"
                "HKLOUT XDS_ASCII.mtz\n"
                "EXCLUDE N(+) N(-)\n"
                "END\n"
            )
            with open(log_path, "a") as fh:
                subprocess.run(["mtzutils"], input=mtzutils_stdin,
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
                subprocess.run(["unique", "hklout", "unq.mtz"],
                               input=unique_stdin, text=True, stdout=fh, stderr=fh)

            cad_stdin = (
                f"LABI FILE 1  ALLIN\n"
                f"LABI FILE 2  ALLIN\n"
                f"LABI FILE 3  E1=FreeR_flag\n"
                "END\n"
            )
            with open(log_path, "a") as fh:
                subprocess.run(
                    ["cad",
                     "HKLIN1", aimless_mtz,
                     "HKLIN2", "unq.mtz",
                     "HKLIN3", str(self.ref_mtz),
                     "HKLOUT", "XDS_ASCII_uniq.mtz"],
                    input=cad_stdin, text=True, stdout=fh, stderr=fh,
                )

            freerflag_stdin = (
                "HKLIN XDS_ASCII_uniq.mtz\n"
                "HKLOUT XDS_ASCII_rfree.mtz\n"
                "COMPLETE FREE=FreeR_flag\n"
                "END\n"
            )
            with open(log_path, "a") as fh:
                subprocess.run(["freerflag"], input=freerflag_stdin,
                               text=True, stdout=fh, stderr=fh)

            mtzutils_stdin = (
                "HKLIN XDS_ASCII_rfree.mtz\n"
                "HKLOUT XDS_ASCII.mtz\n"
                "EXCLUDE FUNI SIGFUNI N(+) N(-)\n"
                "END\n"
            )
            with open(log_path, "a") as fh:
                subprocess.run(["mtzutils"], input=mtzutils_stdin,
                               text=True, stdout=fh, stderr=fh)

        # Amplitude conversion via ctruncate (same for both branches)
        with open(log_path, "a") as fh:
            subprocess.run(
                [
                    "ctruncate",
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

    def __init__(self, params, job):
        """
        Parameters
        ----------
        params : dict
            Pipeline parameters from RunProcess.
        job : int
            Sequential job number within the pipeline.
        """
        self.params          = params
        self.job             = job
        self.pipeline_subdir = Path(params.get("pipeline_subdir"))

    def run(self):
        """
        Execute phenix.xtriage on the final XDS_ASCII.HKL.

        The last XDS directory is job-5; if that directory is absent (the
        third XDS pass was not run) job-8 is used instead.
        """
        last_xds_job = self.job - 5
        if not (self.pipeline_subdir / f"{last_xds_job}_xds").is_dir():
            last_xds_job = self.job - 8

        job_dir = self.pipeline_subdir / f"{self.job}_xtriage"
        job_dir.mkdir(parents=True, exist_ok=True)
        os.chdir(job_dir)

        hkl_input = f"../{last_xds_job}_xds/XDS_ASCII.HKL"
        log_path  = job_dir / f"{self.job}_xtriage.log"

        with open(log_path, "w") as fh:
            subprocess.run(["phenix.xtriage", hkl_input], stdout=fh, stderr=fh)


class RunProcess:
    """
    Orchestrator that instantiates and executes the appropriate Run* classes.
    Submits per-dataset jobs to the Wynton cluster as SGE arrays (qsub).

    Intended to be importable and callable from other modules::

        from avicenna.pipelines.proc_pipeline import RunProcess
        proc = RunProcess(params)
        proc.run()

    Order of events
    ---------------
    run() — outer loop, mirrors xds_pipeline_prep.sh:
      1.  Read symmetry entries from known_symmetry.txt (or build from params).
      2.  For each pin in params["pins"] (format "collection,dset[,data_range]"):
            - Detect up to 4 collection prefixes; skip datasets with < 180 images.
            - Compute indexing image ranges (first_half / last_half / all /
              quarters / eighths / explicit start-end).
      3.  For each (dataset × symmetry_entry × image_range) combination:
            - Create directory tree:
                {process_dir}/{collection_id}_{dset}_{prefix}/
                  {crystal_number}_{sg}_{refine_cell_integrate}/
                    index_{start}_{end}/          ← pipeline_subdir
            - Write params.json and run_pipeline.py into pipeline_subdir.
      4.  Collect all pipeline_subdir paths into xds_pipeline_index_qsub_list.txt.
      5.  Write and submit xds_pipeline_index_qsub_submit.sh as an SGE array
          (-t 1-N); each task cd's into its pipeline_subdir and calls run_pipeline.py.

    run_single() — sequential per-dataset pipeline, mirrors xds_pipeline_index_qsub.sh:
      Normal indexing path (job 1 produces XDS_ASCII.HKL):
        job  1  RunXDS.xds_initial_indexing
        job  2  RunXDS.xds_recycle_detector    [only if recycle_xds=true]
        job  3  RunPointless
        job  4  RunAimless  (no resolution cutoff — first-pass statistics)
        job  5  RunXDS.xds_refine_symmetry
        job  6  RunPointless
        job  7  RunAimless  (CC½ / completeness cutoff applied)
        -- if 7_aimless CC½ limit is finer than 5_xds INCLUDE_RESOLUTION_RANGE --
        job  8  RunXDS.xds_refine_symmetry
        job  9  RunPointless
        job 10  RunAimless
        -- always --
        job 11  RunRfree
        job 13  RunXtriage

      Forced integration path (job 1 produces XPARM.XDS but not XDS_ASCII.HKL):
        - Parse IDXREF.LP for starred Bravais lattice solutions; map to space
          groups via xds_lattices.txt.
          (try_alt_sym=all: every starred lattice; try_alt_sym=top: best only;
           known_sg supplied: use it directly, skip IDXREF.LP.)
        - For each candidate SG create pipeline_subdir/{prefix}_{sg}_force/
          and submit a second SGE array (xds_pipeline_lattice_qsub_submit.sh,
          h_rt=02:00:00):
            job  1  RunXDS.xds_forced_integration
            job  2  RunXDS.xds_recycle_detector   [only if recycle_xds=true]
            job  3  RunPointless
            job  4  RunAimless
            job  5  RunXDS.xds_refine_symmetry
            job  6  RunPointless
            job  7  RunAimless
            job  8  RunXDS.xds_refine_symmetry    (always — no convergence check)
            job  9  RunPointless
            job 10  RunAimless
            job 11  RunRfree
            job 13  RunXtriage
    """

    #TODO: make these executable paths configurable via params; consider a settings file for the whole pipeline
    XDS_EXECUTABLE     = Path("/programs/x86_64-linux/system/sbgrid_bin/xds_par")
    XDSSTAT_EXECUTABLE = Path("/programs/x86_64-linux/system/sbgrid_bin/xdsstat")
    MIN_IMAGES = 180

    def __init__(self, params):
        self.params        = params
        self.process_dir   = Path(params.get("process_dir", os.getcwd()))
        self.transfer_dir  = Path(params.get("image_dir", ""))
        self.script_dir    = Path(params.get("script_dir", ""))
        self.ncpu          = int(params.get("ncpu", 8))
        self.indexing_images = params.get("indexing_images", "first_half")
        self.no_symmetry   = params.get("no_symmetry", "false")
        self.try_alt_sym   = params.get("try_alt_sym", "all")
        self.recycle_xds   = params.get("recycle_xds", "false")
        # Absolute path to the src/ directory so worker scripts can import this module
        self._src_dir = str(Path(__file__).parents[3] / "src")

    # ------------------------------------------------------------------
    # Setup helpers
    # ------------------------------------------------------------------

    def _known_symmetry_entries(self):
        """
        Return a list of symmetry-entry dicts, reading from known_symmetry.txt
        if present, otherwise building a single entry from params.

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
                entries.append({
                    "crystal_number":        parts[0],
                    "first_pass":            parts[1],
                    "sg":                    parts[2],
                    "sg_number":             parts[3],
                    "cell":                  parts[4],
                    "ref_hkl":               parts[5],
                    "ref_mtz":               parts[6],
                    "refine_cell_integrate": parts[7],
                })
        elif self.no_symmetry == "true":
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
        else:
            refcell = self.params.get("refine_cell_integrate", "true")
            base = {
                "crystal_number":        "1",
                "first_pass":            self.params.get("first_pass", "none"),
                "sg":                    self.params.get("sg", "none"),
                "sg_number":             self.params.get("number", "none"),
                "cell":                  self.params.get("cell", "none"),
                "ref_hkl":               str(self.params.get("ref_hkl") or "none"),
                "ref_mtz":               str(self.params.get("ref_mtz") or "none"),
            }
            if refcell == "both":
                entries.append({**base, "refine_cell_integrate": "true"})
                entries.append({**base, "refine_cell_integrate": "false"})
            else:
                entries.append({**base, "refine_cell_integrate": refcell})
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

    def _sym_from_idxref(self, idxref_lp):
        """
        Parse IDXREF.LP and map starred Bravais lattice solutions to candidate
        space groups using BRAVAIS_LATTICES from avicenna.utils.utils.

        Returns a list of (sg_prefix, sg_name, sg_number, cell_str) tuples,
        lowest-penalty solution first (matching the tac/reverse ordering in
        the bash).
        """
        # Parse starred solutions
        solutions = []
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
            solutions.append((lattice, cell))
        solutions = list(reversed(solutions))   # lowest penalty first

        if self.try_alt_sym == "all":
            results = []
            prefix = 0
            for lattice, cell in solutions:
                sg_list = BRAVAIS_LATTICES.get(lattice, [])
                if sg_list:
                    number, name = sg_list[0]
                    prefix += 1
                    results.append((str(prefix), name, str(number), cell.replace(" ", "-")))
            return results
        elif self.try_alt_sym == "top":
            if not solutions:
                return []
            lattice, cell = solutions[-1]   # best before reversal
            sg_list = BRAVAIS_LATTICES.get(lattice, [])
            if sg_list:
                number, name = sg_list[0]
                return [("1", name, str(number), cell.replace(" ", "-"))]
        return []

    def _write_worker_script(self, pipeline_subdir, per_params, method="run_single"):
        """
        Serialise per_params to params.json and write a run_pipeline.py that
        imports this module and calls RunProcess(params).{method}().
        """
        import json
        Path(pipeline_subdir, "params.json").write_text(
            json.dumps(per_params, indent=2, default=str)
        )
        script = Path(pipeline_subdir) / "run_pipeline.py"
        script.write_text(f"""\
#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path
sys.path.insert(0, {self._src_dir!r})
from avicenna.pipelines.proc_pipeline import RunProcess
                          
shell = os.environ.get('SHELL', '/bin/bash')
if any(s in shell for s in ('csh', 'tcsh')):
    _sbgrid_cmd = "tcsh -c 'source /programs/sbgrid.cshrc && env'"
else:
    _sbgrid_cmd = "bash --norc --noprofile -c 'source /programs/sbgrid.shrc && env'"
_result = subprocess.run(_sbgrid_cmd, shell=True, capture_output=True, text=True)
for _line in _result.stdout.splitlines():
    if '=' in _line:
        _key, _, _val = _line.partition('=')
        if _key.isidentifier():
            os.environ[_key] = _val

params = json.loads(Path('params.json').read_text())
print("=" * 60)
print(f"pin          : {{params.get('pin', 'n/a')}}")
print(f"dataset      : {{params.get('dset', 'n/a')}}")
print(f"image_dir    : {{params.get('image_dir', 'n/a')}}")
print(f"image_range  : {{params.get('image_range', 'n/a')}}")
print(f"workdir      : {{params.get('pipeline_subdir', 'n/a')}}")
print(f"space_group  : {{params.get('sg', 'n/a')}}")
print(f"cell         : {{params.get('cell', 'n/a')}}")
print("=" * 60, flush=True)
RunProcess(params).{method}()
""")
        script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IRUSR | stat.S_IWUSR)

    def _submit_sge_array(self, job_list_path, submit_script_path, h_rt="06:00:00"):
        """Write and qsub an SGE array job over the paths listed in job_list_path."""
        if not Path(job_list_path).is_file():
            print(f"No jobs found — {job_list_path} was not created. Check that datasets exist and have >= {self.MIN_IMAGES} images.")
            return
        lines = [l for l in Path(job_list_path).read_text().splitlines() if l.strip()]
        njob  = len(lines)
        if njob == 0:
            return
        submit_script_path.write_text(
            "#!/bin/bash\n"
            "#$ -S /bin/bash\n"
            "#$ -cwd\n"
            f"#$ -t 1-{njob}\n"
            "#$ -j y\n"
            f"#$ -pe smp {self.ncpu}\n"
            "#$ -l mem_free=2G\n"
            f"#$ -l h_rt={h_rt}\n"
            "#$ -l scratch=20G\n\n"
            f'input_dir=$(sed "${{SGE_TASK_ID}}q;d" {job_list_path})\n'
            "cd ${input_dir}\n"
            "./run_pipeline.py\n"
        )
        submit_script_path.chmod(
            submit_script_path.stat().st_mode | stat.S_IXUSR | stat.S_IRUSR | stat.S_IWUSR
        )
        subprocess.run(["qsub", str(submit_script_path)])

    # ------------------------------------------------------------------
    # Per-step dispatch helpers
    # ------------------------------------------------------------------

    def _xds(self, job, params, scenario):
        RunXDS(params, job, self.XDS_EXECUTABLE, self.XDSSTAT_EXECUTABLE).__getattribute__(scenario)()

    def _pointless(self, job, params):
        RunPointless(params, job).run()

    def _aimless(self, job, params):
        RunAimless(params, job).run()

    def _rfree(self, job, params):
        RunRfree(params, job).run()

    def _xtriage(self, job, params):
        RunXtriage(params, job).run()

    def _xds_ascii_ok(self, pipeline_subdir, job):
        return (Path(pipeline_subdir) / f"{job}_xds" / "XDS_ASCII.HKL").is_file()

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def run(self):
        """
        Outer loop: build the directory tree and submit per-dataset SGE array jobs.
        Mirrors xds_pipeline_prep.sh.
        """
        qsub_list = self.process_dir / "xds_pipeline_index_qsub_list.txt"
        processed  = self.process_dir / "pin_list_processed.txt"
        for f in (qsub_list, processed):
            if f.is_file():
                f.unlink()

        sym_entries = self._known_symmetry_entries()

        for pin_line in self.params.get("pins", []):
            parts       = pin_line.strip().split(",")
            if len(parts) < 2:
                continue
            collection  = parts[0].strip()
            dset        = parts[1].strip()
            data_range  = parts[2].strip() if len(parts) > 2 else "false"
            collection_id = Path(collection).name.replace("_transfer", "")

            for prefix in range(1, 5):
                image_dir = Path(collection) / dset
                if not (image_dir / f"{dset}_{prefix}_00100.cbf").is_file():
                    continue

                if data_range == "false":
                    image_count = len(list(image_dir.glob(f"{dset}_{prefix}_*.cbf")))
                    first_image = 1
                else:
                    first_str, last_str = data_range.split("-")
                    first_image = int(first_str)
                    image_count = int(last_str)

                if image_count < self.MIN_IMAGES:
                    print(f"Partial dataset ({image_count} images) — skipping {dset}_{prefix}")
                    continue

                pin_dir = self.process_dir / f"{collection_id}_{dset}_{prefix}"
                pin_dir.mkdir(exist_ok=True)

                index_ranges = self._indexing_ranges(image_count, first_image)

                for sym in sym_entries:
                    crystal_number       = sym["crystal_number"]
                    refine_cell_integrate = sym["refine_cell_integrate"]
                    pipeline_dir = (pin_dir / "none" if crystal_number == "none"
                                    else pin_dir / f"{crystal_number}_{sym['sg']}_{refine_cell_integrate}")
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
                            "dset":                  dset,
                            "image_dir":             str(image_dir),
                            "pipeline_subdir":       str(pipeline_subdir),
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
                        }
                        self._write_worker_script(pipeline_subdir, per_params, method="run_single")
                        with open(qsub_list, "a") as fh:
                            fh.write(str(pipeline_subdir) + "\n")

                with open(processed, "a") as fh:
                    fh.write(f"{collection_id}_{dset}_{prefix}\n")

        submit_sh = self.process_dir / "xds_pipeline_index_qsub_submit.sh"
        self._submit_sge_array(qsub_list, submit_sh, h_rt="06:00:00")

    def run_single(self):
        """
        Sequential pipeline for one dataset. Called by run_pipeline.py on a
        compute node. Mirrors xds_pipeline_index_qsub.sh.
        """
        params          = self.params
        pipeline_subdir = Path(params["pipeline_subdir"])

        # --- job 1: initial indexing ---
        self._xds(1, params, "xds_initial_indexing")

        if self._xds_ascii_ok(pipeline_subdir, 1):
            job = 1
            if params.get("recycle_xds") == "true":
                print("## job 1 successful, re-running with recycled detector params")
                self._xds(2, params, "xds_recycle_detector")
                job = 2

            if self._xds_ascii_ok(pipeline_subdir, job):
                print(f"## job {job} successful, running Pointless / Aimless / XDS")
                self._pointless(3, params)
                self._aimless(4, params)
                self._xds(5, params, "xds_refine_symmetry")

                if self._xds_ascii_ok(pipeline_subdir, 5):
                    print("## job 5 successful, running Pointless / Aimless")
                    self._pointless(6, params)
                    self._aimless(7, params)

                    # Third XDS round only if resolution improved after job 7
                    aim7_log = pipeline_subdir / "7_aimless" / "7_aimless.log"
                    xds5_inp = pipeline_subdir / "5_xds" / "XDS.INP"
                    res_lim_7 = next(
                        (l.split("=", 1)[1].split("A")[0].strip()
                         for l in aim7_log.read_text().splitlines()
                         if "0.30: limit =" in l),
                        "0.00",
                    )
                    res_lim_5 = next(
                        (l.split()[1]
                         for l in xds5_inp.read_text().splitlines()
                         if "INCLUDE_RESOLUTION_RANGE=" in l),
                        "0.00",
                    )

                    if float(res_lim_7) > float(res_lim_5):
                        print("## Resolution improved, running third XDS round")
                        self._xds(8, params, "xds_refine_symmetry")
                        if self._xds_ascii_ok(pipeline_subdir, 8):
                            self._pointless(9, params)
                            self._aimless(10, params)
                        else:
                            print("## job 8 failed. Check logs.")

                    print("## Running Rfree / Xtriage")
                    self._rfree(11, params)
                    self._xtriage(13, params)
                else:
                    print("## job 5 failed. Check logs.")
            else:
                print(f"## job {job} failed. Check logs.")

        elif (pipeline_subdir / "1_xds" / "XPARM.XDS").is_file():
            print("## Indexing failed; trying forced integration from IDXREF.LP solutions")
            self._run_forced_integration(params, pipeline_subdir)
        else:
            print("## XDS did not attempt to index. Check images and logs.")

    def _run_forced_integration(self, params, pipeline_subdir):
        """
        Enumerate candidate space groups and submit a second SGE array for
        forced integration. Mirrors the XPARM.XDS branch of xds_pipeline_index_qsub.sh.
        """
        known_sg = params.get("known_sg", "none")
        if known_sg != "none":
            sym_to_try = [(
                "1",
                params["known_sg"],
                params["known_sg_number"],
                params.get("known_cell", "none"),
            )]
        else:
            sym_to_try = self._sym_from_idxref(pipeline_subdir / "1_xds" / "IDXREF.LP")

        if not sym_to_try:
            print("## No candidate lattices found in IDXREF.LP")
            return

        lattice_list = pipeline_subdir / "xds_pipeline_lattice_qsub_list.txt"
        if lattice_list.is_file():
            lattice_list.unlink()

        for sg_prefix, sg, sg_number, cell in sym_to_try:
            force_dir = pipeline_subdir / f"{sg_prefix}_{sg}_force"
            force_dir.mkdir(exist_ok=True)
            per_params = {
                **params,
                "pipeline_subdir":   str(force_dir),
                "force_integration": "true",
                "sg":                sg,
                "sg_number":         sg_number,
                "cell":              cell,
            }
            self._write_worker_script(force_dir, per_params, method="run_single_forced")
            with open(lattice_list, "a") as fh:
                fh.write(str(force_dir) + "\n")

        submit_sh = pipeline_subdir / "xds_pipeline_lattice_qsub_submit.sh"
        self._submit_sge_array(lattice_list, submit_sh, h_rt="02:00:00")

    def run_single_forced(self):
        """
        Forced-integration pipeline for one candidate space group. Called by
        run_pipeline.py inside a {prefix}_{sg}_force/ directory.
        Mirrors xds_pipeline_lattice_qsub.sh.

        Always runs three full rounds of XDS (jobs 1/5/8) without the
        resolution-convergence check used in run_single(), because the symmetry
        was externally imposed and a fixed three-round protocol is sufficient.
        """
        params          = self.params
        pipeline_subdir = Path(params["pipeline_subdir"])

        self._xds(1, params, "xds_forced_integration")

        if not self._xds_ascii_ok(pipeline_subdir, 1):
            print("## XDS did not attempt to index. Check images and logs.")
            return

        job = 1
        if params.get("recycle_xds") == "true":
            self._xds(2, params, "xds_recycle_detector")
            job = 2

        if not self._xds_ascii_ok(pipeline_subdir, job):
            print(f"## job {job} failed. Check logs.")
            return

        # Round 2: first symmetry refinement
        self._pointless(3, params)
        self._aimless(4, params)
        self._xds(5, params, "xds_refine_symmetry")

        if not self._xds_ascii_ok(pipeline_subdir, 5):
            print("## job 5 failed. Check logs.")
            return

        # Round 3: second symmetry refinement (always runs for forced path)
        self._pointless(6, params)
        self._aimless(7, params)
        self._xds(8, params, "xds_refine_symmetry")

        if not self._xds_ascii_ok(pipeline_subdir, 8):
            print("## job 8 failed. Check logs.")
            return

        self._pointless(9, params)
        self._aimless(10, params)
        self._rfree(11, params)
        self._xtriage(13, params)
