from scripts.mongo import get_mongo_client
from scripts.util import create_response

client = None


def lambda_handler(event, context):
    try:
        global client
        if client is None:
            client = get_mongo_client()

        db = client["shipcore"]
        products = list(db["product"].find({}, {"_id": 0}))

        return create_response(200, products)

    except Exception as e:

        return create_response(500, {"error": str(e)})
