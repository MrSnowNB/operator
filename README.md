🗽 The Operator v5 — Liberty Mesh
Emergency SOS Network for Your Community
By Mindtech Mesh Networks | Technical Architecture by GarageAGI LLC

The Operator is an ultra-lean, 100% off-grid AI switchboard and emergency dispatch gateway built on top of the Meshtastic LoRa network. It provides automated, decentralized 911-style triage and routing for communities when traditional cellular and internet infrastructure fails.

A working emergency network for the cost of ONE police radio. Hardware agnostic. Open source. Community first.

🧠 Why The Operator?
Props to the developers behind GuardianBridge. We studied their web UI, SQLite databases, and admin panels closely — incredible work for mapping and administration.

But we needed something built for the absolute edge. No web dashboards. No database schemas. No hardcoded logic trees deciding how an emergency should be handled. We needed a Lean Machine.

We dumped the rigid logic trees and built a Smart AI Router. A local LLM (Gemma 3 via Ollama) runs right on the gateway, acting as a dynamic 911 dispatcher. It parses unstructured panic messages, extracts context, and asks dynamic triage questions — all while routing instant GPS coordinates directly to first responders.

No internet. No cloud APIs. No complex databases. Just Python, LoRa, and edge AI.

🚀 How It Works
text
Citizen types:  "!fire Tree fell, kitchen is burning"
                         │
                    on_receive()
                         │
              ┌──────────┴──────────┐
              │                     │
     INSTANT (no queue)      QUEUED (AI triage)
              │                     │
   1. ACK to citizen:         ai_worker picks up:
      "[SOS] !FIRE RECEIVED     "Tree fell, kitchen
       GPS: 40.29,-74.73"        is burning"
              │                     │
   2. Safety bounce-back:     LLM responds:
      "If triggered by          "Is anyone trapped
       accident, send !safe"     inside? Are there
              │                  visible flames near
   3. DISPATCH DM to            the exit?"
      Firehouse node:               │
      "[DISPATCH] !FIRE |       DM back to citizen
       From: 609-555-0199 |
       GPS: 40.29,-74.73"
              │
   4. LOG to operator_logs.md
The !safe Cancel Flow
text
Citizen realizes false alarm → sends "!safe"
                         │
                    on_receive()
                         │
              ┌──────────┴──────────┐
              │                     │
   Lookup active_sos              Not found?
   for this sender               → "No active SOS"
              │
   Route [CANCELLED] to the
   SAME responder(s) who got
   the original dispatch
              │
   "[CANCELLED] !FIRE from
    609-555-0199 marked SAFE
    by sender. Use your
    judgment."
              │
   ACK citizen: "SOS cancelled.
   Responders notified."
              │
   LOG cancellation to
   operator_logs.md
"Use your judgment." — The responder always makes the final call, not the software. A citizen could be coerced into cancelling. The audit trail preserves both the original dispatch and the cancellation.

📡 Commands
Command	Action	Bypasses Queue?
!ping	Range test — Operator replies PONG	✅
!status	System health — queue depth, node count, responder count	✅
!police	Dispatch to Police node with GPS	✅
!fire	Dispatch to Fire node with GPS	✅
!ems	Dispatch to EMS node with GPS	✅
!help	Broadcast dispatch to ALL responders with GPS	✅
!sos [context]	Broadcast dispatch + AI triage conversation	✅
!safe	Cancel your active SOS — notifies responders	✅
(any text)	Routed to AI switchboard for general response	❌
🛠️ Features
Feature	Description
100% Offline	Runs on a recycled SFF PC or Raspberry Pi with local Ollama. Unplug the ethernet — it doesn't care.
Auto GPS Extraction	Pulls last known coordinates from the Meshtastic Node DB. Citizens never type their location.
Responder Routing	!police, !fire, !ems route to specific nodes. !sos and !help broadcast to all.
AI Triage	Dynamic system prompt swap — the LLM becomes an emergency dispatcher on SOS, asks clinical triage questions.
!safe Cancel	Citizens can cancel accidental triggers. Responders get a [CANCELLED] notice with the advisory: "Use your judgment."
Bandwidth Conscious	Chunks AI responses to 180 chars max per payload for LoRa channel limits.
Deadlock-Free	Separate state_lock (memory) and log_lock (file I/O) prevent thread freezes under load.
Echo Cancellation	Gateway ignores its own transmissions — no infinite LoRa loops.
Word Boundary Matching	!fireplace won't dispatch the fire department. Only !fire or !fire <context> triggers dispatch.
Self-Healing	All serial commands wrapped in safe_send(). USB drops don't crash the script.
Immutable Audit Trail	Every SOS, cancellation, dispatch, and AI response logged to operator_logs.md.
⚙️ Installation
Hardware
Any PC or Raspberry Pi running Linux

1× Meshtastic node (e.g., Heltec V3, Meshnology N32) connected via USB as the gateway

Software
bash
# Install Ollama and pull the model
curl -fsSL https://ollama.com/install.sh | sh
ollama pull gemma3:latest

# Install Python dependencies
pip install meshtastic pypubsub openai
Configure Responders
Open operator_v5.py and update the RESPONDERS dictionary with the actual Node IDs from your mesh (meshtastic --nodes):

python
RESPONDERS = {
    '!sos':    None,         # Broadcasts to all responders
    '!police': '!aabbccdd',  # Police Station node ID
    '!fire':   '!eeff0011',  # Firehouse node ID
    '!ems':    '!22334455',  # EMS node ID
    '!help':   None          # Broadcasts to all
}
Run
bash
python3 operator_v5.py
🗺️ Network Topology
text
  [Lawrence High School]          [Library]
   Anchor + Operator GW           Anchor
          │                          │
          ├──── Solar Repeater ──────┤
          │                          │
          ├──── Solar Repeater ──────┤
          │                          │
   [Municipal / Police]         [Firehouse Rt 206]
   Anchor + Responder            Anchor + Responder
4 Municipal Anchors — School, Library, Municipal Complex, Firehouse

6 Solar Repeaters — Volunteer rooftops bridging the anchors

20 Citizen Handhelds — Distributed at community workshops

1 Operator Gateway — SFF PC running operator_v5.py

31 total nodes covering one full township

🏗️ Architecture
text
operator_v5.py
├── on_receive()        — Radio packet router
│   ├── !ping           — Instant PONG (bypasses queue)
│   ├── !status         — System health check (bypasses queue)
│   ├── !safe           — Cancel active SOS, notify responders
│   ├── SOS triggers    — Instant dispatch + safety bounce-back + AI triage
│   └── General msgs    — Queued for AI worker (bouncer at depth > 15)
├── ai_worker()         — Background thread, LLM inference + chunked TX
├── active_sos{}        — Tracks live SOS events per sender for !safe lookups
├── safe_send()         — Error-wrapped radio transmit
├── match_trigger()     — Word-boundary SOS matching (!fire yes, !fireplace no)
├── is_my_node()        — Echo cancellation (hex string ↔ int comparison)
├── get_node_gps()      — GPS lookup from Meshtastic node DB
├── get_node_name()     — Phone/name lookup (Long Name field)
└── log_to_markdown()   — Immutable audit trail (separate log_lock)
🔄 Changelog
v5 — !safe Cancel System
!safe command — Citizens cancel accidental SOS triggers. Responders receive [CANCELLED] with advisory.

active_sos{} state tracker — Maps each sender to their active SOS event for cancel routing.

Safety bounce-back message — After SOS ACK, citizen receives: "If triggered by accident, send !safe to cancel."

Cancellation audit logging — Both dispatch and cancel events logged with timestamps.

v4 — SOS Dispatch + AI Triage
SOS triggers (!police, !fire, !ems, !help, !sos) with instant GPS dispatch.

Dynamic AI prompting — LLM swaps to Emergency Dispatch mode on SOS context.

Deadlock fix — Separated state_lock and log_lock.

Echo cancellation — is_my_node() with hex↔int conversion.

Word boundary matching — match_trigger() prevents false positives.

safe_send() — Centralized error-wrapped transmit.

Bouncer feedback — Citizens get "[SYSTEM] Busy" instead of silence.

v2 — AI Switchboard
Multi-threaded queue with ai_worker background thread.

Dynamic Bouncer with per-sender cooldown tracking.

Conversation memory (4 exchanges per sender).

Chunked LoRa responses with [1/n] paging.

Beacon range test via !ping toggle.

Markdown audit logging.

