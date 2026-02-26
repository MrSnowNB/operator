import time
import textwrap
import queue
import threading
import meshtastic
import meshtastic.serial_interface
from pubsub import pub
from openai import OpenAI

# ==========================================
# 1. CORE SYSTEM VARIABLES & TRACKERS
# ==========================================
message_queue = queue.Queue()
client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")
radio_interface = None

# Memory Bank: Holds the last 4 messages for each unique student ID
conversation_history = {}

# The UX Bouncer Trackers
cooldown_tracker = {}       # Tracks the timestamp of their last valid question
warning_tracker = {}        # Tracks the timestamp of their last warning message
COOLDOWN_SECONDS = 60       # The main penalty box (60 seconds between questions)
WARNING_THROTTLE = 15       # Only send a warning every 15s to prevent radio storms

# ==========================================
# 2. THE CONSUMER (AI Worker Thread)
# ==========================================
def ai_worker():
    print("[WORKER] Thread started. Monitoring the ticket spike...")
    while True:
        incoming_data = message_queue.get() 
        sender = incoming_data['sender']
        message = incoming_data['message']
        
        print(f"\n[WORKER] Ticket pulled. Routing {sender} to Alice (Gemma 3)...")
            
        try:
            # --- CONVERSATION MEMORY ---
            if sender not in conversation_history:
                conversation_history[sender] = []
                
            # Add new student input
            conversation_history[sender].append({"role": "user", "content": message})
            
            # Trim memory to the last 4 messages to protect LoRa payload limits
            if len(conversation_history[sender]) > 4:
                conversation_history[sender] = conversation_history[sender][-4:]
                
            # --- AI GENERATION ---
            messages = [{"role": "system", "content": "You are Alice. Be concise. Limit responses to 3 sentences. Do not use markdown."}]
            messages.extend(conversation_history[sender])
            
            response = client.chat.completions.create(
                model="gemma3:latest", 
                messages=messages
            )
            full_reply = response.choices[0].message.content.strip()
            
            # Save Alice's response back to memory for the next turn
            conversation_history[sender].append({"role": "assistant", "content": full_reply})
            
            # --- LOCAL LOGGING ---
            with open("classroom_logs.md", "a", encoding="utf-8") as file:
                file.write(f"**Student ({sender}):** {message}\n\n**Alice:** {full_reply}\n---\n")
            
            # --- LORA TRANSMISSION ---
            chunks = textwrap.wrap(full_reply, width=180)
            for index, chunk in enumerate(chunks):
                paged_text = f"[{index+1}/{len(chunks)}] {chunk}"
                print(f"  -> Transmitting: {paged_text}")
                
                if radio_interface:
                    radio_interface.sendText(text=paged_text, destinationId=sender, wantAck=True)
                
                # 10-second buffer delay to respect physical RF duty cycles
                time.sleep(10)
                
        except Exception as e:
            print(f"[WORKER] AI Processing Error: {e}")
            
        finally:
            message_queue.task_done() 

# ==========================================
# 3. THE PRODUCER (Fast Listener & UX Bouncer)
# ==========================================
def onReceive(packet, interface):
    try:
        if 'decoded' in packet and 'text' in packet['decoded']:
            message = packet['decoded']['text'].strip()
            sender = packet.get('fromId', 'Unknown')
            current_time = time.time()
            
            # --- 1. THE UX BOUNCER (Rate Limiting & Feedback) ---
            if sender in cooldown_tracker:
                time_since_valid = current_time - cooldown_tracker[sender]
                
                if time_since_valid < COOLDOWN_SECONDS:
                    # Calculate exactly how much time is left in the penalty box
                    time_left = int(COOLDOWN_SECONDS - time_since_valid)
                    
                    # Check if we've already warned them recently
                    time_since_warning = current_time - warning_tracker.get(sender, 0)
                    
                    if time_since_warning > WARNING_THROTTLE:
                        warning_msg = f"[SYSTEM] Alice is busy. Please wait {time_left}s."
                        print(f"\n[RADIO] Spam intercepted from {sender}. Sending UX warning: {time_left}s left.")
                        
                        if radio_interface:
                            # sendText with wantAck=False so warnings don't cause their own traffic jams
                            radio_interface.sendText(text=warning_msg, destinationId=sender, wantAck=False)
                        
                        # Log that we just warned them
                        warning_tracker[sender] = current_time
                    else:
                        print(f"\n[RADIO] Silent drop for {sender} (Warning already sent recently).")
                    
                    return # Drop the spam packet from the AI queue
            
            # --- 2. VALID MESSAGE INGESTION ---
            # If they made it here, it's a valid message. Reset their trackers.
            cooldown_tracker[sender] = current_time
            warning_tracker[sender] = 0 # Clear the warning throttle
            
            # --- 3. COMMAND ROUTING ---
            if message.lower() == "!status":
                backlog = message_queue.qsize()
                status_reply = f"[SYSTEM] Alice is Online. {backlog} questions in queue."
                print(f"\n[RADIO] Status ping routed for {sender}.")
                if radio_interface:
                    radio_interface.sendText(text=status_reply, destinationId=sender, wantAck=True)
                return 
                
            # --- 4. QUEUE INGESTION ---
            print(f"\n[RADIO] Fast-caught message from {sender}! Routing to queue.")
            message_queue.put({'sender': sender, 'message': message}) 
            
    except Exception as e:
        print(f"[RADIO] Error catching packet: {e}")

# ==========================================
# 4. MAIN EXECUTION
# ==========================================
if __name__ == "__main__":
    worker_thread = threading.Thread(target=ai_worker, daemon=True)
    worker_thread.start()

    print("Connecting to Heltec V3 Bridge on COM6...")
    try:
        radio_interface = meshtastic.serial_interface.SerialInterface(devPath='COM6')
        pub.subscribe(onReceive, "meshtastic.receive")
        print("\nLiberty Mesh v2 is ONLINE. (Press Ctrl+C to stop)")
        
        while True:
            time.sleep(1)
            
    except KeyboardInterrupt:
        print("\nShutting down Liberty Mesh...")
        if radio_interface:
            radio_interface.close()
    except Exception as e:
        print(f"Failed to start RF Network: {e}")