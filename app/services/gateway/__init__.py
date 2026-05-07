"""Gateway · process-local event bus + optional local HTTP+SSE bridge."""
from app.services.gateway.bus import (
    KIND_DISPATCH,
    KIND_GATEWAY,
    KIND_TASK,
    EventBus,
    GatewayEvent,
    default_bus,
    reset_default_bus_for_tests,
)
from app.services.gateway.http_server import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    TOKEN_ENV,
    GatewayServer,
    get_singleton,
    reset_singleton_for_tests,
)

__all__ = [
    "KIND_DISPATCH",
    "KIND_GATEWAY",
    "KIND_TASK",
    "EventBus",
    "GatewayEvent",
    "default_bus",
    "reset_default_bus_for_tests",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "TOKEN_ENV",
    "GatewayServer",
    "get_singleton",
    "reset_singleton_for_tests",
]
