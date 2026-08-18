"""Bundled launcher for detached persistent tasks.

This script is the *parent* of the real command. The runtime spawns it with
``sys.executable``; it runs the real command, redirects the command's combined
stdout/stderr to a log file, waits for it, and atomically records the exit code
to a status file so a restarted runtime can recover it (the runtime cannot
``waitpid`` a process it did not fork).

CLI::

    python _launcher.py --log <log_file> --status <status_file> \
        [--cwd <dir>] [--env K=V ...] -- <argv...>

Designed to be importable (``main(argv)``) so the status-writing logic can be
unit-tested without a subprocess.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def _parse_env(items: list[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"--env value must be K=V, got: {item!r}")
        key, value = item.split("=", 1)
        env[key] = value
    return env


def _write_status(status_file: Path, exit_code: int) -> None:
    """Atomically write the status JSON so readers never see a partial file."""
    status_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "exit_code": exit_code,
        "finished_at": datetime.now(timezone.utc).isoformat(),  # noqa: UP017 - remote py3.8
    }
    tmp = status_file.with_suffix(status_file.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, status_file)


def _install_signal_forwarder(proc: subprocess.Popen[bytes]) -> None:
    """Forward SIGTERM/SIGINT received by the launcher to the real command.

    On POSIX the command runs in the launcher's process group only if it did not
    detach further; targeting the process group is the safe superset.
    """

    def _forward(_signum: int, _frame: object) -> None:
        if proc.poll() is None:
            try:
                if os.name == "posix":
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)  # type: ignore[attr-defined]
                else:
                    proc.terminate()  # type: ignore[unreachable]
            except ProcessLookupError:
                pass

    sigterm = getattr(signal, "SIGTERM", None)
    sigint = getattr(signal, "SIGINT", None)
    if sigterm is not None:
        signal.signal(sigterm, _forward)
    if sigint is not None:
        signal.signal(sigint, _forward)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="_launcher", add_help=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("--status", required=True)
    parser.add_argument("--cwd", default=None)
    parser.add_argument("--env", action="append", default=[])
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    # argparse REMAINDER keeps a leading "--" if present; drop it.
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise SystemExit("launcher requires a command after --")

    log_file = Path(args.log)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update(_parse_env(args.env))

    # Append so reattached tails and reruns keep prior bytes; the runtime tails
    # from a persisted offset, so appending is correct and never double-reads.
    log_handle = log_file.open("ab")
    try:
        proc = subprocess.Popen(  # noqa: S603 - argv is caller-controlled (runtime)
            command,
            cwd=args.cwd,
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            # Start the command in its own group on POSIX so we can signal the
            # whole tree and so it is not tied to the launcher's controlling tty.
            start_new_session=(os.name == "posix"),
        )
    finally:
        # Keep log_handle open for the child's lifetime; close in parent after wait.
        pass

    _install_signal_forwarder(proc)
    exit_code = proc.wait()
    log_handle.close()
    _write_status(Path(args.status), exit_code)
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
