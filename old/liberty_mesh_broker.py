import time
import textwrap
import queue
import threading
import meshtastic
import meshtastic.serial_interface
from pubsub import pub
from openai import OpenAI

# ==========================================
# 1. THE INBOX (The Ticket Spike)
# This creates a thread-safe FIFO (First-In, First-Out) queue.
# ==========================================
message_queue = queue.Queue()

# Connect directly to your local Gemma model
client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")

# Global variable to hold the radio connection
radio_interface = None

# ==========================================
# 2. THE CONSUMER (The Slow Worker Thread)
# This runs constantly in the background, completely separate from the radio.
# ==========================================
def ai_worker():
    print("[WORKER] Thread started. Staring at the ticket spike...")
    while True:
        # get() acts as a blocker; the thread literally goes to sleep here 
        # until a message appears in the queue.
        incoming_data = message_queue.get() 
        
        sender = incoming_data['sender']
        message = incoming_data['message']
        
        print(f"\n[WORKER] Pulled ticket from {sender}. Pinging Tiny Alice...")
        
        try:
            # Ping the local AI
            response = client.chat.completions.create(
                model="gemma3:latest", 
                messages=[
                    {"role": "system", "content": "You are Alice, an AI assistant. Give a clear, helpful answer in about 3 or 4 short sentences. Do not use markdown."},
                    {"role": "user", "content": message}
                ]
            )
            
            full_reply = response.choices[0].message.content.strip()
            
            # Log the full interaction
            with open("classroom_logs.md", "a", encoding="utf-8") as file:
                file.write(f"**Student ({sender}):** {message}\n\n")
                file.write(f"**Alice:** {full_reply}\n")
                file.write("---\n")
            print("[WORKER] Interaction safely logged to classroom_logs.md")
            
            # Textwrap to protect the radio
            chunks = textwrap.wrap(full_reply, width=180)
            total_chunks = len(chunks)
            
            for index, chunk in enumerate(chunks):
                paged_text = f"[{index+1}/{total_chunks}] {chunk}"
                print(f"  -> Sending to {sender}: {paged_text}")
                
                # Transmit using the global radio interface
                if radio_interface:
                    radio_interface.sendText(text=paged_text, destinationId=sender, wantAck=True)
                
                # The crucial 10-second delay so the hardware buffer doesn't overflow
                time.sleep(10)
                
        except Exception as e:
            print(f"[WORKER] Error processing request: {e}")
            
        finally:
            # task_done() tells the queue this specific ticket is fully processed.
            # This is critical so the queue knows it's safe to move to the next one!
            message_queue.task_done() 
            print("[WORKER] Finished processing. Waiting for next ticket...")


# ==========================================
# 3. THE PRODUCER (The Fast Listener)
# This ONLY catches radio waves and drops them in the queue.
# ==========================================
# ==========================================
# 3. THE PRODUCER (The Fast Listener)
# ==========================================
def onReceive(packet, interface):
    try:
        if 'decoded' in packet and 'text' in packet['decoded']:
            message = packet['decoded']['text']
            sender = packet.get('fromId', 'Unknown')
            
            # --- THE SYSTEM OVERRIDE ---
            if message.strip().lower() == "!status":
                print(f"\n[RADIO] Status ping received from {sender}!")
                
                # Check how many tickets are on the spike
                backlog = message_queue.qsize()
                
                status_reply = f"[SYSTEM] Alice is Online. Signal strong. {backlog} questions waiting in queue."
                
                # Shoot the reply back instantly, bypassing the queue and the AI
                radio_interface.sendText(text=status_reply, destinationId=sender, wantAck=True)
                return # Stop here so the command doesn't go to the AI
            # ---------------------------
            
            print(f"\n[RADIO] Fast-caught message from {sender}!")
            # Drop regular questions into the thread-safe queue
            message_queue.put({'sender': sender, 'message': message}) 
            
    except Exception as e:
        print(f"[RADIO] Error catching packet: {e}")


# ==========================================
# --- MAIN EXECUTION ---
# ==========================================

# Spin up the AI Worker as a background "Daemon" thread.
# A Daemon thread automatically shuts itself down cleanly when you close the script.
worker_thread = threading.Thread(target=ai_worker, daemon=True)
worker_thread.start()

# Subscribe the fast listener to the radio waves
pub.subscribe(onReceive, "meshtastic.receive")

print("Connecting to L-01 on COM6...")
radio_interface = meshtastic.serial_interface.SerialInterface(devPath='COM6')

print("Liberty Mesh Traffic Cop is ONLINE! (Press Ctrl+C to stop)")

# Keep the main program alive
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("\nShutting down Liberty Mesh...")
    radio_interface.close()