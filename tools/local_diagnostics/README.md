# Local Diagnostics

This directory holds one-off local probes and debugging helpers that are not part of
the maintained scheduler or application entrypoints.

Rules:
- Keep outputs in the project root `reports/`, `logs/`, or explicit debug files only when needed.
- Prefer stable project-root resolution via `Path(__file__).resolve().parents[2]`.
- Do not place new ad hoc probes back in the repository root.
