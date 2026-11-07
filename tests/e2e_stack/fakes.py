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
  when opened, sends the signed callback Paymob would send - to the API itself,
  because the notification URL the API asked for is built on the *frontend*
  origin (FEB-12) - and then redirects to the return URL.
- **Google**: ``/google/authorize`` redirects straight back to the redirect URI
  with a code (append ``&email=`` to choose who signs in); the token endpoint
  checks PKCE and answers with an ID token really signed by a key generated at
  start-up, whose JWKS this service also serves. Every check the API makes -
  binding cookie, state, nonce, signature, issuer, audience - is the real one.

Run as a service: ``python -m tests.e2e_stack.fakes``. Every answer is logged
to stdout by host and path only.
"""

from __future__ import annotations

import base64
import hashlib
import itertools
import json
import os
import re
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


def _say(*parts: object) -> None:
    print("fakes", *parts, flush=True)  # noqa: T201 - the service's own log


def _jwks() -> dict[str, Any]:
    key = dict(jwt.algorithms.RSAAlgorithm.to_jwk(_signing_key.public_key(), as_dict=True))
    key.update({"kid": KID, "use": "sig", "alg": "RS256"})
    return {"keys": [key]}


# ------------------------------------------------------------------- Meta Graph


async def _graph(method: str, path: str, request: Request) -> Response:
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
        "id": 190_000_000 + int(time.time()) % 1_000_000,
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
        intention = _intentions.get(request.query_params.get("clientSecret", ""))
        if intention is None:
            return JSONResponse({"error": "unknown client secret"}, status_code=404)
        transaction = _transaction(intention)
        signature = hmac_signature(transaction, secret=PAYMOB_HMAC_SECRET)
        # The API asked for its callback on the frontend origin (FEB-12); this
        # stack has no frontend, so the callback goes to the API directly.
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            callback = await client.post(
                f"{API_INTERNAL}/api/v1/webhooks/paymob",
                params={"hmac": signature},
                json={"type": "TRANSACTION", "obj": transaction},
            )
        _say("paymob callback", callback.status_code)
        back = str(intention.get("redirection_url") or "/")
        separator = "&" if "?" in back else "?"
        query = urlencode({"success": "true", "id": transaction["id"], "pending": "false"})
        return RedirectResponse(f"{back}{separator}{query}", status_code=302)

    return api


if __name__ == "__main__":
    import uvicorn

    from tests.e2e_stack.guard import install_socket_wall

    install_socket_wall()
    uvicorn.run(service(), host="0.0.0.0", port=int(os.environ.get("PORT", "8100")))  # noqa: S104
