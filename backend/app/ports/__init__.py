"""Port (Protocol) definitions.

P0 §2.1 freezes four ports: ``CommunicationProvider``, ``SpeechToTextProvider``,
``SearchInterpreter`` and ``OperationalIntentInterpreter``.

**None is defined yet.** P1 implements no adapter and no feature that consumes
one, and a Protocol with no implementation and no caller is speculative code.
Each port is declared by the package that first needs it (P7, P8, P9).

The architecture guard in ``tests/test_architecture.py`` already enforces the
domain -> adapters prohibition (A-SLOT-5), so the boundary is protected from the
first commit even though the directory is otherwise empty.
"""
