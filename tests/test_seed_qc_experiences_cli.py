import json
import sqlite3

from scripts.seed_qc_experiences import main


def test_cli_can_run_directly_and_writes_json_report(tmp_path):
    db_path = tmp_path / "ebay_collection.db"
    report_path = tmp_path / "qc-seed-report.json"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE collected_products (id INTEGER PRIMARY KEY)")

    exit_code = main(
        [
            "--db",
            str(db_path),
            "--backup-dir",
            str(tmp_path / "backups"),
            "--report",
            str(report_path),
            "--timestamp",
            "20260804_190005",
        ]
    )

    assert exit_code == 0
    assert json.loads(report_path.read_text(encoding="utf-8"))["status"] == "dry_run"
