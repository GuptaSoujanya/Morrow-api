from starlette.responses import JSONResponse


class RequestBoundary:
    """Bound bodies before parsing, including chunked uploads; never cache private responses."""

    def __init__(self, app, artifact_limit: int, trusted_origins: set[str]):
        self.app = app
        self.artifact_limit = artifact_limit
        self.trusted_origins = trusted_origins

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def private_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(
                    [
                        (b"cache-control", b"no-store"),
                        (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"no-referrer"),
                    ]
                )
                message = {**message, "headers": headers}
            await send(message)

        if scope["method"] in ("POST", "PUT", "PATCH", "DELETE"):
            limit = self.artifact_limit if scope["path"] == "/api/v1/artifacts" else 1024 * 1024
            headers = dict(scope.get("headers", []))
            origin = headers.get(b"origin")
            if origin and origin.decode() not in self.trusted_origins:
                return await JSONResponse({"detail": "Untrusted request origin"}, 403)(
                    scope, receive, private_send
                )
            try:
                size = int(headers.get(b"content-length", b"0"))
                if size < 0:
                    raise ValueError
            except ValueError:
                return await JSONResponse({"detail": "Invalid Content-Length"}, 400)(
                    scope, receive, private_send
                )
            if size > limit:
                return await JSONResponse({"detail": "Request body exceeds size limit"}, 413)(
                    scope, receive, private_send
                )
            chunks = []
            size = 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                size += len(chunk)
                if size > limit:
                    return await JSONResponse({"detail": "Request body exceeds size limit"}, 413)(
                        scope, receive, private_send
                    )
                chunks.append(chunk)
                if not message.get("more_body", False):
                    break
            body = b"".join(chunks)
            delivered = False

            async def bounded_receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            return await self.app(scope, bounded_receive, private_send)
        await self.app(scope, receive, private_send)
