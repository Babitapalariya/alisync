"""Expose identical order handlers for /app and /api deployments."""
from copy import copy
from fastapi import APIRouter

def include_order_routes(app, router):
    app.include_router(router)
    aliases = APIRouter()
    for route in router.routes:
        if route.path.startswith("/app/orders"):
            alias = copy(route)
            alias.path = "/api/orders" + route.path[len("/app/orders"):]
            aliases.routes.append(alias)
    # include_router reconstructs routing regexes and preserves auth dependencies.
    app.include_router(aliases)
