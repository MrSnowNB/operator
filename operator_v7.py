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
TRIAGE_TIMEOUT = 600        # 10 min silence = auto-close triage
TRIAGE_MAX_EXCHANGES = 12   # 6 back-and-forths before trimming
LOCKOUT_MINUTES = 120       # Default restriction duration
MENU_911_TIMEOUT = 120      # 2 min to reply to !911 menu before alert
LOG_FILE = "operator_logs.jsonl"


# ==========================================
# CORE STATE
# ==========================================
message_queue = queue.Queue()
conversation_history = {}   # General chat only (non-emergency)
active_sessions = {}        # Triage sessions: {sender_id: session_obj}
restricted_list = {}        # {sender_id: {phone, node_name, locked_until, locked_by}}
last_dispatch_to = {}       # {responder_id: citizen_sender_id}
pending_911 = {}            # {sender_id: {ts, gps_lat, gps_lon, channel}}
pending_cancel = {}         # {responder_id: [ordered list of restricted sender_ids]}
state_lock = threading.Lock()
log_lock = threading.Lock()
radio_interface = None

MENU_911 = (
    "[SOS] Emergency received.\n"
    "Reply with a NUMBER:\n"
    "1 = Fire\n"
    "2 = Medical\n"
    "3 = Police\n"
    "4 = Other\n"
    "5 = Accident (sent by mistake)"
)
MENU_911_MAP = {'1': '!fire', '2': '!ems', '3': '!police', '4': '!help', '5': 'false_alarm'}


# ==========================================
# JSONL LOGGING
# ==========================================
def log_event(event: dict):
    event.setdefault("ts", time.strftime('%Y-%m-%dT%H:%M:%S'))
    with log_lock:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")


# ==========================================
# HELPERS
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
        sender_num = int(sender[1:], 16) if sender.startswith("!") else int(sender)
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


def is_responder(sender):
    """Check if sender is a registered responder node."""
    all_resp = [v for v in RESPONDERS.values() if v]
    return sender in all_resp


def is_restricted(sender):
    """Check if sender is on the restricted list and lockout hasn't expired."""
    with state_lock:
        entry = restricted_list.get(sender)
        if not entry:
            return False
        if time.time() > entry['locked_until']:
            # Expired — clean up
            expired = restricted_list.pop(sender)
            log_event({
                "type": "restriction_expired",
                "sender": sender,
                "phone": expired["phone"]
            })
            return False
        return True


# ==========================================
# TRIAGE SESSION MANAGEMENT
# ==========================================
def create_session(sender_id, trigger, context, target, phone, gps_lat, gps_lon):
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
    if context:
        session["exchanges"].append({
            "ts": time.strftime('%H:%M:%S'),
            "role": "citizen",
            "msg": context
        })
    return session


def trim_exchanges(exchanges):
    if len(exchanges) <= TRIAGE_MAX_EXCHANGES:
        return exchanges
    return exchanges[:2] + exchanges[-(TRIAGE_MAX_EXCHANGES - 2):]


def build_triage_prompt(session):
    gps = f"{session['gps_lat']},{session['gps_lon']}" if session['gps_lat'] else "UNKNOWN"
    dispatched = session['dispatched_to'] or "ALL RESPONDERS"

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
    with state_lock:
        session = active_sessions.pop(sender_id, None)
    if session:
        try:
            started = time.mktime(time.strptime(session["started_at"], '%Y-%m-%dT%H:%M:%S'))
            duration = int(time.time() - started)
        except ValueError:
            duration = 0
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
        print(f"[SESSION] Closed for {session['phone']} ({reason})")
    return session


# ==========================================
# SOS DISPATCH (shared by !triggers and !911)
# ==========================================
def dispatch_sos(sender, phone, incoming_chan, trigger, context):
    """Core dispatch logic — used by both direct !flags and !911 menu."""
    lat, lon = get_node_gps(sender)
    gps_str = f"GPS: {lat},{lon}" if lat else "GPS: UNKNOWN"
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')

    print(f"\n{'='*50}")
    print(f"  SOS EVENT: {trigger.upper()}")
    print(f"  From: {phone} ({sender})")
    print(f"  {gps_str}")
    print(f"  Time: {timestamp}")
    print(f"{'='*50}\n")

    # 1. ACK
    ack = f"[SOS] {trigger.upper()} RECEIVED. {gps_str}"
    safe_send(ack, destinationId=sender, channelIndex=incoming_chan)
    time.sleep(2)

    # 2. Safety bounce-back
    safe_send("[SOS] If triggered by accident, send !safe to cancel.",
              destinationId=sender, channelIndex=incoming_chan)
    time.sleep(2)

    # 3. Dispatch
    target = RESPONDERS.get(trigger)
    all_responders = [v for v in RESPONDERS.values() if v]

    dispatch = (
        f"[DISPATCH] {trigger.upper()} | "
        f"From: {phone} | {gps_str} | "
        f"Time: {time.strftime('%H:%M:%S')}"
    )
    if context:
        dispatch += f" | {context[:80]}"

    if target:
        safe_send(dispatch, destinationId=target, channelIndex=incoming_chan)
        with state_lock:
            last_dispatch_to[target] = sender
        print(f"[DISPATCH] Routed to {target}")
    elif all_responders:
        for resp_id in all_responders:
            safe_send(dispatch, destinationId=resp_id, channelIndex=incoming_chan)
            with state_lock:
                last_dispatch_to[resp_id] = sender
            time.sleep(2)
        print(f"[DISPATCH] Broadcast to {len(all_responders)} responder(s)")
    else:
        safe_send(dispatch, channelIndex=incoming_chan)
        print("[DISPATCH] No responders configured — broadcast to channel")

    # 4. Create triage session
    session = create_session(
        sender_id=sender, trigger=trigger, context=context,
        target=target, phone=phone, gps_lat=lat, gps_lon=lon
    )
    with state_lock:
        active_sessions[sender] = session

    # 5. Log dispatch
    log_event({
        "type": "sos_dispatch",
        "sender": sender,
        "phone": phone,
        "trigger": trigger,
        "context": context,
        "gps_lat": lat,
        "gps_lon": lon,
        "routed_to": target or "ALL_RESPONDERS"
    })

    # 6. Queue context for first AI triage
    if context:
        message_queue.put({
            'sender': sender,
            'message': context,
            'channel': incoming_chan,
            'is_triage': True
        })


# ==========================================
# WATCHDOG (Background Thread)
# ==========================================
def watchdog():
    """Sweep for triage timeouts, 911 no-responses, and expired restrictions."""
    while True:
        time.sleep(30)
        now = time.time()

        # --- Triage session timeouts ---
        stale_sessions = []
        with state_lock:
            for sid, session in active_sessions.items():
                try:
                    last = time.mktime(time.strptime(session["last_activity"], '%Y-%m-%dT%H:%M:%S'))
                    if now - last > TRIAGE_TIMEOUT:
                        stale_sessions.append(sid)
                except ValueError:
                    pass

        for sid in stale_sessions:
            session = close_session(sid, "timeout")
            if session:
                safe_send("[SYSTEM] Triage session timed out. Send !911 or !help if you need assistance.",
                          destinationId=sid, channelIndex=CHANNEL_INDEX)
                timeout_msg = (
                    f"[TIMEOUT] {session['trigger'].upper()} triage from {session['phone']} "
                    f"closed after {TRIAGE_TIMEOUT // 60}min silence."
                )
                target = session['dispatched_to']
                all_resp = [v for v in RESPONDERS.values() if v]
                if target:
                    safe_send(timeout_msg, destinationId=target, channelIndex=CHANNEL_INDEX)
                elif all_resp:
                    for r in all_resp:
                        safe_send(timeout_msg, destinationId=r, channelIndex=CHANNEL_INDEX)
                        time.sleep(2)

        # --- !911 no-response alerts ---
        stale_911 = []
        with state_lock:
            for sid, p in pending_911.items():
                try:
                    started = time.mktime(time.strptime(p["ts"], '%Y-%m-%dT%H:%M:%S'))
                    if now - started > MENU_911_TIMEOUT:
                        stale_911.append(sid)
                except ValueError:
                    pass

        for sid in stale_911:
            with state_lock:
                p = pending_911.pop(sid, None)
            if p:
                phone = get_node_name(sid)
                gps_str = f"GPS: {p['gps_lat']},{p['gps_lon']}" if p['gps_lat'] else "GPS: UNKNOWN"
                alert = (
                    f"[DISPATCH] !911 NO RESPONSE | From: {phone} | {gps_str} | "
                    f"Citizen triggered 911 but did not respond. Possible incapacitation."
                )
                all_resp = [v for v in RESPONDERS.values() if v]
                for r in all_resp:
                    safe_send(alert, destinationId=r, channelIndex=CHANNEL_INDEX)
                    with state_lock:
                        last_dispatch_to[r] = sid
                    time.sleep(2)
                if not all_resp:
                    safe_send(alert, channelIndex=CHANNEL_INDEX)

                log_event({
                    "type": "sos_911_no_response",
                    "sender": sid,
                    "phone": phone,
                    "gps_lat": p["gps_lat"],
                    "gps_lon": p["gps_lon"]
                })
                print(f"[911] No response from {phone} — dispatched to all responders")

        # --- Expired restrictions ---
        expired = []
        with state_lock:
            for sid, entry in restricted_list.items():
                if now > entry['locked_until']:
                    expired.append(sid)
            for sid in expired:
                e = restricted_list.pop(sid)
                log_event({"type": "restriction_expired", "sender": sid, "phone": e["phone"]})


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
                with state_lock:
                    session = active_sessions.get(sender_id)
                    if not session:
                        message_queue.task_done()
                        continue

                    session["exchanges"].append({
                        "ts": time.strftime('%H:%M:%S'),
                        "role": "citizen",
                        "msg": message
                    })
                    session["exchanges"] = trim_exchanges(session["exchanges"])
                    session["last_activity"] = time.strftime('%Y-%m-%dT%H:%M:%S')
                    sys_prompt = build_triage_prompt(session)

                messages = [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": message}
                ]

                response = client.chat.completions.create(model=MODEL, messages=messages, timeout=30.0)
                full_reply = response.choices[0].message.content.strip()

                if not full_reply:
                    full_reply = "[SYSTEM] No response generated. Repeat your last message."

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

                log_event({
                    "type": "triage_exchange",
                    "sender": sender_id,
                    "session_trigger": session["trigger"],
                    "citizen": message,
                    "operator": full_reply
                })

                # Stamp !safe footer — deterministic, not LLM-generated
                full_reply = full_reply + "\n[Send !safe when emergency is resolved]"

                sender_name = get_node_name(sender_id)
                print(f"  -> TRIAGE to {sender_name}: {full_reply.splitlines()[0]}")

            else:
                with state_lock:
                    if sender_id not in conversation_history:
                        conversation_history[sender_id] = []
                    conversation_history[sender_id].append({"role": "user", "content": message})
                    if len(conversation_history[sender_id]) > 4:
                        conversation_history[sender_id] = conversation_history[sender_id][-4:]
                    current_history = list(conversation_history[sender_id])

                sys_prompt = "You are The Operator. Be clinical and concise. 2 sentences max. No markdown."
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

            # Chunk and transmit
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
            log_event({"type": "system", "event": "ai_worker_error", "sender": sender_id, "error": str(e)})

        message_queue.task_done()


# ==========================================
# THE ROUTER
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

    # ============================================
    # RESPONDER-ONLY COMMANDS (checked first)
    # ============================================
    if is_responder(sender):

        # === !spam — Restrict last-dispatched citizen ===
        if msg_lower == "!spam":
            with state_lock:
                target_citizen = last_dispatch_to.get(sender)

            if not target_citizen:
                safe_send("[SYSTEM] No recent dispatch found. Cannot identify target.",
                          destinationId=sender, channelIndex=incoming_chan)
                return

            target_phone = get_node_name(target_citizen)

            # Force-close any active triage session
            session = close_session(target_citizen, "restricted")
            if session:
                safe_send(f"[RESTRICTED] Triage for {target_phone} force-closed.",
                          destinationId=sender, channelIndex=incoming_chan)
                time.sleep(2)

            # Add to restricted list
            with state_lock:
                restricted_list[target_citizen] = {
                    "phone": target_phone,
                    "node_name": get_node_name(target_citizen),
                    "locked_until": time.time() + (LOCKOUT_MINUTES * 60),
                    "locked_by": sender
                }
                # Clear from pending states
                pending_911.pop(target_citizen, None)

            # Notify responder
            safe_send(f"[RESTRICTED] {target_phone} locked out for {LOCKOUT_MINUTES} min.",
                      destinationId=sender, channelIndex=incoming_chan)

            # Notify citizen
            safe_send("[SYSTEM] Your access has been temporarily restricted by a responder.",
                      destinationId=target_citizen, channelIndex=incoming_chan)

            log_event({
                "type": "restricted",
                "sender": target_citizen,
                "phone": target_phone,
                "duration_minutes": LOCKOUT_MINUTES,
                "locked_by": sender
            })
            print(f"[RESTRICTED] {target_phone} locked out by {phone} for {LOCKOUT_MINUTES} min")
            return

        # === !cancel — Show restricted list for removal ===
        if msg_lower == "!cancel":
            with state_lock:
                active_restrictions = {}
                now = time.time()
                for sid, entry in restricted_list.items():
                    remaining = int((entry['locked_until'] - now) / 60)
                    if remaining > 0:
                        active_restrictions[sid] = {
                            "phone": entry["phone"],
                            "remaining": remaining
                        }

            if not active_restrictions:
                safe_send("[SYSTEM] Restricted list is empty. No users locked out.",
                          destinationId=sender, channelIndex=incoming_chan)
                return

            # Build numbered list
            ordered = list(active_restrictions.keys())
            lines = ["[RESTRICTED LIST]"]
            for i, sid in enumerate(ordered, 1):
                info = active_restrictions[sid]
                lines.append(f"{i}. {info['phone']} — {info['remaining']} min left")
            lines.append("Reply with number to remove.")

            with state_lock:
                pending_cancel[sender] = ordered

            list_msg = "\n".join(lines)
            # Chunk the list if it's long
            chunks = textwrap.wrap(list_msg, 180)
            for chunk in chunks:
                safe_send(chunk, destinationId=sender, channelIndex=incoming_chan)
                time.sleep(2)
            return

        # === Numbered reply to !cancel list ===
        with state_lock:
            cancel_list = pending_cancel.get(sender)

        if cancel_list and msg_lower.isdigit():
            idx = int(msg_lower) - 1
            if 0 <= idx < len(cancel_list):
                target_citizen = cancel_list[idx]
                with state_lock:
                    removed = restricted_list.pop(target_citizen, None)
                    pending_cancel.pop(sender, None)

                if removed:
                    safe_send(f"[SYSTEM] {removed['phone']} removed from restricted list.",
                              destinationId=sender, channelIndex=incoming_chan)
                    safe_send(
                        "[SYSTEM] Your access has been restored. Send !911 or !help if you need assistance.",
                        destinationId=target_citizen, channelIndex=incoming_chan
                    )
                    log_event({
                        "type": "restriction_lifted",
                        "sender": target_citizen,
                        "phone": removed["phone"],
                        "lifted_by": sender
                    })
                    print(f"[RESTRICTED] {removed['phone']} removed by {phone}")
                else:
                    safe_send("[SYSTEM] User already removed or restriction expired.",
                              destinationId=sender, channelIndex=incoming_chan)
            else:
                safe_send("[SYSTEM] Invalid number. Send !cancel to see the list again.",
                          destinationId=sender, channelIndex=incoming_chan)
            return

    # ============================================
    # RESTRICTION GATE (hard block for citizens)
    # ============================================
    if is_restricted(sender):
        safe_send("[SYSTEM] Your access has been temporarily restricted by a responder.",
                  destinationId=sender, channelIndex=incoming_chan)
        return

    # ============================================
    # CITIZEN + GENERAL COMMANDS
    # ============================================

    # === !ping ===
    if msg_lower == "!ping":
        safe_send("[SYSTEM] PONG. Signal received by The Operator.",
                  destinationId=sender, channelIndex=incoming_chan)
        log_event({"type": "command", "sender": sender, "command": "ping"})
        return

    # === !status ===
    if msg_lower == "!status":
        node_count = len(radio_interface.nodes) if radio_interface else 0
        resp_count = len([v for v in RESPONDERS.values() if v])
        with state_lock:
            session_count = len(active_sessions)
            restrict_count = len(restricted_list)
        status = (
            f"[SYSTEM] Operator Online | "
            f"Queue: {message_queue.qsize()} | "
            f"Nodes: {node_count} | "
            f"Responders: {resp_count} | "
            f"Triage: {session_count} | "
            f"Restricted: {restrict_count}"
        )
        safe_send(status, destinationId=sender, channelIndex=incoming_chan)
        log_event({"type": "command", "sender": sender, "command": "status"})
        return

    # === !safe (Cancel active triage) ===
    if msg_lower == "!safe":
        session = close_session(sender, "safe")
        if session:
            target = session['dispatched_to']
            all_resp = [v for v in RESPONDERS.values() if v]
            cancel_msg = (
                f"[CANCELLED] {session['trigger'].upper()} from {phone} "
                f"marked SAFE by sender. Use your judgment."
            )
            if target:
                safe_send(cancel_msg, destinationId=target, channelIndex=incoming_chan)
            elif all_resp:
                for r in all_resp:
                    safe_send(cancel_msg, destinationId=r, channelIndex=incoming_chan)
                    time.sleep(2)
            else:
                safe_send(cancel_msg, channelIndex=incoming_chan)

            safe_send("[SYSTEM] SOS cancelled. Responders notified. Stay safe.",
                      destinationId=sender, channelIndex=incoming_chan)
            print(f"[SAFE] SOS from {phone} cancelled")
        else:
            safe_send("[SYSTEM] No active SOS to cancel.",
                      destinationId=sender, channelIndex=incoming_chan)
        return

    # === !911 (Guided menu) ===
    if msg_lower == "!911":
        lat, lon = get_node_gps(sender)
        gps_str = f"GPS: {lat},{lon}" if lat else "GPS: UNKNOWN"

        # Send GPS ACK first
        safe_send(f"[SOS] 911 RECEIVED. {gps_str}", destinationId=sender, channelIndex=incoming_chan)
        time.sleep(2)

        # Send menu
        chunks = textwrap.wrap(MENU_911, 180)
        for chunk in chunks:
            safe_send(chunk, destinationId=sender, channelIndex=incoming_chan)
            time.sleep(2)

        with state_lock:
            pending_911[sender] = {
                "ts": time.strftime('%Y-%m-%dT%H:%M:%S'),
                "gps_lat": lat,
                "gps_lon": lon,
                "channel": incoming_chan
            }

        log_event({"type": "sos_911_triggered", "sender": sender, "phone": phone, "gps_lat": lat, "gps_lon": lon})
        print(f"[911] Menu sent to {phone}")
        return

    # === Numbered reply to !911 menu ===
    with state_lock:
        p911 = pending_911.get(sender)

    if p911 and msg_lower in MENU_911_MAP:
        with state_lock:
            pending_911.pop(sender, None)

        selection = MENU_911_MAP[msg_lower]

        if selection == 'false_alarm':
            safe_send("[SYSTEM] No emergency dispatched. Stay safe.",
                      destinationId=sender, channelIndex=incoming_chan)
            log_event({"type": "sos_false_alarm", "sender": sender, "phone": phone, "method": "911_menu"})
            print(f"[911] {phone} selected: accident / false alarm")
            return

        # Dispatch using the mapped trigger
        dispatch_sos(
            sender=sender,
            phone=phone,
            incoming_chan=incoming_chan,
            trigger=selection,
            context=""
        )
        return

    # === Direct SOS triggers (!fire, !ems, !police, !help, !sos) ===
    matched_trigger = match_trigger(msg_lower)
    if matched_trigger:
        context = message[len(matched_trigger):].strip()
        dispatch_sos(
            sender=sender,
            phone=phone,
            incoming_chan=incoming_chan,
            trigger=matched_trigger,
            context=context
        )
        return

    # === Active triage session? Route all messages through triage ===
    with state_lock:
        in_triage = sender in active_sessions

    if in_triage:
        message_queue.put({
            'sender': sender,
            'message': message,
            'channel': incoming_chan,
            'is_triage': True
        })
        return

    # === General messages → AI queue ===
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
# MAIN
# ==========================================
def main():
    global radio_interface

    print("""
    ╔══════════════════════════════════════╗
    ║   THE OPERATOR V7 — LIBERTY MESH    ║
    ║   Mindtech Mesh Networks            ║
    ║   Triage + 911 Menu + Restricted    ║
    ╚══════════════════════════════════════╝
    """)

    threading.Thread(target=ai_worker, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()

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
        print(f"  Triage Timeout: {TRIAGE_TIMEOUT // 60} min")
        print(f"  Lockout Duration: {LOCKOUT_MINUTES} min")
        print(f"  Commands (citizen):    !911 | !sos | !police | !fire | !ems | !help | !safe | !ping | !status")
        print(f"  Commands (responder):  !spam | !cancel")
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
