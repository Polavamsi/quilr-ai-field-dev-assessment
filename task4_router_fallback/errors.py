"""The only error shape clients ever see.

    {"error": {"code": "...", "message": "...", "request_id": "..."}}

Codes and messages are fixed strings chosen here. Nothing from an upstream
response body, exception message or stack trace is ever copied into them.
"""


class GatewayError(Exception):
    def __init__(self, status: int, code: str, message: str, retry_after_s: int | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retry_after_s = retry_after_s


def error_body(code: str, message: str, request_id: str) -> dict:
    return {"error": {"code": code, "message": message, "request_id": request_id}}


def unauthorized() -> GatewayError:
    return GatewayError(401, "invalid_api_key", "Missing or invalid API key")


def rate_limited(retry_after_s: int) -> GatewayError:
    return GatewayError(429, "rate_limited", "Token rate limit exceeded for this API key", retry_after_s)


def request_too_large() -> GatewayError:
    return GatewayError(400, "request_too_large", "Request needs more tokens than the per-minute limit allows")


def upstream_rejected() -> GatewayError:
    return GatewayError(502, "upstream_error", "The model provider could not process the request")


def upstream_unavailable() -> GatewayError:
    return GatewayError(503, "upstream_unavailable", "All model providers are unavailable, try again later")


def internal_error() -> GatewayError:
    return GatewayError(500, "internal_error", "Internal gateway error")
