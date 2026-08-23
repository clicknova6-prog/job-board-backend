from fastapi import FastAPI
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.api.routers.admin_affiliate import router as admin_affiliate_router
from app.api.routers.admin_providers import router as admin_providers_router
from app.api.routers.redirect import router as affiliate_redirect_router
from app.api.routers.sitemaps import router as sitemap_router
from app.api.v1.jobs import router as jobs_v1_router
from app.auth.admin_router import router as admin_auth_router
from app.auth.router import router as auth_router
from app.core.rate_limit import limiter

app = FastAPI(title="Job Board Backend")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.include_router(auth_router)
app.include_router(admin_auth_router)
app.include_router(admin_affiliate_router)
app.include_router(admin_providers_router)
app.include_router(affiliate_redirect_router)
app.include_router(sitemap_router)
app.include_router(jobs_v1_router, prefix="/api/v1", tags=["Jobs"])


@app.get("/")
def root():
    return {"message": "Job Board Backend is running!"}
