import json
import base64
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

from scripts.util import get_mongo_client
from scripts.util import create_response
from scripts.util import ssm

# --- constants: production only ---
TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"  # Prod endpoint
client = None  # cache Mongo client

def lambda_handler(event, context):
    global client
    if client is None:
        client = get_mongo_client()
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