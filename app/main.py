import logging

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.routers.admin_affiliate import router as admin_affiliate_router
from app.api.routers.admin_imports import router as admin_imports_router
from app.api.routers.admin_providers import router as admin_providers_router
from app.api.routers.redirect import router as affiliate_redirect_router
from app.api.routers.sitemaps import router as sitemap_router
from app.api.v1.jobs import router as jobs_v1_router
from app.api.v1.me import router as me_v1_router
from app.auth.admin_router import router as admin_auth_router
from app.auth.router import router as auth_router
from app.core.config import CORS_ALLOWED_ORIGINS
from app.core.errors import error_code, error_content
from app.core.rate_limit import limiter, rate_limit_exceeded_handler

logger = logging.getLogger(__name__)


app = FastAPI(title="Job Board Backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)
app.state.limiter = limiter


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(
    request: Request,
    exc: StarletteHTTPException,
) -> JSONResponse:
    del request
    message = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content=error_content(code=error_code(exc.status_code), message=message),
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    del request
    return JSONResponse(
        status_code=422,
        content=error_content(
            code="VALIDATION_ERROR",
            message="Request validation failed",
            details=jsonable_encoder(exc.errors()),
        ),
    )


@app.exception_handler(RateLimitExceeded)
async def rate_limit_exception_handler(
    request: Request,
    exc: RateLimitExceeded,
) -> JSONResponse:
    return rate_limit_exceeded_handler(request, exc)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception(
        "Unhandled exception while processing %s %s",
        request.method,
        request.url.path,
        exc_info=exc,
    )
    return JSONResponse(
        status_code=500,
        content=error_content(
            code="INTERNAL_ERROR",
            message="Internal server error",
        ),
    )

app.include_router(auth_router)
app.include_router(admin_auth_router)
app.include_router(admin_affiliate_router)
app.include_router(admin_imports_router)
app.include_router(admin_providers_router)
app.include_router(affiliate_redirect_router)
app.include_router(sitemap_router)
app.include_router(jobs_v1_router, prefix="/api/v1", tags=["Jobs"])
app.include_router(me_v1_router, prefix="/api/v1")


@app.get("/")
def root():
    return {"message": "Job Board Backend is running!"}
