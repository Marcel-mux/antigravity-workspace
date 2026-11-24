"""Async subprocess runner for the Antigravity CLI (`agy`)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agy_gateway.formatting import parse_stream_event

ProgressCallback = Callable[[str, str], Awaitable[None]]

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\r")


def parse_models_output(raw: str) -> list[tuple[str, str]]:
    """Parse `agy models` output into `(model_id, display_label)` pairs."""
    models: list[tuple[str, str]] = []
    for line in raw.splitlines():
        cleaned = ANSI_ESCAPE_RE.sub("", line).strip()
        if not cleaned or "Fetching available models" in cleaned:
            continue
        parts = re.split(r"\t+|\s{2,}", cleaned, maxsplit=1)
        model_id = parts[0].strip()
        label = parts[1].strip() if len(parts) > 1 else model_id
        if model_id:
            models.append((model_id, label))
    return models


@dataclass
class AgyRunResult:
    """Outcome of a single `agy -p` execution."""

    response: str
    conversation_id: str | None = None
    duration_seconds: float | None = None
    exit_code: int = 0
    error: str | None = None
    cancelled: bool = False
    usage: dict[str, int] | None = None
    context_tokens: int | None = None


def build_agy_command(
    agy_bin: str,
    prompt: str,
    *,
    conversation_id: str | None = None,
    mode: str = "auto",
    model: str | None = None,
    sandbox: bool = False,
    skip_permissions: bool = False,
    timeout_seconds: int = 0,
    extra_dirs: list[Path] | None = None,
) -> list[str]:
    """Construct the `agy` CLI argument list for a headless streaming turn."""
    cmd = [agy_bin, "-p", prompt, "--output-format", "stream-json", "--mode", mode]
    if conversation_id:
        cmd.extend(["--conversation", conversation_id])
    if model:
        clean_model = model.split("\t", 1)[0].strip()
        if clean_model:
            cmd.extend(["--model", clean_model])
    if sandbox:
        cmd.append("--sandbox")
    if skip_permissions:
        cmd.append("--dangerously-skip-permissions")
    if timeout_seconds > 0:
        cmd.extend(["--print-timeout", f"{timeout_seconds}s"])
    for d in extra_dirs or ():
        cmd.extend(["--add-dir", str(d)])
    return cmd


def _step_status_label(step_type: str | None, state: str | None) -> str:
    if not step_type:
        return "⏳ Working..."
    if step_type == "agent_response":
        return "✍️ Writing response..." if state == "ACTIVE" else "⏳ Finishing..."
    if "tool" in step_type or "command" in step_type:
        return f"🛠️ Running `{step_type}`..."
    return f"⏳ `{step_type}` ({state or 'running'})..."


class AgyRunner:
    """Manages active `agy` subprocesses per Discord channel."""

    def __init__(self, agy_bin: str = "agy") -> None:
        self.agy_bin = agy_bin
        self._active_procs: dict[int, asyncio.subprocess.Process] = {}
        self._cancelled_channels: set[int] = set()
        self._models_cache: list[tuple[str, str]] = []
        self._models_cache_ts: float = 0.0

    def is_running(self, channel_id: int) -> bool:
        """Return True if an `agy` turn is currently running in channel_id."""
        proc = self._active_procs.get(channel_id)
        return proc is not None and proc.returncode is None

    async def stop(self, channel_id: int) -> bool:
        """Terminate the active `agy` process for channel_id, if any."""
        proc = self._active_procs.get(channel_id)
        if proc is None or proc.returncode is not None:
            return False
        self._cancelled_channels.add(channel_id)
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        return True

    async def run_prompt(
        self,
        channel_id: int,
        prompt: str,
        *,
        workdir: Path,
        conversation_id: str | None = None,
        mode: str = "auto",
        model: str | None = None,
        sandbox: bool = False,
        skip_permissions: bool = False,
        timeout_seconds: int = 0,
        extra_dirs: list[Path] | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> AgyRunResult:
        """Execute a prompt via `agy` and stream updates to on_progress."""
        cmd = build_agy_command(
            self.agy_bin,
            prompt,
            conversation_id=conversation_id,
            mode=mode,
            model=model,
            sandbox=sandbox,
            skip_permissions=skip_permissions,
            timeout_seconds=timeout_seconds,
            extra_dirs=extra_dirs,
        )

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(workdir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=4 * 1024 * 1024,
        )
        self._active_procs[channel_id] = proc
        self._cancelled_channels.discard(channel_id)

        accumulated = ""
        final_text: str | None = None
        observed_conv_id = conversation_id
        duration_seconds: float | None = None
        error_msg: str | None = None
        last_usage: dict[str, int] | None = None
        last_context_tokens: int | None = None

        async def _read_stderr() -> str:
            if proc.stderr is None:
                return ""
            raw = await proc.stderr.read()
            return raw.decode("utf-8", errors="replace").strip()

        stderr_task = asyncio.create_task(_read_stderr())

        try:
            assert proc.stdout is not None
            while True:
                raw_line = await proc.stdout.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace")
                ev = parse_stream_event(line)
                if ev is None:
                    continue

                if ev.conversation_id:
                    observed_conv_id = ev.conversation_id

                if ev.event_type == "step_update":
                    if ev.usage:
                        last_usage = ev.usage
                        last_context_tokens = ev.usage.get("input_tokens", 0) + ev.usage.get(
                            "cache_read_tokens", 0
                        )
                    if ev.text_delta:
                        accumulated += ev.text_delta
                    if on_progress is not None:
                        status = _step_status_label(ev.step_type, ev.step_state)
                        await on_progress(accumulated, status)
                elif ev.event_type == "result":
                    if ev.usage:
                        last_usage = ev.usage
                    if ev.final_response is not None:
                        final_text = ev.final_response
                    duration_seconds = ev.duration_seconds
                    if ev.status and ev.status.upper() not in ("SUCCESS", "OK"):
                        error_msg = ev.error_message or f"agy finished with status: {ev.status}"
                elif ev.event_type == "error":
                    error_msg = ev.error_message or "Unknown agy stream error"

            exit_code = await proc.wait()
            stderr_text = await stderr_task
        finally:
            self._active_procs.pop(channel_id, None)
            if not stderr_task.done():
                stderr_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stderr_task

        cancelled = channel_id in self._cancelled_channels
        self._cancelled_channels.discard(channel_id)

        response_text = (final_text if final_text is not None else accumulated).strip()
        if exit_code != 0 and not cancelled and not error_msg:
            error_msg = stderr_text or f"agy exited with code {exit_code}"

        return AgyRunResult(
            response=response_text,
            conversation_id=observed_conv_id,
            duration_seconds=duration_seconds,
            exit_code=exit_code,
            error=error_msg,
            cancelled=cancelled,
            usage=last_usage,
            context_tokens=last_context_tokens,
        )

    async def fetch_models(
        self, *, use_cache: bool = True, ttl_seconds: float = 300.0
    ) -> list[tuple[str, str]]:
        """Return parsed `(model_id, display_label)` pairs from `agy models`."""
        now = time.monotonic()
        if use_cache and self._models_cache and (now - self._models_cache_ts) < ttl_seconds:
            return list(self._models_cache)

        proc = await asyncio.create_subprocess_exec(
            self.agy_bin,
            "models",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        raw = (stdout or stderr).decode("utf-8", errors="replace")
        parsed = parse_models_output(raw)
        if parsed:
            self._models_cache = parsed
            self._models_cache_ts = now
        return parsed

    async def list_models(self) -> str:
        """Return formatted output of `agy models`."""
        models = await self.fetch_models()
        if not models:
            return "(no models returned)"
        return "\n".join(f"{mid:<32} {label}" for mid, label in models)

    async def fetch_usage(self) -> dict[str, Any]:
        """Fetch quota/usage JSON from `agy -p /usage --output-format json`."""
        proc = await asyncio.create_subprocess_exec(
            self.agy_bin,
            "-p",
            "/usage",
            "--output-format",
            "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        raw = stdout.decode("utf-8", errors="replace").strip()
        if not raw:
            err = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(err or f"agy /usage exited with code {proc.returncode}")
        return json.loads(raw)
