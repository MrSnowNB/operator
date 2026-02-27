# The Operator: Project Evolution

This project has evolved significantly from a simple AI classroom assistant into a robust, asynchronous emergency dispatch and AI switchboard system for Meshtastic networks. Here is the chronological history of the project based on its iterations.

## Phase 1: The Basic Listener
* **`mesh_listener.py`** & **`send_mesh_BT.py`**
The initial proof-of-concept. The system listened to Meshtastic packets over serial, queried a local Ollama model (acting as an AI assistant named "Alice"), and broke the responses into 180-character chunks using `textwrap` to safely transmit them over the radio. However, the radio listener was blocked while waiting for the AI response, leading to dropped packets. `send_mesh_BT.py` was an early test of BLE connections that was later abandoned for serial stability.

## Phase 2: Asynchronous Architecture
* **`liberty_mesh_broker.py`**
To solve the blocking issue, a robust asynchronous architecture was introduced. The system was split into a "Producer" (a fast listener that catches radio waves) and a "Consumer" (a background `ai_worker` thread). They communicated via a thread-safe `queue.Queue()`. This allowed the radio to continue receiving while the AI was "thinking". System commands like `!status` were also introduced.

## Phase 3: The "UX Bouncer" and Context
* **`liberty_mesh_v2.py`** & **`liberty_mesh_v3.py`**
These versions introduced conversation memory, allowing the AI to remember the last 4 messages per user. To protect the radio mesh from spam, the "UX Bouncer" was implemented—a rate-limiting system with a cooldown timer and warning throttle. `v3` heavily refined the AI's persona into a "patient elementary-school teacher" for classroom environments and added commands like `!help` and `!students`.

## Phase 4: Rebranding to "The Operator"
* **`operator_v1.py`** & **`operator_v2.py`**
The project shifted focus from a classroom setting to a more clinical, utility-focused AI named "The Operator". 
- **Thread Safety:** Implemented `threading.Lock()` for safe state management.
- **Range Testing:** Added a `beacon_worker` that allows users to type `!ping` to initiate continuous automated direct-message range tests.
- **Dynamic Bouncer:** The rate-limiting bouncer was made dynamic, only enforcing cooldowns if the AI queue is actually backed up. Ghost packets and radio errors were properly handled.

## Phase 5: SOS Dispatch Engine (Current)
* **`operator_v3.py`**
The most massive evolution of the system. "The Operator" is now a **Liberty Mesh SOS Dispatch + AI Switchboard**.
- **SOS Interception:** The system monitors for emergency flags (e.g., `SOSP`, `SOSF`, `SOSM`, `SOS`).
- **GuardianBridge Integration:** When an SOS is received, the script acts like a dispatcher. It requests the sender's GPS coordinates and forwards a formatted alert to a configured list of responder nodes.
- **Incident Management:** Responders can reply with `ACK` or `RESPONDING <incident#>`. The system tracks active incidents, their status, and logs everything to a structured JSON file (`operator_logs.jsonl`).
- **Welfare Watchdog:** A background thread automatically escalates incidents if ignored, and conducts periodic welfare checks on the SOS sender.
- **AI Fallback:** If a message is not an SOS or system command, it is routed to the Ollama AI switchboard.