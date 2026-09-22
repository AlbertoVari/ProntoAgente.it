"""FastAPI application factory."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from prontoagente.api.routes import router
from prontoagente.config import get_settings
from prontoagente.errors import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    NotFoundError,
)
from prontoagente.v2.routes import router as v2_router


def create_app() -> FastAPI:
    application = FastAPI(
        title="ProntoAgente ERP Workflow API",
        version="0.2.0",
        description="Tenant-scoped agent/workflow catalog and durable execution platform.",
    )

    @application.exception_handler(ConflictError)
    async def handle_conflict(_request: Request, exc: ConflictError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"detail": {"code": exc.code, "message": exc.message}},
        )

    @application.exception_handler(NotFoundError)
    async def handle_not_found(_request: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content={"detail": {"code": exc.code, "message": exc.message}},
        )

    @application.exception_handler(AuthenticationError)
    async def handle_authentication(
        _request: Request, exc: AuthenticationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content={"detail": {"code": exc.code, "message": exc.message}},
            headers={"WWW-Authenticate": "Bearer"},
        )

    @application.exception_handler(AuthorizationError)
    async def handle_authorization(
        _request: Request, exc: AuthorizationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=403,
            content={"detail": {"code": exc.code, "message": exc.message}},
        )

    settings = get_settings()
    if settings.app_env != "production" or settings.enable_legacy_v1:
        application.include_router(router, prefix="/v1")
    application.include_router(v2_router, prefix="/v2")
    return application


app = create_app()
