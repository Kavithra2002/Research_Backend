"""MongoDB cache for the full CSE listed-company directory (allSecurityCode)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
CSE_LISTED_COLLECTION = "cse_listed_companies"


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    path = BACKEND_DIR / ".env"
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip("'\"")
    return env


def save_trade_summary_to_db(rows: list[dict[str, Any]]) -> None:
    from pymongo import MongoClient, ReplaceOne

    env = load_env()
    uri = env.get("MONGO_URI", "mongodb://localhost:27017")
    db_name = env.get("MONGO_DB_NAME", "Research_Project")
    synced_at = datetime.now(tz=timezone.utc)

    seen: set[str] = set()
    ops: list[ReplaceOne] = []
    for row in rows:
        symbol = str(row.get("symbol") or "").strip()
        name = str(row.get("name") or "").strip()
        if not symbol or not name or symbol in seen:
            continue
        seen.add(symbol)
        ops.append(
            ReplaceOne(
                {"symbol": symbol},
                {"symbol": symbol, "name": name, "synced_at": synced_at},
                upsert=True,
            )
        )

    if not ops:
        return

    client = MongoClient(uri, serverSelectionTimeoutMS=5000)
    try:
        coll = client[db_name][CSE_LISTED_COLLECTION]
        coll.bulk_write(ops, ordered=False)
        coll.delete_many({"symbol": {"$nin": list(seen)}})
    finally:
        client.close()


def load_trade_summary_from_db() -> list[dict[str, Any]]:
    from pymongo import MongoClient

    env = load_env()
    uri = env.get("MONGO_URI", "mongodb://localhost:27017")
    db_name = env.get("MONGO_DB_NAME", "Research_Project")

    client = MongoClient(uri, serverSelectionTimeoutMS=5000)
    try:
        docs = list(
            client[db_name][CSE_LISTED_COLLECTION]
            .find({}, {"_id": 0, "name": 1, "symbol": 1})
            .sort("name", 1)
        )
    finally:
        client.close()

    out: list[dict[str, Any]] = []
    for doc in docs:
        name = str(doc.get("name") or "").strip()
        symbol = str(doc.get("symbol") or "").strip()
        if name and symbol:
            out.append({"name": name, "symbol": symbol})
    return out
