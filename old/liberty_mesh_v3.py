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
COM_PORT = "COM6"                    # ← Change if needed
CHANNEL_INDEX = 1
COOLDOWN_SECONDS = 60
WARNING_THROTTLE = 15
MAX_CHUNK = 195                      # Safe for LongFast

# ==========================================
# CORE
# ==========================================
message_queue = queue.Queue()
client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")
radio_interface = None
conversation_history = {}
cooldown_tracker = {}
warning_tracker = {}

def get_node_name(node_id):
    """Friendly name or fallback to ID"""
    if not radio_interface or not node_id:
        return node_id or "Unknown"
    node = radio_interface.nodes.get(node_id, {})
    user = node.get('user', {})
    return user.get('longName') or user.get('shortName') or node_id

# ==========================================
# AI WORKER
# ==========================================
def ai_worker():
    print("[WORKER] Alice (Gemma3) is ready for class...")
    while True:
        data = message_queue.get()
        sender_id = data['sender']
        message = data['message']
        sender_name = get_node_name(sender_id)

        print(f"\n[WORKER] Processing from {sender_name}: {message[:60]}...")

        try:
            if sender_id not in conversation_history:
                conversation_history[sender_id] = []

            conversation_history[sender_id].append({"role": "user", "content": message})
            if len(conversation_history[sender_id]) > 4:
                conversation_history[sender_id] = conversation_history[sender_id][-4:]

            messages = [{
                "role": "system",
                "content": "You are Alice, a kind and patient elementary-school teacher on a slow radio network. "
                           "Be encouraging, concise, and fun. Limit every reply to 3 sentences max unless paging. "
                           "Never use markdown or emojis. Always stay in character as Alice."
            }]
            messages.extend(conversation_history[sender_id])

            response = client.chat.completions.create(model="gemma3:latest", messages=messages)
            full_reply = response.choices[0].message.content.strip()

            conversation_history[sender_id].append({"role": "assistant", "content": full_reply})

            with open("classroom_logs.md", "a", encoding="utf-8") as f:
                f.write(f"**{sender_name}:** {message}\n\n**Alice:** {full_reply}\n---\n")

            chunks = textwrap.wrap(full_reply, width=MAX_CHUNK)
            for i, chunk in enumerate(chunks):
                paged = f"[{i+1}/{len(chunks)}] {chunk}"
                print(f"  → Sending chunk {i+1}/{len(chunks)} to {sender_name}")
                if radio_interface:
                    radio_interface.sendText(
                        text=paged,
                        destinationId=sender_id,
                        channelIndex=CHANNEL_INDEX,
                        wantAck=True
                    )
                time.sleep(10)

        except Exception as e:
            print(f"[WORKER] Error: {e}")
        finally:
            message_queue.task_done()

# ==========================================
# RADIO + BOUNCER
# ==========================================
def onReceive(packet, interface):
    try:
        if 'decoded' not in packet or 'text' not in packet['decoded']:
            return
        message = packet['decoded']['text'].strip()
        sender = packet.get('fromId')
        if not sender or packet.get('channel', 0) != CHANNEL_INDEX:
            return

        current_time = time.time()
        sender_name = get_node_name(sender)

        # Bouncer
        if sender in cooldown_tracker and current_time - cooldown_tracker[sender] < COOLDOWN_SECONDS:
            time_left = int(COOLDOWN_SECONDS - (current_time - cooldown_tracker[sender]))
            if current_time - warning_tracker.get(sender, 0) > WARNING_THROTTLE:
                warning = f"[SYSTEM] Alice is busy. Please wait {time_left}s."
                print(f"[RADIO] Spam intercepted from {sender_name} → warning sent ({time_left}s left)")
                if radio_interface:
                    radio_interface.sendText(text=warning, destinationId=sender, channelIndex=CHANNEL_INDEX, wantAck=False)
                warning_tracker[sender] = current_time
            else:
                print(f"[RADIO] Silent drop from {sender_name}")
            return

        # Valid
        cooldown_tracker[sender] = current_time
        warning_tracker[sender] = 0

        # Commands
        lower = message.lower()
        if lower == "!help":
            help_msg = "[SYSTEM] Commands: !status, !help, !students"
            if radio_interface:
                radio_interface.sendText(text=help_msg, destinationId=sender, channelIndex=CHANNEL_INDEX)
            return
        if lower == "!students":
            students = [get_node_name(s) for s in cooldown_tracker.keys()]
            status = f"[SYSTEM] Students online: {', '.join(students) or 'None yet'}"
            if radio_interface:
                radio_interface.sendText(text=status, destinationId=sender, channelIndex=CHANNEL_INDEX)
            return
        if lower == "!status":
            status = f"[SYSTEM] Alice Online. Queue: {message_queue.qsize()}"
            if radio_interface:
                radio_interface.sendText(text=status, destinationId=sender, channelIndex=CHANNEL_INDEX)
            return

        print(f"[RADIO] New question from {sender_name} → queued")
        message_queue.put({'sender': sender, 'message': message})

    except Exception as e:
        print(f"[RADIO] Error: {e}")

# ==========================================
# START
# ==========================================
if __name__ == "__main__":
    worker = threading.Thread(target=ai_worker, daemon=True)
    worker.start()

    print(f"🔌 Connecting to Heltec V3 on {COM_PORT} (Private Channel {CHANNEL_INDEX})...")
    try:
        radio_interface = meshtastic.serial_interface.SerialInterface(devPath=COM_PORT)
        time.sleep(4)  # Let node list populate

        # FIXED LINE — works with current meshtastic library (returns dict)
        my_node_info = radio_interface.getMyNodeInfo()
        my_id = my_node_info.get("user", {}).get("id") if my_node_info else "UNKNOWN"

        pub.subscribe(onReceive, "meshtastic.receive")

        print(f"✅ Liberty Mesh v2.2 + Alice is LIVE")
        print(f"   My node: {get_node_name(my_id)} ({my_id})")
        print("   Students: DM me directly\n   (Ctrl+C to stop)\n")

        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        print("\n\nShutting down Liberty Mesh...")
        if radio_interface:
            radio_interface.close()
    except Exception as e:
        print(f"Connection failed: {e}")