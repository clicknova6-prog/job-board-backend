"""Authentication service exceptions."""


class AuthServiceError(Exception):
    """Base exception for expected authentication failures."""


class InvalidAccessTokenError(AuthServiceError):
    """Raised when an access token cannot be trusted."""


class InvalidRefreshTokenError(AuthServiceError):
    """Raised when a refresh token is missing, expired, or revoked."""


class AuthSubjectDisabledError(AuthServiceError):
    """Raised when a token owner has been deleted or disabled."""


class InvalidAdminCredentialsError(AuthServiceError):
    """Raised for every administrator authentication failure."""


class OAuthEmailCollisionError(AuthServiceError):
    """Raised when an email belongs to a different OAuth identity."""


class GoogleOAuthExchangeError(AuthServiceError):
    """Raised when Google rejects or returns an invalid OAuth exchange."""
