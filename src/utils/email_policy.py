"""Email silence policy for multi-store ops.

Default (EMAIL_FORCE_ROUTINE unset/off):
  - Routine success / empty / all-green reports stay local under reports/
  - SMTP only for failures, CRITICAL/HIGH issues, login drop, order mismatch,
    CRO plunge, and anything needing human approval

Set EMAIL_FORCE_ROUTINE=1 to restore legacy "email everything" behavior.
"""
from __future__ import annotations

import os


def routine_smtp_forced() -> bool:
    return os.getenv("EMAIL_FORCE_ROUTINE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def should_smtp(*, has_issues: bool = False, force: bool = False) -> bool:
    """Return True when SMTP delivery should run (local report always saved)."""
    if force or routine_smtp_forced():
        return True
    return bool(has_issues)
