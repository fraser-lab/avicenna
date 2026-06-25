# AVICENNA vs. the original bash pipeline — functional differences

**Purpose.** AVICENNA is a Python/PHIL reimplementation of the original bash
crystallography pipeline (Galen's scripts in
`/wynton/group/fraser/alyubimov/processing_pipeline/scripts/`). The guiding
principle is that **AVICENNA's defaults reproduce the bash pipeline's behavior**,
so the bash workflow remains the de-facto default. Deliberate, justified
divergences are allowed — this document tracks every *functional* difference so
the changes are explicit.

**Scope.** Only **functional** differences (behavior, parameters, control flow,
thresholds, added/removed steps). Pure implementation details (Python vs bash,
PHIL config, logging format, code structure) are out of scope except where they
change behavior.

**Maintenance.** Update this file in the same change that introduces a behavioral
difference. Keep entries tagged and dated. When in doubt, note the bash baseline
with a `file:line` citation.

**Legend**
- `[parity]` — intentionally matches the bash (may be re-expressed/renamed)
- `[change]` — justified divergence from bash defaults
- `[added]` — capability with no bash equivalent
- `[removed]` — bash capability deliberately dropped
- `[gap?]` — bash capability whose AVICENNA status is unverified (TODO)

_Last updated: 2026-06-09._

---

## 1. Processing sub-pipeline (`vic.process`)

Bash baseline: `xds_pipeline_single.sh` / `xds_pipeline_prep.sh` orchestrating
`run_xds.sh`, `run_pointless.sh`, `run_aimless.sh`, `run_rfree.sh`,
`run_xdsconv.sh`, `run_xtriage.sh`. Core loop: XDS → Pointless → Aimless (no
cut) → XDS → Pointless → Aimless (res cut) → [improve?] XDS → Pointless →
Aimless → R-free/ctruncate → Xtriage. AVICENNA default sequence
(`default_processing.phil`): `1_xds → 2_pointless → 3_aimless → 4_xds →
5_pointless → 6_aimless → 7_xds → 8_pointless → 9_aimless → 10_rfree →
11_xtriage`.

| # | Area | Bash baseline | AVICENNA | Tag |
|---|------|---------------|----------|-----|
| P1 | Forced integration on indexing failure | `try_alt_sym = all` → one SGE job **per starred Bravais lattice** (~9 jobs); `top` → best lattice only (`xds_pipeline_prep.sh`) | Default `on_indexing_failure = retry`, `retry_mode = p1`: a **single** forced job at `SPACE_GROUP_NUMBER=1`; Pointless then finds the true SG. `best`/`top_ten` still available. | `[change]` |
| P2 | Why P1 retry is safe | n/a (bash tried many lattices) | Pointless determines the real SG regardless of the forced lattice, so one job suffices. Verified on HUWE1/FRS-049_06 (P4-forced → P41212 at 1.37 Å). | `[change]` rationale |
| P3 | Pointless SG enforcement in forced path | `known_sg="none"` in the force path → **every** Pointless call is a free run | `enforce_spacegroup` defaults False; True only on `8_pointless` and only when `known_sg` is supplied. Free runs elsewhere (matches bash intent). The legacy Python had a bug (enforced from Aimless) — fixed. | `[parity]` (restored) |
| P4 | Indexing/integration failure actions | Implicit ("retry if no known_sg, else bypass") | Explicit choices: `on_indexing_failure = auto\|retry\|reject\|bypass`, `on_integration_failure = reject\|bypass`. `auto` = bypass when `known_sg` set, else retry. | `[change]` (formalized) |
| P5 | Resolution-cutoff criterion | `res_limit = cc12` (CC½ from Aimless) or a multiplicity threshold; floor to `detector_edge`; optional `custom_res` | Same options, plus an `i_over_sigma` criterion. | `[added]` |
| P6 | HDF5 / Eiger data | CBF-oriented | Detects and processes HDF5 (h5py image counting, raster filtering, NSLS layout); reaches both PHIL and `--legacy` paths. | `[added]` |
| P7 | Pipeline configurability | Monolithic fixed sequence | PHIL-defined step list: reorder/add/remove/swap programs; per-step program blocks. Default reproduces the bash sequence. | `[added]` |
| P8 | xdsconv merge (bash Job 12) | Separate `run_xdsconv.sh` producing `XDS_ASCII.mtz` | The R-free/ctruncate step (`10_rfree`) produces `XDS_ASCII_ctruncate.mtz` directly; no separate xdsconv merge step. Verify the downstream MTZ consumed by refine matches. | `[change?]` |
| P9 | Multiple crystal forms & image-range strategies | Up to 6 crystal forms; per-form cell-refine on/off branches; image-range strategies (`first_half`, `last_half`, `quarters`, `eighths`, explicit ranges) | AVICENNA handles multiple pins/datasets and cell-refine branches; full set of image-range strategies **not yet verified** as replicated. | `[gap?]` verify |
| P12 | XDS/dataset run settings source | All XDS run settings live in `xds_input_parameters.txt` (read by `xds_pipeline_prep.sh` / `run_*.sh`) | **Migrated to PHIL.** Program-agnostic dataset settings in `processing.dataset {}` (indexing_images, refine_cell_integrate, no_symmetry, reference_hkl); XDS-specific settings in a backend-declared `processing.xds {}` (ncpu, detector_edge, res_limit, scaling). `vic.process` flattens these into the executor's params; the txt is read ONLY on `--legacy`/`--bash`. Defaults reproduce the old `XDS_PARAM_DEFAULTS` exactly. Dead keys dropped (`script_dir`, `cleanup`, `recycle_xds`); the `on_indexing_failure`/`retry_mode`/`on_integration_failure` double-source is resolved (per-step only). | `[change]` (PHIL single source) |
| P13 | Pluggability | Monolithic XDS scripts | Each indexing/merge backend declares its per-step block (`PROGRAMS`) + global settings scope (`SETTINGS`) + `run_step`; the framework assembles them. Swapping XDS = drop in a backend that declares its own `SETTINGS`/`PROGRAMS`; the `processing.xds {}` scope is replaced by the new program's. | `[added]` |
| P11 | Known-symmetry input source | `known_symmetry.txt` and/or `## crystal ##` sections in `xds_input_parameters.txt` (`xds_pipeline_prep.sh`) | Read from the PHIL `processing.known_symmetry { enabled; isoform { space_group; unit_cell; reference_hkl } }` scope (`ProcessingOrchestrator._known_symmetry_entries`). Empty/template isoforms (no `space_group`) are skipped — the `.multiple` default template would otherwise append a spurious `none` dataset. Legacy file sources still used as fallback when disabled. | `[change]` (input source) |
| P10 | Min images / SGE resources / CCP4 pin | min 180 images; 2 GB / 2 h (6 h forced); CCP4 7.0 pin | Same SGE resource defaults; CCP4 sourced via the software registry (`ccp4.setup-sh` for CLIBD); CCP4 7.0 pin retained where REFMAC low-res output matters. Min-image gate: verify. | `[parity]` / verify |
| P14 | Hung-job protection | None (jobs run until the SGE `h_rt`, e.g. 6 h) | Progress-based **stall watchdog** (`pipelines/jobbase.SubPipelineJob`): every interpreter job (processing + refine) is killed if it makes no file writes under its working dirs for `STALL_WINDOW_S` (default 900 s of *silence*, not total runtime). Distinguishes a legitimately-long job (large integration, scratch image copy — both keep writing) from a genuinely hung one. On a stall: terminal ERROR line → FAILED, `os._exit(1)` so the SGE task ends and the completion sentinel can fire. SGE `h_rt` remains the backstop. | `[added]` |
| P15 | Cell refinement during integration default | Per-form cell-refine on/off branches run (effectively `both`) | Default `refine_cell_integrate = true` (single cell-refined XDS pass). Justified by a 245-dataset HUWE1/enamine analysis: `true` vs `false` tied on resolution in 226/229 datasets, identical merging quality, and `false` never uniquely won (`true` rescued 1 failure). `both` (run both passes) and `false` remain selectable. XDS-only setting (controls `REFINE(INTEGRATE) CELL`); does not affect Pointless/Aimless or the refine sub-pipeline. Halves processing compute vs running both. | `[change]` (default) |
| P16 | Processor / SGE slot count | Fixed `MAXIMUM_NUMBER_OF_PROCESSORS=8`, `MAXIMUM_NUMBER_OF_JOBS=1`, `-pe smp 8` for every dataset regardless of size | Default `processing.xds.ncpu = auto`: the slot request is sized per dataset from the frame count (`utils/resources.xds_slots`), split across `MAXIMUM_NUMBER_OF_JOBS` (forked frame batches) × `MAXIMUM_NUMBER_OF_PROCESSORS` (threads), capped at 16 cores. The array's `-pe smp` is the max sizing across its datasets. **Adds `MAXIMUM_NUMBER_OF_JOBS` (was always 1).** An explicit integer (`processing.xds.ncpu=8`) reproduces the old fixed behaviour (8 processors, 1 job). Analysis `cpus = auto` sizes pandda.analyse from the dataset count (one core/dataset, capped 36); refine `ncpu = auto` resolves to 1 (REFMAC5 serial). | `[change]` (auto resource sizing) |

---

## 2. Refinement sub-pipeline (`vic.refine`)

Bash baseline: `qrefine.sh` (per dataset × water suffix): input prep → rigid-body
`phenix.refine` (5 macro-cycles, `hydrogens.refine=riding`) → [if Rfree >
`mr_cutoff`] `phenix.phaser` → jelly `refmac5` (4 cyc, ridge σ 0.01) → restrained
`refmac5` (8 cyc) → [if `mr_try_monomer` and (phaser failed or final Rfree > 0.4)]
monomer phaser → jelly → restrained. Per-crystal MR parameters came from
`qrefine_input_parameters.txt`. AVICENNA default sequence
(`default_refine.phil`): `1_phaser → 2_jelly → 3_refmac5 → 4_fill`.
**Rigid body is no longer in the default sequence** — Phaser is the first step
(see R1). The `phenix.refine` step type remains available for custom pipelines.

| # | Area | Bash baseline | AVICENNA | Tag |
|---|------|---------------|----------|-----|
| R1 | First step / rigid body | Always runs rigid-body `phenix.refine` first (5 macro-cycles, `strategy=rigid_body`, `hydrogens.refine=riding`, `miller_array.labels.name=F,SIGF`; `qrefine.sh:141`), then conditionally Phaser | **Phaser is the default first step; rigid body is dropped from the default sequence.** Rationale: Phaser is *faster* than rigid body (~2 min CPU vs ~3 min wall, measured over 25 datasets), resolves the enantiomorphic space group automatically (rigid body cannot — see [[enantiomorph-mismatch-processing-bug]]), and is the step with real contingencies when a ligand-bound structure differs from the reference. The `phenix.refine` step type remains available for custom pipelines; when present and reaching Rfree ≤ threshold, it still gates Phaser (R2). When a phenix.refine step IS used, it also gets a second array selection `miller_array.labels.name=FreeR_flag` (bash uses only `F,SIGF`) to exclude the integer `ISYM` column that phenix.refine intermittently mistakes for R-free flags ("Multiple equally suitable arrays of R-free flags found"). | `[change]` (default = Phaser-first; diverges from bash) |
| R2 | MR gate | Phaser runs if rigid-body Rfree > `mr_cutoff` | Phaser is the default first step and runs unconditionally. The `phaser.rfree_threshold` (default 0.45) gate applies only when a refinement step precedes Phaser in a custom pipeline (then MR is skipped if that step already reached the threshold). | `[change]` (Phaser-first default) |
| R3 | **MR composition (MW / copies)** | User supplies `mr_mw`, `mr_copies`, `mr_seq_id` per crystal in `qrefine_input_parameters.txt` | `phaser.asu_composition.derive_from_pdb = True` (default): **MW and copy number are estimated from the reference PDB** (residue-mass sum + chain-count ratio). Explicit `mw`/`num_copies`/`seq_id` used when derivation is off. `mol_weight` omitted (Phaser self-estimates) when 0. | `[change]`/`[added]` |
| R4 | **Monomer retry** | Conditional second MR with a separate user-supplied monomer model (`mr_pdb_monomer`), triggered by `mr_try_monomer` and (phaser failed or final Rfree > 0.4); followed by monomer jelly + restrained (`qrefine.sh:246`) | **Removed.** Rationale: the "first-chain monomer" fallback only works for homo-oligomers (breaks for the common hetero-oligomer case), and it was a fallback-of-a-fallback over rigid-body+MR. Use `phaser.alt_search_model` to run MR with a monomer/alt model directly. | `[removed]` |
| R5 | Rfree thresholds | Two: `mr_cutoff` (MR gate) and hard-coded `0.4` (monomer-retry trigger) | One: `rfree_threshold` (default 0.45) — the `0.4` monomer trigger is gone with the monomer retry. | `[change]` |
| R6 | Alternate search model | `mr_pdb_monomer` (monomer for the retry) | `phaser.alt_search_model` — any alternate model (monomer, poly-Ala, perturbed); used in place of the reference. Generalized + no longer retry-coupled. | `[change]` |
| R7 | phenix.refine / phenix.phaser tunables | Hard-coded in the script | Exposed as typed PHIL params (strategy, macro-cycles, hydrogens; tncs, seq_id) plus a verbatim `extra_args` pass-through forwarded to phenix (validated by phenix). | `[added]` flexibility (defaults = bash) |
| R8 | Refmac cycle counts / jelly | Hard-coded ncycle 4 (jelly, ridge σ 0.01) / 8 (final) (`qrefine.sh:218,232`) | Same defaults, now configurable (`refmac.ncycles`, `refmac.jelly`). | `[parity]` (configurable) |
| R9 | Water handling | `mr_water = true\|false\|both` (hoh / nohoh / both jobs) | `refine.water = both\|hoh\|nohoh` — same semantics, renamed; translated to the legacy vocabulary internally. | `[parity]` (renamed) |
| R10 | Reflection fill for PanDDA | none | `5_fill` step: `uniqueify → CAD → phenix.maps` → `final_{suffix}_filled.mtz`. (Added in the Python port, pre-PHIL.) Was **broken since inception** (failed in legacy too): (a) the CAD step carried a literal unsubstituted `resolution file 1 999.0 HIGHRES` keyword that rejected *every* reflection, so phenix.maps aborted with "No array of observed xray data found"; (b) the output-file check looked for `fill_02_cad_map_coeffs.mtz` but phenix.maps names the coefficient MTZ after the model stem (`final_{suffix}_map_coeffs.mtz`). Both fixed: CAD resolution line dropped (pass all reflections); output located by model-stem name + `*_map_coeffs.mtz` glob. | `[added]` (fixed) |
| R11 | Per-crystal vs global settings | `qrefine_input_parameters.txt` = **per-crystal** MR params | One **global** `refine.phil` (reference_pdb + MR settings) for all datasets, assuming an isomorphous same-protein campaign. `qrefine_input_parameters.txt` is **legacy-only** (ignored by the PHIL path). Dataset enumeration stays in `qrefine_list.txt`. | `[change]` (workflow assumption) |
| R12 | Pipeline linking | Manual prep of the qrefine input files | `vic.process` emits a ready `refine.phil` (paths filled, `reference_pdb` left as a placeholder to edit). | `[added]` (workflow) |
| R13 | Data passed to refmac | refmac's `HKLIN` is always the original ctruncate MTZ (`self.mtz`); phaser supplies only the model (`qrefine_pipeline.py:346/366`). Works only if processing already produced the correct enantiomorph. | **Phaser hands its solution-frame data forward** (`ctx["data_mtz"]`); refmac consumes that, not the original. When Phaser resolves a space-group ambiguity (e.g. enantiomorph flip P41212↔P43212 — indistinguishable by the data, so processing may pick either hand), the corrected hand now propagates to refinement instead of being silently reverted. Free flags: original kept if same SG; re-stamped (transferred) for an enantiomorph relabel; freshly generated otherwise. Fixes Rfree≈0.44→0.23 on wrong-hand-processed data. See [[enantiomorph-mismatch-processing-bug]]. | `[change]` (robustness; modularity) |

---

## 3. Analysis sub-pipeline (`vic.analysis`)

| # | Area | Bash baseline | AVICENNA | Tag |
|---|------|---------------|----------|-----|
| A1 | Program | `dimple.sh` (DIMPLE quick MR + refinement; `-M0/-M1` MR toggle) + `dimple_check.sh` results scrape | **PanDDA** orchestration (`pandda.analyse` / `pandda2`). DIMPLE is **not** reimplemented. | `[change]` (different scope) |
| A2 | PanDDA orchestration | Run manually / outside these scripts | AVICENNA provides a PanDDA sub-pipeline + `pandda_template.sh` generation (FILL_IN dataset prefix / reference PDB / reference MTZ) bridged from refine output. | `[added]` |
| A3 | Status | — | Analysis PHIL + interpreter activation is the least-developed of the three sub-pipelines (see project notes). | `[gap?]` in progress |

---

## Open items to verify against bash (TODO)

- **P8** — confirm the refine-input MTZ (`XDS_ASCII_ctruncate.mtz`) is equivalent to bash's xdsconv `XDS_ASCII.mtz` for downstream use.
- **P9** — confirm whether the bash image-range strategies (quarters/eighths/halves) and 6-crystal-form handling are replicated, or are an intentional simplification.
- **P10** — confirm the min-180-images gate and forced-job SGE resource/scratch settings.
- **R3** — `derive_from_pdb` is naive for hetero-oligomers (counts chains uniformly); a per-component composition would be the general fix if needed.
