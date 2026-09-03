"""Reminder scheduling, eligibility, delivery and history (P0 §10, REM-1..REM-8).

    schedule.py   the tenant's stage configuration, and the catch-up rule
    engine.py     eligibility, the current amount, generation and dispatch
    runner.py     the same-day job guard and the per-tenant round
    reporting.py  the owner's work list and one reminder's attempt history

The application decides who, which stage, how much and whether to suppress. The
communication provider only delivers -- and it reaches this package through
``app.ports.comms``, never the other way round.
"""
