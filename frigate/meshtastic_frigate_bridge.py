import json
import time
import threading
from typing import Any, Dict

import requests
import paho.mqtt.client as mqtt
from meshtastic import SerialInterface
from meshtastic.mesh_pb2 import MeshPacket

# Configuration
MESHTASTIC_PORT = "COM6"
FRIGATE_API_URL = "http://localhost:5000/api"
MQTT_BROKER = "localhost"
MQTT_PORT = 1883
MQTT_TOPIC = "frigate/events"


def on_mqtt_message(client, userdata, msg):
    """Handle incoming MQTT messages from Frigate.
    Expected payload is JSON with detection information.
    """
    try:
        payload = json.loads(msg.payload.decode())
        handle_frigate_event(payload)
    except Exception as e:
        print(f"[Bridge] Error processing MQTT message: {e}")


def handle_frigate_event(event: Dict[str, Any]):
    """Convert a Frigate event into a Meshtastic payload and send it.
    Example event structure (simplified)::
        {
            "camera": "front_door",
            "label": "person",
            "timestamp": 1690000000,
            "snapshot_url": "http://..."
        }
    """
    # Build a simple string payload – can be extended to protobuf if needed
    payload_str = json.dumps({
        "camera": event.get("camera"),
        "label": event.get("label"),
        "time": event.get("timestamp"),
    })
    # Send via Meshtastic
    try:
        iface.sendData(payload_str.encode())
        print(f"[Bridge] Sent Meshtastic message: {payload_str}")
    except Exception as e:
        print(f"[Bridge] Failed to send Meshtastic message: {e}")


def listen_to_mqtt():
    client = mqtt.Client()
    client.on_message = on_mqtt_message
    client.connect(MQTT_BROKER, MQTT_PORT, 60)
    client.subscribe(MQTT_TOPIC)
    client.loop_forever()

# Initialize Meshtastic interface
iface = SerialInterface(port=MESHTASTIC_PORT)
print("[Bridge] Connected to Meshtastic node on", MESHTASTIC_PORT)

# Start MQTT listener in a separate thread
mqtt_thread = threading.Thread(target=listen_to_mqtt, daemon=True)
mqtt_thread.start()

print("[Bridge] MQTT listener started. Waiting for events…")
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("[Bridge] Shutting down…")
    iface.close()
