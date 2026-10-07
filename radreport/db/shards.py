"""Operating the shards: migrate every one, see where labs live, plan what adding a shard would move, pin a lab.

Order: python -m radreport.db.shards migrate --owner-url-template 'postgresql+psycopg://owner@host/{db}'
-> where -> plan --add shard3 -> pin <lab-id> <shard> --reason "big lab".
"""

from __future__ import annotations

import argparse
import uuid

from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.engine import make_url

from radreport.db import sharding
from radreport.db.models.tenancy import Tenant
from radreport.db.session import system_session


def migrate(owner_url_template: str | None) -> None:
    """alembic upgrade head on every shard; migrations run as each database's owner."""
    for name, url in sharding.shard_map().urls.items():
        target = owner_url_template.format(db=make_url(url).database) if owner_url_template else url
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", target.replace("%", "%%"))
        command.upgrade(config, "head")
        print(f"{name}: at head")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    up = sub.add_parser("migrate")
    up.add_argument("--owner-url-template", help="URL with {db} for the database name; defaults to each shard's own URL")
    sub.add_parser("where")
    plan = sub.add_parser("plan")
    plan.add_argument("--add", required=True)
    pin = sub.add_parser("pin")
    pin.add_argument("lab_id", type=uuid.UUID)
    pin.add_argument("shard")
    pin.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    if args.command == "migrate":
        migrate(args.owner_url_template)
        return
    with system_session() as session:
        labs = [(t.id, t.slug) for t in session.execute(select(Tenant).order_by(Tenant.slug)).scalars()]
        if args.command == "where":
            for lab_id, slug in labs:
                print(f"{slug:<30} {sharding.lab_shard(lab_id)}")
        elif args.command == "plan":
            moving = set(sharding.moves_if_added([lab for lab, _ in labs], args.add))
            print(f"adding {args.add} moves {len(moving)} of {len(labs)} labs")
            for lab_id, slug in labs:
                if lab_id in moving:
                    print(f"  {slug}: {sharding.lab_shard(lab_id)} -> {args.add}")
        else:
            sharding.pin_lab(session, args.lab_id, args.shard, reason=args.reason)
            print(f"pinned {args.lab_id} to {args.shard}")


if __name__ == "__main__":
    main()
