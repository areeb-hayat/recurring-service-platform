"""Constrained scalar types shared by every request and operation payload.

These live in ``core`` because they are wire primitives, not the property of one
transport. The same operation arrives two ways — as an ordinary HTTP request body
and as an envelope inside a sync batch (P0 §7.2) — and SYN-8 requires both to be
validated identically. One definition, imported by both, is what makes that
structural rather than a promise to keep two copies in step.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import StringConstraints

__all__ = ["QuantityStr"]

# Quantity as a string: "2", "1.5", "0.333". Rejects JSON floats outright, which
# cannot represent 0.1 exactly (FIN-2). The decimal parse itself happens in the
# domain, where the error message is a business one.
QuantityStr = Annotated[str, StringConstraints(strip_whitespace=True, max_length=24)]
