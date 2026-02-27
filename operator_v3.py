#!/usr/bin/env python3
"""
THE OPERATOR V3 — Liberty Mesh SOS Dispatch + AI Switchboard
=============================================================
Mindtech - Mesh Networks | GarageAGI LLC

Integrates GuardianBridge-style SOS dispatch into The Operator.
SOS flags (SOSP, SOSF, SOSM, SOS) are intercepted, GPS is requested,
and structured alerts are forwarded to configurable responder nodes.

All other messages route to the AI switchboard (Ollama/Gemma3).

Dependencies:
    pip install meshtastic pypubsub openai

Hardware:
    Any Meshtastic-compatible node connected via USB serial.

References:
    - GuardianBridge: https://github.com/rkolbi/GuardianBridge
    - Meshtastic Python API: https://meshtastic.org/docs/software/python/cli/
    - Canned Messages: https://meshtastic.org/docs/configuration/module/canned-message/
"""

import time
import json
import textwrap
import queue
import threading
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import meshtastic
import meshtastic.serial_interface
from pubsub import pub
from openai import OpenAI


# ============================================================
# CONFIGURATION
# ============================================================

# -- Serial Connection --
# Set to your device path. Examples:
#   Windows:  "COM6"
#   Linux:    "/dev/ttyUSB0" or "/dev/ttyACM0"
#   Mac:      "/dev/cu.usbmodem*"
COM_PORT = os.environ.get("OPERATOR_PORT", "COM6")

# -- Mesh Settings --
# Channel name to listen on. The Operator will resolve this to a channel index
# at startup and ONLY process messages on that channel (+ direct messages).
# Set via env var or edit here. Must match exactly (case-insensitive).
CHANNEL_NAME = os.environ.get("OPERATOR_CHANNEL", "LibertyMesh")
CHANNEL_INDEX = None  # Resolved at startup from CHANNEL_NAME

# -- AI Backend (Ollama) --
AI_BASE_URL = os.environ.get("OPERATOR_AI_URL", "http://localhost:11434/v1")
AI_MODEL = os.environ.get("OPERATOR_AI_MODEL", "gemma3:latest")
AI_TIMEOUT = 60.0
AI_MAX_TOKENS = 300  # Keep replies short — fits in ~2 mesh messages max

# -- Rate Limiting --
COOLDOWN_SECONDS = 10
WARNING_THROTTLE = 10
MAX_CHUNK = 200  # Meshtastic payload limit ~228 bytes, stay under

# -- SOS Configuration --
# Responder node IDs — replace these with your actual responder node IDs.
# Format: Meshtastic node ID string, e.g. "!a1b2c3d4"
# You can also set via environment: OPERATOR_RESPONDERS="!node1,!node2,!node3"
RESPONDER_NODES = os.environ.get("OPERATOR_RESPONDERS", "!ffffffff").split(",")

# SOS escalation timers (seconds)
SOS_ACK_TIMEOUT = int(os.environ.get("SOS_ACK_TIMEOUT", "300"))        # 5 min
SOS_CHECKIN_INTERVAL = int(os.environ.get("SOS_CHECKIN_INTERVAL", "120"))  # 2 min
SOS_CHECKIN_MAX = int(os.environ.get("SOS_CHECKIN_MAX", "3"))

# SOS trigger keywords — matches GuardianBridge convention
SOS_COMMANDS = {
    "SOSP": "POLICE",
    "SOSF": "FIRE",
    "SOSM": "MEDICAL",
    "SOS":  "GENERAL",
}

# -- Logging --
LOG_FILE = os.environ.get("OPERATOR_LOG", "operator_logs.jsonl")
MARKDOWN_LOG = os.environ.get("OPERATOR_MD_LOG", "operator_logs.md")

# ============================================================
# LOGGING SETUP
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("operator")


# ============================================================
# CORE STATE
# ============================================================
message_queue = queue.Queue()
client = OpenAI(base_url=AI_BASE_URL, api_key="ollama")
radio_interface = None

state_lock = threading.Lock()
conversation_history = {}
cooldown_tracker = {}
warning_tracker = {}

# Boot timestamp — used to ignore stale packets buffered on the radio
BOOT_TIME = time.time()
STALE_WINDOW = int(os.environ.get("OPERATOR_STALE_WINDOW", "10"))  # seconds

# Range test state
range_test_active = False
ping_counter = 0
test_destination = None

# SOS incident tracking
# Format: {incident_id: {author, code, timestamp, lat, lon, acks, responding, checkins, active}}
sos_incidents = {}
sos_counter = 0


# ============================================================
# HELPERS
# ============================================================

def resolve_channel_index(interface, target_name):
    """
    Look up the channel index by name from the radio's channel list.
    Returns the index (int) or None if not found.
    Comparison is case-insensitive.
    """
    if not interface or not interface.localNode:
        return None
    channels = interface.localNode.channels
    if not channels:
        return None
    for i, ch in enumerate(channels):
        ch_settings = getattr(ch, "settings", None)
        if ch_settings:
            ch_name = getattr(ch_settings, "name", "") or ""
            if ch_name.strip().lower() == target_name.strip().lower():
                return i
    return None


def is_direct_message(packet):
    """
    Check if a packet is a direct message (DM) to this node.
    DMs have a 'toId' that matches our node ID.
    """
    if not radio_interface or not radio_interface.myInfo:
        return False
    my_id = radio_interface.myInfo.my_node_num
    to_id = packet.get("to", 0)
    return to_id == my_id

def get_node_name(node_id):
    """Resolve node ID to human-readable name from the mesh node database."""
    if not radio_interface or not node_id:
        return node_id or "Unknown"
    node = radio_interface.nodes.get(node_id, {})
    user = node.get("user", {})
    return user.get("longName") or user.get("shortName") or str(node_id)


def get_node_position(node_id):
    """Extract last known GPS coordinates for a node, if available."""
    if not radio_interface or not node_id:
        return None, None
    node = radio_interface.nodes.get(node_id, {})
    pos = node.get("position", {})
    lat = pos.get("latitude")
    lon = pos.get("longitude")
    if lat and lon:
        return round(lat, 6), round(lon, 6)
    return None, None


def node_has_gps(node_id):
    """Check if a node has ever reported a GPS position (i.e., has a GPS module)."""
    if not radio_interface or not node_id:
        return False
    node = radio_interface.nodes.get(node_id, {})
    pos = node.get("position", {})
    # If the node has ever reported latitude, it has GPS hardware
    return pos.get("latitude") is not None


def send_dm(text, destination_id, channel=None, want_ack=True):
    """
    Send a direct message to a specific node. Handles chunking.
    Set want_ack=False for fire-and-forget (SOS fallbacks, broadcasts).
    """
    if not radio_interface:
        log.warning("Radio not connected, cannot send: %s", text[:50])
        return
    ch = channel if channel is not None else CHANNEL_INDEX
    chunks = textwrap.wrap(text, width=MAX_CHUNK)
    for i, chunk in enumerate(chunks):
        if len(chunks) > 1:
            paged = f"[{i+1}/{len(chunks)}] {chunk}"
        else:
            paged = chunk
        try:
            radio_interface.sendText(
                text=paged,
                destinationId=destination_id,
                channelIndex=ch,
                wantAck=want_ack,
            )
            if len(chunks) > 1:
                time.sleep(3)  # Spacing between multi-part messages
        except Exception as e:
            log.error("Send error to %s: %s", destination_id, e)


def broadcast(text, channel=None):
    """Broadcast a message to the mesh channel."""
    if not radio_interface:
        return
    ch = channel if channel is not None else CHANNEL_INDEX
    chunks = textwrap.wrap(text, width=MAX_CHUNK)
    for chunk in chunks:
        try:
            radio_interface.sendText(text=chunk, channelIndex=ch)
            time.sleep(2)
        except Exception as e:
            log.error("Broadcast error: %s", e)


def request_position(node_id, timeout=10):
    """
    Request a fresh GPS position from a node (GuardianBridge pattern).
    Runs in a sub-thread with a timeout so it never blocks the SOS handler.
    Returns True if the request was sent, False if it failed or timed out.
    """
    if not radio_interface:
        return False

    result = {"ok": False}

    def _do_request():
        try:
            radio_interface.sendPosition(destinationId=node_id, wantResponse=True)
            result["ok"] = True
            log.info("Requested position from %s", get_node_name(node_id))
        except Exception as e:
            log.warning("Position request failed for %s: %s", get_node_name(node_id), e)

    t = threading.Thread(target=_do_request, daemon=True)
    t.start()
    t.join(timeout=timeout)

    if t.is_alive():
        log.warning("Position request to %s timed out after %ds — continuing without GPS.", get_node_name(node_id), timeout)
        return False
    return result["ok"]


def log_event(event_type, data):
    """Append a structured JSON log entry (immutable audit trail)."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event_type,
        **data,
    }
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        log.error("Log write error: %s", e)


def log_markdown(sender_name, message, reply=None):
    """Append a human-readable markdown log entry."""
    try:
        with open(MARKDOWN_LOG, "a", encoding="utf-8") as f:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"### {ts}\n")
            f.write(f"**{sender_name}:** {message}\n\n")
            if reply:
                f.write(f"**Operator:** {reply}\n\n")
            f.write("---\n\n")
    except Exception as e:
        log.error("Markdown log error: %s", e)


# ============================================================
# SOS DISPATCH ENGINE
# (Inspired by GuardianBridge dispatcher/sos.py)
# ============================================================

def handle_sos(sender_id, sos_code, extra_text, channel):
    """
    Process an SOS trigger.
    1. Log the incident
    2. Request GPS from sender
    3. Wait briefly for position update
    4. Format and forward alert to all responder nodes
    5. Acknowledge to sender
    """
    global sos_counter

    sender_name = get_node_name(sender_id)
    sos_label = SOS_COMMANDS.get(sos_code, "GENERAL")

    with state_lock:
        sos_counter += 1
        incident_id = sos_counter
        sos_incidents[incident_id] = {
            "id": incident_id,
            "author": sender_id,
            "author_name": sender_name,
            "code": sos_code,
            "label": sos_label,
            "extra": extra_text,
            "timestamp": time.time(),
            "lat": None,
            "lon": None,
            "acks": [],
            "responding": [],
            "checkin_count": 0,
            "last_checkin": time.time(),
            "active": True,
        }

    log.warning(
        "*** SOS ALERT #%d *** %s from %s [%s] — %s",
        incident_id, sos_code, sender_name, sender_id, extra_text or "no details",
    )

    log_event("SOS_TRIGGERED", {
        "incident": incident_id,
        "author": sender_id,
        "author_name": sender_name,
        "code": sos_code,
        "label": sos_label,
        "extra": extra_text,
    })

    # GPS handling — check if the node has GPS hardware before requesting
    lat, lon = None, None
    has_gps = node_has_gps(sender_id)

    if has_gps:
        # Node has GPS — request fresh position (with timeout, won't block)
        try:
            gps_ok = request_position(sender_id)
            if gps_ok:
                time.sleep(5)  # Brief wait for position packet to arrive
            lat, lon = get_node_position(sender_id)
        except Exception as e:
            log.warning("GPS lookup failed for SOS #%d: %s", incident_id, e)
    else:
        # Node has no GPS module — check cache just in case, but don't request
        lat, lon = get_node_position(sender_id)
        if not lat:
            log.info("SOS #%d: Node %s has no GPS module — position unavailable.", incident_id, sender_name)

    with state_lock:
        sos_incidents[incident_id]["lat"] = lat
        sos_incidents[incident_id]["lon"] = lon

    # Format the dispatch alert — GuardianBridge sends 3 messages per responder
    alert_line1 = f"SOS #{incident_id} [{sos_label}] from {sender_name}"
    if extra_text:
        alert_line1 += f" — {extra_text}"

    if lat and lon:
        alert_line2 = f"GPS: {lat}, {lon} | Map: https://maps.google.com/?q={lat},{lon}"
    elif not has_gps:
        alert_line2 = "GPS: Node has no GPS module. Sender: share your location or nearest landmark."
    else:
        alert_line2 = "GPS: Position unavailable — last known location not on file."

    alert_line3 = f"Callback: {sender_name} | Node: {sender_id}"

    # Dispatch to all responder nodes
    dispatched_to = []
    for responder_id in RESPONDER_NODES:
        responder_id = responder_id.strip()
        if not responder_id or responder_id == "!ffffffff":
            log.warning("Dummy responder node %s — alert printed but not sent.", responder_id)
            continue
        responder_name = get_node_name(responder_id)
        log.info("Dispatching SOS #%d to responder: %s (%s)", incident_id, responder_name, responder_id)
        send_dm(alert_line1, responder_id, channel)
        time.sleep(2)
        send_dm(alert_line2, responder_id, channel)
        time.sleep(2)
        send_dm(alert_line3, responder_id, channel)
        dispatched_to.append(responder_id)
        time.sleep(1)

    log_event("SOS_DISPATCHED", {
        "incident": incident_id,
        "responders": dispatched_to,
        "lat": lat,
        "lon": lon,
    })

    # Acknowledge to the sender
    if dispatched_to:
        ack = f"SOS #{incident_id} received. {sos_label} alert sent to {len(dispatched_to)} responder(s). Stay on this channel."
        send_dm(ack, sender_id, channel)
        # If no GPS, ask sender to share location manually
        if not lat and not has_gps:
            time.sleep(2)
            send_dm(
                f"SOS #{incident_id}: Your node has no GPS. "
                f"Reply with your location or nearest landmark so responders can find you.",
                sender_id, channel, want_ack=False,
            )
    else:
        # Fallback: no responders reachable — give the sender clear guidance
        # Use want_ack=False to prevent blocking on connection timeouts
        fallback_1 = (
            f"SOS #{incident_id} RECEIVED — your {sos_label} alert has been "
            f"logged but emergency responder nodes are not currently reachable."
        )
        fallback_2 = (
            "If you have cell service, call 911 immediately. "
            "If not, broadcast your emergency on this channel — "
            "all nearby nodes will see your message."
        )
        fallback_3 = (
            f"Your alert is recorded and will be dispatched when a responder "
            f"comes online. Stay on this channel and send CANCEL {incident_id} "
            f"if no longer needed."
        )
        send_dm(fallback_1, sender_id, channel, want_ack=False)
        time.sleep(3)
        send_dm(fallback_2, sender_id, channel, want_ack=False)
        time.sleep(3)
        send_dm(fallback_3, sender_id, channel, want_ack=False)

        # Also broadcast a condensed alert to the mesh so anyone listening can help
        broadcast(
            f"SOS #{incident_id} [{sos_label}] from {sender_name} — "
            f"no responder nodes online. All stations: acknowledge if able."
        )

    # Print full alert to console (always — even with dummy nodes)
    print("\n" + "=" * 60)
    print(f"  *** SOS INCIDENT #{incident_id} ***")
    print(f"  Type:     {sos_label} ({sos_code})")
    print(f"  From:     {sender_name} ({sender_id})")
    print(f"  Details:  {extra_text or 'None'}")
    print(f"  GPS:      {lat}, {lon}" if lat else "  GPS:      Unavailable")
    print(f"  Sent to:  {len(dispatched_to)} responder(s)")
    print("=" * 60 + "\n")


def handle_sos_ack(sender_id, incident_num):
    """Process a responder ACK for an SOS incident."""
    sender_name = get_node_name(sender_id)
    with state_lock:
        incident = sos_incidents.get(incident_num)
        if not incident or not incident["active"]:
            send_dm(f"SOS #{incident_num}: No active incident found.", sender_id)
            return
        if sender_id not in incident["acks"]:
            incident["acks"].append(sender_id)

    log.info("ACK received for SOS #%d from %s", incident_num, sender_name)
    log_event("SOS_ACK", {"incident": incident_num, "responder": sender_id, "name": sender_name})

    # Notify the SOS author
    send_dm(f"SOS #{incident_num}: {sender_name} acknowledged your alert.", incident["author"])


def handle_sos_responding(sender_id, incident_num):
    """Process a responder RESPONDING for an SOS incident."""
    sender_name = get_node_name(sender_id)
    with state_lock:
        incident = sos_incidents.get(incident_num)
        if not incident or not incident["active"]:
            send_dm(f"SOS #{incident_num}: No active incident found.", sender_id)
            return
        if sender_id not in incident["responding"]:
            incident["responding"].append(sender_id)

    log.info("RESPONDING received for SOS #%d from %s", incident_num, sender_name)
    log_event("SOS_RESPONDING", {"incident": incident_num, "responder": sender_id, "name": sender_name})

    # Notify the SOS author
    send_dm(f"SOS #{incident_num}: {sender_name} is RESPONDING to your location.", incident["author"])

    # Notify other responders
    with state_lock:
        others = [r for r in incident["acks"] + incident["responding"] if r != sender_id]
    for other_id in set(others):
        send_dm(f"SOS #{incident_num}: {sender_name} is responding.", other_id)


def handle_sos_cancel(sender_id, incident_num):
    """Allow the original SOS author to cancel their alert."""
    with state_lock:
        incident = sos_incidents.get(incident_num)
        if not incident:
            send_dm(f"SOS #{incident_num}: Not found.", sender_id)
            return
        if incident["author"] != sender_id:
            send_dm(f"SOS #{incident_num}: Only the original sender can cancel.", sender_id)
            return
        incident["active"] = False

    sender_name = get_node_name(sender_id)
    log.info("SOS #%d CANCELLED by %s", incident_num, sender_name)
    log_event("SOS_CANCELLED", {"incident": incident_num, "author": sender_id})

    send_dm(f"SOS #{incident_num}: Your alert has been cancelled.", sender_id)

    # Notify responders
    with state_lock:
        all_responders = set(incident.get("acks", []) + incident.get("responding", []))
    for r_id in all_responders:
        send_dm(f"SOS #{incident_num}: CANCELLED by {sender_name}.", r_id)


# ============================================================
# SOS WELFARE CHECK WORKER
# (Runs periodically — GuardianBridge handle_active_sos_tasks pattern)
# ============================================================

def sos_watchdog_worker():
    """Periodically check active SOS incidents for escalation and welfare checks."""
    while True:
        time.sleep(60)  # Check every minute
        current_time = time.time()
        with state_lock:
            active_incidents = {
                k: v for k, v in sos_incidents.items() if v["active"]
            }

        for inc_id, inc in active_incidents.items():
            elapsed = current_time - inc["timestamp"]

            # Escalation: No ACK within timeout → broadcast to mesh
            if not inc["acks"] and elapsed > SOS_ACK_TIMEOUT:
                log.warning("SOS #%d: No ACK after %ds — escalating to broadcast!", inc_id, SOS_ACK_TIMEOUT)
                broadcast(
                    f"ESCALATION: SOS #{inc_id} [{inc['label']}] from {inc['author_name']} — "
                    f"NO RESPONDER ACK. All stations: acknowledge."
                )
                log_event("SOS_ESCALATED", {"incident": inc_id})
                # Prevent re-escalation by adding a dummy ack
                with state_lock:
                    inc["acks"].append("__escalated__")

            # Welfare check-in: Ping the SOS author periodically
            if inc["active"] and (current_time - inc["last_checkin"]) > SOS_CHECKIN_INTERVAL:
                with state_lock:
                    inc["checkin_count"] += 1
                    inc["last_checkin"] = current_time
                    checkins = inc["checkin_count"]

                if checkins <= SOS_CHECKIN_MAX:
                    send_dm(
                        f"SOS #{inc_id} CHECK-IN ({checkins}/{SOS_CHECKIN_MAX}): "
                        f"Are you OK? Reply 'OK' or 'CANCEL {inc_id}' to close.",
                        inc["author"],
                    )
                    log.info("Welfare check #%d sent for SOS #%d", checkins, inc_id)
                else:
                    # Max check-ins exceeded — escalate as UNRESPONSIVE
                    log.warning("SOS #%d: Author UNRESPONSIVE after %d check-ins!", inc_id, SOS_CHECKIN_MAX)
                    alert = (
                        f"ALERT: SOS #{inc_id} — {inc['author_name']} is UNRESPONSIVE "
                        f"after {SOS_CHECKIN_MAX} welfare checks. Last known: "
                    )
                    if inc.get("lat") and inc.get("lon"):
                        alert += f"{inc['lat']}, {inc['lon']}"
                    else:
                        alert += "Position unknown"

                    for r_id in RESPONDER_NODES:
                        r_id = r_id.strip()
                        if r_id and r_id != "!ffffffff":
                            send_dm(alert, r_id)
                            time.sleep(2)

                    broadcast(alert)
                    log_event("SOS_UNRESPONSIVE", {"incident": inc_id})
                    with state_lock:
                        inc["active"] = False


# ============================================================
# BEACON WORKER (DM RANGE TEST)
# ============================================================

def beacon_worker():
    """Sends periodic range test beacons when activated via !ping."""
    global range_test_active, ping_counter, test_destination
    while True:
        with state_lock:
            active = range_test_active
            dest = test_destination

        if active and radio_interface and dest:
            with state_lock:
                ping_counter += 1
                current_ping = ping_counter

            msg = f"[BEACON] Ping {current_ping} — The Operator"
            log.info("BEACON → %s: %s", get_node_name(dest), msg)
            try:
                radio_interface.sendText(text=msg, destinationId=dest, wantAck=True)
            except Exception as e:
                log.error("Beacon DM error: %s", e)
            time.sleep(30)
        else:
            time.sleep(1)


# ============================================================
# AI WORKER (THE SWITCHBOARD)
# ============================================================

def ai_worker():
    """Process queued messages through the LLM and respond."""
    log.info("The Operator is at the switchboard...")
    while True:
        data = message_queue.get()
        sender_id = data["sender"]
        message = data["message"]
        chan = data["channel"]
        sender_name = get_node_name(sender_id)

        try:
            with state_lock:
                if sender_id not in conversation_history:
                    conversation_history[sender_id] = []
                conversation_history[sender_id].append({"role": "user", "content": message})
                if len(conversation_history[sender_id]) > 4:
                    conversation_history[sender_id] = conversation_history[sender_id][-4:]
                current_history = list(conversation_history[sender_id])

            system_prompt = (
                "You are The Operator, the AI switchboard for Liberty Mesh — "
                "a community emergency communications network. Be clinical and concise. "
                "2 sentences max. No markdown. No emoji. "
                "If someone needs emergency help, tell them to send: SOSP (police), SOSF (fire), or SOSM (medical)."
            )
            messages = [{"role": "system", "content": system_prompt}]
            messages.extend(current_history)

            response = client.chat.completions.create(
                model=AI_MODEL,
                messages=messages,
                max_tokens=AI_MAX_TOKENS,
                timeout=AI_TIMEOUT,
            )
            full_reply = response.choices[0].message.content.strip()

            with state_lock:
                conversation_history[sender_id].append({"role": "assistant", "content": full_reply})

            log_markdown(sender_name, message, full_reply)
            log_event("AI_RESPONSE", {
                "sender": sender_id,
                "sender_name": sender_name,
                "message": message,
                "reply": full_reply,
            })

            # Send reply — send_dm handles chunking + pagination
            log.info("→ %s: %s", sender_name, full_reply[:100])
            send_dm(full_reply, sender_id, chan)

        except Exception as e:
            log.error("AI Switchboard error: %s", e)
            send_dm("[SYSTEM] The Operator encountered an error. Try again.", sender_id, chan)
        finally:
            message_queue.task_done()


# ============================================================
# MESSAGE ROUTER (onReceive callback)
# ============================================================

def onReceive(packet, interface):
    """
    Main packet handler. Routes incoming messages:
    1. SOS commands → dispatch engine
    2. ACK/RESPONDING → incident management
    3. !ping, !status → system commands
    4. Everything else → AI queue
    """
    global range_test_active, ping_counter, test_destination
    try:
        decoded = packet.get("decoded", {})
        if "text" not in decoded:
            return

        message = decoded["text"].strip()
        sender = packet.get("fromId")

        # Drop ghost packets
        if not sender or sender == "Unknown":
            return

        # Drop stale packets buffered on the radio before this script started.
        # Meshtastic replays recent packets on serial connect — these are old
        # messages that should not trigger SOS dispatch or AI responses.
        rx_time = packet.get("rxTime", 0)
        if rx_time and rx_time < BOOT_TIME - STALE_WINDOW:
            sender_name = get_node_name(sender)
            log.debug("Dropping stale packet from %s (rxTime %d < boot %d)", sender_name, rx_time, int(BOOT_TIME))
            return

        incoming_chan = packet.get("channel", 0)
        sender_name = get_node_name(sender)
        text_upper = message.upper().strip()

        log.debug("RX: %s (ch%d) → %s", sender_name, incoming_chan, message[:50])

        # Only process messages on our named channel OR direct messages to us.
        # This filters out LongFast, unencrypted, and any other channel traffic.
        dm = is_direct_message(packet)
        if not dm and incoming_chan != CHANNEL_INDEX:
            return

        current_time = time.time()

        # ======================
        # SOS INTERCEPT (Priority 1 — before cooldown, before AI)
        # ======================

        # Check for SOS trigger keywords
        sos_match = None
        for cmd in sorted(SOS_COMMANDS.keys(), key=len, reverse=True):
            if text_upper.startswith(cmd):
                sos_match = cmd
                break

        if sos_match:
            extra = message[len(sos_match):].strip()
            log_event("SOS_RECEIVED", {"sender": sender, "name": sender_name, "code": sos_match, "raw": message})
            # Run SOS handler in a thread to avoid blocking the radio listener
            threading.Thread(
                target=handle_sos, args=(sender, sos_match, extra, incoming_chan), daemon=True
            ).start()
            return

        # ACK <n> — responder acknowledges an SOS
        if text_upper.startswith("ACK"):
            parts = text_upper.split()
            if len(parts) >= 2 and parts[1].isdigit():
                threading.Thread(
                    target=handle_sos_ack, args=(sender, int(parts[1])), daemon=True
                ).start()
            else:
                send_dm("Usage: ACK <incident#>  Example: ACK 1", sender)
            return

        # RESPONDING <n> — responder is en route
        if text_upper.startswith("RESPONDING"):
            parts = text_upper.split()
            if len(parts) >= 2 and parts[1].isdigit():
                threading.Thread(
                    target=handle_sos_responding, args=(sender, int(parts[1])), daemon=True
                ).start()
            else:
                send_dm("Usage: RESPONDING <incident#>  Example: RESPONDING 1", sender)
            return

        # CANCEL <n> — author cancels their SOS
        if text_upper.startswith("CANCEL"):
            parts = text_upper.split()
            if len(parts) >= 2 and parts[1].isdigit():
                threading.Thread(
                    target=handle_sos_cancel, args=(sender, int(parts[1])), daemon=True
                ).start()
            else:
                send_dm("Usage: CANCEL <incident#>  Example: CANCEL 1", sender)
            return

        # ======================
        # SYSTEM COMMANDS
        # ======================

        # !ping — toggle range test
        if text_upper == "!PING":
            with state_lock:
                range_test_active = not range_test_active
                test_destination = sender
                if range_test_active:
                    ping_counter = 0
                    ack_msg = f"[SYSTEM] Range test STARTED for {sender_name}."
                else:
                    ack_msg = "[SYSTEM] Range test STOPPED."
            send_dm(ack_msg, sender, incoming_chan)
            log.info(ack_msg)
            return

        # !status — system status
        if text_upper == "!STATUS":
            active_sos = sum(1 for i in sos_incidents.values() if i["active"])
            status = (
                f"[SYSTEM] Operator Online | "
                f"Queue: {message_queue.qsize()} | "
                f"Active SOS: {active_sos} | "
                f"Responders: {len([r for r in RESPONDER_NODES if r.strip() != '!ffffffff'])}"
            )
            send_dm(status, sender, incoming_chan)
            return

        # !help — show available commands
        if text_upper == "!HELP":
            help_text = (
                "Commands: SOSP (police) | SOSF (fire) | SOSM (medical) | "
                "SOS (general) | ACK <#> | RESPONDING <#> | CANCEL <#> | "
                "!ping | !status | !help — Or just talk to The Operator."
            )
            send_dm(help_text, sender, incoming_chan)
            return

        # ======================
        # DYNAMIC BOUNCER (rate limiting for AI queue)
        # ======================
        with state_lock:
            if message_queue.qsize() > 0 and sender in cooldown_tracker:
                if current_time - cooldown_tracker[sender] < COOLDOWN_SECONDS:
                    if current_time - warning_tracker.get(sender, 0) > WARNING_THROTTLE:
                        time_left = int(COOLDOWN_SECONDS - (current_time - cooldown_tracker[sender]))
                        send_dm(f"[SYSTEM] Busy. Wait {time_left}s.", sender, incoming_chan)
                        warning_tracker[sender] = current_time
                    return
            cooldown_tracker[sender] = current_time
            warning_tracker[sender] = 0

        # ======================
        # ROUTE TO AI SWITCHBOARD
        # ======================
        log.info("Queued message from %s for AI.", sender_name)
        message_queue.put({"sender": sender, "message": message, "channel": incoming_chan})

    except Exception as e:
        log.error("onReceive error: %s", e)


# ============================================================
# STARTUP
# ============================================================

def print_banner():
    """Print startup banner with configuration summary."""
    print("\n" + "=" * 60)
    print("  THE OPERATOR V3 — Liberty Mesh SOS Dispatch")
    print("  Mindtech - Mesh Networks | GarageAGI LLC")
    print("=" * 60)
    print(f"  Port:        {COM_PORT}")
    print(f"  Channel:     \"{CHANNEL_NAME}\" (index {CHANNEL_INDEX})")
    print(f"  AI Model:    {AI_MODEL}")
    print(f"  Responders:  {', '.join(RESPONDER_NODES)}")
    print(f"  Log:         {LOG_FILE}")
    print(f"  SOS ACK Timeout:    {SOS_ACK_TIMEOUT}s")
    print(f"  SOS Check-in:       every {SOS_CHECKIN_INTERVAL}s (max {SOS_CHECKIN_MAX})")
    print("=" * 60)

    if all(r.strip() == "!ffffffff" for r in RESPONDER_NODES):
        print("\n  ⚠  WARNING: Using dummy responder node (!ffffffff)")
        print("  ⚠  SOS alerts will be LOGGED but not dispatched.")
        print("  ⚠  Set OPERATOR_RESPONDERS env var or edit RESPONDER_NODES.")
        print()


if __name__ == "__main__":

    # Start worker threads
    threading.Thread(target=ai_worker, daemon=True, name="ai-worker").start()
    threading.Thread(target=beacon_worker, daemon=True, name="beacon").start()
    threading.Thread(target=sos_watchdog_worker, daemon=True, name="sos-watchdog").start()

    log.info("Connecting to mesh radio on %s...", COM_PORT)
    try:
        radio_interface = meshtastic.serial_interface.SerialInterface(devPath=COM_PORT)

        # Resolve channel name to index
        CHANNEL_INDEX = resolve_channel_index(radio_interface, CHANNEL_NAME)
        if CHANNEL_INDEX is None:
            # List available channels to help the user
            available = []
            if radio_interface.localNode and radio_interface.localNode.channels:
                for i, ch in enumerate(radio_interface.localNode.channels):
                    ch_settings = getattr(ch, "settings", None)
                    if ch_settings:
                        ch_name = getattr(ch_settings, "name", "") or ""
                        if ch_name.strip():
                            available.append(f"  [{i}] {ch_name}")
            log.error("Channel \"%s\" not found on this radio!", CHANNEL_NAME)
            if available:
                print("\nAvailable channels:")
                for line in available:
                    print(line)
            print(f"\nSet OPERATOR_CHANNEL env var to one of the above, or edit CHANNEL_NAME in the script.")
            radio_interface.close()
            exit(1)

        print_banner()
        pub.subscribe(onReceive, "meshtastic.receive")

        log.info("The Operator V3 is LIVE on \"%s\" (channel %d) + DMs", CHANNEL_NAME, CHANNEL_INDEX)
        log.info("SOS commands active: %s", ", ".join(SOS_COMMANDS.keys()))

        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        print("\nShutting down The Operator...")
        if radio_interface:
            radio_interface.close()
        log.info("The Operator has left the switchboard.")
    except Exception as e:
        log.error("Fatal startup error: %s", e)
        if radio_interface:
            radio_interface.close()