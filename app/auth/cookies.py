"""Authentication cookie configuration shared by auth routers."""

from starlette.responses import Response

PUBLIC_REFRESH_COOKIE = "job_board_refresh_token"
ADMIN_REFRESH_COOKIE = "job_board_admin_refresh_token"
OAUTH_STATE_COOKIE = "job_board_google_oauth_state"

PUBLIC_REFRESH_PATH = "/auth"
ADMIN_REFRESH_PATH = "/admin/auth"
OAUTH_STATE_PATH = "/auth/google"

REFRESH_COOKIE_MAX_AGE = 30 * 24 * 60 * 60
OAUTH_STATE_MAX_AGE = 10 * 60


def set_refresh_cookie(
    response: Response,
    *,
    cookie_name: str,
    cookie_path: str,
    raw_token: str,
) -> None:
    """Set one secure refresh-token cookie."""
    response.set_cookie(
        key=cookie_name,
        value=raw_token,
        max_age=REFRESH_COOKIE_MAX_AGE,
        path=cookie_path,
        secure=True,
        httponly=True,
        samesite="lax",
    )


def clear_refresh_cookie(
    response: Response,
    *,
    cookie_name: str,
    cookie_path: str,
) -> None:
    """Expire one refresh-token cookie using its original attributes."""
    response.delete_cookie(
        key=cookie_name,
        path=cookie_path,
        secure=True,
        httponly=True,
        samesite="lax",
    )


def clear_oauth_state_cookie(response: Response) -> None:
    """Expire the transient Google OAuth state cookie."""
    response.delete_cookie(
        key=OAUTH_STATE_COOKIE,
        path=OAUTH_STATE_PATH,
        secure=True,
        httponly=True,
        samesite="lax",
    )
