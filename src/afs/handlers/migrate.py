"""Lambda: apply schema migrations (and, once per environment, create the DB roles).

Invoke with {} to run pending migrations as afs_migrator (IAM auth).
Invoke with {"master_password": "..."} to first run the role bootstrap as the
RDS master user. The password comes from the invoker (`afs remote-migrate
--bootstrap`), because this function sits in a VPC with no route to Secrets
Manager. Never log the event.
"""

from afs import db


def handler(event: dict, context) -> dict:
    password = event.get("master_password")
    if password:
        with db.connect_master(password) as conn:
            db.bootstrap_roles(conn)

    with db.connect() as conn:
        applied = db.migrate(conn)

    return {"bootstrapped": bool(password), "applied": applied}
