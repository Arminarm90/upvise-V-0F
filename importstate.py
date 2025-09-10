import os
import sys
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.storage.state import SQLiteStateStore

def db_has_data(db_path: str) -> bool:
    try:
        s = SQLiteStateStore(db_path=db_path)
        with s._locked_cursor() as cur:
            cur.execute("SELECT 1 FROM chats LIMIT 1")
            r = cur.fetchone()
        s.close()
        return bool(r)
    except Exception:
        return False

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--json", "-j", required=True, help="Path to old JSON file (subs.json)")
    p.add_argument("--db", "-d", default="state.db", help="Target sqlite DB path")
    p.add_argument("--force", action="store_true", help="If DB already has chats, overwrite / import anyway")
    args = p.parse_args()

    json_path = Path(args.json)
    if not json_path.exists():
        print("ERROR: json file not found:", json_path)
        sys.exit(2)

    db_path = str(Path(args.db))

    # backup DB if exists
    if Path(db_path).exists():
        bak = Path(db_path + ".bak")
        print(f"Backing up existing DB -> {bak}")
        Path(db_path).replace(bak)

    store = SQLiteStateStore(db_path=db_path)

    if db_has_data(db_path) and not args.force:
        print("Target DB already contains data. Use --force to import anyway. Aborting.")
        store.close()
        sys.exit(3)

    print("Importing", json_path, "into", db_path)
    try:
        store.import_from_json_file(str(json_path))
        print("Import completed.")
    except Exception as ex:
        print("Import failed:", ex)
    finally:
        store.close()

if __name__ == "__main__":
    main()
