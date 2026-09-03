"""Scheduled job entrypoints, driven by the host's cron (P0 §2.1, §12).

No scheduler, no broker and no worker process: an authenticated HTTP endpoint
that the host's crontab calls. See ``daily.py``.
"""
