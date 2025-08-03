from scripts.mongo import get_mongo_client
from scripts.util import create_response
import json

client = None


def lambda_handler(event, context):
    try:
        global client
        if client is None:
            client = get_mongo_client()

        body = json.loads(event.get("body") or "{}")

        required_fields = ["upc", "mpn", "category", "brand"]
        missing = [field for field in required_fields if field not in body]
        if missing:
            return create_response(
                400, {"error": f"Missing fields: {', '.join(missing)}"}
            )

        db = client["shipcore"]
        db["product"].insert_one(body)

        return create_response(201, {"message": "Product added successfully"})

    except Exception as e:
        return create_response(500, {"error": str(e)})
