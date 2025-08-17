from datetime import datetime, timezone, timedelta

from scripts.util import get_mongo_client
from scripts.util import create_response
from scripts.util import to_utc_date

client = None  # cache Mongo client

def lambda_handler(event, context):

    global client
    if client is None:
        client = get_mongo_client()
    try:
        qsp = (event or {}).get("queryStringParameters") or {}
        account = qsp.get("account") if isinstance(qsp, dict) and qsp.get("account") else "default"
        doc = client.shipcore.ebay_tokens.find_one({"account": account})

        if not doc:
            return create_response(200, {"connected": False})
        now = datetime.now(timezone.utc)
        skew = timedelta(seconds=30)
        refresh_expires_at = to_utc_date(doc.get("refresh_expires_at"))
        refresh_ok = bool(refresh_expires_at and refresh_expires_at > (now + skew))

        payload = {
            "connected": refresh_ok,
        }
        return create_response(200, payload)

    except Exception:
        return create_response(200, {"connected": False})