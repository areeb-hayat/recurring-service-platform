"""Helpers for source-level guard tests.

Scanning raw file text produces false positives: a docstring that says "never
pruned" is not a pruning mechanism. These helpers strip comments and string
literals so the guards look at *code* only.
"""

from __future__ import annotations

import io
import pathlib
import tokenize

APP_ROOT = pathlib.Path(__file__).resolve().parents[1] / "app"


def code_only(path: pathlib.Path) -> str:
    """Return the file's source with comments and string literals removed."""
    source = path.read_text(encoding="utf-8")
    out: list[str] = []
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for tok_type, tok_str, _, _, _ in tokens:
            if tok_type in (tokenize.COMMENT, tokenize.STRING):
                continue
            out.append(tok_str)
    except (tokenize.TokenError, IndentationError):  # pragma: no cover
        return source
    return " ".join(out)


def python_files() -> list[pathlib.Path]:
    return sorted(APP_ROOT.rglob("*.py"))


def module_name(path: pathlib.Path) -> str:
    rel = path.relative_to(APP_ROOT.parent).with_suffix("")
    return ".".join(rel.parts)
