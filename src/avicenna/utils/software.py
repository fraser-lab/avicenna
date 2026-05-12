"""Generalised scientific software finder for Avicenna.

The canonical list of known software suites lives in the resource file
``default_software_registry.phil`` (PHIL schema defined in
``software_registry.phil``).  Power users may extend or override entries by
placing a ``software_registry.phil`` in:

  1. ``~/.avicenna/software_registry.phil``   (user-level overrides)
  2. ``./software_registry.phil``             (project-level overrides)

Entries from later files are merged on top of earlier ones (standard PHIL
semantics).  Adding a new suite requires only a new ``suite { }`` block in
one of those files — no Python changes are needed.

Public API
----------
find(name, root=None, version="latest")  -> (bin_dir, version_str)
    Generic finder for any suite in the registry.

find_xds(root=None)                      -> (bin_dir, xdsstat_path, version_str)
    Convenience wrapper that also locates xdsstat.

find_ccp4(root=None, version="latest")   -> (bin_dir, version_str)
find_phenix(root=None, version="latest") -> (bin_dir, version_str)
find_pandda(root=None)                   -> (bin_dir, version_str)
find_pandda2(root=None)                  -> (bin_dir, version_str)

check_programs(programs_params)          -> dict[suite -> {bin, version, ...}]
    Pre-flight checker called by CLIs before job submission.
"""

import os
import platform
import re
import shutil
import sys
from pathlib import Path

import freephil


# ---------------------------------------------------------------------------
# Registry loading
# ---------------------------------------------------------------------------

_RESOURCES = Path(__file__).parents[1] / "resources"
_SCHEMA_FILE  = _RESOURCES / "software_registry.phil"
_DEFAULT_FILE = _RESOURCES / "default_software_registry.phil"

_USER_OVERRIDE   = Path.home() / ".avicenna" / "software_registry.phil"
_LOCAL_OVERRIDE  = Path("software_registry.phil")


def _load_registry():
    """Parse and return the merged software registry as a freephil extract."""
    master  = freephil.parse(_SCHEMA_FILE.read_text())
    sources = [freephil.parse(_DEFAULT_FILE.read_text())]
    for override in (_USER_OVERRIDE, _LOCAL_OVERRIDE):
        if override.is_file():
            sources.append(freephil.parse(override.read_text()))
    return master.fetch(sources=sources).extract().software_registry


def _registry_by_name() -> dict:
    """Return {suite_name: entry} for every suite in the merged registry."""
    reg = _load_registry()
    return {e.name: e for e in reg.suite if e.name}


# ---------------------------------------------------------------------------
# Shared search utilities
# ---------------------------------------------------------------------------

def _arch() -> str:
    return (f"{platform.machine()}-"
            f"{'darwin' if sys.platform == 'darwin' else 'linux'}")


def _sbgrid_suite_roots(sbgrid_name: str):
    """Yield candidate SBGrid root directories for *sbgrid_name*."""
    arch = _arch()
    sbg = os.environ.get("SBG_PROGRAMS")
    if sbg:
        yield Path(sbg) / arch / sbgrid_name
    yield Path("/programs") / arch / sbgrid_name


def _version_tuple(s: str) -> tuple:
    parts = re.findall(r"\d+", s)
    return tuple(int(x) for x in parts) if parts else ()


def _version_matches(candidate_tuple: tuple, requested: str) -> bool:
    if not requested or requested == "latest":
        return True
    req = _version_tuple(requested)
    return candidate_tuple[: len(req)] == req


def _sentinel_in_dir(directory: Path, sentinel: str) -> Path | None:
    """Return the sentinel Path if it exists under *directory*, else None."""
    p = directory / sentinel
    return p if p.exists() else None


def _find_bin_in_version_dir(version_dir: Path, entry) -> Path | None:
    """Return the bin_dir for *entry*'s sentinel within *version_dir*,
    accounting for subdir_prefix and search_subdirs."""
    sentinel = entry.sentinel or ""
    if not sentinel:
        return None

    if entry.subdir_prefix:
        for sub in version_dir.iterdir():
            if sub.is_dir() and sub.name.startswith(entry.subdir_prefix):
                if _sentinel_in_dir(sub, sentinel):
                    # bin_dir is the directory containing the sentinel's parent
                    return (sub / sentinel).parent
        return None

    if entry.search_subdirs:
        for subname in entry.search_subdirs:
            sub = version_dir / subname
            if sub.is_dir() and _sentinel_in_dir(sub, sentinel):
                return (sub / sentinel).parent
        return None

    hit = _sentinel_in_dir(version_dir, sentinel)
    return hit.parent if hit else None


# ---------------------------------------------------------------------------
# Generic finder
# ---------------------------------------------------------------------------

def find(name: str, root=None, version: str = "latest") -> tuple:
    """Locate suite *name* and return ``(bin_dir, version_str)``.

    Parameters
    ----------
    name : str
        Suite name as declared in the registry (e.g. ``"ccp4"``).
    root : str or Path or None
        Explicit installation root.  Skips all auto-detection when provided.
    version : str
        ``"latest"`` or a version prefix (e.g. ``"9.0"``).  Ignored when
        ``version_pinnable`` is False for this suite.

    Raises
    ------
    KeyError
        If *name* is not present in the registry.
    FileNotFoundError
        If the suite cannot be located.
    """
    registry = _registry_by_name()
    if name not in registry:
        raise KeyError(
            f"'{name}' is not in the software registry.  "
            f"Known suites: {sorted(registry)}"
        )
    entry = registry[name]
    effective_version = version if entry.version_pinnable else "latest"

    if root:
        root = Path(root)
        bin_dir = _find_bin_in_version_dir(root, entry)
        if bin_dir:
            return bin_dir, root.name
        raise FileNotFoundError(
            f"No valid {name} installation found at {root} "
            f"(sentinel '{entry.sentinel}' missing)."
        )

    sbgrid_name = entry.sbgrid_name or name
    for suite_root in _sbgrid_suite_roots(sbgrid_name):
        if not (suite_root and suite_root.is_dir()):
            continue
        candidates = []
        for d in suite_root.iterdir():
            if not d.is_dir():
                continue
            if entry.version_style == "date":
                if not (d.name.isdigit() and len(d.name) == 8):
                    continue
                vtup = (int(d.name),)
            else:
                vtup = _version_tuple(d.name)
                if not vtup:
                    continue
            if not _version_matches(vtup, effective_version):
                continue
            bin_dir = _find_bin_in_version_dir(d, entry)
            if bin_dir:
                candidates.append((vtup, bin_dir, d.name))
        if candidates:
            _, bin_dir, ver = max(candidates, key=lambda x: x[0])
            return bin_dir, ver

    for exe in (entry.path_fallback or []):
        path = shutil.which(exe)
        if path:
            return Path(path).parent, "unknown"

    suffix = f" (version {version})" if version != "latest" else ""
    raise FileNotFoundError(
        f"{name}{suffix} not found.  "
        f"Source SBGrid or ensure {entry.path_fallback or [name]} is on PATH."
    )


# ---------------------------------------------------------------------------
# Named convenience wrappers (used by proc_pipeline.py and others)
# ---------------------------------------------------------------------------

def find_xds(root=None) -> tuple:
    """Return ``(bin_dir, xdsstat_path, version_str)`` for XDS + xdsstat."""
    bin_dir, ver = find("xds", root=root)
    try:
        xdsstat_bin, _ = find("xdsstat")
        xdsstat_path = xdsstat_bin / "xdsstat"
    except FileNotFoundError:
        xdsstat_path = shutil.which("xdsstat") or Path("xdsstat")
        xdsstat_path = Path(xdsstat_path)
    return bin_dir, xdsstat_path, ver


def find_ccp4(root=None, version: str = "latest") -> tuple:
    return find("ccp4", root=root, version=version)


def find_phenix(root=None, version: str = "latest") -> tuple:
    return find("phenix", root=root, version=version)


def find_pandda(root=None) -> tuple:
    return find("pandda", root=root)


def find_pandda2(root=None) -> tuple:
    return find("pandda2", root=root)


# ---------------------------------------------------------------------------
# Pre-flight checker
# ---------------------------------------------------------------------------

def check_programs(programs_params) -> dict:
    """Locate all suites declared in a PHIL ``programs {}`` scope.

    Reads the software registry to discover how to find each suite.  Prints a
    one-line summary per suite found, then either returns the results dict or
    exits with a non-zero status if any required suite is missing.

    Parameters
    ----------
    programs_params : freephil scope_extract
        The ``.programs`` sub-object from a pipeline PHIL parameter set.

    Returns
    -------
    dict[str, dict]
        ``{suite_name: {"bin": Path, "version": str, ...}}`` for each suite.
    """
    registry = _registry_by_name()
    found  = {}
    errors = []
    reported = set()   # avoid double-printing companions

    print("Software check:")
    for suite_name, entry in registry.items():
        suite_params = getattr(programs_params, suite_name, None)
        if suite_params is None:
            continue   # this pipeline does not use this suite

        if suite_name in reported:
            continue

        root    = getattr(suite_params, "root",    None)
        version = (getattr(suite_params, "version", "latest")
                   if entry.version_pinnable else "latest")

        try:
            bin_dir, ver = find(suite_name, root=root, version=version)
            ver_display  = ver if ver != "unknown" else "(version unknown)"
            print(f"  {suite_name:<12} {ver_display:<22} {bin_dir}")
            found[suite_name] = {"bin": bin_dir, "version": ver}
            reported.add(suite_name)

            if entry.companion:
                comp = entry.companion
                try:
                    comp_bin, comp_ver = find(comp)
                    comp_path = comp_bin / comp
                    print(f"  {comp:<12} {'':22} {comp_path}")
                    found[comp] = {"bin": comp_bin, "version": comp_ver}
                    reported.add(comp)
                except FileNotFoundError as exc:
                    errors.append(str(exc))

        except (FileNotFoundError, KeyError) as exc:
            errors.append(str(exc))

    if errors:
        print("ERROR: Required software not found:")
        for msg in errors:
            print(f"  {msg}")
        sys.exit(1)

    return found
