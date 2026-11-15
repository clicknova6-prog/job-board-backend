from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.site_dependencies import get_current_site
from app.db.site_repositories import SiteRecord
from app.main import app, unhandled_exception_handler


def test_unknown_route_uses_error_envelope() -> None:
    response = TestClient(app).get("/definitely-not-a-route")

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "NOT_FOUND",
            "message": "Not Found",
            "details": None,
        }
    }


def test_request_validation_uses_error_envelope_with_field_details() -> None:
    app.dependency_overrides[get_current_site] = lambda: SiteRecord(
        id=1, slug="s", name="S", base_url="https://s.test", config={}
    )
    try:
        response = TestClient(app).get("/api/v1/jobs", params={"limit": 0})
    finally:
        app.dependency_overrides.pop(get_current_site, None)

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "VALIDATION_ERROR",
            "message": "Request validation failed",
            "details": [
                {
                    "type": "greater_than_equal",
                    "loc": ["query", "limit"],
                    "msg": "Input should be greater than or equal to 1",
                    "input": "0",
                    "ctx": {"ge": 1},
                }
            ],
        }
    }


def test_unhandled_exception_uses_non_leaking_error_envelope() -> None:
    test_app = FastAPI()
    test_app.add_exception_handler(Exception, unhandled_exception_handler)

    @test_app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("sensitive internal failure")

    response = TestClient(test_app, raise_server_exceptions=False).get("/boom")

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "Internal server error",
            "details": None,
        }
    }
    assert "sensitive internal failure" not in response.text
