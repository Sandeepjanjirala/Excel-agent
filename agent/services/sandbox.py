"""
Restricted execution sandbox for LLM-generated pandas code.

We validate the AST, allow safe mathematical and data operations, allow safe
standard imports (pandas, numpy, math, re, datetime), auto-assign the result to
`answer` if a bare expression is returned, and execute within process isolation.
"""
from __future__ import annotations

import ast
import builtins
import datetime
import math
import multiprocessing
import re
import numpy as np
import pandas as pd


class UnsafeCodeError(Exception):
    pass


def _json_safe(obj):
    """
    Recursively convert a pandas/numpy result into something that survives a
    real round-trip through json.dumps() + the browser's JSON.parse().
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


# Node types allowed in execution.
_ALLOWED_NODES_LIST = [
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
    ast.If, ast.IfExp, ast.For, ast.While, ast.Break, ast.Continue, ast.Pass,
    ast.FunctionDef, ast.Return, ast.Lambda, ast.arguments, ast.arg,
    ast.ListComp, ast.DictComp, ast.SetComp, ast.GeneratorExp, ast.comprehension,
    ast.JoinedStr, ast.FormattedValue,
    ast.Starred,
    ast.Try, ast.ExceptHandler,
    ast.alias, ast.Import, ast.ImportFrom,
]

# Conditionally add modern Python AST nodes if available
for node_name in ("NamedExpr", "Match", "match_case", "MatchValue", "MatchAs"):
    if hasattr(ast, node_name):
        _ALLOWED_NODES_LIST.append(getattr(ast, node_name))

_ALLOWED_NODES = tuple(_ALLOWED_NODES_LIST)

_SAFE_MODULES = {
    "pandas", "pd", "numpy", "np", "math", "re", "datetime",
    "collections", "itertools", "functools",
}

_FORBIDDEN_NAMES = {
    "__import__", "eval", "exec", "compile", "open", "input",
    "globals", "locals", "vars", "dir", "getattr", "setattr", "delattr",
    "__builtins__", "__loader__", "__spec__", "os", "sys", "subprocess",
    "socket", "shutil", "pathlib", "importlib",
}


def _auto_assign_answer(code: str) -> str:
    """If code ends with a bare expression and doesn't assign to `answer`, assign it to `answer`."""
    try:
        tree = ast.parse(code, mode="exec")
        if not tree.body:
            return code
        last_stmt = tree.body[-1]
        if isinstance(last_stmt, ast.Expr):
            tree.body[-1] = ast.Assign(
                targets=[ast.Name(id="answer", ctx=ast.Store())],
                value=last_stmt.value
            )
            ast.fix_missing_locations(tree)
            return ast.unparse(tree)
    except Exception:
        pass
    return code


def _validate_ast(code: str) -> ast.AST:
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as e:
        raise UnsafeCodeError(f"Code does not parse: {e}")

    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise UnsafeCodeError(f"Disallowed syntax: {type(node).__name__}")

        if isinstance(node, ast.Import):
            for alias in node.names:
                root_module = alias.name.split(".")[0]
                if root_module not in _SAFE_MODULES:
                    raise UnsafeCodeError(f"Importing '{alias.name}' is not allowed.")

        if isinstance(node, ast.ImportFrom):
            if node.module:
                root_module = node.module.split(".")[0]
                if root_module not in _SAFE_MODULES:
                    raise UnsafeCodeError(f"Importing from '{node.module}' is not allowed.")

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
        "map", "filter", "divmod", "pow", "repr", "iter", "next",
    )
    if hasattr(builtins, name)
}


def _run_in_process(code: str, dataframes: dict, result_queue):
    try:
        prepared_code = _auto_assign_answer(code)
        _validate_ast(prepared_code)
        safe_globals = {
            "__builtins__": _SAFE_BUILTINS,
            "pd": pd,
            "np": np,
            "math": math,
            "re": re,
            "datetime": datetime,
            "pandas": pd,
            "numpy": np,
        }
        safe_locals = {name: df.copy() for name, df in dataframes.items()}
        if len(dataframes) == 1:
            safe_locals["df"] = next(iter(safe_locals.values())).copy()

        exec(prepared_code, safe_globals, safe_locals)

        if "answer" not in safe_locals:
            for fallback_key in ("result", "ans", "res", "output", "final"):
                if fallback_key in safe_locals:
                    safe_locals["answer"] = safe_locals[fallback_key]
                    break

        if "answer" not in safe_locals:
            raise UnsafeCodeError(
                "Code must assign the final result to a variable named `answer`."
            )
        result = safe_locals["answer"]

        # Make DataFrames and Series clean and JSON-serializable
        if isinstance(result, pd.Series):
            result = result.to_dict()
        elif isinstance(result, pd.DataFrame):
            if len(result) <= 200:
                result = result.to_dict(orient="records")
            else:
                result = result.head(200).to_dict(orient="records")

        result = _json_safe(result)
        result_queue.put(("ok", result))
    except Exception as e:
        result_queue.put(("error", f"{type(e).__name__}: {e}"))


def run_pandas_snippet(code: str, dataframes: dict, timeout: int = 10):
    """
    Execute `code` in an isolated subprocess with restricted builtins and
    an AST safety check. `dataframes` maps sheet_name -> DataFrame.
    """
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
