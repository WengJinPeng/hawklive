from __future__ import annotations

import argparse
import hashlib
import secrets
import time

from cloud_api import connect, initialize_schema


def main() -> int:
    parser = argparse.ArgumentParser(description="Provision a customer site and edge upload token.")
    parser.add_argument("--customer-id", required=True)
    parser.add_argument("--customer-name", required=True)
    parser.add_argument("--site-id", required=True)
    parser.add_argument("--site-name", required=True)
    args = parser.parse_args()
    initialize_schema()
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with connect() as db:
        db.execute(
            "INSERT INTO customers(id, name) VALUES (%s, %s) ON CONFLICT(id) DO UPDATE SET name=excluded.name",
            (args.customer_id, args.customer_name),
        )
        db.execute(
            """
            INSERT INTO sites(id, customer_id, name) VALUES (%s, %s, %s)
            ON CONFLICT(id) DO UPDATE SET
              customer_id=excluded.customer_id,name=excluded.name,
              active_collector_instance=NULL,active_collector_seen_at=NULL,
              updated_at=now()
            """,
            (args.site_id, args.customer_id, args.site_name),
        )
        # Provisioning an existing node is credential rotation. Keeping two active
        # credentials would let two machines poll the same Modbus devices.
        db.execute(
            "UPDATE edge_tokens SET enabled=false WHERE customer_id=%s AND site_id=%s",
            (args.customer_id, args.site_id),
        )
        db.execute(
            "INSERT INTO edge_tokens(token_hash, customer_id, site_id, label) VALUES (%s, %s, %s, %s)",
            (token_hash, args.customer_id, args.site_id, f"Provisioned {time.strftime('%Y-%m-%d')}"),
        )
    print("Edge token (shown once):")
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
