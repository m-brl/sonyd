"""IPC : socket Unix, une requête texte par ligne, une réponse par ligne.

    > status                 < ok: mode=nc voice=off level=20
    > anc ambient 15 voice   < ok: mode=ambient voice=on level=15
    > anc switch             < ok: mode=off voice=on level=15
    > pause off              < ok: off
    > foo                    < err: unknown command 'foo' …
"""

import asyncio
import contextlib
import logging
import os
import socket

from . import request
from .request import RequestError

log = logging.getLogger("sonyd.ipc")

HELP = ("commands: status | battery | firmware | anc <off|nc|ambient> [level 0-20] [voice|novoice]"
        " | anc switch | pause [on|off] | speak-to-chat [on|off] | poweroff")


class AlreadyRunning(RuntimeError):
    pass


def default_socket_path() -> str:
    if path := os.environ.get("SONYD_SOCKET"):
        return path
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    return os.path.join(runtime, "sonyd.sock") if runtime else f"/tmp/sonyd-{os.getuid()}.sock"


def _parse_anc_args(args: list[str]) -> tuple[str, int | None, bool | None]:
    if not args:
        raise RequestError("usage: anc <off|nc|ambient> [level 0-20] [voice|novoice] | anc switch")
    mode, level, voice = args[0], None, None
    for arg in args[1:]:
        if arg.isdigit():
            level = int(arg)
        elif arg in ("voice", "novoice"):
            voice = arg == "voice"
        else:
            raise RequestError(f"unknown argument '{arg}'")
    return mode, level, voice


async def dispatch(device, line: str) -> str:
    """Traduit une ligne de texte en appel au module request."""
    cmd, *args = line.split() or [""]
    match cmd:
        case "status":
            return str(await request.status(device))
        case "battery":
            return str(await request.battery(device))
        case "firmware":
            return await request.firmware(device)
        case "anc" if args[:1] == ["switch"]:
            if len(args) > 1:
                raise RequestError("anc switch takes no argument")
            return str(await request.switch_anc(device))
        case "anc":
            return str(await request.set_anc(device, *_parse_anc_args(args)))
        case "pause" | "speak-to-chat":
            match args:
                case []:
                    on = await request.get_toggle(device, cmd)
                case ["on" | "off" as value]:
                    on = await request.set_toggle(device, cmd, value == "on")
                case _:
                    raise RequestError(f"usage: {cmd} [on|off]")
            return "on" if on else "off"
        case "poweroff":
            if args:
                raise RequestError("poweroff takes no argument")
            return await request.poweroff(device)
        case "help" | "":
            return HELP
    raise RequestError(f"unknown command '{cmd}' — {HELP}")


class IpcServer:
    def __init__(self, device, path: str | None = None):
        self.device = device
        self.path = path or default_socket_path()

    async def serve_forever(self):
        self._remove_stale_socket()
        server = await asyncio.start_unix_server(self._handle_client, self.path)
        os.chmod(self.path, 0o600)
        log.info("listening on %s", self.path)
        try:
            async with server:
                await server.serve_forever()
        finally:
            with contextlib.suppress(FileNotFoundError):  # déjà supprimé par asyncio ≥ 3.13
                os.unlink(self.path)

    def _remove_stale_socket(self):
        if not os.path.exists(self.path):
            return
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            try:
                s.connect(self.path)
            except OSError:
                os.unlink(self.path)  # reste d'une instance précédente
                return
        raise AlreadyRunning(f"sonyd is already running on {self.path}")

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            while line := await reader.readline():
                text = line.decode(errors="replace").strip()
                log.debug("request: %s", text)
                try:
                    response = "ok: " + await dispatch(self.device, text)
                except Exception as e:
                    if not isinstance(e, RequestError):
                        log.warning("'%s' failed: %r", text, e)
                    response = f"err: {e or type(e).__name__}"
                writer.write(response.encode() + b"\n")
                await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()
