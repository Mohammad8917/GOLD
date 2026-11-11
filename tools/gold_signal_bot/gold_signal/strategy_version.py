"""Manual strategy identity and AST-based guard against forgotten version bumps.

When strategy.py or indicators.py logic changes, deliberately update both
STRATEGY_VERSION and STRATEGY_AST_HASH in this file in the same commit.
Comments, whitespace, and docstrings are excluded from the fingerprint.
"""
from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from typing import Mapping

STRATEGY_VERSION = "ema20-ema50-rsi14-atr14-v1"
STRATEGY_AST_HASH = "sha256:e6f806c4e13fdebdcca260a44b6290195915d5b888f337a48244c7be6d9ca977"


def _remove_docstrings(tree: ast.AST) -> ast.AST:
    """Remove module/class/function docstrings before canonical AST dumping."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr):
                value = body[0].value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    del body[0]
    return tree


def strategy_ast_hash(sources: Mapping[str, str]) -> str:
    """Hash normalized strategy and indicator ASTs; ignore formatting and docstrings."""
    digest = hashlib.sha256()
    for filename in sorted(sources):
        tree = ast.parse(sources[filename], filename=filename)
        normalized = ast.dump(_remove_docstrings(tree), include_attributes=False)
        digest.update(filename.encode("utf-8"))
        digest.update(b"\0")
        digest.update(normalized.encode("utf-8"))
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def current_strategy_ast_hash() -> str:
    package = Path(__file__).resolve().parent
    sources = {
        name: (package / name).read_text(encoding="utf-8")
        for name in ("strategy.py", "indicators.py")
    }
    return strategy_ast_hash(sources)
