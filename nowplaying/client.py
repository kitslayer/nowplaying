"""Client helpers for talking to the daemon over its unix socket."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator

from . import config
from .state import State


def is_running() -> bool:
    sock = config.socket_path()
    if not sock.exists():
        return False
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(str(sock))
        return True
    except OSError:
        return False
    finally:
        s.close()


def spawn_daemon(source: str = "auto") -> bool:
    """Start the daemon detached, and wait briefly for its socket."""
    cmd = [sys.executable, "-m", "nowplaying", "daemon", "--source", source]
    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    for _ in range(50):
        if is_running():
            return True
        time.sleep(0.1)
    return False


def connect(autostart: bool = True, source: str = "auto") -> socket.socket:
    sock_path = str(config.socket_path())
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(sock_path)
        return s
    except OSError:
        s.close()
        if not autostart:
            raise
    if not spawn_daemon(source):
        raise ConnectionError("could not start the nowplaying daemon")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(sock_path)
    return s


def stream(sock: socket.socket) -> Iterator[State]:
    """Yield a State for every update the daemon pushes."""
    buf = b""
    with sock:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    yield State.from_dict(json.loads(line))
                except (ValueError, TypeError):
                    continue


def get_once(autostart: bool = True, source: str = "auto") -> State | None:
    sock = connect(autostart=autostart, source=source)
    for state in stream(sock):
        return state
    return None
