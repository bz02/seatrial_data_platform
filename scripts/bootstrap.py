"""Prepare a database for a real (non-demo) deployment, and create accounts.

  python scripts/bootstrap.py init
      Create the schema, the platform org, and the marketplace task catalog. Safe to run twice.
  python scripts/bootstrap.py admin --email you@company.com --name "Your Name"
      Create a platform admin and print a one-time password.
  python scripts/bootstrap.py org --name "Acme Corp" --admin-email it@acme.com --admin-name "IT Admin"
      Create an enterprise org with its first org admin, and print a one-time password.
"""
import argparse
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from seatrail import auth, db, enterprise, experts, tasks  # noqa: E402


def init() -> None:
    db.x("INSERT OR IGNORE INTO orgs (id, name, kind, policy, created_at) VALUES (0, 'SeaTrail Marketplace',"
         " 'platform', '{}', ?)", db.now())
    for template in tasks.TEMPLATES:
        for seed in range(1, 5):
            tasks.create_task(template, seed)
    experts.ensure_qualification_tasks(tasks.TEMPLATES)
    print("Database ready:", db.q1("SELECT COUNT(*) n FROM tasks")["n"], "tasks")


def _user(org_id: int, role: str, name: str, email: str) -> str:
    email = email.strip().lower()
    if db.q1("SELECT id FROM users WHERE lower(email)=?", email):
        sys.exit(f"An account for {email} already exists.")
    pw = secrets.token_urlsafe(12)
    db.x("INSERT INTO users (org_id, role, name, email, password_hash, created_at) VALUES (?,?,?,?,?,?)",
         org_id, role, name, email, auth.hash_password(pw), db.now())
    return pw


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    a = sub.add_parser("admin")
    a.add_argument("--email", required=True)
    a.add_argument("--name", required=True)
    o = sub.add_parser("org")
    o.add_argument("--name", required=True)
    o.add_argument("--admin-email", required=True)
    o.add_argument("--admin-name", required=True)
    o.add_argument("--allowed-host", action="append", default=[],
                   help="internal app host the recorder may capture, repeatable")
    args = ap.parse_args()
    if args.cmd == "init":
        init()
    elif args.cmd == "admin":
        init()
        pw = _user(0, "admin", args.name, args.email)
        print(f"Admin created. Sign in as {args.email} with one-time password: {pw}\nChange it after signing in.")
    else:
        policy = {**enterprise.DEFAULT_POLICY}
        if args.allowed_host:
            policy["allowed_hosts"] = args.allowed_host
        oid = db.x("INSERT INTO orgs (name, kind, policy, created_at) VALUES (?, 'enterprise', ?, ?)",
                   args.name, db.dumps(policy), db.now())
        pw = _user(oid, "org_admin", args.admin_name, args.admin_email)
        print(f"Org {args.name} (id {oid}) created. Org admin {args.admin_email}, one-time password: {pw}")


if __name__ == "__main__":
    main()
