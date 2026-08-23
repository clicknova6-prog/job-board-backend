"""Safety-net CLI: reset an administrator password without email delivery."""

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

# scripts/ is not a package and Python puts this file's own directory on
# sys.path, not the repo root, so `import app...` fails without this.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from app.db.models import AdminUser, RefreshToken
from app.db.session import SessionLocal
from app.services.auth.admin_auth_service import hash_password


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reset a job-board administrator password."
    )
    parser.add_argument("--email", required=True, help="Administrator email address")
    parser.add_argument(
        "--new-password",
        required=True,
        help="New administrator password",
    )
    args = parser.parse_args()
    args.email = args.email.strip().lower()
    if not args.email:
        parser.error("--email must not be empty")
    if not args.new_password:
        parser.error("--new-password must not be empty")
    return args


def main() -> int:
    args = _parse_args()

    with SessionLocal() as session:
        admin = session.scalar(select(AdminUser).where(AdminUser.email == args.email))
        if admin is None:
            print(
                f"Error: administrator '{args.email}' does not exist",
                file=sys.stderr,
            )
            return 1

        reset_at = datetime.now(tz=UTC)
        admin.password_hash = hash_password(args.new_password)
        admin.updated_at = reset_at
        session.execute(
            update(RefreshToken)
            .where(
                RefreshToken.admin_user_id == admin.id,
                RefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=reset_at)
        )

        try:
            session.commit()
        except SQLAlchemyError as error:
            session.rollback()
            print(
                f"Error: could not reset administrator '{args.email}': {error}",
                file=sys.stderr,
            )
            return 1

        print(
            f"Reset administrator password "
            f"(id={admin.id}, email={admin.email}, role={admin.role.value})"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
