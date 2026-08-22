from fastapi import FastAPI
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.auth.admin_router import router as admin_auth_router
from app.auth.router import router as auth_router
from app.core.rate_limit import limiter
from app.jobs.router import router as jobs_router

app = FastAPI(title="Job Board Backend")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.include_router(auth_router)
app.include_router(admin_auth_router)
app.include_router(jobs_router, prefix="/jobs", tags=["Jobs"])


@app.get("/")
def root():
    return {"message": "Job Board Backend is running!"}
