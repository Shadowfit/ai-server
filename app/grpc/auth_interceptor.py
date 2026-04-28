from __future__ import annotations

import grpc

from app.config import settings


def _deny_handler(message: str):
    def deny(request, context):
        context.abort(grpc.StatusCode.UNAUTHENTICATED, message)

    return grpc.unary_unary_rpc_method_handler(deny)


class AuthInterceptor(grpc.ServerInterceptor):
    """Validate the shared internal token on every inbound gRPC call."""

    def intercept_service(self, continuation, handler_call_details):
        metadata = dict(handler_call_details.invocation_metadata or [])
        token = metadata.get("Authorization") or metadata.get("authorization")
        expected = f"Bearer {settings.INTERNAL_API_TOKEN}"

        if token != expected:
            return _deny_handler("Invalid internal token")

        return continuation(handler_call_details)
