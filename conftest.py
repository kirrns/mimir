# ponytail: empty on purpose. pytest adds this file's directory (the repo root)
# to sys.path when it discovers it, which is what makes `bench` importable in
# tests — `bench` is deliberately not part of the installed package (repo-tree
# only, see mimir/cli.py), so without this, only `python -m pytest` (which adds
# cwd to sys.path itself) could find it; plain `pytest` could not.
