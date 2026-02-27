import time
import threading
import queue
import textwrap
import json
from pubsub import pub
import meshtastic
import meshtastic.serial_interface
from openai import OpenAI


# ==========================================
# CONFIGURATION
# ==========================================
client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")
MODEL = "gemma3:latest"
DEV_PATH = None  # e.g. "/dev/ttyACM0" or "COM6"

RESPONDERS = {
    '!sos':    None,         # Broadcasts to all responders
    '!police': '!aabbccdd',  # Police Station node ID
    '!fire':   '!eeff0011',  # Firehouse node ID
    '!ems':    '!22334455',  # EMS node ID
    '!help':   None          # Broadcasts to all
}

CHANNEL_INDEX = 0
TRIAGE_TIMEOUT = 600  # 10 minutes of silence = auto-close session
TRIAGE_MAX_EXCHANGES = 12  # 6 back-and-forths before trimming
LOG_FILE = "operator_logs.jsonl"


# ==========================================
# CORE STATE
# ==========================================
message_queue = queue.Queue()
conversation_history = {}   # General chat only (non-emergency)
active_sessions = {}        # Triage sessions: {sender_id: session_obj}
state_lock = threading.Lock()
log_lock = threading.Lock()
radio_interface = None


# ==========================================
# JSONL LOGGING
# ==========================================
def log_event(event: dict):
    """Append a single JSON object as one line to the log file."""
    event.setdefault("ts", time.strftime('%Y-%m-%dT%H:%M:%S'))
    with log_lock:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")


# ==========================================
# HELPER FUNCTIONS
# ==========================================
def get_node_name(node_id):
    if not radio_interface:
        return str(node_id)
    node = radio_interface.nodes.get(node_id, {})
    user = node.get('user', {})
    return user.get('longName') or user.get('shortName') or str(node_id)


def get_node_gps(node_id):
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


def safe_send(text, destinationId=None, channelIndex=0):
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
    SOS_TRIGGERS = ['!police', '!fire', '!ems', '!help', '!sos']
    for trigger in SOS_TRIGGERS:
        if msg_lower == trigger or msg_lower.startswith(trigger + ' '):
            return trigger
    return None


# ==========================================
# TRIAGE SESSION MANAGEMENT
# ==========================================
def create_session(sender_id, trigger, context, target, phone, gps_lat, gps_lon):
    """Create a new triage session for an active emergency."""
    now = time.strftime('%Y-%m-%dT%H:%M:%S')
    session = {
        "sender_id": sender_id,
        "phone": phone,
        "node_name": get_node_name(sender_id),
        "trigger": trigger,
        "context": context,
        "gps_lat": gps_lat,
        "gps_lon": gps_lon,
        "dispatched_to": target,
        "started_at": now,
        "last_activity": now,
        "exchanges": []
    }
    # Seed with initial context if provided
    if context:
        session["exchanges"].append({
            "ts": time.strftime('%H:%M:%S'),
            "role": "citizen",
            "msg": context
        })
    return session


def trim_exchanges(exchanges):
    """Keep first 2 entries (original emergency) + last 10 (recent conversation)."""
    if len(exchanges) <= TRIAGE_MAX_EXCHANGES:
        return exchanges
    return exchanges[:2] + exchanges[-(TRIAGE_MAX_EXCHANGES - 2):]


def build_triage_prompt(session):
    """Build a contextualized system prompt from the triage session object."""
    gps = f"{session['gps_lat']},{session['gps_lon']}" if session['gps_lat'] else "UNKNOWN"
    dispatched = session['dispatched_to'] or "ALL RESPONDERS"

    # Build triage log from exchanges
    triage_log = ""
    for ex in session["exchanges"]:
        role = "CITIZEN" if ex["role"] == "citizen" else "OPERATOR"
        triage_log += f"  [{ex['ts']}] {role}: {ex['msg']}\n"

    prompt = (
        "You are an Emergency Dispatch Operator on a LoRa mesh network.\n\n"
        "ACTIVE EMERGENCY:\n"
        f"  Trigger: {session['trigger']}\n"
        f"  Time: {session['started_at']}\n"
        f"  Citizen: {session['phone']} ({session['node_name']})\n"
        f"  GPS: {gps}\n"
        f"  Dispatched To: {dispatched}\n\n"
    )

    if triage_log:
        prompt += f"TRIAGE LOG:\n{triage_log}\n"

    prompt += (
        "RULES:\n"
        "- You are triaging the above emergency ONLY.\n"
        "- If the citizen goes off-topic, redirect to the active emergency.\n"
        "- Ask ONE follow-up triage question per response.\n"
        "- 2 sentences max. No markdown.\n"
    )

    return prompt


def close_session(sender_id, reason):
    """Close a triage session by reason ('safe' or 'timeout'). Returns session or None."""
    with state_lock:
        session = active_sessions.pop(sender_id, None)
    if session:
        duration = int(time.time() - time.mktime(time.strptime(session["started_at"], '%Y-%m-%dT%H:%M:%S')))
        log_event({
            "type": "sos_closed",
            "reason": reason,
            "sender": sender_id,
            "phone": session["phone"],
            "trigger": session["trigger"],
            "context": session["context"],
            "gps_lat": session["gps_lat"],
            "gps_lon": session["gps_lon"],
            "dispatched_to": session["dispatched_to"],
            "started_at": session["started_at"],
            "exchange_count": len(session["exchanges"]),
            "duration_seconds": duration
        })
        print(f"[SESSION] Closed for {session['phone']} ({reason}) — {len(session['exchanges'])} exchanges, {duration}s")
    return session


# ==========================================
# TIMEOUT WATCHDOG (Background Thread)
# ==========================================
def timeout_watchdog():
    """Periodically check for stale triage sessions and auto-close them."""
    while True:
        time.sleep(30)  # Check every 30 seconds
        now = time.time()
        stale = []

        with state_lock:
            for sender_id, session in active_sessions.items():
                last = time.mktime(time.strptime(session["last_activity"], '%Y-%m-%dT%H:%M:%S'))
                if now - last > TRIAGE_TIMEOUT:
                    stale.append(sender_id)

        for sender_id in stale:
            session = close_session(sender_id, "timeout")
            if session:
                # Notify citizen
                safe_send("[SYSTEM] Triage session timed out. Send a new !sos if you need help.",
                          destinationId=sender_id, channelIndex=CHANNEL_INDEX)
                # Notify responder(s)
                timeout_msg = (
                    f"[TIMEOUT] {session['trigger'].upper()} triage from {session['phone']} "
                    f"closed after {TRIAGE_TIMEOUT // 60}min silence. No !safe received."
                )
                target = session['dispatched_to']
                all_responders = [v for v in RESPONDERS.values() if v]
                if target:
                    safe_send(timeout_msg, destinationId=target, channelIndex=CHANNEL_INDEX)
                elif all_responders:
                    for resp_id in all_responders:
                        safe_send(timeout_msg, destinationId=resp_id, channelIndex=CHANNEL_INDEX)
                        time.sleep(2)


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
        is_triage = data.get('is_triage', False)

        try:
            if is_triage:
                # --- TRIAGE MODE ---
                with state_lock:
                    session = active_sessions.get(sender_id)
                    if not session:
                        # Session closed between queue and processing
                        message_queue.task_done()
                        continue

                    # Append citizen message to session
                    session["exchanges"].append({
                        "ts": time.strftime('%H:%M:%S'),
                        "role": "citizen",
                        "msg": message
                    })
                    session["exchanges"] = trim_exchanges(session["exchanges"])
                    session["last_activity"] = time.strftime('%Y-%m-%dT%H:%M:%S')

                    # Build contextualized prompt from session object
                    sys_prompt = build_triage_prompt(session)

                # LLM call outside lock
                messages = [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": message}
                ]

                response = client.chat.completions.create(model=MODEL, messages=messages, timeout=30.0)
                full_reply = response.choices[0].message.content.strip()

                if not full_reply:
                    full_reply = "[SYSTEM] No response generated. Repeat your last message."

                # Append operator response to session
                with state_lock:
                    if sender_id in active_sessions:
                        active_sessions[sender_id]["exchanges"].append({
                            "ts": time.strftime('%H:%M:%S'),
                            "role": "operator",
                            "msg": full_reply
                        })
                        active_sessions[sender_id]["exchanges"] = trim_exchanges(
                            active_sessions[sender_id]["exchanges"]
                        )

                # Log the exchange
                log_event({
                    "type": "triage_exchange",
                    "sender": sender_id,
                    "session_trigger": session["trigger"],
                    "citizen": message,
                    "operator": full_reply
                })

                # Stamp the !safe footer — deterministic, not LLM-generated
                full_reply = full_reply + "\n[Send !safe when emergency is resolved]"

                sender_name = get_node_name(sender_id)
                print(f"  -> TRIAGE to {sender_name}: {full_reply.splitlines()[0]}")

            else:
                # --- GENERAL MODE ---
                with state_lock:
                    if sender_id not in conversation_history:
                        conversation_history[sender_id] = []

                    conversation_history[sender_id].append({"role": "user", "content": message})
                    if len(conversation_history[sender_id]) > 4:
                        conversation_history[sender_id] = conversation_history[sender_id][-4:]

                    current_history = list(conversation_history[sender_id])

                sys_prompt = (
                    "You are The Operator. Be clinical and concise. "
                    "2 sentences max. No markdown."
                )

                messages = [{"role": "system", "content": sys_prompt}]
                messages.extend(current_history)

                response = client.chat.completions.create(model=MODEL, messages=messages, timeout=30.0)
                full_reply = response.choices[0].message.content.strip()

                if not full_reply:
                    full_reply = "[SYSTEM] No response generated. Try again."

                with state_lock:
                    conversation_history[sender_id].append({"role": "assistant", "content": full_reply})

                log_event({
                    "type": "general_exchange",
                    "sender": sender_id,
                    "citizen": message,
                    "operator": full_reply
                })

                sender_name = get_node_name(sender_id)
                print(f"  -> Operator to {sender_name}: {full_reply}")

            # --- CHUNKING & TRANSMIT (shared by both modes) ---
            chunks = textwrap.wrap(full_reply, 180)
            total = len(chunks)
            for i, chunk in enumerate(chunks):
                chunk_text = f"[{i+1}/{total}] {chunk}" if total > 1 else chunk
                safe_send(chunk_text, destinationId=sender_id, channelIndex=chan)
                time.sleep(3)

        except Exception as e:
            print(f"[ERROR] AI Worker failed: {e}")
            safe_send("[SYSTEM] Operator error. Message logged. Try again.",
                      destinationId=sender_id, channelIndex=chan)
            log_event({
                "type": "system",
                "event": "ai_worker_error",
                "sender": sender_id,
                "error": str(e)
            })

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

    if incoming_chan != CHANNEL_INDEX:
        return

    if is_my_node(sender):
        return

    phone = get_node_name(sender)
    msg_lower = message.lower().strip()

    print(f"[RX] {phone}: {message}")
    log_event({"type": "rx", "sender": sender, "phone": phone, "message": message})

    # === COMMAND: !ping ===
    if msg_lower == "!ping":
        safe_send("[SYSTEM] PONG. Signal received by The Operator.",
                  destinationId=sender, channelIndex=incoming_chan)
        log_event({"type": "command", "sender": sender, "command": "ping"})
        return

    # === COMMAND: !status ===
    if msg_lower == "!status":
        node_count = len(radio_interface.nodes) if radio_interface else 0
        resp_count = len([v for v in RESPONDERS.values() if v])
        session_count = len(active_sessions)
        status = (
            f"[SYSTEM] Operator Online | "
            f"Queue: {message_queue.qsize()} | "
            f"Nodes: {node_count} | "
            f"Responders: {resp_count} | "
            f"Active Triage: {session_count}"
        )
        safe_send(status, destinationId=sender, channelIndex=incoming_chan)
        log_event({"type": "command", "sender": sender, "command": "status"})
        return

    # === COMMAND: !safe (Cancel active triage session) ===
    if msg_lower == "!safe":
        session = close_session(sender, "safe")

        if session:
            original_trigger = session['trigger']
            target = session['dispatched_to']
            all_responders = [v for v in RESPONDERS.values() if v]

            cancel_msg = (
                f"[CANCELLED] {original_trigger.upper()} from {phone} "
                f"marked SAFE by sender. Use your judgment."
            )

            if target:
                safe_send(cancel_msg, destinationId=target, channelIndex=incoming_chan)
                print(f"[SAFE] Cancel sent to {target}")
            elif all_responders:
                for resp_id in all_responders:
                    safe_send(cancel_msg, destinationId=resp_id, channelIndex=incoming_chan)
                    time.sleep(2)
                print(f"[SAFE] Cancel broadcast to {len(all_responders)} responder(s)")
            else:
                safe_send(cancel_msg, channelIndex=incoming_chan)

            safe_send("[SYSTEM] SOS cancelled. Responders notified. Stay safe.",
                      destinationId=sender, channelIndex=incoming_chan)
            print(f"[SAFE] SOS from {phone} cancelled")

        else:
            safe_send("[SYSTEM] No active SOS to cancel.",
                      destinationId=sender, channelIndex=incoming_chan)
        return

    # === SOS TRIGGERS ===
    matched_trigger = match_trigger(msg_lower)

    if matched_trigger:
        lat, lon = get_node_gps(sender)
        gps_str = f"GPS: {lat},{lon}" if lat else "GPS: UNKNOWN"
        timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
        context = message[len(matched_trigger):].strip()

        print(f"\n{'='*50}")
        print(f"  SOS EVENT: {matched_trigger.upper()}")
        print(f"  From: {phone} ({sender})")
        print(f"  {gps_str}")
        print(f"  Time: {timestamp}")
        print(f"{'='*50}\n")

        # 1. ACK
        ack = f"[SOS] {matched_trigger.upper()} RECEIVED. {gps_str}"
        safe_send(ack, destinationId=sender, channelIndex=incoming_chan)
        time.sleep(2)

        # 2. Safety bounce-back
        safe_send("[SOS] If triggered by accident, send !safe to cancel.",
                  destinationId=sender, channelIndex=incoming_chan)
        time.sleep(2)

        # 3. Dispatch
        target = RESPONDERS.get(matched_trigger)
        all_responders = [v for v in RESPONDERS.values() if v]

        dispatch = (
            f"[DISPATCH] {matched_trigger.upper()} | "
            f"From: {phone} | {gps_str} | "
            f"Time: {time.strftime('%H:%M:%S')}"
        )
        if context:
            dispatch += f" | {context[:80]}"

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

        # 4. Create triage session
        session = create_session(
            sender_id=sender,
            trigger=matched_trigger,
            context=context,
            target=target,
            phone=phone,
            gps_lat=lat,
            gps_lon=lon
        )
        with state_lock:
            active_sessions[sender] = session

        # 5. Log dispatch
        log_event({
            "type": "sos_dispatch",
            "sender": sender,
            "phone": phone,
            "trigger": matched_trigger,
            "context": context,
            "gps_lat": lat,
            "gps_lon": lon,
            "routed_to": target or "ALL_RESPONDERS"
        })

        # 6. Queue context for first AI triage if present
        if context:
            message_queue.put({
                'sender': sender,
                'message': context,
                'channel': incoming_chan,
                'is_triage': True
            })
        return

    # === CHECK: Is sender in active triage session? ===
    with state_lock:
        in_triage = sender in active_sessions

    if in_triage:
        # All messages from this sender route through triage until !safe
        message_queue.put({
            'sender': sender,
            'message': message,
            'channel': incoming_chan,
            'is_triage': True
        })
        return

    # === GENERAL MESSAGES → AI QUEUE ===
    if message_queue.qsize() > 15:
        print(f"[BOUNCER] Queue full ({message_queue.qsize()}), dropping from {phone}")
        safe_send("[SYSTEM] Busy. Try again in 30s.",
                  destinationId=sender, channelIndex=incoming_chan)
        log_event({"type": "bouncer_drop", "sender": sender, "phone": phone, "message": message})
        return

    message_queue.put({
        'sender': sender,
        'message': message,
        'channel': incoming_chan,
        'is_triage': False
    })


# ==========================================
# MAIN EXECUTION
# ==========================================
def main():
    global radio_interface

    print("""
    ╔══════════════════════════════════════╗
    ║   THE OPERATOR V6 — LIBERTY MESH    ║
    ║   Mindtech Mesh Networks            ║
    ║   Triage Sessions + JSONL Logging   ║
    ╚══════════════════════════════════════╝
    """)

    threading.Thread(target=ai_worker, daemon=True).start()
    threading.Thread(target=timeout_watchdog, daemon=True).start()

    try:
        if DEV_PATH:
            radio_interface = meshtastic.serial_interface.SerialInterface(devPath=DEV_PATH)
        else:
            radio_interface = meshtastic.serial_interface.SerialInterface()

        pub.subscribe(on_receive, "meshtastic.receive.text")

        resp_count = len([v for v in RESPONDERS.values() if v])
        print(f"  Connected to Meshtastic Gateway")
        print(f"  Channel: {CHANNEL_INDEX}")
        print(f"  LLM: {MODEL} via Ollama")
        print(f"  Responders: {resp_count} configured")
        print(f"  Triage Timeout: {TRIAGE_TIMEOUT // 60} minutes")
        print(f"  Commands: !ping | !status | !sos | !police | !fire | !ems | !help | !safe")
        print(f"  Logs: {LOG_FILE}")
        print(f"\n  Listening...\n")

        log_event({"type": "system", "event": "startup", "model": MODEL, "responders": resp_count})

    except Exception as e:
        print(f"[ERROR] Could not connect to radio: {e}")
        return

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down The Operator...")
        # Close any active sessions
        with state_lock:
            active = list(active_sessions.keys())
        for sid in active:
            close_session(sid, "shutdown")
        if radio_interface:
            radio_interface.close()
        log_event({"type": "system", "event": "shutdown"})
        print("Operator offline. All logs preserved.")


if __name__ == "__main__":
    main()
