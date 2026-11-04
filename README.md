# bazaar-contrib-http

Serve HTTP for the contribs of a composition from one listener. Contribs that accept `HTTP` register routes, mounted ASGI applications and middleware for as long as their lifespan runs.

```sh
bcn contrib add http --enable
```

The corresponding entry in `config.toml`:

```toml
version = "5"

[[contribs]]
package = "bazaar-contrib-http==0.3.*"
[[contribs.enabled]]
entry = "http"
[contribs.enabled.config]
listen = "127.0.0.1:8765"
```

`listen` takes `host:port`, `[::1]:port`, or either with a scheme in front; port `0` picks a free port.

A contrib registers what it serves inside its lifespan:

```python
from contextlib import asynccontextmanager

from bazaar_contrib_http import HTTP
from bazaar_core import Context, Contrib
from starlette.responses import JSONResponse


async def status(request):
    return JSONResponse({"ok": True})


@asynccontextmanager
async def lifespan(ctx: Context):
    http = ctx.accept(HTTP)
    async with http.route("/status", status, methods=["GET"]):
        yield


reporter = Contrib(accepts=[HTTP], lifespan=lifespan)
```

`http.mount(path, app)` serves an ASGI application, such as a FastAPI app, under a path, and `http.middleware(dispatch)` wraps every request; the first middleware registered is the outermost. `http.url` is where the listener answers.

When a registration's context closes, it stops taking requests, and the requests it is still handling get five seconds to finish before they are cancelled. A request still running ten seconds after it is cancelled is no longer waited for.

Access log lines include each request's query string. An endpoint that receives secrets there, such as a sign-in callback, marks those parameters so that their values are logged as `[redacted]`:

```python
from bazaar_contrib_http import redact


@redact("code", "state")
async def callback(request): ...
```

The mark works on endpoints registered with `http.route` and on the routes of a mounted Starlette or FastAPI application.
