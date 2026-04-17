import os
import sys
import argparse
import pathlib

parser = argparse.ArgumentParser(prog="vic.pandda_prepare")
parser.add_argument("path", type=pathlib.Path, default=os.curdir, help="Path to data, default is current directory")
parser.add_argument("--prefix", type=str, default="FRS", help="Prefix for output folder names (default: FRS)")
parser.add_argument("--dry_run", action="store_true", help="Print the proposed renaming without actually renaming the folders")
parser.add_argument("--offset", type=int, default=0, help="Offset for numbering the folders (default: 0)")

def _read_and_rename(args):
    """ Read folder names in the given path, rename them to match pandda input format, and return a list of tuples with original name and new name. """
    folders = []
    idx = 1 + args.offset
    for folder in os.scandir(args.path):
        if folder.is_dir():
            original_name = folder.name
            new_name = f"{args.prefix}_x{idx:04d}"
            folders.append((original_name, new_name))
            idx += 1
    return folders

def _write_csv(new_path, folders):
    """ Write a CSV file with original and new folder names. """
    csv_path = new_path / "info.csv"
    with open(csv_path, "w") as f:
        for original_name, new_name in folders:
            f.write(f"{original_name},{new_name}\n")

def main():
    args = parser.parse_args()
    folders = _read_and_rename(args)
    for original_name, new_name in folders:
        original_path = args.path / original_name
        new_path = args.path / new_name
        print(f"Renaming {original_path} to {new_path}")
        if not args.dry_run:
            os.rename(original_path, new_path)