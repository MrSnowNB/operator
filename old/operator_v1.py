import time
import textwrap
import queue
import threading
import meshtastic
import meshtastic.serial_interface
from pubsub import pub
from openai import OpenAI

# ==========================================
# CONFIG
# ==========================================
COM_PORT = "COM6"
CHANNEL_INDEX = 0      # Set to 0 to work with standard Primary channel
COOLDOWN_SECONDS = 10
WARNING_THROTTLE = 10
MAX_CHUNK = 180

# ==========================================
# CORE GLOBALS
# ==========================================
message_queue = queue.Queue()
client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")
radio_interface = None
conversation_history = {}
cooldown_tracker = {}
warning_tracker = {}

range_test_active = False
ping_counter = 0

def get_node_name(node_id):
    if not radio_interface or not node_id:
        return node_id or "Unknown"
    node = radio_interface.nodes.get(node_id, {})
    user = node.get('user', {})
    return user.get('longName') or user.get('shortName') or node_id

# ==========================================
# BEACON WORKER (RANGE TEST)
# ==========================================
def beacon_worker():
    global range_test_active, ping_counter
    while True:
        if range_test_active and radio_interface:
            ping_counter += 1
            msg = f"[BEACON] Range Test Ping {ping_counter} - The Operator is Online"
            print(f"\n[BEACON] Transmitting: {msg}")
            try:
                radio_interface.sendText(text=msg, channelIndex=CHANNEL_INDEX)
            except Exception as e:
                print(f"[BEACON] Error: {e}")
            time.sleep(30)
        else:
            time.sleep(1)

# ==========================================
# AI WORKER (THE OPERATOR)
# ==========================================
def ai_worker():
    print("[WORKER] The Operator is standing by at the switchboard...")
    while True:
        data = message_queue.get()
        sender_id = data['sender']
        message = data['message']
        chan = data['channel'] # Reply on the channel the user is actually on
        sender_name = get_node_name(sender_id)

        try:
            if sender_id not in conversation_history:
                conversation_history[sender_id] = []

            conversation_history[sender_id].append({"role": "user", "content": message})
            if len(conversation_history[sender_id]) > 4:
                conversation_history[sender_id] = conversation_history[sender_id][-4:]

            messages = [{"role": "system", "content": "You are The Operator. Be clinical and concise. 2 sentences max. No markdown."}]
            messages.extend(conversation_history[sender_id])

            response = client.chat.completions.create(model="gemma3:latest", messages=messages)
            full_reply = response.choices[0].message.content.strip()
            conversation_history[sender_id].append({"role": "assistant", "content": full_reply})

            with open("operator_logs.md", "a", encoding="utf-8") as f:
                f.write(f"**{sender_name}:** {message}\n\n**Operator:** {full_reply}\n---\n")

            chunks = textwrap.wrap(full_reply, width=MAX_CHUNK)
            for i, chunk in enumerate(chunks):
                paged = f"[{i+1}/{len(chunks)}] {chunk}"
                print(f"  → Routing to {sender_name}: {paged}")
                if radio_interface:
                    radio_interface.sendText(text=paged, destinationId=sender_id, channelIndex=chan, wantAck=True)
                time.sleep(5)

        except Exception as e:
            print(f"[WORKER] AI Switchboard Error: {e}")
        finally:
            message_queue.task_done()

# ==========================================
# RADIO + BOUNCER
# ==========================================
def onReceive(packet, interface):
    global range_test_active, ping_counter
    try:
        if 'decoded' not in packet or 'text' not in packet['decoded']:
            return
        
        message = packet['decoded']['text'].strip()
        sender = packet.get('fromId', 'Unknown')
        incoming_chan = packet.get('channel', 0)
        sender_name = get_node_name(sender)

        # DEBUG: See everything hitting the radio
        print(f"[DEBUG] Raw Signal: From={sender_name} | Chan={incoming_chan} | Msg={message[:30]}")

        # CHANNEL GATE: Only process if it matches our config
        if incoming_chan != CHANNEL_INDEX:
            return

        current_time = time.time()
        
        # Bouncer Logic
        if sender in cooldown_tracker:
            if current_time - cooldown_tracker[sender] < COOLDOWN_SECONDS:
                if current_time - warning_tracker.get(sender, 0) > WARNING_THROTTLE:
                    time_left = int(COOLDOWN_SECONDS - (current_time - cooldown_tracker[sender]))
                    warning = f"[SYSTEM] Busy. Wait {time_left}s."
                    if radio_interface:
                        radio_interface.sendText(text=warning, destinationId=sender, channelIndex=incoming_chan)
                    warning_tracker[sender] = current_time
                return

        # Valid Traffic
        cooldown_tracker[sender] = current_time
        warning_tracker[sender] = 0

        # Commands
        if message.lower() == "!ping":
            range_test_active = not range_test_active
            ack = "[SYSTEM] Range test STARTED." if range_test_active else "[SYSTEM] Range test STOPPED."
            if radio_interface:
                radio_interface.sendText(text=ack, destinationId=sender, channelIndex=incoming_chan)
            return

        if message.lower() == "!status":
            status = f"[SYSTEM] Operator Online. Queue: {message_queue.qsize()}"
            if radio_interface:
                radio_interface.sendText(text=status, destinationId=sender, channelIndex=incoming_chan)
            return

        # Queue for AI
        print(f"[RADIO] Valid message from {sender_name} queued for Operator.")
        message_queue.put({'sender': sender, 'message': message, 'channel': incoming_chan})

    except Exception as e:
        print(f"[RADIO] Receive Error: {e}")

# ==========================================
# EXECUTION
# ==========================================
if __name__ == "__main__":
    threading.Thread(target=ai_worker, daemon=True).start()
    threading.Thread(target=beacon_worker, daemon=True).start()

    print(f"Connecting to Heltec V3 on {COM_PORT}...")
    try:
        radio_interface = meshtastic.serial_interface.SerialInterface(devPath=COM_PORT)
        pub.subscribe(onReceive, "meshtastic.receive")
        print(f"\n✅ The Operator is LIVE on Channel {CHANNEL_INDEX}")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down...")
        if radio_interface: radio_interface.close()