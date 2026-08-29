import os
import secrets
from datetime import datetime, timezone

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

app = FastAPI(
    title="LinkedIn Profile API",
    version="1.0.0",
    summary="Normalize a LinkedIn member profile into structured JSON",
)
pool = LinkedInPool.from_env()
api_key = os.getenv("API_KEY", "")
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


@app.get("/health", tags=["Operations"])
async def health(x_api_key: str | None = Header(default=None)):
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
    responses={
        status: {"model": ErrorResponse} for status in (401, 404, 422, 429, 502, 503)
    },
    tags=["Profiles"],
)
async def profile(
    payload: ProfileRequest, x_api_key: str | None = Header(default=None)
):
    if api_key and (not x_api_key or not secrets.compare_digest(x_api_key, api_key)):
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")
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
