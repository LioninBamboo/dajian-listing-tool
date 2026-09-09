# Root Structure Audit

Root: C:\Users\poonx\Dajian_Listing_Tool
Items: 51

## Summary
- active_root_entrypoint: 20
- local_config_file: 1
- local_workspace_directory: 8
- root_directory: 11
- root_python_candidate: 2
- runtime_root_file: 8
- tooling_directory: 1

## Review Candidates
- .qwen (local_workspace_directory): ignore_or_keep_local_only -> .gitignore or local-only workspace convention
- .streamlit (local_workspace_directory): ignore_or_keep_local_only -> .gitignore or local-only workspace convention
- .venv-1 (local_workspace_directory): ignore_or_keep_local_only -> .gitignore or local-only workspace convention
- .vscode (local_workspace_directory): ignore_or_keep_local_only -> .gitignore or local-only workspace convention
- auto_favorite_gigacloud.py (root_python_candidate): review_then_move -> scripts/ for maintained workflows or tools/local_diagnostics/ for probes
- backups (local_workspace_directory): ignore_or_keep_local_only -> .gitignore or local-only workspace convention
- daily_terapeak_report.py (root_python_candidate): review_then_move -> scripts/ for maintained workflows or tools/local_diagnostics/ for probes
- debug_server.log (runtime_root_file): review_then_move_or_ignore -> cache/ or logs/ with compatibility shim if referenced
- ebay_collection.db (runtime_root_file): review_then_move_or_ignore -> cache/ or logs/ with compatibility shim if referenced
- ebay_collection.db-shm (runtime_root_file): review_then_move_or_ignore -> cache/ or logs/ with compatibility shim if referenced
- ebay_collection.db-wal (runtime_root_file): review_then_move_or_ignore -> cache/ or logs/ with compatibility shim if referenced
- ebay_tokens.db (runtime_root_file): review_then_move_or_ignore -> cache/ or logs/ with compatibility shim if referenced
- favorite_progress.json (runtime_root_file): review_then_move_or_ignore -> cache/ or logs/ with compatibility shim if referenced
- scratch (local_workspace_directory): ignore_or_keep_local_only -> .gitignore or local-only workspace convention
- sku_product_id_map.json (runtime_root_file): review_then_move_or_ignore -> cache/ or logs/ with compatibility shim if referenced
- tmp_imgs (local_workspace_directory): ignore_or_keep_local_only -> .gitignore or local-only workspace convention
- tmp_pdfs (local_workspace_directory): ignore_or_keep_local_only -> .gitignore or local-only workspace convention
- unfavorited_skus.txt (runtime_root_file): review_then_move_or_ignore -> cache/ or logs/ with compatibility shim if referenced
