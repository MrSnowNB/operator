# Meshtastic - Frigate Bridge Setup

Since the Frigate container is already running, this guide explains how to start the bridge script to connect your Meshtastic node (on COM6) with Frigate events.

## Prerequisites
Ensure Python is installed along with the required packages:
```bash
python -m pip install -r requirements.txt
```

## Running the Bridge
Start the bridge script from the project directory:
```bash
python meshtastic_frigate_bridge.py
```

### Configuration
By default, the script connects to:
- **Meshtastic Port:** `COM6`
- **Frigate MQTT:** `localhost:1883` listening on topic `frigate/events`

If your Frigate MQTT broker runs on a different host, edit `MQTT_BROKER` inside `meshtastic_frigate_bridge.py`.

## Testing the Connection
You can run the unit test to verify that the payload conversion works correctly:
```bash
pytest test_bridge.py
```
Or simply use Python's built-in unittest if pytest is not installed:
```bash
python -m unittest test_bridge.py
```
