# GIGA New Arrivals checkpoint + login hard-stop (P0-1)

Daily GIGA NA collect→publish must be **resumable** and must **surface login
loss** to the parent agent — never silently scrape masked/empty pages all day.

## Checkpoint file

Write/update atomically (temp then rename):

- Box mirror: `/workspace/giga_new_arrivals_checkpoint_YYYY-MM-DD.json`
- Optional ZBook copy: `C:\Users\poonx\Dajian_Listing_Tool\cache\giga_new_arrivals_checkpoint_YYYY-MM-DD.json`

Required fields:

```json
{
  "job": "giga-new-arrivals-collect-publish",
  "run_id": "giga-new-arrivals_YYYYMMDDTHHMM",
  "date": "YYYY-MM-DD",
  "phase": "SCRAPE|GATE|HEART|COLLECT|ANALYZE|DRYRUN|LIVE|COMPLETE",
  "status": "running|interrupted|blocked_user|done",
  "hard_stop": false,
  "hard_stop_reason": null,
  "login_ok": true,
  "login_detail": "Hi xiaoting … / Login wall",
  "resume_from": "gate_heart_collect",
  "steps_done": [],
  "blockers": [],
  "updated_at": "ISO-8601 +08:00"
}
```

Helper: `python scripts/giga_na_checkpoint.py write --phase SCRAPE --resume-from gate ...`
(see script `--help`).

## HARD_STOP_LOGGED_OUT

When GIGA session shows Login / masked getList / no `Hi xiaoting`:

1. Stop the batch cleanly (do not invent favorites/collect/LIVE).
2. Checkpoint: `status=blocked_user`, `hard_stop=true`,
   `hard_stop_reason="HARD_STOP_LOGGED_OUT"`, `login_ok=false`.
3. Parent-facing ping (chat report — **do not invent Slack sends**): one short
   Chinese line that includes the exact token **`HARD_STOP_LOGGED_OUT`**, the
   checkpoint path, and who must re-login (box Fork / account hint).
4. After Sergey re-logs: re-read checkpoint and **resume from `resume_from`**,
   not from zero.

Skills: `giga-session-preflight-checkpoint`, `zbook-batch-checkpoint-resume`.
