"""Who may talk to this app, and what its pages may do once loaded.

There is no login, so "who" cannot mean a user. It means two narrower things,
and both matter even when the port is bound to localhost — because the browser
on that machine also visits the rest of the internet.

1. **The Host header must be one of ours.** Without this, a page on
   `attacker.example` can re-point its own hostname at 127.0.0.1 (DNS
   rebinding). The browser then believes the app *is* attacker.example, same
   origin, and lets that page read every response and send any request.
   Binding to localhost does not help; the connection really is local. The
   defence is to refuse a request that names a host we were never told about.

2. **A write must not come from another site.** A form POST, or a `fetch` with
   no body, is a CORS "simple request": the browser sends it without asking
   first. The attacker cannot read the answer, but the side effect lands —
   tick a task, reset a plan, pop an undo. No cookie is needed to ride on; the
   port being reachable from the victim's browser is enough. So any request
   that changes something and carries a browser's cross-site markings is
   refused. Requests carrying neither marking are allowed, because that is
   curl, a script or the test client, and scripted access is intended.

Everything is decided from request headers alone, so this is a plain ASGI
wrapper with no dependency beyond the Starlette that FastAPI already brings.
The same file is used, byte for byte, by smoltask and smolplan.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse

# Enough for the machine the app runs on, under every name a browser on it will
# use. Anything else — a LAN address, a hostname, a reverse proxy's name — is
# added by the person deploying it, through the environment.
DEFAULT_HOSTS = ("localhost", "127.0.0.1", "[::1]")

# The methods that cannot change anything. Everything else is a write.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Sec-Fetch-Site values a legitimate write can carry: the app's own page, or
# something the user did directly (a bookmark, the address bar). "same-site"
# is deliberately not here — another port on the same host is same-site, and
# that is another app.
TRUSTED_FETCH_SITES = frozenset({"same-origin", "none"})

# Sent on every response, errors included.
#
# The CSP allows nothing that is not served from here: no inline script, no
# inline style *attribute*, no plugins, no framing. The pages set styles
# through the CSSOM (`node.style.cssText`), which a CSP does not govern, so
# `'unsafe-inline'` is not needed; `tests/test_frontend.py` fails if a style
# attribute is set any other way. `frame-ancestors 'none'` is the clickjacking
# guard, and X-Frame-Options says the same to browsers that predate it.
SECURITY_HEADERS = (
    (b"content-security-policy",
     b"default-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
     b"form-action 'self'; object-src 'none'"),
    (b"x-frame-options", b"DENY"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
)


def host_only(value: str) -> str:
    """`Host` or an allowlist entry, without its port, lower-cased.

    Not `split(":")[0]`, which is what Starlette's own TrustedHostMiddleware
    does and which turns `[::1]:8000` into `[`.
    """
    value = value.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return value[: end + 1] if end != -1 else value
    return value.split(":", 1)[0]


def allowed_hosts(variable: str) -> frozenset[str]:
    """The hosts this app answers to, from a comma-separated environment
    variable. Unset or blank means the defaults. `*` switches the check off,
    for someone who has put their own proxy in front and means it."""
    raw = os.environ.get(variable, "")
    entries = [host_only(h) for h in raw.split(",") if h.strip()]
    return frozenset(entries or DEFAULT_HOSTS)


class Guard:
    """Host allowlist, cross-site write refusal and security headers, in that
    order. See the module docstring for why each exists."""

    def __init__(self, app, hosts: frozenset[str]):
        self.app = app
        self.hosts = hosts

    def _host_ok(self, host: str) -> bool:
        return "*" in self.hosts or host_only(host) in self.hosts

    @staticmethod
    def _cross_site(headers: Headers) -> bool:
        site = headers.get("sec-fetch-site")
        if site is not None:
            return site not in TRUSTED_FETCH_SITES
        origin = headers.get("origin")
        if origin is None:
            return False  # not a browser, or one too old to say: allowed
        # An Origin must name exactly the host and port this request was sent
        # to. "null" — a sandboxed frame, a file:// page — names nothing.
        return urlsplit(origin).netloc.lower() != headers.get("host", "").lower()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def with_headers(message):
            if message["type"] == "http.response.start":
                present = list(message.get("headers", []))
                names = {k.lower() for k, _ in present}
                message["headers"] = present + [
                    (k, v) for k, v in SECURITY_HEADERS if k not in names
                ]
            await send(message)

        headers = Headers(scope=scope)
        if not self._host_ok(headers.get("host", "")):
            response = PlainTextResponse("Invalid host header.", status_code=400)
        elif scope["method"] not in SAFE_METHODS and self._cross_site(headers):
            response = PlainTextResponse("Cross-site request refused.", status_code=403)
        else:
            await self.app(scope, receive, with_headers)
            return
        await response(scope, receive, with_headers)
