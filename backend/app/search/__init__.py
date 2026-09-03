"""Structured customer search and deterministic customer identification (P0 §2.1).

The package answers two different questions with one body of matching rules:

* *"show me who matches this"* — :mod:`app.search.query`, a tenant-scoped,
  parameterised query built from a closed filter object;
* *"which customer is this?"* — :mod:`app.search.resolver`, which answers
  ``RESOLVED``, ``AMBIGUOUS`` or ``NOT_FOUND`` and **never** guesses.

Both are channel-independent on purpose. P8 drives them from typed website
search; a later package drives the same resolver from a speech transcript or an
inbound text message. There is deliberately no per-channel matching code for
those packages to grow into.

Nothing is re-exported here. ``app.customers`` writes normalized comparison keys
using :mod:`app.search.normalize`, so a package-level import of the query and
resolver modules would close a cycle; every caller imports the module it wants,
which is how the rest of this codebase reads anyway.
"""
