"""One-time bootstrap: ensure the jobg8 Provider row exists."""

import sys
from pathlib import Path

# scripts/ is not a package and Python puts this file's own directory on
# sys.path, not the repo root, so `import app...` fails without this.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import select

from app.db.models import Provider
from app.db.session import SessionLocal


def main() -> None:
    with SessionLocal() as session:
        existing = session.scalar(select(Provider).where(Provider.name == "jobg8"))
        if existing is not None:
            print(f"Provider 'jobg8' already exists (id={existing.id}); config not changed")
            return

        provider = Provider(
            name="jobg8",
            format="xml",
            archive_type="zip",
            is_active=True,
            config={
                "anomaly_drop_threshold_pct": 20,
                "anomaly_rejection_rate_pct": 15,
            },
        )
        session.add(provider)
        session.commit()
        print(f"Created provider 'jobg8' (id={provider.id})")


if __name__ == "__main__":
    main()
