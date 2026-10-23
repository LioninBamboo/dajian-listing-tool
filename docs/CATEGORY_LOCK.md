# Category lock reconcile

PR-B ports the GrovePop fail-closed category lock onto Main's
`gate_publish_category_choice` / `apply_category_write_gate` choke point.

## Who may write a category id

A category id may be written to eBay or to the local DB only when it comes
from:

1. the stored id,
2. the approved pin (`categoryApprovedId`), or
3. an explicit human `--override-category-lock` run with an explicit target
   id (`--category-id` / `--target-category SKU=ID`).

Matcher / suggestion output stays report-only. `APPROVED_AUTO_REMAPS` stays
empty. The high-insertion-fee blacklist still wins.

## Fail-closed lock

`category_approval_flag_set` is true whenever `optimization.categoryApproved`
is truthy, including a blank or mismatched pin.

- `is_category_approved` keeps the pin-equals-stored check (legacy flag-only
  rows still preserve the current leaf).
- Callers that decide whether automation may *change* the category must use
  `blocks_automated_category_change`.
- `locked_category_write_error` / `plan_locked_override_revision` gate the
  remap scripts.

## Tree-aware validation (Aqua)

Trees 0 and 100 stay loaded. Lookups for a **new** target use
`(tree_id, category_id)` so a colliding id cannot resolve to the other
tree's name/parent.

- Aqua Trading / SiteID-100 path → tree 100
- Aqua `TREE0_FORCE_INVENTORY_CATEGORIES` / Inventory path → tree 0
- Main / GrovePop → tree 0

A new target that is only a leaf in the other tree is rejected
(report-only), never substituted. Stored ids keep their 4250f71 keep/hold
outcome. Aqua READY furniture leaves `20487` / `88057` / `262980` are
reported as `needs_manual_motors_leaf_review` (decision B3 default: no hold).

No static `data/motors_category_tree_100_leaves.json` (decision B2).

## Open decisions (defaults in this PR)

- **B1:** approved leaf + tree unavailable → still `hold_tree_unavailable`
  on unpublished rows. See `TODO(decision B1)` in `apply_category_write_gate`.
- **B2:** no static tree-100 snapshot.
- **B3:** do not hold Aqua READY rows whose stored leaf is a tree-0 furniture
  id; a human assigns the Motors leaf.
