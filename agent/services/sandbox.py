"""
Restricted execution sandbox for LLM-generated pandas code.

We never trust Gemini's *answer* directly for numbers - we trust its
*code*, and only after that code passes an AST safety check do we run it,
with no filesystem/network/import access, against the real dataframe(s).
"""
from __future__ import annotations

import ast
import builtins
import math
import multiprocessing
import numpy as np
import pandas as pd


class UnsafeCodeError(Exception):
    pass


def _json_safe(obj):
    """
    Recursively convert a pandas/numpy result into something that survives a
    real round-trip through json.dumps() + the browser's JSON.parse().

    Two things break that round-trip if left alone:
      - float('nan') / float('inf') - Python's json module writes these as
        the bare literals NaN/Infinity by default, which are NOT valid JSON
        and make JSON.parse() throw in the browser (this looks like "the
        server returned 200 but the request still failed").
      - Non-string dict keys (e.g. a tuple key from a multi-column groupby,
        or a numpy scalar key) - JSON object keys must be strings.
    """
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, (np.floating, float)):
        f = float(obj)
        return None if (math.isnan(f) or math.isinf(f)) else f
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    return obj


# Node types we are willing to execute. Anything else (Import, With, Global,
# Lambda-that-calls-dunders, etc. are still checked further below) is rejected.
_ALLOWED_NODES = (
    ast.Module, ast.Expr, ast.Assign, ast.AugAssign, ast.AnnAssign,
    ast.Load, ast.Store, ast.Del,
    ast.Name, ast.Attribute, ast.Subscript, ast.Index, ast.Slice,
    ast.Call, ast.keyword,
    ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.USub, ast.UAdd, ast.Not, ast.Invert,
    ast.And, ast.Or,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.Is, ast.IsNot,
    ast.List, ast.Tuple, ast.Dict, ast.Set,
    ast.Constant,
    ast.If, ast.For, ast.While, ast.Break, ast.Continue, ast.Pass,
    ast.FunctionDef, ast.Return, ast.Lambda, ast.arguments, ast.arg,
    ast.ListComp, ast.DictComp, ast.SetComp, ast.GeneratorExp, ast.comprehension,
    ast.JoinedStr, ast.FormattedValue,
    ast.Starred,
)

_FORBIDDEN_NAMES = {
    "__import__", "eval", "exec", "compile", "open", "input",
    "globals", "locals", "vars", "dir", "getattr", "setattr", "delattr",
    "__builtins__", "__loader__", "__spec__", "os", "sys", "subprocess",
    "socket", "shutil", "pathlib", "importlib",
}


def _validate_ast(code: str) -> ast.AST:
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as e:
        raise UnsafeCodeError(f"Code does not parse: {e}")

    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise UnsafeCodeError(f"Disallowed syntax: {type(node).__name__}")

        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise UnsafeCodeError("Imports are not allowed")

        name = None
        if isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Attribute):
            name = node.attr

        if name and (name in _FORBIDDEN_NAMES or name.startswith("__")):
            raise UnsafeCodeError(f"Forbidden identifier used: {name}")

    return tree


_SAFE_BUILTINS = {
    name: getattr(builtins, name)
    for name in (
        "abs", "all", "any", "bool", "dict", "enumerate", "float", "int",
        "len", "list", "max", "min", "range", "round", "set", "sorted",
        "str", "sum", "tuple", "zip", "print", "isinstance", "type",
    )
}


def _run_in_process(code: str, dataframes: dict, result_queue):
    try:
        _validate_ast(code)
        safe_globals = {
            "__builtins__": _SAFE_BUILTINS,
            "pd": pd,
            "np": np,
        }
        # `dataframes` keys are already safe, unique variable names assigned
        # at upload time (see excel_parser.build_batch) - one per sheet,
        # across every file in the batch. Exposed as-is, plus `df` as a
        # convenience alias when the whole batch is just one sheet.
        safe_locals = {name: df.copy() for name, df in dataframes.items()}
        if len(dataframes) == 1:
            safe_locals["df"] = next(iter(safe_locals.values())).copy()

        exec(code, safe_globals, safe_locals)

        if "answer" not in safe_locals:
            raise UnsafeCodeError(
                "Code must assign the final result to a variable named `answer`."
            )
        result = safe_locals["answer"]

        # Make the result JSON/text friendly (DataFrame/Series -> dict, then
        # recursively strip NaN/Infinity/non-str-keys so it survives an
        # actual JSON round-trip to the browser).
        if isinstance(result, (pd.DataFrame, pd.Series)):
            result = result.to_dict()
        result = _json_safe(result)

        result_queue.put(("ok", result))
    except Exception as e:  # noqa: BLE001 - deliberately broad, isolated process
        result_queue.put(("error", f"{type(e).__name__}: {e}"))


def run_pandas_snippet(code: str, dataframes: dict, timeout: int = 10):
    """
    Execute `code` in an isolated subprocess with restricted builtins and
    an AST safety check. `dataframes` maps sheet_name -> DataFrame.

    The snippet MUST set a variable called `answer`.
    Returns (status, value) where status is "ok" or "error".
    """
    # "fork" avoids re-importing/re-executing the parent's __main__ module
    # (which "spawn" does, and which is fragile under things like
    # `manage.py runserver`, Gunicorn, or `python -c`). Fork is available
    # on Linux/macOS, which covers typical Django deployment targets.
    try:
        ctx = multiprocessing.get_context("fork")
    except ValueError:
        ctx = multiprocessing.get_context("spawn")
    q = ctx.Queue()
    p = ctx.Process(target=_run_in_process, args=(code, dataframes, q))
    p.start()
    p.join(timeout)

    if p.is_alive():
        p.terminate()
        p.join()
        return "error", f"Execution timed out after {timeout}s"

    if not q.empty():
        return q.get()
    return "error", "No result produced (process exited unexpectedly)"
