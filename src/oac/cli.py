from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .context import build_context_brief
from .identity import identity_aliases_json, resolve_identity_from_store, set_identity_alias
from .record import parse_fact, record_event
from .synthesize import atomic_write_json, load_jsonl, load_state, synthesize_digest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oac", description="Omnichannel Agent Continuity local utilities")
    subcommands = parser.add_subparsers(dest="command", required=True)

    synthesize = subcommands.add_parser(
        "synthesize",
        help="Build a deterministic local continuity digest artifact from JSONL events and state.",
    )
    synthesize.add_argument("--events", required=True, type=Path, help="Path to append-only events.jsonl")
    synthesize.add_argument("--state", type=Path, help="Path to rolling state.json")
    synthesize.add_argument("--out", required=True, type=Path, help="Path to write digest artifact JSON")
    synthesize.add_argument("--surface", required=True, help="Target surface receiving the digest")
    synthesize.add_argument("--canonical-user-id", required=True, help="Canonical user id to collect events for")
    synthesize.add_argument("--query", default="", help="Current user query/message for topic matching")
    synthesize.add_argument("--as-of-ms", type=int, default=None, help="Deterministic generation timestamp in epoch ms")
    synthesize.add_argument("--max-events", type=int, default=12, help="Maximum recent events to render")
    synthesize.set_defaults(func=run_synthesize)

    record = subcommands.add_parser(
        "record",
        help="Record one compact continuity event into a local append-only store.",
    )
    record.add_argument("--store", required=True, type=Path, help="Directory containing events.jsonl and state.json")
    record.add_argument("--surface", required=True, help="Source surface, e.g. telegram or signal")
    record.add_argument("--channel-id", required=True, help="Surface channel/thread/session identifier")
    record.add_argument("--sender", required=True, help="Surface-specific sender display name or id")
    record.add_argument("--canonical-user-id", required=True, help="Canonical stitched user id")
    record.add_argument("--role", required=True, help="Event role: user, assistant, system, or tool")
    record.add_argument("--summary", required=True, help="Compact event summary; raw transcripts should not be stored")
    record.add_argument("--topic-id", required=True, help="Deterministic topic id")
    record.add_argument("--topic-title", default=None, help="Human-readable topic title for rolling state")
    record.add_argument("--sensitivity", default="private", help="Sensitivity: public, private, sensitive, or secret")
    record.add_argument("--timestamp-ms", type=int, default=None, help="Event timestamp in epoch ms")
    record.add_argument("--id", dest="event_id", default=None, help="Explicit event id; otherwise deterministic")
    record.add_argument("--continuity-intent", default="continue_topic", help="Continuity intent marker")
    record.add_argument("--modality", default="text", help="Event modality, e.g. text or voice")
    record.add_argument("--artifact-ref", default=None, help="Optional retained artifact reference, e.g. local://audio.ogg")
    record.add_argument("--decision", action="append", default=[], help="Decision text to attach and roll into state")
    record.add_argument("--question", action="append", default=[], help="Open question text to attach and roll into state")
    record.add_argument("--promise", action="append", default=[], help="Pending promise text to attach and roll into state")
    record.add_argument("--fact", action="append", default=[], help="Structured fact as KEY=VALUE")
    record.set_defaults(func=run_record)

    context = subcommands.add_parser(
        "context",
        help="Emit a surface-filtered continuity brief from a local store to stdout.",
    )
    context.add_argument("--store", required=True, type=Path, help="Directory containing events.jsonl and state.json")
    context.add_argument("--surface", required=True, help="Target surface receiving the brief")
    context.add_argument("--canonical-user-id", default="", help="Canonical user id to collect events for")
    context.add_argument("--channel-id", default="", help="Surface channel/thread/session identifier for alias lookup")
    context.add_argument("--sender", default="", help="Surface-specific sender for alias lookup")
    context.add_argument("--query", default="", help="Current user query/message for topic matching")
    context.add_argument("--as-of-ms", type=int, default=None, help="Deterministic generation timestamp in epoch ms")
    context.add_argument("--max-chars", type=int, default=1800, help="Maximum characters to emit")
    context.add_argument("--max-events", type=int, default=12, help="Maximum recent events to render")
    context.set_defaults(func=run_context)

    alias = subcommands.add_parser("alias", help="Manage deterministic identity aliases in the local store.")
    alias_subcommands = alias.add_subparsers(dest="alias_command", required=True)

    alias_set = alias_subcommands.add_parser("set", help="Map a surface sender to a canonical user id.")
    add_alias_identity_args(alias_set, include_canonical=True)
    alias_set.add_argument("--force", action="store_true", help="Allow remapping an existing alias")
    alias_set.set_defaults(func=run_alias_set)

    alias_resolve = alias_subcommands.add_parser("resolve", help="Resolve a surface sender to a canonical user id.")
    add_alias_identity_args(alias_resolve, include_canonical=False)
    alias_resolve.set_defaults(func=run_alias_resolve)

    alias_list = alias_subcommands.add_parser("list", help="List configured identity aliases as JSON.")
    alias_list.add_argument("--store", required=True, type=Path, help="Directory containing state.json")
    alias_list.set_defaults(func=run_alias_list)
    return parser


def add_alias_identity_args(parser: argparse.ArgumentParser, *, include_canonical: bool) -> None:
    parser.add_argument("--store", required=True, type=Path, help="Directory containing state.json")
    parser.add_argument("--surface", required=True, help="Surface, e.g. telegram or signal")
    parser.add_argument("--channel-id", required=True, help="Surface channel/thread/session identifier")
    parser.add_argument("--sender", required=True, help="Surface-specific sender display name or id")
    if include_canonical:
        parser.add_argument("--canonical-user-id", required=True, help="Canonical stitched user id")


def run_synthesize(args: argparse.Namespace) -> int:
    events = load_jsonl(args.events)
    state = load_state(args.state)
    digest = synthesize_digest(
        events=events,
        state=state,
        surface=args.surface,
        canonical_user_id=args.canonical_user_id,
        query=args.query,
        as_of_ms=args.as_of_ms,
        max_events=args.max_events,
    )
    atomic_write_json(args.out, digest)
    print(f"Wrote digest artifact: {args.out}")
    return 0


def run_record(args: argparse.Namespace) -> int:
    event = record_event(
        store=args.store,
        surface=args.surface,
        channel_id=args.channel_id,
        sender=args.sender,
        canonical_user_id=args.canonical_user_id,
        role=args.role,
        summary=args.summary,
        topic_id=args.topic_id,
        sensitivity=args.sensitivity,
        timestamp_ms=args.timestamp_ms,
        event_id=args.event_id,
        topic_title=args.topic_title,
        continuity_intent=args.continuity_intent,
        modality=args.modality,
        artifact_ref=args.artifact_ref,
        decisions=args.decision,
        questions=args.question,
        promises=args.promise,
        facts=[parse_fact(value) for value in args.fact],
    )
    print(f"Recorded event {event['id']} -> {args.store / 'events.jsonl'}")
    return 0


def run_context(args: argparse.Namespace) -> int:
    brief = build_context_brief(
        store=args.store,
        surface=args.surface,
        canonical_user_id=args.canonical_user_id,
        channel_id=args.channel_id,
        sender=args.sender,
        query=args.query,
        as_of_ms=args.as_of_ms,
        max_chars=args.max_chars,
        max_events=args.max_events,
    )
    if brief:
        print(brief, end="")
    return 0


def run_alias_set(args: argparse.Namespace) -> int:
    key = set_identity_alias(
        store=args.store,
        surface=args.surface,
        channel_id=args.channel_id,
        sender=args.sender,
        canonical_user_id=args.canonical_user_id,
        force=args.force,
    )
    print(f"Mapped {key} -> {args.canonical_user_id}")
    return 0


def run_alias_resolve(args: argparse.Namespace) -> int:
    canonical_user_id = resolve_identity_from_store(
        store=args.store,
        surface=args.surface,
        channel_id=args.channel_id,
        sender=args.sender,
    )
    if canonical_user_id:
        print(canonical_user_id)
    return 0


def run_alias_list(args: argparse.Namespace) -> int:
    print(identity_aliases_json(store=args.store), end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (OSError, ValueError) as exc:
        print(f"oac: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
