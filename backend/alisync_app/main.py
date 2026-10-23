import traceback

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

from .config import get_settings
from .database import Base, engine
from . import models  # noqa: F401
from .routes import router
from .operations import api as operations_router, lifespan
from .schema_migrations import add_missing_columns
from .notification_routes import router as notification_router
from .shopify_webhooks import router as shopify_webhook_router
from .shopify_session import router as shopify_session_router

Base.metadata.create_all(bind=engine, checkfirst=True)
add_missing_columns()
settings = get_settings()

app = FastAPI(title="AliSync Multi-User API", docs_url="/app/docs", openapi_url="/app/openapi.json",
              lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_URL, "http://127.0.0.1:3002", "http://localhost:3002"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", include_in_schema=False)
def root():
    # Shopify's Application URL can point at the OAuth backend host so its
    # host exactly matches the callback; send normal app launches to the UI.
    return RedirectResponse(settings.FRONTEND_URL)


@app.get("/docs", include_in_schema=False)
@app.get("/api/docs", include_in_schema=False)
def docs_redirect():
    return RedirectResponse("/app/docs")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    print(f"[AliSync ERROR] {request.method} {request.url.path}: {exc}", flush=True)
    traceback.print_exc()
    return JSONResponse(status_code=500, content={"detail": "Internal AliSync error. See backend terminal."})


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    errors = []
    for error in exc.errors():
        field = ".".join(str(part) for part in error.get("loc", []) if part != "body") or "request"
        errors.append(f"{field}: {error.get('msg', 'invalid value')}")
    message = "; ".join(errors)
    print(f"[AliSync VALIDATION] {request.method} {request.url.path}: {message}", flush=True)
    return JSONResponse(status_code=422, content={"detail": message, "errors": exc.errors()})


app.include_router(operations_router, prefix="/app", tags=["AliSync Operations"])
app.include_router(router)
app.include_router(notification_router)
app.include_router(shopify_webhook_router)
app.include_router(shopify_session_router)
