import time
import orjson
from src.core.logging import get_logger

logger = get_logger(component="rate_limiter")


class RateLimitMiddleware:
    """ASGI middleware implementing Redis-backed fixed window rate limiting.

    Limits requests per client IP using a Redis counter with TTL.
    Fails open on Redis errors (requests pass through).
    Health and other excluded paths bypass rate limiting.
    """

    def __init__(
        self,
        app,
        max_requests: int = 100,
        window: int = 60,
        exclude_paths: set[str] | None = None,
        redis_client=None,
    ):
        self.app = app
        self.max_requests = max_requests
        self.window = window
        self.exclude_paths = exclude_paths if exclude_paths is not None else {"/health"}
        self._redis = redis_client

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in self.exclude_paths:
            await self.app(scope, receive, send)
            return

        redis = self._redis
        if redis is None:
            # Try to get redis from the ASGI scope (Starlette sets scope["app"])
            app_instance = scope.get("app")
            if app_instance is not None:
                redis = getattr(getattr(app_instance, "state", None), "redis_client", None)

        if redis is None:
            # No redis available, fail open
            logger.warning("rate_limit_no_redis", path=path)
            await self.app(scope, receive, send)
            return

        client_host = "unknown"
        client = scope.get("client")
        if client:
            client_host = client[0]

        key = f"rate_limit:{client_host}"
        now = int(time.time())
        window_key = f"{key}:{now // self.window}"

        try:
            pipe = redis.pipeline()
            pipe.incr(window_key)
            pipe.expire(window_key, self.window)
            results = await pipe.execute()
            count = results[0]
        except Exception as e:
            logger.warning("rate_limit_redis_error", error=str(e), client=client_host)
            # Fail open: allow request through on Redis errors
            await self.app(scope, receive, send)
            return

        if count > self.max_requests:
            retry_after = self.window - (now % self.window)
            body = orjson.dumps({
                "detail": "Rate limit exceeded",
                "retry_after": retry_after,
            })
            headers = [
                (b"content-type", b"application/json"),
                (b"retry-after", str(retry_after).encode()),
            ]
            await send({
                "type": "http.response.start",
                "status": 429,
                "headers": headers,
            })
            await send({
                "type": "http.response.body",
                "body": body,
            })
            return

        await self.app(scope, receive, send)
