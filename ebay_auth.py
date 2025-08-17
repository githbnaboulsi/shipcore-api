import json
import base64
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

import boto3
from scripts.mongo import get_mongo_client
from scripts.util import create_response

# --- constants: production only ---
TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"  # Prod endpoint
client = None  # cache Mongo client


def _ssm(name: str, decrypt: bool = True) -> str:
    ssm = boto3.client("ssm", region_name="us-east-1")
    return ssm.get_parameter(Name=name, WithDecryption=decrypt)["Parameter"]["Value"]


def _get_method(event) -> str:
    """Support API Gateway REST and HTTP APIs."""
    if not event:
        return "GET"
    if "httpMethod" in event:  # REST API
        return event["httpMethod"].upper()
    # HTTP API v2
    return (event.get("requestContext", {}).get("http", {}).get("method", "GET")).upper()


def _iso_z(dt) -> str | None:
    if not dt:
        return None
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")
    # try to parse string
    try:
        s = dt
        if isinstance(s, str) and s.endswith("Z"):
            s = s[:-1] + "+00:00"
        parsed = datetime.fromisoformat(s)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        else:
            parsed = parsed.astimezone(timezone.utc)
        return parsed.isoformat().replace("+00:00", "Z")
    except Exception:
        return None

def _to_utc_dt(value):
    """Coerce Mongo/ISO string/naive datetime -> aware UTC datetime, or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    if isinstance(value, str):
        s = value
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
        except Exception:
            return None
    return None

def _handle_status(event):
    try:
        qsp = (event or {}).get("queryStringParameters") or {}
        account = qsp.get("account") if isinstance(qsp, dict) and qsp.get("account") else "default"
        doc = client.shipcore.ebay_tokens.find_one({"account": account})
        if not doc:
            return create_response(200, {"connected": False})

        now = datetime.now(timezone.utc)
        skew = timedelta(seconds=30)

        refresh_expires_at = _to_utc_dt(doc.get("refresh_expires_at"))
        # Only consider the refresh token
        refresh_ok = bool(refresh_expires_at and refresh_expires_at > (now + skew))

        payload = {
            "connected": refresh_ok,                           # <-- only refresh token decides
        }
        return create_response(200, payload)

    except Exception:
        # Safe fallback so the UI never stalls on "loading"
        return create_response(200, {"connected": False})


def _handle_exchange(event):
    """
    POST /... (body: {"code": "<auth_code>"})
    Exchanges code for tokens, stores in Mongo, returns {ok: true} or a concise error.
    """
    try:
        body = json.loads(event.get("body") or "{}")
        code = body.get("code")

        if not code:
            return create_response(400, {"ok": False, "message": "Missing code"})

        # --- prod secrets/config from SSM ---
        client_id = _ssm("/shipcore/ebay/client_id")
        client_secret = _ssm("/shipcore/ebay/client_secret")
        ru_name = _ssm("/shipcore/ebay/ru_name")  # OAuth Enabled RuName (PRODUCTION)

        # eBay may send URL-encoded code; decode once before form-encoding
        decoded_code = urllib.parse.unquote(code) if isinstance(code, str) and "%" in code else code

        # --- form body per eBay docs ---
        form = {
            "grant_type": "authorization_code",
            "code": decoded_code,
            "redirect_uri": ru_name,  # must be the RuName
        }
        data = urllib.parse.urlencode(form).encode("utf-8")

        # --- headers: Basic base64(client_id:client_secret), x-www-form-urlencoded ---
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {basic}",
        }

        # --- call eBay token endpoint (prod) ---
        req = urllib.request.Request(TOKEN_URL, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                token_payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode("utf-8")
            except Exception:
                err_body = str(e)
            return create_response(502, {"ok": False, "message": "eBay token exchange failed", "error": err_body})

        access_token = token_payload.get("access_token")
        if not access_token:
            return create_response(502, {"ok": False, "message": "No access_token from eBay", "ebay": token_payload})

        now = datetime.now(timezone.utc)
        access_expires_in = int(token_payload.get("expires_in", 0)) if token_payload.get("expires_in") is not None else None
        refresh_expires_in = token_payload.get("refresh_token_expires_in")
        refresh_expires_in = int(refresh_expires_in) if refresh_expires_in is not None else None

        doc = {
            "account": "default",
            "env": "production",
            "token_type": token_payload.get("token_type"),
            "scope": token_payload.get("scope"),
            "access_token": access_token,
            "access_expires_in": access_expires_in,
            "access_expires_at": (now + timedelta(seconds=access_expires_in)) if access_expires_in else None,
            "refresh_token": token_payload.get("refresh_token"),
            "refresh_expires_in": refresh_expires_in,
            "refresh_expires_at": (now + timedelta(seconds=refresh_expires_in)) if refresh_expires_in else None,
            "issued_at": now,
        }
        client.shipcore.ebay_tokens.update_one({"account": "default"}, {"$set": doc}, upsert=True)

        return create_response(200, {"ok": True})

    except Exception as e:
        return create_response(500, {"ok": False, "error": str(e)})


def lambda_handler(event, context):
    global client
    if client is None:
        client = get_mongo_client()

    method = _get_method(event)

    # CORS preflight (if your create_response adds CORS headers, this is enough)
    if method == "OPTIONS":
        return create_response(200, "OK")

    # Route by HTTP method:
    if method == "GET":
        return _handle_status(event)
    elif method == "POST":
        return _handle_exchange(event)
    else:
        return create_response(405, {"message": f"Method {method} not allowed"})