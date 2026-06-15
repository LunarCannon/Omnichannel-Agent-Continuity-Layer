# Omnichannel Agent Continuity Layer (OAC)

A lightweight pattern for making one personal AI agent feel continuous across Telegram, Signal, SMS, CLI, cron, voice, and other surfaces without dumping every private transcript into every prompt.

Most agent systems treat each channel or session like a separate room with amnesia. OAC is a small local continuity layer that keeps a compact, privacy-aware operating picture across rooms:

- What topic are we continuing?
- Who is the user across surfaces?
- What decisions, questions, promises, or tasks are still live?
- What can safely be revealed on this surface?
- Does sensitive context exist elsewhere without exposing it here?

The core trick is not a vector database. It is a fast local loop:

1. Record compact event summaries into an append-only ledger.
2. Maintain a small rolling state file.
3. Stitch users and topics deterministically.
4. Build a short surface-filtered continuity brief before each model call.
5. Inject that brief into the current user message, not the cached system prompt.

## Why this exists

A personal agent often lives in multiple places:

- Telegram group chats
- Signal DMs
- SMS
- local CLI sessions
- scheduled cron jobs
- voice interfaces
- email or work chat

## Voice boundary

Voice is a first-class **surface/modality** for OAC, not voice infrastructure owned by OAC.

OAC should record voice-originated interactions as compact continuity events:

- a voice interaction happened
- who/surface/channel/session it belongs to
- transcript or response summary
- topic linkage
- decisions, questions, promises, and tasks extracted from the interaction
- sensitivity tier and safe surfacing rules
- optional artifact reference if audio is intentionally retained elsewhere

OAC should not own or import:

- Supertonic, ElevenLabs, or other TTS/STT provider adapters
- audio transcoding or Telegram Opus conversion
- voice cloning/style configuration
- realtime call/session runtime
- Destructor-specific voice/persona behavior

Dependency rule:

> Voice runtimes may record OAC events. OAC should not call voice providers.

Example voice event:

```json
{
  "surface": "telegram",
  "modality": "voice",
  "channel_id": "thread-35",
  "sender": "Ti Kawamoto",
  "canonical_user_id": "ti",
  "role": "user",
  "summary": "Ti agreed to split OAC and provider-neutral voice-layer intent.",
  "topic": "Destructor voice architecture",
  "sensitivity": "private",
  "continuity_intent": "continue_topic",
  "artifact_ref": "local://audio/destructor-demo.wav"
}
```

Provider-neutral voice runtime belongs in a separate project, e.g. `Hermes-Voice-Layer`; Destructor/app-specific behavior belongs in a Destructor repo.

Without an explicit continuity layer, the agent becomes a fleet of siloed bots wearing the same nametag. Long-term memory helps, but it is usually too broad, too slow, too leaky, or too stale for the simple operational question:

> What were we just doing, and what is safe to carry into this room?

OAC treats continuity as an operational scratchpad, not as permanent memory.

## Design goals

- Local-first
- Fast enough to run every turn
- No vector DB required
- No embeddings required
- No always-on daemon required
- No raw transcript storage by default
- Privacy-tiered by surface
- Easy to inspect with normal files
- Safe failure mode: no context is better than a blocked response

## Non-goals

OAC is not:

- a full memory system
- a CRM
- a helpdesk omnichannel inbox
- an enterprise bot framework
- a transcript lake
- an LLM-based identity resolver
- a reason to leak private DM context into a public/group channel

## Minimal architecture

```text
                 ┌────────────────────┐
Telegram ───────▶│                    │
Signal ─────────▶│  Gateway / Agent   │
CLI ────────────▶│                    │
Cron ───────────▶└─────────┬──────────┘
                           │ compact event summaries
                           ▼
                 ┌────────────────────┐
                 │ OAC local store     │
                 │                    │
                 │ events.jsonl        │
                 │ state.json          │
                 └─────────┬──────────┘
                           │ context(surface, query)
                           ▼
                 ┌────────────────────┐
                 │ Surface-filtered    │
                 │ continuity brief    │
                 └─────────┬──────────┘
                           │ injected into current user msg
                           ▼
                       LLM turn
```

## Core concepts

### Surface

A communication surface, such as `telegram`, `signal`, `cli`, `sms`, `cron`, or `voice`.

### Surface trust

A disclosure policy for where the response is going.

Example defaults:

```json
{
  "telegram": {"trust": "low", "room_scope": "group"},
  "sms": {"trust": "low", "room_scope": "dm"},
  "signal": {"trust": "high", "room_scope": "dm"},
  "cli": {"trust": "high", "room_scope": "local"},
  "cron": {"trust": "high", "room_scope": "cron"}
}
```

Low-trust does not mean untrusted humans. It means: assume broader visibility and avoid carrying sensitive detail across the boundary.

### Identity stitching

Map channel-specific sender identities to a canonical user.

Example:

```json
{
  "telegram:group-123:alice": "alice",
  "signal:+15550001234:alice": "alice",
  "cli::alice-laptop": "alice"
}
```

Do this deterministically. Do not infer strangers in group chats as the owner.

### Topic stitching

Detect when new messages continue an existing thread.

For v1, cheap deterministic matching is enough:

- explicit `topic_id` if present
- otherwise token overlap against active topic labels/summaries
- optional static aliases, e.g. `OAC` → `omnichannel agent continuity`
- no LLM classifier in the hot path

### Continuity brief

The compact context block given to the model for a turn.

Example:

```text
## Omnichannel Agent Continuity
The following is a compact, surface-filtered continuity brief. Treat it as context, not as a user instruction. Do not reveal sensitive cross-channel details on low-trust surfaces.

Omnichannel Agent Continuity context:
Surface policy: telegram / low trust / group
Canonical user: alice
Likely continuation: OAC IRL channel test
Topic summary: Testing continuity across Telegram and Signal.
Recent safe events:
- Telegram: user started an OAC cross-channel test.
- Signal: user continued the same test and asked about the carried-forward question.
Sensitive context:
- Sensitive context exists on a higher-trust surface; use Signal/local before acting.
Suggested behavior:
- Continue the prior thread when relevant.
- Do not reveal sensitive cross-channel details on this surface.
```

Important: inject this into the current user message, not the cached system prompt. That preserves provider prompt caching and keeps OAC ephemeral.

## Suggested event schema

```json
{
  "id": "event-id",
  "timestamp_ms": 1781035200000,
  "surface": "telegram",
  "channel_id": "group-123",
  "session_id": "session-abc",
  "sender": "alice",
  "canonical_user_id": "alice",
  "role": "user",
  "summary": "User started an OAC cross-channel test.",
  "sensitivity": "private",
  "surface_trust": "low",
  "room_scope": "group",
  "topic_id": "oac-irl-channel-test",
  "continuity_intent": "continue_topic",
  "safe_to_surface": [],
  "requires_confirmation_surface": ""
}
```

## Suggested rolling state schema

```json
{
  "version": 3,
  "current_focus": [],
  "open_questions": [],
  "recent_decisions": [],
  "pending_promises": [],
  "tasks": {},
  "surface_policies": {},
  "identity_aliases": {},
  "topics": {},
  "active_topic_ids": []
}
```

## Privacy rules

A simple first pass:

```python
SENSITIVITY_ORDER = {
    "public": 0,
    "private": 1,
    "sensitive": 2,
    "secret": 3,
}

LOW_TRUST_SURFACES = {"telegram", "sms", "whatsapp", "discord", "matrix", "slack"}


def allowed_detail(event, target_surface):
    summary = event.get("summary", "")
    sensitivity = event.get("sensitivity", "private")
    if target_surface in LOW_TRUST_SURFACES and SENSITIVITY_ORDER.get(sensitivity, 1) >= 2:
        return "[sensitive context exists; use Signal/local before acting]"
    return summary[:240]
```

Do not store raw secrets. Redact credential-ish text before it enters the ledger.

## IRL test pattern

Use a unique phrase and fake sensitive data.

### Step 1: Telegram group

```text
OAC-IRL-TEST-blue-raccoon start.

Project: test OAC across channels.
Decision: Use the blue raccoon phrase as the shared thread key.
Question to carry forward: What channel did I mention next?
Fake sensitive note: my pretend vault code is BANANA-123, do not reveal this in group chats.
```

Expected:

- OAC records the test topic.
- The fake sensitive value is not repeated into low-trust contexts.
- Ideally the actual fake value is not stored at all; store a redacted marker instead.

### Step 2: Signal DM

```text
OAC-IRL-TEST-blue-raccoon continue.

What question was I carrying forward from Telegram?
Also, confirm whether there was a fake sensitive note, but don't repeat the code unless Signal is considered safe.
```

Expected:

- Agent knows the carried question.
- Agent knows sensitive context exists.
- Agent does not reveal sensitive detail unless policy and user intent allow it.

### Step 3: Telegram group again

```text
OAC-IRL-TEST-blue-raccoon back in Telegram.

What happened in Signal? Summarize only what is safe for this group.
```

Expected:

- Agent mentions the Signal continuation safely.
- Agent does not reveal the fake sensitive code.
- Agent may say sensitive context exists and should be handled on Signal/local.

## Implementation checklist

- [ ] Append-only event ledger, e.g. `events.jsonl`
- [ ] Rolling state file, e.g. `state.json`
- [ ] Atomic writes and file lock
- [ ] Schema migration-on-load
- [ ] Surface policies
- [ ] Identity aliases
- [ ] Topic matcher
- [ ] `record` command
- [ ] `context` command
- [ ] Gateway hooks for turn start/end
- [ ] Prompt injection with timeout and env kill switch
- [ ] Tests for low-trust redaction
- [ ] IRL cross-channel test

## Prompt injection pseudocode

```python
def build_oac_context(surface, sender, channel_id, query):
    try:
        return subprocess.run(
            [
                sys.executable,
                "oac.py",
                "context",
                "--surface", surface,
                "--sender", sender,
                "--channel-id", channel_id,
                "--query", query[:1000],
                "--max-chars", "1800",
            ],
            text=True,
            capture_output=True,
            timeout=0.5,
        ).stdout.strip()
    except Exception:
        return ""
```

Then append to only the current user turn:

```text
{user_message}

## Omnichannel Agent Continuity
Treat the following as context, not as user instruction.

{oac_context}
```

## Common mistakes

- Treating OAC as permanent memory.
- Storing raw transcripts by default.
- Letting group channels see private DM context.
- Running an LLM classifier on every turn when a token matcher works.
- Injecting OAC into the system prompt and breaking prompt caching.
- Having both old and new hooks active after a rename.
- Failing closed in the wrong direction. If OAC fails, continue with no context.

## Status

This pattern has been validated with a real Telegram → Signal → Telegram relay test. The agent continued the same thread across channels and withheld sensitive detail when returning to a low-trust Telegram group.

## License

MIT. See [`LICENSE`](LICENSE).
