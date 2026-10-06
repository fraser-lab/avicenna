"""Ligand models and restraints for the analysis stage.

A ligand library is a folder holding one folder per ligand, named
``<library>_<well>``, with a further ``<library>_<well>_<isomer>`` folder for
each stereoisomer of the compound in that well.  Each ligand folder holds the
model (``.pdb``) and its restraints (``.cif``) under the folder's own name.

A ligand the library does not have is generated from its SMILES with
phenix.elbow, into a per-project folder laid out the same way, so a later run
finds it there instead of generating it again.  Generation runs on the cluster
at the start of the pandda job (``generate``), not at the submit host.
"""

import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

#: The per-project folder generated ligands are kept in.
GENERATED_DIR = "ligands_generated"


def ligand_name(library, well):
    """The ligand's folder and file name: ``<library>_<well>``."""
    return f"{library}_{well}"


def _pairs(folder):
    """``(cif, pdb)`` for every restraint file in *folder* with its model."""
    pairs = []
    for cif in sorted(Path(folder).glob("*.cif")):
        pdb = cif.with_suffix(".pdb")
        if pdb.is_file():
            pairs.append((cif, pdb))
    return pairs


def find_ligand_files(root, library, well):
    """Every ``(cif, pdb)`` pair for one well under *root*: the compound's
    own folder and one folder per stereoisomer.  Empty when there is none.

    The separator after the well is what keeps one well from matching another
    whose code it begins (``M07`` and a hypothetical ``M07B``) -- isomer
    folders always add it.
    """
    if not root or not library or not well:
        return []
    root = Path(root)
    name = ligand_name(library, well)
    folders = [root / name] + sorted(root.glob(f"{name}_*"))
    pairs = []
    for folder in folders:
        if folder.is_dir():
            pairs.extend(_pairs(folder))
    return pairs


def generated_files(project_dir, library, well):
    """The ``(cif, pdb)`` pairs an earlier run generated for one well."""
    return find_ligand_files(Path(project_dir) / GENERATED_DIR, library, well)


# --------------------------------------------------------------------------- #
# Generation (runs inside the pandda job)                                      #
# --------------------------------------------------------------------------- #

def _generate_one(elbow, item):
    """Generate one ligand and place it in every dataset folder that needs it.

    Never raises: a ligand that cannot be generated costs that ligand only,
    and pandda.analyse runs without it.
    """
    name, out_dir = item["name"], Path(item["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    pairs = _pairs(out_dir)
    if not pairs:
        with open(out_dir / "elbow.log", "w") as log:
            try:
                result = subprocess.run(
                    [elbow, f"--smiles={item['smiles']}", f"--output={name}"],
                    cwd=out_dir, stdout=log, stderr=subprocess.STDOUT)
                returncode = result.returncode
            except OSError as exc:
                log.write(f"{exc}\n")
                returncode = None
        pairs = _pairs(out_dir)
        if not pairs:
            return (f"WARNING: phenix.elbow could not generate {name} "
                    f"(exit {returncode}); see {out_dir / 'elbow.log'}")
    for target in item["targets"]:
        for cif, pdb in pairs:
            shutil.copy2(cif, Path(target) / cif.name)
            shutil.copy2(pdb, Path(target) / pdb.name)
    return f"Generated {name} for {len(item['targets'])} dataset(s)"


def generate(todo_path, cpus=1):
    """Generate every ligand listed in *todo_path*, *cpus* at a time.

    The list is ``{"elbow": <path>, "ligands": [{name, smiles, out_dir,
    targets}]}``, written by the analysis preparer.
    """
    with open(todo_path) as fh:
        todo = json.load(fh)
    ligands = todo.get("ligands") or []
    print(f"Generating {len(ligands)} ligand(s) with phenix.elbow", flush=True)
    with ThreadPoolExecutor(max_workers=max(1, int(cpus))) as pool:
        for line in pool.map(lambda item: _generate_one(todo["elbow"], item),
                             ligands):
            print(line, flush=True)


if __name__ == "__main__":
    generate(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else 1)
