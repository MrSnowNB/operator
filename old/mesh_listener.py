import time
import textwrap
import meshtastic
import meshtastic.serial_interface
from pubsub import pub
from openai import OpenAI

client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")

def onReceive(packet, interface):
    try:
        if 'decoded' in packet and 'text' in packet['decoded']:
            message = packet['decoded']['text']
            sender = packet.get('fromId', 'Unknown')
            
            print(f"\nIncoming from {sender}: {message}")
            
            # 1. LET THE AI BE NATURAL
            # We just ask for brief sentences, no weird delimiters needed.
            response = client.chat.completions.create(
                model="gemma3:latest", 
                messages=[
                    {"role": "system", "content": "You are Alice, an AI assistant. Give a clear, helpful answer in about 3 or 4 short sentences. Do not use markdown."},
                    {"role": "user", "content": message}
                ]
            )
            
            full_reply = response.choices[0].message.content.strip()
            
            # 2. LOG THE FULL INTERACTION TO MARKDOWN
            with open("classroom_logs.md", "a", encoding="utf-8") as file:
                file.write(f"**Student ({sender}):** {message}\n\n")
                file.write(f"**Alice:** {full_reply}\n")
                file.write("---\n")
            print("Interaction safely logged to classroom_logs.md")
            
            # 3. THE TEXTWRAP SAFETY NET
            # This cleanly breaks the text into safe 180-character chunks
            # without ever slicing a word in half.
            chunks = textwrap.wrap(full_reply, width=180) 
            total_chunks = len(chunks)
            
            print(f"Divided cleanly into {total_chunks} chunks. Transmitting...")
            
            for index, chunk in enumerate(chunks):
                # Add the paging header dynamically (e.g., "[1/3] ")
                paged_text = f"[{index+1}/{total_chunks}] {chunk}"
                
                print(f"  -> Sending: {paged_text}")
                interface.sendText(text=paged_text, destinationId=sender, wantAck=True) 
                
                # Give the Heltec buffer 10 seconds to clear the airwaves
                time.sleep(10)
            
            print("Transmission sequence complete!")
            
    except Exception as e:
        print(f"Error processing packet: {e}")

pub.subscribe(onReceive, "meshtastic.receive")

print("Connecting to L-01 on COM6...")
interface = meshtastic.serial_interface.SerialInterface(devPath='COM6')

print("Alice is online with Python Textwrap! (Press Ctrl+C to stop)")

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    interface.close()