"""Call one function of a candidate solution and print the canonical form of its result.

Usage: ``python -I call.py <solution.py> <func_name>``; one JSON argument list is
read from stdin. LiveCodeBench functional tasks define a ``Solution`` class, so the
method is looked up there first and then at module level. The solution's own
prints go to stderr so the only thing on stdout is the result.

The result is turned into ``canonical.canonical_value`` here, from the real Python
object, because a JSON round trip would turn nested tuples into lists and int dict
keys into strings, which LiveCodeBench's comparison tells apart. A result with no
canonical form (NaN, a non-string dict key, an unsupported type) makes this exit
non-zero. ``canonical.py`` is loaded by path before the solution, since ``-I`` keeps
this directory off ``sys.path``.
"""

import contextlib
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_canonical():
    spec = importlib.util.spec_from_file_location(
        "canonical", os.path.join(HERE, "canonical.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load(path):
    spec = importlib.util.spec_from_file_location("candidate_solution", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _target(module, func_name):
    cls = getattr(module, "Solution", None)
    if cls is not None and hasattr(cls, func_name):
        return getattr(cls(), func_name)
    return getattr(module, func_name)


def main(argv):
    solution_path, func_name = argv[1], argv[2]
    canonical_value = _load_canonical().canonical_value
    args = json.loads(sys.stdin.read())
    if not isinstance(args, list):
        raise ValueError("the argument list must be a JSON array")
    with contextlib.redirect_stdout(sys.stderr):
        result = _target(_load(solution_path), func_name)(*args)
    sys.stdout.write(canonical_value(result))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
