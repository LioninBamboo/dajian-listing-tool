import json
import sqlite3

from scripts.replay_qc_experiences import main
from src.services.qc_experience_seed import seed_initial_qc_experiences


def test_replay_cli_writes_replay_and_recurrence_reports(tmp_path):
    db_path = tmp_path / "ebay_collection.db"
    history_path = tmp_path / "history.json"
    clean_path = tmp_path / "clean.json"
    replay_path = tmp_path / "replay.json"
    recurrence_path = tmp_path / "recurrence.json"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE collected_products (id INTEGER PRIMARY KEY)")
        seed_initial_qc_experiences(conn)
    history_path.write_text(json.dumps({"issues": []}), encoding="utf-8")
    clean_path.write_text(json.dumps({"results": []}), encoding="utf-8")

    exit_code = main(
        [
            "--db",
            str(db_path),
            "--historical",
            str(history_path),
            "--clean",
            str(clean_path),
            "--replay-report",
            str(replay_path),
            "--recurrence-report",
            str(recurrence_path),
            "--timestamp",
            "20260804_190006",
        ]
    )

    assert exit_code == 0
    assert json.loads(replay_path.read_text(encoding="utf-8"))["summary"]["case_count"] == 6
    assert json.loads(recurrence_path.read_text(encoding="utf-8"))["summary"]["rule_count"] == 6
