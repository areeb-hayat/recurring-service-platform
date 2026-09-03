"""Provider implementations. The only place a vendor name may appear.

P0 §2.1 and A-SLOT-5: a domain module imports :mod:`app.ports` and never reaches
in here. Selection happens at the API layer, from configuration, so swapping a
provider is a settings change rather than an edit to business logic.

``comms/`` is the only subpackage that exists. ``speech/`` and ``ai/`` belong to
P9 and P8; the architecture guard asserts they are still absent, and no real
communication adapter exists yet either — P7 records what to send and delivers
through the mock. P10 adds the first one that makes a network call.
"""
