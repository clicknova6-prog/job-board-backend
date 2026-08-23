"""Helpers shared by HTTP route handlers."""

from fastapi import Request


def get_client_ip(request: Request) -> str | None:
    """Return the originating IP, preferring the first forwarded address."""
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        first_address = forwarded_for.split(",", 1)[0].strip()
        if first_address:
            return first_address
    return request.client.host if request.client is not None else None
