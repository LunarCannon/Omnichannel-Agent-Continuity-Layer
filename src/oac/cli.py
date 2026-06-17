from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .context import build_context_brief
from .gateway_hook import (
    DEFAULT_ENABLED_ENV_VAR,
    install_gateway_hook_bundle,
    run_gateway_hook_context,
    run_gateway_hook_record,
    run_gateway_hook_smoke,
    stage_gateway_hook_bundle,
)
from .identity import identity_aliases_json, resolve_identity_from_store, set_identity_alias
from .record import parse_fact, record_event
from .smoke import run_smoke_check
from .state_db_sync import sync_state_db
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

    smoke = subcommands.add_parser(
        "smoke",
        help="Run a deterministic local cross-channel continuity smoke check and write a report artifact.",
    )
    smoke.add_argument("--store", required=True, type=Path, help="Directory containing events.jsonl and state.json")
    smoke.add_argument("--out", required=True, type=Path, help="Path to write smoke report JSON")
    smoke.add_argument("--surface", required=True, help="Target surface receiving the brief")
    smoke.add_argument("--canonical-user-id", required=True, help="Canonical stitched user id")
    smoke.add_argument("--query", default="", help="Current user query/message for topic matching")
    smoke.add_argument("--as-of-ms", type=int, default=None, help="Deterministic generation timestamp in epoch ms")
    smoke.add_argument("--max-events", type=int, default=12, help="Maximum recent events to render")
    smoke.add_argument(
        "--forbidden-string",
        action="append",
        default=[],
        help="String that must be absent from the serialized smoke report; repeatable.",
    )
    smoke.set_defaults(func=run_smoke)

    sync_state = subcommands.add_parser(
        "sync-state-db",
        help="Sync compact user/assistant summaries from Hermes state.db into a v1 OAC store.",
    )
    sync_state.add_argument("--store", required=True, type=Path, help="Directory containing events.jsonl and state.json")
    sync_state.add_argument(
        "--state-db",
        required=True,
        type=Path,
        help="Hermes SQLite state.db to read in read-only mode.",
    )
    sync_state.add_argument("--limit", type=int, default=500, help="Maximum messages to scan per run")
    sync_state.add_argument("--full", action="store_true", help="Scan from message id 0 but do not duplicate existing event ids")
    sync_state.add_argument("--quiet", action="store_true", help="Suppress report output when no events are synced")
    sync_state.set_defaults(func=run_sync_state_db)

    gateway_hook = subcommands.add_parser(
        "gateway-hook",
        help="Local fail-open helpers for Hermes gateway hook integration design.",
    )
    gateway_hook_subcommands = gateway_hook.add_subparsers(dest="gateway_hook_command", required=True)
    hook_context = gateway_hook_subcommands.add_parser(
        "context",
        help="Build a bounded prompt-context artifact for a Hermes agent:start gateway event.",
    )
    hook_context.add_argument("--store", required=True, type=Path, help="Directory containing events.jsonl and state.json")
    hook_context.add_argument("--event", required=True, type=Path, help="Gateway event JSON file")
    hook_context.add_argument("--out", required=True, type=Path, help="Path to write hook context artifact JSON")
    hook_context.add_argument("--timeout-ms", type=int, default=500, help="Fail-open timeout budget in milliseconds")
    hook_context.add_argument("--max-chars", type=int, default=1800, help="Maximum context characters to emit")
    hook_context.add_argument(
        "--enabled-env-var",
        default=DEFAULT_ENABLED_ENV_VAR,
        help="Environment variable that must be truthy for hook context generation.",
    )
    hook_context.set_defaults(func=run_gateway_hook_context_command)

    hook_record = gateway_hook_subcommands.add_parser(
        "record",
        help="Record a compact v1 OAC event for a Hermes agent:start or agent:end gateway event.",
    )
    hook_record.add_argument("--store", required=True, type=Path, help="Directory containing events.jsonl and state.json")
    hook_record.add_argument("--event", required=True, type=Path, help="Gateway event JSON file")
    hook_record.add_argument("--out", required=True, type=Path, help="Path to write hook record report JSON")
    hook_record.add_argument(
        "--enabled-env-var",
        default=DEFAULT_ENABLED_ENV_VAR,
        help="Environment variable that must be truthy for hook recording.",
    )
    hook_record.set_defaults(func=run_gateway_hook_record_command)

    hook_bundle = gateway_hook_subcommands.add_parser(
        "bundle",
        help="Stage HOOK.yaml + handler.py for explicit Hermes gateway hook installation later.",
    )
    hook_bundle.add_argument("--out-dir", required=True, type=Path, help="Directory to write staged hook bundle files")
    hook_bundle.add_argument("--store", required=True, type=Path, help="Local OAC store path the staged handler should read")
    hook_bundle.add_argument("--artifact-dir", required=True, type=Path, help="Directory where live hook context artifacts should be written")
    hook_bundle.add_argument("--python", default=sys.executable, help="Python executable for the staged handler to call")
    hook_bundle.add_argument("--src-path", type=Path, default=None, help="Optional OAC src path to prepend to PYTHONPATH")
    hook_bundle.add_argument("--timeout-ms", type=int, default=500, help="Fail-open timeout budget in milliseconds")
    hook_bundle.add_argument("--max-chars", type=int, default=1800, help="Maximum context characters to emit")
    hook_bundle.add_argument(
        "--enabled-env-var",
        default=DEFAULT_ENABLED_ENV_VAR,
        help="Environment variable that must be truthy for live handler context generation.",
    )
    hook_bundle.add_argument("--hook-name", default="oac-context", help="Hermes hook directory/name")
    hook_bundle.add_argument(
        "--allow-live-target",
        action="store_true",
        help="Allow writing directly under ~/.hermes/hooks; still disabled until the env var is truthy.",
    )
    hook_bundle.set_defaults(func=run_gateway_hook_bundle_command)

    hook_install = gateway_hook_subcommands.add_parser(
        "install",
        help="Plan or explicitly apply a staged hook bundle into a Hermes hooks root.",
    )
    hook_install.add_argument("--bundle-dir", required=True, type=Path, help="Staged hook bundle directory")
    hook_install.add_argument(
        "--hooks-root",
        type=Path,
        default=None,
        help="Hermes hooks root; defaults to ~/.hermes/hooks",
    )
    hook_install.add_argument("--hook-name", default=None, help="Hook directory/name; defaults to bundle manifest")
    hook_install.add_argument("--plan-out", type=Path, default=None, help="Optional JSON plan artifact path")
    hook_install.add_argument("--apply", action="store_true", help="Copy the bundle into hooks-root/hook-name")
    hook_install.add_argument(
        "--confirm-hook-name",
        default="",
        help="Required with --apply; must exactly match the resolved hook name.",
    )
    hook_install.add_argument("--force", action="store_true", help="Replace an existing hook target")
    hook_install.set_defaults(func=run_gateway_hook_install_command)

    hook_smoke = gateway_hook_subcommands.add_parser(
        "smoke",
        help="Run an enabled local smoke against an installed hook bundle without restarting gateway.",
    )
    hook_smoke.add_argument(
        "--hooks-root",
        type=Path,
        default=Path.home() / ".hermes" / "hooks",
        help="Hermes hooks root containing the installed hook; defaults to ~/.hermes/hooks",
    )
    hook_smoke.add_argument("--hook-name", default="oac-context", help="Installed hook directory/name")
    hook_smoke.add_argument("--event", required=True, type=Path, help="Gateway event JSON file")
    hook_smoke.add_argument("--out", required=True, type=Path, help="Path to write live smoke report JSON")
    hook_smoke.add_argument(
        "--allow-live-root",
        action="store_true",
        help="Allow smoking the real ~/.hermes/hooks root; does not restart gateway or enable env globally.",
    )
    hook_smoke.add_argument(
        "--forbidden-string",
        action="append",
        default=[],
        help="String that must be absent from the serialized smoke report; repeatable.",
    )
    hook_smoke.set_defaults(func=run_gateway_hook_smoke_command)

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


def run_smoke(args: argparse.Namespace) -> int:
    passed, _report = run_smoke_check(
        store=args.store,
        out=args.out,
        surface=args.surface,
        canonical_user_id=args.canonical_user_id,
        query=args.query,
        as_of_ms=args.as_of_ms,
        max_events=args.max_events,
        forbidden_strings=args.forbidden_string,
    )
    print(f"Wrote smoke report: {args.out}")
    if not passed:
        print(f"Smoke checks failed: {args.out}", file=sys.stderr)
        return 1
    return 0


def run_sync_state_db(args: argparse.Namespace) -> int:
    report = sync_state_db(
        store=args.store,
        state_db=args.state_db,
        limit=args.limit,
        full=args.full,
    )
    if not args.quiet or report.get("synced", 0) > 0:
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 1


def run_gateway_hook_context_command(args: argparse.Namespace) -> int:
    artifact = run_gateway_hook_context(
        store=args.store,
        event_path=args.event,
        out=args.out,
        timeout_ms=args.timeout_ms,
        max_chars=args.max_chars,
        enabled_env_var=args.enabled_env_var,
    )
    print(f"Wrote gateway hook context artifact: {args.out} ({artifact['status']})")
    return 0


def run_gateway_hook_record_command(args: argparse.Namespace) -> int:
    report = run_gateway_hook_record(
        store=args.store,
        event_path=args.event,
        out=args.out,
        enabled_env_var=args.enabled_env_var,
    )
    print(f"Wrote gateway hook record report: {args.out} ({report['status']})")
    return 0


def run_gateway_hook_bundle_command(args: argparse.Namespace) -> int:
    manifest = stage_gateway_hook_bundle(
        out_dir=args.out_dir,
        store=args.store,
        artifact_dir=args.artifact_dir,
        python=args.python,
        src_path=args.src_path,
        timeout_ms=args.timeout_ms,
        max_chars=args.max_chars,
        enabled_env_var=args.enabled_env_var,
        hook_name=args.hook_name,
        allow_live_target=args.allow_live_target,
    )
    print(f"Staged gateway hook bundle: {args.out_dir} ({manifest['hook_name']})")
    return 0


def run_gateway_hook_install_command(args: argparse.Namespace) -> int:
    plan = install_gateway_hook_bundle(
        bundle_dir=args.bundle_dir,
        hooks_root=args.hooks_root,
        hook_name=args.hook_name,
        plan_out=args.plan_out,
        apply=args.apply,
        confirm_hook_name=args.confirm_hook_name,
        force=args.force,
    )
    if args.apply:
        print(f"Installed gateway hook bundle: {plan['target_dir']} ({plan['hook_name']})")
    else:
        print(f"Planned gateway hook install: {plan['target_dir']} ({plan['hook_name']})")
    return 0


def run_gateway_hook_smoke_command(args: argparse.Namespace) -> int:
    passed, _report = run_gateway_hook_smoke(
        hooks_root=args.hooks_root,
        hook_name=args.hook_name,
        event_path=args.event,
        out=args.out,
        allow_live_root=args.allow_live_root,
        forbidden_strings=args.forbidden_string,
    )
    print(f"Smoked installed gateway hook: {args.hooks_root / args.hook_name} -> {args.out}")
    return 0 if passed else 1


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
