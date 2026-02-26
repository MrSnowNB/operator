import meshtastic
import meshtastic.ble_interface
import os # Added for the hard exit

ble_address = "AC:A7:04:07:E0:29"

print(f"Connecting to {ble_address} over Bluetooth...")
interface = meshtastic.ble_interface.BLEInterface(address=ble_address)

print("Connection established! Sending payload...")
interface.sendText("System Alert: Link Established!", channelIndex=1)

print("Message sent over LoRa! Closing port.")
interface.close()

# Forcefully kill the background Bluetooth threads and exit
os._exit(0)

# This method leaves the Bluetooth threads running in the background, 
# which can cause issues if you try to run the script again without restarting your Python environment.
# The os._exit(0) call ensures that all threads are terminated and the program exits cleanly.