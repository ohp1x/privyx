"""``privyx mcp serve``: a local MCP server exposing detection, file masking, and git checks.

MCP's stdio transport is JSON-RPC, one message per line.  Each tool and prompt
is a use case of the app's services; this module speaks the protocol.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from privyx.version import __version__

if TYPE_CHECKING:
    from privyx.app import Privyx

_TOOLS = [
    {
        "name": "privyx_scan",
        "description": (
            "Find sensitive spans with the configured detector and policy, "
            "without returning their values."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "categories": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Entity types to keep, such as EMAIL (case-insensitive)",
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "privyx_sanitize_file",
        "description": (
            "Mask a UTF-8 file under the server's root, returning the result or "
            "writing a new file. Relative paths are resolved against the root."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "output_path": {"type": "string"},
                "format": {"type": "string", "enum": ["auto", "text", "json", "jsonl"]},
            },
            "required": ["path"],
        },
    },
    {
        "name": "privyx_precommit_check",
        "description": "Check added lines in a unified diff for secrets and placeholders.",
        "inputSchema": {
            "type": "object",
            "properties": {"git_diff": {"type": "string"}},
            "required": ["git_diff"],
        },
    },
]

#: Commands for the person at the client (Claude Code lists them as
#: `/privyx:status`).  None takes arguments: a client may send them on to the
#: model, so a prompt that masked or restored text would hand it over anyway.
_PROMPTS = [
    {
        "name": "status",
        "description": "Vault sessions and audit trail totals, never the masked values.",
    },
    {
        "name": "audit-tail",
        "description": "The latest audit events, which never hold the masked values.",
    },
]

#: Audit events `audit-tail` shows, as many as `privyx audit tail` does.
_TAIL = 10

#: Sessions `status` lists, most recently active first.
_RECENT = 5

#: Protocol versions this server speaks; a client asking for another gets the
#: newest.  2025-03-26 allows batches, which are answered.
_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")


class _ToolError(Exception):
    """A tool call the caller can correct; the message holds no file content."""


class _UnknownTool(Exception):
    pass


async def serve(app: Privyx, root: Path) -> None:
    """Answer MCP messages on stdin until it closes; the file tool reads and writes under ``root``.

    Raises:
        ConfigError: If the app cannot be built.
    """
    write, restore = _protocol_output()
    try:
        # Anything else printed, by a plugin as it loads say, would corrupt
        # the JSON-RPC stream: it goes to stderr.
        with contextlib.redirect_stdout(sys.stderr):
            # Opened once, for every call: a model-backed detector is slow to
            # load, and a broken config fails here rather than on every call.
            async with app.engine():
                while raw := sys.stdin.buffer.readline():
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    response = await _respond(line, app, root)
                    if response is not None:
                        write((_dumps(response) + "\n").encode())
    finally:
        restore()


def _dumps(response: Any) -> str:
    if isinstance(response, list):
        # Each answer of a batch on its own: one that cannot be written must
        # not take the others with it.
        return "[" + ", ".join(map(_dumps, response)) + "]"
    # ASCII: a lone surrogate the client sent cannot be encoded as UTF-8, its
    # escape can.
    try:
        # NaN and Infinity are not JSON, though the parser takes them as ids.
        return json.dumps(response, allow_nan=False)
    except (RecursionError, ValueError):
        # Too deep to echo (an id nested just under what the parser takes),
        # or not JSON.
        return json.dumps(_error(None, -32603, "Internal error"))


def _protocol_output() -> tuple[Callable[[bytes], None], Callable[[], None]]:
    """A writer for responses, and how to undo what it set up.

    A library writing below Python (a C extension, a child process) bypasses
    ``sys.stdout``: file descriptor 1 is pointed at stderr too, and responses go
    to a copy of the original.
    """
    try:
        fd, err = sys.stdout.fileno(), sys.stderr.fileno()
    except (AttributeError, OSError, ValueError):  # not a real file, as in tests
        out = sys.stdout.buffer

        def write_buffer(data: bytes) -> None:
            out.write(data)
            out.flush()

        return write_buffer, lambda: None
    sys.stdout.flush()
    protocol = os.dup(fd)
    os.dup2(err, fd)

    def write(data: bytes) -> None:
        view = memoryview(data)
        while view:
            view = view[os.write(protocol, view) :]

    def restore() -> None:
        os.dup2(protocol, fd)
        os.close(protocol)

    return write, restore


async def _respond(
    line: str, app: Privyx, root: Path
) -> dict[str, Any] | list[dict[str, Any]] | None:
    try:
        message = json.loads(line)
    except (ValueError, RecursionError):
        return _error(None, -32700, "Parse error")
    if not isinstance(message, list):
        return await _answer(message, app, root)
    if not message:
        return _error(None, -32600, "Invalid request")
    responses = [r for m in message if (r := await _answer(m, app, root))]
    return responses or None


async def _answer(message: Any, app: Privyx, root: Path) -> dict[str, Any] | None:
    try:
        return await _handle(message, app, root)
    except Exception as exc:
        # One bad message must not stop the server, nor leave its caller
        # waiting: the error carries the request's id.
        _log(type(exc).__name__)
        return _error(_id(message), -32603, "Internal error")


def _id(message: Any) -> Any:
    return message.get("id") if isinstance(message, dict) else None


async def _handle(message: Any, app: Privyx, root: Path) -> dict[str, Any] | None:
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(_id(message), -32600, "Invalid request")
    if "method" not in message and ("result" in message or "error" in message):
        return None  # a response: this server sends no requests
    method = message.get("method")
    if not isinstance(method, str):
        return _error(message.get("id"), -32600, "Invalid request")
    if "id" not in message:
        return None  # a notification
    request_id = message["id"]
    params = message.get("params", {})
    if not isinstance(params, dict):
        return _error(request_id, -32602, "Invalid params")
    if method == "initialize":
        version = params.get("protocolVersion")
        result: Any = {
            "protocolVersion": version if version in _VERSIONS else _VERSIONS[-1],
            "capabilities": {"tools": {}, "prompts": {}},
            "serverInfo": {"name": "privyx", "version": __version__},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": _TOOLS}
    elif method == "prompts/list":
        result = {"prompts": _PROMPTS}
    elif method == "prompts/get":
        name = params.get("name")
        if name == "status":
            text = await _status(app)
        elif name == "audit-tail":
            text = _audit_tail(app)
        else:
            return _error(request_id, -32602, "Unknown prompt")
        result = {"messages": [{"role": "user", "content": {"type": "text", "text": text}}]}
    elif method == "tools/call":
        try:
            result = await _tool_result(params, app, root)
        except _UnknownTool:
            return _error(request_id, -32602, "Unknown tool")
    else:
        return _error(request_id, -32601, "Method not found")
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


async def _tool_result(params: dict[str, Any], app: Privyx, root: Path) -> dict[str, Any]:
    name = params.get("name")
    if not isinstance(name, str) or name not in {tool["name"] for tool in _TOOLS}:
        raise _UnknownTool
    try:
        args = params.get("arguments", {})
        if not isinstance(args, dict):
            raise _ToolError("arguments must be an object")
        value = await _call(name, args, app, root)
        return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}
    except Exception as exc:
        message = _describe(exc)
        if message is None:
            # The exception's own message may quote what was being masked:
            # only its type is logged.
            _log(f"{name} failed: {type(exc).__name__}")
            message = "Tool failed; see the server's log."
        return {"content": [{"type": "text", "text": message}], "isError": True}


def _describe(exc: Exception) -> str | None:
    """What a caller can be told about ``exc``, or None when it may hold content."""
    if isinstance(exc, _ToolError):
        return str(exc)
    if isinstance(exc, FileNotFoundError):
        return "file not found"
    if isinstance(exc, FileExistsError):
        return "output file already exists"
    if isinstance(exc, UnicodeDecodeError):
        return "file is not UTF-8 text"
    if isinstance(exc, json.JSONDecodeError):
        return "file is not valid JSON"
    if isinstance(exc, OSError):
        return f"cannot access the file: {exc.strerror or type(exc).__name__}"
    return None


async def _call(name: str, args: dict[str, Any], app: Privyx, root: Path) -> dict[str, Any]:
    if name == "privyx_scan":
        text = args.get("text")
        if not isinstance(text, str):
            raise _ToolError("text must be a string")
        categories = args.get("categories")
        if categories is not None and (
            not isinstance(categories, list)
            or not categories
            or not all(isinstance(c, str) for c in categories)
        ):
            raise _ToolError("categories must be a non-empty array of strings")
        # The policy too, as `privyx detect` does: what is reported is what
        # privyx_sanitize_file masks.
        found = await app.masking.detect(text, categories=categories)
        response: dict[str, Any] = {}
        if found.filtered_out is not None:
            response["filtered_out"] = found.filtered_out
        response["findings"] = [
            {"start": f.start, "end": f.end, "category": f.category} for f in found.findings
        ]
        return response
    if name == "privyx_sanitize_file":
        import asyncio

        path, output = args.get("path"), args.get("output_path")
        fmt = args.get("format", "auto")
        if not isinstance(path, str) or (output is not None and not isinstance(output, str)):
            raise _ToolError("path and output_path must be strings")
        if fmt not in ("auto", "text", "json", "jsonl"):
            raise _ToolError("format must be auto, text, json, or jsonl")
        source_path = _inside(root, path)
        output_path = _inside(root, output) if output else None
        if not source_path.exists():
            raise FileNotFoundError
        if not source_path.is_file():
            # A FIFO or a device would block the server, or never end.
            raise _ToolError("path is not a regular file")
        # Before masking: a call that cannot write leaves no session behind.
        if output_path is not None:
            # `_inside` followed every link, so an existing name is the check.
            if output_path.exists():
                raise FileExistsError
            if not output_path.parent.is_dir():
                raise _ToolError("output directory does not exist")
        source = await asyncio.to_thread(source_path.read_text, encoding="utf-8")

        async def write(text: str) -> None:
            assert output_path is not None
            await asyncio.to_thread(_write_new, output_path, text)

        masked = await app.masking.mask_document(
            source,
            fmt=app.masking.format_of(fmt, path),
            write=write if output_path is not None else None,
        )
        response = {"output_path": output} if output else {"text": masked.text}
        if masked.session_id is not None:
            response["session_id"] = masked.session_id
        else:
            response["warning"] = (
                "The memory vault mapping is lost after this call and cannot be restored."
            )
        return response
    diff = args.get("git_diff")
    if not isinstance(diff, str):
        raise _ToolError("git_diff must be a string")
    findings = [
        {"path": f.path, "line": f.line, "category": f.what.split(" ", 1)[0]}
        for f in app.workspace.check_diff(diff)
    ]
    return {"ok": not findings, "findings": findings}


async def _status(app: Privyx) -> str:
    from privyx.cli.commands.audit import _breakdown
    from privyx.cli.commands.session import _ts
    from privyx.observability.metrics import AuditStats

    lines = ["Privyx status, which never holds the masked values:", ""]
    vault = app.sessions.vault_type
    if vault == "memory":
        lines.append(
            "Vault: memory, which each privyx process keeps to itself: "
            "the proxy's sessions are not listed"
        )
    else:
        try:
            sessions = await app.sessions.list()
        except Exception as exc:
            # Only the type: the message may quote the vault's DSN, password
            # included.
            lines.append(f"Vault: {vault}, sessions cannot be listed ({type(exc).__name__})")
        else:
            mappings = sum(len(s.mapping) for s in sessions)
            lines.append(f"Vault: {vault}, {len(sessions)} session(s), {mappings} mapping(s)")
            lines += [
                f"  {s.session_id}  last active {_ts(s.updated_at)}  {len(s.mapping)} mapping(s)"
                for s in sessions[:_RECENT]
            ]
            if len(sessions) > _RECENT:
                lines.append(f"  and {len(sessions) - _RECENT} more")
    if not app.audit.enabled:
        lines.append("Audit trail: off (audit.enabled is false)")
        return "\n".join(lines)
    try:
        found = app.audit.totals()
        totals, skipped = found.stats, found.skipped
    except FileNotFoundError:
        totals, skipped = AuditStats(), 0
    except OSError as exc:
        lines.append(f"Audit trail: cannot be read ({exc.strerror or type(exc).__name__})")
        return "\n".join(lines)
    events = totals.events
    if not events:
        lines.append("Audit trail: no events yet")
    else:
        period = ""
        if totals.first_ts is not None and totals.last_ts is not None:
            period = f", {_ts(totals.first_ts)} to {_ts(totals.last_ts)}"
        lines += [
            f"Audit trail: {events.total()} event(s){period}",
            f"  Requests: {events['proxy.request']}, "
            f"errors: {events['proxy.error']}{_breakdown(totals.errors)}",
            f"  Masked: {totals.entities.total()} entities{_breakdown(totals.entities)}",
            f"  Restored: {totals.restored} pseudonyms",
        ]
    if skipped:
        lines.append(f"  Skipped {skipped} unreadable line(s)")
    return "\n".join(lines)


def _audit_tail(app: Privyx) -> str:
    from collections import deque

    from privyx.cli.commands.audit import _line

    if not app.audit.enabled:
        return "The Privyx audit trail is off (audit.enabled is false)."
    recent: deque[str] = deque(maxlen=_TAIL)
    try:
        for record in app.audit.records():
            # A line that is not a record is left out, not shown as written.
            if record is not None:
                with contextlib.suppress(Exception):
                    recent.append(_line(record))
    except FileNotFoundError:
        pass
    except OSError as exc:
        return f"The Privyx audit trail cannot be read ({exc.strerror or type(exc).__name__})."
    if not recent:
        return "No Privyx audit events yet."
    return "The latest Privyx audit events, which never hold the masked values:\n\n" + "\n".join(
        recent
    )


def _inside(root: Path, path: str) -> Path:
    """``path`` resolved against ``root``; one outside it, links followed, is refused."""
    try:
        resolved = (root / Path(path).expanduser()).resolve()
    except (OSError, RuntimeError, ValueError):  # a link loop before 3.13, a NUL byte
        raise _ToolError("invalid path") from None
    if not resolved.is_relative_to(root):
        raise _ToolError("path is outside the server's root")
    try:
        resolved.stat()
    except OSError as exc:
        # From 3.13 `resolve` keeps a link loop as it is instead of raising.
        if exc.errno == errno.ELOOP:
            raise _ToolError("invalid path") from None
    return resolved


def _write_new(path: Path, text: str) -> None:
    f = path.open("x", encoding="utf-8")
    try:
        with f:
            f.write(text)
    except BaseException:
        # A partial file would block a retry and hold tokens nobody can
        # restore.
        path.unlink(missing_ok=True)
        raise


def _log(message: str) -> None:
    """A line for the server's log: stderr, which the client shows or keeps."""
    print(f"privyx mcp serve: {message}", file=sys.stderr, flush=True)
