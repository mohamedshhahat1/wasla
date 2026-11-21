"""Deterministic stand-ins for every provider the e2e stack's api and worker call (E2E-03).

The api and worker never connect here by name: `guard.install_reroute` rewrites
each provider request to ``/_host/<provider host><path>``, so this one service
answers as each provider by the host it was asked for. Where the repository's
suites already have a double, it is reused rather than rewritten:

- **Meta Graph**: sends answer with a sequential ``wamid.E2E<n>``; a media id
  resolves to a URL on ``lookaside.fbsbx.com`` that serves a small PNG.
- **OpenAI**: embeddings are `tests.fake_embeddings.embed_text` (via
  `tests.integration.ai_harness.embedding_response`); the sentiment reader gets
  a neutral reading; the agent follows a fixed script - a reply in the
  customer's language (Arabic or English), ``request_human_handoff`` when the
  customer writes "agent" or "موظف", ``search_knowledge`` when they write
  "price" or "سعر" (then a reply once the tool's output is back).
- **Paymob**: an intention answers with the order id the suites' double uses
  (`tests.paymob_orders.order_for`); the hosted checkout page (``/paymob/``),
  when opened, sends the signed callback Paymob would send - to the API's
  address on the e2e network, because the notification URL (built on
  API_PUBLIC_URL since FEB-12) names the host's published port, which a
  container cannot reach - and then redirects to the return URL.
- **Google**: ``/google/authorize`` redirects straight back to the redirect URI
  with a code (append ``&email=`` to choose who signs in); the token endpoint
  checks PKCE and answers with an ID token really signed by a key generated at
  start-up, whose JWKS this service also serves. Every check the API makes -
  binding cookie, state, nonce, signature, issuer, audience - is the real one.

- **Meta Embedded Signup** (ADR-204, FK-01 .. FK-07): the five onboarding Graph
  calls - the code exchange, ``subscribed_apps``, ``register``, the
  ``is_on_biz_app`` read and ``smb_app_data`` - each logged by method and step
  (``GET /_meta/calls``; ``DELETE`` clears it). The outcome is chosen by the
  code (``es-expired``, ``es-invalid``, ``es-subfail-*``, ``es-regfail-*``)
  or the number (an id containing ``COEX`` is on the WhatsApp Business app),
  never by a global switch. ``/meta/sdk.js`` is a fake of Meta's SDK and
  ``/meta/popup`` a fake of the Embedded Signup window, which posts Meta's
  documented message events to its opener; ``POST /_meta/webhook`` signs and
  posts a Coexistence or account_update delivery to the API.

Run as a service: ``python -m tests.e2e_stack.fakes``. Every answer is logged
to stdout by host and path only.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import itertools
import json
import os
import re
import secrets
import time
import uuid
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from app.integrations.billing.paymob import hmac_signature
from tests.integration.ai_harness import SENTIMENT_FORMAT, embedding_response, text_response
from tests.paymob_orders import order_for

API_INTERNAL = os.environ.get("WASLA_E2E_API_INTERNAL", "http://api:8000")
FAKES_PUBLIC = os.environ.get("WASLA_E2E_FAKES_PUBLIC_URL", "http://localhost:8026").rstrip("/")
META_APP_SECRET = os.environ.get("META_APP_SECRET", "")
PAYMOB_HMAC_SECRET = os.environ.get("PAYMOB_HMAC_SECRET", "")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
DEFAULT_GOOGLE_EMAIL = "e2e.google.user@example.com"
KID = "wasla-e2e-key"
# A 1x1 transparent PNG.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
_ARABIC = re.compile(r"[؀-ۿ]")
_HANDOFF_WORDS = ("agent", "موظف")
_SEARCH_WORDS = ("price", "سعر")

_signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_sent = itertools.count(1)
_intentions: dict[str, dict[str, Any]] = {}
_codes: dict[str, dict[str, str]] = {}
# Every onboarding call the API or worker made, by method and step only (FK-03).
_meta_calls: list[dict[str, str]] = []
_sync_requests = itertools.count(1)
#: Paymob transaction ids (FEB-47): one counter per run started at a random place, so two
#: checkouts in the same second never share an id (the clock second made the API drop the second
#: callback as a duplicate and leave that payment pending).
_transactions = itertools.count(190_000_000 + secrets.randbelow(800_000) * 1_000)
#: The Messaging account the e2e ownership fake answers for every e2e number.
ES_WABA = "555000111"


def _say(*parts: object) -> None:
    print("fakes", *parts, flush=True)  # noqa: T201 - the service's own log


def _jwks() -> dict[str, Any]:
    key = dict(jwt.algorithms.RSAAlgorithm.to_jwk(_signing_key.public_key(), as_dict=True))
    key.update({"kid": KID, "use": "sig", "alg": "RS256"})
    return {"keys": [key]}


# ------------------------------------------------------------------- Meta Graph


async def _graph(method: str, path: str, request: Request) -> Response:
    onboarding = await _onboarding(method, path, request)
    if onboarding is not None:
        return onboarding
    parts = [part for part in path.split("/") if part]
    if method == "POST" and parts[-1:] == ["messages"]:
        body = json.loads(await request.body() or b"{}")
        return JSONResponse(
            {
                "messaging_product": "whatsapp",
                "contacts": [{"input": body.get("to"), "wa_id": body.get("to") or "201000000001"}],
                "messages": [{"id": f"wamid.E2E{next(_sent)}"}],
            }
        )
    if method == "POST" and parts[-1:] == ["media"]:
        return JSONResponse({"id": f"e2e-upload-{uuid.uuid4().hex[:8]}"})
    if method == "GET" and len(parts) == 2 and parts[1].startswith("e2e-media"):
        media_id = parts[1]
        return JSONResponse(
            {
                "messaging_product": "whatsapp",
                "id": media_id,
                "url": f"https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid={media_id}",
                "mime_type": "image/png",
                "sha256": hashlib.sha256(PNG).hexdigest(),
                "file_size": len(PNG),
            }
        )
    return JSONResponse({"data": [], "id": parts[-1] if parts else "e2e"})


# ------------------------------------------------- Meta Embedded Signup (FK-01)


def _bearer(request: Request) -> str:
    return request.headers.get("authorization", "").removeprefix("Bearer ").strip()


def _logged(step: str, method: str, path: str) -> None:
    # The path names a number or an account, never a credential (FK-03).
    _meta_calls.append({"step": step, "method": method, "path": path})


async def _onboarding(method: str, path: str, request: Request) -> Response | None:
    """The five onboarding calls, or None for any other Graph request."""
    parts = [part for part in path.split("/") if part]
    token = _bearer(request)
    if method == "GET" and parts[-2:] == ["oauth", "access_token"]:
        _logged("exchange", method, path)
        code = request.query_params.get("code", "")
        if code == "es-expired":
            return JSONResponse(
                {
                    "error": {
                        "message": "This authorization code has expired.",
                        "type": "OAuthException",
                        "code": 100,
                    }
                },
                status_code=400,
            )
        if code == "es-invalid":
            return JSONResponse(
                {
                    "error": {
                        "message": "This authorization code has been used.",
                        "type": "OAuthException",
                        "code": 100,
                    }
                },
                status_code=400,
            )
        marker = "subfail" if "subfail" in code else "regfail" if "regfail" in code else "ok"
        return JSONResponse(
            {
                "access_token": f"e2e-es-token-{marker}-{uuid.uuid4().hex[:8]}",
                "token_type": "bearer",
            }
        )
    if parts[-1:] == ["subscribed_apps"] and method in ("POST", "DELETE", "GET"):
        step = {"POST": "subscribe", "DELETE": "unsubscribe", "GET": "subscriptions"}[method]
        _logged(step, method, path)
        if method == "POST" and "subfail" in token:
            return JSONResponse(
                {"error": {"message": "Unsupported request", "code": 100}}, status_code=400
            )
        return JSONResponse({"success": True})
    if method == "POST" and parts[-1:] == ["register"]:
        _logged("register", method, path)
        if "regfail" in token:
            return JSONResponse(
                {"error": {"message": "Registration failed", "code": 133005}}, status_code=400
            )
        return JSONResponse({"success": True})
    if method == "POST" and parts[-1:] == ["smb_app_data"]:
        body = json.loads(await request.body() or b"{}")
        _logged(f"sync:{body.get('sync_type')}", method, path)
        return JSONResponse(
            {"messaging_product": "whatsapp", "request_id": f"e2e-request-{next(_sync_requests)}"}
        )
    fields = request.query_params.get("fields", "")
    if method == "GET" and "is_on_biz_app" in fields and len(parts) == 2:
        _logged("coexistence", method, path)
        number = parts[1]
        return JSONResponse(
            {"is_on_biz_app": "COEX" in number, "platform_type": "CLOUD_API", "id": number}
        )
    return None


SDK = """(function () {
  var ORIGIN = "__ORIGIN__";
  var pending = null;
  window.FB = {
    init: function (options) { window.__waslaFakeFbInit = options; },
    login: function (callback, options) {
      pending = callback;
      window.__waslaFakeFbLogin = options;
      window.open(ORIGIN + "/meta/popup", "wasla-fake-es", "width=600,height=720");
    }
  };
  window.addEventListener("message", function (event) {
    if (event.origin !== ORIGIN) return;
    var data;
    try { data = typeof event.data === "string" ? JSON.parse(event.data) : event.data; }
    catch (error) { return; }
    if (data && data.type === "FAKE_FB_LOGIN_RESULT" && pending) {
      var callback = pending; pending = null; callback(data.response);
    }
  });
  if (typeof window.fbAsyncInit === "function") window.fbAsyncInit();
})();
"""

POPUP = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Fake Embedded Signup</title></head>
<body>
<h1>Fake Embedded Signup (e2e)</h1>
<label>Number <input id="number" value="PNID-E2E-ES-NEW-1"></label>
<label>Code <input id="code" value="es-ok"></label>
<label>Account <input id="waba" value="__WABA__"></label>
<button data-action="finish">Finish</button>
<button data-action="finish-app">Finish (WhatsApp Business app number)</button>
<button data-action="close">Close on the last screen</button>
<button data-action="cancel">Cancel at phone verification</button>
<button data-action="error">Error 3441042</button>
<script>
  function send(message) { window.opener.postMessage(JSON.stringify(message), "*"); }
  function session(event, onApp) {
    var number = document.getElementById("number").value;
    send({
      type: "WA_EMBEDDED_SIGNUP", event: event,
      data: {
        phone_number_id: number, is_on_biz_app: onApp,
        messaging_account_ids: [document.getElementById("waba").value], primary_waac_ids: [],
        business_id: "e2e-business", page_ids: [], catalog_ids: [], dataset_ids: [],
        instagram_account_ids: []
      }
    });
  }
  function login(code) {
    send({ type: "FAKE_FB_LOGIN_RESULT",
           response: code
             ? { authResponse: { code: code } }
             : { authResponse: null, status: "unknown" } });
  }
  document.querySelectorAll("button").forEach(function (button) {
    button.addEventListener("click", function () {
      var action = button.getAttribute("data-action");
      var code = document.getElementById("code").value;
      if (action === "finish") { session("FINISH", false); login(code); }
      if (action === "finish-app") {
        session("FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING", true);
        login(code);
      }
      if (action === "close") { session("FINISH", false); login(null); }
      if (action === "cancel") {
        send({ type: "WA_EMBEDDED_SIGNUP", event: "CANCEL",
               data: { current_step: "PHONE_NUMBER_VERIFICATION" } });
        login(null);
      }
      if (action === "error") {
        send({ type: "WA_EMBEDDED_SIGNUP", event: "ERROR",
               data: { error_message: "Unavailable in this region", error_code: 3441042,
                       session_id: "e2e-session-3441042", timestamp: String(Date.now()) } });
        login(null);
      }
      setTimeout(function () { window.close(); }, 50);
    });
  });
</script>
</body></html>
"""


async def _inject(body: dict[str, Any]) -> Response:
    """Sign a delivery as Meta would and post it to the API (FK-07)."""
    payload = json.dumps(body.get("payload", body)).encode()
    signature = hmac.new(META_APP_SECRET.encode(), payload, hashlib.sha256).hexdigest()
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        posted = await client.post(
            f"{API_INTERNAL}/api/v1/webhooks/whatsapp",
            content=payload,
            headers={
                "content-type": "application/json",
                "x-hub-signature-256": f"sha256={signature}",
            },
        )
    _say("meta webhook injected", posted.status_code)
    return JSONResponse({"status": posted.status_code}, status_code=posted.status_code)


# ----------------------------------------------------------------------- OpenAI


def _customer_text(body: dict[str, Any]) -> str:
    texts = [
        item.get("content")
        for item in body.get("input", [])
        if isinstance(item, dict) and item.get("role") == "user"
    ]
    strings = [text for text in texts if isinstance(text, str)]
    return strings[-1] if strings else ""


def _call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    response = text_response(None)
    response["output"].append(
        {
            "type": "function_call",
            "call_id": "call_" + uuid.uuid4().hex[:8],
            "name": name,
            "arguments": json.dumps(arguments, ensure_ascii=False),
        }
    )
    return response


def agent_answer(body: dict[str, Any]) -> dict[str, Any]:
    """The scripted agent: deterministic in what it is asked, never in what it was told."""
    text = _customer_text(body)
    arabic = bool(_ARABIC.search(text))
    has_tool_output = any(
        isinstance(item, dict) and item.get("type") == "function_call_output"
        for item in body.get("input", [])
    )
    lowered = text.lower()
    if not has_tool_output and any(word in lowered for word in _HANDOFF_WORDS):
        return _call("request_human_handoff", {"reason": "The customer asked for a person."})
    if not has_tool_output and any(word in lowered for word in _SEARCH_WORDS):
        return _call("search_knowledge", {"query": text[:200]})
    if has_tool_output:
        return text_response("هذا ما وجدته." if arabic else "Here is what I found.")
    return text_response(
        "أهلاً، أنا مساعد وصلة التجريبي." if arabic else "Hello from the Wasla e2e agent."
    )


async def _openai(path: str, request: Request) -> Response:
    body: dict[str, Any] = json.loads(await request.body() or b"{}")
    if path.endswith("/embeddings"):
        answered = embedding_response(body)
        return Response(answered.content, media_type="application/json")
    fmt = body.get("text", {}).get("format", {})
    if isinstance(fmt, dict) and fmt.get("name") == SENTIMENT_FORMAT:
        reading = {"sentiment": "neutral", "score": 0.0, "intent": None, "confidence": 0.9}
        return JSONResponse(text_response(json.dumps(reading), input_tokens=3, output_tokens=2))
    return JSONResponse(agent_answer(body))


# ----------------------------------------------------------------------- Paymob


async def _paymob(method: str, path: str, request: Request) -> Response:
    if method == "POST" and path.rstrip("/").endswith("/v1/intention"):
        body: dict[str, Any] = json.loads(await request.body() or b"{}")
        number = len(_intentions) + 1
        secret = f"egy_csk_test_e2e{number:06d}{uuid.uuid4().hex[:8]}"
        _intentions[secret] = body
        return JSONResponse(
            {
                "id": f"pi_test_e2e{number:06d}",
                "client_secret": secret,
                "intention_order_id": order_for(body.get("special_reference")),
            },
            status_code=201,
        )
    return JSONResponse({"detail": "not faked"}, status_code=404)


def _transaction(intention: dict[str, Any]) -> dict[str, Any]:
    reference = intention.get("special_reference")
    methods = [m for m in intention.get("payment_methods", []) if isinstance(m, int)]
    return {
        "id": next(_transactions),
        "pending": False,
        "amount_cents": int(intention.get("amount", 0)),
        "success": True,
        "is_auth": False,
        "is_capture": False,
        "is_standalone_payment": True,
        "is_voided": False,
        "is_refunded": False,
        "is_3d_secure": True,
        "integration_id": methods[0] if methods else 0,
        "has_parent_transaction": False,
        "order": {"id": order_for(reference), "merchant_order_id": reference},
        "is_live": False,
        "created_at": "2026-11-07T00:00:00.000000",
        "currency": intention.get("currency", "EGP"),
        "source_data": {"pan": "2346", "type": "card", "sub_type": "MasterCard"},
        "error_occured": False,
        "owner": 302852,
    }


PAYMOB_OUTCOMES = frozenset({"paid", "declined", "pending", "late"})

# The late callbacks in flight, held so the event loop does not collect them.
_late_callbacks: set[asyncio.Task[None]] = set()


def _declined(transaction: dict[str, Any]) -> dict[str, Any]:
    """The callback a real Test decline sent (P54): not a success, a message."""
    return {
        **transaction,
        "success": False,
        "error_occured": False,
        "data": {"message": "AUTHENTICATION_FAILED"},
    }


async def _paymob_callback(transaction: dict[str, Any], *, delay: float = 0.0) -> None:
    """Send the signed callback to the API.

    The notification URL names the API's published port on the host
    (API_PUBLIC_URL, FEB-12); from inside this network the API is reached by
    its service name instead.
    """
    if delay:
        await asyncio.sleep(delay)
    signature = hmac_signature(transaction, secret=PAYMOB_HMAC_SECRET)
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        callback = await client.post(
            f"{API_INTERNAL}/api/v1/webhooks/paymob",
            params={"hmac": signature},
            json={"type": "TRANSACTION", "obj": transaction},
        )
    _say(
        "paymob callback", callback.status_code, "success" if transaction["success"] else "declined"
    )


# ----------------------------------------------------------------------- Google


async def _google(method: str, host: str, path: str, request: Request) -> Response:
    if host == "www.googleapis.com" and path.endswith("/certs"):
        return JSONResponse(_jwks())
    if host == "oauth2.googleapis.com" and path.rstrip("/").endswith("/token"):
        form = dict((await request.form()).items())
        grant = _codes.pop(str(form.get("code", "")), None)
        verifier = str(form.get("code_verifier", ""))
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        if grant is None or challenge != grant["code_challenge"]:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        now = int(time.time())
        claims = {
            "iss": "https://accounts.google.com",
            "aud": grant["client_id"],
            "sub": "e2e-" + hashlib.sha256(grant["email"].encode()).hexdigest()[:20],
            "email": grant["email"],
            "email_verified": True,
            "name": grant["email"].split("@")[0],
            "nonce": grant["nonce"],
            "iat": now,
            "exp": now + 600,
        }
        token = jwt.encode(claims, _signing_key, algorithm="RS256", headers={"kid": KID})
        return JSONResponse(
            {"access_token": "e2e-google-access", "id_token": token, "token_type": "Bearer"}
        )
    return JSONResponse({"error": "not faked"}, status_code=404)


# ------------------------------------------------------------------ the service


def service() -> FastAPI:
    api = FastAPI(title="wasla e2e fakes", docs_url=None, redoc_url=None, openapi_url=None)

    @api.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @api.api_route("/_host/{host}/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def provider(host: str, path: str, request: Request) -> Response:
        path = "/" + path
        _say(request.method, host, path)
        if host.endswith("facebook.com"):
            return await _graph(request.method, path, request)
        if host.endswith("fbsbx.com"):
            return Response(PNG, media_type="image/png")
        if host == "api.openai.com":
            return await _openai(path, request)
        if host.endswith("paymob.com"):
            return await _paymob(request.method, path, request)
        if host.endswith("googleapis.com"):
            return await _google(request.method, host, path, request)
        return JSONResponse({"error": f"no fake for {host}"}, status_code=404)

    @api.get("/_meta/calls")
    async def meta_calls() -> list[dict[str, str]]:
        return list(_meta_calls)

    @api.delete("/_meta/calls")
    async def clear_meta_calls() -> dict[str, int]:
        cleared = len(_meta_calls)
        _meta_calls.clear()
        return {"cleared": cleared}

    @api.get("/meta/sdk.js")
    async def meta_sdk() -> Response:
        # Loaded with crossorigin="anonymous" as Meta documents it, so the answer carries the
        # CORS header Meta's CDN sends; without it the browser refuses the script.
        return Response(
            SDK.replace("__ORIGIN__", FAKES_PUBLIC),
            media_type="application/javascript",
            headers={"Access-Control-Allow-Origin": "*"},
        )

    @api.get("/meta/popup")
    async def meta_popup() -> Response:
        return Response(POPUP.replace("__WABA__", ES_WABA), media_type="text/html")

    @api.post("/_meta/webhook")
    async def meta_webhook(request: Request) -> Response:
        return await _inject(json.loads(await request.body() or b"{}"))

    @api.get("/google/authorize")
    async def google_authorize(request: Request) -> Response:
        query = request.query_params
        code = "e2e-code-" + uuid.uuid4().hex
        _codes[code] = {
            "nonce": query.get("nonce", ""),
            "code_challenge": query.get("code_challenge", ""),
            "client_id": query.get("client_id", GOOGLE_CLIENT_ID),
            "email": query.get("email", DEFAULT_GOOGLE_EMAIL),
        }
        target = query.get("redirect_uri", "")
        separator = "&" if "?" in target else "?"
        _say("google authorize ->", urlsplit(target).path)
        return RedirectResponse(
            f"{target}{separator}{urlencode({'code': code, 'state': query.get('state', '')})}",
            status_code=302,
        )

    @api.get("/paymob/")
    async def paymob_checkout(request: Request) -> Response:
        """The hosted page: pay, then send the browser back.

        `outcome` is the journeys' own switch, read here and never by the API
        (EC8-04). Whatever happens, the redirect says `success=true`, as a real
        redirect can while the payment is still pending or declined (P02, P54) -
        the return page must decide from the payment the API reads, not from
        this query string.

        - ``paid`` (default): the signed success callback, then the redirect.
        - ``declined``: a signed callback with ``success=false`` and the
          provider's message, then the redirect.
        - ``pending``: no callback at all; the redirect says ``pending=true``.
        - ``late``: the redirect at once; the success callback ``delay``
          seconds later (at most 30).
        """
        intention = _intentions.get(request.query_params.get("clientSecret", ""))
        if intention is None:
            return JSONResponse({"error": "unknown client secret"}, status_code=404)
        outcome = request.query_params.get("outcome", "paid")
        if outcome not in PAYMOB_OUTCOMES:
            return JSONResponse({"error": f"unknown outcome {outcome}"}, status_code=400)
        transaction = _transaction(intention)
        if outcome == "declined":
            transaction = _declined(transaction)
        if outcome in ("paid", "declined"):
            await _paymob_callback(transaction)
        elif outcome == "late":
            delay = min(max(float(request.query_params.get("delay", "5")), 0.0), 30.0)
            task = asyncio.create_task(_paymob_callback(transaction, delay=delay))
            _late_callbacks.add(task)
            task.add_done_callback(_late_callbacks.discard)
            _say("paymob callback scheduled", int(delay))
        back = str(intention.get("redirection_url") or "/")
        separator = "&" if "?" in back else "?"
        query = urlencode(
            {
                "success": "true",
                "id": transaction["id"],
                "pending": "true" if outcome == "pending" else "false",
            }
        )
        return RedirectResponse(f"{back}{separator}{query}", status_code=302)

    return api


if __name__ == "__main__":
    import uvicorn

    from tests.e2e_stack.guard import install_socket_wall

    install_socket_wall()
    uvicorn.run(service(), host="0.0.0.0", port=int(os.environ.get("PORT", "8100")))  # noqa: S104
