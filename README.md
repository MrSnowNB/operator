# 🗽 The Operator v7 — Liberty Mesh
**Emergency SOS Network for Your Community**
*By Mindtech Mesh Networks | Technical Architecture by GarageAGI LLC*

**The Operator** is an ultra-lean, 100% off-grid AI switchboard and emergency dispatch gateway built on the Meshtastic LoRa network. It provides automated, decentralized 911-style triage and routing for communities when traditional cellular and internet infrastructure fails.

*A working emergency network for the cost of ONE police radio. Hardware agnostic. Open source. Community first.*

---

## 🧠 Why The Operator?

**Props to the developers behind [GuardianBridge](https://github.com/rkolbi/GuardianBridge).** We studied their web UI, SQLite databases, and admin panels closely — incredible work for mapping and administration.

But we needed something built for the absolute edge. No web dashboards. No database schemas. No hardcoded logic trees deciding how an emergency should be handled. We needed a **Lean Machine**.

We dumped the rigid logic trees and built a **Smart AI Router**. A local LLM (Gemma 3 via Ollama) runs right on the gateway, acting as a dynamic 911 dispatcher. It parses unstructured panic messages, extracts context, and asks dynamic triage questions — all while routing instant GPS coordinates directly to first responders.

No internet. No cloud APIs. No complex databases. Just Python, LoRa, and edge AI.

---

## 🚀 How It Works

### Direct Flag Dispatch

```
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
   2. Safety bounce-back:     LLM responds with full
      "If triggered by          emergency context
       accident, send !safe"    injected into prompt:
              │                     │
   3. DISPATCH DM to           "Is anyone trapped
      Firehouse node              inside?"
              │                  [Send !safe when
   4. Triage session opens       emergency is resolved]
              │                     │
   5. LOG to .jsonl            DM back to citizen
```

### !911 Guided Menu (for users who can't type)

```
Citizen types:  "!911"
       │
  ACK: "[SOS] 911 RECEIVED. GPS: 40.29,-74.73"
       │
  Menu: "Reply with a NUMBER:
         1 = Fire
         2 = Medical
         3 = Police
         4 = Other
         5 = Accident (sent by mistake)"
       │
  Citizen replies: "2"
       │
  Maps to !ems → same dispatch flow as above
       │
  ⚠️ No reply after 2 min?
     → Broadcast to ALL responders:
       "!911 NO RESPONSE — Possible incapacitation"
```

### !safe Cancel Flow

```
Citizen sends "!safe"
       │
  Lookup active_sessions for sender
       │
  Route [CANCELLED] to SAME
  responder(s) who got original dispatch:
  "!FIRE from 609-555-0199 marked
   SAFE by sender. Use your judgment."
       │
  ACK citizen: "SOS cancelled."
       │
  Close triage session → LOG to .jsonl
```

> **"Use your judgment."** — The responder always makes the final call, not the software. A citizen could be coerced into cancelling. The audit trail preserves both the original dispatch and the cancellation.

---

## 🔒 Triage Sessions

When a citizen triggers any SOS (`!fire`, `!911`, etc.), The Operator opens a **triage session** for that sender. Every subsequent message — regardless of content — routes through the Emergency Dispatch prompt until `!safe` or a 10-minute timeout.

### Context-Locked AI

The LLM receives the **full triage session object** injected into its system prompt every turn:

```
ACTIVE EMERGENCY:
  Trigger: !ems
  Time: 2026-02-26T22:22:57
  Citizen: 609-555-0199 (Liberty-Node-02)
  GPS: 40.29,-74.73
  Dispatched To: !22334455 (EMS)

TRIAGE LOG:
  [22:23:05] CITIZEN: I cut myself
  [22:23:05] OPERATOR: Are you currently experiencing any active bleeding?
  [22:23:30] CITIZEN: Yes
  [22:23:30] OPERATOR: Assess the wound. Size, location, vital signs.

CURRENT MESSAGE: My cat is stuck in a tree

RULES:
  - You are triaging the above emergency ONLY.
  - If the citizen goes off-topic, redirect.
```

The citizen's cat question gets redirected back to the laceration. The LLM can't drift because the emergency is in the prompt, not in memory.

### Memory Management

Triage history is capped at 12 entries. When exceeded, the system keeps the **first 2** (original emergency description) and the **last 10** (recent conversation). The anchor is never lost.

### Every Triage Response Includes:

```
Elevate the affected hand. Is the bleeding slowing?
[Send !safe when emergency is resolved]
```

The `!safe` footer is **stamped by code**, not generated by the LLM. Deterministic. Every message. The citizen always knows how to exit.

---

## 🚫 Restricted List

First responders can lock out citizens who abuse the system.

### `!spam` — Responder Only

```
Responder receives bogus [DISPATCH]
Responder sends:   !spam
                     │
  Auto-targets the last citizen dispatched to THIS responder
  (no node ID typing required)
                     │
  Force-closes any active triage session
  Locks citizen out for 120 minutes
                     │
  Responder gets:  "[RESTRICTED] Liberty-Node-02 locked out for 120 min."
  Citizen gets:    "Your access has been temporarily restricted."
```

While restricted, ALL commands are blocked — no `!911`, no `!fire`, no general chat. Hard gate.

### `!cancel` — Responder Removes Restriction

```
Responder sends:   !cancel
                     │
  "[RESTRICTED LIST]
   1. Liberty-Node-02 (609-555-0199) — 87 min left
   2. Liberty-Node-14 (732-555-0312) — 22 min left
   Reply with number to remove."
                     │
  Responder sends:   1
                     │
  Citizen immediately restored + notified
```

Restrictions auto-expire after 120 minutes. The watchdog thread cleans them up.

---

## 📡 Commands

### Citizen Commands

| Command | Action | Bypasses Queue? |
|---------|--------|:---:|
| `!911` | Guided numbered menu → dispatch | ✅ |
| `!police` | Direct dispatch to Police node with GPS | ✅ |
| `!fire` | Direct dispatch to Fire node with GPS | ✅ |
| `!ems` | Direct dispatch to EMS node with GPS | ✅ |
| `!help` | Broadcast dispatch to ALL responders | ✅ |
| `!sos [context]` | Broadcast dispatch + AI triage | ✅ |
| `!safe` | Cancel active SOS / exit triage session | ✅ |
| `!ping` | Range test — Operator replies PONG | ✅ |
| `!status` | System health check | ✅ |
| *(any text)* | Routed to AI switchboard (or triage if session active) | ❌ |

### Responder Commands

| Command | Action |
|---------|--------|
| `!spam` | Restrict last-dispatched citizen for 120 min |
| `!cancel` | Show restricted list → numbered removal |

### Recommended Quick Replies (Meshtastic App)

| Slot | Message | Purpose |
|------|---------|---------|
| 1 | `!911` | Universal emergency — works for everyone |
| 2 | `!safe` | Exit triage / cancel SOS |
| 3 | `!help` | Instant broadcast to all responders |
| 4 | `!status` | Check if Operator is alive |

---

## 📋 JSONL Logging — AI-First Audit Trail

All events are logged to `operator_logs.jsonl` — one JSON object per line. Machine-readable, grep-friendly, streamable.

### Event Types

| Type | When |
|------|------|
| `rx` | Every incoming message |
| `sos_dispatch` | SOS trigger fired, GPS extracted, dispatched |
| `sos_911_triggered` | Citizen sent !911, menu shown |
| `sos_911_no_response` | No reply to !911 after 2 min — all responders alerted |
| `sos_false_alarm` | Citizen selected "5 = Accident" on !911 menu |
| `triage_exchange` | Each citizen ↔ operator turn during active triage |
| `general_exchange` | Normal AI chat (non-emergency) |
| `sos_closed` | Session ended (reason: safe / timeout / restricted / shutdown) |
| `restricted` | Citizen locked out by responder |
| `restriction_lifted` | Responder manually removed restriction |
| `restriction_expired` | Lockout auto-expired after 120 min |
| `command` | !ping, !status responses |
| `bouncer_drop` | Message dropped due to queue overload |
| `system` | Startup, shutdown, errors |

### Example Log Lines

```jsonl
{"type":"sos_dispatch","ts":"2026-02-26T22:22:57","sender":"!0408a160","phone":"609-555-0199","trigger":"!ems","context":"I cut myself","gps_lat":null,"gps_lon":null,"routed_to":"!22334455"}
{"type":"triage_exchange","ts":"2026-02-26T22:23:05","sender":"!0408a160","session_trigger":"!ems","citizen":"I cut myself","operator":"Are you currently experiencing any active bleeding?"}
{"type":"sos_closed","ts":"2026-02-26T22:26:00","reason":"safe","sender":"!0408a160","phone":"609-555-0199","trigger":"!ems","exchange_count":6,"duration_seconds":183}
{"type":"restricted","ts":"2026-02-26T23:00:00","sender":"!0408a160","phone":"609-555-0199","duration_minutes":120,"locked_by":"!aabbccdd"}
```

---

## 🛠️ Features

| Feature | Description |
|---------|-------------|
| **100% Offline** | Runs on a recycled SFF PC or Raspberry Pi with local Ollama. Unplug the ethernet — it doesn't care. |
| **!911 Guided Menu** | Numbered selection for citizens who can't type. Two taps + one digit = full dispatch. |
| **Triage Sessions** | Context-locked AI — every message routes through emergency prompt until `!safe` or timeout. |
| **Session Prompt Injection** | Full triage object (trigger, GPS, history) injected into every LLM call. No drift. |
| **Restricted List** | Responders lock out spammers with `!spam`. 120-min auto-expiry. `!cancel` to manually remove. |
| **Responder Authorization** | `!spam` and `!cancel` only work from nodes in the RESPONDERS config. Silent drop for everyone else. |
| **Auto GPS Extraction** | Pulls coordinates from Meshtastic Node DB. Citizens never type their location. |
| **Silence-After-911 Alert** | No reply to `!911` menu within 2 min = broadcast to all responders as possible incapacitation. |
| **JSONL Audit Trail** | Machine-readable event log. Every dispatch, triage exchange, restriction, and cancellation recorded. |
| **Deadlock-Free** | Separate `state_lock` and `log_lock`. No nested acquisition. |
| **Echo Cancellation** | Gateway ignores its own transmissions — hex↔int conversion. |
| **Word Boundary Matching** | `!fireplace` won't dispatch the fire department. |
| **Self-Healing** | All sends wrapped in `safe_send()`. USB drops don't crash the script. |

---

## ⚙️ Installation

### Hardware
- Any PC or Raspberry Pi running Linux
- 1× Meshtastic node (e.g., Heltec V3, Meshnology N32) connected via USB as the gateway

### Software

```bash
# Install Ollama and pull the model
curl -fsSL https://ollama.com/install.sh | sh
ollama pull gemma3:latest

# Install Python dependencies
pip install meshtastic pypubsub openai
```

### Configure Responders

Open `operator_v7.py` and update the `RESPONDERS` dictionary with actual Node IDs (`meshtastic --nodes`):

```python
RESPONDERS = {
    '!sos':    None,         # Broadcasts to all responders
    '!police': '!aabbccdd',  # Police Station node ID
    '!fire':   '!eeff0011',  # Firehouse node ID
    '!ems':    '!22334455',  # EMS node ID
    '!help':   None          # Broadcasts to all
}
```

### Run

```bash
python3 operator_v7.py
```

---

## 🗺️ Network Topology

```
  [Lawrence High School]          [Library]
   Anchor + Operator GW           Anchor
          │                          │
          ├──── Solar Repeater ──────┤
          │                          │
          ├──── Solar Repeater ──────┤
          │                          │
   [Municipal / Police]         [Firehouse Rt 206]
   Anchor + Responder            Anchor + Responder
```

- **4 Municipal Anchors** — School, Library, Municipal Complex, Firehouse
- **6 Solar Repeaters** — Volunteer rooftops bridging the anchors
- **20 Citizen Handhelds** — Distributed at community workshops
- **1 Operator Gateway** — SFF PC running `operator_v7.py`
- **31 total nodes** covering one full township

---

## 🏗️ Architecture

```
operator_v7.py
├── on_receive()           — Radio packet router
│   ├── RESPONDER BLOCK    — !spam, !cancel (authorized nodes only)
│   ├── RESTRICTION GATE   — Hard block for locked-out citizens
│   ├── !ping / !status    — System commands
│   ├── !safe              — Cancel SOS / exit triage
│   ├── !911               — Guided menu → dispatch
│   ├── !911 reply (1-5)   — Menu selection → dispatch or false alarm
│   ├── SOS triggers       — Direct dispatch + triage session
│   ├── Active triage?     — Route to triage queue
│   └── General messages   — Route to AI queue (bouncer at depth > 15)
│
├── ai_worker()            — Background thread
│   ├── Triage mode        — Session prompt injection + !safe footer
│   └── General mode       — Standard Operator prompt
│
├── dispatch_sos()         — Shared dispatch logic (flags + 911 converge here)
├── watchdog()             — Background thread (30s sweep)
│   ├── Triage timeouts    — 10 min silence → close + notify
│   ├── !911 no-response   — 2 min silence → broadcast incapacitation alert
│   └── Expired lockouts   — Auto-remove from restricted list
│
├── State
│   ├── active_sessions{}  — Triage session objects per sender
│   ├── restricted_list{}  — Locked-out citizens with expiry
│   ├── last_dispatch_to{} — Responder → citizen mapping for !spam
│   ├── pending_911{}      — Awaiting menu reply
│   ├── pending_cancel{}   — Awaiting !cancel number reply
│   └── conversation_history{} — General chat (non-emergency)
│
├── Helpers
│   ├── safe_send()        — Error-wrapped radio transmit
│   ├── match_trigger()    — Word-boundary SOS matching
│   ├── is_my_node()       — Echo cancellation
│   ├── is_responder()     — Authorization check
│   ├── is_restricted()    — Lockout check with auto-expiry
│   ├── get_node_gps()     — GPS from Meshtastic node DB
│   ├── get_node_name()    — Phone/name lookup
│   └── log_event()        — JSONL append
│
└── Triage Engine
    ├── create_session()       — Initialize session object
    ├── build_triage_prompt()  — Inject session into system prompt
    ├── trim_exchanges()       — First 2 + last 10 memory cap
    └── close_session()        — Flush to JSONL + cleanup
```

---

## 🔄 Changelog

### v7 — !911 Menu + Restricted List + Responder Controls
- **`!911` guided menu** — Numbered selection for low-literacy users. No reply = incapacitation alert.
- **`!spam` (responder only)** — Auto-targets last-dispatched citizen. 120-min lockout. Force-closes triage.
- **`!cancel` (responder only)** — Numbered restricted list for manual removal.
- **`is_responder()` authorization** — Silent drop for unauthorized `!spam`/`!cancel` attempts.
- **`restricted_list{}`** — Hard gate in router. All commands blocked while restricted.
- **`last_dispatch_to{}`** — Tracks responder→citizen mapping for `!spam` targeting.
- **`pending_911{}`** — Awaits menu reply with 2-min timeout watchdog.
- **`dispatch_sos()`** — Extracted shared function. !flags and !911 converge.

### v6 — Triage Sessions + JSONL Logging
- **Triage session objects** — Full incident state injected into every LLM prompt.
- **Context-locked routing** — All messages route through triage until `!safe` or timeout.
- **JSONL logging** — Machine-readable event stream replacing markdown.
- **`!safe` footer** — Code-stamped on every triage response.
- **Timeout watchdog** — Auto-closes stale sessions after 10 min.

### v5 — !safe Cancel System
- **`!safe` command** — Citizens cancel accidental SOS. Responders receive `[CANCELLED]`.
- **Safety bounce-back** — Second message after SOS ACK: "Send !safe to cancel."

### v4 — SOS Dispatch + AI Triage
- **SOS triggers** with instant GPS dispatch.
- **Dynamic AI prompting** — Emergency vs general mode.
- **Deadlock fix** — Separated state and log locks.
- **Echo cancellation** — `is_my_node()` with hex↔int.
- **Word boundary matching** — `match_trigger()`.

### v2 — AI Switchboard
- Multi-threaded queue with `ai_worker`.
- Dynamic Bouncer. Conversation memory. Chunked LoRa TX.
- Beacon range test. Markdown logging.

---

## 🗓️ Roadmap

| Phase | Timeline | Milestone |
|-------|----------|-----------|
| **Layer 1** | Spring 2026 | Deploy 31-node mesh in Lawrence Township, NJ |
| **Community** | Summer 2026 | Public workshops at the library — citizens build their own nodes |
| **Municipal** | Fall 2026 | Demo to township officials, grant applications |
| **Freedom Core** | 2027 | Transition to 100% American-allied hardware (Semtech + Nordic + US assembly) |

---

## 📄 License

**CC BY-SA 4.0 v1.0** — [Mindtech Mesh Networks](https://mindtechmesh.org)

Technical architecture by [GarageAGI LLC](https://github.com/MrSnowNB).

Powered by [Meshtastic](https://meshtastic.org) · [Semtech SX1262](https://www.semtech.com) · [Ollama](https://ollama.com)
