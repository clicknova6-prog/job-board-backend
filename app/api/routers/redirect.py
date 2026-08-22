"""Root-level public affiliate redirect route."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.affiliate_repositories import AffiliateRepository
from app.db.async_session import get_async_session

router = APIRouter(tags=["Affiliate Redirect"])

JOB_UNAVAILABLE_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Job no longer available</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 0; padding: 3rem 1rem;
           color: #1f2937; background: #f8fafc; text-align: center; }
    main { max-width: 36rem; margin: 0 auto; padding: 2rem; background: white;
           border-radius: 0.75rem; box-shadow: 0 4px 16px #0001; }
    a { color: #2563eb; }
  </style>
</head>
<body>
  <main>
    <h1>This job is no longer available</h1>
    <p>The listing may have expired or been removed.</p>
    <a href="/">Return to the homepage</a>
  </main>
</body>
</html>"""


@router.get("/r/{short_hash}", response_model=None)
async def redirect_affiliate_link(
    short_hash: str,
    session: Annotated[AsyncSession, Depends(get_async_session)],
) -> RedirectResponse | HTMLResponse:
    """Redirect valid hashes even while their jobs are soft-deleted."""
    link = await AffiliateRepository(session).get_by_short_hash(short_hash)
    if link is None:
        return HTMLResponse(content=JOB_UNAVAILABLE_HTML, status_code=404)
    return RedirectResponse(url=link["apply_url"], status_code=302)
