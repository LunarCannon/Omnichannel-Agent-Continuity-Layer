from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .record import file_lock, migrate_state
from .synthesize import atomic_write_json, load_state


def identity_key(surface: str, channel_id: str, sender: str) -> str:
    validate_identity_part("surface", surface)
    validate_identity_part("channel_id", channel_id)
    validate_identity_part("sender", sender)
    return f"{surface}:{channel_id}:{sender}"


def set_identity_alias(
    *,
    store: Path,
    surface: str,
    channel_id: str,
    sender: str,
    canonical_user_id: str,
    force: bool = False,
) -> str:
    validate_identity_part("canonical_user_id", canonical_user_id)
    store.mkdir(parents=True, exist_ok=True)
    state_path = store / "state.json"
    key = identity_key(surface, channel_id, sender)
    with file_lock(store / ".lock"):
        state = migrate_state(load_state(state_path))
        aliases = state.setdefault("identity_aliases", {})
        existing = aliases.get(key)
        if existing and existing != canonical_user_id and not force:
            raise ValueError(
                f"Identity alias {key!r} already maps to {existing!r}; pass --force to remap."
            )
        aliases[key] = canonical_user_id
        atomic_write_json(state_path, state)
    return key


def resolve_identity_alias(*, state: dict[str, Any], surface: str, channel_id: str, sender: str) -> str:
    aliases = state.get("identity_aliases", {})
    if not isinstance(aliases, dict):
        return ""
    return str(aliases.get(identity_key(surface, channel_id, sender), ""))


def resolve_identity_from_store(*, store: Path, surface: str, channel_id: str, sender: str) -> str:
    state_path = store / "state.json"
    if not state_path.exists():
        return ""
    return resolve_identity_alias(
        state=load_state(state_path),
        surface=surface,
        channel_id=channel_id,
        sender=sender,
    )


def list_identity_aliases(*, store: Path) -> dict[str, str]:
    state_path = store / "state.json"
    if not state_path.exists():
        return {}
    aliases = load_state(state_path).get("identity_aliases", {})
    if not isinstance(aliases, dict):
        return {}
    return {str(key): str(value) for key, value in sorted(aliases.items())}


def identity_aliases_json(*, store: Path) -> str:
    return json.dumps(list_identity_aliases(store=store), indent=2, sort_keys=True) + "\n"


def validate_identity_part(name: str, value: str) -> None:
    if value == "":
        raise ValueError(f"Identity {name} cannot be empty")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"Identity {name} cannot contain control characters")
    if ":" in value:
        raise ValueError(f"Identity {name} cannot contain ':'")
