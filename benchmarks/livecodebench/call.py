"""Call one function of a candidate solution and print its result as JSON.

Usage: ``python -I call.py <solution.py> <func_name>``; one JSON argument list is
read from stdin. LiveCodeBench functional tasks define a ``Solution`` class, so the
method is looked up there first and then at module level. The solution's own
prints go to stderr so the only thing on stdout is the result.
"""

import contextlib
import importlib.util
import json
import sys


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
    args = json.loads(sys.stdin.read())
    if not isinstance(args, list):
        raise ValueError("the argument list must be a JSON array")
    with contextlib.redirect_stdout(sys.stderr):
        result = _target(_load(solution_path), func_name)(*args)
    sys.stdout.write(json.dumps(result))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
