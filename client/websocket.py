import socketio
import wx
import json
from traceback import format_exc

websocket_client = socketio.client(
    ssl_verify=True,
    ssl_ca_certs=r"C:/Users/Gabri/AppData/Local/mkcert/rootCA.pem"
)

@websocket_client.event
def connect():
    print("WebSocket connected.")

@websocket_client.event
def disconnect():
    print("WebSocket disconnected.")

@websocket_client.on("message")
def on_message(data):
    try:
        message = json.loads(data)
        print(f"Received message: {message}")
    except json.JSONDecodeError:
        print("Failed to decode JSON message.")
    except Exception:
        print(f"An error occurred: {format_exc()}")