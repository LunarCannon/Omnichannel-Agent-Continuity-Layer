from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import importlib.util
import inspect
import json
import multiprocessing
import os
import re
import shutil
import sys
import textwrap
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .context import build_context_brief
from .identity import resolve_identity_alias
from .record import record_event
from .synthesize import atomic_write_json, load_state

DEFAULT_ENABLED_ENV_VAR = "OAC_GATEWAY_HOOKS_ENABLED"
DEFAULT_HOOK_NAME = "oac-context"
SUPPORTED_CONTEXT_EVENT = "agent:start"
SUPPORTED_RECORD_EVENTS = ("agent:start", "agent:end")
REQUIRED_BUNDLE_FILES = ("HOOK.yaml", "handler.py", "INSTALL.md", "bundle-manifest.json")
SAFE_HOOK_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
SAFE_ENV_VAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
HOOK_CONTRACT = {
    "manifest": "HOOK.yaml",
    "handler": "handler.py",
    "hermes_event": SUPPORTED_CONTEXT_EVENT,
    "handler_function": "handle(event_type: str, context: dict)",
}


@dataclass(frozen=True)
class TimeoutResult:
    value: str | None
    timed_out: bool
    error: str | None


def run_gateway_hook_context(
    *,
    store: Path,
    event_path: Path,
    out: Path,
    timeout_ms: int = 500,
    max_chars: int = 1800,
    enabled_env_var: str = DEFAULT_ENABLED_ENV_VAR,
) -> dict[str, Any]:
    enabled = env_enabled(enabled_env_var)
    try:
        event = load_event(event_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        artifact = base_artifact(
            enabled=enabled,
            status="error",
            event_type="",
            platform="",
            canonical_user_id="",
            smoke_id="",
            timeout_ms=timeout_ms,
            enabled_env_var=enabled_env_var,
        )
        artifact["error"] = str(exc)[:240]
        atomic_write_json(out, artifact)
        return artifact

    event_type = str(event.get("event_type") or event.get("type") or "")
    platform = str(event.get("platform") or "")
    canonical_user_id = str(event.get("canonical_user_id") or "")
    smoke_id = str(event.get("oac_smoke_id") or "")

    artifact = base_artifact(
        enabled=enabled,
        status="disabled" if not enabled else "pending",
        event_type=event_type,
        platform=platform,
        canonical_user_id=canonical_user_id,
        smoke_id=smoke_id,
        timeout_ms=timeout_ms,
        enabled_env_var=enabled_env_var,
    )

    if not enabled:
        atomic_write_json(out, artifact)
        return artifact

    if event_type != SUPPORTED_CONTEXT_EVENT:
        artifact["status"] = "skipped_event"
        atomic_write_json(out, artifact)
        return artifact

    resolved = resolve_gateway_identity(store=store, event=event)
    if resolved["canonical_user_id"] == "":
        artifact["status"] = "no_identity"
        artifact["canonical_user_id"] = ""
        atomic_write_json(out, artifact)
        return artifact
    artifact["canonical_user_id"] = resolved["canonical_user_id"]

    def build_context() -> str:
        context_markdown = build_context_brief(
            store=store,
            surface=platform,
            canonical_user_id=resolved["canonical_user_id"],
            channel_id=resolved["channel_id"],
            sender=resolved["sender"],
            query=str(event.get("message") or ""),
            max_chars=max_chars,
            infer_identity=False,
        )
        return context_markdown or ""

    result = run_with_timeout(build_context, timeout_ms=timeout_ms)
    if result.timed_out:
        artifact["status"] = "timeout"
    elif result.error is not None:
        artifact["status"] = "error"
        artifact["error"] = result.error
    elif result.value:
        artifact["status"] = "context_ready"
        artifact["context_markdown"] = result.value
    else:
        artifact["status"] = "no_context"

    atomic_write_json(out, artifact)
    return artifact


def run_gateway_hook_record(
    *,
    store: Path,
    event_path: Path,
    out: Path,
    enabled_env_var: str = DEFAULT_ENABLED_ENV_VAR,
) -> dict[str, Any]:
    enabled = env_enabled(enabled_env_var)
    try:
        event = load_event(event_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        report = base_record_report(
            enabled=enabled,
            status="error",
            event_type="",
            platform="",
            enabled_env_var=enabled_env_var,
        )
        report["error"] = str(exc)[:240]
        atomic_write_json(out, report)
        return report

    event_type = str(event.get("event_type") or event.get("type") or "")
    platform = str(event.get("platform") or "")
    report = base_record_report(
        enabled=enabled,
        status="disabled" if not enabled else "pending",
        event_type=event_type,
        platform=platform,
        enabled_env_var=enabled_env_var,
    )

    if not enabled:
        atomic_write_json(out, report)
        return report
    if event_type not in SUPPORTED_RECORD_EVENTS:
        report["status"] = "skipped_event"
        atomic_write_json(out, report)
        return report

    try:
        resolved = resolve_gateway_identity(store=store, event=event)
        if resolved["canonical_user_id"] == "":
            report["status"] = "no_identity"
            atomic_write_json(out, report)
            return report

        summary = gateway_record_summary(event_type=event_type, event=event)
        if summary == "":
            report["status"] = "empty_summary"
            atomic_write_json(out, report)
            return report

        role = "assistant" if event_type == "agent:end" else "user"
        recorded = record_event(
            store=store,
            surface=platform,
            channel_id=resolved["channel_id"],
            sender=resolved["sender"],
            canonical_user_id=resolved["canonical_user_id"],
            role=role,
            summary=summary,
            topic_id=gateway_topic_id(event),
            sensitivity="private",
            continuity_intent="note",
            modality="text",
        )
    except Exception as exc:
        report["status"] = "error"
        report["error"] = str(exc)[:240]
        atomic_write_json(out, report)
        return report

    report.update(
        {
            "status": "recorded",
            "recorded_event_id": recorded["id"],
            "role": role,
            "topic_id": recorded["topic_id"],
            "channel_id": recorded["channel_id"],
            "canonical_user_id": recorded["canonical_user_id"],
        }
    )
    atomic_write_json(out, report)
    return report


def stage_gateway_hook_bundle(
    *,
    out_dir: Path,
    store: Path,
    artifact_dir: Path,
    python: str = sys.executable,
    src_path: Path | None = None,
    timeout_ms: int = 500,
    max_chars: int = 1800,
    enabled_env_var: str = DEFAULT_ENABLED_ENV_VAR,
    hook_name: str = DEFAULT_HOOK_NAME,
    allow_live_target: bool = False,
) -> dict[str, Any]:
    live_install = is_live_hermes_hooks_target(out_dir)
    validate_hook_name(hook_name)
    validate_enabled_env_var(enabled_env_var)
    if live_install and not allow_live_target:
        raise ValueError("refusing to stage directly under ~/.hermes/hooks without --allow-live-target")

    manifest = {
        "artifact_dir": str(artifact_dir),
        "enabled_env_var": enabled_env_var,
        "events": list(SUPPORTED_RECORD_EVENTS),
        "fail_open": True,
        "handler": "handler.py",
        "hook_name": hook_name,
        "live_install": live_install,
        "manifest": "HOOK.yaml",
        "max_chars": max_chars,
        "python": python,
        "src_path": str(src_path) if src_path is not None else "",
        "store": str(store),
        "timeout_ms": timeout_ms,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    write_text_atomic(out_dir / "HOOK.yaml", render_hook_yaml(hook_name=hook_name))
    write_text_atomic(out_dir / "handler.py", render_handler(manifest))
    write_text_atomic(out_dir / "INSTALL.md", render_install_md(out_dir=out_dir, manifest=manifest))
    atomic_write_json(out_dir / "bundle-manifest.json", manifest)
    return manifest


def install_gateway_hook_bundle(
    *,
    bundle_dir: Path,
    hooks_root: Path | None = None,
    hook_name: str | None = None,
    plan_out: Path | None = None,
    apply: bool = False,
    confirm_hook_name: str = "",
    force: bool = False,
) -> dict[str, Any]:
    bundle_manifest = validate_gateway_hook_bundle(bundle_dir)
    resolved_hook_name = hook_name or str(bundle_manifest.get("hook_name") or DEFAULT_HOOK_NAME)
    validate_hook_name(resolved_hook_name)
    resolved_hooks_root = hooks_root or (Path.home() / ".hermes" / "hooks")
    target_dir = resolved_hooks_root / resolved_hook_name

    if plan_out is not None and is_relative_to(plan_out.resolve(strict=False), target_dir.resolve(strict=False)):
        raise ValueError("plan-out must not be inside the hook target")

    plan = build_install_plan(
        bundle_dir=bundle_dir,
        hooks_root=resolved_hooks_root,
        target_dir=target_dir,
        hook_name=resolved_hook_name,
        bundle_manifest=bundle_manifest,
        apply=apply,
        force=force,
    )

    if not apply:
        if plan_out is not None:
            atomic_write_json(plan_out, plan)
        return plan

    required_confirmation = f"--confirm-hook-name {resolved_hook_name}"
    if confirm_hook_name != resolved_hook_name:
        raise ValueError(f"live hook install requires {required_confirmation}")
    if path_exists(target_dir) and not force:
        raise ValueError(f"target already exists: {target_dir}; pass --force to replace it")

    resolved_hooks_root.mkdir(parents=True, exist_ok=True)
    tmp_dir = resolved_hooks_root / f".{resolved_hook_name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        tmp_dir.mkdir(parents=False, exist_ok=False)
        for relative_path in REQUIRED_BUNDLE_FILES:
            shutil.copy2(bundle_dir / relative_path, tmp_dir / relative_path)
        if path_exists(target_dir):
            remove_path(target_dir)
        tmp_dir.replace(target_dir)
    finally:
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)

    plan["status"] = "installed"
    plan["mode"] = "apply"
    if plan_out is not None:
        atomic_write_json(plan_out, plan)
    return plan


def run_gateway_hook_smoke(
    *,
    hooks_root: Path,
    hook_name: str,
    event_path: Path,
    out: Path,
    allow_live_root: bool = False,
    forbidden_strings: list[str] | None = None,
) -> tuple[bool, dict[str, Any]]:
    validate_hook_name(hook_name)
    if is_live_hermes_hooks_target(hooks_root) and not allow_live_root:
        raise ValueError("refusing to smoke live Hermes hooks root without --allow-live-root")

    hook_dir = hooks_root / hook_name
    manifest = validate_gateway_hook_bundle(hook_dir)
    enabled_env_var = str(manifest.get("enabled_env_var") or DEFAULT_ENABLED_ENV_VAR)
    artifact_dir = Path(str(manifest.get("artifact_dir") or ""))
    event = load_event(event_path)
    event_type = str(event.get("event_type") or event.get("type") or "")
    smoke_id = uuid.uuid4().hex
    context = dict(event)
    context.pop("event_type", None)
    context.pop("type", None)
    context["oac_smoke_id"] = smoke_id

    before = artifact_paths(artifact_dir)
    previous_env = os.environ.get(enabled_env_var)
    os.environ[enabled_env_var] = "1"
    handler_error = ""
    try:
        module = load_hook_handler(hook_dir / "handler.py")
        result = module.handle(event_type, context)
        if inspect.isawaitable(result):
            asyncio.run(result)
    except Exception as exc:  # local smoke should report errors instead of crashing with partial output
        handler_error = str(exc)[:240]
    finally:
        if previous_env is None:
            os.environ.pop(enabled_env_var, None)
        else:
            os.environ[enabled_env_var] = previous_env

    after = artifact_paths(artifact_dir)
    created_paths = [path for path in after if path not in before]
    context_artifacts = [load_context_artifact(path) for path in created_paths]
    context_ready = any(
        artifact.get("status") == "context_ready"
        and artifact.get("event_type") == SUPPORTED_CONTEXT_EVENT
        and artifact.get("smoke_id") == smoke_id
        and bool(artifact.get("context_markdown"))
        for artifact in context_artifacts
    )
    checks = {
        "handler_error_free": handler_error == "",
        "context_artifact_created": len(context_artifacts) > 0,
        "context_ready": context_ready,
        "redaction_ok": True,
    }
    status = "passed" if checks["handler_error_free"] and checks["context_artifact_created"] and checks["context_ready"] else "failed"
    report = {
        "artifact_type": "gateway_hook_live_smoke",
        "version": 1,
        "status": status,
        "hook_name": hook_name,
        "smoke_id": smoke_id,
        "hook_dir": str(hook_dir),
        "event_type": event_type,
        "enabled_env_var": enabled_env_var,
        "enabled_during_smoke": True,
        "context_artifacts_created": len(context_artifacts),
        "context_artifacts": context_artifacts,
        "checks": checks,
        "fail_open": True,
        "delivery_action": "none",
        "gateway_restart_action": "none",
        "hook_contract": dict(HOOK_CONTRACT),
    }
    if handler_error:
        report["handler_error"] = handler_error

    forbidden_values = [value for value in (forbidden_strings or []) if value]
    rendered = json.dumps(report, sort_keys=True)
    leaked = any(value in rendered for value in forbidden_values)
    if leaked:
        checks["redaction_ok"] = False
        delete_paths(created_paths)
        report = sanitized_smoke_failure(report)

    passed = report["status"] == "passed" and report["checks"]["redaction_ok"] is True
    atomic_write_json(out, report)
    return passed, report


def artifact_paths(artifact_dir: Path) -> set[Path]:
    if not artifact_dir.exists() or not artifact_dir.is_dir():
        return set()
    return set(artifact_dir.glob("hook-context-*.json"))


def load_context_artifact(path: Path) -> dict[str, Any]:
    artifact = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(artifact, dict):
        raise ValueError(f"Invalid context artifact at {path}: expected object")
    artifact = dict(artifact)
    artifact["path"] = str(path)
    return artifact


def delete_paths(paths: list[Path]) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def sanitized_smoke_failure(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifact_type": report["artifact_type"],
        "version": report["version"],
        "status": "failed",
        "hook_name": report["hook_name"],
        "smoke_id": report.get("smoke_id", ""),
        "hook_dir": report["hook_dir"],
        "event_type": report["event_type"],
        "enabled_env_var": report["enabled_env_var"],
        "enabled_during_smoke": report["enabled_during_smoke"],
        "context_artifacts_created": report["context_artifacts_created"],
        "context_artifacts": [
            {
                "path": artifact.get("path", ""),
                "status": artifact.get("status", ""),
                "delivery_action": artifact.get("delivery_action", "none"),
            }
            for artifact in report.get("context_artifacts", [])
            if isinstance(artifact, dict)
        ],
        "checks": {**report["checks"], "redaction_ok": False},
        "fail_open": True,
        "delivery_action": "none",
        "gateway_restart_action": "none",
        "hook_contract": dict(HOOK_CONTRACT),
    }


def load_hook_handler(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(f"oac_live_smoke_{uuid.uuid4().hex}", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Unable to load hook handler at {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "handle"):
        raise ValueError(f"Hook handler at {path} has no handle function")
    return module


def validate_gateway_hook_bundle(bundle_dir: Path) -> dict[str, Any]:
    if not bundle_dir.exists() or not bundle_dir.is_dir():
        raise ValueError(f"bundle directory does not exist: {bundle_dir}")
    for relative_path in REQUIRED_BUNDLE_FILES:
        if not (bundle_dir / relative_path).is_file():
            raise ValueError(f"missing required bundle file: {relative_path}")
    manifest = json.loads((bundle_dir / "bundle-manifest.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("bundle-manifest.json must contain an object")
    hook_name = str(manifest.get("hook_name") or DEFAULT_HOOK_NAME)
    validate_hook_name(hook_name)
    if manifest.get("events") != list(SUPPORTED_RECORD_EVENTS):
        raise ValueError("bundle manifest must subscribe only to agent:start and agent:end")
    enabled_env_var = str(manifest.get("enabled_env_var") or "")
    validate_enabled_env_var(enabled_env_var)
    if (bundle_dir / "HOOK.yaml").read_text(encoding="utf-8") != render_hook_yaml(hook_name=hook_name):
        raise ValueError("HOOK.yaml must match staged bundle manifest")
    if (bundle_dir / "handler.py").read_text(encoding="utf-8") != render_handler(manifest):
        raise ValueError("handler.py must match staged bundle manifest")
    return manifest


def build_install_plan(
    *,
    bundle_dir: Path,
    hooks_root: Path,
    target_dir: Path,
    hook_name: str,
    bundle_manifest: dict[str, Any],
    apply: bool,
    force: bool,
) -> dict[str, Any]:
    return {
        "artifact_type": "gateway_hook_install_plan",
        "version": 1,
        "mode": "apply" if apply else "dry_run",
        "apply": apply,
        "status": "planned",
        "hook_name": hook_name,
        "bundle_dir": str(bundle_dir),
        "hooks_root": str(hooks_root),
        "target_dir": str(target_dir),
        "target_exists": path_exists(target_dir),
        "force": force,
        "files": bundle_file_entries(bundle_dir=bundle_dir, target_dir=target_dir),
        "enabled_after_install": False,
        "requires_env_enable": f"{bundle_manifest.get('enabled_env_var', DEFAULT_ENABLED_ENV_VAR)}=1",
        "gateway_restart_action": "none",
        "delivery_action": "none",
        "fail_open": True,
        "hook_contract": dict(HOOK_CONTRACT),
    }


def bundle_file_entries(*, bundle_dir: Path, target_dir: Path) -> list[dict[str, Any]]:
    entries = []
    for relative_path in REQUIRED_BUNDLE_FILES:
        source = bundle_dir / relative_path
        entries.append(
            {
                "relative_path": relative_path,
                "source": str(source),
                "target": str(target_dir / relative_path),
                "sha256": sha256_file(source),
                "bytes": source.stat().st_size,
            }
        )
    return entries


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_hook_name(hook_name: str) -> None:
    if (
        not hook_name
        or hook_name in {".", ".."}
        or not SAFE_HOOK_NAME_RE.fullmatch(hook_name)
        or "/" in hook_name
        or "\\" in hook_name
        or any(ord(char) < 32 for char in hook_name)
    ):
        raise ValueError(f"unsafe hook name: {hook_name!r}")


def validate_enabled_env_var(name: str) -> None:
    if not SAFE_ENV_VAR_RE.fullmatch(name):
        raise ValueError(f"unsafe enabled env var: {name!r}")


def unique_nonempty(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def path_exists(path: Path) -> bool:
    return os.path.lexists(path)


def remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def is_live_hermes_hooks_target(path: Path) -> bool:
    hooks_root = (Path.home() / ".hermes" / "hooks").resolve(strict=False)
    target = path.resolve(strict=False)
    try:
        target.relative_to(hooks_root)
        return True
    except ValueError:
        return False


def render_hook_yaml(*, hook_name: str) -> str:
    return textwrap.dedent(
        f"""
        name: {hook_name}
        description: Add local OAC context artifacts and record compact gateway turns.
        events:
          - agent:start
          - agent:end
        """
    ).lstrip()


def render_handler(manifest: dict[str, Any]) -> str:
    config_literal = repr(manifest)
    return f'''"""Staged OAC gateway hook handler.

Generated by `oac gateway-hook bundle`. Disabled by default via the
configured env kill switch and fail-open on every error path.
"""

import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path


CONFIG = {config_literal}


async def handle(event_type: str, context: dict):
    if os.environ.get(CONFIG["enabled_env_var"], "").strip().lower() not in {{"1", "true", "yes", "on"}}:
        return
    if event_type not in {{"agent:start", "agent:end"}}:
        return

    artifact_dir = Path(CONFIG["artifact_dir"])
    event_path = None
    try:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            suffix=".json",
            prefix="oac-gateway-event-",
            dir=artifact_dir,
            delete=False,
        ) as event_file:
            event_path = Path(event_file.name)
            payload = {{**dict(context), "event_type": event_type}}
            json.dump(payload, event_file, sort_keys=True)

        context_out_path = artifact_dir / f"hook-context-{{int(time.time() * 1000)}}-{{uuid.uuid4().hex}}.json"
        record_out_path = artifact_dir / f"hook-record-{{int(time.time() * 1000)}}-{{uuid.uuid4().hex}}.json"
        env = os.environ.copy()
        src_path = CONFIG.get("src_path") or ""
        if src_path:
            existing = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = src_path if not existing else src_path + os.pathsep + existing

        if event_type == "agent:start":
            subprocess.run(
                [
                    CONFIG["python"],
                    "-m",
                    "oac.cli",
                    "gateway-hook",
                    "context",
                    "--store",
                    CONFIG["store"],
                    "--event",
                    str(event_path),
                    "--out",
                    str(context_out_path),
                    "--timeout-ms",
                    str(CONFIG["timeout_ms"]),
                    "--max-chars",
                    str(CONFIG["max_chars"]),
                    "--enabled-env-var",
                    CONFIG["enabled_env_var"],
                ],
                text=True,
                capture_output=True,
                timeout=(max(int(CONFIG["timeout_ms"]), 0) / 1000) + 1,
                env=env,
                check=False,
            )

        subprocess.run(
            [
                CONFIG["python"],
                "-m",
                "oac.cli",
                "gateway-hook",
                "record",
                "--store",
                CONFIG["store"],
                "--event",
                str(event_path),
                "--out",
                str(record_out_path),
                "--enabled-env-var",
                CONFIG["enabled_env_var"],
            ],
            text=True,
            capture_output=True,
            timeout=1,
            env=env,
            check=False,
        )
    except Exception:
        return
    finally:
        if event_path is not None:
            try:
                event_path.unlink(missing_ok=True)
            except Exception:
                pass
'''


def render_install_md(*, out_dir: Path, manifest: dict[str, Any]) -> str:
    return textwrap.dedent(
        f"""
        # OAC Gateway Hook Bundle

        This staged Hermes gateway hook bundle is disabled by default.

        Do not copy this into ~/.hermes/hooks until you explicitly want live gateway wiring.
        When you are ready, copy this directory to `~/.hermes/hooks/{manifest['hook_name']}` and restart the gateway.

        Enable the hook only for a smoke run:

        ```bash
        {manifest['enabled_env_var']}=1 hermes gateway run
        ```

        Files:

        - `HOOK.yaml` subscribes only to `agent:start` and `agent:end`.
        - `handler.py` calls the local OAC CLI with a timeout and fails open.
        - `bundle-manifest.json` records deterministic staging config.

        Output artifacts will be written under:

        ```text
        {manifest['artifact_dir']}
        ```
        """
    ).lstrip()


def write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)

def load_event(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Invalid gateway event at {path}: expected object")
    return value


def env_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def resolve_gateway_identity(*, store: Path, event: dict[str, Any]) -> dict[str, str]:
    surface = str(event.get("platform") or "")
    sender = str(event.get("sender") or event.get("user_id") or "")
    channel_ids = unique_nonempty(
        [
            str(event.get("channel_id") or ""),
            str(event.get("chat_id") or ""),
            str(event.get("thread_id") or ""),
        ]
    )
    if not surface or not sender or not channel_ids:
        return {"canonical_user_id": "", "channel_id": channel_ids[0] if channel_ids else "", "sender": sender}
    canonical_user_id = str(event.get("canonical_user_id") or "")
    if canonical_user_id:
        return {"canonical_user_id": canonical_user_id, "channel_id": channel_ids[0], "sender": sender}

    state = load_state(store / "state.json")
    for channel_id in channel_ids:
        resolved = resolve_identity_alias(
            state=state,
            surface=surface,
            channel_id=channel_id,
            sender=sender,
        )
        if resolved:
            return {"canonical_user_id": resolved, "channel_id": channel_id, "sender": sender}
    return {"canonical_user_id": "", "channel_id": channel_ids[0], "sender": sender}


def gateway_record_summary(*, event_type: str, event: dict[str, Any]) -> str:
    raw_value = event.get("response") if event_type == "agent:end" else event.get("message")
    raw = str(raw_value or "")
    stripped = strip_injected_oac_context(raw)
    return " ".join(stripped.split())[:240]


def strip_injected_oac_context(text: str) -> str:
    header_re = re.compile(r"(^|\n)\s*##\s+Omnichannel Agent Continuity\b")
    match = header_re.search(text)
    if match:
        return text[: match.start()].strip()
    legacy_re = re.compile(r"(^|\n)\s*Omnichannel Agent Continuity\s*\nThe following\b")
    match = legacy_re.search(text)
    if match:
        return text[: match.start()].strip()
    return text.strip()


def gateway_topic_id(event: dict[str, Any]) -> str:
    for key in ("topic_id", "topic", "session_title", "session_id"):
        value = str(event.get(key) or "").strip()
        if value and not any(ord(character) < 32 or ord(character) == 127 for character in value):
            return value[:120]
    return "gateway-turns"


def base_artifact(
    *,
    enabled: bool,
    status: str,
    event_type: str,
    platform: str,
    canonical_user_id: str,
    smoke_id: str,
    timeout_ms: int,
    enabled_env_var: str,
) -> dict[str, Any]:
    return {
        "artifact_type": "gateway_hook_context",
        "version": 1,
        "enabled": enabled,
        "enabled_env_var": enabled_env_var,
        "status": status,
        "event_type": event_type,
        "platform": platform,
        "canonical_user_id": canonical_user_id,
        "smoke_id": smoke_id,
        "timeout_ms": timeout_ms,
        "fail_open": True,
        "delivery_action": "none",
        "hook_contract": dict(HOOK_CONTRACT),
        "context_markdown": "",
    }


def base_record_report(
    *,
    enabled: bool,
    status: str,
    event_type: str,
    platform: str,
    enabled_env_var: str,
) -> dict[str, Any]:
    return {
        "artifact_type": "gateway_hook_record",
        "version": 1,
        "enabled": enabled,
        "enabled_env_var": enabled_env_var,
        "status": status,
        "event_type": event_type,
        "platform": platform,
        "fail_open": True,
        "delivery_action": "none",
        "recorded_event_id": "",
    }


def run_with_timeout(func: Callable[[], str], *, timeout_ms: int) -> TimeoutResult:
    if "fork" not in multiprocessing.get_all_start_methods():
        return run_with_thread_timeout(func, timeout_ms=timeout_ms)

    context = multiprocessing.get_context("fork")
    queue: multiprocessing.Queue[tuple[str, str]] = context.Queue(maxsize=1)
    process = context.Process(target=_run_timeout_child, args=(func, queue))
    process.start()
    process.join(max(timeout_ms, 0) / 1000)
    if process.is_alive():
        process.terminate()
        process.join(timeout=0.05)
        if process.is_alive():
            process.kill()
            process.join(timeout=0.05)
        return TimeoutResult(value=None, timed_out=True, error=None)

    if queue.empty():
        return TimeoutResult(value=None, timed_out=False, error="context builder exited without a result")
    status, payload = queue.get_nowait()
    if status == "ok":
        return TimeoutResult(value=payload, timed_out=False, error=None)
    return TimeoutResult(value=None, timed_out=False, error=payload)


def _run_timeout_child(func: Callable[[], str], queue: multiprocessing.Queue[tuple[str, str]]) -> None:
    try:
        queue.put(("ok", func()))
    except Exception as exc:  # fail-open boundary for gateway hook callers
        queue.put(("error", str(exc)))


def run_with_thread_timeout(func: Callable[[], str], *, timeout_ms: int) -> TimeoutResult:
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(func)
    try:
        return TimeoutResult(value=future.result(timeout=max(timeout_ms, 0) / 1000), timed_out=False, error=None)
    except concurrent.futures.TimeoutError:
        future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        return TimeoutResult(value=None, timed_out=True, error=None)
    except Exception as exc:  # fail-open boundary for gateway hook callers
        return TimeoutResult(value=None, timed_out=False, error=str(exc))
    finally:
        if future.done():
            executor.shutdown(wait=False, cancel_futures=True)
