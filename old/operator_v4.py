import time
import threading
import queue
import textwrap
from pubsub import pub
import meshtastic
import meshtastic.serial_interface
from openai import OpenAI


# ==========================================
# CONFIGURATION & STATE
# ==========================================
# Local Ollama connection (100% off-grid)
client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")
MODEL = "gemma3:latest"

# Serial port for Meshtastic gateway (None = auto-detect)
DEV_PATH = None  # e.g. "/dev/ttyACM0" or "COM6"

# RESPONDER NODE IDs
# IMPORTANT: Replace these with the actual IDs of your demo nodes!
RESPONDERS = {
    '!sos':    None,         # Broadcasts to all responders
    '!police': '!aabbccdd',  # Police Station node ID
    '!fire':   '!eeff0011',  # Firehouse node ID
    '!ems':    '!22334455',  # EMS node ID
    '!help':   None          # Broadcasts to all
}

# Channel to monitor (set to your primary channel index)
CHANNEL_INDEX = 0

# Core State
message_queue = queue.Queue()
conversation_history = {}
state_lock = threading.Lock()
log_lock = threading.Lock()  # Separate lock for file I/O — prevents deadlock
radio_interface = None


# ==========================================
# HELPER FUNCTIONS
# ==========================================
def get_node_name(node_id):
    """Get the long name (phone number/name) of the sender."""
    if not radio_interface:
        return str(node_id)
    node = radio_interface.nodes.get(node_id, {})
    user = node.get('user', {})
    return user.get('longName') or user.get('shortName') or str(node_id)


def get_node_gps(node_id):
    """Pull last known GPS from Meshtastic's node DB."""
    if not radio_interface or not node_id:
        return None, None
    node = radio_interface.nodes.get(node_id, {})
    position = node.get('position', {})
    lat = position.get('latitude')
    lon = position.get('longitude')
    if lat and lon:
        return round(lat, 5), round(lon, 5)
    return None, None


def is_my_node(sender):
    """Check if a sender ID is our own gateway node. Prevents echo loops."""
    if not radio_interface or not sender:
        return False
    try:
        my_num = radio_interface.myInfo.my_node_num
        if sender.startswith("!"):
            sender_num = int(sender[1:], 16)
        else:
            sender_num = int(sender)
        return sender_num == my_num
    except (ValueError, AttributeError):
        return False


def log_to_markdown(entry):
    """Immutable audit trail logging. Uses its own lock — never nest inside state_lock."""
    with log_lock:
        with open("operator_logs.md", "a", encoding="utf-8") as f:
            f.write(entry + "\n\n")


def safe_send(text, destinationId=None, channelIndex=0):
    """Centralized send with error handling."""
    if not radio_interface:
        print(f"[SEND] No radio. Dropping: {text[:60]}")
        return False
    try:
        if destinationId:
            radio_interface.sendText(text=text, destinationId=destinationId, channelIndex=channelIndex)
        else:
            radio_interface.sendText(text=text, channelIndex=channelIndex)
        return True
    except Exception as e:
        print(f"[SEND] Error: {e}")
        return False


def match_trigger(msg_lower):
    """Match an SOS trigger with word boundary check. Returns trigger string or None."""
    SOS_TRIGGERS = ['!police', '!fire', '!ems', '!help', '!sos']
    for trigger in SOS_TRIGGERS:
        if msg_lower == trigger or msg_lower.startswith(trigger + ' '):
            return trigger
    return None


# ==========================================
# THE AI WORKER (Background Thread)
# ==========================================
def ai_worker():
    print("[WORKER] The Operator is at the switchboard...")
    while True:
        data = message_queue.get()
        sender_id = data['sender']
        message = data['message']
        chan = data['channel']
        is_sos = data.get('is_sos', False)

        with state_lock:
            # Memory Management (keep last 4 exchanges)
            if sender_id not in conversation_history:
                conversation_history[sender_id] = []

            conversation_history[sender_id].append({"role": "user", "content": message})
            if len(conversation_history[sender_id]) > 4:
                conversation_history[sender_id] = conversation_history[sender_id][-4:]

            current_history = list(conversation_history[sender_id])
        # Lock released before LLM call — this is correct, don't hold it during inference

        # Dynamic System Prompting
        if is_sos:
            sys_prompt = (
                "You are an Emergency Dispatch Operator. "
                "Assess the situation, ask ONE critical triage question, "
                "and keep your response under 2 sentences. "
                "Be clinical and calm. No markdown."
            )
        else:
            sys_prompt = (
                "You are The Operator. Be clinical and concise. "
                "2 sentences max. No markdown."
            )

        messages = [{"role": "system", "content": sys_prompt}]
        messages.extend(current_history)

        try:
            response = client.chat.completions.create(model=MODEL, messages=messages, timeout=30.0)
            full_reply = response.choices[0].message.content.strip()

            # Guard against empty LLM response
            if not full_reply:
                full_reply = "[SYSTEM] No response generated. Try again."

            with state_lock:
                conversation_history[sender_id].append({"role": "assistant", "content": full_reply})

            sender_name = get_node_name(sender_id)
            prefix = "SOS TRIAGE" if is_sos else "Operator"
            log_to_markdown(f"**To {sender_name}:** {full_reply}\n---")

            # Chunking for LoRa (180 chars max)
            chunks = textwrap.wrap(full_reply, 180)
            total = len(chunks)
            for i, chunk in enumerate(chunks):
                chunk_text = f"[{i+1}/{total}] {chunk}" if total > 1 else chunk
                print(f"  -> {prefix} to {sender_name}: {chunk_text}")
                safe_send(chunk_text, destinationId=sender_id, channelIndex=chan)
                time.sleep(3)

        except Exception as e:
            print(f"[ERROR] AI Worker failed: {e}")
            safe_send("[SYSTEM] Operator error. Message logged. Try again.",
                      destinationId=sender_id, channelIndex=chan)

        message_queue.task_done()


# ==========================================
# THE ROUTER (Meshtastic Event Hook)
# ==========================================
def on_receive(packet, interface):
    if 'decoded' not in packet or 'text' not in packet['decoded']:
        return

    message = packet['decoded']['text'].strip()
    sender = packet.get('fromId')

    if not sender or sender == "Unknown":
        return

    incoming_chan = packet.get('channel', 0)

    # Channel filter — only process messages on our primary channel
    if incoming_chan != CHANNEL_INDEX:
        return

    # Ignore messages from ourselves (prevents echo loops)
    if is_my_node(sender):
        return

    phone = get_node_name(sender)

    print(f"[RX] {phone}: {message}")
    log_to_markdown(f"**From {phone} ({sender}):** {message}")

    msg_lower = message.lower().strip()

    # === COMMAND: !ping ===
    if msg_lower == "!ping":
        safe_send("[SYSTEM] PONG. Signal received by The Operator.",
                  destinationId=sender, channelIndex=incoming_chan)
        return

    # === COMMAND: !status ===
    if msg_lower == "!status":
        node_count = len(radio_interface.nodes) if radio_interface else 0
        resp_count = len([v for v in RESPONDERS.values() if v])
        status = (
            f"[SYSTEM] Operator Online | "
            f"Queue: {message_queue.qsize()} | "
            f"Nodes: {node_count} | "
            f"Responders: {resp_count}"
        )
        safe_send(status, destinationId=sender, channelIndex=incoming_chan)
        return

    # === SOS BLOCK ===
    matched_trigger = match_trigger(msg_lower)

    if matched_trigger:
        lat, lon = get_node_gps(sender)
        gps_str = f"GPS: {lat},{lon}" if lat else "GPS: UNKNOWN"
        timestamp = time.strftime('%Y-%m-%d %H:%M:%S')

        print(f"\n{'='*50}")
        print(f"  SOS EVENT: {matched_trigger.upper()}")
        print(f"  From: {phone} ({sender})")
        print(f"  {gps_str}")
        print(f"  Time: {timestamp}")
        print(f"{'='*50}\n")

        # 1. Immediate ACK to citizen (bypasses AI queue)
        ack = f"[SOS] {matched_trigger.upper()} RECEIVED. {gps_str}"
        safe_send(ack, destinationId=sender, channelIndex=incoming_chan)
        time.sleep(2)  # Let LoRa TX buffer clear before dispatch

        # 2. Build dispatch payload
        context = message[len(matched_trigger):].strip()
        dispatch = (
            f"[DISPATCH] {matched_trigger.upper()} | "
            f"From: {phone} | {gps_str} | "
            f"Time: {time.strftime('%H:%M:%S')}"
        )
        if context:
            dispatch += f" | {context[:80]}"

        # 3. Route to specific responder OR broadcast
        target = RESPONDERS.get(matched_trigger)
        all_responders = [v for v in RESPONDERS.values() if v]

        if target:
            safe_send(dispatch, destinationId=target, channelIndex=incoming_chan)
            print(f"[DISPATCH] Routed to {target}")
        elif all_responders:
            for resp_id in all_responders:
                safe_send(dispatch, destinationId=resp_id, channelIndex=incoming_chan)
                time.sleep(2)
            print(f"[DISPATCH] Broadcast to {len(all_responders)} responder(s)")
        else:
            safe_send(dispatch, channelIndex=incoming_chan)
            print("[DISPATCH] No responders configured — broadcast to channel")

        # 4. Log the SOS event
        log_to_markdown(
            f"## SOS EVENT\n"
            f"**Trigger:** {matched_trigger}\n"
            f"**From:** {phone} ({sender})\n"
            f"**{gps_str}**\n"
            f"**Time:** {timestamp}\n"
            f"**Routed To:** {target or 'ALL RESPONDERS'}\n"
            f"---"
        )

        # 5. Queue context for AI triage if present
        if context:
            message_queue.put({
                'sender': sender,
                'message': context,
                'channel': incoming_chan,
                'is_sos': True
            })
        return

    # === GENERAL MESSAGES → AI QUEUE ===
    if message_queue.qsize() > 15:
        print(f"[BOUNCER] Queue full ({message_queue.qsize()}), dropping from {phone}")
        safe_send("[SYSTEM] Busy. Try again in 30s.",
                  destinationId=sender, channelIndex=incoming_chan)
        return

    message_queue.put({
        'sender': sender,
        'message': message,
        'channel': incoming_chan,
        'is_sos': False
    })


# ==========================================
# MAIN EXECUTION
# ==========================================
def main():
    global radio_interface

    print("""
    ╔══════════════════════════════════════╗
    ║   THE OPERATOR V4 — LIBERTY MESH    ║
    ║   Mindtech Mesh Networks            ║
    ║   SOS Dispatch + AI Switchboard     ║
    ╚══════════════════════════════════════╝
    """)

    threading.Thread(target=ai_worker, daemon=True).start()

    try:
        if DEV_PATH:
            radio_interface = meshtastic.serial_interface.SerialInterface(devPath=DEV_PATH)
        else:
            radio_interface = meshtastic.serial_interface.SerialInterface()

        # Subscribe AFTER radio is connected — prevents crash on early packets
        pub.subscribe(on_receive, "meshtastic.receive.text")

        resp_count = len([v for v in RESPONDERS.values() if v])
        print(f"  Connected to Meshtastic Gateway")
        print(f"  Channel: {CHANNEL_INDEX}")
        print(f"  LLM: {MODEL} via Ollama")
        print(f"  Responders: {resp_count} configured")
        print(f"  Commands: !ping | !status | !sos | !police | !fire | !ems | !help")
        print(f"  Logs: operator_logs.md")
        print(f"\n  Listening...\n")

    except Exception as e:
        print(f"[ERROR] Could not connect to radio: {e}")
        return

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down The Operator...")
        if radio_interface:
            radio_interface.close()
        print("Operator offline. All logs preserved.")


if __name__ == "__main__":
    main()