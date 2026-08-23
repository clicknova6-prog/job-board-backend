"""One-time bootstrap: create the first administrator account."""

import argparse
import sys
from pathlib import Path

# scripts/ is not a package and Python puts this file's own directory on
# sys.path, not the repo root, so `import app...` fails without this.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.models import AdminRole, AdminUser
from app.db.session import SessionLocal
from app.services.auth.admin_auth_service import hash_password


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create the first job-board administrator account."
    )
    parser.add_argument("--email", required=True, help="Administrator email address")
    parser.add_argument("--password", required=True, help="Administrator password")
    parser.add_argument(
        "--role",
        choices=("admin", "super_admin"),
        default="super_admin",
        help="Administrator role (default: super_admin)",
    )
    args = parser.parse_args()
    args.email = args.email.strip().lower()
    if not args.email:
        parser.error("--email must not be empty")
    if not args.password:
        parser.error("--password must not be empty")
    return args


def main() -> int:
    args = _parse_args()

    with SessionLocal() as session:
        existing_id = session.scalar(
            select(AdminUser.id).where(AdminUser.email == args.email)
        )
        if existing_id is not None:
            print(
                f"Error: administrator '{args.email}' already exists "
                f"(id={existing_id})",
                file=sys.stderr,
            )
            return 1

        admin = AdminUser(
            email=args.email,
            password_hash=hash_password(args.password),
            role=AdminRole(args.role),
            is_active=True,
        )
        session.add(admin)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            print(
                f"Error: administrator '{args.email}' already exists",
                file=sys.stderr,
            )
            return 1

        print(
            f"Created administrator "
            f"(id={admin.id}, email={admin.email}, role={admin.role.value})"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
