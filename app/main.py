import os
import secrets
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .linkedin import LinkedInError, LinkedInPool
from .models import (
    ErrorDetail,
    ErrorResponse,
    ProfileRequest,
    ProfileResponse,
    ResponseMeta,
)
from .parser import parse_profile
from .urls import parse_profile_url


def error_docs(
    description: str, examples: dict[str, tuple[str, str]]
) -> dict[str, Any]:
    return {
        "model": ErrorResponse,
        "description": description,
        "content": {
            "application/json": {
                "examples": {
                    name: {
                        "value": {"error": {"code": code, "message": message}}
                    }
                    for name, (code, message) in examples.items()
                }
            }
        },
    }


PROFILE_RESPONSES = {
    200: {
        "description": "Profile fetched.",
        "content": {
            "application/json": {
                "example": {
                    "meta": {
                        "schema_version": "1.0",
                        "fetched_at": "2026-08-29T14:11:59Z",
                        "endpoint": "dash:101",
                    },
                    "profile": {
                        "public_identifier": "vinod-khosla-65387416",
                        "profile_url": "https://www.linkedin.com/in/vinod-khosla-65387416/",
                        "name": "Vinod Khosla",
                        "headline": "Founder",
                        "experience": [],
                        "education": [],
                        "skills": [],
                        "certifications": [],
                        "languages": [],
                    },
                }
            }
        },
    },
    404: error_docs(
        "Profile unavailable.",
        {
            "not_found": (
                "profile_not_found",
                "Profile was not found or is not visible to this session",
            )
        },
    ),
    422: error_docs(
        "Invalid request.",
        {
            "body": ("invalid_request", "body must contain a valid url"),
            "url": ("invalid_request", "invalid LinkedIn profile URL"),
            "host": ("invalid_request", "use an HTTPS linkedin.com profile URL"),
            "path": (
                "invalid_request",
                "URL must match https://www.linkedin.com/in/<identifier>",
            ),
        },
    ),
    429: error_docs(
        "LinkedIn rate limit.",
        {
            "rate_limited": (
                "linkedin_rate_limited",
                "LinkedIn rate-limited this session",
            )
        },
    ),
    502: error_docs(
        "LinkedIn failed.",
        {
            "unreachable": ("upstream_error", "LinkedIn could not be reached"),
            "http": ("upstream_error", "LinkedIn returned HTTP 500"),
            "non_json": ("upstream_error", "LinkedIn returned a non-JSON response"),
            "unexpected": ("upstream_error", "LinkedIn returned an unexpected response"),
            "missing_profile": (
                "upstream_error",
                "LinkedIn returned no recognizable profile",
            ),
            "unsupported": (
                "upstream_error",
                "LinkedIn returned an unsupported profile shape",
            ),
        },
    ),
    503: error_docs(
        "Session unavailable.",
        {
            "not_configured": (
                "linkedin_session_unavailable",
                "LinkedIn session cookies are not configured",
            ),
            "expired": (
                "linkedin_session_expired",
                "LinkedIn session is invalid, expired, or checkpointed",
            ),
            "none_healthy": (
                "linkedin_session_unavailable",
                "No healthy LinkedIn sessions are available",
            ),
        },
    ),
}

HEALTH_RESPONSES = {
    200: {
        "description": "Account health returned.",
        "content": {
            "application/json": {
                "example": {
                    "total_accounts": 1,
                    "healthy_accounts": [
                        {"account": "account-one", "location": "London"}
                    ],
                    "unhealthy_accounts": [],
                }
            }
        },
    },
    401: error_docs(
        "Bad health API key.",
        {"unauthorized": ("unauthorized", "invalid or missing X-API-Key")},
    ),
    422: error_docs(
        "Invalid request.",
        {"request": ("invalid_request", "body must contain a valid url")},
    ),
}


app = FastAPI(
    title="LinkedIn Profile API",
    version="1.0.0",
    summary="Normalize a LinkedIn member profile into structured JSON",
    description="Use **POST /v1/profile** to fetch a profile. Use **GET /health** to check accounts.",
)
pool = LinkedInPool.from_env()
health_api_key = os.getenv("HEALTH_API_KEY", "")


@app.middleware("http")
async def response_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.exception_handler(LinkedInError)
async def linkedin_error(_: Request, exc: LinkedInError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(
            error=ErrorDetail(code=exc.code, message=str(exc))
        ).model_dump(),
        headers={"Retry-After": "60"} if exc.status_code == 429 else None,
    )


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
    codes = {401: "unauthorized", 422: "invalid_request"}
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(
            error=ErrorDetail(
                code=codes.get(exc.status_code, "request_error"),
                message=str(exc.detail),
            )
        ).model_dump(),
    )


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, __: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=ErrorResponse(
            error=ErrorDetail(
                code="invalid_request", message="body must contain a valid url"
            )
        ).model_dump(),
    )


@app.get("/", include_in_schema=False)
async def root():
    return {"name": "LinkedIn Profile API", "docs": "/docs", "endpoint": "/v1/profile"}


@app.get(
    "/health",
    tags=["Operations"],
    summary="Check accounts",
    responses=HEALTH_RESPONSES,
)
async def health(
    x_api_key: str | None = Header(
        default=None, description="HEALTH_API_KEY from .env"
    ),
):
    if (
        not health_api_key
        or not x_api_key
        or not secrets.compare_digest(x_api_key, health_api_key)
    ):
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")
    return await pool.check_health()


@app.post(
    "/v1/profile",
    response_model=ProfileResponse,
    response_model_exclude_none=True,
    responses=PROFILE_RESPONSES,
    tags=["Profiles"],
    summary="Fetch profile",
)
async def profile(
    payload: ProfileRequest,
):
    try:
        identifier, canonical_url = parse_profile_url(payload.url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    document = await pool.fetch(identifier)
    try:
        result = parse_profile(document.payload, identifier, canonical_url)
    except ValueError as exc:
        raise LinkedInError("LinkedIn returned an unsupported profile shape") from exc

    return ProfileResponse(
        meta=ResponseMeta(
            fetched_at=datetime.now(timezone.utc),
            endpoint=document.endpoint,
        ),
        profile=result,
    )
