from __future__ import annotations

import argparse
import hashlib
import secrets
from uuid import uuid4

from auth_service import AuthService
from cloud_api import connect, initialize_schema


def main() -> int:
    parser = argparse.ArgumentParser(description="Provision a HawkHive cloud customer and first device.")
    parser.add_argument("--customer-id", required=True)
    parser.add_argument("--customer-name", required=True)
    parser.add_argument("--site-id", required=True)
    parser.add_argument("--site-name", required=True)
    parser.add_argument("--room-id", required=True)
    parser.add_argument("--room-name", default="Cleanroom 1")
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--device-name", default="Device 1")
    parser.add_argument("--device-host", required=True)
    parser.add_argument("--device-port", type=int, default=502)
    parser.add_argument("--device-slave", type=int, default=1)
    parser.add_argument("--username", required=True)
    parser.add_argument("--password")
    parser.add_argument("--profile-name", default="Class 100K")
    parser.add_argument("--particle-0-5-max", "--particle-5-max", dest="particle_0_5_max", type=float, default=100000)
    parser.add_argument("--temperature-min", type=float, default=18)
    parser.add_argument("--temperature-max", type=float, default=25)
    parser.add_argument("--humidity-min", type=float, default=40)
    parser.add_argument("--humidity-max", type=float, default=70)
    args = parser.parse_args()
    initialize_schema()
    password = args.password or secrets.token_urlsafe(12)
    salt, password_hash = AuthService.password_hash(password)
    edge_token = secrets.token_urlsafe(32)
    edge_hash = hashlib.sha256(edge_token.encode("utf-8")).hexdigest()
    with connect() as db:
        db.execute(
            "INSERT INTO customers(id,name) VALUES(%s,%s) ON CONFLICT(id) DO UPDATE SET name=excluded.name",
            (args.customer_id, args.customer_name),
        )
        db.execute(
            """
            INSERT INTO sites(id,customer_id,name) VALUES(%s,%s,%s)
            ON CONFLICT(id) DO UPDATE SET customer_id=excluded.customer_id,name=excluded.name,
                active_collector_instance=NULL,active_collector_seen_at=NULL,updated_at=now()
            """,
            (args.site_id, args.customer_id, args.site_name),
        )
        db.execute(
            """
            INSERT INTO cleanrooms(id,customer_id,name,sort_order) VALUES(%s,%s,%s,1)
            ON CONFLICT(id) DO UPDATE SET customer_id=excluded.customer_id,name=excluded.name
            """,
            (args.room_id, args.customer_id, args.room_name),
        )
        db.execute(
            """
            INSERT INTO devices(
                id,customer_id,cleanroom_id,site_id,name,host,tcp_port,slave,sort_order
            ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,1)
            ON CONFLICT(id) DO UPDATE SET customer_id=excluded.customer_id,
                cleanroom_id=excluded.cleanroom_id,site_id=excluded.site_id,name=excluded.name,
                host=excluded.host,tcp_port=excluded.tcp_port,slave=excluded.slave,
                enabled=true,disabled_at=NULL,updated_at=now()
            """,
            (
                args.device_id,args.customer_id,args.room_id,args.site_id,args.device_name,
                args.device_host,args.device_port,args.device_slave,
            ),
        )
        db.execute(
            """
            INSERT INTO thresholds(cleanroom_id,customer_id,profile_name,particle_0_5_max,particle_5_max,
                temperature_min,temperature_max,humidity_min,humidity_max,alarm_delay_seconds)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,300)
            ON CONFLICT(cleanroom_id) DO UPDATE SET customer_id=excluded.customer_id,
                profile_name=excluded.profile_name,particle_0_5_max=excluded.particle_0_5_max,
                temperature_min=excluded.temperature_min,temperature_max=excluded.temperature_max,
                humidity_min=excluded.humidity_min,humidity_max=excluded.humidity_max,
                alarm_delay_seconds=300
            """,
            (
                args.room_id,args.customer_id,args.profile_name,args.particle_0_5_max,args.particle_0_5_max,
                args.temperature_min,args.temperature_max,args.humidity_min,args.humidity_max,
            ),
        )
        db.execute(
            """
            INSERT INTO customer_users(id,customer_id,username,display_name,password_salt,password_hash)
            VALUES(%s,%s,%s,%s,%s,%s)
            ON CONFLICT(username) DO UPDATE SET customer_id=excluded.customer_id,
                display_name=excluded.display_name,password_salt=excluded.password_salt,
                password_hash=excluded.password_hash,enabled=true
            """,
            (uuid4(),args.customer_id,args.username,args.customer_name,salt,password_hash),
        )
        # Re-running provisioning is a credential rotation, not an additional
        # collector authorization. A site may have only one active collector
        # instance, so old credentials must stop working immediately.
        db.execute(
            "UPDATE edge_tokens SET enabled=false WHERE customer_id=%s AND site_id=%s AND enabled=true",
            (args.customer_id, args.site_id),
        )
        db.execute(
            "INSERT INTO edge_tokens(token_hash,customer_id,site_id,label) VALUES(%s,%s,%s,%s)",
            (edge_hash,args.customer_id,args.site_id,f"{args.customer_name} edge"),
        )
    print("Customer login and edge token (shown once):")
    print(f"Username: {args.username}")
    print(f"Password: {password}")
    print(f"Edge token: {edge_token}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
