#!/usr/bin/env python3
import json
import logging
from quart import Quart, request, jsonify

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Initialize Quart app
app = Quart(__name__)

@app.route('/webhook', methods=['POST'])
async def webhook():
    """Handle incoming webhook requests and display them."""
    try:
        # Log request headers
        logger.info("Request headers:")
        logger.info(dict(request.headers))

        # Get the raw request data
        raw_data = await request.get_data()
        logger.info(f"Raw request data: {raw_data.decode('utf-8')}")

        # Try to parse JSON data
        try:
            data = await request.get_json()
            logger.info("Received webhook data:")
            logger.info(json.dumps(data, indent=2))
        except Exception as json_error:
            logger.error(f"Error parsing JSON: {str(json_error)}")
            return jsonify({
                "status": "error",
                "message": "Invalid JSON data received",
                "error": str(json_error)
            }), 400

        # Return a success response
        return jsonify({
            "status": "success",
            "message": "Webhook received and displayed",
            "received_data": data
        })

    except Exception as e:
        logger.error(f"Error processing webhook: {str(e)}")
        return jsonify({
            "status": "error",
            "message": f"Error processing webhook: {str(e)}"
        }), 500

if __name__ == "__main__":
    from hypercorn.config import Config
    from hypercorn.asyncio import serve
    import asyncio

    config = Config()
    config.bind = ["0.0.0.0:8001"]  # Using port 5002 to avoid conflicts with the main server
    config.worker_class = "asyncio"

    print("Test webhook server starting on http://localhost:5002/webhook")
    print("Send POST requests to this endpoint to test your webhook payloads")
    print("Press Ctrl+C to stop the server")

    asyncio.run(serve(app, config))